"""
Download phase: for each clipping with status='pending', download the image
and OCR text from the article page, then mark it 'downloaded'.

OCR is extracted from JSON-LD structured data embedded in the article page HTML.
This requires cf_clearance in cookies.txt (see README for how to export it).

Features:
- Polite random delays between requests
- Up to 3 retries per clipping on transient errors
- Detects session expiry and raises a clear error
- Optional --limit for testing a small batch first
"""
import html as html_lib
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session
from tqdm import tqdm

from .auth import build_requests_session
from .database import Clipping, get_engine, init_db

log = logging.getLogger(__name__)

IMAGES_DIR = Path(os.environ.get("IMAGES_DIR", "images"))
MAX_RETRIES = 3
MIN_DELAY = 1.5
MAX_DELAY = 3.5

_ARTICLE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.newspapers.com/clippings/",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
}


def _polite_sleep():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def _download_image(session: requests.Session, image_url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = session.get(image_url, timeout=60, stream=True)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)


def _fetch_ocr_from_article(session: requests.Session, clipping_url: str) -> str | None:
    """
    Fetch the article page and extract OCR text from its JSON-LD block.
    The text field in application/ld+json contains the full clipping OCR.
    """
    if not clipping_url:
        return None
    try:
        resp = session.get(clipping_url, timeout=30, headers=_ARTICLE_HEADERS)
        if resp.status_code in (401, 403):
            raise RuntimeError(
                f"Cloudflare blocked article page ({resp.status_code}). "
                "Re-export cookies.txt (make sure cf_clearance is included)."
            )
        if resp.status_code != 200:
            return None

        # OCR lives in a JSON-LD <script> block as the "text" field
        ld_blocks = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            resp.text,
            re.DOTALL,
        )
        for block in ld_blocks:
            try:
                data = json.loads(block)
                if isinstance(data, dict) and data.get("text"):
                    return html_lib.unescape(data["text"])
            except Exception:
                continue
    except RuntimeError:
        raise
    except Exception as exc:
        log.debug("OCR fetch error for %s: %s", clipping_url, exc)
    return None


def _process_clipping(
    session: requests.Session,
    clipping: Clipping,
    images_dir: Path,
) -> tuple[bool, str, str | None]:
    """
    Download image and fetch OCR for one clipping.
    Returns (success, error_message, ocr_text).
    """
    ext = ".jpg"
    if clipping.image_url:
        suffix = Path(clipping.image_url.split("?")[0]).suffix.lower()
        if suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            ext = suffix
    image_path = images_dir / f"{clipping.id}{ext}"

    # Download image
    if not image_path.exists():
        if not clipping.image_url:
            return False, "no image_url in database", None
        try:
            _download_image(session, clipping.image_url, image_path)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            if code in (401, 403):
                raise RuntimeError(
                    f"Session expired or access denied ({code}). "
                    "Re-export cookies.txt and restart."
                ) from exc
            return False, f"image HTTP {code}", None
        except Exception as exc:
            return False, str(exc), None

    # Fetch OCR from article page (best-effort, don't fail the whole clipping)
    ocr_text = clipping.ocr_text  # already stored from a previous run
    if not ocr_text and clipping.clipping_url:
        try:
            ocr_text = _fetch_ocr_from_article(session, clipping.clipping_url)
        except RuntimeError:
            raise
        except Exception as exc:
            log.debug("OCR error for clipping %s: %s", clipping.id, exc)

    return True, "", ocr_text


def download(
    cookies_file: str = "cookies.txt",
    database_url: str | None = None,
    images_dir: str | Path = IMAGES_DIR,
    limit: int | None = None,
) -> tuple[int, int]:
    """
    Download images and OCR for all pending clippings.
    Returns (downloaded_count, failed_count).
    """
    engine = init_db(get_engine(database_url))
    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)

    session = build_requests_session(cookies_file)
    session.headers["Referer"] = "https://www.newspapers.com/"

    with Session(engine) as db_session:
        query = select(Clipping).where(Clipping.status == "pending")
        if limit:
            query = query.limit(limit)
        pending = db_session.execute(query).scalars().all()

    log.info("Found %d pending clippings to download.", len(pending))
    downloaded = 0
    failed = 0

    for clipping in tqdm(pending, desc="Downloading", unit="clip"):
        success = False
        last_error = ""
        ocr_text = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                success, last_error, ocr_text = _process_clipping(
                    session, clipping, images_dir
                )
                if success:
                    break
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = str(exc)
                log.warning("Attempt %d/%d failed for %s: %s",
                            attempt, MAX_RETRIES, clipping.id, exc)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

        with Session(engine) as db_session:
            row = db_session.get(Clipping, clipping.id)
            if row is None:
                continue
            if success:
                ext = ".jpg"
                if clipping.image_url:
                    suffix = Path(clipping.image_url.split("?")[0]).suffix.lower()
                    if suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                        ext = suffix
                row.image_path = str(images_dir / f"{clipping.id}{ext}")
                row.ocr_text = ocr_text
                row.scraped_at = datetime.now(timezone.utc)
                row.status = "downloaded"
                downloaded += 1
            else:
                log.error("Failed clipping %s: %s", clipping.id, last_error)
                row.status = "failed"
                failed += 1
            db_session.commit()

        _polite_sleep()

    log.info("Done. Downloaded: %d  Failed: %d", downloaded, failed)
    return downloaded, failed

"""
Discovery phase: paginate the newspapers.com private clippings API using
cursor-based pagination and upsert each clipping into the database.

No Playwright needed — the API is accessible directly with session cookies.

API endpoint: GET https://www.newspapers.com/api/clipping/list
  params:
    user={username}      (filters to just that user's clippings)
    count=24             (max page size the API allows)
    cursor_mark=*        (start cursor; subsequent pages use next_cursor_mark)
    product_id=1
  response:
    { clippings: [...], next_cursor_mark: "...", more_results: true/false }
"""
import logging
import time

from sqlalchemy.orm import Session

from .auth import build_requests_session
from .database import get_engine, init_db, upsert_clipping

log = logging.getLogger(__name__)

API_URL = "https://www.newspapers.com/api/clipping/list"
PAGE_SIZE = 24


def _build_highres_url(raw: dict) -> str | None:
    """
    Build the full-resolution image URL from API response fields.
    Pattern: https://img.newspapers.com/img/img?user={user_id}&id={page_id}
             &clippingId={clipping_id}&width={w}&height={h}
             &crop={x}_{y}_{w}_{h}&rotation={rot}
    Falls back to thumbnail_url if required fields are missing.
    """
    try:
        rect = raw.get("rectangle") or {}
        user_id = raw["user_id"]
        page_id = raw["page_id"]
        clipping_id = raw["clipping_id"]
        x = int(rect["x"])
        y = int(rect["y"])
        w = int(rect["width"])
        h = int(rect["height"])
        rot = int(rect.get("rotation") or 0)
        return (
            f"https://img.newspapers.com/img/img"
            f"?user={user_id}&id={page_id}&clippingId={clipping_id}"
            f"&width={w}&height={h}&crop={x}_{y}_{w}_{h}&rotation={rot}"
        )
    except (KeyError, TypeError, ValueError):
        return raw.get("thumbnail_url")


def _parse_clipping(raw: dict) -> dict:
    """Map the API response fields to our DB column names."""
    pub = raw.get("publication") or {}

    clipping_id = str(raw.get("clipping_id", ""))
    if not clipping_id:
        return {}

    path = raw.get("url", "")
    clipping_url = f"https://www.newspapers.com{path}" if path.startswith("/") else path

    return {
        "id": clipping_id,
        "title": raw.get("title"),
        "newspaper_name": pub.get("name"),
        "publication_date": raw.get("page_date"),
        "author": None,
        "page_number": raw.get("page_title"),
        "clipping_url": clipping_url,
        "image_url": _build_highres_url(raw),    # full-res from img.newspapers.com
        "ocr_text": raw.get("body"),             # usually None; populated if available
        "notes": None,
        "raw_metadata": raw,
        "status": "pending",
    }


def discover(
    cookies_file: str = "cookies.txt",
    database_url: str | None = None,
    username: str = "",
) -> int:
    """
    Discover all saved clippings for `username` and upsert them into the database.
    Returns the count of clippings found.
    """
    if not username:
        raise ValueError("username is required. Pass --username to the CLI.")

    engine = init_db(get_engine(database_url))
    session = build_requests_session(cookies_file)

    total = 0
    cursor_mark = "*"
    page = 0

    while True:
        page += 1
        params = {
            "user": username,
            "count": PAGE_SIZE,
            "cursor_mark": cursor_mark,
            "product_id": 1,
        }

        log.info("Fetching page %d (cursor: %s...)", page, cursor_mark[:20])
        resp = session.get(API_URL, params=params, timeout=30)

        if resp.status_code == 401:
            raise RuntimeError(
                "Session expired (401). Re-export your cookies.txt and try again."
            )
        resp.raise_for_status()
        data = resp.json()

        clippings = data.get("clippings", [])
        if not clippings:
            log.info("No clippings on page %d — done.", page)
            break

        with Session(engine) as db_session:
            for raw in clippings:
                parsed = _parse_clipping(raw)
                if parsed:
                    upsert_clipping(db_session, parsed)
            db_session.commit()

        total += len(clippings)
        log.info("  Page %d: %d clippings (total so far: %d)", page, len(clippings), total)

        if not data.get("more_results"):
            log.info("No more pages.")
            break

        next_cursor = str(data.get("next_cursor_mark", ""))
        if not next_cursor or next_cursor == cursor_mark:
            log.info("Cursor did not advance — done.")
            break
        cursor_mark = next_cursor

        time.sleep(0.5)

    log.info("Discovery complete. %d total clippings upserted.", total)
    return total

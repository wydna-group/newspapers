"""
Simple Flask viewer for newspapers.com clippings.
Run: python webapp/app.py
"""
import math
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, send_from_directory, url_for
from sqlalchemy import or_, select, func
from sqlalchemy.orm import Session

load_dotenv()

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from scraper.database import Clipping, get_engine, init_db

app = Flask(__name__)
engine = init_db(get_engine())

IMAGES_DIR = Path(os.environ.get("IMAGES_DIR", "images")).resolve()
PER_PAGE = 48


def _db():
    return Session(engine)


@app.route("/images/<path:filename>")
def serve_image(filename):
    return send_from_directory(IMAGES_DIR, filename)


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    newspaper = request.args.get("newspaper", "").strip()
    page = max(1, int(request.args.get("page", 1)))

    with _db() as db:
        stmt = select(Clipping).where(Clipping.status == "downloaded")

        if q:
            stmt = stmt.where(
                or_(
                    Clipping.title.ilike(f"%{q}%"),
                    Clipping.ocr_text.ilike(f"%{q}%"),
                    Clipping.newspaper_name.ilike(f"%{q}%"),
                )
            )
        if newspaper:
            stmt = stmt.where(Clipping.newspaper_name == newspaper)

        total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar()
        stmt = stmt.order_by(Clipping.publication_date.desc().nullslast()).offset((page - 1) * PER_PAGE).limit(PER_PAGE)
        clippings = db.execute(stmt).scalars().all()

        # Newspaper list for filter dropdown
        newspapers = db.execute(
            select(Clipping.newspaper_name)
            .where(Clipping.status == "downloaded", Clipping.newspaper_name.isnot(None))
            .group_by(Clipping.newspaper_name)
            .order_by(Clipping.newspaper_name)
        ).scalars().all()

    total_pages = max(1, math.ceil(total / PER_PAGE))
    return render_template(
        "index.html",
        clippings=clippings,
        q=q,
        newspaper=newspaper,
        newspapers=newspapers,
        page=page,
        total=total,
        total_pages=total_pages,
    )


def _parse_page(s):
    """Extract integer from 'Page 5' or '5' or '5A' etc. Returns None if unparseable."""
    if not s:
        return None
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def _story_confidence(page_nums):
    """
    Given a list of integer page numbers for a group, return
    'consecutive', 'nearby', or 'scattered'.
    """
    nums = sorted(p for p in page_nums if p is not None)
    if len(nums) < 2:
        return "single"
    span = nums[-1] - nums[0]
    # Allow for jump-pages (story starts p1, continues p18)
    # Treat as consecutive if span <= count + 1 (i.e. pages run together)
    if span <= len(nums):
        return "consecutive"
    if span <= 10:
        return "nearby"
    return "scattered"


@app.route("/stories")
def stories():
    q = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = 30

    with _db() as db:
        # All downloaded clippings, grouped by newspaper+date
        stmt = (
            select(Clipping)
            .where(Clipping.status == "downloaded")
        )
        if q:
            stmt = stmt.where(
                or_(
                    Clipping.newspaper_name.ilike(f"%{q}%"),
                    Clipping.title.ilike(f"%{q}%"),
                    Clipping.ocr_text.ilike(f"%{q}%"),
                )
            )
        all_clips = db.execute(stmt).scalars().all()

    # Group by (newspaper, date) in Python
    from collections import defaultdict
    groups = defaultdict(list)
    for c in all_clips:
        key = (c.newspaper_name or "", str(c.publication_date or ""))
        groups[key].append(c)

    # Sort each group by page number
    for key in groups:
        groups[key].sort(key=lambda c: (_parse_page(c.page_number) or 9999, c.id))

    # Build group summaries, filter to multi-clip groups
    group_list = []
    for (newspaper, date_str), clips in groups.items():
        page_nums = [_parse_page(c.page_number) for c in clips]
        confidence = _story_confidence(page_nums)
        group_list.append({
            "newspaper": newspaper,
            "date_str": date_str,
            "clips": clips,
            "count": len(clips),
            "confidence": confidence,
            "pages_display": ", ".join(
                c.page_number or "?" for c in clips
            ),
        })

    # Sort: multi-clip groups first, then by date desc
    group_list.sort(key=lambda g: (-g["count"], g["date_str"]))

    total = len(group_list)
    total_pages = max(1, math.ceil(total / per_page))
    paged = group_list[(page - 1) * per_page: page * per_page]

    return render_template(
        "stories.html",
        groups=paged,
        q=q,
        page=page,
        total=total,
        total_pages=total_pages,
    )


@app.route("/stories/<path:story_key>")
def story_detail(story_key):
    # story_key = "newspaper|||YYYY-MM-DD"
    parts = story_key.split("|||")
    if len(parts) != 2:
        abort(404)
    newspaper, date_str = parts

    from datetime import date as date_type
    with _db() as db:
        stmt = select(Clipping).where(Clipping.status == "downloaded")
        if newspaper:
            stmt = stmt.where(Clipping.newspaper_name == newspaper)
        else:
            stmt = stmt.where(Clipping.newspaper_name.is_(None))
        if date_str:
            try:
                d = date_type.fromisoformat(date_str)
                stmt = stmt.where(Clipping.publication_date == d)
            except ValueError:
                stmt = stmt.where(Clipping.publication_date.is_(None))
        else:
            stmt = stmt.where(Clipping.publication_date.is_(None))
        clips = db.execute(stmt).scalars().all()

    clips = sorted(clips, key=lambda c: (_parse_page(c.page_number) or 9999, c.id))
    page_nums = [_parse_page(c.page_number) for c in clips]
    confidence = _story_confidence(page_nums)

    return render_template(
        "story_detail.html",
        clips=clips,
        newspaper=newspaper,
        date_str=date_str,
        confidence=confidence,
    )


@app.route("/clipping/<clipping_id>")
def detail(clipping_id):
    with _db() as db:
        clipping = db.get(Clipping, clipping_id)
    if clipping is None:
        abort(404)
    return render_template("detail.html", clipping=clipping)


@app.route("/clipping/<clipping_id>/tei")
def tei_download(clipping_id):
    with _db() as db:
        clipping = db.get(Clipping, clipping_id)
    if clipping is None or not clipping.tei_xml:
        abort(404)
    from flask import Response
    return Response(
        clipping.tei_xml,
        mimetype="application/xml",
        headers={"Content-Disposition": f"attachment; filename={clipping_id}.xml"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)

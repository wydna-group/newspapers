"""
Database module: SQLAlchemy model, engine setup, and upsert helpers.
"""
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import (
    Column, Date, Index, String, Text, TIMESTAMP, create_engine
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Session

load_dotenv()


class Base(DeclarativeBase):
    pass


class Clipping(Base):
    __tablename__ = "clippings"

    id = Column(String, primary_key=True)          # newspapers.com clipping ID
    title = Column(Text)                            # clipping title / user label
    newspaper_name = Column(Text)
    publication_date = Column(Date)
    author = Column(Text)
    page_number = Column(Text)
    clipping_url = Column(Text)                     # canonical URL on site
    image_url = Column(Text)                        # remote image URL
    image_path = Column(Text)                       # local path: images/{id}.jpg
    ocr_text = Column(Text)
    notes = Column(Text)                            # user's notes on the clipping
    tei_xml = Column(Text)                          # TEI P5 encoded XML
    raw_metadata = Column(JSONB)                    # full API response blob
    scraped_at = Column(TIMESTAMP(timezone=True))
    status = Column(String, default="pending")      # pending | downloaded | failed

    __table_args__ = (
        Index("ix_clippings_newspaper_name", "newspaper_name"),
        Index("ix_clippings_publication_date", "publication_date"),
        Index("ix_clippings_status", "status"),
    )


def get_engine(database_url: str | None = None):
    url = database_url or os.environ["DATABASE_URL"]
    return create_engine(url, pool_pre_ping=True)


def init_db(engine=None):
    """Create all tables if they don't exist."""
    if engine is None:
        engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


def upsert_clipping(session: Session, data: dict) -> Clipping:
    """
    Insert or update a clipping row.
    `data` keys should match Clipping column names.
    Only updates fields that are present in `data` (won't overwrite
    existing image_path/ocr_text/status if not provided).
    """
    clipping = session.get(Clipping, data["id"])
    if clipping is None:
        clipping = Clipping(**{k: v for k, v in data.items() if hasattr(Clipping, k)})
        session.add(clipping)
    else:
        for key, value in data.items():
            if hasattr(Clipping, key) and value is not None:
                setattr(clipping, key, value)
    return clipping


def get_status_counts(session: Session) -> dict:
    """Return counts keyed by status value."""
    from sqlalchemy import func, select
    rows = session.execute(
        select(Clipping.status, func.count().label("n"))
        .group_by(Clipping.status)
    ).all()
    return {row.status: row.n for row in rows}

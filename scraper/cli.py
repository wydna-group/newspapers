"""
CLI entry point.

Usage:
    python -m scraper discover --username wydna
    python -m scraper download
    python -m scraper download --limit 5
    python -m scraper run --username wydna
    python -m scraper status
    python -m scraper retry-failed
"""
import logging
import os
import sys

import click
from dotenv import load_dotenv
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .database import Clipping, get_engine, get_status_counts, init_db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def _db_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        click.echo(
            "ERROR: DATABASE_URL not set.\n"
            "Copy .env.example to .env and fill in your PostgreSQL connection string.",
            err=True,
        )
        sys.exit(1)
    return url


def _cookies_file(ctx_obj):
    path = ctx_obj.get("cookies_file", "cookies.txt")
    if not os.path.exists(path):
        click.echo(
            f"ERROR: Cookie file not found: {path}\n"
            "Export your newspapers.com cookies using the\n"
            "'Get cookies.txt LOCALLY' browser extension and save as cookies.txt",
            err=True,
        )
        sys.exit(1)
    return path


@click.group()
@click.option(
    "--cookies",
    default="cookies.txt",
    show_default=True,
    envvar="COOKIES_FILE",
    help="Path to Netscape-format cookies.txt",
)
@click.pass_context
def cli(ctx, cookies):
    ctx.ensure_object(dict)
    ctx.obj["cookies_file"] = cookies


@cli.command()
@click.option(
    "--username", required=True, envvar="NCOM_USERNAME",
    help="Your newspapers.com username (shown on My Clippings tab)",
)
@click.pass_context
def discover(ctx, username):
    """Phase 1: find all YOUR saved clippings and store metadata in the database."""
    from .discover import discover as _discover
    cookies = _cookies_file(ctx.obj)
    count = _discover(
        cookies_file=cookies,
        database_url=_db_url(),
        username=username,
    )
    click.echo(f"Discovery complete. {count} clippings upserted.")


@cli.command()
@click.option("--limit", default=None, type=int,
              help="Only download this many clippings (useful for testing)")
@click.option(
    "--images-dir",
    default=os.environ.get("IMAGES_DIR", "images"),
    show_default=True,
    help="Directory to save downloaded images",
)
@click.pass_context
def download(ctx, limit, images_dir):
    """Phase 2: download images for all pending clippings."""
    from .download import download as _download
    cookies = _cookies_file(ctx.obj)
    dl, failed = _download(
        cookies_file=cookies,
        database_url=_db_url(),
        images_dir=images_dir,
        limit=limit,
    )
    click.echo(f"Download complete. Downloaded: {dl}  Failed: {failed}")


@cli.command()
@click.option(
    "--username", required=True, envvar="NCOM_USERNAME",
    help="Your newspapers.com username",
)
@click.option(
    "--images-dir",
    default=os.environ.get("IMAGES_DIR", "images"),
    show_default=True,
)
@click.pass_context
def run(ctx, username, images_dir):
    """Run both discovery and download phases in sequence."""
    from .discover import discover as _discover
    from .download import download as _download
    cookies = _cookies_file(ctx.obj)
    db_url = _db_url()

    click.echo("=== Phase 1: Discovery ===")
    count = _discover(cookies_file=cookies, database_url=db_url, username=username)
    click.echo(f"Discovered {count} clippings.\n")

    click.echo("=== Phase 2: Download ===")
    dl, failed = _download(
        cookies_file=cookies,
        database_url=db_url,
        images_dir=images_dir,
    )
    click.echo(f"Downloaded: {dl}  Failed: {failed}")


@cli.command()
def status():
    """Show counts of clippings by status (pending / downloaded / failed)."""
    engine = init_db(get_engine(_db_url()))
    with Session(engine) as db_session:
        counts = get_status_counts(db_session)

    total = sum(counts.values())
    click.echo(f"{'Status':<15} {'Count':>8}")
    click.echo("-" * 25)
    for s in ("downloaded", "pending", "failed"):
        click.echo(f"{s:<15} {counts.get(s, 0):>8}")
    for k, v in counts.items():
        if k not in ("downloaded", "pending", "failed"):
            click.echo(f"{k:<15} {v:>8}")
    click.echo("-" * 25)
    click.echo(f"{'TOTAL':<15} {total:>8}")


@cli.command("encode-tei")
@click.option("--limit", default=None, type=int,
              help="Only encode this many clippings (useful for testing)")
@click.option("--reprocess", is_flag=True, default=False,
              help="Re-encode clippings that already have TEI XML")
def encode_tei(limit, reprocess):
    """Encode TEI XML for all downloaded clippings with OCR text."""
    from .tei_encode import encode_all
    encoded, skipped = encode_all(
        database_url=_db_url(),
        limit=limit,
        reprocess=reprocess,
    )
    click.echo(f"TEI encoding complete. Encoded: {encoded}  Skipped (no OCR): {skipped}")


@cli.command("fix-image-urls")
def fix_image_urls():
    """Backfill high-res image_url for all rows using stored raw_metadata."""
    from .discover import _build_highres_url
    engine = init_db(get_engine(_db_url()))
    updated = 0
    skipped = 0
    with Session(engine) as db_session:
        rows = db_session.execute(select(Clipping)).scalars().all()
        for row in rows:
            if not row.raw_metadata:
                skipped += 1
                continue
            new_url = _build_highres_url(row.raw_metadata)
            if new_url and new_url != row.image_url:
                row.image_url = new_url
                updated += 1
        db_session.commit()
    click.echo(f"Updated {updated} image URLs. Skipped {skipped} rows without metadata.")


@cli.command("retry-failed")
@click.option(
    "--images-dir",
    default=os.environ.get("IMAGES_DIR", "images"),
    show_default=True,
)
@click.pass_context
def retry_failed(ctx, images_dir):
    """Reset all failed clippings to pending, then re-run the download phase."""
    from .download import download as _download
    engine = init_db(get_engine(_db_url()))
    with Session(engine) as db_session:
        result = db_session.execute(
            update(Clipping).where(Clipping.status == "failed").values(status="pending")
        )
        db_session.commit()
        click.echo(f"Reset {result.rowcount} failed clippings to pending.")

    cookies = _cookies_file(ctx.obj)
    dl, failed = _download(
        cookies_file=cookies,
        database_url=_db_url(),
        images_dir=images_dir,
    )
    click.echo(f"Retry complete. Downloaded: {dl}  Failed: {failed}")

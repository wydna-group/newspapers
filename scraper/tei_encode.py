"""
TEI encoding for newspaper clippings.

Runs spaCy NER over each clipping's OCR text and produces a TEI P5 XML document
with named entities tagged as:
  PERSON    → <persName>
  ORG       → <orgName>
  GPE/LOC   → <placeName>
  FAC       → <placeName type="facility">
  DATE      → <date>
  TIME      → <time>
  NORP      → <name type="norp">   (nationalities, religions, political groups)
  EVENT     → <name type="event">
  WORK_OF_ART → <title>
  LAW       → <name type="law">

The result is stored in the clipping's tei_xml column.
"""
import logging
import re
from datetime import timezone, datetime
from pathlib import Path

from lxml import etree
from sqlalchemy import select
from sqlalchemy.orm import Session
from tqdm import tqdm

from .database import Clipping, get_engine, init_db

log = logging.getLogger(__name__)

TEI_NS = "http://www.tei-c.org/ns/1.0"
T = f"{{{TEI_NS}}}"  # namespace prefix for lxml

# spaCy label → (TEI element name, extra attribs)
_LABEL_MAP: dict[str, tuple[str, dict]] = {
    "PERSON":      ("persName",  {}),
    "ORG":         ("orgName",   {}),
    "GPE":         ("placeName", {}),
    "LOC":         ("placeName", {"type": "natural"}),
    "FAC":         ("placeName", {"type": "facility"}),
    "DATE":        ("date",      {}),
    "TIME":        ("time",      {}),
    "NORP":        ("name",      {"type": "norp"}),
    "EVENT":       ("name",      {"type": "event"}),
    "WORK_OF_ART": ("title",     {}),
    "LAW":         ("name",      {"type": "law"}),
}


def _load_nlp():
    import spacy
    try:
        return spacy.load("en_core_web_lg")
    except OSError:
        raise RuntimeError(
            "spaCy model not found. Run: "
            ".venv/Scripts/python -m spacy download en_core_web_lg"
        )


def _fill_mixed_content(parent_el, text: str, ents):
    """
    Populate *parent_el* with alternating plain text and entity sub-elements.
    Entities that map to an unknown label are skipped (left as plain text).
    """
    pos = 0
    for ent in ents:
        mapping = _LABEL_MAP.get(ent.label_)
        if mapping is None:
            continue
        tag_name, attribs = mapping

        # text before this entity
        before = text[pos:ent.start_char]
        if before:
            if len(parent_el):
                parent_el[-1].tail = (parent_el[-1].tail or "") + before
            else:
                parent_el.text = (parent_el.text or "") + before

        child = etree.SubElement(parent_el, T + tag_name, **attribs)
        child.text = ent.text
        pos = ent.end_char

    # trailing text
    remaining = text[pos:]
    if remaining:
        if len(parent_el):
            parent_el[-1].tail = (parent_el[-1].tail or "") + remaining
        else:
            parent_el.text = (parent_el.text or "") + remaining


def _split_paragraphs(text: str) -> list[str]:
    """
    Split OCR text into paragraphs on blank lines.
    Collapses soft hyphens / line-break hyphens within a paragraph.
    """
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_paras = re.split(r"\n{2,}", text)
    paras = []
    for p in raw_paras:
        # Rejoin hyphenated line breaks (e.g. "sum-\nmer" → "summer")
        p = re.sub(r"-\n(\S)", r"\1", p)
        # Collapse remaining newlines to spaces
        p = re.sub(r"\n", " ", p)
        p = p.strip()
        if p:
            paras.append(p)
    return paras


def encode_clipping(clipping: Clipping, nlp) -> str:
    """
    Return a TEI XML string for *clipping*.
    Raises ValueError if there is no OCR text.
    """
    if not clipping.ocr_text:
        raise ValueError("no OCR text")

    # --- teiHeader ---
    root = etree.Element(T + "TEI", nsmap={None: TEI_NS})

    header = etree.SubElement(root, T + "teiHeader")
    file_desc = etree.SubElement(header, T + "fileDesc")

    title_stmt = etree.SubElement(file_desc, T + "titleStmt")
    title_el = etree.SubElement(title_stmt, T + "title",
                                level="a", type="main")
    title_el.text = clipping.title or "(untitled)"

    pub_stmt = etree.SubElement(file_desc, T + "publicationStmt")
    pub_p = etree.SubElement(pub_stmt, T + "p")
    pub_p.text = f"Encoded from newspapers.com clipping {clipping.id}"

    source_desc = etree.SubElement(file_desc, T + "sourceDesc")
    bibl = etree.SubElement(source_desc, T + "bibl")

    j_title = etree.SubElement(bibl, T + "title", level="j")
    j_title.text = clipping.newspaper_name or "Unknown"

    if clipping.publication_date:
        date_iso = clipping.publication_date.isoformat()
        date_el = etree.SubElement(bibl, T + "date", when=date_iso)
        date_el.text = date_iso

    if clipping.page_number:
        scope = etree.SubElement(bibl, T + "biblScope", unit="page")
        scope.text = clipping.page_number

    if clipping.clipping_url:
        ptr = etree.SubElement(bibl, T + "ptr", target=clipping.clipping_url)

    # --- text/body ---
    text_el = etree.SubElement(root, T + "text")
    body = etree.SubElement(text_el, T + "body")
    div = etree.SubElement(body, T + "div", type="article", subtype="clipping")

    # Headline
    head_el = etree.SubElement(div, T + "head")
    head_el.text = clipping.title or "(untitled)"

    # Process each paragraph through NER
    paragraphs = _split_paragraphs(clipping.ocr_text)
    for para_text in paragraphs:
        doc = nlp(para_text)
        p_el = etree.SubElement(div, T + "p")
        _fill_mixed_content(p_el, para_text, doc.ents)

    return etree.tostring(
        root,
        pretty_print=True,
        xml_declaration=True,
        encoding="UTF-8",
    ).decode("utf-8")


def encode_all(
    database_url: str | None = None,
    limit: int | None = None,
    reprocess: bool = False,
) -> tuple[int, int]:
    """
    Encode TEI for all downloaded clippings that have OCR text.
    Returns (encoded_count, skipped_count).
    """
    nlp = _load_nlp()
    engine = init_db(get_engine(database_url))

    with Session(engine) as db:
        stmt = select(Clipping).where(
            Clipping.status == "downloaded",
            Clipping.ocr_text.isnot(None),
        )
        if not reprocess:
            stmt = stmt.where(Clipping.tei_xml.is_(None))
        if limit:
            stmt = stmt.limit(limit)
        clippings = db.execute(stmt).scalars().all()

    log.info("Found %d clippings to encode.", len(clippings))
    encoded = skipped = 0

    for clipping in tqdm(clippings, desc="Encoding TEI", unit="clip"):
        try:
            tei = encode_clipping(clipping, nlp)
        except Exception as exc:
            log.warning("Skipping %s: %s", clipping.id, exc)
            skipped += 1
            continue

        with Session(engine) as db:
            row = db.get(Clipping, clipping.id)
            if row:
                row.tei_xml = tei
                db.commit()
        encoded += 1

    log.info("Done. Encoded: %d  Skipped: %d", encoded, skipped)
    return encoded, skipped

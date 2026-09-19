"""
HTML ingestion — Path B (unstructured) of Stage 2, per PRD v2 Section 5.

Separates prose from embedded tables (they need different downstream
handling) and preserves document order + nearest-heading "section" for
each block, so later stages can cite "this came from Item 1A, Risk
Factors" rather than just a raw offset.

SCOPE NOTE: this parses the common SEC EDGAR filing-viewer HTML shape
(heading tags, <p>, <table>). Real 10-K/10-Q HTML from different filers
varies more than this (nested tables, div-based pseudo-tables, inline
styling standing in for structure) — this covers the common case as a
Phase 2 starting point, not a guarantee against every filer's quirks.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Union

from bs4 import BeautifulSoup, Tag

HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


@dataclass
class ProseBlock:
    section: str
    text: str
    order: int


@dataclass
class TableBlock:
    section: str
    caption: str | None
    rows: list[list[str]]
    order: int
    complex_layout: bool = False


Block = Union[ProseBlock, TableBlock]


def parse_filing_html(html: str) -> list[Block]:
    """
    Parse a filing HTML document into an ordered list of ProseBlock/TableBlock
    objects, each tagged with the nearest preceding heading as `section`.
    """
    # Inline-XBRL filings are XHTML often prefixed with an XML declaration.
    # Parse their HTML body, excluding hidden machine-readable facts.
    html = re.sub(r"^\s*<\?xml[^>]*\?>", "", html, count=1)
    soup = BeautifulSoup(html, "lxml")
    for hidden in soup.find_all(["script", "style", "ix:header", "ix:hidden"]):
        hidden.decompose()
    body = soup.body or soup

    blocks: list[Block] = []
    current_section = "(no heading)"
    order = 0
    last_paragraph_text: str | None = None

    for el in body.find_all(HEADING_TAGS + ["p", "div", "table"], recursive=True):
        # Skip anything nested inside a <table> we'll capture as a whole
        # TableBlock below (e.g. a <p> inside a <td>) so it isn't also
        # picked up as a separate top-level ProseBlock.
        if el.find_parent("table") is not None:
            continue
        if el.name == "div" and el.find(HEADING_TAGS + ["p", "div", "table"]) is not None:
            continue  # capture leaf prose blocks without duplicating wrapper text

        if el.name in HEADING_TAGS:
            current_section = el.get_text(strip=True) or current_section
            last_paragraph_text = None
            continue

        if el.name == "table":
            rows = _extract_table_rows(el)
            if not rows:
                continue
            blocks.append(
                TableBlock(section=current_section, caption=last_paragraph_text, rows=rows, order=order,
                           complex_layout=el.find("table") is not None or any(
                               cell.get("colspan", "1") != "1" or cell.get("rowspan", "1") != "1"
                               for cell in el.find_all(["td", "th"])))
            )
            order += 1
            last_paragraph_text = None
            continue

        # <p>
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if len(text) < 200 and re.match(r"^item\s+\d+[a-z]?\s*[.:-]", text, re.I):
            current_section = text
            last_paragraph_text = None
            continue
        blocks.append(ProseBlock(section=current_section, text=text, order=order))
        order += 1
        last_paragraph_text = text

    return blocks


def _extract_table_rows(table: Tag) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        row = [c.get_text(strip=True) for c in cells]
        if any(cell for cell in row):  # skip fully-empty spacer rows
            rows.append(row)
    return rows

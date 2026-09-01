"""
Chunking — turns the block list from html_ingest.py into retrieval-ready
chunks. Per PRD v2: boundaries follow document structure (section /
paragraph), not a fixed character count, and a TableBlock is always its
own chunk — never merged with prose, never split — so a number is never
separated from the row/column labels that give it meaning.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.retrieval.html_ingest import Block, TableBlock

DEFAULT_MAX_CHUNK_CHARS = 1500


@dataclass
class Chunk:
    chunk_id: str
    section: str
    text: str  # retrievable text — for tables, a flattened label|value rendering
    kind: str  # "prose" | "table"
    source_orders: list[int]  # originating block order(s), for citation/traceability
    raw_table: list[list[str]] | None = None  # only set for kind == "table"


def chunk_blocks(blocks: list[Block], max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS) -> list[Chunk]:
    chunks: list[Chunk] = []
    buffer_section: str | None = None
    buffer_texts: list[str] = []
    buffer_orders: list[int] = []

    def flush() -> None:
        nonlocal buffer_section, buffer_texts, buffer_orders
        if buffer_texts:
            chunks.append(
                Chunk(
                    chunk_id=f"prose-{buffer_orders[0]}",
                    section=buffer_section or "(no heading)",
                    text=" ".join(buffer_texts),
                    kind="prose",
                    source_orders=list(buffer_orders),
                )
            )
        buffer_section = None
        buffer_texts = []
        buffer_orders = []

    for block in blocks:
        if isinstance(block, TableBlock):
            # Flush any buffered prose first, then the table becomes its
            # own standalone chunk — never merged with anything.
            flush()
            chunks.append(
                Chunk(
                    chunk_id=f"table-{block.order}",
                    section=block.section,
                    text=_render_table_as_text(block),
                    kind="table",
                    source_orders=[block.order],
                    raw_table=block.rows,
                )
            )
            continue

        # ProseBlock
        if buffer_section is not None and block.section != buffer_section:
            flush()  # never merge prose across a section boundary

        candidate_len = sum(len(t) for t in buffer_texts) + len(block.text)
        if buffer_texts and candidate_len > max_chunk_chars:
            flush()

        buffer_section = block.section
        buffer_texts.append(block.text)
        buffer_orders.append(block.order)

    flush()
    return chunks


def _render_table_as_text(block: TableBlock) -> str:
    """Flatten a table into text a BM25/embedding index can use, keeping
    each row's label next to its values on the same line."""
    lines = []
    if block.caption:
        lines.append(block.caption)
    for row in block.rows:
        lines.append(" | ".join(row))
    return "\n".join(lines)

"""
PDF rendering of a memo - PRD v2 Section 5 Stage 5, "Markdown/PDF output".

Renders from the structured RunRecord, not by converting the Markdown. A
Markdown-to-PDF conversion would make the PDF a derivative of a rendering
rather than of the data, and every caveat this project prints would then
depend on a Markdown parser getting a table right.

reportlab is used because it is pure Python: WeasyPrint and wkhtmltopdf need
cairo/pango or a Qt build, which a slim container image does not have. It is
an OPTIONAL dependency - the import is deferred and its absence produces a
clear error, so the rest of the service runs without it.

The scope disclaimer and the per-figure provenance markers are not optional
decoration. They are in the Markdown memo for the reasons memo.py sets out,
and a PDF that dropped them would be the same document making stronger claims
than the system can support.
"""
from __future__ import annotations

from io import BytesIO

from src.output.memo import SCOPE_DISCLAIMER
from src.service.records import RunRecord

_PROVENANCE = {
    "xbrl": "XBRL",
    "derived": "derived",
    "html_table_fallback": "HTML fallback - review",
    "unknown": "unknown",
}


def _require_reportlab():
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
        )
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "PDF export needs reportlab. Install it with `pip install reportlab` "
            "(it is listed in requirements.txt), or use the Markdown memo instead."
        ) from exc
    return {
        "colors": colors, "TA_LEFT": TA_LEFT, "LETTER": LETTER,
        "ParagraphStyle": ParagraphStyle, "getSampleStyleSheet": getSampleStyleSheet,
        "inch": inch, "PageBreak": PageBreak, "Paragraph": Paragraph,
        "SimpleDocTemplate": SimpleDocTemplate, "Spacer": Spacer,
        "Table": Table, "TableStyle": TableStyle,
    }


def _escape(text: object) -> str:
    """reportlab paragraphs are mini-HTML, so raw ampersands and angle brackets
    in filing prose would otherwise be parsed as markup and raise."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _money(value) -> str:
    if value is None:
        return "N/A"
    return f"{'-' if value < 0 else ''}${abs(value):,.0f}"


def _pct(value, suffix: str = "%") -> str:
    return "N/A" if value is None else f"{value:.1f}{suffix}"


def memo_pdf_bytes(record: RunRecord) -> bytes:
    """Render one processed filing to PDF. Raises RuntimeError if reportlab is absent."""
    rl = _require_reportlab()
    colors, inch = rl["colors"], rl["inch"]
    styles = rl["getSampleStyleSheet"]()
    body = rl["ParagraphStyle"]("body", parent=styles["BodyText"], fontSize=8.5, leading=11)
    small = rl["ParagraphStyle"]("small", parent=body, fontSize=7.5, leading=9.5,
                                 textColor=colors.HexColor("#444444"))
    h1 = rl["ParagraphStyle"]("h1", parent=styles["Heading1"], fontSize=15, spaceAfter=4)
    h2 = rl["ParagraphStyle"]("h2", parent=styles["Heading2"], fontSize=11, spaceBefore=12, spaceAfter=4)

    buffer = BytesIO()
    doc = rl["SimpleDocTemplate"](
        buffer, pagesize=rl["LETTER"],
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"Filing Research Memo - {record.company_name} {record.accession_number}",
        author="Financial Research Copilot",
    )

    def table(data, widths, extra=None):
        t = rl["Table"](data, colWidths=widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ECEFF3")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#111111")),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9CFD6")),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        t.setStyle(rl["TableStyle"](style + (extra or [])))
        return t

    story = [
        rl["Paragraph"](_escape(f"Filing Research Memo - {record.company_name} ({record.company_cik})"), h1),
        rl["Paragraph"](_escape(
            f"{record.form} - accession {record.accession_number} - filed {record.filing_date}"
            + (f" - period ended {record.report_date}" if record.report_date else "")), small),
        rl["Spacer"](1, 8),
        rl["Paragraph"](f"<b>Scope:</b> {_escape(SCOPE_DISCLAIMER)}", small),
        rl["Spacer"](1, 4),
        rl["Paragraph"](f"<b>Data basis:</b> {_escape(record.data_provenance_note)}", small),
        rl["Paragraph"](f"<b>Rubric version:</b> {_escape(record.rubric_version)}", small),
    ]

    if record.executive_summary:
        story += [rl["Paragraph"]("Executive Summary", h2)]
        story += [rl["Paragraph"](f"- {_escape(line)}", body) for line in record.executive_summary]

    if record.metric_rows:
        story += [rl["Paragraph"]("Key Metric Changes", h2)]
        data = [["Metric", "Period", "Comparison", "Value", "Prior", "Change", "Source"]]
        for row in record.metric_rows:
            change = row.get("percent_change")
            data.append([
                _escape(row.get("concept", "")),
                _escape(row.get("period_label", "")),
                _escape(row.get("comparison_label", "")),
                _money(row.get("current_value")),
                _money(row.get("comparison_value")),
                "N/A" if change is None else f"{change:+.2f}%",
                _PROVENANCE.get(row.get("provenance", "unknown"), row.get("provenance", "")),
            ])
        story.append(table(data, [0.95 * inch, 0.7 * inch, 1.35 * inch, 1.05 * inch,
                                  1.05 * inch, 0.7 * inch, 1.4 * inch],
                           [("ALIGN", (3, 1), (5, -1), "RIGHT")]))

    peers = record.peer_comparison
    if peers:
        story += [rl["Paragraph"]("Peer Comparison", h2),
                  rl["Paragraph"](_escape(peers.get("alignment_note", "")), small),
                  rl["Spacer"](1, 4)]
        data = [["Company", "Period", "Period end", "Offset", "Revenue", "Net income", "Net mgn", "Gross mgn"]]
        for row in [peers.get("subject")] + list(peers.get("peers", [])):
            if not row:
                continue
            label = (row.get("company_ticker") or row.get("company_cik") or "")
            if row.get("is_subject"):
                label += " (subject)"
            if row.get("unavailable_reason"):
                data.append([_escape(label), "not aligned", "-", "-", "N/A", "N/A", "N/A", "N/A"])
                continue
            offset = row.get("end_offset_days")
            data.append([
                _escape(label),
                f"{row.get('fiscal_period') or '-'} FY{row.get('fiscal_year') or '-'}",
                _escape(row.get("period_end_date") or "-"),
                "subject" if row.get("is_subject") else ("-" if offset is None else f"{offset:+d}d"),
                _money(row.get("revenue")), _money(row.get("net_income")),
                _pct(row.get("net_margin_pct")), _pct(row.get("gross_margin_pct")),
            ])
        story.append(table(data, [0.95 * inch, 0.75 * inch, 0.8 * inch, 0.55 * inch,
                                  1.15 * inch, 1.15 * inch, 0.6 * inch, 0.65 * inch],
                           [("ALIGN", (3, 1), (-1, -1), "RIGHT")]))
        excluded = [r for r in peers.get("peers", []) if r.get("unavailable_reason")]
        if excluded:
            story.append(rl["Spacer"](1, 4))
            for r in excluded:
                story.append(rl["Paragraph"](
                    _escape(f"{r.get('company_ticker') or r.get('company_cik')}: {r['unavailable_reason']}"), small))

    notable = [f for f in record.flags if f.get("severity") != "routine"]
    routine = [f for f in record.flags if f.get("severity") == "routine"]

    story += [rl["Paragraph"]("Flagged Items", h2)]
    if not notable:
        story.append(rl["Paragraph"]("None - nothing met the current rubric's thresholds.", body))
    for flag in notable:
        story += [
            rl["Paragraph"](f"<b>[{_escape(flag.get('rule_id', ''))}]</b> {_escape(flag.get('detail', ''))}", body),
            rl["Paragraph"](f"Cites: {_escape(flag.get('citation', ''))}", small),
            rl["Paragraph"](f"Rule: {_escape(flag.get('rule_description', ''))}", small),
            rl["Spacer"](1, 5),
        ]
    story.append(rl["Paragraph"](
        "For human review: every Judgment agent flag is a screen, not a verdict (PRD Section 5).", small))

    if routine:
        story += [rl["Paragraph"]("Routine - screened but unchanged", h2),
                  rl["Paragraph"](_escape(
                      f"{len(routine)} screened passage(s) repeat prior-filing language after normalizing "
                      "dates. Listed for completeness; none of them is a change."), small),
                  rl["Spacer"](1, 4)]
        for flag in routine:
            story += [
                rl["Paragraph"](f"<b>[{_escape(flag.get('rule_id', ''))}]</b> {_escape(flag.get('detail', ''))}", body),
                rl["Paragraph"](f"Cites: {_escape(flag.get('citation', ''))}", small),
                rl["Spacer"](1, 4),
            ]

    doc.build(story)
    return buffer.getvalue()

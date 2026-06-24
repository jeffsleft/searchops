"""Render tailored .docx resume + cover letter from a locked master template.

WP-E: the design lives in the master templates (app/templates/docx/), authored by
scripts/build_doc_templates.py. This module emits *content only* — it marshals the
parsed resume / generated cover letter into a docxtpl render context and fills the
template. It never rebuilds the document from scratch, so the design is byte-stable
across renders: tailoring the same resume twice produces identical bytes except for
the swapped content.

Inline formatting (bold lead-ins, right-tabbed date rows) is supplied as RichText so
the engine controls content emphasis; the template owns page setup, margins, spacing,
tab stops, and rules.
"""
from __future__ import annotations

import io
from pathlib import Path

from docxtpl import DocxTemplate, RichText

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "docx"
_RESUME_TEMPLATE = _TEMPLATE_DIR / "resume_template.docx"
_COVER_TEMPLATE = _TEMPLATE_DIR / "cover_letter_template.docx"

_FONT = "Source Serif 4"
_EM_DASH = " — "  # space — space


# ── RichText builders (half-point sizes; omit size to inherit Normal=10.25pt) ─
def _bullet_rich(text: str) -> RichText:
    """Bullet with lead-in bolding when a 'Lead — body' pattern is present."""
    rt = RichText()
    rt.add("• ", font=_FONT)
    idx = text.find(_EM_DASH)
    if 0 < idx < 45:
        rt.add(text[:idx], font=_FONT, bold=True)
        rt.add(_EM_DASH + text[idx + len(_EM_DASH):], font=_FONT)
    else:
        rt.add(text, font=_FONT)
    return rt


def _prose_line_rich(line: str) -> RichText:
    """Prose line with 'Label: detail' or 'Label — detail' lead-in bolding."""
    rt = RichText()
    colon_i = line.find(": ")
    em_i = line.find(_EM_DASH)
    if 0 < colon_i < 35 and not line[0].islower():
        rt.add(line[:colon_i], font=_FONT, bold=True)
        rt.add(": " + line[colon_i + 2:], font=_FONT)
    elif 0 < em_i < 25:
        rt.add(line[:em_i], font=_FONT, bold=True)
        rt.add(_EM_DASH + line[em_i + len(_EM_DASH):], font=_FONT)
    else:
        rt.add(line, font=_FONT)
    return rt


def _meta_or_summary_rich(text: str, is_meta: bool) -> RichText:
    rt = RichText()
    if is_meta:
        rt.add(text, font=_FONT, size=19, color="444444")  # 9.5pt
    else:
        rt.add(text, font=_FONT, size=20, color="1d1d1d")  # ~9.75pt
    return rt


def _experience_block(section: dict, company_bullets: dict[str, list[str]]) -> dict:
    heading = section.get("heading", "")
    dates = heading.split("\t")[1] if "\t" in heading else ""
    org_name = section.get("company") or heading.split("\t")[0]

    paragraphs = section.get("paragraphs", [])
    if paragraphs:
        role_line = paragraphs[0]
        role = role_line.split("\t")[0] if "\t" in role_line else role_line
        loc = role_line.split("\t")[1] if "\t" in role_line else ""
        rest = paragraphs[1:]
    else:
        role, loc, rest = "", "", []

    # Row 1: Company (bold 11pt) + right tab + Dates (italic 9.5pt)
    org_row = RichText()
    org_row.add(org_name, font=_FONT, bold=True, size=22, color="111111")
    if dates:
        org_row.add("\t", font=_FONT)
        org_row.add(dates, font=_FONT, italic=True, size=19, color="333333")

    # Row 2: Role (italic 10pt) + right tab + Location (9.5pt)
    role_row = None
    if role:
        role_row = RichText()
        role_row.add(role, font=_FONT, italic=True, size=20)
        if loc:
            role_row.add("\t", font=_FONT)
            role_row.add(loc, font=_FONT, size=19, color="444444")

    # Meta / summary lines
    lines = []
    for j, para in enumerate(rest):
        is_meta = j == 0 and (
            para.startswith("$") or "Team:" in para or "ARR" in para or "→" in para
        )
        lines.append(_meta_or_summary_rich(para, is_meta))

    # Replace mode: tailored bullets replace originals when present
    co_tailored = company_bullets.get(section.get("company", ""), [])
    bullets = co_tailored if co_tailored else section.get("bullets", [])

    return {
        "first": False,
        "heading": "",
        "org_row": org_row,
        "role_row": role_row,
        "lines": lines,
        "bullets": [_bullet_rich(b) for b in bullets],
    }


def _prose_block(section: dict) -> dict:
    return {
        "first": False,
        "heading": section.get("heading", "").upper(),
        "org_row": None,
        "role_row": None,
        "lines": [_prose_line_rich(ln) for ln in section.get("paragraphs", [])],
        "bullets": [_bullet_rich(b) for b in section.get("bullets", [])],
    }


def build_resume_docx(
    header: dict,
    sections: list[dict],
    company_bullets: dict[str, list[str]],
    job_company: str = "",
    sections_to_drop: list[str] | None = None,
) -> bytes:
    """Return .docx bytes for the tailored resume (renders the locked master)."""
    drop_set = set(sections_to_drop or [])
    visible = [
        s for s in sections
        if (s.get("company") or s.get("heading", "")) not in drop_set
    ]

    blocks: list[dict] = []
    for section in visible:
        if section.get("type") == "experience":
            blocks.append(_experience_block(section, company_bullets))
        else:
            blocks.append(_prose_block(section))
    if blocks:
        blocks[0]["first"] = True  # suppress the leading inter-section rule

    doc = DocxTemplate(str(_RESUME_TEMPLATE))
    doc.render({"header": header, "blocks": blocks})
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_cover_letter_docx(
    header: dict,
    body: list[str],
    *,
    date: str = "",
    recipient: str = "",
    salutation: str = "",
    closing: str = "Sincerely,",
    signature: str = "",
) -> bytes:
    """Return .docx bytes for a full tailored cover letter (renders the locked master).

    `body` is the list of full letter paragraphs (full sentences, not fragments —
    per Jeff's cover-letter voice). `header` is {name, contact}.
    """
    context = {
        "header": header,
        "date": date,
        "recipient": recipient,
        "salutation": salutation or "Dear Hiring Team,",
        "body": [p for p in body if p and p.strip()],
        "closing": closing,
        "signature": signature or header.get("name", ""),
    }
    doc = DocxTemplate(str(_COVER_TEMPLATE))
    doc.render(context)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

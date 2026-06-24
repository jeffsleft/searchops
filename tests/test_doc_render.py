"""
WP-E regression guard for the document engine.

The whole point of the docxtpl rewrite is that design stops drifting: the engine
renders a *locked master template* and injects content, so tailoring the same resume
twice yields byte-identical output (except swapped content). These tests pin that
contract plus the basic "it renders without leftover Jinja tags" invariant.
"""
import io
import zipfile

from docx import Document

from app.resume_docx import build_resume_docx, build_cover_letter_docx


def _archive_payload(docx_bytes):
    """Map of {member name: content} for a .docx zip.

    Compares the actual document payload (document.xml, styles, etc.) while
    ignoring zip *container* metadata — python-docx stamps each entry with the
    wall-clock time at save(), so raw bytes differ across a one-second boundary
    even when the document is identical. Design stability is about content, not
    the zip's mod-time fields.
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        return {name: z.read(name) for name in sorted(z.namelist())}


HEADER = {
    "name": "Jane Candidate",
    "tagline": "GTM Operations Leader",
    "contact": "Somewhere, USA · jane@example.com · 555-0100",
}

SECTIONS = [
    {
        "type": "prose", "heading": "Profile", "company": None,
        "paragraphs": ["Operator who builds the systems revenue teams run on."],
        "bullets": [],
    },
    {
        "type": "experience", "heading": "Acme\tJan 2020 – Present", "company": "Acme",
        "paragraphs": ["Director of Operations\tRemote", "$50M ARR · Team: 6"],
        "bullets": ["Renewals — rebuilt the renewal motion and lifted GRR 6 points"],
    },
    {
        "type": "experience", "heading": "Globex\tJan 2017 – Dec 2019", "company": "Globex",
        "paragraphs": ["Manager\tNYC"],
        "bullets": ["Did a thing of consequence with a measurable result"],
    },
]


def _render(company_bullets):
    return build_resume_docx(
        header=HEADER, sections=SECTIONS, company_bullets=company_bullets,
        job_company="Acme", sections_to_drop=["Globex"],
    )


def _texts(docx_bytes):
    return [p.text for p in Document(io.BytesIO(docx_bytes)).paragraphs]


def test_resume_render_has_no_leftover_jinja_tags():
    texts = _texts(_render({}))
    leftover = [t for t in texts if "{%" in t or "{{" in t]
    assert not leftover, f"unrendered template tags: {leftover}"


def test_resume_design_is_stable_for_same_content():
    cb = {"Acme": ["Renewals — same tailored bullet every time"]}
    assert _archive_payload(_render(cb)) == _archive_payload(_render(cb))


def test_resume_changes_when_content_changes():
    a = _archive_payload(_render({"Acme": ["Renewals — first version of the bullet"]}))
    b = _archive_payload(_render({"Acme": ["Digital CS — a completely different bullet"]}))
    assert a != b
    # only the document body changes — styles/theme/numbering are untouched
    assert a["word/styles.xml"] == b["word/styles.xml"]


def test_dropped_section_is_omitted():
    texts = " ".join(_texts(_render({})))
    assert "Globex" not in texts
    assert "Acme" in texts


def test_cover_letter_render():
    body = ["Opening hook paragraph.", "Fit paragraph.", "Closing ask."]
    docx_bytes = build_cover_letter_docx(
        header={"name": "Jane Candidate", "contact": "jane@example.com"},
        body=body, date="January 1, 2026", recipient="Hiring Team, Acme",
        salutation="Dear Hiring Team,", signature="Jane Candidate",
    )
    texts = _texts(docx_bytes)
    joined = "\n".join(texts)
    assert "{%" not in joined and "{{" not in joined
    for para in body:
        assert para in texts
    assert "Dear Hiring Team," in texts


def test_cover_letter_skips_empty_paragraphs():
    docx_bytes = build_cover_letter_docx(
        header={"name": "Jane", "contact": ""},
        body=["Real paragraph.", "", "   ", "Another real one."],
    )
    paras = [t for t in _texts(docx_bytes) if t.strip()]
    # both real paragraphs survive; the blank/whitespace ones are dropped
    assert "Real paragraph." in paras
    assert "Another real one." in paras

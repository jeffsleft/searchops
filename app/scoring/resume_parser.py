"""
Parse data/resume.docx into a structured dict for PDF/HTML assembly.

Resume uses Normal style throughout with List Paragraph for bullets.
Experience sections are identified by company name + date pattern on the same line.
"""
import re
from pathlib import Path
from docx import Document

# Companies in resume order, with display name and keywords for bullet matching
EXPERIENCE_COMPANIES = [
    {"name": "Mercy Ships", "keywords": ["mercy ships", "mercy ship"]},
    {"name": "Jeff Beaumont Consulting", "keywords": ["consulting", "beaumont consulting"]},
    {"name": "GitLab", "keywords": ["gitlab"]},
    {"name": "RightCapital", "keywords": ["rightcapital", "right capital"]},
    {"name": "Riskalyze", "keywords": ["riskalyze"]},
    {"name": "Ronald Blue", "keywords": ["ronald blue"]},
]

# Regex: line ends with a date range like "Oct 2019 – Sep 2023" or "Feb 2024 – Present"
_DATE_PATTERN = re.compile(r".+\t.+\d{4}.+$", re.IGNORECASE)
_SECTION_HEADERS = {"PROFILE", "CORE COMPETENCIES", "PROFESSIONAL EXPERIENCE",
                    "SPEAKING & THOUGHT LEADERSHIP", "EDUCATION & CREDENTIALS"}


def _is_company_line(text: str) -> str | None:
    """Returns company name if the line looks like an experience header, else None."""
    for co in EXPERIENCE_COMPANIES:
        for kw in co["keywords"]:
            if kw.lower() in text.lower() and _DATE_PATTERN.match(text):
                return co["name"]
    return None


def load_resume(path: Path | None = None) -> dict:
    """
    Parse resume.docx into structured sections.

    Returns:
        {
          "header": {"name": str, "tagline": str, "contact": str},
          "sections": [
            {
              "type": "prose" | "bullets" | "experience",
              "heading": str,       # section heading text
              "paragraphs": [str],  # Normal-style text lines (non-bullet)
              "bullets": [str],     # List Paragraph items
              "company": str | None # set for experience sections
            }
          ]
        }
    """
    if path is None:
        path = Path("data/resume.docx")

    doc = Document(str(path))
    paragraphs = [(p.style.name, p.text) for p in doc.paragraphs]

    header = {
        "name": paragraphs[0][1] if paragraphs else "Jeff Beaumont",
        "tagline": paragraphs[1][1] if len(paragraphs) > 1 else "",
        "contact": paragraphs[2][1] if len(paragraphs) > 2 else "",
    }

    sections: list[dict] = []
    current: dict | None = None

    for style, text in paragraphs[3:]:  # skip header block
        is_bullet = style == "List Paragraph"
        clean = text.strip()

        if not clean:
            continue

        # Top-level section header (e.g. PROFESSIONAL EXPERIENCE)
        if clean.upper() in _SECTION_HEADERS:
            if current:
                sections.append(current)
            current = {"type": "prose", "heading": clean, "paragraphs": [], "bullets": [], "company": None}
            continue

        # Experience company line
        company = _is_company_line(clean)
        if company:
            if current:
                sections.append(current)
            current = {"type": "experience", "heading": clean, "paragraphs": [], "bullets": [], "company": company}
            continue

        if current is None:
            continue

        if is_bullet:
            current["bullets"].append(clean)
        else:
            current["paragraphs"].append(clean)

    if current:
        sections.append(current)

    return {"header": header, "sections": sections}


def match_bullet_to_company(bullet: str) -> str | None:
    """Return the company name a tailored bullet most likely belongs to, or None."""
    lower = bullet.lower()
    for co in EXPERIENCE_COMPANIES:
        for kw in co["keywords"]:
            if kw in lower:
                return co["name"]
    return None

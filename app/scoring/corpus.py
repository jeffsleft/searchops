"""
Candidate corpus loader.

Reads `data/Accomplishments_Inventory.docx` and parses it into a structured
dict that Layer 2 (Match to Candidate) feeds into the LLM prompt.

Actual docx structure (v1.1):
  - Heading 1  → company / role section (e.g., "GitLab, Inc. | Oct 2019 – Sep 2023")
  - Heading 2  → theme inside that role (e.g., "Renewal Operations")
  - Table      → accomplishments table (4 cols: Accomplishment, Quantified Result,
                  Metric Type, Theme Tag). Follows the Heading 2 that owns it.
  - Special H1 "Narrative Corpus (Pass 1 Context Block)" → dense prose for LLM context
  - Special H1 "Resume Bullet Swap Library" → pre-written resume bullets (List Paragraph)
  - Special H1 "Theme Tags" → tag dictionary (not included in prompt output)

If the real inventory is absent, `load_corpus()` falls back to the committed example
corpus (`data/Accomplishments_Inventory.example.docx`) so a fresh clone still exercises
Layer 2 for real, and flags `is_example: True` so the UI can label it. If neither file
exists, it returns {"available": False, ...} and Layer 2 contributes zero.
"""
from __future__ import annotations

import hashlib
import logging
from functools import lru_cache
from pathlib import Path

from lxml import etree

from app.config import ROOT

INVENTORY_PATH = ROOT / "data" / "Accomplishments_Inventory.docx"

# Committed fictional corpus for the example persona (see candidate_profile.example.yaml).
# Used only when the real inventory is absent — i.e. on a fresh clone. Without this, Layer 2
# contributes 0.0 for a forker and the Application Kit's evidence/bullets/hooks render empty.
# Regenerate with: python3 scripts/build_example_corpus.py
EXAMPLE_INVENTORY_PATH = ROOT / "data" / "Accomplishments_Inventory.example.docx"

# SHA-256 of the known-good Accomplishments_Inventory.docx.
# Update this constant whenever the file is intentionally replaced.
# Generate with: python3 -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('data/Accomplishments_Inventory.docx').read_bytes()).hexdigest())"
#
# Only ever checked against INVENTORY_PATH. A forker's own inventory legitimately hashes
# differently, and the example corpus is verified by its own test — warning on either would
# train everyone to ignore the one warning that means something.
INVENTORY_EXPECTED_SHA256: str | None = "6c5fdd094fb13b6542c23b0a211ee1da44f0d4ae5992c1e2354992ef7949351c"


def _verify_corpus_integrity(path: Path) -> str:
    """Return hex SHA-256 of path; warn if it doesn't match INVENTORY_EXPECTED_SHA256."""
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if INVENTORY_EXPECTED_SHA256 and actual != INVENTORY_EXPECTED_SHA256:
        logging.warning(
            f"[corpus] INTEGRITY MISMATCH: Accomplishments_Inventory.docx hash {actual!r} "
            f"!= expected {INVENTORY_EXPECTED_SHA256!r}. "
            "If you didn't intentionally replace the file, treat Layer 2 output as suspect."
        )
    else:
        logging.info(f"[corpus] Inventory SHA-256: {actual}")
    return actual


def resolve_inventory_path() -> tuple[Path, bool]:
    """
    Pick the corpus to load. Returns (path, is_example).

    Real inventory wins; the example is the fresh-clone fallback. `is_example` propagates to
    the UI so demo evidence is never mistaken for the real thing.
    """
    if INVENTORY_PATH.exists():
        return INVENTORY_PATH, False
    if EXAMPLE_INVENTORY_PATH.exists():
        return EXAMPLE_INVENTORY_PATH, True
    return INVENTORY_PATH, False

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _tag(local: str) -> str:
    return f"{{{WORD_NS}}}{local}"


def _para_style(para_elem) -> str:
    pPr = para_elem.find(_tag("pPr"))
    if pPr is None:
        return ""
    pStyle = pPr.find(_tag("pStyle"))
    if pStyle is None:
        return ""
    return pStyle.get(_tag("val"), "")


def _para_text(para_elem) -> str:
    return "".join(
        t.text or ""
        for t in para_elem.iter(_tag("t"))
    ).strip()


def _is_heading(style_val: str, level: int) -> bool:
    return style_val.lower() in (f"heading{level}", f"heading {level}")


def _cell_text(cell_elem) -> str:
    return " ".join(
        _para_text(p)
        for p in cell_elem.iter(_tag("p"))
    ).strip()


def _parse_table(tbl_elem) -> list[dict]:
    """
    Extract accomplishment rows from a 4-column table.
    Skips the header row (first row) and blank rows.
    Returns list of dicts: {text, result, metric_type, tags}
    """
    rows = tbl_elem.findall(f".//{_tag('tr')}")
    if not rows:
        return []

    header = [_cell_text(c) for c in rows[0].findall(f".//{_tag('tc')}")[:1]]
    is_accomplishment_table = header and "accomplishment" in header[0].lower()
    if not is_accomplishment_table:
        return []

    bullets = []
    for row in rows[1:]:
        cells = [_cell_text(c) for c in row.findall(f".//{_tag('tc')}")]
        if len(cells) < 2:
            continue
        text = cells[0].strip()
        if not text:
            continue
        result = cells[1].strip() if len(cells) > 1 else ""
        metric_type = cells[2].strip() if len(cells) > 2 else ""
        raw_tags = cells[3].strip() if len(cells) > 3 else ""
        tags = [t.strip().lower().replace(" ", "-") for t in raw_tags.split("|") if t.strip()]
        bullets.append({
            "text": text,
            "result": result,
            "metric_type": metric_type,
            "tags": tags,
        })
    return bullets


def _parse_list_bullets(body_children, start_idx: int) -> list[str]:
    """
    Collect List Paragraph bullets from body_children starting at start_idx.
    Stops when a non-List Paragraph, non-None paragraph or end of children is hit.
    """
    bullets = []
    for elem in body_children[start_idx:]:
        local = etree.QName(elem.tag).localname
        if local == "tbl":
            break
        if local == "p":
            style = _para_style(elem).lower()
            if style == "listparagraph" or style == "list paragraph":
                text = _para_text(elem)
                if text:
                    bullets.append(text.lstrip("•-*· ").strip())
            elif style.startswith("heading"):
                break
    return bullets


@lru_cache(maxsize=1)
def load_corpus(path: Path | None = None) -> dict:
    """
    Parse the Accomplishments Inventory docx.

    Returns:
    {
      "available": bool,
      "path": str,
      "narrative": str,              # prose context block for LLM
      "sections": [
        {
          "name": "GitLab, Inc. | Oct 2019 – Sep 2023",
          "context": "...",          # body-text context under the H1
          "themes": [
            {
              "name": "Renewal Operations",
              "bullets": [
                {"text": "Built renewal ops from scratch",
                 "result": "Function built from zero",
                 "metric_type": "Greenfield",
                 "tags": ["greenfield", "nrr"]}
              ]
            }
          ]
        }
      ],
      "swap_library": {              # pre-written resume bullets by theme
        "For roles emphasizing NRR / Retention": ["bullet1", ...]
      }
    }
    """
    if path is not None:
        p, is_example = path, path == EXAMPLE_INVENTORY_PATH
    else:
        p, is_example = resolve_inventory_path()

    if not p.exists():
        return {
            "available": False,
            "is_example": False,
            "path": str(p),
            "narrative": "",
            "sections": [],
            "swap_library": {},
        }

    if p == INVENTORY_PATH:
        _verify_corpus_integrity(p)
    elif is_example:
        logging.info("[corpus] No real inventory found — loading example corpus at %s", p)

    from docx import Document
    doc = Document(str(p))

    body = doc.element.body
    children = list(body)

    sections: list[dict] = []
    swap_library: dict[str, list[str]] = {}
    narrative_lines: list[str] = []

    current_section: dict | None = None
    current_theme: dict | None = None
    mode = "normal"  # "normal" | "narrative" | "swap_library"
    swap_theme: str | None = None

    for i, elem in enumerate(children):
        local = etree.QName(elem.tag).localname

        if local == "p":
            style = _para_style(elem)
            text = _para_text(elem)

            if _is_heading(style, 1):
                name_lc = text.lower()
                if "narrative corpus" in name_lc or "pass 1 context" in name_lc:
                    mode = "narrative"
                    current_section = None
                    current_theme = None
                    continue
                elif "resume bullet swap" in name_lc:
                    mode = "swap_library"
                    swap_theme = None
                    current_section = None
                    current_theme = None
                    continue
                elif "theme tags" in name_lc or "purpose" in name_lc or "speaking" in name_lc:
                    mode = "skip"
                    current_section = None
                    current_theme = None
                    continue
                else:
                    mode = "normal"
                    current_section = {"name": text, "context": "", "themes": []}
                    current_theme = None
                    sections.append(current_section)
                    continue

            if _is_heading(style, 2):
                if mode == "swap_library":
                    swap_theme = text
                    if swap_theme not in swap_library:
                        swap_library[swap_theme] = []
                    continue
                elif mode == "normal" and current_section is not None:
                    current_theme = {"name": text, "bullets": []}
                    current_section["themes"].append(current_theme)
                    continue

            if not text:
                continue

            style_lc = style.lower()

            if mode == "narrative":
                narrative_lines.append(text)
                continue

            if mode == "swap_library":
                if style_lc in ("listparagraph", "list paragraph"):
                    if swap_theme and text:
                        swap_library.setdefault(swap_theme, []).append(
                            text.lstrip("•-*· ").strip()
                        )
                continue

            if mode == "normal" and current_section is not None and current_theme is None:
                # Context paragraph directly under an H1 (role summary)
                existing = current_section["context"]
                current_section["context"] = (existing + " " + text).strip() if existing else text

        elif local == "tbl":
            if mode == "normal" and current_section is not None:
                rows = _parse_table(elem)
                if rows:
                    if current_theme is None:
                        # Table directly under H1 with no H2 — create a synthetic theme
                        current_theme = {"name": "(general)", "bullets": []}
                        current_section["themes"].append(current_theme)
                    current_theme["bullets"].extend(rows)

    return {
        "available": True,
        "is_example": is_example,
        "path": str(p),
        "narrative": "\n\n".join(narrative_lines),
        "sections": sections,
        "swap_library": swap_library,
    }


def render_corpus_for_prompt(corpus: dict, max_chars: int = 18000) -> str:
    """
    Compact text rendering of the corpus for inlining into an LLM prompt.
    Leads with the narrative context block (pre-written for LLM consumption),
    then structured accomplishment rows by section + theme.
    Capped at max_chars.
    """
    if not corpus.get("available"):
        return "(No accomplishments inventory available.)"

    lines: list[str] = []

    if corpus.get("narrative"):
        lines.append("## Candidate Background (Pre-written Context)")
        lines.append(corpus["narrative"])
        lines.append("")

    for section in corpus["sections"]:
        lines.append(f"## {section['name']}")
        if section.get("context"):
            lines.append(section["context"])
        for theme in section["themes"]:
            lines.append(f"### {theme['name']}")
            for b in theme["bullets"]:
                result = f" → {b['result']}" if b.get("result") else ""
                tags = f" [{', '.join(b['tags'])}]" if b.get("tags") else ""
                lines.append(f"- {b['text']}{result}{tags}")
        lines.append("")

    out = "\n".join(lines).strip()
    if len(out) > max_chars:
        out = out[:max_chars] + "\n\n…(truncated — corpus exceeds prompt budget)"
    return out


COMPANY_ORDER = ["gitlab", "mercy ships", "consulting", "rightcapital", "riskalyze", "ronald blue"]


def get_bullets_for_themes(themes: list[str], corpus: dict, max_results: int = 3) -> list[dict]:
    """Return top corpus bullets matching any of the given theme tags, most recent company first."""
    if not themes or not corpus.get("available"):
        return []
    themes_lower = {t.lower() for t in themes}

    def section_rank(section_name: str) -> int:
        n = section_name.lower()
        for i, co in enumerate(COMPANY_ORDER):
            if co in n:
                return i
        return len(COMPANY_ORDER)

    sections = sorted(corpus.get("sections", []), key=lambda s: section_rank(s["name"]))
    results = []
    for section in sections:
        company = section["name"].split("|")[0].strip()
        for theme in section.get("themes", []):
            for bullet in theme.get("bullets", []):
                if themes_lower & {t.lower() for t in bullet.get("tags", [])}:
                    results.append({
                        "company": company,
                        "accomplishment": bullet["text"],
                        "result": bullet.get("result", ""),
                        "theme": theme["name"],
                    })
                    if len(results) >= max_results:
                        return results
    return results


def corpus_status() -> dict:
    """Lightweight status for diagnostic / settings UI."""
    c = load_corpus()
    return {
        "available": c["available"],
        "is_example": c.get("is_example", False),
        "path": c["path"],
        "n_sections": len(c.get("sections", [])),
        "has_narrative": bool(c.get("narrative")),
        "n_swap_themes": len(c.get("swap_library", {})),
        "n_bullets": sum(
            len(theme["bullets"])
            for section in c.get("sections", [])
            for theme in section["themes"]
        ),
    }

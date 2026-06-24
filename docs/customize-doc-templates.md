# Customizing your resume & cover-letter design

SearchOps generates a tailored resume and a full cover letter for any scored job. The
**content** (which bullets, which evidence, the letter prose) is produced by the
scoring engine. The **design** (fonts, margins, spacing, rules) lives entirely in two
locked Word templates:

```
app/templates/docx/resume_template.docx
app/templates/docx/cover_letter_template.docx
```

The engine renders these templates with [docxtpl](https://docxtpl.readthedocs.io/),
injecting only content. Because the design never gets rebuilt from code at render
time, tailoring the same resume twice produces a byte-identical document except for
the swapped content — no more formatting drift.

This is the part you fork to make the output *yours*.

## The two ways to customize

### 1. Edit the template in Word (no code)

Open `app/templates/docx/resume_template.docx` directly in Word, Google Docs, or
LibreOffice. You'll see your layout with placeholder tags:

- `{{ header.name }}`, `{{ header.tagline }}`, `{{ header.contact }}` — single fields.
- `{%p for block in blocks %}` … `{%p endfor %}` — the section loop.
- `{%p if block.heading %}` … `{%p endif %}` — conditional blocks.
- `{{r block.org_row }}`, `{{r bullet }}` — rich-text content (bold lead-ins, tab rows)
  injected by the engine.

Change the font, the margins, the colors, the spacing — anything Word lets you style.
**Leave the `{{ … }}` and `{%p … %}` tags in place**; they're how content flows in.
Save the file. The next render uses your design.

> Tip: the `{%p … %}` control-flow tags sit on their own near-invisible lines (line
> height collapsed to nothing). If you reveal formatting marks you'll see them; don't
> delete them or the loop breaks.

### 2. Regenerate the templates from code

The templates are authored programmatically by:

```
scripts/build_doc_templates.py
```

This is the single source of truth for the *default* design. The design tokens live at
the top of the file (`FONT`, `BODY_PT`, `LINE_PT`, margins). Change them and re-run:

```bash
python scripts/build_doc_templates.py
```

This overwrites both `.docx` templates. Use this path when you want the default design
to live in version control (so a fresh clone gets your design), rather than a
hand-edited binary `.docx`.

## The render contract

`app/resume_docx.py` builds the render context. If you add a field to a template, add
it to the context there too. The resume context is:

```python
{
  "header": {"name", "tagline", "contact"},
  "blocks": [
    {
      "first":    bool,            # True for the first block (suppresses the top rule)
      "heading":  str,             # prose section heading (upper-cased), else ""
      "org_row":  RichText | None, # experience: "Company \t Dates"
      "role_row": RichText | None, # experience: "Role \t Location"
      "lines":    [RichText, ...], # prose lines OR experience meta/summary
      "bullets":  [RichText, ...], # bullets, lead-in bolding pre-applied
    },
  ],
}
```

The cover-letter context:

```python
{
  "header":    {"name", "contact"},
  "date":      "June 20, 2026",
  "recipient": "Hiring Team, Acme",
  "salutation":"Dear Hiring Team,",
  "body":      ["paragraph 1", "paragraph 2", ...],
  "closing":   "Sincerely,",
  "signature": "Your Name",
}
```

Inline emphasis (bold lead-ins, right-tabbed date rows) is passed as `RichText` so the
engine controls content emphasis; the template owns page layout, tab stops, and rules.

## Why it ships in the repo

Both templates are committed so the project works out of the box — no Google setup, no
manual template authoring. A generic default is enough to produce a real resume and a
real cover letter on first run; swap in your own design when you're ready.

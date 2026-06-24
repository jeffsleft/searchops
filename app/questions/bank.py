"""
Question bank. Seeds questions from the spec, manages lifecycle,
detects divergence between answers from different contacts.
"""
import json
from app.models import get_db
from app.providers import get_provider
from app.scoring.prompts import DIVERGENCE_PROMPT


THEME_KEYWORDS = {
    "greenfield":       ["greenfield", "build from scratch", "zero to one", "standing up", "brand new"],
    "nrr":              ["nrr", "net revenue retention", "expansion", "upsell", "churn"],
    "renewal-ops":      ["renewal", "retention", "renew", "at-risk"],
    "health-scoring":   ["health score", "health scoring", "risk score", "qbr", "customer health"],
    "digital-cs":       ["digital", "community", "one-to-many", "tech touch"],
    "systems-thinking": ["system", "architecture", "infrastructure", "tech stack", "platform", "framework"],
    "scale":            ["scale", "scaling", "growth", "arr", "revenue"],
    "team-build":       ["team", "hire", "hiring", "manage", "org", "staffing"],
    "finance-ops":      ["finance", "financial", "budget", "forecast", "fp&a", "p&l", "treasury"],
    "ai-native":        ["ai", "automation", "ml", "machine learning", "llm", "genai", "automated"],
}

CATEGORY_BASE_THEMES = {
    "Financial":   ["finance-ops", "nrr"],
    "Technical":   ["systems-thinking", "ai-native"],
    "Operational": ["systems-thinking", "renewal-ops"],
    "Strategic":   ["greenfield", "scale"],
    "Cultural":    ["team-build"],
    "Pricing":     ["nrr", "finance-ops"],
}


def infer_themes(question: str, category: str) -> list[str]:
    """Deterministically infer theme tags from question text and category."""
    text = question.lower()
    themes = set(CATEGORY_BASE_THEMES.get(category, []))
    for tag, keywords in THEME_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            themes.add(tag)
    return list(themes)


# Seed questions from interview-pipeline-spec.md §3
SEED_QUESTIONS = {
    "High": [
        {"category": "Financial",    "persona": "CFO",     "question": "What is your current runway, and what does the path to profitability look like?"},
        {"category": "Financial",    "persona": "CRO",     "question": "What is your current NRR, and how has it trended over the last 4 quarters?"},
        {"category": "Strategic",    "persona": "Founder", "question": "What is the #1 piece of strategic debt you are currently 'powering through' with manual effort?"},
        {"category": "Strategic",    "persona": "CRO",     "question": "How would you describe the ICP today vs. 12 months ago? Has it shifted?"},
        {"category": "Pricing",      "persona": "Any",     "question": "What is your pricing model? Seat-based, consumption, outcome-based, or a hybrid?"},
        {"category": "Pricing",      "persona": "CFO",     "question": "Are you transitioning pricing models? If so, what stage is the transition?"},
        {"category": "Operational",  "persona": "Any",     "question": "Do you have a Forward Deployed Engineering (FDE) team? Where does it sit organizationally?"},
        {"category": "Cultural",     "persona": "Any",     "question": "Are we using AI to make our current messy process faster, or are we building new capabilities that weren't possible before?"},
        {"category": "Technical",    "persona": "Any",     "question": "What is your current tech stack? CRM, CS platform, communication tools?"},
    ],
    "Medium": [
        {"category": "Strategic",    "persona": "CRO",     "question": "When a customer is technically successful but expensive to serve, how do you decide between gross margin and market share?"},
        {"category": "Cultural",     "persona": "Any",     "question": "How would you describe the relationship between Sales and CS today?"},
        {"category": "Operational",  "persona": "Any",     "question": "What metrics does the CS team own today? How are those changing?"},
        {"category": "Financial",    "persona": "CFO",     "question": "What does your customer segmentation look like from a revenue perspective?"},
        {"category": "Technical",    "persona": "VP Eng",  "question": "What does your data infrastructure look like for customer usage signals?"},
    ],
    "Low": [
        {"category": "Financial",    "persona": "CFO",     "question": "How are your bank accounts and treasury operations structured?"},
        {"category": "Cultural",     "persona": "Any",     "question": "What does the onboarding process look like for a new leader at this level?"},
        {"category": "Operational",  "persona": "Any",     "question": "How large is the team I would be managing, and what does the org chart look like?"},
    ],
}


def seed_questions(job_id: int) -> int:
    """Insert universal seed questions for a job. Returns count of questions added."""
    count = 0
    with get_db() as conn:
        for priority, questions in SEED_QUESTIONS.items():
            for q in questions:
                existing = conn.execute(
                    "SELECT id FROM questions WHERE job_id = ? AND question = ?",
                    (job_id, q["question"]),
                ).fetchone()
                if not existing:
                    themes = infer_themes(q["question"], q["category"])
                    conn.execute(
                        """INSERT INTO questions
                           (job_id, question, category, persona_target, priority, status, source, suggested_themes)
                           VALUES (?,?,?,?,?,'unasked','seed',?)""",
                        (job_id, q["question"], q["category"],
                         q["persona"], priority, json.dumps(themes)),
                    )
                    count += 1
    return count


def add_question(
    job_id: int,
    question: str,
    category: str,
    persona_target: str = "Any",
    priority: str = "Medium",
    source: str = "manual",
    suggested_themes: list | None = None,
) -> int:
    with get_db() as conn:
        themes = json.dumps(suggested_themes if suggested_themes is not None else infer_themes(question, category))
        conn.execute(
            """INSERT INTO questions
               (job_id, question, category, persona_target, priority, status, source, suggested_themes)
               VALUES (?,?,?,?,?,'unasked',?,?)""",
            (job_id, question, category, persona_target, priority, source, themes),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def mark_asked(question_id: int, asked_to: str, asked_on: str | None = None) -> None:
    from datetime import date
    with get_db() as conn:
        conn.execute(
            "UPDATE questions SET status='asked', asked_to=?, asked_on=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (asked_to, asked_on or str(date.today()), question_id),
        )


def mark_answered(question_id: int, answer_notes: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE questions SET status='answered', answer_notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (answer_notes, question_id),
        )
    _check_divergence(question_id)


def _check_divergence(question_id: int) -> None:
    """After an answer is recorded, check if the same question was answered by someone else."""
    with get_db() as conn:
        q = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
        if not q:
            return
        siblings = conn.execute(
            """SELECT * FROM questions
               WHERE job_id = ? AND question = ? AND status = 'answered'
                 AND asked_to IS NOT NULL AND id != ?""",
            (q["job_id"], q["question"], question_id),
        ).fetchall()

    if not siblings:
        return

    sibling = siblings[0]
    with get_db() as conn:
        job = conn.execute("SELECT company FROM jobs WHERE id = ?", (q["job_id"],)).fetchone()

    llm = get_provider()
    prompt = DIVERGENCE_PROMPT.format(
        company=job["company"] if job else "Unknown",
        question=q["question"],
        person_a_title=q["asked_to"],
        person_a_name=q["asked_to"],
        answer_a=q["answer_notes"],
        person_b_title=sibling["asked_to"],
        person_b_name=sibling["asked_to"],
        answer_b=sibling["answer_notes"],
    )
    result = llm.generate_json(prompt)

    notes = json.dumps(result)
    is_divergent = not result.get("aligned", True)

    with get_db() as conn:
        for qid in (question_id, sibling["id"]):
            conn.execute(
                "UPDATE questions SET divergence_flag=?, divergence_notes=? WHERE id=?",
                (int(is_divergent), notes, qid),
            )


def get_questions(job_id: int, priority: str | None = None, persona: str | None = None) -> list[dict]:
    filters = ["job_id = ?"]
    params: list = [job_id]
    if priority:
        filters.append("priority = ?")
        params.append(priority)
    if persona and persona != "Any":
        filters.append("(persona_target = ? OR persona_target = 'Any')")
        params.append(persona)
    where = " AND ".join(filters)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM questions WHERE {where} ORDER BY CASE priority WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END",
            params,
        ).fetchall()
    return [dict(r) for r in rows]

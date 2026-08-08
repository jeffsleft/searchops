"""
Question bank. Seeds questions from the spec and backfills theme tags.

The read/write/mark-lifecycle + divergence-detection functions that used to
live here were retired 2026-07-21 -- their only callers (questions_list,
add_question_post, mark_question_asked, mark_question_answered in routes.py)
had no route registration, orphaned by the May 25 Interview Prep rebuild's
move to a per-session model. Divergence detection was rebuilt on that model
instead: see app.pipeline.prep.check_question_divergence.
"""
import json
from app.models import get_db


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


def backfill_missing_themes() -> int:
    """Backfill story anchor tags for questions missing suggested_themes.
    Returns count of questions tagged."""
    with get_db() as conn:
        qs = conn.execute(
            "SELECT id, question, category FROM questions WHERE suggested_themes IS NULL"
        ).fetchall()
        for q in qs:
            themes = json.dumps(infer_themes(q["question"], q["category"]))
            conn.execute(
                "UPDATE questions SET suggested_themes=? WHERE id=?",
                (themes, q["id"]),
            )
    return len(qs)



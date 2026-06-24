"""
Transcript paste handler. Runs Gemini independent analysis, then
optionally compares to Granola analysis if provided.
"""
import json
import logging
from datetime import date

from app.config import load_profile
from app.models import get_db
from app.providers import get_provider
from app.scoring.prompts import TRANSCRIPT_ANALYSIS_PROMPT, TRANSCRIPT_COMPARISON_PROMPT
from app.scoring.schemas import TranscriptAnalysis, TranscriptComparison


def _candidate_summary(profile: dict) -> str:
    c = profile.get("candidate", {})
    name = c.get("name", "Unknown")
    summary = c.get("summary", "").strip()
    return f"{name} — {summary}"


def analyze_transcript(
    job_id: int,
    raw_transcript: str,
    granola_analysis: str | None = None,
    contact_name: str = "",
    contact_title: str = "",
    interview_round: str = "",
    interview_date: str | None = None,
) -> dict:
    """
    Run Gemini's independent analysis on the raw transcript.
    If granola_analysis is provided, run comparison prompt too.
    Saves to transcripts table and returns the result dict.
    """
    profile = load_profile()
    llm = get_provider()

    with get_db() as conn:
        job = conn.execute(
            "SELECT company, job_title FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if not job:
            raise ValueError(f"Job {job_id} not found")

        existing_qs = conn.execute(
            "SELECT question, category, persona_target, priority, status FROM questions WHERE job_id = ?",
            (job_id,),
        ).fetchall()

    existing_questions_json = json.dumps([dict(q) for q in existing_qs], indent=2)

    analysis_prompt = TRANSCRIPT_ANALYSIS_PROMPT.format(
        candidate_name=profile.get("candidate", {}).get("name", "Unknown"),
        company=job["company"],
        job_title=job["job_title"],
        interviewers=f"{contact_name} ({contact_title})" if contact_name else "Unknown",
        candidate_summary=_candidate_summary(profile),
        transcript_text=raw_transcript,
        existing_questions_json=existing_questions_json,
    )

    gemini_analysis_raw = llm.generate_json(analysis_prompt)
    try:
        gemini_analysis = TranscriptAnalysis(**gemini_analysis_raw).dict()
    except Exception as e:
        logging.error(f"TranscriptAnalysis validation failed: {e}")
        gemini_analysis = gemini_analysis_raw

    comparison_result = None
    if granola_analysis and granola_analysis.strip():
        comparison_prompt = TRANSCRIPT_COMPARISON_PROMPT.format(
            gemini_analysis=json.dumps(gemini_analysis, indent=2),
            granola_analysis=granola_analysis,
        )
        comparison_result_raw = llm.generate_json(comparison_prompt)
        try:
            comparison_result = TranscriptComparison(**comparison_result_raw).dict()
        except Exception as e:
            logging.error(f"TranscriptComparison validation failed: {e}")
            comparison_result = comparison_result_raw

    with get_db() as conn:
        conn.execute(
            """INSERT INTO transcripts
               (job_id, interview_date, contact_name, contact_title, round,
                raw_transcript, granola_analysis, gemini_analysis, comparison_result)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                interview_date or str(date.today()),
                contact_name, contact_title, interview_round,
                raw_transcript, granola_analysis,
                json.dumps(gemini_analysis),
                json.dumps(comparison_result) if comparison_result else None,
            ),
        )
        transcript_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Add unanswered questions to question bank
        for q in gemini_analysis.get("unanswered_questions", []):
            conn.execute(
                """INSERT INTO questions
                   (job_id, question, category, persona_target, priority,
                    status, source, answer_notes)
                   VALUES (?,?,?,?,?,'unasked','transcript',?)""",
                (job_id, q["question"], q.get("category", "Strategic"),
                 "Any", q.get("priority", "High"), q.get("context")),
            )
        for q in gemini_analysis.get("new_questions_to_ask", []):
            conn.execute(
                """INSERT INTO questions
                   (job_id, question, category, persona_target, priority, status, source)
                   VALUES (?,?,?,?,?,'unasked','transcript')""",
                (job_id, q["question"], q.get("category", "Strategic"),
                 q.get("persona_target", "Any"), q.get("priority", "Medium")),
            )

        # Update contact persona if available
        persona = gemini_analysis.get("interviewer_persona", {})
        if contact_name and persona.get("persona_type"):
            conn.execute(
                "UPDATE contacts SET persona_type = ? WHERE job_id = ? AND name = ?",
                (persona["persona_type"], job_id, contact_name),
            )

    return {
        "transcript_id": transcript_id,
        "gemini_analysis": gemini_analysis,
        "comparison_result": comparison_result,
    }

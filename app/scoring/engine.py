"""
Deterministic scoring helpers.

Layer 1 — Auto-Reject (blocked sectors, salary floor).
Layer 4 — Adjustment Weights (substring keyword signals: tech stack, greenfield/upcycling,
          pricing, founder type, sector, remote, churn).

Layer 2 (match) lives in `match.py`; Layer 3 (LLM qualitative) lives in `research.py`'s
score_job() via SCORING_PROMPT. Final-score composition happens in `compute_final_score()`
below — single end-clamp, never mid-clamp.
"""
import re
from typing import Optional
from app.config import load_profile  # noqa: F401  — kept for callers / tests

# ---------------------------------------------------------------------------
# Signal keyword lists (Layer 4 — Adjustment Weights)
# ---------------------------------------------------------------------------

GREENFIELD_SIGNALS = [
    "build from scratch", "build from the ground up", "greenfield",
    "first hire", "founding team", "0 to 1", "zero to one",
    "create the function", "build the team", "define the strategy",
    "no existing", "new function", "stand up", "architect",
]

PROCESS_UPCYCLING_SIGNALS = [
    "clean up", "fix broken", "optimize existing", "improve current",
    "streamline", "reduce manual", "automate existing",
]

CONSUMPTION_PRICING_SIGNALS = [
    "consumption", "usage-based", "pay-as-you-go", "metered",
    "token-based", "credit-based", "outcome-based",
]

REMOTE_SIGNALS = [
    "100% remote", "fully remote", "remote-first", "work from anywhere",
]

WINDOWS_TEAMS_SIGNALS = [
    "microsoft teams", "ms teams", "windows environment",
    "microsoft 365", "o365",
]

MAC_SLACK_SIGNALS = [
    "slack", "macbook", "macos", "mac environment",
    "google workspace", "g suite",
]


def _text_lower(text: str) -> str:
    return text.lower()


def _any_signal(text: str, signals: list[str]) -> bool:
    t = _text_lower(text)
    return any(s in t for s in signals)


def _extract_salary(text: str) -> Optional[int]:
    """Highest salary figure in USD found in text, or None. Excludes funding amounts.
    Uses max so that a range like $150k–$220k only rejects if the ceiling is below floor."""
    amounts = []
    for match in re.finditer(r"\$(\d{1,3}(?:,\d{3})*)\s*[kK](?![+MmBb])", text):
        val = int(match.group(1).replace(",", "")) * 1000
        amounts.append(val)
    for match in re.finditer(r"\$(\d{1,3}(?:,\d{3})+)(?!\s*[MmBbKk])", text):
        val = int(match.group(1).replace(",", ""))
        amounts.append(val)
    if not amounts:
        return None
    return max(amounts)


# ---------------------------------------------------------------------------
# Layer 1 — Auto-Reject
# ---------------------------------------------------------------------------

_HARD_NO_KEYWORDS: dict[str, list[str]] = {
    "Traditional Healthcare": ["hospital system", "pharma", "pharmaceutical", "clinical trial",
                               "medical device", "life sciences", "health system", "health plan",
                               "insurance carrier", "managed care"],
    "Direct Government Employment": ["federal agency", "department of defense",
                                      "department of state", "department of homeland",
                                      "department of treasury", "department of justice",
                                      "ministry of ", "civil service", "government employee",
                                      "gs-13", "gs-14", "gs-15", "top secret clearance",
                                      "ts/sci", "active clearance required",
                                      "security clearance required"],
    "Utilities": ["electric utility", "water utility", "gas utility", "power grid"],
    "Adult / Pornography": ["adult content", "pornography", "adult entertainment", "onlyfans"],
    "Gambling": ["gambling", "casino", "poker", "wagering", "sports betting",
                  "online gaming", "igaming", "i-gaming", "lottery"],
    "Predatory Lending": ["payday loan", "predatory lending", "check cashing"],
    "Anything promoting immorality or racism": ["white supremac", "racist", "hate group"],
}


def _kw_match(text_lower: str, keyword: str) -> bool:
    """Word-boundary-aware match to avoid false positives from substring overlap.
    Falls back to plain substring for multi-word phrases containing spaces, where
    word boundaries on internal spaces are not meaningful."""
    pattern = r"\b" + re.escape(keyword) + r"\b"
    return bool(re.search(pattern, text_lower))


def _seed_no_go_if_empty(conn) -> None:
    """Populate no_go_industries from _HARD_NO_KEYWORDS if the table is empty."""
    count = conn.execute("SELECT COUNT(*) FROM no_go_industries").fetchone()[0]
    if count > 0:
        return
    for sector_name, keywords in _HARD_NO_KEYWORDS.items():
        for kw in keywords:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO no_go_industries (sector, keyword) VALUES (?,?)",
                    (sector_name, kw),
                )
            except Exception:
                pass


def load_no_go_keywords() -> dict[str, list[str]]:
    """Active no-go sectors→keywords from the DB (seeds from _HARD_NO_KEYWORDS on
    first run). Falls back to the hardcoded defaults on any error (e.g. no DB)."""
    try:
        from app.models import get_db
        with get_db() as conn:
            _seed_no_go_if_empty(conn)
            rows = conn.execute(
                "SELECT sector, keyword FROM no_go_industries WHERE enabled=1 ORDER BY sector, keyword"
            ).fetchall()
        result: dict[str, list[str]] = {}
        for r in rows:
            result.setdefault(r["sector"], []).append(r["keyword"])
        if result:
            return result
    except Exception:
        pass
    return _HARD_NO_KEYWORDS


def check_auto_reject(jd_text: str, profile: dict) -> tuple[bool, Optional[str]]:
    """Layer 1. Returns (is_rejected, reason)."""
    t = _text_lower(jd_text)

    salary = _extract_salary(jd_text)
    comp = profile.get("compensation", {})
    floor = comp.get("base_min", 175000)
    if salary is not None and salary < floor:
        return True, f"Below salary floor (${salary:,} < ${floor:,})"

    for sector_name, keywords in load_no_go_keywords().items():
        for kw in keywords:
            if _kw_match(t, kw):
                return True, f"Blocked sector: {sector_name}"

    return False, None


# ---------------------------------------------------------------------------
# Layer 4 — Adjustment Weights
# ---------------------------------------------------------------------------

def calculate_adjustment_weights(
    jd_text: str,
    company_info: dict,
    profile: dict = None,
) -> dict:
    """
    Layer 4. Returns dict: {adjustment_score: float (additive, can be negative),
                            flags: list[str]}.
    Does NOT add the base score and does NOT clamp — that happens at the end in
    compute_final_score().

    Separates positive and negative L4 contributions. Positive contributions are
    capped to max_l4_positive (default 2.5); negative contributions are uncapped.
    """
    if profile is None:
        profile = {}

    max_l4_positive = float(profile.get("scoring", {}).get("max_l4_positive", 2.5))

    positive = 0.0
    negative = 0.0
    flags: list[str] = []

    # Tech stack
    if _any_signal(jd_text, WINDOWS_TEAMS_SIGNALS):
        negative -= 2.0
        flags.append("windows_penalty")
    elif _any_signal(jd_text, MAC_SLACK_SIGNALS):
        positive += 1.0
        flags.append("modern_tech")

    # Founder type (from research data)
    founder_type = company_info.get("founder_type") or company_info.get("ceo_founder_type", "")
    if "2nd" in founder_type or "second" in founder_type.lower():
        positive += 1.0
        flags.append("experienced_founder")
    elif "1st" in founder_type or "first" in founder_type.lower():
        negative -= 1.0
        flags.append("first_time_founder")

    # Greenfield vs. process upcycling
    is_greenfield = _any_signal(jd_text, GREENFIELD_SIGNALS)
    is_upcycling = _any_signal(jd_text, PROCESS_UPCYCLING_SIGNALS)
    if is_greenfield:
        positive += 2.0
        flags.append("greenfield")
    elif is_upcycling and not is_greenfield:
        negative -= 1.0
        flags.append("process_upcycling")

    # Pricing model
    if _any_signal(jd_text, CONSUMPTION_PRICING_SIGNALS):
        positive += 1.0
        flags.append("modern_pricing")

    # Sector match
    sector = company_info.get("sector", "")
    if sector:
        sector_lower = sector.lower()
        if any(s in sector_lower for s in ["developer tools", "devtools", "dev ops"]):
            positive += 1.5
            flags.append("target_sector")
        elif any(s in sector_lower for s in ["ai", "ai-native", "machine learning"]):
            positive += 2.0
            flags.append("target_sector")
        elif any(s in sector_lower for s in ["fintech", "finance"]):
            positive += 1.0
            flags.append("target_sector")

    # Remote
    if _any_signal(jd_text, REMOTE_SIGNALS):
        positive += 0.5
        flags.append("remote")

    # Churn & Burn
    if company_info.get("cs_shrinking_sales_growing"):
        negative -= 1.0
        flags.append("churn_burn")

    # Warning flags (no score impact)
    if company_info.get("cfo_cro_antagonism"):
        flags.append("cfo_cro_warning")
    if company_info.get("runway_months") and company_info["runway_months"] < 18:
        flags.append("low_runway_warning")

    # Cap positive contributions; negatives are uncapped
    capped_positive = min(positive, max_l4_positive)
    score = capped_positive + negative

    return {
        "adjustment_score": round(score, 2),
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# Backward-compatible deterministic wrapper (used by older callers / tests).
# Combines Layer 1 + Layer 4. Match (Layer 2) and LLM nudge (Layer 3) are not here.
# ---------------------------------------------------------------------------

def calculate_deterministic_score(
    jd_text: str,
    company_info: dict,
    profile: dict,
) -> dict:
    """
    Legacy shape: returns deterministic_score (base + Layer 4, clamped 0-10) plus the
    auto-reject and flags. Kept so existing tests + callers don't break. New code
    should call check_auto_reject() and calculate_adjustment_weights() separately and
    pass results into compute_final_score().
    """
    rejected, reason = check_auto_reject(jd_text, profile)
    if rejected:
        return {
            "deterministic_score": 0.0,
            "flags": ["auto_rejected"],
            "auto_rejected": True,
            "reject_reason": reason,
        }

    adj = calculate_adjustment_weights(jd_text, company_info, profile)
    base = profile.get("scoring", {}).get("base_score", 3.5)
    score = round(max(0.0, min(10.0, base + adj.get("adjustment_score", 0.0))), 1)
    return {
        "deterministic_score": score,
        "flags": adj.get("flags", []),
        "auto_rejected": False,
        "reject_reason": None,
    }


# ---------------------------------------------------------------------------
# Final score composition — single end-clamp
# ---------------------------------------------------------------------------

def compute_final_score(
    *,
    profile: dict,
    auto_reject: tuple[bool, Optional[str]],
    adjustment_result: dict,
    match_result: dict,
    llm_result: dict,
) -> dict:
    """
    Combine all four layers into a single scoring record. Single clamp at the end.

      raw   = base_score (Layer 0 anchor)
            + adjustment_score (Layer 4)
            + match_score (Layer 2)
            + llm_adjustment (Layer 3)
      final = round(clamp(raw, 0.0, 10.0), 1)

    If auto-rejected, final = 0.0 and the rest is preserved for forensics.

    Top-band gate: if final > gate_threshold and gate_enabled, check 5 must-haves:
    (a) comp trajectory on target, (b) leadership role (not IC), (c) acceptable/preferred
    sector, (d) build/strategic mandate, (e) strong L2 evidence. If any fail, clamp to
    gate_threshold.
    """
    is_rejected, reason = auto_reject
    scoring_cfg = profile.get("scoring", {})
    base = float(scoring_cfg.get("base_score", 3.5))
    adj_score = float(adjustment_result.get("adjustment_score", 0.0))
    match_score = float(match_result.get("match_score", 0.0))
    llm_adj = float(llm_result.get("llm_adjustment", 0.0))
    flags = list(adjustment_result.get("flags", []))

    if is_rejected:
        return {
            "final_score": 0.0,
            "deterministic_score": 0.0,
            "adjustment_weights_score": adj_score,
            "match_score": match_score,
            "llm_adjustment": llm_adj,
            "auto_rejected": True,
            "reject_reason": reason,
            "flags": ["auto_rejected"] + flags,
            **{k: v for k, v in match_result.items() if k != "match_score"},
            **llm_result,
        }

    raw = base + adj_score + match_score + llm_adj
    final = round(max(0.0, min(10.0, raw)), 1)

    # Top-band gate: apply if final > threshold and gate enabled
    gate_threshold = float(scoring_cfg.get("top_band_gate_threshold", 7.0))
    gate_enabled = bool(scoring_cfg.get("top_band_gate_enabled", True))

    if gate_enabled and final > gate_threshold:
        # Must-haves for top-band jobs
        must_haves_pass = True

        # (a) comp trajectory — check salary if available
        comp_cfg = profile.get("compensation", {})
        floor = comp_cfg.get("base_min", 175000)
        salary_detected = llm_result.get("salary_range_detected")
        if salary_detected:
            try:
                salary_val = int("".join(c for c in salary_detected if c.isdigit()).lstrip("$")[:6])
                if salary_val < floor:
                    must_haves_pass = False
                    flags.append("gate_fail_comp")
            except (ValueError, TypeError):
                pass

        # (b) leadership role — check job title for Director/VP/Head/Lead
        job_title = llm_result.get("job_title", "").lower()
        if job_title and not any(s in job_title for s in ["director", "vp", "vice president", "head", "lead"]):
            must_haves_pass = False
            flags.append("gate_fail_leadership")

        # (c) acceptable/preferred sector — check sector and hard_no list
        sector = llm_result.get("sector", "").lower()
        hard_no_check = False
        for sector_name, keywords in _HARD_NO_KEYWORDS.items():
            for kw in keywords:
                if _kw_match(sector, kw):
                    hard_no_check = True
                    break
            if hard_no_check:
                break
        if hard_no_check:
            must_haves_pass = False
            flags.append("gate_fail_sector")

        # (d) build/strategic mandate — check L3 prose
        llm_pros = llm_result.get("pros", "").lower()
        if llm_pros and not any(s in llm_pros for s in ["build", "strategic", "greenfield"]):
            must_haves_pass = False
            flags.append("gate_fail_mandate")

        # (e) strong L2 evidence — match_score >= 2.0
        if match_score < 2.0:
            must_haves_pass = False
            flags.append("gate_fail_l2_evidence")

        if not must_haves_pass:
            final = float(gate_threshold)
            flags.append("gate_clamped")

    # `deterministic_score` is kept for backward compatibility with score history,
    # the score-breakdown UI, and Sheets — it now means "base + Layer 4 only, clamped".
    legacy_det = round(max(0.0, min(10.0, base + adj_score)), 1)

    return {
        "final_score": final,
        "deterministic_score": legacy_det,
        "adjustment_weights_score": adj_score,
        "match_score": match_score,
        "llm_adjustment": llm_adj,
        "auto_rejected": False,
        "reject_reason": None,
        "flags": flags,
        **{k: v for k, v in match_result.items() if k != "match_score"},
        **llm_result,
    }


SCORE_TIERS = [
    (9.0, "Dream Job", "gold"),
    (7.0, "Solid Bet", "green"),
    (5.0, "Worth a Look", "yellow"),
    (3.0, "Probably Skip", "orange"),
    (0.0, "Hard Pass", "red"),
]


def classify_score(score: float) -> dict:
    for threshold, label, color in SCORE_TIERS:
        if score >= threshold:
            return {"tier": label, "color": color}
    return {"tier": "Hard Pass", "color": "red"}

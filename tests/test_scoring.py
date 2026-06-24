"""
Deterministic scoring engine unit tests.
Runs against seed_data/example_jds.json and example_scores.json.
No LLM calls — tests the rules layer only.
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SEED_JDS = json.loads((ROOT / "seed_data" / "example_jds.json").read_text())
SEED_SCORES = {s["id"]: s for s in json.loads((ROOT / "seed_data" / "example_scores.json").read_text())}


def get_profile():
    import yaml
    # Personal profile is gitignored; fall back to the seed profile in CI
    for fname in ("candidate_profile.yaml", "seed_data/seed_profile.yaml"):
        path = ROOT / fname
        if path.exists():
            with open(path) as f:
                return yaml.safe_load(f)
    return {}


from app.scoring.engine import check_auto_reject, calculate_deterministic_score


@pytest.mark.parametrize("seed", SEED_JDS)
def test_auto_reject_flags(seed):
    profile = get_profile()
    rejected, reason = check_auto_reject(seed["jd_text"], profile)
    expected = SEED_SCORES[seed["id"]]

    if expected["auto_rejected"]:
        assert rejected, f"{seed['id']}: expected auto-reject, got score"
        assert reason is not None
    else:
        assert not rejected, f"{seed['id']}: unexpected auto-reject — {reason}"


def test_seed_001_forgeline_max_score():
    """Forgeline hits every positive signal — L4 cap at 2.5 means deterministic max is base+cap=6.0."""
    profile = get_profile()
    seed = next(s for s in SEED_JDS if s["id"] == "seed_001")
    result = calculate_deterministic_score(seed["jd_text"], {"sector": "Developer Tools"}, profile)
    assert result["auto_rejected"] is False
    assert result["deterministic_score"] == 6.0
    assert "greenfield" in result["flags"]
    assert "modern_tech" in result["flags"]
    assert "modern_pricing" in result["flags"]
    assert "remote" in result["flags"]
    assert "target_sector" in result["flags"]


def test_seed_002_acme_healthtech_allowed():
    """Acme Corp is a *healthcare technology* (HealthTech SaaS) company. The profile
    blocks only Traditional Healthcare (hospital systems, pharma, managed care) and
    explicitly allows HealthTech SaaS — so this must NOT auto-reject. It should score
    low instead: a Windows/Teams shop with a process-cleanup RevOps role.
    See candidate_profile.yaml (Traditional Healthcare block + 'HealthTech SaaS allowed')."""
    profile = get_profile()
    seed = next(s for s in SEED_JDS if s["id"] == "seed_002")
    result = calculate_deterministic_score(seed["jd_text"], {}, profile)
    assert result["auto_rejected"] is False
    assert result["reject_reason"] is None
    assert result["deterministic_score"] == 1.5  # base 3.5 - windows_penalty 2.0
    assert "windows_penalty" in result["flags"]


def test_seed_003_sentora_max_score():
    """Sentora — greenfield DevTools with consumption pricing. L4 cap means deterministic lands at 6.0."""
    profile = get_profile()
    seed = next(s for s in SEED_JDS if s["id"] == "seed_003")
    result = calculate_deterministic_score(seed["jd_text"], {"sector": "Developer Tools"}, profile)
    assert result["auto_rejected"] is False
    assert result["deterministic_score"] == 6.0
    assert "target_sector" in result["flags"]


def test_seed_004_midco_worth_a_look():
    """MidCo — modern tech but no greenfield/sector bonus. base 3.5 + modern_tech 1.0 = 4.5."""
    profile = get_profile()
    seed = next(s for s in SEED_JDS if s["id"] == "seed_004")
    result = calculate_deterministic_score(seed["jd_text"], {}, profile)
    assert result["auto_rejected"] is False
    assert 4.0 <= result["deterministic_score"] <= 6.0
    assert "modern_tech" in result["flags"]
    assert "greenfield" not in result["flags"]


def test_seed_005_pokerstars_ethics_reject():
    """PokerStars — Gambling must reject regardless of comp."""
    profile = get_profile()
    seed = next(s for s in SEED_JDS if s["id"] == "seed_005")
    result = calculate_deterministic_score(seed["jd_text"], {}, profile)
    assert result["auto_rejected"] is True
    assert "Gambling" in result["reject_reason"]


def test_salary_below_floor_rejects():
    """JD with salary explicitly below $175k must auto-reject."""
    profile = get_profile()
    jd = "Director of Operations. Compensation: $80,000-$100,000. Modern SaaS company."
    rejected, reason = check_auto_reject(jd, profile)
    assert rejected
    assert "salary floor" in reason.lower()


def test_unknown_salary_does_not_reject():
    """No salary mentioned must NOT auto-reject."""
    profile = get_profile()
    jd = "VP of GTM Operations. Competitive compensation. Remote-first SaaS."
    rejected, reason = check_auto_reject(jd, profile)
    assert not rejected


def test_windows_teams_penalty():
    """Windows + Teams environment should apply -2.0 penalty."""
    profile = get_profile()
    jd = "Director of RevOps. We use Microsoft Teams and a Windows environment. Series B SaaS."
    result = calculate_deterministic_score(jd, {}, profile)
    assert "windows_penalty" in result["flags"]
    assert result["deterministic_score"] <= 4.0  # 3.5 - 2.0 + possible other adjustments


def test_score_clamped_to_ceiling():
    """Score cannot exceed 10.0 regardless of how many bonuses stack."""
    profile = get_profile()
    jd = (
        "VP GTM Ops. Greenfield build from scratch. Consumption-based pricing. "
        "100% remote. Slack and Google Workspace. MacBook. DevTools SaaS."
    )
    result = calculate_deterministic_score(jd, {"sector": "Developer Tools"}, profile)
    assert result["deterministic_score"] <= 10.0


def test_score_clamped_to_floor():
    """Score cannot go below 0.0."""
    profile = get_profile()
    jd = "Operations role. Microsoft Teams. Windows. Optimize existing broken processes."
    result = calculate_deterministic_score(jd, {}, profile)
    assert result["deterministic_score"] >= 0.0

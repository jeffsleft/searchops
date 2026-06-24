"""
WP-F voice add-on tests. Deterministic Inspector is pinned exactly; the Weaver
(LLM) path is exercised with a fake provider so no network/keys are needed.
"""
import pytest

from app import config, voice
from app.voice import inspector


class FakeProvider:
    """Stand-in LLM. `generate` returns whatever clean text we hand it."""
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def generate(self, prompt):
        self.calls += 1
        return self.reply


@pytest.fixture(autouse=True)
def _voice_on(monkeypatch):
    # Default state for tests: enabled, built-in guide.
    monkeypatch.setattr(config, "VOICE_ENABLED", True)
    monkeypatch.setattr(config, "VOICE_GUIDE_PATH", "")


# ---- Inspector (deterministic) -------------------------------------------

def test_builtin_guide_loads():
    g = voice.inspector.load_guide("cover-letter")
    assert g["label"] == inspector.BUILTIN_LABEL
    assert len(g["rules"]) > 20  # base + cover-letter overlay merged


def test_scan_flags_ai_tells():
    g = inspector.load_guide("cover-letter")
    text = "Furthermore, I am passionate about leveraging synergy."
    terms = {v["term"] for v in inspector.scan(text, g)}
    assert "furthermore" in terms
    assert "passionate" in terms      # cover-letter overlay
    assert "leveraging" in terms
    assert "synergy" in terms


def test_word_match_respects_boundaries():
    g = inspector.load_guide("cover-letter")
    # "leverage" (word rule) must not fire on "leveraged"
    assert inspector.scan("I leveraged the data.", g) == [] or all(
        v["term"] != "leverage" for v in inspector.scan("I leveraged the data.", g)
    )


def test_per_piece_limit():
    g = inspector.load_guide("cover-letter")
    once = inspector.scan("Ultimately it shipped.", g)
    twice = inspector.scan("Ultimately, ultimately it shipped.", g)
    assert all(v["term"] != "ultimately" for v in once)      # under limit (1)
    assert any(v["term"] == "ultimately" for v in twice)     # over limit


def test_clean_text_no_violations():
    g = inspector.load_guide("cover-letter")
    assert inspector.scan("I built the renewals model and cut churn forecasting time in half.", g) == []


def test_custom_guide_overrides_builtin(tmp_path, monkeypatch):
    p = tmp_path / "myguide.yaml"
    p.write_text("forbidden:\n  - phrase: \"zibblefrotz\"\n    reason: ai-tell\n")
    monkeypatch.setattr(config, "VOICE_GUIDE_PATH", str(p))
    assert voice.active_guide_label() == f"Custom ({p.name})"
    # built-in tells no longer flagged; the custom one is
    assert voice.scan_text("furthermore this is fine") == []
    assert any(v["term"] == "zibblefrotz" for v in voice.scan_text("zibblefrotz"))


# ---- polish_text (Inspector + Weaver) ------------------------------------

def test_polish_rewrites_when_flagged():
    clean = "I built the renewals model and cut forecasting time in half."
    new_text, rep = voice.polish_text(
        "Furthermore, I am passionate about leveraging synergy.",
        provider=FakeProvider(clean),
    )
    assert new_text == clean
    assert rep["enabled"] and rep["before"] > 0 and rep["after"] == 0 and rep["rewritten"]


def test_polish_skips_clean_text():
    clean = "I led the GTM systems rebuild at a 500-person SaaS company."
    fp = FakeProvider("SHOULD NOT BE USED")
    new_text, rep = voice.polish_text(clean, provider=fp)
    assert new_text == clean and rep["before"] == 0 and not rep["rewritten"]
    assert fp.calls == 0  # no LLM call when nothing is flagged


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    text = "Furthermore, I am passionate about leveraging synergy."
    new_text, rep = voice.polish_text(text, provider=FakeProvider("clean"))
    assert new_text == text and rep["enabled"] is False


def test_weaver_failure_keeps_original():
    class Boom:
        def generate(self, prompt):
            raise RuntimeError("provider down")
    text = "Furthermore, I am passionate about synergy."
    new_text, rep = voice.polish_text(text, provider=Boom())
    assert new_text == text          # never raises; falls back to original
    assert rep["before"] > 0 and rep["after"] > 0


# ---- polish_cover_letter --------------------------------------------------

def test_polish_cover_letter_enabled():
    cl = {
        "salutation": "Dear Hiring Team,",
        "body": ["Furthermore, I am passionate about leveraging synergy.", "Clean second paragraph."],
        "closing": "Sincerely,",
    }
    out = voice.polish_cover_letter(cl, provider=FakeProvider("I built the thing."))
    assert out["body"][0] == "I built the thing."
    assert out["body"][1] == "Clean second paragraph."   # untouched (no flags)
    assert out["voice"]["enabled"] and out["voice"]["before"] > 0 and out["voice"]["rewritten"]


def test_polish_cover_letter_disabled_still_returns(monkeypatch):
    monkeypatch.setattr(config, "VOICE_ENABLED", False)
    cl = {"body": ["Furthermore, synergy."], "closing": "Sincerely,"}
    out = voice.polish_cover_letter(cl, provider=FakeProvider("x"))
    assert out["body"] == ["Furthermore, synergy."]      # generation unaffected
    assert out["voice"]["enabled"] is False

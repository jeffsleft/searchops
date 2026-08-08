"""
Regression test for a real bug found via manual QA (2026-07-29): buttons in
target_detail_panel.html combined an hx-post trigger with an inline
onclick="this.disabled=true" handler. The onclick attribute fires before
htmx's own addEventListener-registered click handler, so by the time htmx
evaluates the triggering element it's already disabled -- and htmx.min.js
silently skips issuing the request for a disabled element. The button visibly
relabels ("Generating...") and looks like it worked, but no request ever
fires. Confirmed live: Generate/Re-run Gap Hypothesis, Scan now, Research,
and Force Refresh were all affected.

Fix: moved the disable+relabel to the htmx:beforeRequest event (base.html's
data-htmx-before-label dispatcher), which only fires after htmx has already
committed to the request.
"""
import re

from app.routes import jinja


def _render_target_panel():
    return jinja.get_template("components/target_detail_panel.html").render(
        target={"id": 42, "name": "Acme", "tier_a": True}
    )


def test_no_button_disables_itself_before_htmx_can_fire():
    html = _render_target_panel()
    # A button carrying both hx-post/hx-get and an onclick that sets
    # disabled=true on itself is the exact bug pattern -- htmx respects
    # `.disabled` on the triggering element and silently no-ops.
    buttons = re.findall(r"<button\b[^>]*>", html)
    assert buttons, "expected at least one <button> in target_detail_panel.html"
    for btn in buttons:
        has_hx_trigger = "hx-post" in btn or "hx-get" in btn
        self_disables = "disabled=true" in btn or "disabled = true" in btn
        assert not (has_hx_trigger and self_disables), (
            f"button races htmx's click handler by disabling itself inline: {btn}"
        )


def test_action_buttons_use_the_before_request_dispatcher():
    html = _render_target_panel()
    assert 'data-htmx-before-label="Generating' in html
    assert 'data-htmx-before-label="Researching' in html
    assert 'data-htmx-before-label="Refreshing' in html

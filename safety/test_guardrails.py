import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "safety"))

from guardrails import check_allowlist, is_risky_step, redact_sensitive  # noqa: E402
from schemas.artifact_schema import Locator, LocatorStrategy, Step  # noqa: E402


def test_allowed_route_passes():
    ok, reason = check_allowlist("http://localhost:5050/search", "navigate")
    assert ok is True
    assert reason == ""


def test_route_outside_allowed_domain_is_rejected():
    ok, reason = check_allowlist("http://evil.com/anything", "navigate")
    assert ok is False
    assert "evil.com" in reason
    assert "domain" in reason.lower()


def test_disallowed_action_type_is_rejected():
    ok, reason = check_allowlist("http://localhost:5050/search", "hover")
    assert ok is False
    assert "action type" in reason.lower()


def test_submit_click_is_risky():
    submit_step = Step(
        step_id=1,
        action="click",
        locator=Locator(primary=LocatorStrategy(strategy="visible_text", value="Submit")),
    )
    assert is_risky_step(submit_step) is True


def test_search_and_login_clicks_are_not_risky():
    search_step = Step(
        step_id=1,
        action="click",
        locator=Locator(primary=LocatorStrategy(strategy="visible_text", value="Search")),
    )
    login_step = Step(
        step_id=2,
        action="click",
        locator=Locator(primary=LocatorStrategy(strategy="visible_text", value="Login")),
    )
    assert is_risky_step(search_step) is False
    assert is_risky_step(login_step) is False


def test_redact_sensitive_redacts_nested_password_only():
    data = {
        "username": "tester",
        "login_info": {"password": "hunter2", "note": "keep me"},
        "steps": [{"action": "type", "value": "hunter2"}],
    }

    redacted = redact_sensitive(data)

    assert redacted["username"] == "tester"
    assert redacted["login_info"]["password"] == "[REDACTED]"
    assert redacted["login_info"]["note"] == "keep me"
    assert redacted["steps"][0]["value"] == "hunter2"  # not under a sensitive key name
    assert data["login_info"]["password"] == "hunter2"  # original left untouched

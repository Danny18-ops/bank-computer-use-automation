import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "escalation"))

from handoff import raise_intervention, wait_for_resume  # noqa: E402


class _MockPage:
    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"fake-png-bytes")


def test_raise_intervention_creates_expected_files():
    page = _MockPage()
    intervention_path = raise_intervention(
        reason="locator_not_found",
        goal="Test goal",
        step_id=5,
        page=page,
        context={"locator": "primary=visible_text:'Submit'"},
    )

    folder = Path(intervention_path)
    assert folder.is_dir()
    assert (folder / "screenshot.png").exists()
    assert (folder / "PENDING").exists()

    request_data = json.loads((folder / "request.json").read_text())
    assert request_data["goal"] == "Test goal"
    assert request_data["step_id"] == 5
    assert request_data["reason"] == "locator_not_found"
    assert request_data["screenshot"] == str(folder / "screenshot.png")
    assert request_data["context"]["locator"] == "primary=visible_text:'Submit'"


def test_wait_for_resume_picks_up_human_resume():
    page = _MockPage()
    intervention_path = raise_intervention(
        reason="locator_not_found", goal="Test goal", step_id=1, page=page
    )
    folder = Path(intervention_path)

    # Simulate a human resuming via the operator console, bypassing the Flask UI.
    (folder / "RESUMED").write_text("looks fine now, go ahead")

    result = wait_for_resume(intervention_path, poll_interval=0.1, timeout=5)

    assert result["status"] == "resumed"
    assert result["notes"] == "looks fine now, go ahead"
    assert result["goal"] == "Test goal"
    assert (folder / "intervention_log.json").exists()
    assert not (folder / "PENDING").exists()  # cleaned up once resumed


def test_wait_for_resume_times_out_if_never_resumed():
    page = _MockPage()
    intervention_path = raise_intervention(
        reason="locator_not_found", goal="Test goal", step_id=2, page=page
    )

    start = time.monotonic()
    result = wait_for_resume(intervention_path, poll_interval=0.2, timeout=2)
    elapsed = time.monotonic() - start

    assert result["status"] == "timeout"
    assert elapsed < 5  # sanity check: didn't hang well past the requested timeout

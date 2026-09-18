"""
Human-in-the-loop escalation: pause a stuck replay run, hand it to a person via
evidence/interventions/<timestamp>/ + the operator console, and resume once they've
looked at it.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INTERVENTIONS_DIR = PROJECT_ROOT / "evidence" / "interventions"
OPERATOR_CONSOLE_BASE_URL = "http://localhost:5099"


def raise_intervention(reason: str, goal: str, step_id: int, page, context: Optional[dict] = None) -> str:
    """Pauses for human review: saves a screenshot + request.json + PENDING marker
    under evidence/interventions/<timestamp>/, and returns that folder's path."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    folder = INTERVENTIONS_DIR / timestamp
    folder.mkdir(parents=True, exist_ok=True)

    screenshot_path = folder / "screenshot.png"
    page.screenshot(path=str(screenshot_path))

    request_data = {
        "timestamp": timestamp,
        "goal": goal,
        "step_id": step_id,
        "reason": reason,
        "screenshot": str(screenshot_path),
        "context": context or {},
    }
    (folder / "request.json").write_text(json.dumps(request_data, indent=2))
    (folder / "PENDING").write_text("")

    return str(folder)


def wait_for_resume(intervention_path: str, poll_interval: float = 1.0, timeout: float = 600) -> dict:
    """Polls until RESUMED appears (or `timeout` seconds elapse), printing where to
    go to resolve it. Returns the merged intervention_log dict, or a timeout dict."""
    folder = Path(intervention_path)
    pending_path = folder / "PENDING"
    resumed_path = folder / "RESUMED"
    request_json_path = folder / "request.json"

    request_data: dict = {}
    if request_json_path.exists():
        request_data = json.loads(request_json_path.read_text())

    console_url = f"{OPERATOR_CONSOLE_BASE_URL}/intervention/{folder.name}"
    deadline = time.monotonic() + timeout

    while True:
        if resumed_path.exists():
            notes = resumed_path.read_text().strip()
            combined = {
                **request_data,
                "status": "resumed",
                "resumed_at": datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
                "notes": notes,
            }
            (folder / "intervention_log.json").write_text(json.dumps(combined, indent=2))
            if pending_path.exists():
                pending_path.unlink()
            return combined

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"[handoff] Timed out waiting for human resume at {intervention_path}")
            return {
                "status": "timeout",
                "intervention_path": str(folder),
                "step_id": request_data.get("step_id"),
                "reason": request_data.get("reason"),
            }

        print(
            f"[handoff] Waiting for human intervention (step {request_data.get('step_id')}, "
            f"reason: {request_data.get('reason')}) - open {console_url} to review and resume..."
        )
        time.sleep(min(poll_interval, remaining))

"""
Operator console: a minimal, unstyled Flask app for reviewing and resuming replay
runs that paused for human intervention. Function over form - no CSS, no JS.
Run separately from the replay engine: python escalation/operator_console.py
"""

from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, abort, redirect, render_template_string, request, send_file, url_for

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INTERVENTIONS_DIR = PROJECT_ROOT / "evidence" / "interventions"

app = Flask(__name__)

INDEX_TEMPLATE = """
<h1>Operator Console</h1>
{% if pending %}
<p>Pending interventions:</p>
<ul>
{% for ts in pending %}
  <li><a href="{{ url_for('show_intervention', timestamp=ts) }}">{{ ts }}</a></li>
{% endfor %}
</ul>
{% else %}
<p>No interventions are currently pending.</p>
{% endif %}
"""

INTERVENTION_TEMPLATE = """
<h1>Intervention: {{ timestamp }}</h1>
<table border="1" cellpadding="4">
<tr><td>Goal</td><td>{{ data.get('goal', '') }}</td></tr>
<tr><td>Step</td><td>{{ data.get('step_id', '') }}</td></tr>
<tr><td>Reason</td><td>{{ data.get('reason', '') }}</td></tr>
</table>
<p><img src="{{ url_for('intervention_screenshot', timestamp=timestamp) }}" width="800"></p>
<form method="POST" action="{{ url_for('resume_intervention', timestamp=timestamp) }}">
<p>Notes (optional):</p>
<textarea name="notes" rows="4" cols="60"></textarea>
<p><button type="submit">Resume</button></p>
</form>
"""

RESUMED_TEMPLATE = """
<h1>Resumed</h1>
<p>Intervention {{ timestamp }} has been marked as resumed. You can close this tab
and return to the terminal running the replay.</p>
"""


def _intervention_folder(timestamp: str) -> Path:
    folder = INTERVENTIONS_DIR / timestamp
    if not folder.is_dir():
        abort(404)
    return folder


def _pending_interventions() -> list[str]:
    if not INTERVENTIONS_DIR.exists():
        return []
    return sorted(
        p.name for p in INTERVENTIONS_DIR.iterdir() if p.is_dir() and (p / "PENDING").exists()
    )


@app.route("/")
def index():
    return render_template_string(INDEX_TEMPLATE, pending=_pending_interventions())


@app.route("/intervention/<timestamp>")
def show_intervention(timestamp):
    folder = _intervention_folder(timestamp)
    request_path = folder / "request.json"
    data = json.loads(request_path.read_text()) if request_path.exists() else {}
    return render_template_string(INTERVENTION_TEMPLATE, timestamp=timestamp, data=data)


@app.route("/intervention/<timestamp>/screenshot.png")
def intervention_screenshot(timestamp):
    folder = _intervention_folder(timestamp)
    screenshot_path = folder / "screenshot.png"
    if not screenshot_path.exists():
        abort(404)
    return send_file(screenshot_path, mimetype="image/png")


@app.route("/intervention/<timestamp>/resume", methods=["POST"])
def resume_intervention(timestamp):
    folder = _intervention_folder(timestamp)
    notes = request.form.get("notes", "")

    pending_path = folder / "PENDING"
    if pending_path.exists():
        pending_path.unlink()
    (folder / "RESUMED").write_text(notes)

    return redirect(url_for("resumed_confirmation", timestamp=timestamp))


@app.route("/intervention/<timestamp>/resumed")
def resumed_confirmation(timestamp):
    _intervention_folder(timestamp)
    return render_template_string(RESUMED_TEMPLATE, timestamp=timestamp)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5099, debug=True)

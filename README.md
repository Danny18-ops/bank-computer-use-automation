# computer-use-automation

## 1. Overview

This project explores LLM-driven browser automation against **MockBank**, a small
fake internal bank app built for testing (`mock_app/`). An LLM-powered discovery
agent (`agent/loop.py`) figures out how to complete a task on the app it's never
seen before, purely by looking at each page and deciding what to click/type/select
next. A successful run is converted into a **capability artifact** — a fixed,
typed, replayable recipe (`schemas/artifact_schema.py`, `agent/build_artifact.py`)
— which a separate, fully deterministic **replay engine** (`replay/engine.py`) can
then run again and again with different inputs, with no LLM involved at all.
Guardrails (`safety/guardrails.py`) constrain what replay is allowed to do, and a
human-in-the-loop escalation mechanism (`escalation/`) pauses and asks for help
when replay gets stuck instead of failing silently or guessing.

## 2. Setup

Requires Python 3.14+ (this project's venv was created with `python3 -m venv venv`
on Python 3.14.3).

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install
```

Then create a `.env` file in the project root with your OpenAI key (only the
discovery agent needs it; the replay engine never calls an LLM):

```
OPENAI_API_KEY=sk-...
```

## 3. How to run

You'll want three terminals. **`mock_app` must already be running** before you
start the discovery agent or the replay engine — both talk to
`http://localhost:5050` directly and will fail to connect otherwise (see note in
section 6).

**Terminal 1 — the app under test (always required):**
```bash
source venv/bin/activate
python mock_app/app.py
```
Runs on http://localhost:5050. Redirects to a login page; any username/password
works.

**Terminal 2 — the operator console (only needed if a replay run gets stuck):**
```bash
source venv/bin/activate
python escalation/operator_console.py
```
Runs on http://localhost:5099. The replay engine launches a visible (non-headless)
browser; if it can't resolve a step on its own, it pauses and raises an
intervention here — this console lists pending interventions (screenshot + reason
+ a notes box) and a "Resume" button lets the run continue.

**Terminal 3 — actual work** (discovery, artifact building, replay, tests — see
sections 4 and 5 below).

## 4. Demo path

With `mock_app` running (Terminal 1), from Terminal 3:

**Run a fresh discovery:**
```bash
source venv/bin/activate
python agent/loop.py
```
Drives a real browser end-to-end (log in, search member 10001, open a Savings
sub-account with a $50 deposit, reach the confirmation screen) and writes evidence
to `evidence/discovery_run_<timestamp>/` (screenshots + `log.json`). The exact
folder name is printed at the end of the run.

**Turn that run into a replayable artifact:**
```bash
python agent/build_artifact.py evidence/discovery_run_<timestamp>/log.json
```
(substitute the timestamp printed above). Prints the generated `artifact_id` and
the exact path it was saved to — normally `artifacts/<artifact_id>_v1.json`, but
if a file with that name already exists (e.g. `artifacts/search_for_member_10001_open_a_sub_account_v1.json`
is already checked in from a prior run against the same goal text), it won't be
overwritten — the script appends `_generated` (or `_generated2`, etc.) instead and
prints that path. Use whatever path it actually printed in the next step.

**Replay the artifact with different inputs** (e.g. a different member and
amount than the discovery run used):
```bash
python replay/engine.py artifacts/<artifact_id>_v1.json \
  '{"username": "tester", "password": "pw", "member_id": "10002", "account_type": "Savings", "initial_deposit": 75, "confirmed": true}'
```
`confirmed: true` is required because the final Submit click is flagged as a
risky/irreversible action by `safety/guardrails.py`; omit it to see the replay
stop with `status: "requires_confirmation"` instead. Prints the result dict
(`status`, extracted `outputs` such as `reference_number`, and where the
evidence — screenshot + `log.json` — was saved under `evidence/replay_<timestamp>/`).

## 5. Running the tests

With `mock_app` running (some tests, like `replay/test_engine.py`, drive a real
browser against it):

```bash
source venv/bin/activate
python -m pytest schemas/ agent/ replay/ safety/ escalation/ -v
```

## 6. Important: start mock_app first

`agent/loop.py` and `replay/engine.py` both connect directly to
`http://localhost:5050`. If `mock_app/app.py` isn't running yet, discovery will
fail to load the login page and replay will fail (or, for a login-required route,
sit on a connection error) before it ever gets to execute a step. Always start
Terminal 1 (`python mock_app/app.py`) before running anything in Terminal 3.

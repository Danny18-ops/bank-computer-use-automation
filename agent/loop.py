"""
LLM-driven discovery agent: drives a browser step by step toward a natural-language
goal on an app it has never seen before, using perceive -> decide -> act.

perceive() reads the page purely via DOM structure / visible text (no id/class
attributes, since the target app intentionally has none). decide() asks an LLM what
to do next, validated against a strict Pydantic schema. act() performs that one step
with Playwright. run_discovery() wires the three together into a loop and records
full evidence (screenshots + a JSON log) for every step.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from openai import BadRequestError, NotFoundError, OpenAI
from playwright.sync_api import Page, sync_playwright
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

PRIMARY_MODEL = "gpt-4o-mini"
FALLBACK_MODEL = "gpt-4o"


# ---------------------------------------------------------------------------
# perceive()
# ---------------------------------------------------------------------------

_PERCEIVE_JS = r"""
() => {
  function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function labelFor(node) {
    const wrapper = node.closest('label');
    if (wrapper) {
      const clone = wrapper.cloneNode(true);
      clone.querySelectorAll('input, select, textarea').forEach(n => n.remove());
      const text = clone.textContent.replace(/\s+/g, ' ').trim();
      if (text) return text;
    }
    const cell = node.closest('td, th');
    const row = node.closest('tr');
    if (cell && row) {
      const cells = Array.prototype.slice.call(row.children);
      const idx = cells.indexOf(cell);
      const before = cells.slice(0, idx)
        .map(c => c.textContent.replace(/\s+/g, ' ').trim())
        .filter(Boolean);
      if (before.length) return before[before.length - 1];
    }
    let sib = node.previousElementSibling;
    while (sib) {
      const text = sib.textContent.replace(/\s+/g, ' ').trim();
      if (text) return text;
      sib = sib.previousElementSibling;
    }
    return node.getAttribute('placeholder') || node.getAttribute('name') || '';
  }

  const inputs = [];
  document.querySelectorAll('input, select, textarea').forEach(el => {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'select' ? 'select'
      : tag === 'textarea' ? 'textarea'
      : (el.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'input' && ['hidden', 'submit', 'button'].includes(type)) return;
    if (!isVisible(el)) return;
    const entry = { label: labelFor(el), type: type };
    if (tag === 'select') {
      entry.options = Array.from(el.options).map(o => o.textContent.trim());
    }
    inputs.push(entry);
  });

  const buttons = [];
  document.querySelectorAll('button').forEach(el => {
    if (!isVisible(el)) return;
    const text = el.textContent.replace(/\s+/g, ' ').trim();
    if (text) buttons.push({ text: text });
  });
  document.querySelectorAll('input[type="submit"], input[type="button"]').forEach(el => {
    if (!isVisible(el)) return;
    const text = (el.getAttribute('value') || '').trim();
    if (text) buttons.push({ text: text });
  });

  const links = [];
  document.querySelectorAll('a[href]').forEach(el => {
    if (!isVisible(el)) return;
    const text = el.textContent.replace(/\s+/g, ' ').trim();
    if (text) links.push({ text: text });
  });

  return { inputs: inputs, buttons: buttons, links: links };
}
"""


def _summarize_text(text: str, max_len: int = 2000) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    summary = "\n".join(lines)
    if len(summary) > max_len:
        summary = summary[:max_len] + "... [truncated]"
    return summary


def perceive(page: Page) -> dict:
    """Return a simplified, structured description of the current page."""
    body_text = page.locator("body").inner_text()
    extracted = page.evaluate(_PERCEIVE_JS)
    return {
        "url": page.url,
        "visible_text_summary": _summarize_text(body_text),
        "inputs": extracted["inputs"],
        "buttons": extracted["buttons"],
        "links": extracted["links"],
    }


# ---------------------------------------------------------------------------
# decide()
# ---------------------------------------------------------------------------

DECISION_SYSTEM_PROMPT = """You are a browser automation agent. You control a web \
browser one step at a time to accomplish a goal on a web application you have never \
seen before.

You are given:
- goal: what you are ultimately trying to accomplish
- page_state: a snapshot of the CURRENT page (its url, a summary of its visible text, \
and lists of the inputs/buttons/links currently on screen)
- history: the actions you have already taken, in order, and whether each succeeded

Respond with EXACTLY one action to take next, and nothing else - no prose, no \
markdown, just a JSON object matching this schema:

{
  "action": "click" | "type" | "select" | "navigate" | "done" | "stuck",
  "target_text": "<label or visible text of the element to act on>",
  "value": "<text to type, or option to select>",
  "reasoning": "<one sentence why>"
}

Rules:
- "target_text" identifies WHICH element to act on: for "type" and "select" it must \
exactly match an entry in page_state.inputs[].label; for "click" and "navigate" it must \
match an entry in page_state.buttons[].text or page_state.links[].text. Omit \
target_text entirely for "done" and "stuck".
- "value" is only used for "type" (the text to type into the input) and "select" (the \
option text to choose, matching one of that input's options). Omit it for every other \
action.
- If the current page is a login form asking for a username and password and the goal \
does not specify credentials, enter any non-empty username and password - internal \
test systems like this one accept any credentials.
- Use "done" once page_state clearly shows the goal has been achieved.
- Use "stuck" if you have tried reasonable actions and cannot find a way to make \
progress, or the page shows an error you cannot resolve.
- Never invent an element that is not present in page_state.
"""


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["click", "type", "select", "navigate", "done", "stuck"]
    target_text: Optional[str] = None
    value: Optional[str] = None
    reasoning: str

    @model_validator(mode="after")
    def _check_field_shape(self) -> "Decision":
        if self.action in ("done", "stuck"):
            if self.target_text:
                raise ValueError(f"target_text must be omitted for action {self.action!r}")
        elif not self.target_text:
            raise ValueError(f"target_text is required for action {self.action!r}")

        if self.action in ("type", "select"):
            if not self.value:
                raise ValueError(f"value is required for action {self.action!r}")
        elif self.value:
            raise ValueError(f"value must be omitted for action {self.action!r}")
        return self


def _build_user_message(goal: str, page_state: dict, history: list[dict]) -> str:
    history_summary = []
    for item in history:
        decision = item.get("decision") or {}
        history_summary.append(
            {
                "action": decision.get("action"),
                "target_text": decision.get("target_text"),
                "value": decision.get("value"),
                "result": item.get("result"),
            }
        )
    payload = {"goal": goal, "page_state": page_state, "history": history_summary}
    return json.dumps(payload, indent=2)


def _request_decision(client: OpenAI, messages: list[dict], model: str) -> Decision:
    if hasattr(client.beta.chat.completions, "parse"):
        completion = client.beta.chat.completions.parse(
            model=model,
            messages=messages,
            response_format=Decision,
        )
        message = completion.choices[0].message
        if message.refusal:
            raise ValueError(f"model refused to answer: {message.refusal}")
        if message.parsed is None:
            raise ValueError("model returned no parsable content")
        return message.parsed

    completion = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0,
    )
    content = completion.choices[0].message.content or ""
    return Decision.model_validate(json.loads(content))


def _call_with_model_fallback(client: OpenAI, messages: list[dict]) -> Decision:
    last_model_error: Exception | None = None
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        try:
            return _request_decision(client, messages, model)
        except (NotFoundError, BadRequestError) as exc:
            last_model_error = exc
            continue
    raise RuntimeError(
        f"None of the configured models ({PRIMARY_MODEL}, {FALLBACK_MODEL}) are "
        f"available: {last_model_error}"
    ) from last_model_error


def decide(
    goal: str,
    page_state: dict,
    history: list[dict],
    client: Optional[OpenAI] = None,
) -> Decision:
    """Ask the LLM what single action to take next, validated against `Decision`."""
    client = client or OpenAI()
    messages: list[dict] = [
        {"role": "system", "content": DECISION_SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(goal, page_state, history)},
    ]

    last_error: Exception | None = None
    for _ in range(2):
        try:
            return _call_with_model_fallback(client, messages)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_error = exc
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Your previous response was invalid: {exc}. Reply again with "
                        "ONLY a corrected JSON object matching the required schema."
                    ),
                }
            )

    raise RuntimeError(
        f"decide(): model failed to produce a valid decision after 2 attempts: {last_error}"
    ) from last_error


# ---------------------------------------------------------------------------
# act()
# ---------------------------------------------------------------------------

_LABEL_FOR_ELEMENT_JS = r"""
(el) => {
  function labelFor(node) {
    const wrapper = node.closest('label');
    if (wrapper) {
      const clone = wrapper.cloneNode(true);
      clone.querySelectorAll('input, select, textarea').forEach(n => n.remove());
      const text = clone.textContent.replace(/\s+/g, ' ').trim();
      if (text) return text;
    }
    const cell = node.closest('td, th');
    const row = node.closest('tr');
    if (cell && row) {
      const cells = Array.prototype.slice.call(row.children);
      const idx = cells.indexOf(cell);
      const before = cells.slice(0, idx)
        .map(c => c.textContent.replace(/\s+/g, ' ').trim())
        .filter(Boolean);
      if (before.length) return before[before.length - 1];
    }
    let sib = node.previousElementSibling;
    while (sib) {
      const text = sib.textContent.replace(/\s+/g, ' ').trim();
      if (text) return text;
      sib = sib.previousElementSibling;
    }
    return node.getAttribute('placeholder') || node.getAttribute('name') || '';
  }
  return labelFor(el);
}
"""


def _find_clickable(page: Page, target_text: str):
    for role in ("button", "link"):
        locator = page.get_by_role(role, name=target_text)
        for i in range(locator.count()):
            candidate = locator.nth(i)
            if candidate.is_visible():
                return candidate
    return None


def _find_input_by_label(page: Page, label_text: str):
    target = label_text.strip().lower()
    for candidate in page.locator("input, select, textarea").all():
        if not candidate.is_visible():
            continue
        tag = candidate.evaluate("el => el.tagName.toLowerCase()")
        if tag == "input":
            input_type = (candidate.get_attribute("type") or "text").lower()
            if input_type in ("hidden", "submit", "button"):
                continue
        label = candidate.evaluate(_LABEL_FOR_ELEMENT_JS) or ""
        if target in label.strip().lower():
            return candidate
    return None


def act(page: Page, decision: Decision) -> None:
    """Perform the one action described by `decision` on `page`."""
    action = decision.action

    if action in ("click", "navigate"):
        if not decision.target_text:
            raise ValueError(f"act(): '{action}' requires target_text")
        element = _find_clickable(page, decision.target_text)
        if element is None:
            raise ValueError(
                f"act(): no visible button or link found matching text {decision.target_text!r}"
            )
        element.click()

    elif action == "type":
        if not decision.target_text:
            raise ValueError("act(): 'type' requires target_text (the input's label)")
        element = _find_input_by_label(page, decision.target_text)
        if element is None:
            raise ValueError(
                f"act(): no visible input found matching label {decision.target_text!r}"
            )
        element.fill(decision.value or "")

    elif action == "select":
        if not decision.target_text:
            raise ValueError("act(): 'select' requires target_text (the dropdown's label)")
        element = _find_input_by_label(page, decision.target_text)
        if element is None:
            raise ValueError(
                f"act(): no visible dropdown found matching label {decision.target_text!r}"
            )
        element.select_option(label=decision.value)

    elif action in ("done", "stuck"):
        return

    else:
        raise ValueError(f"act(): unknown action {action!r}")


# ---------------------------------------------------------------------------
# run_discovery()
# ---------------------------------------------------------------------------


def _save_screenshot(page: Page, run_dir: Path, step_num: int) -> Path:
    path = run_dir / f"step_{step_num}.png"
    page.screenshot(path=str(path))
    return path


def run_discovery(goal: str, start_url: str, max_steps: int = 15) -> dict:
    """Run the perceive -> decide -> act loop toward `goal`, starting at `start_url`.

    Saves a screenshot after every step plus a full JSON log under
    evidence/discovery_run_<timestamp>/, and returns {goal, start_url, history, outcome}.
    """
    import os

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to the project's .env file "
            "(OPENAI_API_KEY=...) before running the discovery agent."
        )
    client = OpenAI()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = PROJECT_ROOT / "evidence" / f"discovery_run_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    history: list[dict] = []
    outcome: dict = {"status": "stuck", "reason": "did_not_start", "steps_taken": 0}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(start_url)

            for step_num in range(1, max_steps + 1):
                page_state = perceive(page)
                step_record: dict = {"step": step_num, "page_state": page_state}

                try:
                    decision = decide(goal, page_state, history, client=client)
                except Exception as exc:  # decide() already retried once internally
                    step_record["decision"] = None
                    step_record["result"] = f"error: decide() failed: {exc}"
                    step_record["screenshot"] = str(_save_screenshot(page, run_dir, step_num))
                    history.append(step_record)
                    outcome = {
                        "status": "stuck",
                        "reason": f"decide() failed: {exc}",
                        "steps_taken": step_num,
                    }
                    break

                step_record["decision"] = decision.model_dump(exclude_none=True)

                if decision.action == "done":
                    step_record["result"] = "success"
                    step_record["screenshot"] = str(_save_screenshot(page, run_dir, step_num))
                    history.append(step_record)
                    outcome = {
                        "status": "success",
                        "reason": decision.reasoning,
                        "steps_taken": step_num,
                    }
                    break

                if decision.action == "stuck":
                    step_record["result"] = "stuck"
                    step_record["screenshot"] = str(_save_screenshot(page, run_dir, step_num))
                    history.append(step_record)
                    outcome = {
                        "status": "stuck",
                        "reason": decision.reasoning,
                        "steps_taken": step_num,
                    }
                    break

                try:
                    act(page, decision)
                    step_record["result"] = "success"
                except Exception as exc:
                    step_record["result"] = f"error: {exc}"

                step_record["screenshot"] = str(_save_screenshot(page, run_dir, step_num))
                history.append(step_record)
            else:
                outcome = {
                    "status": "stuck",
                    "reason": "max_steps_exceeded",
                    "steps_taken": max_steps,
                }
        except Exception as exc:
            outcome = {
                "status": "stuck",
                "reason": f"unexpected_error: {exc}",
                "steps_taken": len(history),
            }
        finally:
            browser.close()

    result = {
        "goal": goal,
        "start_url": start_url,
        "history": history,
        "outcome": outcome,
        "evidence_dir": str(run_dir),
    }
    (run_dir / "log.json").write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    GOAL = (
        "Search for member 10001, open a sub-account for them with account type "
        "Savings and an initial deposit of 50, and reach the confirmation screen "
        "showing the reference number."
    )
    START_URL = "http://localhost:5050/login"

    result = run_discovery(goal=GOAL, start_url=START_URL)
    outcome = result["outcome"]

    print(f"Discovery run: {outcome['status']}")
    print(f"Reason: {outcome['reason']}")
    print(f"Steps taken: {outcome['steps_taken']}")
    print(f"Evidence saved to: {result['evidence_dir']}")

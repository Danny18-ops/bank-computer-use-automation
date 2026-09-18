"""
Deterministic replay engine: executes a CapabilityArtifact's fixed step list against
a live browser. No LLM is involved anywhere in this module - every action is resolved
mechanically from the artifact's locators and the caller-supplied inputs.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from schemas.artifact_schema import (  # noqa: E402
    CapabilityArtifact,
    Condition,
    InputParameter,
    Locator,
    OutputDeclaration,
    Step,
)
from safety.guardrails import check_allowlist, is_risky_step, redact_sensitive  # noqa: E402
from escalation.handoff import raise_intervention, wait_for_resume  # noqa: E402

DEFAULT_BASE_URL = "http://localhost:5050"


# ---------------------------------------------------------------------------
# Input validation (no browser involved)
# ---------------------------------------------------------------------------

_PY_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}


def _validate_inputs(artifact: CapabilityArtifact, inputs: dict) -> list[str]:
    errors: list[str] = []

    for name, spec in artifact.inputs.items():
        if name not in inputs:
            if spec.required:
                errors.append(f"missing required input '{name}'")
            continue

        value = inputs[name]
        expected_type = _PY_TYPES[spec.type]

        if isinstance(value, bool) and spec.type != "boolean":
            errors.append(f"input '{name}' must be {spec.type}, got bool")
            continue
        if not isinstance(value, expected_type):
            errors.append(f"input '{name}' must be {spec.type}, got {type(value).__name__}")
            continue

        if spec.enum is not None and value not in spec.enum:
            errors.append(f"input '{name}' must be one of {spec.enum}, got {value!r}")

        if spec.constraints:
            if "min" in spec.constraints and value < spec.constraints["min"]:
                errors.append(f"input '{name}' must be >= {spec.constraints['min']}, got {value}")
            if "max" in spec.constraints and value > spec.constraints["max"]:
                errors.append(f"input '{name}' must be <= {spec.constraints['max']}, got {value}")

    return errors


# ---------------------------------------------------------------------------
# DOM locator resolution (same conventions as agent/loop.py, kept independent
# so this module has no dependency on the LLM-driven agent code)
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

_TABLE_ROW_LOOKUP_JS = r"""
(label) => {
  const rows = document.querySelectorAll('tr');
  const target = label.trim().toLowerCase();
  for (const row of rows) {
    const cells = row.querySelectorAll('td, th');
    if (cells.length < 2) continue;
    const first = cells[0].textContent.replace(/\s+/g, ' ').trim().toLowerCase();
    if (first === target || first.includes(target)) {
      return cells[1].textContent.replace(/\s+/g, ' ').trim();
    }
  }
  return null;
}
"""


def _find_input_by_text(page, text: Optional[str]):
    if not text:
        return None
    target = text.strip().lower()
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


def _find_clickable_by_text(page, text: Optional[str]):
    if not text:
        return None
    for role in ("button", "link"):
        locator = page.get_by_role(role, name=text)
        for i in range(locator.count()):
            candidate = locator.nth(i)
            if candidate.is_visible():
                return candidate
    return None


def _resolve_locator(page, locator: Optional[Locator], finder) -> tuple[Optional[object], str]:
    """Try locator.primary, then locator.fallback. Returns (element_or_None, description)."""
    if locator is None:
        return None, "(no locator)"

    description = f"primary={locator.primary.strategy}:{locator.primary.value!r}"
    element = finder(page, locator.primary.value)
    if element is not None:
        return element, description

    if locator.fallback is not None:
        description += f", fallback={locator.fallback.strategy}:{locator.fallback.value!r}"
        element = finder(page, locator.fallback.value)
        if element is not None:
            return element, description

    return None, description


def _condition_matches(page_text: str, condition: Condition) -> bool:
    # Every condition currently supported is a case-insensitive "does the page contain
    # this text" check; condition.type ("page_contains_text" / "text_present") is kept
    # as descriptive metadata rather than a dispatch key.
    return condition.value.strip().lower() in page_text.lower()


def _extract_outputs(page, outputs: dict[str, OutputDeclaration]) -> tuple[dict, Optional[str]]:
    extracted: dict = {}
    for name, decl in outputs.items():
        if decl.extract_from.strategy == "table_row_position":
            label = decl.extract_from.label
            value = page.evaluate(_TABLE_ROW_LOOKUP_JS, label)
            if value is None:
                return extracted, f"could not extract output '{name}': no table row found for label {label!r}"
            extracted[name] = value
        else:
            return (
                extracted,
                f"could not extract output '{name}': unsupported extraction strategy "
                f"{decl.extract_from.strategy!r}",
            )
    return extracted, None


def _hard_failure(step_id, message: str, screenshot_path: Path, expected=None, observed=None) -> dict:
    result = {
        "status": "hard_failure",
        "step": step_id,
        "message": message,
        "screenshot": str(screenshot_path),
    }
    if expected is not None:
        result["expected"] = expected
    if observed is not None:
        result["observed"] = observed
    return result


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------


def _execute_step(page, step: Step, inputs: dict, base_url: str, run_dir: Path) -> dict:
    """Runs one step. Returns {"log_entry": ...} or {"log_entry": ..., "stop": True, "result": ...}."""
    log_entry: dict = {"step_id": step.step_id, "action": step.action}
    screenshot_path = run_dir / "final.png"

    try:
        if step.action == "navigate":
            url = base_url.rstrip("/") + step.target
            log_entry["target"] = step.target
            page.goto(url)
            log_entry["success"] = True
            return {"log_entry": log_entry}

        if step.action in ("type", "select"):
            element, description = _resolve_locator(page, step.locator, _find_input_by_text)
            log_entry["locator"] = description
            if element is None:
                log_entry["success"] = False
                log_entry["error"] = "no matching input found"
                return {
                    "log_entry": log_entry,
                    "stop": True,
                    "locator_not_found": True,
                    "result": _hard_failure(
                        step.step_id,
                        f"Step {step.step_id} ({step.action}): no input found for locator ({description})",
                        screenshot_path,
                    ),
                }
            value = inputs.get(step.value_from_input)
            if step.action == "type":
                element.fill("" if value is None else str(value))
            else:
                element.select_option(label=str(value))
            log_entry["success"] = True
            return {"log_entry": log_entry}

        if step.action == "click":
            element, description = _resolve_locator(page, step.locator, _find_clickable_by_text)
            log_entry["locator"] = description
            if element is None:
                log_entry["success"] = False
                log_entry["error"] = "no matching button/link found"
                return {
                    "log_entry": log_entry,
                    "stop": True,
                    "locator_not_found": True,
                    "result": _hard_failure(
                        step.step_id,
                        f"Step {step.step_id} (click): no button/link found for locator ({description})",
                        screenshot_path,
                    ),
                }
            element.click()
            log_entry["success"] = True
            return {"log_entry": log_entry}

        if step.action == "verify_checkpoint":
            page_text = page.locator("body").inner_text()
            passed = _condition_matches(page_text, step.condition)
            log_entry["condition"] = {"type": step.condition.type, "value": step.condition.value}
            log_entry["success"] = passed
            if passed:
                return {"log_entry": log_entry}

            on_fail = step.on_fail or "hard_failure"
            if on_fail.startswith("business_outcome:"):
                outcome_name = on_fail.split(":", 1)[1]
                return {
                    "log_entry": log_entry,
                    "stop": True,
                    "result": {"status": "business_outcome", "outcome": outcome_name, "step": step.step_id},
                }
            return {
                "log_entry": log_entry,
                "stop": True,
                "result": _hard_failure(
                    step.step_id,
                    f"Checkpoint failed: expected page to contain {step.condition.value!r}",
                    screenshot_path,
                    expected=step.condition.value,
                    observed=page_text[:200],
                ),
            }

        log_entry["success"] = False
        log_entry["error"] = f"unsupported action {step.action!r}"
        return {
            "log_entry": log_entry,
            "stop": True,
            "result": _hard_failure(step.step_id, f"Unsupported action {step.action!r}", screenshot_path),
        }

    except Exception as exc:  # never let a raw Playwright exception leak up unformatted
        log_entry["success"] = False
        log_entry["error"] = str(exc)
        return {
            "log_entry": log_entry,
            "stop": True,
            "result": _hard_failure(
                step.step_id,
                f"Step {step.step_id} ({step.action}) raised an unexpected error: {exc}",
                screenshot_path,
            ),
        }


# ---------------------------------------------------------------------------
# replay()
# ---------------------------------------------------------------------------


def replay(artifact_path: str, inputs: dict, base_url: str) -> dict:
    artifact = CapabilityArtifact.load(artifact_path)

    validation_errors = _validate_inputs(artifact, inputs)
    if validation_errors:
        return {"status": "invalid_input", "errors": validation_errors}

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = PROJECT_ROOT / "evidence" / f"replay_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    step_log: list[dict] = []
    result: dict = {}
    intervention_log_path: Optional[str] = None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        try:
            page.goto(base_url)

            for step in artifact.steps:
                check_target = base_url.rstrip("/") + step.target if step.action == "navigate" else page.url
                allowed, reason = check_allowlist(check_target, step.action)
                if not allowed:
                    step_log.append(
                        {
                            "step_id": step.step_id,
                            "action": step.action,
                            "success": False,
                            "error": f"blocked_by_policy: {reason}",
                        }
                    )
                    result = {"status": "blocked_by_policy", "step": step.step_id, "reason": reason}
                    break

                if is_risky_step(step) and inputs.get("confirmed") is not True:
                    message = (
                        "This action is irreversible and requires confirmed=true in "
                        "inputs to proceed."
                    )
                    step_log.append(
                        {
                            "step_id": step.step_id,
                            "action": step.action,
                            "success": False,
                            "error": "requires_confirmation",
                        }
                    )
                    result = {
                        "status": "requires_confirmation",
                        "step": step.step_id,
                        "message": message,
                    }
                    break

                step_outcome = _execute_step(page, step, inputs, base_url, run_dir)
                step_log.append(step_outcome["log_entry"])

                if step_outcome.get("locator_not_found"):
                    intervention_path = raise_intervention(
                        reason="locator_not_found",
                        goal=artifact.description,
                        step_id=step.step_id,
                        page=page,
                        context={
                            "action": step.action,
                            "locator": step_outcome["log_entry"].get("locator"),
                        },
                    )
                    resume_result = wait_for_resume(intervention_path)

                    if resume_result.get("status") == "timeout":
                        result = {
                            "status": "escalation_timeout",
                            "step": step.step_id,
                            "intervention_path": intervention_path,
                        }
                        break

                    intervention_log_path = str(Path(intervention_path) / "intervention_log.json")

                    # Re-run perception implicitly: every locator-resolution helper
                    # queries the live DOM fresh, so simply retrying re-reads the
                    # current page state. Retry the SAME step exactly once.
                    retry_outcome = _execute_step(page, step, inputs, base_url, run_dir)
                    retry_outcome["log_entry"]["intervention_log"] = intervention_log_path
                    step_log.append(retry_outcome["log_entry"])

                    if retry_outcome.get("stop"):
                        result = retry_outcome["result"]
                        result["intervention_log"] = intervention_log_path
                        break
                    continue  # retry succeeded - proceed to the next step

                if step_outcome.get("stop"):
                    result = step_outcome["result"]
                    break
            else:
                page_text = page.locator("body").inner_text()
                if _condition_matches(page_text, artifact.success_condition):
                    outputs, extraction_error = _extract_outputs(page, artifact.outputs)
                    if extraction_error:
                        result = _hard_failure("output_extraction", extraction_error, run_dir / "final.png")
                    else:
                        result = {
                            "status": "success",
                            "outputs": outputs,
                            "steps_completed": len(artifact.steps),
                        }
                else:
                    result = _hard_failure(
                        "final_success_condition",
                        "Final success_condition not met: expected page to contain "
                        f"{artifact.success_condition.value!r}",
                        run_dir / "final.png",
                        expected=artifact.success_condition.value,
                        observed=page_text[:200],
                    )
                if intervention_log_path:
                    result["intervention_log"] = intervention_log_path
        except Exception as exc:
            result = _hard_failure("unexpected_error", f"Unexpected error during replay: {exc}", run_dir / "final.png")
        finally:
            try:
                page.screenshot(path=str(run_dir / "final.png"))
            except Exception:
                pass
            browser.close()

    result.setdefault("evidence_dir", str(run_dir))
    result.setdefault("screenshot", str(run_dir / "final.png"))

    log_payload = redact_sensitive(
        {
            "artifact_path": str(artifact_path),
            "artifact_id": artifact.artifact_id,
            "inputs": inputs,
            "base_url": base_url,
            "steps": step_log,
            "result": result,
        }
    )
    (run_dir / "log.json").write_text(json.dumps(log_payload, indent=2))

    return result


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        print(
            "Usage: python replay/engine.py <artifact_path> '<json_inputs>' [base_url]",
            file=sys.stderr,
        )
        raise SystemExit(1)

    cli_artifact_path = sys.argv[1]
    cli_inputs = json.loads(sys.argv[2])
    cli_base_url = sys.argv[3] if len(sys.argv) == 4 else DEFAULT_BASE_URL

    cli_result = replay(cli_artifact_path, cli_inputs, cli_base_url)
    print(json.dumps(cli_result, indent=2))

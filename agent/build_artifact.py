"""
Converts a discovery run's evidence/discovery_run_<timestamp>/log.json into a
CapabilityArtifact: a generalized, replayable recipe with hardcoded values lifted
out into named input parameters, plus checkpoints inferred from what the run
actually saw on screen.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from schemas.artifact_schema import (  # noqa: E402
    CapabilityArtifact,
    Condition,
    InputParameter,
    Locator,
    LocatorStrategy,
    OutputDeclaration,
    OutputExtraction,
    Step,
)


def _slugify(text: str) -> str:
    text = text.strip().rstrip(":").strip()
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return slug or "value"


def _infer_param_name(label: str) -> str:
    lower = label.lower()
    if "member id" in lower:
        return "member_id"
    if "account type" in lower:
        return "account_type"
    if "deposit" in lower:
        return "initial_deposit"
    return _slugify(label)


def _build_input_parameter(name: str, options: Optional[list[str]]) -> InputParameter:
    if name == "initial_deposit":
        return InputParameter(type="number", required=True, constraints={"min": 0.01})
    if name == "account_type":
        kwargs: dict = {"type": "string", "required": True}
        if options:
            kwargs["enum"] = options
        return InputParameter(**kwargs)
    return InputParameter(type="string", required=True)


def _get_or_create_param(
    label: str,
    options: Optional[list[str]],
    label_to_param: dict[str, str],
    inputs: dict[str, InputParameter],
) -> str:
    if label in label_to_param:
        return label_to_param[label]

    base_name = _infer_param_name(label)
    name = base_name
    suffix = 2
    while name in inputs:
        name = f"{base_name}_{suffix}"
        suffix += 1

    label_to_param[label] = name
    inputs[name] = _build_input_parameter(name, options)
    return name


def _select_options_for_label(page_state: dict, label: str) -> Optional[list[str]]:
    for entry in page_state.get("inputs", []):
        if entry.get("label") == label:
            return entry.get("options")
    return None


def _second_heading_line(text_summary: str) -> Optional[str]:
    lines = [line.strip() for line in text_summary.splitlines() if line.strip()]
    return lines[1] if len(lines) > 1 else None


def _find_success_line(text_summary: str) -> str:
    lines = [line.strip() for line in text_summary.splitlines() if line.strip()]
    for line in lines:
        if "success" in line.lower():
            return line
    return lines[-1] if lines else "success"


def _extract_table_rows(text_summary: str) -> list[tuple[str, str]]:
    rows = []
    for line in text_summary.splitlines():
        if "\t" in line:
            label, _, value = line.partition("\t")
            label, value = label.strip(), value.strip()
            if label and value:
                rows.append((label, value))
    return rows


def _infer_output(text_summary: str) -> Optional[tuple[str, str]]:
    """Returns (raw_label, output_name) for the most likely "result" table row."""
    rows = _extract_table_rows(text_summary)
    if not rows:
        return None
    for label, _ in rows:
        if "reference" in label.lower():
            return label, _slugify(label)
    label, _ = rows[0]
    return label, _slugify(label)


def _infer_target_app(history: list[dict]) -> str:
    if history:
        summary = history[0]["page_state"]["visible_text_summary"]
        lines = [line.strip() for line in summary.splitlines() if line.strip()]
        if lines:
            return lines[0]
    return "unknown_app"


def _artifact_id_from_goal(goal: str, max_words: int = 8, max_len: int = 60) -> str:
    words = re.findall(r"[A-Za-z0-9]+", goal)[:max_words]
    slug = "_".join(word.lower() for word in words)
    return slug[:max_len].rstrip("_") or "discovery_artifact"


def build_artifact_from_log(log_path: Path) -> CapabilityArtifact:
    log_path = Path(log_path)
    data = json.loads(log_path.read_text())

    goal = data["goal"]
    history = data.get("history", [])
    run_folder_name = log_path.resolve().parent.name

    inputs: dict[str, InputParameter] = {}
    label_to_param: dict[str, str] = {}
    steps: list[Step] = []
    step_id_counter = 1

    def next_step_id() -> int:
        nonlocal step_id_counter
        value = step_id_counter
        step_id_counter += 1
        return value

    for i, entry in enumerate(history):
        decision = entry.get("decision")
        if not decision or entry.get("result") != "success":
            continue

        action = decision["action"]
        if action not in ("click", "type", "select", "navigate"):
            continue

        target_text = decision.get("target_text")
        description = decision.get("reasoning")

        if action == "navigate":
            next_state = history[i + 1]["page_state"] if i + 1 < len(history) else entry["page_state"]
            target_path = urlparse(next_state["url"]).path or "/"
            steps.append(
                Step(step_id=next_step_id(), action="navigate", target=target_path, description=description)
            )
            continue

        locator = Locator(primary=LocatorStrategy(strategy="visible_text", value=target_text))

        if action == "click":
            steps.append(Step(step_id=next_step_id(), action="click", locator=locator, description=description))

            if target_text and "search" in target_text.lower() and i + 1 < len(history):
                success_text = _second_heading_line(history[i + 1]["page_state"]["visible_text_summary"])
                if success_text:
                    steps.append(
                        Step(
                            step_id=next_step_id(),
                            action="verify_checkpoint",
                            condition=Condition(type="text_present", value=success_text),
                            on_fail="business_outcome:member_not_found",
                            description=f"Verify search succeeded (expected '{success_text}')",
                        )
                    )
        else:  # "type" or "select"
            options = _select_options_for_label(entry["page_state"], target_text) if action == "select" else None
            param_name = _get_or_create_param(target_text, options, label_to_param, inputs)
            steps.append(
                Step(
                    step_id=next_step_id(),
                    action=action,
                    locator=locator,
                    value_from_input=param_name,
                    description=description,
                )
            )

    final_text_summary = history[-1]["page_state"]["visible_text_summary"] if history else ""
    success_line = _find_success_line(final_text_summary)

    steps.append(
        Step(
            step_id=next_step_id(),
            action="verify_checkpoint",
            condition=Condition(type="text_present", value=success_line),
            on_fail="hard_failure",
            description="Verify the discovery run's final success state",
        )
    )

    outputs: dict[str, OutputDeclaration] = {}
    inferred_output = _infer_output(final_text_summary)
    if inferred_output:
        label, output_name = inferred_output
        outputs[output_name] = OutputDeclaration(
            type="string",
            extract_from=OutputExtraction(strategy="table_row_position", label=label),
        )

    return CapabilityArtifact(
        artifact_id=_artifact_id_from_goal(goal),
        version=1,
        description=goal,
        target_app=_infer_target_app(history),
        created_from_discovery_run=run_folder_name,
        inputs=inputs,
        steps=steps,
        outputs=outputs,
        success_condition=Condition(type="text_present", value=success_line),
    )


def _resolve_output_path(artifacts_dir: Path, artifact_id: str, version: int) -> Path:
    candidate = artifacts_dir / f"{artifact_id}_v{version}.json"
    if not candidate.exists():
        return candidate

    candidate = artifacts_dir / f"{artifact_id}_v{version}_generated.json"
    counter = 2
    while candidate.exists():
        candidate = artifacts_dir / f"{artifact_id}_v{version}_generated{counter}.json"
        counter += 1
    return candidate


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python agent/build_artifact.py <path/to/log.json>", file=sys.stderr)
        raise SystemExit(1)

    log_path = Path(sys.argv[1])
    if not log_path.exists():
        print(f"No such file: {log_path}", file=sys.stderr)
        raise SystemExit(1)

    artifact = build_artifact_from_log(log_path)

    artifacts_dir = PROJECT_ROOT / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    output_path = _resolve_output_path(artifacts_dir, artifact.artifact_id, artifact.version)
    artifact.save(output_path)

    print(f"Generated artifact '{artifact.artifact_id}' (v{artifact.version})")
    print(f"  from:    {log_path}")
    print(f"  steps:   {len(artifact.steps)}")
    print(f"  inputs:  {', '.join(artifact.inputs.keys()) or '(none)'}")
    print(f"  outputs: {', '.join(artifact.outputs.keys()) or '(none)'}")
    print(f"  saved to: {output_path}")


if __name__ == "__main__":
    main()

"""
Data model for a "capability artifact" — a recorded, replayable automation
recipe (navigate/type/click/select/verify steps) discovered against a target
app, along with its typed inputs/outputs and success condition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ActionType = Literal["navigate", "type", "click", "select", "verify_checkpoint"]


class InputParameter(BaseModel):
    """Describes one entry in an artifact's `inputs` dict."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "integer", "number", "boolean"]
    required: bool = True
    enum: Optional[list[str]] = None
    constraints: Optional[dict[str, float]] = None


class LocatorStrategy(BaseModel):
    """One way to find an element, e.g. {"strategy": "label_text", "value": "Member ID:"}."""

    model_config = ConfigDict(extra="forbid")

    strategy: str
    value: str


class Locator(BaseModel):
    """A primary element-finding strategy plus an optional fallback."""

    model_config = ConfigDict(extra="forbid")

    primary: LocatorStrategy
    fallback: Optional[LocatorStrategy] = None


class Condition(BaseModel):
    """A generic {type, value} predicate, used for checkpoints and success_condition."""

    model_config = ConfigDict(extra="forbid")

    type: str
    value: str


class Step(BaseModel):
    """One step in an artifact's ordered `steps` list."""

    model_config = ConfigDict(extra="forbid")

    step_id: int
    action: ActionType
    locator: Optional[Locator] = None
    value_from_input: Optional[str] = None
    target: Optional[str] = None
    condition: Optional[Condition] = None
    on_fail: Optional[str] = None
    description: Optional[str] = None

    @field_validator("on_fail")
    @classmethod
    def _validate_on_fail(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "hard_failure" or v.startswith("business_outcome:"):
            return v
        raise ValueError(
            f"on_fail must be 'hard_failure' or 'business_outcome:<name>', got {v!r}"
        )

    @model_validator(mode="after")
    def _validate_action_fields(self) -> "Step":
        rules = _ACTION_FIELD_RULES[self.action]
        for field_name in _ACTION_SPECIFIC_FIELDS:
            value = getattr(self, field_name)
            if field_name in rules["required"] and value is None:
                raise ValueError(
                    f"step_id {self.step_id}: action '{self.action}' requires '{field_name}'"
                )
            if field_name not in rules["allowed"] and value is not None:
                raise ValueError(
                    f"step_id {self.step_id}: action '{self.action}' must not set '{field_name}'"
                )
        return self


_ACTION_SPECIFIC_FIELDS = {"locator", "value_from_input", "target", "condition", "on_fail"}

_ACTION_FIELD_RULES: dict[str, dict[str, set[str]]] = {
    "navigate": {"required": {"target"}, "allowed": {"target"}},
    "type": {
        "required": {"locator", "value_from_input"},
        "allowed": {"locator", "value_from_input"},
    },
    "click": {"required": {"locator"}, "allowed": {"locator"}},
    "select": {
        "required": {"locator", "value_from_input"},
        "allowed": {"locator", "value_from_input"},
    },
    "verify_checkpoint": {"required": {"condition"}, "allowed": {"condition", "on_fail"}},
}


class OutputExtraction(BaseModel):
    """Describes how to pull an output value out of the page, e.g.
    {"strategy": "table_row_position", "label": "Reference Number"}."""

    model_config = ConfigDict(extra="forbid")

    strategy: str
    label: Optional[str] = None
    value: Optional[str] = None

    @model_validator(mode="after")
    def _require_label_or_value(self) -> "OutputExtraction":
        if self.label is None and self.value is None:
            raise ValueError("extract_from must set at least one of 'label' or 'value'")
        return self


class OutputDeclaration(BaseModel):
    """One entry in an artifact's `outputs` dict."""

    model_config = ConfigDict(extra="forbid")

    type: str
    extract_from: OutputExtraction


class CapabilityArtifact(BaseModel):
    """Top-level capability artifact: metadata + inputs + steps + outputs + success condition."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    version: int
    description: str
    target_app: str
    created_from_discovery_run: Optional[str] = None

    inputs: dict[str, InputParameter] = Field(default_factory=dict)
    steps: list[Step]
    outputs: dict[str, OutputDeclaration] = Field(default_factory=dict)
    success_condition: Condition

    @model_validator(mode="after")
    def _validate_steps(self) -> "CapabilityArtifact":
        step_ids = [step.step_id for step in self.steps]
        expected = list(range(1, len(step_ids) + 1))
        if step_ids != expected:
            raise ValueError(
                f"step_ids must be unique and sequential starting at 1, in order; got {step_ids}"
            )

        for step in self.steps:
            if step.value_from_input is not None and step.value_from_input not in self.inputs:
                raise ValueError(
                    f"step_id {step.step_id}: value_from_input '{step.value_from_input}' "
                    "is not a declared input parameter"
                )
        return self

    def to_json(self, *, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent, exclude_none=True)

    @classmethod
    def from_json(cls, data: str) -> "CapabilityArtifact":
        return cls.model_validate_json(data)

    def save(self, path: str | Path, *, indent: int = 2) -> None:
        Path(path).write_text(self.to_json(indent=indent) + "\n")

    @classmethod
    def load(cls, path: str | Path) -> "CapabilityArtifact":
        return cls.from_json(Path(path).read_text())


def build_open_sub_account_example() -> CapabilityArtifact:
    """The reference "open sub-account" flow against the MockBank mock_app."""

    return CapabilityArtifact(
        artifact_id="open_sub_account_v1",
        version=1,
        description=(
            "Opens a new sub-account (Checking or Savings) for an existing MockBank "
            "member, given a member ID, account type, and initial deposit amount."
        ),
        target_app="MockBank",
        created_from_discovery_run="discovery_run_2026_09_10_001",
        inputs={
            "username": InputParameter(type="string", required=True),
            "password": InputParameter(type="string", required=True),
            "member_id": InputParameter(type="string", required=True),
            "account_type": InputParameter(
                type="string", required=True, enum=["Checking", "Savings"]
            ),
            "initial_deposit": InputParameter(
                type="number", required=True, constraints={"min": 0.01}
            ),
        },
        steps=[
            Step(step_id=1, action="navigate", target="/login", description="Go to the login page"),
            Step(
                step_id=2,
                action="type",
                locator=Locator(primary=LocatorStrategy(strategy="label_text", value="Username:")),
                value_from_input="username",
                description="Type the username into the login form",
            ),
            Step(
                step_id=3,
                action="type",
                locator=Locator(primary=LocatorStrategy(strategy="label_text", value="Password:")),
                value_from_input="password",
                description="Type the password into the login form",
            ),
            Step(
                step_id=4,
                action="click",
                locator=Locator(primary=LocatorStrategy(strategy="visible_text", value="Login")),
                description="Click the Login button",
            ),
            Step(step_id=5, action="navigate", target="/search", description="Go to member search"),
            Step(
                step_id=6,
                action="type",
                locator=Locator(primary=LocatorStrategy(strategy="label_text", value="Member ID:")),
                value_from_input="member_id",
                description="Type the member ID into the search field",
            ),
            Step(
                step_id=7,
                action="click",
                locator=Locator(primary=LocatorStrategy(strategy="visible_text", value="Search")),
                description="Click the Search button",
            ),
            Step(
                step_id=8,
                action="verify_checkpoint",
                condition=Condition(type="text_present", value="Member Detail"),
                on_fail="business_outcome:member_not_found",
                description="Confirm the member detail page loaded",
            ),
            Step(
                step_id=9,
                action="click",
                locator=Locator(
                    primary=LocatorStrategy(strategy="visible_text", value="Open Sub-Account")
                ),
                description="Click Open Sub-Account",
            ),
            Step(
                step_id=10,
                action="select",
                locator=Locator(
                    primary=LocatorStrategy(strategy="label_text", value="Account Type:")
                ),
                value_from_input="account_type",
                description="Select the account type",
            ),
            Step(
                step_id=11,
                action="type",
                locator=Locator(
                    primary=LocatorStrategy(
                        strategy="label_text", value="Initial Deposit Amount:"
                    )
                ),
                value_from_input="initial_deposit",
                description="Type the initial deposit amount",
            ),
            Step(
                step_id=12,
                action="click",
                locator=Locator(primary=LocatorStrategy(strategy="visible_text", value="Submit")),
                description="Click Submit",
            ),
            Step(
                step_id=13,
                action="verify_checkpoint",
                condition=Condition(
                    type="text_present", value="Sub-account created successfully"
                ),
                on_fail="hard_failure",
                description="Confirm the confirmation page shows success",
            ),
        ],
        outputs={
            "reference_number": OutputDeclaration(
                type="string",
                extract_from=OutputExtraction(
                    strategy="table_row_position", label="Reference Number"
                ),
            ),
        },
        success_condition=Condition(type="text_present", value="Sub-account created successfully"),
    )


if __name__ == "__main__":
    example = build_open_sub_account_example()
    output_path = Path(__file__).resolve().parent.parent / "artifacts" / "open_sub_account_v1.json"
    example.save(output_path)
    print(f"Wrote {output_path}")

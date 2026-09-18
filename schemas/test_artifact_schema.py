import pytest
from pydantic import ValidationError

from artifact_schema import (
    CapabilityArtifact,
    Condition,
    InputParameter,
    Locator,
    LocatorStrategy,
    Step,
    build_open_sub_account_example,
)


def test_open_sub_account_example_validates():
    artifact = build_open_sub_account_example()
    assert artifact.artifact_id == "open_sub_account_v1"
    assert [step.step_id for step in artifact.steps] == list(range(1, 14))
    assert artifact.outputs["reference_number"].extract_from.label == "Reference Number"


def test_open_sub_account_example_round_trips_through_json():
    artifact = build_open_sub_account_example()
    json_text = artifact.to_json()
    reloaded = CapabilityArtifact.from_json(json_text)
    assert reloaded == artifact


def test_duplicate_step_ids_raise_validation_error():
    with pytest.raises(ValidationError):
        CapabilityArtifact(
            artifact_id="broken",
            version=1,
            description="duplicate step ids",
            target_app="MockBank",
            inputs={"member_id": InputParameter(type="string")},
            steps=[
                Step(step_id=1, action="navigate", target="/search"),
                Step(
                    step_id=1,
                    action="type",
                    locator=Locator(
                        primary=LocatorStrategy(strategy="label_text", value="Member ID:")
                    ),
                    value_from_input="member_id",
                ),
            ],
            success_condition=Condition(type="text_present", value="ok"),
        )


def test_value_from_input_must_reference_declared_input():
    with pytest.raises(ValidationError):
        CapabilityArtifact(
            artifact_id="broken",
            version=1,
            description="dangling value_from_input reference",
            target_app="MockBank",
            inputs={},
            steps=[
                Step(
                    step_id=1,
                    action="type",
                    locator=Locator(
                        primary=LocatorStrategy(strategy="label_text", value="Member ID:")
                    ),
                    value_from_input="member_id",
                ),
            ],
            success_condition=Condition(type="text_present", value="ok"),
        )


def test_click_action_rejects_value_from_input():
    with pytest.raises(ValidationError):
        Step(
            step_id=1,
            action="click",
            locator=Locator(primary=LocatorStrategy(strategy="visible_text", value="Search")),
            value_from_input="member_id",
        )

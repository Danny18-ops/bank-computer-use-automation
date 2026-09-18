import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "agent"))

from build_artifact import build_artifact_from_log  # noqa: E402
from schemas.artifact_schema import CapabilityArtifact  # noqa: E402


def _find_a_discovery_log() -> Path:
    candidates = sorted((PROJECT_ROOT / "evidence").glob("discovery_run_*/log.json"))
    if not candidates:
        pytest.skip("no discovery run log.json found under evidence/ to convert")
    return candidates[-1]


def test_build_artifact_from_real_discovery_log():
    log_path = _find_a_discovery_log()

    artifact = build_artifact_from_log(log_path)

    assert isinstance(artifact, CapabilityArtifact)
    assert {"member_id", "account_type", "initial_deposit"}.issubset(artifact.inputs.keys())
    assert any(step.action == "verify_checkpoint" for step in artifact.steps)
    assert artifact.created_from_discovery_run == log_path.resolve().parent.name

    reloaded = CapabilityArtifact.from_json(artifact.to_json())
    assert reloaded == artifact

"""
Allowlist and risk-policy guardrails for the replay engine. Pure, data-driven policy
checks - no LLM calls, no browser access. The replay engine consults these before
acting on any step.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from schemas.artifact_schema import Step  # noqa: E402

_ALLOWLIST_PATH = Path(__file__).resolve().parent / "allowlist.json"
ALLOWLIST: dict = json.loads(_ALLOWLIST_PATH.read_text())

# Policy list of (action, target_text) pairs identified as irreversible / high-risk.
# This is the single source of truth for what requires explicit confirmation before
# replay will perform it - grow this list as new risky actions are identified.
RISKY_ACTIONS: list[tuple[str, str]] = [
    ("click", "Submit"),
]

_SENSITIVE_KEY_SUBSTRINGS = ("password", "secret", "token", "credential")


def _route_matches(pattern: str, path: str) -> bool:
    pattern_segments = [s for s in pattern.split("/") if s]
    path_segments = [s for s in path.split("/") if s]
    if len(pattern_segments) != len(path_segments):
        return False
    return all(p == "*" or p == s for p, s in zip(pattern_segments, path_segments))


def check_allowlist(url_or_route: str, action_type: str) -> tuple[bool, str]:
    """Returns (True, "") if permitted, else (False, "<clear reason>").

    `url_or_route` may be a full URL ("http://localhost:5050/search") or a bare
    route ("/search"); a bare route skips the domain check.
    """
    if action_type not in ALLOWLIST["allowed_action_types"]:
        return (
            False,
            f"action type '{action_type}' is not in allowed_action_types "
            f"{ALLOWLIST['allowed_action_types']}",
        )

    parsed = urlparse(url_or_route)
    path = parsed.path or "/"

    if parsed.netloc and parsed.netloc not in ALLOWLIST["allowed_domains"]:
        return (
            False,
            f"domain '{parsed.netloc}' is not in allowed_domains {ALLOWLIST['allowed_domains']}",
        )

    if not any(_route_matches(pattern, path) for pattern in ALLOWLIST["allowed_routes"]):
        return (
            False,
            f"route '{path}' does not match any allowed_routes pattern {ALLOWLIST['allowed_routes']}",
        )

    return True, ""


def is_risky_step(step: Step) -> bool:
    """True for steps matching a (action, target_text) pair on the RISKY_ACTIONS policy list."""
    if step.locator is None:
        return False
    target_text = (step.locator.primary.value or "").strip().lower()
    return any(
        step.action == action and target_text == text.strip().lower()
        for action, text in RISKY_ACTIONS
    )


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(substring in lowered for substring in _SENSITIVE_KEY_SUBSTRINGS)


def redact_sensitive(data: Any) -> Any:
    """Deep copy of `data` with any dict key matching password/secret/token/credential
    (case-insensitive substring) replaced with "[REDACTED]", recursively through
    nested dicts/lists."""
    if isinstance(data, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_key(key) else redact_sensitive(value)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact_sensitive(item) for item in data]
    return copy.deepcopy(data)

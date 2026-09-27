"""Checkpoint evaluation.

A checkpoint is how replay knows it actually arrived somewhere, rather than
assuming the click worked. Every evaluation returns *what was observed* as well
as pass/fail, because that string is the difference between a failure report a
human can act on and one that just says False.
"""

from __future__ import annotations

import re

from src.artifact.schema import Checkpoint, CheckpointRule
from src.surface.base import Observation


def _text_snapshot(obs: Observation, limit: int = 240) -> str:
    flat = " | ".join(line.strip() for line in obs.text.splitlines() if line.strip())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def evaluate_rule(rule: CheckpointRule, obs: Observation, surface=None) -> tuple[bool, str]:
    match rule.kind:
        case "text_present":
            haystack = obs.text if rule.case_sensitive else obs.text.casefold()
            needle = rule.text if rule.case_sensitive else rule.text.casefold()
            found = needle in haystack
            ok = (not found) if rule.negate else found
            verb = "absent" if rule.negate else "present"
            return ok, (
                f"text {rule.text!r} {'was' if found else 'was not'} on screen "
                f"(required {verb})"
            )

        case "element_present":
            if surface is None:
                return False, "element check requires a live surface"
            _, report = surface.resolve(rule.descriptor, wait_ms=1200)
            ok = (not report.matched) if rule.negate else report.matched
            return ok, f"{rule.descriptor.describe()} {'resolved' if report.matched else 'did not resolve'}"

        case "location_matches":
            ok = bool(re.search(rule.pattern, obs.location))
            return ok, f"location was {obs.location!r}"

    return False, f"unknown checkpoint rule {rule.kind!r}"


def evaluate(checkpoint: Checkpoint, obs: Observation, surface=None) -> tuple[bool, str]:
    """Evaluate a checkpoint. Returns (passed, observed-description)."""
    results = [evaluate_rule(r, obs, surface) for r in checkpoint.rules]
    passed = all(ok for ok, _ in results) if checkpoint.all_required else any(ok for ok, _ in results)
    if passed:
        return True, "; ".join(desc for _, desc in results)
    failed = [desc for ok, desc in results if not ok]
    return False, "; ".join(failed) + f" || on screen: {_text_snapshot(obs)}"

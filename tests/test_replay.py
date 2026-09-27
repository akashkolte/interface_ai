"""Deterministic replay against the live target app.

The case these tests exist to pin down is the one the brief calls the most
common design mistake: a legitimate business answer ("no such member") must be a
successful execution with exit code 0, not a failure.
"""

from __future__ import annotations

import pytest

from src.artifact.store import load_latest
from src.errors.taxonomy import FailureKind, ReplayStatus
from src.replay.engine import ReplayEngine, render
from src.safety.policy import Policy
from src.surface.base import Action, ActionKind

CAPABILITY = "member.lookup_savings_balance"


@pytest.fixture(scope="session")
def artifact():
    return load_latest(CAPABILITY)


@pytest.fixture
def engine(surface, target_servers):
    return ReplayEngine(surface, Policy.for_local_targets(*target_servers.values()))


def _with_fault(surface, base, fault):
    surface.act(Action(kind=ActionKind.NAVIGATE, value=f"{base}/search?fault={fault}"))


# ------------------------------------------------------------------- basics


def test_render_substitutes_and_preserves_unknowns():
    assert render("/member/{id}", {"id": "5"}) == "/member/5"
    assert render("/x/{missing}", {}) == "/x/{missing}", "unknown placeholders must stay visible"


def test_success_returns_typed_outputs(engine, artifact, target_servers):
    r = engine.run(artifact, {"memberId": "12345"}, base_url=target_servers["base"])
    assert r.status is ReplayStatus.SUCCESS
    assert r.outputs["savingsBalance"] == "$4,182.55"
    assert r.outputs["memberName"] == "Dana Whitfield"
    assert r.exit_code == 0


def test_replay_is_deterministic(engine, artifact, target_servers):
    a = engine.run(artifact, {"memberId": "12345"}, base_url=target_servers["base"])
    b = engine.run(artifact, {"memberId": "12345"}, base_url=target_servers["base"])
    assert a.outputs == b.outputs
    assert [s.step_id for s in a.steps] == [s.step_id for s in b.steps]
    assert [s.tier_used for s in a.steps] == [s.tier_used for s in b.steps]


# ------------------------------------ the distinction the brief cares about


def test_missing_member_is_a_business_outcome_not_a_failure(engine, artifact, target_servers):
    r = engine.run(artifact, {"memberId": "99999"}, base_url=target_servers["base"])
    assert r.status is ReplayStatus.BUSINESS_OUTCOME
    assert r.outcome_code == "member_not_found"
    assert r.ok is True
    assert r.exit_code == 0, "a legitimate business answer must not look like a crash"
    assert r.failure is None


def test_injected_not_found_is_also_a_business_outcome(engine, artifact, target_servers, surface):
    _with_fault(surface, target_servers["base"], "member_not_found")
    r = engine.run(artifact, {"memberId": "12345"}, base_url=target_servers["base"])
    assert r.status is ReplayStatus.BUSINESS_OUTCOME and r.exit_code == 0


def test_application_error_is_a_hard_failure_with_debuggable_detail(
    engine, artifact, target_servers, surface
):
    _with_fault(surface, target_servers["base"], "app_error_500")
    r = engine.run(artifact, {"memberId": "12345"}, base_url=target_servers["base"])
    assert r.status is ReplayStatus.FAILURE and r.exit_code == 1
    f = r.failure
    assert f.kind is FailureKind.CHECKPOINT_FAILED
    assert f.step_id and f.expected and f.observed
    assert "Application Error" in f.observed, "the report must say what was actually on screen"


def test_bad_input_is_rejected_before_anything_is_driven(engine, artifact, target_servers):
    r = engine.run(artifact, {"memberId": "not-an-id"}, base_url=target_servers["base"])
    assert r.status is ReplayStatus.FAILURE
    assert r.failure.kind is FailureKind.INPUT_INVALID
    assert r.steps == [], "no action should be taken when the contract is violated"


def test_unknown_parameter_is_rejected(engine, artifact, target_servers):
    r = engine.run(artifact, {"memberId": "12345", "nope": "x"}, base_url=target_servers["base"])
    assert r.failure.kind is FailureKind.INPUT_INVALID


# ------------------------------------------------------------ multi-tenant


def test_base_artifact_replays_on_variant_tenant_via_overrides(engine, artifact, target_servers):
    r = engine.run(artifact, {"memberId": "12345"},
                   tenant_id="creditunion_b", base_url=target_servers["creditunion_b"])
    assert r.status is ReplayStatus.SUCCESS
    assert r.outputs["savingsBalance"] == "$4,182.55"


def test_variant_tenant_without_overrides_fails_clearly(engine, artifact, target_servers):
    """Same product, different configured labels -- this is why overrides exist."""
    r = engine.run(artifact, {"memberId": "12345"},
                   tenant_id=None, base_url=target_servers["creditunion_b"])
    assert r.status is ReplayStatus.FAILURE
    assert r.failure.kind is FailureKind.TARGET_UNRESOLVABLE
    assert "Search" in r.failure.expected


# ------------------------------------------------------------------ policy


def test_out_of_allowlist_target_is_blocked(surface, artifact, target_servers):
    narrow = ReplayEngine(surface, Policy.for_local_targets("http://localhost:5010"))
    r = narrow.run(artifact, {"memberId": "12345"}, base_url=target_servers["creditunion_b"])
    assert r.status is ReplayStatus.BLOCKED
    assert r.failure.kind is FailureKind.POLICY_BLOCKED

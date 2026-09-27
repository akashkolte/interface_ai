"""Control transfer: the state machine, and a real cede/act/resume on a live session.

The mechanism is exercised for real here -- the same `take()` / `hand_back()`
transitions, the same token invalidation, the same live browser session. Only
the *person* is stood in for, by a callable that performs the manual steps.
"""

from __future__ import annotations

import pytest

from src.artifact.store import load_latest
from src.errors.taxonomy import ReplayStatus
from src.escalation.control import (
    ControlState, ControlViolation, InterventionStore, SessionControl,
)
from src.escalation.escalator import Escalator, Resolution
from src.replay.engine import ReplayEngine
from src.safety.policy import Policy
from src.surface.base import Action, ActionKind


# ------------------------------------------------------- the state machine


def test_automation_may_act_only_while_it_holds_control():
    c = SessionControl()
    token = c.token
    c.guard(token)                      # fine

    c.request_intervention()
    with pytest.raises(ControlViolation):
        c.guard(token)

    c.take("operator")
    with pytest.raises(ControlViolation):
        c.guard(token)


def test_handing_control_back_invalidates_the_old_token():
    """Single-writer: a step that raced the operator must fail, not fight them."""
    c = SessionControl()
    stale = c.token
    c.request_intervention()
    c.take("operator")
    fresh = c.hand_back()
    c.confirm_resumed()

    assert fresh != stale
    with pytest.raises(ControlViolation):
        c.guard(stale)
    c.guard(fresh)


def test_illegal_transitions_are_refused():
    c = SessionControl()
    with pytest.raises(ControlViolation):
        c.hand_back()                    # nobody has taken control
    c.request_intervention()
    with pytest.raises(ControlViolation):
        c.request_intervention()         # already requested


def test_intervention_request_carries_what_an_operator_needs(tmp_path):
    from src.surface.base import Observation
    from src.artifact.schema import Step
    from src.surface.descriptors import AccessibleNameTier, ElementDescriptor

    esc = Escalator(SessionControl(), InterventionStore(tmp_path))
    step = Step(id="submit", intent="Submit the new sub-account request",
                action=Action(kind=ActionKind.CLICK,
                              target=ElementDescriptor(primary=AccessibleNameTier(role="button", name="Confirm"))))
    req = esc.raise_intervention(
        capability_id="member.open_subaccount", run_id="r1", step=step,
        reason="step mutates state (CONFIRM); requires confirmation",
        observation=Observation(location="http://localhost:5010/x", text="Open Sub-Account"))

    assert req.capability_id and req.step_id == "submit"
    assert req.step_intent and req.reason and req.location
    assert req.suggested_action, "an operator should be told what to do"
    assert InterventionStore(tmp_path).get(req.id) is not None, "must survive the process"


def test_secrets_never_reach_a_stored_intervention(tmp_path):
    from src.surface.base import Observation
    esc = Escalator(SessionControl(), InterventionStore(tmp_path))
    req = esc.raise_intervention(
        capability_id="c.x", run_id="r", step=None,
        reason="failed near SSN 123-45-6789",
        observation=Observation(location="u", text="member ssn 123-45-6789"))
    raw = InterventionStore(tmp_path).path(req.id).read_text()
    assert "123-45-6789" not in raw


# --------------------------------------------- live cede / act / resume


def test_human_takes_the_live_session_and_the_run_resumes(surface, target_servers, tmp_path):
    """A checkpoint that cannot pass is escalated; the operator fixes it in the
    SAME browser session; the run resumes and the checkpoint then passes."""
    artifact = load_latest("member.lookup_savings_balance")
    base = target_servers["base"]

    took: dict[str, object] = {}

    def operator(live_surface, request):
        # Runs while the human holds control, on the SAME session and window.
        took["request_id"] = request.id
        took["location_seen"] = live_surface.location()
        # Do what the intervention asked: clear the failing condition and get the
        # session to the screen the stuck step expected -- the search results.
        # Note it must be that screen specifically; jumping ahead to the member
        # record would leave the step's checkpoint unsatisfied, and the engine
        # re-checks rather than taking the operator's word for it.
        live_surface.act(Action(kind=ActionKind.NAVIGATE,
                                value=f"{base}/search/run?mid=12345&fault=none"))

    esc = Escalator(SessionControl(), InterventionStore(tmp_path), auto_resolver=operator)
    engine = ReplayEngine(surface, Policy.for_local_targets(base), escalator=esc)

    # Break the run: the search screen will 500, so submit_search's checkpoint fails.
    surface.act(Action(kind=ActionKind.NAVIGATE, value=f"{base}/search?fault=app_error_500"))
    result = engine.run(artifact, {"memberId": "12345"}, base_url=base)

    assert took["request_id"], "the operator was never handed the session"
    assert took["location_seen"], "the operator was handed a live session to look at"
    assert result.escalation_id, "the run must record which intervention it raised"
    # The operator put the session on the member detail screen, so the run
    # completed rather than failing.
    assert result.status is ReplayStatus.SUCCESS, result.summary()
    assert result.outputs.get("savingsBalance") == "$4,182.55"

    stored = InterventionStore(tmp_path).get(result.escalation_id)
    assert stored.resolved_at is not None
    assert stored.human_actions, "what the human did must be recorded as evidence"


def test_unattended_run_reports_escalated_rather_than_hanging(surface, target_servers, tmp_path):
    """With nobody to take it, the request is queued and the run returns."""
    artifact = load_latest("member.lookup_savings_balance")
    base = target_servers["base"]
    esc = Escalator(SessionControl(), InterventionStore(tmp_path), wait_seconds=0)
    engine = ReplayEngine(surface, Policy.for_local_targets(base), escalator=esc)

    surface.act(Action(kind=ActionKind.NAVIGATE, value=f"{base}/search?fault=app_error_500"))
    result = engine.run(artifact, {"memberId": "12345"}, base_url=base)

    assert result.status is ReplayStatus.ESCALATED
    assert result.exit_code == 1
    assert InterventionStore(tmp_path).pending(), "the request must be queued for an operator"


def test_intervention_request_carries_the_params_the_run_was_using(
    surface, target_servers, tmp_path
):
    """An intervention must say what the run was working on.

    The brief requires the request to carry enough context to act on it, and
    which member the run was servicing is the first thing an operator needs.
    Values are redacted on the way in, so a sensitive parameter never reaches
    the stored record even though the operator still sees the shape of the call.
    """
    artifact = load_latest("member.lookup_savings_balance")
    base = target_servers["base"]
    store = InterventionStore(tmp_path)
    esc = Escalator(SessionControl(), store, wait_seconds=0)
    engine = ReplayEngine(surface, Policy.for_local_targets(base), escalator=esc)

    surface.act(Action(kind=ActionKind.NAVIGATE, value=f"{base}/search?fault=app_error_500"))
    engine.run(artifact, {"memberId": "12345"}, base_url=base)

    pending = store.pending()
    assert pending, "expected a queued intervention"
    request = pending[0]
    assert request.params, "intervention request must not carry empty params"
    assert request.params["memberId"] == "12345"

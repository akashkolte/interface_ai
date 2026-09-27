"""Deterministic replay: the production execution path.

No model is consulted anywhere in this file. Given an artifact and a set of
parameters, the same inputs produce the same steps and the same outputs. That is
the whole promise the artifact makes to a calling agent, and the reason the
discovery run only has to happen once.

Order of checks inside a step matters, and is deliberate:

  1. policy      -- refuse before acting, never after
  2. resolve     -- walk the descriptor tier chain
  3. act
  4. business outcome -- BEFORE treating anything as an error, because "no such
                          member" looks exactly like a failed checkpoint if you
                          ask the questions the other way round
  5. recovery    -- bounded, declared, then retry the step
  6. checkpoint  -- prove we arrived
  7. extract     -- collect declared outputs

Step 4 sitting above 5 and 6 is the single most important ordering decision
here. Invert it and every legitimate business answer is reported as a crash.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from src.artifact.schema import (
    CapabilityArtifact,
    OutcomeRule,
    RecoveryRule,
    RiskClass,
    Step,
)
from src.errors.taxonomy import (
    FailureDetail,
    FailureKind,
    RecoveryEvent,
    ReplayResult,
    ReplayStatus,
    StepTrace,
)
from src.evidence.recorder import EvidenceRecorder
from src.replay.checkpoints import evaluate
from src.escalation.escalator import Resolution
from src.safety.policy import Policy
from src.surface.base import Action, ActionKind, ElementNotFound, Observation, Surface

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def render(template: str | None, params: dict[str, str]) -> str | None:
    """Substitute {param} placeholders. Unknown placeholders are left intact so
    they surface as a visible failure rather than a silent empty string."""
    if not template:
        return template
    return _PLACEHOLDER.sub(lambda m: params.get(m.group(1), m.group(0)), template)


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        policy: Policy,
        *,
        recorder: EvidenceRecorder | None = None,
        escalator=None,
    ) -> None:
        self.surface = surface
        self.policy = policy
        self.recorder = recorder
        #: Optional. When present, conditions the engine cannot resolve become
        #: an intervention request instead of a hard failure.
        self.escalator = escalator
        #: The control token this run must hold to dispatch an action. It is
        #: invalidated whenever a human takes the session, so an automation step
        #: that races the operator fails loudly instead of fighting them.
        self.control = escalator.control if escalator is not None else None
        self._token = self.control.token if self.control else ""
        #: Bounded, so a step that escalates forever cannot loop forever.
        self.max_escalations_per_step = 2

    def _dispatch(self, action: Action):
        """The single dispatch point. Enforces the control invariant before acting.

        Every action the engine takes goes through here, so "the automation must
        hold control to act" is checked in one place rather than remembered in
        several.
        """
        if self.control is not None:
            self.control.guard(self._token)
        return self.surface.act(action)

    # ------------------------------------------------------------------ run

    def run(
        self,
        artifact: CapabilityArtifact,
        params: dict[str, Any] | None = None,
        *,
        tenant_id: str | None = None,
        base_url: str | None = None,
    ) -> ReplayResult:
        started = time.monotonic()
        run_id = uuid.uuid4().hex[:12]
        params = params or {}

        resolved = artifact.for_tenant(tenant_id)
        override = artifact.tenant_overrides.get(tenant_id or "")
        base = base_url or (override.base_url if override else None) or (
            artifact.provenance.recorded_against_base_url if artifact.provenance else ""
        )

        result = ReplayResult(
            status=ReplayStatus.SUCCESS,
            capability_id=resolved.capability_id,
            artifact_version=resolved.version,
            run_id=run_id,
            tenant_id=tenant_id,
            evidence_dir=str(self.recorder.dir) if self.recorder else None,
        )
        self._log("replay.start", capability=resolved.capability_id, version=resolved.version,
                  tenant=tenant_id, base_url=base, params=params)

        # 1. Typed input validation, before anything is driven.
        try:
            bound = self._bind_params(resolved, params)
        except ValueError as exc:
            return self._fail(result, started, FailureDetail(
                kind=FailureKind.INPUT_INVALID,
                expected="input parameters matching the capability contract",
                observed=str(exc), detail=str(exc)))

        # 2. Enter at the (parameterized) entry point.
        entry = base.rstrip("/") + render(resolved.target.entry_point, bound)
        nav = Action(kind=ActionKind.NAVIGATE, value=entry)
        decision = self.policy.check(nav, risk=RiskClass.SAFE)
        if decision.blocked:
            return self._blocked(result, started, decision.reason, step_id=None)
        try:
            self._dispatch(nav)
        except Exception as exc:
            return self._fail(result, started, FailureDetail(
                kind=FailureKind.SURFACE_ERROR, expected=f"entry point {entry} to load",
                observed=f"{type(exc).__name__}: {exc}"))

        # 3. Steps.
        for step in resolved.steps:
            outcome = self._run_step(step, bound, resolved, result, started)
            if outcome is not None:
                return outcome

        # 4. Overall success checkpoint.
        obs = self.surface.observe()
        passed, observed = evaluate(resolved.success, obs, self.surface)
        if not passed:
            if (hit := self._match_outcome(resolved, obs)) is not None:
                return self._business(result, started, hit)
            return self._fail(result, started, FailureDetail(
                kind=FailureKind.CHECKPOINT_FAILED, expected=resolved.success.expected,
                observed=observed, location=obs.location,
                screenshot=self._shot("success-checkpoint-failed")))

        # 5. Every promised output must be present.
        for spec in resolved.outputs:
            if spec.required and spec.name not in result.outputs:
                return self._fail(result, started, FailureDetail(
                    kind=FailureKind.OUTPUT_MISSING,
                    expected=f"output {spec.name!r} to be extracted",
                    observed=f"outputs produced: {sorted(result.outputs)}",
                    location=obs.location, screenshot=self._shot("output-missing")))

        result.duration_ms = int((time.monotonic() - started) * 1000)
        self._log("replay.success", outputs=result.outputs, degraded=result.degraded)
        return result

    # ----------------------------------------------------------------- step

    def _run_step(
        self,
        step: Step,
        params: dict[str, str],
        artifact: CapabilityArtifact,
        result: ReplayResult,
        started: float,
    ) -> ReplayResult | None:
        """Execute one step. Returns a terminal ReplayResult, or None to continue."""
        t0 = time.monotonic()
        escalations = 0
        action = step.action.model_copy(deep=True)
        action.value = render(action.value, params)
        if action.target is not None:
            action.target = self._render_descriptor(action.target, params)

        # Policy first, always. It needs the address, not the whole tree.
        decision = self.policy.check(action, risk=step.risk,
                                     current_url=self.surface.location())
        if decision.blocked:
            if decision.requires_confirmation and self.escalator is not None:
                # Only now is a full observation worth its cost: an operator
                # needs to see the screen they are being handed.
                if self._try_escalate_and_resume(
                    result, step, reason=decision.reason,
                    observation=self.surface.observe(),
                ) is Resolution.RESUMED:
                    # The operator performed the risky step themselves. Verify the
                    # expected state rather than re-running the mutation.
                    obs_now = self.surface.observe()
                    if step.checkpoint is None or evaluate(step.checkpoint, obs_now, self.surface)[0]:
                        result.steps.append(StepTrace(
                            step_id=step.id, intent=step.intent, action=action.describe(),
                            status="performed_by_human",
                            note=f"operator completed this step; intervention {result.escalation_id}"))
                        self._log("step.performed_by_human", step=step.id,
                                  intervention=result.escalation_id)
                        return None
                result.status = ReplayStatus.ESCALATED
                return self._finish(result, started)
            return self._blocked(result, started, decision.reason, step_id=step.id)

        # Act, with bounded recovery for declared conditions.
        attempts = 0
        max_attempts = 1 + max((r.max_attempts for r in step.recovery), default=0)
        last_error = ""
        while attempts < max_attempts:
            attempts += 1
            try:
                res = self._dispatch(action)
                if res.ok:
                    break
                last_error = res.error or "action failed"
            except ElementNotFound as exc:
                last_error = str(exc)
                res = None
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                res = None

            obs = self.surface.observe()

            # A legitimate business answer can look exactly like a broken step.
            if (hit := self._match_outcome(artifact, obs)) is not None and hit.terminal:
                return self._business(result, started, hit)

            if (cleared := self._try_recover(step, obs, result)) :
                continue
            if step.optional:
                result.steps.append(StepTrace(
                    step_id=step.id, intent=step.intent, action=action.describe(),
                    status="skipped", note="optional step; target not present on this tenant",
                    duration_ms=int((time.monotonic() - t0) * 1000)))
                self._log("step.skipped", step=step.id, reason=last_error)
                return None
            if self.escalator is not None and escalations < self.max_escalations_per_step:
                escalations += 1
                if self._try_escalate_and_resume(
                    result, step, reason=last_error, observation=obs
                ) is Resolution.RESUMED:
                    max_attempts += 1          # the human bought this step another go
                    continue
                result.status = ReplayStatus.ESCALATED
                return self._finish(result, started)
            return self._fail(result, started, FailureDetail(
                kind=FailureKind.TARGET_UNRESOLVABLE, step_id=step.id, step_intent=step.intent,
                expected=f"to {action.describe()}", observed=last_error,
                location=obs.location, screenshot=self._shot(f"{step.id}-unresolvable")))
        else:
            return self._fail(result, started, FailureDetail(
                kind=FailureKind.RECOVERY_EXHAUSTED, step_id=step.id, step_intent=step.intent,
                expected=f"to {action.describe()} after recovery",
                observed=last_error, screenshot=self._shot(f"{step.id}-recovery-exhausted")))

        trace = StepTrace(
            step_id=step.id, intent=step.intent, action=action.describe(), status="ok",
            tier_used=res.resolution.tier_kind if res and res.resolution else None,
            tier_index=res.resolution.tier_index if res and res.resolution else None,
            used_fallback=bool(res and res.resolution and res.resolution.used_fallback),
        )

        obs_after = self.surface.observe()

        # Business outcome check BEFORE the checkpoint, so a legitimate answer is
        # never reported as a failed assertion.
        if (hit := self._match_outcome(artifact, obs_after)) is not None:
            if hit.terminal:
                result.steps.append(trace)
                return self._business(result, started, hit)
            self._log("outcome.noted", code=hit.code, step=step.id)
            trace.note = f"non-terminal outcome: {hit.code}"

        if step.checkpoint is not None:
            passed, observed = evaluate(step.checkpoint, obs_after, self.surface)

            # A failed checkpoint is a candidate for human help before it is
            # declared a failure: the operator may be able to reach the expected
            # screen by hand. We re-evaluate afterwards rather than trusting that
            # they did -- the checkpoint is the arbiter either way.
            if (not passed and self.escalator is not None
                    and escalations < self.max_escalations_per_step):
                escalations += 1
                if self._try_escalate_and_resume(
                    result, step,
                    reason=f"checkpoint failed: {observed}",
                    observation=obs_after,
                ) is Resolution.RESUMED:
                    obs_after = self.surface.observe()
                    passed, observed = evaluate(step.checkpoint, obs_after, self.surface)

            trace.checkpoint_passed = passed
            if not passed:
                result.steps.append(trace)
                if self.escalator is not None:
                    result.status = ReplayStatus.ESCALATED
                    return self._finish(result, started)
                return self._fail(result, started, FailureDetail(
                    kind=FailureKind.CHECKPOINT_FAILED, step_id=step.id, step_intent=step.intent,
                    expected=step.checkpoint.expected, observed=observed,
                    location=obs_after.location, screenshot=self._shot(f"{step.id}-checkpoint")))

        # Extractions.
        for binding in step.extracts:
            raw = res.extracted if res else None
            if raw is None:
                result.steps.append(trace)
                return self._fail(result, started, FailureDetail(
                    kind=FailureKind.OUTPUT_MISSING, step_id=step.id, step_intent=step.intent,
                    expected=f"a value for output {binding.output!r}",
                    observed="the step read no value", location=obs_after.location,
                    screenshot=self._shot(f"{step.id}-no-value")))
            value = self._transform(raw, binding.transform)
            result.outputs[binding.output] = value
            trace.extracted[binding.output] = value

        trace.duration_ms = int((time.monotonic() - t0) * 1000)
        result.steps.append(trace)
        self._log("step.ok", step=step.id, action=action.describe(),
                  tier=trace.tier_used, fallback=trace.used_fallback, extracted=trace.extracted)
        return None

    # ------------------------------------------------------------- helpers

    def _bind_params(self, artifact: CapabilityArtifact, params: dict[str, Any]) -> dict[str, str]:
        bound: dict[str, str] = {}
        for spec in artifact.inputs:
            if spec.name not in params:
                if spec.required:
                    raise ValueError(f"missing required parameter {spec.name!r}")
                continue
            bound[spec.name] = spec.validate_value(params[spec.name])
        if unknown := set(params) - {p.name for p in artifact.inputs}:
            raise ValueError(f"unknown parameter(s): {sorted(unknown)}")
        return bound

    def _render_descriptor(self, descriptor, params: dict[str, str]):
        """Templates can appear inside descriptors too (e.g. a row keyed by {memberId})."""
        d = descriptor.model_copy(deep=True)
        for tier in [d.primary, *d.fallbacks]:
            for field in ("name", "label_text", "row_label", "column_header"):
                if (val := getattr(tier, field, None)) and isinstance(val, str):
                    setattr(tier, field, render(val, params))
        return d

    def _match_outcome(self, artifact: CapabilityArtifact, obs: Observation) -> OutcomeRule | None:
        for rule in artifact.business_outcomes:
            passed, _ = evaluate(rule.detect, obs, self.surface)
            if passed:
                return rule
        return None

    def _try_recover(self, step: Step, obs: Observation, result: ReplayResult) -> bool:
        """Apply the first matching recovery rule. Bounded by its own attempt budget."""
        for rule in step.recovery:
            matched, _ = evaluate(rule.detect, obs, self.surface)
            if not matched:
                continue
            used = sum(1 for e in result.recoveries if e.step_id == step.id and e.rule_name == rule.name)
            if used >= rule.max_attempts:
                self._log("recovery.exhausted", step=step.id, rule=rule.name, attempts=used)
                return False
            for act in rule.actions:
                try:
                    self._dispatch(act)
                except Exception as exc:
                    self._log("recovery.action_failed", step=step.id, rule=rule.name, error=str(exc))
            result.recoveries.append(
                RecoveryEvent(step_id=step.id, rule_name=rule.name, attempt=used + 1, cleared=True))
            self._log("recovery.applied", step=step.id, rule=rule.name, attempt=used + 1)
            return rule.retry_step
        return False

    @staticmethod
    def _transform(raw: str, kind: str) -> str:
        match kind:
            case "strip":
                return raw.strip()
            case "digits":
                return re.sub(r"\D", "", raw)
            case "money":
                m = re.search(r"-?\$?[\d,]+(?:\.\d{2})?", raw)
                return m.group(0) if m else raw.strip()
        return raw

    def _shot(self, label: str) -> str | None:
        return self.recorder.screenshot(self.surface, label) if self.recorder else None

    def _log(self, event: str, **fields) -> None:
        if self.recorder:
            self.recorder.event(event, **fields)

    # --------------------------------------------------------- terminations

    def _finish(self, result: ReplayResult, started: float) -> ReplayResult:
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    def _fail(self, result: ReplayResult, started: float, failure: FailureDetail) -> ReplayResult:
        result.status = ReplayStatus.FAILURE
        result.failure = failure
        self._log("replay.failure", kind=failure.kind, step=failure.step_id,
                  expected=failure.expected, observed=failure.observed)
        return self._finish(result, started)

    def _business(self, result: ReplayResult, started: float, rule: OutcomeRule) -> ReplayResult:
        result.status = ReplayStatus.BUSINESS_OUTCOME
        result.outcome_code = rule.code
        result.outcome_message = rule.message
        self._log("replay.business_outcome", code=rule.code, message=rule.message)
        self._shot(f"outcome-{rule.code}")
        return self._finish(result, started)

    def _blocked(self, result: ReplayResult, started: float, reason: str, step_id: str | None) -> ReplayResult:
        result.status = ReplayStatus.BLOCKED
        result.failure = FailureDetail(
            kind=FailureKind.POLICY_BLOCKED, step_id=step_id,
            expected="an action permitted by policy", observed=reason, detail=reason)
        self._log("replay.blocked", step=step_id, reason=reason)
        return self._finish(result, started)

    def _escalate(self, result: ReplayResult, started: float, step: Step, *,
                  reason: str, observation: Observation) -> ReplayResult:
        """Terminal escalation: nobody took the session, so report and stop."""
        request = self.escalator.raise_intervention(
            capability_id=result.capability_id, run_id=result.run_id, step=step,
            reason=reason, observation=observation, surface=self.surface,
            recorder=self.recorder)
        result.status = ReplayStatus.ESCALATED
        result.escalation_id = request.id
        self._log("replay.escalated", step=step.id, reason=reason, intervention=request.id)
        return self._finish(result, started)

    def _try_escalate_and_resume(self, result: ReplayResult, step: Step, *,
                                 reason: str, observation: Observation) -> Resolution:
        """Hand the live session to a human and wait for it back.

        On RESUMED the caller retries the step; the automation deliberately does
        not assume the human did what was asked, because the step's own
        checkpoint is re-evaluated on the retry.
        """
        request = self.escalator.raise_intervention(
            capability_id=result.capability_id, run_id=result.run_id, step=step,
            reason=reason, observation=observation, surface=self.surface,
            recorder=self.recorder)
        result.escalation_id = request.id
        self._log("replay.escalated", step=step.id, reason=reason, intervention=request.id)

        resolution = self.escalator.await_resolution(request, self.surface, self.recorder)
        if resolution is Resolution.RESUMED:
            # Control came back with a fresh token; the old one is now stale.
            self._token = self.control.token if self.control else ""
            self._log("replay.resumed", step=step.id, intervention=request.id,
                      human_actions=request.human_actions)
        return resolution

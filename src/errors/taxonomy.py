"""The result contract, and the three-way split the brief cares most about.

    "'no such member' is a legitimate answer the caller needs, not a crash.
     Conflating the two is the most common design mistake here."

So the split is expressed in the *type*, not in a status string a caller might
skim past:

  SUCCESS           the goal was reached; declared outputs are present
  BUSINESS_OUTCOME  the application gave a legitimate non-success answer
                    ("no such member", "account closed"). The capability worked.
                    Exit code 0 -- this is not an error.
  FAILURE           execution broke. Carries step id, what was expected, what was
                    observed, and where the evidence is.
  ESCALATED         the run stopped and handed control to a human.
  BLOCKED           policy refused the action before it ran.

Recoverable conditions deliberately have no status: recovering is not an outcome,
it is something that happened on the way to one. They are recorded in
`recoveries` so a reviewer can see a replay is quietly degrading -- a capability
that recovers on every run is one UI change away from failing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class ReplayStatus(StrEnum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"
    ESCALATED = "escalated"
    BLOCKED = "blocked"


#: Statuses that mean "the system behaved correctly" -- the process exits 0 for
#: these. A business outcome is a working capability reporting a real answer.
NON_ERROR_STATUSES = {ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME}


class FailureKind(StrEnum):
    TARGET_UNRESOLVABLE = "target_unresolvable"     # no descriptor tier matched
    CHECKPOINT_FAILED = "checkpoint_failed"         # we did not reach the expected state
    OUTPUT_MISSING = "output_missing"               # reached success without a promised output
    INPUT_INVALID = "input_invalid"                 # caller supplied bad parameters
    RECOVERY_EXHAUSTED = "recovery_exhausted"       # a known condition would not clear
    SURFACE_ERROR = "surface_error"                 # the surface itself failed
    POLICY_BLOCKED = "policy_blocked"               # guardrail refused


class FailureDetail(BaseModel):
    """Everything needed to debug without re-running. The brief asks for exactly these three."""

    kind: FailureKind
    step_id: str | None = None
    step_intent: str | None = None
    expected: str = Field(description="What should have been true.")
    observed: str = Field(description="What was actually seen.")
    detail: str = ""
    screenshot: str | None = None
    location: str | None = None


class RecoveryEvent(BaseModel):
    step_id: str
    rule_name: str
    attempt: int
    cleared: bool


class StepTrace(BaseModel):
    """One step as executed. The structured log the brief asks for in 3.5."""

    step_id: str
    intent: str
    action: str
    status: str
    tier_used: str | None = None
    tier_index: int | None = None
    used_fallback: bool = False
    checkpoint_passed: bool | None = None
    extracted: dict[str, str] = Field(default_factory=dict)
    duration_ms: int = 0
    note: str = ""


class ReplayResult(BaseModel):
    """What an AI agent gets back when it invokes a capability."""

    status: ReplayStatus
    capability_id: str
    artifact_version: int
    run_id: str
    tenant_id: str | None = None

    outputs: dict[str, str] = Field(default_factory=dict)

    outcome_code: str | None = Field(default=None, description="Set when status is business_outcome.")
    outcome_message: str | None = None

    failure: FailureDetail | None = None
    escalation_id: str | None = None

    steps: list[StepTrace] = Field(default_factory=list)
    recoveries: list[RecoveryEvent] = Field(default_factory=list)

    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int = 0
    evidence_dir: str | None = None

    @property
    def ok(self) -> bool:
        """True when the system behaved correctly -- including a business outcome."""
        return self.status in NON_ERROR_STATUSES

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    @property
    def degraded(self) -> bool:
        """Succeeded, but only via fallback tiers or recovery. A drift warning."""
        return self.ok and (bool(self.recoveries) or any(s.used_fallback for s in self.steps))

    def summary(self) -> str:
        match self.status:
            case ReplayStatus.SUCCESS:
                out = ", ".join(f"{k}={v}" for k, v in self.outputs.items()) or "no outputs"
                return f"SUCCESS  {self.capability_id}  {out}"
            case ReplayStatus.BUSINESS_OUTCOME:
                return f"BUSINESS OUTCOME  {self.outcome_code}: {self.outcome_message}"
            case ReplayStatus.FAILURE:
                f = self.failure
                return (f"FAILURE  [{f.kind}] at step {f.step_id!r}\n"
                        f"  expected: {f.expected}\n  observed: {f.observed}") if f else "FAILURE"
            case ReplayStatus.ESCALATED:
                return f"ESCALATED  intervention {self.escalation_id}"
            case ReplayStatus.BLOCKED:
                f = self.failure
                return f"BLOCKED  {f.detail if f else 'policy refused the action'}"
        return self.status

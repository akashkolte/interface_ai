"""The capability artifact: what a successful discovery run becomes.

This is the contract between the AI agent that wants something done and the
replay engine that does it. It is deliberately *not* a transcript and *not* a
step list -- it is closer to a function signature with a body.

Three decisions shape it, and each is defensible on its own:

1. **It declares its own non-success answers.** `business_outcomes` lets the
   capability say "if the page says this, that is a legitimate result called
   `member_not_found`, not a failure". Without that, the replay engine has to
   guess -- and the brief names conflating outcomes with failures as the most
   common design mistake in this problem. Which results are legitimate is
   capability-specific knowledge discovered *once*, so it belongs in the
   artifact, next to the steps that produce it.

2. **Recovery is declared per step, and bounded.** A known interstitial or a
   session timeout is handled by a rule with a finite attempt budget, not by an
   open-ended retry loop and not by calling a model back into the decision path.

3. **Tenant differences are sparse patches, not forks.** Hundreds of
   institutions run the same vendor product. `tenant_overrides` carries only
   what differs, so drift is a diff a human can review rather than a re-record.

Everything here is Pydantic, so the JSON on disk and the type in memory cannot
drift apart, and an invalid artifact fails loudly at load time.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from src.surface.base import Action, ActionKind
from src.surface.descriptors import ElementDescriptor

SCHEMA_VERSION = "1.0"

#: `{name}` placeholders in entry points, action values and checkpoint text.
PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


# ------------------------------------------------------------------- risk


class RiskClass(StrEnum):
    """How much damage a step can do if it fires when it shouldn't.

    Classified per step at record time so the replay engine never has to infer
    intent from an action verb alone -- `click` is harmless on a search button
    and irreversible on a wire-transfer confirm.
    """

    SAFE = "safe"          # read-only or trivially reversible
    CONFIRM = "confirm"    # mutates state; needs explicit authorization to run unattended
    BLOCKED = "blocked"    # irreversible / out of policy; never runs unattended


# -------------------------------------------------------------- typed I/O


class ParamType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    MONEY = "money"


class ParamSpec(BaseModel):
    """One input the calling agent must supply."""

    name: str
    type: ParamType = ParamType.STRING
    required: bool = True
    description: str = ""
    pattern: str | None = Field(default=None, description="Optional regex the value must match.")
    example: str | None = Field(
        default=None,
        description="Illustrative value only. Never a real captured value -- see redaction.",
    )
    sensitive: bool = Field(
        default=False,
        description="If true the value is redacted everywhere it would otherwise be logged.",
    )

    def validate_value(self, value: Any) -> str:
        """Coerce and check one supplied value. Raises ValueError with a caller-usable message."""
        if value is None:
            raise ValueError(f"parameter {self.name!r} is required")
        s = str(value)
        match self.type:
            case ParamType.INTEGER:
                if not re.fullmatch(r"-?\d+", s.strip()):
                    raise ValueError(f"parameter {self.name!r} must be an integer, got {s!r}")
            case ParamType.NUMBER | ParamType.MONEY:
                if not re.fullmatch(r"-?\$?\d[\d,]*(\.\d+)?", s.strip()):
                    raise ValueError(f"parameter {self.name!r} must be a number, got {s!r}")
            case ParamType.BOOLEAN:
                if s.strip().lower() not in {"true", "false", "1", "0", "yes", "no"}:
                    raise ValueError(f"parameter {self.name!r} must be a boolean, got {s!r}")
        if self.pattern and not re.fullmatch(self.pattern, s):
            raise ValueError(f"parameter {self.name!r} does not match {self.pattern!r}")
        return s


class OutputSpec(BaseModel):
    """One value the capability promises to return."""

    name: str
    type: ParamType = ParamType.STRING
    description: str = ""
    from_step: str = Field(description="Id of the step whose extraction produces this value.")
    required: bool = Field(
        default=True,
        description="If true, a run that reaches success without this value is a hard failure.",
    )


# ----------------------------------------------------------- checkpoints


class TextPresent(BaseModel):
    """Assert visible text. The workhorse: legacy screens signal state in prose."""

    kind: Literal["text_present"] = "text_present"
    text: str
    negate: bool = False
    case_sensitive: bool = False


class ElementPresent(BaseModel):
    kind: Literal["element_present"] = "element_present"
    descriptor: ElementDescriptor
    negate: bool = False


class LocationMatches(BaseModel):
    kind: Literal["location_matches"] = "location_matches"
    pattern: str = Field(description="Regex matched against the surface location.")


CheckpointRule = Annotated[
    Union[TextPresent, ElementPresent, LocationMatches],
    Field(discriminator="kind"),
]


class Checkpoint(BaseModel):
    """A condition asserted to prove we actually reached the expected state.

    Every checkpoint carries `expected` in words. When one fails, the failure
    report can say what was expected and what was observed instead of "element
    not found", which is the difference between a debuggable error and a shrug.
    """

    rules: list[CheckpointRule] = Field(min_length=1)
    expected: str = Field(description="Human-readable statement of what should be true.")
    all_required: bool = True
    timeout_ms: int = 8000


# ------------------------------------------------- outcomes and recovery


class OutcomeRule(BaseModel):
    """A non-success result the caller needs to hear about, declared in advance.

    "No such member" is an answer, not a crash. Declaring these makes that
    distinction data rather than a heuristic buried in the executor.
    """

    code: str = Field(description="Stable machine code, e.g. 'member_not_found'.")
    detect: Checkpoint
    message: str = Field(description="Human-readable explanation for the caller.")
    terminal: bool = Field(
        default=True, description="If true the run stops here and reports this outcome."
    )


class RecoveryRule(BaseModel):
    """A known, bounded way out of a transient or interstitial condition.

    Bounded on purpose: `max_attempts` is what keeps "recoverable" from becoming
    an infinite loop, and what makes exhausting it a clean escalation trigger.
    """

    name: str
    detect: Checkpoint
    actions: list[Action] = Field(
        default_factory=list, description="Actions that clear the condition, e.g. dismiss a dialog."
    )
    max_attempts: int = Field(default=2, ge=1, le=5)
    retry_step: bool = Field(
        default=True, description="Re-run the step after recovering, rather than moving on."
    )


# ---------------------------------------------------------------- steps


class OutputBinding(BaseModel):
    """Bind what a step reads to a declared output name."""

    output: str
    transform: Literal["none", "strip", "digits", "money"] = "none"


class Step(BaseModel):
    id: str
    intent: str = Field(
        description="Why this step exists, in operator language. Survives when the UI does not."
    )
    action: Action
    param_bindings: list[str] = Field(
        default_factory=list,
        description="Input parameter names referenced by this step's value template.",
    )
    extracts: list[OutputBinding] = Field(default_factory=list)
    checkpoint: Checkpoint | None = None
    risk: RiskClass = RiskClass.SAFE
    recovery: list[RecoveryRule] = Field(default_factory=list)
    optional: bool = Field(
        default=False,
        description=(
            "If true, an unresolvable target is skipped rather than failed. Reserved for "
            "steps that only exist on some tenants, e.g. a disclosure interstitial."
        ),
    )

    @model_validator(mode="after")
    def _extraction_needs_read(self) -> "Step":
        if self.extracts and self.action.kind is not ActionKind.READ:
            raise ValueError(f"step {self.id!r} extracts a value but its action is not 'read'")
        return self


# ------------------------------------------------------------- artifact


class TargetSpec(BaseModel):
    app_id: str = Field(description="The vendor product this flow belongs to.")
    app_version_hint: str | None = None
    entry_point: str = Field(description="Parameterized start location, e.g. '/member/{memberId}'.")
    surface_kind: str = Field(default="web.playwright.chromium")


class Provenance(BaseModel):
    """How this artifact came to exist. Reviewability depends on it."""

    discovery_run_id: str
    model: str
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recorded_against_tenant: str
    recorded_against_base_url: str
    steps_explored: int = 0
    notes: str = ""


class TenantOverride(BaseModel):
    """A sparse patch for one tenant running the same product.

    Only differences live here. Anything absent is inherited from the base
    artifact, so a re-branded field label is a three-line diff rather than a
    second copy of the capability that will silently rot.
    """

    tenant_id: str
    base_url: str | None = None
    app_version_hint: str | None = None
    step_targets: dict[str, ElementDescriptor] = Field(
        default_factory=dict, description="step id -> replacement descriptor"
    )
    step_checkpoints: dict[str, Checkpoint] = Field(default_factory=dict)
    extra_recovery: list[RecoveryRule] = Field(
        default_factory=list,
        description="Rules for conditions only this tenant has, e.g. a disclosure interstitial.",
    )
    disabled_steps: list[str] = Field(default_factory=list)
    note: str = ""


class CapabilityArtifact(BaseModel):
    """A reusable, reviewable, agent-invocable capability."""

    schema_version: str = SCHEMA_VERSION
    capability_id: str = Field(description="Stable dotted id, e.g. 'member.read_savings_balance'.")
    version: int = Field(default=1, ge=1, description="Artifact revision, monotonic.")

    name: str
    description: str = Field(
        description="What a calling agent reads to decide whether to invoke this."
    )
    goal: str = Field(description="The natural-language goal this was discovered from.")

    target: TargetSpec
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    success: Checkpoint

    business_outcomes: list[OutcomeRule] = Field(default_factory=list)
    risk_profile: RiskClass = RiskClass.SAFE
    provenance: Provenance | None = None
    tenant_overrides: dict[str, TenantOverride] = Field(default_factory=dict)

    # ------------------------------------------------------------ validation

    @field_validator("capability_id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", v):
            raise ValueError("capability_id must be dotted snake_case, e.g. 'member.read_balance'")
        return v

    @model_validator(mode="after")
    def _coherent(self) -> "CapabilityArtifact":
        step_ids = [s.id for s in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("step ids must be unique")

        declared = {p.name for p in self.inputs}
        for tmpl in self.placeholders():
            if tmpl not in declared:
                raise ValueError(f"{{{tmpl}}} is used but not declared as an input parameter")

        out_names = {o.name for o in self.outputs}
        for o in self.outputs:
            if o.from_step not in step_ids:
                raise ValueError(f"output {o.name!r} references unknown step {o.from_step!r}")
        for s in self.steps:
            for b in s.extracts:
                if b.output not in out_names:
                    raise ValueError(f"step {s.id!r} extracts undeclared output {b.output!r}")

        # risk_profile must not understate the steps.
        order = [RiskClass.SAFE, RiskClass.CONFIRM, RiskClass.BLOCKED]
        highest = max((s.risk for s in self.steps), key=order.index, default=RiskClass.SAFE)
        if order.index(self.risk_profile) < order.index(highest):
            raise ValueError(
                f"risk_profile {self.risk_profile} understates the highest step risk {highest}"
            )
        return self

    # -------------------------------------------------------------- helpers

    def placeholders(self) -> set[str]:
        """Every `{param}` referenced anywhere in the artifact."""
        found: set[str] = set(PLACEHOLDER.findall(self.target.entry_point))
        for s in self.steps:
            if s.action.value:
                found |= set(PLACEHOLDER.findall(s.action.value))
        return found

    def input(self, name: str) -> ParamSpec | None:
        return next((p for p in self.inputs if p.name == name), None)

    def for_tenant(self, tenant_id: str | None) -> "CapabilityArtifact":
        """Apply a tenant's sparse patch, returning a resolved artifact.

        Returns `self` unchanged when there is no override, so the base tenant
        costs nothing and the common path is the simple one.
        """
        if not tenant_id or tenant_id not in self.tenant_overrides:
            return self
        ov = self.tenant_overrides[tenant_id]
        patched = self.model_copy(deep=True)

        if ov.app_version_hint:
            patched.target.app_version_hint = ov.app_version_hint

        kept: list[Step] = []
        for step in patched.steps:
            if step.id in ov.disabled_steps:
                continue
            if (d := ov.step_targets.get(step.id)) is not None:
                step.action.target = d
            if (c := ov.step_checkpoints.get(step.id)) is not None:
                step.checkpoint = c
            kept.append(step)
        patched.steps = kept

        if ov.extra_recovery and patched.steps:
            # Tenant-specific conditions (a disclosure page) can appear at any
            # step, so the rules are attached to every step rather than guessed at.
            for step in patched.steps:
                step.recovery = [*step.recovery, *ov.extra_recovery]
        return patched

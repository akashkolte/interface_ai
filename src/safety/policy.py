"""The guardrail choke point.

Every action taken by this system -- during LLM discovery and during
deterministic replay alike -- passes through `Policy.check`. There is exactly
one such path, because a guardrail with two entry points is a guardrail with a
bypass.

Two independent questions are asked, and both must pass:

  1. Is the *destination* permitted?  -- allowlist of origins and route patterns
  2. Is the *action* permitted?       -- action kind, plus the step's risk class

Risk is not inferred from the verb. `click` is harmless on a search button and
irreversible on a wire-transfer confirm, so the risk class is recorded per step
at discovery time and checked here. Unattended replay refuses anything above
SAFE unless explicitly authorized by the caller -- the conservative default the
brief asks for, with the escape hatch made explicit rather than implicit.
"""

from __future__ import annotations

import fnmatch
import re
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from src.artifact.schema import RiskClass
from src.surface.base import Action, ActionKind

#: Route fragments that suggest an irreversible money movement or destructive
#: admin operation. Matching one forces BLOCKED regardless of recorded risk --
#: a belt-and-braces check in case a discovery run mis-classified a step.
IRREVERSIBLE_HINTS = (
    "transfer", "wire", "payment", "paybill", "withdraw", "disburse",
    "delete", "remove", "close-account", "closeaccount", "approve", "authorize",
)


class PolicyDecision(BaseModel):
    allowed: bool
    reason: str = ""
    requires_confirmation: bool = False

    @property
    def blocked(self) -> bool:
        return not self.allowed


class Policy(BaseModel):
    """Configurable, explicit, and inspectable. Deny by default."""

    allowed_origins: list[str] = Field(
        default_factory=list,
        description="Exact origins the agent may touch, e.g. 'http://localhost:5010'.",
    )
    allowed_route_globs: list[str] = Field(
        default_factory=lambda: ["/*"],
        description="Glob patterns for permitted paths within an allowed origin.",
    )
    denied_route_globs: list[str] = Field(
        default_factory=lambda: ["/admin*"],
        description="Checked before the allowlist; a match always denies.",
    )
    allowed_actions: list[ActionKind] = Field(
        default_factory=lambda: list(ActionKind),
        description="Action kinds permitted at all.",
    )
    allow_risky: bool = Field(
        default=False,
        description=(
            "Authorizes CONFIRM-class steps to run unattended. Off by default: an "
            "agent invoking a capability should have to opt into state changes."
        ),
    )
    max_steps: int = Field(default=40, ge=1)

    # ------------------------------------------------------------------ checks

    def check_location(self, url: str) -> PolicyDecision:
        if not url or url == "about:blank":
            return PolicyDecision(allowed=True, reason="no navigation")
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path or "/"

        if origin not in self.allowed_origins:
            return PolicyDecision(
                allowed=False,
                reason=f"origin {origin!r} is not in the allowlist {self.allowed_origins}",
            )
        for glob in self.denied_route_globs:
            if fnmatch.fnmatch(path, glob):
                return PolicyDecision(allowed=False, reason=f"route {path!r} matches deny rule {glob!r}")
        if not any(fnmatch.fnmatch(path, g) for g in self.allowed_route_globs):
            return PolicyDecision(allowed=False, reason=f"route {path!r} is not in the route allowlist")
        return PolicyDecision(allowed=True, reason="permitted")

    def check(
        self,
        action: Action,
        *,
        risk: RiskClass = RiskClass.SAFE,
        current_url: str = "",
    ) -> PolicyDecision:
        """The single gate. Every action, both loops."""
        if action.kind not in self.allowed_actions:
            return PolicyDecision(allowed=False, reason=f"action kind {action.kind!r} is not permitted")

        # Where we are, and where we are being sent.
        target_url = action.value if action.kind is ActionKind.NAVIGATE else current_url
        if target_url:
            decision = self.check_location(target_url)
            if decision.blocked:
                return decision

        if risk is RiskClass.BLOCKED:
            return PolicyDecision(allowed=False, reason="step is classified BLOCKED and never runs unattended")

        # Independent check on the destination itself: a recorded risk class is
        # a claim made at discovery time, and claims can be wrong.
        haystack = f"{target_url} {action.value or ''}".lower()
        if action.kind not in {ActionKind.READ, ActionKind.WAIT_FOR}:
            for hint in IRREVERSIBLE_HINTS:
                if hint in haystack:
                    return PolicyDecision(
                        allowed=False,
                        reason=f"target matches irreversible-operation pattern {hint!r}",
                    )

        if risk is RiskClass.CONFIRM and not self.allow_risky:
            return PolicyDecision(
                allowed=False,
                requires_confirmation=True,
                reason="step mutates state (CONFIRM); requires --allow-risky or human confirmation",
            )
        return PolicyDecision(allowed=True, reason="permitted")

    @classmethod
    def for_local_targets(cls, *origins: str, allow_risky: bool = False) -> "Policy":
        """Convenience for the demo: allow the tenant surfaces, deny /admin."""
        return cls(allowed_origins=list(origins), allow_risky=allow_risky)

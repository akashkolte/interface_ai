"""Control transfer between the automation and a human operator.

The requirement is not "log a TODO and stop". It is: pause the automation, let a
person drive **the same live session**, capture what they did, and resume from
where we left off -- with an unambiguous answer at every instant to "who is
allowed to act right now?"

That last part is why control is a token rather than a boolean. A boolean is a
convention any code path can ignore; a token that the executor must hold to
dispatch an action makes single-writer an *enforced* invariant. Handing control
to a human invalidates the token, so an automation step that races the human
fails loudly instead of fighting them for the mouse.

    AUTOMATION ──raise()──> INTERVENTION_REQUESTED ──take()──> HUMAN
         ^                                                       │
         └──────────── resume() ◄── RESUMING ◄──── hand_back() ──┘
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from src.safety.redaction import redact_deep, redact_text

INTERVENTION_DIR = Path("evidence/interventions")


class ControlState(StrEnum):
    AUTOMATION = "automation"
    INTERVENTION_REQUESTED = "intervention_requested"
    HUMAN = "human"
    RESUMING = "resuming"
    CLOSED = "closed"


class ControlViolation(RuntimeError):
    """Raised when something tries to act without holding control."""


class InterventionRequest(BaseModel):
    """Everything an operator needs to act, without reading the code.

    The brief asks for the capability/goal, the current step, the current state
    or screenshot, and *why* it stopped. All four are required fields here so an
    escalation cannot be raised without them.
    """

    id: str = Field(default_factory=lambda: f"iv_{uuid.uuid4().hex[:10]}")
    run_id: str
    capability_id: str
    goal: str = ""

    step_id: str | None = None
    step_intent: str | None = None
    reason: str = Field(description="Why the automation stopped.")

    location: str = ""
    screen_summary: str = ""
    screenshot: str | None = None
    params: dict[str, str] = Field(default_factory=dict)

    suggested_action: str = ""
    state: ControlState = ControlState.INTERVENTION_REQUESTED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    operator_note: str = ""
    human_actions: list[str] = Field(default_factory=list)

    def brief(self) -> str:
        return (
            f"[{self.id}] {self.capability_id} run={self.run_id}\n"
            f"  stopped at: {self.step_id} ({self.step_intent})\n"
            f"  why:        {self.reason}\n"
            f"  location:   {self.location}\n"
            f"  screen:     {self.screen_summary[:200]}\n"
            f"  suggestion: {self.suggested_action}"
        )


class InterventionStore:
    """File-backed queue.

    A directory of JSON files, deliberately: the operator console, the CLI and a
    human with `cat` all read the same thing, and an escalation survives the
    process that raised it. Swapping this for a real queue is one class.
    """

    def __init__(self, directory: Path = INTERVENTION_DIR) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, request_id: str) -> Path:
        return self.dir / f"{request_id}.json"

    def put(self, request: InterventionRequest) -> Path:
        dest = self.path(request.id)
        dest.write_text(json.dumps(redact_deep(request.model_dump(mode="json")), indent=2) + "\n")
        return dest

    def get(self, request_id: str) -> InterventionRequest | None:
        p = self.path(request_id)
        if not p.exists():
            return None
        return InterventionRequest.model_validate_json(p.read_text())

    def pending(self) -> list[InterventionRequest]:
        out = []
        for f in sorted(self.dir.glob("iv_*.json")):
            try:
                r = InterventionRequest.model_validate_json(f.read_text())
            except Exception:
                continue
            if r.state in {ControlState.INTERVENTION_REQUESTED, ControlState.HUMAN}:
                out.append(r)
        return out


class SessionControl:
    """Who may drive the session, enforced by a token.

    Thread-safe because the operator console answers HTTP on another thread while
    the run loop is blocked waiting for hand-back.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = ControlState.AUTOMATION
        self._token = uuid.uuid4().hex
        self._resumed = threading.Event()
        self.holder: str = "automation"

    @property
    def state(self) -> ControlState:
        with self._lock:
            return self._state

    @property
    def token(self) -> str:
        with self._lock:
            return self._token

    def guard(self, token: str) -> None:
        """Called before every dispatched action. The single-writer invariant."""
        with self._lock:
            if self._state is not ControlState.AUTOMATION:
                raise ControlViolation(
                    f"automation may not act while control is {self._state} (holder={self.holder})"
                )
            if token != self._token:
                raise ControlViolation("stale control token; control was transferred and returned")

    # ---------------------------------------------------------- transitions

    def request_intervention(self) -> None:
        with self._lock:
            if self._state is not ControlState.AUTOMATION:
                raise ControlViolation(f"cannot request intervention from {self._state}")
            self._state = ControlState.INTERVENTION_REQUESTED
            self._resumed.clear()

    def take(self, operator: str = "operator") -> None:
        """A human takes the wheel. Invalidates the automation's token."""
        with self._lock:
            if self._state not in {ControlState.INTERVENTION_REQUESTED, ControlState.AUTOMATION}:
                raise ControlViolation(f"cannot take control from {self._state}")
            self._state = ControlState.HUMAN
            self.holder = operator
            self._token = uuid.uuid4().hex   # any in-flight automation token is now stale

    def hand_back(self) -> str:
        """Human is done. Returns the fresh token the automation must use."""
        with self._lock:
            if self._state is not ControlState.HUMAN:
                raise ControlViolation(f"cannot hand back from {self._state}")
            self._state = ControlState.RESUMING
            self.holder = "automation"
            self._token = uuid.uuid4().hex
            self._resumed.set()
            return self._token

    def confirm_resumed(self) -> None:
        with self._lock:
            self._state = ControlState.AUTOMATION

    def wait_for_hand_back(self, timeout_s: float) -> bool:
        """Block the run loop while the human works. Returns False on timeout."""
        return self._resumed.wait(timeout=timeout_s)

    def close(self) -> None:
        with self._lock:
            self._state = ControlState.CLOSED
            self._resumed.set()

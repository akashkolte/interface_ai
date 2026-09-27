"""Raising an intervention, ceding the live session, and resuming afterwards.

The seam this implies, spelled out because it is the part that is easy to fake:

  * The automation and the human use the **same** browser session. Nothing is
    torn down and nothing is re-launched -- the human continues from whatever
    screen the automation was stuck on, which is the only way the context that
    made it stuck is still visible to them.
  * The automation *blocks* while the human works. It does not poll and retry in
    parallel, because two writers on one session is precisely the bug the
    control token exists to prevent.
  * On hand-back the automation does not assume the human did what was asked.
    It re-observes and re-evaluates the step's own checkpoint. If the expected
    state was not reached, it escalates again rather than proceeding blindly.

`headless` is the one real constraint: a human cannot drive a session they
cannot see, so any run that might escalate should launch headed.
"""

from __future__ import annotations

from enum import StrEnum

from src.artifact.schema import Step
from src.escalation.control import (
    ControlState,
    InterventionRequest,
    InterventionStore,
    SessionControl,
)
from src.safety.redaction import redact_text
from src.surface.base import Observation


class Resolution(StrEnum):
    RESUMED = "resumed"        # human handed control back; retry the step
    UNRESOLVED = "unresolved"  # nobody took it in time; terminate as ESCALATED


class Escalator:
    """Routes a stuck run to a human and brings it back."""

    def __init__(
        self,
        control: SessionControl | None = None,
        store: InterventionStore | None = None,
        *,
        wait_seconds: float = 0.0,
        auto_resolver=None,
    ) -> None:
        self.control = control or SessionControl()
        self.store = store or InterventionStore()
        #: How long to block waiting for a human. 0 means fire-and-return, which
        #: is what an unattended production run does -- the request is queued and
        #: the run reports ESCALATED rather than holding a browser open forever.
        self.wait_seconds = wait_seconds
        #: Test/demo hook: a callable that plays the operator's part.
        self.auto_resolver = auto_resolver
        self.last_request: InterventionRequest | None = None

    # ------------------------------------------------------------------ api

    def raise_intervention(
        self,
        *,
        capability_id: str,
        run_id: str,
        step: Step | None,
        reason: str,
        observation: Observation,
        surface=None,
        recorder=None,
        goal: str = "",
        params: dict[str, str] | None = None,
    ) -> InterventionRequest:
        shot = recorder.screenshot(surface, f"intervention-{step.id if step else 'run'}") \
            if (recorder and surface) else None

        request = InterventionRequest(
            run_id=run_id,
            capability_id=capability_id,
            goal=goal,
            step_id=step.id if step else None,
            step_intent=step.intent if step else None,
            reason=redact_text(reason),
            location=observation.location,
            screen_summary=redact_text(
                " | ".join(l.strip() for l in observation.text.splitlines() if l.strip())[:400]
            ),
            screenshot=shot,
            params=params or {},
            suggested_action=self._suggest(step, reason),
        )
        self.control.request_intervention()
        self.store.put(request)
        self.last_request = request
        if recorder:
            recorder.event("intervention.raised", intervention=request.id,
                           step=request.step_id, reason=request.reason)
        return request

    def await_resolution(self, request: InterventionRequest, surface=None, recorder=None) -> Resolution:
        """Block while a human drives, then take control back.

        Returns RESUMED when control came back, UNRESOLVED on timeout.
        """
        watcher = _HumanActionWatcher(surface)

        if self.auto_resolver is not None:
            # Demo/test path: something plays the operator. It still goes through
            # the real take/hand_back transitions -- the mechanism is not mocked,
            # only the person is.
            self.control.take("auto-operator")
            watcher.start()
            try:
                self.auto_resolver(surface, request)
            finally:
                watcher.stop()
            self.control.hand_back()
        else:
            if self.wait_seconds <= 0:
                return Resolution.UNRESOLVED
            watcher.start()
            got = self.control.wait_for_hand_back(self.wait_seconds)
            watcher.stop()
            if not got:
                return Resolution.UNRESOLVED

        if self.control.state is not ControlState.RESUMING:
            return Resolution.UNRESOLVED

        request.human_actions = watcher.actions
        request.state = ControlState.AUTOMATION
        from datetime import datetime, timezone
        request.resolved_at = datetime.now(timezone.utc)
        self.store.put(request)

        self.control.confirm_resumed()
        if recorder:
            recorder.event("intervention.resumed", intervention=request.id,
                           human_actions=watcher.actions)
        return Resolution.RESUMED

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _suggest(step: Step | None, reason: str) -> str:
        low = reason.lower()
        if "confirm" in low or "mutates state" in low:
            return "Review the pending change and complete it manually if it is correct, then hand control back."
        if "checkpoint failed" in low:
            return "Navigate to the expected screen manually, then hand control back."
        if "expired" in low or "timeout" in low:
            return "Sign in again, return to the screen shown, then hand control back."
        if step is not None:
            return f"Complete this manually: {step.intent}. Then hand control back."
        return "Resolve the condition on screen, then hand control back."


class _HumanActionWatcher:
    """Records what the human did, so the handoff is auditable.

    Navigation-level only. Full input capture would mean keylogging an operator
    inside a banking session, which is exactly the kind of data this system is
    supposed to avoid persisting.
    """

    def __init__(self, surface) -> None:
        self.surface = surface
        self.actions: list[str] = []
        self._page = getattr(surface, "page", None) if surface else None
        self._handler = None

    def start(self) -> None:
        if self._page is None:
            return
        def on_nav(frame):
            try:
                self.actions.append(f"navigated: {frame.url}")
            except Exception:
                pass
        self._handler = on_nav
        try:
            self._page.on("framenavigated", on_nav)
        except Exception:
            self._handler = None

    def stop(self) -> None:
        if self._page is not None and self._handler is not None:
            try:
                self._page.remove_listener("framenavigated", self._handler)
            except Exception:
                pass

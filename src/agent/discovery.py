"""The LLM-driven discovery loop: observe -> decide -> act, until the goal is met.

This is the only place a model sits in the decision path, and it runs once per
capability. Everything it learns is written into an artifact so that production
never pays this cost -- or this uncertainty -- again.

Two deliberate choices shape the loop:

**The model chooses a descriptor, not a selector.** The action tool's schema is
the tier vocabulary from `src.surface.descriptors`, so the model is required to
say *how it recognises* a control ("the button named Search", "the field beside
the text 'Member ID'") rather than pointing at markup. It must also supply a
`rationale` for that choice. That reasoning is the expensive part to recover
later and the cheapest thing to capture now, and it ends up in the artifact.

**Every action still passes the guardrail.** Discovery is not a trusted context.
The model proposes; `Policy.check` disposes. A refused action is fed back to the
model as an observation so it can choose differently, rather than aborting.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

from src.agent.llm import BedrockClient, LLMUnavailable
from src.artifact.schema import RiskClass
from src.evidence.recorder import EvidenceRecorder
from src.safety.policy import Policy
from src.surface.base import Action, ActionKind, Observation, Surface
from src.surface.descriptors import (
    AccessibleNameTier,
    ElementDescriptor,
    LabelProximityTier,
    OrdinalTier,
    Scope,
    TableCellTier,
)

SYSTEM = """You operate a legacy back-office banking application by driving its user interface, the way a human operator would. You cannot call any API.

You perceive the screen as an accessibility tree: each line is `role "accessible name" [value]`, grouped by frame. This is what a screen reader reads. Note that legacy form fields usually have an EMPTY accessible name -- they are named only by nearby text -- so to target an input you normally use `label_proximity`, not `accessible_name`.

FRAMES. The screen is listed grouped under `FRAME <name>:` headings. This application is built from a frameset, so almost every control lives in a named frame rather than the top document. If the control you want is listed under a frame heading other than `(main)`, you MUST pass that frame's name in the `frame` field. Omitting it will fail to find the control.

Choose exactly ONE action per turn. Work in small, verifiable steps.

How to target a control, in order of preference:
1. accessible_name  - a control with visible text (buttons, links). Most robust.
2. label_proximity  - a form field, identified by the text that labels it.
3. table_cell       - a value in a record grid, identified by its row label and/or column header.
4. ordinal          - the nth control of a role. Brittle; only when nothing else works.

Always explain in `rationale` why your targeting will still work after the vendor restyles the page or a different institution rebrands it. Avoid depending on anything a human operator would not use to recognise the control.

IMPORTANT -- reading values. If the goal asks you to report a value, you MUST perform an explicit `read` action for it, with `table_cell` targeting (its row label, and column header if the grid has one), even though you can already see the value on screen. Deterministic replay has no eyes: it can only return values that a recorded read action produces. A value you merely observed is not recorded and will not be returned in production.

Call `finish` with status "done" once the goal is satisfied AND every requested value has been explicitly read, or "stuck" if you cannot proceed."""

ACTION_TOOL = {
    "name": "take_action",
    "description": "Perform one action on the application surface.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "description": "Why this step, in operator language."},
            "kind": {"type": "string", "enum": ["click", "type", "select", "read", "navigate", "press_key"]},
            "targeting": {
                "type": "string",
                "enum": ["accessible_name", "label_proximity", "table_cell", "ordinal", "none"],
                "description": "How to identify the control. Use 'none' only for navigate/press_key.",
            },
            "role": {"type": "string", "description": "Control role, e.g. button, link, textbox."},
            "name": {"type": "string", "description": "Accessible name, for accessible_name targeting."},
            "label_text": {"type": "string", "description": "The text that labels the field, for label_proximity."},
            "row_label": {"type": "string", "description": "Row label, for table_cell."},
            "column_header": {"type": "string", "description": "Column header, for table_cell."},
            "index": {"type": "integer", "description": "Zero-based index, for ordinal."},
            "frame": {"type": "string", "description": "Frame name the control is in, e.g. contentframe. Omit for the top document."},
            "value": {"type": "string", "description": "Text to type, option to select, key to press, or URL to navigate to."},
            "rationale": {"type": "string", "description": "Why this targeting survives restyling and rebranding."},
            "mutates_state": {"type": "boolean", "description": "True if this action changes data on the server."},
        },
        "required": ["intent", "kind", "targeting", "rationale"],
    },
}

FINISH_TOOL = {
    "name": "finish",
    "description": "End the run when the goal is met or cannot be met.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["done", "stuck"]},
            "summary": {"type": "string"},
            "extracted": {
                "type": "object",
                "description": "Any values the goal asked for, as name -> value.",
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["status", "summary"],
    },
}

COMBINED_TOOL = {
    "name": "decide",
    "description": "Decide the next thing to do: either take one action, or finish.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": ACTION_TOOL["input_schema"],
            "finish": FINISH_TOOL["input_schema"],
        },
    },
}


@dataclass
class DiscoveryStep:
    """One decision and its consequence. Becomes an artifact step if the run succeeds."""

    intent: str
    action: Action
    rationale: str
    mutates_state: bool
    observation_before: Observation
    observation_after: Observation | None = None
    tier_used: str | None = None
    ok: bool = True
    note: str = ""


@dataclass
class DiscoveryRun:
    run_id: str
    goal: str
    base_url: str
    tenant_id: str
    model: str
    steps: list[DiscoveryStep] = field(default_factory=list)
    status: str = "running"          # running | done | stuck | failed
    summary: str = ""
    extracted: dict[str, str] = field(default_factory=dict)
    duration_ms: int = 0


def render_observation(obs: Observation, limit: int = 70) -> str:
    """Compact the accessibility tree into something worth spending tokens on.

    Only what an operator could act on or read: interactive controls, and the
    text that gives them meaning. The raw tree is mostly layout scaffolding.
    """
    by_frame: dict[str, list[str]] = {}
    for el in obs.elements:
        if el.role in {"document", "generic", "table", "row"}:
            continue
        if el.role == "text" and not el.name:
            continue
        key = "/".join(el.frame_path) or "(main)"
        bits = f'{el.role} "{el.name}"'
        if el.value:
            bits += f" [{el.value}]"
        if not el.enabled:
            bits += " (disabled)"
        by_frame.setdefault(key, []).append(bits)

    out = [f"LOCATION: {obs.location}"]
    for frame, items in by_frame.items():
        out.append(f"\nFRAME {frame}:")
        out.extend(f"  {b}" for b in items[:limit])
        if len(items) > limit:
            out.append(f"  ... {len(items) - limit} more")
    return "\n".join(out)


def build_descriptor(choice: dict) -> ElementDescriptor | None:
    """Turn the model's targeting choice into a descriptor with sensible fallbacks.

    The fallbacks are added by us, not by the model: they are mechanical
    (an anonymous field is also the nth field of its role) and the model's
    attention is better spent on the primary choice and its rationale.
    """
    scope = Scope(frame_path=[choice["frame"]] if choice.get("frame") else [])
    rationale = choice.get("rationale", "")
    kind = choice.get("targeting")

    match kind:
        case "accessible_name":
            if not choice.get("name"):
                return None
            return ElementDescriptor(
                primary=AccessibleNameTier(role=choice.get("role") or "button", name=choice["name"]),
                fallbacks=[OrdinalTier(role=choice.get("role") or "button", index=0)],
                scope=scope, rationale=rationale)
        case "label_proximity":
            if not choice.get("label_text"):
                return None
            return ElementDescriptor(
                primary=LabelProximityTier(label_text=choice["label_text"],
                                           control_role=choice.get("role") or "textbox"),
                fallbacks=[OrdinalTier(role=choice.get("role") or "textbox", index=0)],
                scope=scope, rationale=rationale)
        case "table_cell":
            return ElementDescriptor(
                primary=TableCellTier(row_label=choice.get("row_label"),
                                      column_header=choice.get("column_header")),
                scope=scope, rationale=rationale)
        case "ordinal":
            return ElementDescriptor(
                primary=OrdinalTier(role=choice.get("role") or "button",
                                    index=int(choice.get("index") or 0)),
                scope=scope, rationale=rationale)
    return None


class DiscoveryAgent:
    def __init__(
        self,
        surface: Surface,
        policy: Policy,
        llm: BedrockClient,
        *,
        recorder: EvidenceRecorder | None = None,
        max_steps: int = 18,
        timeout_s: float = 240.0,
    ) -> None:
        self.surface = surface
        self.policy = policy
        self.llm = llm
        self.recorder = recorder
        self.max_steps = max_steps
        self.timeout_s = timeout_s

    def run(self, goal: str, base_url: str, *, tenant_id: str = "base") -> DiscoveryRun:
        started = time.monotonic()
        run = DiscoveryRun(run_id=uuid.uuid4().hex[:12], goal=goal, base_url=base_url,
                           tenant_id=tenant_id, model=self.llm.describe())
        self._log("discovery.start", goal=goal, base_url=base_url, model=run.model)

        self.surface.act(Action(kind=ActionKind.NAVIGATE, value=base_url))
        messages: list[dict] = [{
            "role": "user",
            "content": f"GOAL: {goal}\n\nCurrent screen:\n{render_observation(self.surface.observe())}",
        }]

        stagnant = 0
        last_signature = ""

        for step_no in range(1, self.max_steps + 1):
            if time.monotonic() - started > self.timeout_s:
                run.status, run.summary = "stuck", "wall-clock timeout during discovery"
                break
            try:
                choice = self.llm.choose(system=SYSTEM, messages=messages, tool=COMBINED_TOOL)
            except LLMUnavailable as exc:
                run.status, run.summary = "failed", f"model unavailable: {exc}"
                break

            if fin := choice.get("finish"):
                run.status = fin.get("status", "done")
                run.summary = fin.get("summary", "")
                run.extracted = {k: str(v) for k, v in (fin.get("extracted") or {}).items()}
                self._log("discovery.finish", status=run.status, summary=run.summary,
                          extracted=run.extracted)
                break

            act_choice = choice.get("action")
            if not act_choice:
                messages.append({"role": "assistant", "content": json.dumps(choice)})
                messages.append({"role": "user", "content": "Choose either 'action' or 'finish'."})
                continue

            outcome = self._apply(act_choice, run, step_no)
            messages.append({"role": "assistant", "content": json.dumps({"action": act_choice})})
            messages.append({"role": "user", "content": outcome})

            signature = self.surface.observe().location + str(len(run.steps))
            stagnant = stagnant + 1 if signature == last_signature else 0
            last_signature = signature
            if stagnant >= 3:
                run.status, run.summary = "stuck", "no progress across three consecutive actions"
                break
        else:
            run.status = run.status if run.status != "running" else "stuck"
            run.summary = run.summary or f"step budget of {self.max_steps} exhausted"

        if run.status == "running":
            run.status = "stuck"
        run.duration_ms = int((time.monotonic() - started) * 1000)
        self._log("discovery.end", status=run.status, steps=len(run.steps),
                  tokens_in=self.llm.input_tokens, tokens_out=self.llm.output_tokens)
        return run

    # ------------------------------------------------------------- internals

    def _apply(self, choice: dict, run: DiscoveryRun, step_no: int) -> str:
        """Execute one model-chosen action and describe the result back to it."""
        kind = ActionKind(choice["kind"])
        descriptor = build_descriptor(choice)
        if descriptor is None and kind not in {ActionKind.NAVIGATE, ActionKind.PRESS_KEY}:
            return "That action needs a target. Supply targeting plus the matching fields."

        value = choice.get("value")
        if kind is ActionKind.NAVIGATE and value and not value.startswith("http"):
            value = run.base_url.rstrip("/") + "/" + value.lstrip("/")

        action = Action(kind=kind, target=descriptor, value=value, note=choice.get("intent"))
        obs_before = self.surface.observe()

        risk = RiskClass.CONFIRM if choice.get("mutates_state") else RiskClass.SAFE
        decision = self.policy.check(action, risk=risk, current_url=obs_before.location)
        if decision.blocked:
            self._log("discovery.blocked", step=step_no, action=action.describe(),
                      reason=decision.reason)
            run.steps.append(DiscoveryStep(
                intent=choice.get("intent", ""), action=action, rationale=choice.get("rationale", ""),
                mutates_state=bool(choice.get("mutates_state")), observation_before=obs_before,
                ok=False, note=f"blocked by policy: {decision.reason}"))
            return (f"REFUSED by safety policy: {decision.reason}\n"
                    f"Choose a different action that stays within policy.\n\n"
                    f"Current screen:\n{render_observation(obs_before)}")

        try:
            result = self.surface.act(action)
            ok, err = result.ok, result.error
            tier = result.resolution.tier_kind if result.resolution else None
            extracted = result.extracted
        except Exception as exc:
            ok, err, tier, extracted = False, f"{type(exc).__name__}: {exc}", None, None

        obs_after = self.surface.observe()
        run.steps.append(DiscoveryStep(
            intent=choice.get("intent", ""), action=action, rationale=choice.get("rationale", ""),
            mutates_state=bool(choice.get("mutates_state")), observation_before=obs_before,
            observation_after=obs_after, tier_used=tier, ok=ok, note=err or ""))
        self._log("discovery.step", step=step_no, intent=choice.get("intent"),
                  action=action.describe(), targeting=choice.get("targeting"),
                  rationale=choice.get("rationale"), ok=ok, tier=tier,
                  extracted=extracted, error=err)
        if self.recorder:
            self.recorder.screenshot(self.surface, f"step{step_no:02d}")

        head = f"Action {'succeeded' if ok else 'FAILED: ' + (err or 'unknown')}."
        if extracted:
            head += f'\nValue read: "{extracted}"'
        return f"{head}\n\nCurrent screen:\n{render_observation(obs_after)}"

    def _log(self, event: str, **fields) -> None:
        if self.recorder:
            self.recorder.event(event, **fields)

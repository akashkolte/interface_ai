"""Turn a successful discovery run into a capability artifact.

Split deliberately in two, because the two halves have different failure modes:

  **Mechanical** -- the ordered steps, the descriptors the model already chose,
  which tier resolved, the rationales. These are facts about what happened. We
  assemble them in code, where they cannot be hallucinated.

  **Judgment** -- which literal in the transcript was really a *parameter*, what
  the outputs should be called and typed, what proves the goal was reached, and
  which screens are legitimate business answers rather than errors. These are
  interpretations, and a second model pass is the right tool.

Keeping compilation out of the action loop is also what satisfies the brief's
requirement that the artifact be "decoupled from the raw model transcript": the
transcript is evidence, the artifact is a contract, and they are produced by
different passes with different inputs.
"""

from __future__ import annotations

import json
import re

from src.agent.discovery import DiscoveryRun
from src.agent.llm import BedrockClient
from src.artifact.schema import (
    CapabilityArtifact,
    Checkpoint,
    OutcomeRule,
    OutputBinding,
    OutputSpec,
    ParamSpec,
    ParamType,
    Provenance,
    RiskClass,
    Step,
    TargetSpec,
    TextPresent,
)
from src.surface.base import Action, ActionKind
from src.surface.descriptors import ElementDescriptor, OrdinalTier, Scope, TableCellTier

COMPILE_SYSTEM = """You convert a completed UI automation transcript into a reusable capability contract.

The transcript shows what an operator did once, with concrete values. Your job is to generalise it so it can be re-run for DIFFERENT inputs, without a model in the loop.

Be precise about three things:

1. PARAMETERS. Find literal values that were specific to this one run (an account number, a member id, an amount) and turn them into named typed parameters. Values that are part of the flow itself (a menu name, a button label) are NOT parameters.

2. OUTPUTS. Name and type whatever the goal asked to be read back.

3. CHECKPOINTS AND OUTCOMES. A checkpoint is text that proves the run reached the right screen -- prefer stable screen titles or headings over data values. A business outcome is a screen the application shows that is a LEGITIMATE ANSWER rather than a malfunction: "no record found", "account closed", "validation rejected". These must never be reported as failures.

   CRITICAL: `detect_text` must be copied EXACTLY from text that appears in the transcript's "screen after" lines, character for character. Do not paraphrase it and do not invent plausible wording -- the replay engine matches this string literally, so an approximation silently never fires. If you did not actually observe a screen for an outcome, do not list that outcome.

4. TARGETING MUST NOT DEPEND ON RUN-SPECIFIC DATA. This is the most common way a recorded flow fails on its second use. If a step clicked something identified by a value the application RETURNED -- a member's name, an account nickname, a date -- that target is useless for any other input. Report it in `data_dependent_targets`: give the step index and the input parameter whose value identifies that row instead. The step will be re-targeted to address the row by the parameter the caller supplies, not by data only this run saw.

Use snake_case for the capability id, dotted, e.g. member.read_savings_balance."""

COMPILE_TOOL = {
    "name": "compile_capability",
    "description": "Emit the generalised capability contract for this transcript.",
    "input_schema": {
        "type": "object",
        "properties": {
            "capability_id": {"type": "string", "description": "Dotted snake_case, e.g. member.read_savings_balance"},
            "name": {"type": "string"},
            "description": {"type": "string", "description": "What a calling agent reads to decide to invoke this."},
            "inputs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string", "enum": ["string", "integer", "number", "boolean", "money"]},
                        "description": {"type": "string"},
                        "pattern": {"type": "string", "description": "Optional regex the value must match."},
                        "example_literal": {"type": "string", "description": "The literal value used in this run, so it can be replaced by a placeholder."},
                        "sensitive": {"type": "boolean"},
                    },
                    "required": ["name", "type", "example_literal"],
                },
            },
            "outputs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string", "enum": ["string", "integer", "number", "boolean", "money"]},
                        "description": {"type": "string"},
                        "from_step_index": {"type": "integer", "description": "0-based index of the transcript step that read it."},
                        "read_row_label": {"type": "string", "description": "If the value was NOT produced by an explicit read action, give the row label in the record grid where it appears, so a read step can be added."},
                        "read_column_header": {"type": "string", "description": "Column header for the read, if the grid has one."},
                    },
                    "required": ["name", "type", "from_step_index"],
                },
            },
            "step_checkpoints": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "step_index": {"type": "integer"},
                        "text": {"type": "string", "description": "Text that must be on screen after this step."},
                        "expected": {"type": "string", "description": "Plain-language statement of what should be true."},
                    },
                    "required": ["step_index", "text", "expected"],
                },
            },
            "data_dependent_targets": {
                "type": "array",
                "description": "Steps whose target depends on data returned by this run rather than on an input. These must be re-targeted.",
                "items": {
                    "type": "object",
                    "properties": {
                        "step_index": {"type": "integer"},
                        "row_label_param": {"type": "string", "description": "Name of the input parameter whose value identifies the row, e.g. member_id."},
                        "column_header": {"type": "string", "description": "Column header of the cell to act on, e.g. Name."},
                    },
                    "required": ["step_index", "row_label_param"],
                },
            },
            "success_text": {"type": "array", "items": {"type": "string"}, "description": "Text proving the goal was reached."},
            "success_expected": {"type": "string"},
            "business_outcomes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "snake_case, e.g. member_not_found"},
                        "detect_text": {"type": "string", "description": "Exact on-screen text identifying this outcome."},
                        "message": {"type": "string"},
                    },
                    "required": ["code", "detect_text", "message"],
                },
            },
        },
        "required": ["capability_id", "name", "description", "success_text", "success_expected"],
    },
}


def _transcript(run: DiscoveryRun) -> str:
    lines = [f"GOAL: {run.goal}", f"BASE URL: {run.base_url}", ""]
    for i, s in enumerate(run.steps):
        if not s.ok:
            continue
        lines.append(f"STEP {i}: {s.intent}")
        lines.append(f"  action: {s.action.describe()}")
        lines.append(f"  targeting rationale: {s.rationale}")
        if s.observation_after:
            screen = " | ".join(
                l.strip() for l in s.observation_after.text.splitlines() if l.strip()
            )[:400]
            lines.append(f"  screen after: {screen}")
        lines.append("")
    if run.extracted:
        lines.append(f"VALUES THE RUN REPORTED: {json.dumps(run.extracted)}")
    return "\n".join(lines)


def compile_artifact(run: DiscoveryRun, llm: BedrockClient) -> CapabilityArtifact:
    """Second pass: generalise the transcript into a contract."""
    spec = llm.choose(
        system=COMPILE_SYSTEM,
        messages=[{"role": "user", "content": _transcript(run)}],
        tool=COMPILE_TOOL,
        max_tokens=3000,
    )

    inputs: list[ParamSpec] = []
    literal_to_param: dict[str, str] = {}
    for raw in spec.get("inputs") or []:
        inputs.append(ParamSpec(
            name=raw["name"], type=ParamType(raw.get("type", "string")),
            description=raw.get("description", ""), pattern=raw.get("pattern") or None,
            example=raw.get("example_literal"), sensitive=bool(raw.get("sensitive")),
        ))
        if lit := raw.get("example_literal"):
            literal_to_param[lit] = raw["name"]

    def parameterize(text: str | None) -> str | None:
        """Replace this run's literals with {placeholders}."""
        if not text:
            return text
        for literal, param in literal_to_param.items():
            if literal and literal in text:
                text = text.replace(literal, "{" + param + "}")
        return text

    checkpoints = {c["step_index"]: c for c in (spec.get("step_checkpoints") or [])}
    data_dependent = {d["step_index"]: d for d in (spec.get("data_dependent_targets") or [])}
    outputs_by_step: dict[int, list[dict]] = {}
    for o in spec.get("outputs") or []:
        outputs_by_step.setdefault(int(o["from_step_index"]), []).append(o)

    steps: list[Step] = []
    output_specs: list[OutputSpec] = []
    deferred_outputs: list[tuple[dict, str, Scope]] = []
    for i, ds in enumerate(run.steps):
        if not ds.ok:
            continue
        step_id = re.sub(r"[^a-z0-9]+", "_", (ds.intent or f"step_{i}").lower()).strip("_")[:40] \
            or f"step_{i}"
        while any(s.id == step_id for s in steps):
            step_id += "_x"

        action = ds.action.model_copy(deep=True)
        action.value = parameterize(action.value)
        if action.target is not None:
            for tier in [action.target.primary, *action.target.fallbacks]:
                for field in ("name", "label_text", "row_label", "column_header"):
                    if (v := getattr(tier, field, None)) and isinstance(v, str):
                        setattr(tier, field, parameterize(v))

        # Re-target steps the model flagged as depending on data this run happened
        # to see. Addressing the row by the caller's own parameter is the only
        # form that generalises.
        if (dd := data_dependent.get(i)) is not None and action.target is not None:
            action.target = ElementDescriptor(
                primary=TableCellTier(
                    row_label="{" + dd["row_label_param"] + "}",
                    column_header=dd.get("column_header") or None),
                fallbacks=[OrdinalTier(role="link", index=0)],
                scope=action.target.scope,
                rationale=("Addressed by the member id the caller supplied rather than by the "
                           "name the search returned. The returned value differs on every "
                           "invocation, so targeting it would make the capability single-use."))

        cp = None
        if (c := checkpoints.get(i)) is not None:
            cp = Checkpoint(rules=[TextPresent(text=c["text"])], expected=c["expected"])

        extracts = []
        for o in outputs_by_step.get(i, []):
            if ds.action.kind is not ActionKind.READ:
                # The model attributed a value to a step that did not read it --
                # it saw the value on screen. Replay cannot do that, so the read
                # is synthesized below instead of silently losing the output.
                deferred_outputs.append((o, step_id, action.target.scope if action.target else Scope()))
                continue
            extracts.append(OutputBinding(
                output=o["name"],
                transform="money" if o.get("type") == "money" else "strip"))
            output_specs.append(OutputSpec(
                name=o["name"], type=ParamType(o.get("type", "string")),
                description=o.get("description", ""), from_step=step_id))

        steps.append(Step(
            id=step_id, intent=ds.intent or step_id, action=action,
            param_bindings=[p for lit, p in literal_to_param.items()
                            if lit and ds.action.value and lit in ds.action.value],
            extracts=extracts, checkpoint=cp,
            risk=RiskClass.CONFIRM if ds.mutates_state else RiskClass.SAFE,
        ))

    # Synthesize the reads the run implied but never performed.
    for o, after_step, scope in deferred_outputs:
        row = o.get("read_row_label")
        if not row:
            # No way to address it deterministically; better to drop the output
            # than to ship a contract that promises a value replay cannot produce.
            continue
        read_id = f"read_{re.sub(r'[^a-z0-9]+', '_', o['name'].lower()).strip('_')}"[:40]
        while any(s.id == read_id for s in steps):
            read_id += "_x"
        steps.append(Step(
            id=read_id,
            intent=f"Read {o.get('description') or o['name']}",
            action=Action(kind=ActionKind.READ, target=ElementDescriptor(
                primary=TableCellTier(row_label=parameterize(row),
                                      column_header=o.get("read_column_header") or None),
                scope=scope,
                rationale=("Row-label addressing in the record grid: the detail screens have no "
                           "ids or test hooks, so the label beside the value is the only stable "
                           "key. Synthesized during compilation because the discovery run read "
                           "this value by sight rather than with an explicit read action."))),
            extracts=[OutputBinding(output=o["name"],
                                    transform="money" if o.get("type") == "money" else "strip")],
            risk=RiskClass.SAFE,
        ))
        output_specs.append(OutputSpec(
            name=o["name"], type=ParamType(o.get("type", "string")),
            description=o.get("description", ""), from_step=read_id))

    if not steps:
        raise ValueError("discovery run produced no successful steps to compile")

    success = Checkpoint(
        rules=[TextPresent(text=t) for t in (spec.get("success_text") or ["."])],
        expected=spec.get("success_expected", "the goal state was reached"),
    )
    outcomes = [
        OutcomeRule(
            code=o["code"],
            detect=Checkpoint(rules=[TextPresent(text=o["detect_text"])],
                              expected=f"screen shows {o['detect_text']!r}"),
            message=o["message"],
        )
        for o in (spec.get("business_outcomes") or [])
    ]

    highest = RiskClass.CONFIRM if any(s.risk is RiskClass.CONFIRM for s in steps) else RiskClass.SAFE

    # Guard: any remaining target literal that equals a value this run read back
    # is by definition data the caller will not have next time.
    read_values = {v.strip() for v in run.extracted.values() if v and len(v.strip()) > 2}
    suspect: list[str] = []
    for st in steps:
        if st.action.target is None:
            continue
        for tier in [st.action.target.primary, *st.action.target.fallbacks]:
            for f in ("name", "label_text", "row_label", "column_header"):
                v = getattr(tier, f, None)
                if isinstance(v, str) and v.strip() in read_values:
                    suspect.append(f"step {st.id!r} targets {v!r}, which this run read back")

    artifact_notes = run.summary
    if suspect:
        artifact_notes += " | WARNING, data-dependent targets: " + "; ".join(suspect)

    return CapabilityArtifact(
        capability_id=spec["capability_id"],
        name=spec["name"],
        description=spec["description"],
        goal=run.goal,
        target=TargetSpec(app_id="coreservicing", entry_point="/",
                          surface_kind="web.playwright.chromium"),
        inputs=inputs, outputs=output_specs, steps=steps, success=success,
        business_outcomes=outcomes, risk_profile=highest,
        provenance=Provenance(
            discovery_run_id=run.run_id, model=run.model,
            recorded_against_tenant=run.tenant_id,
            recorded_against_base_url=run.base_url,
            steps_explored=len(run.steps),
            notes=artifact_notes,
        ),
    )

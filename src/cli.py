"""Command line entry points.

    python -m src.cli discover --goal "..." --target http://localhost:5010
    python -m src.cli replay   --capability member.read_savings_balance --params '{"memberId":"12345"}'
    python -m src.cli catalog
    python -m src.cli operator

Exit codes follow the result contract rather than "did the process throw":
0 for success AND for a business outcome, 1 otherwise. A calling agent that
shells out should be able to trust that a non-zero exit means something is
broken, not that a member happened not to exist.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.agent.compile import compile_artifact
from src.agent.discovery import DiscoveryAgent
from src.agent.llm import BedrockClient, LLMUnavailable
from src.artifact.store import DEFAULT_DIR, list_capabilities, load, load_latest, save
from src.errors.taxonomy import ReplayStatus
from src.escalation.control import ControlState, InterventionStore, SessionControl
from src.escalation.escalator import Escalator
from src.evidence.recorder import EvidenceRecorder
from src.replay.engine import ReplayEngine
from src.safety.policy import Policy
from src.surface.base import Action, ActionKind
from src.surface.web_playwright import WebSurface

TENANT_URLS = {"base": "http://localhost:5010", "creditunion_b": "http://localhost:5011"}


def _policy(args, extra_origins: list[str] | None = None) -> Policy:
    origins = list({*(extra_origins or []), *TENANT_URLS.values()})
    return Policy.for_local_targets(*origins, allow_risky=getattr(args, "allow_risky", False))


# ----------------------------------------------------------------- discover


def cmd_discover(args) -> int:
    llm = BedrockClient(model=args.model) if args.model else BedrockClient()
    surface = WebSurface(headless=not args.headed)
    import uuid as _uuid
    recorder = EvidenceRecorder(run_id=_uuid.uuid4().hex[:12], kind="discovery")
    try:
        agent = DiscoveryAgent(surface, _policy(args, [args.target]), llm,
                               recorder=recorder, max_steps=args.max_steps)
        print(f"discovering against {args.target} with {llm.describe()}\ngoal: {args.goal}\n")
        run = agent.run(args.goal, args.target, tenant_id=args.tenant)

        print(f"status: {run.status}  ({len(run.steps)} steps, {run.duration_ms}ms)")
        print(f"summary: {run.summary}")
        if run.extracted:
            print(f"read: {run.extracted}")
        recorder.write_json("discovery-run.json", {
            "run_id": run.run_id, "goal": run.goal, "status": run.status,
            "summary": run.summary, "extracted": run.extracted, "model": run.model,
            "steps": [{"intent": s.intent, "action": s.action.describe(),
                       "rationale": s.rationale, "tier": s.tier_used,
                       "ok": s.ok, "note": s.note} for s in run.steps],
        })

        if run.status != "done":
            print("\ndiscovery did not complete; no artifact emitted.", file=sys.stderr)
            return 1

        print("\ncompiling artifact from transcript...")
        artifact = compile_artifact(run, llm)
        path = save(artifact, bump=True)
        recorder.write_json("artifact.json", artifact)
        print(f"artifact: {path}")
        print(f"  capability: {artifact.capability_id} v{artifact.version}")
        print(f"  inputs:     {[p.name for p in artifact.inputs]}")
        print(f"  outputs:    {[o.name for o in artifact.outputs]}")
        print(f"  steps:      {len(artifact.steps)}")
        print(f"  outcomes:   {[o.code for o in artifact.business_outcomes]}")
        print(f"evidence: {recorder.dir}")
        print(f"tokens: in={llm.input_tokens} out={llm.output_tokens}")
        return 0
    except LLMUnavailable as exc:
        print(f"model unavailable: {exc}", file=sys.stderr)
        return 1
    finally:
        surface.close()


# ------------------------------------------------------------------- replay


def _announce_intervention(request) -> None:
    """Tell the operator what to do, at the moment the run goes quiet.

    Without this the run blocks silently for up to `--wait-seconds` and the
    intervention id is only printed once it is over -- by which time it is no
    use to anybody. Printed to stderr so piping the run's result stays clean.
    """
    lines = [
        "",
        "=" * 72,
        f"PAUSED -- waiting for a human.   intervention {request.id}",
        "=" * 72,
        f"  stopped at: {request.step_id} ({request.step_intent})",
        f"  why:        {request.reason[:160]}",
        f"  params:     {request.params}",
    ]
    if request.screenshot:
        lines.append(f"  screenshot: {request.screenshot}")
    lines += [
        "",
        "  In another terminal:",
        f"    python -m src.cli operator --take {request.id}",
        "",
        "  Then fix the screen in the browser window this run left open, and:",
        f"    python -m src.cli operator --resume {request.id}",
        "=" * 72,
        "",
    ]
    print("\n".join(lines), file=sys.stderr, flush=True)


def cmd_replay(args) -> int:
    artifact = load(args.artifact) if args.artifact else load_latest(args.capability)
    params = json.loads(args.params) if args.params else {}
    base = args.base_url or TENANT_URLS.get(args.tenant or "base")

    recorder = EvidenceRecorder(run_id=args.label or "run", kind="replay")
    escalator = None
    if args.escalate:
        escalator = Escalator(SessionControl(), InterventionStore(),
                              wait_seconds=args.wait_seconds,
                              notify=_announce_intervention)

    surface = WebSurface(headless=not args.headed)
    try:
        if args.fault:
            # Faults are a property of the target app, not of the capability.
            # Injected here so an evidence run is reproducible without editing
            # the artifact.
            surface.act(Action(kind=ActionKind.NAVIGATE, value=f"{base}/search?fault={args.fault}"))

        engine = ReplayEngine(surface, _policy(args, [base]),
                              recorder=recorder, escalator=escalator)
        result = engine.run(artifact, params, tenant_id=args.tenant, base_url=base)

        recorder.write_json("result.json", result)
        print(result.summary())
        print()
        for st in result.steps:
            tier = f"{st.tier_used}" + (" (FALLBACK)" if st.used_fallback else "")
            print(f"  {st.step_id:24} {st.status:18} {tier:24} "
                  f"checkpoint={st.checkpoint_passed} {st.extracted or ''}")
        if result.recoveries:
            print("\n  recoveries:")
            for r in result.recoveries:
                print(f"    {r.step_id}: {r.rule_name} (attempt {r.attempt})")
        if result.degraded:
            print("\n  NOTE: run succeeded but degraded (fallback tier or recovery used) "
                  "-- a drift signal worth reviewing.")
        if result.status is ReplayStatus.ESCALATED and result.failure is not None:
            if result.failure.detail:
                print(f"\n  WHY THE HANDOFF DID NOT FINISH THE RUN:\n    {result.failure.detail}")
            print(f"\n  the step expected: {result.failure.expected}")
            print(f"  what was on screen: {str(result.failure.observed)[:200]}")

        print(f"\nstatus={result.status}  exit={result.exit_code}  evidence={recorder.dir}")
        return result.exit_code
    finally:
        surface.close()


# ------------------------------------------------------------------ catalog


def cmd_catalog(args) -> int:
    """What an agent-facing capability registry would expose."""
    caps = list_capabilities(Path(args.dir))
    if not caps:
        print(f"no capabilities in {args.dir}")
        return 0
    for c in caps:
        print(f"\n{c.capability_id}  v{c.version}   [{c.risk_profile}]")
        print(f"  {c.description}")
        if c.inputs:
            print("  inputs:")
            for p in c.inputs:
                req = "required" if p.required else "optional"
                print(f"    {p.name}: {p.type} ({req}) {p.description}")
        if c.outputs:
            print("  outputs:")
            for o in c.outputs:
                print(f"    {o.name}: {o.type} - {o.description}")
        if c.business_outcomes:
            print(f"  business outcomes: {[o.code for o in c.business_outcomes]}")
        if c.tenant_overrides:
            print(f"  tenant overrides:  {list(c.tenant_overrides)}")
    return 0


# ----------------------------------------------------------------- operator


def cmd_operator(args) -> int:
    """The minimal operator surface.

    Deliberately a CLI rather than a web console (see REPORT.md 'Cuts') -- but a
    real one: `--take` and `--resume` drive the same control-transfer state
    machine the run enforces, from a different process, on the same live session.
    """
    store = InterventionStore()

    if args.take or args.resume:
        request_id = args.take or args.resume
        request = store.get(request_id)
        if request is None:
            print(f"no such intervention: {request_id}", file=sys.stderr)
            return 2

        if args.take:
            if request.state is not ControlState.INTERVENTION_REQUESTED:
                print(f"cannot take control of a request in state {request.state}", file=sys.stderr)
                return 2
            request.state = ControlState.HUMAN
            request.operator_note = args.operator
            store.put(request)
            print(f"control taken by {args.operator!r} for {request_id}.")
            print("The run is paused and will not act until you hand control back.")
            print("Drive the browser window the run left open, then:")
            print(f"  python -m src.cli operator --resume {request_id}")
            return 0

        if request.state not in {ControlState.HUMAN, ControlState.INTERVENTION_REQUESTED}:
            print(f"cannot hand back from state {request.state}", file=sys.stderr)
            return 2
        request.state = ControlState.RESUMING
        if args.note:
            request.operator_note = args.note
        store.put(request)
        print(f"control handed back for {request_id}.")
        print("The run re-checks its checkpoint rather than assuming you did what was asked.")
        return 0

    pending = store.pending()
    if not pending:
        print("no pending interventions")
        return 0
    for r in pending:
        print("\n" + "=" * 72)
        print(r.brief())
        if r.screenshot:
            print(f"  screenshot: {r.screenshot}")
        print(f"  state:      {r.state}")
        print("  take it:    python -m src.cli operator --take " + r.id)
        print("  hand back:  python -m src.cli operator --resume " + r.id)
    print("\n" + "=" * 72)
    print("\nTake control, fix the screen in the browser window the run left open,")
    print("then hand control back. The run resumes on the same session.")
    return 0


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="LLM-driven discovery run; emits an artifact")
    d.add_argument("--goal", required=True)
    d.add_argument("--target", default=TENANT_URLS["base"])
    d.add_argument("--tenant", default="base")
    d.add_argument("--model", default=None)
    d.add_argument("--max-steps", type=int, default=18)
    d.add_argument("--headed", action="store_true")
    d.add_argument("--allow-risky", action="store_true")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay; no model in the loop")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--artifact", help="path to an artifact JSON file")
    g.add_argument("--capability", help="capability id; uses the latest version")
    r.add_argument("--params", default="{}")
    r.add_argument("--tenant", default=None)
    r.add_argument("--base-url", default=None)
    r.add_argument("--fault", default=None, help="inject a target-app fault for evidence runs")
    r.add_argument("--label", default="run", help="names the evidence directory")
    r.add_argument("--escalate", action="store_true", help="route stuck states to a human")
    r.add_argument("--wait-seconds", type=float, default=0.0)
    r.add_argument("--allow-risky", action="store_true")
    r.add_argument("--headed", action="store_true")
    r.set_defaults(func=cmd_replay)

    c = sub.add_parser("catalog", help="list saved capabilities")
    c.add_argument("--dir", default=str(DEFAULT_DIR))
    c.set_defaults(func=cmd_catalog)

    o = sub.add_parser("operator", help="show pending interventions; take or hand back control")
    o.add_argument("--take", metavar="ID", help="take control of the live session for this request")
    o.add_argument("--resume", metavar="ID", help="hand control back so the run continues")
    o.add_argument("--operator", default="operator", help="who is taking control")
    o.add_argument("--note", default="", help="what you did, recorded on the request")
    o.set_defaults(func=cmd_operator)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

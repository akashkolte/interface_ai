# Design Report

## 1. Architecture

Five layers, one direction of dependency. Nothing above the surface layer knows
what a browser is.

```
CLI  ──────────────────────────────────────────────────────────
 │
 ├── agent/      LLM discovery loop  ──┐   the model sits here, once
 │   compile.py  transcript → artifact │
 │                                      ▼
 ├── artifact/   CapabilityArtifact — the contract
 │                                      │
 ├── replay/     deterministic executor ◄┘   no model, ever
 │
 ├── safety/     ONE allowlist + risk choke point, used by BOTH loops
 ├── escalation/ control token, intervention store
 ├── evidence/   structured log + screenshots, redacted on write
 │
 └── surface/    THE SEAM — perceive/act. Only web_playwright.py knows Playwright.
```

**Key decisions.**

*Perception is the accessibility tree, not the DOM.* Read via CDP
`Accessibility.getFullAXTree`. This costs a little fidelity and buys the central
claim of the design: the `Observation` shape — role, accessible name, value,
enabled, frame path — is exactly what Windows UIA or macOS AX returns for a
desktop application. The artifact is therefore not web-shaped.

*The action vocabulary is a closed set of seven verbs.* `navigate`, `click`,
`type`, `select`, `read`, `wait_for`, `press_key`. Anything richer (drag-drop,
hover menus) would not survive a desktop or screenshot adapter, so it is
excluded until a real flow needs it.

*Discovery and replay are separate programs sharing one guardrail.* They have
different trust models — one has a model proposing actions, one does not — but
both dispatch through `Policy.check`. A guardrail with two entry points has a
bypass.

*Single process, files on disk.* The brief penalizes premature infrastructure. An
artifact is a reviewable document; keeping it in git next to the code that runs
it is a feature. The seams that would need to become services later —
artifact store, intervention store, LLM client — are each one class.

**Trade-off accepted:** the a11y tree is poorer than the DOM on a *modern* SPA
where semantic markup and test IDs exist. We optimised for the legacy case,
because that is the case the brief says has no other way in.

---

## 2. Artifact schema

An artifact is a **capability contract**, not a step list — closer to a function
signature with a body. Full schema in [`src/artifact/schema.py`](src/artifact/schema.py);
a real one in [`artifacts/`](artifacts/).

```
CapabilityArtifact
  schema_version, capability_id, version      versioned and reviewable
  name, description, goal                     what a calling agent reads
  target      { app_id, app_version_hint, entry_point, surface_kind }
  inputs      [ParamSpec]     typed, validated, pattern-checked, sensitive flag
  outputs     [OutputSpec]    typed, each bound to the step that reads it
  steps       [Step]          ordered
  success     Checkpoint      proves the goal state was reached
  business_outcomes [OutcomeRule]     ← see below
  risk_profile, provenance, tenant_overrides
```

**Targets are descriptors, not selectors.** A CSS path or XPath into legacy
`<table>` soup encodes incidental structure. Instead each target is an ordered
chain of tiers, each expressible against an accessibility tree:

| Tier | Meaning | Why it exists |
|---|---|---|
| `accessible_name` | role + accessible name | Most portable; works on web and desktop |
| `label_proximity` | "the field beside the text that names it" | Legacy forms have **no** `label for`, so inputs are anonymous — verified: the search field's accessible name is literally `""` |
| `table_cell` | value by row label / column header | How data is read out of record-detail grids |
| `ordinal` | nth control of a role | Brittle; recorded **explicitly** rather than guessed silently |
| `visual_bounds` | click a viewport-relative point | Escape hatch for surfaces with no queryable tree |

Resolution records **which tier matched**. A run that passes only on tier 3 is a
drift signal, surfaced as `degraded` on the result even though it succeeded.

**Three decisions worth defending:**

1. **The artifact declares its own business outcomes.** `business_outcomes` lets
   the capability say "if the screen says *No member found*, that is a
   legitimate result called `member_not_found`". Which answers are legitimate is
   capability-specific knowledge discovered once, so it belongs beside the steps
   that produce it — not in executor heuristics.

2. **Robustness rationale is stored per descriptor**, written by the discovery
   model at the moment the choice is made. Reconstructing "why is this the right
   handle?" later, from a selector, is close to impossible.

3. **Tenant differences are sparse patches, not forks.** `tenant_overrides` holds
   only what differs. Drift becomes a reviewable diff rather than a re-record.

**Compilation is a separate pass from the action loop**, which is what makes the
artifact genuinely decoupled from the transcript. Mechanical facts (steps,
descriptors, tiers) are assembled in code where they cannot be hallucinated;
judgment (which literals are parameters, output types, checkpoints, which
screens are business outcomes) comes from a second model call.

---

## 3. Determinism & error handling

Replay consults **no model**. Same artifact + same params → same steps, same
outputs. Pinned by `test_replay_is_deterministic`.

**How determinism is achieved:**

- **Tier chains with recorded outcomes.** Fallbacks are tried in order; the tier
  that matched is reported.
- **Conditions, never sleeps.** Resolution polls the whole tier chain against a
  deadline. This is also the handling for the "transient slowness" case — a slow
  screen costs time only when something is genuinely missing.
- **Checkpoints on every step, not just at the end**, each carrying `expected` in
  plain language, so a failure names the step, the expectation and the
  observation.
- **Typed input validation before anything is driven.** A bad parameter fails
  with zero actions taken.

**The error taxonomy** — three classes, structurally distinct in the result type:

| Class | Meaning | Result |
|---|---|---|
| **Business outcome** | A legitimate answer: "no such member", "account closed", validation rejection | `status=business_outcome`, `outcome_code`, **exit 0** |
| **Recoverable** | Known interstitial, transient slowness, session expiry | Bounded `RecoveryRule`, retried, recorded in `recoveries` |
| **Hard failure** | Unresolvable | `status=failure` + step id, expected, observed, screenshot, **exit 1** |

Two details carry most of the weight here:

**Business outcomes are checked *before* checkpoints and *before* error
handling.** Invert that order and every legitimate answer is reported as a failed
assertion. The target app makes this concrete: "no member found" returns **HTTP
200** — the distinction cannot be read off a status code, only off declared
content.

**Recoverable conditions get no status of their own.** Recovering is not an
outcome, it is something that happened on the way to one. They are recorded so a
reviewer can see a capability quietly degrading: one that recovers on every run
is one UI change away from failing.

**On UI drift** (secondary, as the brief notes): drift shows up as fallback-tier
usage before it shows up as breakage. `used_fallback` per step and `degraded` on
the result are the early warning; a capability replaying on tier 4 is a
re-record candidate.

**A finding worth stating plainly: the model-compiled artifact got this wrong.**
`evidence/replay-discovered-missing-member/` shows the compiled capability
reporting a missing member as a **hard failure** rather than a business outcome.
The cause is not the taxonomy — it is that the compiling model *paraphrased* the
detection text as `"No members found"` when the application actually renders
`"No member found"`. The literal never matched, so the outcome rule never fired.
The hand-curated artifact, with the exact string, classifies the same case
correctly (`evidence/replay-business-outcome/`, exit 0).

Two conclusions follow, and both shaped the design:

- **Exact-match detection is the right mechanism but an unforgiving one.** A
  near-miss fails silently and degrades to the error path — which is at least the
  *safe* direction to fail, but it is still wrong. Fuzzier matching would trade a
  silent miss for a silent false positive, which is worse: it would report a
  malfunction as a legitimate answer.
- **This is the argument for an approval gate.** A freshly discovered artifact is
  a draft. It replays, and it generalises — `replay-discovered-generalizes` shows
  the artifact recorded against member 67890 correctly returning member 12345's
  balance — but its *outcome rules* are the part a human should review before it
  runs unattended. That gate is listed under Cuts as the first thing I would
  build next, and this run is why it is first.

**Two real bugs found by running against the frameset**, both now regression
tests, both illustrative of what "deterministic" actually costs:

- **Detached frames.** After a cross-document navigation Playwright still exposes
  the *previous* document's children via `main_frame.child_frames`. Frame lookup
  returned a dead frame pointing at the old tenant's URL while the page was on
  the new one — every query silently returned zero. Filtered at the single
  frame-lookup point.
- **`Locator.count()` does not auto-wait.** One resolution pass could miss an
  element milliseconds late and wrongly demote to a fallback tier, or fail.
  Resolution is now deadline-driven.

---

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `Surface`: `observe() -> Observation`,
`act(Action)`, `resolve(descriptor)`. Everything above it — artifact, replay,
guardrails, escalation — is written against those types alone.

Adding a **desktop** surface means implementing that protocol over UIA/AX and
nothing else: roles, names, values and bounds all exist there, and all five
locator tiers remain meaningful (`label_proximity` is how a human reads a Win32
dialog too). A **screenshot-only** surface (Citrix, a remote framebuffer) is the
degenerate case: tiers 1–4 are unavailable, `visual_bounds` survives, and the
artifact records that it is running low-confidence rather than pretending
otherwise.

What does *not* cross the seam is the `_adapter_ref` on an observed element — a
live handle, deliberately untyped and never serialized. Artifacts contain only
descriptions, which is why they are portable at all.

**Multi-tenant reuse.** One base artifact plus per-tenant sparse patches. The
repo demonstrates this for real: `creditunion_b` runs the same product with
"Member ID" → "Account Holder #", "Search" → "Find Member", "Savings Balance" →
"Share Savings Balance", a reordered nav and an extra disclosure interstitial.
The base artifact replays successfully there with a **three-descriptor
override**, and fails cleanly with `target_unresolvable` without it.

**Detecting drift at scale** is the part that is designed but not built: with
hundreds of tenants, the signal is already in the result — fallback-tier usage
and recovery counts per tenant per version. Aggregating those would identify
which tenants have diverged and which vendor version introduced it, without
re-recording anything. The data model supports it; the aggregation does not
exist.

---

## 5. Escalation & handoff

**Detecting stuck:** descriptor resolution exhausted every tier; a checkpoint
failed with no matching recovery rule; a recovery budget was exhausted; a
`CONFIRM`-risk step was reached without authorization; no progress across three
consecutive discovery actions; or a wall-clock timeout.

**The control model is a token, not a flag.** A boolean is a convention any code
path can ignore. `SessionControl.guard(token)` is called at the single dispatch
point before every action, and taking control **invalidates the automation's
token** — so a step that races the operator fails loudly instead of fighting them
for the mouse.

```
AUTOMATION ──raise()──> INTERVENTION_REQUESTED ──take()──> HUMAN
     ^                                                       │
     └──────────── resume() ◄── RESUMING ◄──── hand_back() ──┘
```

**Taking over the live session.** The automation blocks — it does not poll and
retry in parallel, because two writers on one session is precisely the bug the
token prevents. The human continues in the *same* browser window, on the screen
that caused the problem, which is the only way the context that made it stuck is
still visible.

**Resuming.** On hand-back the engine does **not** assume the human did what was
asked. It re-observes and re-evaluates the step's own checkpoint. If the expected
state still isn't there, it escalates again — bounded at two escalations per
step. For a `CONFIRM`-risk step that the operator performed by hand, the engine
verifies the resulting state rather than re-running the mutation.

**What the human did** is captured at navigation granularity and written to the
intervention record. Deliberately not full input capture: keylogging an operator
inside a banking session is exactly the data this system exists to avoid
persisting.

**Unattended runs** don't hang. With no operator, the request is queued to the
intervention store and the run returns `ESCALATED` with exit 1.

---

**The signal has to cross a process boundary.** `SessionControl` is a threading
primitive — correct for the run loop, useless to a human in another terminal. So
the `InterventionStore` (a directory of JSON files) doubles as the transfer
channel: `src.cli operator --take` writes `human` into the record, `--resume`
writes `resuming`, and the blocked run polls for it and mirrors the transition
onto the real `SessionControl`.

The store is the **signal, never the authority**. Every transition is applied to
the in-process `SessionControl` in the process that actually drives the session,
which is where the token rotates and the single-writer invariant is enforced. A
stale or hand-edited record cannot grant anything; it can only ask. That
separation is what makes a file-backed queue safe here, and it is why swapping
the store for Redis or SQS changes one class and no logic.

On resume the engine **re-evaluates the stuck step's checkpoint** rather than
trusting that the operator did what was asked. A human who fixes the wrong screen
gets the run escalated again, not a false success.

## 6. Safety

**One choke point.** Every action from both loops passes `Policy.check`.
Discovery is not a trusted context: the model proposes, the policy disposes, and
a refusal is fed back as an observation so the model can choose differently
rather than the run aborting.

**Allowlist.** Explicit origins plus route globs, deny rules evaluated first,
deny-by-default. A different scheme or a different port is a different origin.

**Risk is per step, not per verb.** `click` is harmless on a search button and
irreversible on a wire-transfer confirm, so `SAFE` / `CONFIRM` / `BLOCKED` is
recorded at discovery time. Unattended replay refuses anything above `SAFE`
unless the caller passes `--allow-risky` — the conservative default, with the
escape hatch explicit. `BLOCKED` is refused even then.

A recorded risk class is a *claim made at discovery time*, and claims can be
wrong — so there is an independent check: routes matching irreversible-operation
patterns (`transfer`, `wire`, `withdraw`, `delete`, `approve`…) are blocked
regardless of the recorded class.

**Redaction happens on the write path**, in `EvidenceRecorder` and the
intervention store, not at each call site. Field-name heuristics plus value-shape
patterns (SSN, card, bearer token, API keys, email). Artifacts store parameter
*shapes and examples*, never captured values.

**Limits, stated plainly.** The redactor is heuristic — it will mask harmless
values and could miss an unusual format; it is a safety net, not a guarantee.
The irreversible-route patterns are English-language and would need per-vendor
tuning. Neither survives a determined adversary; both substantially reduce the
chance of an accident, which is the realistic threat here.

---

## 7. Cuts

Deliberately not built, with the seam left clean:

- **Operator console is CLI-only.** `src.cli operator` lists pending requests
  with full context, and `--take` / `--resume` drive the real control-transfer
  state machine from a separate process; fixing the screen means using the
  browser window the run left open. The brief explicitly permits a bare operator
  surface — the **mechanism** (token rotation, state machine, cross-process
  signalling, same-session takeover, resume checkpoint) is real and tested. A web
  console would replace the CLI's two writes to the same `InterventionStore`.
  What is *not* built is richer operator tooling: no co-browsing, no queue
  assignment, no audit UI.
- **No desktop adapter.** The `Surface` protocol is the whole point and is
  exercised by one implementation. A second (UIA/AX) would validate the seam;
  I'd build it next.
- **No drift aggregation across tenants.** The per-run signals exist
  (`used_fallback`, `degraded`, recovery counts); nothing rolls them up.
- **No confidence scoring or approval gating.** Artifacts are `draft` in effect;
  there is no `draft → approved` state machine gating unattended replay. The
  evidence shows exactly why this matters: a compiled artifact replayed and
  generalised correctly while carrying a wrong business-outcome string (§3).
- **No assisted fallback on replay failure.** A bounded, policy-checked single-step
  LLM recovery is the natural next feature; today a stuck replay escalates to a
  human instead. That ordering is deliberate — a human is the safe default.
- **No multi-run stability signal.** Replaying N times and reporting flakiness
  would be cheap and valuable; not done.
- **Artifact storage is files.** No registry, no concurrent-writer story.
- **Single LLM provider.** Bedrock only, isolated in one file.
- **Human action capture is navigation-level**, for the privacy reason above.

**What I'd do next, in order:** (1) the `draft → approved` gate, because
unattended replay of an unreviewed artifact is the most dangerous thing here;
(2) multi-run stability scoring to feed that gate; (3) a UIA desktop adapter to
prove the seam; (4) cross-tenant drift aggregation.

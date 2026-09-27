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

*Perception is the accessibility tree, not the DOM*, read via CDP
`Accessibility.getFullAXTree`. The `Observation` shape — role, accessible name,
value, enabled, frame path — is exactly what Windows UIA or macOS AX returns for
a desktop app, so the artifact is not web-shaped.

*The action vocabulary is a closed set of seven verbs*: `navigate`, `click`,
`type`, `select`, `read`, `wait_for`, `press_key`. Anything richer would not
survive a desktop or screenshot adapter.

*Discovery and replay are separate programs sharing one guardrail.* Different
trust models, but both dispatch through `Policy.check` — a guardrail with two
entry points has a bypass.

*Single process, files on disk.* An artifact is a reviewable document; keeping it
in git beside the code that runs it is a feature. The seams that would become
services later — artifact store, intervention store, LLM client — are one class
each.

**Trade-off accepted:** the a11y tree is poorer than the DOM on a modern SPA with
semantic markup and test IDs. We optimised for the legacy case, which is the one
the brief says has no other way in.

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

**Targets are descriptors, not selectors.** An XPath into legacy `<table>` soup
encodes incidental structure. Each target is instead an ordered chain of tiers,
all expressible against an accessibility tree:

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

1. **The artifact declares its own business outcomes.** It says "if the screen
   reads *No member found*, that is a legitimate result called
   `member_not_found`". Which answers are legitimate is capability-specific
   knowledge discovered once, so it belongs beside the steps that produce it, not
   in executor heuristics.

2. **Robustness rationale is stored per descriptor**, written by the discovery
   model as the choice is made. Reconstructing "why this handle?" later from a
   selector is close to impossible.

3. **Tenant differences are sparse patches, not forks.** Drift becomes a
   reviewable diff rather than a re-record.

**Compilation is a separate pass from the action loop**, which is what decouples
the artifact from the transcript. Mechanical facts (steps, descriptors, tiers)
are assembled in code where they cannot be hallucinated; judgment (which literals
are parameters, output types, checkpoints, business outcomes) comes from a second
model call.

---

## 3. Determinism & error handling

Replay consults **no model**. Same artifact + same params → same steps, same
outputs. Pinned by `test_replay_is_deterministic`.

**How:** tier chains resolved in order with the matched tier recorded;
conditions rather than sleeps (resolution polls the whole chain against a
deadline, which doubles as the handling for transient slowness); checkpoints on
every step carrying `expected` in plain language; typed input validation before
anything is driven, so a bad parameter fails with zero actions taken.

**The error taxonomy** — three classes, structurally distinct in the result type:

| Class | Meaning | Result |
|---|---|---|
| **Business outcome** | A legitimate answer: "no such member", "account closed", validation rejection | `status=business_outcome`, `outcome_code`, **exit 0** |
| **Recoverable** | Known interstitial, transient slowness, session expiry | Bounded `RecoveryRule`, retried, recorded in `recoveries` |
| **Hard failure** | Unresolvable | `status=failure` + step id, expected, observed, screenshot, **exit 1** |

Two details carry the weight. **Business outcomes are checked before checkpoints
and before error handling** — invert that and every legitimate answer becomes a
failed assertion. The target app makes it concrete: "no member found" returns
**HTTP 200**, so the distinction cannot be read off a status code, only off
declared content. And **recoverable conditions get no status of their own**:
recovering is not an outcome, it is something that happened on the way to one.
They are recorded so a reviewer can see a capability quietly degrading — one that
recovers every run is one UI change from failing.

**On UI drift** (secondary, as the brief notes): it shows up as fallback-tier
usage before it shows up as breakage. `used_fallback` per step and `degraded` on
the result are the early warning; a capability replaying on tier 4 is a re-record
candidate.

**The model-compiled artifact got this wrong, and the run is committed.**
`evidence/replay-discovered-missing-member/` shows the compiled capability
reporting a missing member as a hard failure rather than a business outcome. The
cause is not the taxonomy: the compiling model paraphrased the detection text as
`"No members found"` where the app renders `"No member found"`, so the literal
never matched and the rule never fired. The hand-curated artifact, with the exact
string, classifies the same case correctly (`evidence/replay-business-outcome/`,
exit 0).

The root cause generalises: **a discovery run only observes the happy path**, so
any outcome rule it declares for a branch it never visited is a guess. Two
conclusions followed:

- **Exact-match detection is right but unforgiving.** A near-miss fails silently
  into the error path — the safe direction, still wrong. Fuzzier matching would
  trade a silent miss for a silent false positive, which is worse: reporting a
  malfunction as a legitimate answer.
- **This is the argument for an approval gate.** A freshly discovered artifact is
  a draft. It replays and it generalises — `replay-discovered-generalizes` shows
  the artifact recorded on member 67890 correctly returning 12345's balance — but
  its *outcome rules* need human review before unattended use. That gate is first
  under Cuts, and this run is why.

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

**A nastier class: bugs in the evidence, not the behaviour.** The two above
announce themselves. The next two did not — the feature worked and the *record*
of it was silently wrong, which in a system whose output is an audit trail is the
worse failure. Both surfaced only by running the handoff by hand; no test caught
them, because the tests asserted on return values rather than on what a reviewer
would later read.

- **The evidence log accumulated across runs.** `EvidenceRecorder` opened
  `run.jsonl` in append mode, so re-running a `--label` appended to it and
  `evidence/replay-escalation/` held two days of interleaved runs. Every line was
  true; the file was a lie. A label now truncates on open.
- **`human_actions` was always empty.** Sync Playwright dispatches page events
  only while a call into it is in flight, and the wait loop blocked on a
  threading primitive and a file read — so `framenavigated` never fired and every
  handoff recorded that the human did nothing. The watcher now pumps the
  connection while waiting: an observation, not an action, so the human keeps
  control.

The lesson, and why they are named here rather than quietly fixed: **assert on
the artefact a human will read, not only on the return value.** Both regression
tests now open the record and check its contents. Here the evidence *is* the
product — a capability whose replay cannot be audited is not one a bank runs
unattended.

---

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `Surface`: `observe() -> Observation`,
`act(Action)`, `resolve(descriptor)`. Everything above it — artifact, replay,
guardrails, escalation — is written against those types alone.

A **desktop** surface means implementing that protocol over UIA/AX and nothing
else: roles, names, values and bounds all exist there, and all five tiers stay
meaningful (`label_proximity` is how a human reads a Win32 dialog too). A
**screenshot-only** surface (Citrix, a remote framebuffer) is the degenerate
case: tiers 1–4 are unavailable, `visual_bounds` survives, and the artifact
records that it is running low-confidence rather than pretending otherwise.

What does *not* cross the seam is `_adapter_ref` on an observed element — a live
handle, never serialized. Artifacts contain only descriptions, which is why they
are portable at all.

**Multi-tenant reuse.** One base artifact plus per-tenant sparse patches. The
repo demonstrates this for real: `creditunion_b` runs the same product with
"Member ID" → "Account Holder #", "Search" → "Find Member", "Savings Balance" →
"Share Savings Balance", a reordered nav and an extra disclosure interstitial.
The base artifact replays successfully there with a **three-descriptor
override**, and fails cleanly with `target_unresolvable` without it.

**Detecting drift at scale** is designed but not built. The signal is already in
every result — fallback-tier usage and recovery counts, per tenant per version.
Aggregating it would identify which tenants diverged and which vendor version
caused it, without re-recording anything. The data model supports it; the
aggregation does not exist.

---

## 5. Escalation & handoff

**Detecting stuck:** descriptor resolution exhausted every tier; a checkpoint
failed with no matching recovery rule; a recovery budget was exhausted; a
`CONFIRM`-risk step was reached without authorization; no progress across three
consecutive discovery actions; or a wall-clock timeout.

**The control model is a token, not a flag.** A boolean is a convention any code
path can ignore. `SessionControl.guard(token)` runs at the single dispatch point
before every action, and taking control **rotates the token** — so a step that
races the operator fails loudly instead of fighting them for the mouse.

```
AUTOMATION ──raise()──> INTERVENTION_REQUESTED ──take()──> HUMAN
     ^                                                       │
     └──────────── resume() ◄── RESUMING ◄──── hand_back() ──┘
```

**Taking over the live session.** The automation blocks rather than polling and
retrying in parallel — two writers on one session is the bug the token prevents.
The human continues in the *same* window, on the screen that caused the problem,
which is the only way the context that made it stuck is still visible.

**Resuming.** The engine does **not** assume the human did what was asked: it
re-observes and re-evaluates the step's own checkpoint, and escalates again if
the expected state still isn't there (bounded at two per step). For a
`CONFIRM`-risk step the operator performed by hand, it verifies the resulting
state rather than re-running the mutation. Proven end to end in
`evidence/replay-escalation/`: raised → taken by a human in another process →
navigation recorded → `checkpoint.rechecked_after_handoff passed=True` → success.
Recorded in `evidence/escalation-handoff.mp4`.

**What the human did** is captured at navigation granularity. Deliberately not
full input capture: keylogging an operator inside a banking session is exactly
the data this system exists to avoid persisting.

**Unattended runs** don't hang — the request is queued and the run returns
`ESCALATED`, exit 1.

**The signal crosses a process boundary.** `SessionControl` is a threading
primitive — useless to a human in another terminal — so the `InterventionStore`
doubles as the transfer channel: `operator --take` writes `human` into the
record, `--resume` writes `resuming`, and the blocked run mirrors the transition
onto the real `SessionControl`. The store is the **signal, never the authority**:
every transition is applied in the process that drives the session, where the
token rotates. A hand-edited record can ask for control; it cannot grant it.
Swapping the store for Redis or SQS changes one class and no logic.

## 6. Safety

**One choke point.** Every action from both loops passes `Policy.check`.
Discovery is not a trusted context: the model proposes, the policy disposes, and
a refusal returns as an observation so the model can choose differently rather
than the run aborting.

**Allowlist:** explicit origins plus route globs, deny rules first,
deny-by-default. A different scheme or port is a different origin.

**Risk is per step, not per verb.** `click` is harmless on a search button and
irreversible on a wire-transfer confirm, so `SAFE`/`CONFIRM`/`BLOCKED` is
recorded at discovery time. Unattended replay refuses anything above `SAFE`
without `--allow-risky`; `BLOCKED` is refused even then. But a recorded class is
a *claim made at discovery time*, so an independent check blocks routes matching
irreversible-operation patterns (`transfer`, `wire`, `withdraw`, `delete`,
`approve`…) regardless of what was recorded.

**Redaction is on the write path**, in `EvidenceRecorder` and the intervention
store rather than at each call site: field-name heuristics plus value-shape
patterns (SSN, card, bearer token, API keys, email). Artifacts store parameter
*shapes and examples*, never captured values.

**Limits.** The redactor is heuristic — it masks harmless values and could miss
an unusual format; a safety net, not a guarantee. The irreversible-route patterns
are English and need per-vendor tuning. Neither survives a determined adversary;
both substantially reduce the chance of an accident, which is the realistic
threat here.

---

## 7. Cuts

Deliberately not built, with the seam left clean:

- **Operator console is CLI-only.** `operator` lists pending requests with full
  context and `--take`/`--resume` drive the real state machine from a separate
  process; fixing the screen means using the window the run left open. The
  **mechanism** — token rotation, cross-process signalling, same-session
  takeover, resume checkpoint — is real and tested. Not built: co-browsing, queue
  assignment, audit UI.
- **No desktop adapter.** The `Surface` protocol is the whole point and is
  exercised by one implementation. A second (UIA/AX) would validate the seam;
  I'd build it next.
- **No drift aggregation across tenants.** The per-run signals exist
  (`used_fallback`, `degraded`, recovery counts); nothing rolls them up.
- **No confidence scoring or approval gating.** Artifacts are `draft` in effect;
  there is no `draft → approved` state machine gating unattended replay. The
  evidence shows exactly why this matters: a compiled artifact replayed and
  generalised correctly while carrying a wrong business-outcome string (§3).
- **No assisted fallback on replay failure.** A bounded, policy-checked
  single-step LLM recovery is the natural next feature; today a stuck replay
  escalates to a human. That ordering is deliberate — a human is the safe
  default.
- **No multi-run stability signal.** Replaying N times and reporting flakiness
  would be cheap and valuable; not done.
- **Artifact storage is files.** No registry, no concurrent-writer story.
- **Single LLM provider.** Bedrock only, isolated in one file.
- **Human action capture is navigation-level**, for the privacy reason above.

**What I'd do next, in order:** (1) the `draft → approved` gate, because
unattended replay of an unreviewed artifact is the most dangerous thing here;
(2) multi-run stability scoring to feed that gate; (3) a UIA desktop adapter to
prove the seam; (4) cross-tenant drift aggregation.

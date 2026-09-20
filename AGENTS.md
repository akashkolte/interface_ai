# AGENTS.md — Build Plan & Working Notes

Working plan for the interface.ai take-home: **Computer-Use Automation System**.
Read this first; it is the source of truth for what is being built and why.

---

## Context

interface.ai builds AI agents for banks and credit unions. This project is the
backend integration layer that gives those agents **hands** — the system that
lets an agent operate back-office applications that have **no API at all**.

The through-line the brief states, and the thing every decision serves:

> **The model discovers. The artifact becomes a reusable capability.
> Deterministic replay is how the AI agent invokes it in production.**

The end-to-end thread that must run all the way through:

```
goal
  → LLM-driven discovery run that completes it
    → saved capability artifact
      → deterministic replay (typed params, typed outputs, error/outcome handling)
        → human-escalation path that takes over the LIVE session
          → evidence for both runs
```

### What the evaluation actually rewards

Weighted in this order by the brief: system design → correctness of the core
loop → robustness & error handling → human-in-the-loop escalation →
generalization → safety & data handling → code quality → communication.

Explicitly **not** rewarded: feature breadth, framework name-dropping, scaling
infrastructure (queues, clusters, multi-tenant plumbing).

Three load-bearing pieces, per the brief: **artifact schema**, **deterministic
replay + error taxonomy**, **safety/escalation model**.

### Non-negotiables

- The discovery run **has to be real** — at least one genuine LLM-driven run
  against a live surface, with evidence in `/evidence/`. Everything else may be
  a clean, documented seam or mock.
- Deliverable paths are mandated exactly: `/README.md`, `/REPORT.md`,
  `/evidence/`.
- `REPORT.md` must use seven specific headings, verbatim (listed below).
- Submit by pushing to a **public GitHub repo** and emailing the link to
  `assignments@interface.ai` from the address applied with. Repo URL on its own
  line. **No zip.**

### Timeline

Assignment received 8 Sep. Target submission **Fri 26 Sep**. The brief states
*no deadline*, but elapsed silence is the main risk to the outcome — an
acknowledgement email with a committed date goes out first.

---

## Decisions locked

| Decision | Choice | Why |
|---|---|---|
| Target surface | Locally-built hostile legacy web app + a second tenant variant | Matches the "legacy, no clean DOM" reality; no ToS/rate-limit risk; lets us *inject* the error states the brief wants demonstrated |
| Stack | Python 3.12 + Playwright | a11y-tree perception fits the no-clean-DOM bias; Pydantic gives a genuinely typed artifact |
| Perception | Accessibility tree via CDP, **not** the DOM | Same shape a desktop UIA/AX adapter returns — makes the portability claim honest |
| Targeting | Ordered descriptor tiers, no CSS/XPath/test-ids | Survives rebranding and markup churn; degrades explicitly instead of silently |
| Architecture | Single process, file-backed artifacts | Brief penalizes premature infrastructure; abstractions must scale, plumbing need not exist |

---

## Current status

**Day 1 complete** (and the surface adapter, which was a Day 2 item).

### Built and verified

- **`target_app/`** — deliberately legacy member-servicing surface. One codebase,
  per-tenant config, because that is the real situation: many institutions
  running the same vendor product configured differently.
  - `<frameset>` (nav + content), table-based layout, **no ids, no test IDs, no
    `<label for>`**, meaningless class names
  - Flow: search → member detail → open sub-account → confirmation
  - Two tenants on distinct origins:
    - `base` → `http://localhost:5010` — CoreServicing 7.2
    - `creditunion_b` → `http://localhost:5011` — CoreServicing 7.4, relabelled
      fields, reordered nav, extra disclosure interstitial
  - 8 injectable faults spanning all three error classes
- **`src/surface/`** — the perceive/act seam. Perception via CDP accessibility
  tree; closed 7-verb action set; five-tier descriptor resolution.
- **`tests/test_surface.py`** — 10 tests, all passing.

### Verified facts worth keeping

- "No member found" returns **HTTP 200**. The business-outcome distinction
  genuinely cannot be read off a status code — which is exactly why the error
  taxonomy is a design problem, not a plumbing problem.
- The search input's accessible name is genuinely `''`. This is the legacy
  condition that forces the label-proximity tier to exist.
- Ports are **5010/5011, not 5000/5001**: macOS Control Center (AirPlay
  Receiver) listens on 5000 and silently 403s every request.

### Two real bugs found by running it, not reading it

Both are the flakiness class that makes naive UI automation untrustworthy. Both
are now regression-tested, and both belong in `REPORT.md` §3.

1. **Detached frames.** After a cross-document navigation, Playwright still
   exposes the *previous* document's children via `main_frame.child_frames`.
   Frame lookup returned a dead frame pointing at the old tenant's URL while the
   page was on the new one; queries silently returned zero matches. Fixed by
   filtering `is_detached()` at the single frame-lookup point.
2. **`Locator.count()` does not auto-wait.** A single resolution pass could miss
   an element milliseconds late and wrongly demote to a fallback tier — or fail
   outright. Resolution is now deadline-driven: the whole tier chain retries
   until the budget expires, primary always getting first refusal. This doubles
   as the handling for the "transient slowness" recoverable case.

---

## Repository layout

```
README.md                 # setup, keys, demo path (exact commands)   [mandated]
REPORT.md                 # 7 mandated headings, verbatim             [mandated]
AGENTS.md                 # this file
evidence/                                                             [mandated]
  discovery-<goal>/       # transcript, structured log, screenshots, artifact
  replay-success/         # structured log, outputs, checkpoint results
  replay-business-outcome/# "member not found" — a result, not a crash
  replay-hard-failure/    # injected failure, with debuggable error report
  replay-escalation/      # stuck → intervention request → human → resume
artifacts/                # saved capability artifacts (versioned JSON)
target_app/               # the hostile legacy app
  app.py                  # Flask; frameset, table layout, no test IDs
  tenants.py              # per-tenant config (base, creditunion_b)
  faults.py               # injectable runtime faults
  data.py                 # synthetic members (no real PII, ever)
  serve.py                # runs both tenant instances
  templates/              # deliberately legacy markup
src/
  surface/                # THE SEAM — perceive/act abstraction
    base.py               # Surface protocol, Action, Observation
    descriptors.py        # ElementDescriptor + tier chain
    ax.py                 # CDP accessibility-tree normalization
    web_playwright.py     # the one concrete adapter
  agent/                  # LLM discovery loop (observe → decide → act)
  artifact/               # Pydantic schema, versioning, serialization
  replay/                 # deterministic executor, checkpoints
  errors/                 # the three-class taxonomy
  safety/                 # allowlist, action risk, redaction
  escalation/             # control-transfer state machine + operator console
  evidence/               # structured logging, screenshot capture
tests/
```

---

## Core design

### 1. Surface abstraction — the seam §3.7 asks about

Everything above this line — agent loop, artifact schema, replay engine, safety
layer, escalation machinery — is written against these types and knows nothing
about browsers. Adding a Win32/UIA desktop adapter or a Citrix/screenshot
adapter means implementing `Surface` and nothing else.

```python
class Surface(Protocol):
    surface_kind: str
    def observe(self) -> Observation: ...
    def act(self, action: Action) -> ActionResult: ...
    def resolve(self, d: ElementDescriptor) -> tuple[ObservedElement | None, ResolutionReport]: ...
    def screenshot(self, path: str) -> str | None: ...
```

Two constraints make the portability claim true:

1. `Observation` is a **normalized element graph, not a DOM** — roles,
   accessible names, values, bounds. Every one of those surfaces can report
   these.
2. `Action` is a **small closed set**: `navigate`, `click`, `type`, `select`,
   `read`, `wait_for`, `press_key`. Anything richer (drag-drop, hover menus)
   would not survive the desktop adapter, so it is left out until a real flow
   needs it.

### 2. Artifact schema — focal point of the evaluation

Versioned Pydantic model → JSON. Framed as a **capability contract an AI agent
can call**, not a step list.

```
CapabilityArtifact
  schema_version                 # the schema's own version
  capability_id                  # stable, e.g. "member.lookup_savings_balance"
  version: int                   # artifact revision, monotonic
  name / description             # what a calling agent reads to decide to invoke
  target: {app_id, app_version_hint, entry_point}   # entry_point parameterized
  inputs:  [ParamSpec]           # name, type, required, constraints, example
  outputs: [OutputSpec]          # name, type, shape, source step ref
  steps:   [Step]                # ordered
  success:  Checkpoint           # overall success condition
  risk_profile                   # highest risk class among steps
  provenance                     # model, timestamp, discovery run id, tenant
  tenant_overrides: {tenant_id: PartialArtifact}    # sparse patches
```

```
Step
  id, intent                     # human-readable "why this step exists"
  action                         # from the closed set
  target: ElementDescriptor
  param_bindings                 # which inputs flow into this step
  extracts: [OutputBinding]      # what data this step reads out
  checkpoint                     # assert we actually got there
  risk: SAFE | CONFIRM | BLOCKED
  recovery: [RecoveryRule]       # known interstitials, bounded retries
```

**Design notes to defend in interview:**

- **Descriptor, not selector.** `role=button, name="Search"` survives rebranding
  and markup churn; an XPath into `<table>` soup does not.
- **Ordered fallbacks with recorded rationale** — the brief explicitly asks for
  "your reasoning about robustness" to live in the artifact. Written by the
  discovery LLM, when the reasoning is cheap to capture; reconstructing it later
  from a selector is nearly impossible.
- **Checkpoints on steps, not just at the end** — so a failure report can name
  the step, the expectation, and the observation.
- **`tenant_overrides` as sparse patches, not forked artifacts** — one base
  capability, per-tenant deltas. Drift becomes a diff, not a rebuild.

### 3. Locator tiers

Five ordered tiers. **No tier anywhere reads a CSS class, an id, or a test id** —
the target app has none on purpose. Resolution records *which tier matched*; a
run that succeeds only on tier 3 is a drift signal worth surfacing even though
it passed.

| Tier | Kind | What it means | Notes |
|---|---|---|---|
| 1 | `accessible_name` | role + accessible name | Most portable — web and desktop. Fails on anonymous legacy inputs |
| 2 | `label_proximity` | "the control nearest the text that names it" | Structural traversal over rendered table geometry. The tier legacy forms force |
| 3 | `table_cell` | value addressed by row label / column header | How data is read out of record-detail grids |
| 4 | `ordinal` | nth control of a role in scope | Brittle; recorded explicitly rather than guessed silently |
| 5 | `visual_bounds` | click a viewport-relative point | Escape hatch for surfaces with no queryable tree. Flagged low-confidence |

`Scope` carries `frame_path` — framesets make this mandatory, not optional.

### 4. Discovery loop (`src/agent/`)

- Input: natural-language goal + target entry point.
- Loop: `observe()` → compact a11y snapshot → LLM picks **one** action
  (structured tool-call output) → guardrail check → `act()` → repeat.
- Stopping: goal checkpoint met, max steps, wall-clock timeout, or dead-end (no
  progress across N observations) → escalate.
- On success, a **second LLM pass compiles the transcript into the artifact**:
  parameterizes literals (`12345` → `:memberId`), names outputs, writes
  descriptor rationales, proposes checkpoints. Keeping compilation separate from
  the action loop is what satisfies "decoupled from the raw model transcript".

Model: Claude via the Anthropic API. Key from env, **never committed**.

### 5. Deterministic replay (`src/replay/`)

**No LLM anywhere in this path.** Artifact + params in, structured result out.

- Resolve each descriptor through its tier chain; record which tier matched.
- Wait on **conditions**, never fixed sleeps.
- Evaluate every step checkpoint, then the success checkpoint.
- Extract declared outputs, validate against their `OutputSpec` types.

**Error taxonomy — three classes, structurally distinct in the result type:**

| Class | Meaning | Result |
|---|---|---|
| `BusinessOutcome` | A legitimate answer the caller needs: "no such member", "account closed", validation rejection | `status=business_outcome` + `outcome_code`, still a **successful execution**, exit 0 |
| `Recoverable` | Known interstitial, transient slowness, benign dialog, session re-auth | Handled by bounded `RecoveryRule`s, retried, logged, run continues |
| `HardFailure` | Unresolvable | `status=failure` + step id, expected, observed, screenshot |

The brief calls conflating outcome-vs-failure **"the most common design mistake
here"**, so this gets its own types, its own tests, and its own evidence
directory.

### 6. Safety (`src/safety/`)

- **Allowlist** — permitted origins/route patterns *and* permitted action types.
  Enforced at a **single choke point** every action passes through, during both
  discovery and replay, so it cannot be bypassed.
- **Action risk classification** — `SAFE` (read, navigate in-allowlist, type
  into search) / `CONFIRM` (mutating submit) / `BLOCKED` (irreversible patterns:
  transfer, delete, approve). Default posture: risky actions need an explicit
  flag plus recorded confirmation, else they escalate to a human.
- **Redaction** — filter on the write path into both artifacts and logs.
  Field-name heuristics (ssn, password, token, account_number, card) plus regex.
  Artifacts store **parameter shapes and examples, never captured real values**.

### 7. Escalation & handoff (`src/escalation/`)

Must be real, not a TODO — it is a named evaluation criterion.

**Control-transfer state machine, single-writer invariant:**

```
AUTOMATION_CONTROL → INTERVENTION_REQUESTED → HUMAN_CONTROL
                   → RESUMING → AUTOMATION_CONTROL
```

- A **control token** gates the executor: no action dispatches unless the run
  holds it. Ceding releases it. "Who is in control" becomes an enforced
  invariant rather than a convention.
- **Stuck detection:** descriptor resolution exhausted its tiers, a
  `BLOCKED`/`CONFIRM` action hit, checkpoint failed with no recovery rule,
  no-progress loop, or session timeout.
- **Intervention request** carries: capability/goal, current step id + intent,
  input params (redacted), live observation + screenshot, and *why it stopped*.
- **Same live session:** headed browser with a persistent context. On cede, the
  automation stops driving and the human uses that same window. A minimal
  operator console (FastAPI page + CLI fallback) shows the request, an
  "I have control" ack, and a "resume" button.
- **Capture what the human did:** page event listeners + navigation history,
  appended to run evidence.
- **Resume:** on hand-back the executor re-observes and evaluates a **resume
  checkpoint** before continuing. If it fails, escalate again rather than
  blindly proceeding.

Documented cut: the operator console is intentionally minimal — the brief
permits a bare/mock operator surface. The **mechanism and control model are the
real parts**.

### 8. Tenant variant — the multi-tenant answer

`creditunion_b` is the same product, configured differently: relabelled fields
("Member ID" → "Account Holder #"), reordered nav, one extra disclosure
interstitial, a different version string.

Demonstrates one base artifact plus a small `tenant_overrides` patch replaying
successfully against both. Already verified at the surface level: a descriptor
naming `"Search"` fails on the variant, `"Find Member"` succeeds.

---

## REPORT.md — the seven mandated headings

Use verbatim; ~1–3 pages total.

1. **Architecture** — surface/agent/artifact/replay/escalation split; why single
   process is right here; what would become a service later.
2. **Artifact schema** — contract framing, descriptor tiers, parameterization,
   why rationale is stored, versioning and overrides.
3. **Determinism & error handling** — no-LLM replay path, tier fallbacks,
   condition-based waits, checkpoints, the three-class taxonomy, and the two
   real bugs above as evidence of what "deterministic" actually costs.
4. **Heterogeneity & multi-tenant** — the `Surface` seam and how a desktop
   UIA/AX adapter drops in unchanged; base artifact + sparse overrides; drift
   detection via fallback-tier telemetry.
5. **Escalation & handoff** — stuck detection, intervention request contents,
   control token + state machine, same-session takeover, resume checkpoint.
6. **Safety** — allowlist choke point, three-tier action risk, redaction on the
   write path, and the limits of each.
7. **Cuts** — honest list: minimal operator UI, no desktop adapter implemented,
   no persistence beyond files, no confidence scoring, single LLM provider, no
   multi-run stability signal. Plus what comes next with more time.

---

## Schedule to Fri 26 Sep

| Day | Work | State |
|---|---|---|
| **Sat 20** | Acknowledgement email. Repo scaffold, `Surface` protocol, hostile target app + fault injection | **done** (+ surface adapter, pulled forward) |
| **Sun 21** | Artifact schema v1 (Pydantic), safety choke point | |
| **Mon 22** | Discovery loop against Anthropic API, first **real** LLM run, transcript → artifact compilation | |
| **Tue 23** | Replay engine: tier resolution, checkpoints, typed outputs. Deterministic success path green | |
| **Wed 24** | Error taxonomy wired to fault injection; three failure-mode evidence runs; redaction; tests | |
| **Thu 25** | Escalation: control token, state machine, intervention request, operator console, resume checkpoint. Tenant overrides | |
| **Fri 26** | Capture `/evidence/`, write `REPORT.md` + `README.md`, push public repo, email link | |

**Hard rule:** if a day slips, **cut depth, not a capability**. The brief rewards
a thin-but-complete slice and penalizes a polished subset. Anything dropped goes
into the Cuts section, which is itself a scored deliverable.

---

## Verification

```bash
# all tests
.venv/bin/python -m pytest tests/ -q

# run both tenant surfaces
.venv/bin/python -m target_app.serve
```

End-to-end checks, each producing evidence:

| Check | Command / condition | Expected |
|---|---|---|
| Discovery | `discover --goal "..." --target http://localhost:5010` | real artifact in `artifacts/`, evidence + screenshots |
| Replay success | `replay --artifact <id>.json --params '{"memberId":"12345"}'` | typed outputs, checkpoints pass, **exit 0** |
| Business outcome | same, non-existent member id | `status=business_outcome`, `outcome_code=member_not_found`, **exit 0** |
| Hard failure | `FAULT=app_error_500` | `status=failure` + step id, expected, observed, screenshot; non-zero exit |
| Escalation | `FAULT=unexpected_confirm_dialog`, no matching recovery rule | intervention request raised; human takes over *same* window; resume completes; human actions in evidence |
| Cross-tenant | base artifact vs `creditunion_b` | succeeds with override patch; clear resolution error without it |
| README demo path | run exact README commands in a clean clone | completes with no undocumented steps |

---

## Conventions for this repo

- **Never commit secrets.** `ANTHROPIC_API_KEY` lives in `.env` (gitignored);
  `.env.example` is committed.
- **No real PII, ever.** `target_app/data.py` is synthetic. The sensitive-looking
  fields exist so redaction can be proven, not because they are real.
- The assignment PDF is gitignored — do not publish the company's brief.
- Prefer **conditions over sleeps** anywhere timing is involved.
- When behaviour changes, add or update a test. The two regressions above are
  the model: name the failure mode in the test docstring.
- Comments explain **why**, not what. The reasoning is the deliverable here.

---

## Open items

- [ ] **Acknowledgement email to `assignments@interface.ai`** — owner: Akash.
      Highest-leverage open item.
- [ ] `ANTHROPIC_API_KEY` in `.env` — needed Mon 22 for the mandatory real
      discovery run. On the critical path.
- [ ] Public GitHub repo name — needed Fri 26.
- [ ] Optional short screen recording of the escalation flow — decide Fri 26
      based on time remaining.

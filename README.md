# Computer-Use Automation System

An LLM discovers how to accomplish a goal by driving a legacy application's UI.
The successful run is recorded as a **typed, versioned capability artifact**.
From then on the artifact is **replayed deterministically, with no model in the
decision loop** — which is how an AI agent invokes it in production.

> The model discovers. The artifact becomes a reusable capability.
> Deterministic replay is how the agent invokes it.

Built against a deliberately legacy target: a `<frameset>` banking servicing app
with table-based layout, **no ids, no test IDs and no `<label for>`**. Perception
is the **accessibility tree**, not the DOM, so the same artifact shape would work
against a desktop UIA/AX surface.

See [REPORT.md](REPORT.md) for the design and the trade-offs.

---

## Setup

Requires Python 3.12+.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

**Model access.** Discovery (and only discovery) needs a Claude model. This runs
against **Claude on Amazon Bedrock** using your existing AWS credentials:

```bash
export AWS_REGION=us-east-1
export CUA_MODEL=us.anthropic.claude-sonnet-4-5-20250929-v1:0   # optional; this is the default
```

Bedrock needs the **cross-region inference profile id** (the `us.` prefix). The
bare `anthropic.*` ids require provisioned throughput and fail on-demand.

To use the Anthropic API directly instead, swap the client in
[`src/agent/llm.py`](src/agent/llm.py) — it is the only file that knows about a
model vendor.

**Running without a model:** everything except `discover` works with no model
access at all. Replay, the error taxonomy, guardrails, escalation and all tests
run offline against the committed artifact.

---

## Start the target application

Two tenants, one codebase, distinct origins — the stand-in for two institutions
running the same vendor product, configured differently:

```bash
.venv/bin/python -m target_app.serve
```

| Tenant | URL | Product | Differences |
|---|---|---|---|
| `base` | http://localhost:5010 | CoreServicing 7.2 | — |
| `creditunion_b` | http://localhost:5011 | CoreServicing 7.4 | relabelled fields, reordered nav, extra disclosure step |

> Ports are 5010/5011, not 5000/5001: on macOS, Control Center (AirPlay Receiver)
> listens on 5000 and silently returns 403 to everything.

---

## Demo path

### 1. Discovery — a real LLM run against the live surface

```bash
.venv/bin/python -m src.cli discover \
  --goal "look up member 67890 and read their current savings balance" \
  --target http://localhost:5010
```

Writes a capability artifact to `artifacts/` and the full transcript,
screenshots and structured log to `evidence/discovery-<id>/`.

### 2. Replay — deterministic, no model

```bash
.venv/bin/python -m src.cli replay \
  --capability member.lookup_savings_balance \
  --params '{"memberId":"12345"}'
```

→ `SUCCESS  memberName=Dana Whitfield, savingsBalance=$4,182.55`, exit **0**.

### 3. A business outcome is *not* a failure

```bash
.venv/bin/python -m src.cli replay \
  --capability member.lookup_savings_balance \
  --params '{"memberId":"99999"}'
```

→ `BUSINESS OUTCOME  member_not_found`, exit **0**. The capability worked; the
answer is that no such member exists.

### 4. A hard failure is debuggable

```bash
.venv/bin/python -m src.cli replay \
  --capability member.lookup_savings_balance \
  --params '{"memberId":"12345"}' --fault app_error_500
```

→ `FAILURE [checkpoint_failed]` naming the step, what was **expected**, what was
**observed**, and a screenshot. Exit **1**.

### 5. Cross-tenant reuse — one artifact, a sparse override

```bash
.venv/bin/python -m src.cli replay \
  --capability member.lookup_savings_balance \
  --params '{"memberId":"12345"}' --tenant creditunion_b
```

→ `SUCCESS` on the relabelled tenant. Drop `--tenant` and it fails cleanly with
`target_unresolvable`, which is the point: the override is a three-line patch,
not a second copy of the capability.

### 6. Escalation — a human takes the live session

```bash
.venv/bin/python -m src.cli replay \
  --capability member.lookup_savings_balance \
  --params '{"memberId":"12345"}' \
  --fault app_error_500 --escalate --wait-seconds 120 --headed
```

The run stops, raises an intervention request, and **hands you the same browser
window**. Fix the screen by hand, then hand control back; the run re-checks its
checkpoint and resumes. Pending requests:

```bash
.venv/bin/python -m src.cli operator
```

### 7. The capability catalogue an agent would browse

```bash
.venv/bin/python -m src.cli catalog
```

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

The target app is started in-process by the fixtures, so no setup step is
needed. Browser-driven tests take a few minutes.

---

## Layout

```
src/
  surface/      THE SEAM — perceive/act abstraction; only this knows about browsers
    base.py         Surface protocol, Action, Observation
    descriptors.py  ElementDescriptor + the five-tier locator chain
    ax.py           CDP accessibility-tree normalization
    web_playwright.py   the one concrete adapter
  artifact/     the capability schema and its file store
  agent/        LLM discovery loop, and transcript -> artifact compilation
  replay/       deterministic executor + checkpoint evaluation
  errors/       the three-class result taxonomy
  safety/       allowlist, action-risk policy, redaction
  escalation/   control-transfer state machine, intervention store
  evidence/     structured logging and screenshot capture
target_app/     the deliberately legacy surface, two tenants, injectable faults
artifacts/      saved capability artifacts (versioned JSON)
evidence/       discovery and replay runs
```

## Safety notes

- No secrets in the repo. Credentials come from the environment.
- `target_app/data.py` is entirely synthetic. The SSN- and card-shaped fields
  exist so redaction can be *proven*, not because they are real.
- Every action — discovery and replay alike — passes one allowlist choke point.
- `--allow-risky` is required before any state-mutating step runs unattended.

# Computer-Use Automation System

A system that gives an AI agent hands to operate legacy software UIs with no API — the
integration layer between an agent that decides *what* to do and the back-office
applications where the work actually happens.

The core loop: an LLM drives a real browser to accomplish a goal once, the successful
run is recorded as a typed capability artifact, and that artifact replays deterministically
in production with no model in the decision loop.

---

## Architecture overview

Discovery (LLM) Artifact Replay (no LLM)
───────────────── → ─────────────── → ─────────────────────
observe → decide typed, versioned deterministic steps
→ act loop JSON capability stable locators
against live UI with schema error taxonomy
contract structured result
↓
Operator Console
(escalation + live feed)


**Stack:** Python 3.11 · Playwright async · AWS Bedrock (Claude Sonnet via boto3) ·
FastAPI · Flask · Pydantic v2 · SQLite · MCP SDK

---

## What's implemented

### Core (all required sections of the brief)

| Component | Location | Description |
|-----------|----------|-------------|
| Discovery agent | `agent/loop.py` | LLM observe→decide→act loop against a live browser surface |
| Artifact compiler | `agent/compiler.py` | Converts LLM trajectory into a typed, versioned capability JSON |
| Capability schema | `capability/schema.py` | Single source of truth for all data models |
| Deterministic replay | `replay/executor.py` | Replays recorded capabilities with no LLM; 4-class error taxonomy |
| Error taxonomy | `replay/outcomes.py` | `ReplaySuccess`, `BusinessOutcome`, `HardFailure`, `IndeterminateCommit` |
| Safety guardrails | `policy/engine.py` | Allowlist enforcement, risky-action classification |
| PII redaction | `policy/redactor.py` | Redacts credentials and sensitive data before every log write |
| Human escalation | `escalation/controller.py` | asyncio.Event gate; same-session browser handoff |
| Operator console | `escalation/api.py` + `console.html` | Real-time FastAPI dashboard with WebSocket step feed |
| Surface abstraction | `surfaces/base.py` | Abstract interface separating perception/action from recorded flow |
| Mock target app | `target_app/app.py` | Intentionally legacy Flask app (table layouts, iframes, no test IDs) |

### Stretch goals

| Feature | Location | Description |
|---------|----------|-------------|
| Capability invocation API | `escalation/api.py` | `GET /capabilities`, `POST /capabilities/{name}/invoke` |
| Stability runner | `evals/stability_runner.py` | Runs replay N times; reports success rate, p50/p95, STABLE/FLAKY/BROKEN |
| ARIA drift detection | `replay/conditions.py` | Structural fingerprint at each checkpoint; detects UI changes before failure |
| Confidence gate | `evals/stability_runner.py` | `--approve` promotes artifact from `draft` to `approved` after STABLE verdict |
| MCP server | `escalation/api.py` | Capabilities exposed as MCP tools at `/mcp`; any MCP-compatible agent can invoke them |
| Assisted fallback | `replay/fallback.py` | Bounded single-step LLM recovery on eligible hard failures; one call, one attempt |

---

## Setup

### Requirements
- Python 3.11+
- AWS account with Bedrock access (Claude Sonnet model enabled in `us-east-1`)
- Playwright Chromium

### Install

```bash
git clone https://github.com/Xaaby/computer-use-automation
cd computer-use-automation
pip install -e .
playwright install chromium
```

### Configure

```bash
cp .env.example .env
```

Edit `.env` with your values:

AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0
APP_USERNAME=admin
APP_PASSWORD=admin


> The mock bank app does not require a database or external service.
> All seed data is in `target_app/data/members.json`.

---

## Running without live AWS

All tests except the discovery run mock Bedrock:

```bash
python -m pytest tests/ -v
```

To run the operator console and replay without triggering AWS, use a
pre-recorded artifact from `capabilities/`:

```bash
# Terminal 1
python -m target_app.app

# Terminal 2
python -m escalation.api

# Terminal 3 — replay against existing artifact (no LLM call)
python -m replay.executor \
  --artifact capabilities/member.lookup_savings_balance.capability.json \
  --params '{"member_id": "10001"}' \
  --headless
```

---

## Demo path

### 1. Start the mock bank app

```bash
python -m target_app.app
```

Serves a legacy-style bank admin UI at `http://localhost:5000` — table layouts,
iframes for account data, no test IDs. This is the stand-in for the real thing.

### 2. Start the operator console

```bash
python -m escalation.api
```

Opens the real-time dashboard at `http://localhost:8765`.

### 3. Run deterministic replay (happy path)

```bash
python -m replay.executor \
  --artifact capabilities/member.lookup_savings_balance.capability.json \
  --params '{"member_id": "10001"}' \
  --console
```

The `--console` flag streams live step events and ~1fps browser screenshots to
the dashboard. Open `http://localhost:8765` to watch the replay in real time.

Expected result:
```json
{"status": "success", "outputs": {"savings_balance": "4821.50"}}
```

### 4. Run with a business outcome (member not found)

```bash
python -m replay.executor \
  --artifact capabilities/member.lookup_savings_balance.capability.json \
  --params '{"member_id": "88888"}' \
  --headless
```

Expected result:
```json
{"status": "business_outcome", "business_outcome": {"code": "MEMBER_NOT_FOUND"}}
```

### 5. Run with a hard failure (permission denied)

```bash
python -m replay.executor \
  --artifact capabilities/member.lookup_savings_balance.capability.json \
  --params '{"member_id": "90001"}' \
  --headless
```

Expected result:
```json
{"status": "hard_failure", "failure": {"code": "PERMISSION_DENIED"}}
```

A screenshot and ARIA snapshot are saved to `evidence/runs/` automatically.

### 6. Re-run discovery (requires AWS Bedrock)

This is the LLM-driven run. It costs one API call and takes 30–90 seconds.

```bash
python -m agent.loop \
  --goal "Look up member 10001 and read their current savings balance" \
  --entry-point http://localhost:5000/members/search \
  --params '{"member_id": "10001"}'
```

A new capability artifact is saved to `capabilities/`.

### 7. Invoke via the capability API

```bash
curl http://localhost:8765/capabilities

curl -X POST http://localhost:8765/capabilities/member.lookup_savings_balance/invoke \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"member_id": "10001"}}'
```

### 8. Run the stability eval

```bash
python -m evals.stability_runner \
  --artifact capabilities/member.lookup_savings_balance.capability.json \
  --params '{"member_id": "10001"}' \
  --runs 20
```

To promote the artifact from `draft` to `approved` after a STABLE verdict:

```bash
python -m evals.stability_runner \
  --artifact capabilities/member.lookup_savings_balance.capability.json \
  --params '{"member_id": "10001"}' \
  --runs 20 --approve
```

---

## Evidence

Pre-recorded evidence from a real LLM discovery run and three replay variants
is committed under `evidence/runs/`:

| Run | Folder | What it shows |
|-----|--------|---------------|
| Discovery | `9b4334d4-...` | Real LLM-driven run; step-by-step JSONL + trace |
| Replay success | `fc97a898-...` | Member 10001, `savings_balance: 4821.50` |
| Business outcome | `bf4e2e19-...` | Member 88888, `MEMBER_NOT_FOUND` |
| Hard failure | `7a4668b9-...` | Member 90001, `PERMISSION_DENIED` + screenshot |

Stability report (20 runs, p50=3141ms, p95=4587ms, verdict STABLE) is at
`evidence/stability_report.json`.

---

## Test suite

```bash
python -m pytest tests/ -v
```

63 tests covering schema validation, guardrails, error taxonomy, drift detection,
replay outcomes, escalation gate logic, MCP tool registration, and assisted fallback.
No external services required except for the discovery run.

---

## Repository layout

agent/ LLM discovery loop and artifact compiler
capability/ Pydantic schema (single source of truth) and artifact store
escalation/ FastAPI operator console, WebSocket feed, MCP server, human handoff
evals/ Stability runner and confidence scoring
evidence/ Committed run logs, screenshots, and artifacts
policy/ Allowlist engine and PII redactor
replay/ Deterministic executor, error taxonomy, drift detection, fallback
surfaces/ Abstract surface interface and Playwright implementation
target_app/ Mock legacy bank app (Flask)
tests/ 63 tests across all modules
capabilities/ Saved capability artifacts

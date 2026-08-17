# Computer-Use Automation System

A system that gives AI agents "hands" to operate legacy bank software with no API.
Built for the interface.ai engineering take-home assessment.

## Architecture

LLM Discovery → Capability Artifact → Deterministic Replay → Human Escalation

## Quick Start

### Prerequisites

- Python 3.11+
- AWS credentials with Bedrock access (discovery only)

### Setup

```bash
git clone https://github.com/Xaaby/computer-use-automation.git
cd computer-use-automation
pip install -e .
playwright install chromium
cp .env.example .env
# Edit .env with your AWS credentials
```

### Environment Variables (`.env`)

```
AWS_ACCESS_KEY_ID=your_key
AWS_SECRET_ACCESS_KEY=your_secret
AWS_REGION=us-east-2
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0
APP_USERNAME=admin
APP_PASSWORD=admin
```

### Start the Mock Bank App

```bash
python target_app/app.py
# App runs at http://localhost:5000
```

### Demo: Full End-to-End

**Step 1: Discovery Run (real LLM driving the browser)**

```bash
python -m agent.loop \
  --goal "Look up member 10001 and find their savings balance" \
  --entry-point "http://localhost:5000/members/search" \
  --params "{\"member_id\": \"10001\"}" \
  --headless
```

Artifact written to `capabilities/member.lookup_savings_balance.capability.json`.

**Step 2: Replay (deterministic, no LLM)**

```bash
python -m replay.executor \
  --artifact capabilities/member.lookup_savings_balance.capability.json \
  --params "{\"member_id\": \"10001\"}" \
  --headless
```

**Step 3: Business Outcome (member not found)**

```bash
# Decision: use 88888 — IDs starting with 9 return HTTP 403 (PERMISSION_DENIED)
python -m replay.executor \
  --artifact capabilities/member.lookup_savings_balance.capability.json \
  --params "{\"member_id\": \"88888\"}" \
  --headless
```

**Step 4: Capability API (Stretch A)**

```bash
python -m escalation.api
# In another shell:
curl http://localhost:8765/capabilities
curl -X POST http://localhost:8765/capabilities/member.lookup_savings_balance/invoke \
  -H "Content-Type: application/json" \
  -d "{\"inputs\": {\"member_id\": \"10001\"}}"
```

**Step 5: Stability Eval (Stretch B)**

```bash
python -m evals.stability_runner \
  --artifact capabilities/member.lookup_savings_balance.capability.json \
  --params "{\"member_id\": \"10001\"}" \
  --runs 20 \
  --headless \
  --output evidence/stability_report.json
```

**Step 6: Run All Tests**

```bash
python -m pytest tests/ -v
```

## Evidence

See `evidence/` for discovery logs, replay logs, error cases, screenshots, and the stability report.

## Design Write-up

See [REPORT.md](REPORT.md) for architecture decisions, trade-offs, and cuts.

## Key Decisions

| Topic | Decision |
|-------|----------|
| MEMBER_NOT_FOUND test ID | `88888` (not `99999`) — `9*` IDs are 403 |
| Auth during discovery/replay | Session bootstrap from `APP_USERNAME`/`APP_PASSWORD` env vars |
| LLM provider | AWS Bedrock `converse()` via boto3 (not Anthropic SDK) |
| ARIA snapshots | Always `page.aria_snapshot(mode="ai")` |
| Windows asyncio | `WindowsProactorEventLoopPolicy` in every async entrypoint |

# Phase Status - Update After Completing Each Phase

## Current Phase: ALL PHASES COMPLETE (1–7) + Enhancement Layers 1–5

## Phase Completion Checklist

### Phase 1 - Foundation (START HERE)
- [x] `pyproject.toml` - dependencies installed
- [x] `capability/schema.py` - all Pydantic models
- [x] `policy/allowlist.json` - allowlist config
- [x] `policy/engine.py` - allowlist enforcement
- [x] `policy/redactor.py` - PII redaction
- [x] `target_app/app.py` - Flask mock bank app
- [x] `target_app/data/members.json` - 10 fake members
- [x] `target_app/templates/` - all HTML templates (zero data-testid)
- [x] `tests/test_schema.py` - schema validation tests
- [x] `tests/test_guardrails.py` - policy enforcement tests

**Checkpoint 1 verification:**
```bash
python target_app/app.py &
curl http://localhost:5000/members/search
python -m pytest tests/test_schema.py tests/test_guardrails.py -v
```
All must pass before Phase 2.

---

### Phase 2 - Surface + Tools
- [x] `surfaces/base.py` - abstract Surface interface
- [x] `surfaces/playwright_web.py` - Playwright implementation
- [x] `agent/tools.py` - 8 tool definitions (JSON schemas)
- [x] `agent/prompts.py` - system prompt + observation formatter

**Checkpoint 2 verification:**
```bash
python -c "
import asyncio, sys
sys.platform == 'win32' and setattr(asyncio, '_default_executor', None)
asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
from playwright.async_api import async_playwright
async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto('http://localhost:5000/members/search')
        snap = await page.aria_snapshot(mode='ai')
        print('OK - snapshot length:', len(snap))
        await browser.close()
    asyncio.run(test())
"
```

---

### Phase 3 - Discovery Agent
- [x] `agent/loop.py` - LLM observe→decide→act loop (boto3 Bedrock)
- [x] `agent/compiler.py` - discovery trajectory → capability artifact

**Checkpoint 3 verification:**
```bash
python -m agent.loop --goal "Look up member 10001 and find their savings balance" --entry-point "http://localhost:5000/members/search"
ls capabilities/*.capability.json
```

---

### Phase 4 - Replay Engine
- [x] `replay/outcomes.py` - typed result classes
- [x] `replay/conditions.py` - condition evaluators
- [x] `replay/executor.py` - deterministic replay engine
- [x] `tests/test_replay.py` - replay tests
- [x] `tests/test_error_taxonomy.py` - error classification tests

**Checkpoint 4 verification:**
```bash
python -m replay.executor --artifact capabilities/member.lookup_savings_balance.capability.json --params '{"member_id": "10001"}'
python -m replay.executor --artifact capabilities/member.lookup_savings_balance.capability.json --params '{"member_id": "88888"}'
python -m pytest tests/test_replay.py tests/test_error_taxonomy.py -v
```
Decision: MEMBER_NOT_FOUND uses `88888` (not `99999`) because IDs starting with `9` are HTTP 403.

---

### Phase 5 - Escalation
- [x] `escalation/controller.py` - asyncio.Event gate + state machine
- [x] `escalation/api.py` - FastAPI operator console (port 8765)
- [x] `escalation/recorder.py` - human action capture
- [x] `tests/test_escalation.py` - escalation mechanism tests

**Checkpoint 5 verification:**
```bash
python -m pytest tests/test_escalation.py -v
```

---

### Phase 6 - Stretch Goals
- [x] `capability/api.py` - FastAPI capability invocation helpers
- [x] `evals/stability_runner.py` - runs replay N times, reports score
- [x] `tests/test_stability.py` - stability score tests

**Checkpoint 6 verification:**
```bash
curl http://localhost:8765/capabilities
curl -X POST http://localhost:8765/capabilities/member.lookup_savings_balance/invoke -H "Content-Type: application/json" -d '{"inputs": {"member_id": "10001"}}'
python -m evals.stability_runner --artifact capabilities/member.lookup_savings_balance.capability.json --runs 20
```

---

### Phase 7 - Documentation + Evidence
- [x] `README.md` - setup + exact demo commands
- [x] `REPORT.md` - 7-section design write-up
- [x] `evidence/runs/` - discovery log, replay logs, error log
- [x] `evidence/capabilities/` - example artifact
- [x] All tests pass: `python -m pytest tests/ -v`

---

## Phase History
| Phase | Status | Completed At | Notes |
|-------|--------|-------------|-------|
| Phase 1 | Complete | 2026-08-16 23:54:46 | Checkpoint verified: 18 pytest passed; Flask /members/search smoke OK (Member ID form) |
| Phase 2 | Complete | 2026-08-17 | Checkpoint 2 OK: aria_snapshot mode=ai has refs + Member ID; imports OK |
| Phase 3 | Complete | 2026-08-17 | Discovery OK: member.lookup_savings_balance.capability.json; schema valid |
| Phase 4 | Complete | 2026-08-17 | MEMBER_NOT_FOUND uses 88888 (not 99999); 13 pytest OK; CLI success+BO OK |
| Phase 5 | Complete | 2026-08-17 | 10 escalation tests passed incl. FastAPI gate/resume |
| Phase 6 | Complete | 2026-08-17 | Capability API invoke OK; stability 20/20 STABLE |
| Phase 7 | Complete | 2026-08-17 | README + REPORT + evidence; full suite 43 passed |

---

## Enhancement Sprint (Layers 1–5) COMPLETE

- [x] Enhancement Layer 1 — Schema extensions: COMPLETE (`ARIAFingerprint`, `ConfidenceScore`, `FallbackPatch`, `StepEvent`; `StepDefinition.fingerprint`; `Provenance.confidence/approved_by/approved_at`; `ReplayResult.patches`)
- [x] Enhancement Layer 2 — Operator console (WebSocket + screenshot + HTML): COMPLETE (`escalation/console.html`, `/ws/steps`, `--console`, in-process `ws_manager`)
- [x] Enhancement Layer 3 — ARIA fingerprint + drift + confidence gate: COMPLETE (`compute_fingerprint` handles Playwright YAML + `{role,name}`; `compute_confidence` 0.95/0.50; `promote_artifact` writes `provenance.status`)
- [x] Enhancement Layer 4 — MCP server exposure: COMPLETE (`mcp>=2.0.0`, module-level `MCPServer` + `streamable_http_app`, lifespan mount at `/mcp`)
- [x] Enhancement Layer 5 — Assisted fallback: COMPLETE (`replay/fallback.py`, one `converse()`, never irreversible/draft, `PolicyViolationError` → `policy_blocked`)

**Current Phase: ALL PHASES COMPLETE (1–7) + Enhancement Layers 1–5**


---

## Enhancement Layers (Post Phase 7)

- [x] Enhancement Layer 1 — Schema extensions (ARIAFingerprint, ConfidenceScore, FallbackPatch, StepEvent): COMPLETE
- [x] Enhancement Layer 2 — Operator console (WebSocket + screenshot + console.html): COMPLETE
- [x] Enhancement Layer 3 — ARIA fingerprint + drift + confidence gate: COMPLETE
- [x] Enhancement Layer 4 — MCP server exposure at `/mcp`: COMPLETE
- [x] Enhancement Layer 5 — Assisted fallback (bounded, approved-artifacts only): COMPLETE

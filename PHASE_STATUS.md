# Phase Status - Update After Completing Each Phase

## Current Phase: PHASE 1 COMPLETE — next is Phase 2 (not started)

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
- [ ] `surfaces/base.py` - abstract Surface interface
- [ ] `surfaces/playwright_web.py` - Playwright implementation
- [ ] `agent/tools.py` - 8 tool definitions (JSON schemas)
- [ ] `agent/prompts.py` - system prompt + observation formatter

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
- [ ] `agent/loop.py` - LLM observedecideact loop (boto3 Bedrock)
- [ ] `agent/compiler.py` - discovery trajectory  capability artifact

**Checkpoint 3 verification:**
```bash
python -m agent.loop --goal "Look up member 10001 and find their savings balance" --entry-point "http://localhost:5000/members/search"
ls capabilities/*.capability.json
```

---

### Phase 4 - Replay Engine
- [ ] `replay/outcomes.py` - typed result classes
- [ ] `replay/conditions.py` - condition evaluators
- [ ] `replay/executor.py` - deterministic replay engine
- [ ] `tests/test_replay.py` - replay tests
- [ ] `tests/test_error_taxonomy.py` - error classification tests

**Checkpoint 4 verification:**
```bash
python -m replay.executor --artifact capabilities/member.lookup_savings_balance.capability.json --params '{"member_id": "10001"}'
python -m replay.executor --artifact capabilities/member.lookup_savings_balance.capability.json --params '{"member_id": "99999"}'
python -m pytest tests/test_replay.py tests/test_error_taxonomy.py -v
```

---

### Phase 5 - Escalation
- [ ] `escalation/controller.py` - asyncio.Event gate + state machine
- [ ] `escalation/api.py` - FastAPI operator console (port 8765)
- [ ] `escalation/recorder.py` - human action capture
- [ ] `tests/test_escalation.py` - escalation mechanism tests

**Checkpoint 5 verification:**
```bash
python -m pytest tests/test_escalation.py -v
# Manual: start a run, verify it pauses at risky step, POST /resume, verify it continues
curl -X POST http://localhost:8765/resume
```

---

### Phase 6 - Stretch Goals
- [ ] `capability/api.py` - FastAPI capability invocation endpoint
- [ ] `evals/stability_runner.py` - runs replay N times, reports score
- [ ] `tests/test_stability.py` - stability score tests

**Checkpoint 6 verification:**
```bash
curl http://localhost:8765/capabilities
curl -X POST http://localhost:8765/capabilities/member.lookup_savings_balance/invoke -H "Content-Type: application/json" -d '{"inputs": {"member_id": "10001"}}'
python -m evals.stability_runner --artifact capabilities/member.lookup_savings_balance.capability.json --runs 20
```

---

### Phase 7 - Documentation + Evidence
- [ ] `README.md` - setup + exact demo commands
- [ ] `REPORT.md` - 7-section design write-up
- [ ] `evidence/runs/` - discovery log, replay logs, error log
- [ ] `evidence/capabilities/` - example artifact
- [ ] All tests pass: `python -m pytest tests/ -v`

---

## Phase History
| Phase | Status | Completed At | Notes |
|-------|--------|-------------|-------|
| Phase 1 | Complete | 2026-08-16 23:54:46 | Checkpoint verified: 18 pytest passed; Flask /members/search smoke OK (Member ID form) |
| Phase 2 | ?? Not Started | - | - |
| Phase 3 | ?? Not Started | - | - |
| Phase 4 | ?? Not Started | - | - |
| Phase 5 | ?? Not Started | - | - |
| Phase 6 | ?? Not Started | - | - |
| Phase 7 | ?? Not Started | - | - |

# RULES — Read First Every Session Before Writing Any Code

## What You Are Building
A Computer-Use Automation System for interface.ai take-home assessment.
Python 3.11, Playwright ≥1.49 (async), AWS Bedrock (boto3), Flask mock bank app, FastAPI operator console.
GitHub: https://github.com/Xaaby/computer-use-automation
Local path: C:\Users\AbhishekkumarYadav\Documents\computer-use-automation\

## Non-Negotiable Rules (Violating Any of These Breaks the Build)
1. ALWAYS use `asyncio.WindowsProactorEventLoopPolicy()` in EVERY async entrypoint — Playwright requires Proactor on Windows
2. ALWAYS use `pathlib.Path` for file paths. NEVER string concatenation with / or \
3. NEVER use WAL mode for SQLite. Use default DELETE journal mode
4. NEVER add `data-testid` attributes to any target_app HTML template — zero, none, ever
5. NEVER use `page.accessibility.snapshot()` — ALWAYS use `page.aria_snapshot(mode="ai")`
6. NEVER put credentials, tokens, API keys, or PII in any artifact, log, or evidence file
7. NEVER call any browser action without going through `policy/engine.py` check FIRST
8. NEVER retry a step with `risk_level: "irreversible_commit"` — always escalate immediately
9. Flask ALWAYS runs with `use_reloader=False, debug=False` — no exceptions
10. All capability artifacts MUST validate against Pydantic models in `capability/schema.py`
11. NEVER use the Anthropic SDK directly — use AWS Bedrock via boto3 `client.converse()`
12. uvicorn ALWAYS runs with `reload=False` on Windows — use `await server.serve()` pattern
13. ALWAYS redact PII patterns before writing to any log or evidence file
14. NEVER store element refs (e.g. `e17`) in capability artifacts — refs are ephemeral, only valid for the current snapshot session
15. ALWAYS call `locator.normalize()` after resolving an ephemeral ref to get a durable locator
16. FastAPI MUST run via `asyncio.create_task(server.serve())` — NEVER via `uvicorn.run()` inside an async context

## AWS Bedrock Configuration
```python
import boto3, os
from dotenv import load_dotenv
load_dotenv()

bedrock = boto3.client(
    "bedrock-runtime",
    region_name=os.environ["AWS_REGION"],
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
)
MODEL_ID = os.environ["BEDROCK_MODEL_ID"]  # us.anthropic.claude-sonnet-4-20250514-v1:0
```
Use `bedrock.converse()` NOT `invoke_model()`. Tool format uses `toolSpec`/`inputSchema.json`.
See PHASE_3_INSTRUCTIONS.md for exact Bedrock message format.

## ARIA Snapshot Facts (Confirmed by D1/D2 Research)
- `page.aria_snapshot(mode="ai")` — includes element refs AND iframe contents in one call
- `page.aria_snapshot()` (default, no mode) — does NOT include iframe contents, does NOT include refs
- Element refs like `[ref=e17]` are EPHEMERAL — valid only immediately after the snapshot that produced them
- Unlabeled table cells appear as `cell: "text content"` — the cell text becomes the accessible name
- `aria-label="Member ID"` on an input → accessible name is "Member ID" → `get_by_role("textbox", name="Member ID")` works
- iframe contents are included when using `mode="ai"` — no separate frame snapshot needed for agent

## Ref → Durable Locator Algorithm (Confirmed D7 Research)
```python
# Step 1: Snapshot with refs
snapshot = await page.aria_snapshot(mode="ai")

# Step 2: Claude returns ref (e.g. "e17") — resolve IMMEDIATELY before any navigation
ephemeral = page.locator("aria-ref=e17")
assert await ephemeral.count() == 1, "Ref resolved to 0 or >1 elements"

# Step 3: Normalize to durable locator (Playwright ≥1.49)
durable = ephemeral.normalize()
assert await durable.count() == 1, "Durable locator not unique"

# Step 4: Store in artifact as locator candidates
# NEVER store "e17" — store the durable locator string
```
`normalize()` follows Playwright best practices: prefers role+name, label, text over CSS structure.

## FastAPI + asyncio Architecture (Confirmed D3 Research)
```python
async def main():
    controller = EscalationController()
    app = create_app(controller)
    config = uvicorn.Config(app, host="0.0.0.0", port=8765, reload=False, log_level="warning")
    server = uvicorn.Server(config)
    
    await asyncio.gather(
        server.serve(),        # FastAPI shares the same event loop
        run_executor(controller),  # executor awaits controller.gate.wait()
    )

asyncio.run(main())  # ONE asyncio.run() at the top
```
Both share the same event loop → `gate.set()` in FastAPI handler wakes `gate.wait()` in executor.

## Source of Truth for Data Types
`capability/schema.py` defines ALL Pydantic models.
Import from there everywhere. Do NOT define duplicate types elsewhere.

## Module Ownership
- `capability/schema.py` — all data models
- `policy/engine.py` — all allowlist enforcement
- `escalation/controller.py` — all pause/resume state
- `surfaces/playwright_web.py` — all browser interaction
- `policy/redactor.py` — all PII redaction
- `evidence/` directory — all log files

## Locator Priority Order in Artifacts (Confirmed D7 Research)
1. Role + accessible name: `get_by_role("textbox", name="Member ID", exact=True)`
2. Label: `get_by_label("Member ID", exact=True)`
3. Placeholder: `get_by_placeholder("Enter member ID")`
4. Visible text: `get_by_text("Search", exact=True)`
5. CSS (stable attribute only): `input[aria-label='Member ID']`
6. Structural/nth: last resort, document why in artifact

Strict mode: if count != 1 → hard_failure. Never pick one from many.

## Error Taxonomy
- `business_outcome`: valid terminal result (MEMBER_NOT_FOUND) → return to caller
- `recoverable`: transient condition → retry within declared budget
- `hard_failure`: unrecoverable → stop, capture evidence, return failure detail
- `indeterminate_commit`: irreversible action timed out → NEVER retry, escalate immediately

## Build Sequence (Phases)
Phase 1: capability/schema.py + target_app/ + policy/ + tests/
Phase 2: surfaces/playwright_web.py + agent/tools.py + agent/prompts.py
Phase 3: agent/loop.py + agent/compiler.py
Phase 4: replay/executor.py + replay/conditions.py + replay/outcomes.py
Phase 5: escalation/controller.py + escalation/api.py + escalation/recorder.py
Phase 6: Stretch A (capability API) + Stretch B (stability runner)
Phase 7: REPORT.md + README.md + evidence generation

## Check Current Phase
Read PHASE_STATUS.md before starting work.
Update PHASE_STATUS.md after completing each phase.

## Windows-Specific (All Required)
- `asyncio.WindowsProactorEventLoopPolicy()` in every async entrypoint
- `pathlib.Path` for all file paths
- SQLite: `PRAGMA journal_mode = DELETE`
- Flask: `use_reloader=False, debug=False`
- uvicorn: `reload=False`, use `await server.serve()` pattern
- Evidence paths: keep shallow (max 3 levels deep) to avoid Windows 260-char path limit

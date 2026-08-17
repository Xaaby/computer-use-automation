# Phase 5 Instructions — Human Escalation
## Read RULES.md first. Phases 1-4 must be complete.

## Key Research Findings That Shape This Phase (D3, D6)

**D3 — FastAPI + asyncio.Event pattern (CONFIRMED AND UPDATED):**
Use `asyncio.create_task(server.serve())` inside a single `asyncio.run(main())`.
Do NOT use `uvicorn.run()` inside an async context — it tries to create its own loop.
Both the executor and FastAPI run as tasks in the same event loop.
`gate.set()` in the FastAPI handler wakes `gate.wait()` in the executor — they share the loop.

**D6 — Human action recorder (CONFIRMED with working code):**
- `context.add_init_script()` persists across ALL page navigations automatically — no reinjection needed
- `context.expose_binding()` exposes function in every frame of every page in the context
- Never capture element values — only capture field identity (aria_label, role, tag)
- Run `redact_log_entry()` on Python side AFTER client-side masking (double defense)

---

## What to Build

```
escalation/
    __init__.py
    controller.py    ← asyncio.Event gate + owner state machine
    api.py           ← FastAPI operator console (port 8765)
    recorder.py      ← human action capture via add_init_script
tests/
    test_escalation.py
```

---

## 1. escalation/controller.py

```python
"""
escalation/controller.py
Controls who operates the browser at any given moment.
One asyncio.Event gate: open = automation running, closed = human in control.

State machine:
AUTOMATION_CONTROL
    ↓ (stuck / risky / retry exhausted)
PAUSING
    ↓ (evidence saved, intervention request written)
WAITING_FOR_OPERATOR
    ↓ (operator GETs /  and clicks Accept)
HUMAN_CONTROL
    ↓ (operator clicks Resume → POST /resume)
RESYNCING
    ↓ (checkpoint verified)
AUTOMATION_CONTROL
    OR → TERMINAL (if checkpoint fails after resume)
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from capability.schema import ControlOwner


class EscalationController:
    def __init__(self, evidence_dir: Path):
        self._gate = asyncio.Event()
        self._gate.set()              # Starts open — automation running
        self._owner = ControlOwner.AUTOMATION
        self._current_request: dict | None = None
        self._approval_token: str | None = None
        self._human_action_log: list[dict] = []
        self._evidence_dir = evidence_dir
        self._abort_requested = False

    @property
    def owner(self) -> ControlOwner:
        return self._owner

    @property
    def approval_token(self) -> str | None:
        return self._approval_token

    @property
    def current_request(self) -> dict | None:
        return self._current_request

    @property
    def abort_requested(self) -> bool:
        return self._abort_requested

    async def pause_for_human(
        self,
        run_id: str,
        capability_id: str,
        goal: str,
        current_step_id: str | None,
        current_step_seq: int,
        reason: str,
        current_url: str,
        screenshot_b64: str | None = None,
        aria_snapshot: str | None = None,
    ) -> None:
        """
        Pause automation and transfer control to human.
        This coroutine BLOCKS until human calls resume() or abort().
        Same browser window stays open — human interacts directly with it.
        """
        self._owner = ControlOwner.HUMAN
        self._gate.clear()           # Block executor at next gate.wait()

        request_id = str(uuid4())
        self._current_request = {
            "request_id": request_id,
            "run_id": run_id,
            "capability_id": capability_id,
            "goal": goal,
            "current_step_id": current_step_id,
            "current_step_seq": current_step_seq,
            "reason": reason,
            "current_url": current_url,
            "screenshot_b64": screenshot_b64,
            "aria_snapshot": aria_snapshot,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "status": "pending",
        }

        # Write intervention request to disk
        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        req_path = self._evidence_dir / f"intervention_{request_id}.json"
        # Don't write screenshot_b64 to disk — too large, keep in memory only
        disk_req = {k: v for k, v in self._current_request.items() if k != "screenshot_b64"}
        req_path.write_text(json.dumps(disk_req, indent=2), encoding="utf-8")

        # Print clear instructions to terminal
        print(f"\n{'='*60}")
        print(f"ESCALATION TRIGGERED")
        print(f"Reason: {reason}")
        print(f"Step: {current_step_seq} ({current_step_id})")
        print(f"URL: {current_url}")
        print(f"\nOperator console: http://localhost:8765")
        print(f"The browser is now under your control.")
        print(f"When done: POST http://localhost:8765/resume")
        print(f"{'='*60}\n")

        # Block until resume() or abort() is called
        await self._gate.wait()

    def accept(self) -> str:
        """Human accepted the intervention. Returns approval token."""
        if self._current_request:
            self._current_request["status"] = "accepted"
        self._approval_token = str(uuid4())
        if self._current_request:
            self._current_request["approval_token"] = self._approval_token
        return self._approval_token

    def resume(self) -> None:
        """Human signals they are done. Automation resumes."""
        if self._current_request:
            self._current_request["status"] = "completed"
        self._owner = ControlOwner.AUTOMATION
        self._gate.set()             # Unblock executor

    def abort(self) -> None:
        """Human aborts the run. Executor checks this flag and stops."""
        self._abort_requested = True
        self._owner = ControlOwner.AUTOMATION
        self._gate.set()             # Unblock so executor can check abort flag

    def add_human_action(self, action: dict) -> None:
        """Record a human action during their session (PII already masked by recorder)."""
        from policy.redactor import redact_log_entry
        self._human_action_log.append(redact_log_entry(action))

    def get_human_actions(self) -> list[dict]:
        return list(self._human_action_log)
```

---

## 2. escalation/recorder.py — D6 Confirmed Working Implementation

```python
"""
escalation/recorder.py
Captures human actions during escalation via Playwright's add_init_script + expose_binding.

D6 research confirmed:
- add_init_script persists across ALL page navigations automatically
- expose_binding exposes function in every frame of every page in the context
- NEVER capture element.value — only capture field identity

Two-layer PII protection:
1. Client-side: JS never reads element.value
2. Server-side: redact_log_entry() applied before writing to disk
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext
from escalation.controller import EscalationController


# D6 confirmed: this script is injected into every page/frame on navigation automatically
RECORDER_SCRIPT = r"""
(() => {
    // Guard against double-injection
    if (window.__humanRecorderInstalled) return;
    window.__humanRecorderInstalled = true;

    function getLabel(element) {
        // Priority: aria-label > aria-labelledby > associated <label> > name attr > placeholder
        const ariaLabel = element.getAttribute('aria-label');
        if (ariaLabel) return ariaLabel;

        const labelledBy = element.getAttribute('aria-labelledby');
        if (labelledBy) {
            const labelEl = document.getElementById(labelledBy);
            if (labelEl) return labelEl.textContent.trim();
        }

        if ('labels' in element && element.labels && element.labels.length > 0) {
            return element.labels[0].textContent.trim();
        }

        return element.getAttribute('name') || element.getAttribute('placeholder') || 'unknown';
    }

    function getDescriptor(element) {
        if (!(element instanceof Element)) return null;
        return {
            tag: element.tagName.toLowerCase(),
            role: element.getAttribute('role') || null,
            label: getLabel(element),
            input_type: element instanceof HTMLInputElement ? element.type : null,
            // INTENTIONALLY NOT CAPTURED: element.value
            // Never capture what the human typed — only WHERE they typed
        };
    }

    function send(type, target, extra) {
        if (typeof window.__pwRecordEvent !== 'function') return;
        const payload = {
            type,
            timestamp_ms: Date.now(),
            target: getDescriptor(target),
            ...extra,
        };
        void window.__pwRecordEvent(payload).catch(() => {});
    }

    document.addEventListener('click', (e) => {
        let el = e.target;
        if (el instanceof Element) {
            el = el.closest('button,a,input,select,textarea,[role]') || el;
        }
        send('click', el, {});
    }, true);

    document.addEventListener('input', (e) => {
        // Field identity only — NO value
        send('input', e.target, {});
    }, true);

    document.addEventListener('change', (e) => {
        send('change', e.target, {});
    }, true);

    document.addEventListener('submit', (e) => {
        const form = e.target;
        send('submit', form, {
            form_action: form instanceof HTMLFormElement ? form.action : null,
            form_method: form instanceof HTMLFormElement ? form.method : null,
        });
    }, true);
})();
"""


async def attach_recorder(
    context: BrowserContext,
    controller: EscalationController,
    output_path: Path,
) -> None:
    """
    Attach human action recorder to browser context.

    D6 confirmed: add_init_script persists automatically — no reinjection needed.
    expose_binding is available in every frame of every page in the context.

    Call this ONCE when creating the BrowserContext.
    It continues recording for the entire session (including during automation).
    During non-escalation periods, recordings are benign (just navigation/clicks).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_lock = asyncio.Lock()

    def append_line(line: str) -> None:
        with output_path.open("a", encoding="utf-8") as f:
            f.write(line)

    async def record_event(source, payload) -> None:
        from policy.redactor import redact_log_entry

        record = {
            "page_url": source["page"].url,
            "frame_url": source["frame"].url,
            "ts": datetime.utcnow().isoformat() + "Z",
            **payload,
        }

        # Server-side redaction as second layer of defense
        record = redact_log_entry(record)
        controller.add_human_action(record)

        line = json.dumps(record, ensure_ascii=False) + "\n"
        async with write_lock:
            await asyncio.to_thread(append_line, line)

    # Install binding FIRST, then init script
    await context.expose_binding("__pwRecordEvent", record_event)
    await context.add_init_script(script=RECORDER_SCRIPT)
```

---

## 3. escalation/api.py — FastAPI Operator Console

```python
"""
escalation/api.py
FastAPI operator console for human-in-the-loop escalation.
Port: 8765
Runs in the SAME asyncio event loop as the executor via asyncio.create_task(server.serve()).

D3 research confirmed: use asyncio.create_task(server.serve()) NOT uvicorn.run().
This ensures gate.set() in FastAPI handlers wakes gate.wait() in the executor.

CRITICAL on Windows: uvicorn Config must have reload=False.
"""
import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse


def create_app(controller) -> FastAPI:
    """
    Create the FastAPI app. Controller is injected at creation time.
    Both this app and the executor share the same controller instance.
    """
    app = FastAPI(title="Computer-Use Automation — Operator Console")

    @app.get("/", response_class=HTMLResponse)
    async def operator_console():
        """Minimal HTML operator interface."""
        req = controller.current_request
        if req is None:
            return HTMLResponse("""
<html><body style="font-family:monospace;background:#111;color:#ccc;padding:20px">
<h2>✓ No Active Intervention</h2>
<p>Automation is running normally.</p>
<p><a href="/status" style="color:#7cf">/status</a></p>
</body></html>""")

        screenshot_img = ""
        if req.get("screenshot_b64"):
            screenshot_img = f'<img src="data:image/png;base64,{req["screenshot_b64"]}" style="max-width:100%;border:1px solid #444;margin:10px 0"/>'

        return HTMLResponse(f"""
<!DOCTYPE html>
<html>
<head>
    <title>Operator Console — Intervention Required</title>
    <style>
        body {{font-family: monospace; padding: 20px; background: #111; color: #e0e0e0;}}
        .alert {{background: #3a1010; border: 2px solid #cc4444; padding: 15px; margin: 10px 0; border-radius: 4px;}}
        .info {{background: #101830; padding: 15px; margin: 10px 0; border-radius: 4px;}}
        pre {{background: #080808; padding: 10px; overflow-x: auto; font-size: 11px; border-radius: 4px;}}
        button {{padding: 12px 24px; font-size: 16px; cursor: pointer; border: none; border-radius: 4px; margin: 5px;}}
        .btn-accept {{background: #1a5c2a; color: white;}}
        .btn-resume {{background: #1a4060; color: white;}}
        .btn-abort {{background: #5c1a1a; color: white;}}
        #status-msg {{margin-top: 15px; color: #aaa; font-size: 14px;}}
    </style>
</head>
<body>
    <h1>⚠ Human Intervention Required</h1>
    <div class="alert">
        <strong>Reason:</strong> {req.get('reason', 'Unknown')}<br>
        <strong>Capability:</strong> {req.get('capability_id', 'Unknown')}<br>
        <strong>Step:</strong> {req.get('current_step_seq')} ({req.get('current_step_id')})<br>
        <strong>URL:</strong> {req.get('current_url')}
    </div>
    <div class="info"><strong>Goal:</strong> {req.get('goal')}</div>
    {screenshot_img}
    <h3>Instructions</h3>
    <p>The browser window is under your control. Complete the required action, then click Resume.</p>
    <button class="btn-accept" onclick="post('/accept')">Accept Control</button>
    <button class="btn-resume" onclick="post('/resume')">Resume Automation</button>
    <button class="btn-abort" onclick="post('/abort')">Abort Run</button>
    <div id="status-msg"></div>
    <script>
        async function post(path) {{
            const r = await fetch(path, {{method: 'POST'}});
            const d = await r.json();
            document.getElementById('status-msg').textContent = JSON.stringify(d);
        }}
    </script>
</body>
</html>""")

    @app.get("/status")
    async def get_status():
        return {
            "control_owner": controller.owner.value,
            "has_active_intervention": controller.current_request is not None,
            "intervention_status": (
                controller.current_request.get("status")
                if controller.current_request else None
            ),
            "abort_requested": controller.abort_requested,
        }

    @app.post("/accept")
    async def accept():
        token = controller.accept()
        return {"status": "accepted", "approval_token": token}

    @app.post("/resume")
    async def resume():
        controller.resume()
        return {"status": "resumed", "control_owner": "automation"}

    @app.post("/abort")
    async def abort():
        controller.abort()
        return {"status": "aborted"}

    # ─── Stretch A: Capability Invocation API ─────────────────────────────────

    @app.get("/capabilities")
    async def list_capabilities():
        """List all saved capability artifacts."""
        cap_dir = Path("capabilities")
        if not cap_dir.exists():
            return []
        caps = []
        for f in cap_dir.glob("*.capability.json"):
            try:
                data = json.loads(f.read_text())
                caps.append({
                    "id": data.get("name"),
                    "version": data.get("version"),
                    "description": data.get("description"),
                    "status": data.get("provenance", {}).get("status"),
                    "risk_class": data.get("policy", {}).get("risk_class"),
                    "inputs": [i["name"] for i in data.get("inputs", [])],
                    "outputs": [o["name"] for o in data.get("outputs", [])],
                })
            except Exception:
                continue
        return caps

    @app.post("/capabilities/{capability_name}/invoke")
    async def invoke_capability(capability_name: str, body: dict):
        """
        Stretch A: Invoke a capability by name with typed inputs.
        Runs deterministic replay — no LLM involved.
        """
        import asyncio
        from capability.schema import CapabilityArtifact
        from replay.executor import ReplayExecutor

        cap_file = Path(f"capabilities/{capability_name}.capability.json")
        if not cap_file.exists():
            return JSONResponse(
                {"error": f"Capability '{capability_name}' not found"},
                status_code=404,
            )

        try:
            artifact = CapabilityArtifact.model_validate(
                json.loads(cap_file.read_text())
            )
        except Exception as e:
            return JSONResponse({"error": f"Invalid artifact: {e}"}, status_code=422)

        inputs = body.get("inputs", {})
        for inp in artifact.inputs:
            if inp.required and inp.name not in inputs:
                return JSONResponse(
                    {"error": f"Required input '{inp.name}' missing"},
                    status_code=400,
                )

        result = await ReplayExecutor(
            artifact=artifact,
            params=inputs,
            headless=True,
        ).run()

        return result.__dict__

    return app


async def run_operator_console(controller, host: str = "0.0.0.0", port: int = 8765):
    """
    Start the operator console as a task in the current event loop.
    D3 confirmed: use await server.serve() NOT uvicorn.run() when inside asyncio.run().

    Usage:
        asyncio.create_task(run_operator_console(controller))
    """
    app = create_app(controller)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        reload=False,           # NEVER reload on Windows
        log_level="warning",
    )
    server = uvicorn.Server(config)
    await server.serve()
```

---

## 4. How to Integrate Everything in main entrypoints

```python
# Pattern for any script that runs both executor and FastAPI (D3 confirmed):
import asyncio, sys
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from escalation.controller import EscalationController
from escalation.api import run_operator_console
from pathlib import Path

async def main(goal, entry_point, params):
    evidence_dir = Path("evidence") / "runs"
    controller = EscalationController(evidence_dir=evidence_dir)

    # Run both tasks in same event loop
    # D3: asyncio.create_task shares the loop — gate.set() wakes gate.wait()
    console_task = asyncio.create_task(
        run_operator_console(controller),
        name="operator-console",
    )

    try:
        # Run your executor/agent here, passing controller
        result = await run_replay_or_discovery(controller, goal, entry_point, params)
    finally:
        console_task.cancel()
        try:
            await console_task
        except asyncio.CancelledError:
            pass

asyncio.run(main(goal, entry_point, params))
```

---

## 5. tests/test_escalation.py — Tests to Implement

```python
"""
tests/test_escalation.py
Test the escalation mechanism.
Key: these tests verify the asyncio.Event gate works correctly — the hardest part.
"""
import asyncio, sys
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import pytest
from pathlib import Path
from escalation.controller import EscalationController
```

Tests:
1. Controller starts with `owner == AUTOMATION`, gate open (is_set() == True)
2. After `pause_for_human()` starts: gate is closed (is_set() == False), owner == HUMAN
3. After `resume()`: gate is open, owner == AUTOMATION
4. After `abort()`: gate is open, abort_requested == True
5. `accept()` returns a non-empty UUID token string
6. Gate correctly blocks: a coroutine awaiting `gate.wait()` unblocks after `resume()`
7. Human actions added via `add_human_action()` are stored and redacted
8. `get_human_actions()` returns a copy (not a reference to internal list)
9. Intervention request JSON is written to evidence_dir on `pause_for_human()`
10. `abort_requested` is False before abort(), True after

**Critical test (gate blocking):**
```python
async def test_gate_blocks_and_unblocks():
    controller = EscalationController(evidence_dir=Path("evidence/test"))
    results = []

    async def worker():
        await controller._gate.wait()
        results.append("unblocked")

    # Pause
    controller._gate.clear()
    controller._owner = ControlOwner.HUMAN

    task = asyncio.create_task(worker())
    await asyncio.sleep(0.05)  # Worker should be blocked
    assert results == []  # Still blocked

    controller.resume()  # Unblock
    await asyncio.sleep(0.05)
    assert results == ["unblocked"]  # Now unblocked
```

---

## Checkpoint Verification
```bash
python -m pytest tests/test_escalation.py -v

# FastAPI + executor event loop test
python -c "
import asyncio, sys
asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
import uvicorn
from fastapi import FastAPI
from escalation.controller import EscalationController
from escalation.api import run_operator_console
from pathlib import Path

async def test():
    ctrl = EscalationController(evidence_dir=Path('evidence/test'))
    task = asyncio.create_task(run_operator_console(ctrl))
    await asyncio.sleep(1)
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.get('http://localhost:8765/status')
        print('Status:', r.json())
        r2 = await client.post('http://localhost:8765/resume')
        print('Resume:', r2.json())
        print('Gate open:', ctrl._gate.is_set())
    task.cancel()

asyncio.run(test())
"
```
Update PHASE_STATUS.md after completion.

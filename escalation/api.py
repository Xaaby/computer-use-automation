"""
escalation/api.py
FastAPI operator console for human-in-the-loop escalation.
Port: 8765
Runs in the SAME asyncio event loop as the executor via asyncio.create_task(server.serve()).

D3 research confirmed: use asyncio.create_task(server.serve()) NOT uvicorn.run().
This ensures gate.set() in FastAPI handlers wakes gate.wait() in the executor.

CRITICAL on Windows: uvicorn Config must have reload=False.
"""
from __future__ import annotations

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
            screenshot_img = (
                f'<img src="data:image/png;base64,{req["screenshot_b64"]}" '
                'style="max-width:100%;border:1px solid #444;margin:10px 0"/>'
            )

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
                if controller.current_request
                else None
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
                data = json.loads(f.read_text(encoding="utf-8"))
                caps.append(
                    {
                        "id": data.get("name"),
                        "version": data.get("version"),
                        "description": data.get("description"),
                        "status": data.get("provenance", {}).get("status"),
                        "risk_class": data.get("policy", {}).get("risk_class"),
                        "inputs": [i["name"] for i in data.get("inputs", [])],
                        "outputs": [o["name"] for o in data.get("outputs", [])],
                    }
                )
            except Exception:
                continue
        return caps

    @app.post("/capabilities/{capability_name}/invoke")
    async def invoke_capability(capability_name: str, body: dict):
        """
        Stretch A: Invoke a capability by name with typed inputs.
        Runs deterministic replay — no LLM involved.
        """
        from capability.schema import CapabilityArtifact
        from replay.executor import ReplayExecutor, outcome_to_dict

        cap_file = Path("capabilities") / f"{capability_name}.capability.json"
        if not cap_file.exists():
            return JSONResponse(
                {"error": f"Capability '{capability_name}' not found"},
                status_code=404,
            )

        try:
            artifact = CapabilityArtifact.model_validate(
                json.loads(cap_file.read_text(encoding="utf-8"))
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
            escalation_controller=controller,
        ).run()

        return outcome_to_dict(result)

    return app


async def run_operator_console(
    controller, host: str = "0.0.0.0", port: int = 8765
):
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
        reload=False,  # NEVER reload on Windows
        log_level="warning",
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    import asyncio
    import os
    import sys

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    from escalation.controller import EscalationController

    async def _main():
        port = int(os.environ.get("OPERATOR_CONSOLE_PORT", "8765"))
        ctrl = EscalationController(evidence_dir=Path("evidence") / "runs")
        print(f"Operator console + capability API on http://127.0.0.1:{port}")
        await run_operator_console(ctrl, host="127.0.0.1", port=port)

    asyncio.run(_main())

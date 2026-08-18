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

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

# session_manager exists only after streamable_http_app() — call it at module
# level after tools are registered, then enter it from the FastAPI lifespan.
mcp_server = MCPServer("computer-use-automation")


@mcp_server.tool(annotations=ToolAnnotations(read_only_hint=True))
async def list_capabilities() -> list[dict]:
    """List all saved capability artifacts available for replay."""
    from capability.api import list_capability_summaries

    return list_capability_summaries()


@mcp_server.tool()
async def invoke_capability(capability_name: str, inputs: dict[str, str]) -> dict:
    """
    Invoke a saved capability by name with typed inputs.
    Do NOT expose resume/abort/accept — humans own the gate.
    """
    from capability.api import invoke_capability as invoke_cap

    try:
        return await invoke_cap(capability_name, inputs, ws_manager=ws_manager)
    except FileNotFoundError as e:
        return {"status": "error", "detail": str(e)}
    except ValueError as e:
        return {"status": "error", "detail": str(e)}


mcp_http_app = mcp_server.streamable_http_app(streamable_http_path="/")


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: str):
        dead: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = ConnectionManager()


async def broadcast_step_event(
    manager: ConnectionManager,
    step_num: int,
    status: str,
    action: str,
    locator: dict,
    error: str | None = None,
    reason: str | None = None,
):
    event = {
        "type": "step_event",
        "step": step_num,
        "status": status,
        "action": action,
        "locator": locator,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "reason": reason,
    }
    await manager.broadcast(json.dumps(event))


async def screenshot_loop(page, manager: ConnectionManager, done: asyncio.Event):
    """Runs as asyncio.create_task() alongside the executor. ~1fps."""
    while not done.is_set():
        try:
            img_bytes = await page.screenshot()
            b64 = base64.b64encode(img_bytes).decode()
            event = {
                "type": "screenshot",
                "image": b64,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await manager.broadcast(json.dumps(event))
        except Exception:
            pass
        await asyncio.sleep(1.0)


def create_app(controller) -> FastAPI:
    """
    Create the FastAPI app. Controller is injected at creation time.
    Both this app and the executor share the same controller instance.
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp_server.session_manager.run():
            yield

    app = FastAPI(title="Computer-Use Automation — Operator Console", lifespan=lifespan)
    app.mount("/mcp", mcp_http_app)

    @app.get("/", response_class=HTMLResponse)
    async def console_root():
        html_path = Path(__file__).parent / "console.html"
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

    @app.websocket("/ws/steps")
    async def websocket_steps(ws: WebSocket):
        await ws_manager.connect(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            ws_manager.disconnect(ws)

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

    @app.get("/capabilities")
    async def list_capabilities():
        """List all saved capability artifacts."""
        from capability.api import list_capability_summaries

        return list_capability_summaries()

    @app.post("/capabilities/{capability_name}/invoke")
    async def invoke_capability_route(capability_name: str, body: dict):
        """
        Invoke a capability by name with typed inputs.
        Runs deterministic replay in-process with live WebSocket streaming.
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
            ws_manager=ws_manager,
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
        reload=False,
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

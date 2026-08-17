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
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from capability.schema import ControlOwner


class EscalationController:
    def __init__(self, evidence_dir: Path | None = None):
        self._gate = asyncio.Event()
        self._gate.set()  # Starts open — automation running
        self._owner = ControlOwner.AUTOMATION
        self._current_request: dict | None = None
        self._approval_token: str | None = None
        self._human_action_log: list[dict] = []
        self._evidence_dir = evidence_dir or Path("evidence") / "runs"
        self._abort_requested = False

    # Compatibility aliases used by ReplayExecutor (Phase 4)
    @property
    def gate(self):
        return self._gate

    @property
    def state(self):
        return self._owner

    @property
    def owner(self) -> ControlOwner:
        return self._owner

    @property
    def approval_token(self) -> str | None:
        return self._approval_token

    @approval_token.setter
    def approval_token(self, value: str | None) -> None:
        self._approval_token = value

    @property
    def current_request(self) -> dict | None:
        return self._current_request

    @property
    def abort_requested(self) -> bool:
        return self._abort_requested

    def has_approval(self) -> bool:
        return bool(self._approval_token)

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
        self._gate.clear()  # Block executor at next gate.wait()

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
        disk_req = {
            k: v for k, v in self._current_request.items() if k != "screenshot_b64"
        }
        req_path.write_text(json.dumps(disk_req, indent=2), encoding="utf-8")

        print(f"\n{'=' * 60}")
        print("ESCALATION TRIGGERED")
        print(f"Reason: {reason}")
        print(f"Step: {current_step_seq} ({current_step_id})")
        print(f"URL: {current_url}")
        print("\nOperator console: http://localhost:8765")
        print("The browser is now under your control.")
        print("When done: POST http://localhost:8765/resume")
        print(f"{'=' * 60}\n")

        # Block until resume() or abort() is called
        await self._gate.wait()

    async def pause(self, request) -> None:
        """Thin wrapper for ReplayExecutor irreversible_commit path."""
        await self.pause_for_human(
            run_id=getattr(request, "run_id", ""),
            capability_id="",
            goal="",
            current_step_id=getattr(request, "step_id", None),
            current_step_seq=0,
            reason=getattr(request, "reason", "escalation"),
            current_url="",
        )

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
        self._gate.set()  # Unblock executor

    async def resume_async(self, approval_token: str | None = None) -> None:
        if approval_token:
            self._approval_token = approval_token
        self.resume()

    def abort(self) -> None:
        """Human aborts the run. Executor checks this flag and stops."""
        self._abort_requested = True
        self._owner = ControlOwner.AUTOMATION
        self._gate.set()  # Unblock so executor can check abort flag

    def add_human_action(self, action: dict) -> None:
        """Record a human action during their session (PII already masked by recorder)."""
        from policy.redactor import redact_log_entry

        self._human_action_log.append(redact_log_entry(action))

    def get_human_actions(self) -> list[dict]:
        return list(self._human_action_log)

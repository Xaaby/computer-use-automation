"""
tests/test_escalation.py
Test the escalation mechanism.
Key: these tests verify the asyncio.Event gate works correctly — the hardest part.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from capability.schema import ControlOwner
from escalation.controller import EscalationController


@pytest.fixture
def controller(tmp_path: Path) -> EscalationController:
    return EscalationController(evidence_dir=tmp_path)


def test_starts_automation_gate_open(controller: EscalationController):
    assert controller.owner == ControlOwner.AUTOMATION
    assert controller.gate.is_set() is True
    assert controller.abort_requested is False


@pytest.mark.asyncio
async def test_pause_closes_gate_and_sets_human(controller: EscalationController):
    async def pauser():
        await controller.pause_for_human(
            run_id="r1",
            capability_id="cap",
            goal="test",
            current_step_id="step_1",
            current_step_seq=1,
            reason="stuck",
            current_url="http://localhost:5000/members/search",
        )

    task = asyncio.create_task(pauser())
    await asyncio.sleep(0.05)
    assert controller.gate.is_set() is False
    assert controller.owner == ControlOwner.HUMAN
    controller.resume()
    await task


@pytest.mark.asyncio
async def test_resume_opens_gate(controller: EscalationController):
    controller.gate.clear()
    controller._owner = ControlOwner.HUMAN
    controller.resume()
    assert controller.gate.is_set() is True
    assert controller.owner == ControlOwner.AUTOMATION


def test_abort_sets_flag(controller: EscalationController):
    assert controller.abort_requested is False
    controller.gate.clear()
    controller.abort()
    assert controller.abort_requested is True
    assert controller.gate.is_set() is True


def test_accept_returns_token(controller: EscalationController):
    controller._current_request = {"status": "pending"}
    token = controller.accept()
    assert isinstance(token, str) and len(token) > 0
    assert controller.approval_token == token


@pytest.mark.asyncio
async def test_gate_blocks_and_unblocks(controller: EscalationController):
    results = []

    async def worker():
        await controller.gate.wait()
        results.append("unblocked")

    controller.gate.clear()
    controller._owner = ControlOwner.HUMAN

    task = asyncio.create_task(worker())
    await asyncio.sleep(0.05)
    assert results == []

    controller.resume()
    await asyncio.sleep(0.05)
    assert results == ["unblocked"]
    await task


def test_human_actions_stored_and_redacted(controller: EscalationController):
    controller.add_human_action(
        {"type": "input", "target": {"label": "SSN"}, "note": "123-45-6789"}
    )
    actions = controller.get_human_actions()
    assert len(actions) == 1
    assert "[SSN-REDACTED]" in actions[0].get("note", "")


def test_get_human_actions_returns_copy(controller: EscalationController):
    controller.add_human_action({"type": "click", "target": {"label": "Search"}})
    a = controller.get_human_actions()
    b = controller.get_human_actions()
    assert a == b
    assert a is not b
    a.append({"extra": True})
    assert len(controller.get_human_actions()) == 1


@pytest.mark.asyncio
async def test_intervention_json_written(controller: EscalationController, tmp_path: Path):
    async def pauser():
        await controller.pause_for_human(
            run_id="r1",
            capability_id="cap",
            goal="g",
            current_step_id="s1",
            current_step_seq=1,
            reason="risky",
            current_url="http://localhost:5000/",
            screenshot_b64="AAAA",
        )

    task = asyncio.create_task(pauser())
    await asyncio.sleep(0.05)
    files = list(tmp_path.glob("intervention_*.json"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "screenshot_b64" not in text
    assert "risky" in text
    controller.resume()
    await task


@pytest.mark.asyncio
async def test_operator_console_status_and_resume(tmp_path: Path):
    """FastAPI shares the event loop; resume opens the gate."""
    import socket

    import httpx

    from escalation.api import run_operator_console

    # Bind an ephemeral port so this test does not collide with a live console.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    ctrl = EscalationController(evidence_dir=tmp_path)
    task = asyncio.create_task(
        run_operator_console(ctrl, host="127.0.0.1", port=port)
    )
    try:
        await asyncio.sleep(0.8)
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/status")
            assert r.status_code == 200
            assert r.json()["control_owner"] == "automation"
            ctrl.gate.clear()
            r2 = await client.post(f"http://127.0.0.1:{port}/resume")
            assert r2.json()["status"] == "resumed"
            assert ctrl.gate.is_set() is True
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, SystemExit):
            pass

"""
tests/test_replay.py
Test the replay executor against the mock Flask app.
Flask app must be running on port 5000 before these tests run.

Decision (MEMBER_NOT_FOUND id): Flask treats member IDs starting with '9' as
HTTP 403 (PERMISSION_DENIED). Phase 4 docs suggested 99999 for MEMBER_NOT_FOUND,
but that would 403. Use 88888 (unknown, non-9-prefix) for MEMBER_NOT_FOUND and
keep 90001 for PERMISSION_DENIED.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from capability.schema import (
    ActionType,
    CapabilityArtifact,
    RiskLevel,
    StepDefinition,
)
from replay.executor import ReplayExecutor
from replay.outcomes import (
    BusinessOutcome,
    HardFailure,
    IndeterminateCommit,
    ReplaySuccess,
)

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = ROOT / "capabilities" / "member.lookup_savings_balance.capability.json"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "example_capability.json"


def _load_artifact() -> CapabilityArtifact:
    path = ARTIFACT_PATH if ARTIFACT_PATH.exists() else FIXTURE_PATH
    return CapabilityArtifact.model_validate_json(path.read_text(encoding="utf-8"))


@pytest.fixture
def artifact() -> CapabilityArtifact:
    return _load_artifact()


@pytest.mark.asyncio
async def test_replay_success_member_10001(artifact: CapabilityArtifact):
    result = await ReplayExecutor(
        artifact=artifact,
        params={"member_id": "10001"},
        headless=True,
    ).run()
    assert isinstance(result, ReplaySuccess), result
    assert "savings_balance" in result.outputs
    assert "4821.50" in str(result.outputs["savings_balance"])


@pytest.mark.asyncio
async def test_replay_business_outcome_member_not_found(artifact: CapabilityArtifact):
    # Decision: use 88888 — not 99999 — see module docstring.
    result = await ReplayExecutor(
        artifact=artifact,
        params={"member_id": "88888"},
        headless=True,
    ).run()
    assert isinstance(result, BusinessOutcome), result
    assert result.code == "MEMBER_NOT_FOUND"


@pytest.mark.asyncio
async def test_replay_permission_denied_90001(artifact: CapabilityArtifact):
    result = await ReplayExecutor(
        artifact=artifact,
        params={"member_id": "90001"},
        headless=True,
    ).run()
    assert isinstance(result, HardFailure), result
    assert result.code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_replay_ambiguous_locator(artifact: CapabilityArtifact):
    """Inject a duplicate Search button to force AMBIGUOUS_LOCATOR."""
    from surfaces.playwright_web import PlaywrightWebSurface
    from policy.engine import PolicyEngine

    # Build a minimal one-step artifact that clicks Search
    data = artifact.model_dump(mode="json")
    click_step = next(s for s in data["steps"] if s["action"] == "click")
    data["steps"] = [
        {
            "id": "step_1",
            "description": "Navigate",
            "action": "navigate",
            "url": "http://localhost:5000/members/search",
            "risk_level": "safe",
            "preconditions": [],
            "postconditions": [],
        },
        click_step,
    ]
    mini = CapabilityArtifact.model_validate(data)

    surface, browser, context, pw_cm = await PlaywrightWebSurface.create(
        headless=True, policy_engine=PolicyEngine()
    )
    try:
        await surface.navigate("http://localhost:5000/members/search")
        await surface._page.evaluate(
            """() => {
                const b = document.createElement('button');
                b.textContent = 'Search';
                document.querySelector('main').appendChild(b);
            }"""
        )
        # Use surface resolve directly to assert ambiguous
        target = click_step["target"]
        # Prefer text strategy which will match both buttons
        target = {
            "frame_path": None,
            "expected_matches": 1,
            "candidates": [
                {"priority": 1, "strategy": "text", "text": "Search", "exact": True}
            ],
        }
        result = await surface.resolve_and_act(
            action_type="click",
            target_spec=target,
            current_url=surface._page.url,
        )
        assert result.error_type == "ambiguous_locator"
    finally:
        await context.close()
        await browser.close()
        await pw_cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_indeterminate_commit_never_retries(artifact: CapabilityArtifact):
    """Simulate irreversible timeout → IndeterminateCommit with no retries."""
    from unittest.mock import AsyncMock, patch

    from surfaces.base import ActionResult
    from surfaces.playwright_web import PlaywrightWebSurface

    data = artifact.model_dump(mode="json")
    data["steps"] = [
        {
            "id": "step_confirm",
            "description": "Confirm Transfer",
            "action": "click",
            "risk_level": "irreversible_commit",
            "target": {
                "frame_path": None,
                "expected_matches": 1,
                "candidates": [
                    {
                        "priority": 1,
                        "strategy": "role",
                        "role": "button",
                        "name": "Confirm Transfer",
                        "exact": True,
                    }
                ],
            },
            "preconditions": [],
            "postconditions": [],
        }
    ]
    mini = CapabilityArtifact.model_validate(data)

    executor = ReplayExecutor(
        artifact=mini, params={}, headless=True, bootstrap_session=False
    )
    executor._escalation.approval_token = "test-token"

    fake_result = ActionResult(
        success=False, error="Timeout 10000ms exceeded", error_type="timeout"
    )

    real_create = PlaywrightWebSurface.create

    async def fake_create(*args, **kwargs):
        surface, browser, context, pw = await real_create(headless=True)
        surface.resolve_and_act = AsyncMock(return_value=fake_result)
        surface.capture_evidence = AsyncMock(
            return_value=type(
                "E",
                (),
                {
                    "screenshot_path": "x.png",
                    "aria_snapshot_path": "x.yaml",
                    "trace_path": None,
                },
            )()
        )
        surface.start_tracing = AsyncMock()
        surface.stop_tracing = AsyncMock()
        surface.navigate = AsyncMock(return_value=ActionResult(success=True))
        return surface, browser, context, pw

    with patch(
        "replay.executor.PlaywrightWebSurface.create", side_effect=fake_create
    ):
        result = await executor.run()

    assert isinstance(result, IndeterminateCommit), result
    assert result.step_id == "step_confirm"


@pytest.mark.asyncio
async def test_precondition_fail(artifact: CapabilityArtifact):
    data = artifact.model_dump(mode="json")
    # Force a precondition that cannot match
    data["steps"] = [
        {
            "id": "step_1",
            "description": "fill requiring wrong route",
            "action": "fill",
            "value": "10001",
            "risk_level": "safe",
            "target": {
                "frame_path": None,
                "expected_matches": 1,
                "candidates": [
                    {
                        "priority": 1,
                        "strategy": "role",
                        "role": "textbox",
                        "name": "Member ID",
                        "exact": True,
                    }
                ],
            },
            "preconditions": [
                {"type": "route_matches", "pattern": "*/this-route-does-not-exist*"}
            ],
            "postconditions": [],
        }
    ]
    mini = CapabilityArtifact.model_validate(data)
    result = await ReplayExecutor(
        artifact=mini, params={}, headless=True
    ).run()
    assert isinstance(result, HardFailure), result
    assert result.code == "PRECONDITION_FAILED"

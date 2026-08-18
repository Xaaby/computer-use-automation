"""Tests for bounded assisted fallback."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from capability.schema import ArtifactStatus, RiskLevel, StepDefinition, ActionType
from policy.engine import PolicyViolationError
from replay.fallback import attempt_fallback


def make_step(risk_level=RiskLevel.SAFE, step_id="step_3"):
    return StepDefinition(
        id=step_id,
        description="click search",
        action=ActionType.CLICK,
        risk_level=risk_level,
    )


@pytest.mark.asyncio
async def test_fallback_not_attempted_for_irreversible():
    step = make_step(risk_level=RiskLevel.IRREVERSIBLE_COMMIT)
    result = await attempt_fallback(
        step=step,
        error_detail="timeout",
        live_aria="",
        surface=AsyncMock(),
        policy=MagicMock(),
        artifact_status=ArtifactStatus.APPROVED,
        run_id="test",
        evidence_dir=Path("/tmp/test_ev"),
        fallback_used=False,
    )
    assert result is None


@pytest.mark.asyncio
async def test_fallback_not_attempted_for_draft():
    step = make_step()
    result = await attempt_fallback(
        step=step,
        error_detail="timeout",
        live_aria="",
        surface=AsyncMock(),
        policy=MagicMock(),
        artifact_status=ArtifactStatus.DRAFT,
        run_id="test",
        evidence_dir=Path("/tmp/test_ev"),
        fallback_used=False,
    )
    assert result is None


@pytest.mark.asyncio
async def test_fallback_not_attempted_if_already_used():
    step = make_step()
    result = await attempt_fallback(
        step=step,
        error_detail="timeout",
        live_aria="",
        surface=AsyncMock(),
        policy=MagicMock(),
        artifact_status=ArtifactStatus.APPROVED,
        run_id="test",
        evidence_dir=Path("/tmp/test_ev"),
        fallback_used=True,
    )
    assert result is None


@pytest.mark.asyncio
async def test_fallback_policy_blocks_disallowed_action(tmp_path: Path):
    step = make_step()
    policy = MagicMock()
    policy.check_action.side_effect = PolicyViolationError("blocked", "navigate", "http://evil.com")
    surface = AsyncMock()
    surface._page = MagicMock()
    surface._page.url = "http://localhost:5000/members/search"
    with patch("replay.fallback.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": '{"action":"navigate","locator_strategy":"url","locator_value":"evil.com","value":""}'
                        }
                    ]
                }
            }
        }
        result = await attempt_fallback(
            step=step,
            error_detail="locator miss",
            live_aria="- role: button\n  name: Find",
            surface=surface,
            policy=policy,
            artifact_status=ArtifactStatus.APPROVED,
            run_id="test",
            evidence_dir=tmp_path,
            fallback_used=False,
        )
    assert result is not None
    assert result.succeeded is False
    assert result.corrective_locator == "policy_blocked"


@pytest.mark.asyncio
async def test_fallback_succeeds_on_corrected_locator(tmp_path: Path):
    step = make_step()
    policy = MagicMock()
    policy.check_action.return_value = MagicMock()
    surface = AsyncMock()
    surface._page = MagicMock()
    surface._page.url = "http://localhost:5000/members/search"
    surface.resolve_and_act = AsyncMock(return_value=MagicMock(success=True))
    with patch("replay.fallback.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": '{"action":"click","locator_strategy":"text","locator_value":"Find Member","value":""}'
                        }
                    ]
                }
            }
        }
        result = await attempt_fallback(
            step=step,
            error_detail="locator miss",
            live_aria="- role: button\n  name: Find Member",
            surface=surface,
            policy=policy,
            artifact_status=ArtifactStatus.APPROVED,
            run_id="test",
            evidence_dir=tmp_path,
            fallback_used=False,
        )
    assert result is not None
    assert result.succeeded is True
    assert "text:Find Member" in result.corrective_locator


@pytest.mark.asyncio
async def test_fallback_fails_gracefully(tmp_path: Path):
    step = make_step()
    policy = MagicMock()
    policy.check_action.return_value = MagicMock()
    surface = AsyncMock()
    surface._page = MagicMock()
    surface._page.url = "http://localhost:5000/members/search"
    surface.resolve_and_act = AsyncMock(return_value=MagicMock(success=False))
    with patch("replay.fallback.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": '{"action":"click","locator_strategy":"text","locator_value":"Search","value":""}'
                        }
                    ]
                }
            }
        }
        result = await attempt_fallback(
            step=step,
            error_detail="locator miss",
            live_aria="",
            surface=surface,
            policy=policy,
            artifact_status=ArtifactStatus.APPROVED,
            run_id="test",
            evidence_dir=tmp_path,
            fallback_used=False,
        )
    assert result is not None
    assert result.succeeded is False

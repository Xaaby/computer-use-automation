"""
tests/test_error_taxonomy.py
Error taxonomy classification tests (unit + light integration).

Decision: MEMBER_NOT_FOUND tests use member_id=88888 (not 99999) because
IDs starting with '9' map to HTTP 403 / PERMISSION_DENIED in the mock app.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from capability.schema import CapabilityArtifact
from replay.conditions import ConditionEvaluator
from replay.outcomes import (
    BusinessOutcome,
    HardFailure,
    IndeterminateCommit,
    RecoverableExhausted,
)


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = ROOT / "capabilities" / "member.lookup_savings_balance.capability.json"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "example_capability.json"


def _artifact() -> CapabilityArtifact:
    path = ARTIFACT_PATH if ARTIFACT_PATH.exists() else FIXTURE_PATH
    return CapabilityArtifact.model_validate_json(path.read_text(encoding="utf-8"))


def test_business_outcome_is_not_exception():
    bo = BusinessOutcome(
        run_id="r1",
        capability_id="c1",
        code="MEMBER_NOT_FOUND",
        description="not found",
        at_step_id="step_2",
        at_step_seq=2,
        evidence_path="evidence/runs/r1",
    )
    assert bo.code == "MEMBER_NOT_FOUND"
    assert not isinstance(bo, Exception)


def test_recoverable_exhausted_contains_retry_count():
    re = RecoverableExhausted(
        run_id="r1",
        capability_id="c1",
        condition_code="TRANSIENT_LOAD",
        step_id="step_1",
        retries_attempted=3,
        evidence={"screenshot": "x.png"},
    )
    assert re.retries_attempted == 3
    assert re.condition_code == "TRANSIENT_LOAD"


def test_hard_failure_contains_debug_fields():
    hf = HardFailure(
        run_id="r1",
        capability_id="c1",
        code="LOCATOR_NOT_FOUND",
        step_id="step_2",
        step_seq=2,
        expected="1 match",
        observed="0 matches",
        evidence={"screenshot": "a.png", "aria_snapshot": "a.yaml"},
    )
    assert hf.step_id == "step_2"
    assert hf.expected == "1 match"
    assert "screenshot" in hf.evidence


def test_indeterminate_commit_never_has_retries():
    ic = IndeterminateCommit(
        run_id="r1",
        capability_id="c1",
        step_id="step_x",
        step_seq=1,
        action_description="confirm",
        evidence={},
    )
    assert not hasattr(ic, "retries_attempted") or getattr(ic, "retries_attempted", 0) == 0
    assert "Cannot determine" in ic.reconciliation_note


@pytest.mark.asyncio
async def test_member_not_found_recognizer():
    from surfaces.playwright_web import PlaywrightWebSurface

    art = _artifact()
    bo = next(b for b in art.business_outcomes if b.code == "MEMBER_NOT_FOUND")
    surface, browser, context, pw = await PlaywrightWebSurface.create(headless=True)
    try:
        # Login then search unknown non-9 id
        from replay.executor import ReplayExecutor

        ex = ReplayExecutor(artifact=art, params={}, headless=True)
        ex._surface = surface
        await ex._re_authenticate()
        page = surface._page
        await page.goto("http://localhost:5000/members/search")
        await page.get_by_role("textbox", name="Member ID").fill("88888")
        await page.get_by_role("button", name="Search").click()
        await page.wait_for_load_state("domcontentloaded")
        ev = ConditionEvaluator(surface)
        assert await ev.evaluate_group(bo.recognizer.model_dump(exclude_none=True))
    finally:
        await context.close()
        await browser.close()
        await pw.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_session_expired_recognizer():
    from surfaces.playwright_web import PlaywrightWebSurface

    art = _artifact()
    rec = next(r for r in art.error_taxonomy.recoverable if r.code == "SESSION_EXPIRED")
    surface, browser, context, pw = await PlaywrightWebSurface.create(headless=True)
    try:
        await surface.navigate("http://localhost:5000/login?expired=true")
        ev = ConditionEvaluator(surface)
        # Page shows "session has expired" text
        ok = await ev.evaluate_group(rec.recognizer.model_dump(exclude_none=True))
        assert ok
    finally:
        await context.close()
        await browser.close()
        await pw.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_permission_denied_recognizer_403():
    from surfaces.playwright_web import PlaywrightWebSurface

    art = _artifact()
    hf = next(h for h in art.error_taxonomy.hard_failures if h.code == "PERMISSION_DENIED")
    surface, browser, context, pw = await PlaywrightWebSurface.create(headless=True)
    try:
        from replay.executor import ReplayExecutor

        ex = ReplayExecutor(artifact=art, params={}, headless=True)
        ex._surface = surface
        await ex._re_authenticate()
        page = surface._page
        await page.goto("http://localhost:5000/members/search")
        await page.get_by_role("textbox", name="Member ID").fill("90001")
        await page.get_by_role("button", name="Search").click()
        await page.wait_for_load_state("domcontentloaded")
        ev = ConditionEvaluator(surface)
        assert await ev.evaluate_group(hf.recognizer.model_dump(exclude_none=True))
    finally:
        await context.close()
        await browser.close()
        await pw.__aexit__(None, None, None)

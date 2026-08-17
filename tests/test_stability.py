"""
tests/test_stability.py
Stability score helpers and a short live smoke (3 runs).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from evals.stability_runner import run_stability_eval, verdict_from_rate

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "capabilities" / "member.lookup_savings_balance.capability.json"


def test_verdict_thresholds():
    assert verdict_from_rate(1.0) == "STABLE"
    assert verdict_from_rate(0.90) == "STABLE"
    assert verdict_from_rate(0.85) == "FLAKY"
    assert verdict_from_rate(0.70) == "FLAKY"
    assert verdict_from_rate(0.69) == "BROKEN"


@pytest.mark.asyncio
async def test_stability_smoke_three_runs():
    if not ARTIFACT.exists():
        pytest.skip("capability artifact missing")
    report = await run_stability_eval(
        artifact_path=ARTIFACT,
        params={"member_id": "10001"},
        n_runs=3,
        headless=True,
    )
    assert report["n_runs"] == 3
    assert report["success_count"] >= 2
    assert report["verdict"] in ("STABLE", "FLAKY", "BROKEN")
    assert "p50_ms" in report

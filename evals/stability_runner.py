"""
evals/stability_runner.py
Runs a capability replay N times and produces a stability/flakiness report.
This is the eval suite — proves the system works consistently, not just once.
This is what "evals are how you know what shipped is correct" means in practice.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from datetime import datetime, timezone

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from capability.schema import ArtifactStatus, CapabilityArtifact, ConfidenceScore
from replay.executor import ReplayExecutor
from replay.outcomes import (
    BusinessOutcome,
    HardFailure,
    IndeterminateCommit,
    ReplaySuccess,
)


async def run_stability_eval(
    artifact_path: Path,
    params: dict[str, str],
    n_runs: int = 20,
    headless: bool = True,
) -> dict:
    """
    Run the capability N times and report stability metrics.
    """
    artifact = CapabilityArtifact.model_validate(
        json.loads(artifact_path.read_text(encoding="utf-8"))
    )

    results: dict = {
        "capability_id": artifact.name,
        "n_runs": n_runs,
        "params": params,
        "success_count": 0,
        "business_outcome_count": 0,
        "failure_count": 0,
        "indeterminate_count": 0,
        "durations_ms": [],
        "failures_by_step": {},
        "run_details": [],
    }

    for i in range(n_runs):
        print(f"Run {i + 1}/{n_runs}...", end=" ", flush=True)
        start = time.perf_counter()

        result = await ReplayExecutor(
            artifact=artifact,
            params=params,
            headless=headless,
        ).run()

        duration_ms = int((time.perf_counter() - start) * 1000)
        results["durations_ms"].append(duration_ms)

        if isinstance(result, ReplaySuccess):
            results["success_count"] += 1
            print(f"OK {duration_ms}ms")
        elif isinstance(result, BusinessOutcome):
            results["business_outcome_count"] += 1
            print(f"BO {result.code} {duration_ms}ms")
        elif isinstance(result, HardFailure):
            results["failure_count"] += 1
            step = result.step_id
            results["failures_by_step"][step] = (
                results["failures_by_step"].get(step, 0) + 1
            )
            print(f"FAIL {result.code} at {step} {duration_ms}ms")
        elif isinstance(result, IndeterminateCommit):
            results["indeterminate_count"] += 1
            print(f"INDETERMINATE {duration_ms}ms")
        else:
            results["indeterminate_count"] += 1
            print(f"UNKNOWN {duration_ms}ms")

        results["run_details"].append(
            {
                "run": i + 1,
                "outcome": type(result).__name__,
                "duration_ms": duration_ms,
            }
        )

    durations = sorted(results["durations_ms"])
    results["p50_ms"] = durations[len(durations) // 2]
    results["p95_ms"] = durations[min(len(durations) - 1, int(len(durations) * 0.95))]
    results["mean_ms"] = int(sum(durations) / len(durations))
    results["success_rate"] = results["success_count"] / n_runs

    if results["success_rate"] >= 0.90:
        results["verdict"] = "STABLE"
    elif results["success_rate"] >= 0.70:
        results["verdict"] = "FLAKY"
    else:
        results["verdict"] = "BROKEN"

    return results


def verdict_from_rate(success_rate: float) -> str:
    """Pure helper for unit tests. Thresholds: STABLE >= 0.90, FLAKY >= 0.70."""
    if success_rate >= 0.90:
        return "STABLE"
    if success_rate >= 0.70:
        return "FLAKY"
    return "BROKEN"


def compute_confidence(run_results: list) -> ConfidenceScore:
    """
    Compute confidence score from N replay results.
    Promotion gate uses stricter thresholds: STABLE >= 0.95, FLAKY >= 0.5.
    """
    n = len(run_results)
    if n == 0:
        return ConfidenceScore(
            score=0.0, sample_size=0, p50_ms=0, p95_ms=0, verdict="BROKEN"
        )
    success_count = sum(1 for r in run_results if r.get("status") == "success")
    score = success_count / n
    durations = sorted(
        r["duration_ms"] for r in run_results if r.get("duration_ms") is not None
    )
    p50 = float(durations[len(durations) // 2]) if durations else 0.0
    p95_idx = min(len(durations) - 1, int(len(durations) * 0.95)) if durations else 0
    p95 = float(durations[p95_idx]) if durations else 0.0
    verdict = "STABLE" if score >= 0.95 else "FLAKY" if score >= 0.5 else "BROKEN"
    return ConfidenceScore(
        score=score,
        sample_size=n,
        p50_ms=p50,
        p95_ms=p95,
        verdict=verdict,
    )


def promote_artifact(
    artifact_path: Path,
    confidence: ConfidenceScore,
    operator: str = "stability_runner",
) -> dict:
    """Promote artifact from draft to approved via provenance.status."""
    if confidence.verdict != "STABLE":
        raise ValueError(
            f"Cannot promote artifact with verdict={confidence.verdict}. "
            f"Required: STABLE (score >= 0.95). Got: score={confidence.score:.2f}"
        )
    artifact_data = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact_data.setdefault("provenance", {})["status"] = ArtifactStatus.APPROVED.value
    artifact_data["provenance"]["confidence"] = confidence.model_dump()
    artifact_data["provenance"]["approved_by"] = operator
    artifact_data["provenance"]["approved_at"] = datetime.now(timezone.utc).isoformat()
    artifact_path.write_text(json.dumps(artifact_data, indent=2), encoding="utf-8")
    return artifact_data


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--params", default='{"member_id": "10001"}')
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--output", help="Save report to JSON file")
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Promote artifact to approved after STABLE confidence run",
    )
    args = parser.parse_args()

    report = asyncio.run(
        run_stability_eval(
            artifact_path=Path(args.artifact),
            params=json.loads(args.params),
            n_runs=args.runs,
            headless=args.headless,
        )
    )

    run_results = [
        {
            "status": "success" if d["outcome"] == "ReplaySuccess" else "hard_failure",
            "duration_ms": d["duration_ms"],
        }
        for d in report["run_details"]
    ]
    confidence = compute_confidence(run_results)

    print("\n" + "=" * 60)
    print(f"STABILITY REPORT: {report['verdict']}")
    print(f"CONFIDENCE (promotion gate): {confidence.verdict} score={confidence.score:.2f}")
    print(
        f"Success rate: {report['success_rate'] * 100:.1f}% "
        f"({report['success_count']}/{report['n_runs']})"
    )
    print(
        f"Latency: p50={report['p50_ms']}ms "
        f"p95={report['p95_ms']}ms mean={report['mean_ms']}ms"
    )
    if report["failures_by_step"]:
        print(f"Failures by step: {report['failures_by_step']}")
    print("=" * 60)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report saved to {args.output}")

    if args.approve:
        updated = promote_artifact(Path(args.artifact), confidence)
        print(f"Artifact promoted to APPROVED by stability_runner")
        print(f"  status={updated['provenance']['status']}")

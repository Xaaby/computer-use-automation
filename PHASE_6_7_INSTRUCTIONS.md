# Phase 6 + 7 Instructions — Stretch Goals + Documentation
## Read RULES.md first. Phases 1-5 must be complete.

---

# Phase 6 — Stretch Goals

## Stretch A: Agent-Facing Capability API (already in escalation/api.py)
The `/capabilities` and `/capabilities/{name}/invoke` endpoints are already specified in Phase 5 `escalation/api.py`. Verify they work:

```bash
curl http://localhost:8765/capabilities
# Returns list of all .capability.json files with metadata

curl -X POST http://localhost:8765/capabilities/member_lookup_savings_balance/invoke \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"member_id": "10001"}}'
# Returns ReplayResult JSON
```

---

## Stretch B: Multi-Run Stability Score

### evals/stability_runner.py

```python
"""
evals/stability_runner.py
Runs a capability replay N times and produces a stability/flakiness report.
This is the eval suite — proves the system works consistently, not just once.
This is what "evals are how you know what shipped is correct" means in practice.
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from capability.schema import CapabilityArtifact
from replay.executor import ReplayExecutor
from replay.outcomes import ReplaySuccess, BusinessOutcome, HardFailure


async def run_stability_eval(
    artifact_path: Path,
    params: dict[str, str],
    n_runs: int = 20,
    headless: bool = True,
) -> dict:
    """
    Run the capability N times and report stability metrics.
    
    Returns:
    {
        "capability_id": "...",
        "n_runs": 20,
        "params": {...},
        "success_count": 19,
        "business_outcome_count": 0,
        "failure_count": 1,
        "indeterminate_count": 0,
        "success_rate": 0.95,
        "durations_ms": [...],
        "p50_ms": 2340,
        "p95_ms": 4210,
        "mean_ms": 2580,
        "failures_by_step": {"step_3": 1},
        "verdict": "STABLE"  # STABLE if >90% success, FLAKY if 70-90%, BROKEN if <70%
    }
    """
    artifact = CapabilityArtifact.model_validate(json.loads(artifact_path.read_text()))
    
    results = {
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
        print(f"Run {i+1}/{n_runs}...", end=" ", flush=True)
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
            print(f"✓ {duration_ms}ms")
        elif isinstance(result, BusinessOutcome):
            results["business_outcome_count"] += 1
            print(f"○ BUSINESS_OUTCOME:{result.code} {duration_ms}ms")
        elif isinstance(result, HardFailure):
            results["failure_count"] += 1
            step = result.step_id
            results["failures_by_step"][step] = results["failures_by_step"].get(step, 0) + 1
            print(f"✗ FAILURE:{result.code} at {step} {duration_ms}ms")
        else:
            results["indeterminate_count"] += 1
            print(f"? INDETERMINATE {duration_ms}ms")
        
        results["run_details"].append({
            "run": i+1,
            "outcome": type(result).__name__,
            "duration_ms": duration_ms,
        })
    
    # Calculate statistics
    durations = sorted(results["durations_ms"])
    results["p50_ms"] = durations[len(durations)//2]
    results["p95_ms"] = durations[int(len(durations)*0.95)]
    results["mean_ms"] = int(sum(durations)/len(durations))
    results["success_rate"] = results["success_count"] / n_runs
    
    if results["success_rate"] >= 0.90:
        results["verdict"] = "STABLE"
    elif results["success_rate"] >= 0.70:
        results["verdict"] = "FLAKY"
    else:
        results["verdict"] = "BROKEN"
    
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--params", default="{}")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--output", help="Save report to JSON file")
    args = parser.parse_args()
    
    report = asyncio.run(run_stability_eval(
        artifact_path=Path(args.artifact),
        params=json.loads(args.params),
        n_runs=args.runs,
        headless=args.headless,
    ))
    
    print("\n" + "="*60)
    print(f"STABILITY REPORT: {report['verdict']}")
    print(f"Success rate: {report['success_rate']*100:.1f}% ({report['success_count']}/{report['n_runs']})")
    print(f"Latency: p50={report['p50_ms']}ms p95={report['p95_ms']}ms mean={report['mean_ms']}ms")
    if report['failures_by_step']:
        print(f"Failures by step: {report['failures_by_step']}")
    print("="*60)
    
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2))
        print(f"Report saved to {args.output}")
```

### Checkpoint Verification for Phase 6
```bash
python -m evals.stability_runner \
  --artifact capabilities/member_lookup_savings_balance.capability.json \
  --params '{"member_id": "10001"}' \
  --runs 20 \
  --headless \
  --output evidence/stability_report.json

cat evidence/stability_report.json
```
Expected: STABLE verdict, >90% success rate.

---

# Phase 7 — Documentation + Evidence

## README.md — Exact Template

```markdown
# Computer-Use Automation System

A system that gives AI agents "hands" to operate legacy bank software with no API.
Built for interface.ai engineering take-home assessment.

## Architecture
LLM Discovery → Capability Artifact → Deterministic Replay → Human Escalation

## Quick Start

### Prerequisites
- Python 3.11+
- AWS credentials with Bedrock access

### Setup
\`\`\`bash
git clone https://github.com/Xaaby/computer-use-automation.git
cd computer-use-automation
pip install -e .
playwright install chromium
cp .env.example .env
# Edit .env with your AWS credentials
\`\`\`

### Environment Variables (.env)
\`\`\`
AWS_ACCESS_KEY_ID=your_key
AWS_SECRET_ACCESS_KEY=your_secret
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0
APP_USERNAME=admin
APP_PASSWORD=admin
\`\`\`

### Start the Mock Bank App
\`\`\`bash
python target_app/app.py
# App runs at http://localhost:5000
\`\`\`

### Demo: Full End-to-End

**Step 1: Discovery Run (real LLM driving the browser)**
\`\`\`bash
python -m agent.loop \\
  --goal "Look up member 10001 and find their current savings balance" \\
  --entry-point "http://localhost:5000/members/search" \\
  --params '{"member_id": "10001"}'
\`\`\`

**Step 2: Replay (deterministic, no LLM)**
\`\`\`bash
python -m replay.executor \\
  --artifact capabilities/member_lookup_savings_balance.capability.json \\
  --params '{"member_id": "10001"}'
\`\`\`

**Step 3: Business Outcome (member not found)**
\`\`\`bash
python -m replay.executor \\
  --artifact capabilities/member_lookup_savings_balance.capability.json \\
  --params '{"member_id": "99999"}'
\`\`\`

**Step 4: Capability API (Stretch A)**
\`\`\`bash
# Start the operator console (also serves capability API)
python -m escalation.api &

# List capabilities
curl http://localhost:8765/capabilities

# Invoke a capability
curl -X POST http://localhost:8765/capabilities/member_lookup_savings_balance/invoke \\
  -H "Content-Type: application/json" \\
  -d '{"inputs": {"member_id": "10001"}}'
\`\`\`

**Step 5: Stability Eval (Stretch B)**
\`\`\`bash
python -m evals.stability_runner \\
  --artifact capabilities/member_lookup_savings_balance.capability.json \\
  --params '{"member_id": "10001"}' \\
  --runs 20 \\
  --headless \\
  --output evidence/stability_report.json
\`\`\`

**Step 6: Run All Tests**
\`\`\`bash
python -m pytest tests/ -v
\`\`\`

## Evidence
See /evidence/ for discovery logs, replay logs, error cases, and screenshots.

## Design Write-up
See REPORT.md for architecture decisions, trade-offs, and cuts.
```

---

## REPORT.md — Seven Section Template

Write as opinionated engineering decisions. Every paragraph makes an argument.

### Section 1: Architecture
- Single process, single repo — justified by assignment scope
- Four clear modules: Discovery Agent, Capability Repository, Replay Executor, Escalation Controller
- AWS Bedrock (boto3) instead of Anthropic SDK — enterprise deployment alignment
- Surface abstraction seam: `Surface` ABC separates logical capability from execution mechanics
- FastAPI runs in same event loop as executor — enables asyncio.Event sharing

### Section 2: Artifact Schema
- Why it's a contract not a recording: typed inputs/outputs, versioned, policy-embedded
- Three-tier locator (role→label→css→coords) — explain why each tier and when each fails
- Business outcomes in the artifact, not just the code — makes the contract self-documenting
- Schema version vs capability version distinction — defend why both are needed
- `status: "draft"` — capability must be replayed once before it can be trusted

### Section 3: Determinism & Error Handling
- Four outcome classes not three — defend IndeterminateCommit as a separate class
- Why irreversible commits NEVER retry — the double-transaction risk in banking
- Checkpoint granularity: state transitions, not every keystroke
- Locator strict mode: ambiguous locator is always a hard failure, never a guess
- Session recovery as a declared sub-routine, not a catch-all exception handler

### Section 4: Heterogeneity & Multi-Tenant
- Surface abstraction: how PlaywrightWebSurface → future WindowsUISurface
- iframe handling: frame_path in locator spec, frame_locator in Playwright adapter
- Multi-tenant: base capability + tenant_overrides.json pattern (designed, not built)
- Drift detection: structural fingerprint of ARIA tree at checkpoints

### Section 5: Escalation & Handoff
- Same session, same browser window — not a new browser, not a screenshot
- asyncio.Event gate — only mechanism that properly pauses a coroutine without blocking the loop
- Human action recording via add_init_script + expose_binding — persists across navigation
- Post-human resync: checkpoint verification before automation resumes
- What's mocked: operator UI is minimal HTML. Real product would use WebRTC session streaming.

### Section 6: Safety
- Policy enforced at Surface.act() — below both LLM and replay engine, not above
- Allowlist is a config file checked at every action — not just at planning time
- Irreversible commit gate: approval token required, no exceptions
- PII redaction: regex patterns on all log writes, ARIA label-based screenshot masking
- Credentials: environment variables only, never in artifacts or logs

### Section 7: Cuts
For each cut: what it is, what seam exists for it, what would need to change, why it's the right next thing.

Cuts made:
1. Remote co-browsing operator console (WebRTC session streaming)
2. Desktop surface adapter (Windows UI Automation)
3. Multi-tenant infrastructure (base capability + sparse tenant overrides at scale)
4. Artifact confidence scoring and draft→approved gate
5. Assisted LLM fallback on single-step replay failure

---

## Evidence Files to Generate

Run these commands and save outputs to evidence/:

```bash
# 1. Discovery run (real LLM, headed browser)
python -m agent.loop \
  --goal "Look up member 10001 and find their current savings balance" \
  --entry-point "http://localhost:5000/members/search" \
  --params '{"member_id": "10001"}'
# Saves: evidence/runs/{run_id}/discovery.jsonl

# 2. Replay success
python -m replay.executor \
  --artifact capabilities/member_lookup_savings_balance.capability.json \
  --params '{"member_id": "10001"}'
# Saves: evidence/runs/{run_id}/replay_success.jsonl

# 3. Business outcome (MEMBER_NOT_FOUND)
python -m replay.executor \
  --artifact capabilities/member_lookup_savings_balance.capability.json \
  --params '{"member_id": "99999"}'
# Saves: evidence/runs/{run_id}/replay_not_found.jsonl

# 4. Hard failure (permission denied)
python -m replay.executor \
  --artifact capabilities/member_lookup_savings_balance.capability.json \
  --params '{"member_id": "90001"}'
# Saves: evidence/runs/{run_id}/replay_permission_denied.jsonl with screenshot

# 5. Stability report
python -m evals.stability_runner \
  --artifact capabilities/member_lookup_savings_balance.capability.json \
  --params '{"member_id": "10001"}' \
  --runs 20 --headless \
  --output evidence/stability_report.json
```

Copy the example capability artifact to evidence/:
```bash
cp capabilities/member_lookup_savings_balance.capability.json evidence/
```

---

## Final Verification Before Submission
```bash
# All tests pass
python -m pytest tests/ -v

# README demo commands all work
python target_app/app.py &
python -m replay.executor --artifact capabilities/member_lookup_savings_balance.capability.json --params '{"member_id": "10001"}'

# All required evidence files present
ls evidence/runs/*/discovery.jsonl
ls evidence/runs/*/replay_success.jsonl
ls evidence/stability_report.json
ls capabilities/*.capability.json

# REPORT.md has all 7 sections
grep "^## " REPORT.md

# Push to GitHub
git add -A
git commit -m "Complete computer-use automation system"
git push origin main
```

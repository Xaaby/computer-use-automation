# Phase 4 Instructions — Replay Engine
## Read RULES.md first. Phases 1-3 must be complete.

## What to Build

```
replay/
    __init__.py
    outcomes.py      ← typed result classes
    conditions.py    ← condition evaluators
    executor.py      ← deterministic replay engine (NO LLM)
tests/
    test_replay.py
    test_error_taxonomy.py
```

---

## 1. replay/outcomes.py

```python
"""
replay/outcomes.py
Typed terminal result classes for replay execution.
These are the only possible outcomes — no exceptions escape the executor.

CRITICAL: IndeterminateCommit NEVER retries. Ever.
"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReplaySuccess:
    """The capability completed successfully with typed outputs."""
    run_id: str
    capability_id: str
    outputs: dict[str, Any]
    steps_completed: int
    duration_ms: int
    evidence_path: str


@dataclass
class BusinessOutcome:
    """
    A valid terminal result that is NOT a success.
    e.g. MEMBER_NOT_FOUND — the operation completed, but the answer is 'no'.
    This is NOT an error. The caller asked a question and got a valid answer.
    """
    run_id: str
    capability_id: str
    code: str          # e.g. "MEMBER_NOT_FOUND"
    description: str
    at_step_id: str
    at_step_seq: int
    evidence_path: str


@dataclass
class HardFailure:
    """
    Unrecoverable failure. Replay stopped. Debug info provided.
    """
    run_id: str
    capability_id: str
    code: str
    step_id: str
    step_seq: int
    expected: str
    observed: str
    evidence: dict[str, str]  # {"screenshot": "path", "aria_snapshot": "path", "trace": "path"}


@dataclass
class RecoverableExhausted:
    """
    A recoverable condition was encountered but retry budget was exhausted.
    Treated as a hard failure for the caller.
    """
    run_id: str
    capability_id: str
    condition_code: str
    step_id: str
    retries_attempted: int
    evidence: dict[str, str]


@dataclass
class IndeterminateCommit:
    """
    An irreversible action timed out. We do not know if it committed.
    
    CRITICAL DESIGN NOTE:
    This is the most dangerous outcome. A fund transfer that timed out may or
    may not have posted. Retrying could duplicate the transaction.
    This MUST be escalated to a human for reconciliation.
    NEVER retry this. NEVER treat this as a regular hard_failure with retry logic.
    """
    run_id: str
    capability_id: str
    step_id: str
    step_seq: int
    action_description: str
    evidence: dict[str, str]
    reconciliation_note: str = (
        "Cannot determine if irreversible action committed. "
        "Human must check the banking system and confirm or reverse manually."
    )


# Type alias for all possible replay outcomes
ReplayOutcome = (
    ReplaySuccess
    | BusinessOutcome
    | HardFailure
    | RecoverableExhausted
    | IndeterminateCommit
)
```

---

## 2. replay/conditions.py

```python
"""
replay/conditions.py
Evaluates condition specs from capability artifacts against live page state.
Used for: preconditions, postconditions, checkpoints, business outcome recognizers.
"""
import fnmatch
from surfaces.base import Surface


class ConditionEvaluator:
    def __init__(self, surface: Surface):
        self._surface = surface

    async def evaluate(self, condition: dict) -> bool:
        """Evaluate a single condition dict. Returns True if condition is met."""
        ctype = condition["type"]

        if ctype == "route_matches":
            return await self._surface.check_condition(condition)
        elif ctype == "text_visible":
            return await self._surface.check_condition(condition)
        elif ctype == "heading_visible":
            return await self._surface.check_condition(condition)
        elif ctype == "element_present":
            return await self._surface.check_condition(condition)
        elif ctype == "http_status":
            return await self._surface.check_condition(condition)
        elif ctype == "input_value_equals":
            return await self._surface.check_condition(condition)
        elif ctype == "timeout":
            # Always False when evaluated — means we've waited too long
            # Handled specially by executor with actual timeout
            return False
        elif ctype == "locator_matches_multiple":
            # Checked by surface during resolve_and_act
            return False
        elif ctype == "all_candidates_failed":
            return False
        return False

    async def evaluate_group(self, group: dict) -> bool:
        """
        Evaluate a condition group with 'all' or 'any' logic.
        group = {"all": [condition, ...]} or {"any": [condition, ...]}
        """
        if "all" in group and group["all"]:
            for cond in group["all"]:
                if not await self.evaluate(cond):
                    return False
            return True
        elif "any" in group and group["any"]:
            for cond in group["any"]:
                if await self.evaluate(cond):
                    return True
            return False
        return False
```

---

## 3. replay/executor.py

```python
"""
replay/executor.py
Deterministic replay engine. NO LLM involved.
Given a capability artifact + input params → executes steps → returns typed outcome.

This is the production execution path. Every run is identical given the same inputs.
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from capability.schema import (
    CapabilityArtifact, StepDefinition, RiskLevel,
    ExecutionMode, ControlOwner, LogEntry, ActionType
)
from replay.outcomes import (
    ReplaySuccess, BusinessOutcome, HardFailure,
    RecoverableExhausted, IndeterminateCommit, ReplayOutcome
)
from replay.conditions import ConditionEvaluator
from surfaces.playwright_web import PlaywrightWebSurface
from policy.engine import PolicyEngine, PolicyViolationError
from policy.redactor import redact_log_entry
from escalation.controller import EscalationController
```

**ReplayExecutor class:**

```python
class ReplayExecutor:
    def __init__(
        self,
        artifact: CapabilityArtifact,
        params: dict[str, str],
        headless: bool = False,
        evidence_base: Path | None = None,
        escalation_controller: EscalationController | None = None,
    ):
        self._artifact = artifact
        self._params = params
        self._headless = headless
        self._evidence_base = evidence_base or Path("evidence/runs")
        self._escalation = escalation_controller
        self._run_id = str(uuid4())
        self._log_entries: list[LogEntry] = []

    async def run(self) -> ReplayOutcome:
        """Execute the capability deterministically."""
        ...
```

**Step execution algorithm:**
```
For each step in artifact.steps:
    1. await controller.gate.wait()  # pause if escalation is pending
    2. Substitute params: replace $inputs.param_name in step.value
    3. Check preconditions — if fail → hard_failure
    4. Check all business outcome recognizers BEFORE executing step
       If any matches → return BusinessOutcome immediately
    5. Check policy BEFORE acting (via surface — already done inside surface)
    6. If risk_level == "irreversible_commit":
       - Check for approval_token in escalation controller
       - If no token → escalate for approval, await gate
    7. Execute action via surface.resolve_and_act()
    8. If ActionResult.error_type == "ambiguous_locator" → HardFailure
    9. If ActionResult.error_type == "all_candidates_failed" → HardFailure
    10. Check for TRANSIENT_LOAD / SESSION_EXPIRED recoverable conditions
        If found and retry budget > 0:
           - wait backoff_ms
           - if SESSION_EXPIRED: call re_authenticate sub-routine
           - retry step (increment retry_attempt)
        If budget exhausted → RecoverableExhausted
    11. If step has postconditions: evaluate each
        If fail and timeout → check for recoverable conditions → handle
    12. Write LogEntry to JSONL
    13. Check business outcome recognizers AFTER step too

After all steps:
    14. Evaluate success_condition checkpoint
    15. Extract outputs via read operations on output definitions
    16. Return ReplaySuccess with outputs
```

**CRITICAL — Irreversible commit timeout:**
```python
# If step.risk_level == "irreversible_commit" and action times out:
# DO NOT retry. DO NOT treat as recoverable.
# Return IndeterminateCommit IMMEDIATELY.
if step.risk_level == RiskLevel.IRREVERSIBLE_COMMIT and result.error_type == "timeout":
    evidence = await self._capture_and_save_evidence(step.id, step_seq)
    return IndeterminateCommit(
        run_id=self._run_id,
        capability_id=self._artifact.name,
        step_id=step.id,
        step_seq=step_seq,
        action_description=step.description,
        evidence=evidence,
    )
```

**Session re-authentication sub-routine:**
```python
async def _re_authenticate(self) -> bool:
    """
    Handle session expiry by logging in again.
    Credentials come from environment variables ONLY — never from artifact.
    Returns True if re-auth succeeded.
    """
    import os
    username = os.environ.get("APP_USERNAME", "admin")
    password = os.environ.get("APP_PASSWORD", "admin")
    # Navigate to login, fill credentials, submit
    # Return True if now on an authenticated page
    ...
```

**CLI entry point:**
```python
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True, help="Path to .capability.json")
    parser.add_argument("--params", default="{}", help="JSON string of input params")
    parser.add_argument("--headless", action="store_true", default=False)
    args = parser.parse_args()
    
    with open(args.artifact) as f:
        artifact = CapabilityArtifact.model_validate(json.load(f))
    
    result = asyncio.run(ReplayExecutor(
        artifact=artifact,
        params=json.loads(args.params),
        headless=args.headless,
    ).run())
    
    print(json.dumps(result.__dict__, indent=2, default=str))
```

---

## 4. tests/test_replay.py — Tests to Implement

```python
"""
tests/test_replay.py
Test the replay executor against the mock Flask app.
Flask app must be running on port 5000 before these tests run.
"""
```

Tests:
1. **Success case**: replay with member_id="10001" → ReplaySuccess with savings_balance
2. **Business outcome**: replay with member_id="99999" → BusinessOutcome(code="MEMBER_NOT_FOUND")
3. **Permission denied**: replay with member_id="90001" → HardFailure(code="PERMISSION_DENIED")
4. **Locator ambiguity**: inject duplicate elements → HardFailure(code="AMBIGUOUS_LOCATOR")
5. **Session expiry simulation**: hit `/simulate/slow` → verify recoverable handling with retry
6. **IndeterminateCommit**: simulate timeout on transfer confirm step → never retried
7. **Precondition fail**: start on wrong page → HardFailure with precondition details

---

## 5. tests/test_error_taxonomy.py — Tests to Implement

1. BusinessOutcome is returned, NOT raised as exception
2. RecoverableExhausted after N retries contains retry count
3. HardFailure contains step_id, expected, observed, evidence paths
4. IndeterminateCommit is never retried (verify retry_count == 0 always)
5. MEMBER_NOT_FOUND recognizer correctly identifies "No member found" text
6. SESSION_EXPIRED recognizer correctly identifies login redirect
7. PERMISSION_DENIED recognizer correctly identifies 403 response

---

## Checkpoint Verification
```bash
# Success case
python -m replay.executor \
  --artifact capabilities/member_lookup_savings_balance.capability.json \
  --params '{"member_id": "10001"}'
# Expected: {"status": "success", "outputs": {"savings_balance": "..."}}

# Business outcome case
python -m replay.executor \
  --artifact capabilities/member_lookup_savings_balance.capability.json \
  --params '{"member_id": "99999"}'
# Expected: {"status": "business_outcome", "business_outcome": {"code": "MEMBER_NOT_FOUND"}}

# All tests
python -m pytest tests/test_replay.py tests/test_error_taxonomy.py -v
```
Update PHASE_STATUS.md after completion.

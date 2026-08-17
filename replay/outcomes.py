"""
replay/outcomes.py
Typed terminal result classes for replay execution.
These are the only possible outcomes — no exceptions escape the executor.

CRITICAL: IndeterminateCommit NEVER retries. Ever.
"""
from dataclasses import dataclass
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

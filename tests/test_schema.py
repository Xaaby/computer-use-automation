"""Schema validation tests for capability artifacts (PHASE_1 §8)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from capability.schema import (
    BusinessOutcomeResult,
    CapabilityArtifact,
    ExecutionMode,
    FailureDetail,
    InputParameter,
    LocatorCandidate,
    LocatorStrategy,
    LogEntry,
    ReplayResult,
    ReplayStatus,
)

# Decision: fixture lives under tests/fixtures/, not capability/examples/ (that dir stays empty).
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "example_capability.json"


def test_example_capability_json_validates() -> None:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    artifact = CapabilityArtifact.model_validate(data)
    assert artifact.name == "member.lookup_savings_balance"
    # Decision: Phase 3 flatter recognizer dicts are invalid; fixture wraps them as ConditionGroup.
    assert artifact.error_taxonomy.recoverable[0].recognizer.any is not None
    assert artifact.error_taxonomy.hard_failures[0].recognizer.all is not None


def test_locator_candidate_invalid_priority_raises() -> None:
    with pytest.raises(ValidationError):
        LocatorCandidate(priority=0, strategy=LocatorStrategy.ROLE)
    with pytest.raises(ValidationError):
        LocatorCandidate(priority=6, strategy=LocatorStrategy.ROLE)


def test_log_entry_model_reasoning_allowed_during_replay() -> None:
    # Schema does not block model_reasoning when execution_mode is replay.
    entry = LogEntry(
        run_id="run-1",
        execution_mode=ExecutionMode.REPLAY,
        seq=1,
        model_reasoning="reasoning present during replay is allowed at schema level",
    )
    assert entry.model_reasoning is not None
    assert entry.execution_mode is ExecutionMode.REPLAY


def test_replay_result_success_has_outputs_only() -> None:
    result = ReplayResult(
        capability_id="cap-1",
        capability_version="1.0.0",
        status=ReplayStatus.SUCCESS,
        outputs={"savings_balance": "4821.50"},
        failure=None,
        business_outcome=None,
    )
    assert result.outputs == {"savings_balance": "4821.50"}
    assert result.failure is None
    assert result.business_outcome is None


def test_replay_result_business_outcome_has_outcome_only() -> None:
    result = ReplayResult(
        capability_id="cap-1",
        capability_version="1.0.0",
        status=ReplayStatus.BUSINESS_OUTCOME,
        outputs=None,
        failure=None,
        business_outcome=BusinessOutcomeResult(
            code="MEMBER_NOT_FOUND",
            description="Search completed but no member with this ID exists",
            at_step="step_3",
        ),
    )
    assert result.business_outcome is not None
    assert result.business_outcome.code == "MEMBER_NOT_FOUND"
    assert result.outputs is None
    assert result.failure is None


def test_replay_result_hard_failure_has_required_fields() -> None:
    result = ReplayResult(
        capability_id="cap-1",
        capability_version="1.0.0",
        status=ReplayStatus.HARD_FAILURE,
        outputs=None,
        business_outcome=None,
        failure=FailureDetail(
            code="PERMISSION_DENIED",
            step_id="step_3",
            expected="HTTP 200",
            observed="HTTP 403",
        ),
    )
    assert result.failure is not None
    assert result.failure.code == "PERMISSION_DENIED"
    assert result.failure.step_id == "step_3"
    assert result.failure.expected == "HTTP 200"
    assert result.failure.observed == "HTTP 403"


def test_capability_artifact_extra_field_forbidden() -> None:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    data["not_a_schema_field"] = "forbidden"
    with pytest.raises(ValidationError):
        CapabilityArtifact.model_validate(data)


def test_input_parameter_invalid_type_raises() -> None:
    with pytest.raises(ValidationError):
        InputParameter(name="member_id", type="object", description="invalid type")

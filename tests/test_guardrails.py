"""Guardrail tests for allowlist policy and PII redaction (PHASE_1 §9)."""
from __future__ import annotations

import pytest

from capability.schema import RiskLevel
from policy.engine import PolicyEngine, PolicyViolationError
from policy.redactor import redact_log_entry, redact_string, should_mask_field


def test_fill_on_allowed_route_is_safe() -> None:
    # Decision: allowed fill uses GET search URL; members/** matches, not risky.
    engine = PolicyEngine()
    result = engine.check_action("fill", "http://localhost:5000/members/search")
    assert result is RiskLevel.SAFE


def test_blocked_route_raises() -> None:
    # Decision: /admin/users matches blocked_routes admin/** (checked before origin allow).
    engine = PolicyEngine()
    with pytest.raises(PolicyViolationError):
        engine.check_action("click", "http://localhost:5000/admin/users")


def test_unknown_action_type_raises() -> None:
    engine = PolicyEngine()
    with pytest.raises(PolicyViolationError):
        engine.check_action("delete", "http://localhost:5000/members/search")


def test_transfer_route_requires_confirmation() -> None:
    # Decision: /transfers/confirm is allowlisted AND matches transfers/** → risky, not blocked.
    engine = PolicyEngine()
    result = engine.check_action("click", "http://localhost:5000/transfers/confirm")
    assert result is RiskLevel.REQUIRES_CONFIRMATION


def test_unknown_origin_raises() -> None:
    # Decision: origin startswith would allow any localhost:5000 URL that is not blocked;
    # use a non-localhost origin so this test actually hits the unknown-pattern path.
    engine = PolicyEngine()
    with pytest.raises(PolicyViolationError):
        engine.check_action("navigate", "http://evil.example/x")


def test_redact_string_ssn() -> None:
    assert redact_string("123-45-6789") == "[SSN-REDACTED]"


def test_redact_string_routing_number() -> None:
    assert redact_string("021000021") == "[ROUTING-REDACTED]"


def test_redact_log_entry_nested_ssn() -> None:
    entry = {
        "note": "ssn 123-45-6789",
        "nested": {"value": "123-45-6789"},
        "items": ["123-45-6789", 1],
    }
    redacted = redact_log_entry(entry)
    assert redacted["note"] == "ssn [SSN-REDACTED]"
    assert redacted["nested"]["value"] == "[SSN-REDACTED]"
    assert redacted["items"][0] == "[SSN-REDACTED]"
    assert "123-45-6789" not in str(redacted)


def test_should_mask_password_field() -> None:
    engine = PolicyEngine()
    assert should_mask_field("Password", engine.get_pii_field_labels()) is True


def test_should_not_mask_member_name_field() -> None:
    engine = PolicyEngine()
    assert should_mask_field("Member Name", engine.get_pii_field_labels()) is False

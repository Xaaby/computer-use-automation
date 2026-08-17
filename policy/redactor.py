"""
policy/redactor.py
PII redaction for logs, artifacts, and screenshots.
Applied before ANY data is written to disk or logs.
"""
import re
from typing import Any

# Banking PII patterns
_PII_PATTERNS = [
    (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '[SSN-REDACTED]'),           # SSN
    (re.compile(r'\b\d{9}\b'), '[ROUTING-REDACTED]'),                     # Routing number
    (re.compile(r'\b\d{10,17}\b'), '[ACCOUNT-REDACTED]'),                 # Account number
    (re.compile(r'\b4[0-9]{12}(?:[0-9]{3})?\b'), '[CC-REDACTED]'),       # Visa
    (re.compile(r'\b5[1-5][0-9]{14}\b'), '[CC-REDACTED]'),                # Mastercard
]


def redact_string(text: str) -> str:
    """Apply all PII patterns to a string."""
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_log_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Redact PII from all string values in a log entry dict."""
    result = {}
    for key, value in entry.items():
        if isinstance(value, str):
            result[key] = redact_string(value)
        elif isinstance(value, dict):
            result[key] = redact_log_entry(value)
        elif isinstance(value, list):
            result[key] = [
                redact_string(v) if isinstance(v, str) else v
                for v in value
            ]
        else:
            result[key] = value
    return result


def should_mask_field(aria_label: str, pii_labels: list[str]) -> bool:
    """Check if a field with this ARIA label should be masked in screenshots."""
    label_lower = aria_label.lower()
    return any(pii.lower() in label_lower for pii in pii_labels)

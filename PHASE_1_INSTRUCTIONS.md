# Phase 1 Instructions — Foundation
## Read RULES.md first. Then implement everything below exactly.

---

## What to Build in This Phase

### 1. Directory Structure to Create
```
capability/
    __init__.py
    schema.py          ← ALL Pydantic models (source of truth)
    repository.py      ← load/save/list capabilities
    examples/          ← empty dir for now

policy/
    __init__.py
    engine.py          ← allowlist enforcement
    allowlist.json     ← config file
    redactor.py        ← PII redaction

target_app/
    __init__.py
    app.py             ← Flask mock bank admin
    data/
        members.json   ← 10 fake members
    templates/
        base.html
        login.html
        search.html
        member_detail.html
        accounts_frame.html   ← served inside iframe
        transfer_form.html
        transfer_confirm.html
        error.html
        not_found.html

evidence/
    runs/              ← empty dir, created at runtime
    capabilities/      ← empty dir
    .gitkeep

tests/
    __init__.py
    test_schema.py
    test_guardrails.py
```

---

## 2. capability/schema.py — IMPLEMENT EXACTLY AS SPECIFIED

All Pydantic v2 models. `model_config = ConfigDict(extra="forbid")` on every model.

```python
"""
capability/schema.py
Source of truth for ALL data models in this system.
Import from here everywhere else — never define duplicate models.
"""
from __future__ import annotations
import asyncio
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ─── Enums ────────────────────────────────────────────────────────────────────

class SurfaceType(str, Enum):
    PLAYWRIGHT_WEB = "playwright_web"
    LEGACY_WEB = "legacy_web"
    DESKTOP = "desktop"

class ActionType(str, Enum):
    FILL = "fill"
    CLICK = "click"
    PRESS = "press"
    NAVIGATE = "navigate"
    READ = "read"
    OBSERVE_SCREENSHOT = "observe_screenshot"
    ASSERT = "assert"
    WAIT = "wait"

class RiskLevel(str, Enum):
    SAFE = "safe"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    IRREVERSIBLE_COMMIT = "irreversible_commit"

class LocatorStrategy(str, Enum):
    ROLE = "role"
    LABEL = "label"
    TEXT = "text"
    CSS = "css"
    COORDS = "coords"

class ExecutionMode(str, Enum):
    DISCOVERY = "discovery"
    REPLAY = "replay"
    HUMAN = "human"

class ControlOwner(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"

class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    HARD_FAILURE = "hard_failure"
    INDETERMINATE_COMMIT = "indeterminate_commit"
    RECOVERABLE_EXHAUSTED = "recoverable_exhausted"

class ArtifactStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


# ─── Locator Models ───────────────────────────────────────────────────────────

class LocatorCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: int = Field(ge=1, le=5)
    strategy: LocatorStrategy
    role: str | None = None           # for strategy=role
    name: str | None = None           # accessible name
    exact: bool = True
    text: str | None = None           # for strategy=label or text
    selector: str | None = None       # for strategy=css
    coords: dict[str, int] | None = None  # {"x": 340, "y": 218}

class TargetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frame_path: str | None = None     # CSS selector of iframe, or None for main frame
    candidates: list[LocatorCandidate]
    expected_matches: int = Field(default=1, ge=1)


# ─── Condition Models ─────────────────────────────────────────────────────────

class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str   # route_matches | text_visible | heading_visible | element_present |
                # http_status | timeout | locator_matches_multiple | all_candidates_failed
                # input_value_equals
    pattern: str | None = None   # for route_matches
    text: str | None = None      # for text_visible, heading_visible
    strategy: LocatorStrategy | None = None
    role: str | None = None
    name: str | None = None
    code: int | None = None      # for http_status
    after_ms: int | None = None  # for timeout
    value: str | None = None     # for input_value_equals

class ConditionGroup(BaseModel):
    """Combines multiple conditions with AND or OR logic."""
    model_config = ConfigDict(extra="forbid")
    all: list[Condition] | None = None   # ALL must be true (AND)
    any: list[Condition] | None = None   # ANY must be true (OR)


# ─── Step Models ──────────────────────────────────────────────────────────────

class StepDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    description: str
    action: ActionType
    target: TargetSpec | None = None        # None for navigate/press
    value: str | None = None               # literal or $inputs.param_name
    key: str | None = None                 # for press action
    url: str | None = None                 # for navigate action
    risk_level: RiskLevel = RiskLevel.SAFE
    preconditions: list[Condition] = Field(default_factory=list)
    postconditions: list[Condition] = Field(default_factory=list)
    output_name: str | None = None         # if this step reads a value, store as this name


# ─── Output Extraction ────────────────────────────────────────────────────────

class OutputExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frame_path: str | None = None
    strategy: LocatorStrategy
    role: str | None = None
    name: str | None = None
    selector: str | None = None
    method: Literal["text_content", "inner_text", "attribute"] = "text_content"
    attribute: str | None = None    # for method=attribute

class OutputDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: Literal["string", "number", "boolean"]
    description: str
    pattern: str | None = None       # regex for validation
    extract: OutputExtraction


# ─── Checkpoint and Business Outcome ─────────────────────────────────────────

class CheckpointDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    all: list[Condition] | None = None
    any: list[Condition] | None = None

class BusinessOutcomeDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    description: str
    recognizer: ConditionGroup


# ─── Error Taxonomy ───────────────────────────────────────────────────────────

class RecoverableCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    recognizer: ConditionGroup
    recovery: Literal["re_authenticate", "wait_and_retry", "dismiss_dialog"]
    max_retries: int = Field(default=3, ge=1, le=5)
    backoff_ms: int = Field(default=2000, ge=500)

class HardFailureDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    recognizer: ConditionGroup

class ErrorTaxonomy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recoverable: list[RecoverableCondition] = Field(default_factory=list)
    hard_failures: list[HardFailureDefinition] = Field(default_factory=list)


# ─── Policy ───────────────────────────────────────────────────────────────────

class CapabilityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    risk_class: Literal["read_only", "write", "irreversible"]
    allowed_actions: list[ActionType]
    allowed_routes: list[str]    # glob patterns


# ─── Target ───────────────────────────────────────────────────────────────────

class CapabilityTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    surface_type: SurfaceType = SurfaceType.PLAYWRIGHT_WEB
    app_family: str
    app_version: str = "1.0"
    entry_point: str


# ─── Input Parameter ─────────────────────────────────────────────────────────

class InputParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: Literal["string", "number", "boolean"]
    required: bool = True
    pattern: str | None = None
    description: str


# ─── Provenance ───────────────────────────────────────────────────────────────

class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discovery_run_id: str
    discovery_model: str
    compiler_version: str = "1.0.0"
    source_fingerprint: str | None = None
    status: ArtifactStatus = ArtifactStatus.DRAFT


# ─── ROOT: Capability Artifact ────────────────────────────────────────────────

class CapabilityArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str                          # e.g. "member.lookup_savings_balance"
    version: str = "1.0.0"            # semver
    description: str
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    target: CapabilityTarget
    policy: CapabilityPolicy
    inputs: list[InputParameter]
    steps: list[StepDefinition]
    outputs: list[OutputDefinition]
    success_condition: dict[str, str]  # {"checkpoint_id": "...", "description": "..."}
    checkpoints: dict[str, CheckpointDefinition]
    business_outcomes: list[BusinessOutcomeDefinition] = Field(default_factory=list)
    error_taxonomy: ErrorTaxonomy = Field(default_factory=ErrorTaxonomy)
    provenance: Provenance


# ─── Replay Result Models ─────────────────────────────────────────────────────

class FailureDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    step_id: str
    expected: str
    observed: str
    evidence: dict[str, str] = Field(default_factory=dict)  # {"screenshot": "path", ...}

class BusinessOutcomeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    description: str
    at_step: str

class ReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    capability_id: str
    capability_version: str
    status: ReplayStatus
    outputs: dict[str, Any] | None = None
    business_outcome: BusinessOutcomeResult | None = None
    failure: FailureDetail | None = None
    steps_completed: int = 0
    steps_total: int = 0
    duration_ms: int = 0
    evidence_path: str | None = None


# ─── JSONL Log Entry ──────────────────────────────────────────────────────────

class LogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ts: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    run_id: str
    capability_id: str | None = None
    execution_mode: ExecutionMode
    control_owner: ControlOwner = ControlOwner.AUTOMATION
    step_id: str | None = None
    seq: int
    action_type: ActionType | None = None
    locator_strategy_used: str | None = None
    locator_candidate_attempted: int | None = None
    frame_path: str | None = None
    route: str | None = None
    risk_class: RiskLevel | None = None
    outcome: str | None = None          # success | business_outcome | recoverable | hard_failure | paused
    outcome_code: str | None = None
    retry_attempt: int = 0
    duration_ms: int = 0
    precondition_result: str | None = None
    postcondition_result: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    model_reasoning: str | None = None  # ONLY set during discovery, null during replay


# ─── Intervention Request (for human escalation) ──────────────────────────────

class InterventionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    capability_id: str
    goal: str
    current_step_id: str | None
    current_step_seq: int
    reason: str                         # why escalation was triggered
    current_route: str
    screenshot_b64: str | None = None   # base64 PNG of current state
    aria_snapshot: str | None = None    # current ARIA snapshot
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    status: Literal["pending", "accepted", "completed"] = "pending"
    approval_token: str | None = None   # set when human accepts
```

---

## 3. policy/allowlist.json — EXACT CONTENT

```json
{
  "schema_version": "1.0",
  "allowed_origins": [
    "http://localhost:5000"
  ],
  "allowed_route_patterns": [
    "http://localhost:5000/members/**",
    "http://localhost:5000/frames/**",
    "http://localhost:5000/login",
    "http://localhost:5000/logout",
    "http://localhost:5000/transfers/confirm"
  ],
  "allowed_action_types": [
    "fill",
    "click",
    "press",
    "navigate",
    "read",
    "observe_screenshot",
    "assert",
    "wait"
  ],
  "blocked_routes": [
    "http://localhost:5000/admin/**",
    "http://localhost:5000/simulate/**"
  ],
  "risky_action_routes": [
    "http://localhost:5000/transfers/**"
  ],
  "pii_field_labels": [
    "password",
    "ssn",
    "social security",
    "account number",
    "routing number",
    "date of birth",
    "tax id"
  ]
}
```

---

## 4. policy/engine.py — IMPLEMENT

```python
"""
policy/engine.py
Allowlist enforcement. Called before EVERY browser action.
This is the security boundary — nothing bypasses it.
"""
import fnmatch
import json
from pathlib import Path
from capability.schema import ActionType, RiskLevel


class PolicyViolationError(Exception):
    """Raised when an action violates the allowlist policy."""
    def __init__(self, message: str, action: str, route: str):
        super().__init__(message)
        self.action = action
        self.route = route


class PolicyEngine:
    def __init__(self, allowlist_path: Path | None = None):
        if allowlist_path is None:
            allowlist_path = Path(__file__).parent / "allowlist.json"
        with open(allowlist_path) as f:
            self._config = json.load(f)

    def check_action(self, action_type: str, url: str) -> RiskLevel:
        """
        Check if an action is permitted.
        Returns the risk level if permitted.
        Raises PolicyViolationError if not permitted.
        """
        # Check action type is allowed
        if action_type not in self._config["allowed_action_types"]:
            raise PolicyViolationError(
                f"Action type '{action_type}' is not in allowlist",
                action=action_type,
                route=url,
            )

        # Check URL is not blocked
        for blocked in self._config.get("blocked_routes", []):
            if fnmatch.fnmatch(url, blocked):
                raise PolicyViolationError(
                    f"Route '{url}' is explicitly blocked",
                    action=action_type,
                    route=url,
                )

        # Check URL is in allowed routes
        allowed = False
        for pattern in self._config["allowed_route_patterns"]:
            if fnmatch.fnmatch(url, pattern):
                allowed = True
                break

        # Also allow exact origin match for root
        for origin in self._config["allowed_origins"]:
            if url.startswith(origin):
                allowed = True
                break

        if not allowed:
            raise PolicyViolationError(
                f"Route '{url}' is not in allowed route patterns",
                action=action_type,
                route=url,
            )

        # Determine risk level
        for risky_pattern in self._config.get("risky_action_routes", []):
            if fnmatch.fnmatch(url, risky_pattern):
                return RiskLevel.REQUIRES_CONFIRMATION

        return RiskLevel.SAFE

    def get_pii_field_labels(self) -> list[str]:
        return self._config.get("pii_field_labels", [])
```

---

## 5. policy/redactor.py — IMPLEMENT

```python
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
```

---

## 6. target_app/data/members.json — EXACT CONTENT

```json
{
  "members": [
    {
      "id": "10001",
      "name": "James Harrington",
      "status": "active",
      "accounts": [
        {"type": "Savings", "number": "SAV-10001", "balance": "4821.50", "currency": "USD"},
        {"type": "Checking", "number": "CHK-10001", "balance": "1203.77", "currency": "USD"}
      ]
    },
    {
      "id": "10002",
      "name": "Maria Delgado",
      "status": "active",
      "accounts": [
        {"type": "Savings", "number": "SAV-10002", "balance": "12450.00", "currency": "USD"},
        {"type": "Checking", "number": "CHK-10002", "balance": "890.23", "currency": "USD"}
      ]
    },
    {
      "id": "10003",
      "name": "Robert Chen",
      "status": "active",
      "accounts": [
        {"type": "Savings", "number": "SAV-10003", "balance": "750.00", "currency": "USD"}
      ]
    },
    {
      "id": "10004",
      "name": "Patricia Wallace",
      "status": "active",
      "accounts": [
        {"type": "Savings", "number": "SAV-10004", "balance": "33100.88", "currency": "USD"},
        {"type": "Checking", "number": "CHK-10004", "balance": "5240.15", "currency": "USD"},
        {"type": "Money Market", "number": "MM-10004", "balance": "50000.00", "currency": "USD"}
      ]
    },
    {
      "id": "10005",
      "name": "David Okafor",
      "status": "active",
      "accounts": [
        {"type": "Savings", "number": "SAV-10005", "balance": "2900.00", "currency": "USD"}
      ]
    },
    {
      "id": "10006",
      "name": "Susan Takahashi",
      "status": "active",
      "accounts": [
        {"type": "Savings", "number": "SAV-10006", "balance": "7640.30", "currency": "USD"},
        {"type": "Checking", "number": "CHK-10006", "balance": "310.00", "currency": "USD"}
      ]
    },
    {
      "id": "10007",
      "name": "Michael Torres",
      "status": "active",
      "accounts": [
        {"type": "Savings", "number": "SAV-10007", "balance": "1100.00", "currency": "USD"}
      ]
    },
    {
      "id": "10008",
      "name": "Linda Petrov",
      "status": "active",
      "accounts": [
        {"type": "Savings", "number": "SAV-10008", "balance": "9910.45", "currency": "USD"},
        {"type": "Checking", "number": "CHK-10008", "balance": "2200.00", "currency": "USD"}
      ]
    },
    {
      "id": "10009",
      "name": "Thomas Nguyen",
      "status": "active",
      "accounts": [
        {"type": "Savings", "number": "SAV-10009", "balance": "450.00", "currency": "USD"}
      ]
    },
    {
      "id": "10010",
      "name": "Angela Murphy",
      "status": "active",
      "accounts": [
        {"type": "Savings", "number": "SAV-10010", "balance": "18750.00", "currency": "USD"},
        {"type": "Checking", "number": "CHK-10010", "balance": "4500.00", "currency": "USD"}
      ]
    }
  ]
}
```
Notes:
- Member IDs 10001-10010 exist
- ANY other ID → "No member found" (MEMBER_NOT_FOUND business outcome)
- Member IDs starting with 9 → 403 Forbidden (PERMISSION_DENIED hard failure)
- Account numbers in JSON are display names only — not stored in artifacts

---

## 7. target_app/app.py — IMPLEMENT

Key requirements:
- `use_reloader=False, debug=False` always
- Session timeout: 2-minute idle → redirect to `/login?expired=true`
- Zero `data-testid` attributes in any template
- All inputs have `aria-label` attributes
- Tables use classes `row-data`, `col1`, `col2` only
- `/frames/accounts/<member_id>` → served inside iframe on member detail page
- `/simulate/error` → renders error page
- `/simulate/slow?delay=N` → sleeps N seconds
- Member IDs starting with 9 → 403 response
- GET `/members/search` → search form
- POST `/members/search` → process search, redirect to detail or show not-found
- GET `/members/<id>` → member detail (with iframe for accounts)
- GET `/members/<id>/transfer` → transfer form
- POST `/members/<id>/transfer` → validate and redirect to confirm
- GET/POST `/transfers/confirm` → confirmation page
- GET `/login` → login form (username: admin, password: admin)
- POST `/login` → authenticate, set session
- GET `/logout` → clear session

---

## 8. tests/test_schema.py — IMPLEMENT

Tests to write:
1. Load example capability JSON → validates against CapabilityArtifact model
2. LocatorCandidate with invalid priority (0 or 6) → raises ValidationError
3. LogEntry with model_reasoning set during replay → should be allowed (just validated, not blocked at schema level)
4. ReplayResult success has outputs, no failure, no business_outcome
5. ReplayResult business_outcome has outcome field, no outputs, no failure
6. ReplayResult hard_failure has failure field with all required fields
7. CapabilityArtifact with extra field → raises ValidationError (extra="forbid")
8. InputParameter with invalid type string → raises ValidationError

---

## 9. tests/test_guardrails.py — IMPLEMENT

Tests to write:
1. PolicyEngine allows `fill` action on allowed route → returns RiskLevel.SAFE
2. PolicyEngine blocks action on blocked route → raises PolicyViolationError
3. PolicyEngine blocks unknown action type → raises PolicyViolationError  
4. PolicyEngine returns RiskLevel.REQUIRES_CONFIRMATION for transfer route
5. PolicyEngine blocks URL not matching any allowed pattern
6. redact_string with SSN "123-45-6789" → returns "[SSN-REDACTED]"
7. redact_string with routing number "021000021" → returns "[ROUTING-REDACTED]"
8. redact_log_entry with nested dict containing SSN → all SSN occurrences redacted
9. should_mask_field("Password", pii_labels) → True
10. should_mask_field("Member Name", pii_labels) → False

---

## Checkpoint Verification Command

After completing Phase 1, run:
```bash
cd C:\Users\AbhishekkumarYadav\Documents\computer-use-automation
python -m pytest tests/test_schema.py tests/test_guardrails.py -v
python target_app/app.py &
curl http://localhost:5000/members/search
```

All must pass. Then update PHASE_STATUS.md marking Phase 1 complete.

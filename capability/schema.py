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

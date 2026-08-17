"""
surfaces/base.py
Abstract interface for all surface adapters.
Seam between the recorded capability (what) and execution mechanics (how).
web, legacy_web, desktop — all implement this interface.
The replay executor calls only Surface methods — it knows nothing about Playwright.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Observation:
    """What the agent sees at any given moment."""
    aria_snapshot: str          # raw YAML string from page.aria_snapshot(mode="ai")
    current_url: str            # full current URL
    screenshot_b64: str | None = None   # base64 PNG, only when requested


@dataclass
class ActionResult:
    """Result of executing one browser action."""
    success: bool
    error: str | None = None
    error_type: str | None = None   # element_not_found | ambiguous_locator | policy_violation | timeout | etc.
    extracted_value: str | None = None  # for read actions


@dataclass
class EvidenceRefs:
    """Paths to evidence files captured during a run."""
    screenshot_path: str | None = None
    aria_snapshot_path: str | None = None
    trace_path: str | None = None


class Surface(ABC):
    """
    Abstract surface adapter.

    Design rationale: The capability artifact describes WHAT to do and HOW to identify
    elements logically (role, name, label). The Surface adapter implements the mechanics
    of HOW to perceive and act on a specific surface type.

    This seam means artifacts are surface-agnostic at the logical level.
    A PlaywrightWebSurface handles modern and legacy web apps.
    A future WindowsUISurface would handle desktop apps via UI Automation API.
    The replay executor calls Surface.resolve_and_act() — it doesn't know which adapter runs underneath.
    """

    @abstractmethod
    async def observe(self, include_screenshot: bool = False) -> Observation:
        """
        Capture current page state.
        During discovery: always use mode="ai" to get refs and iframe content.
        Returns ARIA snapshot string (always) and optionally a base64 screenshot.
        """
        ...

    @abstractmethod
    async def resolve_and_act(
        self,
        action_type: str,
        target_spec: dict | None,
        value: str | None = None,
        key: str | None = None,
        url: str | None = None,
        risk_level: str = "safe",
        current_url: str = "",
    ) -> ActionResult:
        """
        Resolve locator candidates in priority order, then execute action.
        Policy check MUST happen inside this method BEFORE executing — never after.
        Strict mode: if locator matches != 1 element → return error, never guess.
        """
        ...

    @abstractmethod
    async def read_element(
        self,
        target_spec: dict,
        frame_path: str | None = None,
    ) -> tuple[bool, str]:
        """
        Read text content from an element.
        Returns (success, text_content).
        """
        ...

    @abstractmethod
    async def capture_evidence(
        self,
        run_id: str,
        step_id: str,
        label: str,
        evidence_dir: str,
    ) -> EvidenceRefs:
        """
        Capture screenshot + ARIA snapshot text + stop trace and save ZIP.
        Called on failures and at declared checkpoints.
        Save all three — do not assume trace.zip replaces aria.yaml.
        """
        ...

    @abstractmethod
    async def check_condition(self, condition: dict) -> bool:
        """
        Evaluate a single condition dict against current page state.
        Used by replay executor for preconditions, postconditions, checkpoints,
        and business outcome recognizers.
        """
        ...

    @abstractmethod
    async def navigate(self, url: str, current_url: str = "") -> ActionResult:
        """Navigate to URL (policy-checked inside this method before executing)."""
        ...

    @abstractmethod
    async def start_tracing(self) -> None:
        """Start Playwright trace. Call screenshots=True, snapshots=True, sources=True."""
        ...

    @abstractmethod
    async def stop_tracing(self, output_path: str) -> None:
        """Stop trace and save ZIP to output_path."""
        ...

    @abstractmethod
    async def take_screenshot(
        self,
        output_path: str | None = None,
        mask_locators: list[str] | None = None,
    ) -> str:
        """
        Take screenshot. mask_locators: CSS selectors of PII-containing elements to mask.
        Returns base64 PNG string. Saves to output_path if provided.
        """
        ...

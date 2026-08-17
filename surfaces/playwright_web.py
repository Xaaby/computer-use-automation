"""
surfaces/playwright_web.py
Playwright implementation of the Surface interface.
Handles modern and legacy web apps (no test IDs, table layouts, iframes).

ARCHITECTURE NOTE:
- Discovery mode: observe() uses mode="ai" to get refs + iframe content in one snapshot
- Replay mode: resolve_and_act() uses durable locator candidates — NO refs, NO LLM
- Human mode: same Page object kept alive, gate paused via EscalationController

WINDOWS NOTE:
asyncio.WindowsProactorEventLoopPolicy() is set by the entrypoint script (agent/loop.py,
replay/executor.py main blocks). This module does NOT set it — callers do.
"""
from __future__ import annotations

import base64
import fnmatch
import sys
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    async_playwright,
)

from policy.engine import PolicyEngine, PolicyViolationError
from surfaces.base import ActionResult, EvidenceRefs, Observation, Surface


class PlaywrightWebSurface(Surface):
    """Playwright-backed Surface for modern and legacy web UIs."""

    def __init__(
        self,
        page: Page,
        context: BrowserContext,
        policy_engine: PolicyEngine,
        max_action_timeout_ms: int = 10000,
    ):
        self._page = page
        self._context = context
        self._policy = policy_engine
        self._timeout = max_action_timeout_ms
        self._last_response_status: int | None = None
        # Track HTTP response status for condition checks
        page.on("response", lambda r: setattr(self, "_last_response_status", r.status))

    async def observe(self, include_screenshot: bool = False) -> Observation:
        # ALWAYS use mode="ai" — gets refs AND iframe content in one call
        # Confirmed by D1 research: default mode skips iframes, AI mode includes them
        aria_snapshot = await self._page.aria_snapshot(mode="ai")
        current_url = self._page.url

        screenshot_b64 = None
        if include_screenshot:
            png_bytes = await self._page.screenshot(full_page=False)
            screenshot_b64 = base64.b64encode(png_bytes).decode()

        return Observation(
            aria_snapshot=aria_snapshot,
            current_url=current_url,
            screenshot_b64=screenshot_b64,
        )

    def _build_locator(self, candidate: dict, page_or_frame) -> Locator | None:
        """
        Build a Playwright locator from a candidate dict.
        candidate = {"strategy": "role", "role": "textbox", "name": "Member ID", "exact": True}
        Returns None for coords strategy (handled separately).
        """
        strategy = candidate.get("strategy")
        if strategy == "role":
            kwargs = {}
            if candidate.get("name"):
                kwargs["name"] = candidate["name"]
                kwargs["exact"] = candidate.get("exact", True)
            return page_or_frame.get_by_role(candidate["role"], **kwargs)
        elif strategy == "label":
            return page_or_frame.get_by_label(candidate["text"], exact=True)
        elif strategy == "placeholder":
            return page_or_frame.get_by_placeholder(candidate["text"])
        elif strategy == "text":
            return page_or_frame.get_by_text(
                candidate["text"], exact=candidate.get("exact", True)
            )
        elif strategy == "css":
            return page_or_frame.locator(candidate["selector"])
        elif strategy == "coords":
            return None  # handled separately with page.mouse.click(x, y)
        return None

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
        # Step 1: Policy check BEFORE anything else.
        # Decision: for navigate, check the DESTINATION url (not about:blank /
        # prior page). Otherwise the initial goto from a blank tab always fails.
        policy_url = url if action_type == "navigate" and url else (
            current_url or self._page.url
        )
        try:
            self._policy.check_action(action_type, policy_url)
        except PolicyViolationError as e:
            return ActionResult(
                success=False, error=str(e), error_type="policy_violation"
            )

        # Step 2: Handle navigation separately
        if action_type == "navigate" and url:
            try:
                await self._page.goto(
                    url, wait_until="domcontentloaded", timeout=self._timeout
                )
                return ActionResult(success=True)
            except Exception as e:
                return ActionResult(
                    success=False, error=str(e), error_type="navigation_error"
                )

        # Step 3: Handle press separately
        if action_type == "press" and key:
            await self._page.keyboard.press(key)
            return ActionResult(success=True)

        # Step 4: Resolve target element from candidates
        if not target_spec:
            return ActionResult(
                success=False,
                error="No target_spec provided",
                error_type="missing_target",
            )

        candidates = sorted(
            target_spec.get("candidates", []), key=lambda c: c.get("priority", 99)
        )
        frame_path = target_spec.get("frame_path")

        # Determine the page or frame to act on.
        # Decision: aria-ref selectors are page-scoped (Playwright resolves across
        # frames). Never wrap them in frame_locator.
        uses_aria_ref = any(
            "aria-ref=" in str(c.get("selector", ""))
            for c in candidates
        )
        if frame_path and not uses_aria_ref:
            page_or_frame = self._page.frame_locator(frame_path)
        else:
            page_or_frame = self._page

        resolved_locator = None
        for candidate in candidates:
            locator = self._build_locator(candidate, page_or_frame)
            if locator is None:
                continue  # coords handled separately below
            try:
                count = await locator.count()
            except Exception:
                continue

            if count == 0:
                continue
            if count > 1:
                # Strict mode: multiple matches = hard failure, never guess
                return ActionResult(
                    success=False,
                    error=f"Locator {candidate} matched {count} elements — ambiguous",
                    error_type="ambiguous_locator",
                )
            resolved_locator = locator
            break

        # Coords fallback
        if resolved_locator is None:
            for candidate in candidates:
                if candidate.get("strategy") == "coords":
                    coords = candidate.get("coords", {})
                    x, y = coords.get("x", 0), coords.get("y", 0)
                    if action_type == "click":
                        await self._page.mouse.click(x, y)
                        return ActionResult(success=True)

        if resolved_locator is None:
            return ActionResult(
                success=False,
                error="All locator candidates failed to resolve",
                error_type="all_candidates_failed",
            )

        # Step 5: Execute the action
        try:
            if action_type == "click":
                await resolved_locator.click(timeout=self._timeout)
            elif action_type == "fill":
                await resolved_locator.fill(value or "", timeout=self._timeout)
            elif action_type == "read":
                text = await resolved_locator.inner_text(timeout=self._timeout)
                return ActionResult(success=True, extracted_value=text)
            else:
                return ActionResult(
                    success=False,
                    error=f"Unknown action: {action_type}",
                    error_type="unknown_action",
                )
            return ActionResult(success=True)
        except Exception as e:
            err_str = str(e)
            error_type = "timeout" if "timeout" in err_str.lower() else "action_error"
            return ActionResult(success=False, error=err_str, error_type=error_type)

    async def read_element(
        self,
        target_spec: dict,
        frame_path: str | None = None,
    ) -> tuple[bool, str]:
        """Read text content from an element via locator candidates."""
        effective_spec = dict(target_spec)
        if frame_path is not None:
            effective_spec["frame_path"] = frame_path

        result = await self.resolve_and_act(
            action_type="read",
            target_spec=effective_spec,
            current_url=self._page.url,
        )
        if result.success:
            return True, result.extracted_value or ""
        return False, result.error or "read failed"

    async def check_condition(self, condition: dict) -> bool:
        ctype = condition.get("type")
        if ctype == "route_matches":
            return fnmatch.fnmatch(self._page.url, condition.get("pattern", ""))
        elif ctype == "text_visible":
            return await self._page.get_by_text(condition["text"]).count() > 0
        elif ctype == "heading_visible":
            return (
                await self._page.get_by_role(
                    "heading", name=condition["text"]
                ).count()
                > 0
            )
        elif ctype == "element_present":
            strategy = condition.get("strategy", "role")
            if strategy == "role" or (
                hasattr(strategy, "value") and strategy.value == "role"
            ):
                role = condition.get("role") or ""
                name = condition.get("name") or ""
                kwargs = {}
                if name:
                    kwargs["name"] = name
                loc = self._page.get_by_role(role, **kwargs)
            elif strategy == "css" or (
                hasattr(strategy, "value") and strategy.value == "css"
            ):
                loc = self._page.locator(condition["selector"])
            else:
                return False
            return await loc.count() > 0
        elif ctype == "http_status":
            return self._last_response_status == condition.get("code")
        elif ctype == "input_value_equals":
            # Fully implemented in Phase 4 conditions; stub keeps discovery usable
            # Decision: return True when no target available (compiler may omit it)
            text = condition.get("text") or condition.get("name")
            value = condition.get("value", "")
            if not text:
                return True
            loc = self._page.get_by_label(text, exact=True)
            if await loc.count() != 1:
                loc = self._page.get_by_role("textbox", name=text, exact=True)
            if await loc.count() != 1:
                return False
            actual = await loc.input_value()
            return actual == value
        return False

    async def capture_evidence(
        self,
        run_id: str,
        step_id: str,
        label: str,
        evidence_dir: str,
    ) -> EvidenceRefs:
        """
        Capture screenshot + ARIA snapshot + stop trace.
        IMPORTANT: Save all three separately — trace.zip does not replace aria.yaml.
        Confirmed by D4 research: Trace Viewer shows DOM snapshots but not raw ARIA YAML.
        Keep paths shallow to avoid Windows 260-char limit.
        """
        base = Path(evidence_dir) / run_id
        base.mkdir(parents=True, exist_ok=True)

        # Screenshot with PII masking
        pii_labels = self._policy.get_pii_field_labels()
        mask_locators = []
        for pii_label in pii_labels:
            loc = self._page.get_by_label(pii_label)
            if await loc.count() > 0:
                mask_locators.append(loc)

        png_path = base / f"{step_id}_{label}.png"
        await self._page.screenshot(
            path=str(png_path),
            mask=mask_locators if mask_locators else None,
        )

        # ARIA snapshot as text file
        aria_path = base / f"{step_id}_{label}_aria.yaml"
        aria = await self._page.aria_snapshot(mode="ai")
        aria_path.write_text(aria, encoding="utf-8")

        # Stop trace
        trace_file = base / f"{step_id}_{label}_trace.zip"
        trace_path_str: str | None
        try:
            await self._context.tracing.stop(path=str(trace_file))
            trace_path_str = str(trace_file)
        except Exception:
            trace_path_str = None  # tracing may not have been started

        return EvidenceRefs(
            screenshot_path=str(png_path),
            aria_snapshot_path=str(aria_path),
            trace_path=trace_path_str,
        )

    async def navigate(self, url: str, current_url: str = "") -> ActionResult:
        return await self.resolve_and_act(
            action_type="navigate",
            target_spec=None,
            url=url,
            current_url=current_url or self._page.url,
        )

    async def start_tracing(self) -> None:
        # screenshots=True: filmstrip; snapshots=True: DOM snapshots + network
        # sources=True: source files (useful for debugging)
        await self._context.tracing.start(
            screenshots=True,
            snapshots=True,
            sources=True,
        )

    async def stop_tracing(self, output_path: str) -> None:
        await self._context.tracing.stop(path=output_path)

    async def take_screenshot(
        self,
        output_path: str | None = None,
        mask_locators: list[str] | None = None,
    ) -> str:
        masks = []
        if mask_locators:
            for sel in mask_locators:
                loc = self._page.locator(sel)
                if await loc.count() > 0:
                    masks.append(loc)

        kwargs: dict = {"full_page": False}
        if masks:
            kwargs["mask"] = masks
        if output_path:
            kwargs["path"] = output_path

        png_bytes = await self._page.screenshot(**kwargs)
        return base64.b64encode(png_bytes).decode()

    @classmethod
    async def create(
        cls,
        headless: bool = False,
        policy_engine: PolicyEngine | None = None,
        max_action_timeout_ms: int = 10000,
    ) -> tuple["PlaywrightWebSurface", "Browser", "BrowserContext", object]:
        """
        Launch browser and create surface.
        Returns (surface, browser, context, playwright_cm).
        playwright_cm is the async context manager — call await cm.__aexit__ on cleanup.
        headless=False: human can see and operate the browser window during escalation.
        """
        # Decision: keep the AsyncPlaywright context manager (not the Playwright
        # instance) so callers can __aexit__ correctly on Windows.
        pw_cm = async_playwright()
        pw = await pw_cm.__aenter__()
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--disable-gpu"] if sys.platform == "win32" else [],
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        if policy_engine is None:
            policy_engine = PolicyEngine()

        surface = cls(page, context, policy_engine, max_action_timeout_ms)
        return surface, browser, context, pw_cm

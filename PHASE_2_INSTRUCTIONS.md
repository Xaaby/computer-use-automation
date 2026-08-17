# Phase 2 Instructions — Surface + Tools
## Read RULES.md first. Phase 1 must be complete before starting this.

## Key Research Findings That Shape This Phase (D1, D2, D7)

**D1 confirmed:** `page.aria_snapshot(mode="ai")` includes iframe contents AND element refs in one call.
Default mode (no `mode`) does NOT include iframes. Always use `mode="ai"` during discovery.

**D2 confirmed:** Use `asyncio.run(main())` at the entrypoint. Do not set event loop policy inside
library modules — set it once at the top of entrypoint scripts only.

**D7 confirmed:** `Locator.normalize()` exists in Playwright ≥1.49. Use it to convert an ephemeral
`aria-ref=e17` locator into a durable role/name/label locator. Never store the `e17` ref in artifacts.

**Unlabeled table cells:** appear in ARIA snapshot as `cell: "text content"` — the cell text IS the name.
**aria-label on input:** becomes the accessible name → `get_by_role("textbox", name="Member ID")` works.

---

## What to Build in This Phase

```
surfaces/
    __init__.py
    base.py            ← Abstract Surface interface
    playwright_web.py  ← Playwright implementation

agent/
    __init__.py
    tools.py           ← 8 tool definitions as JSON schemas (Bedrock toolSpec format)
    prompts.py         ← System prompt + observation formatter
```

---

## 1. surfaces/base.py — IMPLEMENT EXACTLY

```python
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
```

---

## 2. surfaces/playwright_web.py — IMPLEMENT

```python
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
import asyncio
import base64
import sys
from pathlib import Path

from playwright.async_api import (
    Browser, BrowserContext, Page, Locator,
    async_playwright
)
from policy.engine import PolicyEngine, PolicyViolationError
from policy.redactor import should_mask_field
from capability.schema import RiskLevel
from surfaces.base import Surface, Observation, ActionResult, EvidenceRefs
```

### Implement `PlaywrightWebSurface(Surface)` with these methods:

**Constructor:**
```python
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
```

**`observe()` method:**
```python
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
```

**`_build_locator()` private method:**
```python
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
        return page_or_frame.get_by_text(candidate["text"], exact=candidate.get("exact", True))
    elif strategy == "css":
        return page_or_frame.locator(candidate["selector"])
    elif strategy == "coords":
        return None  # handled separately with page.mouse.click(x, y)
    return None
```

**`resolve_and_act()` method:**
```python
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
    # Step 1: Policy check BEFORE anything else
    try:
        self._policy.check_action(action_type, current_url or self._page.url)
    except PolicyViolationError as e:
        return ActionResult(success=False, error=str(e), error_type="policy_violation")

    # Step 2: Handle navigation separately
    if action_type == "navigate" and url:
        try:
            await self._page.goto(url, wait_until="domcontentloaded",
                                  timeout=self._timeout)
            return ActionResult(success=True)
        except Exception as e:
            return ActionResult(success=False, error=str(e), error_type="navigation_error")

    # Step 3: Handle press separately
    if action_type == "press" and key:
        await self._page.keyboard.press(key)
        return ActionResult(success=True)

    # Step 4: Resolve target element from candidates
    if not target_spec:
        return ActionResult(success=False, error="No target_spec provided", error_type="missing_target")

    candidates = sorted(target_spec.get("candidates", []), key=lambda c: c.get("priority", 99))
    frame_path = target_spec.get("frame_path")

    # Determine the page or frame to act on
    if frame_path:
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
            return ActionResult(success=False, error=f"Unknown action: {action_type}", error_type="unknown_action")
        return ActionResult(success=True)
    except Exception as e:
        err_str = str(e)
        error_type = "timeout" if "timeout" in err_str.lower() else "action_error"
        return ActionResult(success=False, error=err_str, error_type=error_type)
```

**`check_condition()` method — handle all condition types:**
```python
async def check_condition(self, condition: dict) -> bool:
    ctype = condition.get("type")
    if ctype == "route_matches":
        import fnmatch
        return fnmatch.fnmatch(self._page.url, condition.get("pattern", ""))
    elif ctype == "text_visible":
        return await self._page.get_by_text(condition["text"]).count() > 0
    elif ctype == "heading_visible":
        return await self._page.get_by_role("heading", name=condition["text"]).count() > 0
    elif ctype == "element_present":
        strategy = condition.get("strategy", "role")
        if strategy == "role":
            loc = self._page.get_by_role(condition["role"], name=condition.get("name", ""))
        elif strategy == "css":
            loc = self._page.locator(condition["selector"])
        else:
            return False
        return await loc.count() > 0
    elif ctype == "http_status":
        return self._last_response_status == condition.get("code")
    elif ctype == "input_value_equals":
        # check via target_spec resolution — simplified version
        return True  # implement fully in Phase 4
    return False
```

**`capture_evidence()` method:**
```python
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
    mask_locators = [
        self._page.get_by_label(label)
        for label in pii_labels
        if await self._page.get_by_label(label).count() > 0
    ]
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
    trace_path = base / f"{step_id}_{label}_trace.zip"
    try:
        await self._context.tracing.stop(path=str(trace_path))
    except Exception:
        trace_path = None  # tracing may not have been started

    return EvidenceRefs(
        screenshot_path=str(png_path),
        aria_snapshot_path=str(aria_path),
        trace_path=str(trace_path) if trace_path else None,
    )
```

**`start_tracing()` and `stop_tracing()` methods:**
```python
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
```

**Class method for creating the surface:**
```python
@classmethod
async def create(
    cls,
    headless: bool = False,
    policy_engine: PolicyEngine | None = None,
    max_action_timeout_ms: int = 10000,
) -> tuple["PlaywrightWebSurface", "Browser", "BrowserContext", object]:
    """
    Launch browser and create surface.
    Returns (surface, browser, context, playwright_instance).
    Caller is responsible for cleanup.
    headless=False: human can see and operate the browser window during escalation.
    """
    pw = await async_playwright().__aenter__()
    browser = await pw.chromium.launch(
        headless=headless,
        args=["--disable-gpu"] if sys.platform == "win32" else [],
    )
    context = await browser.new_context(viewport={"width": 1280, "height": 800})
    page = await context.new_page()

    if policy_engine is None:
        policy_engine = PolicyEngine()

    surface = cls(page, context, policy_engine, max_action_timeout_ms)
    return surface, browser, context, pw
```

---

## 3. agent/tools.py — IMPLEMENT EXACTLY

**CRITICAL: Bedrock `converse()` uses `toolSpec` with `inputSchema.json` format.**
This is DIFFERENT from Anthropic SDK which uses `input_schema` directly.

```python
"""
agent/tools.py
The 8 tools Claude can call during discovery.
Format: Bedrock converse() toolSpec format — NOT Anthropic SDK format.
Sent as toolConfig={"tools": DISCOVERY_TOOLS} in every bedrock.converse() call.
"""

DISCOVERY_TOOLS = [
    {
        "toolSpec": {
            "name": "click",
            "description": (
                "Click an element identified by its ARIA snapshot ref. "
                "Set risk_level='irreversible_commit' ONLY for final transaction submission buttons. "
                "Set risk_level='requires_confirmation' for form submissions that change data. "
                "Default is 'safe' for navigation and read-only actions."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["ref"],
                    "properties": {
                        "ref": {
                            "type": "string",
                            "description": "Element ref from aria_snapshot e.g. e17"
                        },
                        "risk_level": {
                            "type": "string",
                            "enum": ["safe", "requires_confirmation", "irreversible_commit"],
                            "description": "Risk classification for this action"
                        }
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "fill",
            "description": (
                "Fill an input field. Use '$inputs.param_name' syntax to reference "
                "a capability input parameter (e.g. '$inputs.member_id'). "
                "Use a literal string for fixed values."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["ref", "value"],
                    "properties": {
                        "ref": {"type": "string", "description": "Element ref from aria_snapshot"},
                        "value": {
                            "type": "string",
                            "description": "Literal value or $inputs.param_name reference"
                        }
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "press",
            "description": "Press a keyboard key or combination",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["key"],
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Key or combo: Enter, Tab, Escape, Control+a"
                        }
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "navigate",
            "description": "Navigate to a URL. Only URLs on the policy allowlist are permitted.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {"type": "string", "description": "Full URL to navigate to"}
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "read",
            "description": "Read and extract the text content of an element for the capability output.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["ref", "output_name"],
                    "properties": {
                        "ref": {"type": "string", "description": "Element ref from aria_snapshot"},
                        "output_name": {
                            "type": "string",
                            "description": "Key to store this value under in outputs e.g. savings_balance"
                        }
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "observe_screenshot",
            "description": (
                "Capture a screenshot when the ARIA accessibility tree is insufficient. "
                "Screenshots are expensive — prefer ARIA tree navigation. "
                "Use only when you cannot identify an element from the ARIA snapshot."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {}
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "done",
            "description": "Signal goal completion with all extracted output values.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["outputs", "success_description"],
                    "properties": {
                        "outputs": {
                            "type": "object",
                            "description": "All extracted output key-value pairs"
                        },
                        "success_description": {
                            "type": "string",
                            "description": "Human-readable description of what was accomplished"
                        }
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "escalate",
            "description": (
                "Signal that automation cannot safely proceed and needs human help. "
                "Use when: stuck in a loop, facing a risky action needing approval, "
                "or in an unexpected UI state after multiple attempts."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["reason"],
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Why escalation is needed"
                        },
                        "stuck_description": {
                            "type": "string",
                            "description": "Detailed description of the current blocked state"
                        }
                    }
                }
            }
        }
    }
]
```

---

## 4. agent/prompts.py — IMPLEMENT

```python
"""
agent/prompts.py
System prompt and observation formatter for Claude discovery agent.
"""

SYSTEM_PROMPT = """You are a bank back-office automation agent. Your job is to accomplish
a specific goal by navigating a legacy credit union admin web UI.

## Your Rules
- Call ONLY the 8 tools provided. No other actions.
- Interact ONLY with elements visible in the ARIA snapshot.
- Classify risk_level accurately:
  - safe: navigation, search, read-only
  - requires_confirmation: form submissions that modify data
  - irreversible_commit: final transaction commits ONLY (e.g. "Confirm Transfer" button)
- Use '$inputs.param_name' when filling with capability input parameters.
- Call done() when goal is fully accomplished with all outputs extracted.
- Call escalate() if you cannot proceed safely.

## Reading the ARIA Snapshot
The ARIA snapshot is a YAML accessibility tree. Each element has:
- A role: textbox, button, link, cell, heading, etc.
- An accessible name (quoted): "Member ID", "Search", etc.
- A ref like [ref=e17] — use this in tool calls to identify the element.
- Elements inside iframes appear nested in the snapshot (already included).

## Parameter References
When filling input fields with capability parameters, use $inputs.param_name syntax.
Example: fill(ref="e3", value="$inputs.member_id")
This tells the compiler the step uses an input parameter, not a hardcoded value.

## Output Extraction
Use read(ref="...", output_name="savings_balance") to extract values.
output_name must match a declared capability output.

## History Awareness
You see your recent action history. If an action failed twice, try a different approach.
Do not repeat exactly the same failed action.

## Efficiency
Stop immediately when the goal is achieved. Do not take extra exploratory actions."""


def format_observation(
    aria_snapshot: str,
    current_url: str,
    step_number: int,
    goal: str,
    action_history: list[dict],
    available_inputs: dict[str, str],
) -> str:
    """
    Format current page state into a user message for Claude.
    Keeps the last 10 history entries to manage context window.
    """
    history_text = ""
    if action_history:
        recent = action_history[-10:]
        lines = []
        for h in recent:
            status = "✓" if h.get("success") else "✗"
            lines.append(f"  {status} Step {h['seq']}: {h['action']} — {h.get('note', '')}")
        history_text = "\n## Action History (last 10)\n" + "\n".join(lines)

    inputs_text = ""
    if available_inputs:
        lines = [f"  {k} = {v}" for k, v in available_inputs.items()]
        inputs_text = "\n## Available Input Parameters\n" + "\n".join(lines)

    return f"""## Goal
{goal}

## Current State
Step: {step_number}
URL: {current_url}
{inputs_text}{history_text}

## Current Page (ARIA Snapshot — includes iframes)
{aria_snapshot}

What is the next action to take?"""
```

---

## Checkpoint Verification

After Phase 2, run this and confirm it works:
```bash
python -c "
import asyncio, sys
asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=False)
        ctx = await b.new_context()
        page = await ctx.new_page()
        await page.goto('http://localhost:5000/members/search')
        
        # D1 confirmed: mode='ai' gets refs + iframe content
        snap = await page.aria_snapshot(mode='ai')
        print('ARIA snapshot OK, length:', len(snap))
        print('Has refs:', '[ref=' in snap)
        print(snap[:600])
        
        await b.close()

asyncio.run(test())
"
```
Expected: snapshot contains `[ref=e` strings and accessible names like "Member ID".

Update PHASE_STATUS.md after completion.

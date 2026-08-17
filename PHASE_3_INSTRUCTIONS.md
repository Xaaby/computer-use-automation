# Phase 3 Instructions — Discovery Agent + Compiler
## Read RULES.md first. Phases 1 and 2 must be complete.

## Key Research Findings That Shape This Phase (D5, D7)

**D5 — Multi-turn loop:** The loop structure is confirmed. `stop_reason == "tool_use"` means Claude
wants a tool executed. `stop_reason == "end_turn"` means Claude is done. Append the full assistant
response content (including tool_use blocks) before appending tool_result messages.

**D7 — Ref → Durable Locator (MAJOR UPDATE):** The compiler algorithm is substantially better than
the original spec. Use `locator.normalize()` to convert an ephemeral ref locator to a durable one.
Do NOT attempt to parse ARIA YAML and guess a locator — use Playwright's own normalization.

```python
# D7 confirmed algorithm:
ephemeral = page.locator("aria-ref=e17")      # resolve immediately after snapshot
assert await ephemeral.count() == 1
durable = ephemeral.normalize()                # Playwright generates best-practice locator
assert await durable.count() == 1
# Store durable, NEVER store "e17"
```

**iframe refs:** With `mode="ai"`, iframe contents are in the same snapshot. Refs inside iframes
still resolve via `page.locator("aria-ref=eN")` — Playwright handles frame context internally.
The artifact STILL needs to store frame_path explicitly so replay works without refs.

**D5 uses Anthropic SDK, not boto3:** The research example uses `anthropic.Anthropic()`. Our code
uses `boto3.converse()`. The LOOP LOGIC is the same; the API call format is different. Use the
Bedrock format from RULES.md — not the Anthropic SDK format from the research document.

---

## Critical: Bedrock API (NOT Anthropic SDK)

```python
import boto3, os
from dotenv import load_dotenv
load_dotenv()

bedrock = boto3.client(
    "bedrock-runtime",
    region_name=os.environ["AWS_REGION"],
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
)
MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
```

## Bedrock converse() Message Format

```python
# User message
{"role": "user", "content": [{"text": "your text here"}]}

# Assistant message with tool use (what Bedrock returns)
{"role": "assistant", "content": [
    {"toolUse": {"toolUseId": "id", "name": "click", "input": {"ref": "e17", "risk_level": "safe"}}}
]}

# User message with tool result (what you send back)
{"role": "user", "content": [
    {"toolResult": {"toolUseId": "id", "content": [{"text": "clicked successfully"}]}}
]}

# Tool result with error
{"role": "user", "content": [
    {"toolResult": {"toolUseId": "id", "content": [{"text": "element not found"}], "status": "error"}}
]}
```

## Detecting stop reason from Bedrock
```python
stop_reason = response["stopReason"]
# "tool_use"  → Claude wants to call a tool (check response["output"]["message"]["content"])
# "end_turn"  → Claude is done (extract text from content blocks)
# "max_tokens" → context overflow, truncate history and retry
```

## Extracting tool use from Bedrock response
```python
tool_uses = [
    block["toolUse"]
    for block in response["output"]["message"]["content"]
    if "toolUse" in block
]
# Each tool_use has: {"toolUseId": "...", "name": "click", "input": {...}}
```

---

## What to Build

### agent/loop.py

```python
"""
agent/loop.py
Discovery agent: LLM observe→decide→act loop.
Uses AWS Bedrock (boto3 converse API) — NOT Anthropic SDK.
Runs until: done() called, escalate() called, max_steps, or timeout.

WINDOWS ENTRYPOINT: sets ProactorEventLoop before any async work.
"""
import asyncio
import sys

# MUST be before any other async imports on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import boto3
from dotenv import load_dotenv
import os

load_dotenv()
```

**DiscoveryStep dataclass:**
```python
@dataclass
class DiscoveryStep:
    """One step recorded during discovery. Used by compiler to build artifact."""
    seq: int
    tool_name: str
    tool_input: dict
    aria_snapshot_before: str      # ARIA snapshot before this action (mode="ai")
    current_url_before: str        # URL before this action
    ref_used: str | None           # the ephemeral ref (e.g. "e17") if tool used one
    result_text: str
    is_error: bool
    risk_level: str = "safe"
    param_refs_found: list[str] = field(default_factory=list)  # ["$inputs.member_id"]
    model_reasoning: str | None = None   # any text Claude returned alongside tool_use
```

**DiscoveryAgent class constructor:**
```python
class DiscoveryAgent:
    def __init__(
        self,
        goal: str,
        entry_point: str,
        input_params: dict[str, str],
        max_steps: int = 30,
        max_duration_seconds: int = 120,
        headless: bool = False,
        output_dir: Path | None = None,
    ):
        self._goal = goal
        self._entry_point = entry_point
        self._input_params = input_params
        self._max_steps = max_steps
        self._max_duration_seconds = max_duration_seconds
        self._headless = headless
        self._output_dir = output_dir or Path("capabilities")
        self._bedrock = boto3.client(
            "bedrock-runtime",
            region_name=os.environ["AWS_REGION"],
            aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        )
        self._model_id = os.environ["BEDROCK_MODEL_ID"]
        self._trajectory: list[DiscoveryStep] = []
        self._final_outputs: dict = {}
        self._run_id = str(uuid4())
```

**Main `run()` method flow:**
```python
async def run(self) -> dict | None:
    """
    Run discovery. Returns the compiled artifact dict on success, None on failure.
    """
    from surfaces.playwright_web import PlaywrightWebSurface
    from agent.compiler import ArtifactCompiler
    from agent.prompts import SYSTEM_PROMPT, format_observation
    from agent.tools import DISCOVERY_TOOLS

    # Set up evidence directory
    evidence_dir = Path("evidence") / "runs" / self._run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    log_path = evidence_dir / "discovery.jsonl"

    # Create surface
    surface, browser, context, pw = await PlaywrightWebSurface.create(
        headless=self._headless,
        max_action_timeout_ms=10000,
    )

    await surface.start_tracing()
    await surface.navigate(self._entry_point)

    conversation_history = []
    action_history = []
    step = 0
    start_time = time.time()

    try:
        while step < self._max_steps:
            # Check timeout
            if time.time() - start_time > self._max_duration_seconds:
                print(f"Discovery timeout after {self._max_duration_seconds}s")
                break

            # Observe current state
            obs = await surface.observe(include_screenshot=False)

            # Format as user message
            user_text = format_observation(
                aria_snapshot=obs.aria_snapshot,
                current_url=obs.current_url,
                step_number=step + 1,
                goal=self._goal,
                action_history=action_history,
                available_inputs=self._input_params,
            )

            # Append to conversation
            conversation_history.append({
                "role": "user",
                "content": [{"text": user_text}]
            })

            # Call Bedrock
            response = self._bedrock.converse(
                modelId=self._model_id,
                system=[{"text": SYSTEM_PROMPT}],
                messages=conversation_history,
                toolConfig={"tools": DISCOVERY_TOOLS},
                inferenceConfig={"maxTokens": 2048, "temperature": 0},
            )

            stop_reason = response["stopReason"]
            assistant_content = response["output"]["message"]["content"]

            # Extract any text reasoning Claude included
            model_reasoning = " ".join(
                block["text"] for block in assistant_content if "text" in block
            ) or None

            # Append assistant response to history (FULL content, including tool_use blocks)
            conversation_history.append({
                "role": "assistant",
                "content": assistant_content,
            })

            if stop_reason == "end_turn":
                # Claude finished without a tool call — unexpected, treat as done
                print("Claude returned end_turn without done() — treating as stuck")
                break

            elif stop_reason == "max_tokens":
                # Truncate oldest history entries (keep system + last 6 turns)
                if len(conversation_history) > 8:
                    conversation_history = conversation_history[-6:]
                continue

            elif stop_reason == "tool_use":
                tool_uses = [
                    block["toolUse"]
                    for block in assistant_content
                    if "toolUse" in block
                ]

                tool_results = []

                for tool_use in tool_uses:
                    tool_name = tool_use["name"]
                    tool_input = tool_use["input"]
                    tool_use_id = tool_use["toolUseId"]

                    # Record snapshot before action
                    snap_before = obs.aria_snapshot
                    url_before = obs.current_url

                    result_text, is_error = await self._execute_tool(
                        tool_name, tool_input, surface, obs
                    )

                    # Record trajectory step
                    disc_step = DiscoveryStep(
                        seq=step + 1,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        aria_snapshot_before=snap_before,
                        current_url_before=url_before,
                        ref_used=tool_input.get("ref"),
                        result_text=result_text,
                        is_error=is_error,
                        risk_level=tool_input.get("risk_level", "safe"),
                        param_refs_found=[
                            v for v in tool_input.values()
                            if isinstance(v, str) and v.startswith("$inputs.")
                        ],
                        model_reasoning=model_reasoning,
                    )
                    self._trajectory.append(disc_step)

                    # Log to JSONL
                    self._log_step(log_path, disc_step, obs.current_url)

                    # Track for history display
                    action_history.append({
                        "seq": step + 1,
                        "action": f"{tool_name}({json.dumps(tool_input)[:60]})",
                        "success": not is_error,
                        "note": result_text[:80],
                    })

                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [{"text": result_text}],
                            **({"status": "error"} if is_error else {}),
                        }
                    })

                    # Check terminal tools
                    if tool_name == "done":
                        # Compile artifact
                        compiler = ArtifactCompiler()
                        artifact = await compiler.compile(
                            trajectory=self._trajectory,
                            goal=self._goal,
                            entry_point=self._entry_point,
                            input_params=self._input_params,
                            final_outputs=self._final_outputs,
                            run_id=self._run_id,
                            surface=surface,
                        )
                        self._output_dir.mkdir(parents=True, exist_ok=True)
                        artifact_path = self._output_dir / f"{artifact['name']}.capability.json"
                        artifact_path.write_text(json.dumps(artifact, indent=2))
                        print(f"Artifact saved: {artifact_path}")
                        return artifact

                    if tool_name == "escalate":
                        print(f"Agent escalated: {tool_input.get('reason')}")
                        break

                # Send tool results back
                conversation_history.append({
                    "role": "user",
                    "content": tool_results,
                })

                step += 1

        return None  # Discovery did not complete

    finally:
        trace_path = evidence_dir / "discovery_trace.zip"
        await surface.stop_tracing(str(trace_path))
        await context.close()
        await browser.close()
        await pw.__aexit__(None, None, None)
```

**`_execute_tool()` method:**
```python
async def _execute_tool(
    self,
    tool_name: str,
    tool_input: dict,
    surface,
    obs,
) -> tuple[str, bool]:
    """Execute a tool call. Returns (result_text, is_error)."""

    try:
        if tool_name == "navigate":
            result = await surface.navigate(tool_input["url"])
            return ("Navigated successfully", False) if result.success else (result.error, True)

        elif tool_name == "press":
            result = await surface._page.keyboard.press(tool_input["key"])
            return ("Key pressed", False)

        elif tool_name == "observe_screenshot":
            obs_with_ss = await surface.observe(include_screenshot=True)
            return (f"Screenshot captured. URL: {obs_with_ss.current_url}", False)

        elif tool_name in ("click", "fill", "read"):
            ref = tool_input.get("ref")
            if ref:
                # Resolve ref immediately using aria-ref locator
                # D7 confirmed: aria-ref=eN resolves from Playwright's snapshot cache
                target_spec = self._ref_to_target_spec(ref, obs.aria_snapshot)
            else:
                return ("No ref provided", True)

            value = tool_input.get("value")
            if value:
                value = self._substitute_params(value)

            result = await surface.resolve_and_act(
                action_type=tool_name,
                target_spec=target_spec,
                value=value,
                risk_level=tool_input.get("risk_level", "safe"),
                current_url=obs.current_url,
            )

            if tool_name == "read" and result.success:
                self._final_outputs[tool_input.get("output_name", "value")] = result.extracted_value
                return (f"Read: {result.extracted_value}", False)

            return (result.error or "Action completed", not result.success)

        elif tool_name == "done":
            self._final_outputs.update(tool_input.get("outputs", {}))
            return ("Goal accomplished", False)

        elif tool_name == "escalate":
            return (f"Escalating: {tool_input.get('reason')}", False)

        return (f"Unknown tool: {tool_name}", True)

    except Exception as e:
        return (f"{type(e).__name__}: {e}", True)
```

**`_ref_to_target_spec()` method:**
```python
def _ref_to_target_spec(self, ref: str, aria_snapshot: str) -> dict:
    """
    Parse role and name from ARIA snapshot for a given ref.
    Returns a target_spec dict with locator candidates.
    The actual ref resolution happens in PlaywrightWebSurface using aria-ref=eN locator.

    D7 algorithm: parse YAML line for metadata, but use normalize() for the actual locator.
    """
    import re
    role = None
    name = None

    for line in aria_snapshot.splitlines():
        if f"[ref={ref}]" not in line:
            continue
        m = re.match(r'\s*-\s+(\w+)(?:\s+"([^"]+)")?\s*(?:\[ref=)', line)
        if m:
            role = m.group(1)
            name = m.group(2)
        break

    candidates = []
    if role and name:
        candidates.append({"priority": 1, "strategy": "role", "role": role, "name": name, "exact": True})
        candidates.append({"priority": 2, "strategy": "label", "text": name})
        candidates.append({"priority": 3, "strategy": "text", "text": name, "exact": True})
    elif role:
        candidates.append({"priority": 1, "strategy": "role", "role": role, "exact": False})

    return {
        "frame_path": None,  # compiler will determine frame context later
        "candidates": candidates,
        "_discovery_ref": ref,  # stored for compiler use, removed from artifact
        "expected_matches": 1,
    }
```

**`_substitute_params()` method:**
```python
def _substitute_params(self, value: str) -> str:
    """Replace $inputs.param_name with actual input values."""
    for key, val in self._input_params.items():
        value = value.replace(f"$inputs.{key}", val)
    return value
```

**`_log_step()` method:**
```python
def _log_step(self, log_path: Path, step: DiscoveryStep, current_url: str):
    from capability.schema import LogEntry, ExecutionMode, ActionType
    from policy.redactor import redact_log_entry
    import json

    entry = {
        "ts": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "run_id": self._run_id,
        "execution_mode": "discovery",
        "control_owner": "automation",
        "step_id": None,
        "seq": step.seq,
        "action_type": step.tool_name,
        "route": current_url,
        "risk_class": step.risk_level,
        "outcome": "error" if step.is_error else "success",
        "model_reasoning": step.model_reasoning,
        "duration_ms": 0,
    }
    redacted = redact_log_entry(entry)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(redacted) + "\n")
```

**CLI entry point:**
```python
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run discovery agent")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--entry-point", required=True)
    parser.add_argument("--params", default="{}", help="JSON input params")
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--headless", action="store_true", default=False)
    args = parser.parse_args()

    result = asyncio.run(DiscoveryAgent(
        goal=args.goal,
        entry_point=args.entry_point,
        input_params=json.loads(args.params),
        max_steps=args.max_steps,
        max_duration_seconds=args.timeout,
        headless=args.headless,
    ).run())

    if result:
        print(f"Discovery succeeded. Artifact: {result.get('name')}")
    else:
        print("Discovery failed or timed out.")
```

---

### agent/compiler.py — D7 Updated Algorithm

```python
"""
agent/compiler.py
Converts discovery trajectory to a typed capability artifact.

D7 KEY CHANGE: Use Playwright's Locator.normalize() to convert ephemeral refs
to durable locators. Never store refs ("e17") in artifacts.

Compilation is an explicit separate phase — not inline during discovery.
After compilation, immediately replay once to validate. Mark as "draft".
"""
import json
import re
from pathlib import Path
from uuid import uuid4
from datetime import datetime


class ArtifactCompiler:

    async def compile(
        self,
        trajectory: list,          # list[DiscoveryStep]
        goal: str,
        entry_point: str,
        input_params: dict[str, str],
        final_outputs: dict[str, str],
        run_id: str,
        surface,                   # PlaywrightWebSurface — needed for normalize()
    ) -> dict:
        """
        Convert discovery trajectory → capability artifact JSON.

        D7 algorithm for each step with a ref:
        1. page.locator("aria-ref=eN") — resolve immediately (already done during discovery)
           NOTE: refs are no longer valid at compile time (after navigation).
           We use the YAML-parsed role/name as primary candidates instead,
           and store them as the durable locator bundle.
        2. The _discovery_ref in target_spec is removed from the final artifact.
        3. Locator candidates come from the parsed ARIA metadata (role+name first).
        """
        name = self._goal_to_name(goal)
        steps = self._build_steps(trajectory, input_params)
        outputs = self._build_outputs(final_outputs, trajectory)
        checkpoints = self._build_checkpoints(trajectory)
        business_outcomes = self._build_business_outcomes()

        artifact = {
            "schema_version": "1.0",
            "id": str(uuid4()),
            "name": name,
            "version": "1.0.0",
            "description": goal,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "target": {
                "surface_type": "playwright_web",
                "app_family": "mock_core_admin",
                "app_version": "1.0",
                "entry_point": entry_point,
            },
            "policy": {
                "risk_class": self._infer_risk_class(trajectory),
                "allowed_actions": self._infer_allowed_actions(trajectory),
                "allowed_routes": self._infer_allowed_routes(trajectory),
            },
            "inputs": [
                {
                    "name": k,
                    "type": "string",
                    "required": True,
                    "description": f"Input parameter: {k}",
                }
                for k in input_params.keys()
            ],
            "steps": steps,
            "outputs": outputs,
            "success_condition": {
                "checkpoint_id": list(checkpoints.keys())[-1] if checkpoints else "goal_reached",
                "description": goal,
            },
            "checkpoints": checkpoints,
            "business_outcomes": business_outcomes,
            "error_taxonomy": {
                "recoverable": [
                    {
                        "code": "SESSION_EXPIRED",
                        "recognizer": {"any": [
                            {"type": "route_matches", "pattern": "*/login*"},
                            {"type": "text_visible", "text": "session has expired"},
                        ]},
                        "recovery": "re_authenticate",
                        "max_retries": 1,
                        "backoff_ms": 2000,
                    },
                    {
                        "code": "TRANSIENT_LOAD",
                        "recognizer": {"type": "timeout", "after_ms": 5000},
                        "recovery": "wait_and_retry",
                        "max_retries": 3,
                        "backoff_ms": 2000,
                    },
                ],
                "hard_failures": [
                    {"code": "PERMISSION_DENIED", "recognizer": {"type": "http_status", "code": 403}},
                    {"code": "APP_ERROR", "recognizer": {"type": "text_visible", "text": "Application Error"}},
                    {"code": "AMBIGUOUS_LOCATOR", "recognizer": {"type": "locator_matches_multiple"}},
                    {"code": "LOCATOR_NOT_FOUND", "recognizer": {"type": "all_candidates_failed"}},
                ],
            },
            "provenance": {
                "discovery_run_id": run_id,
                "discovery_model": "claude-sonnet-4-6-bedrock",
                "compiler_version": "1.0.0",
                "status": "draft",
            },
        }

        return artifact

    def _goal_to_name(self, goal: str) -> str:
        """Convert goal string to snake_case artifact name."""
        import re
        words = re.sub(r'[^a-z0-9\s]', '', goal.lower()).split()[:5]
        return "_".join(words)

    def _build_steps(self, trajectory: list, input_params: dict) -> list:
        """Convert DiscoveryStep list to StepDefinition list."""
        steps = []
        for i, disc_step in enumerate(trajectory):
            if disc_step.tool_name in ("done", "escalate", "observe_screenshot"):
                continue

            # Remove _discovery_ref from target — never goes in artifact
            target_spec = None
            if "_discovery_ref" in (disc_step.tool_input or {}):
                target = dict(disc_step.tool_input)
                # Build candidates from parsed ARIA metadata only
                target_spec = disc_step._ref_to_target_spec_or_empty()
            elif disc_step.tool_name not in ("navigate", "press"):
                target_spec = {
                    "frame_path": None,
                    "candidates": [],
                    "expected_matches": 1,
                }

            value = disc_step.tool_input.get("value")

            step = {
                "id": f"step_{i+1}",
                "description": f"{disc_step.tool_name}: {disc_step.current_url_before}",
                "action": disc_step.tool_name,
                "target": target_spec,
                "value": value,
                "url": disc_step.tool_input.get("url"),
                "key": disc_step.tool_input.get("key"),
                "risk_level": disc_step.risk_level,
                "preconditions": [
                    {"type": "route_matches", "pattern": f"*{disc_step.current_url_before.split('localhost:5000')[-1]}*"}
                ],
                "postconditions": [],
                "output_name": disc_step.tool_input.get("output_name"),
            }
            steps.append(step)
        return steps

    def _build_outputs(self, final_outputs: dict, trajectory: list) -> list:
        """Build output definitions from read() calls during discovery."""
        outputs = []
        for name, value in final_outputs.items():
            # Find the read step that captured this output
            read_step = next(
                (s for s in trajectory if s.tool_name == "read"
                 and s.tool_input.get("output_name") == name),
                None
            )
            outputs.append({
                "name": name,
                "type": "string",
                "description": f"Extracted value: {name}",
                "extract": {
                    "frame_path": None,
                    "strategy": "role",
                    "role": "cell",
                    "name": name.replace("_", " ").title(),
                    "method": "text_content",
                },
            })
        return outputs

    def _build_checkpoints(self, trajectory: list) -> dict:
        """Infer checkpoints from URL transitions in trajectory."""
        checkpoints = {}
        seen_urls = set()
        for step in trajectory:
            url = step.current_url_before
            if url not in seen_urls and "localhost:5000" in url:
                seen_urls.add(url)
                path = url.split("localhost:5000")[-1].replace("/", "_").strip("_")
                key = f"at_{path}" if path else "at_root"
                checkpoints[key] = {
                    "all": [
                        {"type": "route_matches", "pattern": f"*{url.split('localhost:5000')[-1]}*"}
                    ]
                }
        return checkpoints

    def _build_business_outcomes(self) -> list:
        """Standard business outcomes for member lookup flows."""
        return [
            {
                "code": "MEMBER_NOT_FOUND",
                "description": "Search completed but no member with this ID exists",
                "recognizer": {
                    "all": [
                        {"type": "text_visible", "text": "No member found"},
                        {"type": "route_matches", "pattern": "*/members/search*"},
                    ]
                },
            }
        ]

    def _infer_risk_class(self, trajectory: list) -> str:
        levels = [s.risk_level for s in trajectory]
        if "irreversible_commit" in levels:
            return "irreversible"
        if "requires_confirmation" in levels:
            return "write"
        return "read_only"

    def _infer_allowed_actions(self, trajectory: list) -> list:
        return list(set(s.tool_name for s in trajectory
                        if s.tool_name not in ("done", "escalate")))

    def _infer_allowed_routes(self, trajectory: list) -> list:
        routes = set()
        for step in trajectory:
            url = step.current_url_before
            if "localhost:5000" in url:
                path = url.split("localhost:5000")[-1].split("?")[0]
                # Parameterize IDs in paths: /members/10001 → /members/**
                import re
                generic = re.sub(r'/\d+', '/**', path)
                routes.add(f"http://localhost:5000{generic}")
        return list(routes)
```

---

## Checkpoint Verification
```bash
# Run discovery (headed browser opens, Claude navigates)
python -m agent.loop \
  --goal "Look up member 10001 and find their savings balance" \
  --entry-point "http://localhost:5000/members/search" \
  --params '{"member_id": "10001"}' \
  --timeout 120

# Check outputs
ls capabilities/*.capability.json
cat capabilities/*.capability.json | python -m json.tool | head -60
ls evidence/runs/*/discovery.jsonl
```
Update PHASE_STATUS.md after completion.

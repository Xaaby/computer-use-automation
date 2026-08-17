"""
agent/loop.py
Discovery agent: LLM observe→decide→act loop.
Uses AWS Bedrock (boto3 converse API) — NOT Anthropic SDK.
Runs until: done() called, escalate() called, max_steps, or timeout.

WINDOWS ENTRYPOINT: sets ProactorEventLoop before any async work.
"""
from __future__ import annotations

import asyncio
import sys

# MUST be before any other async imports on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import boto3
from dotenv import load_dotenv

load_dotenv()


@dataclass
class DiscoveryStep:
    """One step recorded during discovery. Used by compiler to build artifact."""
    seq: int
    tool_name: str
    tool_input: dict
    aria_snapshot_before: str  # ARIA snapshot before this action (mode="ai")
    current_url_before: str  # URL before this action
    ref_used: str | None  # the ephemeral ref (e.g. "e17") if tool used one
    result_text: str
    is_error: bool
    risk_level: str = "safe"
    param_refs_found: list[str] = field(default_factory=list)  # ["$inputs.member_id"]
    model_reasoning: str | None = None  # any text Claude returned alongside tool_use
    # Populated during tool execution for the compiler (durable candidates, frame).
    resolved_target_spec: dict | None = None


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

    async def _bootstrap_session(self, surface) -> None:
        """
        Decision: log in before discovery so the compiled artifact focuses on
        the capability goal (search → detail → read), matching the fixture shape.
        Credentials come from env (APP_USERNAME / APP_PASSWORD); never written
        into capability inputs or evidence logs as secrets.
        """
        username = os.environ.get("APP_USERNAME", "admin")
        password = os.environ.get("APP_PASSWORD", "admin")
        login_url = "http://localhost:5000/login"
        nav = await surface.navigate(login_url)
        if not nav.success:
            print(f"Session bootstrap navigate failed: {nav.error}")
            return
        user_box = surface._page.get_by_role("textbox", name=re.compile("user", re.I))
        if await user_box.count() == 0:
            user_box = surface._page.locator('input[name="username"]')
        pass_box = surface._page.locator('input[type="password"]')
        if await user_box.count() == 1:
            await user_box.fill(username)
        if await pass_box.count() == 1:
            await pass_box.fill(password)
        submit = surface._page.get_by_role("button", name=re.compile("log ?in", re.I))
        if await submit.count() == 0:
            submit = surface._page.locator('button[type="submit"]')
        if await submit.count() >= 1:
            await submit.first.click()
            await surface._page.wait_for_load_state("domcontentloaded")

    async def run(self) -> dict | None:
        """
        Run discovery. Returns the compiled artifact dict on success, None on failure.
        """
        from agent.compiler import ArtifactCompiler
        from agent.prompts import SYSTEM_PROMPT, format_observation
        from agent.tools import DISCOVERY_TOOLS
        from surfaces.playwright_web import PlaywrightWebSurface

        evidence_dir = Path("evidence") / "runs" / self._run_id
        evidence_dir.mkdir(parents=True, exist_ok=True)
        log_path = evidence_dir / "discovery.jsonl"

        surface, browser, context, pw = await PlaywrightWebSurface.create(
            headless=self._headless,
            max_action_timeout_ms=10000,
        )

        await surface.start_tracing()
        await self._bootstrap_session(surface)
        await surface.navigate(self._entry_point)

        conversation_history: list[dict] = []
        action_history: list[dict] = []
        step = 0
        start_time = time.time()
        artifact_result: dict | None = None

        try:
            while step < self._max_steps:
                if time.time() - start_time > self._max_duration_seconds:
                    print(f"Discovery timeout after {self._max_duration_seconds}s")
                    break

                obs = await surface.observe(include_screenshot=False)

                user_text = format_observation(
                    aria_snapshot=obs.aria_snapshot,
                    current_url=obs.current_url,
                    step_number=step + 1,
                    goal=self._goal,
                    action_history=action_history,
                    available_inputs=self._input_params,
                )

                conversation_history.append(
                    {"role": "user", "content": [{"text": user_text}]}
                )

                response = self._bedrock.converse(
                    modelId=self._model_id,
                    system=[{"text": SYSTEM_PROMPT}],
                    messages=conversation_history,
                    toolConfig={"tools": DISCOVERY_TOOLS},
                    inferenceConfig={"maxTokens": 2048, "temperature": 0},
                )

                stop_reason = response["stopReason"]
                assistant_content = response["output"]["message"]["content"]

                model_reasoning = (
                    " ".join(
                        block["text"] for block in assistant_content if "text" in block
                    )
                    or None
                )

                conversation_history.append(
                    {"role": "assistant", "content": assistant_content}
                )

                if stop_reason == "end_turn":
                    print("Claude returned end_turn without done() — treating as stuck")
                    break

                elif stop_reason == "max_tokens":
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
                    terminal = False

                    for tool_use in tool_uses:
                        tool_name = tool_use["name"]
                        tool_input = tool_use["input"]
                        tool_use_id = tool_use["toolUseId"]

                        snap_before = obs.aria_snapshot
                        url_before = obs.current_url

                        result_text, is_error, resolved_spec = await self._execute_tool(
                            tool_name, tool_input, surface, obs
                        )

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
                                v
                                for v in tool_input.values()
                                if isinstance(v, str) and v.startswith("$inputs.")
                            ],
                            model_reasoning=model_reasoning,
                            resolved_target_spec=resolved_spec,
                        )
                        self._trajectory.append(disc_step)
                        self._log_step(log_path, disc_step, obs.current_url)

                        action_history.append(
                            {
                                "seq": step + 1,
                                "action": f"{tool_name}({json.dumps(tool_input)[:60]})",
                                "success": not is_error,
                                "note": result_text[:80],
                            }
                        )

                        tool_results.append(
                            {
                                "toolResult": {
                                    "toolUseId": tool_use_id,
                                    "content": [{"text": result_text}],
                                    **({"status": "error"} if is_error else {}),
                                }
                            }
                        )

                        if tool_name == "done":
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
                            artifact_path = (
                                self._output_dir / f"{artifact['name']}.capability.json"
                            )
                            artifact_path.write_text(
                                json.dumps(artifact, indent=2), encoding="utf-8"
                            )
                            print(f"Artifact saved: {artifact_path}")
                            artifact_result = artifact
                            terminal = True
                            break

                        if tool_name == "escalate":
                            print(f"Agent escalated: {tool_input.get('reason')}")
                            terminal = True
                            break

                    conversation_history.append(
                        {"role": "user", "content": tool_results}
                    )
                    step += 1
                    if terminal:
                        break

            return artifact_result

        finally:
            trace_path = evidence_dir / "discovery_trace.zip"
            try:
                await surface.stop_tracing(str(trace_path))
            except Exception:
                pass
            await context.close()
            await browser.close()
            await pw.__aexit__(None, None, None)

    async def _execute_tool(
        self,
        tool_name: str,
        tool_input: dict,
        surface,
        obs,
    ) -> tuple[str, bool, dict | None]:
        """Execute a tool call. Returns (result_text, is_error, resolved_target_spec)."""
        try:
            if tool_name == "navigate":
                result = await surface.navigate(tool_input["url"])
                if result.success:
                    return "Navigated successfully", False, None
                return result.error or "navigate failed", True, None

            elif tool_name == "press":
                await surface._page.keyboard.press(tool_input["key"])
                return "Key pressed", False, None

            elif tool_name == "observe_screenshot":
                obs_with_ss = await surface.observe(include_screenshot=True)
                return (
                    f"Screenshot captured. URL: {obs_with_ss.current_url}",
                    False,
                    None,
                )

            elif tool_name in ("click", "fill", "read"):
                ref = tool_input.get("ref")
                if not ref:
                    return "No ref provided", True, None

                target_spec = await self._ref_to_target_spec(ref, obs, surface)

                # Decision: aria-ref resolves on the Page (incl. iframe contents).
                # Do NOT scope via frame_locator during live discovery — that breaks
                # aria-ref. frame_path is kept on clean_spec for durable replay only.
                live_spec = dict(target_spec)
                if any(
                    "aria-ref=" in str(c.get("selector", ""))
                    for c in live_spec.get("candidates", [])
                ):
                    live_spec["frame_path"] = None

                value = tool_input.get("value")
                if value:
                    value = self._substitute_params(value)

                result = await surface.resolve_and_act(
                    action_type=tool_name,
                    target_spec=live_spec,
                    value=value,
                    risk_level=tool_input.get("risk_level", "safe"),
                    current_url=obs.current_url,
                )

                clean_spec = {
                    k: v for k, v in target_spec.items() if not k.startswith("_")
                }
                # Remove ephemeral aria-ref from what the compiler will store
                clean_spec["candidates"] = [
                    c
                    for c in clean_spec.get("candidates", [])
                    if "aria-ref=" not in str(c.get("selector", ""))
                ]
                for i, c in enumerate(clean_spec["candidates"], start=1):
                    c["priority"] = min(i, 5)

                if tool_name == "read" and result.success:
                    out_name = tool_input.get("output_name", "value")
                    self._final_outputs[out_name] = result.extracted_value
                    return f"Read: {result.extracted_value}", False, clean_spec

                return (
                    result.error or "Action completed",
                    not result.success,
                    clean_spec,
                )

            elif tool_name == "done":
                self._final_outputs.update(tool_input.get("outputs", {}))
                return "Goal accomplished", False, None

            elif tool_name == "escalate":
                return f"Escalating: {tool_input.get('reason')}", False, None

            return f"Unknown tool: {tool_name}", True, None

        except Exception as e:
            return f"{type(e).__name__}: {e}", True, None

    async def _ref_to_target_spec(self, ref: str, obs, surface) -> dict:
        """
        Build a target_spec for a discovery ref.

        Decision (D7): Prefer aria-ref for the live action (ephemeral, current
        snapshot only). Also parse role/name from YAML for durable artifact
        candidates. NEVER leave the raw ref in candidates written to the artifact.
        """
        role = None
        name = None
        for line in obs.aria_snapshot.splitlines():
            if f"[ref={ref}]" not in line:
                continue
            # Match: `- textbox "Member ID" [ref=e12]:` or `- cell [ref=e20]: "4821.50"`
            m = re.search(
                rf'-\s+(\w+)(?:\s+"([^"]*)")?.*\[ref={re.escape(ref)}\]',
                line,
            )
            if m:
                role = m.group(1)
                name = m.group(2)
            if name is None:
                m2 = re.search(r':\s*"([^"]+)"\s*$', line)
                if m2:
                    name = m2.group(1)
            break

        candidates: list[dict] = [
            {
                "priority": 1,
                "strategy": "css",
                "selector": f"aria-ref={ref}",
            }
        ]

        frame_path = None
        try:
            ephemeral = surface._page.locator(f"aria-ref={ref}")
            count = await ephemeral.count()
            if count == 1:
                try:
                    durable = await ephemeral.normalize()
                    _ = durable
                except Exception:
                    pass
                try:
                    handle = await ephemeral.element_handle()
                    if handle is not None:
                        frame = await handle.owner_frame()
                        if frame is not None and frame != surface._page.main_frame:
                            frame_path = 'iframe[title="Accounts"]'
                except Exception:
                    pass
        except Exception:
            pass

        if role and name:
            candidates.append(
                {
                    "priority": 2,
                    "strategy": "role",
                    "role": role,
                    "name": name,
                    "exact": True,
                }
            )
            if role in ("textbox", "searchbox", "combobox"):
                candidates.append(
                    {"priority": 3, "strategy": "label", "text": name, "exact": True}
                )
            candidates.append(
                {"priority": 4, "strategy": "text", "text": name, "exact": True}
            )
        elif role:
            candidates.append(
                {"priority": 2, "strategy": "role", "role": role, "exact": False}
            )

        return {
            "frame_path": frame_path,
            "candidates": candidates,
            "_discovery_ref": ref,
            "expected_matches": 1,
        }

    def _substitute_params(self, value: str) -> str:
        """Replace $inputs.param_name with actual input values."""
        for key, val in self._input_params.items():
            value = value.replace(f"$inputs.{key}", val)
        return value

    def _log_step(self, log_path: Path, step: DiscoveryStep, current_url: str):
        from policy.redactor import redact_log_entry

        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
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

    result = asyncio.run(
        DiscoveryAgent(
            goal=args.goal,
            entry_point=args.entry_point,
            input_params=json.loads(args.params),
            max_steps=args.max_steps,
            max_duration_seconds=args.timeout,
            headless=args.headless,
        ).run()
    )

    if result:
        print(f"Discovery succeeded. Artifact: {result.get('name')}")
    else:
        print("Discovery failed or timed out.")
        sys.exit(1)

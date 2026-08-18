"""
Bounded assisted fallback for replay executor.

Rules (absolute — do not relax):
1. ONE LLM call max per invocation. Never loop.
2. ONE fallback per run. Max 1. Second failure → hard_failure as normal.
3. NEVER attempt fallback on risk_level="irreversible_commit" steps.
4. NEVER attempt fallback on draft artifacts (status != "approved").
5. Policy check on LLM suggestion BEFORE executing on surface.
6. PII redact patch before writing to JSONL.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import boto3
from dotenv import load_dotenv

from capability.schema import ArtifactStatus, FallbackPatch, LocatorCandidate, LocatorStrategy, RiskLevel, StepDefinition
from policy.engine import PolicyEngine, PolicyViolationError
from policy.redactor import redact_log_entry

load_dotenv()

FALLBACK_SYSTEM_PROMPT = """You are a UI automation recovery assistant.
A single replay step has failed. Suggest exactly ONE corrective action.
Respond ONLY with valid JSON:
{"action": "click|fill|press", "locator_strategy": "role|label|text|css",
 "locator_value": "...", "value": ""}
No explanation. No multi-step plans. No navigation actions."""


async def attempt_fallback(
    step: StepDefinition,
    error_detail: str,
    live_aria: str,
    surface,
    policy: PolicyEngine,
    artifact_status: ArtifactStatus,
    run_id: str,
    evidence_dir: Path,
    fallback_used: bool,
) -> FallbackPatch | None:
    if fallback_used:
        return None
    if step.risk_level == RiskLevel.IRREVERSIBLE_COMMIT:
        return None
    if artifact_status != ArtifactStatus.APPROVED:
        return None

    prompt = (
        f"Failed step: {json.dumps({'action': step.action.value, 'locator': str(step.target)})}\n"
        f"Error: {error_detail}\n"
        f"Current page ARIA:\n{live_aria[:2000]}"
    )

    # Env lookups use .get so mocked tests do not require a live .env;
    # production still supplies these via dotenv / the process environment.
    bedrock = boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("AWS_REGION"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    response = bedrock.converse(
        modelId=os.environ.get("BEDROCK_MODEL_ID"),
        system=[{"text": FALLBACK_SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 200, "temperature": 0},
    )
    raw_text = response["output"]["message"]["content"][0]["text"]

    try:
        suggestion = json.loads(raw_text)
    except json.JSONDecodeError:
        patch = FallbackPatch(
            run_id=run_id,
            step_id=step.id,
            failed_locator=str(step.target),
            llm_suggested_action={"raw": raw_text},
            corrective_locator="parse_failed",
            succeeded=False,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )
        _write_patch(patch, evidence_dir)
        return patch

    action = suggestion.get("action", "")
    current_url = surface._page.url if hasattr(surface, "_page") else ""

    try:
        policy.check_action(action, current_url)
    except PolicyViolationError:
        patch = FallbackPatch(
            run_id=run_id,
            step_id=step.id,
            failed_locator=str(step.target),
            llm_suggested_action=suggestion,
            corrective_locator="policy_blocked",
            succeeded=False,
            captured_at=datetime.now(timezone.utc).isoformat(),
        )
        _write_patch(patch, evidence_dir)
        return patch

    strategy_str = suggestion.get("locator_strategy", "text")
    try:
        strategy = LocatorStrategy(strategy_str)
    except ValueError:
        strategy = LocatorStrategy.TEXT

    candidate = LocatorCandidate(
        priority=1,
        strategy=strategy,
        role=strategy_str if strategy_str == "role" else None,
        name=suggestion.get("locator_value") if strategy_str == "role" else None,
        text=suggestion.get("locator_value") if strategy_str in ("label", "text") else None,
        selector=suggestion.get("locator_value") if strategy_str == "css" else None,
    )
    target_spec = {
        "candidates": [candidate.model_dump(exclude_none=True)],
        "expected_matches": 1,
    }
    corrective_locator = f"{strategy_str}:{suggestion.get('locator_value', '')}"

    try:
        result = await surface.resolve_and_act(
            action_type=action,
            target_spec=target_spec,
            value=suggestion.get("value", ""),
            current_url=current_url,
        )
        succeeded = result.success
    except Exception:
        succeeded = False

    patch = FallbackPatch(
        run_id=run_id,
        step_id=step.id,
        failed_locator=str(step.target),
        llm_suggested_action=suggestion,
        corrective_locator=corrective_locator,
        succeeded=succeeded,
        captured_at=datetime.now(timezone.utc).isoformat(),
    )
    _write_patch(patch, evidence_dir)
    return patch


def _write_patch(patch: FallbackPatch, evidence_dir: Path) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    line = redact_log_entry(patch.model_dump())
    with (evidence_dir / "fallback_patches.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")

"""
replay/executor.py
Deterministic replay engine. NO LLM involved.
Given a capability artifact + input params → executes steps → returns typed outcome.

This is the production execution path. Every run is identical given the same inputs.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from dotenv import load_dotenv

from capability.schema import (
    ArtifactStatus,
    CapabilityArtifact,
    ControlOwner,
    FallbackPatch,
    RiskLevel,
)
from escalation.controller import EscalationController
from policy.redactor import redact_log_entry
from replay.conditions import (
    ConditionEvaluator,
    compute_fingerprint,
    diff_fingerprints,
    fingerprint_matches,
)
from replay.outcomes import (
    ArtifactNotApprovedError,
    BusinessOutcome,
    HardFailure,
    IndeterminateCommit,
    RecoverableExhausted,
    ReplayOutcome,
    ReplaySuccess,
)
from surfaces.playwright_web import PlaywrightWebSurface

load_dotenv()


class ReplayExecutor:
    def __init__(
        self,
        artifact: CapabilityArtifact,
        params: dict[str, str],
        headless: bool = False,
        evidence_base: Path | None = None,
        escalation_controller: EscalationController | None = None,
        bootstrap_session: bool = True,
        ws_manager=None,
        allow_draft: bool = True,
    ):
        self._artifact = artifact
        self._params = params
        self._headless = headless
        self._evidence_base = evidence_base or Path("evidence") / "runs"
        self._escalation = escalation_controller or EscalationController()
        self._run_id = str(uuid4())
        self._log_entries: list[dict] = []
        self._surface: PlaywrightWebSurface | None = None
        self._evaluator: ConditionEvaluator | None = None
        self._bootstrap_session = bootstrap_session
        self._outputs: dict[str, str] = {}
        self._ws_manager = ws_manager
        self._allow_draft = allow_draft
        self._patches: list[FallbackPatch] = []
        self._fallback_used = False

    def _locator_payload(self, step) -> dict:
        if step.target is None:
            return {}
        payload = step.target.model_dump(exclude_none=True)
        candidates = payload.get("candidates") or []
        if candidates:
            first = candidates[0]
            return {
                "strategy": first.get("strategy"),
                "role": first.get("role"),
                "name": first.get("name"),
                "text": first.get("text"),
            }
        return payload

    async def _broadcast_step(
        self, seq: int, status: str, step, error: str | None = None, reason: str | None = None
    ):
        if self._ws_manager is None:
            return
        from escalation.api import broadcast_step_event

        action = step.action.value if hasattr(step.action, "value") else str(step.action)
        await broadcast_step_event(
            self._ws_manager,
            seq,
            status,
            action,
            self._locator_payload(step),
            error=error,
            reason=reason,
        )

    async def _broadcast_run_complete(self, status: str):
        if self._ws_manager is None:
            return
        event = {
            "type": "run_complete",
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self._ws_manager.broadcast(json.dumps(event))

    async def run(self) -> ReplayOutcome:
        """Execute the capability deterministically."""
        if (
            self._artifact.provenance.status == ArtifactStatus.DRAFT
            and not self._allow_draft
        ):
            raise ArtifactNotApprovedError(
                f"Artifact '{self._artifact.name}' is DRAFT. "
                f"Run stability_runner with --approve first, or pass allow_draft=True."
            )

        start = time.time()
        evidence_dir = self._evidence_base / self._run_id
        evidence_dir.mkdir(parents=True, exist_ok=True)
        log_path = evidence_dir / "replay.jsonl"

        surface, browser, context, pw_cm = await PlaywrightWebSurface.create(
            headless=self._headless,
            max_action_timeout_ms=10000,
        )
        self._surface = surface
        self._evaluator = ConditionEvaluator(surface)

        screenshot_done = asyncio.Event()
        screenshot_task = None
        if self._ws_manager is not None:
            from escalation.api import screenshot_loop

            screenshot_task = asyncio.create_task(
                screenshot_loop(surface._page, self._ws_manager, screenshot_done)
            )

        await surface.start_tracing()
        if self._bootstrap_session:
            await self._re_authenticate()

        try:
            steps = self._artifact.steps
            for seq, step in enumerate(steps, start=1):
                await self._escalation.gate.wait()
                if self._escalation.abort_requested:
                    outcome = await self._hard_failure(
                        code="ABORTED",
                        step=step,
                        seq=seq,
                        expected="continue",
                        observed="operator aborted",
                        evidence_dir=evidence_dir,
                    )
                    await self._broadcast_step(seq, "failed", step, error="ABORTED")
                    await self._broadcast_run_complete("hard_failure")
                    return outcome

                await self._broadcast_step(seq, "start", step)

                value = self._substitute(step.value) if step.value else None

                for pre in step.preconditions:
                    cond = pre.model_dump(exclude_none=True)
                    if not await self._evaluator.evaluate(cond):
                        outcome = await self._hard_failure(
                            code="PRECONDITION_FAILED",
                            step=step,
                            seq=seq,
                            expected=str(cond),
                            observed=f"url={surface._page.url}",
                            evidence_dir=evidence_dir,
                        )
                        await self._broadcast_step(seq, "failed", step, error="PRECONDITION_FAILED")
                        await self._broadcast_run_complete("hard_failure")
                        return outcome

                bo = await self._check_business_outcomes(step.id, seq, evidence_dir)
                if bo is not None:
                    self._write_log(log_path, seq, step, "business_outcome", bo.code)
                    await self._broadcast_step(seq, "success", step)
                    await self._broadcast_run_complete("business_outcome")
                    return bo

                if step.risk_level == RiskLevel.IRREVERSIBLE_COMMIT:
                    if not self._escalation.has_approval():
                        self._escalation._owner = ControlOwner.HUMAN
                        self._escalation.gate.clear()
                        self._escalation._current_request = {
                            "reason": "irreversible_commit requires human approval",
                            "run_id": self._run_id,
                            "current_step_id": step.id,
                            "status": "pending",
                        }
                        await self._broadcast_step(
                            seq, "escalation", step, reason="irreversible_commit"
                        )
                        await self._escalation.gate.wait()
                        if self._escalation.abort_requested:
                            outcome = await self._hard_failure(
                                code="ABORTED",
                                step=step,
                                seq=seq,
                                expected="continue",
                                observed="operator aborted",
                                evidence_dir=evidence_dir,
                            )
                            await self._broadcast_run_complete("hard_failure")
                            return outcome
                        if not self._escalation.has_approval():
                            outcome = await self._hard_failure(
                                code="APPROVAL_REQUIRED",
                                step=step,
                                seq=seq,
                                expected="approval_token",
                                observed="missing",
                                evidence_dir=evidence_dir,
                            )
                            await self._broadcast_run_complete("hard_failure")
                            return outcome

                if step.fingerprint and self._surface:
                    obs = await self._surface.observe()
                    live_fp = compute_fingerprint(obs.aria_snapshot)
                    if not fingerprint_matches(step.fingerprint, live_fp):
                        diff = diff_fingerprints(step.fingerprint, live_fp)
                        drift_event = {
                            "type": "drift_detected",
                            "step_id": step.id,
                            "expected_hash": step.fingerprint.hash,
                            "actual_hash": live_fp.hash,
                            "diff": diff,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                        self._write_drift_log(log_path, drift_event)
                        if self._ws_manager:
                            await self._ws_manager.broadcast(
                                json.dumps(
                                    {
                                        "type": "step_event",
                                        "step": seq,
                                        "status": "failed",
                                        "action": "fingerprint_check",
                                        "locator": {},
                                        "timestamp": drift_event["timestamp"],
                                        "error": f"DRIFT: {diff}",
                                    }
                                )
                            )

                result = await self._execute_step_with_retries(
                    step, value, seq, evidence_dir, log_path
                )
                if isinstance(result, HardFailure):
                    eligible = (
                        not self._fallback_used
                        and self._artifact.provenance.status == ArtifactStatus.APPROVED
                        and step.risk_level != RiskLevel.IRREVERSIBLE_COMMIT
                    )
                    if eligible:
                        from replay.fallback import attempt_fallback

                        try:
                            obs = await self._surface.observe()
                            live_aria = obs.aria_snapshot[:2000]
                            patch = await attempt_fallback(
                                step=step,
                                error_detail=result.observed,
                                live_aria=live_aria,
                                surface=self._surface,
                                policy=self._surface._policy,
                                artifact_status=self._artifact.provenance.status,
                                run_id=self._run_id,
                                evidence_dir=evidence_dir,
                                fallback_used=self._fallback_used,
                            )
                        except Exception:
                            patch = None
                        if patch is not None:
                            self._patches.append(patch)
                            self._fallback_used = True
                            if patch.succeeded:
                                await self._broadcast_step(seq, "success", step)
                                continue
                    await self._broadcast_step(seq, "failed", step, error=result.code)
                    await self._broadcast_run_complete("hard_failure")
                    return self._attach_patches(result)

                if isinstance(result, IndeterminateCommit):
                    await self._broadcast_step(
                        seq, "escalation", step, reason="IndeterminateCommit"
                    )
                    await self._broadcast_run_complete("indeterminate_commit")
                    return result

                if isinstance(result, (RecoverableExhausted, BusinessOutcome)):
                    await self._broadcast_step(seq, "failed", step, error=type(result).__name__)
                    await self._broadcast_run_complete(type(result).__name__)
                    return result

                await self._broadcast_step(seq, "success", step)

                bo = await self._check_business_outcomes(step.id, seq, evidence_dir)
                if bo is not None:
                    self._write_log(log_path, seq, step, "business_outcome", bo.code)
                    await self._broadcast_run_complete("business_outcome")
                    return bo

                if step.action.value == "read" and step.output_name:
                    if hasattr(self, "_last_extracted") and self._last_extracted is not None:
                        self._outputs[step.output_name] = self._last_extracted

            success_cp = self._artifact.success_condition.get("checkpoint_id")
            if success_cp and success_cp in self._artifact.checkpoints:
                cp = self._artifact.checkpoints[success_cp]
                ok = await self._evaluator.evaluate_group(
                    cp.model_dump(exclude_none=True)
                )
                if not ok:
                    outcome = await self._hard_failure(
                        code="SUCCESS_CHECKPOINT_FAILED",
                        step=steps[-1] if steps else None,
                        seq=len(steps),
                        expected=f"checkpoint {success_cp}",
                        observed=f"url={surface._page.url}",
                        evidence_dir=evidence_dir,
                    )
                    await self._broadcast_run_complete("hard_failure")
                    return outcome

            await self._extract_declared_outputs()

            duration_ms = int((time.time() - start) * 1000)
            self._write_log(
                log_path,
                len(steps),
                steps[-1] if steps else None,
                "success",
                None,
            )
            success = ReplaySuccess(
                run_id=self._run_id,
                capability_id=self._artifact.name,
                outputs=dict(self._outputs),
                steps_completed=len(steps),
                duration_ms=duration_ms,
                evidence_path=str(evidence_dir),
                patches=list(self._patches),
            )
            await self._broadcast_run_complete("success")
            return success

        finally:
            screenshot_done.set()
            if screenshot_task is not None:
                screenshot_task.cancel()
                try:
                    await screenshot_task
                except asyncio.CancelledError:
                    pass
            try:
                await surface.stop_tracing(str(evidence_dir / "replay_trace.zip"))
            except Exception:
                pass
            await context.close()
            await browser.close()
            await pw_cm.__aexit__(None, None, None)

    def _attach_patches(self, outcome: ReplayOutcome) -> ReplayOutcome:
        if isinstance(outcome, HardFailure):
            outcome.patches = list(self._patches)
        return outcome

    def _write_drift_log(self, log_path: Path, event: dict) -> None:
        redacted = redact_log_entry(event)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(redacted) + "\n")

    async def _execute_step_with_retries(
        self, step, value, seq, evidence_dir: Path, log_path: Path
    ) -> ReplayOutcome | None:
        """Execute one step; return outcome object on terminal failure, else None."""
        assert self._surface is not None
        assert self._evaluator is not None

        taxonomy = self._artifact.error_taxonomy
        max_retries = 0
        backoff_ms = 2000
        recoverable_codes = {r.code: r for r in taxonomy.recoverable}
        for r in taxonomy.recoverable:
            max_retries = max(max_retries, r.max_retries)
            backoff_ms = r.backoff_ms

        attempt = 0
        while True:
            self._last_extracted = None
            target = None
            if step.target is not None:
                target = step.target.model_dump(exclude_none=True)

            action = step.action.value if hasattr(step.action, "value") else str(step.action)
            result = await self._surface.resolve_and_act(
                action_type=action,
                target_spec=target,
                value=value,
                key=step.key,
                url=self._substitute(step.url) if step.url else None,
                risk_level=(
                    step.risk_level.value
                    if hasattr(step.risk_level, "value")
                    else str(step.risk_level)
                ),
                current_url=self._surface._page.url,
            )

            # Irreversible timeout → IndeterminateCommit, NEVER retry
            if (
                step.risk_level == RiskLevel.IRREVERSIBLE_COMMIT
                and result.error_type == "timeout"
            ):
                evidence = await self._capture_evidence(step.id, "indeterminate")
                self._write_log(log_path, seq, step, "indeterminate_commit", None)
                return IndeterminateCommit(
                    run_id=self._run_id,
                    capability_id=self._artifact.name,
                    step_id=step.id,
                    step_seq=seq,
                    action_description=step.description,
                    evidence=evidence,
                )

            if result.error_type == "ambiguous_locator":
                evidence = await self._capture_evidence(step.id, "ambiguous")
                self._write_log(log_path, seq, step, "hard_failure", "AMBIGUOUS_LOCATOR")
                return HardFailure(
                    run_id=self._run_id,
                    capability_id=self._artifact.name,
                    code="AMBIGUOUS_LOCATOR",
                    step_id=step.id,
                    step_seq=seq,
                    expected="exactly 1 match",
                    observed=result.error or "ambiguous",
                    evidence=evidence,
                )

            if result.error_type == "all_candidates_failed":
                evidence = await self._capture_evidence(step.id, "not_found")
                self._write_log(log_path, seq, step, "hard_failure", "LOCATOR_NOT_FOUND")
                return HardFailure(
                    run_id=self._run_id,
                    capability_id=self._artifact.name,
                    code="LOCATOR_NOT_FOUND",
                    step_id=step.id,
                    step_seq=seq,
                    expected="locator match",
                    observed=result.error or "all candidates failed",
                    evidence=evidence,
                )

            # Hard failure recognizers (e.g. PERMISSION_DENIED via 403)
            for hf in taxonomy.hard_failures:
                group = hf.recognizer.model_dump(exclude_none=True)
                if await self._evaluator.evaluate_group(group):
                    evidence = await self._capture_evidence(step.id, hf.code.lower())
                    self._write_log(log_path, seq, step, "hard_failure", hf.code)
                    return HardFailure(
                        run_id=self._run_id,
                        capability_id=self._artifact.name,
                        code=hf.code,
                        step_id=step.id,
                        step_seq=seq,
                        expected="permitted action",
                        observed=hf.code,
                        evidence=evidence,
                    )

            if result.success:
                if action == "read":
                    self._last_extracted = result.extracted_value
                # Postconditions
                for post in step.postconditions:
                    cond = post.model_dump(exclude_none=True)
                    if not await self._evaluator.evaluate(cond):
                        # Treat failed postcondition as potential recoverable
                        recovered = await self._try_recover(
                            recoverable_codes, attempt, max_retries, backoff_ms
                        )
                        if recovered is True:
                            attempt += 1
                            continue
                        if isinstance(recovered, RecoverableExhausted):
                            recovered.step_id = step.id
                            return recovered
                        evidence = await self._capture_evidence(step.id, "postcond")
                        return HardFailure(
                            run_id=self._run_id,
                            capability_id=self._artifact.name,
                            code="POSTCONDITION_FAILED",
                            step_id=step.id,
                            step_seq=seq,
                            expected=str(cond),
                            observed=f"url={self._surface._page.url}",
                            evidence=evidence,
                        )
                self._write_log(log_path, seq, step, "success", None, retry=attempt)
                return None

            # Action failed — try recoverable
            recovered = await self._try_recover(
                recoverable_codes, attempt, max_retries, backoff_ms
            )
            if recovered is True:
                attempt += 1
                continue
            if isinstance(recovered, RecoverableExhausted):
                recovered.step_id = step.id
                self._write_log(
                    log_path, seq, step, "recoverable_exhausted", recovered.condition_code
                )
                return recovered

            evidence = await self._capture_evidence(step.id, "action_error")
            self._write_log(log_path, seq, step, "hard_failure", "ACTION_ERROR")
            return HardFailure(
                run_id=self._run_id,
                capability_id=self._artifact.name,
                code="ACTION_ERROR",
                step_id=step.id,
                step_seq=seq,
                expected="successful action",
                observed=result.error or result.error_type or "unknown",
                evidence=evidence,
            )

    async def _try_recover(
        self,
        recoverable_codes: dict,
        attempt: int,
        max_retries: int,
        backoff_ms: int,
    ) -> bool | RecoverableExhausted | None:
        """Return True to retry, RecoverableExhausted, or None if not recoverable."""
        assert self._evaluator is not None
        matched = None
        for code, rec in recoverable_codes.items():
            group = rec.recognizer.model_dump(exclude_none=True)
            if await self._evaluator.evaluate_group(group):
                matched = rec
                break
            # SESSION_EXPIRED also if on login route
            if code == "SESSION_EXPIRED" and "login" in (
                self._surface._page.url if self._surface else ""
            ):
                matched = rec
                break

        if matched is None:
            return None

        if attempt >= matched.max_retries:
            evidence = await self._capture_evidence("recover", matched.code.lower())
            return RecoverableExhausted(
                run_id=self._run_id,
                capability_id=self._artifact.name,
                condition_code=matched.code,
                step_id="",
                retries_attempted=attempt,
                evidence=evidence,
            )

        await asyncio.sleep(matched.backoff_ms / 1000.0)
        if matched.recovery == "re_authenticate":
            ok = await self._re_authenticate()
            if not ok:
                evidence = await self._capture_evidence("reauth", "fail")
                return RecoverableExhausted(
                    run_id=self._run_id,
                    capability_id=self._artifact.name,
                    condition_code=matched.code,
                    step_id="",
                    retries_attempted=attempt + 1,
                    evidence=evidence,
                )
        return True

    async def _check_business_outcomes(
        self, step_id: str, seq: int, evidence_dir: Path
    ) -> BusinessOutcome | None:
        assert self._evaluator is not None
        for bo in self._artifact.business_outcomes:
            group = bo.recognizer.model_dump(exclude_none=True)
            if await self._evaluator.evaluate_group(group):
                return BusinessOutcome(
                    run_id=self._run_id,
                    capability_id=self._artifact.name,
                    code=bo.code,
                    description=bo.description,
                    at_step_id=step_id,
                    at_step_seq=seq,
                    evidence_path=str(evidence_dir),
                )
        return None

    async def _extract_declared_outputs(self) -> None:
        """Fill outputs from artifact output definitions when not already set."""
        assert self._surface is not None
        for out in self._artifact.outputs:
            if out.name in self._outputs:
                continue
            extract = out.extract
            frame_path = extract.frame_path
            page_or_frame = (
                self._surface._page.frame_locator(frame_path)
                if frame_path
                else self._surface._page
            )
            loc = None
            if extract.strategy.value == "role" or extract.strategy == "role":
                kwargs = {}
                if extract.name:
                    kwargs["name"] = extract.name
                    kwargs["exact"] = True
                loc = page_or_frame.get_by_role(extract.role or "cell", **kwargs)
            elif extract.selector:
                loc = page_or_frame.locator(extract.selector)

            if loc is None:
                continue
            try:
                if await loc.count() == 1:
                    if extract.method == "inner_text":
                        self._outputs[out.name] = await loc.inner_text()
                    else:
                        self._outputs[out.name] = await loc.text_content() or ""
            except Exception:
                continue

    async def _re_authenticate(self) -> bool:
        """
        Handle session expiry by logging in again.
        Credentials come from environment variables ONLY — never from artifact.
        """
        assert self._surface is not None
        username = os.environ.get("APP_USERNAME", "admin")
        password = os.environ.get("APP_PASSWORD", "admin")
        nav = await self._surface.navigate("http://localhost:5000/login")
        if not nav.success:
            return False
        page = self._surface._page
        user_box = page.get_by_role("textbox", name="Username")
        pass_box = page.locator('input[type="password"]')
        await user_box.fill(username)
        await pass_box.fill(password)
        await page.get_by_role("button", name="Log in").click()
        await page.wait_for_load_state("domcontentloaded")
        return "login" not in page.url

    def _substitute(self, value: str | None) -> str | None:
        if value is None:
            return None
        for key, val in self._params.items():
            value = value.replace(f"$inputs.{key}", val)
        return value

    async def _capture_evidence(self, step_id: str, label: str) -> dict[str, str]:
        assert self._surface is not None
        refs = await self._surface.capture_evidence(
            run_id=self._run_id,
            step_id=step_id,
            label=label,
            evidence_dir=str(self._evidence_base),
        )
        out: dict[str, str] = {}
        if refs.screenshot_path:
            out["screenshot"] = refs.screenshot_path
        if refs.aria_snapshot_path:
            out["aria_snapshot"] = refs.aria_snapshot_path
        if refs.trace_path:
            out["trace"] = refs.trace_path
        # Restart tracing so subsequent captures can stop again
        try:
            await self._surface.start_tracing()
        except Exception:
            pass
        return out

    async def _hard_failure(
        self, code, step, seq, expected, observed, evidence_dir: Path
    ) -> HardFailure:
        step_id = step.id if step is not None else "unknown"
        evidence = await self._capture_evidence(step_id, code.lower())
        return HardFailure(
            run_id=self._run_id,
            capability_id=self._artifact.name,
            code=code,
            step_id=step_id,
            step_seq=seq,
            expected=expected,
            observed=observed,
            evidence=evidence,
            patches=list(self._patches),
        )

    def _write_log(
        self,
        log_path: Path,
        seq: int,
        step,
        outcome: str,
        outcome_code: str | None,
        retry: int = 0,
    ) -> None:
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "run_id": self._run_id,
            "capability_id": self._artifact.name,
            "execution_mode": "replay",
            "control_owner": "automation",
            "step_id": step.id if step else None,
            "seq": seq,
            "action_type": (
                step.action.value if step and hasattr(step.action, "value") else None
            ),
            "route": self._surface._page.url if self._surface else None,
            "outcome": outcome,
            "outcome_code": outcome_code,
            "retry_attempt": retry,
            "model_reasoning": None,
        }
        redacted = redact_log_entry(entry)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(redacted) + "\n")


def outcome_to_dict(result: ReplayOutcome, patches: list | None = None) -> dict:
    """Serialize a typed outcome for CLI / API responses."""
    patch_data = patches or getattr(result, "patches", None) or []
    base = {"run_id": result.run_id, "capability_id": result.capability_id}
    if isinstance(result, ReplaySuccess):
        return {
            **base,
            "status": "success",
            "outputs": result.outputs,
            "steps_completed": result.steps_completed,
            "duration_ms": result.duration_ms,
            "evidence_path": result.evidence_path,
            "patches": [p.model_dump() if hasattr(p, "model_dump") else p for p in (result.patches or patch_data)],
        }
    if isinstance(result, BusinessOutcome):
        return {
            **base,
            "status": "business_outcome",
            "business_outcome": {
                "code": result.code,
                "description": result.description,
                "at_step": result.at_step_id,
            },
            "evidence_path": result.evidence_path,
        }
    if isinstance(result, HardFailure):
        return {
            **base,
            "status": "hard_failure",
            "failure": {
                "code": result.code,
                "step_id": result.step_id,
                "expected": result.expected,
                "observed": result.observed,
                "evidence": result.evidence,
            },
            "patches": [p.model_dump() if hasattr(p, "model_dump") else p for p in patch_data],
        }
    if isinstance(result, RecoverableExhausted):
        return {
            **base,
            "status": "recoverable_exhausted",
            "condition_code": result.condition_code,
            "retries_attempted": result.retries_attempted,
            "evidence": result.evidence,
        }
    if isinstance(result, IndeterminateCommit):
        return {
            **base,
            "status": "indeterminate_commit",
            "step_id": result.step_id,
            "reconciliation_note": result.reconciliation_note,
            "evidence": result.evidence,
        }
    return {**base, "status": "unknown"}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True, help="Path to .capability.json")
    parser.add_argument("--params", default="{}", help="JSON string of input params")
    parser.add_argument("--headless", action="store_true", default=False)
    parser.add_argument(
        "--require-approved",
        action="store_true",
        help="Reject draft artifacts (sets allow_draft=False)",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="Start operator console in-process for live WebSocket streaming",
    )
    args = parser.parse_args()

    with open(args.artifact, encoding="utf-8") as f:
        artifact = CapabilityArtifact.model_validate(json.load(f))

    async def _main():
        from escalation.api import run_operator_console, ws_manager
        from escalation.controller import EscalationController

        controller = EscalationController(evidence_dir=Path("evidence") / "runs")
        console_task = None
        if args.console:
            import os

            port = int(os.environ.get("OPERATOR_CONSOLE_PORT", "8765"))
            console_task = asyncio.create_task(
                run_operator_console(controller, host="127.0.0.1", port=port)
            )
            await asyncio.sleep(0.5)

        result = await ReplayExecutor(
            artifact=artifact,
            params=json.loads(args.params),
            headless=args.headless,
            escalation_controller=controller if args.console else None,
            ws_manager=ws_manager if args.console else None,
            allow_draft=not args.require_approved,
        ).run()

        if console_task is not None:
            console_task.cancel()
            try:
                await console_task
            except asyncio.CancelledError:
                pass

        return result

    result = asyncio.run(_main())
    print(json.dumps(outcome_to_dict(result), indent=2, default=str))

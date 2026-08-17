"""
capability/api.py
Stretch A — agent-facing capability invocation surface.

Decision: HTTP routes live in escalation/api.py (same FastAPI app on :8765)
so operator console and capability invoke share one event loop / process.
This module re-exports helpers for programmatic use and docs clarity.
"""
from __future__ import annotations

import json
from pathlib import Path

from capability.schema import CapabilityArtifact
from replay.executor import ReplayExecutor, outcome_to_dict


def list_capability_summaries(cap_dir: Path | None = None) -> list[dict]:
    cap_dir = cap_dir or Path("capabilities")
    if not cap_dir.exists():
        return []
    caps = []
    for f in cap_dir.glob("*.capability.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            caps.append(
                {
                    "id": data.get("name"),
                    "version": data.get("version"),
                    "description": data.get("description"),
                    "status": data.get("provenance", {}).get("status"),
                    "risk_class": data.get("policy", {}).get("risk_class"),
                    "inputs": [i["name"] for i in data.get("inputs", [])],
                    "outputs": [o["name"] for o in data.get("outputs", [])],
                }
            )
        except Exception:
            continue
    return caps


async def invoke_capability(
    capability_name: str,
    inputs: dict[str, str],
    headless: bool = True,
) -> dict:
    cap_file = Path("capabilities") / f"{capability_name}.capability.json"
    if not cap_file.exists():
        raise FileNotFoundError(f"Capability '{capability_name}' not found")
    artifact = CapabilityArtifact.model_validate(
        json.loads(cap_file.read_text(encoding="utf-8"))
    )
    for inp in artifact.inputs:
        if inp.required and inp.name not in inputs:
            raise ValueError(f"Required input '{inp.name}' missing")
    result = await ReplayExecutor(
        artifact=artifact, params=inputs, headless=headless
    ).run()
    return outcome_to_dict(result)

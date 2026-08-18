"""
replay/conditions.py
Evaluates condition specs from capability artifacts against live page state.
Used for: preconditions, postconditions, checkpoints, business outcome recognizers.
Also provides ARIA fingerprint helpers for drift detection.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

import yaml

from capability.schema import ARIAFingerprint
from surfaces.base import Surface

# Playwright mode=ai lines: `button "Search" [ref=e12]` or `- button "Search"`
_PLAYWRIGHT_LINE = re.compile(
    r"^\s*-?\s*(?P<role>\w+)\s+\"(?P<name>[^\"]+)\"",
    re.MULTILINE,
)


def compute_fingerprint(aria_yaml: str) -> ARIAFingerprint:
    """
    Parse ARIA snapshot YAML, extract {role: [sorted names]} structure.
    Hash it with sha256. Elements with no accessible name are skipped.
    """
    structure: dict[str, list[str]] = {}
    _extract_roles_from_yaml(aria_yaml, structure)
    _extract_roles_from_playwright_lines(aria_yaml, structure)

    for role in structure:
        structure[role] = sorted(set(structure[role]))

    canonical = json.dumps(structure, sort_keys=True)
    fp_hash = hashlib.sha256(canonical.encode()).hexdigest()

    return ARIAFingerprint(
        hash=fp_hash,
        structure=structure,
        captured_at=datetime.now(timezone.utc).isoformat(),
    )


def _extract_roles_from_yaml(aria_yaml: str, acc: dict[str, list[str]]) -> None:
    try:
        data = yaml.safe_load(aria_yaml) or {}
    except Exception:
        return
    _extract_roles(data, acc)


def _extract_roles(node, acc: dict[str, list[str]]) -> None:
    if isinstance(node, dict):
        role = node.get("role") or node.get("type")
        name = node.get("name") or node.get("value") or node.get("text")
        if role and name and isinstance(name, str) and name.strip():
            acc.setdefault(str(role), []).append(name.strip())
        for v in node.values():
            _extract_roles(v, acc)
    elif isinstance(node, list):
        for item in node:
            _extract_roles(item, acc)


def _extract_roles_from_playwright_lines(aria_yaml: str, acc: dict[str, list[str]]) -> None:
    for match in _PLAYWRIGHT_LINE.finditer(aria_yaml):
        role = match.group("role")
        name = match.group("name").strip()
        if role and name:
            acc.setdefault(role, []).append(name)


def fingerprint_matches(stored: ARIAFingerprint, live: ARIAFingerprint) -> bool:
    return stored.hash == live.hash


def diff_fingerprints(stored: ARIAFingerprint, live: ARIAFingerprint) -> dict:
    added, removed = {}, {}
    all_roles = set(stored.structure) | set(live.structure)
    for role in all_roles:
        s_names = set(stored.structure.get(role, []))
        l_names = set(live.structure.get(role, []))
        if l_names - s_names:
            added[role] = list(l_names - s_names)
        if s_names - l_names:
            removed[role] = list(s_names - l_names)
    return {"added": added, "removed": removed}


class ConditionEvaluator:
    def __init__(self, surface: Surface):
        self._surface = surface

    async def evaluate(self, condition: dict) -> bool:
        """Evaluate a single condition dict. Returns True if condition is met."""
        ctype = condition["type"] if isinstance(condition, dict) else condition.get("type")

        if ctype in (
            "route_matches",
            "text_visible",
            "heading_visible",
            "element_present",
            "http_status",
            "input_value_equals",
        ):
            return await self._surface.check_condition(condition)
        elif ctype == "timeout":
            return False
        elif ctype == "locator_matches_multiple":
            return False
        elif ctype == "all_candidates_failed":
            return False
        return False

    async def evaluate_group(self, group: dict) -> bool:
        if group is None:
            return False
        all_conds = group.get("all") if isinstance(group, dict) else None
        any_conds = group.get("any") if isinstance(group, dict) else None
        if all_conds:
            for cond in all_conds:
                c = cond if isinstance(cond, dict) else cond.model_dump(exclude_none=True)
                if not await self.evaluate(c):
                    return False
            return True
        elif any_conds:
            for cond in any_conds:
                c = cond if isinstance(cond, dict) else cond.model_dump(exclude_none=True)
                if await self.evaluate(c):
                    return True
            return False
        return False

"""
agent/compiler.py
Converts discovery trajectory to a typed capability artifact.

D7 KEY CHANGE: Use Playwright's Locator.normalize() to convert ephemeral refs
to durable locators. Never store refs ("e17") in artifacts.

Compilation is an explicit separate phase — not inline during discovery.
After compilation, immediately replay once to validate. Mark as "draft".
"""
from __future__ import annotations

import re
from datetime import datetime
from uuid import uuid4

from capability.schema import CapabilityArtifact


class ArtifactCompiler:

    async def compile(
        self,
        trajectory: list,  # list[DiscoveryStep]
        goal: str,
        entry_point: str,
        input_params: dict[str, str],
        final_outputs: dict[str, str],
        run_id: str,
        surface,  # PlaywrightWebSurface — needed for normalize()
    ) -> dict:
        """
        Convert discovery trajectory → capability artifact JSON.

        Decision: Prefer resolved_target_spec captured during discovery (while
        refs were still valid). Strip any aria-ref / _discovery_ref before
        writing the artifact. Validate against CapabilityArtifact before return.
        """
        _ = surface  # reserved for future post-hoc normalize() passes
        self._entry_point_for_steps = entry_point
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
                "allowed_routes": self._infer_allowed_routes(trajectory, entry_point),
            },
            "inputs": [
                {
                    "name": k,
                    "type": "string",
                    "required": True,
                    "description": f"Input parameter: {k}",
                    **({"pattern": "^[0-9]+$"} if k == "member_id" else {}),
                }
                for k in input_params.keys()
            ],
            "steps": steps,
            "outputs": outputs,
            "success_condition": {
                "checkpoint_id": (
                    list(checkpoints.keys())[-1] if checkpoints else "goal_reached"
                ),
                "description": goal,
            },
            "checkpoints": checkpoints,
            "business_outcomes": business_outcomes,
            "error_taxonomy": {
                "recoverable": [
                    {
                        "code": "SESSION_EXPIRED",
                        "recognizer": {
                            "any": [
                                {"type": "route_matches", "pattern": "*/login*"},
                                {
                                    "type": "text_visible",
                                    "text": "session has expired",
                                },
                            ]
                        },
                        "recovery": "re_authenticate",
                        "max_retries": 1,
                        "backoff_ms": 2000,
                    },
                    {
                        "code": "TRANSIENT_LOAD",
                        "recognizer": {
                            "all": [{"type": "timeout", "after_ms": 5000}]
                        },
                        "recovery": "wait_and_retry",
                        "max_retries": 3,
                        "backoff_ms": 2000,
                    },
                ],
                "hard_failures": [
                    {
                        "code": "PERMISSION_DENIED",
                        "recognizer": {
                            "all": [{"type": "http_status", "code": 403}]
                        },
                    },
                    {
                        "code": "APP_ERROR",
                        "recognizer": {
                            "all": [
                                {
                                    "type": "text_visible",
                                    "text": "Application Error",
                                }
                            ]
                        },
                    },
                    {
                        "code": "AMBIGUOUS_LOCATOR",
                        "recognizer": {
                            "all": [{"type": "locator_matches_multiple"}]
                        },
                    },
                    {
                        "code": "LOCATOR_NOT_FOUND",
                        "recognizer": {
                            "all": [{"type": "all_candidates_failed"}]
                        },
                    },
                ],
            },
            "provenance": {
                "discovery_run_id": run_id,
                "discovery_model": "claude-sonnet-4-6-bedrock",
                "compiler_version": "1.0.0",
                "status": "draft",
            },
        }

        # Validate against schema (source of truth) before returning
        validated = CapabilityArtifact.model_validate(artifact)
        return validated.model_dump(mode="json", exclude_none=True)

    def _goal_to_name(self, goal: str) -> str:
        """
        Convert goal string to artifact name.
        Decision: map the known take-home goal to the fixture name
        member.lookup_savings_balance so Phase 4/6 paths stay stable.
        """
        g = goal.lower()
        if "savings" in g and ("balance" in g or "lookup" in g or "look up" in g):
            return "member.lookup_savings_balance"
        words = re.sub(r"[^a-z0-9\s]", "", g).split()[:5]
        return "_".join(words) if words else "unnamed_capability"

    def _strip_ephemeral_candidates(self, target_spec: dict | None) -> dict | None:
        """Remove aria-ref / discovery-only keys from a target_spec."""
        if not target_spec:
            return None
        cleaned = {
            k: v for k, v in target_spec.items() if not str(k).startswith("_")
        }
        candidates = []
        for c in cleaned.get("candidates", []):
            sel = c.get("selector") or ""
            if "aria-ref=" in str(sel):
                continue
            candidates.append(c)
        # Re-number priorities starting at 1
        for i, c in enumerate(candidates, start=1):
            c = dict(c)
            c["priority"] = min(i, 5)
            candidates[i - 1] = c
        cleaned["candidates"] = candidates
        cleaned.setdefault("expected_matches", 1)
        return cleaned

    def _build_steps(self, trajectory: list, input_params: dict) -> list:
        """Convert DiscoveryStep list to StepDefinition list."""
        _ = input_params
        steps = []
        step_num = 0

        # Decision: discovery bootstraps session then lands on entry_point, so
        # the trajectory often omits navigate. Replay still needs an explicit
        # first navigate step — prepend one when missing.
        has_navigate = any(
            s.tool_name == "navigate" and not s.is_error for s in trajectory
        )
        if not has_navigate:
            # entry_point is injected by compile(); stash via attribute set below
            entry = getattr(self, "_entry_point_for_steps", None)
            if entry:
                step_num += 1
                steps.append(
                    {
                        "id": f"step_{step_num}",
                        "description": "Navigate to capability entry point",
                        "action": "navigate",
                        "target": None,
                        "value": None,
                        "url": entry,
                        "key": None,
                        "risk_level": "safe",
                        "preconditions": [],
                        "postconditions": [
                            {
                                "type": "route_matches",
                                "pattern": f"*{entry.split('localhost:5000')[-1].split('?')[0]}*",
                            }
                        ],
                        "output_name": None,
                    }
                )

        for disc_step in trajectory:
            if disc_step.tool_name in ("done", "escalate", "observe_screenshot"):
                continue
            if disc_step.is_error:
                # Decision: skip failed attempts so the artifact encodes the
                # successful path only.
                continue

            step_num += 1
            target_spec = None
            if disc_step.tool_name not in ("navigate", "press"):
                raw = disc_step.resolved_target_spec
                if raw is None and disc_step.ref_used:
                    # Fallback: parse from stored ARIA snapshot
                    raw = self._parse_target_from_snapshot(
                        disc_step.ref_used, disc_step.aria_snapshot_before
                    )
                target_spec = self._strip_ephemeral_candidates(raw)
                if target_spec and not target_spec.get("candidates"):
                    # Keep a minimal role candidate if we still have metadata
                    fallback = self._parse_target_from_snapshot(
                        disc_step.ref_used or "",
                        disc_step.aria_snapshot_before,
                    )
                    target_spec = self._strip_ephemeral_candidates(fallback)

            path_suffix = ""
            url = disc_step.current_url_before or ""
            if "localhost:5000" in url:
                path_suffix = url.split("localhost:5000", 1)[-1].split("?")[0]

            step = {
                "id": f"step_{step_num}",
                "description": f"{disc_step.tool_name} at {path_suffix or url}",
                "action": disc_step.tool_name,
                "target": target_spec,
                "value": disc_step.tool_input.get("value"),
                "url": disc_step.tool_input.get("url"),
                "key": disc_step.tool_input.get("key"),
                "risk_level": disc_step.risk_level,
                "preconditions": (
                    [
                        {
                            "type": "route_matches",
                            "pattern": f"*{path_suffix}*",
                        }
                    ]
                    if path_suffix
                    else []
                ),
                "postconditions": [],
                "output_name": disc_step.tool_input.get("output_name"),
            }
            steps.append(step)
        return steps

    def _parse_target_from_snapshot(self, ref: str, aria_snapshot: str) -> dict:
        role = None
        name = None
        if ref:
            for line in aria_snapshot.splitlines():
                if f"[ref={ref}]" not in line:
                    continue
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

        candidates = []
        if role and name:
            candidates.append(
                {
                    "priority": 1,
                    "strategy": "role",
                    "role": role,
                    "name": name,
                    "exact": True,
                }
            )
            if role in ("textbox", "searchbox", "combobox"):
                candidates.append(
                    {
                        "priority": 2,
                        "strategy": "label",
                        "text": name,
                        "exact": True,
                    }
                )
        elif role:
            candidates.append(
                {"priority": 1, "strategy": "role", "role": role, "exact": False}
            )

        frame_path = None
        if "iframe" in aria_snapshot.lower() and role == "cell":
            frame_path = 'iframe[title="Accounts"]'

        return {
            "frame_path": frame_path,
            "candidates": candidates,
            "expected_matches": 1,
        }

    def _build_outputs(self, final_outputs: dict, trajectory: list) -> list:
        """Build output definitions from read() calls during discovery."""
        outputs = []
        for name, _value in final_outputs.items():
            read_step = next(
                (
                    s
                    for s in trajectory
                    if s.tool_name == "read"
                    and s.tool_input.get("output_name") == name
                    and not s.is_error
                ),
                None,
            )
            extract = {
                "frame_path": None,
                "strategy": "role",
                "role": "cell",
                "method": "text_content",
            }
            if read_step and read_step.resolved_target_spec:
                spec = read_step.resolved_target_spec
                extract["frame_path"] = spec.get("frame_path")
                for c in spec.get("candidates", []):
                    if c.get("strategy") == "role":
                        extract["strategy"] = "role"
                        extract["role"] = c.get("role") or "cell"
                        if c.get("name"):
                            extract["name"] = c["name"]
                        break
            elif name == "savings_balance":
                extract["frame_path"] = 'iframe[title="Accounts"]'
                extract["role"] = "cell"

            outputs.append(
                {
                    "name": name,
                    "type": "string",
                    "description": f"Extracted value: {name}",
                    "extract": extract,
                }
            )
        return outputs

    def _build_checkpoints(self, trajectory: list) -> dict:
        """Infer checkpoints from URL transitions in trajectory."""
        checkpoints = {}
        seen_urls = set()
        for step in trajectory:
            url = step.current_url_before
            if url not in seen_urls and "localhost:5000" in url:
                seen_urls.add(url)
                path = url.split("localhost:5000")[-1].split("?")[0]
                key = (
                    "at_" + path.replace("/", "_").strip("_")
                    if path
                    else "at_root"
                )
                # Collapse member detail /members/10001 → at_members_detail
                if re.match(r"^/members/\d+$", path or ""):
                    key = "at_members_detail"
                    pattern = "*/members/*"
                elif path == "/members/search":
                    key = "at_members_search"
                    pattern = "*/members/search*"
                else:
                    pattern = f"*{path}*"
                if key not in checkpoints:
                    checkpoints[key] = {
                        "all": [{"type": "route_matches", "pattern": pattern}]
                    }
        if not checkpoints:
            checkpoints["goal_reached"] = {
                "all": [{"type": "route_matches", "pattern": "*/members/*"}]
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
                        {
                            "type": "route_matches",
                            "pattern": "*/members/search*",
                        },
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
        return list(
            set(
                s.tool_name
                for s in trajectory
                if s.tool_name
                not in ("done", "escalate", "observe_screenshot")
                and not s.is_error
            )
        )

    def _infer_allowed_routes(self, trajectory: list, entry_point: str) -> list:
        routes = set()
        routes.add("http://localhost:5000/members/**")
        routes.add("http://localhost:5000/frames/**")
        for step in trajectory:
            url = step.current_url_before
            if "localhost:5000" in url:
                path = url.split("localhost:5000")[-1].split("?")[0]
                generic = re.sub(r"/\d+", "/**", path)
                routes.add(f"http://localhost:5000{generic}")
        if entry_point:
            routes.add(entry_point.split("?")[0])
        return list(routes)

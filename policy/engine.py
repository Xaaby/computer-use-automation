"""
policy/engine.py
Allowlist enforcement. Called before EVERY browser action.
This is the security boundary — nothing bypasses it.
"""
import fnmatch
import json
from pathlib import Path
from capability.schema import ActionType, RiskLevel


class PolicyViolationError(Exception):
    """Raised when an action violates the allowlist policy."""
    def __init__(self, message: str, action: str, route: str):
        super().__init__(message)
        self.action = action
        self.route = route


class PolicyEngine:
    def __init__(self, allowlist_path: Path | None = None):
        if allowlist_path is None:
            allowlist_path = Path(__file__).parent / "allowlist.json"
        with open(allowlist_path) as f:
            self._config = json.load(f)

    def check_action(self, action_type: str, url: str) -> RiskLevel:
        """
        Check if an action is permitted.
        Returns the risk level if permitted.
        Raises PolicyViolationError if not permitted.
        """
        # Check action type is allowed
        if action_type not in self._config["allowed_action_types"]:
            raise PolicyViolationError(
                f"Action type '{action_type}' is not in allowlist",
                action=action_type,
                route=url,
            )

        # Check URL is not blocked
        for blocked in self._config.get("blocked_routes", []):
            if fnmatch.fnmatch(url, blocked):
                raise PolicyViolationError(
                    f"Route '{url}' is explicitly blocked",
                    action=action_type,
                    route=url,
                )

        # Check URL is in allowed routes
        allowed = False
        for pattern in self._config["allowed_route_patterns"]:
            if fnmatch.fnmatch(url, pattern):
                allowed = True
                break

        # Also allow exact origin match for root
        for origin in self._config["allowed_origins"]:
            if url.startswith(origin):
                allowed = True
                break

        if not allowed:
            raise PolicyViolationError(
                f"Route '{url}' is not in allowed route patterns",
                action=action_type,
                route=url,
            )

        # Determine risk level
        for risky_pattern in self._config.get("risky_action_routes", []):
            if fnmatch.fnmatch(url, risky_pattern):
                return RiskLevel.REQUIRES_CONFIRMATION

        return RiskLevel.SAFE

    def get_pii_field_labels(self) -> list[str]:
        return self._config.get("pii_field_labels", [])

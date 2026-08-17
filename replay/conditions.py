"""
replay/conditions.py
Evaluates condition specs from capability artifacts against live page state.
Used for: preconditions, postconditions, checkpoints, business outcome recognizers.
"""
from surfaces.base import Surface


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
            # Always False when evaluated — means we've waited too long
            # Handled specially by executor with actual timeout
            return False
        elif ctype == "locator_matches_multiple":
            # Checked by surface during resolve_and_act
            return False
        elif ctype == "all_candidates_failed":
            return False
        return False

    async def evaluate_group(self, group: dict) -> bool:
        """
        Evaluate a condition group with 'all' or 'any' logic.
        group = {"all": [condition, ...]} or {"any": [condition, ...]}
        """
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

"""Typed JSON comparison for offline/native verification reports only."""
import math
from typing import Any


def json_values_equal(left: Any, right: Any) -> bool:
    """JSON booleans differ from numbers; numeric equality keeps arbitrary integers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        if any(isinstance(value, float) and not math.isfinite(value) for value in (left, right)):
            return False
        return isinstance(left, (int, float)) and isinstance(right, (int, float)) and left == right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (isinstance(left, list) and isinstance(right, list) and len(left) == len(right)
                and all(json_values_equal(a, b) for a, b in zip(left, right, strict=True)))
    if isinstance(left, dict) or isinstance(right, dict):
        return (isinstance(left, dict) and isinstance(right, dict) and left.keys() == right.keys()
                and all(json_values_equal(left[key], right[key]) for key in left))
    return False

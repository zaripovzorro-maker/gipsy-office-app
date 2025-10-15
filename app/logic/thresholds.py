from typing import Tuple


def inv_status(capacity: float, current: float) -> Tuple[str, float]:
    ratio = current / capacity if capacity > 0 else 0.0
    if ratio > 0.75:
        return "🔵", ratio
    if ratio > 0.50:
        return "🟡", ratio
    if ratio > 0.25:
        return "🟠", ratio
    return "🔴", ratio

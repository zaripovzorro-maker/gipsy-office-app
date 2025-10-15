def status_color(ratio: float) -> str:
    if ratio >= 0.75: return "🔵"
    if ratio >= 0.50: return "🟡"
    if ratio >= 0.25: return "🟠"
    return "🔴"


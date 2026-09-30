"""Game playtime inferred from Jarvis's existing focused-window timeline."""

import time

from .games import catalog


def summary(store, days=7, now=None):
    """Report recorded focus time; this is not total Steam or emulator playtime."""
    now = time.time() if now is None else now
    start = now - max(1, min(days, 30)) * 86400
    totals = {}
    for process, seconds in store.app_totals(start, now):
        game = catalog.detect(process)
        if game:
            totals[game.name] = totals.get(game.name, 0) + max(0, float(seconds or 0))
    if not totals:
        return f"No known games were recorded in the last {days} days. Watching must be on to log focus time."
    lines = [f"Recorded focused game time, last {days} days (not full playtime):"]
    for name, seconds in sorted(totals.items(), key=lambda item: -item[1])[:12]:
        lines.append(f"- {name}: {int(seconds // 3600)}h {int(seconds % 3600 // 60)}m")
    return "\n".join(lines)

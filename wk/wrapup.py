"""End-of-day wrap-up: what Shawn did today, what's unfinished, and a first task for tomorrow.

Two layers, so it's useful even with the model off:
  day_facts()      counted straight from Jarvis's own records (apps, renders, fixes, projects)
  wrapup_messages  the prompt that turns those facts into a short, specific summary

The scheduler side (WrapupClock) decides when to OFFER it: once per day, after the configured
time, and only while Shawn is actually at the PC - never as a surprise at 3 am.
"""
import json
import time

from . import config


def day_start(now=None):
    return time.mktime(time.strptime(time.strftime("%Y-%m-%d", time.localtime(now or time.time())), "%Y-%m-%d"))


def _fmt(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m" if seconds >= 3600 else f"{seconds // 60}m"


def day_facts(store, start, end):
    """Plain numbers and lists for [start, end) from Jarvis's database."""
    apps = [(p, s) for p, s in store.app_totals(start, end) if s >= 120]
    events = store.rows("SELECT kind, text FROM events WHERE ts>=? AND ts<? ORDER BY id", (start, end))
    by_kind = {}
    for kind, text in events:
        by_kind.setdefault(kind, []).append(text)
    projects = store.projects(include_done=False)                 # list of dicts (title, status, ...)
    reminders = [r for r in store.reminders() if not r[3]]        # (id, due, text, done): still pending
    journal = store.rows("SELECT text FROM journal WHERE period_end>=? AND period_start<? ORDER BY period_start",
                         (start, end))
    return {
        "active_seconds": sum(s for _, s in apps),
        "apps": apps[:10],
        "renders_ok": len([t for t in by_kind.get("render", []) if not t.startswith("Failed")]),
        "renders_failed": len([t for t in by_kind.get("render", []) if t.startswith("Failed")]),
        "errors_fixed": by_kind.get("error-help", [])[-5:],
        "files": by_kind.get("file", [])[-8:],
        "game_help": by_kind.get("game", [])[-5:],
        "actions": by_kind.get("action", [])[-8:],
        "open_projects": [(p["title"], p["status"]) for p in projects][:8],
        "reminders_pending": len(reminders),
        "journal": [row[0] for row in journal][-6:],
    }


def facts_markdown(facts, app_label=lambda p: p):
    """The instant part of the card: shown before (or without) the model's summary."""
    lines = [f"**Active today:** {_fmt(facts['active_seconds'])}"]
    if facts["apps"]:
        lines.append("**Where the time went:** " + ", ".join(f"{app_label(p)} {_fmt(s)}" for p, s in facts["apps"][:6]))
    if facts["renders_ok"] or facts["renders_failed"]:
        lines.append(f"**Renders:** {facts['renders_ok']} finished, {facts['renders_failed']} failed")
    if facts["errors_fixed"]:
        lines.append(f"**Errors Jarvis helped with:** {len(facts['errors_fixed'])}")
    if facts["open_projects"]:
        lines.append("**Open projects:** " + ", ".join(t for t, _ in facts["open_projects"]))
    if facts["reminders_pending"]:
        lines.append(f"**Reminders still pending:** {facts['reminders_pending']}")
    return "\n\n".join(lines)


def wrapup_messages(facts, persona, app_label=lambda p: p):
    apps = "\n".join(f"- {app_label(p)} ({p}): {_fmt(s)}" for p, s in facts["apps"]) or "- (nothing recorded)"
    extras = []
    for key, title in (("journal", "Journal entries written today"), ("errors_fixed", "Errors he copied"),
                       ("files", "New files"), ("actions", "Actions he ran"), ("game_help", "Game help")):
        if facts[key]:
            extras.append(f"{title}:\n" + "\n".join(f"- {t[:300]}" for t in facts[key]))
    projects = "\n".join(f"- {t} ({s})" for t, s in facts["open_projects"]) or "- (none)"
    user = (f"Today's record for Shawn:\nActive time: {_fmt(facts['active_seconds'])}\nApps:\n{apps}\n"
            f"Renders: {facts['renders_ok']} finished, {facts['renders_failed']} failed\n"
            f"Open projects:\n{projects}\n\n" + "\n\n".join(extras) + "\n\n"
            "Write his end-of-day wrap-up:\n"
            "**Today** - 3-5 bullets of what he actually worked on, grouped by project, specific (names, files, sites).\n"
            "**Unfinished** - 1-3 bullets of loose ends worth picking up.\n"
            "**First thing tomorrow** - one concrete task.\n"
            "Only use what's in the record; don't invent work.")
    return [{"role": "system", "content": persona}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# this is the "when to offer it" section; state lives in data/features.json
# ---------------------------------------------------------------------------
STATE_FILE = "features.json"


def _state():
    try:
        return json.loads((config.DATA_DIR / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state):
    path = config.DATA_DIR / STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


class WrapupClock:
    """due(now, idle) is True once per day, after wrapup_time (HH:MM, default 21:30), while Shawn is active."""

    def __init__(self, cfg):
        self.cfg = cfg

    def due(self, now, idle_seconds):
        if not self.cfg.get("wrapup_enabled", True):
            return False
        hh, mm = (int(x) for x in str(self.cfg.get("wrapup_time", "21:30")).split(":")[:2])
        local = time.localtime(now)
        if (local.tm_hour, local.tm_min) < (hh, mm) or idle_seconds > 120:
            return False
        return _state().get("wrapup_offered") != time.strftime("%Y-%m-%d", local)

    def mark_offered(self, now):
        state = _state()
        state["wrapup_offered"] = time.strftime("%Y-%m-%d", time.localtime(now))
        _save_state(state)

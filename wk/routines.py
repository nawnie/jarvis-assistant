"""User-authored, deterministic actions when a named program comes to the foreground.

Rules live in data/routines.json. No model can invent a rule from observed text;
the /routine command receives Shawn's direct chat or phone request.
"""

import json
import re
import time

from . import config


PROCESS = re.compile(r"^[a-zA-Z0-9_.-]{1,80}\.exe$")
ACTION = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
COOLDOWN = 300


def path():
    return config.DATA_DIR / "routines.json"


def load():
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(raw, list):
        raise ValueError("routines.json must contain a list")
    return [r for r in raw if isinstance(r, dict) and PROCESS.fullmatch(str(r.get("process", "")))
            and ACTION.fullmatch(str(r.get("action", "")))]


def save(rules):
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(rules, indent=2), encoding="utf-8")
    temp.replace(target)


def add(process, action, available):
    process, action = process.lower(), action.lower()
    if not PROCESS.fullmatch(process) or not ACTION.fullmatch(action):
        raise ValueError("Use a process name ending in .exe and an action ID from /actions")
    matches = [item for item in available if item.id == action]
    if not matches:
        raise ValueError("That action ID is not in /actions")
    if matches[0].confirm:
        raise ValueError("That action asks each time; choose an action without a confirmation step")
    rules = load()
    if {"process": process, "action": action} not in rules:
        rules.append({"process": process, "action": action})
        save(rules)
    return f"When {process} becomes focused, run action {action}."


def remove(process, action):
    rules = load()
    remaining = [r for r in rules if not (r["process"] == process.lower() and r["action"] == action.lower())]
    if len(remaining) == len(rules):
        return "No matching routine was found."
    save(remaining)
    return f"Removed the {process} → {action} routine."


class RoutineClock:
    """Trigger once per focus change, then wait before repeating the same rule."""

    def __init__(self):
        self.last_process = None
        self.last_run = {}

    def due(self, process, now=None):
        process = (process or "").lower()
        if process == self.last_process:
            return []
        self.last_process = process
        if not PROCESS.fullmatch(process):
            return []
        now = time.time() if now is None else now
        due = []
        for rule in load():
            key = (rule["process"], rule["action"])
            if rule["process"] == process and now - self.last_run.get(key, -COOLDOWN) >= COOLDOWN:
                due.append(rule["action"])
                self.last_run[key] = now
        return due

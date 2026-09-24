"""Small, read-only task hints from local Codex and Claude Code session files.

Only recent user requests are sampled. No assistant output, tool output, pasted
attachments, or transcript is copied into Jarvis's database.
"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _tail_lines(path, limit=512_000):
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            data = stream.read(limit)
    except OSError:
        return []
    if size > limit:
        data = data.partition(b"\n")[2]
    return data.decode("utf-8", errors="replace").splitlines()


def _compact(value):
    return " ".join(value.split())[:300] if isinstance(value, str) else ""


def _timestamp(value):
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 10**11 else 1)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def recent_tasks(home=None, now=None, hours=24):
    """Return at most six source-labeled user prompts; missing stores are normal."""
    home = Path(home) if home else Path.home()
    now = time.time() if now is None else now
    cutoff = now - hours * 3600
    found = []

    for line in _tail_lines(home / ".claude" / "history.jsonl"):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        stamp = _timestamp(item.get("timestamp"))
        prompt = _compact(item.get("display"))
        if stamp >= cutoff and prompt:
            found.append((stamp, "Claude Code", prompt))

    sessions = home / ".codex" / "sessions"
    local_day = datetime.fromtimestamp(now).date()
    utc_day = datetime.fromtimestamp(now, timezone.utc).date()
    days = {local_day, local_day - timedelta(days=1), utc_day, utc_day - timedelta(days=1)}
    files = []
    for day in days:
        folder = sessions / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")
        try:
            files.extend(path for path in folder.glob("rollout-*.jsonl") if path.stat().st_mtime >= cutoff)
        except OSError:
            continue
    for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:6]:
        for line in _tail_lines(path):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("payload", {})
            if event.get("type") != "response_item" or item.get("type") != "message" or item.get("role") != "user":
                continue
            stamp = _timestamp(event.get("timestamp"))
            texts = [part.get("text", "") for part in item.get("content", [])
                     if isinstance(part, dict) and part.get("type") == "input_text"]
            prompt = _compact(" ".join(texts))
            if stamp >= cutoff and prompt:
                found.append((stamp, "Codex", prompt))

    found.sort(reverse=True)
    unique = []
    seen = set()
    for stamp, source, prompt in found:
        key = (source, prompt)
        if key not in seen:
            seen.add(key)
            unique.append((stamp, source, prompt))
        if len(unique) == 6:
            break
    return unique

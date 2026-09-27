"""Fixed-kind Jarvis alerts for the paired phone companion.

Only kind, sequence, and time leave this process. No reminder text, filename,
crash title, credential, or arbitrary notification body enters this queue.
"""

import json
import os
import threading
import time

from . import config


KINDS = {"crash", "download", "reminder"}
_LOCK = threading.Lock()


def path():
    return config.DATA_DIR / "phone-alerts.json"


def record(kind):
    if kind not in KINDS:
        raise ValueError("unsupported phone alert kind")
    with _LOCK:
        target = path()
        try:
            state = json.loads(target.read_text(encoding="utf-8"))
            sequence = max(1, int(state["next_sequence"]))
            events = list(state["events"])[-49:]
        except (OSError, ValueError, KeyError, TypeError):
            sequence, events = 1, []
        events.append({"sequence": sequence, "kind": kind, "occurred_at": int(time.time())})
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps({"schema_version": 1, "next_sequence": sequence + 1,
                                    "events": events}, indent=2), encoding="utf-8")
        os.replace(temp, target)
        return sequence


def try_record(kind):
    """Phone delivery must never prevent a local reminder or crash notice."""
    try:
        return record(kind)
    except (OSError, ValueError, TypeError):
        return None

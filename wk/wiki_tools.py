"""Typed local AES project-wiki access; these calls never publish online."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

WIKI_CLI = Path(r"C:\AI-Agent-Workspace\bin\aes-wiki.cmd")
PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,70}$")
MAX_OUTPUT = 20_000


def _call(args: list[str]) -> dict:
    if not WIKI_CLI.is_file():
        raise FileNotFoundError(f"local AES wiki CLI is missing: {WIKI_CLI}")
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([str(WIKI_CLI), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=40,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode:
        raise RuntimeError(f"local AES wiki exited {proc.returncode}: {proc.stderr[:500] or proc.stdout[:500]}")
    if len(proc.stdout) > MAX_OUTPUT:
        return {"truncated": True, "preview": proc.stdout[:MAX_OUTPUT],
                "next": "Narrow the query or read the specific project page."}
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"text": proc.stdout[:MAX_OUTPUT]}


def _project(project_id: str) -> str:
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
        raise ValueError("project_id must be a stable lowercase wiki project ID")
    return project_id


def search(query: str) -> dict:
    if not isinstance(query, str) or not 1 <= len(query) <= 200:
        raise ValueError("query must have 1-200 characters")
    return _call(["search", query])


def context(project_id: str, task: str) -> dict:
    if not isinstance(task, str) or not 1 <= len(task) <= 300:
        raise ValueError("task must have 1-300 characters")
    return _call(["context", task, "--project", _project(project_id)])


def board(project_id: str) -> dict:
    return _call(["board", "--project", _project(project_id), "--agent", "jarvis-assistant"])


def post(project_id: str, kind: str, status: str, text: str, source: str, verification: str, next_step: str) -> dict:
    """Record a local-only update or handoff after actual project work."""
    _project(project_id)
    if kind not in ("update", "handoff"):
        raise ValueError("kind must be update or handoff; online publication is not exposed")
    if not isinstance(text, str) or not 1 <= len(text) <= 1500 or \
            not isinstance(verification, str) or not 1 <= len(verification) <= 800 or \
            not isinstance(next_step, str) or len(next_step) > 600:
        raise ValueError("bounded text, verification and next_step are required")
    if not isinstance(source, str) or not source or len(source) > 300:
        raise ValueError("source must point to an actual local evidence file")
    if not Path(source).is_file():
        raise FileNotFoundError(f"source evidence not found: {source}")
    if status not in ("working", "ready", "blocked", "done"):
        raise ValueError("status must be working, ready, blocked or done")
    result = _call(["post", "--kind", kind, "--project", project_id,
                    "--agent", "jarvis-assistant", "--status", status,
                    "--text", text, "--source", source,
                    "--verification", verification, "--next", next_step])
    return {"local_only_not_published": True, "receipt": result}

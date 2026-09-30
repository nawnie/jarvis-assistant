"""Owner-registered, fixed project test targets for unattended Jarvis repair checks.

Registration is local PC data, separate from the project under test. A model can
select an ID, but cannot pass a command, module, root, or environment variable.
Tests execute project code with the user's OS access; register only trusted roots.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import path_policy


REGISTRY = Path(r"C:\AI-Agent-Workspace\policy\jarvis-project-tests.v1.json")
ID_RE = re.compile(r"[a-z][a-z0-9-]{1,47}\Z")
MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,7}\Z")
MAX_OUTPUT = 12_000
BYTECODE_CACHE_ROOT = Path(r"F:\caches\Jarvis\project-test-bytecode")


def _registered(registry_path: Path) -> dict[str, dict]:
    if registry_path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(registry_path)):
        raise ValueError("registered project test catalog must be a real file")
    path = registry_path.resolve()
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 32_000:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"version", "projects"} or \
            payload["version"] != 1 or not isinstance(payload["projects"], list) or \
            len(payload["projects"]) > 20:
        raise ValueError("invalid registered project test catalog")
    projects: dict[str, dict] = {}
    for item in payload["projects"]:
        if not isinstance(item, dict) or set(item) != {"id", "root", "modules", "timeout_seconds", "enabled"}:
            raise ValueError("invalid registered project test entry")
        project_id = item["id"]
        if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id) or project_id in projects:
            raise ValueError("invalid or repeated registered project ID")
        root = item["root"]
        modules = item["modules"]
        timeout = item["timeout_seconds"]
        if not isinstance(root, str) or not Path(root).is_absolute() or not isinstance(modules, list) or \
                not 1 <= len(modules) <= 12 or any(not isinstance(m, dict) or set(m) != {"module", "sha256"}
                    or not isinstance(m["module"], str) or not MODULE_RE.fullmatch(m["module"])
                    or not isinstance(m["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", m["sha256"])
                    for m in modules) or \
                not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 180 or \
                not isinstance(item["enabled"], bool):
            raise ValueError("invalid registered test root, modules, timeout, or state")
        raw_folder = Path(root)
        if raw_folder.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(raw_folder)):
            raise ValueError("registered test root cannot be a link")
        folder = path_policy.check_path(root, mutation=True)
        if not folder.is_dir():
            raise ValueError("registered test root must be a real existing folder")
        for entry in modules:
            target = folder.joinpath(*entry["module"].split(".")).with_suffix(".py")
            actual_target = path_policy.check_path(str(target))
            if not target.is_file() or target.is_symlink() or not actual_target.is_relative_to(folder):
                raise ValueError("registered test module must be a file in its project root")
            if target.stat().st_size > 256_000 or hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]:
                raise PermissionError("registered test target changed since owner review")
        projects[project_id] = {"id": project_id, "root": folder, "modules": modules,
                                "timeout_seconds": timeout, "enabled": item["enabled"]}
    return projects


def list_projects(registry_path: Path | None = None) -> dict:
    projects = _registered(registry_path or REGISTRY)
    return {"source": "owner_registered_local_catalog", "projects": [
        {"id": item["id"], "runner": "python_unittest", "enabled": item["enabled"]}
        for item in projects.values()]}


def run_project(project_id: str, registry_path: Path | None = None) -> dict:
    if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
        raise ValueError("invalid registered project ID")
    item = _registered(registry_path or REGISTRY).get(project_id)
    if item is None or not item["enabled"]:
        raise PermissionError("project test runner is not registered and enabled")
    environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP")
                   if key in os.environ}
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    argv = [sys.executable, "-m", "unittest", *(entry["module"] for entry in item["modules"]), "-q"]
    started = time.monotonic()
    try:
        BYTECODE_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=BYTECODE_CACHE_ROOT) as bytecode_dir:
            # A unique bytecode location prevents same-second, same-size source
            # edits from reusing stale .pyc during immediate repair reruns.
            environment["PYTHONPYCACHEPREFIX"] = bytecode_dir
            result = subprocess.run(argv, cwd=item["root"], env=environment,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=item["timeout_seconds"],
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"project_id": project_id, "runner": "python_unittest", "state": "timed_out",
                "exit_code": None, "elapsed_ms": round((time.monotonic() - started) * 1000)}
    return {"project_id": project_id, "runner": "python_unittest", "state": "completed",
            "exit_code": result.returncode, "elapsed_ms": round((time.monotonic() - started) * 1000),
            "stdout": result.stdout[:MAX_OUTPUT], "stderr": result.stderr[:MAX_OUTPUT],
            "truncated": len(result.stdout) > MAX_OUTPUT or len(result.stderr) > MAX_OUTPUT}

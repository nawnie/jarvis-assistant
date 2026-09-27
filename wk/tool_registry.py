"""One callable tool catalog for model schemas, /tools, and execution receipts."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import mcp_client, path_policy, pc_tools, phone_tools, project_tests, wiki_tools

MAX_TEXT_BYTES = 128_000
MAX_OUTPUT_CHARS = 12_000
_last_results: dict[str, dict] = {}


def _path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path is required")
    return path_policy.check_path(value)


def file_info(path: str) -> dict:
    p = _path(path)
    s = p.stat()
    return {"path": str(p), "type": "directory" if p.is_dir() else "file",
            "bytes": s.st_size, "modified": s.st_mtime,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and s.st_size <= MAX_TEXT_BYTES else None}


def read_file(path: str, start_line: int = 1, max_lines: int = 160) -> dict:
    p = _path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    if p.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError(f"file exceeds {MAX_TEXT_BYTES} byte read limit; narrow the request")
    start = int(start_line)
    count = int(max_lines)
    if start < 1 or not 1 <= count <= 400:
        raise ValueError("start_line must be positive; max_lines must be 1-400")
    raw = p.read_bytes()
    lines = raw.decode("utf-8-sig").splitlines()
    return {"path": str(p), "start_line": start,
            "end_line": min(len(lines), start + count - 1), "total_lines": len(lines),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "text": "\n".join(f"{i}: {line}" for i, line in enumerate(lines[start-1:start-1+count], start))[:MAX_OUTPUT_CHARS]}


def search_text(path: str, query: str, max_results: int = 80) -> dict:
    root = _path(path)
    if not root.exists():
        raise FileNotFoundError(str(root))
    if not isinstance(query, str) or not query or len(query) > 300:
        raise ValueError("query must contain 1-300 characters")
    limit = int(max_results)
    if not 1 <= limit <= 200:
        raise ValueError("max_results must be 1-200")
    rg = shutil.which("rg") if root.is_file() else None
    if rg:
        args = [rg, "--color", "never", "--line-number", "--fixed-strings", "--max-columns", "300",
                "--glob", "!*.db", "--glob", "!*.gguf", "--glob", "!*.safetensors", query, str(root)]
        try:
            run = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if run.returncode in (0, 1):
                matches = run.stdout.splitlines()
                return {"root": str(root), "query": query, "matches": matches[:limit],
                        "truncated": len(matches) > limit, "backend": "rg"}
        except (OSError, subprocess.TimeoutExpired):
            pass
    # Some Windows rg shims are broken. Bound fallback by files, bytes and results.
    def safe_files():
        if root.is_file():
            yield root
            return
        for current, directories, filenames in os.walk(root, followlinks=False):
            safe_directories = []
            for name in directories:
                child = Path(current) / name
                if child.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(child)):
                    continue
                try:
                    path_policy.check_path(str(child))
                except PermissionError:
                    continue
                safe_directories.append(name)
            directories[:] = safe_directories
            for name in filenames:
                child = Path(current) / name
                if child.is_symlink():
                    continue
                try:
                    path_policy.check_path(str(child))
                except PermissionError:
                    continue
                yield child
    files = safe_files()
    matches = []
    visited = 0
    for p in files:
        if not p.is_file() or p.suffix.lower() in (".db", ".gguf", ".safetensors"):
            continue
        visited += 1
        if visited > 3000 or len(matches) >= limit:
            break
        try:
            if p.stat().st_size > MAX_TEXT_BYTES:
                continue
            for n, line in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
                if query in line:
                    matches.append(f"{p}:{n}:{line[:300]}")
                    if len(matches) >= limit:
                        break
        except (OSError, UnicodeError):
            continue
    return {"root": str(root), "query": query, "matches": matches,
            "truncated": visited > 3000 or len(matches) >= limit, "backend": "bounded-python"}


def patch_text(path: str, old: str, new: str, expected_sha256: str = "") -> dict:
    raw_path = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"'))))
    if raw_path.is_symlink():
        raise ValueError("patch target is a symbolic link; inspect its target explicitly")
    path_policy.check_path(path, mutation=True)
    p = _path(path)
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("a fresh expected_sha256 from file_info/read_file is required")
    if not p.is_file():
        raise ValueError("patch target must be an existing regular file")
    original = p.read_bytes()
    if len(original) > MAX_TEXT_BYTES:
        raise ValueError("file exceeds patch limit")
    before_hash = hashlib.sha256(original).hexdigest()
    if expected_sha256.lower() != before_hash:
        raise ValueError("file changed since inspection; patch not applied")
    text = original.decode("utf-8-sig")
    if not old or text.count(old) != 1:
        raise ValueError("old text must match exactly once; patch not applied")
    updated = text.replace(old, new, 1).encode("utf-8")
    fd, temp = tempfile.mkstemp(prefix=".jarvis-patch-", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(updated)
            out.flush()
            os.fsync(out.fileno())
        # Recheck just before replace; no silent overwrite of a concurrent edit.
        if hashlib.sha256(p.read_bytes()).hexdigest() != before_hash:
            raise ValueError("file changed during patch; patch not applied")
        os.replace(temp, p)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return {"path": str(p), "old_sha256": before_hash,
            "new_sha256": hashlib.sha256(updated).hexdigest(), "bytes": len(updated)}


def run_command(program: str, args: list[str], cwd: str, timeout_seconds: int = 30) -> dict:
    """Privileged host helper; never exposed directly to a model turn."""
    folder = _path(cwd)
    if not folder.is_dir():
        raise ValueError("cwd must be an existing directory")
    if not isinstance(program, str) or not program or not isinstance(args, list) or \
            len(args) > 64 or len(program) > 400 or \
            any(not isinstance(a, str) or len(a) > 300 for a in args) or \
            sum(len(a) for a in args) > 3000:
        raise ValueError("program and a string argument array are required")
    path_policy.check_command(program, args, cwd)
    timeout = int(timeout_seconds)
    if not 1 <= timeout <= 180:
        raise ValueError("timeout_seconds must be 1-180")
    exe = shutil.which(program) if not Path(program).is_absolute() else program
    if not exe:
        raise FileNotFoundError(f"executable not found: {program}")
    try:
        result = subprocess.run([exe, *args], cwd=folder, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        return {"program": program, "cwd": str(folder), "state": "timed_out",
                "timeout_seconds": timeout,
                "stdout": (exc.stdout or b"").decode("utf-8", "replace")[:MAX_OUTPUT_CHARS]
                if isinstance(exc.stdout, bytes) else (exc.stdout or "")[:MAX_OUTPUT_CHARS]}
    return {"program": program, "cwd": str(folder), "state": "completed",
            "exit_code": result.returncode, "stdout": result.stdout[:MAX_OUTPUT_CHARS],
            "stderr": result.stderr[:MAX_OUTPUT_CHARS],
            "truncated": len(result.stdout) > MAX_OUTPUT_CHARS or len(result.stderr) > MAX_OUTPUT_CHARS}


def run_jarvis_tests() -> dict:
    """Execute only the reviewed, fixed offline Jarvis boundary suite."""
    root = Path(__file__).resolve().parent.parent
    modules = ("tests.test_agent_loop", "tests.test_phone_tools",
               "tests.test_operator_identity", "tests.test_operator_project_api",
               "tests.test_project_tests")
    result = subprocess.run(
        [sys.executable, "-m", "unittest", *modules, "-q"], cwd=root,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {"suite": "jarvis_offline_boundary", "state": "completed", "exit_code": result.returncode,
            "stdout": result.stdout[:MAX_OUTPUT_CHARS], "stderr": result.stderr[:MAX_OUTPUT_CHARS],
            "truncated": len(result.stdout) > MAX_OUTPUT_CHARS or len(result.stderr) > MAX_OUTPUT_CHARS}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    call: Callable
    mutation: bool = False

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name,
                "description": self.description, "parameters": self.parameters}}


def _params(properties: dict, required: tuple[str, ...]) -> dict:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


STR = {"type": "string"}
INT = {"type": "integer"}
OBJ = {"type": "object"}
TOOLS = {
    "file_info": Tool("file_info", "Get actual file or folder metadata.", _params({"path": STR}, ("path",)), file_info),
    "read_file": Tool("read_file", "Read a bounded range of a UTF-8 text file with source line numbers.",
                      _params({"path": STR, "start_line": INT, "max_lines": INT}, ("path",)), read_file),
    "search_text": Tool("search_text", "Search for literal text below a file or directory.",
                        _params({"path": STR, "query": STR, "max_results": INT}, ("path", "query")), search_text),
    "patch_text": Tool("patch_text", "Atomically replace one exact span in an existing UTF-8 file.",
                       _params({"path": STR, "old": STR, "new": STR, "expected_sha256": STR},
                               ("path", "old", "new", "expected_sha256")), patch_text, True),
    "run_command": Tool("run_command", "Privileged host command helper; not offered to model turns.",
                        _params({"program": STR, "args": {"type": "array", "items": STR},
                                 "cwd": STR, "timeout_seconds": INT}, ("program", "args", "cwd")), run_command, True),
    "run_jarvis_tests": Tool("run_jarvis_tests", "Run the fixed offline Jarvis boundary suite with no caller path or command.",
                             _params({}, ()), run_jarvis_tests),
    "registered_test_projects": Tool("registered_test_projects", "List owner-registered project test IDs without paths or commands.",
                                     _params({}, ()), project_tests.list_projects),
    "run_registered_test": Tool("run_registered_test", "Run the fixed unittest suite for one owner-registered project ID.",
                                _params({"project_id": STR}, ("project_id",)), project_tests.run_project, True),
}

# This fixed catalog is shared by Bonsai 8B and 27B. Policy is checked on the
# host for every call; selecting another model never grants another device/root.
_phone = phone_tools.PhoneTools()
TOOLS.update({
    "phone_list_folder": Tool("phone_list_folder", "List names in one selected folder on the one authorized Android device.",
                              _params({"path": STR}, ("path",)), _phone.list_folder),
    "phone_search_names": Tool("phone_search_names", "Search file names within one selected Android folder, at most three levels deep.",
                               _params({"folder": STR, "query": STR}, ("folder", "query")), _phone.search_names),
    "phone_read_text": Tool("phone_read_text", "Read a bounded UTF-8 text file inside a selected Android folder.",
                            _params({"path": STR, "max_bytes": INT}, ("path",)), _phone.read_text),
    "phone_pull_file": Tool("phone_pull_file", "Copy one selected Android file of at most 2 MB into Jarvis's fixed private phone-pulls folder.",
                            _params({"path": STR}, ("path",)), _phone.pull_file, True),
    "phone_compose_message": Tool("phone_compose_message", "Preview the exact recipient and body for owner confirmation; this does not send a message.",
                                  _params({"recipient": STR, "body": STR}, ("recipient", "body")), _phone.compose_message),
    "phone_open_draft": Tool("phone_open_draft", "Ask Shawn in a native PC confirmation dialog, then open the exact Android message draft. Android still requires a Send tap.",
                             _params({"draft_id": STR, "device": STR, "recipient": STR, "body": STR},
                                     ("draft_id", "device", "recipient", "body")), _phone.open_confirmed_draft, True),
    "phone_prepare_setting": Tool("phone_prepare_setting", "Preview a named supported Android setting/value pair; no setting changes yet.",
                                  _params({"name": STR, "value": STR}, ("name", "value")), _phone.prepare_setting),
    "phone_apply_setting": Tool("phone_apply_setting", "Ask Shawn in a native PC confirmation dialog, then apply the exact one-use Android setting change.",
                                _params({"change_id": STR, "device": STR, "name": STR, "value": STR},
                                        ("change_id", "device", "name", "value")), _phone.apply_confirmed_setting, True),
})

# Existing Windows actions remain callable and are described by the same catalog.
for _name in ("list_folder", "find_files", "move", "copy", "rename", "make_folder", "delete",
              "extract", "open", "disk_space", "cast_voice"):
    _fn, _fields, _description = pc_tools.TOOLS[_name]
    def _adapt(fn=_fn, fields=_fields, tool_name=_name):
        def call(**kwargs):
            for field in fields:
                if field in ("path", "source", "destination") and kwargs[field] and \
                        not (tool_name == "open" and re.match(r"^[a-z]+://", kwargs[field], re.I)):
                    path_policy.check_path(kwargs[field], mutation=tool_name not in
                                           ("list_folder", "find_files", "disk_space"))
            return fn(*[kwargs[p] for p in fields])
        return call
    TOOLS[_name] = Tool(_name, _description, _params({p: STR for p in _fields}, _fields),
                        _adapt(), _name not in ("list_folder", "find_files", "disk_space"))


def selected(question: str, cfg: dict | None = None) -> dict[str, Tool]:
    """Small initial schema set; all turns can discover further tools."""
    names = ["file_info", "read_file", "search_text", "patch_text", "list_folder", "find_files"]
    lower = (question or "").lower()
    if any(word in lower for word in ("move", "copy", "rename", "delete", "recycle", "unzip", "extract")):
        names += ["move", "copy", "rename", "delete", "extract"]
    if any(word in lower for word in ("powershell", "launch", "open", "disk", "folder")):
        names += ["open", "disk_space", "make_folder"]
    if any(word in lower for word in ("test", "pytest", "unittest", "compile", "lint", "verify")):
        names += ["run_jarvis_tests", "registered_test_projects", "run_registered_test"]
    if any(word in lower for word in ("phone", "android", "adb", "text message", "sms")):
        names += ["phone_list_folder", "phone_search_names", "phone_read_text", "phone_pull_file",
                  "phone_compose_message", "phone_open_draft", "phone_prepare_setting", "phone_apply_setting"]
    offered = {name: TOOLS[name] for name in dict.fromkeys(names)}
    if any(word in lower for word in ("project", "repo", "wiki", "code", "source", "handoff")):
        offered.update({
            "wiki_search": Tool("wiki_search", "Search the local AES project wiki for a stable project ID.",
                                _params({"query": STR}, ("query",)), wiki_tools.search),
            "wiki_context": Tool("wiki_context", "Read published reference plus local-only updates and messages for a project.",
                                 _params({"project_id": STR, "task": STR}, ("project_id", "task")), wiki_tools.context),
            "wiki_board": Tool("wiki_board", "Read current local-only agent handoffs and messages for a project.",
                               _params({"project_id": STR}, ("project_id",)), wiki_tools.board),
            "wiki_post": Tool("wiki_post", "Post a local-only project update or handoff with existing evidence; never publishes online.",
                              _params({"project_id": STR, "kind": STR, "status": STR, "text": STR,
                                       "source": STR, "verification": STR, "next_step": STR},
                                      ("project_id", "kind", "status", "text", "source", "verification", "next_step")),
                              wiki_tools.post, True),
        })
    cfg = cfg or {}
    try:
        if not path_policy.owner_roots()["write_roots"]:
            for unavailable in ("patch_text", "move", "copy", "rename", "make_folder", "delete", "extract"):
                offered.pop(unavailable, None)
    except (OSError, ValueError, PermissionError):
        for unavailable in ("patch_text", "move", "copy", "rename", "make_folder", "delete", "extract"):
            offered.pop(unavailable, None)
    if mcp_client.configured(cfg):
        offered["mcp_discover"] = Tool("mcp_discover", "Initialize one named configured MCP server and list its actual tools.",
                                        _params({"server": STR}, ("server",)),
                                        lambda server: mcp_client.discover(cfg, server))
        offered["mcp_call"] = Tool("mcp_call", "Call a tool actually listed by a named configured MCP server.",
                                    _params({"server": STR, "tool": STR, "arguments": OBJ},
                                            ("server", "tool", "arguments")),
                                    lambda server, tool, arguments: mcp_client.call(cfg, server, tool, arguments), True)
    return offered


def describe(cfg: dict) -> str:
    if not cfg.get("pc_actions_enabled", True):
        return "PC action tools: disabled in Jarvis settings."
    lines = ["Local tools (configured and callable by host; individual calls still need execution proof):"]
    try:
        roots = path_policy.owner_roots()
        write_roots = roots["write_roots"]
        lines.append(f"Owner file roots: {len(roots['read_roots'])} read, {len(roots['write_roots'])} write; source={roots['source']}.")
    except (OSError, ValueError, PermissionError):
        write_roots = []
        lines.append("Owner file-root policy: unavailable; file tools fail closed.")
    lines += [f"- {name}: {tool.description} Last tested: "
              f"{_last_results.get(name, {}).get('state', 'not tested')}"
              for name, tool in TOOLS.items() if name != "run_command" and
              (name not in {"patch_text", "move", "copy", "rename", "make_folder", "delete", "extract"}
               or write_roots)]
    lines.append("Generic model-supplied run_command is unavailable; project tests require an owner-registered ID.")
    try:
        ids = [item["id"] for item in project_tests.list_projects()["projects"] if item["enabled"]]
        lines.append("Registered project test IDs: " + (", ".join(ids) if ids else "none"))
    except (OSError, ValueError, PermissionError):
        lines.append("Registered project test catalog: unavailable")
    lines.append("Local AES wiki tools: search, context, board, local-only update/handoff; online publish is unavailable.")
    servers = mcp_client.configured(cfg)
    if servers:
        for name, spec in servers.items():
            state = mcp_client.last_health(name)
            lines.append(f"MCP {name}: configured={'yes' if spec.get('enabled') else 'disabled'}, "
                         f"health={state.get('state', 'not_tested')}; discover before calling.")
    else:
        lines.append("MCP servers: none explicitly configured in Jarvis; Codex MCP settings are not inherited.")
    return "\n".join(lines)


def execute(name: str, arguments: dict, allowed: dict[str, Tool], timeout_seconds: int | None = None) -> dict:
    if name not in allowed:
        return {"ok": False, "error": f"tool {name!r} was not offered for this turn"}
    tool = allowed[name]
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "arguments must be a JSON object"}
    props = tool.parameters["properties"]
    missing = [p for p in tool.parameters["required"] if p not in arguments]
    if missing or set(arguments) - set(props):
        return {"ok": False, "error": f"invalid arguments; missing={missing}; extra={sorted(set(arguments)-set(props))}"}
    for key, value in arguments.items():
        kind = props[key]["type"]
        if kind == "string" and not isinstance(value, str) or \
                kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)) or \
                kind == "array" and (not isinstance(value, list) or any(not isinstance(v, str) for v in value)) or \
                kind == "object" and not isinstance(value, dict):
            return {"ok": False, "error": f"invalid type for {key}"}
    started = time.monotonic()
    try:
        if timeout_seconds is not None and timeout_seconds < 1:
            raise TimeoutError("agent time budget expired before tool execution")
        call_args = dict(arguments)
        if name == "run_command" and timeout_seconds is not None:
            call_args["timeout_seconds"] = min(int(call_args.get("timeout_seconds", 30)), timeout_seconds)
        result = tool.call(**call_args)
        mcp_error = bool(name == "mcp_call" and isinstance(result, dict) and
                         isinstance(result.get("result"), dict) and result["result"].get("is_error"))
        command_error = bool(name in {"run_command", "run_jarvis_tests", "run_registered_test"} and isinstance(result, dict) and
                             (result.get("state") != "completed" or result.get("exit_code") != 0))
        legacy_error = bool(isinstance(result, str) and
                            (re.match(r"^exit code (?!0\b)\d+", result.lower()) or
                             result.lower().startswith(("failed:", "stopped:", "not a folder:",
                                                       "not extracted:", "not a zip file:",
                                                       "nothing found", "moved nothing", "copied nothing",
                                                       "deleted nothing", "could not delete"))))
        failed = mcp_error or command_error or legacy_error
        _last_results[name] = {"state": "failed" if failed else "healthy", "tested_at": time.time()}
        return {"ok": not failed, "tool": name, "result": result,
                "elapsed_ms": round((time.monotonic() - started) * 1000)}
    except Exception as exc:
        _last_results[name] = {"state": "failed", "tested_at": time.time()}
        return {"ok": False, "tool": name,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": round((time.monotonic() - started) * 1000)}

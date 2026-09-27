"""Host-side exclusions for generic assistant file and command tools."""
from __future__ import annotations

import os
import re
import json
from pathlib import Path

PRIVATE_SEGMENTS = {"personal", "medical", "hr", "benefits", "accommodation"}
MODEL_SEGMENTS = {"nsfw", "reg"}
OWNER_CONTROL_FILES = {"jarvis-project-tests.v1.json", "jarvis-file-roots.v1.json",
                       "jarvis-protected-segments.v1.json", "registered_project_tests.v1.json"}
OWNER_POLICY_DIR = Path(r"C:\AI-Agent-Workspace\policy")
ROOTS_FILE = OWNER_POLICY_DIR / "jarvis-file-roots.v1.json"
PROTECTED_SEGMENTS_FILE = OWNER_POLICY_DIR / "jarvis-protected-segments.v1.json"
DEFAULT_ROOT = Path(r"F:\UserFolders\Desktop\Active Projects")
SENSITIVE_PARTS = {".codex", ".ssh", ".aws", ".git", "appdata", "credentials", "secrets"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".jks", ".keystore", ".env", ".db", ".sqlite", ".sqlite3"}
SENSITIVE_NAME_WORDS = ("credential", "password", "secret", "private_key", "token", "api_key")
RUNTIME_PRIVATE_DIRS = (
    Path(r"F:\_Projects\Watchkeeper\data"),
    Path(r"E:\Codex-Projects\Desktop\Active Projects\Tools\PhonePcControl\data"),
    Path(r"C:\Users\Shawn\.codex"),
    OWNER_POLICY_DIR,
)
PATH_TOKEN = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\s\"']+")


def _protected_segments() -> set[str]:
    """Load the owner's private deny labels before allowing configured writes."""
    if not PROTECTED_SEGMENTS_FILE.exists():
        raise PermissionError("owner protected-path policy is required for writes")
    if PROTECTED_SEGMENTS_FILE.is_symlink() or PROTECTED_SEGMENTS_FILE.stat().st_size > 16_000:
        raise PermissionError("invalid owner protected-path policy")
    try:
        payload = json.loads(PROTECTED_SEGMENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PermissionError("invalid owner protected-path policy") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "segments"} or payload["version"] != 1:
        raise PermissionError("invalid owner protected-path policy")
    values = payload["segments"]
    if not isinstance(values, list) or len(values) > 64 or any(
            not isinstance(value, str) or not value.strip() or value in (".", "..") or
            any(char in value for char in ("/", "\\", ":")) for value in values):
        raise PermissionError("invalid owner protected-path policy")
    return {value.strip().casefold() for value in values}


def _load_roots() -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    if not ROOTS_FILE.exists():
        root = DEFAULT_ROOT.resolve()
        return ((root,), ()) if root.is_dir() else ((), ())
    if ROOTS_FILE.is_symlink() or ROOTS_FILE.stat().st_size > 16_000:
        raise PermissionError("invalid owner file-root policy")
    payload = json.loads(ROOTS_FILE.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"version", "read_roots", "write_roots"} or payload["version"] != 1:
        raise PermissionError("invalid owner file-root policy")
    parsed = []
    for key in ("read_roots", "write_roots"):
        roots = payload[key]
        if not isinstance(roots, list) or len(roots) > 16:
            raise PermissionError("invalid owner file-root policy")
        accepted = []
        for value in roots:
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise PermissionError("owner roots must be absolute folders")
            raw = Path(value)
            if raw.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(raw)):
                raise PermissionError("owner roots cannot be links")
            root = raw.resolve()
            if not root.is_dir():
                raise PermissionError("owner root is unavailable")
            accepted.append(root)
        parsed.append(tuple(accepted))
    if any(not any(write == read or write.is_relative_to(read) for read in parsed[0]) for write in parsed[1]):
        raise PermissionError("write roots must be inside read roots")
    return parsed[0], parsed[1]


def owner_roots() -> dict[str, list[str]]:
    read, write = _load_roots()
    return {"read_roots": [str(root) for root in read],
            "write_roots": [str(root) for root in write],
            "source": "owner_policy_file" if ROOTS_FILE.exists() else "workspace_default_read_only"}


def check_path(value: str, *, mutation: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path is required")
    path = Path(os.path.expandvars(os.path.expanduser(value.strip().strip('"')))).resolve()
    parts = {part.casefold() for part in path.parts}
    if parts & PRIVATE_SEGMENTS:
        raise PermissionError("path skipped for privacy")
    if parts & SENSITIVE_PARTS or path.suffix.casefold() in SENSITIVE_SUFFIXES or \
            path.name.casefold() == ".env" or path.name.casefold().startswith(".env.") or \
            ("data" in parts and path.name.casefold() in {"config.json", "auth.json", "settings.json"}) or \
            any(word in path.name.casefold() for word in SENSITIVE_NAME_WORDS):
        raise PermissionError("credential or runtime path skipped for privacy")
    if any(path == private.resolve() or path.is_relative_to(private.resolve()) for private in RUNTIME_PRIVATE_DIRS):
        raise PermissionError("credential or runtime path skipped for privacy")
    if "datasets" in parts and parts & MODEL_SEGMENTS:
        raise PermissionError("path skipped by dataset exclusion")
    if mutation and path.name.casefold() in OWNER_CONTROL_FILES:
        raise PermissionError("owner-managed test registration cannot be changed by a model tool")
    if mutation and path in (OWNER_POLICY_DIR, OWNER_POLICY_DIR.parent):
        raise PermissionError("owner control directory cannot be changed by a model tool")
    read_roots, write_roots = _load_roots()
    roots = write_roots if mutation else read_roots
    if roots and parts & _protected_segments():
        raise PermissionError("owner-protected project requires separate task scope")
    if not any(path == root or path.is_relative_to(root) for root in roots):
        raise PermissionError("path is outside owner-selected file roots")
    return path


def check_command(program: str, args: list[str], cwd: str) -> Path:
    folder = check_path(cwd, mutation=True)
    executable = Path(program).name.casefold()
    if executable in ("python.exe", "python3.exe", "python", "py.exe", "py"):
        if len(args) < 2 or args[0] != "-m" or args[1] not in ("pytest", "unittest", "compileall"):
            raise PermissionError("only typed Python test/compile modules are available to the model")
    elif executable in ("git.exe", "git"):
        if not args or args[0] not in ("status", "diff", "show", "log", "rev-parse"):
            raise PermissionError("only read-only Git checks are available to the model")
    elif executable in ("rg.exe", "rg"):
        if any(arg.startswith(("--pre", "-z")) for arg in args):
            raise PermissionError("rg preprocessors are unavailable to the model")
    else:
        raise PermissionError("arbitrary executables require a separate authorized host route")
    if Path(program).is_absolute():
        check_path(program, mutation=True)
    for arg in args:
        for token in PATH_TOKEN.findall(arg):
            check_path(token, mutation=True)
        candidate = folder / arg
        if arg.startswith((".", "~", "\\", "/")) or candidate.exists():
            check_path(str(candidate), mutation=True)
    return folder

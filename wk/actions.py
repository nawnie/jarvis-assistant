"""Quick actions: named things Jarvis can DO, run from the ask bar, a button, or chat (/do ...).

Every action is declared in data/actions.json (seeded with defaults on first run; Shawn edits
it freely). Only these kinds exist - there is deliberately no "shell" or "powershell" kind,
so an action can never be an arbitrary command line:

  open     open a folder or file with its default app          target = path
  url      open a web page or app link (http, https, steam)    target = URL
  launch   start a program with fixed arguments (no shell)     target = exe, args = [...]
  builtin  a Jarvis feature (free ComfyUI VRAM, wrap-up, snip)  target = builtin name

Targets may use {tokens}: {downloads} {desktop} {documents} {jarvis_data} {comfy_output} {comfy_url}.
"confirm": true makes Jarvis ask before running it.

Matching is plain text similarity (no model), so actions work even while the model is off.
"""
import difflib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config

KINDS = ("open", "url", "launch", "builtin")
URL_SCHEMES = ("http://", "https://", "steam://")
# ask-bar phrases that mean "do something", not "tell me something"
VERBS = ("open", "launch", "start", "run", "show", "free", "do")

DEFAULT_ACTIONS = [
    {"id": "downloads", "label": "Open Downloads", "kind": "open", "target": "{downloads}", "aliases": ["downloads"]},
    {"id": "desktop", "label": "Open Desktop folder", "kind": "open", "target": "{desktop}", "aliases": ["desktop"]},
    {"id": "comfy-output", "label": "Open ComfyUI outputs", "kind": "open", "target": "{comfy_output}",
     "aliases": ["renders", "comfy output", "comfyui output", "outputs"]},
    {"id": "comfy-ui", "label": "Open ComfyUI", "kind": "url", "target": "{comfy_url}", "aliases": ["comfy", "comfyui"]},
    {"id": "llama-chat", "label": "Open llama chat", "kind": "url", "target": "http://127.0.0.1:8080",
     "aliases": ["llama", "llama chat", "chat ui"]},
    {"id": "comfy-free", "label": "Free ComfyUI VRAM", "kind": "builtin", "target": "comfy_free", "confirm": True,
     "aliases": ["free vram", "unload comfy", "free comfy", "free gpu"]},
    {"id": "wrapup", "label": "Day wrap-up", "kind": "builtin", "target": "wrapup", "aliases": ["wrap up", "wrapup", "recap my day"]},
    {"id": "snip", "label": "Snip & ask", "kind": "builtin", "target": "snip", "aliases": ["snip", "screenshot"]},
    {"id": "jarvis-data", "label": "Open Jarvis data folder", "kind": "open", "target": "{jarvis_data}",
     "aliases": ["jarvis folder", "jarvis data"]},
    {"id": "task-manager", "label": "Task Manager", "kind": "launch", "target": "taskmgr.exe", "aliases": ["taskmgr", "task manager"]},
    {"id": "skyrim", "label": "Launch Skyrim Special Edition", "kind": "url", "target": "steam://rungameid/489830",
     "confirm": True, "aliases": ["skyrim"]},
    {"id": "starfield", "label": "Launch Starfield", "kind": "url", "target": "steam://rungameid/1716740",
     "confirm": True, "aliases": ["starfield"]},
]


@dataclass
class Action:
    id: str
    label: str
    kind: str
    target: str
    args: list = field(default_factory=list)
    aliases: list = field(default_factory=list)
    confirm: bool = False


def actions_path():
    return config.DATA_DIR / "actions.json"


def load(path=None):
    """(actions, problems): every valid action from actions.json, and a note for each one skipped.
    Seeds the file with the defaults the first time."""
    path = Path(path or actions_path())
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_ACTIONS, indent=2), encoding="utf-8")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [Action(**a) for a in DEFAULT_ACTIONS], [f"actions.json unreadable ({exc}); using defaults"]
    actions, problems, seen = [], [], set()
    # this loop validates each entry; a bad one is skipped with a reason, never guessed at
    for entry in raw if isinstance(raw, list) else []:
        try:
            action = Action(id=str(entry["id"]), label=str(entry["label"]), kind=str(entry["kind"]),
                            target=str(entry["target"]), args=[str(a) for a in entry.get("args", [])],
                            aliases=[str(a) for a in entry.get("aliases", [])], confirm=bool(entry.get("confirm", False)))
        except (KeyError, TypeError) as exc:
            problems.append(f"skipped an entry missing {exc}")
            continue
        if action.kind not in KINDS:
            problems.append(f"'{action.label}': kind '{action.kind}' isn't allowed (open, url, launch, builtin)")
            continue
        if action.id in seen:
            problems.append(f"'{action.label}': duplicate id '{action.id}'")
            continue
        seen.add(action.id)
        actions.append(action)
    return actions, problems


# ---------------------------------------------------------------------------
# this is the matching section: "open my downloads" -> the Downloads action
# ---------------------------------------------------------------------------
def _norm(text):
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()


def wants_action(text):
    """Does this ask-bar text read like a command? ('!...' always does; else it must start with a verb)."""
    text = (text or "").strip().lower()
    return text.startswith("!") or (bool(_norm(text)) and _norm(text)[0] in VERBS)


def match(text, actions, threshold=0.72):
    """Best (action, score) for the text, or (None, best score). Compares against the label and
    every alias, with and without the leading verb, so 'open downloads' and '!downloads' both work."""
    words = _norm(text.lstrip("!"))
    if words and words[0] in VERBS:
        stripped = words[1:]
    else:
        stripped = words
    candidates = {" ".join(words), " ".join(stripped), " ".join(w for w in stripped if w not in ("my", "the"))}
    best, best_score = None, 0.0
    for action in actions:
        names = [" ".join(_norm(action.label))] + [" ".join(_norm(a)) for a in action.aliases]
        for name in names:
            for cand in candidates:
                if not cand:
                    continue
                score = 1.0 if cand == name else difflib.SequenceMatcher(None, cand, name).ratio()
                if score > best_score:
                    best, best_score = action, score
    return (best, best_score) if best_score >= threshold else (None, best_score)


# ---------------------------------------------------------------------------
# this is the running section
# ---------------------------------------------------------------------------
def tokens(comfy_output=None, comfy_url=None):
    from .games.catalog import documents_dir
    return {"downloads": config.DOWNLOADS, "desktop": config.DESKTOP, "documents": documents_dir(),
            "jarvis_data": str(config.DATA_DIR),
            "comfy_output": comfy_output or str(Path(documents_dir()) / "ComfyUI" / "output"),
            "comfy_url": comfy_url or "http://127.0.0.1:8188"}


def resolve(target, values):
    try:
        return target.format(**values)
    except (KeyError, IndexError, ValueError):
        return target


def run(action, builtins, values):
    """Run one action. builtins: {name: callable() -> message}. Returns a message for Shawn.
    Raises ValueError with a readable reason when the action can't run."""
    target = resolve(action.target, values)
    if action.kind == "open":
        if not os.path.exists(target):
            raise ValueError(f"{target} doesn't exist")
        os.startfile(target)                            # noqa: S606 - opens with the file's default app
        return f"Opened {target}"
    if action.kind == "url":
        if not target.lower().startswith(URL_SCHEMES):
            raise ValueError(f"only http, https and steam links can be opened ({target})")
        os.startfile(target)
        return f"Opened {target}"
    if action.kind == "launch":
        # a fixed program + fixed arguments, started directly - never through a shell
        subprocess.Popen([target, *[resolve(a, values) for a in action.args]], shell=False,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return f"Started {Path(target).name}"
    if action.kind == "builtin":
        if target not in builtins:
            raise ValueError(f"no built-in feature called '{target}'")
        return builtins[target]()
    raise ValueError(f"kind '{action.kind}' isn't allowed")


def list_text(actions):
    lines = ["Quick actions (run one with /do <name>, or type 'open ...' / '!name' in the Ctrl+Alt+J bar):"]
    lines += [f"- {a.label}" + (" (asks first)" if a.confirm else "") for a in actions]
    return "\n".join(lines)

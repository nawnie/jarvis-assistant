"""Game fixing: gather the evidence a person would dig for before asking "why does this crash?".

Read-only. Nothing here edits an ini, a load order or a mod. It collects:
  * Bethesda: newest crash logs (Crash Logger SSE/AE, Buffout 4, Crash Logger SF, .NET Script
    Framework), parsed down to the exception, the modules in the call stack and the plugins
    named; plus the load order size from plugins.txt and which ini files exist
  * emulators: the emulator's own log (error lines only) and whether BIOS files are present
  * any game: Windows' own crash records (Application log, event 1000) for that exe

diagnose() returns plain facts (shown instantly, no model needed) plus an evidence block the
local model can reason over when it is online.
"""
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog

ERROR_LINE = re.compile(r"\b(error|errors|fail(ed|ure)?|fatal|exception|crash(ed)?|missing|not found|unable|"
                        r"cannot|can't|invalid|corrupt)\b", re.I)


@dataclass
class Diagnosis:
    game: catalog.Game
    facts: list = field(default_factory=list)      # short bullet lines for the card
    evidence: str = ""                             # longer text for the model (bounded)
    files: list = field(default_factory=list)      # the log files that were read, newest first


# ===========================================================================
# Crash-log parsing (pure text in, dict out - unit-tested)
# ===========================================================================
_EXCEPTION = re.compile(r"(Unhandled (native )?exception[^\n]*|EXCEPTION_[A-Z_]+[^\n]*|Exception code[^\n]*)", re.I)
_MODULE = re.compile(r"\b([\w.+-]+\.(?:dll|exe))\b", re.I)
_PLUGIN = re.compile(r"\b([\w .'&()+-]{2,80}\.(?:esp|esm|esl))\b", re.I)


def parse_crash_log(text):
    """Pull out what matters from a Bethesda crash log:
    exception - the first 'Unhandled exception ...' style line
    modules   - dll/exe names in the call-stack section, in order, the game exe itself excluded
    plugins   - plugin files named anywhere in the 'possible relevant objects' / stack area
    """
    exc = _EXCEPTION.search(text or "")
    # the call stack section is headed differently by each logger; start there when we can find it
    lower = (text or "").lower()
    start = min([i for i in (lower.find("probable call stack"), lower.find("call stack"),
                             lower.find("probable callstack")) if i >= 0] or [0])
    stack_area = (text or "")[start:start + 6000]
    modules = []
    for name in _MODULE.findall(stack_area):
        low = name.lower()
        if catalog.detect(low) or low in ("kernelbase.dll", "ntdll.dll", "kernel32.dll"):
            continue              # the game itself and Windows' plumbing are in every stack
        if low not in (m.lower() for m in modules):
            modules.append(name)
    plugins = []
    for name in _PLUGIN.findall(text or ""):
        name = name.strip()
        if name.lower() not in (p.lower() for p in plugins):
            plugins.append(name)
    return {"exception": exc.group(0).strip()[:200] if exc else "", "modules": modules[:8], "plugins": plugins[:12]}


def error_lines(text, limit=25):
    """The lines of a log that look like problems, newest last, de-duplicated."""
    seen, out = set(), []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and ERROR_LINE.search(line) and line not in seen:
            seen.add(line)
            out.append(line[:240])
    return out[-limit:]


def parse_wevtutil(text, exe_name):
    """wevtutil /f:text output -> [{time, app, module, code}] for crashes of exe_name."""
    crashes = []
    for block in re.split(r"\n(?=Event\[\d+\])", text or ""):
        app = re.search(r"Faulting application name:\s*([^\s,]+)", block)
        if not app or app.group(1).lower() != exe_name.lower():
            continue
        module = re.search(r"Faulting module name:\s*([^\s,]+)", block)
        code = re.search(r"Exception code:\s*(0x[0-9a-fA-F]+)", block)
        when = re.search(r"Date:\s*([0-9T:.\-]+)", block)
        crashes.append({"time": when.group(1)[:19] if when else "?", "app": app.group(1),
                        "module": module.group(1) if module else "?", "code": code.group(1) if code else "?"})
    return crashes


# ===========================================================================
# Evidence gathering (touches the disk / event log)
# ===========================================================================
def _newest(paths, limit):
    files = [p for p in paths if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def _read(path, max_chars=60_000):
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""


def _age(path):
    minutes = (time.time() - path.stat().st_mtime) / 60
    if minutes < 90:
        return f"{minutes:.0f} min ago"
    if minutes < 48 * 60:
        return f"{minutes / 60:.0f} h ago"
    return f"{minutes / 1440:.0f} days ago"


def windows_crashes(exe_name, max_events=40):
    """Recent Windows crash reports (Application log, event 1000) for this exe. Best effort."""
    try:
        out = subprocess.run(["wevtutil", "qe", "Application", "/q:*[System[(EventID=1000)]]",
                              f"/c:{max_events}", "/rd:true", "/f:text"], capture_output=True, text=True,
                             timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return parse_wevtutil(out.stdout, exe_name)
    except (OSError, subprocess.SubprocessError):
        return []


def bethesda_evidence(game, exe_dir=None):
    """Facts + evidence for a Bethesda game: crash logs, load order, ini files."""
    facts, evidence, files = [], [], []
    my_games = Path(catalog.documents_dir()) / "My Games" / game.my_games
    # this is the crash-log section: every logger writes to a slightly different place
    candidates = []
    if game.extender:
        ext = my_games / game.extender
        for pattern in ("crash-*.log", "Crashlogs/*.log", "Logs/crash-*.log", "*crash*.log"):
            candidates += list(ext.glob(pattern))
    if exe_dir:
        candidates += list(Path(exe_dir, "Data", "NetScriptFramework", "Crash").glob("*.txt"))
    logs = _newest(set(candidates), 3)
    if logs:
        newest = logs[0]
        parsed = parse_crash_log(_read(newest))
        files += [str(p) for p in logs]
        facts.append(f"Newest crash log: **{newest.name}** ({_age(newest)})")
        if parsed["exception"]:
            facts.append(f"Exception: `{parsed['exception']}`")
        if parsed["modules"]:
            facts.append("Modules in the call stack: " + ", ".join(f"`{m}`" for m in parsed["modules"]))
        if parsed["plugins"]:
            facts.append("Plugins named in the log: " + ", ".join(parsed["plugins"][:8]))
        evidence.append(f"Newest crash log ({newest.name}, {_age(newest)}), parsed: {parsed}\n"
                        f"Top of that log:\n{_read(newest, 20_000)[:3500]}")
    else:
        facts.append(f"No crash logs found under {my_games}" + (f"\\{game.extender}" if game.extender else "")
                     + " - a crash logger plugin (Crash Logger / Buffout) makes crashes diagnosable.")
    # this is the load-order section (plugins.txt: '*' marks an active plugin)
    if game.appdata:
        plugins_txt = Path(os.environ.get("LOCALAPPDATA", "")) / game.appdata / "plugins.txt"
        if plugins_txt.is_file():
            lines = [ln.strip() for ln in _read(plugins_txt).splitlines() if ln.strip() and not ln.startswith("#")]
            active = [ln[1:] for ln in lines if ln.startswith("*")]
            facts.append(f"Load order: {len(active)} active of {len(lines)} plugins (plugins.txt, {_age(plugins_txt)}). "
                         "If you use Mod Organizer 2, its profile has its own plugins.txt.")
            evidence.append("Active plugins in order:\n" + "\n".join(active[:250]))
    inis = sorted(p.name for p in my_games.glob("*.ini")) if my_games.is_dir() else []
    if inis:
        facts.append("Ini files: " + ", ".join(inis))
    return facts, evidence, files


# emulator log and firmware locations; {docs} = Documents, {exe} = the emulator's own folder.
# "firmware" lists candidate folders (BIOS files, or Switch keys) in the order the emulator itself
# looks: a PORTABLE install (a user/ or portable folder next to the exe) wins over the per-user one,
# so a portable yuzu is never reported as "missing keys" just because %APPDATA%\yuzu is empty.
def _switch(name):
    return {"logs": ["{exe}/user/log/*.txt", "{appdata}/" + name + "/log/*.txt"],
            "firmware": ["{exe}/user/keys", "{appdata}/" + name + "/keys"], "label": "Switch keys"}


EMULATOR_FILES = {
    "retroarch": {"logs": ["{exe}/logs/*.log", "{exe}/retroarch.log"], "firmware": ["{exe}/system"]},
    "dolphin": {"logs": ["{exe}/User/Logs/*.log", "{docs}/Dolphin Emulator/Logs/*.log"], "firmware": []},
    "pcsx2": {"logs": ["{exe}/logs/*.txt", "{docs}/PCSX2/logs/*.txt", "{docs}/PCSX2/logs/*.log"],
              "firmware": ["{exe}/bios", "{docs}/PCSX2/bios"]},
    "duckstation": {"logs": ["{exe}/*.log", "{docs}/DuckStation/*.log"], "firmware": ["{exe}/bios", "{docs}/DuckStation/bios"]},
    "rpcs3": {"logs": ["{exe}/RPCS3.log"], "firmware": ["{exe}/dev_flash"]},
    "project64": {"logs": ["{exe}/Logs/*.log", "{exe}/Logs/*.txt"], "firmware": []},
    "cemu": {"logs": ["{exe}/log.txt"], "firmware": []},
    "ppsspp": {"logs": ["{exe}/memstick/*.log", "{docs}/PPSSPP/*.log"], "firmware": []},
    "ryujinx": {"logs": ["{exe}/portable/Logs/*.log", "{appdata}/Ryujinx/Logs/*.log"],
                "firmware": ["{exe}/portable/system", "{appdata}/Ryujinx/system"], "label": "Switch keys"},
    "yuzu": _switch("yuzu"), "suyu": _switch("suyu"), "sudachi": _switch("sudachi"), "citron": _switch("citron"),
    "xenia": {"logs": ["{exe}/xenia.log"], "firmware": []},
}


def emulator_evidence(process_name, exe_dir=None):
    facts, evidence, files = [], [], []
    key = next((k for k in EMULATOR_FILES if (process_name or "").lower().startswith(k)), None)
    if not key:
        return ["No log locations are known for this emulator yet."], evidence, files
    spec = EMULATOR_FILES[key]
    fill = {"docs": catalog.documents_dir(), "exe": exe_dir or "", "appdata": os.environ.get("APPDATA", "")}
    paths = []
    for pattern in spec["logs"]:
        if "{exe}" in pattern and not exe_dir:
            continue                          # emulator not running and path unknown: skip exe-relative logs
        full = Path(pattern.format(**fill))
        paths += list(full.parent.glob(full.name))
    logs = _newest(paths, 2)
    if logs:
        errors = error_lines(_read(logs[0]))
        files += [str(p) for p in logs]
        facts.append(f"Newest log: **{logs[0].name}** ({_age(logs[0])}), {len(errors)} problem line(s)")
        if errors:
            facts.append("Last problem line: `" + errors[-1][:160] + "`")
        evidence.append(f"Problem lines from {logs[0].name}:\n" + "\n".join(errors))
    else:
        facts.append("No emulator log found" + ("" if exe_dir else " (start the emulator once so Jarvis knows its folder)"))
    # this is the BIOS / keys check: the most common reason an emulator shows a black screen.
    # The first candidate folder that exists is the one the emulator uses.
    label = spec.get("label", "BIOS/system files")
    candidates = [Path(p.format(**fill)) for p in spec["firmware"] if "{exe}" not in p or exe_dir]
    if candidates:
        folder = next((c for c in candidates if c.is_dir()), None)
        if folder:
            count = len([p for p in folder.glob("*") if p.is_file()])
            facts.append(f"{label} ({folder}): " + (f"{count} file(s)" if count else "**folder is empty**"))
        else:
            facts.append(f"{label}: **none found** (looked in {', '.join(str(c) for c in candidates)})")
    return facts, evidence, files


def diagnose(process_name, exe_path=None):
    """Everything Jarvis can find about why this game misbehaves. process_name like 'skyrimse.exe'."""
    game = catalog.detect(process_name)
    if not game:
        return Diagnosis(catalog.Game(process_name or "Unknown program", "other"),
                         ["Jarvis doesn't know this program as a game, so only Windows' crash records were checked."])
    exe_dir = str(Path(exe_path).parent) if exe_path else None
    if game.family == "bethesda":
        facts, evidence, files = bethesda_evidence(game, exe_dir)
    elif game.family == "emulator":
        facts, evidence, files = emulator_evidence(process_name, exe_dir)
    else:
        facts, evidence, files = [], [], []
    crashes = windows_crashes(process_name)
    if crashes:
        latest = crashes[0]
        facts.append(f"Windows logged {len(crashes)} recent crash(es); latest {latest['time']} in "
                     f"`{latest['module']}` ({latest['code']})")
        evidence.append("Windows crash records (newest first):\n"
                        + "\n".join(f"- {c['time']} module {c['module']} code {c['code']}" for c in crashes[:8]))
    facts += [f"Tip: {t}" for t in game.tips]
    text = "\n\n".join(evidence)
    return Diagnosis(game, facts, text[:9000], files)

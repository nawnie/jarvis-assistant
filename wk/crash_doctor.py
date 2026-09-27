"""Crash doctor: when a program crashes or hangs, Jarvis tells Shawn why and how to fix it.

Source: the Windows event logs (read-only, via wevtutil, no console window):
  Application 1000  "Application Error"  - a program crashed (faulting app + module + exception code)
  Application 1002  "Application Hang"   - a program stopped responding and was closed
  System      4101  "Display"            - the GPU driver stopped responding and recovered (a TDR)

First seen on this PC (2026-09-26): Eden crashed at 02:56 inside ReShade64.dll (an injected
post-processing add-on) with 0xc0000005, and again at 03:22 - exactly the kind of cause a plain
"Eden closed" never shows.

The facts (program, module, code meaning, known-module hints) are worked out here, so they are
right even if the model is not; the model only turns them into a plain cause + fix.
"""
import calendar
import re
import subprocess
import time
import xml.etree.ElementTree as ET

NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}

# this is the exception-code dictionary: the codes Windows logs, in plain words
EXCEPTION_CODES = {
    "0xc0000005": "access violation (it read or wrote memory it doesn't own)",
    "0xc0000409": "stack buffer overrun / fail-fast (the program aborted itself on a security check)",
    "0xc0000135": "a required DLL was not found",
    "0xc0000142": "a DLL failed to initialise",
    "0xc000001d": "illegal CPU instruction (built for a CPU feature this PC lacks, e.g. AVX)",
    "0xc00000fd": "stack overflow (usually runaway recursion)",
    "0xc0000374": "heap corruption (memory was damaged earlier, often by a mod or injected DLL)",
    "0xc0000417": "invalid parameter passed to the C runtime",
    "0xe06d7363": "an unhandled C++ exception",
    "0xe0434352": "an unhandled .NET exception",
    "0x80000003": "breakpoint (the program called abort or hit a debug check)",
    "0x40000015": "abort (the program asked to be terminated)",
    "0xc0000006": "in-page error (a file or drive couldn't be read - disk or network trouble)",
}

# this is the known-module section: DLL name patterns -> what they usually mean
MODULE_HINTS = [
    (r"reshade", "ReShade (a post-processing injector) was loaded into the program; it often crashes emulators "
                 "and games after updates - update ReShade, or remove its DLL from the program's folder"),
    (r"^(nvoglv|nvwgf2|nvd3dum|nvlddmkm|nvcuda|nvgpucomp)", "the NVIDIA graphics driver - try the latest (or "
                 "previous) driver, and check any GPU overclock"),
    (r"^vulkan-1", "the Vulkan loader/driver - update the GPU driver; in emulators, try the other graphics backend"),
    (r"^(d3d11|d3d12|dxgi|d3dcompiler)", "DirectX - update the GPU driver; in emulators, try Vulkan instead"),
    (r"^(ucrtbase|msvcp\d+|vcruntime\d+)", "the Microsoft Visual C++ runtime - reinstall the latest "
                 "'Visual C++ Redistributable (x64)'"),
    (r"^ntdll", "Windows' core library - usually the real cause is memory damage from the program, a mod or an overlay"),
    (r"^kernelbase", "an error the program raised itself - check the program's own log file"),
    (r"(rtss|rtsshooks|msiafterburner)", "RivaTuner/Afterburner's overlay hook - disable its on-screen display for this program"),
    (r"(discord|gameoverlay|overlay)", "an overlay (Discord/Steam/other) - disable the overlay for this program"),
]


def _run_wevtutil(log, query, count):
    try:
        out = subprocess.run(["wevtutil", "qe", log, f"/q:{query}", "/f:xml", "/rd:true", f"/c:{count}"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    try:
        return ET.fromstring(f"<root>{out}</root>").findall("e:Event", NS)
    except ET.ParseError:
        return []


def _system(ev, tag):
    return ev.find(f"e:System/e:{tag}", NS)


def parse(ev):
    """One event -> a crash dict (kind, app, module, code, meaning, hints...), or None if unrelated."""
    event_id = int(_system(ev, "EventID").text)
    record = int(_system(ev, "EventRecordID").text)
    stamp = _system(ev, "TimeCreated").get("SystemTime", "")
    try:
        when = calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))   # the log stamps are UTC
    except ValueError:
        when = time.time()
    data = [d.text or "" for d in ev.findall("e:EventData/e:Data", NS)]
    crash = {"record": record, "when": when, "event_id": event_id}
    if event_id == 1000 and len(data) >= 7:
        module = re.sub(r"_unloaded$", "", data[3], flags=re.I)
        crash.update(kind="crash", app=data[0], module=module, code=data[6].lower(),
                     app_path=data[10] if len(data) > 10 else "", module_path=data[11] if len(data) > 11 else "")
    elif event_id == 1002 and data:
        crash.update(kind="hang", app=data[0], module="", code="", app_path=data[4] if len(data) > 4 else "")
    elif event_id == 4101:
        crash.update(kind="gpu-reset", app="GPU driver", module="nvlddmkm" if "nvlddmkm" in "".join(data).lower() else "",
                     code="", app_path="")
    else:
        return None
    code = crash.get("code", "")
    if code and not code.startswith("0x"):
        code = "0x" + code
        crash["code"] = code
    crash["meaning"] = EXCEPTION_CODES.get(code, "")
    base = re.sub(r"(_unloaded)?(\.dll)?$", "", (crash.get("module") or "").lower())
    crash["unloaded"] = event_id == 1000 and len(data) > 3 and data[3].lower().endswith("_unloaded")
    crash["hints"] = [hint for pattern, hint in MODULE_HINTS if base and re.search(pattern, base)]
    return crash


def read_recent(count=20):
    """The newest crashes, hangs and GPU driver resets, newest first."""
    events = _run_wevtutil("Application", "*[System[(EventID=1000 or EventID=1002)]]", count)
    events += _run_wevtutil("System", "*[System[Provider[@Name='Display'] and (EventID=4101)]]", 5)
    crashes = [c for c in (parse(ev) for ev in events) if c]
    return sorted(crashes, key=lambda c: c["when"], reverse=True)


class CrashDoctor:
    """Remembers what it has already reported, so each crash is announced once."""

    def __init__(self, reader=read_recent):
        self.reader = reader
        self.seen = None               # record ids already known (None until the first poll)
        self.recent = []               # newest first, kept for "explain last crash"

    def poll(self):
        """Returns crashes that are new since the last poll. The first poll only learns what's there
        (old crashes aren't announced), but they stay available in .recent."""
        crashes = self.reader()
        keys = {(c["event_id"], c["record"]) for c in crashes}
        if self.seen is None:
            self.seen = keys
            self.recent = crashes[:10]
            return []
        new = [c for c in crashes if (c["event_id"], c["record"]) not in self.seen]
        self.seen |= keys
        if new:
            self.recent = (new + self.recent)[:10]
        return new


# ---------------------------------------------------------------------------
# What the card shows and what the model is asked
# ---------------------------------------------------------------------------
def headline(crash):
    if crash["kind"] == "hang":
        return f"{crash['app']} froze and was closed"
    if crash["kind"] == "gpu-reset":
        return "The GPU driver stopped responding and recovered"
    where = f" in {crash['module']}" if crash.get("module") and crash["module"].lower() != crash["app"].lower() else ""
    return f"{crash['app']} crashed{where}"


def facts_md(crash):
    lines = [f"**{time.strftime('%a %H:%M', time.localtime(crash['when']))}** · {headline(crash)}"]
    if crash.get("code"):
        lines.append(f"**Error:** `{crash['code']}`" + (f" - {crash['meaning']}" if crash["meaning"] else ""))
    if crash.get("module_path") and crash["module_path"] != crash.get("app_path"):
        lines.append(f"**Module:** `{crash['module_path']}`" + (" (it had already been unloaded)" if crash["unloaded"] else ""))
    if crash.get("app_path"):
        lines.append(f"**Program:** `{crash['app_path']}`")
    for hint in crash["hints"]:
        lines.append(f"**Known culprit:** {hint}.")
    return "\n\n".join(lines)


def question(crash, earlier=()):
    same = [c for c in earlier if c.get("app") == crash.get("app") and c is not crash]
    history = (f"\nIt has also crashed/hung {len(same)} other time(s) recently: " +
               "; ".join(f"{time.strftime('%H:%M', time.localtime(c['when']))} {headline(c)}"
                         f"{' (' + c['code'] + ')' if c.get('code') else ''}" for c in same[:4])) if same else ""
    return ("A program on Shawn's PC just crashed. Windows recorded:\n" + facts_md(crash).replace("**", "") +
            history + "\n\nIn 3-4 short plain sentences, talking to him as 'you': the most likely cause, then the "
            "fix to try first, then what to try if that doesn't work. Use the 'Known culprit' notes if present. "
            "Don't repeat the raw numbers back.")

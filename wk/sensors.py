"""Cheap, local sensors. None of these call the model; they just read the PC's state."""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess

import psutil

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


# ---------------------------------------------------------------------------
# Focused window: which program you're in and its title bar text
# ---------------------------------------------------------------------------
def foreground_window():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", ""
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        proc = psutil.Process(pid.value)
        name = proc.name().lower()
        if name not in EXE_PATHS:  # remember where each program lives, for its friendly name later
            EXE_PATHS[name] = proc.exe()
    except (psutil.Error, ValueError):
        name = "unknown"
    return name, buf.value


def task_windows():
    """Visible Claude/Codex top-level window titles; no chat text or keystrokes."""
    found = []
    allowed = {"claude.exe", "codex.exe"}
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length or length > 500:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            process = psutil.Process(pid.value).name().lower()
        except (psutil.Error, ValueError):
            return True
        if process not in allowed:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if title.value.strip():
            found.append((process, title.value.strip()[:160]))
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Friendly program names ("chrome.exe" -> "Google Chrome"), read from the
# FileDescription in each exe's version info - the same text Task Manager shows.
# ---------------------------------------------------------------------------
EXE_PATHS = {}
_DESCRIPTIONS = {}
_NAMES_FILE = None
version = ctypes.windll.version


def remember_names_in(path):
    """Keep friendly names in a small JSON file, so the Today table can show 'Google Chrome'
    after a restart without waiting for you to focus each app again."""
    import json
    global _NAMES_FILE
    _NAMES_FILE = path
    try:
        _DESCRIPTIONS.update({k: v for k, v in json.loads(open(path, encoding="utf-8").read()).items() if v})
    except (OSError, ValueError):
        pass


def _save_names():
    import json
    if _NAMES_FILE:
        try:
            with open(_NAMES_FILE, "w", encoding="utf-8") as f:
                json.dump({k: v for k, v in _DESCRIPTIONS.items() if v}, f)
        except OSError:
            pass


def _exe_path(process):
    if process in EXE_PATHS:
        return EXE_PATHS[process]
    for proc in psutil.process_iter(["name", "exe"]):  # not seen focused yet: look for it running
        if (proc.info["name"] or "").lower() == process and proc.info["exe"]:
            EXE_PATHS[process] = proc.info["exe"]
            return proc.info["exe"]
    return None


def app_description(process, scan=True):
    """Human name of a program, or "" if unknown (not running and never seen focused).
    scan=False: only use programs already seen this session - never the slow running-process scan
    (used for table labels, which would otherwise freeze the window on first display)."""
    if process in _DESCRIPTIONS:
        return _DESCRIPTIONS[process]
    if not scan and process not in EXE_PATHS:
        return ""
    text = ""
    path = _exe_path(process)
    if path:
        try:
            size = version.GetFileVersionInfoSizeW(path, None)
            data = ctypes.create_string_buffer(size)
            if size and version.GetFileVersionInfoW(path, 0, size, data):
                ptr, length = ctypes.c_void_p(), wt.UINT()
                version.VerQueryValueW(data, r"\VarFileInfo\Translation", ctypes.byref(ptr), ctypes.byref(length))
                lang, codepage = ctypes.cast(ptr, ctypes.POINTER(wt.WORD * 2)).contents
                key = rf"\StringFileInfo\{lang:04x}{codepage:04x}\FileDescription"
                if version.VerQueryValueW(data, key, ctypes.byref(ptr), ctypes.byref(length)) and length.value:
                    text = ctypes.wstring_at(ptr, length.value).rstrip("\x00").strip()
        except (OSError, ValueError):
            text = ""
    _DESCRIPTIONS[process] = text
    if text:
        _save_names()
    return text


# ---------------------------------------------------------------------------
# Idle time: seconds since the last keyboard/mouse input anywhere on the PC
# ---------------------------------------------------------------------------
class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_seconds():
    info = _LastInput()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    # the tick counter wraps every ~49 days; masking keeps the subtraction correct
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


# ---------------------------------------------------------------------------
# System load. nvidia-smi is launched with no console window so nothing flashes.
# ---------------------------------------------------------------------------
def system_stats():
    stats = {"cpu": psutil.cpu_percent(interval=None), "ram": psutil.virtual_memory().percent,
             "gpu": None, "vram_used": None, "vram_total": None, "gpu_temp": None}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        util, used, total, temp = [float(v) for v in out.stdout.splitlines()[0].split(",")]
        stats.update(gpu=util, vram_used=used, vram_total=total, gpu_temp=temp)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass  # no NVIDIA GPU / driver busy: the GPU card just shows "n/a"
    return stats


# ---------------------------------------------------------------------------
# Folder watcher: remembers what was in each folder and reports new arrivals.
# Polling (not OS hooks) keeps it dependency-free; a few folders is cheap.
# ---------------------------------------------------------------------------
PARTIAL_SUFFIXES = (".crdownload", ".part", ".tmp", ".partial", ".download")


class FolderWatcher:
    def __init__(self):
        self.seen = {}

    def poll(self, folders):
        """Return a list of (folder, filename) that appeared since the last poll."""
        found = []
        for folder in folders:
            try:
                names = {e.name for e in os.scandir(folder)}
            except OSError:
                continue
            if folder in self.seen:  # first look at a folder only records a baseline
                for name in sorted(names - self.seen[folder]):
                    if not name.lower().endswith(PARTIAL_SUFFIXES) and not name.startswith("~$"):
                        found.append((folder, name))
            self.seen[folder] = names
        return found

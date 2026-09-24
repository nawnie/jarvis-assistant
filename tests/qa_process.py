"""Whole-app process tests: launches the real app the way the desktop shortcut does (plain pythonw,
one process) against an isolated data folder.

Only one Jarvis may ever run, so qa/run_gates.py stops your Jarvis before this and restarts it
(hidden) afterwards. If a Jarvis is still running when this starts, the test stops immediately.

Checks:
  - starts on an empty data folder and creates its database; --hidden shows no window
  - exactly ONE Jarvis process and at most ONE window at every step
  - launching again (normally or --hidden) hands over and exits; the running Jarvis's window comes up
  - two launches at the same moment from cold: exactly one survives
  - a corrupt config.json doesn't stop it starting
  - it never blocks Windows shutdown/logoff, and exits when the session ends

Run: .venv\\Scripts\\python.exe tests\\qa_process.py      exit code = number of failures
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
PYW = Path(sys.base_prefix) / "pythonw.exe"          # what the desktop shortcut runs
APP = ROOT / "jarvis_assistant.pyw"
TMP = ROOT / "qa" / "tmp_process"
user32 = ctypes.windll.user32
user32.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT,
                                       ctypes.POINTER(ctypes.c_size_t)]
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def jarvis_pids():
    return {p.pid for p in psutil.process_iter(["name", "cmdline"])
            if (p.info["name"] or "").lower() in ("pythonw.exe", "python.exe")
            and any("jarvis_assistant.pyw" in c for c in (p.info["cmdline"] or []))}


def jarvis_windows(visible_only=True):
    """Top-level windows owned by Jarvis processes (visible main windows only, unless asked)."""
    pids, found = jarvis_pids(), []
    PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if not visible_only or (user32.IsWindowVisible(hwnd) and buf.value == "Jarvis Assistant"):
                found.append((hwnd, buf.value))
        return True
    user32.EnumWindows(PROC(cb), 0)
    return found


def fresh_dir(name, config_text=None):
    """An isolated data folder. config_text=None -> quiet test config; "" -> no config file at all."""
    d = TMP / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    if config_text is None:
        config_text = json.dumps({"away_model_enabled": False, "away_free_comfyui": False, "projects_enabled": False, "explain_on_click": False, "quick_ask_hotkey": False, "llm_autostart_server": False})
    if config_text:
        (d / "config.json").write_text(config_text, encoding="utf-8")
    return d


def launch(data_dir, *args):
    return subprocess.Popen([str(PYW), str(APP), *args], cwd=str(ROOT),
                            env=dict(os.environ, JARVIS_DATA_DIR=str(data_dir), JARVIS_NO_MODEL_CONTROL="1"))


def wait_for(cond, secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        if cond():
            return True
        time.sleep(0.2)
    return cond()


def stop_all():
    for pid in jarvis_pids():
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass
    wait_for(lambda: not jarvis_pids(), 10)


if jarvis_pids():
    print("FAIL a Jarvis is already running - stop it first (qa/run_gates.py does this for you)", flush=True)
    sys.exit(1)

try:
    # --- 1. fresh data folder, --hidden -------------------------------------------------
    d1 = fresh_dir("fresh", config_text="")
    a = launch(d1, "--hidden")
    check("starts on an empty folder and creates its database", wait_for(lambda: (d1 / "jarvis.db").exists(), 20))
    time.sleep(3)
    check("--hidden: running as exactly one process", jarvis_pids() == {a.pid}, str(jarvis_pids()))
    check("--hidden: no window shown", not jarvis_windows(), str(jarvis_windows()))

    # --- 2. launching again hands over; still one Jarvis, one window ---------------------
    b = launch(d1)
    check("second launch exits by itself", wait_for(lambda: b.poll() is not None, 15), f"exit code {b.poll()}")
    check("the running Jarvis's window comes up", wait_for(lambda: len(jarvis_windows()) == 1, 10))
    for extra in (launch(d1, "--hidden"), launch(d1)):
        wait_for(lambda: extra.poll() is not None, 15)
    time.sleep(1)
    check("after 3 more launches: still exactly one Jarvis process", jarvis_pids() == {a.pid}, str(jarvis_pids()))
    check("after 3 more launches: still exactly one window", len(jarvis_windows()) == 1, str(jarvis_windows()))

    # --- 3. Windows shutdown / logoff ------------------------------------------------------
    hwnds = jarvis_windows(visible_only=False)
    answers = []
    for hwnd, _ in hwnds:
        res = ctypes.c_size_t()
        if user32.SendMessageTimeoutW(hwnd, 0x0011, 0, 0x80000000, 0x0002, 3000, ctypes.byref(res)):  # WM_QUERYENDSESSION
            answers.append(res.value)
    check("never blocks shutdown/logoff", answers and all(v != 0 for v in answers), f"replies {answers}")
    for hwnd, _ in hwnds:
        res = ctypes.c_size_t()
        user32.SendMessageTimeoutW(hwnd, 0x0016, 1, 0x80000000, 0x0002, 3000, ctypes.byref(res))       # WM_ENDSESSION
    check("exits when the session ends", wait_for(lambda: a.poll() is not None, 10))
    stop_all()

    # --- 4. two launches at the same moment, from cold: exactly one survives ----------------
    d2 = fresh_dir("race")
    racers = [launch(d2, "--hidden"), launch(d2, "--hidden")]
    time.sleep(6)
    alive = [p for p in racers if p.poll() is None]
    check("simultaneous launches: exactly one survives", len(alive) == 1 and len(jarvis_pids()) == 1,
          f"alive {len(alive)}, jarvis processes {len(jarvis_pids())}")
    stop_all()

    # --- 5. corrupt config.json -----------------------------------------------------------------
    d3 = fresh_dir("corrupt", config_text="{this is not json")
    c = launch(d3, "--hidden")
    time.sleep(6)
    check("corrupt config: still starts", c.poll() is None and (d3 / "jarvis.db").exists())
finally:
    stop_all()
    time.sleep(0.5)
    shutil.rmtree(TMP, ignore_errors=True)

failed = results.count(False)
print(f"\nprocess: {len(results) - failed} passed, {failed} failed", flush=True)
sys.exit(min(failed, 250))

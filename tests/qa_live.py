"""Live desktop tests: the global Ctrl+click hook and the Ctrl+Alt+J quick-ask hotkey.

These need real Windows input, so they inject it with SendInput. Each test click is ONE SendInput
batch (Ctrl down, move, click, Ctrl up), which Windows inserts into the input stream as a unit -
your own mouse movement can't split it. The cursor is put back straight afterwards.
Harmless targets come first; the only state-changing click (Pause 30 min) is the LAST test.

The real Jarvis ignores injected input, so it never reacts to these test clicks.

Run: .venv\\Scripts\\python.exe tests\\qa_live.py [evidence-dir]      exit code = number of failures
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

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "qa" / "tmp_live"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
(TMP / "config.json").write_text(json.dumps({"away_model_enabled": False, "away_free_comfyui": False, "projects_enabled": False, "llm_autostart_server": False}), encoding="utf-8")
os.environ["JARVIS_DATA_DIR"] = str(TMP)
os.environ["JARVIS_HOTKEY_KEY"] = "K"      # your real Jarvis holds Ctrl+Alt+J; the test copy uses Ctrl+Alt+K
HOTKEY_VK = ord("K")
sys.path.insert(0, str(ROOT))
EVIDENCE = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "qa" / "evidence" / "live"
EVIDENCE.mkdir(parents=True, exist_ok=True)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

app = QApplication(sys.argv)
from wk import pointer  # noqa: E402
from wk.brain import Engine  # noqa: E402
from wk.ui import MainWindow  # noqa: E402

user32 = ctypes.windll.user32
results = []


# ---------------------------------------------------------------------------
# SendInput plumbing
# ---------------------------------------------------------------------------
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def _key(vk, up=False):
    i = INPUT(type=1)
    i.u.ki = KEYBDINPUT(vk, 0, 2 if up else 0, 0, 0)
    return i


def _mouse(flags, x=0, y=0):
    i = INPUT(type=0)
    if flags & 0x8000:  # absolute: 0..65535 across the whole virtual desktop
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        x = round((x - vx) * 65535 / (vw - 1))
        y = round((y - vy) * 65535 / (vh - 1))
    i.u.mi = MOUSEINPUT(x, y, 0, flags, 0, 0)
    return i


def send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


MOVE = 0x0001 | 0x8000 | 0x4000   # move | absolute | whole virtual desktop


user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
OUR_WINDOWS = []          # filled once the test window exists


def target_is_ours(x, y):
    """SAFETY: which top-level window will Windows deliver a click at (x, y) to?"""
    root = user32.GetAncestor(user32.WindowFromPoint(wt.POINT(x, y)), 2)
    if root in OUR_WINDOWS:
        return True, ""
    buf = ctypes.create_unicode_buffer(200)
    user32.GetWindowTextW(root, buf, 200)
    return False, buf.value or f"hwnd {root}"


def _window_spots():
    """Places to try putting the test window: where it is now, then each screen corner."""
    area = w.screen().availableGeometry()
    right, bottom = area.right() - w.width() - 10, area.bottom() - w.height() - 10
    return [None, (area.left() + 10, area.top() + 10), (right, area.top() + 10),
            (area.left() + 10, bottom), (right, bottom)]


def batch_click(target, ctrl=False):
    """One atomic batch: [Ctrl down] move, left down, left up [Ctrl up].
    target() gives the click point in physical pixels. The click is never sent unless that pixel
    belongs to the test window: if one of your own windows (e.g. an always-on-top app) covers it,
    the test window moves to another spot and re-aims; if every spot is covered, nothing is sent.
    Returns the (x, y) clicked, or None if the click was skipped."""
    for spot in _window_spots():
        if spot is not None:
            w.move(*spot)
        pointer.force_foreground(OUR_WINDOWS[0])
        t0 = time.time()
        while time.time() - t0 < 0.2:
            app.processEvents()
            time.sleep(0.01)
        x, y = target()
        ours, other = target_is_ours(x, y)
        if ours:
            seq = ([_key(0x11)] if ctrl else []) + [_mouse(MOVE, x, y), _mouse(0x0002), _mouse(0x0004)] \
                + ([_key(0x11, up=True)] if ctrl else [])
            send(*seq)
            return x, y
        print(f"window spot {spot}: target ({x},{y}) covered by '{other}' - trying another spot", flush=True)
    print("SKIPPED CLICK: every spot covered - not sent", flush=True)
    return None


def put_cursor_back(point):
    send(_mouse(MOVE, point.x, point.y))


def hotkey_ctrl_alt_j():   # (Ctrl+Alt+K in the test copy)
    send(_key(0x11), _key(0x12), _key(HOTKEY_VK), _key(HOTKEY_VK, True), _key(0x12, True), _key(0x11, True))


# ---------------------------------------------------------------------------
def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def wait(cond, secs=60):
    t0 = time.time()
    while not cond() and time.time() - t0 < secs:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    return cond()


def settle(secs=0.4):
    wait(lambda: False, secs)


def physical(widget, local=None):
    # if the test window was minimized or covered meanwhile (e.g. you clicked away), bring it back
    # BEFORE working out where to click - a minimized window sits at about (-32000, -32000)
    top = widget.window()
    if top.isMinimized() or not top.isVisible():
        top.showNormal()
        top.raise_()
        settle(0.4)
    g = widget.mapToGlobal(local if local is not None else widget.rect().center())
    dpr = widget.screen().devicePixelRatio()
    return round(g.x() * dpr), round(g.y() * dpr)


saved = wt.POINT()
user32.GetCursorPos(ctypes.byref(saved))
e = Engine()
e.mouse.accept_injected = True
w = MainWindow(e)
w.setWindowFlag(Qt.WindowStaysOnTopHint, True)
w.move(80, 60)
w.show()
OUR_WINDOWS[:] = [int(w.winId()), int(w.card.winId())]
now = time.time()
e.store.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)",
            (now - 3000, now - 600, "chrome.exe", "prism-ml - Hugging Face - Google Chrome"))
w._refresh_today()
check("model online", wait(e.llm.online, 60))
settle(1.0)
pause_btn = [b for b in w.findChildren(QPushButton) if b.text() == "Pause 30 min"][0]
nav_item = w.nav.item(1)                              # "Timeline": the harmless Ctrl+click target


def nav_point():
    return physical(w.nav.viewport(), w.nav.visualItemRect(nav_item).center())


helper = None
try:
    # --- A: Ctrl+click through the real global hook -------------------------------------
    w.show_page("Now")
    settle()
    x, y = batch_click(nav_point, ctrl=True) or (0, 0)
    put_cursor_back(saved)
    check("ctrl+click: card appears", wait(lambda: w.card.isVisible(), 5))
    check("ctrl+click: click swallowed (page unchanged)", w.stack.currentIndex() == 0)
    wait(lambda: w.card.title.text() != "Looking...", 10)
    check("ctrl+click: identified the element", "Timeline" in w.card.title.text(), f"{w.card.title.text()} | {w.card.sub.text()}")
    check("ctrl+click: card opens at the click, not the cursor", abs(w.card.x() - (x / w.screen().devicePixelRatio() + 18)) < 40,
          f"card x {w.card.x()} vs click x {x / w.screen().devicePixelRatio():.0f}")
    check("ctrl+click: model answered", wait(lambda: bool(w.card.answer_md), 60), " ".join(w.card.answer_md.split())[:120])
    check("ctrl+click: didn't read its own card", "Looking" not in w.card.answer_md)
    settle(0.3)
    w.card.grab().save(str(EVIDENCE / "card-ctrlclick.png"))

    # --- B: ordinary click elsewhere closes the card ------------------------------------
    batch_click(lambda: physical(w.state_label))
    put_cursor_back(saved)
    check("outside click closes the card", wait(lambda: not w.card.isVisible(), 3))

    # --- C: private windows are never read -------------------------------------------------
    e.cfg["excluded_processes"] = e.cfg["excluded_processes"] + ["python.exe", "pythonw.exe"]
    batch_click(nav_point, ctrl=True)
    put_cursor_back(saved)
    wait(lambda: w.card.isVisible() and w.card.title.text() != "Looking...", 8)
    check("private window: refused", w.card.title.text() == "Private window", w.card.title.text())
    check("private window: click still swallowed", w.stack.currentIndex() == 0)
    e.cfg["excluded_processes"] = e.cfg["excluded_processes"][:-2]
    w.card.hide()
    settle()

    # --- D: paused -> Ctrl+click passes straight through ------------------------------------
    e.set_watching(False)
    settle()
    batch_click(nav_point, ctrl=True)
    put_cursor_back(saved)
    settle(0.6)
    check("paused: no card, click reaches the app", not w.card.isVisible() and w.stack.currentIndex() == 1)
    e.set_watching(True)
    w.show_page("Now")
    settle()

    # --- E: Ctrl+Alt+J quick-ask with selected text in another app ---------------------------
    helper = subprocess.Popen([sys.executable, "-c", r'''
import sys
from PySide6.QtWidgets import QApplication, QPlainTextEdit
app = QApplication(sys.argv)
ed = QPlainTextEdit(); ed.setWindowTitle("Jarvis selection test")
ed.setPlainText("The mitochondria is the powerhouse of the cell."); ed.selectAll()
ed.resize(520, 140); ed.move(200, 700); ed.show(); ed.raise_(); ed.activateWindow()
app.exec()
'''])
    user32.FindWindowW.restype = ctypes.c_void_p
    hwnd, t0 = None, time.time()
    while not hwnd and time.time() - t0 < 15:
        hwnd = user32.FindWindowW(None, "Jarvis selection test")
        settle(0.2)
    settle(0.8)
    pointer.force_foreground(hwnd)
    settle(0.4)
    check("hotkey: test app has focus", user32.GetForegroundWindow() == hwnd)
    hotkey_ctrl_alt_j()
    check("hotkey: ask bar appears", wait(lambda: w.askbar.isVisible(), 5))
    settle(0.4)
    check("hotkey: bar has keyboard focus", user32.GetForegroundWindow() == int(w.askbar.winId()) and w.askbar.line.hasFocus())
    check("hotkey: knows which app you were in", "selection test" in w.askbar.ctx_label.text().lower(), w.askbar.ctx_label.text())
    w.askbar.selection.done.wait(3)
    check("hotkey: read your selected text", "powerhouse" in w.askbar.selection.text, w.askbar.selection.text[:60])
    QTest.keyClicks(w.askbar.line, "what does this sentence mean?")
    QTest.keyClick(w.askbar.line, Qt.Key_Return)
    check("ask: answer card opens", wait(lambda: w.card.isVisible() and not w.askbar.isVisible(), 3))
    check("ask: model answered", wait(lambda: bool(w.card.answer_md), 60), " ".join(w.card.answer_md.split())[:120])
    settle(0.3)
    w.card.grab().save(str(EVIDENCE / "card-quickask.png"))
    w.card.hide()
    pointer.force_foreground(hwnd)
    settle(0.3)
    hotkey_ctrl_alt_j()
    wait(lambda: w.askbar.isVisible(), 5)
    QTest.keyClick(w.askbar, Qt.Key_Escape)
    check("hotkey: Esc closes the bar", wait(lambda: not w.askbar.isVisible(), 2))
    pointer.force_foreground(hwnd)
    settle(0.3)
    hotkey_ctrl_alt_j()
    check("hotkey: bar opens again", wait(lambda: w.askbar.isVisible(), 5))
    settle(0.4)
    pointer.force_foreground(hwnd)          # you click back into another app
    check("ask bar closes when another app takes focus", wait(lambda: not w.askbar.isVisible(), 3))
    helper.terminate()
    helper = None

    # --- F: hotkey switched off at runtime -> released, nothing opens ------------------------
    cfg = dict(e.cfg)
    cfg["quick_ask_hotkey"] = False
    e.save_config(cfg)
    settle(0.5)
    hotkey_ctrl_alt_j()
    settle(0.8)
    check("hotkey off: nothing opens", not w.askbar.isVisible())
    probe = user32.RegisterHotKey(None, 99, 0x0002 | 0x0001 | 0x4000, HOTKEY_VK)
    check("hotkey off: Ctrl+Alt+J is released for other apps", bool(probe))
    if probe:
        user32.UnregisterHotKey(None, 99)
    cfg["quick_ask_hotkey"] = True
    e.save_config(cfg)
    settle(0.5)
    probe = user32.RegisterHotKey(None, 99, 0x0002 | 0x0001 | 0x4000, HOTKEY_VK)
    check("hotkey back on: Jarvis holds it again", not probe)
    if probe:
        user32.UnregisterHotKey(None, 99)

    # --- G (LAST): feature off -> Ctrl+click on "Pause 30 min" reaches the button ------------
    e.cfg["explain_on_click"] = False
    batch_click(lambda: physical(pause_btn), ctrl=True)
    put_cursor_back(saved)
    settle(0.6)
    check("feature off: click reaches the app (Pause 30 min pressed)", not e.watching and not w.card.isVisible())
    e.set_watching(True)
finally:
    if helper:
        helper.terminate()
    put_cursor_back(saved)
    e.shutdown()

failed = results.count(False)
print(f"\nlive: {len(results) - failed} passed, {failed} failed", flush=True)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(min(failed, 250))

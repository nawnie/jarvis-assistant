"""Ctrl+click "explain this": a global mouse trigger and the sensors that read
whatever is under the mouse pointer.

How it fits together:
  MouseTrigger      - a Windows low-level mouse hook on its own thread. When the
                      trigger combo is clicked it reports the pointer position and
                      swallows that click so the app underneath doesn't also react.
  look_at(x, y)     - gathers everything knowable about that spot:
                        * the window + program it belongs to
                        * UI Automation: the control's name / type / value (buttons, menus, fields)
                        * OCR of the screen area around the pointer (images, games, canvases)
                        * a screenshot crop, returned as raw pixels for the popup

All coordinates are physical screen pixels (Qt makes the process per-monitor DPI aware).
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import os
import threading

import psutil

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# ---------------------------------------------------------------------------
# Win32 signatures. Declared explicitly because 64-bit handles and pointers
# get truncated if ctypes has to guess.
# ---------------------------------------------------------------------------
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
# without this, ctypes truncates the 64-bit module handle and Windows refuses the hook (error 126)
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE

WH_MOUSE_LL = 14
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_QUIT, WM_HOTKEY = 0x0201, 0x0202, 0x0012, 0x0312
WM_SET_HOTKEY = 0x8000 + 1   # our own message: wParam 1 = hold the quick-ask hotkey, 0 = release it
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
# Ctrl+Alt+J. JARVIS_HOTKEY_KEY exists only so the QA suite can test on another letter
# while your real Jarvis keeps holding Ctrl+Alt+J.
QUICK_ASK_KEY = (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord(os.environ.get("JARVIS_HOTKEY_KEY", "J")[:1].upper()))
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
LLMHF_INJECTED = 0x01


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


def _held(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


# ---------------------------------------------------------------------------
# The global trigger.
# Windows calls the hook for EVERY mouse event system-wide, so the callback
# does the bare minimum (check button + modifier keys) and hands off; anything
# slow here would make the whole PC's mouse lag.
# ---------------------------------------------------------------------------
class MouseTrigger(threading.Thread):
    def __init__(self, armed, on_fire, listening=lambda: False, on_click=None, on_error=None, on_hotkey=None,
                 hotkey_wanted=lambda: True):
        """armed() -> which combo is live right now: "ctrl", "ctrl+alt", or None (off).
        on_fire(x, y) is called on the hook thread; it must be quick (emit a Qt signal).
        listening() / on_click(): while an info card is open, every ordinary left click is
        reported too (never swallowed) so the card can close when you click elsewhere."""
        super().__init__(daemon=True, name="jarvis-mouse-hook")
        self.armed, self.on_fire = armed, on_fire
        self.listening, self.on_click = listening, on_click
        self.on_error = on_error
        self.on_hotkey = on_hotkey
        self.hotkey_wanted = hotkey_wanted
        self.hotkey_held = False
        self.installed = False
        self._swallow_up = False
        self.accept_injected = False  # synthetic clicks (automation tools) are ignored; tests flip this
        self._proc = HOOKPROC(self._callback)  # keep a reference or Windows calls freed memory
        self._thread_id = None

    def _callback(self, code, wparam, lparam):
        # Any error in here must never leave the PC's mouse in a half-clicked state, so the
        # whole decision is guarded and, on error, the event simply goes on to Windows untouched.
        try:
            if code == 0 and wparam in (WM_LBUTTONDOWN, WM_LBUTTONUP):
                info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if wparam == WM_LBUTTONUP and self._swallow_up:
                    self._swallow_up = False
                    return 1  # eat the release that belongs to the click we ate
                if wparam == WM_LBUTTONDOWN and (self.accept_injected or not info.flags & LLMHF_INJECTED):
                    combo = self.armed()
                    wants_alt = combo == "ctrl+alt"
                    if (combo and _held(VK_CONTROL) and not _held(VK_SHIFT)
                            and _held(VK_MENU) == wants_alt):
                        self.on_fire(info.pt.x, info.pt.y)
                        self._swallow_up = True   # only once the explain request is safely handed over
                        return 1  # eat the click: the app underneath never sees it
                    if self.on_click and self.listening():
                        self.on_click()  # ordinary click, passed through untouched
        except Exception:
            self._swallow_up = False
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _apply_hotkey(self, want):
        """(Un)register the quick-ask hotkey. Runs on this thread, which owns the registration."""
        if want and not self.hotkey_held:
            self.hotkey_held = bool(user32.RegisterHotKey(None, 1, *QUICK_ASK_KEY))
            if not self.hotkey_held and self.on_error:
                self.on_error(f"Quick-ask hotkey Ctrl+Alt+{chr(QUICK_ASK_KEY[1])} is already taken by another app")
        elif not want and self.hotkey_held:
            user32.UnregisterHotKey(None, 1)
            self.hotkey_held = False

    def set_hotkey(self, want):
        """Called from the GUI thread when the setting changes; the hook thread does the work."""
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_SET_HOTKEY, 1 if want else 0, 0)

    def run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        if not hook:
            # hook refused: say so (instead of Ctrl+click silently doing nothing)
            if self.on_error:
                self.on_error(f"Ctrl+click explain could not start (Windows error {kernel32.GetLastError()})")
            return
        self.installed = True

        # the quick-ask hotkey is registered on this same thread (only while it's switched on),
        # so its message arrives in the loop below; the setting can change it at any time
        if self.on_hotkey:
            self._apply_hotkey(self.hotkey_wanted())

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and self.on_hotkey:
                self.on_hotkey()
            elif msg.message == WM_SET_HOTKEY and self.on_hotkey:
                self._apply_hotkey(bool(msg.wParam))
        self._apply_hotkey(False)
        user32.UnhookWindowsHookEx(hook)

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


# ---------------------------------------------------------------------------
# Which window/program is under a screen point (not just the focused one)
# ---------------------------------------------------------------------------
def to_logical(x, y):
    """Physical screen pixel (what Windows and the mouse hook use) -> Qt's logical coordinates,
    on whichever monitor contains it (each monitor can have its own scaling)."""
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtGui import QGuiApplication
    for screen in QGuiApplication.screens():
        geo, dpr = screen.geometry(), screen.devicePixelRatio()
        native = QRect(geo.topLeft(), geo.size() * dpr)
        if native.contains(x, y):
            return QPoint(round(geo.x() + (x - geo.x()) / dpr), round(geo.y() + (y - geo.y()) / dpr))
    dpr = QGuiApplication.primaryScreen().devicePixelRatio()
    return QPoint(round(x / dpr), round(y / dpr))


def window_at(x, y):
    hwnd = user32.GetAncestor(user32.WindowFromPoint(wt.POINT(x, y)), 2)  # GA_ROOT
    if not hwnd:
        return "", ""
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        name = psutil.Process(pid.value).name().lower()
    except (psutil.Error, ValueError):
        name = "unknown"
    return name, buf.value


# ---------------------------------------------------------------------------
# Screen capture of a box around the pointer, as top-down BGRA bytes
# ---------------------------------------------------------------------------
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


def capture_around(x, y, width=960, height=600):
    """Return (bgra_bytes, left, top, w, h) for a box centred on (x, y), clipped to the desktop."""
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)      # virtual screen origin
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)      # virtual screen size
    w, h = min(width, vw), min(height, vh)
    left = max(vx, min(x - w // 2, vx + vw - w))
    top = max(vy, min(y - h // 2, vy + vh - h))
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(mem, bmp)
    gdi32.BitBlt(mem, 0, 0, w, h, screen, left, top, 0x00CC0020 | 0x40000000)  # SRCCOPY | CAPTUREBLT
    header = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, pixels, ctypes.byref(header), 0)
    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, screen)
    return pixels.raw, left, top, w, h


# ---------------------------------------------------------------------------
# UI Automation: what control is under the pointer, and what it's inside of.
# Runs on its own thread because it needs a COM apartment that the OCR code
# (WinRT) would otherwise conflict with.
# ---------------------------------------------------------------------------
def _uia_at(x, y, out):
    try:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            ctrl = auto.ControlFromPoint(x, y)
            if not ctrl:
                return
            out["name"] = (ctrl.Name or "")[:300]
            out["type"] = ctrl.ControlTypeName
            # the control's current value/content (a text field's text, a slider's number...);
            # GetPattern works on every control type, unlike the typed Get*Pattern helpers
            for pattern_id in (auto.PatternId.ValuePattern, auto.PatternId.LegacyIAccessiblePattern):
                try:
                    pattern = ctrl.GetPattern(pattern_id)
                    value = getattr(pattern, "Value", "") or ""
                    if value:
                        out["value"] = value[:500]
                        break
                except Exception:
                    continue
            help_text = getattr(ctrl, "HelpText", "") or ""
            if help_text:
                out["help"] = help_text[:300]
            # parent chain gives context, e.g. "Button 'Run' in ToolBar 'Debug' in Window 'VS Code'"
            chain, node = [], ctrl.GetParentControl()
            while node and len(chain) < 4:
                if node.Name:
                    chain.append(f"{node.ControlTypeName} '{node.Name[:60]}'")
                node = node.GetParentControl()
            out["inside"] = chain
    except Exception as exc:  # UIA is best-effort: some apps (games) expose nothing
        out["error"] = str(exc)[:200]


# ---------------------------------------------------------------------------
# Windows' built-in OCR (the same engine as the Snipping Tool's text actions)
# ---------------------------------------------------------------------------
async def _ocr_async(bgra, w, h):
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        return []
    writer = DataWriter()
    writer.write_bytes(bgra)
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h)
    result = await engine.recognize_async(bitmap)
    lines = []
    for line in result.lines:
        rects = [word.bounding_rect for word in line.words]
        x0 = min(r.x for r in rects)
        y0 = min(r.y for r in rects)
        x1 = max(r.x + r.width for r in rects)
        y1 = max(r.y + r.height for r in rects)
        lines.append((line.text, (x0, y0, x1, y1)))
    return lines


def ocr_lines(bgra, w, h):
    try:
        return asyncio.run(_ocr_async(bgra, w, h))
    except Exception:
        return []  # no OCR language pack or WinRT unavailable: UIA + window info still work


# ---------------------------------------------------------------------------
# Put it all together
# ---------------------------------------------------------------------------
def grab(x, y):
    """The instant part (~20 ms), done on the click itself BEFORE Jarvis shows anything,
    so the screenshot can never contain Jarvis's own card."""
    process, title = window_at(x, y)
    bgra, left, top, w, h = capture_around(x, y)
    return {"x": x, "y": y, "process": process, "title": title,
            "pixels": (bgra, left, top, w, h)}


def look_at(grabbed):
    """The slow part (UI Automation + OCR, ~0.5 s), run off the GUI thread."""
    x, y = grabbed["x"], grabbed["y"]
    uia = {}
    uia_thread = threading.Thread(target=_uia_at, args=(x, y, uia), daemon=True)
    uia_thread.start()

    bgra, left, top, w, h = grabbed["pixels"]
    px, py = x - left, y - top                         # pointer position inside the capture
    lines = ocr_lines(bgra, w, h)

    # rank OCR lines by distance from the pointer; a line the pointer is on counts as distance 0
    def distance(item):
        x0, y0, x1, y1 = item[1]
        dx = max(x0 - px, 0, px - x1)
        dy = max(y0 - py, 0, py - y1)
        return (dx * dx + dy * dy) ** 0.5
    ranked = sorted(lines, key=distance)
    under = [text for text, box in ranked if distance((text, box)) <= 12][:3]
    nearby = [text for text, _ in ranked if text not in under][:25]

    uia_thread.join(timeout=4)
    return {
        "x": x, "y": y, "process": grabbed["process"], "title": grabbed["title"], "uia": uia,
        "text_under_pointer": under, "text_nearby": nearby,
        "image": (bgra, w, h, px, py),
    }


def friendly_type(control_type_name):
    """'ListItemControl' -> 'list item', 'ButtonControl' -> 'button'."""
    import re
    words = re.findall(r"[A-Z][a-z]*", (control_type_name or "").replace("Control", ""))
    return " ".join(words).lower()


def describe_for_model(seen):
    """Turn look_at() output into plain text the language model can reason about."""
    uia = seen["uia"]
    parts = [f"Program: {seen['process']}", f"Window title: {seen['title'] or '(none)'}"]
    if uia.get("type") or uia.get("name"):
        parts.append(f"Thing under the pointer: a {friendly_type(uia.get('type')) or 'element'} "
                     f"labelled '{uia.get('name', '')}'")
    if uia.get("value"):
        parts.append(f"Its value/content: {uia['value']}")
    if uia.get("help"):
        parts.append(f"Its help text: {uia['help']}")
    if uia.get("inside"):
        parts.append("It sits inside: " + " > ".join(uia["inside"]))
    if seen["text_under_pointer"]:
        parts.append("On-screen text right at the pointer (OCR): " + " | ".join(seen["text_under_pointer"]))
    if seen["text_nearby"]:
        parts.append("Other visible text nearby (OCR, closest first):\n" + "\n".join(f"- {t}" for t in seen["text_nearby"]))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Quick-ask helpers
# ---------------------------------------------------------------------------
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.BringWindowToTop.argtypes = [wt.HWND]


class SelectionReader:
    """Reads the text selected in whatever app has focus, via UI Automation.

    Two stages, because the ask bar is about to take focus: `captured` is set as soon
    as the focused control has been found (a few ms), after which it's safe to show
    the bar; the text itself is read from that control afterwards (selection survives
    losing focus). Best effort: many apps expose it, some (games, some Electron) don't.
    """

    def __init__(self):
        self.captured = threading.Event()
        self.done = threading.Event()
        self.text = ""
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        try:
            import uiautomation as auto
            with auto.UIAutomationInitializerInThread():
                ctrl = auto.GetFocusedControl()
                self.captured.set()
                # GetPattern works on every control type; the typed helpers (GetTextPattern) only
                # exist on some wrapper classes, e.g. not on a WindowControl that is itself an editor
                pattern = ctrl.GetPattern(auto.PatternId.TextPattern) if ctrl else None
                if pattern:
                    self.text = "".join(r.GetText(4000) for r in pattern.GetSelection()).strip()[:4000]
        except Exception:
            pass  # no selection available in this app
        finally:
            self.captured.set()
            self.done.set()


def force_foreground(hwnd):
    """Bring our window to the front with keyboard focus. Windows normally refuses this to
    background apps; briefly attaching to the current foreground thread's input is the
    standard, user-initiated way around it (we only do it right after the user's hotkey)."""
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = kernel32.GetCurrentThreadId()
    attached = bool(fg_tid and fg_tid != me and user32.AttachThreadInput(fg_tid, me, True))
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    if attached:
        user32.AttachThreadInput(fg_tid, me, False)

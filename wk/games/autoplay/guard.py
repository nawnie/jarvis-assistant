"""Operator controls for autoplay - they protect Shawn's PC, they are not rules about games.

  kill()             the kill-switch hotkey (and the Stop button) end the session at once
  touched            ANY keyboard/mouse input that Jarvis didn't generate ends the session:
                     grab the mouse and you have control back, no hotkey needed
  focus              input is only sent while the target program is the foreground window;
                     if another window steals focus (a popup) the session pauses and lets go
                     of held keys, and gives up if the game stays unfocused too long.
                     (Shawn Alt-Tabbing away is his own input, so that simply ends the run.)

The touch detector is a pair of low-level keyboard + mouse hooks on their own thread. They
only exist while a session runs, do nothing but set a flag, and pass every event on untouched.
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time

from ... import sensors
from .inputs import JARVIS_TAG

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_QUIT = 0x0012
LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE


class _KBD(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSE(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


def is_ours(flags, extra, injected_bit):
    """Input Jarvis generated: injected AND carrying Jarvis's tag. Everything else is Shawn
    (his hands, or his controller software) and means 'hands off'."""
    return bool(flags & injected_bit) and extra == JARVIS_TAG


# ---------------------------------------------------------------------------
# The touch detector
# ---------------------------------------------------------------------------
class TouchWatch(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="jarvis-autoplay-touch")
        self.touched = threading.Event()
        self.what = ""
        self.ready = threading.Event()
        self.failed = ""
        self._thread_id = None
        self._kbd_proc = HOOKPROC(self._on_key)      # keep references: Windows calls these later
        self._mouse_proc = HOOKPROC(self._on_mouse)

    def _on_key(self, code, wparam, lparam):
        try:
            if code == 0:
                info = ctypes.cast(lparam, ctypes.POINTER(_KBD)).contents
                if not is_ours(info.flags, info.dwExtraInfo, LLKHF_INJECTED):
                    self.what = "keyboard"
                    self.touched.set()
        except Exception:
            pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _on_mouse(self, code, wparam, lparam):
        try:
            if code == 0:
                info = ctypes.cast(lparam, ctypes.POINTER(_MOUSE)).contents
                if not is_ours(info.flags, info.dwExtraInfo, LLMHF_INJECTED):
                    self.what = "mouse"
                    self.touched.set()
        except Exception:
            pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        module = kernel32.GetModuleHandleW(None)
        kbd = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kbd_proc, module, 0)
        mouse = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, module, 0)
        if not (kbd and mouse):
            self.failed = f"input hooks refused (Windows error {kernel32.GetLastError()})"
        self.ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        for hook in (kbd, mouse):
            if hook:
                user32.UnhookWindowsHookEx(hook)

    def stop(self):
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


# ---------------------------------------------------------------------------
# The guard the session consults between every slice of every action
# ---------------------------------------------------------------------------
class OperatorGuard:
    def __init__(self, target_process, stop_on_touch=True, max_unfocused_s=60.0, foreground=None):
        """target_process: e.g. 'skyrimse.exe'. foreground(): (process, title) - injectable for tests."""
        self.target = (target_process or "").lower()
        self.stop_on_touch = stop_on_touch
        self.max_unfocused_s = max_unfocused_s
        self.foreground = foreground or sensors.foreground_window
        self._killed = ""
        self._unfocused_since = None
        self.touch = None

    def start(self, grace_s=1.0):
        """Arm the touch detector. Input during the first grace_s seconds is ignored: that's
        Shawn letting go of the Alt+Tab or hotkey that brought the game to the front."""
        if self.stop_on_touch:
            self.touch = TouchWatch()
            self.touch.start()
            self.touch.ready.wait(2)
            if self.touch.failed:
                raise RuntimeError(f"Can't watch for your input, so autoplay won't start: {self.touch.failed}")
            time.sleep(grace_s)
            self.touch.touched.clear()

    def kill(self, reason="kill switch"):
        self._killed = reason

    def stop_reason(self):
        """Why the session must end now, or '' to carry on."""
        if self._killed:
            return self._killed
        if self.touch and self.touch.touched.is_set():
            return f"you used the {self.touch.what} - control handed back to you"
        if self._unfocused_since and time.monotonic() - self._unfocused_since > self.max_unfocused_s:
            return f"{self.target} was not in front for {self.max_unfocused_s:.0f} s"
        return ""

    def focused(self):
        """True while the target program has focus; tracks how long it has been away."""
        process, _ = self.foreground()
        if (process or "").lower() == self.target:
            self._unfocused_since = None
            return True
        if self._unfocused_since is None:
            self._unfocused_since = time.monotonic()
        return False

    def close(self):
        if self.touch:
            self.touch.stop()

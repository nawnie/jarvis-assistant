"""Global hotkeys for the newer features (snip & ask, game help, the autoplay kill switch).

Ctrl+Alt+J (quick ask) lives in pointer.MouseTrigger with the mouse hook. These extra keys get
their own thread so the features that own them can come and go without touching that hook.

Windows delivers a registered hotkey as a WM_HOTKEY message to the thread that registered it,
so every registration, and the message loop that receives them, stays on this one thread.
Callbacks run on this thread too: keep them instant (emit a Qt signal and return).
"""
import ctypes
import ctypes.wintypes as wt
import threading

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]

WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000

# ---------------------------------------------------------------------------
# this is the key-name section: "ctrl+alt+s" -> (modifier flags, virtual-key code)
# ---------------------------------------------------------------------------
_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_NAMED_KEYS = {"end": 0x23, "home": 0x24, "pause": 0x13, "insert": 0x2D, "delete": 0x2E, "space": 0x20,
               "pageup": 0x21, "pagedown": 0x22, "scrolllock": 0x91, "backspace": 0x08, "tab": 0x09}


def parse(combo):
    """'ctrl+alt+s' -> (MOD_CONTROL|MOD_ALT|MOD_NOREPEAT, ord('S')). Raises ValueError on nonsense."""
    parts = [p.strip().lower() for p in (combo or "").split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError(f"hotkey '{combo}' needs at least one modifier and a key")
    mods = MOD_NOREPEAT
    for part in parts[:-1]:
        if part not in _MODS:
            raise ValueError(f"unknown modifier '{part}' in '{combo}'")
        mods |= _MODS[part]
    key = parts[-1]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1                 # F1 = 0x70
    else:
        raise ValueError(f"unknown key '{key}' in '{combo}'")
    return mods, vk


def pretty(combo):
    """'ctrl+alt+s' -> 'Ctrl+Alt+S' for labels and menus."""
    return "+".join(p.strip().capitalize() if len(p.strip()) > 1 else p.strip().upper()
                    for p in (combo or "").split("+"))


# ---------------------------------------------------------------------------
# The hotkey thread itself
# ---------------------------------------------------------------------------
class HotkeyThread(threading.Thread):
    """Holds a set of global hotkeys while it runs. bindings: {combo_text: callback}.
    on_error(text) reports a key another app already owns (the rest still work)."""

    def __init__(self, bindings, on_error=None):
        super().__init__(daemon=True, name="jarvis-feature-hotkeys")
        self.bindings = dict(bindings)
        self.on_error = on_error
        self.held = {}                  # hotkey id -> combo text, for the ones Windows granted
        self._callbacks = {}
        self._thread_id = None
        self.ready = threading.Event()  # set once registration has been attempted (tests wait on it)

    def run(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        # this loop registers each binding; a key owned by another program is reported, not fatal
        for hotkey_id, (combo, callback) in enumerate(self.bindings.items(), start=100):
            try:
                mods, vk = parse(combo)
            except ValueError as exc:
                self._report(str(exc))
                continue
            if user32.RegisterHotKey(None, hotkey_id, mods, vk):
                self.held[hotkey_id] = combo
                self._callbacks[hotkey_id] = callback
            else:
                self._report(f"Hotkey {pretty(combo)} is already taken by another app")
        self.ready.set()

        # this is the message loop: each WM_HOTKEY names which registration fired
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in self._callbacks:
                try:
                    self._callbacks[msg.wParam]()
                except Exception as exc:        # a broken feature must not kill every hotkey
                    self._report(f"Hotkey {pretty(self.held.get(msg.wParam, '?'))} failed: {exc}")
        for hotkey_id in list(self.held):
            user32.UnregisterHotKey(None, hotkey_id)
        self.held.clear()

    def _report(self, text):
        if self.on_error:
            self.on_error(text)

    def stop(self):
        """Release every key (the thread unregisters them itself on the way out)."""
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

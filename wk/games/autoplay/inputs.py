"""Keyboard and mouse output for autoplay.

Games read the keyboard as hardware scan codes (DirectInput / raw input), so keys are sent
with KEYEVENTF_SCANCODE rather than virtual-key codes, which many games ignore. Every event
Jarvis sends carries JARVIS_TAG in dwExtraInfo, so guard.py can tell Jarvis's input apart
from Shawn's real hands.

Two drivers share one interface:
  WindowsDriver  really sends input (SendInput)
  DryRunDriver   only records what it WOULD send - the default, and what the tests use
Both remember what is held down, so release_all() can always let go of everything.
"""
import ctypes
import ctypes.wintypes as wt
import time

JARVIS_TAG = 0x4A415256          # "JARV": marks input that Jarvis itself generated

# ---------------------------------------------------------------------------
# this is the scan-code table (keyboard "set 1", what games see). Extended keys
# (arrows, right-hand Ctrl/Alt, navigation block) need the EXTENDEDKEY flag.
# ---------------------------------------------------------------------------
SCAN = {"esc": 0x01, "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06, "6": 0x07, "7": 0x08, "8": 0x09,
        "9": 0x0A, "0": 0x0B, "-": 0x0C, "=": 0x0D, "backspace": 0x0E, "tab": 0x0F,
        "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15, "u": 0x16, "i": 0x17, "o": 0x18,
        "p": 0x19, "[": 0x1A, "]": 0x1B, "enter": 0x1C, "ctrl": 0x1D,
        "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23, "j": 0x24, "k": 0x25, "l": 0x26,
        ";": 0x27, "'": 0x28, "`": 0x29, "shift": 0x2A, "\\": 0x2B,
        "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30, "n": 0x31, "m": 0x32, ",": 0x33, ".": 0x34,
        "/": 0x35, "rshift": 0x36, "alt": 0x38, "space": 0x39, "capslock": 0x3A,
        "f1": 0x3B, "f2": 0x3C, "f3": 0x3D, "f4": 0x3E, "f5": 0x3F, "f6": 0x40, "f7": 0x41, "f8": 0x42,
        "f9": 0x43, "f10": 0x44, "f11": 0x57, "f12": 0x58}
EXTENDED = {"up": 0x48, "down": 0x50, "left": 0x4B, "right": 0x4D, "home": 0x47, "end": 0x4F,
            "pageup": 0x49, "pagedown": 0x51, "insert": 0x52, "delete": 0x53, "rctrl": 0x1D, "ralt": 0x38}
ALIASES = {"escape": "esc", "return": "enter", "spacebar": "space", "tilde": "`", "console": "`",
           "control": "ctrl", "lshift": "shift", "lctrl": "ctrl", "lalt": "alt"}
# characters that need Shift held (US layout): what they are typed as
SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
           "_": "-", "+": "=", "{": "[", "}": "]", ":": ";", '"': "'", "~": "`", "|": "\\", "<": ",", ">": ".",
           "?": "/"}
BUTTONS = ("left", "right", "middle")


def normalise_key(key):
    """'W' -> 'w', 'Escape' -> 'esc'. Raises ValueError for keys the driver can't send."""
    k = str(key).strip().lower()
    k = ALIASES.get(k, k)
    if k not in SCAN and k not in EXTENDED:
        raise ValueError(f"unknown key '{key}'")
    return k


# ---------------------------------------------------------------------------
# The shared interface, with the bookkeeping both drivers need
# ---------------------------------------------------------------------------
class InputDriver:
    real = False

    def __init__(self):
        self.held_keys = set()
        self.held_buttons = set()

    # subclasses implement these four
    def _key(self, key, down):
        raise NotImplementedError

    def _button(self, button, down):
        raise NotImplementedError

    def _move(self, dx, dy):
        raise NotImplementedError

    def _wheel(self, notches):
        raise NotImplementedError

    # everything the session calls
    def key_down(self, key):
        key = normalise_key(key)
        self._key(key, True)
        self.held_keys.add(key)

    def key_up(self, key):
        key = normalise_key(key)
        self._key(key, False)
        self.held_keys.discard(key)

    def tap(self, key, hold_s=0.05):
        self.key_down(key)
        time.sleep(max(0.0, hold_s))
        self.key_up(key)

    def mouse_move(self, dx, dy):
        self._move(int(dx), int(dy))

    def button(self, button, down):
        if button not in BUTTONS:
            raise ValueError(f"unknown mouse button '{button}'")
        self._button(button, down)
        (self.held_buttons.add if down else self.held_buttons.discard)(button)

    def click(self, button="left", hold_s=0.04):
        self.button(button, True)
        time.sleep(hold_s)
        self.button(button, False)

    def wheel(self, notches):
        self._wheel(int(notches))

    def type_text(self, text, per_key_s=0.03):
        """Type plain text (US layout): letters, digits, space and common punctuation."""
        for ch in text:
            if ch == " ":
                self.tap("space", per_key_s)
            elif ch == "\n":
                self.tap("enter", per_key_s)
            elif ch.isupper() or ch in SHIFTED:
                self.key_down("shift")
                self.tap(SHIFTED.get(ch, ch.lower()), per_key_s)
                self.key_up("shift")
            else:
                self.tap(ch, per_key_s)
            time.sleep(per_key_s)

    def release_all(self):
        """Let go of every key and button still held. Called on EVERY way a session ends."""
        for key in list(self.held_keys):
            try:
                self.key_up(key)
            except Exception:
                self.held_keys.discard(key)
        for button in list(self.held_buttons):
            try:
                self.button(button, False)
            except Exception:
                self.held_buttons.discard(button)


class DryRunDriver(InputDriver):
    """Records what would be sent; touches nothing. log = [(seconds, action, detail)]."""

    def __init__(self):
        super().__init__()
        self.log = []
        self._t0 = time.monotonic()

    def _note(self, action, detail):
        self.log.append((round(time.monotonic() - self._t0, 3), action, detail))

    def _key(self, key, down):
        self._note("key_down" if down else "key_up", key)

    def _button(self, button, down):
        self._note("button_down" if down else "button_up", button)

    def _move(self, dx, dy):
        self._note("move", (dx, dy))

    def _wheel(self, notches):
        self._note("wheel", notches)


# ---------------------------------------------------------------------------
# The real driver: Win32 SendInput structures
# ---------------------------------------------------------------------------
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0001, 0x0002, 0x0008
MOUSEEVENTF_MOVE, MOUSEEVENTF_WHEEL = 0x0001, 0x0800
_BUTTON_FLAGS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}


class WindowsDriver(InputDriver):
    real = True

    def __init__(self):
        super().__init__()
        self._send = ctypes.windll.user32.SendInput
        self._send.argtypes = [wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        self._send.restype = wt.UINT

    def _dispatch(self, item):
        if self._send(1, ctypes.byref(item), ctypes.sizeof(_INPUT)) != 1:
            raise OSError("Windows refused the input (a higher-privilege window may have focus)")

    def _key(self, key, down):
        flags = KEYEVENTF_SCANCODE | (0 if down else KEYEVENTF_KEYUP)
        if key in EXTENDED:
            scan, flags = EXTENDED[key], flags | KEYEVENTF_EXTENDEDKEY
        else:
            scan = SCAN[key]
        item = _INPUT(type=INPUT_KEYBOARD)
        item.u.ki = _KEYBDINPUT(0, scan, flags, 0, JARVIS_TAG)
        self._dispatch(item)

    def _button(self, button, down):
        item = _INPUT(type=INPUT_MOUSE)
        item.u.mi = _MOUSEINPUT(0, 0, 0, _BUTTON_FLAGS[button][0 if down else 1], 0, JARVIS_TAG)
        self._dispatch(item)

    def _move(self, dx, dy):
        # relative movement: what games use for camera look (absolute moves would snap the cursor)
        item = _INPUT(type=INPUT_MOUSE)
        item.u.mi = _MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, JARVIS_TAG)
        self._dispatch(item)

    def _wheel(self, notches):
        item = _INPUT(type=INPUT_MOUSE)
        item.u.mi = _MOUSEINPUT(0, 0, ctypes.c_uint32(notches * 120).value, MOUSEEVENTF_WHEEL, 0, JARVIS_TAG)
        self._dispatch(item)

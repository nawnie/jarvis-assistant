"""Gamepad presence: playing with a controller counts as being at the PC.

Windows' own idle clock (GetLastInputInfo, used by sensors.idle_seconds) only sees keyboard and
mouse. So during a controller session Jarvis thought Shawn had walked away: break nudges measured
the wrong thing, "welcome back" popped up mid-game, and away mode could start. This module watches
XInput controllers (Xbox pads, and anything Steam Input presents as one) and reports how long ago a
controller was last actually used.

"Used" means a button or trigger pressed, or a stick pushed past the standard deadzone - so a pad
lying on the desk with a slightly drifting stick never counts as activity.

Cost: one background thread polling ~10 times a second; each read of a connected pad takes
microseconds. Empty slots are slow to query on some systems, so they're only re-checked every 3 s.

Not covered yet: controllers that bypass XInput (a PlayStation or Switch Pro pad WITHOUT Steam
running). Those would need Windows.Gaming.Input; add it here if Shawn uses one.
"""
import ctypes
import threading
import time

# this is the XInput structure section (XINPUT_GAMEPAD inside XINPUT_STATE)
class _Gamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class _State(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", _Gamepad)]


LEFT_DEADZONE, RIGHT_DEADZONE, TRIGGER_THRESHOLD = 7849, 8689, 30   # Microsoft's documented defaults
POLL_SECONDS, RESCAN_SECONDS = 0.1, 3.0


def _load_xinput():
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            dll = getattr(ctypes.windll, name)
            dll.XInputGetState.argtypes = [ctypes.c_uint, ctypes.POINTER(_State)]
            dll.XInputGetState.restype = ctypes.c_uint
            return dll
        except (AttributeError, OSError):
            continue
    return None


def in_use(pad):
    """True if this reading shows real input (not a resting pad or stick drift)."""
    return bool(pad.wButtons
                or pad.bLeftTrigger > TRIGGER_THRESHOLD or pad.bRightTrigger > TRIGGER_THRESHOLD
                or max(abs(pad.sThumbLX), abs(pad.sThumbLY)) > LEFT_DEADZONE
                or max(abs(pad.sThumbRX), abs(pad.sThumbRY)) > RIGHT_DEADZONE)


class GamepadWatch(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="jarvis-gamepad")
        self.xinput = _load_xinput()
        self.last_used = 0.0            # time.monotonic() of the last real controller input (0 = never)
        self.connected = set()          # slots 0-3 with a pad plugged in
        self._last_packet = {}

    def run(self):
        if self.xinput is None:
            return                      # no XInput on this system: keyboard/mouse idle only
        state = _State()
        next_scan = 0.0
        while True:
            now = time.monotonic()
            # this loop re-checks empty slots only every few seconds (they are slow to query)
            slots = range(4) if now >= next_scan else sorted(self.connected)
            if now >= next_scan:
                next_scan = now + RESCAN_SECONDS
            for i in slots:
                if self.xinput.XInputGetState(i, ctypes.byref(state)) != 0:
                    self.connected.discard(i)
                    continue
                self.connected.add(i)
                # a new packet number means the pad's state changed; only count it if it's real input
                if state.dwPacketNumber != self._last_packet.get(i) and in_use(state.Gamepad):
                    self.last_used = now
                self._last_packet[i] = state.dwPacketNumber
                if in_use(state.Gamepad):           # holding a stick or button also counts
                    self.last_used = now
            time.sleep(POLL_SECONDS)


_watch = None
_start_lock = threading.Lock()


def idle_seconds():
    """Seconds since a controller was last used; a very large number if never (or no XInput)."""
    global _watch
    with _start_lock:
        if _watch is None:
            _watch = GamepadWatch()
            _watch.start()
    if not _watch.last_used:
        return float("inf")
    return time.monotonic() - _watch.last_used


def connected_count():
    return len(_watch.connected) if _watch else 0

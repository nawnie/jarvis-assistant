"""Only one Jarvis Assistant may ever run - this is the lock that enforces it.

It's a named Windows mutex, owned by whichever process starts first:
  - a second launch sees the mutex already exists, asks the running Jarvis to show its
    window, and exits (so there is only ever one Jarvis and one window)
  - Windows releases the mutex automatically when the owning process ends - even if it
    crashes - so a stale lock can never stop Jarvis from starting again
  - the engine checks it too, so nothing (including test harnesses) can build a second Jarvis
    alongside a running one
"""
import ctypes
import ctypes.wintypes as wt

MUTEX_NAME = "Local\\JarvisAssistant.SingleInstance"   # "Local\\" = per Windows login session
SHOW_SERVER = "JarvisAssistant-single-instance"        # local socket the running Jarvis listens on
ERROR_ALREADY_EXISTS = 183

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
_kernel32.CreateMutexW.restype = wt.HANDLE
_kernel32.CloseHandle.argtypes = [wt.HANDLE]
_held = {}   # mutex name -> handle this process owns (kept open for the life of the process)


def claim(name=MUTEX_NAME):
    """True if this process is (now) the one running Jarvis; False if another process already is."""
    if name in _held:
        return True
    handle = _kernel32.CreateMutexW(None, False, name)
    if not handle:
        return False
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)   # someone else owns it
        return False
    _held[name] = handle
    return True


def release(name=MUTEX_NAME):
    """Only used by tests; the app keeps its lock until it exits."""
    handle = _held.pop(name, None)
    if handle:
        _kernel32.CloseHandle(handle)

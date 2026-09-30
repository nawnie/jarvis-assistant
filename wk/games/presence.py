"""Is Shawn in a game, or in something fullscreen, right now?

Used by the nudge policy (stay quiet mid-game), game help (which game is this about?) and
autoplay (only act while the game itself has focus). All checks are instant Win32 calls.
"""
import ctypes
import ctypes.wintypes as wt

from . import catalog

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
user32.GetForegroundWindow.restype = wt.HWND
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = wt.HMONITOR
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]

# Windows' own answer to "is the user in something fullscreen?" (the API Focus Assist uses)
QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE = 2, 3, 4
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def notification_state():
    """Windows' QUERY_USER_NOTIFICATION_STATE value (0 on failure)."""
    state = ctypes.c_int(0)
    try:
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            return state.value
    except (AttributeError, OSError):
        pass
    return 0


def foreground_covers_monitor():
    """True when the focused window fills its whole monitor (borderless-windowed games, fullscreen video).
    The desktop and taskbar also 'cover' the screen, so they are excluded."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in _SHELL_CLASSES:
        return False
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info)):   # nearest monitor
        return False
    mon = info.rcMonitor
    return (rect.left <= mon.left and rect.top <= mon.top
            and rect.right >= mon.right and rect.bottom >= mon.bottom)


def is_fullscreen():
    """Exclusive fullscreen (Direct3D), presentation mode, or a borderless window covering the monitor."""
    return (notification_state() in (QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE)
            or foreground_covers_monitor())


def game_name(process_name):
    """'Skyrim Special Edition' for 'skyrimse.exe', '' when the program isn't a known game."""
    game = catalog.detect(process_name)
    return game.name if game else ""

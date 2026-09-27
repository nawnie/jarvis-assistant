"""What autoplay sees: a screenshot of the focused game window, with OCR on demand.

Captures the window's CLIENT area (the picture, without the title bar), using the same GDI
capture as Ctrl+click explain. OCR is slow (~0.3 s) so it only runs when a step asks for
text (wait_for_text) or a vision policy wants it; the first call caches the result.
"""
import ctypes
import ctypes.wintypes as wt
import time
from dataclasses import dataclass, field

from ... import pointer, sensors

user32 = ctypes.windll.user32
user32.GetForegroundWindow.restype = wt.HWND
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]


@dataclass
class Observation:
    ts: float
    process: str
    title: str
    rect: tuple                      # (left, top, width, height) in physical screen pixels
    pixels: tuple                    # (bgra bytes, width, height)
    _lines: list | None = field(default=None, repr=False)

    def text_lines(self):
        """OCR lines of the frame (cached after the first call)."""
        if self._lines is None:
            bgra, w, h = self.pixels
            self._lines = [text for text, _box in pointer.ocr_lines(bgra, w, h)] if w and h else []
        return self._lines

    def sees(self, needles):
        """True if any needle appears in the on-screen text (case-insensitive)."""
        text = "\n".join(self.text_lines()).lower()
        return any(n.lower() in text for n in needles)


def client_rect():
    """(left, top, width, height) of the foreground window's client area, in screen pixels."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return origin.x, origin.y, width, height


def observe():
    """Screenshot of the focused window right now."""
    process, title = sensors.foreground_window()
    rect = client_rect()
    if not rect:
        return Observation(time.time(), process, title, (0, 0, 0, 0), (b"", 0, 0))
    left, top, width, height = rect
    bgra, cap_left, cap_top, w, h = pointer.capture_around(left + width // 2, top + height // 2, width, height)
    return Observation(time.time(), process, title, (cap_left, cap_top, w, h), (bgra, w, h))


def save_thumbnail(obs, path, width=480):
    """Keep a small copy of the frame for the session log (so a run can be replayed and reviewed)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    bgra, w, h = obs.pixels
    if not (w and h):
        return False
    image = QImage(bgra, w, h, w * 4, QImage.Format_ARGB32).scaledToWidth(width, Qt.SmoothTransformation)
    return image.save(str(path), "JPG", 70) or image.save(str(path.with_suffix(".png")), "PNG")

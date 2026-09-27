"""Snip & ask: drag a box around anything on screen, then ask Jarvis about it.

Flow:
  1. the whole desktop is captured FIRST (so Jarvis's own overlay is never in the picture)
  2. SnipOverlay shows that frozen frame, dimmed; Shawn drags a box (Esc cancels)
  3. the box is OCR'd with Windows' built-in engine (off the GUI thread)
  4. SnipAskBar asks "what about it?"; the info card shows the snip, the text read from it,
     and the model's answer (or just the text, if the model is off)

Private windows (password managers etc.) are refused the same way Ctrl+click refuses them:
the pixels are dropped before any OCR runs.
"""
import base64

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import hud, pointer, popup, sensors
from .brain import SYSTEM_PERSONA, run_async

user32 = pointer.user32


def capture_desktop():
    """(QImage of the whole virtual desktop in physical pixels, its left, its top)."""
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    bgra, left, top, w, h = pointer.capture_around(vx + vw // 2, vy + vh // 2, vw, vh)
    return QImage(bgra, w, h, w * 4, QImage.Format_ARGB32).copy(), left, top


def image_bgra(image):
    """(bytes, width, height) in BGRA order - what Windows OCR expects."""
    image = image.convertToFormat(QImage.Format_ARGB32)
    return bytes(image.constBits())[:image.sizeInBytes()], image.width(), image.height()


def image_data_uri(image, max_width=400):
    """A small PNG of the snip as a data: URI, so the card can show it inline."""
    if image.width() > max_width:
        image = image.scaledToWidth(max_width, Qt.SmoothTransformation)
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(bytes(data)).decode("ascii")


# ===========================================================================
# The region picker
# ===========================================================================
class SnipOverlay(QWidget):
    """Full-desktop overlay showing a frozen screenshot. Emits picked(crop QImage, physical QRect)."""
    picked = Signal(QImage, QRect)
    cancelled = Signal()

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.CrossCursor)
        self.frame, self.phys_left, self.phys_top = capture_desktop()
        self.setGeometry(QGuiApplication.primaryScreen().virtualGeometry())
        self._start = self._end = None

    # --- logical (widget) <-> physical (screenshot) coordinates ---------------------------
    def _scale(self):
        return self.frame.width() / max(1, self.width()), self.frame.height() / max(1, self.height())

    def _selection(self):
        if not (self._start and self._end):
            return QRect()
        return QRect(self._start, self._end).normalized()

    # --- painting: dimmed frame, the selection shown undimmed with a cyan edge and its size ---
    def paintEvent(self, _event):
        p = QPainter(self)
        p.drawImage(self.rect(), self.frame)
        p.fillRect(self.rect(), QColor(0, 0, 0, 115))
        sel = self._selection()
        if sel.isValid():
            sx, sy = self._scale()
            src = QRect(int(sel.x() * sx), int(sel.y() * sy), int(sel.width() * sx), int(sel.height() * sy))
            p.drawImage(sel, self.frame, src)
            p.setPen(QPen(hud.qcolor(hud.CYAN), 1.5))
            p.drawRect(sel.adjusted(0, 0, -1, -1))
            p.setFont(QFont(hud.UI_FONT, 9))
            p.drawText(sel.bottomLeft() + QPoint(4, 16), f"{src.width()} × {src.height()}")
        else:
            p.setPen(hud.qcolor(hud.CYAN_HI))
            p.setFont(QFont(hud.UI_FONT, 12))
            p.drawText(self.rect().adjusted(0, 60, 0, 0), Qt.AlignHCenter | Qt.AlignTop,
                       "DRAG A BOX AROUND WHAT YOU WANT TO ASK ABOUT  ·  ESC TO CANCEL")
        p.end()

    # --- mouse + keyboard ----------------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._start = self._end = event.position().toPoint()
            self.update()

    def mouseMoveEvent(self, event):
        if self._start:
            self._end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        sel = self._selection()
        self.hide()
        if sel.width() < 8 or sel.height() < 8:
            self.cancelled.emit()
        else:
            sx, sy = self._scale()
            src = QRect(int(sel.x() * sx), int(sel.y() * sy), int(sel.width() * sx), int(sel.height() * sy))
            self.picked.emit(self.frame.copy(src), src.translated(self.phys_left, self.phys_top))
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
            self.cancelled.emit()
            self.close()

    def start(self):
        self.show()
        self.raise_()
        self.activateWindow()
        pointer.force_foreground(int(self.winId()))


# ===========================================================================
# The question bar that follows a snip
# ===========================================================================
class SnipAskBar(popup.AskBar):
    """The quick-ask bar, pointed at a snip instead of the focused window."""

    def summon_for(self, crop, phys_rect, app, lines):
        self.crop, self.lines, self.app = crop, lines, app
        self.context = {"process": "", "title": "", "app": app, "private": False}
        self.ctx_label.setText(f"About your snip · {app} · {len(lines)} line(s) of text read")
        self.line.setPlaceholderText("What do you want to know about it?  ·  Enter = 'What is this?'  ·  Esc to close")
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.adjustSize()
        self.move(area.center().x() - self.width() // 2, area.top() + int(area.height() * 0.22))
        self.line.clear()
        self._was_active = False
        self.show()
        self.raise_()
        self.activateWindow()
        pointer.force_foreground(int(self.winId()))
        self.line.setFocus()

    def _ask(self):
        question = self.line.text().strip() or "What is this?"
        below = QPoint(self.x() + 90, self.y() + self.height() - 10)
        self.hide()
        text = "\n".join(self.lines)
        facts = f"![snip]({image_data_uri(self.crop)})"
        if text:
            facts += "\n\n**Text read from it**\n\n```\n" + text[:1500] + ("\n…" if len(text) > 1500 else "") + "\n```"
        request = self.card.open(question, f"Snip from {self.app}", facts, near=below)
        engine = self.engine
        engine.store.add_event("snip", f"Snip & ask ({self.app}): {question[:80]}")
        engine.data_changed.emit("events")
        prompt = (f"Shawn snipped part of his screen (in {self.app}) and asks: {question}\n\n"
                  f"Text read from the snip by OCR (may be partial or out of order):\n{text[:4000] or '(no text found)'}\n\n"
                  "Answer directly and briefly. If it's an error, give the likely cause and the fix. If the text "
                  "isn't enough to be sure, say what it most likely is and that it's a best guess.")
        run_async(lambda: engine.llm.chat([{"role": "system", "content": SYSTEM_PERSONA},
                                           {"role": "user", "content": prompt}], max_tokens=600),
                  lambda r: self.card.set_answer(request, popup._answer_or_error(r)))


def start(engine, card, bar_holder):
    """Begin a snip. bar_holder: a dict that keeps the overlay/bar alive (Qt would otherwise delete them)."""
    overlay = SnipOverlay()
    bar_holder["overlay"] = overlay

    def picked(crop, phys_rect):
        centre = phys_rect.center()
        process, title = pointer.window_at(centre.x(), centre.y())
        if engine.is_private(process, title):
            request = card.open("Private window", process, "That window is on your private list, so Jarvis didn't read it.")
            card.set_answer(request, " ")
            return
        app = sensors.app_description(process) or process or "the screen"
        bgra, w, h = image_bgra(crop)

        def ocr():
            return [line for line, _box in pointer.ocr_lines(bgra, w, h)]

        def ready(lines):
            if isinstance(lines, Exception):
                lines = []
            if "bar" not in bar_holder:
                bar_holder["bar"] = SnipAskBar(engine, card)
            bar_holder["bar"].summon_for(crop, phys_rect, app, lines)

        run_async(ocr, ready)

    overlay.picked.connect(picked)
    overlay.start()
    return overlay

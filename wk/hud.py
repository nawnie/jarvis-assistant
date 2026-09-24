"""The JARVIS-style heads-up-display look for Jarvis Assistant.

Everything visual that Qt stylesheets can't express lives here: cut-corner (chamfered)
panels, glowing rings, the arc reactor, radial gauges, the sidebar painter, the page
scan sweep, the start-up sequence and the app icon. ui.py and popup.py only PLACE these
widgets; they contain no drawing code of their own.

Animation rule: Jarvis runs all day in the tray, so every animated widget runs its frame
timer ONLY while it is actually on screen (started in showEvent, stopped in hideEvent).
A hidden window, a minimised window or a page you aren't looking at costs no CPU.

Test-safety rule: nothing here changes a widget's text() or object tree in a way the QA
suite depends on. Uppercase lettering is done with QFont capitalization (the text stays
"Pause 30 min", it just renders as PAUSE 30 MIN), and overlays ignore the mouse.
"""
import ctypes
import math
import random
import re
import time
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEvent, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
                           QRadialGradient, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextFrameFormat)
from PySide6.QtWidgets import QFrame, QListView, QStyle, QStyledItemDelegate, QWidget

# ===========================================================================
# Palette: deep-space navy, one hologram cyan, amber for warnings, red for critical.
# Text colours are chosen to stay above WCAG 4.5:1 on the panel fill (the widget
# audit in tests/qa_widgets.py measures this on every label).
# ===========================================================================
BG = "#060b12"            # the window itself
BG_RAISED = "#0a1520"     # opaque surfaces: menus, dialogs, dropdown lists
CYAN = "#38d6ff"          # the hologram accent
CYAN_HI = "#b4f4ff"       # bright highlight text on cyan-tinted surfaces
TEXT = "#d8f4ff"          # normal text, a cool white
MUTED = "#7fa6b8"         # secondary text (7:1 on the panel fill)
AMBER = "#ffb02e"         # warnings: RAM high, model offline, break due
RED = "#ff5468"           # critical: GPU too hot, RAM nearly full
IDLE = "#6d8190"          # paused / switched off

# this is the font naming section: Bahnschrift (a DIN-style face that ships with Windows 10+)
# gives the engineered HUD lettering; Segoe UI stays for long reading text; Cascadia for readouts
UI_FONT = "Bahnschrift"
UI_FONT_SEMIBOLD = "Bahnschrift SemiBold"
UI_FONT_LIGHT = "Bahnschrift SemiLight"
BUTTON_FONT = "Bahnschrift SemiBold SemiCondensed"   # narrower, so uppercase buttons still fit at 900 px
TEXT_FONT = "Segoe UI"
MONO_FONTS = ["Cascadia Mono", "Consolas"]


def qcolor(colour, alpha=255):
    """QColor from '#rrggbb' (or another QColor) with an alpha of 0-255."""
    c = QColor(colour)
    c.setAlpha(int(max(0, min(255, alpha))))
    return c


def css_rgba(colour, alpha):
    """'rgba(r, g, b, a)' for stylesheets, alpha given as 0.0-1.0."""
    c = QColor(colour)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha:.3f})"


def mix(a, b, t):
    """Blend two colours: t=0 gives a, t=1 gives b (used to fade the reactor between states)."""
    a, b = QColor(a), QColor(b)
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t), int(a.alpha() + (b.alpha() - a.alpha()) * t))


# ===========================================================================
# Fonts
# ===========================================================================
def font(size, family=UI_FONT, weight=QFont.Normal, spacing=0.0, caps=False, families=None):
    """A QFont for painted text. spacing = extra pixels between letters (the HUD 'tracking')."""
    f = QFont()
    f.setFamilies(families or [family, TEXT_FONT])
    f.setPointSizeF(size)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    if caps:
        f.setCapitalization(QFont.AllUppercase)
    return f


def caps(widget, spacing=1.2):
    """Render a widget's text in letter-spaced capitals WITHOUT changing widget.text().

    The stylesheet still decides the family and size; QSS has no letter-spacing or
    text-transform, and Qt keeps these two font properties when it applies the sheet."""
    f = widget.font()
    f.setCapitalization(QFont.AllUppercase)
    f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    widget.setFont(f)


def tracked(widget, spacing):
    """Letter spacing only (for labels whose text is already uppercase)."""
    f = widget.font()
    f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    widget.setFont(f)


# ===========================================================================
# Geometry: the cut-corner outline every HUD surface shares.
# Top-left and bottom-right corners are cut at 45 degrees; the other two stay square.
# That asymmetry is what reads as "engineered display" rather than "rounded app".
# ===========================================================================
def chamfer_path(r: QRectF, cut: float) -> QPainterPath:
    path = QPainterPath(QPointF(r.left() + cut, r.top()))
    path.lineTo(r.right(), r.top())
    path.lineTo(r.right(), r.bottom() - cut)
    path.lineTo(r.right() - cut, r.bottom())
    path.lineTo(r.left(), r.bottom())
    path.lineTo(r.left(), r.top() + cut)
    path.closeSubpath()
    return path


def glow_stroke(p: QPainter, path: QPainterPath, colour, width=1.0, strength=1.0):
    """Stroke a path with a soft bloom: two wide faint passes under one crisp line."""
    p.setBrush(Qt.NoBrush)
    for w, a in ((width * 7, 18), (width * 3.5, 38)):
        p.setPen(QPen(qcolor(colour, a * strength), w, Qt.SolidLine, Qt.FlatCap, Qt.MiterJoin))
        p.drawPath(path)
    p.setPen(QPen(qcolor(colour, 235), width, Qt.SolidLine, Qt.FlatCap, Qt.MiterJoin))
    p.drawPath(path)


def arc(p: QPainter, centre: QPointF, radius, start_deg, span_deg, colour, width, glow=0.0):
    """One arc of a ring. Angles in degrees, 0 = 3 o'clock, positive = anticlockwise (Qt's convention)."""
    rect = QRectF(centre.x() - radius, centre.y() - radius, radius * 2, radius * 2)
    if glow:
        halo = qcolor(colour, 60 * glow)
        p.setPen(QPen(halo, width * 3.2, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(rect, int(start_deg * 16), int(span_deg * 16))
    p.setPen(QPen(QColor(colour), width, Qt.SolidLine, Qt.FlatCap))
    p.drawArc(rect, int(start_deg * 16), int(span_deg * 16))


# ===========================================================================
# Animated base: a frame timer that runs only while the widget is on screen
# ===========================================================================
class Animated(QWidget):
    FPS = 30

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(1000 // self.FPS)
        self._frame_timer.timeout.connect(self._frame)
        self._last = time.monotonic()

    def showEvent(self, event):
        self._last = time.monotonic()
        self._frame_timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._frame_timer.stop()
        super().hideEvent(event)

    def _frame(self):
        # belt and braces: a minimised window gets no repaints even if Qt kept us "visible"
        if self.window().isMinimized():
            return
        now = time.monotonic()
        dt, self._last = min(now - self._last, 0.1), now
        self.advance(dt)
        self.update()

    def advance(self, dt):
        """Move the animation on by dt seconds. Subclasses override this."""


# ===========================================================================
# The arc reactor: Jarvis's "I'm alive" indicator.
# Rings rotate at a speed set by the current state, the core breathes, and an
# optional progress ring shows a value (on the Now page: your active streak
# filling up toward the break nudge).
# ===========================================================================
def reactor_style(mode):
    """How the arc reactor looks in each state: colour, ring spin speed, core pulse, glow.

    This is Shawn's call (a UX decision, not a technical one). The reactor sits in the
    corner of his eye all day, so the trade-off is: calm enough to ignore while he works,
    distinct enough that "thinking" and "offline" are noticed at a glance.
      spin  - ring rotation, turns per ~10 s (0 = frozen)
      pulse - how strongly the core breathes, 0-1
      glow  - bloom strength around the rings, 0-1
    """
    return {
        "online":   {"colour": CYAN,    "spin": 1.0, "pulse": 0.25, "glow": 0.75},
        "thinking": {"colour": CYAN_HI, "spin": 3.4, "pulse": 0.95, "glow": 1.0},
        "offline":  {"colour": AMBER,   "spin": 0.3, "pulse": 0.55, "glow": 0.6},
        "paused":   {"colour": IDLE,    "spin": 0.0, "pulse": 0.12, "glow": 0.25},
    }.get(mode, {"colour": CYAN, "spin": 1.0, "pulse": 0.25, "glow": 0.75})


class ArcReactor(Animated):
    def __init__(self, diameter, show_text=True, busy_fn=None, parent=None):
        super().__init__(parent)
        self.setFixedSize(diameter, diameter)
        self.show_text = show_text
        self.busy_fn = busy_fn          # returns True while the local model is answering something
        self.mode = "online"
        self.value = None               # 0-1 fill of the progress ring, or None for no ring
        self.readout, self.caption = "", ""
        # live (smoothed) animation state, eased toward the current mode's style every frame
        self._angle = random.uniform(0, 360)
        self._phase = 0.0
        s = reactor_style(self.mode)
        self._spin, self._colour, self._pulse, self._glow = s["spin"], QColor(s["colour"]), s["pulse"], s["glow"]

    # this is the state section: what the reactor should show right now
    def set_mode(self, mode):
        if mode != self.mode:
            self.mode = mode
            self.update()

    def set_readout(self, readout, caption="", value=None):
        if (readout, caption, value) != (self.readout, self.caption, self.value):
            self.readout, self.caption, self.value = readout, caption, value
            self.update()

    def live_mode(self):
        """'thinking' overrides 'online' the moment the model starts working (checked every frame)."""
        if self.mode == "online" and self.busy_fn is not None:
            try:
                if self.busy_fn():
                    return "thinking"
            except Exception:
                pass  # the engine is shutting down: just keep the plain state
        return self.mode

    def advance(self, dt):
        # ease speed, colour, pulse and glow toward the target, so a state change is a smooth ramp
        s = reactor_style(self.live_mode())
        k = min(1.0, dt * 3.0)
        self._spin += (s["spin"] - self._spin) * k
        self._pulse += (s["pulse"] - self._pulse) * k
        self._glow += (s["glow"] - self._glow) * k
        self._colour = mix(self._colour, QColor(s["colour"]), k)
        self._angle = (self._angle + self._spin * 36 * dt) % 360
        self._phase += dt

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        centre = QPointF(self.width() / 2, self.height() / 2)
        radius = min(self.width(), self.height()) / 2 - 2
        draw_reactor(p, centre, radius, self._angle, self._colour, self._pulse, self._phase, self._glow,
                     value=self.value, dim_core=self.show_text)
        if self.show_text and self.readout:
            # this is the readout section: the big number in the core, a small caption under it
            p.setPen(QColor(TEXT))
            f = font(max(9.0, radius * 0.19), UI_FONT_SEMIBOLD)
            p.setFont(f)
            box = QRectF(centre.x() - radius, centre.y() - radius * 0.30, radius * 2, radius * 0.40)
            p.drawText(box, Qt.AlignHCenter | Qt.AlignVCenter, self.readout)
            if self.caption:
                p.setPen(QColor(MUTED))
                p.setFont(font(max(6.0, radius * 0.075), UI_FONT, spacing=1.4, caps=True))
                p.drawText(QRectF(centre.x() - radius, centre.y() + radius * 0.10, radius * 2, radius * 0.22),
                           Qt.AlignHCenter | Qt.AlignTop, self.caption)
        p.end()


def draw_reactor(p, centre, radius, angle, colour, pulse, phase, glow, value=None, dim_core=False, build=1.0):
    """Paint the reactor rings. build (0-1) grows the rings in one by one (start-up sequence).
    dim_core: a readout will be printed in the middle, so the rings move outward to leave it a
    clear window and the core glow is turned down behind the text."""
    c = QColor(colour)
    # this is the ring-radius section (fractions of the radius): closed = emblem, open = with a readout
    progress_r, plates_r, inner_r, core_r = (0.84, 0.72, 0.625, 0.55) if dim_core else (0.78, 0.64, 0.52, 0.36)

    def shown(start):
        # each ring appears during its own slice of the build-up
        return max(0.0, min(1.0, (build - start) / 0.35))

    # this is the static outer bezel: a faint circle plus 72 ticks, every sixth one longer
    b = shown(0.0)
    if b:
        p.setPen(QPen(qcolor(c, 70 * b), 1))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(centre, radius * 0.98, radius * 0.98)
        for i in range(72):
            a = math.radians(i * 5)
            inner = radius * (0.86 if i % 6 == 0 else 0.905)
            p.setPen(QPen(qcolor(c, (150 if i % 6 == 0 else 70) * b), 1.2 if i % 6 == 0 else 1))
            p.drawLine(QPointF(centre.x() + math.cos(a) * inner, centre.y() + math.sin(a) * inner),
                       QPointF(centre.x() + math.cos(a) * radius * 0.94, centre.y() + math.sin(a) * radius * 0.94))

    # this is the progress ring: a faint track with the value filled clockwise from 12 o'clock
    if value is not None and shown(0.15):
        track_w = max(2.0, radius * 0.05)
        arc(p, centre, radius * progress_r, 0, 360, qcolor(c, 34), track_w)
        v = max(0.0, min(1.0, value))
        if v > 0:
            fill = QColor(AMBER) if v >= 0.85 else c
            arc(p, centre, radius * progress_r, 90, -360 * v, fill, track_w, glow=glow)

    # this is the rotating segmented ring: ten plates with gaps, turning clockwise
    b = shown(0.3)
    if b:
        w = max(2.0, radius * (0.07 if dim_core else 0.085))
        for i in range(10):
            arc(p, centre, radius * plates_r, -angle + i * 36, 25 * b, qcolor(c, 200), w, glow=glow * 0.8)

    # this is the counter-rotating inner ring: three long bright arcs turning the other way
    b = shown(0.5)
    if b:
        w = max(1.5, radius * 0.035)
        for i in range(3):
            arc(p, centre, radius * inner_r, angle * 1.7 + i * 120, 72 * b, c, w, glow=glow)

    # this is the core: a breathing radial glow inside a thin solid ring
    b = shown(0.65)
    if b:
        breath = 0.5 + 0.5 * math.sin(phase * (2.2 + pulse * 4.0))
        strength = (0.55 + pulse * 0.45 * breath) * b * (0.30 if dim_core else 1.0)
        glow_r = radius * (core_r + 0.04)
        g = QRadialGradient(centre, glow_r)
        g.setColorAt(0.0, qcolor(mix(c, QColor("#ffffff"), 0.65), 235 * strength))
        g.setColorAt(0.35, qcolor(c, 120 * strength))
        g.setColorAt(1.0, qcolor(c, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(centre, glow_r, glow_r)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(qcolor(c, 170 * b), max(1.0, radius * 0.02)))
        p.drawEllipse(centre, radius * core_r, radius * core_r)


# ===========================================================================
# Radial gauge: CPU / RAM / GPU / temperature as a segmented 240-degree dial
# ===========================================================================
class RadialGauge(QWidget):
    SEGMENTS = 36
    SWEEP = 240          # degrees of dial, opening at the bottom

    def __init__(self, unit="%", maximum=100.0, parent=None):
        super().__init__(parent)
        self.unit, self.maximum = unit, maximum
        self.warn, self.crit = maximum * 0.85, maximum * 0.95
        self.target = None
        self.shown = 0.0
        self.setMinimumSize(96, 78)
        # value changes glide over 450 ms instead of jumping (the stats arrive every 10 s)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(450)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    def set_limits(self, warn, crit):
        self.warn, self.crit = warn, crit

    def set_value(self, value):
        if value is None:
            self.target = None
            self.update()
            return
        value = float(value)
        if self.target is not None and abs(value - self.target) < 0.05:
            return
        self.target = value
        self._anim.stop()
        self._anim.setStartValue(float(self.shown))
        self._anim.setEndValue(value)
        self._anim.start()

    def _on_anim(self, v):
        self.shown = float(v)
        self.update()

    def colour_for(self, v):
        if v >= self.crit:
            return QColor(RED)
        if v >= self.warn:
            return QColor(AMBER)
        return QColor(CYAN)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # the dial is a circle whose bottom opening fits the widget's height
        radius = min(self.width() / 2 - 4, (self.height() - 6) / 1.62)
        centre = QPointF(self.width() / 2, 4 + radius)
        live = self.target is not None
        v = self.shown if live else 0.0
        frac = max(0.0, min(1.0, v / self.maximum))
        colour = self.colour_for(v) if live else QColor(IDLE)
        lit = round(frac * self.SEGMENTS)
        seg = self.SWEEP / self.SEGMENTS
        start = 90 + self.SWEEP / 2        # left end of the dial, sweeping clockwise
        w = max(3.0, radius * 0.16)
        # this loop paints the dial's segments: lit ones in the state colour, the rest as a faint track
        for i in range(self.SEGMENTS):
            a0 = start - i * seg
            if i < lit:
                arc(p, centre, radius - w / 2, a0, -(seg - 1.6), colour, w, glow=0.35)
            else:
                arc(p, centre, radius - w / 2, a0, -(seg - 1.6), qcolor(CYAN, 26), w)
        # a thin inner guide ring
        arc(p, centre, radius - w - 4, start, -self.SWEEP, qcolor(CYAN, 55), 1)
        # this is the readout: the number in the middle, the unit small beside it
        text = "n/a" if not live else (f"{v:.0f}" if self.unit != "°C" else f"{v:.0f}°")
        p.setPen(QColor(TEXT if live else MUTED))
        p.setFont(font(max(10.0, radius * 0.30), UI_FONT_SEMIBOLD))
        p.drawText(QRectF(centre.x() - radius, centre.y() - radius * 0.34, radius * 2, radius * 0.58),
                   Qt.AlignCenter, text)
        if live and self.unit == "%":
            p.setPen(QColor(MUTED))
            p.setFont(font(max(6.5, radius * 0.12), UI_FONT, spacing=1.0))
            p.drawText(QRectF(centre.x() - radius, centre.y() + radius * 0.22, radius * 2, radius * 0.3),
                       Qt.AlignHCenter | Qt.AlignTop, "PERCENT")
        p.end()


# ===========================================================================
# Panels: the translucent cut-corner cards every page is built from
# ===========================================================================
PANEL_FILL = QColor(9, 20, 31, 225)
PANEL_EDGE = QColor(56, 214, 255, 62)


class HudPanel(QFrame):
    CUT = 11

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.title_label = None       # set by ui.card(); the panel draws a marker + rule beside it

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = chamfer_path(r, self.CUT)
        p.fillPath(path, PANEL_FILL)
        p.setPen(QPen(PANEL_EDGE, 1))
        p.drawPath(path)
        cyan = QColor(CYAN)
        # this is the accent section: the two cut corners glow, the square corners get L-brackets
        p.setPen(QPen(qcolor(cyan, 210), 1.4))
        c = self.CUT
        p.drawLine(QPointF(r.left(), r.top() + c), QPointF(r.left() + c, r.top()))
        p.drawLine(QPointF(r.right() - c, r.bottom()), QPointF(r.right(), r.bottom() - c))
        arm = 12
        p.drawLine(QPointF(r.right() - arm, r.top()), QPointF(r.right(), r.top()))
        p.drawLine(QPointF(r.right(), r.top()), QPointF(r.right(), r.top() + arm))
        p.drawLine(QPointF(r.left(), r.bottom() - arm), QPointF(r.left(), r.bottom()))
        p.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.left() + arm, r.bottom()))
        # this is the title decoration: a small cyan tab left of the title, a faint rule after it
        t = self.title_label
        if t is not None and t.isVisible():
            g = t.geometry()
            cy = g.center().y() + 0.5
            p.setPen(Qt.NoPen)
            p.setBrush(qcolor(cyan, 230))
            p.drawRect(QRectF(g.left() - 8, cy - 4, 3, 8))
            used = QFontMetricsF(t.font()).horizontalAdvance(t.text())
            x0 = g.left() + used + 10
            if x0 < r.right() - 24:
                grad = QLinearGradient(x0, 0, r.right() - 14, 0)
                grad.setColorAt(0, qcolor(cyan, 70))
                grad.setColorAt(1, qcolor(cyan, 0))
                p.setPen(QPen(grad, 1))
                p.drawLine(QPointF(x0, cy), QPointF(r.right() - 14, cy))
        p.end()


class HoloFrame(QFrame):
    """The floating card / quick-ask body: navy glass with a glowing cyan edge and a corner tag."""
    CUT = 14

    def __init__(self, tag="", parent=None):
        super().__init__(parent)
        self.tag = tag

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(6.5, 6.5, -6.5, -6.5)   # 6 px left free for the glow
        path = chamfer_path(r, self.CUT)
        p.fillPath(path, QColor(7, 17, 27, 244))
        # a faint cyan sheen across the top, like light catching glass
        sheen = QLinearGradient(0, r.top(), 0, r.top() + 60)
        sheen.setColorAt(0, qcolor(CYAN, 26))
        sheen.setColorAt(1, qcolor(CYAN, 0))
        p.fillPath(path, sheen)
        glow_stroke(p, path, CYAN, 1.0, strength=0.9)
        # corner brackets on the two square corners
        p.setPen(QPen(qcolor(CYAN_HI, 240), 2))
        arm = 16
        p.drawPolyline([QPointF(r.right() - arm, r.top() + 3), QPointF(r.right() - 3, r.top() + 3),
                        QPointF(r.right() - 3, r.top() + arm)])
        p.drawPolyline([QPointF(r.left() + 3, r.bottom() - arm), QPointF(r.left() + 3, r.bottom() - 3),
                        QPointF(r.left() + arm, r.bottom() - 3)])
        if self.tag:
            # the small identifier stamped along the bottom edge
            p.setFont(font(6.5, families=MONO_FONTS, spacing=1.5))
            p.setPen(qcolor(CYAN, 150))
            p.drawText(QRectF(r.left(), r.bottom() - 15, r.width() - 22, 12), Qt.AlignRight | Qt.AlignVCenter,
                       self.tag)
        p.end()


class HudRule(QWidget):
    """The line under each page title: a bright lead-in, a faint run, and ruler ticks at the end."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(10)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        y = self.height() / 2 + 0.5
        w = self.width()
        p.setPen(QPen(qcolor(CYAN, 230), 2))
        p.drawLine(QPointF(0, y), QPointF(min(64, w), y))
        grad = QLinearGradient(64, 0, w, 0)
        grad.setColorAt(0, qcolor(CYAN, 90))
        grad.setColorAt(1, qcolor(CYAN, 20))
        p.setPen(QPen(grad, 1))
        p.drawLine(QPointF(70, y), QPointF(w - 70, y))
        # this loop draws the ruler ticks at the right end
        p.setPen(QPen(qcolor(CYAN, 110), 1))
        for i in range(12):
            x = w - 64 + i * 5.5
            h = 4 if i % 4 == 0 else 2
            p.drawLine(QPointF(x, y - h), QPointF(x, y + h))
        p.end()


# ===========================================================================
# Window backdrop and sidebar
# ===========================================================================
class Backdrop(QWidget):
    """The window background: deep navy, a faint cyan light from the top, and a fine grid."""
    GRID = 24

    def paintEvent(self, event):
        p = QPainter(self)
        area = event.rect()
        p.fillRect(area, QColor(BG))
        # a soft pool of cyan light near the top centre of the page area
        glow = QRadialGradient(QPointF(self.width() * 0.62, -self.height() * 0.15), self.height() * 0.95)
        glow.setColorAt(0, qcolor(CYAN, 20))
        glow.setColorAt(1, qcolor(CYAN, 0))
        p.fillRect(area, glow)
        # this is the grid section: fine lines every 24 px, a slightly stronger line every 5th
        g = self.GRID
        x0 = area.left() - area.left() % g
        y0 = area.top() - area.top() % g
        minor, major = qcolor(CYAN, 9), qcolor(CYAN, 16)
        for x in range(x0, area.right() + 1, g):
            p.setPen(major if (x // g) % 5 == 0 else minor)
            p.drawLine(x, area.top(), x, area.bottom())
        for y in range(y0, area.bottom() + 1, g):
            p.setPen(major if (y // g) % 5 == 0 else minor)
            p.drawLine(area.left(), y, area.right(), y)
        p.end()


class Sidebar(QWidget):
    """The left column: slightly raised glass, with a glowing edge line on its right."""

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(event.rect(), QColor(7, 15, 24, 235))
        x = self.width() - 1
        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0.0, qcolor(CYAN, 20))
        grad.setColorAt(0.25, qcolor(CYAN, 110))
        grad.setColorAt(0.75, qcolor(CYAN, 40))
        grad.setColorAt(1.0, qcolor(CYAN, 10))
        p.setPen(QPen(grad, 1))
        p.drawLine(x, 0, x, self.height())
        p.end()


class ClockReadout(QWidget):
    """Sidebar footer: the time (with seconds) and date, like a HUD chronometer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(46)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.update)

    def showEvent(self, event):
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor(TEXT))
        p.setFont(font(15, families=MONO_FONTS, spacing=1.0))
        p.drawText(QRectF(20, 0, self.width() - 20, 24), Qt.AlignLeft | Qt.AlignVCenter, time.strftime("%H:%M:%S"))
        p.setPen(QColor(MUTED))
        p.setFont(font(7.5, UI_FONT, spacing=1.6))
        p.drawText(QRectF(20, 24, self.width() - 20, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   time.strftime("%a %d %b %Y").upper())
        p.end()


class NavDelegate(QStyledItemDelegate):
    """Paints each sidebar entry as '01  NOW', with a lit cut-corner plate on the selected page.
    The item's real text is untouched, so show_page("Timeline") and UI Automation still see 'Timeline'."""
    ROW = 36

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.ROW)

    def paint(self, p, option, index):
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(option.rect).adjusted(10, 3, -12, -3)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        # this is the plate behind the entry: lit when selected, a faint wash on hover
        if selected:
            plate = chamfer_path(r, 7)
            p.fillPath(plate, qcolor(CYAN, 30))
            p.setPen(QPen(qcolor(CYAN, 120), 1))
            p.drawPath(plate)
            bar = QRectF(r.left() - 4, r.top() + 4, 3, r.height() - 8)
            p.fillRect(bar.adjusted(-2, -2, 2, 2), qcolor(CYAN, 50))
            p.fillRect(bar, QColor(CYAN))
        elif hover:
            p.fillPath(chamfer_path(r, 7), qcolor(CYAN, 14))
        # this is the label section: a two-digit index in the readout font, then the page name
        p.setFont(font(8, families=MONO_FONTS))
        p.setPen(QColor(CYAN) if selected else qcolor(MUTED, 200))
        p.drawText(QRectF(r.left() + 10, r.top(), 24, r.height()), Qt.AlignLeft | Qt.AlignVCenter,
                   f"{index.row() + 1:02d}")
        p.setFont(font(9.5, UI_FONT_SEMIBOLD if selected else UI_FONT, spacing=1.6, caps=True))
        p.setPen(QColor(CYAN_HI) if selected else QColor(TEXT))
        p.drawText(QRectF(r.left() + 38, r.top(), r.width() - 50, r.height()), Qt.AlignLeft | Qt.AlignVCenter,
                   index.data())
        if selected:
            # a small arrow head at the right edge of the lit plate
            cx, cy = r.right() - 10, r.center().y()
            tri = QPainterPath(QPointF(cx - 3, cy - 4))
            tri.lineTo(cx + 2, cy)
            tri.lineTo(cx - 3, cy + 4)
            tri.closeSubpath()
            p.fillPath(tri, QColor(CYAN))
        p.restore()


# ===========================================================================
# Log and table painters
# ===========================================================================
LOG_LINE = re.compile(r"^(\d\d:\d\d)\s+\[([^\]]+)\]\s+(.*)$", re.S)
KIND_COLOURS = {"error": RED, "alert": AMBER, "warning": AMBER, "nudge": AMBER, "model": CYAN_HI}


class LogDelegate(QStyledItemDelegate):
    """'What it noticed' as a system log: cyan timestamp, a small tag chip, then the message (wrapped)."""
    PAD = 6

    def _parts(self, index):
        m = LOG_LINE.match(index.data() or "")
        return m.groups() if m else ("", "", index.data() or "")

    def _text_rect_width(self, option):
        view = option.widget
        width = view.viewport().width() if view is not None else option.rect.width()
        return max(60, width - 120)

    def sizeHint(self, option, index):
        _, _, text = self._parts(index)
        fm = QFontMetricsF(option.font)
        rect = fm.boundingRect(QRectF(0, 0, self._text_rect_width(option), 10000), Qt.TextWordWrap, text)
        return QSize(option.rect.width(), int(max(fm.height(), rect.height()) + self.PAD * 2))

    def paint(self, p, option, index):
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(option.rect)
        if option.state & QStyle.State_Selected:
            p.fillRect(r, qcolor(CYAN, 36))
        elif option.state & QStyle.State_MouseOver:
            p.fillRect(r, qcolor(CYAN, 14))
        when, kind, text = self._parts(index)
        top = r.top() + self.PAD
        line_h = QFontMetricsF(option.font).height()
        # timestamp in the readout font
        p.setFont(font(8.5, families=MONO_FONTS))
        p.setPen(QColor(CYAN))
        p.drawText(QRectF(r.left() + 6, top, 44, line_h), Qt.AlignLeft | Qt.AlignVCenter, when)
        # this is the tag chip: the event kind, boxed, coloured by how much it matters
        if kind:
            colour = QColor(KIND_COLOURS.get(kind.lower(), MUTED))
            chip = QRectF(r.left() + 50, top + 1, 58, line_h - 2)
            p.setPen(QPen(qcolor(colour, 150), 1))
            p.setBrush(qcolor(colour, 22))
            p.drawRect(chip)
            p.setFont(font(6.8, UI_FONT_SEMIBOLD, spacing=1.0, caps=True))
            p.setPen(colour)
            p.drawText(chip, Qt.AlignCenter, kind[:9])
        # the message itself, word-wrapped
        p.setFont(option.font)
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(r.left() + 116, top, self._text_rect_width(option), r.height() - self.PAD * 2),
                   Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, text)
        # a faint separator line between entries
        p.setPen(QPen(qcolor(CYAN, 16), 1))
        p.drawLine(QPointF(r.left() + 4, r.bottom()), QPointF(r.right() - 4, r.bottom()))
        p.restore()


def make_log_view(view):
    """Turn a QListWidget into the HUD system log (wrapping rows that re-flow on resize)."""
    view.setItemDelegate(LogDelegate(view))
    view.setResizeMode(QListView.Adjust)
    view.setMouseTracking(True)
    view.viewport().setAttribute(Qt.WA_Hover)


class BarDelegate(QStyledItemDelegate):
    """A time column with a thin cyan bar behind the value, sized by the row's share of the busiest app.
    The share (0-1) is stored on the item as Qt.UserRole + 1."""

    def paint(self, p, option, index):
        share = index.data(Qt.UserRole + 1)
        if share:
            p.save()
            r = QRectF(option.rect).adjusted(4, option.rect.height() - 7, -8, -3)
            p.fillRect(QRectF(r.left(), r.top(), r.width(), r.height()), qcolor(CYAN, 22))
            p.fillRect(QRectF(r.left(), r.top(), r.width() * float(share), r.height()), qcolor(CYAN, 170))
            p.restore()
        super().paint(p, option, index)


# ===========================================================================
# Chat transcript: each message in its own holographic frame
# ===========================================================================
def render_chat(view, entries, thinking=False):
    """entries: (role, text) pairs, oldest first. Jarvis's replies sit in cyan-edged frames;
    your messages are plainer and indented, so the conversation reads at a glance."""
    doc = view.document()
    doc.clear()
    cur = QTextCursor(doc)
    rows = list(entries) + ([("assistant", "_Analysing…_")] if thinking else [])
    for role, text in rows:
        jarvis = role != "user"
        frame = QTextFrameFormat()
        frame.setBorder(1)
        frame.setBorderStyle(QTextFrameFormat.BorderStyle_Solid)
        frame.setBorderBrush(qcolor(CYAN, 95) if jarvis else qcolor(MUTED, 55))
        frame.setBackground(qcolor(CYAN, 16) if jarvis else qcolor(MUTED, 10))
        frame.setPadding(10)
        frame.setTopMargin(6)
        frame.setBottomMargin(6)
        frame.setLeftMargin(0 if jarvis else 48)
        frame.setRightMargin(48 if jarvis else 0)
        cur.movePosition(QTextCursor.End)
        cur.insertFrame(frame)
        # the sender line: letter-spaced capitals, cyan for Jarvis
        head = QTextCharFormat()
        head.setFontFamilies([UI_FONT_SEMIBOLD, TEXT_FONT])
        head.setFontPointSize(8)
        head.setFontLetterSpacingType(QFont.AbsoluteSpacing)
        head.setFontLetterSpacing(1.8)
        head.setForeground(QColor(CYAN) if jarvis else QColor(MUTED))
        cur.insertText("◆  JARVIS" if jarvis else "▸  YOU", head)
        # the message body, as markdown, in a clean default format (so it doesn't inherit the header's)
        cur.insertBlock(QTextBlockFormat(), QTextCharFormat())
        cur.setCharFormat(QTextCharFormat())
        cur.insertMarkdown(text)
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum())


# ===========================================================================
# Overlays: page scan sweep and the start-up sequence. Both ignore the mouse,
# so they never block a click (or the QA hit-tests).
# ===========================================================================
class _Cover(QWidget):
    """A see-through layer that always covers its parent exactly."""

    def __init__(self, target):
        super().__init__(target)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        target.installEventFilter(self)
        self.hide()

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Resize:
            self.setGeometry(obj.rect())
        return False

    def _cover(self):
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()


class ScanOverlay(_Cover):
    """A bright scan line that sweeps down a page when it opens, like the HUD 'rendering' it."""
    DURATION_MS = 240

    def __init__(self, target):
        super().__init__(target)
        self.progress = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(self.DURATION_MS)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._step)
        self._anim.finished.connect(self.hide)

    def play(self):
        if not self.parentWidget().isVisible():
            return
        self._anim.stop()
        self.progress = 0.0
        self._cover()
        self._anim.start()

    def _step(self, v):
        self.progress = float(v)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        y = self.progress * self.height()
        fade = 1.0 - self.progress * 0.6
        # the trail above the line, fading out upward
        trail = QLinearGradient(0, max(0.0, y - 90), 0, y)
        trail.setColorAt(0, qcolor(CYAN, 0))
        trail.setColorAt(1, qcolor(CYAN, 26 * fade))
        p.fillRect(QRectF(0, max(0.0, y - 90), self.width(), min(90.0, y)), trail)
        # the line itself, with a soft halo
        p.fillRect(QRectF(0, y - 3, self.width(), 6), qcolor(CYAN, 40 * fade))
        p.fillRect(QRectF(0, y - 0.75, self.width(), 1.5), qcolor(CYAN_HI, 220 * fade))
        p.end()


class BootOverlay(_Cover):
    """The start-up sequence: the reactor builds up ring by ring, the wordmark appears, a few
    status lines type out, then everything fades to the dashboard. About 1.7 s, click-through."""
    BUILD, HOLD, FADE = 0.95, 0.35, 0.40

    def __init__(self, target, lines):
        super().__init__(target)
        self.lines = [str(line) for line in lines][:5]
        self._timer = QTimer(self)
        self._timer.setInterval(1000 // 40)
        self._timer.timeout.connect(self._tick)
        self._t0 = 0.0

    def play(self):
        self._t0 = time.monotonic()
        self._cover()
        self._timer.start()

    def _tick(self):
        if time.monotonic() - self._t0 > self.BUILD + self.HOLD + self.FADE:
            self._timer.stop()
            self.hide()
            self.deleteLater()
            return
        self.update()

    def paintEvent(self, _event):
        t = time.monotonic() - self._t0
        fade = 1.0 if t < self.BUILD + self.HOLD else max(0.0, 1 - (t - self.BUILD - self.HOLD) / self.FADE)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(fade)
        p.fillRect(self.rect(), QColor(BG))
        centre = QPointF(self.width() / 2, self.height() * 0.40)
        radius = min(self.width(), self.height()) * 0.17
        build = min(1.0, t / self.BUILD) * 1.35
        draw_reactor(p, centre, radius, t * 160, QColor(CYAN), 0.9, t * 3, 1.0, value=min(1.0, t / self.BUILD),
                     build=build)
        # this is the wordmark: letters appear with the build-up
        word = "J.A.R.V.I.S."
        shown = word[:max(0, int(len(word) * min(1.0, (t - 0.25) / 0.5)))]
        p.setPen(QColor(TEXT))
        p.setFont(font(20, UI_FONT_LIGHT, spacing=9))
        p.drawText(QRectF(0, centre.y() + radius + 18, self.width(), 34), Qt.AlignHCenter | Qt.AlignVCenter, shown)
        # this loop types the status lines out one after another, each with a cursor block while typing
        p.setFont(font(8.5, families=MONO_FONTS, spacing=1.2))
        y = centre.y() + radius + 64
        for i, line in enumerate(self.lines):
            start = 0.35 + i * 0.16
            if t < start:
                break
            n = int(len(line) * min(1.0, (t - start) / 0.22))
            done = n >= len(line)
            p.setPen(QColor(CYAN) if done else QColor(MUTED))
            p.drawText(QRectF(0, y, self.width(), 18), Qt.AlignHCenter | Qt.AlignVCenter,
                       ("▸ " + line[:n]) + ("" if done else " █"))
            y += 19
        p.end()


class ThinkingBar(Animated):
    """Shown on a card while the model works: 'ANALYSING' and a light that runs along a track."""

    def __init__(self, label="Analysing", parent=None):
        super().__init__(parent)
        self.label = label
        self.setFixedHeight(18)
        self._pos = 0.0

    def advance(self, dt):
        self._pos = (self._pos + dt * 0.9) % 2.0

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(font(7.5, UI_FONT_SEMIBOLD, spacing=2.0, caps=True))
        p.setPen(QColor(CYAN))
        dots = "." * (1 + int(self._pos * 3) % 3)
        p.drawText(QRectF(0, 0, 110, self.height()), Qt.AlignLeft | Qt.AlignVCenter, self.label + dots)
        x0, x1, y = 112.0, self.width() - 4.0, self.height() / 2
        p.setPen(QPen(qcolor(CYAN, 45), 2))
        p.drawLine(QPointF(x0, y), QPointF(x1, y))
        # the runner ping-pongs along the track, with a fading tail
        f = self._pos if self._pos < 1 else 2 - self._pos
        x = x0 + (x1 - x0) * f
        g = QLinearGradient(x - 60, 0, x + 60, 0)
        g.setColorAt(0.0, qcolor(CYAN, 0))
        g.setColorAt(0.5, qcolor(CYAN_HI, 255))
        g.setColorAt(1.0, qcolor(CYAN, 0))
        p.setPen(QPen(g, 2.5))
        p.drawLine(QPointF(max(x0, x - 60), y), QPointF(min(x1, x + 60), y))
        p.end()


class ScrambleLabel:
    """Mixin-free helper: make a QLabel 'decode' new text (random glyphs resolving left to right)
    whenever its text changes. Used for the Now page's focused-app name only; the label's
    text() is scrambled for ~0.35 s during the effect, so never use it where tests read text()."""
    GLYPHS = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789#%&*/<>"

    def __init__(self, label, duration=0.35):
        self.label, self.duration = label, duration
        self.target = label.text()
        self._t0 = 0.0
        self._timer = QTimer(label)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._step)

    def set_text(self, text):
        if text == self.target:
            return
        self.target = text
        if not self.label.isVisible():
            self.label.setText(text)
            return
        self._t0 = time.monotonic()
        self._timer.start()

    def _step(self):
        f = (time.monotonic() - self._t0) / self.duration
        if f >= 1:
            self._timer.stop()
            self.label.setText(self.target)
            return
        fixed = int(len(self.target) * f)
        tail = "".join(ch if ch == " " else random.choice(self.GLYPHS) for ch in self.target[fixed:])
        self.label.setText(self.target[:fixed] + tail)


# ===========================================================================
# Stylesheet images: chamfered button plates for QSS border-image (9-slice).
# Qt stylesheets can't cut corners, but they can stretch an image's edges and keep
# its corners: so the cut corners are drawn once here (at 4x, crisp at any scaling)
# and the stylesheet uses them as the button's border.
# ===========================================================================
BUTTON_SLICE = 28          # image pixels per border slice (= 7 logical px at 4x)
_BUTTON_LOOKS = {
    # token: (fill colour, fill alpha, edge colour, edge alpha)
    "@BTN@": (CYAN, 12, CYAN, 105),
    "@BTN_HOVER@": (CYAN, 34, CYAN, 190),
    "@BTN_DOWN@": (CYAN, 70, CYAN_HI, 230),
    "@BTN_OFF@": (IDLE, 8, IDLE, 70),
    "@BTN_PRI@": (CYAN, 48, CYAN, 235),
    "@BTN_PRI_HOVER@": (CYAN, 80, CYAN_HI, 255),
}


def button_rules(images):
    """QSS for chamfered buttons, for top-level windows that don't inherit the main window's sheet
    (the floating card and the quick-ask bar). images = button_images(...) output."""
    def url(token):
        return str(images[token]).replace("\\", "/")
    s = f"{BUTTON_SLICE} {BUTTON_SLICE} {BUTTON_SLICE} {BUTTON_SLICE} stretch stretch"
    return (f"QPushButton {{ border-image: url({url('@BTN@')}) {s}; border-width: 7px; padding: 0px 5px; "
            f"font-family: '{BUTTON_FONT}'; font-size: 8.5pt; color: {TEXT}; }}\n"
            f"QPushButton:hover {{ border-image: url({url('@BTN_HOVER@')}) {s}; color: #ffffff; }}\n"
            f"QPushButton:pressed {{ border-image: url({url('@BTN_DOWN@')}) {s}; }}\n"
            f"QPushButton:disabled {{ border-image: url({url('@BTN_OFF@')}) {s}; color: #4f6878; }}\n")


def button_images(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = {}
    size, cut, stroke = 64, 20, 4
    for token, (fill, fa, edge, ea) in _BUTTON_LOOKS.items():
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(stroke / 2, stroke / 2, size - stroke, size - stroke)
        path = chamfer_path(r, cut)
        p.fillPath(path, qcolor(fill, fa))
        p.setPen(QPen(qcolor(edge, ea), stroke, Qt.SolidLine, Qt.FlatCap, Qt.MiterJoin))
        p.drawPath(path)
        p.end()
        name = token.strip("@").lower() + ".png"
        pm.save(str(folder / name))
        out[token] = folder / name
    return out


# ===========================================================================
# App icon: a miniature arc reactor. Every size is drawn separately so the 16 px
# tray version stays sharp. Cyan = watching, grey = paused (with a pause badge).
# A dark disc sits behind it so it reads on light and dark taskbars alike.
# ===========================================================================
def draw_reactor_icon(size: int, active: bool) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    colour = QColor(CYAN if active else IDLE)
    c = QPointF(size / 2, size / 2)
    r = size / 2 - (0 if size <= 24 else size * 0.03)

    # the dark disc
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#07121c"))
    p.drawEllipse(c, r, r)
    # outer ring
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(colour, max(1.3, size * 0.075)))
    p.drawEllipse(c, r * 0.80, r * 0.80)
    # three arc segments (the reactor's plates)
    seg_w = max(1.4, size * 0.09)
    for i in range(3):
        arc(p, c, r * 0.54, 90 + i * 120 + 18, 84, colour, seg_w)
    # the glowing core
    core = QRadialGradient(c, r * 0.30)
    core.setColorAt(0.0, QColor("#ffffff") if active else QColor("#c9d3da"))
    core.setColorAt(0.55, colour)
    core.setColorAt(1.0, qcolor(colour, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(core)
    p.drawEllipse(c, r * 0.30, r * 0.30)

    # paused badge (two bars) on the larger sizes, where there is room for it
    if not active and size >= 32:
        br = size * 0.36
        badge = QRectF(size - br, size - br, br, br)
        p.setBrush(QColor("#07121c"))
        p.drawEllipse(badge)
        p.setBrush(QColor(IDLE))
        bw, bh = br * 0.16, br * 0.46
        cy = badge.center().y() - bh / 2
        p.drawRect(QRectF(badge.center().x() - bw * 1.6, cy, bw, bh))
        p.drawRect(QRectF(badge.center().x() + bw * 0.6, cy, bw, bh))
    p.end()
    return pm


# ===========================================================================
# Windows 11 title bar: paint the window's own caption in the HUD colours.
# (DWM attributes 20 = dark mode, 34 = border colour, 35 = caption colour, 36 = caption text.)
# Older Windows versions just ignore the attributes they don't know.
# ===========================================================================
def _colorref(hex_colour):
    c = QColor(hex_colour)
    return c.red() | (c.green() << 8) | (c.blue() << 16)


def style_titlebar(window):
    try:
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        hwnd = ctypes.c_void_p(int(window.winId()))
        for attr, value in ((20, 1), (34, _colorref("#1b6f8a")), (35, _colorref("#07101a")), (36, _colorref(CYAN_HI))):
            v = ctypes.c_uint(value)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
    except (AttributeError, OSError):
        pass  # not Windows / no DWM: the normal title bar is fine

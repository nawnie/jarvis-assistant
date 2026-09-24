"""Jarvis Assistant GUI (PySide6) and system-tray icon.

Layout: a sidebar of pages on the left, one page shown at a time.
  Now        - live dashboard: what has focus, streak, idle, load, today's apps, events
  Timeline   - the recorded window history, with "summarise this range"
  Clipboard  - everything you copied, with explain / summarise actions
  Reminders  - timed reminders that pop from the tray
  Journal    - the model's periodic write-ups, plus "what did I do today?"
  Memory     - long-term facts that get fed into every chat
  Chat       - talk to the model, with live activity as context
  Recall     - search everything Jarvis has seen, or ask a question about it
  Projects   - work you give Jarvis to do on its own while you're away
  Settings   - what to watch, thresholds, model, start with Windows
"""
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPointF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QAbstractSpinBox, QDialog, QDialogButtonBox, QFileDialog, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QStackedWidget,
    QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget)

from . import config, hud, popup, sensors
from .brain import SYSTEM_PERSONA, Engine, run_async

# ---------------------------------------------------------------------------
# Look and feel: a JARVIS-style heads-up display. Deep navy glass, one hologram
# cyan, amber/red only for warnings. The painted parts (cut-corner panels, the
# arc reactor, gauges, sidebar, scan effects) live in wk/hud.py; this stylesheet
# makes the standard Qt controls match them. The @TOKENS@ are small images drawn
# at startup (ui_images), because Qt stylesheets can't draw shapes themselves.
# ---------------------------------------------------------------------------
ACCENT = hud.CYAN
MUTED = hud.MUTED
_FIELD = hud.css_rgba("#06101a", 0.88)       # text fields, lists and tables: dark glass
_EDGE = hud.css_rgba(hud.CYAN, 0.26)         # their resting outline
_SELECT = hud.css_rgba(hud.CYAN, 0.24)       # selected rows and selected text
_BTN = "28 28 28 28 stretch stretch"         # 9-slice of the chamfered button images (hud.button_images)
STYLE = f"""
* {{ font-family: 'Segoe UI'; font-size: 10pt; color: {hud.TEXT}; }}
QMainWindow {{ background: {hud.BG}; }}
QWidget#page {{ background: transparent; }}
QDialog, QMessageBox {{ background: {hud.BG_RAISED}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget#qt_scrollarea_viewport {{ background: transparent; }}

/* sidebar: the list is painted by hud.NavDelegate, so only its frame is cleared here */
QListWidget#nav {{ background: transparent; border: none; }}
QLabel#wordmark {{ font-family: '{hud.UI_FONT_LIGHT}'; font-size: 13pt; color: {hud.CYAN_HI}; }}
QLabel#wordsub {{ font-family: '{hud.UI_FONT}'; font-size: 7pt; color: {hud.MUTED}; }}

/* text roles */
QLabel#h1 {{ font-family: '{hud.UI_FONT_LIGHT}'; font-size: 17pt; color: #eafaff; }}
QLabel#muted {{ color: {hud.MUTED}; }}
QLabel#paneltitle {{ font-family: '{hud.UI_FONT_SEMIBOLD}'; font-size: 8pt; color: {hud.MUTED}; }}
QLabel#big {{ font-family: '{hud.UI_FONT_LIGHT}'; font-size: 16pt; color: {hud.TEXT}; }}
QLabel#status {{ font-family: '{hud.UI_FONT_SEMIBOLD}'; font-size: 9.5pt; }}

/* buttons: chamfered plates. Primary = the lit plate. Text case/spacing is set in code (hud.caps). */
QPushButton {{ border-image: url(@BTN@) {_BTN}; border-width: 7px; padding: 1px 6px; min-height: 16px;
    font-family: '{hud.BUTTON_FONT}'; font-size: 9pt; color: {hud.TEXT}; }}
QPushButton:hover {{ border-image: url(@BTN_HOVER@) {_BTN}; color: #ffffff; }}
QPushButton:pressed {{ border-image: url(@BTN_DOWN@) {_BTN}; }}
QPushButton:disabled {{ border-image: url(@BTN_OFF@) {_BTN}; color: #4f6878; }}
QPushButton#primary {{ border-image: url(@BTN_PRI@) {_BTN}; color: {hud.CYAN_HI}; }}
QPushButton#primary:hover {{ border-image: url(@BTN_PRI_HOVER@) {_BTN}; color: #ffffff; }}
QPushButton#primary:pressed {{ border-image: url(@BTN_DOWN@) {_BTN}; }}
QPushButton#primary:disabled {{ border-image: url(@BTN_OFF@) {_BTN}; color: #4f6878; }}

/* fields, lists, tables and text views: dark glass with a thin cyan edge that lights up on focus */
QLineEdit, QPlainTextEdit, QTextBrowser, QSpinBox, QComboBox, QTableWidget, QListWidget {{
    background: {_FIELD}; border: 1px solid {_EDGE}; border-radius: 0px;
    selection-background-color: {_SELECT}; selection-color: #ffffff; }}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {hud.CYAN}; }}
QLineEdit {{ padding: 4px 6px; }}

/* Spin boxes: the arrow column is laid out HERE, explicitly. Styling a spin box's border without
   this made Qt size its inner text field for a narrow stacked arrow column while the Windows 11
   style drew wide side-by-side arrows - so the text field covered the up arrow and ate its clicks.
   padding-right reserves the column; the two buttons are stacked inside it. */
QAbstractSpinBox {{ padding-right: 24px; min-height: 26px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    subcontrol-origin: border; width: 22px; background: {hud.css_rgba(hud.CYAN, 0.07)}; border-left: 1px solid {_EDGE}; }}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; }}
QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; border-top: 1px solid {_EDGE}; }}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: {hud.css_rgba(hud.CYAN, 0.18)}; }}
QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed {{ background: {hud.css_rgba(hud.CYAN, 0.34)}; }}
/* arrow glyphs are small images drawn at startup (ui_images) - Qt stylesheets can't draw triangles */
QAbstractSpinBox::up-arrow {{ image: url(@UP@); width: 9px; height: 6px; }}
QAbstractSpinBox::down-arrow {{ image: url(@DOWN@); width: 9px; height: 6px; }}
QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off {{ image: url(@UP_OFF@); }}
QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off {{ image: url(@DOWN_OFF@); }}

/* dropdowns: same glass as the fields, a cyan arrow, and an opaque list so it reads over anything */
QComboBox {{ padding: 3px 8px; min-height: 22px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 22px;
    border-left: 1px solid {_EDGE}; }}
QComboBox::down-arrow {{ image: url(@DOWN@); width: 9px; height: 6px; }}
QComboBox QAbstractItemView {{ background: {hud.BG_RAISED}; border: 1px solid {hud.CYAN};
    selection-background-color: {_SELECT}; outline: 0; }}

/* checkboxes: a square cyan indicator (the Windows 11 style otherwise uses the system accent) */
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid {hud.css_rgba(hud.CYAN, 0.55)};
    background: {_FIELD}; }}
QCheckBox::indicator:hover {{ border-color: {hud.CYAN}; }}
QCheckBox::indicator:checked {{ background: {hud.CYAN}; border-color: {hud.CYAN}; image: url(@CHECK@); }}

/* Lists and tables: no dotted focus frame or Windows 11 "caret" bar on the current cell;
   the selection itself is shown in the HUD's colours instead */
QTableView, QListView {{ outline: 0; }}
QTableView::item:selected, QListView::item:selected {{ background: {_SELECT}; color: #ffffff; }}
QTableView::item:hover, QListView::item:hover {{ background: {hud.css_rgba(hud.CYAN, 0.07)}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {hud.css_rgba(hud.CYAN, 0.05)}; border: none;
    border-bottom: 1px solid {hud.css_rgba(hud.CYAN, 0.35)}; padding: 5px 6px; color: {hud.MUTED};
    font-family: '{hud.UI_FONT_SEMIBOLD}'; font-size: 8pt; }}
QTableCornerButton::section {{ background: transparent; border: none; }}

/* thin cyan scrollbars with no arrow buttons */
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px 1px; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 1px 2px; }}
QScrollBar::handle {{ background: {hud.css_rgba(hud.CYAN, 0.30)}; min-height: 28px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {hud.css_rgba(hud.CYAN, 0.60)}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QSplitter::handle {{ background: transparent; }}

QProgressBar {{ background: {hud.css_rgba(hud.CYAN, 0.10)}; border: none; height: 6px; text-align: center; }}
QProgressBar::chunk {{ background: {hud.CYAN}; }}

/* menus and tooltips (the tray menu gets this same sheet) */
QMenu {{ background: {hud.BG_RAISED}; border: 1px solid {hud.css_rgba(hud.CYAN, 0.45)}; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 26px; }}
QMenu::item:selected {{ background: {_SELECT}; color: #ffffff; }}
QMenu::item:disabled {{ color: #4f6878; }}
QMenu::indicator {{ width: 13px; height: 13px; left: 7px; }}
QMenu::indicator:checked {{ image: url(@TICK@); }}
QMenu::separator {{ height: 1px; background: {hud.css_rgba(hud.CYAN, 0.22)}; margin: 4px 8px; }}
QToolTip {{ background: {hud.BG_RAISED}; color: {hud.TEXT}; border: 1px solid {hud.CYAN}; padding: 5px; }}
"""


# ---------------------------------------------------------------------------
# App icon: a miniature arc reactor (drawn in hud.draw_reactor_icon).
# Cyan = watching, grey = paused. Every size is drawn separately (not scaled
# down from one big image) so the 16 px tray version stays sharp; Windows picks
# the size it needs for the tray, taskbar, Alt-Tab and desktop shortcut.
# ---------------------------------------------------------------------------
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def draw_icon(size: int, active: bool) -> QPixmap:
    return hud.draw_reactor_icon(size, active)


def ui_images(folder):
    """Draw the stylesheet's small images (spin arrows, check marks, chamfered button plates) at
    4x size so they stay crisp at any display scaling, and return {token: path} for the stylesheet."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    def triangle(name, up, colour):
        pm = QPixmap(36, 24)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(colour))
        # three corners of the triangle: tip, then the two base corners
        corners = [(18, 2), (34, 22), (2, 22)] if up else [(2, 2), (34, 2), (18, 22)]
        path = QPainterPath(QPointF(*corners[0]))
        for corner in corners[1:]:
            path.lineTo(QPointF(*corner))
        path.closeSubpath()
        p.drawPath(path)
        p.end()
        pm.save(str(folder / name))

    triangle("up.png", True, hud.CYAN)
    triangle("down.png", False, hud.CYAN)
    triangle("up_off.png", True, "#2c4452")
    triangle("down_off.png", False, "#2c4452")

    # two check marks: dark (sits on a cyan checkbox) and cyan (a tick in the tray menu)
    def check(name, colour):
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(colour), 9)
        pen.setCapStyle(Qt.SquareCap)
        pen.setJoinStyle(Qt.MiterJoin)
        p.setPen(pen)
        p.drawPolyline([QPointF(14, 34), QPointF(27, 47), QPointF(50, 18)])
        p.end()
        pm.save(str(folder / name))

    check("check.png", hud.BG)
    check("tick.png", hud.CYAN)
    images = {"@UP@": folder / "up.png", "@DOWN@": folder / "down.png", "@UP_OFF@": folder / "up_off.png",
              "@DOWN_OFF@": folder / "down_off.png", "@CHECK@": folder / "check.png", "@TICK@": folder / "tick.png"}
    images.update(hud.button_images(folder))
    return images


def build_style():
    """The app stylesheet with the image tokens replaced by real file paths (forward slashes for Qt)."""
    style = STYLE
    for token, path in ui_images(config.DATA_DIR / "ui").items():
        style = style.replace(token, str(path).replace("\\", "/"))
    return style


def make_icon(active: bool) -> QIcon:
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(draw_icon(size, active))
    return icon


def save_ico(path, active=True):
    """Write a multi-size .ico (PNG entries, Windows Vista+ format) for the shortcut and startup link."""
    import struct
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    images = []
    for size in ICON_SIZES:
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.WriteOnly)
        draw_icon(size, active).save(buf, "PNG")
        images.append((size, bytes(data)))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO directory
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(blobs))
        blobs += png
    Path(path).write_bytes(header + entries + blobs)


def fmt_dur(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


# this is the page-building helper section: every page is a titled header plus HUD panels
def card(title):
    """A titled cut-corner HUD panel (hud.HudPanel); returns (frame, inner layout)."""
    frame = hud.HudPanel()
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(24, 10, 14, 12)
    head = QLabel(title.upper(), objectName="paneltitle")
    hud.tracked(head, 1.6)
    frame.title_label = head        # the panel paints its cyan tab and rule beside this label
    lay.addWidget(head)
    return frame, lay


def page(title, subtitle=""):
    """A page: its title in spaced capitals over a HUD rule, then an optional one-line explanation."""
    w = QWidget(objectName="page")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(22, 16, 22, 18)
    lay.setSpacing(12)
    head = QVBoxLayout()
    head.setSpacing(2)
    h1 = QLabel(title.upper(), objectName="h1")
    hud.tracked(h1, 5)
    head.addWidget(h1)
    head.addWidget(hud.HudRule())
    lay.addLayout(head)
    if subtitle:
        # wraps, so a long explanation never sets the window's minimum width
        lay.addWidget(QLabel(subtitle, objectName="muted", wordWrap=True))
    return w, lay


def table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.verticalHeader().setDefaultSectionSize(30)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setShowGrid(False)
    t.horizontalHeader().setStretchLastSection(True)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    hud.caps(t.horizontalHeader(), 1.4)
    return t


def fill_table(t, rows, keys=None):
    """keys (optional): a hidden value per row, e.g. the real process name behind a friendly app name."""
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            item = QTableWidgetItem(str(value))
            if keys is not None:
                item.setData(Qt.UserRole, keys[r])
            t.setItem(r, c, item)


def app_label(process):
    """'Google Chrome' for chrome.exe - only from programs already seen this session (never a slow scan)."""
    return sensors.app_description(process, scan=False) or process


class WheelOnlyWhenFocused(QObject):
    """Spin boxes and dropdowns ignore the mouse wheel unless you've clicked into them, and hand
    it to the page instead - so scrolling Settings never silently changes a value you rolled over."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel and not obj.hasFocus():
            event.ignore()      # not accepted -> Qt passes the wheel up to the scrolling page
            return True
        return False


def llm_error(result):
    return (f"Local model unreachable ({result}).\n"
            "Jarvis starts its model server by itself; it may still be loading - try again in a few seconds.")


# ===========================================================================
# Main window
# ===========================================================================
class MainWindow(QMainWindow):
    def __init__(self, engine: Engine):
        super().__init__()
        self.engine = engine
        self.store = engine.store
        self.setWindowTitle("Jarvis Assistant")
        self.setWindowIcon(make_icon(True))
        self.resize(1120, 740)
        self.setStyleSheet(build_style())
        # links in answers and journal entries in the HUD cyan (QSS can't reach QTextBrowser links)
        pal = self.palette()
        pal.setColor(QPalette.Link, QColor(hud.CYAN))
        self.setPalette(pal)

        # this is the sidebar + stacked pages section, on the gridded HUD backdrop
        root = hud.Backdrop()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self._build_sidebar())
        self.stack = QStackedWidget()
        h.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.scan = hud.ScanOverlay(self.stack)     # the sweep that plays when a page opens

        builders = [("Now", self._build_now), ("Timeline", self._build_timeline),
                    ("Clipboard", self._build_clipboard), ("Reminders", self._build_reminders),
                    ("Journal", self._build_journal), ("Memory", self._build_memory),
                    ("Chat", self._build_chat), ("Recall", self._build_recall), ("Projects", self._build_projects),
                    ("Settings", self._build_settings)]
        for name, build in builders:
            self.nav.addItem(QListWidgetItem(name))
            self.stack.addWidget(build())
        self.nav.currentRowChanged.connect(self._on_page)
        self.nav.setCurrentRow(0)

        # every spin box and dropdown: wheel only when focused (see WheelOnlyWhenFocused)
        self._wheel_guard = WheelOnlyWhenFocused(self)
        for box in self.findChildren(QAbstractSpinBox) + self.findChildren(QComboBox):
            box.setFocusPolicy(Qt.StrongFocus)
            box.installEventFilter(self._wheel_guard)

        # every button renders in spaced HUD capitals (text() is unchanged; see hud.caps)
        for button in self.findChildren(QPushButton):
            hud.caps(button, 1.1)
        hud.style_titlebar(self)          # Windows 11: the title bar itself goes HUD navy + cyan

        # the floating info card: Ctrl+click anywhere, or click an app in Today / Timeline
        self.card = popup.InfoCard()
        self.card.visibility_changed.connect(lambda shown: setattr(engine, "card_open", shown))
        self.card.continue_in_chat.connect(self._card_to_chat)
        self.askbar = popup.AskBar(engine, self.card)
        engine.hotkey_pressed.connect(self._quick_ask)   # emitted on the input thread -> bound slot
        # explain_requested is emitted on the mouse-hook thread; connecting it to a bound method of this
        # window (not a lambda) makes Qt queue the call onto the GUI thread, and frees the hook instantly
        engine.explain_requested.connect(self._explain_at)
        engine.outside_click.connect(self.card.click_elsewhere)

        # engine -> GUI wiring
        engine.status.connect(self._on_status)
        engine.data_changed.connect(self._on_data)
        engine.watching_changed.connect(lambda _: self._sync_toggle())

        # today's-apps table is cheap but not free; refresh it every 30 s, not every tick
        self._slow = QTimer(self, timeout=self._refresh_today, interval=30_000)
        self._slow.start()
        self._refresh_today()
        self._refresh_events()

    # -----------------------------------------------------------------------
    # HUD chrome: the sidebar, the "model is thinking" signal, the start-up intro
    # -----------------------------------------------------------------------
    def _build_sidebar(self):
        """Left column: the reactor emblem and wordmark, the page list, and a chronometer."""
        side = hud.Sidebar()
        side.setFixedWidth(184)
        v = QVBoxLayout(side)
        v.setContentsMargins(0, 14, 0, 10)
        v.setSpacing(0)
        # the emblem mirrors Jarvis's state: spins while watching, races while the model works
        self.emblem = hud.ArcReactor(62, show_text=False, busy_fn=self._model_busy)
        v.addWidget(self.emblem, 0, Qt.AlignHCenter)
        mark = QLabel("J.A.R.V.I.S.", objectName="wordmark", alignment=Qt.AlignHCenter)
        hud.tracked(mark, 4)
        v.addSpacing(6)
        v.addWidget(mark)
        sub = QLabel("LOCAL ASSISTANT", objectName="wordsub", alignment=Qt.AlignHCenter)
        hud.tracked(sub, 2.4)
        v.addWidget(sub)
        v.addSpacing(12)
        # the page list: painted by hud.NavDelegate, but its items keep their plain names
        # (show_page("Timeline"), the QA tests and Windows UI Automation all rely on them)
        self.nav = QListWidget(objectName="nav")
        self.nav.setItemDelegate(hud.NavDelegate(self.nav))
        self.nav.setMouseTracking(True)
        self.nav.viewport().setAttribute(Qt.WA_Hover)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        v.addWidget(self.nav, 1)
        v.addWidget(hud.ClockReadout())
        return side

    def _model_busy(self):
        """True while the local model is answering anything (chat, a card, a journal entry...)."""
        return getattr(self.engine.llm, "inflight", 0) > 0

    def play_intro(self):
        """The start-up sequence over the whole window (called once, when Jarvis launches visibly)."""
        cfg = self.engine.cfg
        lines = ["Initialising heads-up display",
                 f"Local model link · {cfg['llm_base_url'].replace('http://', '')}",
                 "Sensors · windows · clipboard · system",
                 "All systems online" if self.engine.watching else "Standing by · watching paused"]
        hud.BootOverlay(self.centralWidget(), [line.upper() for line in lines]).play()

    def showEvent(self, event):
        super().showEvent(event)
        if not event.spontaneous():
            self.scan.play()          # coming back from the tray: sweep the current page in

    def _quick_ask(self):
        self.askbar.summon()

    def open_clip(self, clip_id):
        """Jump to a specific clip (used when you click the 'copied an error' notification)."""
        self._clip_sel_id = clip_id
        self.show_page("Clipboard")
        self._refresh_clips()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _explain_at(self, x, y):
        popup.explain_at(self.card, self.engine, x, y)

    def _card_to_chat(self, topic, answer):
        """'Continue in chat' on a card: put the question and answer into the chat so follow-ups have context."""
        self.store.add_chat("user", f"Tell me about this:\n\n{topic}")
        self.store.add_chat("assistant", answer)
        self.show_page("Chat")
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.chat_in.setFocus()

    def closeEvent(self, event):
        # closing the window only hides it; the assistant keeps running in the tray
        event.ignore()
        self.hide()

    def show_page(self, name):
        for i in range(self.nav.count()):
            if self.nav.item(i).text() == name:
                self.nav.setCurrentRow(i)

    def _on_page(self, row):
        self.stack.setCurrentIndex(row)
        refresh = {1: self._refresh_timeline, 2: self._refresh_clips, 3: self._refresh_reminders,
                   4: self._refresh_journal, 5: self._refresh_memory, 6: self._render_chat,
                   8: self._refresh_projects}
        if row in refresh:
            refresh[row]()
        self.scan.play()              # the HUD scan line sweeps the new page in

    def _on_data(self, area):
        if area == "events":
            self._refresh_events()
        elif area == "clipboard" and self.stack.currentIndex() == 2:
            self._refresh_clips()
        elif area == "journal":
            self._refresh_journal()
        elif area == "reminders":
            self._refresh_reminders()
        elif area == "memory":
            self._refresh_memory()
        elif area == "chat" and self.stack.currentIndex() == 6:
            self._render_chat()
        elif area == "projects":
            self._refresh_projects()
        elif area == "settings":
            self._load_settings_values()

    # -----------------------------------------------------------------------
    # NOW page: live dashboard
    # -----------------------------------------------------------------------
    def _build_now(self):
        w, lay = page("Now")

        # on/off row
        row = QHBoxLayout()
        self.state_label = QLabel(objectName="status")
        hud.caps(self.state_label, 1.8)
        self.toggle_btn = QPushButton(objectName="primary", clicked=self._toggle)
        pause_btn = QPushButton("Pause 30 min", clicked=lambda: self.engine.set_watching(False, 30))
        row.addWidget(self.state_label)
        row.addStretch(1)
        row.addWidget(pause_btn)
        row.addWidget(self.toggle_btn)
        lay.addLayout(row)

        # Compact mission readout: durable intent, saved context, and callable links.
        mission_row = QHBoxLayout()
        mission_row.setSpacing(10)
        f, l = card("Standing mission")
        self.mission_brief = QLabel(objectName="muted")
        self.mission_brief.setWordWrap(True)
        l.addWidget(self.mission_brief)
        mission_row.addWidget(f, 3)
        f, l = card("Memory core")
        self.memory_brief = QLabel(objectName="muted")
        self.memory_brief.setWordWrap(True)
        l.addWidget(self.memory_brief)
        mission_row.addWidget(f, 2)
        f, l = card("Tool links")
        self.tools_brief = QLabel(objectName="muted")
        self.tools_brief.setWordWrap(True)
        l.addWidget(self.tools_brief)
        mission_row.addWidget(f, 2)
        lay.addLayout(mission_row)

        # this is the live telemetry grid: reactor | focused app | model  /  four radial gauges
        grid = QGridLayout()
        grid.setSpacing(10)
        # the arc reactor shows the active streak; its ring fills up toward the break nudge
        f, l = card("Active streak")
        self.reactor = hud.ArcReactor(124, busy_fn=self._model_busy)
        l.addWidget(self.reactor, 0, Qt.AlignHCenter)
        l.addStretch(1)
        grid.addWidget(f, 0, 0)
        f, l = card("Focused on")
        self.now_proc = QLabel("-", objectName="big")
        self._proc_decoder = hud.ScrambleLabel(self.now_proc)   # a new app name "decodes" into place
        self.now_title = QLabel("", objectName="muted")
        self.now_title.setWordWrap(True)
        l.addWidget(self.now_proc)
        l.addWidget(self.now_title)
        l.addStretch(1)
        grid.addWidget(f, 0, 1, 1, 2)
        f, l = card("Local model")
        self.now_llm = QLabel("-", objectName="big")
        self.now_llm.setWordWrap(True)
        l.addWidget(self.now_llm)
        self.now_llm_url = QLabel(self.engine.cfg["llm_base_url"], objectName="muted")
        self.now_llm_url.setWordWrap(True)
        l.addWidget(self.now_llm_url)
        l.addStretch(1)
        grid.addWidget(f, 0, 3)

        # this loop builds the four system gauges (warning colours come from the alert settings)
        self.gauges = {}
        for col, (key, label, unit) in enumerate((("cpu", "CPU", "%"), ("ram", "RAM", "%"), ("gpu", "GPU", "%"),
                                                  ("gpu_temp", "GPU temp", "°C"))):
            f, l = card(label)
            l.setContentsMargins(24, 10, 14, 8)
            gauge = hud.RadialGauge(unit)
            l.addWidget(gauge, 1)
            self.gauges[key] = gauge
            grid.addWidget(f, 1, col)
        grid.setRowStretch(0, 0)
        lay.addLayout(grid)

        # today by app + recent events
        split = QSplitter()
        f, l = card("Today by app")
        self.today_table = table(["App", "Time"])
        self.today_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.today_table.setItemDelegateForColumn(1, hud.BarDelegate(self.today_table))
        self.today_table.setCursor(Qt.PointingHandCursor)
        self.today_table.setToolTip("Click an app to see what it is and what you did in it today")
        self.today_table.cellClicked.connect(self._today_clicked)
        l.addWidget(self.today_table)
        split.addWidget(f)
        f, l = card("What it noticed")
        self.events_list = QListWidget()
        self.events_list.setWordWrap(True)
        hud.make_log_view(self.events_list)     # timestamp · tag chip · message, like a system log
        l.addWidget(self.events_list)
        split.addWidget(f)
        split.setSizes([420, 560])
        lay.addWidget(split, 1)
        self._sync_toggle()
        return w

    def _toggle(self):
        self.engine.set_watching(not self.engine.watching)

    def _sync_toggle(self):
        on = self.engine.watching
        self.toggle_btn.setText("Pause watching" if on else "Resume watching")
        if on:
            self.state_label.setText(f"<span style='color:{ACCENT}'>◆</span>&nbsp;&nbsp;Watching")
        elif self.engine.paused_until:
            self.state_label.setText(f"<span style='color:{hud.AMBER}'>◇</span>&nbsp;&nbsp;Paused until "
                                     f"{hhmm(self.engine.paused_until)}")
        else:
            self.state_label.setText(f"<span style='color:{hud.AMBER}'>◇</span>&nbsp;&nbsp;Paused - nothing is being recorded")
        # both reactors go grey and still while paused
        mode = "online" if on else "paused"
        self.reactor.set_mode(mode)
        self.emblem.set_mode(mode)

    def _on_status(self, s):
        # this is the reactor-state section: paused > model offline > online (thinking is checked per frame)
        mode = "paused" if not s["watching"] else ("online" if s["llm_online"] else "offline")
        self.emblem.set_mode(mode)
        if not self.isVisible():
            return
        self.reactor.set_mode(mode)
        break_after = max(1, int(self.engine.cfg.get("break_after_minutes", 90))) * 60
        self.reactor.set_readout(fmt_dur(s["streak"]) if s["watching"] else "PAUSED",
                                 f"idle {fmt_dur(s['idle'])}",
                                 min(1.0, s["streak"] / break_after) if s["watching"] else None)

        # this is the focused-app and model section
        self._proc_decoder.set_text(app_label(s["process"]) if s["process"] else "-")
        self.now_title.setText(s["title"][:160])
        self.now_llm.setText(s.get("model", "-"))
        link = (f"<span style='color:{hud.CYAN}'>◆ ONLINE</span>" if s["llm_online"]
                else f"<span style='color:{hud.AMBER}'>◇ OFFLINE</span>")
        self.now_llm_url.setText(f"{link}&nbsp;&nbsp;{self.engine.cfg['llm_base_url']}")

        # this loop feeds the gauges; RAM and GPU temperature warn at the thresholds set in Settings
        cfg = self.engine.cfg
        limits = {"ram": float(cfg.get("ram_alert_percent", 90)), "gpu_temp": float(cfg.get("gpu_temp_alert_c", 83))}
        for key, gauge in self.gauges.items():
            warn = limits.get(key, 85.0)
            gauge.set_limits(warn, min(gauge.maximum, warn + (7 if key == "gpu_temp" else 8)))
            gauge.set_value(s.get(key))

    def _refresh_today(self):
        from . import delegate_tools
        projects = self.store.projects(include_done=False)
        facts = self.store.facts()
        self.mission_brief.setText(str(self.engine.cfg.get("assistant_mission", ""))[:150])
        self.memory_brief.setText(f"{len(facts)} saved anchors  ·  {len(projects)} open objectives\n"
                                  f"Recall: {self.engine.cfg.get('memory_fact_mode', 'related')}")
        links = delegate_tools.available()
        self.tools_brief.setText(f"Claude {'ready' if links['claude'] else 'missing'}  ·  "
                                 f"Codex {'ready' if links['codex'] else 'missing'}\n"
                                 f"8B routing {'enabled' if self.engine.cfg.get('tool_use_8b') else 'paused'}")
        start = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        totals = [(p, sec) for p, sec in self.store.app_totals(start, time.time()) if sec >= 30]
        fill_table(self.today_table, [(app_label(p), fmt_dur(sec)) for p, sec in totals], keys=[p for p, _ in totals])
        # each Time cell gets its share of the busiest app, drawn as a thin bar (hud.BarDelegate)
        most = max((sec for _, sec in totals), default=0)
        for r, (_, sec) in enumerate(totals):
            self.today_table.item(r, 1).setData(Qt.UserRole + 1, sec / most if most else 0)

    def _today_clicked(self, row, _col):
        item = self.today_table.item(row, 0)
        if item:
            midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
            popup.explain_app(self.card, self.engine, item.data(Qt.UserRole), midnight, time.time(), "today")

    def _refresh_events(self):
        self.events_list.clear()
        for ts, kind, text in self.store.events(80):
            self.events_list.addItem(f"{hhmm(ts)}  [{kind}]  {text}")

    # -----------------------------------------------------------------------
    # TIMELINE page
    # -----------------------------------------------------------------------
    def _build_timeline(self):
        w, lay = page("Timeline", "Every window that had focus while you were active. Idle time is left out.")
        row = QHBoxLayout()
        self.tl_range = QComboBox()
        self.tl_range.addItems(["Last hour", "Today", "Yesterday", "Last 7 days"])
        self.tl_range.setCurrentIndex(1)
        self.tl_range.currentIndexChanged.connect(self._refresh_timeline)
        summarize = QPushButton("Write a journal entry for this range", objectName="primary",
                                clicked=self._summarize_range)
        row.addWidget(self.tl_range)
        row.addStretch(1)
        row.addWidget(summarize)
        lay.addLayout(row)
        self.tl_table = table(["Start", "App", "Window", "Duration"])
        self.tl_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tl_table.horizontalHeader().setStretchLastSection(False)
        self.tl_table.setCursor(Qt.PointingHandCursor)
        self.tl_table.cellClicked.connect(self._timeline_clicked)
        lay.addWidget(self.tl_table, 1)
        return w

    def _range(self):
        now = time.time()
        midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        return {0: (now - 3600, now), 1: (midnight, now), 2: (midnight - 86400, midnight),
                3: (now - 7 * 86400, now)}[self.tl_range.currentIndex()]

    def _refresh_timeline(self):
        start, end = self._range()
        found = [(a, b, p, t) for a, b, p, t in self.store.activity_rows(start, end) if b - a >= 5]
        fill_table(self.tl_table, [(time.strftime("%a %H:%M", time.localtime(a)), app_label(p), t, fmt_dur(b - a))
                                   for a, b, p, t in found], keys=[p for _, _, p, _ in found])

    def _timeline_clicked(self, row, _col):
        item = self.tl_table.item(row, 1)
        if item:
            start, end = self._range()
            popup.explain_app(self.card, self.engine, item.data(Qt.UserRole), start, end,
                              self.tl_range.currentText().lower())

    def _summarize_range(self):
        # jump to the Journal straight away so the click visibly does something;
        # the finished entry replaces "Writing..." when the engine emits data_changed("journal")
        start, end = self._range()
        self.show_page("Journal")
        self.journal_view.setMarkdown(f"_Writing a journal entry for {self.tl_range.currentText().lower()}..._")
        self.engine.write_journal(start, end)

    # -----------------------------------------------------------------------
    # CLIPBOARD page
    # -----------------------------------------------------------------------
    def _build_clipboard(self):
        w, lay = page("Clipboard", "Text you copied, newest first. Nothing is kept from private windows.")
        split = QSplitter()
        self.clip_list = QListWidget()
        self.clip_list.currentRowChanged.connect(self._show_clip)
        split.addWidget(self.clip_list)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.clip_view = QPlainTextEdit(readOnly=True)
        rl.addWidget(self.clip_view, 2)
        # this is the action-button section. The model buttons act on the clip shown
        # above them; all of them are disabled whenever no clip is selected, so a click
        # can never silently do nothing.
        buttons = QHBoxLayout()
        self.clip_ask_buttons = []
        for label, prompt in (("Explain", "Explain this clearly and briefly:"),
                              ("Summarize", "Summarize this in a few bullets:"),
                              ("Find problems", "Point out any bugs, errors or mistakes in this:")):
            btn = QPushButton(label, clicked=lambda _=False, p=prompt, n=label: self._clip_ask(p, n))
            self.clip_ask_buttons.append(btn)
            buttons.addWidget(btn)
        buttons.addStretch(1)
        self.clip_copy_btn = QPushButton("Copy again", clicked=self._clip_copy)
        self.clip_delete_btn = QPushButton("Delete", clicked=self._clip_delete)
        buttons.addWidget(self.clip_copy_btn)
        buttons.addWidget(self.clip_delete_btn)
        rl.addLayout(buttons)
        self.clip_answer = QTextBrowser()
        rl.addWidget(self.clip_answer, 3)
        split.addWidget(right)
        split.setSizes([360, 620])
        lay.addWidget(split, 1)
        self._clips = []
        self._clip_sel_id = None      # database id of the selected clip, survives list refreshes
        self._clip_busy = False       # True while the model is answering about a clip
        self._sync_clip_buttons()
        return w

    def _refresh_clips(self):
        """Rebuild the list, keeping the same clip selected (or the newest one if none was)."""
        self._clips = self.store.clips()
        self.clip_list.blockSignals(True)
        self.clip_list.clear()
        for _, ts, proc, text in self._clips:
            self.clip_list.addItem(f"{hhmm(ts)}  {proc}\n{text[:90].replace(chr(10), ' ')}")
        self.clip_list.blockSignals(False)
        ids = [c[0] for c in self._clips]
        row = ids.index(self._clip_sel_id) if self._clip_sel_id in ids else (0 if ids else -1)
        self.clip_list.setCurrentRow(row)
        self._show_clip(row)

    def _show_clip(self, row):
        if 0 <= row < len(self._clips):
            self._clip_sel_id = self._clips[row][0]
            self.clip_view.setPlainText(self._clips[row][3])
            # a copied error may already have its cause + fix, worked out in the background
            help_text = self.store.clip_help(self._clip_sel_id)
            if not self._clip_busy:
                self.clip_answer.setMarkdown(help_text or "")
        else:
            self._clip_sel_id = None
            self.clip_view.setPlainText("")
        self._sync_clip_buttons()

    def _sync_clip_buttons(self):
        has_clip = self._clip_sel_id is not None
        for btn in self.clip_ask_buttons:
            btn.setEnabled(has_clip and not self._clip_busy)
        self.clip_copy_btn.setEnabled(has_clip)
        self.clip_delete_btn.setEnabled(has_clip)

    def _clip_ask(self, prompt, label):
        text = self.clip_view.toPlainText()
        if not text:
            self.clip_answer.setPlainText("Select a clip on the left first.")
            return
        self._clip_busy = True
        self._sync_clip_buttons()
        self.clip_answer.setMarkdown(f"_{label}: asking the local model..._")

        def done(result):
            self._clip_busy = False
            self._sync_clip_buttons()
            self.clip_answer.setMarkdown(llm_error(result) if isinstance(result, Exception) else result)

        run_async(lambda: self.engine.llm.chat([
            {"role": "system", "content": SYSTEM_PERSONA},
            {"role": "user", "content": f"{prompt}\n\n{text[:12000]}"}]), done)

    def _clip_copy(self):
        from PySide6.QtGui import QGuiApplication
        text = self.clip_view.toPlainText()
        # tell the engine this copy came from Jarvis itself, so it isn't recorded as a new clip
        self.engine.last_clip = text.strip()
        QGuiApplication.clipboard().setText(text)
        self.clip_answer.setMarkdown("_Copied back to the clipboard._")

    def _clip_delete(self):
        row = self.clip_list.currentRow()
        if 0 <= row < len(self._clips):
            self.store.delete_clip(self._clips[row][0])
            self._clip_sel_id = None
            self._refresh_clips()

    # -----------------------------------------------------------------------
    # REMINDERS page
    # -----------------------------------------------------------------------
    def _build_reminders(self):
        w, lay = page("Reminders", "Pops up from the tray when due. Also works from chat: /remind 20 stretch")
        row = QHBoxLayout()
        self.rem_text = QLineEdit(placeholderText="Remind me to...")
        self.rem_min = QSpinBox(minimum=1, maximum=10080, value=30, suffix=" min")
        row.addWidget(self.rem_text, 1)
        row.addWidget(QLabel("in"))
        row.addWidget(self.rem_min)
        row.addWidget(QPushButton("Add", objectName="primary", clicked=self._add_reminder))
        lay.addLayout(row)
        self.rem_table = table(["Due", "Reminder", "State"])
        self.rem_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.rem_table.horizontalHeader().setStretchLastSection(False)
        lay.addWidget(self.rem_table, 1)
        # the delete button only lights up once a reminder row is selected
        self.rem_delete_btn = QPushButton("Delete selected", clicked=self._delete_reminder, enabled=False)
        self.rem_table.itemSelectionChanged.connect(
            lambda: self.rem_delete_btn.setEnabled(bool(self.rem_table.selectedItems())))
        lay.addWidget(self.rem_delete_btn, 0, Qt.AlignLeft)
        self._rems = []
        return w

    def _add_reminder(self):
        text = self.rem_text.text().strip()
        if text:
            self.store.add_reminder(time.time() + self.rem_min.value() * 60, text)
            self.rem_text.clear()
            self._refresh_reminders()

    def _refresh_reminders(self):
        self._rems = self.store.reminders()
        fill_table(self.rem_table, [(time.strftime("%a %H:%M", time.localtime(d)), t, "done" if done else "waiting")
                                    for _, d, t, done in self._rems])

    def _delete_reminder(self):
        row = self.rem_table.currentRow()
        if 0 <= row < len(self._rems):
            self.store.delete_reminder(self._rems[row][0])
            self._refresh_reminders()

    # -----------------------------------------------------------------------
    # JOURNAL page
    # -----------------------------------------------------------------------
    def _build_journal(self):
        w, lay = page("Journal", "Written by the local model from your activity every digest period.")
        row = QHBoxLayout()
        row.addWidget(QPushButton("Write entry now", clicked=self._journal_now))
        row.addWidget(QPushButton("What did I do today?", objectName="primary", clicked=self._ask_today))
        row.addWidget(QPushButton("Where did I leave off?", clicked=self._ask_resume))
        row.addStretch(1)
        lay.addLayout(row)
        self.journal_view = QTextBrowser()
        lay.addWidget(self.journal_view, 1)
        return w

    def _refresh_journal(self):
        parts = []
        for _, ts, a, b, text in self.store.journals(60):
            parts.append(f"### {time.strftime('%a %d %b', time.localtime(a))}, {hhmm(a)} - {hhmm(b)}\n\n{text}\n")
        self.journal_view.setMarkdown("\n---\n".join(parts) or "_No entries yet. The first arrives after one digest period._")

    def _journal_now(self):
        now = time.time()
        start = self.store.last_journal_end() or now - 3600
        self.journal_view.setMarkdown("_Writing..._")
        self.engine.write_journal(start, now)

    def _ask_journal(self, question, window_start):
        now = time.time()
        log = self.store.activity_digest_text(window_start, now)
        notes = "\n\n".join(t for *_, t in self.store.journals(12))
        self.journal_view.setMarkdown("_Thinking..._")
        run_async(lambda: self.engine.llm.chat([
            {"role": "system", "content": SYSTEM_PERSONA},
            {"role": "user", "content": f"Earlier journal notes:\n{notes[:6000]}\n\nActivity log:\n{log}\n\n{question}"}]),
            lambda r: self.journal_view.setMarkdown(llm_error(r) if isinstance(r, Exception) else r))

    def _ask_today(self):
        midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        self._ask_journal("Give me a short recap of my day so far: main themes, time split, and anything unfinished.",
                          midnight)

    def _ask_resume(self):
        self._ask_journal("Based on the most recent activity, what was I in the middle of, and what is the most "
                          "likely next step? Be specific about files, projects or pages.", time.time() - 6 * 3600)

    # -----------------------------------------------------------------------
    # MEMORY page
    # -----------------------------------------------------------------------
    def _build_memory(self):
        w, lay = page("Memory", "Saved facts and Jarvis's continuity settings. Also from chat: /remember <fact>")
        row = QHBoxLayout()
        self.mem_text = QLineEdit(placeholderText="e.g. My main project is AIWF Studio on F:\\")
        row.addWidget(self.mem_text, 1)
        row.addWidget(QPushButton("Remember", objectName="primary", clicked=self._add_fact))
        lay.addLayout(row)
        self.mem_list = QListWidget()
        lay.addWidget(self.mem_list, 1)
        # the forget button only lights up once a fact is selected
        self.mem_delete_btn = QPushButton("Forget selected", clicked=self._delete_fact, enabled=False)
        self.mem_list.itemSelectionChanged.connect(
            lambda: self.mem_delete_btn.setEnabled(bool(self.mem_list.selectedItems())))
        lay.addWidget(self.mem_delete_btn, 0, Qt.AlignLeft)
        candidate_title = QLabel("PENDING MEMORY / REVIEW BEFORE KEEPING", objectName="paneltitle")
        lay.addWidget(candidate_title)
        self.candidate_list = QListWidget()
        self.candidate_list.setMaximumHeight(110)
        self.candidate_list.setToolTip("Bonsai 8B suggestions from your own chat messages; none become memory without approval.")
        lay.addWidget(self.candidate_list)
        row = QHBoxLayout()
        row.addWidget(QPushButton("Keep selected", objectName="primary",
                                  clicked=lambda: self._resolve_candidate(True)))
        row.addWidget(QPushButton("Dismiss selected", clicked=lambda: self._resolve_candidate(False)))
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(QPushButton("Configure memory, personality & replies…",
                                  clicked=self._configure_behavior), 0, Qt.AlignLeft)
        self._facts = []
        return w

    def _refresh_memory(self):
        self._facts = self.store.facts()
        self.mem_list.clear()
        self.mem_list.addItems([f for _, f in self._facts])
        self._candidates = self.store.fact_candidates()
        self.candidate_list.clear()
        self.candidate_list.addItems([f"{fact}  ·  {reason}" for _, fact, reason in self._candidates])

    def _resolve_candidate(self, keep):
        row = self.candidate_list.currentRow()
        if 0 <= row < len(self._candidates):
            self.store.resolve_candidate(self._candidates[row][0], keep)
            self._refresh_memory()

    def _add_fact(self):
        if self.mem_text.text().strip():
            self.store.add_fact(self.mem_text.text().strip())
            self.mem_text.clear()
            self._refresh_memory()

    def _delete_fact(self):
        row = self.mem_list.currentRow()
        if 0 <= row < len(self._facts):
            self.store.delete_fact(self._facts[row][0])
            self._refresh_memory()

    def _configure_behavior(self):
        from .behavior_ui import BehaviorDialog
        dialog = BehaviorDialog(self.engine.cfg, self.store, self)
        if dialog.exec() == QDialog.Accepted:
            cfg = dict(self.engine.cfg)
            cfg.update(dialog.values())
            self.engine.save_config(cfg)
            self._refresh_today()

    # -----------------------------------------------------------------------
    # CHAT page
    # -----------------------------------------------------------------------
    def _build_chat(self):
        w, lay = page("Chat")
        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(True)
        lay.addWidget(self.chat_view, 1)
        row = QHBoxLayout()
        self.chat_ctx = QCheckBox("Use recent activity and local task requests", checked=True)
        row.addWidget(self.chat_ctx)
        row.addStretch(1)
        row.addWidget(QLabel("/status   /settings   /files   /handoff   /remember …", objectName="muted"))
        lay.addLayout(row)
        row = QHBoxLayout()
        self.chat_in = QLineEdit(placeholderText="Ask anything - e.g. 'what was that error I copied earlier?'")
        self.chat_in.returnPressed.connect(self._send_chat)
        self.chat_btn = QPushButton("Send", objectName="primary", clicked=self._send_chat)
        row.addWidget(self.chat_in, 1)
        row.addWidget(self.chat_btn)
        lay.addLayout(row)
        return w

    def _render_chat(self, pending=False, pending_text=""):
        """pending_text: your message, shown while Jarvis answers (the engine stores it for real).
        Each message is drawn in its own HUD frame (hud.render_chat)."""
        entries = list(self.store.chat_tail(40))
        if pending_text:
            entries.append(("user", pending_text))
        hud.render_chat(self.chat_view, entries, thinking=pending)

    def _send_chat(self):
        text = self.chat_in.text().strip()
        if not text:
            return
        self.chat_in.clear()

        # this is the slash-command section: handled locally, no model call
        if text.startswith("/remember "):
            self.store.add_fact(text[10:].strip())
            self.store.add_chat("assistant", f"Noted: {text[10:].strip()}")
            return self._render_chat()
        if text.startswith("/remind "):
            parts = text.split(maxsplit=2)
            if len(parts) == 3 and parts[1].isdigit():
                self.store.add_reminder(time.time() + int(parts[1]) * 60, parts[2])
                self.store.add_chat("assistant", f"I'll remind you in {parts[1]} min: {parts[2]}")
            else:
                self.store.add_chat("assistant", "Usage: /remind <minutes> <text>")
            return self._render_chat()
        if text == "/clear":
            self.store.clear_chat()
            return self._render_chat()

        # normal message: the engine stores it, asks the model and stores the reply (same path the phone uses)
        self.chat_btn.setEnabled(False)
        include = self.chat_ctx.isChecked()
        self._render_chat(pending=True, pending_text=text)

        def done(_result):
            self.chat_btn.setEnabled(True)
            self._render_chat()

        run_async(lambda: self.engine.chat_reply(text, include), done)

    # -----------------------------------------------------------------------
    # PROJECTS page: work Jarvis does on its own while you're away
    # -----------------------------------------------------------------------
    STATUS_LABEL = {"active": "active", "paused": "paused", "needs_input": "needs your input", "done": "done"}

    def _build_projects(self):
        w, lay = page("Projects", "Give Jarvis work to do while you're away. It reads your folder (read-only) and "
                                  "writes its results into its own workspace folder.")
        row = QHBoxLayout()
        row.addWidget(QPushButton("New project", objectName="primary", clicked=self._new_project))
        self.pj_work_btn = QPushButton("Work on it now", clicked=self._project_work_now)
        self.pj_pause_btn = QPushButton("Pause", clicked=self._project_toggle_pause)
        self.pj_done_btn = QPushButton("Mark done", clicked=lambda: self._project_set("done"))
        self.pj_open_btn = QPushButton("Open workspace", clicked=self._project_open_workspace)
        for b in (self.pj_work_btn, self.pj_pause_btn, self.pj_done_btn, self.pj_open_btn):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        split = QSplitter()
        self.pj_table = table(["Project", "Status", "Steps", "Last worked"])
        self.pj_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.pj_table.horizontalHeader().setStretchLastSection(False)
        self.pj_table.itemSelectionChanged.connect(self._show_project)
        split.addWidget(self.pj_table)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.pj_view = QTextBrowser()
        rl.addWidget(self.pj_view, 1)
        answer_row = QHBoxLayout()
        self.pj_answer = QLineEdit(placeholderText="Answer Jarvis's question...")
        self.pj_answer.returnPressed.connect(self._project_answer)
        self.pj_answer_btn = QPushButton("Send answer", clicked=self._project_answer)
        answer_row.addWidget(self.pj_answer, 1)
        answer_row.addWidget(self.pj_answer_btn)
        rl.addLayout(answer_row)
        split.addWidget(right)
        split.setSizes([420, 560])
        lay.addWidget(split, 1)
        self._projects = []
        self._pj_sel = None
        self._sync_project_buttons()
        return w

    def _refresh_projects(self):
        self._projects = self.store.projects()
        rows = [(p["title"], self.STATUS_LABEL.get(p["status"], p["status"])
                 + (" · working now" if self.engine.projects.current == p["id"] else ""),
                 p["steps"], popup.when_label(p["last_worked"]) if p["last_worked"] else "never")
                for p in self._projects]
        self.pj_table.blockSignals(True)
        fill_table(self.pj_table, rows, keys=[p["id"] for p in self._projects])
        self.pj_table.blockSignals(False)
        ids = [p["id"] for p in self._projects]
        if self._pj_sel in ids:
            self.pj_table.selectRow(ids.index(self._pj_sel))
        elif ids:
            self.pj_table.selectRow(0)
        self._show_project()

    def _selected_project(self):
        rows = self.pj_table.selectionModel().selectedRows() if self.pj_table.selectionModel() else []
        if not rows:
            return None
        item = self.pj_table.item(rows[0].row(), 0)
        return self.store.project(item.data(Qt.UserRole)) if item else None

    def _show_project(self):
        p = self._selected_project()
        self._pj_sel = p["id"] if p else None
        if not p:
            self.pj_view.setMarkdown("_No project selected. Use **New project** to give Jarvis something to work on "
                                     "while you're away._")
        else:
            log = []
            for ts, kind, text in self.store.project_log(p["id"], 60):
                log.append(f"- `{popup.when_label(ts)}` **{kind}** — {text}")
            question = ""
            if p["status"] == "needs_input":
                asked = [t for _, k, t in self.store.project_log(p["id"], 60) if k == "question"]
                question = f"> **Jarvis is asking:** {asked[-1] if asked else ''}\n\n"
            self.pj_view.setMarkdown(
                f"### {p['title']}\n\n{question}**Goal:** {p['goal']}\n\n"
                f"**Reads from:** {p['source_dir'] or '(no folder)'}  \n**Writes to:** {p['workspace']}\n\n"
                f"**Log**\n\n" + ("\n".join(log) or "_nothing yet_"))
        self._sync_project_buttons()

    def _sync_project_buttons(self):
        p = self._selected_project() if hasattr(self, "pj_table") else None
        has = p is not None
        for b in (self.pj_work_btn, self.pj_done_btn, self.pj_open_btn):
            b.setEnabled(has)
        self.pj_pause_btn.setEnabled(has and p["status"] != "done")
        self.pj_pause_btn.setText("Resume" if has and p["status"] in ("paused", "done") else "Pause")
        self.pj_work_btn.setEnabled(has and p["status"] != "done" and not self.engine.projects.busy)
        asking = has and p["status"] == "needs_input"
        self.pj_answer.setEnabled(asking)
        self.pj_answer_btn.setEnabled(asking)

    def _new_project(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("New project")
        dialog.setMinimumWidth(520)
        form = QFormLayout(dialog)
        title = QLineEdit(placeholderText="e.g. Tidy up the AIWF Studio README")
        goal = QPlainTextEdit()
        goal.setPlaceholderText("What should Jarvis produce? What does 'done' look like?")
        goal.setFixedHeight(120)
        folder = QLineEdit(placeholderText="optional - a folder Jarvis may READ")
        browse = QPushButton("Browse...", clicked=lambda: folder.setText(
            QFileDialog.getExistingDirectory(dialog, "Folder Jarvis may read") or folder.text()))
        folder_row = QHBoxLayout()
        folder_row.addWidget(folder, 1)
        folder_row.addWidget(browse)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Create")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow("Title", title)
        form.addRow("Goal", goal)
        form.addRow("Folder", folder_row)
        form.addRow(buttons)
        self._pj_dialog = (dialog, title, goal, folder)     # lets tests fill it in
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self._pj_sel = self.engine.create_project(title.text(), goal.toPlainText(), folder.text())
        except ValueError as exc:
            QMessageBox.warning(self, "New project", str(exc))
            return
        self._refresh_projects()

    def _project_set(self, status):
        p = self._selected_project()
        if p:
            self.engine.set_project_status(p["id"], status)

    def _project_toggle_pause(self):
        p = self._selected_project()
        if p:
            self._project_set("active" if p["status"] in ("paused", "done") else "paused")

    def _project_work_now(self):
        p = self._selected_project()
        if p:
            try:
                if not self.engine.work_on_project_now(p["id"]):
                    QMessageBox.information(self, "Projects", "Jarvis is already working on a project.")
            except ValueError as exc:
                QMessageBox.information(self, "Projects", str(exc))
            self._refresh_projects()

    def _project_answer(self):
        p = self._selected_project()
        text = self.pj_answer.text().strip()
        if p and text:
            self.engine.answer_project(p["id"], text)
            self.pj_answer.clear()

    def _project_open_workspace(self):
        p = self._selected_project()
        if p and p["workspace"]:
            subprocess.Popen(["explorer", p["workspace"]])

    # -----------------------------------------------------------------------
    # RECALL page: search everything Jarvis has kept, or ask a question about it
    # -----------------------------------------------------------------------
    def _build_recall(self):
        w, lay = page("Recall", "Search everything Jarvis has seen - windows, clipboard, journal, chat - "
                                "or ask a question about it.")
        row = QHBoxLayout()
        self.rc_query = QLineEdit(placeholderText="e.g.  llama.cpp   ·   or ask:  when did I last work on the AIWF installer?")
        self.rc_query.returnPressed.connect(self._recall_search)
        row.addWidget(self.rc_query, 1)
        row.addWidget(QPushButton("Search", clicked=self._recall_search))
        self.rc_ask_btn = QPushButton("Ask Jarvis", objectName="primary", clicked=self._recall_ask)
        row.addWidget(self.rc_ask_btn)
        lay.addLayout(row)
        split = QSplitter(Qt.Vertical)
        self.rc_table = table(["When", "Source", "Where", "What"])
        self.rc_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.rc_table.horizontalHeader().setStretchLastSection(False)
        self.rc_table.currentCellChanged.connect(lambda r, *_: self._recall_preview(r))
        split.addWidget(self.rc_table)
        self.rc_view = QTextBrowser()
        split.addWidget(self.rc_view)
        split.setSizes([380, 220])
        lay.addWidget(split, 1)
        self._rc_rows = []
        return w

    def _recall_search(self):
        query = self.rc_query.text().strip()
        if not query:
            return
        self._rc_rows = self.store.search(query)
        fill_table(self.rc_table, [
            (popup.when_label(ts), source, where,
             " ".join(text.split())[:160] + (f"   ({fmt_dur(extra)} total)" if extra else ""))
            for ts, source, where, text, extra in self._rc_rows])
        self.rc_view.setMarkdown(f"_{len(self._rc_rows)} matches for:_ " + ", ".join(
            f"`{k}`" for k in self.store.keywords(query)))

    def _recall_preview(self, row):
        if 0 <= row < len(self._rc_rows):
            ts, source, where, text, extra = self._rc_rows[row]
            self.rc_view.setPlainText(f"{source} · {where}\n\n{text}")

    def _recall_ask(self):
        query = self.rc_query.text().strip()
        if not query:
            return
        self._recall_search()
        matches = popup.memory_matches(self.engine, query, limit=40)
        if not matches:
            self.rc_view.setMarkdown("_Nothing in Jarvis's memory matches that. Try different words._")
            return
        self.rc_ask_btn.setEnabled(False)
        self.rc_view.setMarkdown("_Jarvis is looking through what it remembers..._")

        def done(result):
            self.rc_ask_btn.setEnabled(True)
            self.rc_view.setMarkdown(llm_error(result) if isinstance(result, Exception) else result)

        run_async(lambda: self.engine.llm.chat([
            {"role": "system", "content": SYSTEM_PERSONA},
            {"role": "user", "content":
                f"It is now {time.strftime('%A %H:%M')}. Records from Jarvis's memory that match "
                f"Shawn's question (best matches first; 'window' = a window he had open, with total time):\n"
                f"{matches}\n\nQuestion: {query}\n\nAnswer using ONLY these records, talking to him as 'you'. "
                "Quote the time label exactly as written in the record (e.g. 'yesterday 19:54') - don't "
                "convert it to a date. If the records don't answer it, say so."}],
            max_tokens=400), done)

    # -----------------------------------------------------------------------
    # SETTINGS page
    # -----------------------------------------------------------------------
    def _build_settings(self):
        w, outer = page("Settings", "Everything here is stored in data\\config.json next to the app.")
        # the form is taller than the window, so it lives in a scroll area
        scroll = QScrollArea(widgetResizable=True, frameShape=QFrame.NoFrame)
        inner = QWidget(objectName="page")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 12, 0)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        cfg = self.engine.cfg
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        self.s_checks = {}
        for key, label in (("watch_windows", "Track focused windows"),
                           ("watch_task_windows", "Track visible Claude/Codex window titles while unfocused"),
                           ("watch_clipboard", "Keep clipboard history"),
                           ("watch_folders", "Watch folders for new files"), ("watch_system", "Monitor CPU / RAM / GPU"),
                           ("welcome_back", "Welcome me back with what I was doing after a break"),
                           ("clipboard_error_help", "When I copy an error, work out the fix in the background"),
                           ("quick_ask_hotkey", "Ctrl+Alt+J opens the quick-ask bar from anywhere"),
                           ("keep_pc_awake", "Keep the PC awake while Jarvis runs (the screen can still turn off)"),
                           ("llm_autostart_server", "Start Jarvis's dedicated local model when the app opens"),
                           ("away_model_enabled", "While I'm away, switch to Bonsai 2 27B"),
                           ("away_free_comfyui", "...and ask an idle ComfyUI to unload its models to make room"),
                           ("projects_enabled", "While I'm away, work on my active projects")):
            self.s_checks[key] = QCheckBox(label, checked=bool(cfg[key]))
            form.addRow("", self.s_checks[key])
        self.s_spins = {}
        for key, label, lo, hi, suffix in (
                ("poll_seconds", "Sample every", 1, 60, " s"), ("idle_seconds", "Count as away after", 30, 3600, " s"),
                ("digest_minutes", "Journal entry every", 10, 1440, " min"),
                ("break_after_minutes", "Break nudge after", 10, 600, " min"),
                ("nudge_cooldown_minutes", "Min gap between nudges", 1, 600, " min"),
                ("ram_alert_percent", "RAM alert at", 50, 100, " %"), ("gpu_temp_alert_c", "GPU temp alert at", 50, 110, " °C"),
                ("welcome_back_minutes", "Welcome back after", 5, 480, " min away"),
                ("away_model_after_minutes", "Away mode after", 5, 240, " min away"),
                ("retention_days", "Keep raw history for", 1, 3650, " days")):
            self.s_spins[key] = QSpinBox(minimum=lo, maximum=hi, value=int(cfg[key]), suffix=suffix)
            form.addRow(label, self.s_spins[key])
        self.s_folders = QPlainTextEdit("\n".join(cfg["folders"]))
        self.s_folders.setFixedHeight(64)
        form.addRow("Watched folders (one per line)", self.s_folders)
        self.s_procs = QLineEdit(", ".join(cfg["excluded_processes"]))
        form.addRow("Never record these apps", self.s_procs)
        self.s_words = QLineEdit(", ".join(cfg["excluded_title_words"]))
        form.addRow("…or windows whose title contains", self.s_words)
        self.s_url = QLineEdit(cfg["llm_base_url"])
        form.addRow("Local model API", self.s_url)
        self.s_model = QLineEdit(cfg["llm_model"], placeholderText="blank = whatever the server has loaded")
        form.addRow("Model name", self.s_model)
        self.s_explain = QCheckBox("Explain whatever I Ctrl+click, anywhere on the PC",
                                   checked=bool(cfg["explain_on_click"]))
        form.addRow("", self.s_explain)
        self.s_trigger = QComboBox()
        self.s_trigger.addItem("Ctrl + click", "ctrl")
        self.s_trigger.addItem("Ctrl + Alt + click (if Ctrl+click clashes with an app)", "ctrl+alt")
        self.s_trigger.setCurrentIndex(max(0, self.s_trigger.findData(cfg["explain_trigger"])))
        form.addRow("Explain trigger", self.s_trigger)
        self.s_autostart = QCheckBox("Start Jarvis Assistant when Windows starts (in the tray)", checked=bool(cfg["autostart"]))
        form.addRow("", self.s_autostart)
        lay.addLayout(form)
        lay.addWidget(QPushButton("Configure memory, personality & replies…",
                                  clicked=self._configure_behavior), 0, Qt.AlignLeft)
        lay.addWidget(QPushButton("Save settings", objectName="primary", clicked=self._save_settings), 0, Qt.AlignLeft)
        lay.addStretch(1)
        return w

    def _load_settings_values(self):
        """Put the current settings back into the form (e.g. after the phone changed one)."""
        cfg = self.engine.cfg
        for key, box in self.s_checks.items():
            box.setChecked(bool(cfg[key]))
        for key, spin in self.s_spins.items():
            spin.setValue(int(cfg[key]))
        self.s_url.setText(cfg["llm_base_url"])
        self.s_model.setText(cfg["llm_model"])
        self.s_explain.setChecked(bool(cfg["explain_on_click"]))

    def _save_settings(self):
        cfg = dict(self.engine.cfg)
        for key, box in self.s_checks.items():
            cfg[key] = box.isChecked()
        for key, spin in self.s_spins.items():
            cfg[key] = spin.value()
        cfg["folders"] = [f.strip() for f in self.s_folders.toPlainText().splitlines() if f.strip()]
        cfg["excluded_processes"] = [p.strip().lower() for p in self.s_procs.text().split(",") if p.strip()]
        cfg["excluded_title_words"] = [p.strip().lower() for p in self.s_words.text().split(",") if p.strip()]
        cfg["llm_base_url"] = self.s_url.text().strip()
        cfg["llm_model"] = self.s_model.text().strip()
        cfg["explain_on_click"] = self.s_explain.isChecked()
        cfg["explain_trigger"] = self.s_trigger.currentData()
        if cfg["autostart"] != self.s_autostart.isChecked():
            cfg["autostart"] = self.s_autostart.isChecked()
            set_autostart(cfg["autostart"])
        self.engine.save_config(cfg)
        QMessageBox.information(self, "Jarvis Assistant", "Settings saved.")


# ===========================================================================
# Start-with-Windows: a shortcut in the user's Startup folder that launches
# pythonw (no console) with --hidden, so it comes up straight into the tray.
# ===========================================================================
def set_autostart(enabled: bool, startup_dir=None):
    """startup_dir is only passed by tests; normally it's the user's real Startup folder."""
    # the plain interpreter (not the venv's launcher), so Jarvis runs as a single process
    pythonw = str(Path(sys.base_prefix) / "pythonw.exe")
    script = str(config.APP_DIR / "jarvis_assistant.pyw")
    folder = f"'{startup_dir}'" if startup_dir else "[Environment]::GetFolderPath('Startup')"
    ps = (f"$d={folder}; $p=Join-Path $d 'Jarvis Assistant.lnk'; "
          + (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p); $s.TargetPath='{pythonw}'; "
             f"$s.Arguments='\"{script}\" --hidden'; $s.WorkingDirectory='{config.APP_DIR}'; "
             f"$s.IconLocation='{config.ICON_PATH}'; $s.Save()" if enabled else "Remove-Item $p -ErrorAction SilentlyContinue"))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=subprocess.CREATE_NO_WINDOW)


# ===========================================================================
# Tray icon: right-click menu to toggle watching, left-click to open the GUI
# ===========================================================================
class Tray(QSystemTrayIcon):
    def __init__(self, engine: Engine, window: MainWindow, app):
        super().__init__(make_icon(engine.watching))
        self.engine, self.window = engine, window
        menu = QMenu()
        menu.addAction("Open Jarvis Assistant", self.open_window)
        menu.addAction("Chat…", lambda: self.open_window("Chat"))
        menu.addSeparator()
        self.watch_action = QAction("Watching", menu, checkable=True, checked=engine.watching)
        self.watch_action.toggled.connect(lambda on: on != engine.watching and engine.set_watching(on))
        menu.addAction(self.watch_action)
        self.explain_action = QAction("Ctrl+click to explain", menu, checkable=True,
                                      checked=bool(engine.cfg["explain_on_click"]))
        self.explain_action.toggled.connect(self._toggle_explain)
        menu.addAction(self.explain_action)
        menu.addAction("Pause 30 min", lambda: engine.set_watching(False, 30))
        menu.addAction("Pause 2 hours", lambda: engine.set_watching(False, 120))
        menu.addSeparator()
        menu.addAction("Quit", app.quit)
        self.setContextMenu(menu)
        self._menu = menu  # keep a reference so Python doesn't garbage-collect it
        menu.setStyleSheet(window.styleSheet())   # the tray menu has no parent window, so give it the HUD sheet

        self.activated.connect(lambda reason: reason == QSystemTrayIcon.Trigger and self.open_window())
        self._click_action = None  # what clicking the most recent notification should open
        self.messageClicked.connect(self._notification_clicked)
        engine.watching_changed.connect(self._sync)
        engine.notify.connect(self._notify)
        engine.welcome_back.connect(self._welcome_back)
        engine.error_help.connect(self._error_help)
        self._sync(engine.watching)

    def _sync(self, on):
        # tray icon and taskbar/window icon both show the watching state
        self.setIcon(make_icon(on))
        self.window.setWindowIcon(make_icon(on))
        self.watch_action.blockSignals(True)
        self.watch_action.setChecked(on)
        self.watch_action.blockSignals(False)
        self.setToolTip("Jarvis Assistant - watching" if on else "Jarvis Assistant - paused")

    def _say(self, title, message, action=None):
        self._click_action = action
        self.showMessage(title, message, make_icon(True), 10000)

    def _notify(self, title, message):
        self._say(title, message)

    def _welcome_back(self, info):
        app = sensors.app_description(info["last_process"]) or info["last_process"]
        self._say("Welcome back", f"You were in {app}: {info['last_title'][:90]}\nClick for a quick recap.",
                  lambda: popup.welcome_card(self.window.card, self.engine, info))

    def _error_help(self, clip_id, headline):
        self._say("You copied an error", f"{headline}\nClick for the cause and fix.",
                  lambda: self.window.open_clip(clip_id))

    def _notification_clicked(self):
        action, self._click_action = self._click_action, None
        (action or self.open_window)()

    def _toggle_explain(self, on):
        cfg = dict(self.engine.cfg)
        cfg["explain_on_click"] = on
        self.engine.save_config(cfg)
        self.window.s_explain.setChecked(on)

    def open_window(self, page_name=None):
        if page_name:
            self.window.show_page(page_name)
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

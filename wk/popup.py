"""The floating info card, and the two features that use it:

  * Ctrl+click anywhere on the PC  -> "what is this thing under my pointer?"
  * click an app in Today / Timeline -> "what is this program and what did I do in it?"

The card appears next to the mouse, does not steal focus from the app you're
in, and closes on Esc, its x button, or any click outside it (like a tooltip
you can read). Answers come from the local model; the facts Jarvis already
knows (window info, times) are shown instantly while the model writes.
"""
import time

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPalette
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from . import config, hud, pointer, sensors
from .brain import SYSTEM_PERSONA, run_async

# ---------------------------------------------------------------------------
# HUD look for the floating card: navy glass painted by hud.HoloFrame, the title in
# the HUD face, the subtitle as small spaced capitals. The buttons get the same
# chamfered plates as the main window (added at runtime by _card_style()).
# ---------------------------------------------------------------------------
CARD_STYLE = f"""
* {{ font-family: 'Segoe UI'; font-size: 10pt; color: {hud.TEXT}; }}
QLabel#cardtitle {{ font-family: '{hud.UI_FONT_SEMIBOLD}'; font-size: 12.5pt; color: #eefcff; }}
QLabel#cardsub {{ font-family: '{hud.UI_FONT}'; font-size: 7.5pt; color: {hud.MUTED}; }}
QTextBrowser {{ background: transparent; border: none; color: {hud.TEXT}; }}
QScrollBar:vertical {{ background: transparent; width: 7px; }}
QScrollBar::handle {{ background: {hud.css_rgba(hud.CYAN, 0.35)}; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
"""
CLOSE_STYLE = f"""
QPushButton#x {{ border: none; border-image: none; background: transparent; font-size: 13pt; padding: 0 6px;
    color: {hud.MUTED}; }}
QPushButton#x:hover {{ color: {hud.CYAN_HI}; }}
"""


def _card_style(base):
    """The card/ask-bar sheet plus chamfered buttons (their images live in data/ui, drawn at startup)."""
    return base + hud.button_rules(hud.button_images(config.DATA_DIR / "ui")) + CLOSE_STYLE


def _cyan_links(widget):
    pal = widget.palette()
    pal.setColor(QPalette.Link, QColor(hud.CYAN))
    widget.setPalette(pal)


def _fmt(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


# ===========================================================================
# The card widget
# ===========================================================================
class InfoCard(QWidget):
    continue_in_chat = Signal(str, str)   # (what the user asked about, Jarvis's answer)
    visibility_changed = Signal(bool)     # lets the engine report outside clicks only while a card is up

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)  # don't pull focus away from the app you're using
        self.setStyleSheet(_card_style(CARD_STYLE))
        self.setFixedWidth(452)

        # the glass body (hud.HoloFrame keeps a 6 px margin inside itself for its glow)
        root = hud.HoloFrame(tag="JARVIS // ANALYSIS")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(22, 16, 16, 22)
        lay.setSpacing(6)

        # header: title + subtitle + close button
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title = QLabel(objectName="cardtitle")
        self.title.setWordWrap(True)
        self.sub = QLabel(objectName="cardsub")
        self.sub.setWordWrap(True)
        hud.caps(self.sub, 1.4)            # renders as spaced capitals; sub.text() is unchanged
        titles.addWidget(self.title)
        titles.addWidget(self.sub)
        head.addLayout(titles, 1)
        head.addWidget(QPushButton("×", objectName="x", clicked=self.hide), 0, Qt.AlignTop)
        lay.addLayout(head)

        # while the model works: a running light instead of a line of text
        self.thinking = hud.ThinkingBar("Analysing")
        lay.addWidget(self.thinking)

        # body: facts first, then Jarvis's answer underneath (hidden while there's nothing to show)
        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(True)
        _cyan_links(self.body)
        self.body.document().contentsChanged.connect(self._fit)
        lay.addWidget(self.body)

        # footer actions
        foot = QHBoxLayout()
        self.chat_btn = QPushButton("Continue in chat", clicked=self._to_chat)
        self.copy_btn = QPushButton("Copy", clicked=self._copy)
        for b in (self.chat_btn, self.copy_btn):
            hud.caps(b, 1.1)
        foot.addWidget(self.chat_btn)
        foot.addWidget(self.copy_btn)
        foot.addStretch(1)
        lay.addLayout(foot)

        # this is the effects section: a quick fade-in on open, a scan sweep when the answer lands
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(160)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)
        self._scan = hud.ScanOverlay(root)

        self.facts_md = ""
        self.answer_md = ""
        self.topic = ""
        self.request_id = 0   # bumps on every new question so a slow old answer can't overwrite a new card
        self._opened_at = 0.0

    def showEvent(self, event):
        self.visibility_changed.emit(True)
        super().showEvent(event)

    def hideEvent(self, event):
        self.visibility_changed.emit(False)
        super().hideEvent(event)

    # --- content --------------------------------------------------------------
    def open(self, title, subtitle, facts_md, near=None):
        self.request_id += 1
        self.title.setText(title)
        self.sub.setText(subtitle)
        self.facts_md, self.answer_md, self.topic = facts_md, "", title
        self._opened_at = time.monotonic()
        self._render(thinking=True)
        self._place(near or QCursor.pos())
        if not self.isVisible():
            # fade in from transparent (a card that is already up just changes its content)
            self._fade.stop()
            self.setWindowOpacity(0.0)
            self.show()
            self._fade.start()
        self.raise_()
        return self.request_id

    def set_facts(self, request_id, title=None, subtitle=None, facts_md=None):
        if request_id != self.request_id:
            return
        if title is not None:
            self.title.setText(title)
            self.topic = title
        if subtitle is not None:
            self.sub.setText(subtitle)
        if facts_md is not None:
            self.facts_md = facts_md
        self._render(thinking=not self.answer_md)

    def set_answer(self, request_id, answer_md):
        if request_id != self.request_id:
            return
        self.answer_md = answer_md
        self._render(thinking=False)
        if answer_md.strip():
            self._scan.play()             # the answer "renders in" with a scan sweep

    def _render(self, thinking):
        parts = [p for p in (self.facts_md, self.answer_md) if p and p.strip()]
        self.thinking.setVisible(thinking)
        self.body.setVisible(bool(parts))
        self.body.setMarkdown("\n\n---\n\n".join(parts))
        self.chat_btn.setEnabled(bool(self.answer_md))

    # --- sizing & placement: grow with the text, stay fully on the screen under the pointer ---
    def _fit(self):
        doc_h = int(self.body.document().size().height()) + 8
        self.body.setFixedHeight(max(60, min(doc_h, 420)))
        self.adjustSize()

    def resizeEvent(self, event):
        # Qt applies the new height a moment after adjustSize(), so the on-screen check has to
        # happen here, when the card has really changed size - not straight after asking for it
        super().resizeEvent(event)
        if self.isVisible():
            self._keep_on_screen()

    def _keep_on_screen(self):
        """The answer arrives after the card is placed and makes it taller: slide it up if needed."""
        screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        y = min(self.y(), area.bottom() - self.height() - 8)
        x = min(self.x(), area.right() - self.width() - 8)
        if (x, y) != (self.x(), self.y()):
            self.move(max(area.left() + 8, x), max(area.top() + 8, y))

    def _place(self, point: QPoint):
        self.adjustSize()
        screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(point.x() + 18, area.right() - self.width() - 8)
        y = point.y() + 18
        if y + self.height() > area.bottom() - 8:          # not enough room below: open above the pointer
            y = max(area.top() + 8, point.y() - self.height() - 18)
        self.move(max(area.left() + 8, x), y)

    # --- closing ------------------------------------------------------------------------
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)

    def click_elsewhere(self):
        """Called for every left click on the PC while the card is open: close if it was outside."""
        # ignore the click that opened this card (it can arrive just after the card appears)
        if time.monotonic() - self._opened_at < 0.4:
            return
        if self.isVisible() and not self.frameGeometry().contains(QCursor.pos()):
            self.hide()

    # --- actions ----------------------------------------------------------------------------
    def _copy(self):
        QGuiApplication.clipboard().setText(self.body.toPlainText().strip())

    def _to_chat(self):
        self.continue_in_chat.emit(f"{self.topic}\n\n{self.facts_md}", self.answer_md)
        self.hide()


# ===========================================================================
# Feature 1: Ctrl+click -> explain what's under the pointer
# ===========================================================================
def explain_at(card: InfoCard, engine, x, y):
    grabbed = pointer.grab(x, y)   # screenshot first, so Jarvis's own card is never in it
    # private windows (password managers etc.) are refused right here: the pixels are
    # dropped before any OCR or UI reading happens
    if engine.is_private(grabbed["process"], grabbed["title"]):
        request = card.open("Private window", grabbed["process"],
                            "That window is on your private list, so Jarvis didn't look at it.")
        card.set_answer(request, " ")
        return
    # place the card where the click happened (not wherever the mouse has moved to since)
    request = card.open("Looking...", "Reading what's under the pointer", "", near=pointer.to_logical(x, y))

    def seen_ready(seen):
        if isinstance(seen, Exception):
            card.set_facts(request, title="Couldn't read that spot", facts_md=f"`{seen}`")
            card.set_answer(request, " ")
            return
        uia = seen["uia"]
        name = uia.get("name") or (seen["text_under_pointer"][0] if seen["text_under_pointer"] else "")
        kind = pointer.friendly_type(uia.get("type"))
        app_name = sensors.app_description(seen["process"]) or seen["process"]
        title = name[:80] if name else f"Something in {app_name}"
        where = seen["title"][:60] if seen["title"] and seen["title"] != app_name else ""
        subtitle = " · ".join(p for p in (kind, app_name, where) if p)
        card.set_facts(request, title=title, subtitle=subtitle)
        engine.store.add_event("explain", f"Ctrl+click: {title} ({seen['process']})")
        engine.data_changed.emit("events")

        described = pointer.describe_for_model(seen)
        run_async(lambda: engine.llm.chat([
            {"role": "system", "content": SYSTEM_PERSONA},
            {"role": "user", "content":
                "Shawn Ctrl+clicked something on his screen to ask 'what is this?'. Here is what is there:\n"
                f"{described}\n\n"
                "Answer him directly, as 'you', in 2-4 short plain sentences, starting with \"That's ...\":\n"
                "- what this thing is and what it does or means;\n"
                "- if it's a button, link, tab or setting: what happens when he uses it;\n"
                "- if it's an error or warning: the likely cause and the fix;\n"
                "- if it's a word, name or number: what it refers to here.\n"
                "The thing under the pointer is the subject; nearby text is only context. Never mention "
                "'UI elements', control types, OCR or how you found this out, and don't guess why he clicked. "
                "If you're unsure, give the most likely answer and say it's a best guess."}], max_tokens=350),
            lambda r: card.set_answer(request, _answer_or_error(r)))

    run_async(lambda: pointer.look_at(grabbed), seen_ready)


# ===========================================================================
# Feature 2: click an app row -> what is this program + what did I do in it
# ===========================================================================
def explain_app(card: InfoCard, engine, process, start, end, label="today"):
    detail = engine.store.app_detail(process, start, end)
    friendly = sensors.app_description(process)
    private = process == "(private)"

    facts = [f"**{_fmt(detail['total'])}** {label} · {detail['sessions']} stretch"
             f"{'es' if detail['sessions'] != 1 else ''}"]
    if detail["first"]:
        facts[0] += (f" · first {time.strftime('%H:%M', time.localtime(detail['first']))}"
                     f" · last {time.strftime('%H:%M', time.localtime(detail['last']))}")
    if private:
        facts.append("Window titles are never recorded for private apps.")
    elif detail["windows"]:
        facts.append("**Top windows**\n\n" + "\n".join(
            f"- {t[:90] or '(untitled)'} — {_fmt(s)}" for t, s in detail["windows"][:8]))
    facts_md = "\n\n".join(facts)
    title = friendly or process
    request = card.open(title, process if friendly else "program", facts_md)
    if private:
        card.set_answer(request, " ")
        return

    windows = "\n".join(f"- {t[:120]} ({_fmt(s)})" for t, s in detail["windows"][:25])
    run_async(lambda: engine.llm.chat([
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content":
            f"Program: {process} ({friendly or 'no description'}). Time used {label}: {_fmt(detail['total'])} "
            f"over {detail['sessions']} stretches.\nWindow titles seen, with time:\n{windows or '(none)'}\n\n"
            "In 2-4 sentences: say what this program is (one short clause), then what Shawn mainly used it "
            "for based on the window titles - name the specific projects, files, sites or topics. "
            "Don't list every title back."}], max_tokens=300),
        lambda r: card.set_answer(request, _answer_or_error(r)))


def _answer_or_error(result):
    if isinstance(result, Exception):
        return f"_Local model unreachable ({result}). It may still be starting - try again in a few seconds._"
    return result or "_(no answer)_"


# ===========================================================================
# Shared: what Jarvis remembers that matches a question (used by quick-ask + Recall)
# ===========================================================================
def when_label(ts):
    """'today 14:02', 'yesterday 19:54', 'Monday 09:10', 'Mon 14 Sep 09:10' - worked out here,
    because small models get day-of-week arithmetic wrong if handed raw dates."""
    if not ts:
        return "?"
    t = time.localtime(ts)
    days = (time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
            - time.mktime(time.strptime(time.strftime("%Y-%m-%d", t), "%Y-%m-%d"))) / 86400
    clock = time.strftime("%H:%M", t)
    if days < 1:
        return f"today {clock}"
    if days < 2:
        return f"yesterday {clock}"
    if days < 7:
        return f"{time.strftime('%A', t)} {clock}"
    return time.strftime("%a %d %b %H:%M", t)


def memory_matches(engine, question, limit=20):
    lines = []
    for ts, source, where, text, extra in engine.store.search(question, limit=limit):
        when = when_label(ts)
        spent = f" ({_fmt(extra)} total)" if extra else ""
        lines.append(f"- {when} [{source}{': ' + where if where else ''}] {' '.join(text.split())[:220]}{spent}")
    return "\n".join(lines)


# ===========================================================================
# Feature 3: quick-ask bar (Ctrl+Alt+J from anywhere)
# ===========================================================================
# the quick-ask bar is a HUD command line: glass frame, a cyan prompt chevron, spaced-caps context
ASK_STYLE = f"""
* {{ font-family: 'Segoe UI'; color: {hud.TEXT}; }}
QLabel#askctx {{ font-family: '{hud.UI_FONT}'; font-size: 7.5pt; color: {hud.MUTED}; }}
QLabel#askprompt {{ font-family: '{hud.UI_FONT_SEMIBOLD}'; font-size: 17pt; color: {hud.CYAN}; }}
QLineEdit {{ background: transparent; border: none; font-size: 14pt; color: #f2fbff; padding: 4px 2px;
    selection-background-color: {hud.css_rgba(hud.CYAN, 0.30)}; }}
"""


class AskBar(QWidget):
    def __init__(self, engine, card: InfoCard):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        from PySide6.QtWidgets import QLineEdit
        self.engine, self.card = engine, card
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(ASK_STYLE)
        self.setFixedWidth(660)
        root = hud.HoloFrame(tag="JARVIS // QUICK ASK")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(24, 14, 22, 22)
        lay.setSpacing(2)
        self.ctx_label = QLabel(objectName="askctx")
        self.ctx_label.setWordWrap(True)
        hud.caps(self.ctx_label, 1.6)
        self.line = QLineEdit(placeholderText="Ask Jarvis anything  ·  Enter to ask  ·  Esc to close")
        self.line.returnPressed.connect(self._ask)
        lay.addWidget(self.ctx_label)
        # this is the command-line row: a chevron prompt, then the input
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("›", objectName="askprompt"))
        row.addWidget(self.line, 1)
        lay.addLayout(row)
        self.context = {}
        self.selection = None
        self._was_active = False

    def changeEvent(self, event):
        # like a launcher bar: once it has had focus, clicking anywhere else closes it
        if event.type() == QEvent.ActivationChange and self.isVisible():
            if self.isActiveWindow():
                self._was_active = True
            elif self._was_active:
                self.hide()
        super().changeEvent(event)

    def summon(self):
        # 1) note where you are BEFORE taking focus (after that, the focused app would be Jarvis)
        process, title = sensors.foreground_window()
        self.selection = pointer.SelectionReader()
        self.selection.captured.wait(0.25)   # the focused control is located in a few ms
        private = self.engine.is_private(process, title)
        app = sensors.app_description(process) or process
        self.context = {"process": process, "title": "" if private else title, "app": app, "private": private}
        where = "a private window" if private else f"{app}" + (f" · {title[:70]}" if title and title != app else "")
        self.ctx_label.setText(f"In {where}")
        # 2) show the bar a little above the middle of the screen you're on, and take the keyboard
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

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)

    def _ask(self):
        question = self.line.text().strip()
        if not question:
            return
        ctx = self.context
        self.selection.done.wait(1.0)
        selected = "" if ctx["private"] else self.selection.text
        below = QPoint(self.x() + 90, self.y() + self.height() - 10)
        self.hide()
        facts = f"_Selected text:_ {selected[:300]}{'…' if len(selected) > 300 else ''}" if selected else ""
        request = self.card.open(question, f"Asked in {ctx['app'] if not ctx['private'] else 'a private window'}",
                                 facts, near=below)
        engine = self.engine
        prompt = (f"{engine.context_block(20)}\n\n"
                  f"He pressed the quick-ask hotkey while in {ctx['app']}"
                  + (f" (window: {ctx['title'][:150]})" if ctx["title"] else "") + ".\n"
                  + (f"Text he had selected:\n\"\"\"\n{selected}\n\"\"\"\n" if selected else "")
                  + (f"Things you remember that may be relevant:\n{memory_matches(engine, question)}\n"
                     if engine.store.search(question, limit=1) else "")
                  + f"\nHis question: {question}\n\n"
                  "Answer directly and briefly. If the question is about the selected text or the current "
                  "window, use them. Only use remembered items if they actually help.")
        run_async(lambda: engine.llm.chat([{"role": "system", "content": SYSTEM_PERSONA},
                                           {"role": "user", "content": prompt}], max_tokens=600),
                  lambda r: self.card.set_answer(request, _answer_or_error(r)))


# ===========================================================================
# Feature 4: welcome back -> what you were in the middle of
# ===========================================================================
def welcome_card(card: InfoCard, engine, info):
    app = sensors.app_description(info["last_process"]) or info["last_process"]
    recent = "\n".join(f"- {t[:90]} — {_fmt(s)}" for _, t, s in info["recent"][:5])
    left = time.strftime("%H:%M", time.localtime(info["left_at"]))
    facts = f"You left at **{left}**, in **{app}**:\n\n{info['last_title'][:120]}\n\n**Just before that**\n\n{recent}"
    request = card.open("Welcome back", f"Away for {info['away_text']}", facts)
    log = "\n".join(f"- [{p}] {t[:150]} ({_fmt(s)})" for p, t, s in info["recent"])
    run_async(lambda: engine.llm.chat([
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content":
            f"Shawn just came back to his PC after {info['away_text']} away. The windows he used in the "
            f"45 minutes before leaving, most recent first:\n{log}\n\n"
            "In 2-3 sentences, remind him what he was in the middle of and suggest the most likely next step. "
            "Be specific (project, file, page)."}], max_tokens=250),
        lambda r: card.set_answer(request, _answer_or_error(r)))

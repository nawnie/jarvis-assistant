"""Memory, personality, and reply controls opened from Jarvis's Memory page."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QGroupBox, QLabel, QPlainTextEdit,
                               QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from . import behavior, config


class BehaviorDialog(QDialog):
    def __init__(self, cfg, store=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("J.A.R.V.I.S. / Memory Core")
        self.resize(850, 700)
        self.cfg = cfg
        self.controls = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        title = QLabel("MEMORY CORE", objectName="memoryTitle")
        outer.addWidget(title)
        subtitle = QLabel("Decide what Jarvis observes, keeps, recalls, and carries into independent work.",
                          objectName="memorySub")
        outer.addWidget(subtitle)
        if store is not None:
            facts = store.facts()
            projects = store.projects(include_done=False)
            pending = sum(not reminder[3] for reminder in store.reminders())
            readout = QLabel(f"{len(facts)} ANCHORS   /   {len(projects)} OPEN OBJECTIVES   /   {pending} REMINDERS",
                             objectName="memoryReadout")
            outer.addWidget(readout)

        tabs = QTabWidget()
        outer.addWidget(tabs, 1)

        keep = self._pane(tabs, "01  KEEP")
        anchors = self._group(keep, "Anchors", "Choose which saved facts become long-term context.")
        self._choice(anchors, "memory_fact_mode", "Recall policy", [
            ("All anchors", "all"), ("Mission + question", "mission"),
            ("Relevant + recent", "related"), ("Off in chat", "off")])
        self._spin(anchors, "memory_fact_limit", "Fact cap", 1, 100)
        self._choice(anchors, "memory_capture_mode", "New facts", [
            ("Suggest, then I approve", "suggest"), ("Only when I save one", "off")])
        note = QLabel("8B may suggest one durable fact from a chat message. It stays in a review queue until you keep it; candidates never enter replies or away work.")
        note.setWordWrap(True)
        anchors.addRow(note)
        continuity = self._group(keep, "Continuity", "Keep unfinished work visible across conversations.")
        self._check(continuity, "memory_open_loops", "Open objectives and reminders")
        self._check(continuity, "memory_project_updates", "Latest project progress")
        self._check(continuity, "memory_journal_recall", "Relevant journal hints")
        if store is not None:
            preview = self._group(keep, "In the vault now", "Local preview; this list is never uploaded by Jarvis.")
            sample = [f"• {fact[:100]}" for _, fact in facts[-5:]]
            sample += [f"◇ {project['title'][:100]} [{project['status']}]" for project in projects[:4]]
            label = QLabel("\n".join(sample) or "No saved anchors or open objectives yet.", objectName="memoryPreview")
            label.setWordWrap(True)
            preview.addRow(label)
        keep.addStretch(1)

        sense = self._pane(tabs, "02  SENSE")
        intake = self._group(sense, "Context window", "How much short-lived context Jarvis may use in a reply.")
        self._spin(intake, "memory_chat_messages", "Chat messages", 4, 40)
        self._spin(intake, "memory_activity_minutes", "Activity lookback", 5, 240, " min")
        self._spin(intake, "memory_clipboard_items", "Clipboard items", 0, 5)
        self._spin(intake, "retention_days", "Raw-history retention", 1, 3650, " days")
        note = QLabel("Changing a lookback changes future context. Raw activity, clips, and events age out on the retention schedule.")
        note.setWordWrap(True)
        intake.addRow(note)
        sense.addStretch(1)

        work = self._pane(tabs, "03  ACT")
        sources = self._group(work, "Task signals", "Observed requests are clues, never proof that a task is unfinished.")
        self._check(sources, "read_local_task_prompts", "Read local Codex + Claude Code requests")
        self._spin(sources, "memory_task_hours", "Task lookback", 1, 72, " hours")
        self._check(sources, "project_task_context", "Match clues to an existing away objective")
        note = QLabel("Jarvis only feeds a clue to an existing project when its name or goal matches. Claude Desktop chat and drafts are not connected. Source folders stay read-only; work stays inside the project's workspace.")
        note.setWordWrap(True)
        sources.addRow(note)
        routing = self._group(work, "AI consultations", "Bonsai 8B decides when a second model would help answer Shawn.")
        self._check(routing, "tool_use_8b", "Let 8B consult installed Claude or Codex")
        self._spin(routing, "tool_daily_limit", "Daily consultation cap", 0, 10)
        note = QLabel("Only the current question goes to one selected CLI. Claude and Codex may use their configured accounts. A cap of zero disables consultations. /tools shows installed links.")
        note.setWordWrap(True)
        routing.addRow(note)
        resources = self._group(work, "Resource control", "Shawn can inspect RAM consumers with /processes.")
        note = QLabel("/stop requires the exact PID and process identity from that list. Jarvis does not stop a process from a model suggestion.")
        note.setWordWrap(True)
        resources.addRow(note)
        work.addStretch(1)

        voice = self._pane(tabs, "04  VOICE")
        identity = self._group(voice, "Identity", "A standing purpose that survives app and model changes.")
        self.mission = QPlainTextEdit(str(cfg.get("assistant_mission", config.DEFAULTS["assistant_mission"]))[:400])
        self.mission.setFixedHeight(72)
        identity.addRow("Standing mission", self.mission)
        self._choice(identity, "personality_mode", "Voice", [
            ("Calm operator", "operator"), ("Warm companion", "companion"),
            ("Mission control", "mission"), ("Evidence analyst", "analyst")])
        self.note = QPlainTextEdit(str(cfg.get("personality_note", ""))[:300])
        self.note.setPlaceholderText("Your own style note, e.g. 'Call me Shawn; keep briefings plain.'")
        self.note.setFixedHeight(68)
        identity.addRow("Personal style note", self.note)

        replies = self._group(voice, "Replies", "How much Jarvis says after it has checked what it knows.")
        self._choice(replies, "reply_depth", "Answer depth", [
            ("Quick signal", "quick"), ("Balanced", "balanced"), ("Full debrief", "detailed")])
        self._check(replies, "reply_next_step", "Next step")
        note = QLabel("Observations and guesses must stay distinct, whatever the voice setting.")
        note.setWordWrap(True)
        note.setMaximumWidth(560)
        replies.addRow(note)
        voice.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.setStyleSheet("""
            QDialog { background: #060b12; color: #d8f4ff; font-family: 'Segoe UI'; }
            QLabel { color: #d8f4ff; }
            QLabel#memoryTitle { color: #b4f4ff; font: 22pt 'Bahnschrift'; letter-spacing: 4px; }
            QLabel#memorySub { color: #7fa6b8; margin-bottom: 5px; }
            QLabel#memoryReadout { color: #38d6ff; background: #0a1520; border: 1px solid #286179;
                                   padding: 9px; font: 10pt 'Cascadia Mono'; }
            QLabel#memoryPreview { color: #b4f4ff; padding: 6px; }
            QTabWidget::pane { border: 1px solid #286179; background: #08131d; }
            QTabBar::tab { color: #7fa6b8; background: #0a1520; border: 1px solid #24475b;
                           padding: 10px 20px; font: 10pt 'Bahnschrift'; }
            QTabBar::tab:selected { color: #b4f4ff; background: #123143; border-bottom: 2px solid #38d6ff; }
            QGroupBox { color: #38d6ff; background: #0a1520; border: 1px solid #286179;
                        margin-top: 13px; padding: 13px; font: 11pt 'Bahnschrift'; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
            QComboBox, QSpinBox, QPlainTextEdit { color: #d8f4ff; background: #0b1c29;
                border: 1px solid #286179; padding: 5px; selection-background-color: #196482; }
            QCheckBox { color: #d8f4ff; spacing: 9px; }
            QScrollArea { border: none; background: #08131d; }
            QPushButton { color: #b4f4ff; background: #0a2635; border: 1px solid #38d6ff;
                          padding: 7px 18px; font: 10pt 'Bahnschrift'; }
            QPushButton:hover { background: #16435a; }
        """)

    @staticmethod
    def _pane(tabs, title):
        scroll = QScrollArea(widgetResizable=True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(16, 18, 16, 18)
        layout.setSpacing(15)
        scroll.setWidget(body)
        tabs.addTab(scroll, title)
        return layout

    @staticmethod
    def _group(layout, title, description):
        box = QGroupBox(title)
        form = QFormLayout(box)
        note = QLabel(description)
        note.setWordWrap(True)
        note.setMaximumWidth(560)
        form.addRow(note)
        layout.addWidget(box)
        return form

    def _choice(self, form, key, label, options):
        box = QComboBox()
        for title, value in options:
            box.addItem(title, value)
        box.setMaximumWidth(300)
        current = behavior.choice(self.cfg, key, {value for _, value in options})
        box.setCurrentIndex(max(0, box.findData(current)))
        form.addRow(label, box)
        self.controls[key] = box

    def _spin(self, form, key, label, low, high, suffix=""):
        box = QSpinBox(minimum=low, maximum=high,
                       value=behavior.bounded_int(self.cfg, key, low, high), suffix=suffix)
        form.addRow(label, box)
        self.controls[key] = box

    def _check(self, form, key, label):
        box = QCheckBox(label, checked=bool(self.cfg.get(key, config.DEFAULTS[key])))
        hints = {
            "memory_open_loops": "Include active Jarvis projects and pending reminders in chat memory.",
            "memory_project_updates": "Include recent project progress in chat continuity.",
            "memory_journal_recall": "Bring matching recent journal summaries into chat, labeled as model-written hints.",
            "read_local_task_prompts": "Read recent user requests from local session files only while Watching and chat context are on.",
            "project_task_context": "Give the away worker only recent task hints that match its existing project goal; no new file access.",
            "tool_use_8b": "Bonsai 8B may select one bounded Claude or Codex CLI consultation per reply. The current question is sent to the selected provider.",
            "reply_next_step": "End with one concrete next step when it helps answer the request.",
        }
        box.setToolTip(hints.get(key, ""))
        form.addRow("", box)
        self.controls[key] = box

    def values(self):
        result = {}
        for key, control in self.controls.items():
            if isinstance(control, QComboBox):
                result[key] = control.currentData()
            elif isinstance(control, QSpinBox):
                result[key] = control.value()
            else:
                result[key] = control.isChecked()
        result["assistant_mission"] = self.mission.toPlainText().strip()[:400] or config.DEFAULTS["assistant_mission"]
        result["personality_note"] = self.note.toPlainText().strip()[:300]
        return result

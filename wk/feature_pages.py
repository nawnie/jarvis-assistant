"""The newer features, wired into Jarvis: GPU / Actions / Games pages, and the hub that runs them.

  FeatureHub      one per Jarvis. Owns the render watcher, the wrap-up clock, the extra global
                  hotkeys (snip, game help, autoplay start + kill) and the running autoplay
                  session. Also answers the extra chat commands (/gpu /actions /do /fix /wrapup
                  /autoplay) and quick actions typed into the Ctrl+Alt+J bar.
  build_*_page    the three sidebar pages (MainWindow calls these)
  GameHelpBar     the in-game Ctrl+Alt+H bar: ask about the screen, hints, crash diagnosis
  NarrationStrip  click-through caption at the top of the screen while Jarvis is playing

Settings are read with defaults (engine.cfg.get), so none of this needs new config.py keys:
  snip_hotkey ctrl+alt+s · game_help_hotkey ctrl+alt+h (ctrl+alt+g is taken by another app on this PC) · autoplay_hotkey ctrl+alt+p ·
  autoplay_kill_hotkey ctrl+alt+end · feature_hotkeys (defaults to quick_ask_hotkey) ·
  render_watch True · wrapup_enabled True · wrapup_time "21:30" ·
  autoplay_vision_url "http://127.0.0.1:8080/v1" · autoplay_vision_model "" · autoplay_vision_key_file ""
"""
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import psutil
from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QMessageBox, QProgressBar, QPushButton, QScrollArea, QTextBrowser,
                               QVBoxLayout, QWidget)

from . import actions, config, document_recall, game_log, gpu_traffic, guardian, hotkeys, hud, popup, pointer, routines, screenshot_recall, sensors, snip, wrapup
from .brain import SYSTEM_PERSONA, run_async
from .games import catalog, diagnose, help as game_help, presence
from .games.autoplay import guard as ap_guard
from .games.autoplay import inputs as ap_inputs
from .games.autoplay import policy as ap_policy
from .games.autoplay.session import AutoplaySession

VISION_CHOICE = "Vision model (plays toward the goal below)"


def _now_hhmm():
    return time.strftime("%H:%M")


# ===========================================================================
# The hub
# ===========================================================================
class FeatureHub(QObject):
    gpu_ready = Signal(dict)
    render_done = Signal(str, object)             # (ComfyUI base, RenderResult)
    render_thumb = Signal(object, QImage)          # (RenderResult, thumbnail)
    autoplay_status = Signal(str, str)             # (kind, text) from the session thread
    hotkey = Signal(str)                            # which feature hotkey fired (hotkey thread -> GUI)
    notice = Signal(str)                            # a line for the hotkey/feature error log

    def __init__(self, engine, window):
        super().__init__(window)
        self.engine, self.window = engine, window
        self.tray = None
        self.holders = {}                  # keeps snip overlay / bars alive between signals
        self.game_bar = None
        self.session = None
        self.narration = None
        self.gpu_snapshot = None
        self._gpu_busy = False
        self._render_busy = False
        self.render_watch = gpu_traffic.RenderWatcher(engine.cfg)
        self.wrap_clock = wrapup.WrapupClock(engine.cfg)
        self.action_list, self.action_problems = actions.load()
        self.routine_clock = routines.RoutineClock()
        self.screenshot_watcher = screenshot_recall.FolderWatcher()
        self._screens_pending = []
        self._screens_busy = False
        self.guardian = guardian.Guardian()
        self._guardian_busy = False

        # this is the signal wiring section (worker/hotkey threads -> GUI thread)
        self.render_done.connect(self._on_render)
        self.render_thumb.connect(self._on_render_thumb)
        self.autoplay_status.connect(self._on_autoplay_status)
        self.hotkey.connect(self._on_hotkey)
        self.notice.connect(lambda text: self._event("error", text))

        # this is the timer section: renders every 5 s (cheap HTTP), wrap-up check every minute
        self.t_render = QTimer(self, timeout=self._poll_renders, interval=5_000)
        self.t_wrapup = QTimer(self, timeout=self._check_wrapup, interval=60_000)
        self.t_routines = QTimer(self, timeout=self._poll_routines, interval=3_000)
        self.t_screens = QTimer(self, timeout=self._poll_screenshots, interval=15_000)
        self.t_guardian = QTimer(self, timeout=self._poll_guardian, interval=30 * 60_000)
        self.t_render.start()
        self.t_wrapup.start()
        self.t_routines.start()
        self.t_screens.start()
        self.t_guardian.start()
        QTimer.singleShot(60_000, self._poll_guardian)

        # the chat commands and ask-bar actions reach this hub through two engine attributes
        engine.command_hook = self.command
        engine.ask_hook = self.ask_hook
        self.hotkey_thread = None
        self.start_hotkeys()
        app = QGuiApplication.instance()
        if app:
            app.aboutToQuit.connect(self.shutdown)     # quitting mid-run must still let go of held keys

    def shutdown(self):
        if self.hotkey_thread:
            self.hotkey_thread.stop()
        if self.session and self.session.is_alive():
            self.session.stop("Jarvis is closing")
            self.session.join(1.0)                    # its `finally` releases every held key

    # --- small helpers -------------------------------------------------------------------
    @property
    def cfg(self):
        return self.engine.cfg          # always the live settings (Settings replaces the dict on save)

    def _event(self, kind, text):
        self.engine.store.add_event(kind, text)
        self.engine.data_changed.emit("events")

    def toast(self, title, message, on_click=None, icon=None):
        if self.tray:
            self.tray._click_action = on_click
            self.tray.showMessage(title, message, icon or self.tray.icon(), 10000)
        else:
            self.engine.notify.emit(title, message)

    def _poll_routines(self):
        """Run only saved rules after a focus change and record each real outcome."""
        process, _ = sensors.foreground_window()
        try:
            due = self.routine_clock.due(process)
        except (OSError, ValueError) as exc:
            self._event("error", f"Routines: {exc}")
            return
        for action_id in due:
            action = next((item for item in self.action_list if item.id == action_id and not item.confirm), None)
            if action is None:
                self._event("error", f"Routine skipped: action {action_id} is missing or asks first")
                continue
            try:
                outcome = actions.run(action, self.builtins(), self.action_values())
                self._event("action", f"Routine {process} → {action_id}: {outcome[:160]}")
            except (OSError, ValueError) as exc:
                self._event("error", f"Routine {process} → {action_id}: {exc}")

    def _poll_screenshots(self):
        """Index newly saved screenshots one at a time after an explicit watch command."""
        self._screens_pending.extend(self.screenshot_watcher.poll())
        if self._screens_busy or not self._screens_pending:
            return
        path = self._screens_pending.pop(0)
        self._screens_busy = True

        def done(result):
            self._screens_busy = False
            if isinstance(result, Exception):
                self._event("error", f"Screenshot indexing: {result}")
            else:
                self._event("file", result)
            if self._screens_pending:
                QTimer.singleShot(0, self._poll_screenshots)

        run_async(lambda: screenshot_recall.index_file(self.window.recall, self.window.eyes, path), done)

    def _poll_guardian(self):
        """Read PC health in a worker; make no repair or process-stop decision."""
        if self._guardian_busy:
            return
        self._guardian_busy = True

        def done(result):
            self._guardian_busy = False
            if isinstance(result, Exception):
                self._event("error", f"PC guardian check: {result}")
                return
            for warning in result:
                self._event("system", warning)
                self.toast("PC guardian", warning)

        run_async(self.guardian.check, done)

    # --- hotkeys ------------------------------------------------------------------------------
    def hotkey_combos(self):
        return {"snip": self.cfg.get("snip_hotkey", "ctrl+alt+s"),
                "game": self.cfg.get("game_help_hotkey", "ctrl+alt+h"),
                "translate": self.cfg.get("translate_hotkey", "ctrl+alt+t"),
                "autoplay": self.cfg.get("autoplay_hotkey", "ctrl+alt+p"),
                "kill": self.cfg.get("autoplay_kill_hotkey", "ctrl+alt+end")}

    def start_hotkeys(self):
        # test copies of Jarvis switch quick_ask_hotkey off; they must never hold global keys either
        if not self.cfg.get("feature_hotkeys", self.cfg.get("quick_ask_hotkey", True)):
            return
        combos = self.hotkey_combos()
        bindings = {combos["snip"]: lambda: self.hotkey.emit("snip"),
                    combos["game"]: lambda: self.hotkey.emit("game"),
                    combos["translate"]: lambda: self.hotkey.emit("translate"),
                    combos["autoplay"]: lambda: self.hotkey.emit("autoplay"),
                    # the kill switch acts right here on the hotkey thread - no waiting for the GUI
                    combos["kill"]: self.kill_autoplay}
        self.hotkey_thread = hotkeys.HotkeyThread(bindings, on_error=self.notice.emit)
        self.hotkey_thread.start()

    def _on_hotkey(self, which):
        if which == "snip":
            self.start_snip()
        elif which == "translate":
            # Capture the active screen before the ask bar takes focus; Shawn presses Enter to send it.
            bar = self.window.askbar
            bar.summon()
            bar.screen_mode = "on"
            bar._show_screen_mode()
            bar.line.setText("Translate the visible text into English. Quote the original text when legible.")
        elif which == "game":
            self.open_game_help()
        elif which == "autoplay":
            process, _ = sensors.foreground_window()
            page = getattr(self.window, "games_page", None)
            if page:
                page.start_autoplay(target=process)

    # --- snip & ask ---------------------------------------------------------------------------
    def start_snip(self):
        snip.start(self.engine, self.window.card, self.holders)

    # --- game help bar ----------------------------------------------------------------------------
    def open_game_help(self):
        if self.game_bar is None:
            self.game_bar = GameHelpBar(self.engine, self.window.card)
        self.game_bar.summon()

    # --- ComfyUI render watcher -------------------------------------------------------------------
    def _poll_renders(self):
        if not self.cfg.get("render_watch", True) or self._render_busy:
            return
        self._render_busy = True
        self.render_watch.cfg = self.cfg

        def work():
            try:
                for base, result in self.render_watch.poll():
                    self.render_done.emit(base, result)
            finally:
                self._render_busy = False
        threading.Thread(target=work, daemon=True, name="jarvis-render-watch").start()

    def _on_render(self, base, result):
        folder = self.render_watch.output_dir(base)
        took = f" in {result.seconds:.0f} s" if result.seconds else ""
        if result.ok:
            names = ", ".join(o["filename"] for o in result.outputs[:3]) or "no files"
            self._event("render", f"Finished{took}: {len(result.outputs)} file(s) - {names}")
            first = result.outputs[0] if result.outputs else None
            path = os.path.join(folder, first.get("subfolder", ""), first["filename"]) if first else folder

            def open_it():
                if os.path.exists(path) and os.path.isfile(path):
                    subprocess.Popen(["explorer", f"/select,{path}"])
                elif os.path.isdir(folder):
                    os.startfile(folder)
            self._render_open = open_it
            self.toast("Render finished", f"{len(result.outputs)} file(s){took} · click to open", open_it)
            if first and first["kind"] == "images":
                def fetch():
                    with urllib.request.urlopen(gpu_traffic.view_url(base, first), timeout=5) as resp:
                        image = QImage()
                        image.loadFromData(resp.read())
                        return image
                run_async(fetch, lambda img: None if isinstance(img, Exception) or img.isNull()
                          else self.render_thumb.emit(result, img))
        else:
            short = gpu_traffic.friendly_error(result.error)
            node = f" in {result.failed_node}" if result.failed_node else ""
            self._event("render", f"Failed{node}: {short}")
            self.toast("Render failed", f"{short}\nClick for the likely cause and fix.",
                       lambda: self._explain_render_error(result))

    def _on_render_thumb(self, result, image):
        # re-show the toast with the render itself as the icon (Windows shows it beside the text)
        icon = QIcon(QPixmap.fromImage(image.scaled(256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
        took = f" in {result.seconds:.0f} s" if result.seconds else ""
        self.toast("Render finished", f"{len(result.outputs)} file(s){took} · click to open",
                   getattr(self, "_render_open", None), icon)

    def _explain_render_error(self, result):
        card = self.window.card
        facts = f"**Node:** {result.failed_node or '?'}\n\n```\n{result.error[:1200]}\n```"
        request = card.open("ComfyUI render failed", result.failed_node or "ComfyUI", facts)
        run_async(lambda: self.engine.llm.chat([
            {"role": "system", "content": SYSTEM_PERSONA},
            {"role": "user", "content": f"A ComfyUI render failed in node '{result.failed_node}' with:\n{result.error[:3000]}\n\n"
             "Reply with **Likely cause** (1-2 sentences) then **Fix** (short steps). Common causes: a model file "
             "missing or in the wrong folder, out of VRAM, a custom node not installed or out of date, mismatched "
             "model types (e.g. an SDXL LoRA on a Flux model). Be specific to the error."}], max_tokens=400),
            lambda r: card.set_answer(request, popup._answer_or_error(r)))

    # --- GPU traffic ---------------------------------------------------------------------------------
    def refresh_gpu(self):
        if self._gpu_busy:
            return
        self._gpu_busy = True

        def done(snap):
            self._gpu_busy = False
            if not isinstance(snap, Exception):
                self.gpu_snapshot = snap
                self.gpu_ready.emit(snap)
        run_async(lambda: gpu_traffic.snapshot(self.cfg), done)

    def free_comfy(self, base=None, confirm=True):
        bases = [base] if base else [c["base"] for c in (self.gpu_snapshot or {}).get("comfy", [])] or \
            gpu_traffic.comfy_bases(self.cfg)
        if confirm and QMessageBox.question(self.window, "Free ComfyUI VRAM",
                                            "Ask ComfyUI to unload its models?\nOnly happens if its queue is empty.") \
                != QMessageBox.Yes:
            return "Cancelled."
        messages = []
        for b in bases:
            ok, text = gpu_traffic.free_comfyui(b)
            messages.append(f":{gpu_traffic.urlparse(b).port} - {text}")
            if ok:
                self._event("action", f"Freed ComfyUI VRAM ({b})")
        QTimer.singleShot(3000, self.refresh_gpu)
        return "\n".join(messages)

    # --- quick actions ------------------------------------------------------------------------------
    def builtins(self, from_gui=True):
        table = {"comfy_free": lambda: self.free_comfy(confirm=False),
                 "wrapup": (self.show_wrapup if from_gui else lambda: self.wrapup_text()),
                 "snip": (self.start_snip if from_gui else lambda: "Snip needs the screen: press "
                          + hotkeys.pretty(self.hotkey_combos()["snip"]) + " on the PC.")}
        # every builtin must hand back a message string (snip returns its overlay widget, the wrap-up None)
        return {k: (lambda f=f: (lambda r: r if isinstance(r, str) else "Done.")(f())) for k, f in table.items()}

    def action_values(self):
        comfy = (self.gpu_snapshot or {}).get("comfy") or []
        base = comfy[0]["base"] if comfy else None
        return actions.tokens(self.render_watch.output_dirs.get(base) if base else None, base)

    def run_action(self, action, confirmed=False):
        """Run from the GUI (asks first when the action says so). Returns the message shown."""
        if action.confirm and not confirmed:
            if QMessageBox.question(self.window, action.label, f"Run '{action.label}'?") != QMessageBox.Yes:
                return "Cancelled."
        try:
            message = actions.run(action, self.builtins(), self.action_values())
            self._event("action", f"{action.label}: {message[:120]}")
            return message
        except (ValueError, OSError) as exc:
            return f"Couldn't run '{action.label}': {exc}"

    def ask_hook(self, question, bar):
        """Called by the Ctrl+Alt+J bar before it asks the model. True = handled here as an action."""
        if not actions.wants_action(question):
            return False
        action, _score = actions.match(question, self.action_list)
        if not action:
            return False
        bar.hide()
        message = self.run_action(action)
        request = self.window.card.open(action.label, "Quick action", message)
        self.window.card.set_answer(request, " ")
        return True

    # --- day wrap-up ----------------------------------------------------------------------------------
    def _check_wrapup(self):
        now = time.time()
        self.wrap_clock.cfg = self.cfg
        if self.engine.watching and self.wrap_clock.due(now, sensors.idle_seconds()):
            self.wrap_clock.mark_offered(now)
            self.toast("Your day wrap-up is ready", "Click to see what you got done today.", self.show_wrapup)

    def _wrapup_facts(self):
        from .ui import app_label
        start = wrapup.day_start()
        return wrapup.day_facts(self.engine.store, start, time.time()), app_label

    def show_wrapup(self):
        facts, label = self._wrapup_facts()
        card = self.window.card
        request = card.open("Day wrap-up", time.strftime("%A %d %B"), wrapup.facts_markdown(facts, label))
        start = wrapup.day_start()

        def done(result):
            card.set_answer(request, popup._answer_or_error(result))
            if not isinstance(result, Exception) and result:
                self.engine.store.add_journal(start, time.time(), "**Daily wrap-up**\n\n" + result)
                self.engine.data_changed.emit("journal")
                self._event("wrapup", "Day wrap-up written to the Journal")
        run_async(lambda: self.engine.llm.chat(wrapup.wrapup_messages(facts, SYSTEM_PERSONA, label), max_tokens=500),
                  done)

    def wrapup_text(self):
        """Blocking version for chat/phone: facts plus the model's summary, saved to the Journal."""
        from .ui import app_label
        start = wrapup.day_start()
        facts = wrapup.day_facts(self.engine.store, start, time.time())
        text = wrapup.facts_markdown(facts, app_label)
        try:
            summary = self.engine.llm.chat(wrapup.wrapup_messages(facts, SYSTEM_PERSONA, app_label), max_tokens=500)
            self.engine.store.add_journal(start, time.time(), "**Daily wrap-up**\n\n" + summary)
            self.engine.data_changed.emit("journal")
            return text + "\n\n" + summary
        except Exception as exc:
            return text + f"\n\n(model unreachable: {exc})"

    # --- autoplay ------------------------------------------------------------------------------------
    def kill_autoplay(self):
        """Kill switch (runs on the hotkey thread): the session notices within one 50 ms slice."""
        if self.session and self.session.is_alive():
            self.session.stop("kill switch")

    def start_autoplay(self, target, policy, real_input):
        if self.session and self.session.is_alive():
            raise RuntimeError("Jarvis is already playing - stop that run first")
        if not target:
            raise ValueError("pick the program to play (its exe name, e.g. skyrimse.exe)")
        driver = ap_inputs.WindowsDriver() if real_input else ap_inputs.DryRunDriver()
        guard = ap_guard.OperatorGuard(target)
        log_dir = config.DATA_DIR / "autoplay" / time.strftime("%Y%m%d-%H%M%S")
        self.session = AutoplaySession(policy, driver, guard, log_dir, on_status=self.autoplay_status.emit)
        self.session.start()
        self._event("game", f"Autoplay started: {policy.name} on {target} ({'real input' if real_input else 'dry run'})")
        return log_dir

    def _on_autoplay_status(self, kind, text):
        page = getattr(self.window, "games_page", None)
        if page:
            page.autoplay_line(kind, text)
        if kind in ("say", "start", "end", "paused"):
            if self.narration is None:
                self.narration = NarrationStrip()
            self.narration.say(text)
        if kind == "end":
            self._event("game", f"Autoplay {text}")

    # --- chat commands (run OFF the GUI thread: no widgets in here) ---------------------------------
    def command(self, text):
        """Extra chat/phone commands. Returns the reply, or None when the text isn't one of ours."""
        raw = text.strip()
        low = raw.lower()
        if low == "/gpu":
            return gpu_traffic.snapshot_text(self.cfg)
        if low == "/actions":
            return actions.list_text(self.action_list)
        if low in ("/games", "/games log"):
            return game_log.summary(self.engine.store)
        if low == "/guardian":
            try:
                findings = self.guardian.check()
                return "PC guardian: " + ("; ".join(findings) if findings else "no new warnings in this check")
            except Exception as exc:
                return f"PC guardian check failed: {exc}"
        if low == "/screens":
            return "Use /screens watch <folder>, /screens index <image path>, or /screens find <description>."
        if low.startswith("/screens watch "):
            try:
                return screenshot_recall.set_watched_folder(raw[len("/screens watch "):].strip().strip('"'))
            except (OSError, ValueError) as exc:
                return f"Screenshot watch unchanged: {exc}"
        if low == "/screens stop":
            return screenshot_recall.set_watched_folder("")
        if low.startswith("/screens index "):
            try:
                return screenshot_recall.index_file(self.window.recall, self.window.eyes,
                                                    raw[len("/screens index "):].strip().strip('"'))
            except Exception as exc:
                return f"Screenshot indexing stopped: {exc}"
        if low.startswith("/screens find "):
            return screenshot_recall.search(self.window.recall, raw[len("/screens find "):].strip())
        if low == "/routine":
            try:
                rules = routines.load()
                return ("Saved routines:\n" + "\n".join(f"- {r['process']} → {r['action']}" for r in rules)
                        if rules else "No routines saved. Use /routine add <process.exe> <action-id>.")
            except (OSError, ValueError) as exc:
                return f"Routines unavailable: {exc}"
        if low.startswith("/routine add ") or low.startswith("/routine remove "):
            words = raw.split()
            if len(words) != 4:
                return "Use /routine add <process.exe> <action-id> or /routine remove <process.exe> <action-id>."
            try:
                return (routines.add(words[2], words[3], self.action_list) if words[1].lower() == "add"
                        else routines.remove(words[2], words[3]))
            except (OSError, ValueError) as exc:
                return f"Routine unchanged: {exc}"
        if low == "/docs":
            return "Use /docs index <folder> to select files, then /docs ask <question>. Only that folder is read."
        if low.startswith("/docs index "):
            folder = raw[len("/docs index "):].strip().strip('"')
            try:
                return document_recall.index_folder(self.window.recall, folder)
            except Exception as exc:
                return f"Document indexing stopped: {exc}"
        if low.startswith("/docs ask "):
            question = raw[len("/docs ask "):].strip()
            try:
                return document_recall.answer(self.engine, self.window.recall, question)
            except Exception as exc:
                return f"Document question stopped: {exc}"
        if low.startswith("/do "):
            wanted = raw[4:].strip()
            confirmed = wanted.lower().endswith(" yes")
            wanted = wanted[:-4].strip() if confirmed else wanted
            action, _ = actions.match(wanted, self.action_list, threshold=0.6)
            if not action:
                return f"No quick action matches '{wanted}'. Send /actions to see them."
            if action.confirm and not confirmed:
                return f"'{action.label}' asks first: send /do {wanted} yes to run it."
            try:
                message = actions.run(action, self.builtins(from_gui=False), self.action_values())
                self.engine.store.add_event("action", f"{action.label} (from chat): {message[:120]}")
                return message
            except (ValueError, OSError) as exc:
                return f"Couldn't run '{action.label}': {exc}"
        if low.startswith("/fix"):
            name = raw[4:].strip().lower() or sensors.foreground_window()[0]
            if name and not name.endswith(".exe"):
                name += ".exe"
            diag = diagnose.diagnose(name, _exe_path(name))
            facts = "\n".join(f"- {f}" for f in diag.facts)
            try:
                analysis = self.engine.llm.chat(game_help.fix_messages(diag.game.name, diag.facts, diag.evidence),
                                                max_tokens=600)
            except Exception as exc:
                analysis = f"(model unreachable: {exc})"
            return f"{diag.game.name}:\n{facts}\n\n{analysis}"
        if low == "/wrapup":
            return self.wrapup_text()
        if low.startswith("/autoplay"):
            if "stop" in low:
                self.kill_autoplay()
                return "Autoplay stopped." if self.session else "Autoplay isn't running."
            running = self.session and self.session.is_alive()
            return ("Autoplay is running." if running else "Autoplay is idle.") + \
                " Start it from the Games page, or press " + hotkeys.pretty(self.hotkey_combos()["autoplay"]) + " in a game."
        return None


def _exe_path(process_name):
    for proc in psutil.process_iter(["name", "exe"]):
        if (proc.info["name"] or "").lower() == process_name.lower():
            return proc.info["exe"]
    return sensors.EXE_PATHS.get(process_name)


def hub(window):
    """The one FeatureHub for this window (created on first use)."""
    if getattr(window, "features", None) is None:
        window.features = FeatureHub(window.engine, window)
    return window.features


def extend_tray(tray, menu, before):
    """Add the feature entries to the tray menu, just above `before` (the Quit action)."""
    features = hub(tray.window)
    features.tray = tray
    combos = features.hotkey_combos()
    for label, slot in ((f"Snip && ask ({hotkeys.pretty(combos['snip'])})", features.start_snip),
                        (f"Game help ({hotkeys.pretty(combos['game'])})", features.open_game_help),
                        ("Day wrap-up", features.show_wrapup),
                        ("Stop autoplay", features.kill_autoplay)):
        action = menu.addAction(label, slot)
        menu.insertAction(before, action)
    menu.insertSeparator(before)


# ===========================================================================
# Narration strip: what Jarvis is doing while it plays (click-through, top of screen)
# ===========================================================================
class NarrationStrip(QWidget):
    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        frame = hud.HoloFrame(tag="JARVIS // PLAYING")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(22, 12, 20, 16)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setStyleSheet(f"color: {hud.TEXT}; font-family: '{hud.UI_FONT}'; font-size: 12pt;")
        lay.addWidget(self.label)
        self.setFixedWidth(620)
        self._hide = QTimer(self, singleShot=True, timeout=self.hide)

    def say(self, text):
        self.label.setText(text)
        self.adjustSize()
        area = QGuiApplication.primaryScreen().availableGeometry()
        self.move(area.center().x() - self.width() // 2, area.top() + 24)
        self.show()
        self._hide.start(7000)


# ===========================================================================
# In-game help bar (Ctrl+Alt+H)
# ===========================================================================
class GameHelpBar(popup.AskBar):
    """The quick-ask bar in game mode. The screen is captured BEFORE the bar appears, so
    'what's on screen?' reads the game, not Jarvis. '?' = nudge, '??' = bigger hint,
    '???' = full solution; 'fix ...' diagnoses crashes/logs."""

    def __init__(self, engine, card):
        super().__init__(engine, card)
        self.frame = None
        # this is the button row: the three kinds of game help, for when typing is a hassle
        row = QHBoxLayout()
        for text, slot in (("What's on screen?", lambda: self._go("screen")),
                           ("Hint", lambda: self._go("hint1")),
                           ("Diagnose crashes", lambda: self._go("fix"))):
            button = QPushButton(text, clicked=slot)
            hud.caps(button, 1.1)
            row.addWidget(button)
        row.addStretch(1)
        self.findChild(hud.HoloFrame).layout().addLayout(row)
        self.setStyleSheet(popup._card_style(popup.ASK_STYLE))     # adds the chamfered HUD button plates

    def summon(self):
        process, title = sensors.foreground_window()
        self.frame, _, _ = snip.capture_desktop()       # before the bar exists on screen
        self.process = process
        self.exe = _exe_path(process) if process else None
        self.game = presence.game_name(process) or (sensors.app_description(process) or process or "this game")
        self.context = {"process": process, "title": title, "app": self.game, "private": self.engine.is_private(process, title)}
        self.ctx_label.setText(f"Game help · {self.game}")
        self.line.setPlaceholderText("Ask about the game  ·  ? nudge  ·  ?? bigger hint  ·  ??? solution  ·  fix = check crash logs")
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.adjustSize()
        self.move(area.center().x() - self.width() // 2, area.top() + int(area.height() * 0.18))
        self.line.clear()
        self._was_active = False
        self.show()
        self.raise_()
        self.activateWindow()
        pointer.force_foreground(int(self.winId()))
        self.line.setFocus()

    def _ask(self):
        text = self.line.text().strip()
        if text.lower().startswith("fix"):
            self._go("fix", text[3:].strip())
        elif text.startswith("?"):
            level = min(3, len(text) - len(text.lstrip("?")))
            self._go(f"hint{level}", text.lstrip("?").strip())
        else:
            self._go("screen", text)

    def _go(self, mode, question=None):
        question = self.line.text().strip() if question is None else question
        below = QPoint(self.x() + 90, self.y() + self.height() - 10)
        self.hide()
        card, engine = self.card, self.engine
        if self.context.get("private"):
            request = card.open("Private window", self.game, "That window is on your private list, so Jarvis didn't read it.")
            card.set_answer(request, " ")
            return
        engine.store.add_event("game", f"Game help ({self.game}): {mode} {question[:60]}")
        engine.data_changed.emit("events")
        if mode == "fix":
            request = card.open(f"Fixing {self.game}", "Checking logs and crash records", "", near=below)

            def gathered(diag):
                if isinstance(diag, Exception):
                    card.set_answer(request, f"_Couldn't gather evidence: {diag}_")
                    return
                card.set_facts(request, facts_md="\n\n".join(f"- {f}" for f in diag.facts))
                run_async(lambda: engine.llm.chat(game_help.fix_messages(diag.game.name, diag.facts, diag.evidence, question),
                                                  max_tokens=650),
                          lambda r: card.set_answer(request, popup._answer_or_error(r)))
            run_async(lambda: diagnose.diagnose(self.process, self.exe), gathered)
            return
        frame = self.frame
        level = int(mode[-1]) if mode.startswith("hint") else 0
        title = question or ("What's on screen?" if mode == "screen" else "Hint")
        subtitle = f"{self.game} · " + (game_help.HINT_LEVELS[level][0] if level else "on screen")
        request = card.open(title, subtitle, "", near=below)

        def read_screen():
            bgra, w, h = snip.image_bgra(frame)
            return "\n".join(line for line, _ in pointer.ocr_lines(bgra, w, h))

        def with_text(screen_text):
            screen_text = "" if isinstance(screen_text, Exception) else screen_text
            messages = (game_help.puzzle_messages(self.game, question or "this part", level, screen_text) if level
                        else game_help.screen_messages(self.game, question, screen_text))
            run_async(lambda: engine.llm.chat(messages, max_tokens=500),
                      lambda r: card.set_answer(request, popup._answer_or_error(r)))
        run_async(read_screen, with_text)


# ===========================================================================
# Pages
# ===========================================================================
class _Page(QWidget):
    """A page that refreshes itself whenever it's shown (no row-number bookkeeping in MainWindow)."""
    def __init__(self, refresh):
        super().__init__(objectName="page")
        self._refresh = refresh

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh()


def _wrap(inner, refresh):
    page = _Page(refresh)
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(inner)
    return page


# ---------------------------------------------------------------------------
# GPU page
# ---------------------------------------------------------------------------
def build_gpu_page(window):
    from .ui import card, fill_table, page, table
    features = hub(window)
    w, lay = page("GPU", "Who is using the graphics card right now, what each local AI server has loaded, and "
                         "anything holding VRAM for no reason. Finished ComfyUI renders pop up from the tray.")
    top = QHBoxLayout()
    status = QLabel("Reading the GPU…", objectName="muted")
    status.setWordWrap(True)
    refresh = QPushButton("Refresh", objectName="primary", clicked=features.refresh_gpu)
    free = QPushButton("Free ComfyUI VRAM", clicked=lambda: status.setText(features.free_comfy()))
    top.addWidget(status, 1)
    top.addWidget(free)
    top.addWidget(refresh)
    lay.addLayout(top)

    # this is the headline row: utilisation, temperature and a VRAM bar
    f, l = card("VRAM")
    vram_bar = QProgressBar()
    vram_bar.setTextVisible(False)
    vram_text = QLabel("-", objectName="big")
    l.addWidget(vram_text)
    l.addWidget(vram_bar)
    lay.addWidget(f)

    f, l = card("Model servers and ComfyUI")
    services = table(["Service", "Port", "State", "Loaded / queue"])
    l.addWidget(services)
    lay.addWidget(f, 1)
    f, l = card("GPU programs")
    procs = table(["Program", "Role", "RAM", "VRAM"])
    l.addWidget(procs)
    # names vary a lot in length ("Jarvis (Bonsai)", "(no AI servers answering)"): size those columns to fit
    for t in (services, procs):
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
    lay.addWidget(f, 1)
    f, l = card("Advice")
    advice = QListWidget()
    advice.setWordWrap(True)
    l.addWidget(advice)
    lay.addWidget(f, 1)

    def show(snap):
        if snap.get("vram_total"):
            used, total = snap["vram_used"], snap["vram_total"]
            vram_text.setText(f"{used / 1024:.1f} / {total / 1024:.1f} GB   ·   GPU {snap['gpu']:.0f}%   ·   "
                              f"{snap['gpu_temp']:.0f}°C")
            vram_bar.setRange(0, int(total))
            vram_bar.setValue(int(used))
        else:
            vram_text.setText("No GPU telemetry (nvidia-smi didn't answer)")
        rows = [(s["label"], s["port"], "up", ", ".join(s["models"]) or s.get("note", "no model")) for s in snap["llama"]]
        rows += [("ComfyUI", c["port"], "up", f"{c['running']} running · {c['pending']} queued · "
                  f"{c.get('torch_vram_mb', 0) / 1024:.1f} GB held") for c in snap["comfy"]]
        fill_table(services, rows or [("(no AI servers answering)", "", "", "")])
        fill_table(procs, [(p["detail"] or p["name"], p["role"] + (f" :{p['port']}" if p["port"] else ""),
                            f"{p['ram_mb']} MB", f"{p['vram_mb']} MB" if p["vram_mb"] is not None else "not reported")
                           for p in snap["processes"]] or [("(none)", "", "", "")])
        advice.clear()
        for tip in snap["advice"]:
            advice.addItem(("⚠  " if tip["level"] == "warn" else "·  ") + tip["text"])
        free.setEnabled(bool(snap["comfy"]))
        watched = ", ".join(f":{gpu_traffic.urlparse(b).port}" for b in features.render_watch.live_bases)
        status.setText(f"Updated {_now_hhmm()}  ·  " + (f"watching ComfyUI {watched} for finished renders"
                                                        if watched else "no ComfyUI running to watch"))
    features.gpu_ready.connect(show)

    # auto-refresh every 5 s, but only while this page is on screen
    timer = QTimer(w, timeout=lambda: w.isVisible() and features.refresh_gpu(), interval=5_000)
    timer.start()
    return _wrap(w, features.refresh_gpu)


# ---------------------------------------------------------------------------
# Actions page
# ---------------------------------------------------------------------------
def build_actions_page(window):
    from .ui import card, page
    features = hub(window)
    w, lay = page("Actions", "One-click things Jarvis can do. Also works from the Ctrl+Alt+J bar ('open downloads', "
                             "'!free vram') and from chat or your phone (/do name). Edit the list in actions.json - "
                             "only open / url / launch / built-in kinds exist, never a raw command line.")
    f, l = card("Quick actions")
    grid_host = QWidget()
    grid = QGridLayout(grid_host)
    grid.setSpacing(8)
    l.addWidget(grid_host)
    lay.addWidget(f)
    result = QLabel("", objectName="muted")
    result.setWordWrap(True)
    lay.addWidget(result)
    row = QHBoxLayout()
    row.addWidget(QPushButton("Edit actions.json", clicked=lambda: os.startfile(str(actions.actions_path()))))
    reload_btn = QPushButton("Reload")
    row.addWidget(reload_btn)
    row.addStretch(1)
    lay.addLayout(row)
    lay.addStretch(1)

    def rebuild():
        features.action_list, features.action_problems = actions.load()
        while grid.count():
            item = grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        # this loop lays the actions out three to a row
        for i, action in enumerate(features.action_list):
            # '&' would be read as a keyboard-shortcut marker ("Snip & ask" -> "Snip _ask"), so double it
            button = QPushButton(action.label.replace("&", "&&"))
            button.setToolTip(("Asks before running. " if action.confirm else "") + f"{action.kind}: {action.target}")
            hud.caps(button, 1.1)
            button.clicked.connect(lambda _=False, a=action: result.setText(features.run_action(a)))
            grid.addWidget(button, i // 3, i % 3)
        if features.action_problems:
            result.setText("actions.json: " + "; ".join(features.action_problems))
    reload_btn.clicked.connect(rebuild)
    rebuild()
    return _wrap(w, lambda: None)


# ---------------------------------------------------------------------------
# Games page
# ---------------------------------------------------------------------------
class GamesPage(QWidget):
    """Game help (diagnose, hints) and the autoplay lab."""

    def __init__(self, window):
        from .ui import card, page
        super().__init__(objectName="page")
        self.window, self.features = window, hub(window)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        w, lay = page("Games", f"Help with a game: find out why it crashes, ask about what's on screen, or get a "
                               f"spoiler-safe hint (in a game, press {hotkeys.pretty(self.features.hotkey_combos()['game'])}). "
                               "Below that, the autoplay lab: let Jarvis play while you watch.")
        # three panels don't fit a short window without crushing their fields, so the page scrolls instead
        scroll = QScrollArea(widgetResizable=True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(w)
        w.setMinimumHeight(900)
        outer.addWidget(scroll)

        # this is the game picker: running games first, then everything Jarvis knows
        row = QHBoxLayout()
        self.game_combo = QComboBox()
        self.game_combo.setMinimumWidth(320)
        row.addWidget(QLabel("Game:"))
        row.addWidget(self.game_combo, 1)
        row.addWidget(QPushButton("Refresh", clicked=self.refresh))
        lay.addLayout(row)

        # Game time reuses the timeline already recorded while Watching is on.
        self.playtime = QLabel("", objectName="muted")
        self.playtime.setWordWrap(True)
        lay.addWidget(self.playtime)

        # this is the "fix it" panel
        f, l = card("Fix it")
        fix_row = QHBoxLayout()
        self.problem = QLineEdit(placeholderText="Describe the problem (optional): crashes on load, black screen, stutters…")
        fix_row.addWidget(self.problem, 1)
        fix_row.addWidget(QPushButton("Diagnose", objectName="primary", clicked=self.diagnose))
        l.addLayout(fix_row)
        self.fix_view = QTextBrowser()
        self.fix_view.setOpenExternalLinks(True)
        self.fix_view.setMinimumHeight(130)
        l.addWidget(self.fix_view)
        lay.addWidget(f, 2)

        # this is the hint panel: the same question, three levels of spoiler
        f, l = card("Stuck? Spoiler-safe hints")
        hint_row = QHBoxLayout()
        self.puzzle = QLineEdit(placeholderText="What are you stuck on? e.g. the dragon claw door in Bleak Falls Barrow")
        hint_row.addWidget(self.puzzle, 1)
        for level, (label, _) in game_help.HINT_LEVELS.items():
            hint_row.addWidget(QPushButton(label, clicked=lambda _=False, lv=level: self.hint(lv)))
        l.addLayout(hint_row)
        self.hint_view = QTextBrowser()
        self.hint_view.setMinimumHeight(70)
        l.addWidget(self.hint_view)
        lay.addWidget(f, 1)

        # this is the autoplay lab
        combos = self.features.hotkey_combos()
        f, l = card("Autoplay lab")
        grid = QGridLayout()
        self.target = QComboBox()
        self.target.setEditable(True)
        self.target.setToolTip("The program Jarvis plays, by exe name. Input only goes to it while it's in front.")
        self.policy_combo = QComboBox()
        self.goal = QLineEdit(placeholderText="Goal for the vision model, e.g. 'get through the Helgen intro and follow Hadvar'")
        self.real = QCheckBox("Send real input (off = dry run: Jarvis decides and logs, but presses nothing)")
        grid.addWidget(QLabel("Play:"), 0, 0)
        grid.addWidget(self.target, 0, 1)
        grid.addWidget(QLabel("Using:"), 1, 0)
        grid.addWidget(self.policy_combo, 1, 1)
        grid.addWidget(QLabel("Goal:"), 2, 0)
        grid.addWidget(self.goal, 2, 1)
        l.addLayout(grid)
        l.addWidget(self.real)
        buttons = QHBoxLayout()
        self.start_btn = QPushButton("Start", objectName="primary", clicked=lambda: self.start_autoplay())
        self.stop_btn = QPushButton("Stop", clicked=self.features.kill_autoplay)
        logs = QPushButton("Run logs", clicked=self._open_logs)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_btn)
        buttons.addWidget(logs)
        buttons.addStretch(1)
        l.addLayout(buttons)
        note = QLabel(f"After Start, switch to the game. Take control back any time by touching the mouse or keyboard, "
                      f"or press {hotkeys.pretty(combos['kill'])}. In a game, {hotkeys.pretty(combos['autoplay'])} starts "
                      "the selected routine on whatever game is in front.", objectName="muted")
        note.setWordWrap(True)
        l.addWidget(note)
        self.ap_log = QListWidget()
        self.ap_log.setWordWrap(True)
        hud.make_log_view(self.ap_log)
        self.ap_log.setMinimumHeight(90)
        l.addWidget(self.ap_log)
        lay.addWidget(f, 2)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    # --- game list -------------------------------------------------------------------------------
    def refresh(self):
        self.playtime.setText(game_log.summary(self.window.engine.store).replace("\n", "  ·  "))
        current = self.game_combo.currentData()
        self.game_combo.clear()
        running = catalog.running_games()
        for proc, _pid, game in running:
            self.game_combo.addItem(f"▶ {game.name}  (running)", proc)
        for table in (catalog.BETHESDA, catalog.OTHER):
            for exe, game in table.items():
                if exe not in [r[0] for r in running]:
                    self.game_combo.addItem(game.name, exe)
        for prefix, game in catalog.EMULATORS.items():
            if not any(r[0].startswith(prefix) for r in running):
                self.game_combo.addItem(game.name, prefix + ".exe")
        if current is not None and self.game_combo.findData(current) >= 0:
            self.game_combo.setCurrentIndex(self.game_combo.findData(current))
        # autoplay targets: running games plus anything typed before
        typed = self.target.currentText()
        self.target.clear()
        for proc, _pid, game in running:
            self.target.addItem(proc)
        if typed:
            self.target.setEditText(typed)
        policy_now = self.policy_combo.currentText()
        self.policy_combo.clear()
        for path, name, _notes in ap_policy.available_scripts(config.DATA_DIR / "autoplay_scripts"):
            self.policy_combo.addItem(name, path)
        self.policy_combo.addItem(VISION_CHOICE, "vision")
        if policy_now and self.policy_combo.findText(policy_now) >= 0:
            self.policy_combo.setCurrentIndex(self.policy_combo.findText(policy_now))

    def _selected_game(self):
        proc = self.game_combo.currentData() or ""
        game = catalog.detect(proc)
        return proc, (game.name if game else proc)

    # --- fix it ------------------------------------------------------------------------------------
    def diagnose(self):
        proc, name = self._selected_game()
        if not proc:
            return
        self.fix_view.setMarkdown(f"_Checking {name}'s logs and Windows' crash records…_")
        engine, question = self.features.engine, self.problem.text().strip()

        def gathered(diag):
            if isinstance(diag, Exception):
                self.fix_view.setMarkdown(f"Couldn't gather evidence: `{diag}`")
                return
            facts = "\n".join(f"- {f}" for f in diag.facts)
            self.fix_view.setMarkdown(facts + "\n\n_Asking the model what it means…_")
            engine.store.add_event("game", f"Diagnosed {name}")
            engine.data_changed.emit("events")
            run_async(lambda: engine.llm.chat(game_help.fix_messages(name, diag.facts, diag.evidence, question),
                                              max_tokens=650),
                      lambda r: self.fix_view.setMarkdown(facts + "\n\n---\n\n" + popup._answer_or_error(r)))
        run_async(lambda: diagnose.diagnose(proc, _exe_path(proc)), gathered)

    # --- hints ----------------------------------------------------------------------------------------
    def hint(self, level):
        question = self.puzzle.text().strip()
        if not question:
            self.hint_view.setMarkdown("_Type what you're stuck on first._")
            return
        _proc, name = self._selected_game()
        self.hint_view.setMarkdown(f"_{game_help.HINT_LEVELS[level][0]} coming…_")
        engine = self.features.engine
        run_async(lambda: engine.llm.chat(game_help.puzzle_messages(name, question, level), max_tokens=450),
                  lambda r: self.hint_view.setMarkdown(popup._answer_or_error(r)))

    # --- autoplay ------------------------------------------------------------------------------------
    def _build_policy(self):
        choice = self.policy_combo.currentData()
        if choice == "vision":
            goal = self.goal.text().strip()
            if not goal:
                raise ValueError("give the vision model a goal first")
            cfg = self.features.cfg
            key = ""
            key_file = cfg.get("autoplay_vision_key_file", "")
            if key_file and os.path.isfile(key_file):
                key = Path(key_file).read_text(encoding="utf-8").strip()
            return ap_policy.VisionPolicy(goal, cfg.get("autoplay_vision_url", "http://127.0.0.1:8080/v1"),
                                          cfg.get("autoplay_vision_model", ""), key)
        if not choice:
            raise ValueError("no routine selected")
        return ap_policy.ScriptPolicy(choice)

    def start_autoplay(self, target=None):
        target = (target or self.target.currentText()).strip().lower()
        try:
            policy = self._build_policy()
            log_dir = self.features.start_autoplay(target, policy, self.real.isChecked())
            self.autoplay_line("start", f"Run log: {log_dir}")
        except (ValueError, RuntimeError, OSError) as exc:
            self.autoplay_line("error", str(exc))

    def autoplay_line(self, kind, text):
        self.ap_log.addItem(f"{_now_hhmm()}  [{kind}]  {text}")
        self.ap_log.scrollToBottom()
        running = bool(self.features.session and self.features.session.is_alive() and kind != "end")
        self.start_btn.setEnabled(not running)

    def _open_logs(self):
        folder = config.DATA_DIR / "autoplay"
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))


def build_games_page(window):
    page = GamesPage(window)
    window.games_page = page
    return page

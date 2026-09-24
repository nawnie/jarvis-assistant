"""Jarvis's local API - how the phone companion (PhonePcControl) reaches Jarvis Assistant.

Security boundary:
  - listens on 127.0.0.1 only and refuses any non-loopback caller
  - every request needs the token in data/remote_token.txt (header X-Jarvis-Token); the companion
    reads that fixed file. The phone never talks to this port: it talks to the companion, which
    authenticates the phone (HMAC + enrolled device) and forwards only fixed, validated requests
  - GET  /v1/snapshot : read-only, size-bounded view of Jarvis (status, records, projects, settings)
  - POST /v1/action   : one of a FIXED list of actions with typed, bounded fields - no paths, no
    commands, no arbitrary settings. A project's source folder can only be set on the PC.
Anything that touches the GUI/engine timers runs on the GUI thread (GuiCall); slow model calls
(chat, recall questions) run on the request thread so the window never freezes.
"""
import hmac
import json
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PySide6.QtCore import QObject, Signal, Slot

from . import config, sensors

MAX_BODY = 16_000
BOOL_SETTINGS = {"watch_windows", "watch_task_windows", "read_local_task_prompts", "watch_clipboard", "watch_folders", "watch_system", "welcome_back",
                 "clipboard_error_help", "quick_ask_hotkey", "explain_on_click", "keep_pc_awake",
                 "away_model_enabled", "projects_enabled", "away_free_comfyui"}
INT_SETTINGS = {"away_model_after_minutes": (5, 240), "digest_minutes": (10, 1440),
                "break_after_minutes": (10, 600), "idle_seconds": (30, 3600)}


def token_path():
    return config.DATA_DIR / "remote_token.txt"


def load_or_create_token():
    path = token_path()
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    return token


# ---------------------------------------------------------------------------
# Run a function on the GUI thread and wait for its result
# ---------------------------------------------------------------------------
class GuiCall(QObject):
    request = Signal(object)

    def __init__(self):
        super().__init__()
        self.request.connect(self._run)

    @Slot(object)
    def _run(self, job):
        fn, box, done = job
        try:
            box["result"] = fn()
        except Exception as exc:      # handed back to the caller's thread
            box["error"] = exc
        finally:
            done.set()

    def call(self, fn, timeout=15):
        box, done = {}, threading.Event()
        self.request.emit((fn, box, done))
        if not done.wait(timeout):
            raise TimeoutError("Jarvis's window is busy")
        if "error" in box:
            raise box["error"]
        return box.get("result")


def _cut(text, n):
    text = " ".join(str(text or "").split()) if n < 500 else str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# The snapshot: everything the phone's "Inspect Jarvis" screen shows
# ---------------------------------------------------------------------------
def build_snapshot(engine):
    s, now = engine.store, time.time()
    midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    process, title = engine.current
    running = engine.projects.current
    projects = []
    for p in s.projects()[:25]:
        projects.append({
            "id": p["id"], "title": _cut(p["title"], 120), "goal": _cut(p["goal"], 600), "status": p["status"],
            "steps": p["steps"], "last_worked": p["last_worked"], "has_source": bool(p["source_dir"]),
            "log": [{"ts": ts, "kind": kind, "text": _cut(text, 400)} for ts, kind, text in s.project_log(p["id"], 10)],
        })
    return {
        "ok": True,
        "generated": now,
        "status": {
            "watching": engine.watching, "paused_until": engine.paused_until,
            "app": sensors.app_description(process, scan=False) or process, "window": _cut(title, 120),
            "streak_s": (now - engine.session_start) if engine.session_start else 0,
            "away_for_s": (now - engine.system_away_since) if engine.system_away_since else 0,
            "model": engine.models.describe(), "model_online": engine.llm_online,
            "keep_awake": bool(engine.cfg["keep_pc_awake"]),
            "working_on": ({"id": running, "title": (s.project(running) or {}).get("title", "")} if running else None),
            "cpu": engine.stats.get("cpu"), "ram": engine.stats.get("ram"),
            "gpu": engine.stats.get("gpu"), "gpu_temp": engine.stats.get("gpu_temp"),
        },
        "today": [{"app": sensors.app_description(p, scan=False) or p, "seconds": round(sec)}
                  for p, sec in s.app_totals(midnight, now)[:12] if sec >= 30],
        "events": [{"ts": ts, "kind": kind, "text": _cut(text, 240)} for ts, kind, text in s.events(25)],
        "reminders": [{"id": rid, "due": due, "text": _cut(text, 200), "done": bool(done)}
                      for rid, due, text, done in s.reminders()[:20]],
        "memory": [{"id": fid, "fact": _cut(fact, 300)} for fid, fact in s.facts()[:50]],
        "journal": [{"ts": ts, "start": a, "end": b, "text": _cut(text, 1500)} for _, ts, a, b, text in s.journals(8)],
        "clipboard": [{"ts": ts, "app": proc, "text": _cut(text, 300), "has_fix": bool(s.clip_help(cid))}
                      for cid, ts, proc, text in s.clips(15)],
        "timeline": [{"start": a, "end": b, "app": sensors.app_description(p, scan=False) or p, "window": _cut(t, 100)}
                     for a, b, p, t in s.activity_rows(midnight, now, 40) if b - a >= 5],
        "chat": [{"role": role, "text": _cut(text, 2000)} for role, text in s.chat_tail(20)],
        "projects": projects,
        "settings": {**{k: bool(engine.cfg[k]) for k in sorted(BOOL_SETTINGS)},
                     **{k: int(engine.cfg[k]) for k in sorted(INT_SETTINGS)}},
    }


# ---------------------------------------------------------------------------
# Actions: fixed names, typed and bounded fields
# ---------------------------------------------------------------------------
def _text(body, key, limit, required=True):
    value = body.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key} is required")
    if len(value) > limit:
        raise ValueError(f"{key} is too long (max {limit})")
    return value


def _int(body, key, lo, hi):
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{key} must be a whole number {lo}-{hi}")
    return value


def run_action(engine, gui, body):
    action = body.get("action")
    if action == "set_watching":
        on = body.get("on")
        if not isinstance(on, bool):
            raise ValueError("on must be true or false")
        gui.call(lambda: engine.set_watching(on))
        return {"message": "Watching resumed" if on else "Watching paused"}
    if action == "pause":
        minutes = _int(body, "minutes", 1, 480)
        gui.call(lambda: engine.set_watching(False, minutes))
        return {"message": f"Paused for {minutes} min"}
    if action == "chat":
        text = _text(body, "text", 2000)
        return {"reply": engine.chat_reply(text)}
    if action == "add_reminder":
        text, minutes = _text(body, "text", 300), _int(body, "minutes", 1, 10080)
        engine.store.add_reminder(time.time() + minutes * 60, text)
        engine.data_changed.emit("reminders")
        return {"message": f"Reminder set for {minutes} min from now"}
    if action == "delete_reminder":
        engine.store.delete_reminder(_int(body, "id", 1, 2**31))
        engine.data_changed.emit("reminders")
        return {"message": "Reminder deleted"}
    if action == "add_fact":
        engine.store.add_fact(_text(body, "text", 500))
        engine.data_changed.emit("memory")
        return {"message": "Remembered"}
    if action == "delete_fact":
        engine.store.delete_fact(_int(body, "id", 1, 2**31))
        engine.data_changed.emit("memory")
        return {"message": "Forgotten"}
    if action == "journal_now":
        now = time.time()
        gui.call(lambda: engine.write_journal(engine.store.last_journal_end() or now - 3600, now))
        return {"message": "Writing a journal entry"}
    if action == "ask_recall":
        question = _text(body, "question", 500)
        from .popup import memory_matches
        matches = memory_matches(engine, question, limit=40)
        if not matches:
            return {"reply": "Nothing in Jarvis's memory matches that."}
        from .brain import SYSTEM_PERSONA
        reply = engine.llm.chat([{"role": "system", "content": SYSTEM_PERSONA},
                                 {"role": "user", "content": f"It is now {time.strftime('%A %H:%M')}. Records from "
                                  f"Jarvis's memory:\n{matches}\n\nQuestion: {question}\n\nAnswer using ONLY these "
                                  "records, as 'you'. Quote time labels exactly. If they don't answer it, say so."}],
                                max_tokens=400)
        return {"reply": reply}
    if action == "project_add":
        title, goal = _text(body, "title", 120), _text(body, "goal", 4000)
        pid = gui.call(lambda: engine.create_project(title, goal, ""))
        return {"message": "Project added", "id": pid}
    if action == "project_status":
        pid, status = _int(body, "id", 1, 2**31), _text(body, "status", 10)
        gui.call(lambda: engine.set_project_status(pid, status))
        return {"message": f"Project set to {status}"}
    if action == "project_answer":
        pid, text = _int(body, "id", 1, 2**31), _text(body, "text", 2000)
        gui.call(lambda: engine.answer_project(pid, text))
        return {"message": "Answer sent - Jarvis will continue"}
    if action == "project_work_now":
        pid = _int(body, "id", 1, 2**31)
        started = gui.call(lambda: engine.work_on_project_now(pid))
        return {"message": "Jarvis is working on it now" if started else "Jarvis is already busy with a project"}
    if action == "set_setting":
        key = body.get("key")
        cfg = dict(engine.cfg)
        if key in BOOL_SETTINGS:
            if not isinstance(body.get("value"), bool):
                raise ValueError("value must be true or false")
            cfg[key] = body["value"]
        elif key in INT_SETTINGS:
            cfg[key] = _int(body, "value", *INT_SETTINGS[key])
        else:
            raise ValueError("that setting can't be changed from the phone")
        gui.call(lambda: engine.save_config(cfg))
        engine.data_changed.emit("settings")
        return {"message": f"{key} saved"}
    raise ValueError("unknown action")


# ---------------------------------------------------------------------------
# The HTTP server itself
# ---------------------------------------------------------------------------
class RemoteAPI:
    def __init__(self, engine, port):
        self.engine, self.port = engine, port
        self.token = load_or_create_token()
        self.gui = GuiCall()            # created on the GUI thread, so its slot runs there
        self.server = None

    def start(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "JarvisAssistant"

            def log_message(self, *args):
                pass                    # no console; requests aren't logged with their content

            def _reply(self, status, payload):
                data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _allowed(self):
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    self._reply(HTTPStatus.FORBIDDEN, {"ok": False, "error": "loopback only"})
                    return False
                if not hmac.compare_digest(self.headers.get("X-Jarvis-Token", ""), api.token):
                    self._reply(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "bad token"})
                    return False
                return True

            def do_GET(self):  # noqa: N802
                if not self._allowed():
                    return
                if self.path != "/v1/snapshot":
                    self._reply(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                    return
                self._reply(HTTPStatus.OK, build_snapshot(api.engine))

            def do_POST(self):  # noqa: N802
                if not self._allowed():
                    return
                if self.path != "/v1/action":
                    self._reply(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    size = -1
                if not 0 < size <= MAX_BODY:
                    self._reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad body size"})
                    return
                try:
                    body = json.loads(self.rfile.read(size).decode("utf-8"))
                    if not isinstance(body, dict):
                        raise ValueError("body must be a JSON object")
                    result = run_action(api.engine, api.gui, body)
                except (ValueError, UnicodeDecodeError) as exc:
                    self._reply(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except TimeoutError as exc:
                    self._reply(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
                    return
                self._reply(HTTPStatus.OK, {"ok": True, **result})

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True, name="jarvis-remote-api").start()

    def stop(self):
        if self.server:
            self.server.shutdown()

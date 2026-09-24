"""Functional flows for Jarvis Assistant: every page's buttons + the model-backed features.

Runs on Qt's offscreen platform (private clipboard, nothing on your screen) against an isolated
data folder, and uses the real local model on 127.0.0.1:8081.

Run: .venv\\Scripts\\python.exe tests\\qa_flows.py [evidence-dir]      exit code = number of failures
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "qa" / "tmp_flows"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
(TMP / "config.json").write_text(json.dumps({"away_model_enabled": False, "away_free_comfyui": False, "projects_enabled": False, "explain_on_click": False, "quick_ask_hotkey": False,
                                              "llm_autostart_server": False}), encoding="utf-8")
os.environ["JARVIS_DATA_DIR"] = str(TMP)
sys.path.insert(0, str(ROOT))
EVIDENCE = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "qa" / "evidence" / "flows"
EVIDENCE.mkdir(parents=True, exist_ok=True)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

app = QApplication(sys.argv)
from wk import config, popup, sensors  # noqa: E402
from wk.brain import Engine  # noqa: E402
from wk.ui import MainWindow  # noqa: E402

QMessageBox.information = staticmethod(lambda *a, **k: None)   # "Settings saved" would block the run
e = Engine()
w = MainWindow(e)
w.show()
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


def btn(text):
    return [b for b in w.findChildren(QPushButton) if b.text() == text and b.isVisibleTo(w)][0]


def click(text):
    b = [b for b in w.findChildren(QPushButton) if b.text() == text and b.isVisibleTo(w)][0]
    QTest.mouseClick(b, Qt.LeftButton)
    app.processEvents()
    return b


def wait(cond, secs=90):
    t0 = time.time()
    while not cond() and time.time() - t0 < secs:
        app.processEvents()
        time.sleep(0.03)
    app.processEvents()
    return cond()


def short(text, n=110):
    return " ".join(str(text).split())[:n]


check("model online", wait(e.llm.online, 60))
check("test copy can't swap models or touch ComfyUI", not e.cfg["away_model_enabled"] and not e.cfg["away_free_comfyui"])
cb = QGuiApplication.clipboard()

# ============================== Clipboard page ==============================
cb.setText("first copied thing")
app.processEvents()
cb.setText("def f(x): return x +* 2")
app.processEvents()
w.show_page("Clipboard")
app.processEvents()
check("clipboard: copies recorded", w.clip_list.count() == 2, f"{w.clip_list.count()} rows")
check("clipboard: newest clip auto-selected", w.clip_view.toPlainText().startswith("def f"))
b = click("Find problems")
check("clipboard: buttons lock while answering", not b.isEnabled())
ok = wait(lambda: btn("Explain").isEnabled())
check("clipboard: Find problems answered", ok and "asking" not in w.clip_answer.toPlainText(), short(w.clip_answer.toPlainText()))
w.clip_list.setCurrentRow(1)
app.processEvents()
cb.setText("a third copy arrives while page open")
app.processEvents()
check("clipboard: selection survives a new clip", w.clip_view.toPlainText() == "first copied thing")
n = w.clip_list.count()
click("Copy again")
check("clipboard: Copy again sets clipboard", cb.text() == "first copied thing")
check("clipboard: Copy again not re-recorded", w.clip_list.count() == n)
click("Summarize")
check("clipboard: Summarize answered", wait(lambda: btn("Summarize").isEnabled()), short(w.clip_answer.toPlainText()))
click("Delete")
check("clipboard: Delete removes clip", w.clip_list.count() == n - 1)

# ============================== Reminders page ==============================
w.show_page("Reminders")
w.rem_text.setText("stretch")
click("Add")
check("reminders: Add", w.rem_table.rowCount() == 1)
check("reminders: Delete disabled with no selection", not btn("Delete selected").isEnabled())
w.rem_table.selectRow(0)
app.processEvents()
check("reminders: Delete enabled after select", btn("Delete selected").isEnabled())
click("Delete selected")
check("reminders: Delete removes", w.rem_table.rowCount() == 0)
e.store.add_reminder(time.time() - 1, "due now")
fired = []
e.notify.connect(lambda t, m: fired.append((t, m)))
e._chores()
check("reminders: due reminder notifies + marks done", ("Reminder", "due now") in fired
      and e.store.reminders()[0][3] == 1)

# ============================== Memory page ==============================
w.show_page("Memory")
w.mem_text.setText("likes amber")
click("Remember")
check("memory: Remember", w.mem_list.count() == 1)
check("memory: Forget disabled with no selection", not btn("Forget selected").isEnabled())
w.mem_list.setCurrentRow(0)
app.processEvents()
click("Forget selected")
check("memory: Forget removes", w.mem_list.count() == 0)

# ============================== Timeline -> Journal ==============================
now = time.time()
e.store.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)",
            (now - 1500, now - 600, "code.exe", "ui.py - Jarvis Assistant - VS Code"))
e.store.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)",
            (now - 600, now - 60, "chrome.exe", "prism-ml/Ternary-Bonsai-8B-gguf - Hugging Face"))
w.show_page("Timeline")
w._refresh_timeline()
check("timeline: rows shown", w.tl_table.rowCount() >= 2)
click("Write a journal entry for this range")
check("timeline: jumps to Journal immediately", w.stack.currentIndex() == 4 and "Writing" in w.journal_view.toPlainText())
ok = wait(lambda: "Writing" not in w.journal_view.toPlainText())
check("timeline: journal entry written", ok and ("ui.py" in w.journal_view.toPlainText() or "VS Code" in w.journal_view.toPlainText()
                                                or "Bonsai" in w.journal_view.toPlainText()), short(w.journal_view.toPlainText()))
for label in ("What did I do today?", "Where did I leave off?", "Write entry now"):
    click(label)
    ok = wait(lambda: not any(s in w.journal_view.toPlainText() for s in ("Thinking", "Writing")))
    check(f"journal: {label}", ok and len(w.journal_view.toPlainText()) > 20, short(w.journal_view.toPlainText(), 70))

# ============================== Chat page ==============================
w.show_page("Chat")
w.chat_in.setText("Reply with the single word: ready")
click("Send")
ok = wait(lambda: btn("Send").isEnabled())
check("chat: Send gets a reply", ok and e.store.chat_tail(1)[0][0] == "assistant", short(e.store.chat_tail(1)[0][1], 60))
w.chat_in.setText("/remind 5 test")
QTest.keyClick(w.chat_in, Qt.Key_Return)
app.processEvents()
check("chat: Enter key + /remind", "remind you in 5" in e.store.chat_tail(1)[0][1])
w.chat_in.setText("/remember my GPU is a 4070 Ti Super")
QTest.keyClick(w.chat_in, Qt.Key_Return)
check("chat: /remember stores a fact", any("4070" in f for _, f in e.store.facts()))
w.chat_in.setText("/clear")
QTest.keyClick(w.chat_in, Qt.Key_Return)
check("chat: /clear empties the transcript", not e.store.chat_tail(5))

# ============================== Now page ==============================
w.show_page("Now")
click("Pause watching")
check("now: Pause watching", not e.watching and bool(btn("Resume watching")))
click("Resume watching")
check("now: Resume watching", e.watching)
e.set_watching(False, 30)
e.paused_until = time.time() - 1          # the 30 minutes are up
e._tick()
check("now: timed pause resumes by itself", e.watching)
w._refresh_today()
rows = [w.today_table.item(r, 0).text() for r in range(w.today_table.rowCount())]
check("now: today-by-app lists recorded apps", len(rows) >= 2, ", ".join(rows))

# ============================== copied error -> background fix ==============================
got = {}
e.error_help.connect(lambda cid, headline: got.update(cid=cid, headline=headline))
cb.setText("hello, just a normal sentence")
app.processEvents()
cb.setText('Traceback (most recent call last):\n  File "train.py", line 4, in <module>\n    import torch\n'
           "ModuleNotFoundError: No module named 'torch'")
app.processEvents()
check("error helper: fired only for the error", wait(lambda: "cid" in got, 90), got.get("headline", "")[:120])
w.open_clip(got.get("cid", -1))
check("error helper: click opens that clip", w.stack.currentIndex() == 2 and "torch" in w.clip_view.toPlainText())
answer = w.clip_answer.toPlainText()
check("error helper: fix shown with the clip", "Fix" in answer and ("pip" in answer or "install" in answer), short(answer, 140))

# ============================== Recall ==============================
now = time.time()
e.store.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)",
            (now - 26 * 3600, now - 25 * 3600, "chrome.exe", "llama.cpp/tools/server/README.md at master - GitHub - Google Chrome"))
w.show_page("Recall")
w.rc_query.setText("llama.cpp")
QTest.keyClick(w.rc_query, Qt.Key_Return)
check("recall: search finds the window", w.rc_table.rowCount() >= 1 and "llama.cpp" in w.rc_table.item(0, 3).text())
w.rc_table.setCurrentCell(0, 0)
check("recall: preview shows full text", "README" in w.rc_view.toPlainText())
w.rc_query.setText("when did I last have the llama.cpp server docs open?")
click("Ask Jarvis")
ok = wait(lambda: w.rc_ask_btn.isEnabled(), 90)
text = short(w.rc_view.toPlainText(), 200)
check("recall: Ask answers from memory", ok and "yesterday" in text.lower(), text)
w.rc_query.setText("zzqx qqwv")
click("Ask Jarvis")
check("recall: no matches says so", "Nothing" in w.rc_view.toPlainText())

# ============================== welcome back ==============================
info = {}
e.welcome_back.connect(lambda d: info.update(d))
# the real 5-second tick uses your REAL idle time; pause it so only the simulated absence counts
e.t_tick.stop()
e.away_since = e.system_away_since = None
# what you were doing BEFORE you left (you left ~32 min ago)
now = time.time()
e.store.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)",
            (now - 3600, now - 1950, "chrome.exe", "prism-ml - Hugging Face - Google Chrome"))
real_idle = sensors.idle_seconds
sensors.idle_seconds = lambda: 1900
e._tick()
check("welcome back: away detected", e.away_since is not None)
sensors.idle_seconds = lambda: 1
e._tick()
sensors.idle_seconds = real_idle
check("welcome back: fired on return", bool(info), f"away {info.get('away_text')} - last: {info.get('last_title', '')[:50]}")
check("welcome back: knows what you were doing", "Hugging Face" in info.get("last_title", ""))
if info:
    popup.welcome_card(w.card, e, info)
    check("welcome back: card recap", wait(lambda: bool(w.card.answer_md), 60), short(w.card.answer_md))
w.card.hide()
info.clear()
sensors.idle_seconds = lambda: 400
e._tick()
sensors.idle_seconds = lambda: 1
e._tick()
sensors.idle_seconds = real_idle
check("welcome back: short break stays quiet", not info)
e.set_watching(False)
sensors.idle_seconds = lambda: 1900
e._tick()
sensors.idle_seconds = lambda: 1
e._tick()
sensors.idle_seconds = real_idle
check("welcome back: nothing while paused", not info)
e.set_watching(True)
e.t_tick.start()

# ============================== app card ==============================
midnight = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
popup.explain_app(w.card, e, "chrome.exe", midnight, time.time(), "today")
check("app card: facts instantly", "Top windows" in w.card.body.toPlainText())
check("app card: model answer", wait(lambda: bool(w.card.answer_md), 60), short(w.card.answer_md))
QTest.mouseClick(w.card.chat_btn, Qt.LeftButton)
check("app card: continue in chat", w.stack.currentIndex() == 6 and len(e.store.chat_tail(2)) == 2)

# ============================== Projects: a real work session ==============================
import hashlib  # noqa: E402
import json as _json  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

source = TMP / "my-source"
source.mkdir(exist_ok=True)
(source / "README.md").write_text("# Garden planner\nA tiny app idea: track what I planted and when to water it.\n"
                                  "Ideas: reminders, a planting calendar.\n", encoding="utf-8")


def tree_hash(folder):
    return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file())).hexdigest()


before_source = tree_hash(source)
pid = e.create_project("Garden planner plan",
                       "Read source/README.md and write a short feature plan to workspace/plan.md "
                       "(a heading plus 3-6 bullet points). Then finish.", str(source))
w.show_page("Projects")
check("projects: new project listed", w.pj_table.rowCount() == 1 and "Garden" in w.pj_table.item(0, 0).text())
check("projects: work now starts a session", e.work_on_project_now(pid))
ok = wait(lambda: not e.projects.busy, 300)
project = e.store.project(pid)
workspace_files = [p.name for p in Path(project["workspace"]).rglob("*") if p.is_file()]
kinds = [k for _, k, _ in e.store.project_log(pid)]
check("projects: session finished", ok, f"steps {project['steps']}, status {project['status']}")
check("projects: Jarvis wrote into its workspace", bool(workspace_files), ", ".join(workspace_files))
check("projects: every step logged", project["steps"] >= 1 and "session" in kinds, " ".join(kinds))
check("projects: your source folder untouched", tree_hash(source) == before_source)
check("projects: nothing written outside the project", not [p for p in TMP.rglob("*.md") if p.is_file()
      and "projects" not in p.parts and p.parent != source])
w._refresh_projects()
check("projects: page shows the log", "Log" in w.pj_view.toPlainText() and "session" in w.pj_view.toPlainText())

# ============================== phone access: the local API over real HTTP ==============================
from wk.remote_api import RemoteAPI  # noqa: E402

api = RemoteAPI(e, 8796)
api.start()


def call(method, path, body=None, token=None):
    data = _json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:8796{path}", data=data, method=method,
                                 headers={"X-Jarvis-Token": api.token if token is None else token,
                                          "Content-Type": "application/json"})
    result = {}

    def work():
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result["value"] = (resp.status, _json.loads(resp.read()))
        except urllib.error.HTTPError as err:
            result["value"] = (err.code, _json.loads(err.read()))
    import threading
    t = threading.Thread(target=work)
    t.start()
    wait(lambda: not t.is_alive(), 130)     # keep the GUI thread pumping (actions run on it)
    return result.get("value", (0, {}))


status, _ = call("GET", "/v1/snapshot", token="wrong")
check("api: wrong token refused", status == 401)
status, snap = call("GET", "/v1/snapshot")
check("api: snapshot", status == 200 and {"status", "projects", "memory", "settings", "chat"} <= set(snap),
      f"model {snap.get('status', {}).get('model')}")
check("api: snapshot has the project", any(p["id"] == pid for p in snap.get("projects", [])))
status, reply = call("POST", "/v1/action", {"action": "add_fact", "text": "prefers short answers"})
check("api: add_fact", status == 200 and any("short answers" in f for _, f in e.store.facts()))
status, reply = call("POST", "/v1/action", {"action": "set_setting", "key": "llm_server_exe", "value": "x.exe"})
check("api: disallowed setting refused", status == 400, reply.get("error"))
status, reply = call("POST", "/v1/action", {"action": "set_setting", "key": "welcome_back", "value": True})
check("api: allowed setting saved and shown on the PC", status == 200 and e.cfg["welcome_back"] is True
      and w.s_checks["welcome_back"].isChecked())
e.store.set_project_status(pid, "needs_input")
status, reply = call("POST", "/v1/action", {"action": "project_answer", "id": pid, "text": "Keep it simple."})
check("api: answering a question reactivates the project", status == 200 and e.store.project(pid)["status"] == "active")
status, reply = call("POST", "/v1/action", {"action": "chat", "text": "Reply with the single word: pong"})
check("api: chat from the phone", status == 200 and reply.get("reply")
      and not reply["reply"].startswith("Local model unreachable") and e.store.chat_tail(1)[0][0] == "assistant",
      short(reply.get("reply", ""), 40))
status, reply = call("POST", "/v1/action", {"action": "pause", "minutes": 30})
check("api: pause", status == 200 and not e.watching)
e.set_watching(True)
api.stop()

# ============================== Settings ==============================
w.show_page("Settings")
w.s_checks["welcome_back"].setChecked(False)
w.s_spins["welcome_back_minutes"].setValue(25)
w.s_spins["poll_seconds"].setValue(7)
w._save_settings()
saved = config.load()
check("settings: options persist", saved["welcome_back"] is False and saved["welcome_back_minutes"] == 25
      and saved["poll_seconds"] == 7)
check("settings: new poll interval applied live", e.t_tick.interval() == 7000)

e.shutdown()
failed = results.count(False)
print(f"\nflows: {len(results) - failed} passed, {failed} failed", flush=True)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(min(failed, 250))

"""Core unit tests for Jarvis Assistant - deterministic, no model, no display needed.

Run:  .venv\\Scripts\\python.exe -m pytest tests\\test_core.py -q
"""
import json
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from wk import config  # noqa: E402
from wk.brain import looks_like_error  # noqa: E402
from wk.store import Store  # noqa: E402


# ---------------------------------------------------------------------------
# a fresh in-memory store for each test
# ---------------------------------------------------------------------------
@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# --- activity timeline --------------------------------------------------------------
def test_same_window_extends_one_row(store):
    t = 1000.0
    for i in range(5):
        store.record_activity("code.exe", "a.py", t + i * 5, 5)
    rows = store.activity_rows(0, 10_000)
    assert len(rows) == 1
    assert rows[0][1] - rows[0][0] == 20


def test_window_switch_closes_previous_row_without_losing_time(store):
    store.record_activity("code.exe", "a.py", 1000, 5)
    store.record_activity("code.exe", "a.py", 1005, 5)
    store.record_activity("chrome.exe", "docs", 1010, 5)
    rows = sorted(store.activity_rows(0, 10_000))
    assert rows[0][:2] == (1000, 1010)          # first row runs right up to the switch
    assert rows[1][2] == "chrome.exe"


def test_idle_gap_starts_a_new_row(store):
    store.record_activity("code.exe", "a.py", 1000, 5)
    store.break_activity()
    store.record_activity("code.exe", "a.py", 1400, 5)
    assert len(store.activity_rows(0, 10_000)) == 2


def test_app_detail_counts_stretches_and_top_windows(store):
    for a, b, p, t in ((0, 600, "chrome.exe", "A"), (600, 900, "code.exe", "x"),
                       (900, 1200, "chrome.exe", "B"), (1200, 1500, "chrome.exe", "A")):
        store.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)", (a, b, p, t))
    d = store.app_detail("chrome.exe", 0, 2000)
    assert d["total"] == 1200
    assert d["sessions"] == 2                      # chrome, then code, then chrome again
    assert d["windows"][0] == ("A", 900)
    assert (d["first"], d["last"]) == (0, 1500)


def test_prune_forgets_only_old_raw_data(store):
    now = time.time()
    store.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)", (now - 90 * 86400, now - 90 * 86400, "old.exe", "x"))
    store.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)", (now - 60, now, "new.exe", "y"))
    store.add_fact("keep me")
    store.prune(30)
    assert [r[2] for r in store.activity_rows(0, now + 1)] == ["new.exe"]
    assert store.facts()[0][1] == "keep me"       # long-term memory is never pruned


# --- clipboard + schema migration ------------------------------------------------------
def test_clip_help_roundtrip(store):
    cid = store.add_clip("code.exe", "ValueError: x")
    assert store.clip_help(cid) is None
    store.set_clip_help(cid, "fix it")
    assert store.clip_help(cid) == "fix it"


def test_old_database_gets_help_column(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE clipboard (id INTEGER PRIMARY KEY, ts REAL, process TEXT, text TEXT)")
    con.execute("INSERT INTO clipboard(ts, process, text) VALUES (1, 'a', 'b')")
    con.commit()
    con.close()
    s = Store(db)
    assert "help" in [c[1] for c in s.db.execute("PRAGMA table_info(clipboard)")]
    assert s.clips()[0][3] == "b"                  # existing data kept
    Store(db)                                       # opening again must not fail


# --- recall search -------------------------------------------------------------------
def test_keywords_drop_question_words(store):
    assert store.keywords("when did I last have the llama.cpp docs open?") == ["llama.cpp", "docs"]
    assert store.keywords("the") == ["the"]         # nothing left -> fall back to the raw text


def test_search_ranks_by_keyword_hits_then_recency(store):
    now = time.time()
    store.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)", (now - 900, now - 800, "chrome.exe", "llama.cpp server docs"))
    store.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)", (now - 100, now - 50, "chrome.exe", "llama.cpp releases"))
    store.add_clip("code.exe", "unrelated")
    hits = store.search("llama.cpp docs")
    assert hits[0][3] == "llama.cpp server docs"    # 2 keyword hits beats newer 1-hit row
    assert all("unrelated" not in h[3] for h in hits)


def test_search_is_safe_with_sql_wildcards_and_quotes(store):
    store.add_clip("x", "100% it's done")
    assert store.search("it's")                     # quotes don't break the query
    assert store.search("100%")


# --- error detection -------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("Traceback (most recent call last):\n  File x\nValueError: bad", True),
    ("ModuleNotFoundError: No module named torch", True),
    ("npm ERR! code ELIFECYCLE", True),
    ("error: failed to push some refs", True),
    ("'foo' is not recognized as an internal or external command", True),
    ("The installation failed with HRESULT 0x80070005", True),
    ("hello world, a normal sentence", False),
    ("I love this error-free code", False),
    ("short", False),
])
def test_looks_like_error(text, expected):
    assert looks_like_error(text) is expected


# --- config ------------------------------------------------------------------------------
def test_config_defaults_merge_and_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    assert config.load()["poll_seconds"] == config.DEFAULTS["poll_seconds"]
    (tmp_path / "config.json").write_text(json.dumps({"poll_seconds": 9}), encoding="utf-8")
    cfg = config.load()
    assert cfg["poll_seconds"] == 9 and cfg["digest_minutes"] == config.DEFAULTS["digest_minutes"]
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    assert config.load()["poll_seconds"] == config.DEFAULTS["poll_seconds"]   # corrupt file -> defaults
    config.save({"a": 1})
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {"a": 1}


def test_known_folders_resolve_to_real_paths():
    assert Path(config.DESKTOP).exists()
    assert Path(config.DOWNLOADS).exists()


def test_recent_task_sources_read_only_user_requests(tmp_path):
    from wk.task_sources import recent_tasks
    now = time.time()
    claude = tmp_path / ".claude" / "history.jsonl"
    claude.parent.mkdir()
    claude.write_text(json.dumps({"timestamp": int(now * 1000), "display": "Design a compact UI",
                                  "pastedContents": "private attachment"}) + "\n", encoding="utf-8")
    day = time.strftime("%Y/%m/%d", time.localtime(now))
    codex = tmp_path / ".codex" / "sessions" / day / "rollout-test.jsonl"
    codex.parent.mkdir(parents=True)
    codex.write_text(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                                 "type": "response_item", "payload": {"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": "Fix Jarvis status"}]}}) + "\n" +
                     json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                                 "type": "response_item", "payload": {"type": "message", "role": "assistant",
                                 "content": [{"type": "output_text", "text": "secret answer"}]}}) + "\n", encoding="utf-8")
    tasks = recent_tasks(tmp_path, now=now)
    assert {source for _, source, _ in tasks} == {"Claude Code", "Codex"}
    assert "private attachment" not in str(tasks) and "secret answer" not in str(tasks)


def test_chat_self_knowledge_is_available_without_activity_context(store):
    from types import SimpleNamespace
    from wk.brain import Engine
    seen = []
    llm = SimpleNamespace(chat=lambda messages, **kw: seen.append(messages) or "I can inspect my settings.")
    changed = SimpleNamespace(emit=lambda _: None)
    engine = SimpleNamespace(store=store, cfg={**config.DEFAULTS, "llm_key_file": "private-secret"},
                             watching=True, task_windows=[], llm=llm, data_changed=changed)
    assert "Settings file" in Engine.chat_reply(engine, "/status", False)
    assert "watch_task_windows" in Engine.chat_reply(engine, "/settings", False)
    assert "private-secret" not in store.chat_tail(5)[-1][1]
    assert Engine.chat_reply(engine, "What can you see?", False) == "I can inspect my settings."
    system = seen[0][0]["content"]
    assert "Claude Desktop chat is not connected" in system
    assert "Settings file" in system


def test_memory_and_mission_survive_activity_context_off(store):
    from types import SimpleNamespace
    from wk.brain import Engine
    store.add_fact("Shawn prefers concise status with evidence")
    store.add_project("Website", "Build AI Embedded Systems site", "", "")
    captured = []
    llm = SimpleNamespace(chat=lambda messages, **kw: captured.append((messages, kw)) or "Ready.")
    engine = SimpleNamespace(store=store, cfg={**config.DEFAULTS, "reply_depth": "quick"},
                             watching=False, task_windows=[], llm=llm,
                             data_changed=SimpleNamespace(emit=lambda _: None))
    assert Engine.chat_reply(engine, "What is our mission?", False) == "Ready."
    system = captured[0][0][0]["content"]
    assert "www.aiembeddedsystems.com" in system
    assert "Shawn prefers concise status" in system
    assert "Project Website" in system
    assert "Last 45 minutes" not in system
    assert captured[0][1]["max_tokens"] == 450


def test_memory_preferences_bound_context_and_keep_journal_distinct(store):
    from wk.behavior import memory_context
    store.add_fact("Old anchor")
    store.add_fact("Website anchor")
    store.add_journal(0, 1, "Worked on website layout; deployment not verified")
    cfg = {**config.DEFAULTS, "memory_fact_mode": "related", "memory_fact_limit": 1,
           "memory_open_loops": False}
    context = memory_context(store, "website", cfg)
    assert "Website anchor" in context and "Old anchor" not in context
    assert "model-written" in context and "deployment not verified" in context
    cfg["memory_fact_mode"] = "off"
    cfg["memory_journal_recall"] = False
    assert "Website anchor" not in memory_context(store, "website", cfg)


def test_project_progress_is_bounded_memory_and_can_be_switched_off(store):
    from wk.behavior import memory_context
    pid = store.add_project("Jarvis", "Improve autonomous project work", "", "")
    store.log_project(pid, "note", "Mapped the memory settings to the away worker")
    cfg = dict(config.DEFAULTS)
    assert "Mapped the memory settings" in memory_context(store, "Jarvis", cfg)
    cfg["memory_open_loops"] = False
    assert "Mapped the memory settings" in memory_context(store, "Jarvis", cfg)
    cfg["memory_project_updates"] = False
    assert "Mapped the memory settings" not in memory_context(store, "Jarvis", cfg)


def test_mission_recall_and_memory_core_controls(store, qapp):
    from wk.behavior import memory_context
    from wk.behavior_ui import BehaviorDialog
    store.add_fact("AI Embedded Systems launch is the active mission")
    store.add_fact("The garden fence is blue")
    cfg = {**config.DEFAULTS, "memory_fact_mode": "mission", "memory_fact_limit": 1,
           "assistant_mission": "Build AI Embedded Systems"}
    context = memory_context(store, "What matters now?", cfg)
    assert "active mission" in context and "garden fence" not in context
    dialog = BehaviorDialog(cfg, store)
    assert dialog.values()["memory_fact_mode"] == "mission"
    assert dialog.values()["memory_capture_mode"] == "suggest"
    assert dialog.values()["tool_use_8b"] is True
    assert dialog.values()["tool_daily_limit"] == 3
    assert dialog.values()["retention_days"] == cfg["retention_days"]
    assert dialog.values()["project_task_context"] is True


def test_objectives_command_reports_durable_goal_and_progress_without_model(store):
    from types import SimpleNamespace
    from wk.brain import Engine
    pid = store.add_project("Jarvis", "Finish a bounded memory plan", "", "")
    store.log_project(pid, "note", "Reviewed the continuity settings")
    engine = SimpleNamespace(store=store, cfg=dict(config.DEFAULTS), watching=False, task_windows=[],
                             data_changed=SimpleNamespace(emit=lambda _: None))
    reply = Engine.chat_reply(engine, "/objectives", False)
    assert "Finish a bounded memory plan" in reply
    assert "Reviewed the continuity settings" in reply
    assert "Observed Codex/Claude requests are hints" in reply


def test_resource_stop_requires_exact_user_selected_process_identity(monkeypatch):
    from wk import resource_tools
    calls = []

    class FakeProcess:
        def __init__(self, pid): self.pid = pid
        def create_time(self): return 1234.567
        def name(self): return "render-helper.exe"
        def terminate(self): calls.append(self.pid)
        def wait(self, timeout): return 0

    monkeypatch.setattr(resource_tools.psutil, "Process", FakeProcess)
    assert "Use /processes" in resource_tools.stop_command("/stop 900")
    assert "identity changed" in resource_tools.stop_command("/stop 900 9999")
    assert calls == []
    assert "Stopped render-helper.exe" in resource_tools.stop_command("/stop 900 1234.567")
    assert calls == [900]


def test_away_worker_uses_only_relevant_recent_task_hints(store, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from wk import projects
    pid = store.add_project("Jarvis Assistant", "Improve memory and settings",
                            "", str(tmp_path))
    project = store.project(pid)
    tasks = [(100, "Codex", "Fix Jarvis memory settings"),
             (101, "Claude Code", "Update banking statements"),
             (102, "Claude Code", "Fix memory leak in another app")]
    monkeypatch.setattr(projects.task_sources, "recent_tasks", lambda **kw: tasks)
    engine = SimpleNamespace(store=store, watching=True, cfg=dict(config.DEFAULTS),
                             observation_profile=lambda: "Watching is on")
    runner = projects.ProjectRunner(engine)
    hints = runner._task_hints(project)
    assert len(hints) == 1 and "Jarvis" in hints[0][2]
    store.add_fact("Unrelated personal fact")
    engine.cfg["memory_fact_mode"] = "off"
    text = runner._messages(project, projects.Sandbox(project), [], hints)[1]["content"]
    assert "Fix Jarvis memory settings" in text
    assert "Update banking statements" not in text
    assert "memory leak" not in text
    assert "Unrelated personal fact" not in text
    assert "Help Shawn build AI Embedded Systems" in text
    engine.watching = False
    assert runner._task_hints(project) == []


def test_background_task_titles_respect_private_filter_and_deduplicate(store, monkeypatch):
    from types import SimpleNamespace
    from wk import sensors
    from wk.brain import Engine
    monkeypatch.setattr(sensors, "task_windows", lambda: [
        ("claude.exe", "Design project"), ("codex.exe", "password manager")])
    engine = SimpleNamespace(watching=True, cfg={"watch_task_windows": True},
                             task_windows=[], store=store,
                             is_private=lambda proc, title: "password" in title,
                             data_changed=SimpleNamespace(emit=lambda _: None))
    Engine._poll_task_windows(engine)
    Engine._poll_task_windows(engine)
    assert engine.task_windows == [("claude.exe", "Design project")]
    assert [row[2] for row in store.events() if row[1] == "task-window"] == [
        "Visible title in claude.exe: Design project"]
    engine.watching = False
    Engine._poll_task_windows(engine)
    assert engine.task_windows == []


def test_reviewed_handoff_command_is_read_only(store):
    from types import SimpleNamespace
    from wk.brain import Engine
    engine = SimpleNamespace(store=store, cfg=config.DEFAULTS, watching=True,
                             task_windows=[], data_changed=SimpleNamespace(emit=lambda _: None))
    reply = Engine.chat_reply(engine, "/handoff", False)
    assert "Bonsai 2 27B self-repair lane" in reply
    assert "design target" in reply


# --- local model client ----------------------------------------------------------------------
def test_key_file_comments_skipped_and_errors_redacted(tmp_path):
    from wk.llm import LocalLLM
    key_file = tmp_path / "key.txt"
    key_file.write_text("# comment\n\n# another\nsecret123\n", encoding="utf-8")
    llm = LocalLLM({"llm_key_file": str(key_file), "llm_base_url": "http://127.0.0.1:9/v1", "llm_model": "m"})
    assert llm._key() == "secret123"
    assert llm._headers()["Authorization"] == "Bearer secret123"
    err = llm._clean_error(ValueError("header was Bearer secret123"))
    assert "secret123" not in str(err)
    with pytest.raises(RuntimeError) as info:       # nothing listens on port 9
        llm.chat([{"role": "user", "content": "hi"}])
    assert "secret123" not in str(info.value)


def test_no_key_file_means_no_auth_header():
    from wk.llm import LocalLLM
    assert "Authorization" not in LocalLLM({"llm_key_file": ""})._headers()


# --- pointer helpers -----------------------------------------------------------------------
def test_friendly_type_and_description():
    from wk import pointer
    assert pointer.friendly_type("ListItemControl") == "list item"
    assert pointer.friendly_type("ButtonControl") == "button"
    assert pointer.friendly_type(None) == ""
    text = pointer.describe_for_model({"process": "code.exe", "title": "a.py", "uia": {"type": "ButtonControl", "name": "Run"},
                                       "text_under_pointer": ["Run"], "text_nearby": ["Debug"]})
    assert "button labelled 'Run'" in text and "Control" not in text


def test_when_label_relative_days():
    from wk.popup import when_label
    now = time.time()
    assert when_label(now).startswith("today ")
    assert when_label(now - 86400).startswith("yesterday ")
    assert when_label(now - 3 * 86400).split()[0] in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    assert when_label(None) == "?"


# --- icon file ------------------------------------------------------------------------------
def test_ico_has_every_size(tmp_path, qapp):
    from wk.ui import ICON_SIZES, save_ico
    path = tmp_path / "j.ico"
    save_ico(path)
    data = path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, len(ICON_SIZES))
    sizes = sorted(struct.unpack("<B", data[6 + 16 * i:7 + 16 * i])[0] or 256 for i in range(count))
    assert sizes == sorted(ICON_SIZES)


# --- global mouse hook: never leave a click half-swallowed ------------------------------------
def _fake_click():
    import ctypes
    from wk import pointer
    info = pointer.MSLLHOOKSTRUCT()
    info.pt.x, info.pt.y = 5, 5
    return info, ctypes.addressof(info)


def test_hook_swallows_exactly_one_click_when_it_fires(monkeypatch):
    from wk import pointer
    monkeypatch.setattr(pointer, "_held", lambda vk: vk == pointer.VK_CONTROL)   # "Ctrl is down"
    fired = []
    hook = pointer.MouseTrigger(armed=lambda: "ctrl", on_fire=lambda x, y: fired.append((x, y)))
    info, lp = _fake_click()
    assert hook._callback(0, pointer.WM_LBUTTONDOWN, lp) == 1
    assert hook._callback(0, pointer.WM_LBUTTONUP, lp) == 1
    assert hook._callback(0, pointer.WM_LBUTTONUP, lp) != 1        # the NEXT release is left alone
    assert fired == [(5, 5)]


def test_hook_error_passes_the_click_through(monkeypatch):
    from wk import pointer
    monkeypatch.setattr(pointer, "_held", lambda vk: vk == pointer.VK_CONTROL)

    def boom(x, y):
        raise RuntimeError("explain request failed")
    hook = pointer.MouseTrigger(armed=lambda: "ctrl", on_fire=boom)
    info, lp = _fake_click()
    assert hook._callback(0, pointer.WM_LBUTTONDOWN, lp) != 1       # not swallowed
    assert hook._swallow_up is False                               # next release not eaten


def test_hook_respects_combo_and_off_switch(monkeypatch):
    from wk import pointer
    monkeypatch.setattr(pointer, "_held", lambda vk: vk == pointer.VK_CONTROL)
    info, lp = _fake_click()
    assert pointer.MouseTrigger(armed=lambda: "ctrl+alt", on_fire=lambda x, y: None)._callback(0, pointer.WM_LBUTTONDOWN, lp) != 1
    assert pointer.MouseTrigger(armed=lambda: None, on_fire=lambda x, y: None)._callback(0, pointer.WM_LBUTTONDOWN, lp) != 1


def test_to_logical_on_single_screen(qapp):
    from wk import pointer
    dpr = qapp.primaryScreen().devicePixelRatio()
    p = pointer.to_logical(round(200 * dpr), round(100 * dpr))
    assert (p.x(), p.y()) == (200, 100)


# --- start with Windows (into a temp folder, never the real Startup folder) --------------------
def test_autostart_shortcut_created_and_removed(tmp_path, qapp):
    import subprocess
    from wk.ui import set_autostart
    lnk = tmp_path / "Jarvis Assistant.lnk"
    set_autostart(True, startup_dir=tmp_path)
    assert lnk.exists()
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); $s.TargetPath + '|' + $s.Arguments"],
                         capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
    target, args = out.split("|", 1)
    assert target.lower().endswith("pythonw.exe") and Path(target).exists()
    assert "jarvis_assistant.pyw" in args and args.endswith("--hidden")
    set_autostart(False, startup_dir=tmp_path)
    assert not lnk.exists()


# --- stylesheet images + friendly-name memory -------------------------------------------------------
def test_ui_images_drawn_and_style_has_no_leftover_tokens(tmp_path, qapp, monkeypatch):
    from wk import ui
    images = ui.ui_images(tmp_path)
    assert all(Path(p).exists() and Path(p).stat().st_size > 100 for p in images.values())
    monkeypatch.setattr(ui.config, "DATA_DIR", tmp_path)
    style = ui.build_style()
    assert "@" not in style.replace("@media", "")      # every image token replaced
    assert "\\\\" not in style                            # Qt wants forward slashes


def test_friendly_names_survive_a_restart(tmp_path, monkeypatch):
    from wk import sensors
    monkeypatch.setattr(sensors, "_DESCRIPTIONS", {})
    names = tmp_path / "app_names.json"
    names.write_text('{"chrome.exe": "Google Chrome"}', encoding="utf-8")
    sensors.remember_names_in(names)
    assert sensors.app_description("chrome.exe", scan=False) == "Google Chrome"
    assert sensors.app_description("never-seen.exe", scan=False) == ""


# --- only one Jarvis, ever ---------------------------------------------------------------------------
def test_single_instance_lock_blocks_a_second_process():
    import subprocess
    from wk import instance
    name = f"Local\\JarvisQA.test.{os.getpid()}.{time.time_ns()}"   # private name: never touches the real lock
    code = (f"import sys, time; sys.path.insert(0, r'{ROOT}'); from wk import instance; "
            f"print(instance.claim({name!r}), flush=True); time.sleep(30)")
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "True"
        assert instance.claim(name) is False            # the other process holds it
    finally:
        child.kill()
        child.wait()
    time.sleep(0.3)
    assert instance.claim(name) is True                 # released automatically, even though it was killed
    assert instance.claim(name) is True                 # asking again from the owner is fine
    instance.release(name)


def test_engine_refuses_to_start_while_another_jarvis_runs(qapp, monkeypatch):
    from wk import brain
    monkeypatch.setattr(brain.instance, "claim", lambda *a, **k: False)
    with pytest.raises(RuntimeError, match="already running"):
        brain.Engine()


# --- projects: the sandbox Jarvis works in ------------------------------------------------------------
def _project(tmp_path, with_source=True):
    source = tmp_path / "mine"
    (source / "docs").mkdir(parents=True, exist_ok=True)
    (source / "README.md").write_text("# My project\nhello", encoding="utf-8")
    (source / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return {"id": 1, "workspace": str(workspace), "source_dir": str(source) if with_source else ""}


def test_sandbox_reads_source_and_writes_only_workspace(tmp_path):
    from wk.projects import Sandbox
    box = Sandbox(_project(tmp_path))
    assert "README.md" in box.list_files("source")
    assert "hello" in box.read_file("source/README.md")
    assert "wrote" in box.write_file("workspace/notes/plan.md", "# plan")
    assert (tmp_path / "ws" / "notes" / "plan.md").read_text(encoding="utf-8") == "# plan"
    with pytest.raises(ValueError, match="read-only"):
        box.write_file("source/README.md", "overwritten")
    assert "hello" in (tmp_path / "mine" / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["../escape.txt", "workspace/../../escape.txt", "C:/Windows/win.ini",
                                 "/etc/passwd", "elsewhere/file.txt", "workspace/../mine/README.md"])
def test_sandbox_blocks_every_escape(tmp_path, bad):
    from wk.projects import Sandbox
    box = Sandbox(_project(tmp_path))
    with pytest.raises(ValueError):
        box.write_file(bad, "x")
    with pytest.raises(ValueError):
        box.read_file(bad)


def test_sandbox_refuses_binary_and_has_no_source_when_none_given(tmp_path):
    from wk.projects import Sandbox
    with pytest.raises(ValueError, match="binary"):
        Sandbox(_project(tmp_path)).read_file("source/blob.bin")
    with pytest.raises(ValueError):
        Sandbox(_project(tmp_path, with_source=False)).list_files("source")


def test_create_project_makes_workspace_and_logs(tmp_path, monkeypatch, store):
    from wk import projects
    monkeypatch.setattr(projects.config, "DATA_DIR", tmp_path)
    pid = projects.create_project(store, "Tidy README", "Make the README clear", "")
    p = store.project(pid)
    assert p["status"] == "active" and Path(p["workspace"]).is_dir()
    assert store.project_log(pid)[0][1] == "created"
    with pytest.raises(ValueError):
        projects.create_project(store, "", "goal")
    with pytest.raises(ValueError, match="folder not found"):
        projects.create_project(store, "x", "y", str(tmp_path / "missing"))


def test_next_project_picks_the_least_recently_worked(store, tmp_path):
    a = store.add_project("a", "g", "", str(tmp_path))
    b = store.add_project("b", "g", "", str(tmp_path))
    store.project_worked(a)
    assert store.next_project() == b
    store.set_project_status(b, "paused")
    assert store.next_project() == a


# --- phone actions: fixed names, typed and bounded fields ----------------------------------------------
class _FakeEngine:
    def __init__(self, store):
        from types import SimpleNamespace
        self.store, self.cfg, self.calls = store, {"keep_pc_awake": True, "digest_minutes": 60}, []
        self.data_changed = SimpleNamespace(emit=lambda *_: None)
        self.set_watching = lambda on, minutes=0: self.calls.append(("watch", on, minutes))
        self.save_config = lambda cfg: self.calls.append(("cfg", cfg))
        self.create_project = lambda t, g, s: self.calls.append(("project", t, g, s)) or 7

        def status(pid, st):
            if st not in ("active", "paused", "done"):
                raise ValueError("status must be active, paused or done")
            self.calls.append(("status", pid, st))
        self.set_project_status = status


class _NowGui:
    def call(self, fn, timeout=15):
        return fn()


def test_phone_actions_do_the_right_thing(store):
    from wk.remote_api import run_action
    eng, gui = _FakeEngine(store), _NowGui()
    assert run_action(eng, gui, {"action": "add_fact", "text": "likes amber"})["message"] == "Remembered"
    assert store.facts()[0][1] == "likes amber"
    run_action(eng, gui, {"action": "add_reminder", "text": "stretch", "minutes": 20})
    assert store.reminders()[0][2] == "stretch"
    run_action(eng, gui, {"action": "pause", "minutes": 30})
    run_action(eng, gui, {"action": "set_setting", "key": "keep_pc_awake", "value": False})
    assert ("watch", False, 30) in eng.calls and eng.calls[-1][1]["keep_pc_awake"] is False
    assert run_action(eng, gui, {"action": "project_add", "title": "t", "goal": "g"})["id"] == 7
    assert ("project", "t", "g", "") in eng.calls          # a folder can never be set from the phone


@pytest.mark.parametrize("body, error", [
    ({"action": "run_shell", "cmd": "dir"}, "unknown action"),
    ({"action": "add_fact", "text": ""}, "required"),
    ({"action": "add_fact", "text": "x" * 600}, "too long"),
    ({"action": "add_fact", "text": 5}, "must be text"),
    ({"action": "add_reminder", "text": "a", "minutes": 0}, "whole number"),
    ({"action": "add_reminder", "text": "a", "minutes": "5"}, "whole number"),
    ({"action": "pause", "minutes": True}, "whole number"),
    ({"action": "set_watching", "on": "yes"}, "true or false"),
    ({"action": "set_setting", "key": "llm_server_exe", "value": "evil.exe"}, "changed from the phone"),
    ({"action": "set_setting", "key": "folders", "value": ["C:/"]}, "changed from the phone"),
    ({"action": "set_setting", "key": "digest_minutes", "value": 5}, "whole number"),
    ({"action": "project_status", "id": 1, "status": "deleted"}, "active, paused or done"),
])
def test_phone_actions_reject_bad_input(store, body, error):
    from wk.remote_api import run_action
    with pytest.raises(ValueError, match=error):
        run_action(_FakeEngine(store), _NowGui(), body)


# --- away mode decisions --------------------------------------------------------------------------------
def _away_self():
    from types import SimpleNamespace
    calls = []
    me = SimpleNamespace(
        cfg={"idle_seconds": 300, "away_model_enabled": True, "away_model_after_minutes": 10},
        models=SimpleNamespace(active="small", busy=False, switch_async=calls.append),
        projects=SimpleNamespace(busy=False, stop_requested=False),
        system_away_since=None, _next_big_attempt=0.0)
    return me, calls


def test_away_mode_swaps_up_once_after_the_delay_and_back_on_return():
    from wk.brain import Engine
    me, calls = _away_self()
    Engine._away_mode(me, 10_000, 400, 5)             # away 400 s: not long enough
    assert calls == []
    Engine._away_mode(me, 10_300, 700, 5)             # away ~11.7 min: swap up
    Engine._away_mode(me, 10_340, 740, 5)             # still away, before the retry interval
    assert calls == ["big"]
    me.models.active, me.projects.busy = "big", True
    Engine._away_mode(me, 10_500, 2, 5)               # you're back
    assert calls == ["big", "small"] and me.projects.stop_requested and me.system_away_since is None


def test_away_mode_respects_the_switch_and_counts_sleep_as_away():
    from wk.brain import Engine
    me, calls = _away_self()
    me.cfg["away_model_enabled"] = False
    Engine._away_mode(me, 10_000, 5000, 5)
    assert calls == []
    me2, _ = _away_self()
    Engine._away_mode(me2, 10_000, 1, 3600)            # PC slept an hour: the tick gap counts as away
    assert me2.system_away_since is not None


# --- model manager: VRAM and ComfyUI decisions ------------------------------------------------------------
def _manager(monkeypatch, free_mb, comfy_ok=False):
    from wk import models
    events = []
    m = models.ModelManager({"away_model_vram_mb": 9500, "small_model_vram_mb": 3000, "away_free_comfyui": True,
                             "comfyui_url": "http://127.0.0.1:1", "small_model_idle_seconds": 0}, "log", events.append)
    frees = iter(free_mb)
    monkeypatch.setattr(models.sensors, "system_stats", lambda: {"vram_total": 16000,
                          "vram_used": 16000 - next(frees), "gpu": 0})
    monkeypatch.setattr(m, "our_server_pids", lambda: [1])
    monkeypatch.setattr(m, "_comfyui_idle_unload", lambda: comfy_ok)
    monkeypatch.setattr(models.time, "sleep", lambda s: None)
    return m, events


def test_big_model_loads_only_if_it_fits(monkeypatch):
    m, events = _manager(monkeypatch, [7000])                          # 7 GB free + 3 GB from the small model
    assert m.room_for_big()
    m, events = _manager(monkeypatch, [2000, 2000])                    # won't fit, ComfyUI busy/absent
    assert not m.room_for_big() and "Deferred Bonsai 2" in events[0]
    m, events = _manager(monkeypatch, [1300, 12000], comfy_ok=True)    # idle ComfyUI unloaded -> fits
    assert m.room_for_big() and "ComfyUI" in events[0]


def test_qa_safety_switch_blocks_all_model_control(monkeypatch):
    from wk import models
    monkeypatch.setenv("JARVIS_NO_MODEL_CONTROL", "1")
    m = models.ModelManager({"away_free_comfyui": True, "comfyui_url": "http://127.0.0.1:1"}, "log", lambda e: None)
    monkeypatch.setattr(m, "_stop_ours", lambda: (_ for _ in ()).throw(AssertionError("must not stop servers")))
    monkeypatch.setattr(m, "_start", lambda *a: (_ for _ in ()).throw(AssertionError("must not start servers")))
    m.switch("big")                                   # silently does nothing
    assert m.active == "small" and m._comfyui_idle_unload() is False


def test_model_manager_never_adopts_or_stops_foreign_port_owner(monkeypatch, tmp_path):
    from wk import models
    monkeypatch.setattr(models, "model_control_allowed", lambda: True)
    events = []
    m = models.ModelManager({"llm_base_url": "http://127.0.0.1:8081/v1",
                             "llm_autostart_server": True}, tmp_path / "server.log", events.append)
    killed = []

    class ForeignProcess:
        pid = 12345
        def create_time(self): return 100.0
        def exe(self): return str(tmp_path / "foreign" / "llama-server.exe")
        def cmdline(self): return [self.exe(), "-m", "foreign.gguf", "--port", "8081"]
        def kill(self): killed.append(self.pid)

    monkeypatch.setattr(models.psutil, "Process", lambda pid: ForeignProcess())
    monkeypatch.setattr(m, "_port_in_use", lambda: True)
    m.owner_path.write_text('{"pid":12345,"created":100,"exe":"other.exe","model":"foreign.gguf"}', encoding="utf-8")
    assert m.our_server_pids() == []
    m._stop_ours()
    m.startup()
    m.switch("big")
    assert not killed and any("another service" in event for event in events)


def test_model_manager_recognizes_only_recorded_process_identity(monkeypatch, tmp_path):
    from wk import models
    exe = tmp_path / "llama-server.exe"
    model = str(tmp_path / "bonsai.gguf")
    m = models.ModelManager({"llm_base_url": "http://127.0.0.1:8097/v1"},
                            tmp_path / "server.log", lambda _: None)
    killed = []

    class OwnedProcess:
        pid = 45678
        def create_time(self): return 200.0
        def exe(self): return str(exe)
        def cmdline(self): return [str(exe), "-m", model, "--port", "8097"]
        def kill(self): killed.append(self.pid)

    monkeypatch.setattr(models.psutil, "Process", lambda pid: OwnedProcess())
    m.owner_path.write_text(json.dumps({"pid": 45678, "created": 200.0,
                                        "exe": str(exe), "model": model}), encoding="utf-8")
    assert m.our_server_pids() == [45678]
    original_kill = OwnedProcess.kill
    def stop_once(self):
        original_kill(self)
        m.owner_path.unlink()
    monkeypatch.setattr(OwnedProcess, "kill", stop_once)
    m._stop_ours()
    assert killed == [45678] and not m.owner_path.exists()
    m.owner_path.write_text(json.dumps({"pid": 45678, "created": 201.0,
                                        "exe": str(exe), "model": model}), encoding="utf-8")
    assert m.our_server_pids() == [] and killed == [45678]


def test_consult_cli_is_bounded_and_uses_current_question_only(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from wk import delegate_tools
    monkeypatch.setattr(delegate_tools.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(delegate_tools.shutil, "which", lambda name: f"C:/tools/{name}.cmd")
    seen = []

    def fake_run(args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="Review result")

    monkeypatch.setattr(delegate_tools.subprocess, "run", fake_run)
    assert delegate_tools.consult("codex", "Inspect this issue") == "Review result"
    args, kwargs = seen[-1]
    assert "read-only" in args and "--ephemeral" in args and kwargs["input"] == "Inspect this issue"
    assert kwargs["cwd"] == tmp_path / "consultations"
    assert delegate_tools.consult("claude", "Compare the UI") == "Review result"
    args, kwargs = seen[-1]
    assert "--restricted" in args and "--strict-mcp-config" in args
    assert "--tools" in args and args[args.index("--tools") + 1] == ""
    assert kwargs["input"] == "Compare the UI"


def test_8b_consultation_choice_and_planner_failure(monkeypatch, store):
    from wk import delegate_tools
    class FakeLLM:
        model = "bonsai-8b"
        def online(self): return True
        def chat(self, messages, max_tokens): return "Checked answer"
    llm = FakeLLM()
    monkeypatch.setattr(delegate_tools, "available", lambda: {"claude": True, "codex": True})
    monkeypatch.setattr(delegate_tools, "chat_json", lambda *a, **kw: {"action": "consult_codex", "answer": ""})
    questions = []
    monkeypatch.setattr(delegate_tools, "consult", lambda provider, question: questions.append((provider, question)) or "Evidence")
    cfg = {"tool_use_8b": True, "tool_daily_limit": 1}
    assert delegate_tools.maybe_consult(llm, [{"role": "user", "content": "old"}], "new question", cfg, store) == "Checked answer"
    assert questions == [("codex", "new question")]
    assert delegate_tools.maybe_consult(llm, [], "another", cfg, store) is None
    monkeypatch.setattr(delegate_tools, "chat_json", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("planner")))
    assert delegate_tools.maybe_consult(llm, [], "new", {"tool_use_8b": True, "tool_daily_limit": 2}, store) is None


def test_process_stop_requires_matching_identity(monkeypatch):
    from wk import resource_tools
    stopped = []
    class FakeProcess:
        def create_time(self): return 1234.125
        def name(self): return "worker.exe"
        def terminate(self): stopped.append(True)
        def wait(self, timeout): return 0
    monkeypatch.setattr(resource_tools.psutil, "Process", lambda pid: FakeProcess())
    assert "identity changed" in resource_tools.stop_command("/stop 1234 1235.125")
    assert not stopped
    assert "Stopped worker.exe" in resource_tools.stop_command("/stop 1234 1234.125")
    assert stopped == [True]


def test_memory_candidate_requires_review_before_recall(monkeypatch, store):
    from wk import memory_intake
    from wk.behavior import memory_context
    class FakeLLM:
        def online(self): return True
    monkeypatch.setattr(memory_intake, "chat_json", lambda *a, **kw: {
        "keep": True, "fact": "Shawn prefers concise technical reports", "reason": "stable preference"})
    cfg = {**config.DEFAULTS, "memory_fact_mode": "all", "memory_capture_mode": "suggest"}
    assert memory_intake.propose(FakeLLM(), store, "I prefer concise technical reports", cfg)
    assert len(store.fact_candidates()) == 1 and store.facts() == []
    assert "concise technical reports" not in memory_context(store, "reports", cfg)
    candidate_id = store.fact_candidates()[0][0]
    assert store.resolve_candidate(candidate_id, True)
    assert "concise technical reports" in memory_context(store, "reports", cfg)
    assert not memory_intake.propose(FakeLLM(), store, "I prefer concise technical reports", cfg)
    cfg["memory_capture_mode"] = "off"
    assert not memory_intake.propose(FakeLLM(), store, "I prefer another style", cfg)

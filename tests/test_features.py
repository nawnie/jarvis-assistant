"""Unit tests for the newer features: GPU traffic, render watcher, quick actions, wrap-up,
game catalog / diagnosis, hotkey parsing, and the autoplay pipeline (dry run only).

Deterministic: no model, no ComfyUI, no real keyboard/mouse input, no global hooks.
Run:  .venv\\Scripts\\python.exe -m pytest tests\\test_features.py -q
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from wk import actions, config, gpu_traffic, hotkeys, wrapup  # noqa: E402
from wk.games import catalog, diagnose  # noqa: E402
from wk.games.autoplay import guard as ap_guard  # noqa: E402
from wk.games.autoplay import inputs, policy  # noqa: E402
from wk.games.autoplay.session import AutoplaySession  # noqa: E402
from wk.store import Store  # noqa: E402


# ===========================================================================
# hotkeys
# ===========================================================================
def test_hotkey_parse_letters_named_and_function_keys():
    mods, vk = hotkeys.parse("ctrl+alt+s")
    assert vk == ord("S") and mods & hotkeys.MOD_CONTROL and mods & hotkeys.MOD_ALT and mods & hotkeys.MOD_NOREPEAT
    assert hotkeys.parse("ctrl+alt+end")[1] == 0x23
    assert hotkeys.parse("ctrl+shift+f5")[1] == 0x74
    assert hotkeys.pretty("ctrl+alt+end") == "Ctrl+Alt+End"


@pytest.mark.parametrize("bad", ["s", "ctrl+", "hyper+s", "ctrl+alt+nosuchkey", "ctrl+f99"])
def test_hotkey_parse_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        hotkeys.parse(bad)


# ===========================================================================
# GPU traffic + ComfyUI parsing
# ===========================================================================
def test_parse_queue_ids():
    running, pending = gpu_traffic.parse_queue({"queue_running": [[1, "a", {}, {}, []]],
                                                "queue_pending": [[2, "b", {}, {}, []], [3, "c"]]})
    assert running == ["a"] and pending == ["b", "c"]
    assert gpu_traffic.parse_queue({}) == ([], [])


def test_parse_history_success_duration_and_outputs():
    entry = {"status": {"status_str": "success", "completed": True, "messages": [
                ["execution_start", {"timestamp": 1_000_000}], ["execution_success", {"timestamp": 1_098_000}]]},
             "outputs": {"9": {"images": [{"filename": "a_0001.png", "subfolder": "", "type": "output"}]},
                         "12": {"gifs": [{"filename": "clip.mp4", "subfolder": "vid", "type": "output"}]}}}
    result = gpu_traffic.parse_history_entry("p1", entry)
    assert result.ok and result.seconds == 98
    assert [o["filename"] for o in result.outputs] == ["a_0001.png", "clip.mp4"]
    assert result.outputs[1]["kind"] == "gifs"


def test_parse_history_error_names_the_node():
    entry = {"status": {"status_str": "error", "messages": [
        ["execution_start", {"timestamp": 5}],
        ["execution_error", {"timestamp": 9, "node_type": "UnetLoaderGGUF",
                             "exception_message": "Model file not found: wan.gguf\n"}]]}}
    result = gpu_traffic.parse_history_entry("p2", entry)
    assert not result.ok and result.failed_node == "UnetLoaderGGUF" and "not found" in result.error


def test_output_dir_from_argv_variants():
    assert gpu_traffic.output_dir_from_argv(["main.py", "--output-directory", "D:/out"]) == "D:/out"
    assert gpu_traffic.output_dir_from_argv(["main.py", "--base-directory", r"C:\B"]).replace("\\", "/") == "C:/B/output"
    assert gpu_traffic.output_dir_from_argv([r"F:\Comfy\ComfyUI\main.py"]).replace("\\", "/") == "F:/Comfy/ComfyUI/output"
    assert gpu_traffic.output_dir_from_argv([]) is None


def test_advice_flags_idle_comfy_holding_vram_and_two_llama_servers():
    snap = {"vram_total": 16384, "vram_used": 15800,
            "comfy": [{"base": "http://127.0.0.1:8001", "port": 8001, "running": 0, "pending": 0, "torch_vram_mb": 9000}],
            "llama": [{"port": 8080, "models": ["qwen"]}, {"port": 8082, "models": ["bonsai"]}], "games": []}
    tips = gpu_traffic.advice(snap, {})
    texts = " ".join(t["text"] for t in tips)
    assert "idle but holding 8.8 GB" in texts and "2 llama.cpp servers" in texts and "VRAM left" in texts
    assert any(t["action"] == "comfy-free:http://127.0.0.1:8001" for t in tips)


def test_advice_is_calm_when_nothing_is_wrong():
    tips = gpu_traffic.advice({"vram_total": 16384, "vram_used": 2000, "comfy": [], "llama": [], "games": []}, {})
    assert len(tips) == 1 and tips[0]["level"] == "info"


def test_render_watcher_reports_a_job_once_and_ignores_old_history(monkeypatch):
    base = "http://127.0.0.1:8001"
    state = {"queue": {"queue_running": [[1, "job1", {}, {}, []]], "queue_pending": []}}
    history = {"job1": {"status": {"status_str": "success", "messages": []},
                        "outputs": {"9": {"images": [{"filename": "x.png"}]}}},
               "ancient": {"status": {"status_str": "success"}}}

    def fake_get(url, timeout=0):
        if url.endswith("/queue"):
            return state["queue"]
        if "/history/" in url:
            job = url.rsplit("/", 1)[1]
            return {job: history[job]}
        raise OSError("unexpected")
    monkeypatch.setattr(gpu_traffic, "_get_json", fake_get)
    monkeypatch.setattr(gpu_traffic, "probe_comfy", lambda b: {"up": b == base, "output_dir": "C:/out"})
    watcher = gpu_traffic.RenderWatcher({"comfyui_url": base})
    assert watcher.poll(now=1000) == []                    # job1 seen running
    state["queue"] = {"queue_running": [], "queue_pending": []}
    done = watcher.poll(now=1005)
    assert [(b, r.prompt_id, r.ok) for b, r in done] == [(base, "job1", True)]
    assert watcher.poll(now=1010) == []                    # reported once only; "ancient" never reported
    assert watcher.output_dir(base) == "C:/out"


def test_friendly_error_hides_paths():
    text = "Traceback...\nFileNotFoundError: C:\\Users\\Shawn\\models\\wan.gguf missing"
    assert gpu_traffic.friendly_error(text) == "FileNotFoundError: <path> missing"


# ===========================================================================
# games: catalog + diagnosis parsing
# ===========================================================================
def test_catalog_detects_bethesda_emulators_and_unknown():
    assert catalog.detect("SkyrimSE.exe").name == "Skyrim Special Edition"
    assert catalog.detect("pcsx2-qt.exe").family == "emulator"
    assert catalog.detect("duckstation-qt-x64-ReleaseLTCG.exe").family == "emulator"
    assert catalog.detect("yuzu.exe").name == "yuzu (Switch)"
    assert catalog.detect("notepad.exe") is None


CRASH_LOG = """Skyrim SSE v1.6.1170
CrashLoggerSSE v1-15-0-0 Jan  1 2025
Unhandled exception "EXCEPTION_ACCESS_VIOLATION" at 0x7FF6A1B2C3D4 SkyrimSE.exe+0ABCDEF
PROBABLE CALL STACK:
	[0] 0x7FF6A1B2C3D4 SkyrimSE.exe+0ABCDEF
	[1] 0x7FFB11111111 hdtSMP64.dll+0012345
	[2] 0x7FFB22222222 ntdll.dll+0000001
	[3] 0x7FFB33333333 po3_Tweaks.dll+0000abc
POSSIBLE RELEVANT OBJECTS:
	File: "Immersive Armors.esp"
	File: "Skyrim.esm"
"""


def test_parse_crash_log_finds_exception_modules_and_plugins():
    parsed = diagnose.parse_crash_log(CRASH_LOG)
    assert "EXCEPTION_ACCESS_VIOLATION" in parsed["exception"]
    assert parsed["modules"] == ["hdtSMP64.dll", "po3_Tweaks.dll"]      # game exe + ntdll left out
    assert "Immersive Armors.esp" in parsed["plugins"] and "Skyrim.esm" in parsed["plugins"]


def test_error_lines_dedupes_and_keeps_newest():
    log = "ok\nERROR: bios not found\nfine\nERROR: bios not found\nVulkan init failed\n"
    assert diagnose.error_lines(log) == ["ERROR: bios not found", "Vulkan init failed"]


def test_parse_wevtutil_picks_only_this_exe():
    text = ("Event[0]:\n  Date: 2026-09-24T20:11:03.1230000Z\n  Faulting application name: SkyrimSE.exe, version: 1.6\n"
            "  Faulting module name: hdtSMP64.dll, version: 1\n  Exception code: 0xc0000005\n"
            "Event[1]:\n  Date: 2026-09-23T10:00:00Z\n  Faulting application name: chrome.exe\n")
    crashes = diagnose.parse_wevtutil(text, "skyrimse.exe")
    assert crashes == [{"time": "2026-09-24T20:11:03", "app": "SkyrimSE.exe", "module": "hdtSMP64.dll",
                        "code": "0xc0000005"}]


def test_portable_switch_emulator_keys_are_found_next_to_the_exe(tmp_path, monkeypatch):
    exe_dir = tmp_path / "Kirby"
    (exe_dir / "user" / "keys").mkdir(parents=True)
    (exe_dir / "user" / "keys" / "prod.keys").write_text("x")
    (exe_dir / "user" / "log").mkdir()
    (exe_dir / "user" / "log" / "yuzu_log.txt").write_text("[ 0.1] Core <Error> shader cache failed\n")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata-empty"))
    facts, _evidence, files = diagnose.emulator_evidence("yuzu.exe", str(exe_dir))
    joined = " ".join(facts)
    assert "Switch keys" in joined and "1 file(s)" in joined and "none found" not in joined
    assert files and "yuzu_log.txt" in files[0] and "shader cache failed" in joined


def test_diagnose_unknown_program_still_answers(monkeypatch):
    monkeypatch.setattr(diagnose, "windows_crashes", lambda exe: [])
    diag = diagnose.diagnose("notepad.exe")
    assert diag.facts and "doesn't know this program" in diag.facts[0]


# ===========================================================================
# quick actions
# ===========================================================================
def test_actions_seed_defaults_and_reject_disallowed_kinds(tmp_path):
    path = tmp_path / "actions.json"
    loaded, problems = actions.load(path)
    assert path.exists() and len(loaded) == len(actions.DEFAULT_ACTIONS) and not problems
    path.write_text(json.dumps([
        {"id": "a", "label": "Fine", "kind": "url", "target": "https://x"},
        {"id": "b", "label": "Evil", "kind": "shell", "target": "del C:\\"},
        {"id": "a", "label": "Dup", "kind": "url", "target": "https://y"},
        {"label": "No id", "kind": "url", "target": "https://z"}]), encoding="utf-8")
    loaded, problems = actions.load(path)
    assert [a.label for a in loaded] == ["Fine"]
    assert any("isn't allowed" in p for p in problems) and any("duplicate" in p for p in problems)


def test_action_matching_from_ask_bar_phrases():
    acts = [actions.Action(**a) for a in actions.DEFAULT_ACTIONS]
    assert actions.match("open downloads", acts)[0].id == "downloads"
    assert actions.match("open my downloads", acts)[0].id == "downloads"
    assert actions.match("!free vram", acts)[0].id == "comfy-free"
    assert actions.match("launch skyrim", acts)[0].id == "skyrim"
    assert actions.match("open the quantum flux capacitor", acts)[0] is None
    assert actions.wants_action("open downloads") and actions.wants_action("!snip")
    assert not actions.wants_action("what is vram?")


def test_action_run_guards_urls_paths_and_builtins(tmp_path):
    values = {"x": str(tmp_path)}
    with pytest.raises(ValueError):
        actions.run(actions.Action("u", "U", "url", "file:///C:/Windows"), {}, values)
    with pytest.raises(ValueError):
        actions.run(actions.Action("o", "O", "open", str(tmp_path / "missing")), {}, values)
    with pytest.raises(ValueError):
        actions.run(actions.Action("b", "B", "builtin", "nope"), {}, values)
    assert actions.run(actions.Action("b", "B", "builtin", "hello"), {"hello": lambda: "hi"}, values) == "hi"


# ===========================================================================
# day wrap-up
# ===========================================================================
def test_wrapup_facts_from_a_day_of_records():
    store = Store(":memory:")
    start = 1_000_000.0
    store.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)",
              (start + 10, start + 3610, "code.exe", "ui.py - Watchkeeper"))
    store.run("INSERT INTO events(ts, kind, text) VALUES (?,?,?)", (start + 20, "render", "Finished in 98 s: 1 file(s)"))
    store.run("INSERT INTO events(ts, kind, text) VALUES (?,?,?)", (start + 30, "render", "Failed in X: oom"))
    store.add_reminder(start + 9_999_999, "call mum")
    facts = wrapup.day_facts(store, start, start + 86_400)
    assert facts["active_seconds"] == 3600 and facts["apps"][0][0] == "code.exe"
    assert facts["renders_ok"] == 1 and facts["renders_failed"] == 1 and facts["reminders_pending"] == 1
    md = wrapup.facts_markdown(facts)
    assert "1h 00m" in md and "1 finished, 1 failed" in md
    messages = wrapup.wrapup_messages(facts, "persona")
    assert messages[0]["content"] == "persona" and "First thing tomorrow" in messages[1]["content"]


def test_wrapup_clock_offers_once_per_day_after_the_time(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    clock = wrapup.WrapupClock({"wrapup_time": "21:30"})
    evening = time.mktime((2026, 9, 25, 22, 0, 0, 0, 0, -1))
    morning = time.mktime((2026, 9, 25, 9, 0, 0, 0, 0, -1))
    assert not clock.due(morning, idle_seconds=0)
    assert not clock.due(evening, idle_seconds=600)          # away from the PC: don't offer
    assert clock.due(evening, idle_seconds=0)
    clock.mark_offered(evening)
    assert not clock.due(evening + 60, idle_seconds=0)        # once per day


# ===========================================================================
# autoplay: inputs, policy, session (dry run + fake guard)
# ===========================================================================
def test_keys_normalise_and_typing_uses_shift_for_capitals():
    assert inputs.normalise_key("Escape") == "esc" and inputs.normalise_key("W") == "w"
    with pytest.raises(ValueError):
        inputs.normalise_key("hyperspace")
    driver = inputs.DryRunDriver()
    driver.type_text("Hi!", per_key_s=0)
    downs = [d for _, a, d in driver.log if a == "key_down"]
    assert downs == ["shift", "h", "i", "shift", "1"]
    assert not driver.held_keys


def test_release_all_lets_go_of_everything():
    driver = inputs.DryRunDriver()
    driver.key_down("w")
    driver.button("left", True)
    driver.release_all()
    assert not driver.held_keys and not driver.held_buttons
    assert ("key_up", "w") in [(a, d) for _, a, d in driver.log]


def test_is_ours_needs_both_injected_flag_and_jarvis_tag():
    assert ap_guard.is_ours(0x10, inputs.JARVIS_TAG, 0x10)
    assert not ap_guard.is_ours(0x00, inputs.JARVIS_TAG, 0x10)       # real hands
    assert not ap_guard.is_ours(0x10, 1234, 0x10)                     # other software (e.g. Steam Input)


def test_validate_step_limits():
    assert policy.validate_step({"do": "press", "key": "E"})["key"] == "e"
    for bad in ({"do": "hold", "key": "w", "ms": 999_999}, {"do": "press", "key": "nope"},
                {"do": "fly"}, {"do": "type", "text": ""}, {"do": "wait_for_text", "any": []},
                {"do": "look", "dx": 99_999}):
        with pytest.raises((ValueError, KeyError)):
            policy.validate_step(bad)


def test_script_policy_expands_repeats_in_order():
    script = {"name": "t", "steps": [{"do": "say", "text": "a"},
                                     {"do": "repeat", "times": 2, "steps": [{"do": "press", "key": "e"}]},
                                     {"do": "wait", "ms": 1}]}
    pol = policy.ScriptPolicy(script)
    seen = []
    while (steps := pol.next_steps(None, None)) is not None:
        seen += [s["do"] for s in steps]
    assert seen == ["say", "press", "press", "wait"]


def test_script_policy_rejects_a_bad_step_before_running():
    with pytest.raises(ValueError):
        policy.ScriptPolicy({"steps": [{"do": "say", "text": "ok"}, {"do": "hold", "key": "w", "ms": -5}]})


def test_shipped_scripts_all_validate():
    scripts = policy.available_scripts()
    assert len(scripts) >= 2
    for path, _name, _notes in scripts:
        policy.ScriptPolicy(path)


def test_parse_vision_reply_handles_fences_and_drops_bad_steps():
    reply = '```json\n{"say": "Walking to the door", "done": false, "actions": [' \
            '{"do": "hold", "key": "w", "ms": 800}, {"do": "teleport"}, {"do": "press", "key": "e"}]}\n```'
    say, done, steps = policy.parse_vision_reply(reply)
    assert say == "Walking to the door" and not done and [s["do"] for s in steps] == ["hold", "press"]
    assert policy.parse_vision_reply("I think you should press E") == ("", False, [])
    assert policy.parse_vision_reply('{"done": true}')[1] is True


class FakeGuard:
    """Stands in for OperatorGuard: no hooks. kill_after = stop once this many focus checks passed."""

    def __init__(self, target="game.exe", focus=None, kill_after=None):
        self.target, self._focus, self.kill_after = target, focus, kill_after
        self.checks, self.killed, self.started, self.closed = 0, "", False, False

    def start(self, grace_s=0):
        self.started = True

    def kill(self, reason="kill"):
        self.killed = reason

    def stop_reason(self):
        return self.killed

    def focused(self):
        self.checks += 1
        if self.kill_after and self.checks >= self.kill_after:
            self.killed = "kill switch"
        return self._focus(self.checks) if self._focus else True

    def close(self):
        self.closed = True


class FakeObs:
    def __init__(self, lines):
        self.lines = lines
        self.pixels = (b"", 0, 0)

    def sees(self, needles):
        return any(n.lower() in " ".join(self.lines).lower() for n in needles)


def _run(session):
    session.start()
    session.join(10)
    assert not session.is_alive()
    return session


def test_session_runs_a_script_to_the_end_in_dry_run(tmp_path):
    driver = inputs.DryRunDriver()
    frames = iter([FakeObs(["Loading"]), FakeObs(["Choose your RACE"])])
    script = {"name": "t", "steps": [{"do": "say", "text": "go"}, {"do": "press", "key": "e", "hold_ms": 10},
                                     {"do": "wait_for_text", "any": ["race"], "timeout_ms": 5000},
                                     {"do": "look", "dx": 100, "dy": 0, "ms": 100}]}
    guard = FakeGuard()
    status = []
    s = _run(AutoplaySession(policy.ScriptPolicy(script), driver, guard, tmp_path / "run",
                             on_status=lambda k, t: status.append(k), observe_fn=lambda: next(frames)))
    assert s.result == "finished" and s.steps_done == 4
    assert guard.started and guard.closed and not driver.held_keys
    moved = sum(d[0] for _, a, d in driver.log if a == "move")
    assert moved == 100                                      # the pan adds up to exactly dx
    log = (tmp_path / "run" / "log.jsonl").read_text(encoding="utf-8")
    assert '"kind": "say"' in log and '"kind": "end"' in log and "say" in status


def test_kill_during_a_long_hold_releases_the_key_fast(tmp_path):
    driver = inputs.DryRunDriver()
    script = {"steps": [{"do": "hold", "key": "w", "ms": 10_000}]}
    guard = FakeGuard(kill_after=5)                      # killed a few slices into the hold
    t0 = time.monotonic()
    s = _run(AutoplaySession(policy.ScriptPolicy(script), driver, guard, tmp_path / "run"))
    assert time.monotonic() - t0 < 2.0 and s.result == "kill switch"
    assert not driver.held_keys
    actions_seen = [(a, d) for _, a, d in driver.log]
    assert ("key_down", "w") in actions_seen and ("key_up", "w") in actions_seen


def test_losing_focus_pauses_and_releases_held_keys(tmp_path):
    driver = inputs.DryRunDriver()
    script = {"steps": [{"do": "hold", "key": "w", "ms": 400}]}
    # in focus, then out for a few checks mid-hold, then back
    guard = FakeGuard(focus=lambda n: not (6 <= n <= 9))
    status = []
    s = _run(AutoplaySession(policy.ScriptPolicy(script), driver, guard, tmp_path / "run",
                             on_status=lambda k, t: status.append(k)))
    assert s.result == "finished" and "paused" in status and "resumed" in status
    w_events = [a for _, a, d in driver.log if d == "w"]
    # pressed, let go at the pause, pressed again on resume, let go when the hold ends
    assert w_events == ["key_down", "key_up", "key_down", "key_up"]
    assert not driver.held_keys


def test_wait_for_text_timeout_stops_unless_optional(tmp_path):
    for optional, expected in ((False, "didn't see"), (True, "finished")):
        script = {"steps": [{"do": "wait_for_text", "any": ["never"], "timeout_ms": 300, "optional": optional}]}
        s = _run(AutoplaySession(policy.ScriptPolicy(script), inputs.DryRunDriver(), FakeGuard(),
                                 tmp_path / f"run{optional}", observe_fn=lambda: FakeObs(["nothing"])))
        assert s.result.startswith(expected)


def test_game_never_focused_ends_the_run_cleanly(tmp_path):
    s = AutoplaySession(policy.ScriptPolicy({"steps": [{"do": "wait", "ms": 1}]}), inputs.DryRunDriver(),
                        FakeGuard(focus=lambda n: False), tmp_path / "run", focus_wait_s=0.3)
    _run(s)
    assert "never came to the front" in s.result

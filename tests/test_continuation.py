"""Focused checks for opt-in features and fixed alert boundaries."""

import json
import subprocess

import numpy as np
import pytest

from wk import actions, captions, game_log, guardian, phone_alerts, routines, screenshot_recall


def test_routine_needs_saved_rule_and_respects_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(routines, "path", lambda: tmp_path / "routines.json")
    clock = routines.RoutineClock()
    assert clock.due("eden.exe", now=1000) == []
    items = [actions.Action("downloads", "Downloads", "open", "C:\\Downloads"),
             actions.Action("danger", "Danger", "builtin", "danger", confirm=True)]
    with pytest.raises(ValueError, match="asks each time"):
        routines.add("eden.exe", "danger", items)
    routines.add("eden.exe", "downloads", items)
    assert clock.due("eden.exe", now=1001) == []  # focus had not changed
    clock.due("chrome.exe", now=1002)
    assert clock.due("eden.exe", now=1003) == ["downloads"]
    clock.due("chrome.exe", now=1004)
    assert clock.due("eden.exe", now=1005) == []  # cooldown


def test_screenshot_watch_baselines_existing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(screenshot_recall, "watch_path", lambda: tmp_path / "watch.json")
    folder = tmp_path / "pictures"
    folder.mkdir()
    old = folder / "old.png"
    old.write_bytes(b"old")
    screenshot_recall.set_watched_folder(str(folder))
    watcher = screenshot_recall.FolderWatcher()
    assert watcher.poll() == []
    fresh = folder / "fresh.png"
    fresh.write_bytes(b"new")
    assert watcher.poll() == [fresh]
    assert watcher.poll() == []


def test_phone_queue_carries_no_user_text(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_alerts, "path", lambda: tmp_path / "phone-alerts.json")
    assert phone_alerts.record("reminder") == 1
    assert phone_alerts.record("download") == 2
    with pytest.raises(ValueError):
        phone_alerts.record("arbitrary message")
    saved = json.loads((tmp_path / "phone-alerts.json").read_text(encoding="utf-8"))
    assert saved["events"][0].keys() == {"sequence", "kind", "occurred_at"}


def test_guardian_baselines_new_startup_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(guardian, "baseline_path", lambda: tmp_path / "baseline.json")
    names = [{"Old"}, {"Old", "New"}]
    monkeypatch.setattr(guardian, "startup_names", lambda: names.pop(0))
    monkeypatch.setattr(guardian, "gpu_compute_processes", lambda: {})
    monkeypatch.setattr(guardian, "disk_warnings", lambda: [])
    monkeypatch.setattr(guardian, "physical_disk_warnings", lambda: [])
    guard = guardian.Guardian()
    assert guard.check(now=1000) == []
    assert guard.check(now=1001) == ["New Windows startup entry: New"]


def test_captions_force_cpu_translation_and_remove_temporary_wav(tmp_path, monkeypatch):
    model, exe = tmp_path / "model.bin", tmp_path / "whisper.exe"
    model.write_bytes(b"model")
    exe.write_bytes(b"exe")
    seen = {}

    def fake_run(command, **_kwargs):
        seen["command"] = command
        seen["wav"] = command[command.index("-f") + 1]
        return subprocess.CompletedProcess(command, 0, "translated words\n", "")

    monkeypatch.setattr(captions.subprocess, "run", fake_run)
    assert captions.transcribe(np.ones(16000, dtype=np.float32) * 0.1, model=model, exe=exe) == "translated words"
    assert "-tr" in seen["command"] and "-ng" in seen["command"]
    assert not captions.Path(seen["wav"]).exists()


def test_game_log_labels_focus_time():
    class Store:
        def app_totals(self, _start, _end):
            return [("starfield.exe", 3660), ("chrome.exe", 900)]

    text = game_log.summary(Store(), now=100000)
    assert "Starfield: 1h 1m" in text and "not full playtime" in text

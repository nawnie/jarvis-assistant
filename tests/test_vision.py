"""Tests for the Ctrl+Shift+click trigger and Jarvis's eyes (wk/vision.py).

Nothing here starts a real model server: subprocess.Popen is replaced with a tripwire in every
test that could reach it, so a failing test can never load a model or touch another program.
"""
import base64
import json
import os
import socket
import time

import pytest


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _fake_click():
    import ctypes
    from wk import pointer
    info = pointer.MSLLHOOKSTRUCT()
    info.pt.x, info.pt.y = 7, 9
    return info, ctypes.addressof(info)


def _hold(monkeypatch, *keys):
    """Pretend exactly these modifier keys are held down."""
    from wk import pointer
    monkeypatch.setattr(pointer, "_held", lambda vk: vk in keys)


# --- the trigger: Ctrl+Shift+click fires; plain Ctrl+click is left for the app ----------------------
def test_ctrl_shift_click_fires_and_is_swallowed(monkeypatch):
    from wk import pointer
    _hold(monkeypatch, pointer.VK_CONTROL, pointer.VK_SHIFT)
    fired = []
    hook = pointer.MouseTrigger(armed=lambda: "ctrl+shift", on_fire=lambda x, y: fired.append((x, y)))
    _, lp = _fake_click()
    assert hook._callback(0, pointer.WM_LBUTTONDOWN, lp) == 1
    assert hook._callback(0, pointer.WM_LBUTTONUP, lp) == 1
    assert fired == [(7, 9)]


@pytest.mark.parametrize("held", [("ctrl",), ("ctrl", "alt"), ("ctrl", "shift", "alt"), ("shift",), ()])
def test_ctrl_shift_combo_ignores_every_other_chord(monkeypatch, held):
    from wk import pointer
    vk = {"ctrl": pointer.VK_CONTROL, "shift": pointer.VK_SHIFT, "alt": pointer.VK_MENU}
    _hold(monkeypatch, *(vk[k] for k in held))
    hook = pointer.MouseTrigger(armed=lambda: "ctrl+shift", on_fire=lambda x, y: pytest.fail("fired"))
    _, lp = _fake_click()
    assert hook._callback(0, pointer.WM_LBUTTONDOWN, lp) != 1       # passed through to the app


def test_plain_ctrl_combo_still_leaves_ctrl_shift_alone(monkeypatch):
    from wk import pointer
    _hold(monkeypatch, pointer.VK_CONTROL, pointer.VK_SHIFT)
    hook = pointer.MouseTrigger(armed=lambda: "ctrl", on_fire=lambda x, y: pytest.fail("fired"))
    _, lp = _fake_click()
    assert hook._callback(0, pointer.WM_LBUTTONDOWN, lp) != 1


def test_trigger_default_and_labels():
    from wk import config, pointer
    assert config.DEFAULTS["explain_trigger"] == "ctrl+shift"
    assert pointer.trigger_label("ctrl+shift") == "Ctrl+Shift+click"
    assert pointer.trigger_label("ctrl+alt") == "Ctrl+Alt+click"
    assert pointer.trigger_label(None) == "Ctrl+Shift+click"


# --- the picture and the answer format ---------------------------------------------------------------
def test_marked_jpeg_is_a_jpeg_with_a_magenta_ring(qapp):
    from PySide6.QtGui import QImage
    from wk import vision
    w, h = 200, 120
    bgra = bytes([40, 40, 40, 0]) * (w * h)                           # a flat dark grey screen
    data = base64.b64decode(vision.marked_jpeg((bgra, w, h, 100, 60)))
    assert data[:3] == b"\xff\xd8\xff"                                 # JPEG signature
    img = QImage.fromData(data, "JPG")
    ring = img.pixelColor(100 + 18, 60)                                # a point on the ring, right of the spot
    centre = img.pixelColor(100, 60)                                   # the spot itself stays uncovered
    assert ring.red() > 150 and ring.blue() > 120 and ring.green() < 110
    assert abs(centre.red() - 40) < 20


def test_views_send_a_closeup_first_then_the_surroundings(qapp):
    from PySide6.QtGui import QImage
    from wk import vision
    w, h, px, py = 960, 600, 900, 40                                   # a click near the top-right corner
    bgra = bytes([40, 40, 40, 0]) * (w * h)
    close_b64, wide_b64 = vision.views((bgra, w, h, px, py))
    close = QImage.fromData(base64.b64decode(close_b64), "JPG")
    wide = QImage.fromData(base64.b64decode(wide_b64), "JPG")
    assert (wide.width(), wide.height()) == (w, h)
    assert close.width() == vision.CLOSE_OUT and close.height() == round(vision.CLOSE_H * vision.CLOSE_OUT / vision.CLOSE_W)
    # the close-up box is kept inside the capture, so the ring sits off-centre (to the right, near the top)
    scale = vision.CLOSE_OUT / vision.CLOSE_W
    rx, ry = (px - (w - vision.CLOSE_W)) * scale, py * scale
    ring = close.pixelColor(round(rx + 18 * scale), round(ry))
    assert ring.red() > 150 and ring.blue() > 120 and ring.green() < 110


def test_split_name():
    from wk import vision
    assert vision.split_name("NAME: Options button\nThat's the options menu.") == ("Options button",
                                                                                   "That's the options menu.")
    assert vision.split_name("NAME: **Kirby**\nThat's Kirby.")[0] == "Kirby"
    assert vision.split_name("That's a folder.") == ("", "That's a folder.")


# --- which eyes: the loaded 27B sees by itself; the Qwen eyes only ever run next to the 8B -----------
def _engine(tmp_path, active="small", **cfg_overrides):
    """A stand-in engine: dummy model files, a fake main model client, the chosen Bonsai 'loaded'."""
    from types import SimpleNamespace
    for name in ("server.exe", "model.gguf", "mmproj.gguf", "big-mmproj.gguf"):
        (tmp_path / name).write_bytes(b"x")
    cfg = {"llm_server_exe": str(tmp_path / "server.exe"), "vision_model_file": str(tmp_path / "model.gguf"),
           "vision_mmproj_file": str(tmp_path / "mmproj.gguf"), "vision_port": _free_port()}
    cfg.update(cfg_overrides)
    asked = []
    main_llm = SimpleNamespace(inflight=0, chat=lambda messages, **k: asked.append(messages) or "NAME: X\nThat's X.")
    models = SimpleNamespace(active=active, busy=False,
                             profile=lambda name: {"mmproj": str(tmp_path / "big-mmproj.gguf")})
    return SimpleNamespace(cfg=cfg, models=models, llm=main_llm, llm_online=True), asked


def _eyes(tmp_path, monkeypatch, active="small", **overrides):
    """Eyes over a stand-in engine, model control allowed, and Popen booby-trapped."""
    from wk import vision
    engine, asked = _engine(tmp_path, active, **overrides)
    monkeypatch.setattr(vision, "model_control_allowed", lambda: True)
    monkeypatch.setattr(vision.subprocess, "Popen", lambda *a, **k: pytest.fail("tried to start a model server"))
    return vision.Eyes(engine, data_dir=tmp_path), engine, asked


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_with_the_27b_loaded_nothing_else_starts_and_the_27b_is_asked(tmp_path, monkeypatch):
    eyes, engine, asked = _eyes(tmp_path, monkeypatch, active="big")
    assert eyes.mode() == "big" and eyes.label() == "BONSAI 27B"
    assert eyes.unavailable_reason() is None and not eyes.needs_loading()
    assert eyes.ensure_ready() == (True, "")                     # Popen tripwire: nothing was launched
    assert eyes.look(["abc"], "what is it?", "sys").startswith("NAME: X")
    image_part = asked[0][1]["content"][1]
    assert image_part["type"] == "image_url" and image_part["image_url"]["url"].endswith("abc")


def test_with_the_8b_loaded_the_qwen_eyes_are_used(tmp_path, monkeypatch):
    eyes, _, _ = _eyes(tmp_path, monkeypatch, active="small")
    assert eyes.mode() == "qwen" and eyes.label() == "QWEN 3B" and eyes.needs_loading()


def test_27b_without_its_vision_file_says_so(tmp_path, monkeypatch):
    eyes, engine, _ = _eyes(tmp_path, monkeypatch, active="big")
    (tmp_path / "big-mmproj.gguf").unlink()
    assert eyes.unavailable_reason() == "Bonsai 2 27B's vision file is missing"


def test_qwen_eyes_are_stopped_once_the_27b_is_loaded(tmp_path, monkeypatch):
    eyes, engine, _ = _eyes(tmp_path, monkeypatch, active="small")
    stopped = []
    monkeypatch.setattr(eyes, "_owned", lambda: object())
    monkeypatch.setattr(eyes, "stop", lambda: stopped.append(True))
    eyes.last_used = time.time()                     # just used - but the 27B now sees by itself
    engine.models.active = "big"
    assert "sees by itself" in eyes.unload_if_idle() and stopped


def test_eyes_refuses_a_port_someone_else_holds(tmp_path, monkeypatch):
    eyes, engine, _ = _eyes(tmp_path, monkeypatch)
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", engine.cfg["vision_port"]))
    squatter.listen(1)
    try:
        ok, reason = eyes.ensure_ready()
    finally:
        squatter.close()
    assert not ok and "being used by another program" in reason


def test_eyes_wont_load_when_the_gpu_is_full(tmp_path, monkeypatch):
    eyes, _, _ = _eyes(tmp_path, monkeypatch)
    monkeypatch.setattr(eyes, "_free_vram_mb", lambda: 1200)
    ok, reason = eyes.ensure_ready()
    assert not ok and "GPU only has" in reason


def test_eyes_off_switch_and_missing_files(tmp_path, monkeypatch):
    eyes, _, _ = _eyes(tmp_path, monkeypatch, vision_enabled=False)
    assert eyes.unavailable_reason() == "vision is switched off"
    eyes, _, _ = _eyes(tmp_path, monkeypatch, vision_model_file=str(tmp_path / "gone.gguf"))
    assert "vision model file is missing" in eyes.unavailable_reason()


def test_eyes_never_stops_a_process_it_did_not_record(tmp_path, monkeypatch):
    import psutil
    eyes, _, _ = _eyes(tmp_path, monkeypatch)
    # a record pointing at a real process (this test run) but with the wrong start time = not ours
    eyes.owner_path.write_text(json.dumps({"pid": os.getpid(), "created": 1.0, "exe": "x", "model": "y"}),
                               encoding="utf-8")
    monkeypatch.setattr(psutil.Process, "kill", lambda self: pytest.fail("killed a process that isn't ours"))
    assert eyes._owned() is None
    eyes.stop()
    assert not eyes.owner_path.exists()


@pytest.mark.parametrize("flag,wrong", [("--port", "9999"),
                                         ("--host", "0.0.0.0"),
                                         ("--mmproj", "wrong-projector.gguf")])
def test_eyes_owner_rejects_wrong_port_host_or_projector(tmp_path, monkeypatch, flag, wrong):
    from wk import vision
    eyes, engine, _ = _eyes(tmp_path, monkeypatch)
    model = engine.cfg["vision_model_file"]
    exe = engine.cfg["llm_server_exe"]
    cmd = [exe, "-m", model, "--mmproj", engine.cfg["vision_mmproj_file"],
           "--host", "127.0.0.1", "--port", str(engine.cfg["vision_port"]),
           "--alias", vision.ALIAS]

    class FakeProcess:
        def create_time(self):
            return 42.0

        def exe(self):
            return exe

        def cmdline(self):
            return cmd

        def kill(self):
            pytest.fail("vision killed a process with mismatched launch identity")

    fake = FakeProcess()
    monkeypatch.setattr(vision.psutil, "Process", lambda _pid: fake)
    eyes.owner_path.write_text(json.dumps({"pid": 123, "created": 42.0,
                                           "exe": exe, "model": model}), encoding="utf-8")
    assert eyes._owned() is fake
    cmd[cmd.index(flag) + 1] = wrong
    assert eyes._owned() is None
    eyes.stop()
    assert not eyes.owner_path.exists()


def test_qwen_eyes_unload_only_after_the_idle_time_or_under_gpu_pressure(tmp_path, monkeypatch):
    eyes, _, _ = _eyes(tmp_path, monkeypatch, vision_keep_minutes=10)
    stopped = []
    monkeypatch.setattr(eyes, "_owned", lambda: object())
    monkeypatch.setattr(eyes, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(eyes, "_free_vram_mb", lambda: 6000)
    eyes.last_used = time.time() - 60                  # used a minute ago, GPU has room: keep it
    assert eyes.unload_if_idle() is None and not stopped
    monkeypatch.setattr(eyes, "_free_vram_mb", lambda: 900)
    assert "needed the GPU memory" in eyes.unload_if_idle() and stopped   # a game wants the memory
    stopped.clear()
    monkeypatch.setattr(eyes, "_free_vram_mb", lambda: 6000)
    eyes.last_used = time.time() - 11 * 60             # unused for 11 minutes: give the VRAM back
    assert "unused" in eyes.unload_if_idle() and stopped


# --- the 27B: loads with its vision file, and stays when Shawn chose it ------------------------------
def test_27b_launches_with_its_vision_file(tmp_path, monkeypatch):
    from wk import models
    for name in ("server.exe", "big.gguf", "big-mmproj.gguf"):
        (tmp_path / name).write_bytes(b"x")
    cfg = {"llm_base_url": f"http://127.0.0.1:{_free_port()}/v1", "llm_server_exe": str(tmp_path / "server.exe"),
           "away_model_file": str(tmp_path / "big.gguf"), "away_model_alias": "bonsai-2-27b",
           "away_model_ctx": 8192, "away_model_mmproj_file": str(tmp_path / "big-mmproj.gguf"),
           "llm_model_file": str(tmp_path / "small.gguf"), "llm_model": "bonsai-8b", "llm_ctx": 8192}
    manager = models.ModelManager(cfg, tmp_path / "model-server.log", lambda text: None)
    launched = []

    class Stop(Exception):
        pass

    def fake_popen(args, **kwargs):
        launched.append(args)
        raise Stop()
    monkeypatch.setattr(models.subprocess, "Popen", fake_popen)
    with pytest.raises(Stop):
        manager._start("big")
    args = launched[0]
    assert args[args.index("--mmproj") + 1] == str(tmp_path / "big-mmproj.gguf")
    assert manager.profile("small").get("mmproj") is None      # the 8B has no vision file


def _away_engine(pinned):
    from types import SimpleNamespace
    calls = []
    me = SimpleNamespace(
        cfg={"idle_seconds": 300, "away_model_enabled": True, "away_model_after_minutes": 10,
             "llm_pinned_profile": pinned},
        models=SimpleNamespace(active="big", busy=False, switch_async=calls.append),
        projects=SimpleNamespace(busy=False, stop_requested=False),
        system_away_since=None, _next_big_attempt=0.0)
    return me, calls


def test_a_27b_shawn_loaded_himself_is_never_swapped_back_while_he_is_active():
    from wk.brain import Engine
    me, calls = _away_engine(pinned="big")
    Engine._away_mode(me, 10_000, 2, 5)                # he's active (idle 2 s) with the 27B he chose
    assert calls == []


def test_an_away_mode_27b_still_goes_back_to_8b_when_he_returns():
    from wk.brain import Engine
    me, calls = _away_engine(pinned=None)
    Engine._away_mode(me, 10_000, 2, 5)
    assert calls == ["small"]

"""GPU start contention and occupied Comfy queue safety checks."""
import os
import subprocess
import sys
import threading
from unittest import mock

import pytest

from wk import comfy_tools, gpu_coordination, models


def test_gpu_start_mutex_rejects_second_thread_promptly():
    entered = threading.Event()
    release = threading.Event()

    def owner():
        with gpu_coordination.hold():
            entered.set()
            release.wait(2)

    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert entered.wait(1)
        with pytest.raises(TimeoutError, match="another Jarvis GPU operation"):
            with gpu_coordination.hold(timeout_seconds=0.02):
                pytest.fail("the second start entered while the first held the mutex")
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex contract")
def test_gpu_start_mutex_rejects_second_process():
    child = ("from wk import gpu_coordination\n"
             "try:\n"
             "    with gpu_coordination.hold(timeout_seconds=0.05):\n"
             "        raise SystemExit(2)\n"
             "except TimeoutError:\n"
             "    raise SystemExit(0)\n")
    with gpu_coordination.hold():
        result = subprocess.run([sys.executable, "-c", child], capture_output=True,
                                text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_comfy_queue_activity_fails_closed_on_open_but_invalid_service():
    cfg = {"comfyui_url": "http://127.0.0.1:8188"}
    with mock.patch.object(comfy_tools.socket, "create_connection") as connect, \
            mock.patch.object(comfy_tools, "_request", return_value={"queue_running": [1], "queue_pending": []}):
        assert comfy_tools.queue_activity(cfg) == "busy"
        connect.assert_called_once()
    with mock.patch.object(comfy_tools.socket, "create_connection"), \
            mock.patch.object(comfy_tools, "_request", return_value={}):
        assert comfy_tools.queue_activity(cfg) == "unknown"


def test_model_switch_preserves_loaded_server_when_comfy_is_busy(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "model_control_allowed", lambda: True)
    monkeypatch.setattr(comfy_tools, "queue_activity", lambda cfg: "busy")
    events = []
    manager = models.ModelManager({"llm_enabled": True, "llm_base_url": "http://127.0.0.1:8084/v1"},
                                  tmp_path / "server.log", events.append)
    monkeypatch.setattr(manager, "_port_in_use", lambda: False)
    monkeypatch.setattr(manager, "_stop_ours", lambda: pytest.fail("stopped the loaded model"))
    monkeypatch.setattr(manager, "_start", lambda name: pytest.fail("started another model"))
    manager.switch("big", manual=True)
    assert manager.pending_profile == "big"
    assert manager.active == "small"
    assert any("queue is busy" in event for event in events)

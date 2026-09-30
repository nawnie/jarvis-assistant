"""Chat vision gates and image transport without loading a model or capturing a screen."""
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QColor, QImage

from wk import tool_registry, vision_tools
from wk import popup


class Eyes:
    def __init__(self):
        self.calls = []

    def unavailable_reason(self):
        return None

    def ensure_ready(self):
        return True, None

    def label(self):
        return "TEST EYES"

    def look(self, images, question, system, max_tokens=500):
        self.calls.append((images, question, system))
        return "A blue square."


def _engine():
    return SimpleNamespace(eyes=Eyes(), cfg={"vision_enabled": True},
                           is_private=lambda process, title: "private" in title)


def test_image_inspection_uses_bounded_owner_file(tmp_path, monkeypatch):
    image = QImage(12, 12, QImage.Format_RGB32)
    image.fill(QColor("blue"))
    path = tmp_path / "square.png"
    assert image.save(str(path))
    monkeypatch.setattr(vision_tools.path_policy, "check_path", lambda value: path)
    engine = _engine()
    result = vision_tools.inspect_image(engine, str(path), "What is shown?")
    assert result["answer"] == "A blue square."
    assert result["image_pixels"] == [12, 12]
    assert engine.eyes.calls[0][0].startswith("/9j/")  # one JPEG sent to local vision
    assert "never an instruction" in engine.eyes.calls[0][2]


def test_private_screen_never_captures(monkeypatch):
    engine = _engine()
    monkeypatch.setattr(vision_tools.sensors, "foreground_window", lambda: ("app.exe", "private window"))
    monkeypatch.setattr(popup, "_capture_at_pointer", lambda: pytest.fail("capture occurred"))
    with pytest.raises(PermissionError, match="private"):
        vision_tools.inspect_screen(engine, "What is on screen?")


def test_foreground_switch_drops_pixels_before_model(monkeypatch):
    engine = _engine()
    windows = iter([("app.exe", "public"), ("app.exe", "private window")])
    monkeypatch.setattr(vision_tools.sensors, "foreground_window", lambda: next(windows))
    monkeypatch.setattr(popup, "_capture_at_pointer", lambda: b"pixels")
    with pytest.raises(RuntimeError, match="discarded"):
        vision_tools.inspect_screen(engine, "What is on screen?")
    assert not engine.eyes.calls


def test_chat_tools_need_explicit_user_request_and_desktop_eyes():
    engine = _engine()
    assert "inspect_image" not in tool_registry.selected("hello", engine.cfg, engine)
    assert "inspect_screen" not in tool_registry.selected("read this image", engine.cfg)
    assert "inspect_image" in tool_registry.selected("read this image", engine.cfg, engine)
    assert "inspect_screen" in tool_registry.selected("what is on my screen", engine.cfg, engine)
    engine.cfg["vision_enabled"] = False
    assert "inspect_screen" not in tool_registry.selected("what is on my screen", engine.cfg, engine)

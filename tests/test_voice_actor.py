"""Tests for voice acting (wk/voice_actor.py) - no screen, OCR or audio needed (all faked)."""
import threading
import time

import pytest

from wk import voice_actor as va


def test_speaker_detection():
    assert va.split_speaker(["Magolor", "Kirby! You came back?", "Let's go."]) == \
        ("Magolor", "Kirby! You came back? Let's go.")
    assert va.split_speaker(["Elfilin: Over here!"]) == ("Elfilin", "Over here!")
    assert va.split_speaker(["The door is locked."]) == ("", "The door is locked.")
    assert va.split_speaker(["It is dark.", "You hear a noise."])[0] == ""        # sentence, not a name plate


def test_line_cleanup():
    assert va.clean_line("Then let's go. V") == "Then let's go."
    assert va.clean_line("Next!  ▼") == "Next!"


def test_casting_is_stable_and_matches_gender():
    cast = {}
    assert va.voice_for("Sonic", cast, "male") in va.MALE and va.voice_for("Sonic", cast) == cast["Sonic"]
    assert va.voice_for("Zelda", {}, "female") in va.FEMALE
    assert va.voice_for("", {}) == va.NARRATOR
    assert va.voice_for("Zelda", {"Zelda": "af_sky"}, "female") == "af_sky"      # Shawn's recast wins


class FakeSpeaker:
    def __init__(self):
        self.said, self.stops = [], 0

    def say(self, text, voice=None):
        self.said.append((text, voice))

    def stop(self):
        self.stops += 1


@pytest.fixture
def game(tmp_path, monkeypatch):
    monkeypatch.setattr(va, "store_path", lambda: tmp_path / "voice_act.json")
    monkeypatch.setattr(va, "POLL_S", 0.01)
    monkeypatch.setattr(va.sensors, "foreground_window", lambda: ("kirby.exe", "Kirby"))
    va.save_setup({"kirby.exe": {"region": [0, 0, 100, 50]}})


def test_actor_speaks_each_finished_line_once_and_cuts_off_on_skip(game):
    # a typewriter: the line grows, holds, then the player skips to the next one
    frames = ([["Magolor", "Kir"]] * 2 + [["Magolor", "Kirby! Hi!"]] * 6 +
              [["Magolor", "Kirby! Let's fix my ship."]] * 6 + [[]] * 50)
    feed = iter(frames)
    speaker = FakeSpeaker()
    actor = va.VoiceActor(lambda: speaker, reader=lambda region: next(feed, []),
                          gender_fn=lambda name, game: "male")
    actor.start("kirby.exe")
    time.sleep(0.6)
    actor.stop()
    lines = [text for text, _ in speaker.said]
    assert lines == ["Kirby! Hi!", "Kirby! Let's fix my ship."]            # never the half-typed "Kir"
    assert speaker.said[0][1] in va.MALE and speaker.said[0][1] == speaker.said[1][1]   # one voice per character
    assert va.load_setup()["kirby.exe"]["cast"]["Magolor"] == speaker.said[0][1]        # remembered


def test_actor_only_reads_while_that_game_is_in_front(game, monkeypatch):
    monkeypatch.setattr(va.sensors, "foreground_window", lambda: ("chrome.exe", "Browser"))
    speaker = FakeSpeaker()
    actor = va.VoiceActor(lambda: speaker, reader=lambda region: ["Hello there!"])
    actor.start("kirby.exe")
    time.sleep(0.2)
    actor.stop()
    assert speaker.said == []


def test_recast_by_chat_tool(game):
    from wk import pc_tools
    va.save_setup({"kirby.exe": {"region": [0, 0, 1, 1], "cast": {"Magolor": "af_river"}}})
    assert "am_puck" in pc_tools.cast_voice("Magolor", "am_puck")
    assert va.load_setup()["kirby.exe"]["cast"]["Magolor"] == "am_puck"
    assert "Unknown voice" in pc_tools.cast_voice("Magolor", "robot9000")
    assert threading.active_count() >= 1

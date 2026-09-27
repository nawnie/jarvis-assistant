"""Tests for the awareness features added 2026-09-26: crash doctor, controller presence, media,
the download helper hook and quick-ask's screen rules. No model, microphone or real event log needed."""

import pytest

EVENT_1000 = """<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System>
<Provider Name="Application Error"/><EventID>1000</EventID><TimeCreated SystemTime="2026-09-26T06:56:38.1234Z"/>
<EventRecordID>4242</EventRecordID></System><EventData>
<Data>eden.exe</Data><Data>0.0.0.0</Data><Data>6a1de306</Data><Data>ReShade64.dll_unloaded</Data>
<Data>6.8.0.2155</Data><Data>6a6f6bb3</Data><Data>c0000005</Data><Data>0000000000118430</Data><Data>75e4</Data>
<Data>01dd4d8408ad7df9</Data><Data>D:\\games\\Emulation\\_Emulators\\Eden\\eden.exe</Data><Data>ReShade64.dll</Data>
</EventData></Event>"""


# --- crash doctor ------------------------------------------------------------------------------------
def test_crash_event_is_decoded_with_the_known_culprit():
    import calendar
    import xml.etree.ElementTree as ET
    from wk import crash_doctor as cd
    crash = cd.parse(ET.fromstring(EVENT_1000))
    assert crash["kind"] == "crash" and crash["app"] == "eden.exe" and crash["module"] == "ReShade64.dll"
    assert crash["code"] == "0xc0000005" and "access violation" in crash["meaning"] and crash["unloaded"]
    assert any("ReShade" in h for h in crash["hints"])
    assert crash["when"] == calendar.timegm((2026, 9, 26, 6, 56, 38, 0, 0, 0))     # the log stamp is UTC
    assert cd.headline(crash) == "eden.exe crashed in ReShade64.dll"
    assert "Known culprit" in cd.facts_md(crash) and "cause" in cd.question(crash)


def test_crash_doctor_announces_each_crash_once_and_not_old_ones():
    from wk import crash_doctor as cd
    log = [{"event_id": 1000, "record": 1, "when": 1.0, "app": "old.exe", "kind": "crash"}]
    doctor = cd.CrashDoctor(reader=lambda: list(log))
    assert doctor.poll() == [] and doctor.recent[0]["app"] == "old.exe"       # first poll only learns
    log.insert(0, {"event_id": 1000, "record": 2, "when": 2.0, "app": "eden.exe", "kind": "crash"})
    assert [c["app"] for c in doctor.poll()] == ["eden.exe"]
    assert doctor.poll() == []                                               # announced once


# --- controller presence -----------------------------------------------------------------------------
def test_gamepad_deadzone():
    from wk import gamepad
    pad = gamepad._Gamepad()
    assert not gamepad.in_use(pad)                     # resting
    pad.sThumbLX = 3000                                # stick drift inside the deadzone
    assert not gamepad.in_use(pad)
    pad.sThumbLX = 20000                               # really pushed
    assert gamepad.in_use(pad)
    pad = gamepad._Gamepad(wButtons=0x1000)            # the A button
    assert gamepad.in_use(pad)


def test_idle_counts_the_gamepad(monkeypatch):
    from wk import gamepad, sensors
    monkeypatch.setattr(gamepad, "idle_seconds", lambda: 2.0)       # played 2 s ago
    assert sensors.idle_seconds() <= 2.0


# --- media ---------------------------------------------------------------------------------------------
def test_media_description():
    from wk import media
    now = {"title": "Dreamland", "artist": "Kirby OST", "app": "Spotify.exe", "playing": True}
    assert media.describe(now) == "Dreamland - Kirby OST (Spotify), playing"
    assert media.describe({"title": "", "artist": "", "app": "", "playing": False}) == ""
    assert media.describe(dict(now, playing=False)).endswith("paused")


def test_break_nudge_waits_for_a_fullscreen_video_but_warnings_do_not():
    from wk.brain import NudgeContext, decide_nudge
    cfg = {"nudge_cooldown_minutes": 20, "gpu_temp_alert_c": 85, "ram_alert_percent": 92, "break_after_minutes": 50}
    base = dict(process="vlc.exe", title="movie", active_minutes=120, idle_seconds=1, minutes_since_nudge=999,
                ram_percent=40, gpu_temp=60)
    assert decide_nudge(NudgeContext(**base, fullscreen=True, media_playing=True), cfg) is None
    assert decide_nudge(NudgeContext(**base, fullscreen=False, media_playing=True), cfg)[0] == "Time for a break"
    hot = dict(base, gpu_temp=90)
    assert decide_nudge(NudgeContext(**hot, fullscreen=True, media_playing=True), cfg)[0] == "GPU running hot"


# --- download helper hook ------------------------------------------------------------------------------
def test_new_files_reach_the_download_hook():
    from types import SimpleNamespace
    from wk.brain import Engine
    seen, events = [], []
    me = SimpleNamespace(watching=True, cfg={"watch_folders": True, "folders": ["X"]},
                         folders=SimpleNamespace(poll=lambda folders: [("C:\\Downloads", "Firmware.zip")]),
                         store=SimpleNamespace(add_event=lambda k, t: events.append(t)),
                         data_changed=SimpleNamespace(emit=lambda area: None),
                         file_hook=lambda folder, name: seen.append((folder, name)))
    Engine._poll_folders(me)
    assert seen == [("C:\\Downloads", "Firmware.zip")] and events


# --- quick-ask screen rules ----------------------------------------------------------------------------
@pytest.mark.parametrize("question, visual", [
    ("what's this?", True), ("what is on my screen", True), ("read this for me", True),
    ("which one should I pick", True), ("remind me to call mum at 5", False), ("how do I unzip files", False),
])
def test_visual_question_detection(question, visual):
    from wk import popup
    assert bool(popup.VISUAL_QUESTION.search(question)) == visual


def test_clipboard_image_question():
    from wk import popup
    assert popup.CLIPBOARD_IMAGE_QUESTION.search("explain the screenshot I copied")
    assert not popup.CLIPBOARD_IMAGE_QUESTION.search("what time is it")

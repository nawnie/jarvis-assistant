"""Tests for voice (wk/voice.py) and smart Recall (wk/semantic.py).

The wake-word test uses two synthetic clips in tests/fixtures (Kokoro "George" saying "Hey Jarvis..."
and the near-miss "Hey Travis...") and the real openWakeWord models when they're on this PC.
Smart Recall is tested with a fake embedder, so no server or model is needed.
"""
from pathlib import Path

import numpy as np
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


# --- voice: the pure parts ----------------------------------------------------------------------------
def test_endpoint_waits_for_you_to_start_then_ends_after_a_pause():
    from wk import voice
    ep = voice.Endpoint(start_s=3.0, silence_s=0.9, min_speech_s=0.3, max_s=15)
    assert all(ep.update(False) is None for _ in range(20))          # 1.6 s of quiet after "Hey Jarvis": still waiting
    assert all(ep.update(True) is None for _ in range(20))           # 1.6 s of speech
    verdicts = [ep.update(False) for _ in range(12)]                  # then a pause
    assert verdicts[-1] == "done" and verdicts.count("done") >= 1 and verdicts[8] is None


def test_endpoint_gives_up_if_you_never_speak():
    from wk import voice
    ep = voice.Endpoint(start_s=1.0)
    assert [ep.update(False) for _ in range(13)][-1] == "nothing"


def test_transcript_and_speech_cleanup():
    from wk import voice
    assert voice.clean_transcript(" [BLANK_AUDIO] Hey Jarvis, move the zips. ") == "move the zips."
    assert voice.clean_transcript("Jarvis open downloads") == "open downloads"
    spoken = voice.speakable("Moved **2** files to `D:\\Archive`.\n\n**Done:**\n- **move** a -> b")
    assert spoken == "Moved 2 files to D:\\Archive." and "Done" not in spoken
    assert voice.sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]


@pytest.mark.skipif(
    not (Path(r"F:\Ai_Models\AIWF\audio\wakeword\openwakeword") / "hey_jarvis_v0.1.onnx").exists()
    or not all((FIXTURES / name).exists() for name in ("hey_jarvis_16k.npy", "hey_travis_16k.npy")),
    reason="wake-word model or synthetic clips not available in this checkout",
)
def test_wake_word_fires_on_hey_jarvis_and_not_on_hey_travis():
    from wk import voice
    wake = voice.WakeWord()
    quiet = np.random.default_rng(1).normal(0, 120, 16000).astype(np.int16)

    def best(clip):
        wake.reset()
        audio = np.concatenate([quiet, np.load(FIXTURES / clip), quiet, quiet])
        return max(wake.score(audio[i:i + voice.CHUNK]) for i in range(0, len(audio) - voice.CHUNK + 1, voice.CHUNK))
    assert best("hey_jarvis_16k.npy") >= 0.5
    assert best("hey_travis_16k.npy") < 0.5


def test_microphone_picker_skips_virtual_devices_and_prefers_mme():
    from wk import voice
    apis = [{"name": "MME"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}]
    devs = [{"name": "Microsoft Sound Mapper - Input", "hostapi": 0, "max_input_channels": 2},
            {"name": "Microphone (Steam Streaming Microphone)", "hostapi": 0, "max_input_channels": 8},
            {"name": "Headset Microphone (USB)", "hostapi": 1, "max_input_channels": 1},
            {"name": "Headset Microphone (USB)", "hostapi": 0, "max_input_channels": 1},
            {"name": "Speakers", "hostapi": 0, "max_input_channels": 0},
            {"name": "Microphone (Realtek)", "hostapi": 2, "max_input_channels": 2}]      # WDM-KS: not used
    picks = voice.input_candidates(devs, apis)
    assert [p[0] for p in picks] == [3, 2, None]         # MME headset, then WASAPI headset, then Windows default
    assert picks[1][2] is not None                        # WASAPI gets auto-convert to 16 kHz


# --- smart Recall with a fake embedder ------------------------------------------------------------------
def _fake_embed(texts):
    """Deterministic 'meaning': counts of a few topic words, normalised."""
    topics = ("crash", "firmware", "code", "music")
    vecs = []
    for t in texts:
        t = t.lower()
        v = np.array([t.count(w) + (0.8 if w == "crash" and "error" in t else 0) for w in topics] + [0.05],
                     dtype=np.float32)
        vecs.append(v / np.linalg.norm(v))
    return np.array(vecs)


@pytest.fixture
def jstore(tmp_path):
    from wk.store import Store
    s = Store(tmp_path / "jarvis.db")
    s.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)",
          (1000, 1600, "chrome.exe", "Switch firmware download guide"))
    s.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)",
          (2000, 2600, "code.exe", "ui.py - code"))
    s.add_event("crash", "eden.exe crashed in ReShade64.dll")
    return s


def test_semantic_index_is_incremental_and_ranks_by_meaning(jstore, tmp_path):
    from wk import semantic
    calls = []
    index = semantic.SemanticIndex(jstore, lambda texts: calls.append(len(texts)) or _fake_embed(texts),
                                   tmp_path / "semantic.db")
    assert index.update() == 3 and index.update() == 0                  # nothing new the second time
    jstore.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)",
               (3000, 3300, "chrome.exe", "Switch firmware download guide"))   # seen title again
    assert index.update() == 0                                           # time updated, not re-embedded
    top = index.search("the firmware thing", limit=2, min_score=0.1)
    assert "firmware" in top[0][3].lower() and top[0][4] == 900.0       # both visits' time added up
    assert index.search("that error crash", limit=1, min_score=0.1)[0][1] == "event"


def test_smart_recall_fails_closed_when_the_server_cannot_run(jstore, tmp_path, monkeypatch):
    from wk import semantic
    monkeypatch.setattr(semantic, "model_control_allowed", lambda: False)
    recall = semantic.SmartRecall(jstore, lambda: "", data_dir=tmp_path)
    assert recall.search("anything") == [] and "disabled" in recall.problem

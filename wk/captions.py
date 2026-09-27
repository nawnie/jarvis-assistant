"""Explicit, local captions for the sound playing through the default speaker.

Audio is held in memory for one short chunk, passed to the CPU Whisper program
through a temporary WAV, and deleted immediately. No transcript is saved here.
"""

import subprocess
import tempfile
import threading
import wave
from pathlib import Path

import numpy as np

from .voice import AUDIO, WHISPER_EXE


MODEL = AUDIO / "stt" / "whisper.cpp" / "ggml-small-q5_1.bin"
RATE = 16000
CHUNK_SECONDS = 5


def transcribe(samples, *, model=MODEL, exe=WHISPER_EXE):
    """Translate one loopback chunk to English, including English speech."""
    if not Path(model).is_file() or not Path(exe).is_file():
        raise FileNotFoundError("The multilingual Whisper model or program is missing")
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    if audio.size == 0 or float(np.max(np.abs(audio))) < 0.005:
        return ""
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with tempfile.TemporaryDirectory(prefix="jarvis-captions-") as temp:
        wav = Path(temp) / "chunk.wav"
        with wave.open(str(wav), "wb") as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(RATE)
            file.writeframes(pcm.tobytes())
        result = subprocess.run(
            [str(exe), "-m", str(model), "-f", str(wav), "-l", "auto", "-tr", "-ng", "-nt", "-np", "-t", "4"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=75, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip()[-240:] or "Whisper failed")
        return " ".join(result.stdout.split())[:400]


class LiveCaptions:
    """Start only by user action; capture the selected speaker's loopback, never the microphone."""

    def __init__(self, on_line, on_error):
        self.on_line, self.on_error = on_line, on_error
        self._stop = threading.Event()
        self._thread = None

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running():
            return
        if not MODEL.is_file() or not WHISPER_EXE.is_file():
            raise FileNotFoundError("The multilingual Whisper model or program is missing")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="jarvis-captions")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=CHUNK_SECONDS + 2)
        self._thread = None

    def _run(self):
        try:
            import soundcard as sc

            speaker = sc.default_speaker()
            if speaker is None:
                raise RuntimeError("No default speaker is available")
            loopback = sc.get_microphone(speaker.id, include_loopback=True)
            if loopback is None:
                raise RuntimeError("Speaker loopback is unavailable")
            with loopback.recorder(samplerate=RATE) as recorder:
                while not self._stop.is_set():
                    samples = recorder.record(numframes=RATE * CHUNK_SECONDS)
                    if self._stop.is_set():
                        break
                    line = transcribe(samples)
                    if line:
                        self.on_line(line)
        except Exception as exc:
            self.on_error(str(exc))

"""Voice: say "Hey Jarvis", ask anything, and Jarvis answers out loud. All on the CPU - no GPU memory.

The pipeline, and what runs each step (measured on this PC, 2026-09-26):
  1. Wake word  - openWakeWord "hey_jarvis" (three tiny ONNX models, ~4 MB) runs on every 80 ms of
                  microphone audio: melspectrogram -> speech embedding -> wake-word score.
  2. Listening  - Silero VAD (1.7 MB) hears when you start and stop talking.
  3. Transcribe - whisper.cpp small.en q5_1 (190 MB), about 2 s for a sentence, word-perfect in tests.
  4. Think      - the normal chat path (Engine.chat_reply), so voice can do everything chat can,
                  including PC actions ("move the zips on D into Archive").
  5. Speak      - Kokoro-82M full precision, British male "George" (3.8 s of speech in ~1 s),
                  spoken sentence by sentence so the reply starts quickly. Piper "Alan" is the fallback.

The microphone is ignored while Jarvis is speaking or thinking, so it never wakes itself.
Nothing is recorded or kept: audio lives only in memory until it's transcribed (the one temporary
WAV for whisper is deleted straight after).

Settings (read with cfg.get, no config.py keys required):
  voice_enabled        True        listen for "Hey Jarvis"
  voice_wake_threshold 0.5         wake-word confidence needed (raise it if it wakes by mistake)
  voice_name           "bm_george" Kokoro voice (bm_daniel / bm_fable / bm_lewis are the other British men)
  voice_speed          1.0
  voice_max_seconds    15          longest request after the wake word
"""
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

AUDIO = Path(r"F:\Ai_Models\AIWF\audio")
WAKE_DIR = AUDIO / "wakeword" / "openwakeword"
WHISPER_EXE = AUDIO / "stt" / "whisper.cpp" / "Release" / "whisper-cli.exe"
WHISPER_MODEL = AUDIO / "stt" / "whisper.cpp" / "ggml-small.en-q5_1.bin"
KOKORO_MODEL = AUDIO / "tts" / "kokoro" / "kokoro-v1.0.onnx"
KOKORO_VOICES = AUDIO / "tts" / "kokoro" / "voices-v1.0.bin"
PIPER_EXE = Path(r"F:\Ai_Models\venvs\jarvis-piper\Scripts\piper.exe")
PIPER_VOICE = AUDIO / "tts" / "piper" / "en_GB-alan-medium.onnx"

RATE = 16000          # microphone sample rate (what every model here expects)
CHUNK = 1280          # 80 ms: openWakeWord's step
VAD_FRAME = 512       # Silero's window at 16 kHz
DEFAULTS = {"voice_enabled": True, "voice_wake_threshold": 0.5, "voice_name": "bm_george", "voice_speed": 1.0,
            "voice_max_seconds": 15}


def _session(path, threads=1):
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])


# ===========================================================================
# 1. Wake word: openWakeWord's streaming pipeline, run directly on onnxruntime
#    (the openwakeword package would also pull in SciPy and scikit-learn)
# ===========================================================================
class WakeWord:
    def __init__(self, folder=WAKE_DIR, keyword="hey_jarvis_v0.1.onnx"):
        self.mel = _session(Path(folder) / "melspectrogram.onnx")
        self.emb = _session(Path(folder) / "embedding_model.onnx")
        self.kw = _session(Path(folder) / keyword)
        self.kw_input = self.kw.get_inputs()[0].name
        self.reset()

    def reset(self):
        """Forget everything heard so far (after a wake-up, and after Jarvis has spoken)."""
        self.raw = np.zeros(0, dtype=np.int16)
        self.mel_buf = np.ones((76, 32), dtype=np.float32)
        # openWakeWord primes its feature buffer with embeddings of quiet noise; so do we
        noise = np.random.default_rng(0).integers(-1000, 1000, RATE * 4).astype(np.int16)
        spec = self._melspectrogram(noise)
        windows = [spec[i:i + 76] for i in range(0, spec.shape[0] - 75, 8)]
        self.features = self._embed(np.array(windows)[..., None].astype(np.float32))

    def _melspectrogram(self, samples):
        out = self.mel.run(None, {"input": samples[None, :].astype(np.float32)})[0]
        return np.squeeze(out) / 10 + 2               # openWakeWord's fixed scaling

    def _embed(self, windows):
        return self.emb.run(None, {"input_1": windows})[0].reshape(-1, 96)

    def score(self, chunk):
        """Feed 80 ms (1280 samples, int16); returns the wake-word confidence 0-1 for the latest audio."""
        self.raw = np.concatenate([self.raw, chunk])[-(CHUNK + 480):]     # 80 ms + 30 ms of context
        if len(self.raw) < 400:
            return 0.0
        self.mel_buf = np.vstack([self.mel_buf, self._melspectrogram(self.raw)])[-970:]
        window = self.mel_buf[-76:][None, :, :, None].astype(np.float32)
        self.features = np.vstack([self.features, self._embed(window)])[-120:]
        return float(self.kw.run(None, {self.kw_input: self.features[-16:][None].astype(np.float32)})[0].ravel()[0])


# ===========================================================================
# 2. Listening: Silero VAD + an endpoint rule ("you've finished talking")
# ===========================================================================
class SpeechDetector:
    def __init__(self, path=WAKE_DIR / "silero_vad.onnx"):
        self.vad = _session(path)
        self.reset()

    def reset(self):
        self.h = np.zeros((2, 1, 64), dtype=np.float32)
        self.c = np.zeros((2, 1, 64), dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)

    def speech_probability(self, chunk):
        """Highest speech probability within this chunk (int16 audio)."""
        self.pending = np.concatenate([self.pending, chunk.astype(np.float32) / 32768.0])
        best = 0.0
        while len(self.pending) >= VAD_FRAME:
            frame, self.pending = self.pending[:VAD_FRAME], self.pending[VAD_FRAME:]
            out, self.h, self.c = self.vad.run(None, {"input": frame[None, :], "sr": np.array(RATE, dtype=np.int64),
                                                      "h": self.h, "c": self.c})
            best = max(best, float(out.ravel()[0]))
        return best


class Endpoint:
    """Decides when a spoken request is over, from one speech/no-speech flag per 80 ms chunk.
    Waits up to `start_s` for you to begin (you often pause after "Hey Jarvis"), then ends after
    `silence_s` of quiet following at least `min_speech_s` of speech, or at `max_s` regardless."""

    def __init__(self, start_s=3.0, silence_s=0.9, min_speech_s=0.3, max_s=15.0, chunk_s=CHUNK / RATE):
        self.chunk_s, self.start_s, self.silence_s, self.min_speech_s, self.max_s = chunk_s, start_s, silence_s, min_speech_s, max_s
        self.elapsed = self.speech = self.quiet = 0.0

    def update(self, is_speech):
        """Returns None while listening, 'done' when the request has ended, 'nothing' if you never spoke."""
        self.elapsed += self.chunk_s
        if is_speech:
            self.speech += self.chunk_s
            self.quiet = 0.0
        else:
            self.quiet += self.chunk_s
        if self.speech < self.min_speech_s:
            return "nothing" if self.elapsed >= self.start_s else None
        if self.quiet >= self.silence_s or self.elapsed >= self.max_s:
            return "done"
        return None


# ===========================================================================
# 3. Transcribe with whisper.cpp
# ===========================================================================
def transcribe(samples):
    """int16 16 kHz audio -> text. The temporary WAV is deleted as soon as whisper has read it."""
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="jarvis-voice-")
    os.close(fd)
    try:
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(samples.astype(np.int16).tobytes())
        result = subprocess.run([str(WHISPER_EXE), "-m", str(WHISPER_MODEL), "-f", path, "-nt", "-np", "-l", "en",
                                 "-t", str(max(2, min(8, (os.cpu_count() or 4) // 2)))],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return clean_transcript(result.stdout)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def clean_transcript(text):
    """Drop whisper's non-speech tags and a leading 'Hey Jarvis' that sneaks in with the pre-roll."""
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text or "")
    text = " ".join(text.split())
    text = re.sub(r"^(hey|hi|okay|ok)?[\s,]*jarvis[\s,.!?:]*", "", text, flags=re.IGNORECASE)
    return text.strip()


# ===========================================================================
# 5. Speak: Kokoro (natural) with Piper as the fallback; markdown is cleaned for speech
# ===========================================================================
def speakable(reply, limit=600):
    """What to say out loud: the reply without markdown, links, code blocks or the 'Done:' action list."""
    text = reply.split("\n\n**Done:**")[0]
    text = re.sub(r"```.*?```", " (code is on screen) ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_`#>|]", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = " ".join(text.split())
    return text[:limit].rsplit(" ", 1)[0] + "..." if len(text) > limit else text


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


class Speaker:
    def __init__(self, voice="bm_george", speed=1.0):
        self.voice, self.speed = voice, speed
        self._kokoro = None
        self.stop_flag = threading.Event()

    def _tts(self):
        if self._kokoro is None and KOKORO_MODEL.exists():
            from kokoro_onnx import Kokoro
            session = _session(KOKORO_MODEL, threads=max(2, min(8, (os.cpu_count() or 4) // 2)))
            self._kokoro = Kokoro.from_session(session, str(KOKORO_VOICES))
        return self._kokoro

    def synth(self, sentence, voice=None):
        """One sentence -> (float32 samples, sample rate). voice: any Kokoro voice (default: Jarvis's own)."""
        tts = self._tts()
        if tts is not None:
            voice = voice or self.voice
            lang = "en-gb" if voice[:1] == "b" else "en-us"
            return tts.create(sentence, voice=voice, speed=self.speed, lang=lang)
        return self._piper(sentence)

    def _piper(self, sentence):
        result = subprocess.run([str(PIPER_EXE), "-m", str(PIPER_VOICE), "--output_raw"], input=sentence.encode("utf-8"),
                                capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0, 22050

    def say(self, text, voice=None):
        """Speak text; the next sentence is synthesised while the current one plays. stop() cuts it off."""
        import sounddevice as sd
        self.stop_flag.clear()
        parts = sentences(speakable(text))
        if not parts:
            return
        upcoming = self.synth(parts[0], voice)
        for i in range(len(parts)):
            if self.stop_flag.is_set():
                break
            samples, rate = upcoming
            sd.play(samples, rate)
            upcoming = self.synth(parts[i + 1], voice) if i + 1 < len(parts) else None
            sd.wait()
        sd.stop()

    def stop(self):
        import sounddevice as sd
        self.stop_flag.set()
        sd.stop()


# ===========================================================================
# Choosing a microphone. On Shawn's PC (2026-09-26) Windows' default recording device was the
# "Steam Streaming Microphone" - a virtual device from Steam Remote Play that won't open - so real
# microphones are tried first, through each Windows audio system that can resample to 16 kHz.
# ===========================================================================
NOT_A_MIC = ("steam streaming", "stereo mix", "sound mapper", "primary sound capture", "line in", "what u hear",
             "input ()")
HOST_ORDER = {"MME": 0, "Windows DirectSound": 1, "Windows WASAPI": 2}


def input_candidates(devices=None, hostapis=None):
    """[(device index or None, name, extra_settings)] to try in order; Windows' default comes last."""
    import sounddevice as sd
    devices = devices if devices is not None else sd.query_devices()
    hostapis = hostapis if hostapis is not None else sd.query_hostapis()
    found = []
    for i, d in enumerate(devices):
        api = hostapis[d["hostapi"]]["name"]
        if d["max_input_channels"] < 1 or api not in HOST_ORDER or any(s in d["name"].lower() for s in NOT_A_MIC):
            continue
        extra = sd.WasapiSettings(auto_convert=True) if api == "Windows WASAPI" else None
        found.append((HOST_ORDER[api], i, f"{d['name']} ({api})", extra))
    found.sort(key=lambda f: (f[0], f[1]))
    return [(i, name, extra) for _, i, name, extra in found] + [(None, "Windows default input", None)]


# ===========================================================================
# The listener: microphone -> wake word -> request -> answer, on its own threads
# ===========================================================================
class Voice:
    """on_event(kind, text) is called from the voice thread: kinds are 'wake', 'heard', 'answer',
    'nothing', 'error', 'state'. reply_fn(text) -> reply is Jarvis's chat (blocking; runs here, off the GUI)."""

    def __init__(self, cfg_fn, reply_fn, on_event):
        self.cfg_fn, self.reply_fn, self.on_event = cfg_fn, reply_fn, on_event
        self.audio = queue.Queue(maxsize=200)
        self.state = "off"                # off / waiting / listening / thinking / speaking
        self.speaker = None
        self._stream = None
        self._thread = None
        self._stop = threading.Event()
        self._mic_problem_reported = False

    def get(self, key):
        return self.cfg_fn().get(key, DEFAULTS[key])

    def missing(self):
        """What's missing to run voice (a model file), or None. A missing microphone is handled by retrying."""
        for path in (WAKE_DIR / "hey_jarvis_v0.1.onnx", WHISPER_EXE, WHISPER_MODEL):
            if not Path(path).exists():
                return f"missing {path}"
        return None

    def start(self):
        """Start listening. The microphone is opened on the voice thread, and retried every minute if
        there isn't a usable one yet, so plugging a headset in later just works."""
        if self._thread is not None:
            return True
        problem = self.missing()
        if problem:
            self.on_event("error", f"Voice is off: {problem}")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="jarvis-voice")
        self._thread.start()
        return True

    def _open_microphone(self):
        """Try each real microphone until one opens. Returns its name, or None (and says why, once)."""
        import sounddevice as sd
        tried = []
        for device, name, extra in input_candidates():
            try:
                stream = sd.InputStream(device=device, samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK,
                                        callback=self._on_audio, extra_settings=extra)
                stream.start()
            except Exception as exc:
                tried.append(f"{name}: {str(exc).split('[')[0].strip()}")
                continue
            self._stream = stream
            return name
        if not self._mic_problem_reported:
            self._mic_problem_reported = True
            self.on_event("error", "Voice is waiting for a microphone - none could be opened (" + "; ".join(tried[:3]) +
                                   "). Plug in a headset or USB mic, or pick one in Windows Settings > Sound > Input; "
                                   "Jarvis will start listening by itself.")
        return None

    def stop(self):
        self._stop.set()
        if self.speaker:
            self.speaker.stop()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        self._stream, self._thread = None, None
        self._set_state("off")

    def _on_audio(self, data, frames, time_info, status):
        # the sound card's callback: hand the chunk over and return at once (drop it if the worker is behind)
        if self.state in ("waiting", "listening"):
            try:
                self.audio.put_nowait(data[:, 0].copy())
            except queue.Full:
                pass

    def _set_state(self, state):
        if state != self.state:
            self.state = state
            self.on_event("state", state)

    def _drain(self):
        while not self.audio.empty():
            try:
                self.audio.get_nowait()
            except queue.Empty:
                break

    def _run(self):
        try:
            wake = WakeWord()
            vad = SpeechDetector()
            self.speaker = Speaker(self.get("voice_name"), float(self.get("voice_speed")))
        except Exception as exc:
            self.on_event("error", f"Voice couldn't start: {exc}")
            return
        # this loop waits for a usable microphone (checked every minute until one appears)
        mic = None
        while mic is None and not self._stop.is_set():
            mic = self._open_microphone()
            if mic is None:
                self._stop.wait(60)
        if self._stop.is_set():
            return
        self.on_event("ready", mic)
        self._set_state("waiting")
        preroll = []
        while not self._stop.is_set():
            try:
                chunk = self.audio.get(timeout=0.5)
            except queue.Empty:
                continue
            # this is the waiting state: every 80 ms goes through the wake-word models
            preroll = (preroll + [chunk])[-4:]                 # ~0.3 s kept, so the first word isn't clipped
            if wake.score(chunk) < float(self.get("voice_wake_threshold")):
                continue
            self.on_event("wake", "")
            self._set_state("listening")
            heard = self._listen(vad, preroll)
            preroll = []
            if heard is None:
                self.on_event("nothing", "")
            else:
                self._answer(heard)
            wake.reset()
            self._drain()
            if not self._stop.is_set():
                self._set_state("waiting")

    def _listen(self, vad, preroll):
        """Collect the request after the wake word; returns int16 audio, or None if nothing was said."""
        vad.reset()
        endpoint = Endpoint(max_s=float(self.get("voice_max_seconds")))
        collected = list(preroll)
        while not self._stop.is_set():
            try:
                chunk = self.audio.get(timeout=1.0)
            except queue.Empty:
                continue
            collected.append(chunk)
            verdict = endpoint.update(vad.speech_probability(chunk) >= 0.5)
            if verdict == "nothing":
                return None
            if verdict == "done":
                return np.concatenate(collected)
        return None

    def _answer(self, samples):
        self._set_state("thinking")
        try:
            text = transcribe(samples)
        except Exception as exc:
            self.on_event("error", f"Couldn't transcribe: {exc}")
            return
        if not text:
            self.on_event("nothing", "")
            return
        self.on_event("heard", text)
        try:
            reply = self.reply_fn(text)
        except Exception as exc:
            reply = f"Sorry, I couldn't answer that: {exc}"
        self.on_event("answer", reply)
        self._set_state("speaking")
        try:
            self.speaker.say(reply)
        except Exception as exc:
            self.on_event("error", f"Couldn't speak: {exc}")
        time.sleep(0.3)                  # let the room's echo die down before listening again

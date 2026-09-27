"""Jarvis's eyes: vision for the explain click (Ctrl+Shift+click). Never more than two models at once.

Shawn's rules (2026-09-26): Jarvis's main model is Bonsai 8B OR Bonsai 2 27B (one at a time, on its
llama.cpp server - llama-server, PrismML build, port 8084); the 27B never loads by itself; and the
8B needs vision too. So the explain click sees in one of two ways, decided by which Bonsai is loaded:

  * Bonsai 2 27B loaded -> the 27B looks itself. models.py loads it WITH its vision tower (the 600 MB
    Qwen3.8-27B projector, models.BIG_MODEL_MMPROJ). Nothing else loads; the Qwen eyes below are
    stopped if they were running.
  * Bonsai 8B loaded -> Bonsai 8B can't see: it is built on Qwen3-8B, which has no vision, and no
    projector exists for it (the 27B's refuses to load on it: "mismatch between text model
    (n_embd = 4096) and mmproj (n_embd = 5120)"). So a small vision model, Qwen2.5-VL 3B, is loaded
    on the first explain click on its own llama-server (127.0.0.1:8087, Shawn Core reservation
    "jarvis-assistant-vision") and stopped again after `vision_keep_minutes` unused, when a game
    needs the GPU memory, when the 27B is loaded, and when Jarvis quits.

Measured 2026-09-26 on 8 test screens with the question and pictures used here:
  Bonsai 2 27B   8/8 correct, ~2.0 s per answer (it is the main model: no extra memory for vision)
  Qwen2.5-VL 3B  6/8 correct, ~0.7 s per answer, loads in ~5 s, +3.7 GB VRAM next to the 8B
                 (missed a small HUD counter and an icon-only gear button)

Ownership rule for the Qwen eyes server (the same one wk/models.py follows): only a server this module
launched AND recorded (pid + start time + exe + model file, in data/eyes-server.owner.json) is ever
adopted or stopped. If anything else holds the port, vision is skipped for that click (fail closed).

Settings are read with cfg.get(key, default), so no config.py keys are required:
  vision_enabled           True    look at the pixels when explaining a click
  vision_port              8087    Shawn Core reservation for the Qwen eyes (8085/8086 = review worker)
  vision_model_file / vision_mmproj_file   Qwen2.5-VL 3B + its projector (paths below)
  vision_min_free_mb       3500    only load the Qwen eyes if the GPU has this much free
  vision_keep_minutes      10      unload the Qwen eyes after this many minutes unused
  vision_release_below_mb  1500    unload them early when free GPU memory drops below this (a game)
"""
import base64
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import psutil

from . import config, sensors
from .llm import LocalLLM
from .models import model_control_allowed

# ---------------------------------------------------------------------------
# Defaults for the Qwen eyes (every one can be overridden in data/config.json)
# ---------------------------------------------------------------------------
VISION_DIR = Path(r"F:\Ai_Models\Language Models\AIWF LLM\GGUF\ggml-org\Qwen2.5-VL-3B-Instruct-GGUF")
DEFAULTS = {
    "vision_enabled": True,
    "vision_port": 8087,              # 8085/8086 are Codex's guarded review worker (wk/review.py)
    "vision_model_file": str(VISION_DIR / "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf"),
    "vision_mmproj_file": str(VISION_DIR / "mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf"),
    "vision_min_free_mb": 3500,         # measured +3.7 GB loaded
    "vision_keep_minutes": 10,
    "vision_release_below_mb": 1500,
}
ALIAS = "jarvis-eyes"
MARK = "#ff2bd6"          # the magenta ring drawn on the screenshot where you clicked
LOAD_TIMEOUT = 90         # seconds to wait for a cold start before giving up on vision for that click
LABELS = {"big": "BONSAI 27B", "qwen": "QWEN 3B"}


class Eyes:
    def __init__(self, engine, data_dir=None):
        self.engine = engine
        data = Path(data_dir or config.DATA_DIR)
        self.owner_path = data / "eyes-server.owner.json"
        self.log_path = data / "eyes-server.log"
        self.lock = threading.Lock()      # one start/stop of the Qwen eyes at a time
        self.starting = False             # True while the Qwen eyes are loading
        self.ready = False                # True once the Qwen eyes have answered their health check
        self.last_used = 0.0
        self.qwen = LocalLLM({"llm_base_url": self._url(), "llm_model": ALIAS, "llm_key_file": ""})

    # this is the settings section
    def get(self, key):
        return self.engine.cfg.get(key, DEFAULTS[key])

    def _port(self):
        return int(self.get("vision_port"))

    def _url(self):
        return f"http://127.0.0.1:{self._port()}/v1"

    # -----------------------------------------------------------------------
    # Which eyes: the loaded 27B itself, or the Qwen eyes next to the 8B
    # -----------------------------------------------------------------------
    def mode(self):
        """'big' when Bonsai 2 27B is the loaded model (it sees by itself), else 'qwen'."""
        return "big" if self.engine.models.active == "big" else "qwen"

    def label(self):
        return LABELS[self.mode()]

    def busy(self):
        """True while any vision work is running (drives the HUD reactor's 'thinking' spin)."""
        return self.starting or bool(self.qwen.inflight)

    def unavailable_reason(self):
        """None if an explain click can use vision right now, else a short plain-English reason.
        Cheap (no network): safe on the GUI thread."""
        if not self.get("vision_enabled"):
            return "vision is switched off"
        models = self.engine.models
        if models.busy:
            return "the model is switching"
        if self.mode() == "big":
            projector = models.profile("big").get("mmproj")
            if not projector or not Path(projector).exists():
                return "Bonsai 2 27B's vision file is missing"
            if not getattr(self.engine, "llm_online", True):
                return "Bonsai 2 27B is offline"
            return None
        if not model_control_allowed():
            return "model control is disabled for this copy of Jarvis"
        exe = self.engine.cfg.get("llm_server_exe", "")
        for what, path in (("model server", exe), ("vision model", self.get("vision_model_file")),
                           ("vision projector", self.get("vision_mmproj_file"))):
            if not path or not Path(path).exists():
                return f"the {what} file is missing ({path or 'not set'})"
        return None

    # -----------------------------------------------------------------------
    # The Qwen eyes server: only ever ours (recorded), never anyone else's
    # -----------------------------------------------------------------------
    def _owned(self):
        """The recorded eyes server if it is still exactly the process we launched, else None."""
        try:
            owner = json.loads(self.owner_path.read_text(encoding="utf-8"))
            proc = psutil.Process(int(owner["pid"]))
            cmd = proc.cmdline()
            def arg(flag):
                return cmd[cmd.index(flag) + 1] if flag in cmd else None
            if (abs(proc.create_time() - float(owner["created"])) > 0.01
                    or Path(proc.exe()).resolve() != Path(owner["exe"]).resolve()
                    or Path(owner["exe"]).resolve() != Path(self.engine.cfg["llm_server_exe"]).resolve()
                    or Path(owner["model"]).resolve() != Path(self.get("vision_model_file")).resolve()
                    or arg("-m") is None or Path(arg("-m")).resolve() != Path(owner["model"]).resolve()
                    or arg("--mmproj") is None
                    or Path(arg("--mmproj")).resolve() != Path(self.get("vision_mmproj_file")).resolve()
                    or arg("--host") != "127.0.0.1"
                    or arg("--port") != str(self._port())
                    or arg("--alias") != ALIAS):
                return None
            return proc
        except (OSError, ValueError, KeyError, IndexError, psutil.Error):
            return None

    def _port_in_use(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", self._port())) == 0

    def _healthy(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self._port()}/health", timeout=1.5) as resp:
                return json.loads(resp.read().decode("utf-8")).get("status") == "ok"
        except Exception:
            return False

    def _free_vram_mb(self):
        stats = sensors.system_stats()
        if stats.get("vram_total") is None:
            return None
        return stats["vram_total"] - stats["vram_used"]

    def needs_loading(self):
        """True when the next look will have to start the Qwen eyes first (the card says 'Waking vision')."""
        return self.mode() == "qwen" and self._owned() is None

    def ensure_ready(self):
        """Make sure something can see. Returns (True, "") or (False, reason). Runs on a worker thread.
        With the 27B loaded there is nothing to start; with the 8B, the Qwen eyes are started if needed."""
        if self.mode() == "big":
            return True, ""
        with self.lock:
            reason = self.unavailable_reason()
            if reason:
                return False, reason
            proc = self._owned()
            if proc is None and self._port_in_use():
                # Shawn Core's launch rule: never take over or kill whatever holds the reserved port
                return False, f"port {self._port()} is being used by another program"
            if proc is None:
                free = self._free_vram_mb()
                need = int(self.get("vision_min_free_mb"))
                if free is not None and free < need:
                    return False, (f"the GPU only has {free / 1024:.1f} GB free "
                                   f"(vision needs about {need / 1024:.1f} GB)")
                proc = self._launch()
                if proc is None:
                    return False, "the vision server could not be started (see data\\eyes-server.log)"
            # this loop waits for the model to finish loading (a cold start takes about 5 s)
            self.starting = True
            try:
                deadline = time.time() + LOAD_TIMEOUT
                while time.time() < deadline:
                    if self._healthy():
                        self.qwen.cfg["llm_base_url"] = self._url()
                        self.ready = True
                        return True, ""
                    if not proc.is_running():
                        self.owner_path.unlink(missing_ok=True)
                        self.ready = False
                        return False, "the vision server stopped while loading (see data\\eyes-server.log)"
                    time.sleep(0.25)
                return False, "the vision model took too long to load"
            finally:
                self.starting = False

    def _launch(self):
        cfg = self.engine.cfg
        model = self.get("vision_model_file")
        args = [cfg["llm_server_exe"], "-m", model, "--mmproj", self.get("vision_mmproj_file"),
                "--host", "127.0.0.1", "--port", str(self._port()), "-c", "4096", "-ngl", "999",
                "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--parallel", "1", "--alias", ALIAS]
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "ab") as log:
                process = subprocess.Popen(args, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
            proc = psutil.Process(process.pid)
            owner = {"pid": process.pid, "created": proc.create_time(),
                     "exe": str(Path(cfg["llm_server_exe"]).resolve()), "model": model}
            pending = self.owner_path.with_suffix(".tmp")
            pending.write_text(json.dumps(owner), encoding="utf-8")
            os.replace(pending, self.owner_path)
            return proc
        except (OSError, psutil.Error):
            return None

    def stop(self):
        """Stop our recorded Qwen eyes server (and only that one). Safe to call when nothing is running.
        Never waits long for the lock: this runs on Quit and at Windows shutdown, where a model that is
        still loading must not hold Jarvis open (killing our own recorded process is safe)."""
        got = self.lock.acquire(timeout=2)
        self.ready = False
        try:
            proc = self._owned()
            if proc is not None:
                try:
                    proc.kill()
                    proc.wait(5)
                except psutil.Error:
                    pass
            self.owner_path.unlink(missing_ok=True)
        finally:
            if got:
                self.lock.release()

    def unload_if_idle(self):
        """Called every minute from the GUI. Stops the Qwen eyes and returns why, or returns None.
        Never two vision models: once the 27B is loaded (it sees by itself) the Qwen eyes go at once."""
        if self.starting or self.qwen.inflight or self.lock.locked() or self._owned() is None:
            return None
        if self.mode() == "big":
            self.stop()
            return "Bonsai 2 27B is loaded and sees by itself"
        idle_for = time.time() - self.last_used
        if idle_for > float(self.get("vision_keep_minutes")) * 60:
            self.stop()
            return f"unused for {self.get('vision_keep_minutes')} min"
        free = self._free_vram_mb()
        if idle_for > 20 and free is not None and free < float(self.get("vision_release_below_mb")):
            self.stop()
            return "a game or app needed the GPU memory"
        return None

    # -----------------------------------------------------------------------
    # Asking: the pictures + a question -> the model's answer
    # -----------------------------------------------------------------------
    def look(self, jpegs_b64, question, system, max_tokens=320):
        """jpegs_b64: one base64 JPEG or a list (views() = close-up + surroundings). Asks the loaded 27B,
        or the Qwen eyes when the 8B is loaded (engine.llm is read at call time: it's replaced on every save)."""
        if isinstance(jpegs_b64, str):
            jpegs_b64 = [jpegs_b64]
        client = self.engine.llm if self.mode() == "big" else self.qwen
        self.last_used = time.time()
        try:
            return client.chat([
                {"role": "system", "content": system},
                {"role": "user", "content": [{"type": "text", "text": question}] + [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}
                    for b64 in jpegs_b64]}],
                max_tokens=max_tokens, temperature=0.2)
        finally:
            self.last_used = time.time()


# ---------------------------------------------------------------------------
# The pictures the model sees: a zoomed close-up of the click (reads small text) and the whole
# capture around it (gives context), each with a magenta ring on the spot. Measured 2026-09-26 on
# test screens: the close-up read "MSVCP140.dll" that the wide view misread; the wide view kept
# "prod.keys" exact where the close-up paraphrased - so both are sent.
# All of this is safe off the GUI thread (QImage painting doesn't need it; no text is drawn).
# ---------------------------------------------------------------------------
CLOSE_W, CLOSE_H, CLOSE_OUT = 480, 300, 768     # close-up box (capture pixels) and its output width


def _ring(img, x, y, radius):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QPainter, QPen
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    # a dark halo under the ring so it shows on light and dark backgrounds alike
    for colour, width in ((QColor(0, 0, 0, 160), 6), (QColor(MARK), 3)):
        p.setPen(QPen(colour, width))
        p.drawEllipse(QPointF(x, y), radius, radius)
        # two short ticks, above and below only: side ticks ran straight through the line of text
        # being pointed at and struck letters out ("Fir-ware"), which hurt reading
        for dy in (-1, 1):
            p.drawLine(QPointF(x, y + dy * (radius + 6)), QPointF(x, y + dy * (radius + 16)))
    p.end()
    return img


def _b64_jpeg(img):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "JPG", 88)
    return base64.b64encode(bytes(data)).decode("ascii")


def _capture_image(image):
    from PySide6.QtGui import QImage
    bgra, w, h, px, py = image
    return QImage(bgra, w, h, w * 4, QImage.Format_RGB32).copy(), px, py   # copy: must own its pixels


def marked_jpeg(image):
    """The whole capture with the ring on the clicked spot. image = look_at()'s (bgra, w, h, px, py)."""
    img, px, py = _capture_image(image)
    return _b64_jpeg(_ring(img, px, py, 18))


def closeup_jpeg(image):
    """A 480x300 box centred on the click (kept inside the capture), enlarged to 768 px wide."""
    from PySide6.QtCore import QRect, Qt
    img, px, py = _capture_image(image)
    cw, ch = min(CLOSE_W, img.width()), min(CLOSE_H, img.height())
    x0 = max(0, min(px - cw // 2, img.width() - cw))
    y0 = max(0, min(py - ch // 2, img.height() - ch))
    scale = CLOSE_OUT / cw
    close = img.copy(QRect(x0, y0, cw, ch)).scaledToWidth(CLOSE_OUT, Qt.SmoothTransformation)
    return _b64_jpeg(_ring(close, (px - x0) * scale, (py - y0) * scale, 18 * scale))


def views(image):
    """[close-up, surroundings]: what the explain click sends to the vision model, in that order."""
    return [closeup_jpeg(image), marked_jpeg(image)]


def split_name(answer):
    """The model is asked to start with 'NAME: <a few words>'. Returns (name or '', the rest)."""
    lines = answer.strip().splitlines()
    if lines and lines[0].upper().startswith("NAME:"):
        name = lines[0][5:].strip().strip("*").strip()
        return name[:80], "\n".join(lines[1:]).strip()
    return "", answer.strip()

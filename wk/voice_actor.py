"""Voice acting: Jarvis gives voices to games that have none.

How it works:
  1. Once per game, Shawn drags a box around the game's dialogue area (tray > "Voice act this game...").
     The box is remembered per program in data/voice_act.json.
  2. While THAT game is the focused window, the box is read ~3 times a second with Windows' built-in
     OCR (the same engine the explain click uses) - no model, no GPU.
  3. When the text has stopped changing (games type it out letter by letter) and it's a new line,
     the speaker's name is picked out ("Kirby: Hi!", or a short name plate above the text) and the line
     is spoken with that character's Kokoro voice. Pressing on in the game changes the text, which
     cuts the current line off and speaks the next one - like a voiced game.
  4. Each character keeps one voice: the first time a name appears it's given a voice from the pool
     (stable per name), and Shawn can recast anyone in data/voice_act.json ("cast": {"Kirby": "af_sky"}).
     Lines with no name use the game's narrator voice.

Limits worth knowing: very stylised pixel fonts can defeat Windows OCR; then the vision model is the
next step (read the box with the 27B / Qwen helper instead).
"""
import difflib
import hashlib
import json
import re
import threading
import time

from . import config, pointer, sensors

# this is the casting pool: English voices only (the others are other languages' voices)
MALE = ["am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck",
        "bm_daniel", "bm_fable", "bm_lewis"]          # bm_george is Jarvis's own voice, kept out of the cast
FEMALE = ["af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova",
          "af_river", "af_sarah", "af_sky", "bf_alice", "bf_emma", "bf_isabella", "bf_lily"]
POOL = MALE + FEMALE
NARRATOR = "bm_fable"
POLL_S = 0.33
NAME = r"[A-Z][\w.'\- ]{0,22}"


def store_path():
    return config.DATA_DIR / "voice_act.json"


def load_setup():
    try:
        return json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_setup(data):
    store_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Reading a dialogue box: OCR lines -> (speaker, line)
# ---------------------------------------------------------------------------
def split_speaker(lines):
    """['Kirby', 'Poyo! Let's go!'] or ['Kirby: Poyo!'] -> ('Kirby', 'Poyo! ...'); plain text -> ('', text)."""
    lines = [ln.strip() for ln in lines if ln.strip()]
    if not lines:
        return "", ""
    m = re.match(rf"^({NAME})\s*[:：]\s*(.+)$", lines[0])
    if m:
        return m.group(1).strip(), " ".join([m.group(2)] + lines[1:])
    if len(lines) > 1 and re.fullmatch(NAME, lines[0]) and not lines[0].endswith((".", "!", "?", ",")) \
            and len(lines[0].split()) <= 3:
        return lines[0], " ".join(lines[1:])
    return "", " ".join(lines)


def clean_line(text):
    text = re.sub(r"[▼▶►■◆●…]+$", "", text).strip()          # "press to continue" arrows
    text = re.sub(r"(?<=[.!?\"'])\s+[Vv>»]$", "", text)        # ...and the same arrows as OCR reads them
    return " ".join(text.split())


def voice_for(name, cast, gender=None):
    """The character's voice: Shawn's casting if set, else a stable pick by name from the pool that
    matches the character's gender ("male" / "female"; anything else uses the whole pool)."""
    if not name:
        return cast.get("_narrator", NARRATOR)
    if name not in cast:
        pool = MALE if gender == "male" else FEMALE if gender == "female" else POOL
        cast[name] = pool[int(hashlib.md5(name.lower().encode()).hexdigest(), 16) % len(pool)]
    return cast[name]


GENDER_SCHEMA = {"type": "object", "properties": {"gender": {"type": "string", "enum": ["male", "female", "unknown"]}},
                 "required": ["gender"]}


def guess_gender(llm, name, game):
    """Ask Jarvis's model once per new character (it knows well-known ones). Never raises."""
    from .models import chat_json
    try:
        answer = chat_json(llm, [{"role": "user", "content":
                                  f"In the video game '{game}', is the character named '{name}' male or female? "
                                  "Answer unknown if you don't know."}], GENDER_SCHEMA, max_tokens=20, temperature=0)
        return answer.get("gender", "unknown")
    except Exception:
        return "unknown"


def same(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.9


# ---------------------------------------------------------------------------
# The actor: a background thread watching one game's dialogue box
# ---------------------------------------------------------------------------
class VoiceActor:
    def __init__(self, speaker_fn, on_event=lambda kind, text: None, reader=None, gender_fn=None):
        """speaker_fn() -> a voice.Speaker. reader(region) -> OCR lines (tests).
        gender_fn(name, game) -> 'male' / 'female' / 'unknown' (normally guess_gender with Jarvis's model)."""
        self.speaker_fn, self.on_event = speaker_fn, on_event
        self.gender_fn = gender_fn or (lambda name, game: "unknown")
        self.reader = reader or read_region
        self.process = None
        self._thread = None
        self._stop = threading.Event()

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, process):
        setup = load_setup().get(process)
        if not setup or not setup.get("region"):
            raise ValueError(f"no dialogue box set for {process}")
        self.stop()
        self.process = process
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(process,), daemon=True, name="jarvis-voice-actor")
        self._thread.start()
        self.on_event("started", process)

    def stop(self):
        self._stop.set()
        speaker = self.speaker_fn()
        if speaker is not None:
            speaker.stop()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self, process):
        data = load_setup()
        game = data.setdefault(process, {})
        cast = game.setdefault("cast", {})
        region = game["region"]
        last_seen, stable, spoken = "", 0, ""
        speaking = None
        while not self._stop.is_set():
            time.sleep(POLL_S)
            # only while that game is in front: never read other windows
            if sensors.foreground_window()[0] != process:
                continue
            name, line = split_speaker(self.reader(region))
            line = clean_line(line)
            if not line or len(line) < 2:
                last_seen, stable = "", 0
                continue
            # this is the "has it finished typing out?" check: the same text twice in a row
            if same(line, last_seen):
                stable += 1
            else:
                last_seen, stable = line, 0
                continue
            # unchanged for 3 reads (~0.7 s): a typewriter pausing mid-line doesn't count as finished
            if stable != 2 or same(line, spoken):
                continue
            to_say = line
            if spoken and len(spoken) > 3 and line.lower().startswith(spoken.lower()):
                to_say = line[len(spoken):].strip()        # the game kept typing after a pause: say the rest only
            spoken = line
            if not to_say:
                continue
            if name and name not in cast:
                title = sensors.foreground_window()[1]
                voice = voice_for(name, cast, self.gender_fn(name, title or process))
            else:
                voice = voice_for(name, cast)
            game["cast"] = cast
            save_setup({**load_setup(), process: game})
            speaker = self.speaker_fn()
            if speaker is None:
                continue
            if to_say == line:
                speaker.stop()                                   # the player moved on: cut the old line off
                if speaking is not None:
                    speaking.join(timeout=1)
            elif speaking is not None:
                speaking.join(timeout=15)                        # a continuation waits for the first part
            self.on_event("line", f"{name or 'Narrator'} ({voice}): {to_say}")
            speaking = threading.Thread(target=speaker.say, args=(to_say, voice), daemon=True)
            speaking.start()
        self.on_event("stopped", process)


def read_region(region):
    """OCR the dialogue box. region = [x, y, w, h] in physical screen pixels."""
    x, y, w, h = region
    bgra, _, _, cw, ch = pointer.capture_around(x + w // 2, y + h // 2, w, h)
    lines = pointer.ocr_lines(bgra, cw, ch)
    return [text for text, box in sorted(lines, key=lambda item: (item[1][1], item[1][0]))]


# ---------------------------------------------------------------------------
# Picking the dialogue box: a see-through overlay you drag a rectangle on
# ---------------------------------------------------------------------------
def pick_region(on_done):
    """Shows the overlay; calls on_done([x, y, w, h] physical pixels) or on_done(None) on Esc."""
    from PySide6.QtCore import QPoint, QRect, Qt
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
    from PySide6.QtWidgets import QWidget

    from . import hud

    class Picker(QWidget):
        def __init__(self):
            super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setCursor(Qt.CrossCursor)
            self.setGeometry(QGuiApplication.primaryScreen().virtualGeometry())
            self.start = self.end = None

        def paintEvent(self, _event):
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(4, 12, 20, 110))
            p.setPen(QColor(hud.CYAN_HI))
            p.setFont(QFont("Bahnschrift", 16))
            p.drawText(self.rect().adjusted(0, 60, 0, 0), Qt.AlignHCenter | Qt.AlignTop,
                       "Drag a box around the game's dialogue text  ·  Esc to cancel")
            if self.start and self.end:
                box = QRect(self.start, self.end).normalized()
                p.setCompositionMode(QPainter.CompositionMode_Clear)
                p.fillRect(box, Qt.transparent)
                p.setCompositionMode(QPainter.CompositionMode_SourceOver)
                p.setPen(QPen(QColor(hud.CYAN), 2))
                p.drawRect(box)

        def mousePressEvent(self, e):
            self.start = self.end = e.position().toPoint()
            self.update()

        def mouseMoveEvent(self, e):
            self.end = e.position().toPoint()
            self.update()

        def mouseReleaseEvent(self, e):
            box = QRect(self.start, e.position().toPoint()).normalized()
            self.close()
            if box.width() < 20 or box.height() < 10:
                on_done(None)
                return
            on_done(_to_physical(self.mapToGlobal(box.topLeft()), self.mapToGlobal(box.bottomRight())))

        def keyPressEvent(self, e):
            if e.key() == Qt.Key_Escape:
                self.close()
                on_done(None)

    def _to_physical(a: QPoint, b: QPoint):
        screen = QGuiApplication.screenAt(a) or QGuiApplication.primaryScreen()
        geo, dpr = screen.geometry(), screen.devicePixelRatio()
        conv = lambda q: (round(geo.x() + (q.x() - geo.x()) * dpr), round(geo.y() + (q.y() - geo.y()) * dpr))
        (x0, y0), (x1, y1) = conv(a), conv(b)
        return [x0, y0, x1 - x0, y1 - y0]

    picker = Picker()
    picker.show()
    picker.raise_()
    picker.activateWindow()
    return picker          # the caller keeps a reference while it's open

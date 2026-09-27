"""Deciding what to do next: scripted routines now, a vision model later.

Both policies speak the same small step vocabulary, validated by validate_step() before the
session runs anything:

  {"do": "press", "key": "e", "hold_ms": 60}            tap a key
  {"do": "hold", "key": "w", "ms": 1500}                hold a key (walk forward), up to 30 s
  {"do": "type", "text": "coc riverwood"}               type text (console commands, names)
  {"do": "wait", "ms": 2000}                            do nothing for a while
  {"do": "look", "dx": 300, "dy": 0, "ms": 400}         turn the camera smoothly (relative mouse)
  {"do": "click", "button": "left"}                     mouse click
  {"do": "scroll", "notches": -1}                       mouse wheel
  {"do": "say", "text": "Waiting for the cart..."}      narration for Shawn, shown live and logged
  {"do": "wait_for_text", "any": ["Race"], "timeout_ms": 600000, "optional": false}
                                                        wait until the screen shows some text (OCR)
  {"do": "repeat", "times": 3, "steps": [...]}          (scripts only) repeat a block
"""
import base64
import json
import re
import time
import urllib.request
from pathlib import Path

from .inputs import BUTTONS, normalise_key

# upper bounds, so one bad step (or a confused model) can't hold a key for an hour
LIMITS = {"tap_ms": 5_000,         # press: how long one tap may be held
          "hold_ms": 30_000,       # hold: walking forward etc. (longer runs = repeat blocks)
          "wait_ms": 120_000,      # wait / look duration
          "timeout_ms": 1_800_000,  # wait_for_text: up to 30 min (cutscenes are long)
          "text": 500, "times": 1_000, "move": 5_000, "notches": 20}
SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"


def validate_step(step):
    """Return a clean copy of one step, or raise ValueError saying what's wrong with it."""
    if not isinstance(step, dict) or "do" not in step:
        raise ValueError(f"not a step: {step!r}")
    do = step["do"]

    def ms(key, default, limit):
        value = int(step.get(key, default))
        if not 0 <= value <= LIMITS[limit]:
            raise ValueError(f"{do}: {key}={value} is out of range (0-{LIMITS[limit]})")
        return value
    if do == "press":
        return {"do": do, "key": normalise_key(step["key"]), "hold_ms": ms("hold_ms", 60, "tap_ms")}
    if do == "hold":
        return {"do": do, "key": normalise_key(step["key"]), "ms": ms("ms", 500, "hold_ms")}
    if do == "type":
        text = str(step.get("text", ""))
        if not text or len(text) > LIMITS["text"]:
            raise ValueError("type: text must be 1-500 characters")
        return {"do": do, "text": text}
    if do == "wait":
        return {"do": do, "ms": ms("ms", 500, "wait_ms")}
    if do == "look":
        dx, dy = int(step.get("dx", 0)), int(step.get("dy", 0))
        if abs(dx) > LIMITS["move"] or abs(dy) > LIMITS["move"]:
            raise ValueError("look: dx/dy too large")
        return {"do": do, "dx": dx, "dy": dy, "ms": ms("ms", 300, "wait_ms")}
    if do == "click":
        button = step.get("button", "left")
        if button not in BUTTONS:
            raise ValueError(f"click: unknown button {button!r}")
        return {"do": do, "button": button}
    if do == "scroll":
        notches = int(step.get("notches", -1))
        if abs(notches) > LIMITS["notches"]:
            raise ValueError("scroll: too many notches")
        return {"do": do, "notches": notches}
    if do == "say":
        return {"do": do, "text": str(step.get("text", ""))[:LIMITS["text"]]}
    if do == "wait_for_text":
        needles = step.get("any") or ([step["text"]] if step.get("text") else [])
        if not needles or not all(isinstance(n, str) and n.strip() for n in needles):
            raise ValueError("wait_for_text: needs 'any': [text, ...]")
        return {"do": do, "any": [n.strip() for n in needles], "timeout_ms": ms("timeout_ms", 60_000, "timeout_ms"),
                "optional": bool(step.get("optional", False))}
    raise ValueError(f"unknown step '{do}'")


class Policy:
    """Base: next_steps(obs) returns the next list of steps, or None when the policy is finished."""
    name = "policy"
    needs_screen = False       # True = the session captures a frame before every decision

    def next_steps(self, obs, session):
        raise NotImplementedError


# ===========================================================================
# Scripted routines (JSON files in scripts/)
# ===========================================================================
def load_script(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data.get("steps"), list) or not data["steps"]:
        raise ValueError(f"{Path(path).name}: a script needs a non-empty 'steps' list")
    return data


def available_scripts(extra_dir=None):
    """[(path, name, notes)] for every script shipped with Jarvis plus Shawn's own folder."""
    found = []
    for folder in [SCRIPTS_DIR] + ([Path(extra_dir)] if extra_dir else []):
        for path in sorted(Path(folder).glob("*.json")) if Path(folder).is_dir() else []:
            try:
                data = load_script(path)
                found.append((str(path), data.get("name", path.stem), data.get("notes", "")))
            except (OSError, ValueError):
                continue
    return found


class ScriptPolicy(Policy):
    name = "script"

    def __init__(self, script):
        """script: a dict from load_script(), or a path to one."""
        self.script = load_script(script) if isinstance(script, (str, Path)) else script
        self.name = self.script.get("name", "script")
        self._stack = [(list(self.script["steps"]), 0, 1)]   # (steps, next index, repeats left)
        self.validate_all()

    def validate_all(self):
        """Check every step up front, so a typo on step 40 fails before step 1 runs."""
        def walk(steps):
            for step in steps:
                if isinstance(step, dict) and step.get("do") == "repeat":
                    if not 1 <= int(step.get("times", 1)) <= LIMITS["times"]:
                        raise ValueError("repeat: times out of range")
                    walk(step.get("steps") or [])
                else:
                    validate_step(step)
        walk(self.script["steps"])

    def next_steps(self, obs, session):
        # walk the nested repeat blocks like a call stack: one plain step per decision
        while self._stack:
            steps, index, repeats = self._stack[-1]
            if index >= len(steps):
                self._stack.pop()
                if repeats > 1:
                    self._stack.append((steps, 0, repeats - 1))
                continue
            self._stack[-1] = (steps, index + 1, repeats)
            step = steps[index]
            if step.get("do") == "repeat":
                self._stack.append((list(step.get("steps") or []), 0, int(step.get("times", 1))))
                continue
            return [validate_step(step)]
        return None


# ===========================================================================
# Vision model policy
# ===========================================================================
VISION_SYSTEM = (
    "You are playing a PC game for Shawn while he watches. You see one screenshot at a time. "
    "Reply with ONLY a JSON object: {\"say\": short narration for Shawn, \"done\": true only when the goal "
    "is complete, \"actions\": [up to 4 steps]}. Allowed steps: press {key, hold_ms}, hold {key, ms<=5000}, "
    "type {text}, wait {ms}, look {dx, dy, ms}, click {button}, scroll {notches}. Keys: letters, digits, "
    "space, enter, esc, tab, shift, ctrl, alt, arrows, f1-f12. Prefer small, safe steps; if unsure, wait.")


def parse_vision_reply(text):
    """Model reply -> (say, done, [valid steps]). Invalid steps are dropped, never guessed at."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.S)
    match = re.search(r"\{.*\}", cleaned, re.S)
    try:
        data = json.loads(match.group(0) if match else cleaned)
    except (ValueError, AttributeError):
        return "", False, []
    steps = []
    for raw in (data.get("actions") or [])[:4]:
        try:
            steps.append(validate_step(raw))
        except (ValueError, KeyError, TypeError):
            continue
    return str(data.get("say", ""))[:300], bool(data.get("done")), steps


class VisionPolicy(Policy):
    """Asks a vision-capable model what to do, one screenshot at a time.

    Talks to any OpenAI-compatible /v1/chat/completions endpoint that accepts image input -
    e.g. llama chat (:8080) running a model with a vision projector (mmproj).

    STATUS (2026-09-25): built as groundwork and unit-tested on reply parsing only. It has NOT
    been run against a live model yet: the GPU was reserved for Shawn when this was written.
    First live run should be a dry run (Send real input OFF) to see what the model proposes.
    Latency matters: a local 7-8B vision model takes seconds per frame, so this suits menus,
    dialogue and slow exploration - not combat.
    """
    name = "vision"
    needs_screen = True

    def __init__(self, goal, base_url, model="", api_key="", every_s=3.0, width=768, timeout=90):
        self.goal, self.base_url, self.model, self.api_key = goal, base_url.rstrip("/"), model, api_key
        self.every_s, self.width, self.timeout = every_s, width, timeout
        self._next = 0.0
        self.history = []            # last few narrations, so the model knows what it just did

    def _frame_b64(self, obs):
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
        from PySide6.QtGui import QImage
        bgra, w, h = obs.pixels
        image = QImage(bgra, w, h, w * 4, QImage.Format_ARGB32).scaledToWidth(self.width, Qt.SmoothTransformation)
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.WriteOnly)
        image.save(buf, "JPG", 80)
        return base64.b64encode(bytes(data)).decode("ascii")

    def next_steps(self, obs, session):
        now = time.monotonic()
        if now < self._next:
            return [{"do": "wait", "ms": int((self._next - now) * 1000)}]
        self._next = now + self.every_s
        recent = "\n".join(f"- {h}" for h in self.history[-5:]) or "- (just started)"
        body = {"model": self.model or "default", "max_tokens": 300, "temperature": 0.2, "messages": [
            {"role": "system", "content": VISION_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": f"Goal: {self.goal}\nWhat you did recently:\n{recent}\nWhat next?"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{self._frame_b64(obs)}"}}]}]}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            reply = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]
        say, done, steps = parse_vision_reply(reply)
        if say:
            self.history.append(say)
            steps = [{"do": "say", "text": say}] + steps
        return None if done else (steps or [{"do": "wait", "ms": 500}])

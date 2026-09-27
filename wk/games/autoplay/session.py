"""One autoplay run: observe -> decide -> act, until the policy finishes or Shawn takes over.

Safety comes from structure, not from trusting the policy:
  * every action is broken into ~50 ms slices, and the guard is checked between slices, so
    the kill switch or a touch of the mouse stops a 10-second "hold W" within 50 ms
  * while the game isn't in front, nothing is sent and every held key is released
  * however the run ends (finished, stopped, crashed), release_all() runs in `finally`

Every run gets a folder under data/autoplay/<timestamp>/ with log.jsonl (each step and
narration) and a thumbnail every couple of seconds, so a run can be reviewed afterwards.
"""
import json
import threading
import time
from pathlib import Path

from .observe import observe as capture_frame
from .observe import save_thumbnail
from .policy import validate_step

SLICE_S = 0.05


class Stopped(Exception):
    """Raised inside a step when the guard says stop (unwinds straight to cleanup)."""


class AutoplaySession(threading.Thread):
    def __init__(self, policy, driver, guard, log_dir, on_status=None, observe_fn=None,
                 thumb_every_s=2.0, focus_wait_s=30.0):
        super().__init__(daemon=True, name="jarvis-autoplay")
        self.policy, self.driver, self.guard = policy, driver, guard
        self.log_dir = Path(log_dir)
        self.on_status = on_status or (lambda kind, text: None)
        self.observe = observe_fn or capture_frame
        self.thumb_every_s = thumb_every_s
        self.focus_wait_s = focus_wait_s
        self.result = ""                 # why the run ended, once it has
        self.steps_done = 0
        self._last_thumb = 0.0
        self._log = None

    # --- control from the GUI ------------------------------------------------------------
    def stop(self, reason="stopped from Jarvis"):
        self.guard.kill(reason)

    # --- logging -----------------------------------------------------------------------------
    def _write(self, kind, **data):
        if self._log:
            self._log.write(json.dumps({"t": round(time.time(), 3), "kind": kind, **data}) + "\n")
            self._log.flush()

    def _status(self, kind, text):
        self._write(kind, text=text)
        try:
            self.on_status(kind, text)
        except Exception:
            pass

    # --- the guard, checked between slices -------------------------------------------------------
    def _check(self):
        """Raise Stopped if the run must end; block (keys released) while the game is out of focus."""
        reason = self.guard.stop_reason()
        if reason:
            raise Stopped(reason)
        if not self.guard.focused():
            # let go of everything while another window is in front, but remember what was held,
            # so a "hold W" that was interrupted carries on holding W once the game is back
            keys, buttons = set(self.driver.held_keys), set(self.driver.held_buttons)
            self.driver.release_all()
            self._status("paused", f"Paused: waiting for {self.guard.target} to be in front")
            while not self.guard.focused():
                reason = self.guard.stop_reason()
                if reason:
                    raise Stopped(reason)
                time.sleep(0.2)
            for key in keys:
                self.driver.key_down(key)
            for button in buttons:
                self.driver.button(button, True)
            self._status("resumed", "Resumed")

    def _sleep(self, seconds):
        end = time.monotonic() + max(0.0, seconds)
        while True:
            self._check()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(SLICE_S, left))

    # --- one step ------------------------------------------------------------------------------
    def _execute(self, step):
        step = validate_step(step)
        do = step["do"]
        self._check()
        if do == "press":
            self.driver.key_down(step["key"])
            try:
                self._sleep(step["hold_ms"] / 1000)
            finally:
                self.driver.key_up(step["key"])
        elif do == "hold":
            self.driver.key_down(step["key"])
            try:
                self._sleep(step["ms"] / 1000)
            finally:
                self.driver.key_up(step["key"])
        elif do == "type":
            for ch in step["text"]:
                self._check()
                self.driver.type_text(ch)
        elif do == "wait":
            self._sleep(step["ms"] / 1000)
        elif do == "look":
            # spread the turn over the duration, so the camera pans instead of snapping
            slices = max(1, int(step["ms"] / 1000 / SLICE_S))
            done_x = done_y = 0
            for i in range(1, slices + 1):
                self._check()
                x, y = round(step["dx"] * i / slices), round(step["dy"] * i / slices)
                self.driver.mouse_move(x - done_x, y - done_y)
                done_x, done_y = x, y
                time.sleep(SLICE_S)
        elif do == "click":
            self.driver.click(step["button"])
        elif do == "scroll":
            self.driver.wheel(step["notches"])
        elif do == "say":
            self._status("say", step["text"])
        elif do == "wait_for_text":
            self._wait_for_text(step)
        self.steps_done += 1
        self._write("step", step=step)

    def _wait_for_text(self, step):
        deadline = time.monotonic() + step["timeout_ms"] / 1000
        self._status("waiting", "Watching the screen for: " + " / ".join(step["any"]))
        while time.monotonic() < deadline:
            self._check()
            obs = self.observe()
            self._maybe_thumb(obs)
            if obs.sees(step["any"]):
                self._status("saw", "Saw: " + " / ".join(step["any"]))
                return
            self._sleep(0.5)
        if not step["optional"]:
            raise Stopped("didn't see " + " / ".join(step["any"]) + " in time")

    def _maybe_thumb(self, obs):
        now = time.monotonic()
        if obs and now - self._last_thumb >= self.thumb_every_s:
            self._last_thumb = now
            try:
                save_thumbnail(obs, self.log_dir / f"frame-{int(time.time() * 1000)}.jpg")
            except Exception:
                pass            # a missing thumbnail must never stop a run

    # --- the loop ------------------------------------------------------------------------------
    def run(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_dir / "log.jsonl", "a", encoding="utf-8")
        mode = "REAL INPUT" if self.driver.real else "dry run (no input sent)"
        self._status("start", f"Started {self.policy.name} on {self.guard.target} - {mode}")
        try:
            # give Shawn time to switch to the game after pressing Start in Jarvis. The touch
            # detector is NOT armed yet: the Alt+Tab (or hotkey) that gets him there is his input.
            waited = time.monotonic()
            self._status("waiting", f"Switch to {self.guard.target} - starting when it's in front")
            while not self.guard.focused():
                if time.monotonic() - waited > self.focus_wait_s:
                    raise Stopped(f"{self.guard.target} never came to the front")
                reason = self.guard.stop_reason()
                if reason:
                    raise Stopped(reason)
                time.sleep(0.2)
            self.guard.start()       # arms the touch detector, after a short grace for keys still being released
            while True:
                self._check()
                obs = self.observe() if self.policy.needs_screen else None
                if obs:
                    self._maybe_thumb(obs)
                steps = self.policy.next_steps(obs, self)
                if steps is None:
                    self.result = "finished"
                    break
                for step in steps:
                    self._execute(step)
        except Stopped as exc:
            self.result = str(exc)
        except Exception as exc:        # a policy or driver bug ends the run cleanly, never mid-keypress
            self.result = f"error: {exc}"
        finally:
            self.driver.release_all()
            self.guard.close()
            self._status("end", f"Ended after {self.steps_done} steps: {self.result}")
            self._log.close()

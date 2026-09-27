"""Which model Jarvis runs, and structured (JSON) answers from it.

Model manager: the small model while you're at the PC, the big one while you're away.

  small = Ternary Bonsai 8B    (~3 GB VRAM)  - quick answers, leaves the GPU for you
  big   = Ternary Bonsai 2 27B (~9.5 GB)     - smarter; used for project work while you're away

Only a server launched and recorded by Jarvis is ever stopped or adopted -
never llama chat, ComfyUI or anything else. Before loading the big model it checks free VRAM;
if ComfyUI is idle (empty queue) and holding the GPU, it can ask ComfyUI to unload its models
(setting: away_free_comfyui). If the big model still won't fit or fails to load, Jarvis stays on /
goes back to the small one. Every switch is reported to the "What it noticed" list.
"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import psutil

from . import sensors

# Bonsai 2 27B's vision tower (the Qwen3.8-27B projector, 600 MB; the same file llama chat uses from
# F:\Ai_Models\Chat Models\vision-projectors - they are hardlinks). Loaded with the 27B only.
BIG_MODEL_MMPROJ = (r"F:\Ai_Models\Language Models\AIWF LLM\GGUF\Ternary-Bonsai-2-27B"
                    r"\Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf")


# ---------------------------------------------------------------------------
# Structured answers: the model must reply with JSON matching a schema.
# llama-server turns the schema into a grammar, so the reply always parses.
# ---------------------------------------------------------------------------
def chat_json(llm, messages, schema, max_tokens=1200, temperature=0.3, timeout=600):
    body = {
        "model": llm.model(),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema", "json_schema": {"schema": schema}},
    }
    try:
        req = urllib.request.Request(llm.cfg["llm_base_url"].rstrip("/") + "/chat/completions",
                                     data=json.dumps(body).encode("utf-8"), headers=llm._headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            msg = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]
    except Exception as exc:
        raise llm._clean_error(exc) from None
    return json.loads(msg.get("content") or "{}")


def model_control_allowed():
    """QA safety switch: test copies of Jarvis set JARVIS_NO_MODEL_CONTROL so they can never start,
    stop or swap the real model server, or ask ComfyUI to unload. It only ever REMOVES abilities."""
    return not os.environ.get("JARVIS_NO_MODEL_CONTROL")


class ModelManager:
    def __init__(self, cfg, log_path, on_event):
        self.cfg = cfg
        self.log_path = log_path
        self.owner_path = Path(log_path).with_suffix(".owner.json")
        self.on_event = on_event          # on_event(text) - called from the worker thread
        self.lock = threading.Lock()
        self.active = "small"
        self.busy = False
        self.reviewing = False
        self.task_focus = False
        self._small_gpu_clear_since = None
        self._small_retry_after = 0.0
        self._small_notice_after = 0.0
        self._big_gpu_clear_since = None
        self.pending_profile = None
        self._pending_retry_after = 0.0

    # --- what each profile runs -------------------------------------------------
    def profile(self, name):
        if name == "big":
            # the 27B loads WITH its vision tower (the 600 MB Qwen3.8-27B projector) so Ctrl+Shift+click can
            # look at the screen; the 8B is text-only (Qwen3-8B base) and no projector exists for it
            return {"file": self.cfg["away_model_file"], "alias": self.cfg["away_model_alias"],
                    "ctx": self.cfg["away_model_ctx"], "label": "Bonsai 2 27B",
                    "mmproj": self.cfg.get("away_model_mmproj_file", BIG_MODEL_MMPROJ)}
        return {"file": self.cfg["llm_model_file"], "alias": self.cfg["llm_model"],
                "ctx": self.cfg["llm_ctx"], "label": "Bonsai 8B"}

    def alias(self):
        return self.profile(self.active)["alias"]

    def describe(self):
        if self.busy:
            return "switching model..."
        if self.pending_profile:
            return "waiting for GPU to load " + self.profile(self.pending_profile)["label"]
        if self.reviewing:
            return self.profile(self.active)["label"] + " (read-only review)"
        if self.active == "big":
            # a 27B Shawn loaded himself isn't "away mode"; say it can see instead
            return "Bonsai 2 27B" + (" · vision" if self.cfg.get("llm_pinned_profile") == "big" else " (away mode)")
        return self.profile(self.active)["label"]

    def _port(self):
        return urlparse(self.cfg["llm_base_url"]).port or 8084

    # --- the server process -------------------------------------------------------
    def _owned_process(self):
        """Only a process Jarvis launched and recorded may be adopted or stopped."""
        try:
            owner = json.loads(self.owner_path.read_text(encoding="utf-8"))
            proc = psutil.Process(int(owner["pid"]))
            cmd = proc.cmdline()
            if (abs(proc.create_time() - float(owner["created"])) > 0.01
                    or Path(proc.exe()).resolve() != Path(owner["exe"]).resolve()
                    or "--port" not in cmd or cmd[cmd.index("--port") + 1:cmd.index("--port") + 2] != [str(self._port())]
                    or "-m" not in cmd or cmd[cmd.index("-m") + 1:cmd.index("-m") + 2] != [owner["model"]]):
                return None
            return proc
        except (OSError, ValueError, KeyError, TypeError, psutil.Error):
            return None

    def our_server_pids(self):
        proc = self._owned_process()
        return [proc.pid] if proc else []

    def _port_in_use(self):
        try:
            with socket.create_connection(("127.0.0.1", self._port()), timeout=0.3):
                return True
        except OSError:
            return False

    def _get_json(self, path, timeout=3):
        with urllib.request.urlopen(f"http://127.0.0.1:{self._port()}{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _health(self):
        try:
            return self._get_json("/health").get("status") == "ok"
        except Exception:
            return False

    def loaded_alias(self):
        try:
            data = self._get_json("/v1/models").get("data", [])
            return data[0]["id"] if data else ""
        except Exception:
            return ""

    def profile_load_problem(self, name):
        """Return a bounded reason if the requested profile is not verifiably ready."""
        if self.busy:
            return "Jarvis is switching models"
        if self.reviewing:
            return "another read-only review is in progress"
        if self.active != name:
            return f"{self.profile(name)['label']} is not the active Jarvis model"
        try:
            endpoint = urlparse(self.cfg["llm_base_url"])
            if endpoint.scheme != "http":
                return "the configured model endpoint scheme must be http"
            if endpoint.hostname != "127.0.0.1":
                return "the configured model endpoint host must be 127.0.0.1"
            try:
                endpoint.port
            except ValueError:
                return "the configured model endpoint port is invalid"
            if endpoint.path.rstrip("/") != "/v1":
                return "the configured model endpoint path must be /v1"
            if endpoint.username or endpoint.password:
                return "the configured model endpoint must not contain credentials"
            if endpoint.query:
                return "the configured model endpoint must not contain query parameters"
            if endpoint.fragment:
                return "the configured model endpoint must not contain a fragment"
        except (ValueError, KeyError, TypeError):
            return "the configured model endpoint is invalid"
        proc = self._owned_process()
        if not proc:
            return ("the model port is occupied by a service Jarvis did not launch" if self._port_in_use()
                    else "Jarvis has not launched its model server")
        if not self._health():
            return "Jarvis's owned model server failed its loopback health check"
        try:
            command = proc.cmdline()
            if "--host" not in command or command[command.index("--host") + 1:command.index("--host") + 2] != ["127.0.0.1"]:
                return "Jarvis's recorded model server is not bound to 127.0.0.1"
            for flag, expected in (("-c", self.profile(name)["ctx"]),
                                   ("-ctk", "q8_0"), ("-ctv", "q8_0"),
                                   ("-b", self.cfg.get("llm_batch", 2048)),
                                   ("-ub", self.cfg.get("llm_ubatch", 512))):
                if flag not in command or command[command.index(flag) + 1:command.index(flag) + 2] != [str(expected)]:
                    return f"Jarvis's recorded model server has a different {flag} setting"
            owner = json.loads(self.owner_path.read_text(encoding="utf-8"))
            profile = self.profile(name)
            projector = profile.get("mmproj")
            if projector and not Path(projector).is_file():
                return "the configured vision projector is missing"
            if projector:
                if ("--mmproj" not in command or
                        command[command.index("--mmproj") + 1:command.index("--mmproj") + 2] != [projector]):
                    return "Jarvis's recorded model server lacks the configured vision projector"
            expected_exe = str(Path(self.cfg["llm_server_exe"]).resolve())
            if Path(owner["exe"]).resolve() != Path(expected_exe).resolve():
                return "Jarvis's recorded process uses a different model-server executable"
            if Path(owner["model"]).resolve() != Path(profile["file"]).resolve():
                return "Jarvis's recorded process uses a different model file"
            alias = self.loaded_alias()
            if not alias:
                return "Jarvis's owned server has not reported a loaded model"
            if alias != profile["alias"]:
                return f"Jarvis's owned server reports '{alias}', not {profile['label']}"
        except (OSError, ValueError, KeyError, TypeError):
            return "Jarvis could not verify its model-server identity"
        return None

    def is_profile_loaded(self, name):
        """Confirm this exact configured model is served by Jarvis's recorded process."""
        return self.profile_load_problem(name) is None

    def begin_review(self, profile="big"):
        """Reserve one currently loaded, Jarvis-owned profile for a read-only review."""
        with self.lock:
            if profile not in {"big", "small"} or self.busy or self.reviewing or self.active != profile:
                return False
            if self.profile_load_problem(profile) is not None:
                return False
            self.reviewing = True
            return True

    def end_review(self):
        with self.lock:
            self.reviewing = False

    def begin_task_focus(self):
        """Opt-in 27B lease; only the existing owner-checked switch may load it."""
        if not self.cfg.get("task_focus_27b_enabled", False) or not model_control_allowed():
            return False
        with self.lock:
            if self.task_focus or self.reviewing or self.busy:
                return False
            self.task_focus = True
        if self.profile_load_problem("big") is None:
            return True
        self.switch("big")
        if self.profile_load_problem("big") is None:
            return True
        with self.lock:
            self.task_focus = False
        return False

    def end_task_focus(self, restore_small=False):
        with self.lock:
            held = self.task_focus
            self.task_focus = False
        if held and restore_small and self.cfg.get("llm_pinned_profile") != "big":
            self.switch_async("small")

    def _stop_ours(self):
        proc = self._owned_process()
        if proc:
            try:
                proc.kill()
            except psutil.Error:
                pass
        deadline = time.time() + 15
        while self.our_server_pids() and time.time() < deadline:
            time.sleep(0.3)
        if not self.our_server_pids():
            self.owner_path.unlink(missing_ok=True)

    def _start(self, name, wait=240, *, detached=True):
        """Start one model profile; guarded callers can keep the server in their child tree."""
        if self.our_server_pids():
            self._stop_ours()
        if self._port_in_use():
            self.on_event(f"Jarvis model port {self._port()} is occupied by another service; model start deferred")
            return False
        if name == "small":
            ready, reason = self._small_model_has_room()
            if not ready:
                now = time.monotonic()
                self._small_retry_after = max(self._small_retry_after, now + 15)
                if now >= self._small_notice_after:
                    self.on_event(f"Deferred Bonsai 8B: {reason}; Jarvis will retry when the GPU is clear")
                    self._small_notice_after = now + 60
                return False
        p = self.profile(name)
        if not Path(self.cfg["llm_server_exe"]).exists() or not Path(p["file"]).exists():
            return False
        args = [self.cfg["llm_server_exe"], "-m", p["file"], "--host", "127.0.0.1", "--port", str(self._port()),
                "-c", str(p["ctx"]), "-ngl", str(self.cfg.get("llm_gpu_layers", 999)), "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0",
                "-b", str(self.cfg.get("llm_batch", 2048)), "-ub", str(self.cfg.get("llm_ubatch", 512)),
                "--parallel", "1", "--reasoning", "off", "--alias", p["alias"],
                "--cors-origins", "localhost", "--no-cors-credentials"]
        if p.get("mmproj") and Path(p["mmproj"]).exists():
            args += ["--mmproj", p["mmproj"]]      # the 27B's eyes: lets explain clicks send screenshots
        # the server keeps its own copy of the log handle; ours is closed as soon as it has started
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        if detached:
            creationflags |= subprocess.DETACHED_PROCESS
        with open(self.log_path, "ab") as log:
            process = subprocess.Popen(args, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                       creationflags=creationflags)
        try:
            owner = {"pid": process.pid, "created": psutil.Process(process.pid).create_time(),
                     "exe": str(Path(self.cfg["llm_server_exe"]).resolve()), "model": p["file"]}
            pending = self.owner_path.with_suffix(".owner.tmp")
            pending.write_text(json.dumps(owner), encoding="utf-8")
            os.replace(pending, self.owner_path)
        except (OSError, psutil.Error):
            try:
                process.kill()
            except OSError:
                pass
            self.on_event("Jarvis could not record model ownership; model control is disabled for this process")
            return False
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.our_server_pids() and self._health():
                return True
            if not self.our_server_pids():
                return False          # it exited (e.g. out of VRAM)
            time.sleep(1)
        return False

    # --- making room for the big model ----------------------------------------------
    def _free_vram_mb(self):
        stats = sensors.system_stats()
        if stats.get("vram_total") is None:
            return None
        return stats["vram_total"] - stats["vram_used"]

    def _small_model_has_room(self):
        """Require spare VRAM and a sustained low-utilization window before starting Bonsai 8B."""
        if int(self.cfg.get("llm_gpu_layers", 999)) == 0:
            return True, "CPU-only model loading does not use VRAM"
        stats = sensors.system_stats()
        total, used, util = stats.get("vram_total"), stats.get("vram_used"), stats.get("gpu")
        free = total - used if total is not None and used is not None else None
        need = float(self.cfg.get("small_model_vram_mb", 3000))
        max_util = float(self.cfg.get("small_model_idle_gpu_percent", 10))
        if free is None or util is None:
            self._small_gpu_clear_since = None
            return False, "GPU telemetry is unavailable"
        if free < need:
            self._small_gpu_clear_since = None
            return False, f"only {free:.0f} MiB VRAM is free; Bonsai 8B needs about {need:.0f} MiB"
        if util > max_util:
            self._small_gpu_clear_since = None
            return False, f"GPU utilization is {util:.0f}%; the idle threshold is {max_util:.0f}%"
        now = time.monotonic()
        if self._small_gpu_clear_since is None:
            self._small_gpu_clear_since = now
        clear_for = now - self._small_gpu_clear_since
        hold = max(0.0, float(self.cfg.get("small_model_idle_seconds", 60)))
        if clear_for < hold:
            return False, f"GPU has been quiet for {clear_for:.0f}/{hold:.0f} seconds"
        return True, f"GPU stayed below {max_util:.0f}% utilization with at least {need:.0f} MiB free"

    def maybe_start_small_async(self):
        """Retry an explicit deferred profile or configured small-model autostart."""
        if not self.cfg.get("llm_enabled", True) or not model_control_allowed():
            return
        if self.pending_profile:
            if time.monotonic() < self._pending_retry_after:
                return
            if self.busy or self.reviewing:
                return
            want = self.pending_profile
            self._pending_retry_after = time.monotonic() + (60 if want == "big" else 15)
            threading.Thread(target=self.switch, args=(want,), daemon=True,
                             name=f"jarvis-model-retry-{want}").start()
            return
        if (not self.cfg.get("llm_autostart_server") or self.active == "big"
                or self.our_server_pids() or self._port_in_use()):
            return
        now = time.monotonic()
        if now < self._small_retry_after:
            return
        self._small_retry_after = now + 15
        threading.Thread(target=self._start_small_if_needed, daemon=True,
                         name="jarvis-small-model-retry").start()

    def _start_small_if_needed(self):
        if not self.cfg.get("llm_enabled", True) or not self.cfg.get("llm_autostart_server") or not model_control_allowed():
            return
        with self.lock:
            if self.busy or self.reviewing or self.our_server_pids() or self.active == "big":
                return
            self.busy = True
        try:
            started = self._start("small")
            if started:
                self.active = "small"
                self.on_event("Started Bonsai 8B after the GPU stayed below its idle threshold")
            else:
                self._small_retry_after = max(self._small_retry_after, time.monotonic() + 60)
        finally:
            self.busy = False

    def _comfyui_idle_unload(self):
        """Ask ComfyUI to unload its models - only if its queue is empty (never interrupts a job)."""
        base = (self.cfg.get("comfyui_url") or "").rstrip("/")
        if (base != "http://127.0.0.1:8188" or not self.cfg.get("away_free_comfyui")
                or not model_control_allowed()):
            return False
        try:
            with urllib.request.urlopen(base + "/queue", timeout=3) as resp:
                queue = json.loads(resp.read().decode("utf-8"))
            if queue.get("queue_running") or queue.get("queue_pending"):
                return False
            req = urllib.request.Request(base + "/free", data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).close()
            return True
        except Exception:
            return False            # ComfyUI not running / not reachable: nothing to free

    def room_for_big(self):
        need = self.cfg["away_model_vram_mb"]
        small_share = self.cfg.get("small_model_vram_mb", 3000) if self.our_server_pids() else 0
        stats = sensors.system_stats()
        total, used, util = stats.get("vram_total"), stats.get("vram_used"), stats.get("gpu")
        free = total - used if total is not None and used is not None else None
        max_util = float(self.cfg.get("small_model_idle_gpu_percent", 10))
        if free is None or util is None:
            self._big_gpu_clear_since = None
            self.on_event("Deferred Bonsai 2: GPU telemetry is unavailable; Jarvis will retry later")
            return False
        if util > max_util:
            self._big_gpu_clear_since = None
            self.on_event(f"Deferred Bonsai 2: GPU utilization is {util:.0f}%; waiting for at most {max_util:.0f}%")
            return False
        now = time.monotonic()
        if self._big_gpu_clear_since is None:
            self._big_gpu_clear_since = now
        hold = max(0.0, float(self.cfg.get("small_model_idle_seconds", 60)))
        if now - self._big_gpu_clear_since < hold:
            return False
        if free + small_share >= need:
            return True
        if self._comfyui_idle_unload():
            time.sleep(4)
            stats = sensors.system_stats()
            total, used, util = stats.get("vram_total"), stats.get("vram_used"), stats.get("gpu")
            free = total - used if total is not None and used is not None else 0
            if util is not None and util <= max_util and free + small_share >= need:
                self.on_event("Asked ComfyUI (idle, empty queue) to unload its models to make room for Bonsai 2")
                return True
        self.on_event(f"Deferred Bonsai 2: only {free / 1024:.1f} GB VRAM free, "
                      f"Bonsai 2 needs about {need / 1024:.1f} GB")
        return False

    # --- public entry points (run these on a worker thread) -----------------------------
    def startup(self):
        """At launch: adopt whatever Jarvis server is already running (waiting for one that's still
        loading), or start the small one. Never starts a second server on Jarvis's port."""
        if not model_control_allowed():
            alias = self.loaded_alias() if self.our_server_pids() else ""
            self.active = "big" if alias and alias == self.cfg["away_model_alias"] else "small"
            return
        if not self.cfg.get("llm_enabled", True):
            return
        if self._port_in_use() and not self.our_server_pids():
            self.on_event(f"Jarvis model port {self._port()} belongs to another service; model control is paused")
            return
        deadline = time.time() + 240
        while self.our_server_pids() and not self._health() and time.time() < deadline:
            time.sleep(1)       # a server is loading (e.g. mid-swap) - wait for it rather than race it
        alias = self.loaded_alias() if self.our_server_pids() else ""
        if alias:
            self.active = "big" if alias == self.cfg["away_model_alias"] else "small"
            return
        if self.cfg.get("llm_autostart_server"):
            self._start_small_if_needed()

    def switch(self, want, manual=False):
        """manual=True: Shawn pressed 8B / 27B himself, so the event log says so (not 'away mode')."""
        if want not in {"small", "big"} or not model_control_allowed() or not self.cfg.get("llm_enabled", True):
            return
        with self.lock:
            if self.busy or self.reviewing or (self.task_focus and want == "small"):
                return
            if want == self.active and self.is_profile_loaded(want):
                self.pending_profile = None
                return
            self.busy = True
            self.pending_profile = want
        try:
            if self._port_in_use() and not self.our_server_pids():
                self.pending_profile = None
                self.on_event(f"Jarvis model port {self._port()} belongs to another service; model switch deferred")
                return
            if want == "big" and not self.room_for_big():
                self._pending_retry_after = max(self._pending_retry_after, time.monotonic() + 60)
                return
            self._stop_ours()
            if self._start(want):
                self.active = want
                self.pending_profile = None
                if manual:
                    self.on_event("Loaded Bonsai 2 27B (with vision) - it stays until you pick 8B" if want == "big"
                                  else "Loaded Bonsai 8B")
                else:
                    self.on_event("Switched to Bonsai 2 27B for away-mode work" if want == "big"
                                  else "Welcome back - switched to Bonsai 8B")
            else:
                if want == "big":
                    self._stop_ours()
                    restored = self._start("small")
                    self.active = "small"
                    self.pending_profile = None
                    self.on_event("Couldn't load Bonsai 2; returned to Bonsai 8B" if restored else
                                  "Couldn't load Bonsai 2; Bonsai 8B is waiting for safe GPU capacity")
                else:
                    self.active = "small"
                    self._small_retry_after = max(self._small_retry_after, time.monotonic() + 60)
                    if not Path(self.cfg["llm_server_exe"]).exists() or not Path(self.profile("small")["file"]).exists():
                        self.pending_profile = None
                    self.on_event("Bonsai 8B is not loaded; Jarvis will retry when the GPU is clear")
        finally:
            self.busy = False

    def switch_async(self, want, manual=False):
        threading.Thread(target=self.switch, args=(want,), kwargs={"manual": manual}, daemon=True,
                         name=f"jarvis-model-{want}").start()

    def stop(self):
        """Take Jarvis offline by stopping only its verified, recorded model server."""
        if not model_control_allowed():
            return
        with self.lock:
            if self.busy or self.reviewing:
                self.on_event("Jarvis model could not go offline while a model operation is active")
                return
            self.busy = True
        try:
            self.pending_profile = None
            if self.our_server_pids():
                self._stop_ours()
                self.on_event("Jarvis's model is offline")
            elif self._port_in_use():
                self.on_event("Jarvis did not stop the service on its model port because Jarvis did not launch it")
            else:
                self.on_event("Jarvis's model is already offline")
        finally:
            self.busy = False

    def stop_async(self):
        threading.Thread(target=self.stop, daemon=True, name="jarvis-model-offline").start()

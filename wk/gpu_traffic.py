"""GPU traffic controller and ComfyUI render watcher.

Shawn's GPU is shared by several things that don't know about each other: ComfyUI
(Core-reserved :8188), llama chat (:8080), the prism router (:8082),
Jarvis's own Bonsai (:8084) and games. This module answers "who is on the GPU right now?"
and "is anything holding VRAM for no reason?", and notices when a ComfyUI render finishes.

Everything here is READ-ONLY except free_comfyui(), which only runs when Shawn asks, only
when ComfyUI's queue is empty (it never interrupts a job), and never in a QA test copy
(models.model_control_allowed()).

Split for testing:
  parse_* / advice()   pure functions over JSON / dicts - unit-tested without any server
  snapshot()           gathers live data (nvidia-smi, processes, HTTP probes) - worker thread only
  RenderWatcher        remembers ComfyUI jobs between polls and reports finished / failed ones
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlparse

import psutil

from . import sensors
from .games import catalog

# ---------------------------------------------------------------------------
# this is the known-services section: the ports Shawn's local AI tools listen on
# ---------------------------------------------------------------------------
LLAMA_PORTS = {8080: "llama chat", 8082: "prism router"}
COMFY_PORTS = (8188,)                      # Shawn Core's reserved ComfyUI port on this host
PROBE_TIMEOUT = 0.8                        # seconds; a service that slow to answer is shown as "not answering"


def jarvis_model_port(cfg):
    return urlparse(cfg.get("llm_base_url", "")).port or 8084


def comfy_bases(cfg):
    """Every ComfyUI address worth probing: the configured one plus the usual ports, no repeats."""
    bases = []
    configured = (cfg.get("comfyui_url") or "").rstrip("/")
    # An explicitly configured alternate loopback port may be observed, but
    # free_comfyui still restricts mutations to the Core-reserved :8188.
    parsed = urlparse(configured)
    if parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port != 8000:
        bases.append(configured)
    for port in COMFY_PORTS:
        base = f"http://127.0.0.1:{port}"
        if base not in bases:
            bases.append(base)
    return bases


def _get_json(url, timeout=PROBE_TIMEOUT):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ===========================================================================
# Pure parsing helpers (no network, no processes) - these carry the unit tests
# ===========================================================================
def parse_queue(queue):
    """ComfyUI /queue -> (running prompt ids, pending prompt ids). Each entry is
    [number, prompt_id, prompt, extra, outputs]; only the id matters here."""
    def ids(entries):
        return [e[1] for e in entries or [] if isinstance(e, (list, tuple)) and len(e) > 1]
    return ids(queue.get("queue_running")), ids(queue.get("queue_pending"))


@dataclass
class RenderResult:
    prompt_id: str
    ok: bool
    seconds: float | None = None
    error: str = ""
    failed_node: str = ""
    outputs: list = field(default_factory=list)   # [{"filename", "subfolder", "type", "kind"}]


def parse_history_entry(prompt_id, entry):
    """One /history/<id> entry -> RenderResult. ComfyUI records status messages with
    millisecond timestamps (execution_start ... execution_success / execution_error)."""
    status = entry.get("status") or {}
    messages = status.get("messages") or []
    started = finished = None
    error, failed_node = "", ""
    for name, data in messages:
        stamp = (data or {}).get("timestamp")
        if name == "execution_start" and stamp:
            started = stamp
        elif name in ("execution_success", "execution_error", "execution_interrupted") and stamp:
            finished = stamp
        if name == "execution_error":
            error = (data.get("exception_message") or "unknown error").strip()
            failed_node = data.get("node_type") or ""
        elif name == "execution_interrupted":
            error = "interrupted"
    ok = status.get("status_str") == "success" and not error
    # this loop collects every file the job wrote, whatever node wrote it (images, gifs, videos)
    outputs = []
    for node_out in (entry.get("outputs") or {}).values():
        for kind in ("images", "gifs", "videos", "audio"):
            for item in node_out.get(kind) or []:
                if isinstance(item, dict) and item.get("filename"):
                    outputs.append({"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                                    "type": item.get("type", "output"), "kind": kind})
    seconds = (finished - started) / 1000 if started and finished else None
    return RenderResult(prompt_id, ok, seconds, error[:500], failed_node, outputs)


def output_dir_from_argv(argv):
    """Where ComfyUI saves renders, from its own command line (/system_stats reports argv):
    --output-directory wins, then <--base-directory>/output, then <folder of main.py>/output."""
    argv = list(argv or [])

    def value(flag):
        if flag in argv and argv.index(flag) + 1 < len(argv):
            return argv[argv.index(flag) + 1]
        return None
    if value("--output-directory"):
        return value("--output-directory")
    if value("--base-directory"):
        return str(Path(value("--base-directory")) / "output")
    mains = [a for a in argv if a.lower().endswith("main.py")]
    if mains:
        return str(Path(mains[0]).parent / "output")
    return None


def view_url(base, output):
    """URL of one render file on ComfyUI's /view endpoint (used for the toast thumbnail)."""
    query = urlencode({"filename": output["filename"], "subfolder": output.get("subfolder", ""),
                       "type": output.get("type", "output")})
    return f"{base}/view?{query}"


def advice(snap, cfg):
    """Plain-English suggestions from a snapshot. Each item: {"text", "level": info|warn, "action": id or None}."""
    tips = []
    total, used = snap.get("vram_total"), snap.get("vram_used")
    # this loop looks for ComfyUI holding VRAM while doing nothing
    for comfy in snap.get("comfy", []):
        held = comfy.get("torch_vram_mb") or 0
        if comfy.get("running") == 0 and comfy.get("pending") == 0 and held >= 1500:
            tips.append({"text": f"ComfyUI on :{comfy['port']} is idle but holding {held / 1024:.1f} GB of VRAM.",
                         "level": "warn", "action": f"comfy-free:{comfy['base']}"})
    loaded = [s for s in snap.get("llama", []) if s.get("models")]
    if len(loaded) >= 2:
        names = ", ".join(f":{s['port']}" for s in loaded)
        tips.append({"text": f"{len(loaded)} llama.cpp servers have models loaded at once ({names}); "
                             "each one keeps its VRAM until stopped.", "level": "warn", "action": None})
    if total and used is not None and total - used < 1024:
        tips.append({"text": f"Only {(total - used) / 1024:.1f} GB VRAM left; new model loads will fail or spill to RAM.",
                     "level": "warn", "action": None})
    for game in snap.get("games", []):
        tips.append({"text": f"{game['name']} is running; other GPU jobs will slow it down.", "level": "info",
                     "action": None})
    if not tips:
        tips.append({"text": "Nothing is fighting over the GPU right now.", "level": "info", "action": None})
    return tips


# ===========================================================================
# Live gathering (worker thread: nvidia-smi and HTTP probes can each take up to a second)
# ===========================================================================
def _port_from_cmdline(cmd):
    for flag in ("--port", "-p"):
        if flag in cmd and cmd.index(flag) + 1 < len(cmd):
            try:
                return int(cmd[cmd.index(flag) + 1])
            except ValueError:
                return None
    return None


def _model_from_cmdline(cmd):
    for flag in ("-m", "--model"):
        if flag in cmd and cmd.index(flag) + 1 < len(cmd):
            return Path(cmd[cmd.index(flag) + 1]).name
    return ""


def gpu_processes(vram_by_pid=None):
    """Processes that matter for the GPU, labelled by role. Command lines are read only to find
    a port or model file name; they are never shown or stored whole."""
    vram_by_pid = vram_by_pid or {}
    found = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "memory_info", "create_time"]):
        try:
            name = (proc.info["name"] or "").lower()
            cmd = proc.info["cmdline"] or []
            joined = " ".join(cmd).lower()
            role, port, detail = None, None, ""
            if name.startswith("llama-server"):
                port = _port_from_cmdline(cmd)
                role = "llama.cpp server"
                detail = _model_from_cmdline(cmd)
            elif name in ("python.exe", "pythonw.exe") and "comfyui" in joined and "main.py" in joined:
                role, port = "ComfyUI", _port_from_cmdline(cmd)
            elif name == "comfyui.exe":
                role = "ComfyUI (desktop app)"
            elif name.startswith("ollama"):
                role = "Ollama"
            else:
                game = catalog.detect(name)
                if game:
                    role, detail = "game", game.name
            if not role:
                continue
            rss = proc.info["memory_info"].rss if proc.info["memory_info"] else 0
            found.append({"pid": proc.info["pid"], "name": name, "role": role, "port": port, "detail": detail,
                          "ram_mb": round(rss / 1048576), "vram_mb": vram_by_pid.get(proc.info["pid"])})
        except (psutil.Error, OSError):
            continue
    return found


def probe_llama(port):
    """{'port', 'label', 'up', 'models'} for a llama.cpp / OpenAI-style server on this port."""
    info = {"port": port, "label": LLAMA_PORTS.get(port, "model server"), "up": False, "models": []}
    try:
        data = _get_json(f"http://127.0.0.1:{port}/v1/models")
        info["up"] = True
        info["models"] = [m.get("id", "?") for m in data.get("data", []) if isinstance(m, dict)]
    except urllib.error.HTTPError as exc:
        info["up"] = True                    # answering, just not to us (e.g. needs an API key)
        info["note"] = f"HTTP {exc.code}"
    except (OSError, ValueError):
        pass
    return info


def probe_comfy(base):
    """ComfyUI health: queue sizes, how much VRAM PyTorch is holding, and where renders go."""
    info = {"base": base, "port": urlparse(base).port, "up": False}
    try:
        running, pending = parse_queue(_get_json(base + "/queue"))
        info.update(up=True, running=len(running), pending=len(pending), running_ids=running)
    except (OSError, ValueError):
        return info
    try:
        stats = _get_json(base + "/system_stats")
        device = (stats.get("devices") or [{}])[0]
        info["torch_vram_mb"] = round((device.get("torch_vram_total") or 0) / 1048576)
        info["output_dir"] = output_dir_from_argv((stats.get("system") or {}).get("argv"))
        info["version"] = (stats.get("system") or {}).get("comfyui_version", "")
    except (OSError, ValueError, AttributeError):
        pass
    return info


def snapshot(cfg):
    """Everything the GPU page shows, in one dict. Slow (~1-2 s): call off the GUI thread."""
    from .resource_tools import _gpu_memory        # per-process VRAM, when the driver reports it
    stats = sensors.system_stats()
    procs = gpu_processes(_gpu_memory())
    llama_ports = sorted(set(LLAMA_PORTS) | {jarvis_model_port(cfg)}
                         | {p["port"] for p in procs if p["role"] == "llama.cpp server" and p["port"]})
    llama = []
    for port in llama_ports:
        info = probe_llama(port)
        if port == jarvis_model_port(cfg):
            info["label"] = "Jarvis (Bonsai)"
        if info["up"]:
            llama.append(info)
    comfy = [c for c in (probe_comfy(b) for b in comfy_bases(cfg)) if c["up"]]
    games = [{"name": p["detail"], "pid": p["pid"]} for p in procs if p["role"] == "game"]
    snap = {"taken": time.time(), "gpu": stats.get("gpu"), "gpu_temp": stats.get("gpu_temp"),
            "vram_used": stats.get("vram_used"), "vram_total": stats.get("vram_total"),
            "processes": procs, "llama": llama, "comfy": comfy, "games": games}
    snap["advice"] = advice(snap, cfg)
    return snap


def snapshot_text(cfg):
    """The /gpu chat command: the same snapshot as plain text (works from the phone too)."""
    snap = snapshot(cfg)
    lines = []
    if snap["vram_total"]:
        lines.append(f"GPU {snap['gpu']:.0f}% busy, {snap['gpu_temp']:.0f}°C, VRAM "
                     f"{snap['vram_used'] / 1024:.1f}/{snap['vram_total'] / 1024:.1f} GB used.")
    else:
        lines.append("GPU telemetry unavailable (nvidia-smi did not answer).")
    for s in snap["llama"]:
        lines.append(f"- {s['label']} :{s['port']}: " + (", ".join(s["models"]) or s.get("note", "no model listed")))
    for c in snap["comfy"]:
        lines.append(f"- ComfyUI :{c['port']}: {c['running']} running, {c['pending']} queued, "
                     f"holding {c.get('torch_vram_mb', 0) / 1024:.1f} GB")
    for g in snap["games"]:
        lines.append(f"- Game: {g['name']}")
    lines.append("Advice:")
    lines += [f"- {a['text']}" for a in snap["advice"]]
    return "\n".join(lines)


def free_comfyui(base):
    """Ask ComfyUI to unload its models - Shawn's explicit request only, and only with an empty queue."""
    from .models import model_control_allowed
    if not model_control_allowed():
        return False, "Model control is switched off in this copy of Jarvis."
    if base.rstrip("/") != "http://127.0.0.1:8188":
        return False, "ComfyUI unload is limited to the Core-reserved loopback address :8188."
    try:
        running, pending = parse_queue(_get_json(base + "/queue", timeout=3))
        if running or pending:
            return False, f"ComfyUI is busy ({len(running)} running, {len(pending)} queued); nothing was unloaded."
        req = urllib.request.Request(base + "/free", data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).close()
        return True, "ComfyUI unloaded its models; the VRAM comes back within a few seconds."
    except (OSError, ValueError) as exc:
        return False, f"ComfyUI did not answer ({exc})."


# ===========================================================================
# Render watcher: remembers what was running, reports what finished
# ===========================================================================
class RenderWatcher:
    """Call poll() every few seconds (worker thread). It returns RenderResults for jobs that
    finished since the last poll. A job counts once it has been SEEN running or queued, so
    old history from before Jarvis started is never reported."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.live_bases = []            # ComfyUI addresses that answered at the last discovery
        self._next_discovery = 0.0
        self._watching = {}             # base -> set of prompt ids seen running or queued
        self.output_dirs = {}           # base -> folder its renders are saved in

    def _discover(self, now):
        if now < self._next_discovery:
            return
        self._next_discovery = now + 60      # finding ComfyUI again is cheap, but no need every poll
        self.live_bases = []
        for base in comfy_bases(self.cfg):
            info = probe_comfy(base)
            if info["up"]:
                self.live_bases.append(base)
                if info.get("output_dir"):
                    self.output_dirs[base] = info["output_dir"]

    def poll(self, now=None):
        now = now or time.time()
        self._discover(now)
        finished = []
        # this loop compares each ComfyUI's queue with what it held last time
        for base in list(self.live_bases):
            try:
                running, pending = parse_queue(_get_json(base + "/queue"))
            except (OSError, ValueError):
                self.live_bases.remove(base)          # ComfyUI closed: rediscover next minute
                self._watching.pop(base, None)
                continue
            current = set(running) | set(pending)
            gone = self._watching.get(base, set()) - current
            self._watching[base] = current
            for prompt_id in gone:
                result = self._result(base, prompt_id)
                if result:
                    finished.append((base, result))
        return finished

    def _result(self, base, prompt_id):
        try:
            history = _get_json(f"{base}/history/{prompt_id}", timeout=3)
        except (OSError, ValueError):
            return None
        entry = history.get(prompt_id)
        return parse_history_entry(prompt_id, entry) if entry else None

    def output_dir(self, base):
        return self.output_dirs.get(base) or str(Path(os.environ.get("USERPROFILE", "")) / "Documents" / "ComfyUI" / "output")


def friendly_error(text):
    """Shorten a ComfyUI exception for a toast: the last meaningful line, without file paths."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    line = lines[-1] if lines else "unknown error"
    return re.sub(r"[A-Za-z]:\\[^\s'\"]+", "<path>", line)[:160]

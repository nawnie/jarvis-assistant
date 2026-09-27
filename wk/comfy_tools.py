"""Owner-configured, fixed-graph ComfyUI image generation for ordinary chat.

The model supplies a workflow ID and text prompt. The host owns every node,
checkpoint choice, size, sampler, output location, and Comfy endpoint.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import struct
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import config, sensors

MAX_JSON_BYTES = 2_000_000
MAX_IMAGE_BYTES = 16_000_000
_ID = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")
_CHECKPOINT = re.compile(r"[^/\\]+\.safetensors\Z", re.I)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


_opener = build_opener(_NoRedirect)


def _workflows(cfg: dict) -> dict:
    if cfg.get("comfy_generation_enabled") is not True:
        return {}
    catalog = cfg.get("comfy_workflows")
    if not isinstance(catalog, dict):
        return {}
    result = {}
    for name, spec in catalog.items():
        if not isinstance(name, str) or not _ID.fullmatch(name) or not isinstance(spec, dict):
            continue
        checkpoint = spec.get("checkpoint")
        if isinstance(checkpoint, str) and len(checkpoint) <= 200 and \
                not checkpoint.startswith(".") and not any(c in checkpoint for c in "\r\n\x00") and \
                _CHECKPOINT.fullmatch(checkpoint):
            result[name] = checkpoint
    return result


def available(cfg: dict) -> bool:
    return bool(_workflows(cfg))


def list_workflows(cfg: dict) -> dict:
    """List configured IDs; no model paths or arbitrary graphs are exposed."""
    return {"workflows": sorted(_workflows(cfg))}


def _base(cfg: dict) -> str:
    value = cfg.get("comfyui_url")
    parsed = urlparse(value) if isinstance(value, str) else None
    if not parsed or parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or \
            parsed.username or parsed.password or parsed.path not in ("", "/") or \
            parsed.query or parsed.fragment or parsed.port is None:
        raise ValueError("ComfyUI must use an explicitly configured loopback HTTP port")
    return f"http://{parsed.hostname}:{parsed.port}"


def _request(cfg: dict, route: str, payload: dict | None = None,
             image: bool = False) -> dict | bytes:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(_base(cfg) + route, data=data,
                      headers={"Content-Type": "application/json"} if data else {})
    with _opener.open(request, timeout=20) as response:
        limit = MAX_IMAGE_BYTES if image else MAX_JSON_BYTES
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("ComfyUI response exceeded the local size limit")
    return raw if image else json.loads(raw.decode("utf-8"))


def _graph(checkpoint: str, prompt: str, prefix: str) -> dict:
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 384, "height": 384, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {
            "text": "text, letters, people, faces, logos, watermark, clutter", "clip": ["4", 1]}},
        "8": {"class_type": "KSampler", "inputs": {
            "seed": secrets.randbits(48), "steps": 8, "cfg": 5.5,
            "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["5", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["4", 2]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": prefix}},
    }


def _gpu_guard(cancel: threading.Event | None = None, max_seconds: int = 100) -> dict:
    """Wait for a minute of consecutive quiet samples, within a bounded deadline."""
    deadline = time.monotonic() + max_seconds
    quiet = 0
    minimum_free = None
    maximum_util = 0
    while True:
        if cancel is not None and cancel.is_set():
            raise RuntimeError("image submission cancelled before GPU preflight completed")
        sample = sensors.system_stats()
        total, used, util = sample.get("vram_total"), sample.get("vram_used"), sample.get("gpu")
        if not all(isinstance(value, (int, float)) for value in (total, used, util)):
            raise RuntimeError("GPU headroom cannot be verified; image not submitted")
        free = total - used
        if free >= 4000 and util <= 10:
            quiet += 1
            minimum_free = free if minimum_free is None else min(minimum_free, free)
            maximum_util = max(maximum_util, util)
            if quiet == 13:
                return {"samples": 13, "minimum_free_mb": minimum_free,
                        "maximum_util_percent": maximum_util, "sample_interval_seconds": 5}
        else:
            quiet = 0
            minimum_free = None
            maximum_util = 0
        if time.monotonic() + 5 > deadline:
            raise RuntimeError("GPU did not stay quiet with 4 GB free before the preflight deadline")
        if cancel is None:
            time.sleep(5)
        elif cancel.wait(5):
            raise RuntimeError("image submission cancelled before GPU preflight completed")


def _ticket_path(job_id: str) -> Path:
    try:
        parsed = uuid.UUID(job_id)
    except (TypeError, ValueError):
        raise ValueError("invalid Jarvis image job ID") from None
    if str(parsed) != job_id:
        raise ValueError("invalid Jarvis image job ID")
    return config.DATA_DIR / "comfy-jobs" / (job_id + ".json")


def _write_ticket(job_id: str, ticket: dict) -> None:
    path = _ticket_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(ticket, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _read_ticket(job_id: str) -> dict:
    path = _ticket_path(job_id)
    if not path.is_file():
        raise ValueError("Jarvis did not create this image job")
    ticket = json.loads(path.read_text(encoding="utf-8"))
    if ticket.get("job_id") != job_id:
        raise ValueError("image job receipt is invalid")
    return ticket


def submit(cfg: dict, workflow_id: str, prompt: str,
           cancel: threading.Event | None = None, guard_seconds: int = 100) -> dict:
    workflows = _workflows(cfg)
    if workflow_id not in workflows:
        raise ValueError("workflow is not enabled in owner configuration")
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 400:
        raise ValueError("prompt must contain 1-400 characters")
    prompt = prompt.strip()
    checkpoint = workflows[workflow_id]
    info = _request(cfg, "/object_info/CheckpointLoaderSimple")
    try:
        installed = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    except (KeyError, TypeError, IndexError):
        raise ValueError("ComfyUI did not report installed checkpoints") from None
    if checkpoint not in installed:
        raise ValueError("owner-configured checkpoint is not installed in ComfyUI")
    queue = _request(cfg, "/queue")
    if not isinstance(queue, dict) or not isinstance(queue.get("queue_running"), list) or \
            not isinstance(queue.get("queue_pending"), list) or \
            queue["queue_running"] or queue["queue_pending"]:
        raise RuntimeError("ComfyUI has another job; generation was not submitted")
    guard = _gpu_guard(cancel, guard_seconds)
    queue = _request(cfg, "/queue")
    if not isinstance(queue, dict) or not isinstance(queue.get("queue_running"), list) or \
            not isinstance(queue.get("queue_pending"), list) or \
            queue["queue_running"] or queue["queue_pending"]:
        raise RuntimeError("ComfyUI queue changed during GPU preflight; image not submitted")
    job_id = str(uuid.uuid4())
    ticket = {"job_id": job_id, "state": "preparing", "workflow_id": workflow_id,
              "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
              "created_at": time.time(), "comfy_url": _base(cfg), "gpu_preflight": guard}
    _write_ticket(job_id, ticket)
    graph = _graph(checkpoint, prompt, "Jarvis_" + job_id.replace("-", "")[:16])
    result = _request(cfg, "/prompt", {"prompt": graph, "client_id": "jarvis-" + job_id})
    prompt_id = result.get("prompt_id") if isinstance(result, dict) else None
    try:
        valid_prompt_id = isinstance(prompt_id, str) and str(uuid.UUID(prompt_id)) == prompt_id
    except ValueError:
        valid_prompt_id = False
    if not valid_prompt_id:
        ticket["state"] = "submission_uncertain"
        _write_ticket(job_id, ticket)
        raise RuntimeError("ComfyUI did not return a prompt ID; inspect the job before retrying")
    ticket.update({"state": "submitted", "prompt_id": prompt_id})
    _write_ticket(job_id, ticket)
    return {"job_id": job_id, "state": "submitted", "prompt_id": prompt_id,
            "workflow_id": workflow_id, "prompt_sha256": ticket["prompt_sha256"],
            "gpu_preflight": guard}


def wait(cfg: dict, job_id: str, wait_seconds: int = 60,
         cancel: threading.Event | None = None) -> dict:
    ticket = _read_ticket(job_id)
    if ticket["comfy_url"] != _base(cfg):
        raise ValueError("Comfy endpoint changed since this job was submitted")
    if ticket.get("state") == "complete":
        image_path = Path(ticket["image_path"]).resolve(strict=True)
        if not image_path.is_relative_to((config.DATA_DIR / "comfy-artifacts").resolve()) or \
                hashlib.sha256(image_path.read_bytes()).hexdigest() != ticket["sha256"]:
            raise IOError("saved Comfy artifact changed since completion")
        return {k: ticket[k] for k in ("job_id", "state", "image_path", "sha256", "bytes")}
    prompt_id = ticket.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        return {"job_id": job_id, "state": "submission_uncertain"}
    seconds = int(wait_seconds)
    if not 0 <= seconds <= 90:
        raise ValueError("wait_seconds must be 0-90")
    deadline = time.monotonic() + seconds
    while True:
        if cancel is not None and cancel.is_set():
            return {"job_id": job_id, "state": "wait_cancelled_job_not_cancelled",
                    "prompt_id": prompt_id}
        history = _request(cfg, "/history/" + prompt_id)
        entry = history.get(prompt_id) if isinstance(history, dict) else None
        if entry:
            status = (entry.get("status") or {}).get("status_str")
            if status != "success":
                ticket["state"] = "failed"
                _write_ticket(job_id, ticket)
                return {"job_id": job_id, "state": "failed", "comfy_status": status}
            images = (entry.get("outputs") or {}).get("10", {}).get("images") or []
            if len(images) != 1:
                raise ValueError("ComfyUI did not return exactly one image from the host SaveImage node")
            item = images[0]
            filename = item.get("filename")
            if not isinstance(filename, str) or Path(filename).name != filename or \
                    item.get("type") != "output" or item.get("subfolder") not in (None, ""):
                raise ValueError("ComfyUI returned an unexpected output location")
            query = urlencode({"filename": filename, "subfolder": "", "type": "output"})
            image = _request(cfg, "/view?" + query, image=True)
            if image[:8] != b"\x89PNG\r\n\x1a\n" or len(image) < 45 or \
                    image[12:16] != b"IHDR" or image[-12:] != b"\x00\x00\x00\x00IEND\xaeB`\x82" or \
                    struct.unpack(">II", image[16:24]) != (384, 384):
                raise ValueError("ComfyUI output was not the expected 384x384 PNG")
            from PySide6.QtGui import QImage
            decoded = QImage.fromData(image, "PNG")
            if decoded.isNull() or decoded.width() != 384 or decoded.height() != 384:
                raise ValueError("ComfyUI PNG could not be decoded at the expected size")
            digest = hashlib.sha256(image).hexdigest()
            folder = config.DATA_DIR / "comfy-artifacts"
            folder.mkdir(parents=True, exist_ok=True)
            destination = folder / (digest + ".png")
            if not destination.exists():
                temp = folder / (digest + ".tmp")
                temp.write_bytes(image)
                os.replace(temp, destination)
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise IOError("saved Comfy artifact hash mismatch")
            ticket.update({"state": "complete", "image_path": str(destination),
                           "sha256": digest, "bytes": len(image)})
            _write_ticket(job_id, ticket)
            return {"job_id": job_id, "state": "complete", "image_path": str(destination),
                    "sha256": digest, "bytes": len(image), "width": 384, "height": 384}
        if time.monotonic() >= deadline:
            return {"job_id": job_id, "state": "not_finished_or_history_unavailable",
                    "prompt_id": prompt_id}
        delay = min(2, max(0, deadline - time.monotonic()))
        if cancel is None:
            time.sleep(delay)
        else:
            cancel.wait(delay)

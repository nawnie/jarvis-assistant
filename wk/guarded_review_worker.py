"""Run one packet review inside the process tree owned by Sentinel Jarvis."""
import argparse
import json
import msvcrt
import os
import sys
import time
from types import SimpleNamespace

from . import config
from .llm import LocalLLM
from .models import ModelManager, model_control_allowed
from .review import GUARDED_REVIEW_8B_PORT, GUARDED_REVIEW_PORT, learn_packet, review_packet
from .store import Store


def classify_preflight_reason(reason):
    """Return a fixed, non-sensitive category for a model manager gate reason."""
    text = str(reason or "").casefold()
    if "telemetry is unavailable" in text:
        return "gpu_telemetry_unavailable"
    if "vram" in text and ("free" in text or "needs" in text):
        return "insufficient_free_vram"
    if "utilization" in text or "active" in text:
        return "gpu_above_idle_threshold"
    if "quiet for" in text:
        return "idle_window_not_reached"
    return "gpu_preflight_not_ready"


def wait_for_preflight(manager, profile, idle_seconds):
    """Wait for readiness and retain the last safe reason if the existing gate refuses."""
    attempts = max(120, int(float(idle_seconds)) + 15)
    blocker = "gpu_preflight_not_ready"
    events = getattr(manager, "_guarded_preflight_events", None)
    for _ in range(attempts):
        if profile == "big":
            event_count = len(events) if events is not None else 0
            ready = manager.room_for_big()
            if ready:
                return True, None
            if events is not None and len(events) > event_count:
                blocker = classify_preflight_reason(events[-1])
            elif getattr(manager, "_big_gpu_clear_since", None) is not None:
                blocker = "idle_window_not_reached"
            else:
                blocker = "gpu_preflight_not_ready"
        else:
            ready, reason = manager._small_model_has_room()
            blocker = classify_preflight_reason(reason)
        if ready:
            return True, None
        time.sleep(1)
    return False, blocker


def wait_for_big_preflight(manager, idle_seconds):
    """Backwards-compatible named wrapper for the existing 27B idle gate."""
    return wait_for_preflight(manager, "big", idle_seconds)[0]


def wait_for_small_preflight(manager, idle_seconds):
    """Wait for the existing 8B VRAM and GPU-idle gate."""
    return wait_for_preflight(manager, "small", idle_seconds)[0]


def open_pending_memory_store():
    """Open only Jarvis's existing local database after validating its exact path."""
    data_dir = config.DATA_DIR.resolve(strict=True)
    db_path = config.DB_PATH
    if db_path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(db_path)):
        raise RuntimeError("Jarvis Memory database must not be a link")
    resolved = db_path.resolve(strict=True)
    if resolved.parent != data_dir or not resolved.is_file():
        raise RuntimeError("Jarvis Memory database must be a regular file in local Jarvis data")
    return Store(resolved)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one explicitly selected guarded Jarvis packet action.")
    parser.add_argument("packet")
    parser.add_argument("--action", choices=("review", "learn"), default="review")
    parser.add_argument("--profile", choices=("8b", "27b"), default="27b")
    args = parser.parse_args(argv)
    if args.action == "learn" and args.profile != "27b":
        parser.error("packet learning is pinned to Bonsai 2 27B")
    profile = "small" if args.profile == "8b" else "big"
    port = GUARDED_REVIEW_8B_PORT if profile == "small" else GUARDED_REVIEW_PORT

    if not model_control_allowed():
        raise RuntimeError("Jarvis model control is disabled by the safety switch")
    settings = config.load()
    if args.action == "learn" and settings.get("memory_capture_mode", "suggest") != "suggest":
        raise RuntimeError("memory suggestions are off")
    expected_alias = "ternary-bonsai-8b" if profile == "small" else "ternary-bonsai-2-27b"
    alias_key = "llm_model" if profile == "small" else "away_model_alias"
    if settings.get(alias_key) != expected_alias:
        raise RuntimeError(f"configured {args.profile} profile is not the pinned Bonsai alias")
    settings.update({
        "llm_base_url": f"http://127.0.0.1:{port}/v1",
        "llm_key_file": "",
        "llm_autostart_server": False,
        "away_model_enabled": False,
        "away_free_comfyui": False,
    })
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = config.DATA_DIR / "guarded-review.lock"
    if lock_path.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(lock_path)):
        raise RuntimeError("the guarded review lock must not be a link")
    lock = lock_path.open("a+b")
    try:
        if lock.seek(0, 2) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("another guarded review already owns the temporary model lease") from None
    except Exception:
        lock.close()
        raise

    manager = None
    start_attempted = False
    try:
        preflight_events = []
        manager = ModelManager(settings, config.DATA_DIR / "guarded-review-server.log", preflight_events.append)
        manager._guarded_preflight_events = preflight_events
        if manager._port_in_use():
            raise RuntimeError("assigned one-shot model port is already occupied")
        ready, blocker = wait_for_preflight(manager, profile, settings.get("small_model_idle_seconds", 60))
        if not ready:
            marker = "preflight_timeout_8b" if profile == "small" else "preflight_timeout"
            print(f"JARVIS_REVIEW_FAILURE={marker}", file=sys.stderr, flush=True)
            print(f"JARVIS_REVIEW_BLOCKER={blocker}", file=sys.stderr, flush=True)
            raise RuntimeError("the existing VRAM and GPU-idle preflight did not pass")
        start_attempted = True
        if not manager._start(profile, wait=360, detached=False):
            label = "8B" if profile == "small" else "27B"
            raise RuntimeError(f"the temporary Bonsai 2 {label} server did not become ready")
        manager.active = profile
        problem = manager.profile_load_problem(profile)
        if problem:
            raise RuntimeError(f"the exact Jarvis profile check failed: {problem}")

        llm = LocalLLM(settings)
        llm.alias_fn = manager.alias
        if args.action == "review":
            engine = SimpleNamespace(models=manager, llm=llm)
            reply = review_packet(engine, f"/review {args.packet}", profile=profile)
            expected_prefix = "Jarvis 8B review" if profile == "small" else "Jarvis 27B review"
            if not reply.startswith(expected_prefix + " (advisory; no source changes)"):
                raise RuntimeError("the bounded review did not return a validated result")
            result_prefix = "JARVIS_REVIEW_RESULT_JSON="
        else:
            store = open_pending_memory_store()
            try:
                engine = SimpleNamespace(models=manager, llm=llm, cfg=settings, store=store)
                reply = learn_packet(engine, f"/learn {args.packet}", pending_only=True)
            finally:
                store.close()
            expected_replies = (
                "Jarvis 27B proposed one pending work lesson",
                "Jarvis 27B found no reusable work lesson",
                "No new pending work-lesson candidate",
                "No pending work-lesson candidate",
            )
            if not reply.startswith(expected_replies):
                raise RuntimeError("the bounded learning pass did not return a validated result")
            result_prefix = "JARVIS_LEARN_RESULT_JSON="
        print(result_prefix + json.dumps({"reply": reply}, ensure_ascii=False))
        return 0
    finally:
        try:
            if manager is not None and start_attempted and manager.our_server_pids():
                manager._stop_ours()
                if manager.our_server_pids():
                    raise RuntimeError("the exact owned temporary model server remained after shutdown")
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            lock.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Guarded Jarvis review failed: {exc.__class__.__name__}", file=sys.stderr)
        raise

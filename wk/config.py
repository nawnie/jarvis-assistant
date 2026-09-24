"""Jarvis Assistant settings: defaults, load/save, and Windows folder lookups.

Everything the assistant is allowed to watch is decided here, so this file is
the one place to read if you want to know what gets recorded.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Where Jarvis Assistant keeps its files (all local, next to the app)
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent.parent
# JARVIS_DATA_DIR lets the QA suites run a fully separate copy of Jarvis without touching your real data
DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR") or APP_DIR / "data")
DB_PATH = DATA_DIR / "jarvis.db"
CONFIG_PATH = DATA_DIR / "config.json"
ICON_PATH = DATA_DIR / "jarvis.ico"


# ---------------------------------------------------------------------------
# Real Windows folder lookup.
# Shawn's Desktop is redirected to F:\UserFolders\Desktop, so never guess
# "~/Desktop" - ask the shell (SHGetKnownFolderPath) instead.
# ---------------------------------------------------------------------------
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def known_folder(guid_text: str, fallback: str) -> str:
    """Return the real path of a Windows known folder (Desktop, Downloads...)."""
    try:
        guid = _GUID.from_buffer_copy(uuid.UUID(guid_text).bytes_le)
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
            path = out.value
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except Exception:
        pass
    return fallback


DESKTOP = known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641", r"F:\UserFolders\Desktop")
DOWNLOADS = known_folder("374DE290-123F-4565-9164-39C4925E467B", str(Path.home() / "Downloads"))

# ---------------------------------------------------------------------------
# Default settings. Any key missing from config.json falls back to these.
# ---------------------------------------------------------------------------
DEFAULTS = {
    # master switch + which sensors are allowed to run
    "watching": True,
    "watch_windows": True,       # which app/window has focus
    "watch_task_windows": True,  # visible Claude/Codex window titles, even when unfocused
    "read_local_task_prompts": True,  # recent local Codex/Claude Code user requests, no raw-copy database

    # continuity and reply behavior (edited from Memory > Configure)
    "memory_fact_mode": "all",       # all | mission | related | off
    "memory_fact_limit": 30,
    "memory_open_loops": True,
    "memory_project_updates": True,
    "memory_journal_recall": True,
    "memory_activity_minutes": 45,
    "memory_task_hours": 24,
    "project_task_context": True,
    "memory_clipboard_items": 5,
    "memory_chat_messages": 20,
    "assistant_mission": "Help Shawn build AI Embedded Systems and www.aiembeddedsystems.com.",
    "personality_mode": "operator",  # operator | companion | mission | analyst
    "personality_note": "",
    "reply_depth": "balanced",       # quick | balanced | detailed
    "reply_next_step": True,
    "tool_use_8b": True,
    "tool_daily_limit": 3,
    "memory_capture_mode": "suggest",  # suggest | off; Shawn approves every durable fact
    "watch_clipboard": True,     # text you copy
    "watch_folders": True,       # new files landing in watched folders
    "watch_system": True,        # CPU / RAM / GPU load

    # timing
    "poll_seconds": 5,           # how often the focused window is sampled
    "idle_seconds": 300,         # no input for this long = you're away
    "digest_minutes": 60,        # how often a journal entry is written
    "break_after_minutes": 50,   # continuous activity before a break nudge
    "nudge_cooldown_minutes": 20,

    # alert thresholds
    "ram_alert_percent": 92,
    "gpu_temp_alert_c": 85,

    # what to watch / what to never record
    "folders": [DOWNLOADS, DESKTOP],
    "excluded_processes": ["keepass.exe", "keepassxc.exe", "1password.exe",
                           "bitwarden.exe", "credentialuibroker.exe"],
    "excluded_title_words": ["password", "inprivate", "incognito", "private browsing"],
    "retention_days": 30,

    # local model: Jarvis runs its OWN small llama-server on port 8084 so it never
    # swaps out the model you're chatting with in llama chat (port 8080, one model at a time).
    # Ternary Bonsai 8B (2.18 GB) needs the PrismML fork build; mainline llama.cpp rejects its ternary type.
    "llm_base_url": "http://127.0.0.1:8084/v1",
    "llm_key_file": "",          # 8084 is bound to 127.0.0.1 only and started without a key
    "llm_model": "ternary-bonsai-8b",  # pinned: a blank name could make a router load a different model
    "llm_autostart_server": True,
    "llm_server_exe": r"F:\Ai_Models\llama.cpp\staged\prism-b10709-cuda133-x64\llama-server.exe",
    "llm_model_file": r"F:\Ai_Models\Language Models\AIWF LLM\GGUF\Ternary-Bonsai-8B\Ternary-Bonsai-8B-PQ2_0.gguf",
    "llm_ctx": 16384,

    # Ctrl+click anywhere to have Jarvis explain what's under the pointer.
    # "ctrl" = Ctrl+click, "ctrl+alt" = Ctrl+Alt+click (use that if Ctrl+click clashes with an app).
    # The triggering click is swallowed so the app underneath doesn't also react to it.
    "explain_on_click": True,
    "explain_trigger": "ctrl",

    # --- always on: keep the PC awake, and use the time you're away ---------------------------
    "keep_pc_awake": True,          # Windows won't sleep while Jarvis runs (the screen can still turn off)
    "away_model_enabled": True,     # while you're away, swap to the big model...
    "away_model_after_minutes": 10, # ...once you've been away this long (projects start then too)
    "away_model_retry_minutes": 1,   # retry a deferred Bonsai 2 load if the GPU later clears
    "away_model_file": r"F:\Ai_Models\Language Models\AIWF LLM\GGUF\Ternary-Bonsai-2-27B\Ternary-Bonsai-2-27B-PQ2_0.gguf",
    "away_model_alias": "ternary-bonsai-2-27b",
    "away_model_ctx": 32768,
    "away_model_vram_mb": 9500,     # VRAM the big model needs; it only loads if this much is free
    "small_model_vram_mb": 3000,
    "small_model_idle_gpu_percent": 10,  # don't load Bonsai 8B while another GPU job is active
    "small_model_idle_seconds": 60,       # require a sustained quiet interval before loading it
    "llm_gpu_layers": 999,          # layers on the GPU (999 = all); 0 runs the model on the CPU    # VRAM the small model gives back when it's swapped out
    "away_free_comfyui": True,      # if ComfyUI is idle (empty queue) and holding VRAM, ask it to unload
    "comfyui_url": "http://127.0.0.1:8000",
    "projects_enabled": True,       # work on active projects while you're away
    "project_steps_per_session": 8,
    "project_session_gap_minutes": 5,

    # phone: the PhonePcControl companion reaches Jarvis through a loopback-only API on this port
    "remote_api_enabled": True,
    "remote_api_port": 8795,

    # proactive features
    "welcome_back": True,          # when you return after being away, say what you were in the middle of
    "welcome_back_minutes": 15,
    "clipboard_error_help": True,  # copy an error/traceback -> Jarvis works out the fix in the background
    "quick_ask_hotkey": True,      # Ctrl+Alt+J anywhere -> ask bar that knows your current app + selection

    "autostart": False,
}


def load() -> dict:
    """Read config.json merged over the defaults (missing file = defaults)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass  # a broken config file should not stop the assistant starting
    return cfg


def save(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)

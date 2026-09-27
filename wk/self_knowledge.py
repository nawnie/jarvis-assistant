"""Bounded, read-only facts Jarvis can tell Shawn about itself."""
import urllib.error
from pathlib import Path

from . import config, delegate_tools, review, tool_registry


VISIBLE_SETTINGS = (
    "watching", "watch_windows", "watch_task_windows", "read_local_task_prompts", "watch_clipboard",
    "watch_folders", "watch_system", "poll_seconds", "idle_seconds",
    "folders", "excluded_processes", "excluded_title_words", "retention_days",
    "projects_enabled", "project_task_context", "llm_base_url", "llm_model",
    "llm_enabled", "llm_autostart_server", "away_model_enabled", "task_focus_27b_enabled", "remote_api_enabled",
    "memory_fact_mode", "memory_fact_limit", "memory_capture_mode", "memory_open_loops", "memory_project_updates",
    "memory_journal_recall",
    "memory_activity_minutes", "memory_task_hours", "memory_clipboard_items",
    "memory_chat_messages", "assistant_mission", "personality_mode",
    "personality_note", "reply_depth", "reply_next_step", "tool_use_8b", "tool_daily_limit",
)


def settings_text(cfg):
    return "Jarvis settings (read-only snapshot):\n" + "\n".join(
        f"- {key}: {cfg.get(key, config.DEFAULTS.get(key))}"
        for key in VISIBLE_SETTINGS
    ) + "\nChange capture settings in Settings and continuity or voice in Memory > Configure. Chat cannot change arbitrary settings."


def status_text(engine):
    task_windows = getattr(engine, "task_windows", [])
    seen = "; ".join(f"{name}: {title}" for name, title in task_windows) or "none observed"
    links = delegate_tools.available()
    pending = len(engine.store.fact_candidates())
    model_status = _model_status(engine)
    review_problem = review.review_readiness_problem(engine)
    review_status = "ready with Jarvis-owned Bonsai 2 27B" if review_problem is None else f"not ready: {review_problem}"
    guarded_review_status = ("available; per-run GPU preflight still applies"
                             if review.sentinel_vram_guard() is not None
                             and review.console_python() is not None
                             and engine.cfg.get("away_model_alias") == "ternary-bonsai-2-27b"
                             else "unavailable; Sentinel, console Python, or pinned 27B profile is missing")
    route = getattr(engine, "last_route_receipt", None) or {}
    route_status = (f"{route.get('tier', '?')} task on {route.get('chosen_profile', '?')} "
                    f"({route.get('runtime_state', 'unverified')}); "
                    f"effective context {route.get('effective_context', '?')}; "
                    f"benchmark {route.get('benchmark_state', 'unknown')} "
                    f"{route.get('benchmark_record_id') or ''}; "
                    f"focus {route.get('focus_lease', 'none')}; "
                    f"reason {route.get('reason', 'unknown')}"
                    if route else "none yet")
    return (
        "Jarvis Assistant status:\n"
        f"- Source: {config.APP_DIR}\n"
        f"- Settings file: {config.CONFIG_PATH}\n"
        f"- Local database: {config.DB_PATH}\n"
        f"- Watching: {'on' if engine.watching else 'paused'}\n"
        f"- Model chat: {model_status}\n"
        f"- Last task route: {route_status}. Routing is heuristic until an exact benchmark receipt matches.\n"
        f"- Read-only project review: {review_status}; no auto model swap or source edits. /learn may propose one quoted pending fact when enabled.\n"
        f"- Explicit /review-once: {guarded_review_status}; one sanitized packet, temporary 27B server, "
        "and no saved model preference changes.\n"
        f"- Memory suggestions waiting for Shawn: {pending}; none are recalled until kept.\n"
        f"- Bonsai 8B optional CLI consultations: {'enabled' if engine.cfg.get('tool_use_8b', False) else 'disabled'}; "
        f"Claude CLI {'installed' if links['claude'] else 'missing'}, "
        f"Codex CLI {'installed' if links['codex'] else 'missing'}. CLI consultation does not pass through CLI tools or MCP credentials.\n"
        f"- Claude/Codex windows: {seen}\n"
        "- Window tracking reads visible title bars only. I cannot read chat text, "
        "unsent drafts, or background app contents through this sensor.\n"
        f"- Local Codex/Claude Code prompt hints: {'on' if engine.cfg.get('read_local_task_prompts', False) else 'off'}; "
        "when on, recent user requests from local session files can inform chat with activity context "
        "and matching active projects while Watching is enabled. They are not stored in my database. "
        "Claude Desktop chat is not connected.\n"
        "- Use /objectives for goals, /settings for non-secret settings, /tools for callable local tools and installed AI links, "
        "and /processes for a read-only RAM snapshot. /stop requires Shawn's exact PID and identity.\n"
        + ("- PC actions: ON. Available host tools include read, search, patch, bounded checks, list, find, move, copy, rename and "
           "delete files (deletes go to the Recycle Bin), make folders, unzip, open files or programs, check "
           "disk space. Arbitrary shell commands need a separate authorized host route."
           if engine.cfg.get("pc_actions_enabled", True) else
           "- PC actions: off in settings, so I can describe steps but not do them.")
        + ("\n- Voice: ON. Shawn can say 'Hey Jarvis' and talk to me; I answer out loud (British voice), and "
           "voice requests can do everything chat can." if engine.cfg.get("voice_enabled", True) else "")
    )


def _model_status(engine):
    """Report a bounded, credential-free check of Jarvis's configured model endpoint."""
    llm = getattr(engine, "llm", None)
    if llm is None or not hasattr(llm, "_get"):
        return "online" if getattr(engine, "llm_online", False) else "offline; local model client unavailable"
    try:
        llm._get("/models", timeout=3)
        return "online"
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            reason = "configured API key rejected (HTTP 401)"
        elif exc.code == 404:
            reason = "HTTP 404; check the configured endpoint"
        else:
            reason = f"model service returned HTTP {exc.code}"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            reason = "no model server is listening at the configured endpoint"
        elif isinstance(exc.reason, TimeoutError):
            reason = "model service timed out"
        else:
            reason = "could not reach the configured local model service"
    except TimeoutError:
        reason = "model service timed out"
    except Exception:
        reason = "availability check failed; see the private Jarvis log"
    return f"offline; {reason}"


def objectives_text(engine):
    """Show durable project goals and the latest logged step without a model call."""
    projects = engine.store.projects(include_done=False)[:12]
    if not projects:
        return "No open Jarvis projects. Add one on the Projects page with a goal for away work."
    lines = ["Open Jarvis objectives (saved locally):"]
    for project in projects:
        lines.append(f"- {project['title'][:100]} [{project['status']}]: {project['goal'][:220]}")
        recent = [(kind, text) for _, kind, text in engine.store.project_log(project["id"], 12)
                  if kind in ("wrote", "note", "question", "error", "done")]
        if recent:
            kind, text = recent[-1]
            lines.append(f"  Latest logged {kind}: {' '.join(text.split())[:180]}")
    lines.append("Observed Codex/Claude requests are hints, not independent project assignments.")
    return "\n".join(lines)


def files_text():
    paths = [config.APP_DIR / "jarvis_assistant.pyw", config.APP_DIR / "wk" / "brain.py",
             config.APP_DIR / "wk" / "config.py", config.APP_DIR / "wk" / "memory_intake.py",
             config.APP_DIR / "wk" / "delegate_tools.py", config.APP_DIR / "TOOLS_AUDIT.md",
             config.APP_DIR / "JARVIS_CONTINUITY_HANDOFF.md",
             config.CONFIG_PATH, config.DB_PATH]
    lines = ["Jarvis's own files (metadata only):"]
    for path in paths:
        try:
            info = path.stat()
            lines.append(f"- {path} ({info.st_size} bytes)")
        except OSError:
            lines.append(f"- {path} (not present)")
    lines.append("/settings shows current non-secret settings. File reads require a tool call and an actual path.")
    return "\n".join(lines)


def handoff_text():
    """Return the reviewed handoff packaged with Jarvis, never an arbitrary chat log."""
    path = config.APP_DIR / "JARVIS_CONTINUITY_HANDOFF.md"
    try:
        return path.read_text(encoding="utf-8")[:12_000]
    except OSError:
        return "No reviewed Jarvis handoff is installed."


def capability_text(engine):
    return (
        shared_wiki_instructions() + "\n\n" + status_text(engine) + "\n" +
        tool_registry.describe(engine.cfg) + "\n"
        "A focused-window timeline and clipboard history are available only when "
        "Watching and their settings are enabled. Recent activity is supplied to chat "
        "only when its context option is enabled. Never claim to monitor conversation "
        "text, draft text, or a task's progress based solely on a window title. "
        "If asked to do something outside these capabilities, explain the missing access "
        "and the specific setup needed; do not say it is already active. The /review command can review only "
        "one sanitized JSON packet from the dedicated review_packets folder, and only when Jarvis's own "
        "Bonsai 2 27B is already loaded. The explicit /review-once command can start one temporary 27B "
        "server through Sentinel Jarvis when its installed VRAM guard is available; it requires a safe "
        "GPU preflight, leaves saved model preferences unchanged, and stops its owned process afterward. "
        "The user-triggered /advisory-preview file.json command reads one bounded selection packet "
        "from Jarvis data/advisory_selections, shows exact selected retained Jarvis turns and/or "
        "user-supplied transcript excerpts, and never calls Kairo. Kairo advice remains disabled "
        "under the current CAP-01 authority. "
        "Neither review route edits source. The /learn command can "
        "consider only the packet's purpose field, and may propose one verbatim quote as a pending memory "
        "candidate when memory suggestions are enabled and the same 27B profile is already loaded. It "
        "does not learn from assistant output or activate a fact without Shawn's approval."
    )


def shared_wiki_instructions():
    """Read Shawn's local project-wiki directive on every prompt construction."""
    path = Path(r"F:\RAG\AES Wiki\HARNESS_INSTRUCTIONS.md")
    try:
        return path.read_text(encoding="utf-8")[:4_000].strip()
    except OSError:
        return ("Shared project wiki instructions are unavailable at F:\\RAG\\AES Wiki. "
                "Report that gap; do not invent wiki context or handoff evidence.")

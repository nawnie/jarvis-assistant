"""Bounded, read-only facts Jarvis can tell Shawn about itself."""
from . import config, delegate_tools


VISIBLE_SETTINGS = (
    "watching", "watch_windows", "watch_task_windows", "read_local_task_prompts", "watch_clipboard",
    "watch_folders", "watch_system", "poll_seconds", "idle_seconds",
    "folders", "excluded_processes", "excluded_title_words", "retention_days",
    "projects_enabled", "project_task_context", "llm_base_url", "llm_model",
    "llm_autostart_server", "away_model_enabled", "remote_api_enabled",
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
    return (
        "Jarvis Assistant status:\n"
        f"- Source: {config.APP_DIR}\n"
        f"- Settings file: {config.CONFIG_PATH}\n"
        f"- Local database: {config.DB_PATH}\n"
        f"- Watching: {'on' if engine.watching else 'paused'}\n"
        f"- Memory suggestions waiting for Shawn: {pending}; none are recalled until kept.\n"
        f"- Bonsai 8B consultations: {'enabled' if engine.cfg.get('tool_use_8b', False) else 'disabled'}; "
        f"Claude CLI {'installed' if links['claude'] else 'missing'}, "
        f"Codex CLI {'installed' if links['codex'] else 'missing'}. CLI tools and MCP servers are not passed through.\n"
        f"- Claude/Codex windows: {seen}\n"
        "- Window tracking reads visible title bars only. I cannot read chat text, "
        "unsent drafts, or background app contents through this sensor.\n"
        f"- Local Codex/Claude Code prompt hints: {'on' if engine.cfg.get('read_local_task_prompts', False) else 'off'}; "
        "when on, recent user requests from local session files can inform chat with activity context "
        "and matching active projects while Watching is enabled. They are not stored in my database. "
        "Claude Desktop chat is not connected.\n"
        "- Use /objectives for goals, /settings for non-secret settings, /tools for installed AI links, "
        "and /processes for a read-only RAM snapshot. /stop requires Shawn's exact PID and identity. "
        "I do not run arbitrary shell commands or edit my source or settings from chat."
    )


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
    lines.append("/settings shows current non-secret settings. Chat does not read arbitrary files.")
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
        status_text(engine) + "\n"
        "A focused-window timeline and clipboard history are available only when "
        "Watching and their settings are enabled. Recent activity is supplied to chat "
        "only when its context option is enabled. Never claim to monitor conversation "
        "text, draft text, or a task's progress based solely on a window title. "
        "If asked to do something outside these capabilities, explain the missing access "
        "and the specific setup needed; do not say it is already active."
    )

"""Bounded continuity and reply preferences for Jarvis chat."""
from . import config


FACT_MODES = {"all", "mission", "related", "off"}
VOICES = {
    "operator": "Speak like a calm, capable PC operator: direct, economical, and attentive.",
    "companion": "Speak warmly and naturally; stay practical and avoid empty reassurance.",
    "mission": "Use a restrained mission-control voice: concise status, evidence, and the next action.",
    "analyst": "Prioritize source, uncertainty, and causal reasoning; be precise without sounding stiff.",
}
DEPTH_TOKENS = {"quick": 450, "balanced": 900, "detailed": 1500}


def choice(cfg, key, allowed):
    value = cfg.get(key, config.DEFAULTS[key])
    return value if isinstance(value, str) and value in allowed else config.DEFAULTS[key]


def bounded_int(cfg, key, low, high):
    try:
        value = int(cfg.get(key, config.DEFAULTS[key]))
    except (TypeError, ValueError):
        value = config.DEFAULTS[key]
    return max(low, min(high, value))


def reply_instructions(cfg):
    mission = str(cfg.get("assistant_mission") or config.DEFAULTS["assistant_mission"]).strip()[:400]
    voice = VOICES[choice(cfg, "personality_mode", VOICES)]
    depth = choice(cfg, "reply_depth", DEPTH_TOKENS)
    note = str(cfg.get("personality_note") or "").strip()[:300]
    lines = [f"Standing mission from Shawn: {mission}", voice,
             f"Answer depth: {depth}. Keep the answer proportional to the request."]
    if cfg.get("reply_next_step", True):
        lines.append("When useful, end with one concrete next step grounded in observed state.")
    if note:
        lines.append(f"Shawn's style preference: {note}")
    lines.append("These preferences never authorize new access, editing, or claims without evidence.")
    return "\n".join(lines)


def memory_context(store, question, cfg):
    """Explicit memory stays available even when live activity context is switched off."""
    sections = []
    mode = choice(cfg, "memory_fact_mode", FACT_MODES)
    facts = store.facts()
    limit = bounded_int(cfg, "memory_fact_limit", 1, 100)
    if mode in ("related", "mission"):
        words = [word for word in store.keywords(question) if len(word) >= 3]
        mission_words = ([word for word in store.keywords(str(cfg.get("assistant_mission", ""))) if len(word) >= 5]
                         if mode == "mission" else [])
        scored = [(2 * sum(word in fact.lower() for word in words)
                   + sum(word in fact.lower() for word in mission_words), fid, fact) for fid, fact in facts]
        selected = sorted((row for row in scored if row[0]), key=lambda row: (row[0], row[1]), reverse=True)[:limit]
        if not selected:
            selected = [(0, fid, fact) for fid, fact in facts[-min(3, limit):]]
        facts = [(fid, fact) for _, fid, fact in reversed(selected)]
    elif mode == "all":
        facts = facts[-limit:]
    else:
        facts = []
    if facts:
        sections.append("Facts Shawn explicitly saved (treat as user-provided, not independently verified):\n" +
                        "\n".join(f"- {fact[:400]}" for _, fact in facts))

    projects = [p for p in store.projects(include_done=False) if p["status"] in ("active", "needs_input")][:6]
    if cfg.get("memory_open_loops", True):
        reminders = [r for r in store.reminders() if not r[3]][:5]
        lines = [f"- Project {p['title'][:100]}: {p['status']}; goal {p['goal'][:180]}" for p in projects]
        lines += [f"- Reminder: {text[:160]}" for _, _, text, _ in reminders]
        if lines:
            sections.append("Open loops recorded in Jarvis:\n" + "\n".join(lines))
    if cfg.get("memory_project_updates", True) and projects:
        updates = []
        for project in projects[:4]:
            recent = [(kind, text) for _, kind, text in store.project_log(project["id"], 8)
                      if kind in ("wrote", "note", "question", "error", "done")]
            if recent:
                kind, text = recent[-1]
                updates.append(f"- {project['title'][:80]} [{kind}]: {' '.join(text.split())[:180]}")
        if updates:
            sections.append("Recent project progress (logged by Jarvis, verify outcomes):\n" + "\n".join(updates))

    if cfg.get("memory_journal_recall", True):
        words = [word for word in store.keywords(question) if len(word) >= 4]
        matches = []
        for _, _, _, _, entry in store.journals(12):
            score = sum(word in entry.lower() for word in words)
            if score:
                matches.append((score, entry))
        matches.sort(key=lambda row: row[0], reverse=True)
        if matches:
            sections.append("Related past journal summaries (model-written, may need verification):\n" +
                            "\n".join(f"- {' '.join(entry.split())[:260]}" for _, entry in matches[:2]))
    return "\n\n".join(sections) or "No saved memory selected for this reply."

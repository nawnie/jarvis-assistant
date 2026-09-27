"""Game help prompts: fixing a game, asking about what's on screen, and spoiler-safe puzzle hints.

Only builds the messages for the local model; the UI (feature_pages.GameHelpBar) sends them and
shows the answer. Jarvis cannot browse the web, so answers come from the model's own knowledge
plus the evidence Jarvis gathered - the prompts say so, so it flags guesses instead of inventing.
"""

# ---------------------------------------------------------------------------
# this is the hint-ladder section: each level reveals a bit more than the last
# ---------------------------------------------------------------------------
HINT_LEVELS = {
    1: ("Nudge", "Give ONE gentle nudge: point at what to pay attention to (an object, a pattern, a mechanic) "
                 "without saying what to do with it. No solution, no steps. One or two sentences."),
    2: ("Bigger hint", "Name the mechanic or idea that solves it and where to apply it, but stop short of the "
                       "full answer. Two or three sentences."),
    3: ("Solution", "Give the full solution as short numbered steps."),
}

_BASE = ("You are Jarvis, Shawn's local game helper. You cannot browse the web: answer from what you know "
         "about the game and the evidence given. If you are not sure about a specific detail (a quest step, "
         "a combination, a menu name), say it is a best guess rather than inventing it.")


def puzzle_messages(game_name, question, level, screen_text=""):
    label, rule = HINT_LEVELS.get(level, HINT_LEVELS[1])
    user = (f"Game: {game_name or 'unknown'}\n"
            + (f"Text visible on screen (OCR, may be partial):\n{screen_text[:2500]}\n\n" if screen_text else "")
            + f"Shawn is stuck on: {question}\n\nHint level requested: {label}. {rule}")
    return [{"role": "system", "content": _BASE}, {"role": "user", "content": user}]


def fix_messages(game_name, diagnosis_facts, evidence, question=""):
    facts = "\n".join(f"- {f}" for f in diagnosis_facts)
    user = (f"Game: {game_name}\nShawn wants this game working"
            + (f"; his description: {question}" if question else "") + ".\n\n"
            f"What Jarvis found on the PC:\n{facts}\n\nEvidence:\n{evidence[:7000] or '(none)'}\n\n"
            "Reply in this shape:\n"
            "**Most likely cause** - one or two sentences, naming the specific mod, plugin, dll or setting if the "
            "evidence points at one.\n"
            "**Fix** - concrete steps, in order, most likely first. For mod setups say which mod to disable or "
            "update, and remind him to test after each change.\n"
            "**If that doesn't work** - one next thing to check.\n"
            "Base everything on the evidence; don't list generic advice the evidence doesn't support.")
    return [{"role": "system", "content": _BASE}, {"role": "user", "content": user}]


def screen_messages(game_name, question, screen_text):
    user = (f"Game: {game_name or 'unknown'}\n"
            f"Text visible on screen right now (OCR, may be partial or out of order):\n{screen_text[:3500] or '(none read)'}\n\n"
            f"Shawn asks: {question or 'What am I looking at, and what should I do here?'}\n\n"
            "Answer briefly and specifically for this game. If it's a menu or prompt, say what each "
            "option does and which one he probably wants.")
    return [{"role": "system", "content": _BASE}, {"role": "user", "content": user}]

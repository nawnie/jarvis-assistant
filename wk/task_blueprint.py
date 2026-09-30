"""Local, explicit chat selection for a future Kairo advisory review.

This module does not connect to Kairo. CAP-01 currently grants no execution
authority, so an external provider must be supplied by a later, separately
authorized integration. Sources are retained Jarvis rows or explicit user input.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass

from . import config
from .review import SECRET_PATTERN

MAX_TURNS = 12
MAX_EXCERPT_CHARS = 1200
MAX_TOTAL_CHARS = 8000
SELECTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.json", re.IGNORECASE)


@dataclass(frozen=True)
class AdvisoryClaim:
    text: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class JarvisAdvisoryBlueprint:
    goal: str
    goal_source_refs: tuple[str, ...]
    constraints: tuple[AdvisoryClaim, ...]
    ordered_steps: tuple[AdvisoryClaim, ...]
    checkpoints: tuple[AdvisoryClaim, ...]
    verification: tuple[AdvisoryClaim, ...]
    conflicts: tuple[AdvisoryClaim, ...]
    unknowns: tuple[AdvisoryClaim, ...]
    provenance: tuple[dict, ...]
    authority: str = "advisory_only"


@dataclass
class ApprovedPreview:
    """One-use token minted by a host UI only after a direct owner gesture."""
    sha256: str
    consumed: bool = False


def _bounded_text(value: str, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must be nonempty text of at most {limit} characters")
    if SECRET_PATTERN.search(value):
        raise ValueError(f"{label} appears to contain a credential; redact it first")
    return value.strip()


def preview_selected_sources(store, message_ids: list[int], imports: list[dict], goal: str) -> dict:
    """Prepare explicit retained Jarvis rows and/or user-supplied chat excerpts.

    Imported conversation/message IDs and timestamps are never authenticated;
    preview_ref is a local pointer within this digest, not a source message ID.
    No other app's storage, attachment, or chat database is opened here.
    """
    goal = _bounded_text(goal, 500, "goal")
    if (not isinstance(message_ids, list) or len(message_ids) > MAX_TURNS
            or any(type(i) is not int or i <= 0 for i in message_ids)
            or len(set(message_ids)) != len(message_ids)):
        raise ValueError("select at most 12 distinct positive Jarvis chat message IDs")
    if not isinstance(imports, list) or len(imports) > 4:
        raise ValueError("at most four user-supplied conversations may be selected")
    excerpts = []
    total = 0
    if message_ids:
        if store is None:
            raise ValueError("Jarvis chat storage is required for selected Jarvis IDs")
        marks = ",".join("?" for _ in message_ids)
        rows = store.rows(f"SELECT id, ts, role, text FROM chat WHERE id IN ({marks}) ORDER BY id",
                          tuple(message_ids))
        by_id = {int(row[0]): row for row in rows}
        if set(by_id) != set(message_ids):
            raise ValueError("one or more selected Jarvis chat IDs are missing")
        for message_id in message_ids:
            _, timestamp, role, text = by_id[message_id]
            if role not in {"user", "assistant"}:
                raise ValueError("selected chat row has an unsupported role")
            clean = _bounded_text(text, MAX_EXCERPT_CHARS, "selected excerpt")
            total += len(clean)
            excerpts.append({"preview_ref": f"jarvis:{message_id}", "source": "jarvis_retained_chat",
                             "conversation_id": None, "message_id": message_id,
                             "timestamp": timestamp, "role": role, "text": clean,
                             "provenance_assurance": "jarvis_local_row"})
    for conversation_index, conversation in enumerate(imports):
        if not isinstance(conversation, dict) or set(conversation) != {
                "source_label", "conversation_id", "selected_messages"}:
            raise ValueError("invalid user-supplied conversation shape")
        label = _bounded_text(conversation["source_label"], 80, "source label")
        conversation_id = conversation["conversation_id"]
        if conversation_id is not None:
            conversation_id = _bounded_text(conversation_id, 100, "conversation ID")
        selected = conversation["selected_messages"]
        if not isinstance(selected, list) or not selected or len(selected) > MAX_TURNS:
            raise ValueError("select 1 to 12 messages per supplied conversation")
        for message_index, item in enumerate(selected):
            if not isinstance(item, dict) or set(item) != {"message_id", "timestamp", "role", "text"}:
                raise ValueError("invalid supplied message shape")
            if item["role"] not in {"user", "assistant"}:
                raise ValueError("unsupported supplied message role")
            message_id = item["message_id"]
            timestamp = item["timestamp"]
            if message_id is not None:
                message_id = _bounded_text(message_id, 100, "supplied message ID")
            if timestamp is not None:
                timestamp = _bounded_text(timestamp, 40, "supplied timestamp")
            clean = _bounded_text(item["text"], MAX_EXCERPT_CHARS, "selected excerpt")
            total += len(clean)
            excerpts.append({"preview_ref": f"input:{conversation_index}:{message_index}",
                             "source": "user_supplied_transcript", "source_label": label,
                             "conversation_id": conversation_id, "message_id": message_id,
                             "timestamp": timestamp, "role": item["role"], "text": clean,
                             "provenance_assurance": "user_supplied_unverified"})
    if not 1 <= len(excerpts) <= MAX_TURNS or total > MAX_TOTAL_CHARS:
        raise ValueError("select 1 to 12 excerpts totaling at most 8000 characters")
    payload = {"goal": goal, "excerpts": excerpts, "authority": "advisory_only"}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"payload": payload, "sha256": digest,
            "notice": "Review the exact goal and excerpts. Imported IDs and times are user-supplied, unverified. No Kairo connection is configured."}


def preview_selected_chat(store, message_ids: list[int], goal: str) -> dict:
    """Compatibility entry point for explicit IDs in the retained flat Jarvis chat."""
    return preview_selected_sources(store, message_ids, [], goal)


def preview_selection_file(store, filename: str) -> dict:
    """User-triggered, fixed-folder JSON selection; no background chat scan."""
    if not isinstance(filename, str) or not SELECTION_NAME.fullmatch(filename):
        raise ValueError("use /advisory-preview followed by one JSON filename")
    folder = config.DATA_DIR / "advisory_selections"
    path = folder / filename
    if (folder.is_symlink() or getattr(folder, "is_junction", lambda: False)()
            or path.is_symlink() or getattr(path, "is_junction", lambda: False)()
            or not path.is_file()):
        raise ValueError("selection must be a regular file in Jarvis data/advisory_selections")
    if path.resolve().parent != folder.resolve() or path.stat().st_size > 48 * 1024:
        raise ValueError("selection path or size is invalid")
    with path.open("rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError("selection must be a regular file")
        raw = file.read(48 * 1024 + 1)
    if len(raw) > 48 * 1024:
        raise ValueError("selection is too large")
    def reject_duplicate_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key in selection")
            value[key] = item
        return value
    data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=reject_duplicate_keys)
    if not isinstance(data, dict) or set(data) != {"goal", "jarvis_message_ids", "imported_conversations"}:
        raise ValueError("selection requires goal, jarvis_message_ids and imported_conversations")
    return preview_selected_sources(store, data["jarvis_message_ids"], data["imported_conversations"],
                                    data["goal"])


def preview_command_reply(store, command: str) -> str:
    """Direct slash-command surface; it never submits a provider request."""
    parts = command.strip().split()
    if len(parts) != 2 or parts[0].casefold() != "/advisory-preview":
        return "Use /advisory-preview filename.json after placing a selected JSON packet in data/advisory_selections."
    try:
        preview = preview_selection_file(store, parts[1])
    except (OSError, ValueError, UnicodeError, TypeError) as exc:
        return f"Advisory preview unavailable: {exc}"
    return format_preview(preview)


def format_preview(preview: dict) -> str:
    """Show exact selected data and digest before any owner approval."""
    lines = ["Advisory preview only; Kairo provider disabled under CAP-01.",
             "Goal: " + preview["payload"]["goal"]]
    for row in preview["payload"]["excerpts"]:
        identity = f"{row['source']} / {row.get('conversation_id') or 'no conversation ID'} / "
        identity += f"{row.get('message_id') or 'no source message ID'} / {row.get('timestamp') or 'no timestamp'}"
        lines.append(f"[{row['preview_ref']}] {identity} ({row['role']}; {row['provenance_assurance']}):")
        lines.extend("> " + line for line in row["text"].splitlines())
    lines.append("Exact preview SHA256: " + preview["sha256"])
    lines.append("Owner confirmation path: /advisory-request <exact preview SHA256>. "
                 "The production provider remains disabled under CAP-01.")
    lines.append("No advice was requested or executed. Imported provenance is user-supplied and unverified.")
    return "\n".join(lines)


def _source_refs(value, allowed: set[str], label: str) -> tuple[str, ...]:
    if (not isinstance(value, list) or not value
            or any(not isinstance(item, str) or item not in allowed for item in value)):
        raise ValueError(f"{label} must cite selected preview refs")
    return tuple(dict.fromkeys(value))


def validated_blueprint(candidate: dict, preview: dict) -> JarvisAdvisoryBlueprint:
    """Validate a synthetic or future advisory response, never turn it into a permit."""
    if not isinstance(candidate, dict) or set(candidate) != {
            "goal", "goal_source_refs", "constraints", "ordered_steps",
            "checkpoints", "verification", "conflicts", "unknowns"}:
        raise ValueError("advisory blueprint has unexpected fields")
    if candidate["goal"] != preview["payload"]["goal"]:
        raise ValueError("advisory goal changed")
    allowed = {row["preview_ref"] for row in preview["payload"]["excerpts"]}
    goal_sources = _source_refs(candidate["goal_source_refs"], allowed, "goal")
    fields = {}
    for field in ("constraints", "ordered_steps", "checkpoints", "verification", "conflicts", "unknowns"):
        value = candidate[field]
        minimum = 1 if field in {"ordered_steps", "verification"} else 0
        if not isinstance(value, list) or not minimum <= len(value) <= 12:
            raise ValueError(f"invalid {field}")
        claims = []
        for item in value:
            if not isinstance(item, dict) or set(item) != {"text", "source_refs"}:
                raise ValueError(f"invalid {field} claim")
            claims.append(AdvisoryClaim(_bounded_text(item["text"], 500, field),
                                        _source_refs(item["source_refs"], allowed, field)))
        fields[field] = tuple(claims)
    return JarvisAdvisoryBlueprint(goal=candidate["goal"], goal_source_refs=goal_sources,
                                  provenance=tuple(
        {key: value for key, value in row.items() if key != "text" and key != "role"}
        for row in preview["payload"]["excerpts"]), **fields)


def approve_preview(preview: dict, confirmed_sha256: str, *, owner_confirmed: bool) -> ApprovedPreview:
    if not owner_confirmed or confirmed_sha256 != preview.get("sha256"):
        raise PermissionError("the owner has not confirmed the exact preview digest")
    return ApprovedPreview(confirmed_sha256)


def request_advice(preview: dict, approval: ApprovedPreview, provider=None) -> JarvisAdvisoryBlueprint:
    """Fail closed until a later authorized provider is deliberately supplied.

    The caller must obtain approval through a host UI direct owner action, never
    from a model or imported text. No live provider is shipped or imported here.
    """
    if (not isinstance(approval, ApprovedPreview) or approval.consumed
            or approval.sha256 != preview.get("sha256")):
        raise PermissionError("the exact preview has no unused owner approval")
    approval.consumed = True
    if provider is None:
        raise PermissionError("Kairo advisory provider is disabled under the current CAP-01 authority")
    return validated_blueprint(provider(preview["payload"]), preview)


def format_blueprint(blueprint: JarvisAdvisoryBlueprint) -> str:
    """Show sourced advisory claims without implying an execution permit."""
    lines = ["Advisory only; no execution permit or action was issued.",
             "Goal: " + blueprint.goal + " [" + ", ".join(blueprint.goal_source_refs) + "]"]
    for label in ("constraints", "ordered_steps", "checkpoints", "verification", "conflicts", "unknowns"):
        lines.append(label.replace("_", " ").title() + ":")
        claims = getattr(blueprint, label)
        lines.extend(f"- {claim.text} [{', '.join(claim.source_refs)}]" for claim in claims)
        if not claims:
            lines.append("- none supplied")
    return "\n".join(lines)

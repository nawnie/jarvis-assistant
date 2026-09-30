"""Deterministic task tier, model preference, and effective prompt budget.

The preference is a policy decision, not a measured performance claim. Exact
benchmark claims require a receipt for this configured model and server shape.
"""
from __future__ import annotations

import json
import hashlib
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class Route:
    tier: str
    preferred_profile: str
    reason: str
    benchmark_state: str = "unknown_no_exact_receipt"


DEEP_TERMS = ("architect", "design", "debug", "traceback", "refactor", "security",
              "test suite", "multi-file", "multiple files", "research", "review code",
              "plan", "implement", "fix the project")
VISION_TERMS = ("screenshot", "look at the screen", "what is on my screen", "image", "photo")
TASK_SUITES = {"quick": "jarvis-quick-v1", "normal": "jarvis-general-v1",
               "deep": "jarvis-project-v1", "vision": "jarvis-vision-v1"}


def _evidence_part(item, key, index):
    if isinstance(item, dict):
        return item.get(key)
    if isinstance(item, tuple) and len(item) > index:
        return item[index]
    return None


def choose(question: str, cfg: dict, evidence: dict[str, tuple[str, str | None]] | None = None) -> Route:
    """Select a tier from the current request, never from ambient observations."""
    text = question.casefold()
    pinned = cfg.get("llm_pinned_profile")
    if any(term in text for term in VISION_TERMS):
        tier, preferred, reason = "vision", "big", "request names visual input"
    elif any(term in text for term in DEEP_TERMS) or len(text) > 1500:
        tier, preferred, reason = "deep", "big", "request names sustained project work"
    elif len(text) < 160 and not any(c in text for c in ("\n", "?")):
        tier, preferred, reason = "quick", "small", "short single-turn request"
    else:
        tier, preferred, reason = "normal", "small", "ordinary request"
    if pinned in {"small", "big"}:
        preferred, reason = pinned, "owner-pinned model profile"
    if preferred == "big" and not cfg.get("away_model_enabled", True):
        preferred, reason = "small", "27B profile is disabled"
    evidence = evidence or {}
    preferred_evidence = evidence.get(preferred)
    state = _evidence_part(preferred_evidence, "state", 0) or "unknown_no_exact_receipt"
    other = "small" if preferred == "big" else "big"
    other_evidence = evidence.get(other)
    other_state = _evidence_part(other_evidence, "state", 0) or "unknown_no_exact_receipt"
    if (pinned not in {"small", "big"} and state == "exact_fail"
            and other_state == "exact_pass" and (other != "big" or cfg.get("away_model_enabled", True))):
        preferred, reason, state = other, "exact task-tier receipt favors the alternate profile", other_state
    elif (pinned not in {"small", "big"} and state == other_state == "exact_pass"
          and (other != "big" or cfg.get("away_model_enabled", True))):
        score = _evidence_part(preferred_evidence, "score", 2)
        alternate_score = _evidence_part(other_evidence, "score", 2)
        same_suite = (_evidence_part(preferred_evidence, "suite", 4)
                      == _evidence_part(other_evidence, "suite", 4) == TASK_SUITES[tier])
        same_metric = (_evidence_part(preferred_evidence, "metric", 3)
                       == _evidence_part(other_evidence, "metric", 3) == "success_rate")
        if (same_suite and same_metric and type(score) in {int, float}
                and type(alternate_score) in {int, float} and math.isfinite(score)
                and math.isfinite(alternate_score) and 0 <= score <= 1
                and 0 <= alternate_score <= 1 and alternate_score > score + 0.02):
            preferred, reason = other, "higher comparable exact task-tier success rate"
        else:
            reason += "; both exact passes lack a decisive comparable score"
    elif state == "exact_pass":
        reason += "; exact task-tier receipt matches this profile"
    return Route(tier, preferred, reason,
                 _evidence_part(evidence.get(preferred), "state", 0) or state)


def exact_benchmark_state(profile: dict, runtime: dict, receipt: dict | None) -> str:
    """Accept only an exact dated model/quant/runtime/context/KV/batch/vision test receipt."""
    if not profile.get("hash_verified") or not runtime.get("identity_verified"):
        return "unknown_configuration_identity_incomplete"
    if not isinstance(receipt, dict) or receipt.get("result") not in {"pass", "fail"}:
        return "unknown_no_exact_receipt"
    tier = receipt.get("task_tier")
    if tier not in TASK_SUITES or receipt.get("test_suite") != TASK_SUITES[tier]:
        return "unknown_invalid_test_suite"
    try:
        tested = date.fromisoformat(receipt["date"])
    except (TypeError, ValueError, KeyError):
        return "unknown_invalid_test_date"
    if tested > date.today() or date.today() - tested > timedelta(days=365):
        return "unknown_stale_test_date"
    expected = {
        "model_file": profile.get("file"), "model_sha256": profile.get("sha256"),
        "quant": profile.get("quant"), "runtime_sha256": runtime.get("sha256"),
        "context": profile.get("ctx"), "kv": runtime.get("kv"),
        "batch": runtime.get("batch"), "ubatch": runtime.get("ubatch"),
        "vision": bool(profile.get("mmproj")),
        "vision_projector_sha256": profile.get("mmproj_sha256") if profile.get("mmproj") else "none",
        "task_tier": tier, "test_suite": TASK_SUITES[tier],
        "date": receipt.get("date"),
    }
    if any(value is None or value == "" for value in expected.values()):
        return "unknown_configuration_identity_incomplete"
    if any(receipt.get(key) != value for key, value in expected.items()):
        return "unknown_receipt_configuration_mismatch"
    return "exact_" + receipt["result"]


def _catalog_rows(path: Path) -> list[dict]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
            return []
        rows = json.loads(path.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) and len(rows) <= 100 else []
    except (OSError, ValueError, UnicodeError):
        return []


def candidate_receipt_exists(path: Path, model_file: str, tier: str) -> bool:
    return any(isinstance(row, dict) and row.get("model_file") == model_file
               and row.get("task_tier") == tier for row in _catalog_rows(path))


def _verified_file_sha256(value: str) -> str | None:
    try:
        path = Path(value)
        if path.is_symlink() or not path.is_file():
            return None
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while chunk := file.read(4 * 1024 * 1024):
                digest.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
        return digest.hexdigest()
    except (OSError, ValueError, TypeError):
        return None


def verified_identity(profile: dict, cfg: dict) -> tuple[dict, dict]:
    """Hash exact local artifacts for a candidate receipt; never load a model."""
    model_hash = _verified_file_sha256(profile.get("file"))
    runtime_hash = _verified_file_sha256(cfg.get("llm_server_exe"))
    projector = profile.get("mmproj")
    projector_hash = _verified_file_sha256(projector) if projector else "none"
    quant = Path(profile.get("file") or "").stem.rsplit("-", 1)[-1]
    p = {**profile, "sha256": model_hash, "quant": quant,
         "mmproj_sha256": projector_hash,
         "hash_verified": bool(model_hash and (not projector or projector_hash))}
    r = {"sha256": runtime_hash, "kv": "q8_0",
         "batch": cfg.get("llm_batch"), "ubatch": cfg.get("llm_ubatch"),
         "identity_verified": bool(runtime_hash and isinstance(cfg.get("llm_batch"), int)
                                   and isinstance(cfg.get("llm_ubatch"), int))}
    return p, r


def benchmark_catalog_evidence(profile: dict, runtime: dict, path: Path,
                               tier: str | None = None) -> dict:
    """Read a bounded local catalog; return exact comparable evidence or unknown.

    The caller supplies Jarvis's fixed data path, never a model argument.
    No catalog exists in the shipped source, so current claims remain unknown.
    """
    for row in _catalog_rows(path):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        if tier is not None and row.get("task_tier") != tier:
            continue
        state = exact_benchmark_state(profile, runtime, row)
        if state.startswith("exact_"):
            score = row.get("score")
            if type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 1:
                score = None
            return {"state": state, "id": row["id"][:80], "score": score,
                    "metric": row.get("metric"), "suite": row.get("test_suite"),
                    "date": row.get("date")}
    return {"state": "unknown_no_exact_receipt", "id": None, "score": None,
            "metric": None, "suite": None, "date": None}


def benchmark_catalog_match(profile: dict, runtime: dict, path: Path,
                            tier: str | None = None) -> tuple[str, str | None]:
    """Compatibility pair for callers needing only state and record ID."""
    evidence = benchmark_catalog_evidence(profile, runtime, path, tier)
    return evidence["state"], evidence["id"]


def estimate_tokens(message: dict) -> int:
    # Conservative heuristic; exact tokenization depends on the loaded GGUF.
    return (len(json.dumps(message, ensure_ascii=False).encode("utf-8")) + 2) // 3 + 8


def budget_messages(messages: list[dict], tier: str, server_context: int,
                    output_tokens: int) -> tuple[list[dict], dict]:
    """Keep system and complete recent user turns, including tool-call groups.

    Returned context is an estimate within the server ceiling. If mandatory
    content cannot fit, fail visibly instead of silently truncating user text.
    """
    if not messages or messages[0].get("role") != "system" or server_context < 2048:
        raise ValueError("invalid task context")
    cap = {"quick": 8192, "normal": 12288, "deep": 24576, "vision": 24576}.get(tier)
    if cap is None:
        raise ValueError("invalid task tier")
    effective = min(int(server_context), cap)
    # Reserve output, tool schemas, and tokenizer-estimate error headroom.
    prompt_limit = min(effective - int(output_tokens) - 1024, int(effective * 0.72))
    if prompt_limit < 512:
        raise ValueError("the configured server context is too small for this task")
    groups: list[list[tuple[int, dict]]] = []
    group: list[tuple[int, dict]] = []
    for i, message in enumerate(messages[1:], 1):
        if message.get("role") == "user" and group:
            groups.append(group)
            group = []
        group.append((i, message))
    if group:
        groups.append(group)
    if not groups or not any(message.get("role") == "user" for _, message in groups[-1]):
        raise ValueError("task has no user message")
    for group in groups:
        calls = {call.get("id") for _, message in group for call in (message.get("tool_calls") or [])
                 if isinstance(call, dict)}
        results = {message.get("tool_call_id") for _, message in group
                   if message.get("role") == "tool"}
        if calls != results:
            raise ValueError("incomplete assistant tool-call group in task context")
    required = {0, *(i for i, _ in groups[-1])}
    used = sum(estimate_tokens(messages[i]) for i in required)
    if used > prompt_limit:
        raise ValueError("current request and system context exceed the task prompt budget")
    keep = set(required)
    for group in reversed(groups[:-1]):
        size = sum(estimate_tokens(message) for _, message in group)
        if used + size > prompt_limit:
            break
        keep.update(i for i, _ in group)
        used += size
    retained = [message for i, message in enumerate(messages) if i in keep]
    return retained, {"effective_context": effective, "estimated_prompt_tokens": used,
                      "output_reserve": int(output_tokens),
                      "dropped_prior_messages": len(messages) - len(retained),
                      "estimate_only": True}

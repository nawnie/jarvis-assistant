import json
from datetime import date

import pytest

from wk import task_blueprint, task_routing


class ChatRows:
    def rows(self, _query, ids):
        values = {3: (3, 1000.0, "user", "Please plan the dashboard"),
                  8: (8, 1001.0, "assistant", "A staged test is needed")}
        return [values[i] for i in sorted(ids) if i in values]


def test_selected_chat_preview_and_advisory_provenance():
    preview = task_blueprint.preview_selected_chat(ChatRows(), [8, 3], "Build the dashboard")
    assert [row["message_id"] for row in preview["payload"]["excerpts"]] == [8, 3]
    approval = task_blueprint.approve_preview(preview, preview["sha256"], owner_confirmed=True)
    assert "provider is disabled" in str(pytest.raises(PermissionError, task_blueprint.request_advice,
                                                    preview, approval).value)
    claim = {"text": "Validate a local prototype", "source_refs": ["jarvis:3", "jarvis:8"]}
    candidate = {"goal": "Build the dashboard", "goal_source_refs": ["jarvis:3"],
                 "constraints": [], "ordered_steps": [claim], "checkpoints": [claim],
                 "verification": [claim], "conflicts": [], "unknowns": []}
    approval = task_blueprint.approve_preview(preview, preview["sha256"], owner_confirmed=True)
    result = task_blueprint.request_advice(preview, approval, lambda _payload: candidate)
    assert result.authority == "advisory_only"
    assert result.ordered_steps[0].source_refs == ("jarvis:3", "jarvis:8")
    with pytest.raises(ValueError, match="selected preview refs"):
        task_blueprint.validated_blueprint({**candidate, "unknowns": [
            {"text": "Unsupported claim", "source_refs": ["jarvis:999"]}]}, preview)
    with pytest.raises(PermissionError, match="no unused owner approval"):
        task_blueprint.request_advice(preview, approval, lambda _payload: candidate)
    with pytest.raises(PermissionError, match="owner has not confirmed"):
        task_blueprint.approve_preview(preview, "0" * 64, owner_confirmed=True)


def test_bounded_user_supplied_multi_conversation_preview(tmp_path, monkeypatch):
    imports = [
        {"source_label": "Pasted planning chat", "conversation_id": "chat-a", "selected_messages": [
            {"message_id": "m1", "timestamp": "2026-09-27T08:00:00Z", "role": "user",
             "text": "Build the dashboard"}]},
        {"source_label": "Pasted design chat", "conversation_id": None, "selected_messages": [
            {"message_id": None, "timestamp": None, "role": "assistant",
             "text": "Test the layout before release"}]},
    ]
    preview = task_blueprint.preview_selected_sources(ChatRows(), [3], imports, "Build the dashboard")
    refs = [row["preview_ref"] for row in preview["payload"]["excerpts"]]
    assert refs == ["jarvis:3", "input:0:0", "input:1:0"]
    assert preview["payload"]["excerpts"][2]["message_id"] is None
    claim = {"text": "Check the layout", "source_refs": ["input:1:0"]}
    candidate = {"goal": "Build the dashboard", "goal_source_refs": ["jarvis:3", "input:0:0"],
                 "constraints": [], "ordered_steps": [claim], "checkpoints": [],
                 "verification": [claim], "conflicts": [], "unknowns": []}
    result = task_blueprint.validated_blueprint(candidate, preview)
    assert result.ordered_steps[0].source_refs == ("input:1:0",)
    assert result.provenance[2]["provenance_assurance"] == "user_supplied_unverified"
    with pytest.raises(ValueError, match="credential"):
        task_blueprint.preview_selected_sources(None, [], [{**imports[0], "selected_messages": [
            {**imports[0]["selected_messages"][0], "text": "password=abc"}]}], "Goal")
    folder = tmp_path / "advisory_selections"
    folder.mkdir()
    (folder / "selected.json").write_text(json.dumps({
        "goal": "Build the dashboard", "jarvis_message_ids": [3],
        "imported_conversations": imports}), encoding="utf-8")
    monkeypatch.setattr(task_blueprint.config, "DATA_DIR", tmp_path)
    reply = task_blueprint.preview_command_reply(ChatRows(), "/advisory-preview selected.json")
    assert "input:1:0" in reply and "Exact preview SHA256:" in reply
    assert "No advice was requested or executed" in reply


def test_engine_direct_owner_request_stays_disabled_or_uses_only_injected_double(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from wk.brain import Engine

    folder = tmp_path / "advisory_selections"
    folder.mkdir()
    (folder / "selected.json").write_text(json.dumps({
        "goal": "Plan one task", "jarvis_message_ids": [],
        "imported_conversations": [{"source_label": "Pasted chat", "conversation_id": "chat-1",
                                    "selected_messages": [{"message_id": "m1", "timestamp": None,
                                                           "role": "user", "text": "Plan one task"}]}]}),
        encoding="utf-8")
    monkeypatch.setattr(task_blueprint.config, "DATA_DIR", tmp_path)

    class Store(ChatRows):
        def add_chat(self, role, text):
            pass

    engine = SimpleNamespace(store=Store(), cfg={}, data_changed=SimpleNamespace(emit=lambda _kind: None))
    reply = Engine.chat_reply(engine, "/advisory-preview selected.json")
    digest = reply.split("Exact preview SHA256: ", 1)[1].splitlines()[0]
    assert "input:0:0" in reply
    assert "direct desktop confirmation" in Engine.chat_reply(engine, "/advisory-request " + digest)
    approval = task_blueprint.approve_preview(engine._advisory_preview, digest, owner_confirmed=True)
    assert "provider is disabled" in Engine.chat_reply(
        engine, "/advisory-request " + digest, advisory_approval=approval)
    assert "Preview a selected packet first" in Engine.chat_reply(engine, "/advisory-request " + digest)

    claim = {"text": "Check the plan", "source_refs": ["input:0:0"]}
    engine._advisory_provider = lambda _payload: {
        "goal": "Plan one task", "goal_source_refs": ["input:0:0"],
        "constraints": [], "ordered_steps": [claim], "checkpoints": [],
        "verification": [claim], "conflicts": [], "unknowns": []}
    Engine.chat_reply(engine, "/advisory-preview selected.json")
    # Voice and signed remote chat use the same text path but cannot supply the
    # desktop confirmation token, even with a test provider injected.
    assert "direct desktop confirmation" in Engine.chat_reply(engine, "/advisory-request " + digest)
    approval = task_blueprint.approve_preview(engine._advisory_preview, digest, owner_confirmed=True)
    response = Engine.chat_reply(engine, "/advisory-request " + digest, advisory_approval=approval)
    assert "Advisory only" in response and "Check the plan [input:0:0]" in response


def test_route_budget_and_exact_evidence_boundary():
    cfg = {"away_model_enabled": True}
    assert task_routing.choose("hi", cfg).preferred_profile == "small"
    assert task_routing.choose("Debug the multi-file project", cfg).preferred_profile == "big"
    assert task_routing.choose("Debug the project", {**cfg, "llm_pinned_profile": "small"}).preferred_profile == "small"
    messages = [{"role": "system", "content": "instructions"},
                {"role": "assistant", "content": "old " * 5000},
                {"role": "user", "content": "new question"}]
    kept, receipt = task_routing.budget_messages(messages, "quick", 16384, 600)
    assert [m["role"] for m in kept] == ["system", "user"]
    assert receipt["effective_context"] == 8192
    assert receipt["dropped_prior_messages"] == 1
    with pytest.raises(ValueError, match="exceed"):
        task_routing.budget_messages([messages[0], {"role": "user", "content": "x" * 30000}],
                                     "quick", 8192, 600)
    tool_group = [messages[0], {"role": "user", "content": "old"},
                  {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
                  {"role": "tool", "tool_call_id": "c1", "content": "result"},
                  {"role": "user", "content": "current"}]
    kept, _ = task_routing.budget_messages(tool_group, "normal", 16384, 600)
    assert len(kept) == len(tool_group)
    with pytest.raises(ValueError, match="incomplete assistant tool-call group"):
        task_routing.budget_messages(tool_group[:3] + tool_group[-1:], "normal", 16384, 600)
    assert task_routing.exact_benchmark_state({"file": "x", "ctx": 16384}, {},
                                              {"result": "pass"}).startswith("unknown")


def test_exact_catalog_requires_verified_shape_and_returns_record_id(tmp_path):
    profile = {"file": "x.gguf", "sha256": "abc", "quant": "q4", "ctx": 16384,
               "mmproj": None, "mmproj_sha256": "none", "hash_verified": True}
    runtime = {"sha256": "runtime", "kv": "q8_0", "batch": 512, "ubatch": 256,
               "identity_verified": True}
    row = {"id": "synthetic-001", "result": "pass", "model_file": "x.gguf",
           "model_sha256": "abc", "quant": "q4", "runtime_sha256": "runtime",
           "context": 16384, "kv": "q8_0", "batch": 512, "ubatch": 256,
           "vision": False, "vision_projector_sha256": "none", "task_tier": "deep",
           "test_suite": "jarvis-project-v1", "date": date.today().isoformat(),
           "metric": "success_rate", "score": 0.75}
    catalog = tmp_path / "receipts.json"
    catalog.write_text(__import__("json").dumps([row]), encoding="utf-8")
    assert task_routing.benchmark_catalog_match(profile, runtime, catalog, "deep") == ("exact_pass", "synthetic-001")
    assert task_routing.benchmark_catalog_match({**profile, "hash_verified": False}, runtime, catalog)[0].startswith("unknown")
    assert task_routing.benchmark_catalog_match(profile, {**runtime, "batch": 1024}, catalog)[0].startswith("unknown")
    assert task_routing.candidate_receipt_exists(catalog, "x.gguf", "deep")
    assert not task_routing.candidate_receipt_exists(catalog, "x.gguf", "vision")
    selection = task_routing.choose("Debug the project", {"away_model_enabled": True},
                                    {"big": ("exact_fail", "b1"), "small": ("exact_pass", "s1")})
    assert selection.preferred_profile == "small" and selection.benchmark_state == "exact_pass"
    assert task_routing.choose("Debug the project", {"llm_pinned_profile": "big"},
                               {"big": ("exact_fail", "b1"), "small": ("exact_pass", "s1")}).preferred_profile == "big"
    ranked = task_routing.choose("Debug the project", {"away_model_enabled": True}, {
        "big": {"state": "exact_pass", "id": "b1", "score": 0.74,
                "metric": "success_rate", "suite": "jarvis-project-v1"},
        "small": {"state": "exact_pass", "id": "s1", "score": 0.82,
                  "metric": "success_rate", "suite": "jarvis-project-v1"}})
    assert ranked.preferred_profile == "small" and "higher comparable" in ranked.reason
    assert task_routing.exact_benchmark_state(profile, runtime, {**row, "test_suite": "other"}).startswith("unknown")
    model = tmp_path / "model-PQ2_0.gguf"
    runtime_exe = tmp_path / "server.exe"
    model.write_bytes(b"synthetic model")
    runtime_exe.write_bytes(b"synthetic runtime")
    identity, runtime_identity = task_routing.verified_identity(
        {"file": str(model), "ctx": 16384},
        {"llm_server_exe": str(runtime_exe), "llm_batch": 2048, "llm_ubatch": 512})
    assert identity["quant"] == "PQ2_0" and identity["hash_verified"]
    assert runtime_identity["identity_verified"] and runtime_identity["batch"] == 2048

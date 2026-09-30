"""Explicit /learn pending-candidate behavior and safety boundaries."""
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wk import config, review  # noqa: E402
from wk.brain import Engine  # noqa: E402
from wk.store import Store  # noqa: E402


def packet():
    return {
        "title": "Release work",
        "project": "Synthetic",
        "purpose": "I want Jarvis to review work and propose learning for my approval.",
        "source_excerpt": "Keep generated lessons pending until the user approves them.",
        "validation_evidence": ["The candidate store uses one transaction for duplicate checks."],
        "requested_checks": ["Find one stable user preference."],
    }


def place_packet(monkeypatch, tmp_path, payload=None):
    monkeypatch.setattr(review.config, "APP_DIR", tmp_path)
    folder = tmp_path / "review_packets"
    folder.mkdir(exist_ok=True)
    path = folder / "sample.json"
    path.write_text(json.dumps(payload or packet()), encoding="utf-8")
    return path


def ready_engine(tmp_path, capture="suggest"):
    calls = []

    class Models:
        cfg = {"llm_base_url": "http://127.0.0.1:8084/v1"}

        def profile(self, name):
            assert name == "big"
            return {"alias": "bonsai-27b"}

        def profile_load_problem(self, name):
            assert name == "big"
            return None

        def begin_review(self):
            calls.append("begin")
            return True

        def end_review(self):
            calls.append("end")

    engine = SimpleNamespace(
        models=Models(),
        llm=SimpleNamespace(cfg={"llm_base_url": "http://127.0.0.1:8084/v1"}),
        cfg={**config.DEFAULTS, "memory_capture_mode": capture},
        store=Store(tmp_path / "jarvis.db"),
    )
    return engine, calls


def test_learn_creates_only_one_pending_evidence_backed_work_lesson(monkeypatch, tmp_path):
    place_packet(monkeypatch, tmp_path)
    engine, calls = ready_engine(tmp_path)
    seen = []

    def fake_chat_json(llm, messages, schema, **kwargs):
        assert llm.model() == "bonsai-27b"
        assert "Authorization" not in llm._headers()
        assert schema["required"] == ["keep", "lesson", "evidence_quote"]
        assert kwargs["max_tokens"] <= 500
        body = json.loads(messages[1]["content"])
        assert set(body) == {"title", "project", "purpose", "source_excerpt", "validation_evidence"}
        assert "requested_checks" not in messages[1]["content"]
        assert "untrusted reference data" in messages[0]["content"]
        seen.append(body)
        return {"keep": True,
                "lesson": "Keep generated lessons pending until the user approves them.",
                "evidence_quote": "Keep generated lessons pending until the user approves them."}

    monkeypatch.setattr(review, "chat_json", fake_chat_json)
    result = review.learn_packet(engine, "/learn sample.json")

    candidates = engine.store.fact_candidates()
    assert result.startswith("Jarvis 27B proposed one pending work lesson")
    assert [row[1] for row in candidates] == [
        "Jarvis work lesson: Keep generated lessons pending until the user approves them."]
    assert engine.store.facts() == []
    assert calls == ["begin", "end"]
    assert seen


def test_guarded_learn_uses_non_pruning_pending_writer(monkeypatch, tmp_path):
    place_packet(monkeypatch, tmp_path)
    engine, calls = ready_engine(tmp_path)
    monkeypatch.setattr(review, "chat_json", lambda *_a, **_k: {
        "keep": True,
        "lesson": "Keep generated lessons pending until the user approves them.",
        "evidence_quote": "Keep generated lessons pending until the user approves them.",
    })
    engine.store.suggest_fact = lambda *_a, **_k: pytest.fail("guarded learning must not prune candidates")

    result = review.learn_packet(engine, "/learn sample.json", pending_only=True)

    assert result.startswith("Jarvis 27B proposed one pending work lesson")
    assert len(engine.store.fact_candidates()) == 1
    assert engine.store.facts() == []
    assert calls == ["begin", "end"]


def test_guarded_pending_writer_preserves_candidates_and_enforces_capacity():
    store = Store(":memory:")
    store.db.execute(
        "INSERT INTO memory_candidate(ts, fact, reason) VALUES (?,?,?)",
        (0, "Jarvis work lesson: old candidate", "expired"),
    )
    store.db.commit()

    assert store.suggest_fact_pending("Jarvis work lesson: new candidate", "packet") is True
    assert [row[1] for row in store.fact_candidates()] == [
        "Jarvis work lesson: new candidate", "Jarvis work lesson: old candidate"]
    assert store.facts() == []

    for index in range(98):
        store.db.execute(
            "INSERT INTO memory_candidate(ts, fact, reason) VALUES (?,?,?)",
            (float(index + 1), f"existing candidate {index}", "test"),
        )
    store.db.commit()
    count_before = store.rows("SELECT COUNT(*) FROM memory_candidate")[0][0]
    assert count_before == 100
    assert store.suggest_fact_pending("Jarvis work lesson: queue full", "packet") is None
    assert store.rows("SELECT COUNT(*) FROM memory_candidate")[0][0] == count_before
    store.close()


def test_learn_no_lesson_result_does_not_create_candidate(monkeypatch, tmp_path):
    place_packet(monkeypatch, tmp_path)
    engine, calls = ready_engine(tmp_path)
    monkeypatch.setattr(review, "chat_json", lambda *_a, **_k: {
        "keep": False, "lesson": "", "evidence_quote": ""})

    result = review.learn_packet(engine, "/learn sample.json")

    assert "No memory candidate added" in result
    assert engine.store.fact_candidates() == []
    assert calls == ["begin", "end"]


def test_learn_capture_off_does_not_inspect_model_or_read_packet(monkeypatch, tmp_path):
    monkeypatch.setattr(review.config, "APP_DIR", tmp_path)

    class Engine:
        cfg = {"memory_capture_mode": "off"}

        @property
        def models(self):
            raise AssertionError("model should not be inspected")

    assert "suggestions are off" in review.learn_packet(Engine(), "/learn sample.json")


def test_learn_requires_loaded_big_profile_without_swapping(monkeypatch, tmp_path):
    place_packet(monkeypatch, tmp_path)
    engine, calls = ready_engine(tmp_path)
    engine.models.profile_load_problem = lambda _name: "the large model is not already loaded"
    monkeypatch.setattr(review, "chat_json", lambda *_a, **_k: pytest.fail("model call must not run"))

    result = review.learn_packet(engine, "/learn sample.json")

    assert "not already loaded" in result
    assert "No model or memory candidate changed" in result
    assert engine.store.fact_candidates() == []
    assert calls == []


@pytest.mark.parametrize("model_result", [
    {"keep": True, "lesson": "This is an inferred lesson absent from evidence.",
     "evidence_quote": "not in the packet"},
    {"keep": True, "lesson": "Authorization: Bearer pretend-secret-token",
     "evidence_quote": "Keep generated lessons pending until the user approves them."},
    {"keep": "yes", "lesson": "Keep generated lessons pending until the user approves them.",
     "evidence_quote": "Keep generated lessons pending until the user approves them."},
    {"keep": True, "lesson": "Keep generated lessons pending until the user approves them.",
     "evidence_quote": "Keep generated lessons pending until the user approves them.", "extra": "reject"},
])
def test_learn_rejects_invalid_or_unquoted_model_output_and_releases_reservation(
        monkeypatch, tmp_path, model_result):
    place_packet(monkeypatch, tmp_path)
    engine, calls = ready_engine(tmp_path)
    monkeypatch.setattr(review, "chat_json", lambda *_a, **_k: model_result)

    result = review.learn_packet(engine, "/learn sample.json")

    assert "proposal failed" in result
    assert engine.store.fact_candidates() == []
    assert calls == ["begin", "end"]


def test_learn_quote_must_come_from_a_single_evidence_field(monkeypatch, tmp_path):
    split_packet = packet()
    split_packet["source_excerpt"] = "Use one transaction"
    split_packet["validation_evidence"] = ["for duplicate checks and inserts."]
    place_packet(monkeypatch, tmp_path, split_packet)
    engine, calls = ready_engine(tmp_path)
    monkeypatch.setattr(review, "chat_json", lambda *_a, **_k: {
        "keep": True,
        "lesson": "Keep duplicate checks and inserts in one transaction.",
        "evidence_quote": "Use one transaction for duplicate checks and inserts."})

    result = review.learn_packet(engine, "/learn sample.json")

    assert "proposal failed" in result
    assert engine.store.fact_candidates() == []
    assert calls == ["begin", "end"]


def test_learn_rejects_overlapping_model_reservation_and_releases_first_call(monkeypatch, tmp_path):
    place_packet(monkeypatch, tmp_path)
    first, calls = ready_engine(tmp_path)
    second, _ = ready_engine(tmp_path)
    second.models = first.models
    reservation = threading.Lock()
    state = {"busy": False}
    model_started = threading.Event()
    finish_model = threading.Event()
    invocations = []

    def profile_load_problem(_name):
        with reservation:
            return "another read-only review is in progress" if state["busy"] else None

    def begin_review():
        with reservation:
            if state["busy"]:
                return False
            state["busy"] = True
            calls.append("begin")
            return True

    def end_review():
        with reservation:
            state["busy"] = False
            calls.append("end")

    first.models.profile_load_problem = profile_load_problem
    first.models.begin_review = begin_review
    first.models.end_review = end_review

    def fake_chat_json(*_args, **_kwargs):
        invocations.append("model")
        model_started.set()
        assert finish_model.wait(timeout=5)
        return {
            "keep": True,
            "lesson": "Keep generated lessons pending until the user approves them.",
            "evidence_quote": "Keep generated lessons pending until the user approves them.",
        }

    monkeypatch.setattr(review, "chat_json", fake_chat_json)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(review.learn_packet, first, "/learn sample.json")
        assert model_started.wait(timeout=5)
        overlapping = review.learn_packet(second, "/learn sample.json")
        finish_model.set()
        completed = future.result(timeout=10)

    assert "another read-only review is in progress" in overlapping
    assert completed.startswith("Jarvis 27B proposed one pending work lesson")
    assert invocations == ["model"]
    assert calls == ["begin", "end"]
    candidates = first.store.fact_candidates()
    assert len(candidates) == 1
    assert candidates[0][1] == "Jarvis work lesson: Keep generated lessons pending until the user approves them."


def test_learn_command_dispatches_through_shared_engine(monkeypatch, tmp_path):
    from wk import review

    store = Store(tmp_path / "dispatch.db")
    engine = SimpleNamespace(
        store=store,
        cfg={**config.DEFAULTS},
        data_changed=SimpleNamespace(emit=lambda _topic: None),
    )
    monkeypatch.setattr(review, "learn_packet", lambda _engine, command: f"handled {command}")

    assert Engine.chat_reply(engine, "/learn sample.json", False) == "handled /learn sample.json"
    assert store.chat_tail(2) == [
        ("user", "/learn sample.json"),
        ("assistant", "handled /learn sample.json"),
    ]


def test_concurrent_candidate_suggestions_are_deduplicated_across_connections(tmp_path):
    path = tmp_path / "shared.db"
    stores = [Store(path), Store(path)]
    barrier = threading.Barrier(2)

    def suggest(store):
        barrier.wait(timeout=5)
        return store.suggest_fact("Jarvis work lesson: Keep candidate checks and inserts atomic.", "test")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(suggest, stores))
        assert sorted(results) == [False, True]
        assert len(stores[0].fact_candidates()) == 1
    finally:
        for store in stores:
            store.db.close()


def test_concurrent_keep_promotes_candidate_once_across_connections(tmp_path):
    path = tmp_path / "shared.db"
    stores = [Store(path), Store(path)]
    assert stores[0].suggest_fact("Jarvis work lesson: Keep promotion atomic.", "test")
    candidate_id = stores[0].fact_candidates()[0][0]
    barrier = threading.Barrier(2)

    def keep(store):
        barrier.wait(timeout=5)
        return store.resolve_candidate(candidate_id, True)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(keep, stores))
        assert sorted(results) == [False, True]
        assert len(stores[0].facts()) == 1
        assert stores[0].fact_candidates() == []
    finally:
        for store in stores:
            store.db.close()


@pytest.mark.parametrize("command", ["/learn", "/learn sample.json", " /LeArN\tsample.json"])
def test_learn_dispatch_matches_command_token(command):
    assert review.is_learn_command(command)


@pytest.mark.parametrize("command", ["/learning sample.json", "/learner", "please /learn sample.json"])
def test_learn_dispatch_rejects_similar_words(command):
    assert not review.is_learn_command(command)


@pytest.mark.parametrize("command", ["/learn-once sample.json", " /LEARN-ONCE\tsample.json"])
def test_guarded_learn_command_matches_only_its_explicit_token(command):
    assert review.is_guarded_learn_command(command)


@pytest.mark.parametrize("command", ["/learn-once-more sample.json", "please /learn-once sample.json"])
def test_guarded_learn_command_rejects_similar_text(command):
    assert not review.is_guarded_learn_command(command)


def test_guarded_learn_dispatches_through_shared_engine_and_refreshes_pending_memory(monkeypatch, tmp_path):
    store = Store(tmp_path / "guarded-learn-dispatch.db")
    events = []
    engine = SimpleNamespace(
        store=store,
        cfg={**config.DEFAULTS},
        data_changed=SimpleNamespace(emit=events.append),
    )
    monkeypatch.setattr(
        review, "guarded_learn_packet",
        lambda command: f"Jarvis 27B proposed one pending work lesson for {command.split()[-1]}")

    result = Engine.chat_reply(engine, "/learn-once sample.json", False)

    assert result == "Jarvis 27B proposed one pending work lesson for sample.json"
    assert store.chat_tail(2) == [
        ("user", "/learn-once sample.json"),
        ("assistant", result),
    ]
    assert events == ["chat", "memory"]
    store.close()

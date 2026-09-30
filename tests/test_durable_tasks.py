"""Interruption and same-ID continuation checks for the isolated candidate."""
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wk import agent_loop, config, tool_registry
from wk.store import Store


class OneToolModel:
    def __init__(self, name):
        self.name = name

    def chat_response(self, _messages, **_kwargs):
        return {"message": {"content": "", "tool_calls": [
            {"id": "call-one", "type": "function",
             "function": {"name": self.name, "arguments": "{}"}}]},
            "finish_reason": "tool_calls", "usage": {}}


class ReplyModel:
    def chat_response(self, _messages, **_kwargs):
        return {"message": {"content": "Finished after checking the recorded read.", "tool_calls": []},
                "finish_reason": "stop", "usage": {}}


def fake_engine(store, llm):
    return SimpleNamespace(store=store, llm=llm,
                           cfg={**config.DEFAULTS, "pc_actions_enabled": True, "memory_capture_mode": "off"},
                           watching=False, task_windows=[],
                           data_changed=SimpleNamespace(emit=lambda _kind: None))


class DurableTaskTests(unittest.TestCase):
    def test_rejected_private_path_is_not_copied_into_durable_step(self):
        with tempfile.TemporaryDirectory() as directory:
            private = str(Path(directory) / "personal" / "note.txt")
            step = agent_loop._step_summary("call-private", "patch_text", {"path": private}, True)
            self.assertNotIn("path", step)
            self.assertIn("arguments_sha256", step)
            alternate = agent_loop._step_summary(
                "call-alternate", "run_registered_test", {"project_id": private}, True)
            self.assertNotIn("project_id", alternate)

    def test_read_only_cancel_then_same_id_resume_through_chat(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            store = Store(Path(directory) / "tasks.sqlite3")
            stack.callback(store.db.close)
            stop = threading.Event()

            def read_probe():
                stop.set()
                return {"value": "synthetic read"}

            offered = {"read_probe": tool_registry.Tool(
                "read_probe", "Synthetic read", tool_registry._params({}, ()), read_probe)}
            engine = fake_engine(store, OneToolModel("read_probe"))
            with patch.object(tool_registry, "selected", return_value=offered):
                answer, receipts = agent_loop.run(engine, [{"role": "system", "content": "safe"}],
                                                  "Read the synthetic value", cancel=stop)
            self.assertIn("Cancelled", answer)
            self.assertEqual(len(receipts), 1)
            task_id = engine.last_assistant_task_id
            self.assertEqual(store.assistant_task(task_id)["state"], "paused")
            self.assertFalse(store.assistant_task(task_id)["artifacts"][0]["mutation"])
            engine.llm = ReplyModel()
            from wk.brain import Engine
            response = Engine.chat_reply(engine, f"/resume {task_id}", False)
            self.assertIn("Finished after checking", response)
            self.assertIn(f"Assistant task: #{task_id}", response)
            self.assertEqual(store.assistant_task(task_id)["state"], "completed")
            self.assertEqual(len(store.assistant_tasks()), 1)
            self.assertIn(f"#{task_id}: completed", Engine.chat_reply(engine, "/tasks", False))
            store.db.close()

    def test_crash_after_mutation_quarantines_task_and_prevents_replay(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            target = root / "counter.txt"
            target.write_text("0", encoding="utf-8")
            store = Store(root / "tasks.sqlite3")
            stack.callback(store.db.close)

            def mutate_and_crash():
                target.write_text(str(int(target.read_text(encoding="utf-8")) + 1), encoding="utf-8")
                raise SystemExit("synthetic process loss after write")

            offered = {"increment": tool_registry.Tool(
                "increment", "Synthetic mutation", tool_registry._params({}, ()), mutate_and_crash, True)}
            engine = fake_engine(store, OneToolModel("increment"))
            with patch.object(tool_registry, "selected", return_value=offered):
                with self.assertRaises(SystemExit):
                    agent_loop.run(engine, [{"role": "system", "content": "safe"}], "Increment once")
            task_id = engine.last_assistant_task_id
            self.assertEqual(store.assistant_task(task_id)["state"], "inflight")
            self.assertEqual(store.assistant_task(task_id)["last_receipt"]["tool"], "increment")
            store.db.close()

            reopened = Store(root / "tasks.sqlite3")
            stack.callback(reopened.db.close)
            self.assertEqual(reopened.reconcile_interrupted_assistant_tasks(), 1)
            self.assertEqual(reopened.assistant_task(task_id)["state"], "needs_reconcile")
            next_engine = fake_engine(reopened, OneToolModel("increment"))
            from wk.brain import Engine
            denied = Engine.chat_reply(next_engine, f"/resume {task_id}", False)
            self.assertIn("needs inspection", denied)
            self.assertEqual(target.read_text(encoding="utf-8"), "1")
            self.assertEqual(len(reopened.assistant_tasks()), 1)
            reopened.db.close()


if __name__ == "__main__":
    unittest.main()

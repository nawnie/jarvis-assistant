import unittest

from wk import remote_api


class Store:
    def __init__(self):
        self.items = [{"id": index, "title": f"Older {index}", "status": "open",
                       "steps": 0, "last_worked": None} for index in range(1, 31)]

    def projects(self):
        return list(self.items)

    def project_log(self, project_id, limit):
        return [(100, "work", "synthetic result")] if project_id == 31 else []


class Engine:
    def __init__(self):
        self.store = Store()

    def create_project(self, title, goal, source):
        self.store.items.append({"id": 31, "title": title, "status": "open",
                                 "steps": 1, "last_worked": 100})
        return 31


class Gui:
    def call(self, fn):
        return fn()


class OperatorProjectApiTests(unittest.TestCase):
    def test_idempotency_and_evidence_work_beyond_first_25_projects(self):
        engine = Engine()
        task_id = "a" * 32
        body = {"action": "operator_project_add", "operator_id": task_id,
                "title": "Synthetic task", "goal": "Synthetic evidence only"}
        first = remote_api.run_action(engine, Gui(), body)
        second = remote_api.run_action(engine, Gui(), body)
        self.assertEqual(first["id"], 31)
        self.assertEqual(second["id"], 31)
        self.assertEqual(len(engine.store.items), 31)
        evidence = remote_api.operator_project_evidence(engine, [31])
        self.assertEqual(evidence[0]["project_id"], 31)
        self.assertEqual(evidence[0]["log_count_visible"], 1)
        self.assertEqual(len(evidence[0]["log_sha256"]), 64)
        self.assertNotIn("synthetic result", str(evidence))


if __name__ == "__main__":
    unittest.main()

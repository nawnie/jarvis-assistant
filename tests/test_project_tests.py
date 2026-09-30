"""Synthetic non-Jarvis registered test and repair flow."""
import json
import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

from wk import path_policy, project_tests, tool_registry


class RegisteredProjectTests(unittest.TestCase):
    def setUp(self):
        temporary = Path(tempfile.gettempdir()).resolve()
        override = patch.object(path_policy, "_load_roots", return_value=((temporary,), (temporary,)))
        override.start()
        self.addCleanup(override.stop)
        temp_exception = patch.object(path_policy, "SENSITIVE_PARTS", path_policy.SENSITIVE_PARTS - {"appdata"})
        temp_exception.start()
        self.addCleanup(temp_exception.stop)

    def test_non_jarvis_failing_repair_green_without_caller_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "Aurora Lab"
            project.mkdir()
            source = project / "calculator.py"
            source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            test_file = project / "test_sample.py"
            test_file.write_text(
                "import unittest\nfrom calculator import add\n"
                "class Sample(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n",
                encoding="utf-8",
            )
            registry = base / "registered_project_tests.v1.json"
            pinned_hash = hashlib.sha256(test_file.read_bytes()).hexdigest()
            registry.write_text(json.dumps({"version": 1, "projects": [{
                "id": "aurora-lab", "root": str(project), "modules": [{"module": "test_sample", "sha256": pinned_hash}],
                "timeout_seconds": 10, "enabled": True,
            }]}), encoding="utf-8")
            self.assertEqual(project_tests.list_projects(registry)["projects"][0]["id"], "aurora-lab")
            failed = project_tests.run_project("aurora-lab", registry)
            self.assertEqual(failed["state"], "completed")
            self.assertNotEqual(failed["exit_code"], 0)
            before = tool_registry.file_info(str(source))["sha256"]
            patched_source = tool_registry.patch_text(str(source), "return a - b", "return a + b", before)
            self.assertNotEqual(before, patched_source["new_sha256"])
            with patch.object(project_tests, "REGISTRY", registry):
                model_tools = tool_registry.selected("repair and test Aurora Lab")
                wrapped = tool_registry.execute("run_registered_test", {"project_id": "aurora-lab"}, model_tools)
            self.assertTrue(wrapped["ok"], wrapped)
            passed = wrapped["result"]
            self.assertEqual(passed["exit_code"], 0)
            self.assertIn("OK", passed["stderr"])
            self.assertIn("run_registered_test", model_tools)
            self.assertNotIn("run_command", model_tools)
            injected = tool_registry.execute("run_registered_test", {
                "project_id": "aurora-lab", "program": "powershell"}, model_tools)
            self.assertFalse(injected["ok"])
            test_file.write_text(test_file.read_text(encoding="utf-8") + "\n# unreviewed edit\n", encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "changed since owner review"):
                project_tests.run_project("aurora-lab", registry)

    def test_unregistered_and_protected_roots_are_denied(self):
        with self.assertRaisesRegex(PermissionError, "privacy"):
            tool_registry.file_info(str(project_tests.REGISTRY))
        with self.assertRaisesRegex(PermissionError, "privacy"):
            tool_registry.patch_text(str(project_tests.REGISTRY), "a", "b", "0" * 64)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            registry = base / "registered_project_tests.v1.json"
            registry.write_text('{"version":1,"projects":[]}', encoding="utf-8")
            with self.assertRaises(PermissionError):
                project_tests.run_project("not-registered", registry)
            protected = base / "medical"
            protected.mkdir()
            (protected / "test_sample.py").write_text("pass", encoding="utf-8")
            registry.write_text(json.dumps({"version": 1, "projects": [{
                "id": "protected", "root": str(protected), "modules": [{"module": "test_sample",
                    "sha256": hashlib.sha256((protected / "test_sample.py").read_bytes()).hexdigest()}],
                "timeout_seconds": 10, "enabled": True,
            }]}), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "privacy"):
                project_tests.run_project("protected", registry)


if __name__ == "__main__":
    unittest.main()

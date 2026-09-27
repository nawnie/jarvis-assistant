import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wk import assistant_context, config


class OperatorIdentityTests(unittest.TestCase):
    def test_global_instructions_are_versioned_read_back_and_cas_saved(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(config, "DATA_DIR", Path(folder)):
            initial = assistant_context.instructions_record()
            self.assertEqual(initial["source"], "editable_local_file")
            saved = assistant_context.save_instructions("Synthetic Jarvis identity", initial["sha256"])
            self.assertEqual(saved["text"], "Synthetic Jarvis identity")
            self.assertEqual(saved["version"], 1)
            with self.assertRaises(RuntimeError):
                assistant_context.save_instructions("Stale overwrite", initial["sha256"])
            self.assertEqual(assistant_context.load_instructions()[0], "Synthetic Jarvis identity")

    def test_rejects_empty_and_linked_instruction_file(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(config, "DATA_DIR", Path(folder)):
            record = assistant_context.instructions_record()
            with self.assertRaises(ValueError):
                assistant_context.save_instructions("", record["sha256"])
            path = assistant_context.instruction_path()
            path.unlink()
            other = Path(folder) / "other.txt"
            other.write_text("private", encoding="utf-8")
            path.symlink_to(other)
            with self.assertRaises(PermissionError):
                assistant_context.save_instructions("No", record["sha256"])


if __name__ == "__main__":
    unittest.main()

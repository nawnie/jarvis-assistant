import json
import shlex
import tempfile
import unittest
from pathlib import Path

from wk import phone_tools, tool_registry


ROOT = "/storage/emulated/0/Documents"


class FakeAdb:
    def __init__(self):
        self.devices = "List of devices attached\nR5TEST123\tdevice\n"
        self.calls = []
        self.aliases = {}

    def run(self, args, timeout=15):
        self.calls.append(args)
        if args == ["devices"]:
            return self.devices.encode()
        if args[2] == "shell":
            command = shlex.split(args[3])
            if command[0] == "realpath":
                return (self.aliases.get(command[1], command[1]) + "\n").encode()
            if command[0] == "ls":
                return b"sample.txt\nnotes.md\n"
            if command[0] == "find":
                return (ROOT + "/sample.txt\n").encode()
            if command[:2] == ["settings", "put"]:
                return b""
            if command[:2] == ["settings", "get"]:
                return (command[-1] + "\n").encode()  # synthetic echo
            if command[:2] == ["am", "start"]:
                return b"Starting: Intent"
        if args[2] == "exec-out":
            assert shlex.split(args[3])[0] == "head"
            return b"synthetic text"
        raise AssertionError(args)


class PhoneToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.policy = Path(self.temp.name) / "phone_access.v1.json"
        self.policy.write_text(json.dumps({"version": 1, "serial": "R5TEST123", "selected_folders": [ROOT], "enabled": True}), encoding="utf-8")
        self.adb = FakeAdb()
        self.tools = phone_tools.PhoneTools(self.policy, self.adb)

    def tearDown(self):
        self.temp.cleanup()

    def test_single_authorized_device_and_multiple_device_denial(self):
        self.assertEqual(self.tools.list_folder(ROOT)["entries"], ["sample.txt", "notes.md"])
        self.adb.devices += "OTHER\tdevice\n"
        with self.assertRaises(PermissionError):
            self.tools.list_folder(ROOT)
        self.adb.devices = "List of devices attached\nOTHER\tdevice\n"
        with self.assertRaises(PermissionError):
            self.tools.list_folder(ROOT)

    def test_traversal_and_symlink_outside_root_are_denied(self):
        with self.assertRaises(ValueError):
            self.tools.read_text(ROOT + "/../Secrets.txt")
        self.adb.aliases[ROOT + "/shortcut.txt"] = "/storage/emulated/0/Private/secret.txt"
        with self.assertRaises(PermissionError):
            self.tools.read_text(ROOT + "/shortcut.txt")
        self.assertFalse(any(call[2] == "exec-out" for call in self.adb.calls if len(call) > 3))

    def test_unicode_and_spaces_are_quoted_inside_selected_root(self):
        path = ROOT + "/My résumé 2026.txt"
        result = self.tools.read_text(path)
        self.assertEqual(result["text"], "synthetic text")
        command = next(call[3] for call in self.adb.calls if len(call) > 3 and call[2] == "exec-out")
        self.assertEqual(shlex.split(command)[-1], path)

    def test_message_only_previews_exact_values_and_never_sends(self):
        result = self.tools.compose_message("+15551234567", "Exact synthetic body")
        self.assertEqual(result["recipient"], "+15551234567")
        self.assertEqual(result["body"], "Exact synthetic body")
        self.assertEqual(result["state"], "awaiting_explicit_owner_confirmation")
        self.assertFalse(result["sent"])
        self.assertFalse(any("sms" in str(call).lower() or "am start" in str(call).lower() for call in self.adb.calls))

    def test_message_requires_fresh_exact_confirmation_and_is_one_use(self):
        approved = phone_tools.PhoneTools(self.policy, self.adb, confirm=lambda device, recipient, body: True)
        draft = approved.compose_message("+15551234567", "Exact synthetic body")
        with self.assertRaises(PermissionError):
            approved.open_confirmed_draft(draft["draft_id"], draft["device"], "+15550000000", draft["body"])
        with self.assertRaises(PermissionError):
            approved.open_confirmed_draft(draft["draft_id"], draft["device"], draft["recipient"], draft["body"])
        self.assertFalse(any("am start" in str(call) for call in self.adb.calls))
        denied = phone_tools.PhoneTools(self.policy, self.adb, confirm=lambda device, recipient, body: False)
        denied_draft = denied.compose_message("+15551234567", "Synthetic body")
        with self.assertRaises(PermissionError):
            denied.open_confirmed_draft(denied_draft["draft_id"], denied_draft["device"], denied_draft["recipient"], denied_draft["body"])
        good = approved.compose_message("+15551234567", "Synthetic body")
        result = approved.open_confirmed_draft(good["draft_id"], good["device"], good["recipient"], good["body"])
        self.assertEqual(result["state"], "composer_opened_send_unconfirmed")
        self.assertFalse(result["sent"])
        with self.assertRaises(PermissionError):
            approved.open_confirmed_draft(good["draft_id"], good["device"], good["recipient"], good["body"])

    def test_unknown_setting_rejected_before_write(self):
        with self.assertRaises(ValueError):
            self.tools.prepare_setting("developer_options", "1")
        self.assertFalse(any("settings put" in str(call) for call in self.adb.calls))

    def test_setting_requires_exact_one_use_owner_confirmation(self):
        approved = phone_tools.PhoneTools(self.policy, self.adb, setting_confirm=lambda device, name, value: True)
        change = approved.prepare_setting("auto_rotate", "1")
        self.assertFalse(change["applied"])
        self.assertFalse(any("settings put" in str(call) for call in self.adb.calls))
        with self.assertRaises(PermissionError):
            approved.apply_confirmed_setting(change["change_id"], change["device"], "screen_off_timeout", "1")
        with self.assertRaises(PermissionError):
            approved.apply_confirmed_setting(change["change_id"], change["device"], "auto_rotate", "1")
        denied = phone_tools.PhoneTools(self.policy, self.adb, setting_confirm=lambda device, name, value: False)
        denied_change = denied.prepare_setting("auto_rotate", "1")
        with self.assertRaises(PermissionError):
            denied.apply_confirmed_setting(denied_change["change_id"], denied_change["device"], "auto_rotate", "1")
        self.assertFalse(any("settings put" in str(call) for call in self.adb.calls))
        good = approved.prepare_setting("auto_rotate", "1")
        result = approved.apply_confirmed_setting(good["change_id"], good["device"], "auto_rotate", "1")
        self.assertEqual(result["setting"], "auto_rotate")
        self.assertEqual(sum("settings put" in str(call) for call in self.adb.calls), 1)
        with self.assertRaises(PermissionError):
            approved.apply_confirmed_setting(good["change_id"], good["device"], "auto_rotate", "1")

    def test_both_model_profiles_receive_same_host_policy_tools(self):
        small = tool_registry.selected("Read the Android phone files", {"llm_model": "ternary-bonsai-8b"})
        large = tool_registry.selected("Read the Android phone files", {"llm_model": "ternary-bonsai-2-27b"})
        names = {name for name in small if name.startswith("phone_")}
        self.assertEqual(names, {name for name in large if name.startswith("phone_")})
        self.assertIn("phone_compose_message", names)


if __name__ == "__main__":
    unittest.main()

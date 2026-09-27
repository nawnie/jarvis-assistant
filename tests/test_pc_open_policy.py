"""File and URL opening must preserve the owner's file boundary."""

import unittest
from unittest import mock

from wk import pc_tools


class OpenPathPolicyTests(unittest.TestCase):
    def test_file_uri_does_not_bypass_file_policy(self):
        with mock.patch.object(pc_tools.path_policy, "check_path") as check, \
             mock.patch.object(pc_tools.os, "startfile", create=True) as start:
            with self.assertRaises(PermissionError):
                pc_tools.open_path("file:///C:/Users/Example/.codex/secret.txt")
        check.assert_not_called()
        start.assert_not_called()

    def test_local_path_checks_policy_before_opening(self):
        with mock.patch.object(pc_tools.path_policy, "check_path", side_effect=PermissionError("denied")) as check, \
             mock.patch.object(pc_tools.os, "startfile", create=True) as start:
            with self.assertRaises(PermissionError):
                pc_tools.open_path(r"C:\Users\Example\.codex\secret.txt")
        check.assert_called_once()
        start.assert_not_called()

    def test_https_url_can_open(self):
        with mock.patch.object(pc_tools.path_policy, "check_path") as check, \
             mock.patch.object(pc_tools.os, "startfile", create=True) as start:
            result = pc_tools.open_path("https://example.invalid/page")
        self.assertEqual(result, "Opened https://example.invalid/page")
        check.assert_not_called()
        start.assert_called_once_with("https://example.invalid/page")

    def test_catalog_rejects_file_uri_before_windows_open(self):
        from wk import tool_registry
        with mock.patch.object(tool_registry.path_policy, "check_path", side_effect=PermissionError("denied")) as check, \
             mock.patch.object(pc_tools.os, "startfile", create=True) as start:
            with self.assertRaises(PermissionError):
                tool_registry.TOOLS["open"].call(path="file:///C:/Users/Example/.codex/secret.txt")
        check.assert_called_once()
        start.assert_not_called()

    def test_catalog_keeps_local_open_under_mutation_policy(self):
        from wk import tool_registry
        with mock.patch.object(tool_registry.path_policy, "check_path", side_effect=PermissionError("denied")) as check, \
             mock.patch.object(pc_tools.os, "startfile", create=True) as start:
            with self.assertRaises(PermissionError):
                tool_registry.TOOLS["open"].call(path=r"C:\Users\Example\.codex\secret.txt")
        check.assert_called_once_with(r"C:\Users\Example\.codex\secret.txt", mutation=True)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()

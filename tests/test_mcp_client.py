"""Offline MCP result, pagination, and owned-session regression tests."""
import asyncio
import base64
import hashlib
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wk import config, mcp_client, tool_registry


class MCPClientTests(unittest.TestCase):
    def test_media_result_is_saved_with_hash(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "DATA_DIR", Path(directory)):
            data = b"synthetic media bytes"
            result = SimpleNamespace(model_dump=lambda **_kw: {
                "content": [{"type": "image", "mimeType": "image/png",
                             "data": base64.b64encode(data).decode("ascii")}], "isError": False})
            safe = mcp_client._safe_result(result)
            media = safe["content"][0]
            self.assertEqual(media["state"], "saved_local")
            self.assertEqual(media["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(Path(media["path"]).read_bytes(), data)

    def test_mcp_is_error_marks_outer_tool_failed(self):
        cfg = {"mcp_servers": {"mock": {"enabled": True, "transport": "stdio", "command": "mock"}}}
        offered = tool_registry.selected("project", cfg)
        with patch.object(mcp_client, "call", return_value={"result": {"is_error": True, "content": []}}):
            outcome = tool_registry.execute("mcp_call", {"server": "mock", "tool": "fail",
                                                         "arguments": {}}, offered)
        self.assertFalse(outcome["ok"])

    def test_tool_discovery_reads_next_cursor(self):
        class Session:
            async def list_tools(self, cursor=None):
                if cursor is None:
                    return SimpleNamespace(tools=[SimpleNamespace(name="one", description="", inputSchema={})],
                                           nextCursor="page2")
                self.assert_cursor = cursor
                return SimpleNamespace(tools=[SimpleNamespace(name="two", description="", inputSchema={})],
                                       nextCursor=None)
        session = Session()
        tools = asyncio.run(mcp_client._list_all_tools(session))
        self.assertEqual([tool["name"] for tool in tools], ["one", "two"])
        self.assertEqual(session.assert_cursor, "page2")

    def test_reused_connection_retains_state_between_calls(self):
        class Session:
            calls = 0
            async def call_tool(self, name, arguments, **_kw):
                self.calls += 1
                return SimpleNamespace(model_dump=lambda **_args: {
                    "content": [{"type": "text", "text": str(self.calls)}], "isError": False})
        connection = mcp_client._Connection("fake", {"transport": "stdio", "command": "unused"})
        try:
            fake = Session()
            connection.session = fake
            connection.tools = [{"name": "counter", "input_schema": {}}]
            first = connection.request("call", "counter", {})
            second = connection.request("call", "counter", {})
            self.assertEqual(first["result"]["content"][0]["text"], "1")
            self.assertEqual(second["result"]["content"][0]["text"], "2")
        finally:
            connection.close()

    def test_connection_closes_context_in_opening_task(self):
        class OwnedContext:
            async def __aenter__(self):
                self.owner = asyncio.current_task()
                return self

            async def __aexit__(self, *_exc):
                self.assert_same_task()

            def assert_same_task(self):
                if asyncio.current_task() is not self.owner:
                    raise RuntimeError("MCP context closed from another task")

        async def fake_open(connection):
            if connection.session is not None:
                return
            stack = AsyncExitStack()
            await stack.enter_async_context(OwnedContext())
            connection.stack = stack
            connection.session = object()
            connection.tools = []

        with patch.object(mcp_client._Connection, "_open", fake_open):
            connection = mcp_client._Connection("fake", {"transport": "stdio", "command": "unused"})
            try:
                self.assertEqual(connection.request("discover")["server"], "fake")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()

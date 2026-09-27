"""Owned, reusable connections to explicitly configured local MCP servers."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import threading
from concurrent.futures import Future
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from . import config

MAX_RESULT_CHARS = 16_000
MAX_MEDIA_BYTES = 16_000_000
MAX_TOOL_PAGES = 8
DENIED_TOOLS = {"partner_generate", "install", "uninstall", "download", "stop", "start",
                "publish", "deploy", "send_message", "delete", "shawn_core_swarm"}
_last_health: dict[str, dict] = {}
_pool: dict[str, "_Connection"] = {}
_pool_lock = threading.Lock()


def configured(cfg: dict) -> dict:
    servers = cfg.get("mcp_servers") or {}
    return servers if isinstance(servers, dict) else {}


def last_health(name: str) -> dict:
    return dict(_last_health.get(name) or {"state": "not_tested"})


def _definition(cfg: dict, name: str) -> dict:
    if not isinstance(name, str) or name not in configured(cfg):
        raise ValueError(f"MCP server {name!r} is not configured")
    spec = configured(cfg)[name]
    if not isinstance(spec, dict) or spec.get("enabled") is not True:
        raise ValueError(f"MCP server {name!r} is disabled or invalid")
    allowed = spec.get("allowed_tools")
    if not isinstance(allowed, list) or not allowed or any(not isinstance(item, str) or not item for item in allowed):
        raise ValueError(f"MCP server {name!r} requires an explicit nonempty allowed_tools list")
    if set(allowed) & DENIED_TOOLS:
        raise ValueError("paid, lifecycle, publishing or authority-changing MCP tools need a separate host route")
    if spec.get("transport") == "stdio":
        command, args = spec.get("command"), spec.get("args", [])
        if not isinstance(command, str) or not command or not isinstance(args, list) or \
                any(not isinstance(a, str) for a in args):
            raise ValueError("stdio requires command and string args")
        cwd = spec.get("cwd")
        if cwd and (not isinstance(cwd, str) or not Path(cwd).is_dir()):
            raise ValueError("stdio cwd does not exist")
        refs = spec.get("env_refs") or {}
        if not isinstance(refs, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in refs.items()):
            raise ValueError("env_refs must map names to environment variable names")
        if any(v not in os.environ for v in refs.values()):
            raise ValueError("required MCP environment references are missing")
    elif spec.get("transport") == "streamable_http":
        url = spec.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme not in ("http", "https") or parsed.hostname not in \
                ("localhost", "127.0.0.1", "::1"):
            raise ValueError("MCP HTTP URL must be loopback and explicitly configured")
    else:
        raise ValueError("MCP transport must be stdio or streamable_http")
    return spec


def _save_media(block: dict) -> dict:
    kind = block.get("type")
    mime = block.get("mimeType") or "application/octet-stream"
    encoded = block.get("data")
    if not isinstance(encoded, str):
        return {"type": kind, "mimeType": mime, "state": "no_encoded_media"}
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError:
        return {"type": kind, "mimeType": mime, "state": "invalid_encoding"}
    if len(raw) > MAX_MEDIA_BYTES:
        return {"type": kind, "mimeType": mime, "state": "too_large", "bytes": len(raw)}
    digest = hashlib.sha256(raw).hexdigest()
    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
              "audio/wav": ".wav", "audio/mpeg": ".mp3"}.get(mime, ".bin")
    folder = config.DATA_DIR / "mcp-artifacts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (digest + suffix)
    if not path.exists():
        temp = folder / (digest + ".tmp")
        temp.write_bytes(raw)
        os.replace(temp, path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise IOError("saved MCP media hash mismatch")
    return {"type": kind, "mimeType": mime, "state": "saved_local",
            "path": str(path), "sha256": digest, "bytes": len(raw)}


def _safe_result(result) -> dict:
    raw = result.model_dump(mode="json", exclude_none=True)
    blocks = []
    for block in raw.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("image", "audio"):
            blocks.append(_save_media(block))
        else:
            encoded = json.dumps(block, ensure_ascii=False)
            blocks.append(block if len(encoded) <= MAX_RESULT_CHARS else
                          {"type": block.get("type"), "truncated": True,
                           "preview": encoded[:MAX_RESULT_CHARS]})
    structured = raw.get("structuredContent")
    if structured is not None and len(json.dumps(structured, default=str)) > MAX_RESULT_CHARS:
        structured = {"truncated": True, "preview": json.dumps(structured, default=str)[:MAX_RESULT_CHARS]}
    return {"is_error": bool(raw.get("isError")), "content": blocks,
            "structured_content": structured}


async def _list_all_tools(session) -> list[dict]:
    tools: list[dict] = []
    cursor = None
    seen = set()
    for _ in range(MAX_TOOL_PAGES):
        listing = await session.list_tools(cursor=cursor)
        tools.extend({"name": tool.name, "description": (tool.description or "")[:500],
                      "input_schema": tool.inputSchema} for tool in listing.tools)
        cursor = getattr(listing, "nextCursor", None)
        if not cursor:
            return tools
        if cursor in seen:
            raise RuntimeError("MCP tool pagination repeated a cursor")
        seen.add(cursor)
    raise RuntimeError(f"MCP tool list exceeded {MAX_TOOL_PAGES} pages")


class _Connection:
    """A single MCP session owned by one loop thread for stateful stdio servers."""

    def __init__(self, name: str, spec: dict):
        self.name, self.spec = name, spec
        self.loop = asyncio.new_event_loop()
        self.requests = asyncio.Queue()
        self.closing = False
        self.thread = threading.Thread(target=self._serve, daemon=True, name=f"jarvis-mcp-{name}")
        self.thread.start()
        self.stack = None
        self.session = None
        self.identity = name
        self.tools = None
        self.worker = asyncio.run_coroutine_threadsafe(self._work(), self.loop)

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _open(self):
        if self.session is not None:
            return
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError:
            raise RuntimeError("MCP Python SDK is unavailable in Jarvis's environment") from None
        stack = AsyncExitStack()
        try:
            if self.spec["transport"] == "stdio":
                env = {key: os.environ[value] for key, value in (self.spec.get("env_refs") or {}).items()}
                params = StdioServerParameters(command=self.spec["command"],
                                               args=self.spec.get("args") or [], env=env,
                                               cwd=self.spec.get("cwd"))
                streams = await stack.enter_async_context(stdio_client(params))
            else:
                streams = await stack.enter_async_context(streamablehttp_client(self.spec["url"], timeout=20))
            session = await stack.enter_async_context(ClientSession(
                streams[0], streams[1], read_timeout_seconds=timedelta(seconds=30)))
            hello = await session.initialize()
            self.identity = getattr(getattr(hello, "serverInfo", None), "name", self.name)
            all_tools = await _list_all_tools(session)
            allowed = set(self.spec["allowed_tools"])
            self.tools = [tool for tool in all_tools if tool["name"] in allowed]
            self.session, self.stack = session, stack
        except Exception:
            await stack.aclose()
            raise

    async def _discover(self):
        await self._open()
        return {"server": self.name, "identity": self.identity, "tools": self.tools}

    async def _call(self, tool_name: str, args: dict):
        await self._open()
        selected = next((tool for tool in self.tools if tool["name"] == tool_name), None)
        if selected is None:
            raise ValueError(f"MCP tool {tool_name!r} was not listed by {self.name}")
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            raise RuntimeError("jsonschema is required for MCP argument validation") from None
        Draft202012Validator(selected["input_schema"]).validate(args)
        result = await self.session.call_tool(tool_name, args, read_timeout_seconds=timedelta(seconds=60))
        return {"server": self.name, "identity": self.identity, "tool": tool_name,
                "result": _safe_result(result)}

    async def _work(self):
        """Own the MCP context and every request in one asyncio task."""
        closing = None
        try:
            while True:
                operation, tool_name, args, reply = await self.requests.get()
                if operation == "close":
                    closing = reply
                    break
                try:
                    value = await (self._discover() if operation == "discover"
                                   else self._call(tool_name, args))
                    if not reply.done():
                        reply.set_result(value)
                except Exception as exc:
                    if not reply.done():
                        reply.set_exception(exc)
        finally:
            try:
                await self._close()
            except Exception as exc:
                if closing is not None and not closing.done():
                    closing.set_exception(exc)
                raise
            else:
                if closing is not None and not closing.done():
                    closing.set_result(None)

    def request(self, operation: str, tool_name: str = "", args: dict | None = None):
        if self.closing:
            raise RuntimeError("MCP connection is closing")
        reply = Future()
        self.loop.call_soon_threadsafe(self.requests.put_nowait,
                                       (operation, tool_name, args or {}, reply))
        try:
            return reply.result(timeout=45 if operation == "discover" else 75)
        except Exception:
            reply.cancel()
            raise

    async def _close(self):
        if self.stack:
            await self.stack.aclose()
            self.stack = None
            self.session = None

    def close(self):
        if self.closing:
            if not self.thread.is_alive() and not self.loop.is_closed():
                self.loop.close()
            return
        self.closing = True
        reply = Future()
        self.loop.call_soon_threadsafe(self.requests.put_nowait,
                                       ("close", "", {}, reply))
        try:
            reply.result(timeout=10)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=10)
            if not self.thread.is_alive():
                self.loop.close()


def _get(cfg: dict, name: str) -> _Connection:
    spec = _definition(cfg, name)
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest()
    with _pool_lock:
        current = _pool.get(name)
        if current and getattr(current, "fingerprint", None) == fingerprint:
            return current
        if current:
            current.close()
        connection = _Connection(name, spec)
        connection.fingerprint = fingerprint
        _pool[name] = connection
        return connection


def close_all():
    with _pool_lock:
        for connection in _pool.values():
            connection.close()
        _pool.clear()


def discover(cfg: dict, name: str) -> dict:
    try:
        result = _get(cfg, name).request("discover")
        _last_health[name] = {"state": "healthy", "identity": result["identity"]}
        return result
    except Exception as exc:
        _last_health[name] = {"state": "failed", "reason": f"{type(exc).__name__}: {exc}"[:300]}
        raise


def call(cfg: dict, name: str, tool_name: str, arguments: dict) -> dict:
    if not isinstance(tool_name, str) or not tool_name or not isinstance(arguments, dict):
        raise ValueError("MCP tool name and JSON object arguments are required")
    try:
        result = _get(cfg, name).request("call", tool_name, arguments)
        _last_health[name] = {"state": "healthy", "identity": result["identity"]}
        return result
    except Exception as exc:
        _last_health[name] = {"state": "failed", "reason": f"{type(exc).__name__}: {exc}"[:300]}
        raise

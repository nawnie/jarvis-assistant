"""Talks to the local llama.cpp server (llama chat) over its OpenAI-compatible API.

This client uses a loopback endpoint by default. Optional Claude/Codex consultations
are a separate feature and may send the current question through those CLI accounts.
The local API key is read from its configured file and sent only in the request header.
"""
import json
import threading
import urllib.request
from pathlib import Path


class LocalLLM:
    def __init__(self, cfg):
        self.cfg = cfg
        self._model = None
        self.alias_fn = None     # set by the model manager: name of the model loaded right now
        # how many chat() calls are running right now (worker threads); the HUD's arc reactor
        # reads this every frame and spins up while it's above zero
        self.inflight = 0
        self._inflight_lock = threading.Lock()

    def _key(self):
        """First non-comment line of the key file (llama chat's file has '#' notes above the key)."""
        if not self.cfg.get("llm_key_file"):
            return ""
        try:
            for line in Path(self.cfg["llm_key_file"]).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
        except OSError:
            pass  # server started without --api-key-file: no header needed
        return ""

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _clean_error(self, exc):
        """Errors end up in the chat log and journal, so make sure the key can never appear in them."""
        text = f"{exc.__class__.__name__}: {exc}"
        key = self._key()
        return RuntimeError(text.replace(key, "***") if key else text)

    def _get(self, path, timeout=5):
        req = urllib.request.Request(self.cfg["llm_base_url"].rstrip("/") + path, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def online(self):
        try:
            self._get("/models", timeout=3)
            return True
        except Exception:
            return False

    def model(self):
        """Name of the model loaded right now (model manager), the configured one, or the server's first."""
        if self.alias_fn:
            return self.alias_fn()
        if self.cfg.get("llm_model"):
            return self.cfg["llm_model"]
        if not self._model:
            try:
                data = self._get("/models").get("data", [])
            except Exception as exc:
                raise self._clean_error(exc) from None
            self._model = data[0]["id"] if data else "default"
        return self._model

    def chat(self, messages, max_tokens=900, temperature=0.4):
        body = {
            "model": self.model(),
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # reasoning models otherwise put the whole answer in reasoning_content and leave content blank
            "chat_template_kwargs": {"enable_thinking": False},
        }
        with self._inflight_lock:
            self.inflight += 1
        try:
            req = urllib.request.Request(self.cfg["llm_base_url"].rstrip("/") + "/chat/completions",
                                         data=json.dumps(body).encode("utf-8"), headers=self._headers())
            with urllib.request.urlopen(req, timeout=300) as resp:
                msg = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]
        except Exception as exc:
            raise self._clean_error(exc) from None
        finally:
            with self._inflight_lock:
                self.inflight -= 1
        return (msg.get("content") or msg.get("reasoning_content") or "").strip()

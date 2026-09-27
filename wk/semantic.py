"""Smart Recall: search everything Jarvis has seen by MEANING, not just matching words.

"that emulator crash last night" finds "eden.exe crashed in ReShade64.dll" even though they share
no words. Measured 2026-09-26 on this PC: that pair scored 0.46 against 0.18 for the next-closest
line, and a recipe scored 0.01.

How it runs:
  * the embedding model is EmbeddingGemma 300M Q8_0 (334 MB) on llama.cpp's llama-server, CPU only
    (-ngl 0: no GPU memory), on 127.0.0.1:8089 (Shawn Core reservation
    "jarvis-assistant-recall-embeddings"). It starts in ~1.2 s when needed and is stopped after
    IDLE_STOP_S unused. Only a server this module launched AND recorded is ever stopped; if the port
    is held by anything else, smart Recall is skipped (fail closed) and keyword Recall still works.
  * the index lives in data/semantic.db: one 768-number vector per distinct window title, clipboard
    item, journal entry, chat message, saved fact and notable event, added incrementally (the newest
    rows since the last run, at most BATCH_LIMIT per run, ~110 items a second).
  * EmbeddingGemma expects its own prompt prefixes: "task: search result | query: ..." for questions
    and "title: none | text: ..." for the things searched.

Setting (read with cfg.get): recall_semantic (True).
"""
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import psutil

from . import config
from .models import model_control_allowed

MODEL = Path(r"F:\Ai_Models\Language Models\AIWF LLM\GGUF\embeddinggemma-300M\embeddinggemma-300M-Q8_0.gguf")
PORT = 8089
IDLE_STOP_S = 600              # stop the embedding server after 10 min unused
BATCH = 64                     # texts per embedding request
BATCH_LIMIT = 3000             # items indexed per run (the first run over old history takes a few runs)
QUERY = "task: search result | query: "
DOC = "title: none | text: "
EVENT_KINDS = ("file", "crash", "action", "explain", "welcome", "model")


# ===========================================================================
# The embedding server: ours only, recorded, CPU only
# ===========================================================================
class EmbedServer:
    def __init__(self, exe_fn, data_dir=None):
        self.exe_fn = exe_fn                            # -> path to llama-server.exe (Jarvis's setting)
        data = Path(data_dir or config.DATA_DIR)
        self.owner_path = data / "embed-server.owner.json"
        self.log_path = data / "embed-server.log"
        self.lock = threading.Lock()
        self.last_used = 0.0

    def _owned(self):
        try:
            owner = json.loads(self.owner_path.read_text(encoding="utf-8"))
            proc = psutil.Process(int(owner["pid"]))
            cmd = proc.cmdline()
            if (abs(proc.create_time() - float(owner["created"])) > 0.01 or "-m" not in cmd
                    or cmd[cmd.index("-m") + 1] != owner["model"]):
                return None
            return proc
        except (OSError, ValueError, KeyError, IndexError, psutil.Error):
            return None

    def _healthy(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1.5) as resp:
                return json.loads(resp.read().decode("utf-8")).get("status") == "ok"
        except Exception:
            return False

    def _port_in_use(self):
        with socket.socket() as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", PORT)) == 0

    def ensure(self):
        """Start (or adopt) our server. Returns None when ready, else a reason."""
        with self.lock:
            if not model_control_allowed():
                return "model control is disabled for this copy of Jarvis"
            exe = self.exe_fn()
            if not MODEL.exists() or not exe or not Path(exe).exists():
                return "the embedding model or llama-server is missing"
            proc = self._owned()
            if proc is None:
                if self._port_in_use():
                    return f"port {PORT} is being used by another program"
                args = [exe, "-m", str(MODEL), "--embedding", "-ngl", "0", "--host", "127.0.0.1", "--port", str(PORT),
                        "-c", "2048", "-b", "2048", "-ub", "2048", "-t", str(max(2, min(8, (os.cpu_count() or 4) // 2)))]
                with open(self.log_path, "ab") as log:
                    process = subprocess.Popen(args, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                proc = psutil.Process(process.pid)
                self.owner_path.write_text(json.dumps({"pid": process.pid, "created": proc.create_time(),
                                                       "model": str(MODEL)}), encoding="utf-8")
            deadline = time.time() + 60
            while time.time() < deadline:
                if self._healthy():
                    self.last_used = time.time()
                    return None
                if not proc.is_running():
                    self.owner_path.unlink(missing_ok=True)
                    return "the embedding server stopped while loading (see data\\embed-server.log)"
                time.sleep(0.2)
            return "the embedding server took too long to start"

    def embed(self, texts):
        """Unit-length float32 vectors, one per text (batched)."""
        out = []
        for i in range(0, len(texts), BATCH):
            body = json.dumps({"input": texts[i:i + BATCH], "model": "embeddinggemma"}).encode("utf-8")
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/embeddings", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))["data"]
            out.extend(d["embedding"] for d in sorted(data, key=lambda d: d["index"]))
        self.last_used = time.time()
        vecs = np.array(out, dtype=np.float32)
        return vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)

    def stop(self):
        got = self.lock.acquire(timeout=2)
        try:
            proc = self._owned()
            if proc is not None:
                try:
                    proc.kill()
                    proc.wait(5)
                except psutil.Error:
                    pass
            self.owner_path.unlink(missing_ok=True)
        finally:
            if got:
                self.lock.release()

    def stop_if_idle(self):
        if self._owned() is not None and time.time() - self.last_used > IDLE_STOP_S and not self.lock.locked():
            self.stop()
            return True
        return False


# ===========================================================================
# The index: data/semantic.db, filled incrementally from Jarvis's own database
# ===========================================================================
class SemanticIndex:
    def __init__(self, store, embed_fn, path=None):
        """embed_fn(texts) -> unit vectors. store is Jarvis's Store (read with store.rows)."""
        self.store, self.embed_fn = store, embed_fn
        self.db = sqlite3.connect(str(path or config.DATA_DIR / "semantic.db"), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(
                "CREATE TABLE IF NOT EXISTS vec (key TEXT PRIMARY KEY, source TEXT, ts REAL, where_ TEXT, "
                "text TEXT, extra REAL, v BLOB);"
                "CREATE TABLE IF NOT EXISTS mark (source TEXT PRIMARY KEY, last_id INTEGER);")
        self._cache = None                              # (keys-rows, matrix) for fast search

    def _mark(self, source):
        row = self.db.execute("SELECT last_id FROM mark WHERE source=?", (source,)).fetchone()
        return row[0] if row else 0

    def _pending(self):
        """New rows since the last run, as (key, source, ts, where, text, extra, source_mark, id)."""
        items = []
        s = self.store
        # this is the window-title section: one entry per distinct (program, title), newest time + total time
        last = self._mark("window")
        for mx, process, title, ts, total in s.rows(
                "SELECT MAX(id), process, title, MAX(ts_end), SUM(ts_end-ts_start) FROM activity WHERE id>? AND title<>'' "
                "GROUP BY process, title ORDER BY MAX(id) LIMIT ?", (last, BATCH_LIMIT)):
            items.append((f"window:{process}|{title}", "window", ts, process, title, total, "window", mx))
        for source, sql in (
                ("clipboard", "SELECT id, ts, process, text FROM clipboard WHERE id>? ORDER BY id LIMIT ?"),
                ("journal", "SELECT id, ts, 'journal', text FROM journal WHERE id>? ORDER BY id LIMIT ?"),
                ("chat", "SELECT id, ts, role, text FROM chat WHERE id>? ORDER BY id LIMIT ?"),
                ("memory", "SELECT id, ts, 'fact', fact FROM memory WHERE id>? ORDER BY id LIMIT ?")):
            for rid, ts, where, text in s.rows(sql, (self._mark(source), BATCH_LIMIT)):
                if text and text.strip():
                    items.append((f"{source}:{rid}", source, ts, where, text[:1500], 0.0, source, rid))
        marks = ",".join("?" * len(EVENT_KINDS))
        for rid, ts, kind, text in s.rows(f"SELECT id, ts, kind, text FROM events WHERE id>? AND kind IN ({marks}) "
                                          f"ORDER BY id LIMIT ?", (self._mark("event"), *EVENT_KINDS, BATCH_LIMIT)):
            items.append((f"event:{rid}", "event", ts, kind, text[:600], 0.0, "event", rid))
        return items[:BATCH_LIMIT]

    def update(self):
        """Embed and store what's new. Returns how many items were added."""
        with self.lock:
            items = self._pending()
            # a window title seen before only needs its time updated, not a new vector
            known = {k for (k,) in self.db.execute(
                f"SELECT key FROM vec WHERE key IN ({','.join('?' * len(items))})", [it[0] for it in items])} if items else set()
            for key, source, ts, where, text, extra, mark_source, rid in [it for it in items if it[0] in known]:
                self.db.execute("UPDATE vec SET ts=MAX(ts, ?), extra=extra+? WHERE key=?", (ts, extra, key))
                self.db.execute("INSERT INTO mark (source, last_id) VALUES (?, ?) ON CONFLICT(source) "
                                "DO UPDATE SET last_id=MAX(mark.last_id, excluded.last_id)", (mark_source, rid))
            self.db.commit()
            items = [it for it in items if it[0] not in known]
        if not items:
            return 0
        vecs = self.embed_fn([DOC + (f"{it[3]}: " if it[1] == "window" else "") + it[4] for it in items])
        with self.lock:
            for it, v in zip(items, vecs):
                key, source, ts, where, text, extra, mark_source, rid = it
                self.db.execute("INSERT INTO vec (key, source, ts, where_, text, extra, v) VALUES (?,?,?,?,?,?,?) "
                                "ON CONFLICT(key) DO UPDATE SET ts=excluded.ts, extra=vec.extra+excluded.extra",
                                (key, source, ts, where, text, extra, np.asarray(v, dtype=np.float32).tobytes()))
                self.db.execute("INSERT INTO mark (source, last_id) VALUES (?, ?) ON CONFLICT(source) "
                                "DO UPDATE SET last_id=MAX(mark.last_id, excluded.last_id)", (mark_source, rid))
            self.db.commit()
            self._cache = None
        return len(items)

    def search(self, query, limit=20, min_score=0.30):
        """[(ts, source, where, text, extra, score)], best meaning matches first (Recall's row shape + score)."""
        with self.lock:
            if self._cache is None:
                rows = self.db.execute("SELECT ts, source, where_, text, extra, v FROM vec").fetchall()
                matrix = np.frombuffer(b"".join(r[5] for r in rows), dtype=np.float32).reshape(len(rows), -1) \
                    if rows else np.zeros((0, 768), dtype=np.float32)
                self._cache = ([r[:5] for r in rows], matrix)
            rows, matrix = self._cache
        if not rows:
            return []
        q = self.embed_fn([QUERY + query])[0]
        scores = matrix @ q
        best = np.argsort(-scores)[:limit]
        return [(*rows[i], float(scores[i])) for i in best if scores[i] >= min_score]

    def size(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM vec").fetchone()[0]


class SmartRecall:
    """What the UI uses: the server + the index together, all calls blocking (run them off the GUI)."""

    def __init__(self, store, exe_fn, data_dir=None):
        self.server = EmbedServer(exe_fn, data_dir)
        self.index = SemanticIndex(store, self._embed, Path(data_dir or config.DATA_DIR) / "semantic.db")
        self.problem = None

    def _embed(self, texts):
        problem = self.server.ensure()
        if problem:
            self.problem = problem
            raise RuntimeError(problem)
        return self.server.embed(texts)

    def refresh(self):
        """Index what's new. Returns the number added (0 if nothing new or the server can't run)."""
        try:
            return self.index.update()
        except Exception as exc:
            self.problem = str(exc)
            return 0

    def search(self, query, limit=20):
        try:
            self.refresh()
            return self.index.search(query, limit)
        except Exception as exc:
            self.problem = str(exc)
            return []

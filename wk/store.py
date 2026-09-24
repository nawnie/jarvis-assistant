"""Jarvis Assistant's memory: one local SQLite file holding everything it observed.

Tables in plain terms:
  activity  - which app/window had focus, from when to when
  clipboard - text you copied
  events    - things worth showing on the "Now" page (new files, nudges, alerts)
  reminders - things you asked to be reminded about
  journal   - the model's written summaries of a period of time
  memory    - long-term facts you told it to remember
  chat      - the chat transcript
"""
import re
import sqlite3
import threading
import time

# words ignored when turning a question into search keywords
STOP_WORDS = set("""a an and are at be did do does for from had has have how i in is it last me my of on or
that the this to was were what when where which who why with you your did about ago yesterday today
open opened show find look looked""".split())
TITLE_AND_APP = "title || ' ' || process"   # search a window's title and its program name together

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY, ts_start REAL, ts_end REAL, process TEXT, title TEXT);
CREATE INDEX IF NOT EXISTS activity_time ON activity(ts_start);
CREATE TABLE IF NOT EXISTS clipboard (id INTEGER PRIMARY KEY, ts REAL, process TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, created REAL, due REAL, text TEXT, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY, ts REAL, period_start REAL, period_end REAL, text TEXT);
CREATE TABLE IF NOT EXISTS memory (id INTEGER PRIMARY KEY, ts REAL, fact TEXT);
CREATE TABLE IF NOT EXISTS memory_candidate (id INTEGER PRIMARY KEY, ts REAL, fact TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY, ts REAL, role TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY, created REAL, title TEXT, goal TEXT, source_dir TEXT,
    workspace TEXT, status TEXT DEFAULT 'active', steps INTEGER DEFAULT 0, last_worked REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS project_log (id INTEGER PRIMARY KEY, project_id INTEGER, ts REAL, kind TEXT, text TEXT);
"""


class Store:
    def __init__(self, path):
        # one connection shared by the GUI thread and worker threads, guarded by a lock
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        try:  # added later: the model's explanation for a copied error, kept with the clip
            self.db.execute("ALTER TABLE clipboard ADD COLUMN help TEXT")
            self.db.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        self.lock = threading.Lock()
        self._last = None  # (row id, process, title, ts_end) of the activity row being extended

    # --- low-level helpers -------------------------------------------------
    def rows(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def run(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    # --- activity: focused window timeline ------------------------------------
    def record_activity(self, process, title, now, poll_seconds):
        """Extend the current row if the same window is still focused, else start a new one."""
        gap_ok = self._last and now - self._last[3] <= poll_seconds * 2.5
        if gap_ok and self._last[1] == process and self._last[2] == title:
            self.run("UPDATE activity SET ts_end=? WHERE id=?", (now, self._last[0]))
            self._last = (self._last[0], process, title, now)
            return
        # this is the window-switch section: close the previous row at "now" so no time is lost
        if gap_ok:
            self.run("UPDATE activity SET ts_end=? WHERE id=?", (now, self._last[0]))
        row_id = self.run("INSERT INTO activity(ts_start, ts_end, process, title) VALUES (?,?,?,?)",
                          (now, now, process, title))
        self._last = (row_id, process, title, now)

    def break_activity(self):
        """Called when you go idle: the next sample starts a fresh row."""
        self._last = None

    def activity_rows(self, start, end, limit=2000):
        return self.rows("SELECT ts_start, ts_end, process, title FROM activity "
                         "WHERE ts_end>=? AND ts_start<? ORDER BY ts_start DESC LIMIT ?", (start, end, limit))

    def app_totals(self, start, end):
        return self.rows("SELECT process, SUM(ts_end-ts_start) AS s FROM activity "
                         "WHERE ts_end>=? AND ts_start<? GROUP BY process ORDER BY s DESC", (start, end))

    def app_detail(self, process, start, end):
        """Everything the app card shows: total time, how many separate stretches, first/last use, top windows."""
        rows = self.rows("SELECT ts_start, ts_end, process FROM activity WHERE ts_end>=? AND ts_start<? "
                         "ORDER BY ts_start", (start, end))
        total, stretches, first, last, prev_proc, prev_end = 0.0, 0, None, None, None, 0.0
        for a, b, proc in rows:
            if proc == process:
                total += b - a
                first = a if first is None else first
                last = b
                # a new stretch starts when we switch in from another app or come back after a gap
                if prev_proc != process or a - prev_end > 60:
                    stretches += 1
            prev_proc, prev_end = proc, b
        windows = self.rows("SELECT title, SUM(ts_end-ts_start) s FROM activity WHERE process=? AND ts_end>=? "
                            "AND ts_start<? GROUP BY title ORDER BY s DESC LIMIT 25", (process, start, end))
        return {"total": total, "sessions": stretches, "first": first, "last": last,
                "windows": [(t, s) for t, s in windows if s >= 5]}

    def activity_digest_text(self, start, end):
        """Compact plain-text picture of a time range, used as model context."""
        lines = ["Time per app:"]
        for proc, secs in self.app_totals(start, end)[:15]:
            if secs >= 30:
                lines.append(f"- {proc}: {secs / 60:.0f} min")
        lines.append("Main windows (by time):")
        for proc, title, secs in self.rows(
                "SELECT process, title, SUM(ts_end-ts_start) s FROM activity WHERE ts_end>=? AND ts_start<? "
                "AND title<>'' GROUP BY process, title ORDER BY s DESC LIMIT 40", (start, end)):
            if secs >= 20:
                lines.append(f"- [{proc}] {title[:120]} ({secs / 60:.0f} min)")
        clips = self.rows("SELECT COUNT(*) FROM clipboard WHERE ts>=? AND ts<?", (start, end))[0][0]
        lines.append(f"Clipboard copies: {clips}")
        for (text,) in self.rows("SELECT text FROM events WHERE ts>=? AND ts<? AND kind='file' LIMIT 20",
                                 (start, end)):
            lines.append(f"- {text}")
        return "\n".join(lines)

    # --- clipboard -------------------------------------------------------------
    def add_clip(self, process, text):
        return self.run("INSERT INTO clipboard(ts, process, text) VALUES (?,?,?)", (time.time(), process, text))

    def clips(self, limit=300):
        return self.rows("SELECT id, ts, process, text FROM clipboard ORDER BY id DESC LIMIT ?", (limit,))

    def set_clip_help(self, clip_id, text):
        self.run("UPDATE clipboard SET help=? WHERE id=?", (text, clip_id))

    def clip_help(self, clip_id):
        r = self.rows("SELECT help FROM clipboard WHERE id=?", (clip_id,))
        return r[0][0] if r else None

    def delete_clip(self, clip_id):
        self.run("DELETE FROM clipboard WHERE id=?", (clip_id,))

    # --- events feed ---------------------------------------------------------
    def add_event(self, kind, text):
        self.run("INSERT INTO events(ts, kind, text) VALUES (?,?,?)", (time.time(), kind, text))

    def events(self, limit=100):
        return self.rows("SELECT ts, kind, text FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # --- reminders -----------------------------------------------------------
    def add_reminder(self, due, text):
        self.run("INSERT INTO reminders(created, due, text) VALUES (?,?,?)", (time.time(), due, text))

    def due_reminders(self, now):
        return self.rows("SELECT id, text FROM reminders WHERE done=0 AND due<=?", (now,))

    def reminders(self):
        return self.rows("SELECT id, due, text, done FROM reminders ORDER BY done, due LIMIT 200")

    def finish_reminder(self, rid):
        self.run("UPDATE reminders SET done=1 WHERE id=?", (rid,))

    def delete_reminder(self, rid):
        self.run("DELETE FROM reminders WHERE id=?", (rid,))

    # --- journal ---------------------------------------------------------------
    def add_journal(self, start, end, text):
        self.run("INSERT INTO journal(ts, period_start, period_end, text) VALUES (?,?,?,?)",
                 (time.time(), start, end, text))

    def journals(self, limit=200):
        return self.rows("SELECT id, ts, period_start, period_end, text FROM journal ORDER BY id DESC LIMIT ?",
                         (limit,))

    def last_journal_end(self):
        r = self.rows("SELECT MAX(period_end) FROM journal")
        return r[0][0] or 0

    # --- long-term memory facts ----------------------------------------------
    def add_fact(self, fact):
        self.run("INSERT INTO memory(ts, fact) VALUES (?,?)", (time.time(), fact))

    def facts(self):
        return self.rows("SELECT id, fact FROM memory ORDER BY id")

    def delete_fact(self, fid):
        self.run("DELETE FROM memory WHERE id=?", (fid,))

    def suggest_fact(self, fact, reason):
        fact = " ".join(fact.split())[:400]
        self.run("DELETE FROM memory_candidate WHERE ts<?", (time.time() - 30 * 86400,))
        if not fact or self.rows("SELECT 1 FROM memory WHERE fact=? UNION SELECT 1 FROM memory_candidate WHERE fact=?",
                                 (fact, fact)):
            return False
        self.run("INSERT INTO memory_candidate(ts, fact, reason) VALUES (?,?,?)",
                 (time.time(), fact, reason[:150]))
        self.run("DELETE FROM memory_candidate WHERE id NOT IN "
                 "(SELECT id FROM memory_candidate ORDER BY id DESC LIMIT 100)")
        return True

    def fact_candidates(self):
        return self.rows("SELECT id, fact, reason FROM memory_candidate ORDER BY id DESC LIMIT 30")

    def resolve_candidate(self, candidate_id, keep):
        rows = self.rows("SELECT fact FROM memory_candidate WHERE id=?", (candidate_id,))
        if not rows:
            return False
        if keep:
            self.add_fact(rows[0][0])
        self.run("DELETE FROM memory_candidate WHERE id=?", (candidate_id,))
        return True

    # --- chat transcript -------------------------------------------------------
    def add_chat(self, role, text):
        self.run("INSERT INTO chat(ts, role, text) VALUES (?,?,?)", (time.time(), role, text))

    def chat_tail(self, n=30):
        return list(reversed(self.rows("SELECT role, text FROM chat ORDER BY id DESC LIMIT ?", (n,))))

    def clear_chat(self):
        self.run("DELETE FROM chat")

    # --- recall: keyword search over everything Jarvis has kept ---------------------------
    @staticmethod
    def keywords(query):
        words = [w for w in re.findall(r"[\w.+#-]{2,}", query.lower()) if w not in STOP_WORDS]
        return words or [query.strip().lower()]

    def search(self, query, limit=150):
        """Rows of (ts, source, where, text, extra), best keyword matches first, then newest."""
        words = [w for w in self.keywords(query) if w]
        if not words:
            return []
        likes = [f"%{w}%" for w in words]

        def where(col):
            return "(" + " OR ".join(f"{col} LIKE ?" for _ in words) + ")"

        found = []
        # windows: one row per distinct window title, with total time and when it was last seen
        for ts, proc, title, secs in self.rows(
                f"SELECT MAX(ts_end), process, title, SUM(ts_end-ts_start) FROM activity "
                f"WHERE {where(TITLE_AND_APP)} GROUP BY process, title "
                f"ORDER BY MAX(ts_end) DESC LIMIT 400", likes):
            found.append((ts, "window", proc, title, secs))
        for ts, proc, text in self.rows(f"SELECT ts, process, text FROM clipboard WHERE {where('text')} "
                                        f"ORDER BY id DESC LIMIT 200", likes):
            found.append((ts, "clipboard", proc, text, None))
        for ts, text in self.rows(f"SELECT ts, text FROM journal WHERE {where('text')} ORDER BY id DESC LIMIT 100", likes):
            found.append((ts, "journal", "", text, None))
        for ts, kind, text in self.rows(f"SELECT ts, kind, text FROM events WHERE {where('text')} "
                                        f"ORDER BY id DESC LIMIT 200", likes):
            found.append((ts, kind, "", text, None))
        for ts, role, text in self.rows(f"SELECT ts, role, text FROM chat WHERE {where('text')} ORDER BY id DESC LIMIT 100", likes):
            found.append((ts, "chat", role, text, None))

        def score(row):
            hay = f"{row[2]} {row[3]}".lower()
            return sum(1 for w in words if w in hay)
        found.sort(key=lambda r: (score(r), r[0] or 0), reverse=True)
        return found[:limit]

    # --- projects Jarvis works on while you're away ------------------------------------
    # status: active (Jarvis may work on it) | paused | needs_input (Jarvis asked you something) | done
    PROJECT_COLUMNS = "id, created, title, goal, source_dir, workspace, status, steps, last_worked"

    def add_project(self, title, goal, source_dir, workspace):
        return self.run("INSERT INTO projects(created, title, goal, source_dir, workspace) VALUES (?,?,?,?,?)",
                        (time.time(), title, goal, source_dir, workspace))

    def projects(self, include_done=True):
        where = "" if include_done else "WHERE status <> 'done'"
        rows = self.rows(f"SELECT {self.PROJECT_COLUMNS} FROM projects {where} ORDER BY "
                         "CASE status WHEN 'needs_input' THEN 0 WHEN 'active' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END, id DESC")
        return [dict(zip(self.PROJECT_COLUMNS.split(", "), r)) for r in rows]

    def project(self, project_id):
        rows = self.rows(f"SELECT {self.PROJECT_COLUMNS} FROM projects WHERE id=?", (project_id,))
        return dict(zip(self.PROJECT_COLUMNS.split(", "), rows[0])) if rows else None

    def set_project_status(self, project_id, status):
        self.run("UPDATE projects SET status=? WHERE id=?", (status, project_id))

    def project_worked(self, project_id):
        self.run("UPDATE projects SET steps = steps + 1, last_worked=? WHERE id=?", (time.time(), project_id))

    def next_project(self):
        """The active project Jarvis hasn't touched for the longest."""
        rows = self.rows("SELECT id FROM projects WHERE status='active' ORDER BY last_worked ASC, id ASC LIMIT 1")
        return rows[0][0] if rows else None

    def log_project(self, project_id, kind, text):
        self.run("INSERT INTO project_log(project_id, ts, kind, text) VALUES (?,?,?,?)",
                 (project_id, time.time(), kind, text))

    def project_log(self, project_id, limit=60):
        return list(reversed(self.rows("SELECT ts, kind, text FROM project_log WHERE project_id=? "
                                       "ORDER BY id DESC LIMIT ?", (project_id, limit))))

    # --- housekeeping: forget raw observations older than the retention window --------
    def prune(self, days):
        cutoff = time.time() - days * 86400
        for table, col in (("activity", "ts_end"), ("clipboard", "ts"), ("events", "ts")):
            self.run(f"DELETE FROM {table} WHERE {col}<?", (cutoff,))

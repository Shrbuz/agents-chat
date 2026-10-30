"""Transactional queue and result outbox. Never replay unknown side effects."""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, sender TEXT NOT NULL, target TEXT NOT NULL,
                    idem TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL, started REAL, ended REAL,
                    result TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(sender, idem));
                CREATE INDEX IF NOT EXISTS ready ON tasks(target, state, created);
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT NOT NULL,
                    task_id TEXT NOT NULL UNIQUE, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS inbox ON messages(recipient, seq);
                CREATE TABLE IF NOT EXISTS cursors (
                    recipient TEXT NOT NULL, consumer TEXT NOT NULL, seq INTEGER NOT NULL,
                    PRIMARY KEY(recipient, consumer));
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    state TEXT NOT NULL, at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, role TEXT NOT NULL,
                    adapter TEXT, transport TEXT NOT NULL, status TEXT NOT NULL,
                    description TEXT, metadata TEXT,
                    created REAL NOT NULL, last_seen REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS session_role ON sessions(project, role);
                CREATE INDEX IF NOT EXISTS session_seen ON sessions(last_seen);
            """)

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _enqueue(self, db, sender, target, key, payload):
        fingerprint = hashlib.sha256(compact([target, payload]).encode()).hexdigest()
        row = db.execute("SELECT * FROM tasks WHERE sender=? AND idem=?", (sender, key)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise ValueError("idempotency_conflict: key already used with different content")
            return row["id"], True
        task_id = str(uuid.uuid4())
        db.execute("INSERT INTO tasks(id,sender,target,idem,fingerprint,payload,state,created) VALUES(?,?,?,?,?,?,?,?)",
                   (task_id, sender, target, key, fingerprint, compact(payload), "queued", time.time()))
        db.execute("INSERT INTO events(task_id,state,at) VALUES(?,?,?)", (task_id, "queued", time.time()))
        return task_id, False

    def enqueue(self, sender, target, key, payload):
        with self.db(True) as db:
            return self._enqueue(db, sender, target, key, payload)

    @staticmethod
    def decode(row):
        if row is None:
            raise KeyError("task_not_found")
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["result"] = json.loads(item["result"]) if item["result"] else None
        return item

    def get(self, task_id):
        with self.db() as db:
            return self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def claim(self, target):
        with self.db(True) as db:
            row = db.execute("SELECT * FROM tasks WHERE target=? AND state='queued' ORDER BY created,id LIMIT 1", (target,)).fetchone()
            if not row:
                return None
            now = time.time()
            db.execute("UPDATE tasks SET state='running',started=? WHERE id=?", (now, row["id"]))
            db.execute("INSERT INTO events(task_id,state,at) VALUES(?,?,?)", (row["id"], "running", now))
            return self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone())

    def has_pending(self, target):
        with self.db() as db:
            return db.execute("SELECT 1 FROM tasks WHERE target=? AND state IN ('queued','running') LIMIT 1", (target,)).fetchone() is not None

    def finish(self, task_id, state, result):
        if state not in TERMINAL:
            raise ValueError("invalid_terminal_state")
        with self.db(True) as db:
            task = self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            if task["state"] in TERMINAL:
                return False
            if task["cancel_requested"]:
                state = "cancelled"
            callback_id = None
            if task["payload"].get("reply_mode") == "run" and state != "cancelled":
                # One bounded follow-up only. Its own reply is mailbox-only.
                payload = {"prompt": f"Result of task {task_id} ({state}):\n{result.get('summary', '')}\n"
                           "Review this result and report briefly. Do not resend the original task.",
                           "reply_mode": "mailbox", "timeout": task["payload"]["timeout"],
                           "parent_id": task_id, "kind": "result", "context": []}
                callback_id, _ = self._enqueue(db, task["target"], task["sender"], "result:" + task_id, payload)
            body = {"task_id": task_id, "from": task["target"], "state": state,
                    "summary": result.get("summary", "")[:800], "error": result.get("error"),
                    "result_path": result.get("result_path"), "callback_task_id": callback_id}
            result = {**result, "callback_task_id": callback_id}
            db.execute("UPDATE tasks SET state=?,ended=?,result=? WHERE id=?",
                       (state, time.time(), compact(result), task_id))
            if task["payload"].get("notify", True):
                db.execute("INSERT INTO messages(recipient,task_id,body) VALUES(?,?,?)",
                           (task["sender"], task_id, compact(body)))
            db.execute("INSERT INTO events(task_id,state,at) VALUES(?,?,?)", (task_id, state, time.time()))
            return True

    def recover(self):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM tasks WHERE state='running'")]
        for task_id in ids:
            self.finish(task_id, "interrupted", {"error": "broker_restarted",
                        "summary": "Execution outcome unknown after broker restart. Inspect artifacts before submitting a new task."})
        return len(ids)

    def cancel(self, task_id):
        with self.db(True) as db:
            task = self.decode(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            if task["state"] in TERMINAL:
                return
            db.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (task_id,))
        if task["state"] == "queued":
            self.finish(task_id, "cancelled", {"summary": "Cancelled before execution."})

    def cancelled(self, task_id):
        task = self.get(task_id)
        return bool(task["cancel_requested"]) or task["state"] == "cancelled"

    def inbox(self, recipient, consumer, after=None, limit=10):
        with self.db() as db:
            if after is None:
                row = db.execute("SELECT seq FROM cursors WHERE recipient=? AND consumer=?", (recipient, consumer)).fetchone()
                after = row[0] if row else 0
            rows = db.execute("SELECT seq,body FROM messages WHERE recipient=? AND seq>? ORDER BY seq LIMIT ?",
                              (recipient, after, limit)).fetchall()
        return {"messages": [{"seq": r[0], **json.loads(r[1])} for r in rows],
                "next_cursor": rows[-1][0] if rows else after}

    def ack(self, recipient, consumer, seq):
        with self.db(True) as db:
            if seq != 0 and not db.execute("SELECT 1 FROM messages WHERE recipient=? AND seq=?", (recipient, seq)).fetchone():
                raise ValueError("cursor_not_in_recipient_inbox")
            db.execute("INSERT INTO cursors VALUES(?,?,?) ON CONFLICT(recipient,consumer) DO UPDATE SET seq=MAX(seq,excluded.seq)",
                       (recipient, consumer, seq))

    def register_session(self, session_id, project, role, adapter, transport, description="", metadata=None):
        now = time.time()
        metadata = compact(metadata or {})
        with self.db(True) as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO sessions(id,project,role,adapter,transport,status,description,metadata,created,last_seen)"
                           " VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (session_id, project, role, adapter, transport, "online", description, metadata, now, now))
            elif row["project"] != project or row["role"] != role:
                raise ValueError("session_id_already_bound_to_another_role")
            else:
                db.execute("UPDATE sessions SET adapter=?,transport=?,status='online',description=?,metadata=?,last_seen=?"
                           " WHERE id=?", (adapter, transport, description, metadata, now, session_id))
            return self._session_row(db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())

    def heartbeat_session(self, session_id):
        now = time.time()
        with self.db(True) as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise ValueError("unknown_session")
            db.execute("UPDATE sessions SET status='online',last_seen=? WHERE id=?", (now, session_id))
            return self._session_row(db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())

    def remove_session(self, session_id):
        with self.db(True) as db:
            row = db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            return {"removed": row.rowcount}

    def list_sessions(self, project=None, role=None, include_offline=True):
        query = "SELECT * FROM sessions"
        clauses, values = [], []
        if project is not None:
            clauses.append("project=?")
            values.append(project)
        if role is not None:
            clauses.append("role=?")
            values.append(role)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY last_seen DESC"
        with self.db() as db:
            rows = [self._session_row(row) for row in db.execute(query, values).fetchall()]
        if not include_offline:
            rows = [row for row in rows if row["online"]]
        return rows

    @staticmethod
    def _session_row(row):
        if row is None:
            raise KeyError("session_not_found")
        item = dict(row)
        try:
            item["metadata"] = json.loads(item["metadata"]) if item["metadata"] else {}
        except ValueError:
            item["metadata"] = {}
        item["online"] = item["status"] == "online" and time.time() - item["last_seen"] < 90
        return item

    def purge_stale_sessions(self, days=30):
        cutoff = time.time() - days * 86400
        with self.db(True) as db:
            return db.execute("DELETE FROM sessions WHERE last_seen<?", (cutoff,)).rowcount

    def stats(self):
        with self.db() as db:
            counts = dict(db.execute("SELECT state,count(*) FROM tasks GROUP BY state").fetchall())
            counts["sessions"] = db.execute("SELECT count(*) FROM sessions").fetchone()[0]
            return counts

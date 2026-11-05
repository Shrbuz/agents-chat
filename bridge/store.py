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
                CREATE INDEX IF NOT EXISTS session_seen ON sessions(last_seen);                CREATE TABLE IF NOT EXISTS bus_state (
                    project TEXT PRIMARY KEY, cursor TEXT NOT NULL DEFAULT '',
                    synced REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS bus_tasks (
                    project TEXT NOT NULL, task_id TEXT NOT NULL, requester TEXT,
                    assignee TEXT, status TEXT, priority TEXT, sequence INTEGER,
                    lease_expires_at TEXT, updated_at TEXT, title TEXT, last_type TEXT,
                    PRIMARY KEY(project, task_id));
            """)
            # A session can be retired explicitly (the user moved to a new chat).
            # Older databases predate the column, so add it defensively.
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            if "retired" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN retired INTEGER NOT NULL DEFAULT 0")

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

    def task_by_key(self, sender, key):
        """The task created for one idempotency key, or None.

        Wakeups use this to tell "this event was already turned into a task"
        apart from "the role merely has some other work in flight".
        """
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE sender=? AND idem=?",
                             (sender, key)).fetchone()
            return self.decode(row) if row else None

    def latest_task_by_key_prefix(self, sender, prefix):
        """Latest attempt for a wakeup key, including ``:retry:`` attempts."""
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE sender=? AND substr(idem,1,?)=? "
                "ORDER BY created DESC, rowid DESC LIMIT 1",
                (sender, len(prefix), prefix)).fetchone()
            return self.decode(row) if row else None

    def count_tasks_by_key_prefix(self, sender, prefix):
        with self.db() as db:
            return db.execute(
                "SELECT count(*) FROM tasks WHERE sender=? AND substr(idem,1,?)=?",
                (sender, len(prefix), prefix)).fetchone()[0]

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
            elif row["retired"]:
                # A retired session is a tombstone: the user ended it, and neither
                # the client nor a stray heartbeat may bring it back.
                raise ValueError("session_retired")
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
            if row["retired"]:
                raise ValueError("session_retired")
            db.execute("UPDATE sessions SET status='online',last_seen=? WHERE id=?", (now, session_id))
            return self._session_row(db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())

    def remove_session(self, session_id):
        with self.db(True) as db:
            row = db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            return {"removed": row.rowcount}

    def cancel_wakeups(self, target):
        """Drop advisory bus wakeups for a role that is being rebound.

        A wakeup only says "check your bus inbox"; once the role points at another
        chat the notification is stale, and letting it block the rebind would keep
        the role attached to a session nobody is watching.
        """
        with self.db(True) as db:
            rows = db.execute("SELECT id,payload FROM tasks WHERE target=? AND state IN ('queued','running')",
                              (target,)).fetchall()
            dropped = []
            for row in rows:
                try:
                    kind = json.loads(row["payload"] or "{}").get("kind")
                except ValueError:
                    kind = None
                if kind != "agent_chat_wakeup":
                    continue
                db.execute("UPDATE tasks SET state='cancelled', ended=?, result=? WHERE id=?",
                           (time.time(), json.dumps({"error": "desktop_rebind",
                                                     "summary": "角色已绑定到另一个会话，这条唤醒作废"}),
                            row["id"]))
                dropped.append(row["id"])
            return dropped

    def retire_session(self, session_id, reason=""):
        """Mark a session as finished: no more delivery, no more claiming."""
        with self.db(True) as db:
            row = db.execute("UPDATE sessions SET retired=1, status='retired', description=?"
                             " WHERE id=?", (reason[:300], session_id))
            return {"retired": row.rowcount}

    def takeover_session(self, session_id, project, role, adapter, transport,
                         description="桌面聊天绑定"):
        """Register the new chat and retire the role's other sessions atomically.

        Binding and retirement must commit together: a half-applied takeover is
        exactly the state this method exists to make impossible.
        """
        now = time.time()
        with self.db(True) as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is not None and row["retired"]:
                raise ValueError("session_retired")
            if row is not None and (row["project"] != project or row["role"] != role):
                raise ValueError("session_id_already_bound_to_another_role")
            if row is None:
                db.execute("INSERT INTO sessions(id,project,role,adapter,transport,status,description,metadata,"
                           "created,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (session_id, project, role, adapter, transport, "online", description, "{}",
                            now, now))
            else:
                db.execute("UPDATE sessions SET adapter=?,transport=?,status='online',description=?,last_seen=?"
                           " WHERE id=?", (adapter, transport, description, now, session_id))
            others = db.execute("SELECT id FROM sessions WHERE project=? AND role=? AND id<>? AND retired=0",
                                (project, role, session_id)).fetchall()
            for other in others:
                db.execute("UPDATE sessions SET retired=1,status='retired' WHERE id=?", (other[0],))
            current = self._session_row(db.execute("SELECT * FROM sessions WHERE id=?",
                                                   (session_id,)).fetchone())
            return current, [other[0] for other in others]

    def session_snapshot(self, project, role):
        """JSON-safe copy of every session row for one role, for rollback."""
        fields = ("id", "project", "role", "adapter", "transport", "status", "description",
                  "metadata", "created", "last_seen", "retired")
        with self.db() as db:
            rows = db.execute("SELECT * FROM sessions WHERE project=? AND role=?",
                              (project, role)).fetchall()
            result = []
            for row in rows:
                item = self._session_row(row)
                result.append({key: item.get(key) for key in fields})
            return result

    def restore_session_snapshot(self, project, role, sessions):
        """Undo a takeover by restoring the exact previous session rows."""
        if not isinstance(sessions, list) or len(sessions) > 500:
            raise ValueError("invalid_session_snapshot")
        prepared = []
        for item in sessions:
            if not isinstance(item, dict):
                raise ValueError("invalid_session_snapshot")
            session = item.get("id")
            if not isinstance(session, str) or not session:
                raise ValueError("invalid_session_snapshot")
            metadata = item.get("metadata") or {}
            prepared.append({
                "id": session,
                "adapter": item.get("adapter"),
                "transport": item.get("transport") or "chat",
                "status": item.get("status") or "online",
                "description": item.get("description") or "",
                "metadata": compact(metadata if isinstance(metadata, dict) else {}),
                "created": item.get("created") or time.time(),
                "last_seen": item.get("last_seen") or time.time(),
                "retired": 1 if item.get("retired") else 0,
            })
        with self.db(True) as db:
            for row in prepared:
                current = db.execute("SELECT project,role FROM sessions WHERE id=?",
                                     (row["id"],)).fetchone()
                if current is not None and (current["project"] != project or current["role"] != role):
                    raise ValueError("session_id_already_bound_to_another_role")
            keep = {row["id"] for row in prepared}
            current = db.execute("SELECT id FROM sessions WHERE project=? AND role=?",
                                 (project, role)).fetchall()
            for row in current:
                if row[0] not in keep:
                    db.execute("DELETE FROM sessions WHERE id=?", (row[0],))
            for row in prepared:
                exists = db.execute("SELECT 1 FROM sessions WHERE id=?", (row["id"],)).fetchone()
                if exists:
                    db.execute("UPDATE sessions SET project=?,role=?,adapter=?,transport=?,status=?,"
                               "description=?,metadata=?,created=?,last_seen=?,retired=? WHERE id=?",
                               (project, role, row["adapter"], row["transport"], row["status"],
                                row["description"], row["metadata"], row["created"], row["last_seen"],
                                row["retired"], row["id"]))
                else:
                    db.execute("INSERT INTO sessions(id,project,role,adapter,transport,status,"
                               "description,metadata,created,last_seen,retired) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (row["id"], project, role, row["adapter"], row["transport"], row["status"],
                                row["description"], row["metadata"], row["created"], row["last_seen"],
                                row["retired"]))
            return len(prepared)

    def retire_role_sessions(self, project, role, keep=None):
        """Retire every session of a role except the one being taken over."""
        with self.db(True) as db:
            if keep:
                rows = db.execute("SELECT id FROM sessions WHERE project=? AND role=? AND id<>? AND retired=0",
                                  (project, role, keep)).fetchall()
            else:
                rows = db.execute("SELECT id FROM sessions WHERE project=? AND role=? AND retired=0",
                                  (project, role)).fetchall()
            for row in rows:
                db.execute("UPDATE sessions SET retired=1, status='retired' WHERE id=?", (row[0],))
            return [row[0] for row in rows]

    def session_retired(self, session_id):
        with self.db() as db:
            row = db.execute("SELECT retired FROM sessions WHERE id=?", (session_id,)).fetchone()
            return bool(row and row[0])

    def prune_stale_role_sessions(self, project, role, keep, keep_alive=600):
        """Drop other sessions of a role that have gone quiet.

        A role bound to a desktop chat should not keep showing half-dead sessions
        registered by hand earlier: they never heartbeat and make the list lie.
        """
        cutoff = time.time() - keep_alive
        with self.db(True) as db:
            rows = db.execute("SELECT id FROM sessions WHERE project=? AND role=? AND id<>? AND last_seen<?",
                              (project, role, keep, cutoff)).fetchall()
            for row in rows:
                db.execute("DELETE FROM sessions WHERE id=?", (row[0],))
            return [row[0] for row in rows]

    def recent_failures(self, limit=5):
        """Most recent failed tasks with their reason, for health output."""
        with self.db() as db:
            rows = db.execute("SELECT id,target,result FROM tasks WHERE state='failed'"
                              " ORDER BY created DESC LIMIT ?", (int(limit),)).fetchall()
        failures = []
        for row in rows:
            try:
                result = json.loads(row["result"] or "{}")
            except ValueError:
                result = {}
            failures.append({"task_id": row["id"], "target": row["target"],
                             "error": result.get("error") or result.get("summary") or "unknown"})
        return failures

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

    # -- file-bus projection ---------------------------------------------------
    # The shared bus files stay authoritative. These rows are a disposable
    # mirror so the hub and its UI can read task state without replaying the
    # whole log, and so the wake cursor no longer needs a separate JSON file.

    def bus_cursor(self, project):
        with self.db() as db:
            row = db.execute("SELECT cursor FROM bus_state WHERE project=?", (project,)).fetchone()
        return row["cursor"] if row else ""

    def set_bus_cursor(self, project, cursor):
        with self.db(True) as db:
            db.execute(
                "INSERT INTO bus_state(project, cursor, synced) VALUES(?,?,?) "
                "ON CONFLICT(project) DO UPDATE SET cursor=excluded.cursor, synced=excluded.synced",
                (project, cursor, time.time()))

    def sync_bus(self, project, tasks):
        """Replace this project's projection with a replay result.

        Wholesale replacement keeps the mirror honest: a task that vanished from
        the log (or a bus that was rebuilt) cannot leave a stale row behind.
        """
        rows = []
        for task in tasks.values():
            claim = task.get("claim") or {}
            request = task.get("request") or {}
            rows.append((
                project, task["task_id"], task.get("requester"), task.get("assignee"),
                task.get("status"), request.get("priority", "P2"), task.get("sequence"),
                claim.get("expires_at"), task.get("updated_at"),
                request.get("title"), (task.get("last_update") or {}).get("type"),
            ))
        with self.db(True) as db:
            db.execute("DELETE FROM bus_tasks WHERE project=?", (project,))
            db.executemany(
                "INSERT INTO bus_tasks(project, task_id, requester, assignee, status, priority, "
                "sequence, lease_expires_at, updated_at, title, last_type) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def bus_projection(self, project=None, include_closed=False):
        query = "SELECT * FROM bus_tasks"
        clauses, values = [], []
        if project is not None:
            clauses.append("project=?")
            values.append(project)
        if not include_closed:
            clauses.append("status NOT IN ('closed','cancelled')")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY priority, sequence"
        with self.db() as db:
            return [dict(row) for row in db.execute(query, values).fetchall()]




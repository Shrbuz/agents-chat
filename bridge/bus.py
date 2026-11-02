"""File-bus task protocol (v1) implemented natively for Agents Chat.

The shared on-disk bus stays the source of truth: every message is one
append-only JSON event in the recipient's mailbox, guarded by an OS file lock,
and a task is reconstructed by replaying those events.  This module is the
in-process implementation of that protocol; the bundled ``agent-chat`` skill
script remains a standalone client for projects that do not run this hub.

Nothing here shells out, polls, or keeps a second copy of task state.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import tempfile
import time
import uuid

SCHEMA_VERSION = 1
TERMINAL = ("closed", "cancelled")
PRIORITIES = ("P0", "P1", "P2", "P3")
RESERVED = ("_state", "evidence", ".git", "project.json", "project.local.json")


def utc_now():
    return datetime.now(timezone.utc)


def stamp(value=None):
    return (value or utc_now()).isoformat(timespec="microseconds")


def parse_time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include a timezone.")
    return parsed


def read_json(path):
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def atomic_json(path, value):
    """Caller holds the bus lock. The temporary file stays on the same volume."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".agents-chat-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def file_lock(path, timeout=10):
    """OS lock on the shared bus; a crash releases it, the lock file stays."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("Bus is busy; retry without deleting its lock file.")
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def text_field(data, key):
    if not isinstance(data.get(key), str) or not data[key].strip():
        raise ValueError(f"{key} must be a nonempty string.")


def list_field(data, key):
    values = data.get(key)
    if not isinstance(values, list) or not values:
        raise ValueError(f"{key} must be a nonempty list of strings.")
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise ValueError(f"{key} must contain nonempty strings.")


def uuid_text(value):
    if str(uuid.UUID(str(value))) != value:
        raise ValueError("IDs must be canonical lowercase UUIDs.")
    return value


def validate_role_ids(roles):
    if not isinstance(roles, (list, tuple)) or not roles:
        raise ValueError("roles must be a nonempty list of unique role identifiers.")
    if any(not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", role) for role in roles):
        raise ValueError("Role IDs must start with a lowercase letter and use 1-48 lowercase letters, digits, _ or -.")
    if len(set(roles)) != len(roles):
        raise ValueError("Duplicate role identifiers are not allowed.")
    return tuple(roles)


def mailbox_paths(root, mapping):
    """Resolve role mailboxes, refusing anything that escapes the bus root."""
    if not isinstance(mapping, dict):
        raise ValueError("roles must map role identifiers to mailbox directories.")
    validate_role_ids(list(mapping))
    root = Path(root).resolve()
    paths = {}
    for role, relative in mapping.items():
        if (not isinstance(relative, str) or not relative.strip() or
                any(ord(char) < 32 for char in relative)):
            raise ValueError("Mailbox paths must be nonempty relative directory names.")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("Mailbox path escapes the bus root.")
        path = (root / relative_path).resolve()
        if path == root or root not in path.parents:
            raise ValueError("Mailbox path escapes the bus root.")
        first_part = path.relative_to(root).parts[0].lower()
        if first_part in RESERVED:
            raise ValueError("Mailbox paths cannot use reserved state/evidence/config directories.")
        if any(path == other or path in other.parents or other in path.parents for other in paths.values()):
            raise ValueError("Mailbox directories must be distinct and must not overlap.")
        paths[role] = path
    return paths


class Bus:
    """One shared bus directory, addressed by role mailboxes from project.json."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        manifest = self.root / "project.json"
        if not manifest.is_file():
            raise ValueError(f"No project.json in bus root: {self.root}")
        self.config = read_json(manifest)
        if self.config.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported project configuration version.")
        self.project_id = self.config["project_id"]
        self.mailboxes = mailbox_paths(self.root, self.config["roles"])

    # -- state -----------------------------------------------------------------

    def require_role(self, role):
        if not isinstance(role, str) or role not in self.mailboxes:
            raise ValueError(f"Unknown role {role!r}; configured roles: {', '.join(self.mailboxes)}.")

    @contextlib.contextmanager
    def locked(self):
        with file_lock(self.root / "_state" / "bus.lock"):
            identity_file = self.root / "_state" / "bus.json"
            if identity_file.exists():
                identity = read_json(identity_file)
                if identity != {"schema_version": SCHEMA_VERSION, "project_id": self.project_id}:
                    raise ValueError("Shared bus belongs to another project/version.")
            else:
                atomic_json(identity_file, {"schema_version": SCHEMA_VERSION, "project_id": self.project_id})
            for folder in self.mailboxes.values():
                folder.mkdir(parents=True, exist_ok=True)
            yield

    def load(self):
        """Replay every event; any structural damage is an error, never a guess."""
        events = []
        message_ids, sequences = set(), set()
        for folder in self.root.glob("*_msg"):
            if folder.resolve() not in self.mailboxes.values() and any(folder.glob("*.json")):
                raise ValueError("History found in an unconfigured mailbox; restore the role configuration first.")
        for recipient, folder in self.mailboxes.items():
            for path in folder.glob("*.json"):
                event = read_json(path)
                if not isinstance(event, dict):
                    raise ValueError(f"Message must be a JSON object: {path}")
                if event.get("schema_version") != SCHEMA_VERSION or event.get("project_id") != self.project_id:
                    raise ValueError(f"Foreign or unsupported message: {path}")
                message_id = uuid_text(event["message_id"])
                uuid_text(event["task_id"])
                seq = event["sequence"]
                if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
                    raise ValueError(f"Invalid event sequence: {path}")
                if event.get("to") != recipient or event.get("from") not in self.mailboxes:
                    raise ValueError(f"Invalid message routing: {path}")
                if path.name != f"{seq:08d}_{message_id}.json":
                    raise ValueError(f"Message filename does not match event: {path}")
                parse_time(event["created_at"])
                if message_id in message_ids or seq in sequences:
                    raise ValueError("Duplicate event identity/sequence; inspect the bus before continuing.")
                message_ids.add(message_id)
                sequences.add(seq)
                events.append(event)
        events.sort(key=lambda e: e["sequence"])
        tasks = {}
        for expected_sequence, event in enumerate(events, 1):
            if event["sequence"] != expected_sequence:
                raise ValueError("Event sequence has a gap; restore missing messages before continuing.")
            previous = tasks.get(event["task_id"])
            expected_reply = previous["last_message_id"] if previous else None
            if event.get("in_reply_to") != expected_reply:
                raise ValueError("Event history has a broken reply chain.")
            self.apply(tasks, event)
        return events, tasks

    @staticmethod
    def apply(tasks, event):
        """Fold one event into the task projection. The event log stays authority."""
        kind, task_id, data = event["type"], event["task_id"], event["data"]
        if kind == "request":
            if task_id in tasks:
                raise ValueError("Duplicate task request.")
            tasks[task_id] = {
                "task_id": task_id, "requester": event["from"], "assignee": event["to"],
                "status": "pending", "request": data, "claim": None,
                "created_at": event["created_at"], "latest_result": None,
            }
        elif task_id not in tasks:
            raise ValueError("An event references a nonexistent request.")
        task = tasks[task_id]
        if kind == "claim":
            task["status"] = "in_progress"
            task["claim"] = {"session": event["session"], "token": data["token"],
                             "expires_at": data["expires_at"], "lease_seconds": data["lease_seconds"]}
        elif kind == "renew":
            if task["claim"] is None:
                raise ValueError("Renew event has no active claim.")
            task["claim"]["expires_at"] = data["expires_at"]
        elif kind in ("blocked", "result", "cancel"):
            task["status"] = {"blocked": "blocked", "result": "resolved", "cancel": "cancelled"}[kind]
            task["claim"] = None
            if kind == "result":
                task["latest_result"] = data
        elif kind == "verify":
            task["status"] = "closed" if data["outcome"] == "pass" else "pending"
            task["verification"] = data
        elif kind not in ("request", "progress"):
            raise ValueError(f"Unknown event type: {kind}")
        task["updated_at"] = event["created_at"]
        task["last_message_id"] = event["message_id"]
        task["sequence"] = event["sequence"]
        task["last_update"] = {"type": kind, "session": event["session"], "data": data}

    # -- writes ----------------------------------------------------------------

    def submit(self, kind, role, session, task_id=None, data=None, token=None,
               recipient=None, message_id=None, lease_seconds=None):
        """Append one event under the bus lock; identical replays are idempotent."""
        self.require_role(role)
        if not isinstance(session, str) or not session.strip() or len(session) > 160:
            raise ValueError("A valid role and nonempty session (max 160 chars) are required.")
        if task_id is not None:
            uuid_text(task_id)
        message_id = uuid_text(message_id) if message_id else str(uuid.uuid4())
        data = dict(data or {})
        if lease_seconds is None:
            lease_seconds = self.config.get("lease_seconds", 1800)
        if not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 86400:
            raise ValueError("lease-seconds must be between 30 and 86400.")
        intent = {"type": kind, "role": role, "session": session, "task_id": task_id,
                  "data": data.copy(), "token": token, "recipient": recipient,
                  "lease_seconds": lease_seconds if kind == "claim" else None}
        with self.locked():
            events, tasks = self.load()
            existing = next((e for e in events if e["message_id"] == message_id), None)
            if existing:
                if existing.get("intent") != intent:
                    raise ValueError("message-id was already used with different content.")
                return {"event": existing, "task": tasks[existing["task_id"]], "duplicate": True}
            now = utc_now()
            if kind == "request":
                if recipient not in self.mailboxes or recipient == role:
                    raise ValueError("A request must target another valid role.")
                for key in ("title", "body"):
                    text_field(data, key)
                list_field(data, "acceptance")
                if data.get("priority", "P2") not in PRIORITIES:
                    raise ValueError("priority must be P0/P1/P2/P3.")
                if data.get("due_at"):
                    parse_time(data["due_at"])
                task_id, target, previous = message_id, recipient, None
            else:
                if task_id not in tasks:
                    raise ValueError("Task not found.")
                task = tasks[task_id]
                if task["status"] in TERMINAL:
                    raise ValueError("Task is already closed/cancelled.")
                target, previous = task["requester"], task["last_message_id"]
                claim = task["claim"]
                if kind == "claim":
                    if role != task["assignee"] or task["status"] not in ("pending", "blocked", "in_progress"):
                        raise ValueError("Only the assigned role can claim pending/blocked/expired work.")
                    if claim and parse_time(claim["expires_at"]) > now:
                        raise ValueError("Task already has a live claim; use renew in its owning session.")
                    data = {"token": str(uuid.uuid4()), "lease_seconds": lease_seconds,
                            "expires_at": stamp(now + timedelta(seconds=lease_seconds))}
                elif kind in ("renew", "progress", "blocked", "result"):
                    if role != task["assignee"] or not claim or claim["session"] != session or claim["token"] != token:
                        raise ValueError("Claim is missing, replaced, or belongs to another session.")
                    if parse_time(claim["expires_at"]) <= now:
                        raise ValueError("Claim expired; reconcile work and claim again for a new token.")
                    if kind == "renew":
                        data = {"expires_at": stamp(now + timedelta(seconds=claim["lease_seconds"]))}
                    else:
                        text_field(data, "body")
                        if kind == "result":
                            list_field(data, "evidence")
                elif kind in ("verify", "cancel"):
                    if role != task["requester"]:
                        raise ValueError("Only the original requesting role may verify or cancel.")
                    text_field(data, "body")
                    if kind == "verify":
                        if task["status"] != "resolved" or data.get("outcome") not in ("pass", "fail"):
                            raise ValueError("Verify needs a resolved task and outcome pass/fail.")
                    target = task["assignee"]
                else:
                    raise ValueError("Unsupported command.")
            event = {
                "schema_version": SCHEMA_VERSION, "project_id": self.project_id,
                "sequence": events[-1]["sequence"] + 1 if events else 1,
                "message_id": message_id, "task_id": task_id, "type": kind,
                "from": role, "to": target, "session": session,
                "created_at": stamp(now), "in_reply_to": previous, "data": data, "intent": intent,
            }
            self.apply(tasks, event)
            path = self.mailboxes[target] / f"{event['sequence']:08d}_{message_id}.json"
            if path.exists():
                raise ValueError("Refusing to overwrite an existing event.")
            atomic_json(path, event)
            return {"event": event, "task": tasks[task_id], "duplicate": False}

    # -- reads -----------------------------------------------------------------

    def receipt_path(self, role, session):
        digest = hashlib.sha256(f"{role}\0{session}".encode()).hexdigest()
        return self.root / "_state" / "receipts" / f"{digest}.json"

    def inbox(self, role, session, limit=50):
        self.require_role(role)
        with self.locked():
            events, tasks = self.load()
            path = self.receipt_path(role, session)
            seen = set(read_json(path)["message_ids"]) if path.exists() else set()
            messages = [{**e, "task_status": tasks[e["task_id"]]["status"]}
                        for e in events if e["to"] == role and e["type"] != "renew"
                        and e["message_id"] not in seen]
            messages.sort(key=lambda e: (tasks[e["task_id"]]["request"].get("priority", "P2"), e["sequence"]))
            return {"role": role, "session": session, "total_unread": len(messages),
                    "messages": messages[:limit], "bus_root": str(self.root)}

    def mark_read(self, role, session, message_ids):
        self.require_role(role)
        with self.locked():
            events, _ = self.load()
            eligible = {e["message_id"] for e in events if e["to"] == role}
            if not set(message_ids) <= eligible:
                raise ValueError("Cannot mark nonexistent messages or another role's messages read.")
            path = self.receipt_path(role, session)
            seen = set(read_json(path)["message_ids"]) if path.exists() else set()
            seen.update(message_ids)
            atomic_json(path, {"role": role, "session": session, "message_ids": sorted(seen)})
            return {"marked_read": message_ids}

    def status(self, role=None, include_closed=False):
        if role is not None:
            self.require_role(role)
        with self.locked():
            _, tasks = self.load()
            now = utc_now()
            selected = []
            for original in tasks.values():
                if role and role not in (original["requester"], original["assignee"]):
                    continue
                if original["status"] in TERMINAL and not include_closed:
                    continue
                task = dict(original)
                task["lease_expired"] = bool(task["claim"] and parse_time(task["claim"]["expires_at"]) <= now)
                task["overdue"] = bool(task["request"].get("due_at") and
                                       parse_time(task["request"]["due_at"]) <= now and task["status"] not in TERMINAL)
                selected.append(task)
            selected.sort(key=lambda t: (t["request"].get("priority", "P2"), t["sequence"]))
            return {"project_id": self.project_id, "bus_root": str(self.root), "tasks": selected}

    def show(self, task_id):
        with self.locked():
            events, tasks = self.load()
            if task_id not in tasks:
                raise ValueError("Task not found.")
            return {"task": tasks[task_id], "events": [e for e in events if e["task_id"] == task_id]}

    def paths(self):
        return {"project_id": self.project_id, "bus_root": str(self.root),
                "mailboxes": {k: str(v) for k, v in self.mailboxes.items()},
                "evidence": str(self.root / "evidence")}

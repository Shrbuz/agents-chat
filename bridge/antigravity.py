"""Antigravity App integration through the vendor-managed agentapi Sidecar."""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .client import Client


TRANSPORT = "antigravity_native"
HEARTBEAT_TTL = 20


def read_activity(sessions, database=None):
    """Read only the bound root conversations from Antigravity's local sidebar summaries."""
    if not sessions:
        return {}
    database = database or Path.home() / ".gemini/antigravity/conversation_summaries.db"
    states = {}
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.05)) as db:
            ids = list(sessions)
            for i in range(0, len(ids), 500):
                batch = ids[i:i + 500]
                marks = ",".join("?" for _ in batch)
                rows = db.execute(
                    f"SELECT conversation_id, status, not_fully_idle, killed FROM conversation_summaries "
                    f"WHERE conversation_id IN ({marks}) AND COALESCE(parent_conversation_id, '') = ''", batch)
                for session, status, not_idle, killed in rows:
                    if killed:
                        states[session] = "idle"
                    elif status in ("CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_BUSY",
                                    "CASCADE_RUN_STATUS_CANCELING") or not_idle:
                        states[session] = "active"
                    elif status == "CASCADE_RUN_STATUS_IDLE":
                        states[session] = "idle"
    except (OSError, sqlite3.Error):
        return {}
    return states


def conversation_id(value):
    try:
        canonical = str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("antigravity_conversation_id_required: pass the desktop conversation UUID") from None
    if value.lower() != canonical:
        raise ValueError("antigravity_conversation_id_required: use the hyphenated UUID")
    return canonical


def agentapi_command():
    # The vendor's Windows launcher is a .bat wrapper. Unwrap its fixed argv
    # so collaboration prompts go directly to CreateProcess, never to cmd.exe.
    wrapper = Path.home() / ".gemini/antigravity/bin/agentapi.bat"
    try:
        text = wrapper.read_text(encoding="utf-8-sig")
    except OSError:
        raise ValueError("antigravity_agentapi_missing: launch Antigravity App once") from None
    match = re.fullmatch(r'@echo off\s+"([^"\r\n]+)" agentapi %\*\s*', text, re.IGNORECASE)
    if not match:
        raise ValueError("antigravity_agentapi_launcher_unrecognized")
    exe = Path(match[1])
    if not exe.is_absolute() or exe.name.lower() != "language_server.exe" or not exe.is_file():
        raise ValueError("antigravity_agentapi_executable_missing")
    return [str(exe), "agentapi"]


def atomic_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + "-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def configure_sidecar(state, enabled):
    if os.name != "nt":
        raise ValueError("antigravity_desktop_setup_requires_windows")
    state = Path(state).resolve()
    suffix = hashlib.sha256(os.path.normcase(str(state)).encode()).hexdigest()[:16]
    name = "agents-chat-" + suffix
    config_root = Path.home() / ".gemini/config"
    config_path = config_root / "config.json"
    manifest = config_root / "sidecars" / name / "sidecar.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        config = {}
    if not isinstance(config, dict) or not isinstance(config.setdefault("sidecars", {}), dict):
        raise ValueError("antigravity_config_invalid")
    entry = config["sidecars"].get(name, {})
    if not isinstance(entry, dict):
        raise ValueError("antigravity_sidecar_config_invalid")
    if enabled:
        agentapi_command()
        launcher = Path(__file__).resolve().parents[1] / "antigravity_desktop_link.py"
        if not launcher.is_file():
            raise ValueError("antigravity_sidecar_launcher_missing")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(manifest, {
            "display_name": "Agents Chat connector",
            "description": "Delivers Agents Chat mailbox wakeups to explicitly bound conversations.",
            "command": str(Path(sys.executable).resolve()),
            "args": [str(launcher), "--state", str(state)],
            "restart_policy": "on-failure",
        })
    if enabled or name in config["sidecars"]:
        config["sidecars"][name] = {**entry, "enabled": enabled}
        atomic_json(config_path, config)
    return {"sidecar": name, "enabled": enabled, "manifest": str(manifest), "hub_state": str(state)}


class Probes:
    """Short-lived, in-memory workspace checks requested by join/status."""

    def __init__(self, stop):
        self.stop = stop
        self.cv = threading.Condition()
        self.seen = float("-inf")
        self.pending = {}

    @property
    def ready(self):
        return not self.stop.is_set() and time.monotonic() - self.seen < HEARTBEAT_TTL

    def poll(self):
        with self.cv:
            self.seen = time.monotonic()
            return [probe for probe, result in self.pending.values() if result is None]

    def request(self, session, cwd):
        probe = {"id": str(uuid.uuid4()), "session_id": conversation_id(session),
                 "cwd": str(Path(cwd).resolve())}
        key = probe["id"]
        with self.cv:
            if not self.ready:
                raise ValueError("antigravity_sidecar_offline: run antigravity setup and open Antigravity App")
            if len(self.pending) >= 16:
                raise ValueError("antigravity_probe_queue_full")
            self.pending[key] = (probe, None)
            try:
                if not self.cv.wait_for(lambda: self.pending[key][1] is not None, timeout=12):
                    raise ValueError("antigravity_probe_timeout")
                kind = self.pending[key][1]
                if kind != "ready":
                    raise ValueError(kind)
                return {"ready": True, "session_id": probe["session_id"], "cwd": probe["cwd"]}
            finally:
                del self.pending[key]

    def complete(self, key, kind):
        if kind not in ("ready", "antigravity_probe_failed", "antigravity_session_missing",
                        "antigravity_workspace_mismatch"):
            raise ValueError("antigravity_invalid_probe_result")
        with self.cv:
            if key in self.pending:
                self.pending[key] = (self.pending[key][0], kind)
                self.cv.notify_all()


def call_agentapi(command, *args, timeout=8):
    result = subprocess.run([*command, *args], stdin=subprocess.DEVNULL, capture_output=True,
                            encoding="utf-8", errors="replace", timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    value = json.loads(result.stdout)
    if result.returncode or not isinstance(value, dict) or value.get("error"):
        raise RuntimeError("antigravity_agentapi_error")
    return value["response"]


def probe_session(command, session, cwd):
    session = conversation_id(session)
    try:
        value = call_agentapi(command, "get-conversation-metadata", session, timeout=4)
        meta = value["conversationMetadata"]["metadata"]
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return "antigravity_probe_failed"
    if not isinstance(meta, dict) or meta.get("rootConversationId") != session or meta.get("parentConversationId"):
        return "antigravity_session_missing"
    uris = meta.get("workspaceUris")
    if not isinstance(uris, list):
        return "antigravity_workspace_mismatch"
    workspace = Path(cwd).resolve()
    for uri in uris:
        if not isinstance(uri, str):
            continue
        try:
            parsed = urlsplit(uri)
            if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
                continue
            # Antigravity percent-encodes the Windows drive separator.
            path = re.sub(r"^/([a-z])%3a(?=/)", r"/\1:", parsed.path, flags=re.IGNORECASE)
            local = Path(url2pathname(path))
            if local.is_absolute() and local.resolve() == workspace:
                return "ready"
        except (OSError, ValueError):
            continue
    return "antigravity_workspace_mismatch"


def deliver(client, command, job):
    """Record intent before calling agentapi. A crash must not duplicate a wakeup."""
    state = "failed"
    if job["action"] == "cancel":
        state = "cancelled"
        result = {"summary": "发送前已取消通知。", "run_delivered": False}
    elif not job["notify"]:
        result = {"summary": "Antigravity 桌面角色请通过文件总线派活。",
                  "error": "antigravity_requires_bus", "run_delivered": False}
    else:
        session = conversation_id(job["session_id"])
        receipt = client.state_dir / "runs" / str(uuid.UUID(job["task_id"])) / "antigravity-receipt.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        accepted = False
        try:
            with receipt.open("x", encoding="utf-8") as handle:
                json.dump({"session_id": session, "accepted": False}, handle)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            try:
                previous = json.loads(receipt.read_text(encoding="utf-8"))
                accepted = previous == {"session_id": session, "accepted": True}
            except (OSError, ValueError):
                pass
        else:
            try:
                reply = call_agentapi(command, "send-message", session, job["prompt"])
                accepted = reply["sendMessage"]["recipientId"] == session
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired):
                pass
            if accepted:
                atomic_json(receipt, {"session_id": session, "accepted": True})
        if accepted:
            state = "succeeded"
            result = {"summary": "Antigravity 已接受向目标会话投递通知。",
                      "run_delivered": True, "notification": True, "session_id": session}
        else:
            result = {"summary": "无法确认 Antigravity 是否收到通知，请查看目标会话和总线收件箱。",
                      "error": "antigravity_delivery_uncertain", "delivery_uncertain": True,
                      "run_delivered": False, "unnotified": True, "needs_attention": True}
    client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"], state=state, result=result)
    return state == "succeeded"


def run_sidecar(state):
    if not os.environ.get("ANTIGRAVITY_LS_ADDRESS"):
        raise ValueError("antigravity_host_context_missing: launch as an Antigravity Sidecar")
    command = agentapi_command()
    client = Client(state)
    cache = {}
    while True:
        delay = 1
        try:
            snapshot = client.call("antigravity_poll")
            for item in snapshot["probes"]:
                kind = probe_session(command, item["session_id"], item["cwd"])
                client.call("antigravity_probe_result", id=item["id"], kind=kind)
            roles = snapshot["roles"]
            cache = {r["agent"]: cache[r["agent"]] for r in roles if r["agent"] in cache}
            for item in roles:
                agent, session, cwd = item["agent"], item["session_id"], item["cwd"]
                now = time.monotonic()
                session_key = (session, cwd)
                if agent not in cache or cache[agent][0] != session_key or now >= cache[agent][2]:
                    kind = probe_session(command, session, cwd)
                    cache[agent] = (session_key, kind, now + 8)
                    client.call("transport_report", agent=agent, transport=TRANSPORT, session_id=session,
                                state="ready" if kind == "ready" else "degraded",
                                failure_kind=None if kind == "ready" else kind)
                if cache[agent][1] != "ready":
                    continue
                job = client.call("desktop_receive", agent=agent, session_id=session, transport=TRANSPORT,
                                  seconds=0)
                if job and deliver(client, command, job):
                    client.call("transport_report", agent=agent, transport=TRANSPORT, session_id=session,
                                state="ready", delivered=True)
        except (OSError, ValueError, RuntimeError):
            # The Antigravity process hosts the loop; an Agents Chat restart
            # or a stopped Hub should not restart Antigravity or replay sends.
            cache.clear()
            delay = 3
        time.sleep(delay)

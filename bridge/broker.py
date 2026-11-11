import hmac
import json
import math
import os
import re
import sys
import secrets
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .runtime import run_task
from . import titles
from .store import Store, TERMINAL, compact
from .desktop_transport import DesktopHub
from .agentchat_bridge import AgentChatBridge
from .codex_connection import select_connection, update_connection
from .config import (ConfigError, adapter_for_transport, allowed_target, find_role, label,
                     normalize_config, projects_summary, read_config, session_id as config_session_id,
                     transport_supported)


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"expected_integer_{low}_to_{high}")
    return value


class SingleInstance:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("broker_already_running_for_this_state_directory") from None

    def close(self):
        self.file.close()


class Broker:
    def __init__(self, state_dir, config):
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "connectors.stop").unlink(missing_ok=True)
        self.lock = SingleInstance(self.state_dir / "broker.lock")
        if "config_path" not in config:
            # Internal callers and tests may pass an already flat legacy mapping.
            raw = config if "projects" in config else {"endpoints": config["endpoints"]}
            config = normalize_config(raw, self.state_dir / "config.json")
        self.config = config
        self.endpoints = config["endpoints"]
        self.store = Store(self.state_dir / "bridge.sqlite3")
        self.cv = threading.Condition()
        self.stop = threading.Event()
        self.workers = []
        self.connector_processes = {}
        self.connector_lock = threading.Lock()
        self.busy_cwds = set()
        self.recovered = self.store.recover()
        self.shutdown_callback = None
        self.desktop = DesktopHub(self.state_dir)
        self.agent_chat = AgentChatBridge(
            self.state_dir, config, self.store, self.notify_all,
            transport_online=lambda agent: self.role_transport_health(agent).get("online", False))
        self.store.purge_stale_sessions()
        # A persisted desktop binding is a real session even before its connector
        # sends the first heartbeat.
        for name, binding in list(self.desktop.bindings.items()):
            endpoint = self.endpoints.get(name)
            if not (endpoint and binding.get("session_id")):
                continue
            transport = binding.get("transport", "desktop")
            expected = adapter_for_transport(transport)
            if (expected is not None and endpoint["adapter"] != "fixture"
                    and expected != endpoint["adapter"]):
                # A binding left over from another adapter must not be relabelled
                # with the new adapter on startup.  That relabelling is exactly
                # the half-migrated state; keep the old session row intact and let
                # `session migrate-and-takeover` retire it explicitly.
                sys.stderr.write("agents-chat: binding for " + name + " uses transport "
                                 + transport + " but role adapter is " + endpoint["adapter"]
                                 + "; run session migrate-and-takeover\n")
                sys.stderr.flush()
                continue
            try:
                # Prefer the chat name the client itself shows; fall back to what
                # the hub can prove (which role, through which client).
                label = titles.desktop_label(binding.get("transport", "desktop"),
                                             binding["session_id"], endpoint["role"])
                self.store.register_session("desktop:" + binding["session_id"], endpoint["project"],
                                            endpoint["role"], endpoint["adapter"],
                                            binding.get("transport", "desktop"), label)
                # Drop half-dead sessions registered by hand earlier, so the role
                # list shows the chat that actually serves it.
                self.store.prune_stale_role_sessions(endpoint["project"], endpoint["role"],
                                                     "desktop:" + binding["session_id"])
            except ValueError:
                # Two roles pointing at one chat is a configuration mistake, not a
                # reason to refuse to start. Keep the first binding and drop this
                # one so the hub can still serve every other role.
                sys.stderr.write("agents-chat: dropping duplicate binding for " + name
                                 + " (chat already owned by another role)\n")
                sys.stderr.flush()
                try:
                    self.desktop.bind(name, None)
                except ValueError:
                    self.desktop.bindings.pop(name, None)

    def start(self):
        for project_id in self.config.get("projects", {}):
            self.write_active_sessions(project_id)
        for name, endpoint in self.endpoints.items():
            if endpoint.get("enabled", True) and endpoint.get("mode") != "mcp_pull":
                worker = threading.Thread(target=self.worker, args=(name,), name="adapter-" + name, daemon=True)
                worker.start()
                self.workers.append(worker)
        self.agent_chat.start()
        self._ensure_desktop_connectors()

    def notify_all(self):
        with self.cv:
            self.cv.notify_all()

    def close(self, release_lock=True):
        self.stop.set()
        try:
            (self.state_dir / "connectors.stop").write_text("stop\n", encoding="ascii")
        except OSError:
            pass
        self.agent_chat.close()
        self.desktop.close()
        with self.cv:
            self.cv.notify_all()
        for worker in self.workers:
            worker.join(timeout=20)
        for process in list(self.connector_processes.values()):
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        if release_lock:
            self.lock.close()

    def worker(self, name):
        endpoint = self.endpoints[name]
        while not self.stop.is_set():
            with self.cv:
                if self.stop.is_set():
                    return
                task = None
                # Existing desktop chats can already be running independently of us.
                # Serialize each bound chat; a chat waiting on the other must not hold
                # the CLI workspace lock and deadlock cross-role communication.
                cwd_key = "desktop:"+name if name in self.desktop.bindings else os.path.normcase(endpoint["cwd"])
                if cwd_key not in self.busy_cwds:
                    task = self.store.claim(name)
                if task is None:
                    self.cv.wait(timeout=1)  # Also wakes delayed transport retries; no model call.
                    continue
                self.busy_cwds.add(cwd_key)
            try:
                wakeup = task["payload"].get("kind") == "agent_chat_wakeup"
                if (not wakeup and
                        task["created"] + task["payload"].get("queue_ttl", 3600) < time.time()):
                    state, result = "timed_out", {"error": "queue_deadline", "summary": "Expired before runtime launch."}
                elif self.desktop_input_disabled(name):
                    state, result = "failed", {"error":"qoder_desktop_input_disabled", "summary":"Qoder input automation is disabled; this queued task was not delivered."}
                elif endpoint["adapter"] == "dsh" and name not in self.desktop.bindings:
                    # DSH roles are served by the host plugin. Spawning a separate
                    # headless process is the old way and is no longer supported:
                    # it has no chat to show, and no GUI credentials.
                    state, result = "failed", {
                        "error": "dsh_requires_plugin",
                        "summary": "dsh 角色由 DSH 宿主插件接入：请让插件绑定到你的聊天后再派任务（不再支持另起进程执行）。"}
                else:
                    binding = self.desktop.bindings.get(name)
                    if binding:
                        from .runtime import make_prompt
                        # A timeout only stops the sender waiting unless the
                        # caller explicitly opted into interrupting the chat.
                        # Wakeups never opt in; the broker used to infer the
                        # opposite for ordinary sends and killed live work.
                        interrupt = bool(task["payload"].get("interrupt_on_timeout", False))
                        state, result = self.desktop.dispatch(
                            task, binding, make_prompt(task, endpoint),
                            self.store.cancelled, self.stop,
                            interrupt_on_timeout=interrupt,
                            delivery_timeout=task["payload"].get("delivery_timeout", 30),
                            execution_timeout=task["payload"].get("timeout", 180))
                        result = {**result, "mode": "desktop_session", "session_id": binding["session_id"]}
                        run_dir = self.state_dir / "runs" / task["id"]
                        run_dir.mkdir(parents=True, exist_ok=True)
                        result["result_path"] = str(run_dir / "result.json")
                        (run_dir / "result.json").write_text(compact({"state": state, **result}), encoding="utf-8")
                    else:
                        state, result = run_task(task, endpoint, self.state_dir, self.store.cancelled, self.stop)
                if wakeup and state != "succeeded" and (
                        result.get("retryable") or result.get("error") in (
                            "desktop_delivery_timeout", "desktop_chat_busy", "pipe_missing",
                            "runtime_missing", "app_tools_server_missing",
                            "desktop_bridge_disconnected", "connection_context_missing",
                            "opencode_service_unavailable", "opencode_session_missing",
                            "opencode_transport")):
                    attempts = int(task.get("attempt_count") or 0)
                    delay = 5 if result.get("error") == "desktop_chat_busy" else min(30, 2 ** min(attempts, 5))
                    self.store.requeue_wakeup(task["id"], {
                        **result, "needs_attention": True, "unnotified": True,
                        "summary": result.get("summary") or "Wakeup was not delivered; retry is pending.",
                    }, delay)
                else:
                    self.store.finish(task["id"], state, result)
            except Exception as exc:
                if task["payload"].get("kind") == "agent_chat_wakeup":
                    self.store.requeue_wakeup(task["id"], {
                        "error": "adapter_exception", "summary": str(exc)[:500],
                        "retryable": True, "needs_attention": True, "unnotified": True}, 3)
                else:
                    self.store.finish(task["id"], "failed", {"error": "adapter_exception", "summary": str(exc)[:500]})
            finally:
                with self.cv:
                    self.busy_cwds.discard(cwd_key)
                    self.cv.notify_all()

    def public_task(self, task_id):
        task = self.store.get(task_id)
        # Payload and full history intentionally excluded from default responses.
        return {k: task[k] for k in ("id", "sender", "target", "state", "created", "started", "ended", "result")}

    def resolve(self, address, project=None, required=False):
        try:
            return find_role(self.config, address, project)
        except ConfigError:
            if required:
                raise
            return label(address)

    def target_mode(self, internal):
        if internal not in self.endpoints:
            return "independent_cli_task"
        if self.desktop_input_disabled(internal):
            return "desktop_mailbox"
        if internal in self.desktop.bindings:
            return "desktop_session"
        return self.endpoints[internal].get("mode", "independent_cli_task")

    def active_sessions(self, project=None):
        """role -> bound desktop session id, per project."""
        result = {}
        for name, binding in self.desktop.bindings.items():
            endpoint = self.endpoints.get(name)
            if not endpoint or not binding.get("session_id"):
                continue
            if project is not None and endpoint.get("project") != project:
                continue
            result[endpoint["role"]] = binding["session_id"]
        return result

    def takeover_postconditions(self, internal, session_id, transport, expected_adapter):
        """Prove that a takeover produced a complete, single-owner role.

        The skill and the tray both consume this instead of re-deriving the
        rules, so "success" means the same thing everywhere.
        """
        endpoint = self.endpoints[internal]
        keep = "desktop:" + session_id
        sessions = self.store.list_sessions(project=endpoint["project"], role=endpoint["role"])
        row = next((item for item in sessions if item["id"] in (keep, session_id)), None)
        others = [item for item in sessions if item["id"] != keep]
        binding = self.desktop.bindings.get(internal) or {}
        retired = [item["id"] for item in others if item.get("retired")]
        checks = {
            "role_adapter_matches_client": (endpoint["adapter"] == "fixture"
                                            or endpoint["adapter"] == expected_adapter),
            "session_adapter_matches_role": bool(
                row and (endpoint["adapter"] == "fixture" or row["adapter"] == endpoint["adapter"])),
            "transport_matches_adapter": (endpoint["adapter"] == "fixture"
                                          or transport_supported(endpoint["adapter"], transport)),
            "single_active_session": (binding.get("session_id") == session_id
                                      and not [item for item in others if not item.get("retired")]),
            "old_sessions_retired": all(item.get("retired") for item in others),
        }
        return {
            "ok": all(checks.values()),
            "checks": checks,
            "role_adapter": endpoint["adapter"],
            "client_adapter": expected_adapter,
            "session_adapter": row["adapter"] if row else None,
            "transport": transport,
            "active_session": binding.get("session_id"),
            "retired_sessions": retired,
        }

    def write_active_sessions(self, project_id):
        """Publish the role -> session map into the project's bus.

        The bus is role-level by design, so without this file any live session
        claiming to be that role can take work. The map lets both bus clients
        refuse a session the role has moved away from.
        """
        project = self.config["projects"].get(project_id) or {}
        bus = (project.get("agent_chat") or {}).get("bus")
        if not bus:
            return
        path = Path(bus) / "_state" / "active-sessions.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.active_sessions(project_id), ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            # A missing bus must not break binding; the hub still routes by role.
            pass

    def binding_mismatch(self, agent):
        """True when the persisted chat cannot be served by this role's adapter."""
        endpoint = self.endpoints.get(agent)
        binding = self.desktop.bindings.get(agent, {}) or {}
        transport = binding.get("transport")
        expected = adapter_for_transport(transport)
        return bool(endpoint and transport and expected and endpoint["adapter"] != "fixture"
                    and expected != endpoint["adapter"])

    def desktop_input_disabled(self, agent):
        binding = self.desktop.bindings.get(agent, {})
        return (binding.get("transport") == "qoder_accessibility"
                and not self.endpoints.get(agent, {}).get("desktop_input_enabled", True))

    def _ensure_codex_connectors(self):
        """Start missing Codex role connectors without touching the hub or Qoder."""
        script = Path(__file__).resolve().parent.parent / "desktop_link.py"
        if not script.is_file() or not (self.state_dir / "endpoint.json").is_file():
            return
        with self.connector_lock:
            for agent, binding in list(self.desktop.bindings.items()):
                endpoint = self.endpoints.get(agent) or {}
                if (binding.get("transport") != "codex_app_tools"
                        or endpoint.get("adapter") not in ("codex", "fixture")):
                    continue
                if select_connection(self.state_dir, binding.get("session_id")) is None:
                    continue
                current = self.connector_processes.get(agent)
                if current is not None and current.poll() is None:
                    continue
                self.connector_processes.pop(agent, None)
                safe = agent.replace(".", "-")
                try:
                    probe = SingleInstance(self.state_dir / (safe + "-desktop.lock"))
                except RuntimeError:
                    continue
                probe.close()
                log_path = self.state_dir / (safe + "-desktop.log")
                with log_path.open("ab") as log:
                    process = subprocess.Popen(
                        [sys.executable, str(script), "--state", str(self.state_dir),
                         "--serve", "--agent", agent],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
                self.connector_processes[agent] = process
                threading.Thread(target=process.wait, daemon=True).start()

    def _ensure_opencode_connectors(self):
        """Start one exact-session OpenCode connector per bound role."""
        script = Path(__file__).resolve().parent.parent / "opencode_desktop_link.py"
        if not script.is_file() or not (self.state_dir / "endpoint.json").is_file():
            return
        with self.connector_lock:
            for agent, binding in list(self.desktop.bindings.items()):
                endpoint = self.endpoints.get(agent) or {}
                if (binding.get("transport") != "opencode_native"
                        or endpoint.get("adapter") not in ("opencode", "fixture")):
                    continue
                current = self.connector_processes.get(agent)
                if current is not None and current.poll() is None:
                    continue
                self.connector_processes.pop(agent, None)
                safe = agent.replace(".", "-")
                try:
                    probe = SingleInstance(self.state_dir / (safe + "-desktop.lock"))
                except RuntimeError:
                    continue
                probe.close()
                log_path = self.state_dir / (safe + "-opencode.log")
                with log_path.open("ab") as log:
                    process = subprocess.Popen(
                        [sys.executable, str(script), "--state", str(self.state_dir),
                         "--agent", agent],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
                self.connector_processes[agent] = process
                threading.Thread(target=process.wait, daemon=True).start()

    def _ensure_desktop_connectors(self):
        self._ensure_codex_connectors()
        self._ensure_opencode_connectors()

    def _refresh_codex_context(self, params, force=False):
        context = params.get("connection_context")
        if not isinstance(context, dict):
            return None
        try:
            updated = update_connection(self.state_dir, context)
        except (OSError, ValueError) as exc:
            epoch = self.desktop.refresh_transport("codex_app_tools")
            for agent, binding in list(self.desktop.bindings.items()):
                if binding.get("transport") != "codex_app_tools":
                    continue
                kind = "runtime_missing" if "runtime" in str(exc).lower() else "offline"
                self.desktop.report_transport(
                    agent, "codex_app_tools", kind, error=str(exc), failure_kind=kind)
            return {"updated": False, "epoch": epoch, "error": str(exc)[:300]}
        epoch = self.desktop.connector_epoch("codex_app_tools")
        connection_id = updated["connection"]["connection_id"]
        status_mismatch = any(
            binding.get("transport") == "codex_app_tools"
            and (self.desktop.transport_status.get(agent) or {}).get("connection_id") != connection_id
            for agent, binding in self.desktop.bindings.items())
        if force or updated["generation_changed"] or status_mismatch:
            epoch = self.desktop.refresh_transport(
                "codex_app_tools", connection_id)
        self._ensure_codex_connectors()
        return {"updated": True, "generation_changed": updated["generation_changed"],
                "connection_id": connection_id, "epoch": epoch}

    def role_transport_health(self, internal):
        binding = self.desktop.bindings.get(internal) or {}
        transport = binding.get("transport")
        if transport in ("codex_app_tools", "opencode_native"):
            return self.desktop.connection_status(internal)
        if not transport:
            return {"state": "unbound", "transport": None, "online": False}
        endpoint = self.endpoints.get(internal) or {}
        session_id = binding.get("session_id")
        sessions = self.store.list_sessions(project=endpoint.get("project"), role=endpoint.get("role"))
        row = next((item for item in sessions if item.get("id") in (
            session_id, "desktop:" + str(session_id))), None)
        online = bool(row and row.get("online"))
        last_seen = row.get("last_seen") if row else None
        if transport == "qoder_native":
            try:
                native = json.loads((self.state_dir / "qoder-native-status.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                native = {}
            native_seen = float(native.get("updated_at") or 0) / 1000
            online = bool(native.get("ready") and time.time() - native_seen < 15)
            last_seen = native_seen or last_seen
        return {"state": "ready" if online else "offline", "transport": transport,
                "online": online, "updated_at": last_seen,
                "failure_kind": None if online else "connector_heartbeat_stale"}

    def passive_transport_health(self, transport):
        roles = []
        for internal, binding in self.desktop.bindings.items():
            if binding.get("transport") != transport:
                continue
            item = self.role_transport_health(internal)
            wakeups = self.store.wakeup_stats(internal)
            item.update({"agent": internal, "session_id": binding.get("session_id"),
                         "pending_wakeups": wakeups["pending"],
                         "needs_attention": wakeups["needs_attention"],
                         "pending_result_wakeups": wakeups["pending_results"]})
            roles.append(item)
        degraded = any(not item.get("online") for item in roles)
        return {"ok": not degraded, "degraded": degraded, "transport": transport,
                "state": "degraded" if degraded else ("ready" if roles else "unused"),
                "roles": roles, "consecutive_failures": 0,
                "last_success_at": max((float(item.get("updated_at") or 0) for item in roles), default=0) or None,
                "pending_result_wakeups": sum(item["pending_result_wakeups"] for item in roles),
                "needs_attention": sum(item["needs_attention"] for item in roles)}

    def public_sessions(self, project=None, role=None, include_offline=True):
        rows = self.store.list_sessions(project=project, role=role, include_offline=include_offline)
        result = []
        for row in rows:
            item = {key: row[key] for key in ("id", "project", "role", "adapter", "transport", "status",
                                               "online", "description", "created", "last_seen", "metadata", "retired")}
            if row["id"].startswith("desktop:"):
                internal = next((name for name, endpoint in self.endpoints.items()
                                 if endpoint.get("project") == row.get("project")
                                 and endpoint.get("role") == row.get("role")), None)
                binding = self.desktop.bindings.get(internal, {}) if internal else {}
                if internal and binding.get("session_id") and row["id"] == "desktop:" + binding["session_id"]:
                    status = self.role_transport_health(internal)
                    item["connection_status"] = status.get("state")
                    item["online"] = bool(item["online"] and status.get("online"))
                    if not item["online"] and not item.get("retired"):
                        item["effective_status"] = ("reconnecting" if status.get("state") in (
                            "reconnecting", "pipe_missing", "runtime_missing",
                            "app_tools_server_missing", "service_unavailable",
                            "session_missing") else "offline")
            result.append(item)
        if not include_offline:
            result = [item for item in result if item["online"]]
        return result

    def role_activity(self):
        """Build per-role work and directional traffic for the live topology."""
        activity = {name: {
            "queued_tasks": 0, "running_tasks": 0,
            "last_send_event_id": None, "last_send_at": None, "last_send_kind": None,
            "last_receive_event_id": None, "last_receive_at": None, "last_receive_kind": None,
        } for name in self.endpoints}
        snapshot = self.store.activity_snapshot()

        def record(internal, direction, event_id, at, kind):
            if internal not in activity or at is None:
                return
            prefix = "last_send" if direction == "send" else "last_receive"
            current = activity[internal][prefix + "_at"]
            if current is None or float(at) > float(current):
                activity[internal][prefix + "_event_id"] = str(event_id)
                activity[internal][prefix + "_at"] = float(at)
                activity[internal][prefix + "_kind"] = kind

        def resolve_bus_role(project, role):
            if not role:
                return None
            try:
                return find_role(self.config, role, project)
            except ConfigError:
                return None

        def bus_time(value):
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except (TypeError, ValueError):
                return None

        for item in snapshot["active"]:
            target = item.get("target")
            if target not in activity or item.get("kind") == "agent_chat_wakeup":
                continue
            key = "running_tasks" if item.get("state") == "running" else "queued_tasks"
            activity[target][key] += 1

        for event in snapshot["events"]:
            state = event.get("state")
            event_id = "queue:{}:{}:{}".format(event.get("seq"), event.get("task_id"), state)
            at = event.get("at")
            if state == "queued":
                record(event.get("sender"), "send", event_id, at, "task")
            elif state == "running":
                record(event.get("target"), "receive", event_id, at, "task")
            elif state in TERMINAL and event.get("started") is not None:
                record(event.get("target"), "send", event_id, at, "result")
                record(event.get("sender"), "receive", event_id, at, "result")

        requester_to_assignee = {"request", "verify", "cancel", "reassign"}
        assignee_to_requester = {"claim", "result", "blocked", "release", "progress", "renew"}
        for task in snapshot["bus_tasks"]:
            requester = resolve_bus_role(task.get("project"), task.get("requester"))
            assignee = resolve_bus_role(task.get("project"), task.get("assignee"))
            if task.get("status") == "pending" and assignee in activity:
                activity[assignee]["queued_tasks"] += 1
            elif task.get("status") == "in_progress" and assignee in activity:
                activity[assignee]["running_tasks"] += 1
            kind = task.get("last_type")
            if kind in requester_to_assignee:
                sender, recipient = requester, assignee
            elif kind in assignee_to_requester:
                sender, recipient = assignee, requester
            else:
                continue
            at = bus_time(task.get("updated_at"))
            base = "bus:{}:{}:{}:{}".format(
                task.get("project"), task.get("task_id"), task.get("sequence"), kind)
            record(sender, "send", base + ":send", at, kind)
            record(recipient, "receive", base + ":receive", at, kind)
        return activity

    def call(self, method, p):
        if not isinstance(p, dict):
            raise ValueError("params_must_be_object")
        connection_refresh = None
        if method in ("health", "status", "session_register", "session_heartbeat",
                      "desktop_bind", "session_takeover", "transport_refresh"):
            connection_refresh = self._refresh_codex_context(
                p, force=(method == "transport_refresh"))
        if method == "health":
            # A crashed role connector is restarted independently; unrelated
            # transports and the Hub itself stay up.
            self._ensure_desktop_connectors()
            agent_chat_health = self.agent_chat.health()
            transports = {
                "codex_app_tools": self.desktop.transport_health(self.store),
                "opencode_native": self.desktop.transport_health(self.store, "opencode_native"),
            }
            for transport in ("qoder_native", "qoder_accessibility", "qoder_extension", "dsh_plugin"):
                transports[transport] = self.passive_transport_health(transport)
            transport_degraded = any(item.get("degraded", False) for item in transports.values())
            return {"status": "running", "pid": os.getpid(), "mode": "multi_project_hub", "version": __version__,
                    "degraded": bool(agent_chat_health.get("degraded")
                                     or not agent_chat_health.get("ok", True)
                                     or transport_degraded),
                    "states": self.store.stats(), "projects": len(self.config["projects"]),
                    "roles": len(self.endpoints), "recovered": self.recovered,
                    "agent_chat": self.agent_chat.status(),
                    "agent_chat_health": agent_chat_health,
                    "transports": transports,
                    "workers_alive": sum(w.is_alive() for w in self.workers),
                    "failed_tasks": self.store.recent_failures(5)}
        if method == "status":
            return {"health": self.call("health", {}), "roles": self.call("roles", {})}
        if method in ("roles", "projects", "role_list"):
            role_activity = self.role_activity()
            projects = projects_summary(self.config)
            for project in projects:
                for role in project["roles"]:
                    internal = role["address"]
                    role.update(role_activity.get(internal, {}))
                    role["sessions"] = self.public_sessions(project=project["id"], role=role["role"])
                    role["mode"] = self.target_mode(internal)
                    role["desktop_wakeup"] = internal in self.desktop.bindings and not self.desktop_input_disabled(internal)
                    role["desktop_transport"] = self.desktop.bindings.get(internal, {}).get("transport")
                    role["session_id"] = self.desktop.bindings.get(internal, {}).get("session_id")
                    binding = self.desktop.bindings.get(internal, {})
                    role["session_title"] = titles.desktop_title(binding.get("transport"), binding.get("session_id"))
                    role["pending"] = self.store.has_pending(internal)
                    transport_health = self.role_transport_health(internal)
                    wakeups = self.store.wakeup_stats(internal)
                    transport_health.update({
                        "pending_wakeups": wakeups["pending"],
                        "needs_attention": wakeups["needs_attention"],
                        "pending_result_wakeups": wakeups["pending_results"],
                    })
                    role["transport_health"] = transport_health
                    role["connection_status"] = transport_health.get("state")
                    role["online"] = bool(transport_health.get("online")) if internal in self.desktop.bindings else any(
                        item.get("online") for item in role["sessions"])
                    # The tray and the skill both need an explicit signal.  A
                    # mismatch used to be invisible until tasks failed.
                    role["expected_adapter"] = adapter_for_transport(binding.get("transport"))
                    role["adapter_transport_mismatch"] = self.binding_mismatch(internal)
            return {"projects": projects, "totals": self.store.stats()}
        if method == "targets":
            result = []
            for name, endpoint in self.endpoints.items():
                result.append({"name": name, "address": name, "project": endpoint.get("project"),
                               "role": endpoint.get("role"), "adapter": endpoint["adapter"],
                               "enabled": endpoint.get("enabled", True), "description": endpoint.get("description", ""),
                               "peers": endpoint.get("peers", []), "mode": self.target_mode(name),
                               "desktop_wakeup": name in self.desktop.bindings and not self.desktop_input_disabled(name),
                               "desktop_input_enabled": endpoint.get("desktop_input_enabled", True),
                               "desktop_transport": self.desktop.bindings.get(name, {}).get("transport"),
                               "session_id": self.desktop.bindings.get(name, {}).get("session_id"),
                               "expected_adapter": adapter_for_transport(
                                   self.desktop.bindings.get(name, {}).get("transport")),
                               "adapter_transport_mismatch": self.binding_mismatch(name),
                               "transport_health": self.role_transport_health(name),
                               "sessions": self.public_sessions(project=endpoint.get("project"), role=endpoint.get("role"))})
            return result
        if method == "transport_refresh":
            return connection_refresh or {"updated": False, "reason": "no_codex_context"}
        if method == "connectors_start":
            self._ensure_desktop_connectors()
            return {"started": True}
        if method == "transport_report":
            agent = self.resolve(p.get("agent"), p.get("project"), required=True)
            result = self.desktop.report_transport(
                agent, p.get("transport"), p.get("state"), error=p.get("error"),
                summary=p.get("summary"), connection_id=p.get("connection_id"),
                delivered=bool(p.get("delivered", False)), failure_kind=p.get("failure_kind"))
            return {"status": result, "connector_epoch": self.desktop.connector_epoch(p.get("transport"))}
        if method == "desktop_bind":
            value = p.get("agent", p.get("role"))
            if not isinstance(value, str):
                raise ValueError("target_required")
            internal = self.resolve(value, p.get("project"), required=True)
            endpoint = self.endpoints[internal]
            chat = p.get("session_id")
            if chat is not None and (not isinstance(chat, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", chat)):
                raise ValueError("invalid_desktop_session_id")
            transport = p.get("transport")
            if chat is not None:
                expected = adapter_for_transport(transport)
                if expected is None:
                    raise ValueError("unsupported_desktop_transport")
                if endpoint["adapter"] != "fixture" and expected != endpoint["adapter"]:
                    raise ValueError(
                        "adapter_transport_mismatch: role adapter=" + endpoint["adapter"]
                        + " cannot use transport=" + str(transport) + "; it belongs to adapter="
                        + expected + ". Use session migrate-and-takeover to change the adapter,"
                        " or switch to a chat from the role's own client")
                if transport == "qoder_accessibility":
                    if (self.state_dir / "qoder-input-disabled").exists():
                        raise ValueError(
                            "qoder_desktop_input_disabled: use the qoder_native connector; "
                            "accessibility delivery is disabled")
                    if internal != "qoder":
                        raise ValueError(
                            "qoder_accessibility_project_role_unsupported: project roles require qoder_native")
            if chat and (self.store.session_retired("desktop:" + chat) or self.store.session_retired(chat)):
                raise ValueError("session_retired: 这个会话已被用户结束，请先接管到另一个会话")
            # Binding a role changes routing, so never redirect already accepted work.
            previous = self.desktop.bindings.get(internal)
            wanted = {"session_id": chat, "transport": transport} if chat else None
            # Re-binding the same session is not a redirect; a plugin that restarts
            # while tasks are queued must still be able to reattach.
            unchanged = previous == wanted
            with self.cv:
                if self.store.has_pending(internal) and not unchanged:
                    # Wakeups are advisory: the role is moving to another chat, so
                    # cancel those instead of refusing to reattach.
                    self.store.cancel_wakeups(internal)
                if self.store.has_pending(internal) and not unchanged:
                    raise ValueError("target_has_pending_tasks")
                self.desktop.bind(internal, {"session_id": chat, "transport": transport} if chat else None)
            if previous and previous.get("session_id"):
                self.store.remove_session("desktop:" + previous["session_id"])
            if chat:
                self.store.register_session("desktop:" + chat, endpoint["project"], endpoint["role"],
                                            endpoint["adapter"], transport, "桌面聊天绑定")
                if transport in ("codex_app_tools", "opencode_native"):
                    self._ensure_desktop_connectors()
            self.write_active_sessions(endpoint["project"])
            return {"agent": internal, "address": internal,
                    "mode": "desktop_session" if chat else self.target_mode(internal),
                    "session_id": chat, "transport": transport,
                    "connection_refresh": connection_refresh}
        if method == "desktop_heartbeat":
            internal = self.resolve(p.get("agent", p.get("role")), p.get("project"), required=True)
            binding = self.desktop.bindings.get(internal) or {}
            if (binding.get("session_id") != p.get("session_id")
                    or binding.get("transport") != p.get("transport")):
                raise ValueError("desktop_session_not_bound: call bridge_register in this OpenCode session")
            try:
                row = self.store.heartbeat_session("desktop:" + binding["session_id"])
            except ValueError:
                endpoint = self.endpoints[internal]
                row = self.store.register_session(
                    "desktop:" + binding["session_id"], endpoint["project"], endpoint["role"],
                    endpoint["adapter"], binding["transport"], "桌面聊天绑定")
            self._ensure_desktop_connectors()
            return {"session": row, "transport_health": self.role_transport_health(internal)}
        if method == "session_takeover":
            # One action moves a role to another chat: bind, retire the rest, and
            # publish the active session so the bus can refuse the old one.
            # Adapter migration is *not* part of this command: changing the
            # adapter rewrites the role's runtime and needs a hub restart, which
            # the CLI performs atomically in `session migrate-and-takeover`.
            internal = self.resolve(p.get("agent") or p.get("role"), p.get("project"), required=True)
            endpoint = self.endpoints[internal]
            takeover_session, transport = p.get("session_id"), p.get("transport")
            try:
                takeover_session = config_session_id(takeover_session)
            except ConfigError:
                raise ValueError("invalid_desktop_session_id") from None
            expected = adapter_for_transport(transport)
            if expected is None:
                raise ValueError("unsupported_desktop_transport")
            if endpoint["adapter"] != "fixture" and expected != endpoint["adapter"]:
                raise ValueError(
                    "adapter_transport_mismatch: role adapter=" + endpoint["adapter"]
                    + " cannot be taken over with transport=" + str(transport)
                    + " (that transport belongs to adapter=" + expected
                    + "). Use `session migrate-and-takeover --role " + endpoint["role"]
                    + " --adapter auto --transport " + str(transport)
                    + "` to move the role to this client atomically")
            project_id, role_id = endpoint["project"], endpoint["role"]
            previous_binding = self.desktop.bindings.get(internal)
            sessions_before = self.store.session_snapshot(project_id, role_id)
            try:
                with self.cv:
                    self.desktop.bind(internal, {"session_id": takeover_session, "transport": transport})
                self.store.takeover_session("desktop:" + takeover_session, project_id, role_id,
                                            endpoint["adapter"], transport)
                if transport in ("codex_app_tools", "opencode_native"):
                    self._ensure_desktop_connectors()
                self.write_active_sessions(project_id)
                postconditions = self.takeover_postconditions(
                    internal, takeover_session, transport, expected)
                if not postconditions["ok"]:
                    raise ValueError("takeover_postconditions_failed: "
                                     + compact(postconditions["checks"]))
            except Exception:
                # Put the previous binding and every session row back before the
                # caller sees an error.  A failed takeover must leave no trace.
                try:
                    with self.cv:
                        self.desktop.bind(internal, previous_binding)
                finally:
                    self.store.restore_session_snapshot(project_id, role_id, sessions_before)
                    self.write_active_sessions(project_id)
                raise
            if self.store.has_pending(internal):
                self.store.cancel_wakeups(internal)
            retired = postconditions.get("retired_sessions", [])
            return {"agent": internal, "address": internal, "session_id": takeover_session,
                    "adapter": endpoint["adapter"], "transport": transport,
                    "adapter_changed": False, "hub_restarted": False,
                    "active_session": takeover_session,
                    "retired": retired, "retired_sessions": retired,
                    "postconditions": postconditions,
                    "connection_refresh": connection_refresh}
        if method == "session_retire":
            retire_target = p.get("session_id")
            if not isinstance(retire_target, str) or not retire_target:
                raise ValueError("invalid_session_id")
            # Sessions are stored as bare ids or "desktop:<id>"; retire whichever exists.
            for candidate in (retire_target, "desktop:" + retire_target):
                self.store.retire_session(candidate, p.get("reason", "retired by user"))
            for name, binding in list(self.desktop.bindings.items()):
                if binding.get("session_id") != retire_target:
                    continue
                endpoint = self.endpoints.get(name)
                with self.cv:
                    self.desktop.bind(name, None)
                if endpoint:
                    self.write_active_sessions(endpoint["project"])
            return {"retired": retire_target}
        if method in ("sessions", "session_list"):
            return {"sessions": self.store.list_sessions(project=p.get("project"), role=p.get("role"),
                                                         include_offline=True),
                    "active": self.active_sessions(p.get("project"))}
        if method == "desktop_receive":
            value = p.get("agent", p.get("role"))
            if not isinstance(value, str):
                raise ValueError("target_required")
            internal = self.resolve(value, p.get("project"), required=True)
            binding = self.desktop.bindings.get(internal)
            if not binding or binding["session_id"] != p["session_id"]:
                raise ValueError("desktop_session_not_bound")
            if self.binding_mismatch(internal):
                raise ValueError(
                    "adapter_transport_mismatch: role adapter="
                    + self.endpoints[internal]["adapter"]
                    + " cannot receive through transport=" + str(binding.get("transport"))
                    + "; run session migrate-and-takeover")
            if self.store.session_retired("desktop:" + p["session_id"]):
                raise ValueError("session_retired")
            seconds = p.get("seconds", 40)
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 45:
                raise ValueError("invalid_receive_timeout")
            recover_received = p.get("recover_received", False)
            if type(recover_received) is not bool:
                raise ValueError("invalid_recover_received")
            if recover_received and binding.get("transport") != "opencode_native":
                raise ValueError("recovery_not_supported_for_transport")
            try:
                self.store.heartbeat_session("desktop:" + p["session_id"])
            except ValueError:
                pass
            return self.desktop.receive(internal, p["session_id"], seconds,
                                        transport=p.get("transport"),
                                        connector_epoch=p.get("connector_epoch"),
                                        recover_received=recover_received)
        if method == "desktop_finish":
            if p.get("state") not in ("succeeded", "failed", "cancelled", "interrupted"):
                raise ValueError("invalid_desktop_result_state")
            result = p.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("summary"), str) or len(compact(result)) > 12000:
                raise ValueError("invalid_desktop_result")
            return self.desktop.finish(p["task_id"], p["nonce"], p["state"], result)
        if method == "session_register":
            pid = p.get("project")
            rid = p.get("role")
            if not isinstance(pid, str) or not isinstance(rid, str):
                raise ValueError("project_and_role_required")
            internal = find_role(self.config, rid, pid)
            endpoint = self.endpoints[internal]
            sid = config_session_id(p.get("session"))
            transport = p.get("transport", "chat")
            if not isinstance(transport, str) or not re.fullmatch(r"[a-z_]{1,32}", transport):
                raise ValueError("invalid_session_transport")
            description = p.get("description", "")
            if not isinstance(description, str) or len(description) > 300:
                raise ValueError("session_description_too_long")
            metadata = p.get("metadata") or {}
            if not isinstance(metadata, dict) or len(compact(metadata)) > 4000:
                raise ValueError("invalid_session_metadata")
            row = self.store.register_session(sid, endpoint["project"], endpoint["role"],
                                              endpoint["adapter"], transport, description, metadata)
            return {"session": row, "address": internal, "role": {"role": endpoint["role"],
                    "description": endpoint.get("description", ""), "peers": endpoint.get("peers", []),
                    "mode": self.target_mode(internal)}, "connection_refresh": connection_refresh}
        if method == "session_heartbeat":
            return {"session": self.store.heartbeat_session(config_session_id(p.get("session"))),
                    "connection_refresh": connection_refresh}
        if method == "session_list":
            return {"sessions": self.public_sessions(p.get("project"), p.get("role"),
                                                     p.get("include_offline", True))}
        if method == "session_remove":
            return self.store.remove_session(config_session_id(p.get("session")))
        if method == "send":
            if self.stop.is_set():
                raise ValueError("broker_stopping")
            allowed = {"sender", "target", "prompt", "key", "timeout", "context", "reply_mode",
                       "queue_ttl", "project", "interrupt_on_timeout", "delivery_timeout", "kind"}
            if set(p) - allowed:
                raise ValueError("unknown_send_fields")
            project = p.get("project")
            sender = self.resolve(label(p["sender"]), project, required=False)
            target = self.resolve(label(p["target"]), project, required=True)
            target_endpoint = self.endpoints[target]
            if not target_endpoint.get("enabled", True):
                raise ValueError("target_unavailable")
            if self.binding_mismatch(target):
                raise ValueError(
                    "adapter_transport_mismatch: target role adapter=" + target_endpoint["adapter"]
                    + " but its bound chat uses transport="
                    + str(self.desktop.bindings.get(target, {}).get("transport"))
                    + "; run session migrate-and-takeover before dispatching work")
            if self.desktop_input_disabled(target):
                raise ValueError("qoder_desktop_input_disabled: Qoder can send requests and receive their results through wait/inbox; native unsolicited desktop delivery is not connected")
            if not allowed_target(self.config, sender, target):
                raise ValueError("target_not_allowed_for_sender: update this role's peers in config")
            prompt = p.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
                raise ValueError("prompt_must_be_1_to_6000_characters")
            key = p.get("key")
            if not isinstance(key, str) or not 1 <= len(key) <= 160:
                raise ValueError("stable_idempotency_key_required")
            timeout = integer(p.get("timeout", 180), 1, 3600)
            queue_ttl = integer(p.get("queue_ttl", 3600), 1, 86400)
            reply = p.get("reply_mode", "mailbox")
            if reply not in ("mailbox", "run"):
                raise ValueError("unknown_reply_mode")
            kind = p.get("kind")
            if kind not in (None, "agent_chat_wakeup"):
                raise ValueError("unknown_task_kind")
            interrupt = p.get("interrupt_on_timeout", False)
            if type(interrupt) is not bool:
                raise ValueError("interrupt_on_timeout_must_be_boolean")
            if kind == "agent_chat_wakeup":
                # A wakeup is a notification, never a cancellable execution.
                reply, interrupt = "mailbox", False
            delivery_timeout = integer(p.get("delivery_timeout", min(30, timeout)), 1, 3600)
            if reply == "run" and (sender not in self.endpoints or not self.endpoints[sender].get("enabled", True)):
                raise ValueError("reply_runtime_not_configured")
            if reply == "run" and self.desktop_input_disabled(sender):
                raise ValueError("reply_mode_run_unavailable: use mailbox with wait/inbox; Qoder desktop input automation is disabled")
            context = p.get("context", [])
            if not isinstance(context, list) or len(context) > 8 or not all(isinstance(x, str) and len(x) <= 500 for x in context):
                raise ValueError("context_must_be_up_to_8_local_paths")
            root = Path(target_endpoint["cwd"])
            normalized = []
            for value in context:
                path = (root / value).resolve()
                if not path.is_relative_to(root):
                    raise ValueError("context_path_outside_target_workspace")
                normalized.append(str(path))
            payload = {"prompt": prompt, "timeout": timeout, "context": normalized,
                       "reply_mode": reply, "queue_ttl": queue_ttl,
                       "interrupt_on_timeout": interrupt, "delivery_timeout": delivery_timeout,
                       "sender_address": sender, "target_address": target}
            if kind == "agent_chat_wakeup":
                payload.update({"kind": kind, "notify": False})
            # Stable fields only in the idempotency fingerprint. Queue expiration is derived from created.
            with self.cv:
                task_id, duplicate = self.store.enqueue(sender, target, key, payload)
                self.cv.notify_all()
            return {"task_id": task_id, "duplicate": duplicate, "state": self.store.get(task_id)["state"],
                    "sender": sender, "target": target, "mode": self.target_mode(target),
                    "kind": kind, "delivery_timeout": delivery_timeout,
                    "interrupt_on_timeout": interrupt}
        if method == "pull":
            internal = self.resolve(label(p["recipient"]), p.get("project"), required=True)
            if self.target_mode(internal) != "mcp_pull":
                raise ValueError("role_is_not_mcp_pull")
            while True:
                task = self.store.claim(internal)
                if task is None:
                    return {"task": None, "address": internal}
                if (task["payload"].get("kind") != "agent_chat_wakeup" and
                        task["created"] + task["payload"].get("queue_ttl", 3600) < time.time()):
                    self.store.finish(task["id"], "timed_out", {
                        "error": "queue_deadline", "summary": "Expired before WorkBuddy claimed it."})
                    continue
                payload = task["payload"]
                return {"task": {"id": task["id"], "from": task["sender"],
                                  "prompt": payload["prompt"], "context": payload.get("context", []),
                                  "timeout": payload.get("timeout"), "kind": payload.get("kind")},
                        "address": internal}
        if method == "complete":
            internal = self.resolve(label(p["recipient"]), p.get("project"), required=True)
            task = self.store.get(p["task_id"])
            if task["target"] != internal:
                raise ValueError("task_recipient_mismatch")
            if task["state"] in TERMINAL:
                return {"task_id": task["id"], "state": task["state"], "duplicate": True}
            if task["state"] != "running":
                raise ValueError("task_not_claimed")
            state = p.get("state", "succeeded")
            if state not in ("succeeded", "failed"):
                raise ValueError("invalid_completion_state")
            summary = p.get("summary")
            if not isinstance(summary, str) or not summary.strip() or len(summary) > 800:
                raise ValueError("summary_must_be_1_to_800_characters")
            error = p.get("error")
            if error is not None and (not isinstance(error, str) or len(error) > 200):
                raise ValueError("error_must_be_up_to_200_characters")
            self.store.finish(task["id"], state, {"summary": summary, "error": error,
                                                   "mode": "mcp_pull"})
            with self.cv:
                self.cv.notify_all()
            return {"task_id": task["id"], "state": self.store.get(task["id"])["state"],
                    "duplicate": False}
        if method == "task":
            return self.public_task(p["task_id"])
        if method == "wait":
            seconds = p.get("seconds", 45)
            if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 <= seconds <= 50:
                raise ValueError("wait_seconds_must_be_0_to_50")
            deadline = time.monotonic() + seconds
            with self.cv:
                while True:
                    task = self.public_task(p["task_id"])
                    remaining = deadline - time.monotonic()
                    if task["state"] in TERMINAL or remaining <= 0 or self.stop.is_set():
                        return task
                    self.cv.wait(remaining)
        if method == "cancel":
            self.store.cancel(p["task_id"])
            with self.cv:
                self.cv.notify_all()
            return self.public_task(p["task_id"])
        if method == "inbox":
            after = p.get("after")
            if after is not None:
                integer(after, 0, 2**63 - 1)
            recipient = p["recipient"]
            try:
                recipient = find_role(self.config, recipient, p.get("project"))
            except ConfigError:
                recipient = label(recipient)
            return self.store.inbox(recipient, label(p.get("consumer", "default")), after,
                                    integer(p.get("limit", 10), 1, 20))
        if method == "ack":
            recipient = p["recipient"]
            try:
                recipient = find_role(self.config, recipient, p.get("project"))
            except ConfigError:
                recipient = label(recipient)
            self.store.ack(recipient, label(p.get("consumer", "default")), integer(p["cursor"], 0, 2**63 - 1))
            return {"acknowledged": p["cursor"]}
        if method == "shutdown" and self.shutdown_callback:
            threading.Thread(target=self.shutdown_callback, daemon=True).start()
            return {"stopping": True}
        raise ValueError("unknown_method")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 32


def serve(state_dir, config_path):
    config = read_config(config_path)
    broker = Broker(state_dir, config)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(60)

        def log_message(self, *args):
            pass

        def respond(self, status, body):
            data = compact(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if self.path != "/call":
                return self.respond(404, {"error": "not_found"})
            if self.headers.get("Origin") or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                return self.respond(403, {"error": "nonlocal_client"})
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                return self.respond(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get("Transfer-Encoding") or not 0 < length <= 65536:
                    return self.respond(413, {"error": "request_size_limit"})
                if self.headers.get_content_type() != "application/json":
                    return self.respond(415, {"error": "json_required"})
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ValueError("request_must_be_object")
                result = broker.call(request["method"], request.get("params", {}))
                self.respond(200, {"result": result})
            except KeyError as exc:
                self.respond(404, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self.respond(400, {"error": str(exc)[:300]})
            except Exception:
                self.respond(500, {"error": "internal_error"})

    server = None
    try:
        server = Server(("127.0.0.1", 0), Handler)
        broker.shutdown_callback = server.shutdown
        endpoint = {"url": f"http://127.0.0.1:{server.server_port}", "token": token,
                    "pid": os.getpid(), "config": str(Path(config_path).resolve())}
        descriptor = broker.state_dir / "endpoint.json"
        temp = descriptor.with_suffix(".tmp")
        temp.write_text(compact(endpoint), encoding="utf-8")
        if os.name != "nt":
            temp.chmod(0o600)
        os.replace(temp, descriptor)
        broker.start()
        print(compact({"ready": True, "url": endpoint["url"], "pid": os.getpid()}), flush=True)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        broker.close(release_lock=False)
        if server:
            server.server_close()
        descriptor = broker.state_dir / "endpoint.json"
        try:
            for attempt in range(20):
                try:
                    descriptor.unlink(missing_ok=True)
                    break
                except PermissionError:
                    if attempt==19:
                        raise
                    time.sleep(.05)
        finally:
            broker.lock.close()








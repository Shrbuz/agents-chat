import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .runtime import run_task
from .store import Store, TERMINAL, compact
from .desktop_transport import DesktopHub
from .agentchat_bridge import AgentChatBridge
from .config import (ConfigError, allowed_target, find_role, label, normalize_config,
                     projects_summary, read_config, session_id)


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
        self.busy_cwds = set()
        self.recovered = self.store.recover()
        self.shutdown_callback = None
        self.desktop = DesktopHub(self.state_dir)
        self.agent_chat = AgentChatBridge(self.state_dir, config, self.store, self.notify_all)
        self.store.purge_stale_sessions()
        # A persisted desktop binding is a real session even before its connector
        # sends the first heartbeat.
        for name, binding in self.desktop.bindings.items():
            endpoint = self.endpoints.get(name)
            if endpoint and binding.get("session_id"):
                self.store.register_session("desktop:" + binding["session_id"], endpoint["project"],
                                            endpoint["role"], endpoint["adapter"],
                                            binding.get("transport", "desktop"), "桌面聊天绑定")

    def start(self):
        for name, endpoint in self.endpoints.items():
            if endpoint.get("enabled", True):
                worker = threading.Thread(target=self.worker, args=(name,), name="adapter-" + name, daemon=True)
                worker.start()
                self.workers.append(worker)
        self.agent_chat.start()

    def notify_all(self):
        with self.cv:
            self.cv.notify_all()

    def close(self, release_lock=True):
        self.stop.set()
        self.agent_chat.close()
        self.desktop.close()
        with self.cv:
            self.cv.notify_all()
        for worker in self.workers:
            worker.join(timeout=20)
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
                    self.cv.wait()  # No timer and no model call when the queue is empty.
                    continue
                self.busy_cwds.add(cwd_key)
            try:
                if task["created"] + task["payload"].get("queue_ttl", 3600) < time.time():
                    state, result = "timed_out", {"error": "queue_deadline", "summary": "Expired before runtime launch."}
                elif self.desktop_input_disabled(name):
                    state, result = "failed", {"error":"qoder_desktop_input_disabled", "summary":"Qoder input automation is disabled; this queued task was not delivered."}
                else:
                    binding = self.desktop.bindings.get(name)
                    if binding:
                        from .runtime import make_prompt
                        state, result = self.desktop.dispatch(task, binding, make_prompt(task, endpoint), self.store.cancelled, self.stop)
                        result = {**result, "mode": "desktop_session", "session_id": binding["session_id"]}
                        run_dir = self.state_dir / "runs" / task["id"]
                        run_dir.mkdir(parents=True, exist_ok=True)
                        result["result_path"] = str(run_dir / "result.json")
                        (run_dir / "result.json").write_text(compact({"state": state, **result}), encoding="utf-8")
                    else:
                        state, result = run_task(task, endpoint, self.state_dir, self.store.cancelled, self.stop)
                self.store.finish(task["id"], state, result)
            except Exception as exc:
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
        return "desktop_session" if internal in self.desktop.bindings else "independent_cli_task"

    def desktop_input_disabled(self, agent):
        binding = self.desktop.bindings.get(agent, {})
        return (binding.get("transport") == "qoder_accessibility"
                and not self.endpoints.get(agent, {}).get("desktop_input_enabled", True))

    def public_sessions(self, project=None, role=None, include_offline=True):
        rows = self.store.list_sessions(project=project, role=role, include_offline=include_offline)
        return [{key: row[key] for key in ("id", "project", "role", "adapter", "transport", "status",
                                            "online", "description", "created", "last_seen", "metadata")}
                for row in rows]

    def call(self, method, p):
        if not isinstance(p, dict):
            raise ValueError("params_must_be_object")
        if method == "health":
            return {"status": "running", "pid": os.getpid(), "mode": "multi_project_hub", "version": __version__,
                    "states": self.store.stats(), "projects": len(self.config["projects"]),
                    "roles": len(self.endpoints), "recovered": self.recovered,
                    "agent_chat": self.agent_chat.status(),
                    "workers_alive": sum(w.is_alive() for w in self.workers)}
        if method == "status":
            return {"health": self.call("health", {}), "roles": self.call("roles", {})}
        if method in ("roles", "projects", "role_list"):
            projects = projects_summary(self.config)
            for project in projects:
                for role in project["roles"]:
                    internal = role["address"]
                    role["sessions"] = self.public_sessions(project=project["id"], role=role["role"])
                    role["mode"] = self.target_mode(internal)
                    role["desktop_wakeup"] = internal in self.desktop.bindings and not self.desktop_input_disabled(internal)
                    role["desktop_transport"] = self.desktop.bindings.get(internal, {}).get("transport")
                    role["session_id"] = self.desktop.bindings.get(internal, {}).get("session_id")
                    role["pending"] = self.store.has_pending(internal)
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
                               "sessions": self.public_sessions(project=endpoint.get("project"), role=endpoint.get("role"))})
            return result
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
            valid = {"codex": {"codex_app_tools"},
                     "qoder": {"qoder_accessibility", "qoder_extension", "qoder_native"},
                     "fixture": {"codex_app_tools", "qoder_accessibility", "qoder_extension", "qoder_native"}}
            if chat is not None and transport not in valid.get(endpoint["adapter"], set()):
                raise ValueError("unsupported_desktop_transport")
            # Binding a role changes routing, so never redirect already accepted work.
            previous = self.desktop.bindings.get(internal)
            with self.cv:
                if self.store.has_pending(internal):
                    raise ValueError("target_has_pending_tasks")
                self.desktop.bind(internal, {"session_id": chat, "transport": transport} if chat else None)
            if previous and previous.get("session_id"):
                self.store.remove_session("desktop:" + previous["session_id"])
            if chat:
                self.store.register_session("desktop:" + chat, endpoint["project"], endpoint["role"],
                                            endpoint["adapter"], transport, "桌面聊天绑定")
            return {"agent": internal, "address": internal, "mode": "desktop_session" if chat else "independent_cli_task",
                    "session_id": chat, "transport": transport}
        if method == "desktop_receive":
            value = p.get("agent", p.get("role"))
            if not isinstance(value, str):
                raise ValueError("target_required")
            internal = self.resolve(value, p.get("project"), required=True)
            binding = self.desktop.bindings.get(internal)
            if not binding or binding["session_id"] != p["session_id"]:
                raise ValueError("desktop_session_not_bound")
            seconds = p.get("seconds", 40)
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 45:
                raise ValueError("invalid_receive_timeout")
            try:
                self.store.heartbeat_session("desktop:" + p["session_id"])
            except ValueError:
                pass
            return self.desktop.receive(internal, p["session_id"], seconds)
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
            sid = session_id(p.get("session"))
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
                    "mode": self.target_mode(internal)}}
        if method == "session_heartbeat":
            return {"session": self.store.heartbeat_session(session_id(p.get("session")))}
        if method == "session_list":
            return {"sessions": self.public_sessions(p.get("project"), p.get("role"),
                                                     p.get("include_offline", True))}
        if method == "session_remove":
            return self.store.remove_session(session_id(p.get("session")))
        if method == "send":
            if self.stop.is_set():
                raise ValueError("broker_stopping")
            allowed = {"sender", "target", "prompt", "key", "timeout", "context", "reply_mode", "queue_ttl", "project"}
            if set(p) - allowed:
                raise ValueError("unknown_send_fields")
            project = p.get("project")
            sender = self.resolve(label(p["sender"]), project, required=False)
            target = self.resolve(label(p["target"]), project, required=True)
            target_endpoint = self.endpoints[target]
            if not target_endpoint.get("enabled", True):
                raise ValueError("target_unavailable")
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
                       "sender_address": sender, "target_address": target}
            # Stable fields only in the idempotency fingerprint. Queue expiration is derived from created.
            with self.cv:
                task_id, duplicate = self.store.enqueue(sender, target, key, payload)
                self.cv.notify_all()
            return {"task_id": task_id, "duplicate": duplicate, "state": self.store.get(task_id)["state"],
                    "sender": sender, "target": target, "mode": self.target_mode(target)}
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

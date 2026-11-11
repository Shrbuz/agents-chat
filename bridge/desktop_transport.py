"""Opt-in desktop transports. Never silently fall back to a new CLI session."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

from .codex_connection import active_connection_id, runtime_problem


RECOVERABLE_CODEX_ERRORS = {
    "desktop_chat_busy", "pipe_missing", "runtime_missing",
    "desktop_bridge_disconnected", "desktop_delivery_timeout",
    "connection_context_missing", "app_tools_server_missing",
}


def classify_codex_error(error, connection=None):
    """Separate chat backpressure from broken app transport/runtime state."""
    text = str(error)
    lowered = text.lower()
    command = (connection or {}).get("command") or []
    if "desktop_chat_busy" in lowered:
        return "desktop_chat_busy"
    if command and not Path(command[0]).is_file():
        return "runtime_missing"
    if len(command) > 1 and not Path(command[1]).is_file():
        return "app_tools_server_missing"
    if ("enoent" in lowered and ("pipe" in lowered or "codex-app-tools" in lowered)):
        return "pipe_missing"
    if "desktop_bridge_disconnected" in lowered:
        return "desktop_bridge_disconnected"
    if "winerror 2" in lowered or "系统找不到指定的文件" in text:
        return "runtime_missing"
    if "codex_connection_context_unavailable" in lowered:
        return "connection_context_missing"
    return "codex_desktop_transport"


class McpProcess:
    """Small JSONL MCP client for the installed, vendor supplied desktop bridge."""
    def __init__(self, command, env=None):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.messages = queue.Queue()
        self.sequence = 0
        self.closed = False

        def read():
            try:
                for line in self.process.stdout:
                    try:
                        self.messages.put(json.loads(line))
                    except ValueError:
                        continue
            finally:
                self.messages.put(None)
        threading.Thread(target=read, daemon=True).start()
        try:
            self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "agents-chat-desktop", "version": "0.2.0"}})
            self.write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            self.close()
            raise

    def write(self, data):
        self.process.stdin.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def request(self, method, params, timeout=60):
        self.sequence += 1
        request_id = self.sequence
        self.write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            try:
                response = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError("desktop_bridge_timeout: delivery may be uncertain") from None
            if response is None:
                raise RuntimeError("desktop_bridge_disconnected: delivery may be uncertain")
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(str(response["error"]))
            return response["result"]

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.process.stdout.close()


def unpack(result):
    texts = [item["text"] for item in result.get("content", []) if item.get("type") == "text"]
    if result.get("isError"):
        raise RuntimeError("desktop_tool_error: " + "\n".join(texts)[:800])
    if len(texts) == 1:
        try:
            return json.loads(texts[0])
        except ValueError:
            pass
    return {"text": "\n".join(texts)}


class CodexDesktop:
    """Uses the existing bundled app-tools MCP, with genuine launcher context.

    This is a version-specific integration, not a public desktop REST API.
    The launcher must receive its own context from Codex; no auth files are read.
    """
    def __init__(self, connection):
        environment = dict(os.environ)
        environment["CODEX_APP_TOOLS_PIPE_PATH"] = connection["pipe"]
        self.origin = connection["origin_thread"]
        self.mcp = McpProcess(connection["command"], environment)

    def call(self, name, arguments, timeout=60):
        return unpack(self.mcp.request("tools/call", {"name": name, "arguments": arguments,
            "_meta": {"openai/threadId": self.origin}}, timeout))

    def close(self):
        self.mcp.close()


class DesktopHub:
    """Local connector rendezvous. Empty receives wait in code, never in a model."""
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "desktop-bindings.json"
        self.transport_path = self.state_dir / "transport-status.json"
        self.cv = threading.Condition()
        self.jobs = {}
        self.closed = False
        self.bindings = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        try:
            persisted = json.loads(self.transport_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            persisted = {}
        self.transport_status = persisted if isinstance(persisted, dict) else {}
        self.transport_epochs = {}
        for item in self.transport_status.values():
            transport = item.get("transport") if isinstance(item, dict) else None
            if transport:
                self.transport_epochs[transport] = max(
                    self.transport_epochs.get(transport, 0), int(item.get("epoch") or 0))

    def _save_transport_status(self):
        temporary = self.transport_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.transport_status, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        os.replace(temporary, self.transport_path)

    def refresh_transport(self, transport, connection_id=None):
        """Invalidate only one connector family and wake its long polls."""
        with self.cv:
            epoch = self.transport_epochs.get(transport, 0) + 1
            self.transport_epochs[transport] = epoch
            now = time.time()
            for agent, binding in self.bindings.items():
                if binding.get("transport") != transport:
                    continue
                previous = self.transport_status.get(agent, {})
                self.transport_status[agent] = {
                    **previous, "agent": agent, "transport": transport,
                    "state": "reconnecting", "connection_id": connection_id,
                    "epoch": epoch, "updated_at": now,
                    "last_error": None, "failure_kind": None,
                }
            self._save_transport_status()
            self.cv.notify_all()
            return epoch

    def report_transport(self, agent, transport, state, *, error=None, summary=None,
                         connection_id=None, delivered=False, failure_kind=None):
        allowed = {"ready", "busy", "reconnecting", "degraded", "offline",
                   "pipe_missing", "runtime_missing", "app_tools_server_missing",
                   "service_unavailable", "session_missing", "auth_required", "cli_missing"}
        if state not in allowed:
            raise ValueError("invalid_transport_state")
        with self.cv:
            binding = self.bindings.get(agent) or {}
            if binding.get("transport") != transport:
                raise ValueError("transport_report_binding_mismatch")
            previous = self.transport_status.get(agent, {})
            failures = int(previous.get("consecutive_failures") or 0)
            if failure_kind and state != "busy":
                failures += 1
            elif state == "ready":
                failures = 0
            now = time.time()
            epoch = self.transport_epochs.get(transport, int(previous.get("epoch") or 0))
            item = {
                **previous, "agent": agent, "transport": transport, "state": state,
                "updated_at": now, "epoch": epoch,
                "connection_id": connection_id or previous.get("connection_id"),
                "consecutive_failures": failures,
                "last_error": str(error or summary)[:500] if (error or summary) else None,
                "failure_kind": failure_kind,
            }
            if delivered:
                item["last_success_at"] = now
                item["consecutive_failures"] = 0
                item["last_error"] = None
                item["failure_kind"] = None
                item["state"] = "ready"
            self.transport_status[agent] = item
            self._save_transport_status()
            self.cv.notify_all()
            return dict(item)

    def connector_epoch(self, transport):
        return self.transport_epochs.get(transport, 0)

    def connection_status(self, agent):
        binding = self.bindings.get(agent) or {}
        transport = binding.get("transport")
        if not transport:
            return {"state": "unbound", "transport": None, "online": False}
        item = dict(self.transport_status.get(agent) or {})
        item.setdefault("transport", transport)
        if transport == "codex_app_tools":
            problem = runtime_problem(self.state_dir, binding.get("session_id"))
            if problem:
                item.update({"state": problem, "online": False, "failure_kind": problem})
                return item
            current = active_connection_id(self.state_dir)
            if current and item.get("connection_id") != current:
                item.update({"state": "reconnecting", "online": False})
                return item
        updated = float(item.get("updated_at") or 0)
        if not updated or time.time() - updated > 95:
            item.update({"state": "offline", "online": False,
                         "failure_kind": item.get("failure_kind") or "connector_heartbeat_stale"})
            return item
        state = item.get("state") or "reconnecting"
        item["online"] = state in ("ready", "busy")
        return item

    def transport_health(self, store, transport="codex_app_tools"):
        roles = []
        for agent, binding in self.bindings.items():
            if binding.get("transport") != transport:
                continue
            item = self.connection_status(agent)
            wakeups = store.wakeup_stats(agent)
            item.update({"agent": agent, "session_id": binding.get("session_id"),
                         "pending_wakeups": wakeups["pending"],
                         "needs_attention": wakeups["needs_attention"],
                         "pending_result_wakeups": wakeups["pending_results"]})
            roles.append(item)
        degraded_states = {"reconnecting", "degraded", "offline", "pipe_missing",
                           "runtime_missing", "app_tools_server_missing",
                           "connection_context_missing", "service_unavailable",
                           "session_missing", "auth_required", "cli_missing"}
        degraded = any(item.get("state") in degraded_states for item in roles)
        failures = sum(int(item.get("consecutive_failures") or 0) for item in roles)
        pending_results = sum(item["pending_result_wakeups"] for item in roles)
        last_success = max((float(item.get("last_success_at") or 0) for item in roles), default=0)
        return {
            "ok": not degraded, "degraded": degraded, "transport": transport,
            "state": ("degraded" if degraded else ("ready" if roles else "unused")),
            "roles": roles, "consecutive_failures": failures,
            "last_success_at": last_success or None,
            "pending_result_wakeups": pending_results,
            "needs_attention": sum(item["needs_attention"] for item in roles),
        }

    def bind(self, agent, binding):
        with self.cv:
            target = (binding or {}).get("session_id")
            # Jobs queued for the previous chat can never be delivered once the
            # role moves. Close them explicitly instead of refusing to reattach:
            # the sender learns the delivery was abandoned, and the deadlock that
            # would otherwise keep a role stuck on an unused session is avoided.
            for job in list(self.jobs.values()):
                if job["agent"] != agent or job.get("session_id") == target:
                    continue
                if job["result"] is None:
                    job["result"] = {"state": "cancelled",
                                     "result": {"error": "desktop_rebind",
                                                "summary": "角色已改绑到另一个会话，这条任务没有送达",
                                                "delivery_uncertain": job["received"]}}
            if any(job["agent"] == agent for job in self.jobs.values()):
                self.cv.notify_all()
            previous = self.bindings.get(agent)
            if binding is None:
                self.bindings.pop(agent, None)
            else:
                self.bindings[agent] = binding
            try:
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.bindings, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary, self.path)
            except Exception:
                if previous is None:
                    self.bindings.pop(agent, None)
                else:
                    self.bindings[agent] = previous
                raise
            self.cv.notify_all()

    @staticmethod
    def _result(state, summary, *, delivered, cancel_requested=False,
                cancel_acknowledged=False, error=None, detached=False, session_id=None,
                notification=False):
        result = {"summary": summary, "run_delivered": bool(delivered),
                  "cancel_requested": bool(cancel_requested),
                  "cancel_acknowledged": bool(cancel_acknowledged)}
        if error:
            result["error"] = error
        if detached:
            result["detached"] = True
        if notification:
            result["notification"] = True
        if session_id:
            result["session_id"] = session_id
        return state, result

    def _request_cancel(self, job, state, error):
        """Ask the connector to stop, and say honestly whether it acknowledged."""
        job["action"] = "cancel"
        job["cancel_requested"] = True
        self.cv.notify_all()
        acknowledged = self.cv.wait_for(lambda: job["cancel_sent"], timeout=2)
        job["cancel_acknowledged"] = bool(acknowledged)
        if not acknowledged:
            # Keep the job so a late desktop_finish is accepted instead of
            # turning an unacknowledged cancel into a second, noisy failure.
            job["detached"] = True
        state, result = self._result(
            state,
            "已请求中断目标会话，但连接器没有在时限内确认。" if not acknowledged
            else "已请求中断目标会话，连接器已确认。",
            delivered=job["received"], cancel_requested=True,
            cancel_acknowledged=acknowledged, error=error,
            detached=not acknowledged, session_id=job["session_id"])
        result["delivery_uncertain"] = bool(job["received"])
        return state, result

    def dispatch(self, task, binding, prompt, cancelled, stop,
                 interrupt_on_timeout=False, delivery_timeout=30, execution_timeout=180):
        """Deliver one task without ever cancelling a chat unless asked to.

        Delivery waiting and target execution are separate clocks.  A notification
        completes as soon as the connector acknowledges the prompt; an ordinary
        task waits for its result, but a timeout only stops the sender waiting.
        Only ``interrupt_on_timeout`` turns that timeout into a cancel request.
        """
        task_id = task["id"]
        delivery_timeout = max(0.1, float(delivery_timeout))
        execution_timeout = max(0.1, float(execution_timeout))
        start = time.monotonic()
        job = {"agent": task["target"], "session_id": binding["session_id"], "task_id": task_id,
               "nonce": str(uuid.uuid4()), "prompt": prompt, "received": False, "result": None,
               "action": "run", "cancel_sent": False, "cancel_requested": False,
               "cancel_acknowledged": False, "detached": False,
               "notify": task["payload"].get("kind") == "agent_chat_wakeup",
               "interrupt_on_timeout": bool(interrupt_on_timeout),
               "delivery_timeout": delivery_timeout,
               "execution_timeout": execution_timeout,
               "received_at": None, "claimed_at": None}
        with self.cv:
            self.jobs[task_id] = job
            self.cv.notify_all()
            execution_deadline = None
            try:
                while not job["result"]:
                    now = time.monotonic()
                    if stop.is_set():
                        # Stopping the hub stops the sender's wait. It is not a
                        # request to kill a chat that a person is still using.
                        if job["received"]:
                            job["detached"] = True
                        return self._result(
                            "interrupted", "中枢已停止等待；目标会话可能仍在继续。",
                            delivered=job["received"], detached=job["received"],
                            error="desktop_interrupted", session_id=binding["session_id"])
                    if cancelled(task_id):
                        return self._request_cancel(job, "cancelled", "desktop_cancelled")
                    if not job["received"]:
                        if now >= start + delivery_timeout:
                            return self._result(
                                "timed_out",
                                "目标聊天在投递时限内没有领取任务；已停止发送方等待，未启动或中断目标执行。",
                                delivered=False, error="desktop_delivery_timeout",
                                session_id=binding["session_id"])
                    else:
                        if execution_deadline is None:
                            execution_deadline = now + execution_timeout
                            job["received_at"] = now
                        if now >= execution_deadline:
                            if job["interrupt_on_timeout"]:
                                return self._request_cancel(job, "timed_out",
                                                            "desktop_execution_timeout")
                            # The sender stops waiting, but the chat is still
                            # working. Never send a cancel action for this.
                            job["detached"] = True
                            return self._result(
                                "timed_out",
                                "等待目标执行超时；已停止发送方等待，目标会话未被打断。",
                                delivered=True, detached=True, error="desktop_execution_timeout",
                                session_id=binding["session_id"])
                    if not job["received"]:
                        next_deadline = start + delivery_timeout
                    else:
                        next_deadline = execution_deadline if execution_deadline is not None else now + 0.2
                    self.cv.wait(timeout=min(0.2, max(0, next_deadline - time.monotonic())))
                return job["result"]["state"], job["result"]["result"]
            finally:
                # Detached jobs stay registered until the connector reports a
                # late result, so that report is not mistaken for an unknown task.
                if not job["detached"]:
                    self.jobs.pop(task_id, None)

    def receive(self, agent, session_id, seconds, transport=None, connector_epoch=None,
                recover_received=False):
        deadline = time.monotonic() + seconds
        with self.cv:
            while not self.closed:
                if transport and connector_epoch is not None:
                    current_epoch = self.transport_epochs.get(transport, 0)
                    if int(connector_epoch) != current_epoch:
                        return {"action": "refresh", "transport": transport,
                                "connector_epoch": current_epoch}
                for job in self.jobs.values():
                    if job["agent"] != agent or job["session_id"] != session_id:
                        continue
                    if job["action"] == "cancel" and not job["cancel_sent"]:
                        job["cancel_sent"] = True
                        self.cv.notify_all()
                        return {k: job.get(k) for k in ("task_id", "nonce", "action", "session_id",
                                                         "notify", "interrupt_on_timeout")}
                    if not job["received"]:
                        job["received"] = True  # Generic connectors never redeliver after an uncertain disconnect.
                        job["claimed_at"] = time.monotonic()
                        return {k: job.get(k) for k in ("task_id", "nonce", "action", "session_id", "prompt",
                                                         "notify", "interrupt_on_timeout")}
                    if (recover_received and job.get("action") == "run" and job.get("result") is None
                            and time.monotonic() - float(job.get("claimed_at") or 0) >= 5):
                        # Only connectors that can inspect the target transcript
                        # opt into this lease recovery.  They must prove the
                        # stable task marker absent before submitting again.
                        job["claimed_at"] = time.monotonic()
                        item = {k: job.get(k) for k in ("task_id", "nonce", "action", "session_id", "prompt",
                                                              "notify", "interrupt_on_timeout")}
                        item["recovery"] = True
                        return item
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cv.wait(remaining)
            return None

    def finish(self, task_id, nonce, state, result):
        with self.cv:
            job = self.jobs.get(task_id)
            if job is None:
                # A closed notification or a timed-out task may report late.
                # That is not a receipt error and must not fail the connector.
                return {"accepted": False, "task_closed": True}
            if job["nonce"] != nonce or not job["received"]:
                raise ValueError("desktop_receipt_mismatch")
            if job.get("detached"):
                job["detached"] = False
                self.jobs.pop(task_id, None)
                return {"accepted": True, "late": True, "task_closed": True}
            if job["result"] is None:
                completed = result if isinstance(result, dict) else {"summary": str(result)}
                completed.setdefault("run_delivered", True)
                completed.setdefault("cancel_requested", False)
                completed.setdefault("cancel_acknowledged", False)
                if job["notify"]:
                    completed.setdefault("notification", True)
                job["result"] = {"state": state, "result": completed}
                self.cv.notify_all()
            return {"accepted": True}

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()

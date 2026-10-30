"""Opt-in desktop transports. Never silently fall back to a new CLI session."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid


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
        self.path = Path(state_dir) / "desktop-bindings.json"
        self.cv = threading.Condition()
        self.jobs = {}
        self.closed = False
        self.bindings = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def bind(self, agent, binding):
        with self.cv:
            if any(job["agent"] == agent for job in self.jobs.values()):
                raise ValueError("desktop_has_inflight_task")
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

    def dispatch(self, task, binding, prompt, cancelled, stop):
        task_id = task["id"]
        deadline = time.monotonic() + task["payload"]["timeout"]
        job = {"agent": task["target"], "session_id": binding["session_id"], "task_id": task_id,
               "nonce": str(uuid.uuid4()), "prompt": prompt, "received": False, "result": None,
               "action": "run", "cancel_sent": False}
        with self.cv:
            self.jobs[task_id] = job
            self.cv.notify_all()
            try:
                while not job["result"]:
                    if stop.is_set() or cancelled(task_id) or time.monotonic() >= deadline:
                        # Termination requests are cooperative; no claim of rollback or process kill.
                        state = "interrupted" if stop.is_set() else "cancelled" if cancelled(task_id) else "timed_out"
                        job["action"] = "cancel"
                        self.cv.notify_all()
                        self.cv.wait_for(lambda: job["cancel_sent"], timeout=2)
                        return state, {"error": "desktop_" + state, "summary": "Desktop execution may continue until interruption is acknowledged.",
                                       "session_id": binding["session_id"], "delivery_uncertain": job["received"]}
                    self.cv.wait(timeout=min(0.2, max(0, deadline - time.monotonic())))
                return job["result"]["state"], job["result"]["result"]
            finally:
                self.jobs.pop(task_id, None)

    def receive(self, agent, session_id, seconds):
        deadline = time.monotonic() + seconds
        with self.cv:
            while not self.closed:
                for job in self.jobs.values():
                    if job["agent"] != agent or job["session_id"] != session_id:
                        continue
                    if job["action"] == "cancel" and not job["cancel_sent"]:
                        job["cancel_sent"] = True
                        self.cv.notify_all()
                        return {k: job[k] for k in ("task_id", "nonce", "action", "session_id")}
                    if not job["received"]:
                        job["received"] = True  # Never redeliver after an uncertain disconnect.
                        return {k: job[k] for k in ("task_id", "nonce", "action", "session_id", "prompt")}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cv.wait(remaining)
            return None

    def finish(self, task_id, nonce, state, result):
        with self.cv:
            job = self.jobs.get(task_id)
            if job is None:
                raise ValueError("desktop_task_no_longer_active")
            if job["nonce"] != nonce or not job["received"]:
                raise ValueError("desktop_receipt_mismatch")
            if job["result"] is None:
                job["result"] = {"state": state, "result": result}
                self.cv.notify_all()
            return {"accepted": True}

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()

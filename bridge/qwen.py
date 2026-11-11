"""Qwen Code daemon discovery and REST/SSE client.

The daemon API is still advertised as experimental by Qwen Code, so this
module is deliberately capability-driven.  It never starts a daemon or creates
a session: a user starts ``qwen serve`` and chooses the exact existing session
that Agents Chat may drive.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import ConfigError


DEFAULT_DAEMON_URL = "http://127.0.0.1:4170"
REQUIRED_FEATURES = {
    "capabilities", "session_status", "session_prompt", "session_events",
    "session_transcript", "non_blocking_prompt", "typed_event_schema",
}


class QwenDaemonError(RuntimeError):
    def __init__(self, kind, message, *, status=None, data=None):
        super().__init__(kind + ": " + str(message))
        self.kind = kind
        self.status = status
        self.data = data


def discover_qwen_command():
    """Return Node plus Qwen's real JS entry, never an npm shell wrapper."""
    explicit = os.environ.get("QWEN_CLI")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_file() and path.suffix.lower() not in (".cmd", ".bat", ".ps1"):
            return [str(path)]
        raise ConfigError("qwen_cli_missing_or_not_native: " + str(path))
    node = shutil.which("node.exe") or shutil.which("node")
    appdata = Path(os.environ.get("APPDATA", ""))
    candidates = [
        appdata / "npm/node_modules/@qwen-code/qwen-code/cli-entry.js",
        Path.home() / ".local/share/pnpm/global/5/node_modules/@qwen-code/qwen-code/cli-entry.js",
        Path("/usr/local/lib/node_modules/@qwen-code/qwen-code/cli-entry.js"),
    ]
    for entry in candidates:
        if node and entry.is_file():
            return [str(Path(node).resolve()), str(entry.resolve())]
    raise ConfigError("runtime_not_found: install the official @qwen-code/qwen-code package")


def classify_qwen_error(value):
    if isinstance(value, QwenDaemonError):
        return value.kind
    text = str(value or "").lower()
    if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text:
        return "qwen_auth_required"
    if "session" in text and ("not found" in text or "session_not_found" in text):
        return "qwen_session_missing"
    if "workspace" in text and ("mismatch" in text or "does not match" in text):
        return "qwen_workspace_mismatch"
    if "capabil" in text and ("missing" in text or "unsupported" in text):
        return "qwen_capability_missing"
    if "busy" in text or "queue" in text and "full" in text:
        return "desktop_chat_busy"
    if ("connection refused" in text or "actively refused" in text or
            "timed out" in text or "urlopen error" in text):
        return "qwen_daemon_unavailable"
    return "qwen_transport"


def _same_path(left, right):
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))
    except (OSError, TypeError, ValueError):
        return False


class QwenEventStream:
    """One SSE subscription opened before a prompt is admitted."""

    def __init__(self, client, session_id, *, last_event_id=None, timeout=60):
        self.client = client
        self.session_id = session_id
        self.events = queue.Queue()
        self.closed = threading.Event()
        self.response = None
        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        request = client.request("/session/" + quote(session_id, safe="") + "/events",
                                 headers=headers)
        try:
            self.response = urlopen(request, timeout=timeout)
        except Exception as exc:
            raise client.error(exc) from None
        self.event_epoch = self.response.headers.get("X-Qwen-Event-Epoch")
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        event_type, event_id, data_lines = "message", None, []
        try:
            while not self.closed.is_set():
                raw = self.response.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        try:
                            payload = json.loads("\n".join(data_lines))
                            if isinstance(payload, dict):
                                payload.setdefault("type", event_type)
                                if event_id is not None:
                                    payload.setdefault("id", event_id)
                            self.events.put(payload)
                        except ValueError as exc:
                            self.events.put(QwenDaemonError(
                                "qwen_transport", "invalid SSE JSON: " + str(exc)))
                    event_type, event_id, data_lines = "message", None, []
                elif line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("id:"):
                    event_id = line[3:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
        except Exception as exc:
            if not self.closed.is_set():
                self.events.put(self.client.error(exc))
        finally:
            self.closed.set()

    def get(self, timeout=1):
        try:
            item = self.events.get(timeout=timeout)
        except queue.Empty:
            if self.closed.is_set():
                raise QwenDaemonError("qwen_transport", "SSE stream closed before terminal event")
            return None
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed.set()
        if self.response is not None:
            try:
                self.response.close()
            except OSError:
                pass
        self.thread.join(timeout=2)


class QwenDaemonClient:
    def __init__(self, base_url=None, token=None):
        self.base_url = (base_url or os.environ.get("QWEN_DAEMON_URL") or
                         DEFAULT_DAEMON_URL).rstrip("/")
        self.token = token if token is not None else os.environ.get("QWEN_DAEMON_TOKEN")

    def request(self, path, *, method="GET", body=None, headers=None):
        request_headers = {"Accept": "application/json", **(headers or {})}
        if self.token:
            request_headers["Authorization"] = "Bearer " + self.token
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        return Request(self.base_url + path, data=data, headers=request_headers, method=method)

    def error(self, exc):
        if isinstance(exc, QwenDaemonError):
            return exc
        if isinstance(exc, HTTPError):
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
            except (OSError, ValueError):
                data = {}
            detail = data.get("error") if isinstance(data, dict) else None
            text = detail or getattr(exc, "reason", None) or str(exc)
            if exc.code in (401, 403):
                kind = "qwen_auth_required"
            elif exc.code == 404:
                kind = "qwen_session_missing"
            elif exc.code == 409 and "busy" in str(text).lower():
                kind = "desktop_chat_busy"
            else:
                kind = classify_qwen_error(text)
            return QwenDaemonError(kind, text, status=exc.code, data=data)
        if isinstance(exc, (URLError, TimeoutError, ConnectionError, OSError)):
            return QwenDaemonError("qwen_daemon_unavailable", str(exc))
        return QwenDaemonError(classify_qwen_error(exc), str(exc))

    def json(self, path, *, method="GET", body=None, timeout=20):
        try:
            with urlopen(self.request(path, method=method, body=body), timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise QwenDaemonError("qwen_transport", "expected JSON object")
                return payload
        except Exception as exc:
            raise self.error(exc) from None

    def send(self, path, *, method="POST", body=None, timeout=20):
        """Send a mutation whose success response may intentionally be 204."""
        try:
            with urlopen(self.request(path, method=method, body=body), timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as exc:
            raise self.error(exc) from None

    def probe(self, session_id=None, cwd=None):
        health = self.json("/health", timeout=10)
        capabilities = self.json("/capabilities", timeout=10)
        features = set(capabilities.get("features") or [])
        missing = sorted(REQUIRED_FEATURES - features)
        if missing:
            raise QwenDaemonError("qwen_capability_missing", ", ".join(missing))
        if "rest" not in (capabilities.get("transports") or []):
            raise QwenDaemonError("qwen_capability_missing", "REST transport")
        status = None
        if session_id:
            status = self.status(session_id)
            if cwd and not _same_path(status.get("workspaceCwd"), cwd):
                raise QwenDaemonError(
                    "qwen_workspace_mismatch",
                    "session workspace %s does not match role cwd %s" %
                    (status.get("workspaceCwd"), str(Path(cwd).resolve())))
        return {"ready": True, "experimental": True, "health": health,
                "version": capabilities.get("qwenCodeVersion"),
                "protocol": (capabilities.get("protocolVersions") or {}).get("current"),
                "workspace": status.get("workspaceCwd") if status else capabilities.get("workspaceCwd"),
                "session": status}

    def status(self, session_id):
        return self.json("/session/" + quote(session_id, safe="") + "/status", timeout=10)

    def transcript(self, session_id, limit=256):
        return self.json("/session/" + quote(session_id, safe="") +
                         "/transcript?direction=backward&limit=" + str(int(limit)), timeout=20)

    def events(self, session_id, *, last_event_id=None, timeout=60):
        return QwenEventStream(self, session_id, last_event_id=last_event_id, timeout=timeout)

    def prompt(self, session_id, text, idempotency_key, *, deadline_ms=30000):
        return self.json("/session/" + quote(session_id, safe="") + "/prompt",
                         method="POST", body={
                             "prompt": [{"type": "text", "text": text}],
                             "idempotencyKey": idempotency_key,
                             "deadlineMs": int(deadline_ms),
                         }, timeout=max(35, int(deadline_ms / 1000) + 5))

    def cancel(self, session_id, prompt_id):
        return self.send("/session/" + quote(session_id, safe="") + "/cancel",
                         method="POST", body={"promptId": prompt_id}, timeout=10)


def _update(event):
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        return {}
    nested = data.get("update")
    return nested if isinstance(nested, dict) else data


def event_prompt_id(event):
    if not isinstance(event, dict):
        return None
    if isinstance(event.get("promptId"), str):
        return event["promptId"]
    data = event.get("data")
    return data.get("promptId") if isinstance(data, dict) else None


def event_agent_text(event):
    if not isinstance(event, dict) or event.get("type") != "session_update":
        return ""
    update = _update(event)
    content = update.get("content")
    if update.get("sessionUpdate") != "agent_message_chunk" or not isinstance(content, dict):
        return ""
    text = content.get("text")
    return text if isinstance(text, str) else ""


def inspect_qwen_delivery(client, session_id, task_id):
    """Find a stable Agents Chat marker and any assistant text after it."""
    marker = "Agents Chat wakeup " + task_id + "."
    task_marker = "Agents Chat task " + task_id + "."
    transcript = client.transcript(session_id)
    seen, texts = False, []
    events = transcript.get("events") if isinstance(transcript, dict) else []
    for event in events if isinstance(events, list) else []:
        update = _update(event)
        content = update.get("content") if isinstance(update, dict) else None
        text = content.get("text") if isinstance(content, dict) else None
        kind = update.get("sessionUpdate") if isinstance(update, dict) else None
        if kind == "user_message_chunk" and isinstance(text, str):
            if marker in text or task_marker in text:
                seen, texts = True, []
            elif seen:
                break
        elif seen and kind == "agent_message_chunk" and isinstance(text, str) and text:
            texts.append(text)
    return {"delivered": seen, "summary": "".join(texts).strip()[:800]}


def qwen_status(session_id=None, cwd=None, url=None):
    client = QwenDaemonClient(url)
    try:
        return {"daemon": client.probe(session_id, cwd), "url": client.base_url}
    except Exception as exc:
        kind = classify_qwen_error(exc)
        return {"daemon": {"ready": False, "experimental": True,
                           "state": kind.removeprefix("qwen_"), "error": str(exc)[:500]},
                "url": client.base_url}

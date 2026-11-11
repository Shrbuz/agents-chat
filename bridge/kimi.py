"""Kimi Code Web discovery plus its experimental REST/WebSocket client.

Kimi Code 2.x exposes a bearer-authenticated local server through ``kimi web``.
The API is explicitly experimental, so this module validates the live OpenAPI
and AsyncAPI documents before an exact session can be bound.  It never starts
the server and never guesses a session id.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import select
import shutil
import socket
import struct
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .config import ConfigError


DEFAULT_WEB_URL = "http://127.0.0.1:58627"
REQUIRED_PATHS = {
    "/api/v1/meta",
    "/api/v1/sessions/{session_id}",
    "/api/v1/sessions/{session_id}/prompts",
    "/api/v1/sessions/{session_id}/prompts/{tail}",
    "/api/v1/sessions/{session_id}/messages",
    "/api/v1/sessions/{session_id}/transcript",
}


class KimiWebError(RuntimeError):
    def __init__(self, kind, message, *, status=None, code=None, data=None):
        super().__init__(kind + ": " + str(message))
        self.kind = kind
        self.status = status
        self.code = code
        self.data = data


def discover_kimi_command():
    """Return Node plus Kimi Code's real module, never an npm shell wrapper."""
    explicit = os.environ.get("KIMI_CLI")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_file() and path.suffix.lower() not in (".cmd", ".bat", ".ps1"):
            return [str(path)]
        raise ConfigError("kimi_cli_missing_or_not_native: " + str(path))
    node = shutil.which("node.exe") or shutil.which("node")
    appdata = Path(os.environ.get("APPDATA", ""))
    candidates = [
        appdata / "npm/node_modules/@moonshot-ai/kimi-code/dist/main.mjs",
        Path.home() / ".local/share/pnpm/global/5/node_modules/@moonshot-ai/kimi-code/dist/main.mjs",
        Path("/usr/local/lib/node_modules/@moonshot-ai/kimi-code/dist/main.mjs"),
    ]
    for entry in candidates:
        if node and entry.is_file():
            return [str(Path(node).resolve()), str(entry.resolve())]
    raise ConfigError("runtime_not_found: install the official @moonshot-ai/kimi-code package")


def _kimi_home():
    return Path(os.environ.get("KIMI_CODE_HOME") or (Path.home() / ".kimi-code")).expanduser()


def discover_kimi_urls():
    """Newest registered loopback instances first, then the documented default."""
    explicit = os.environ.get("KIMI_WEB_URL")
    if explicit:
        return [explicit.rstrip("/")]
    found = []
    instances = _kimi_home() / "server/instances"
    if instances.is_dir():
        for path in instances.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                host, port = item.get("host"), int(item.get("port"))
                heartbeat = int(item.get("heartbeat_at") or 0)
                if host in ("127.0.0.1", "localhost", "::1") and 0 < port < 65536:
                    found.append((heartbeat, "http://%s:%d" % (host, port)))
            except (OSError, ValueError, TypeError):
                continue
    urls = [url for _heartbeat, url in sorted(found, reverse=True)]
    if DEFAULT_WEB_URL not in urls:
        urls.append(DEFAULT_WEB_URL)
    return urls


def discover_kimi_token():
    explicit = os.environ.get("KIMI_WEB_TOKEN")
    if explicit:
        return explicit.strip()
    path = _kimi_home() / "server.token"
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        raise KimiWebError(
            "kimi_auth_required",
            "Kimi server token is missing; start the official `kimi web` server after login")
    return token


def classify_kimi_error(value):
    if isinstance(value, KimiWebError):
        return value.kind
    text = str(value or "").lower()
    if "subscription does not have access" in text or "upgrade your plan" in text:
        return "kimi_entitlement_required"
    if "model not set" in text or "model.not_configured" in text or "model not configured" in text:
        return "kimi_model_not_configured"
    if "401" in text or "403" in text or "unauthorized" in text or "authentication" in text:
        return "kimi_auth_required"
    if "session" in text and ("not found" in text or "40401" in text):
        return "kimi_session_missing"
    if "workspace" in text and ("mismatch" in text or "does not match" in text):
        return "kimi_workspace_mismatch"
    if "capabil" in text and ("missing" in text or "unsupported" in text):
        return "kimi_capability_missing"
    if "busy" in text or "occupied" in text or "40901" in text or "40931" in text:
        return "desktop_chat_busy"
    if ("connection refused" in text or "actively refused" in text or
            "timed out" in text or "urlopen error" in text or "websocket closed" in text):
        return "kimi_service_unavailable"
    return "kimi_transport"


def _same_path(left, right):
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))
    except (OSError, TypeError, ValueError):
        return False


class _WebSocket:
    """Small RFC 6455 client for Kimi's loopback JSON event stream."""

    def __init__(self, base_url, token, timeout=15):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise KimiWebError(
                "kimi_capability_missing",
                "kimi_native accepts only a bearer-authenticated loopback http server")
        host, port = parsed.hostname, parsed.port or 80
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.closed = False
        self.buffer = b""
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        host_header = "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)
        protocol = "kimi-code.bearer." + token
        request = (
            "GET /api/v1/ws HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: %s\r\n\r\n" % (host_header, key, protocol)
        ).encode("ascii")
        self.sock.sendall(request)
        header = self._read_until(b"\r\n\r\n", 65536)
        first = header.split(b"\r\n", 1)[0]
        if b" 101 " not in first:
            self.close()
            raise KimiWebError("kimi_auth_required", "WebSocket upgrade failed: " +
                               first.decode("ascii", errors="replace"))
        headers = {}
        for line in header.split(b"\r\n")[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest())
        if headers.get(b"sec-websocket-accept") != expected:
            self.close()
            raise KimiWebError("kimi_transport", "invalid WebSocket accept header")
        self.sock.settimeout(None)

    def _read_until(self, delimiter, limit):
        while delimiter not in self.buffer:
            data = self.sock.recv(8192)
            if not data:
                raise KimiWebError("kimi_service_unavailable", "WebSocket closed during handshake")
            self.buffer += data
            if len(self.buffer) > limit:
                raise KimiWebError("kimi_transport", "WebSocket handshake exceeded limit")
        before, self.buffer = self.buffer.split(delimiter, 1)
        return before + delimiter

    def _read_exact(self, count):
        while len(self.buffer) < count:
            data = self.sock.recv(max(8192, count - len(self.buffer)))
            if not data:
                self.closed = True
                raise KimiWebError("kimi_service_unavailable", "WebSocket closed")
            self.buffer += data
        value, self.buffer = self.buffer[:count], self.buffer[count:]
        return value

    def _send_frame(self, opcode, payload=b""):
        if self.closed:
            return
        mask = secrets.token_bytes(4)
        size = len(payload)
        if size < 126:
            header = bytes((0x80 | opcode, 0x80 | size))
        elif size <= 65535:
            header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", size)
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", size)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_json(self, value):
        self._send_frame(1, json.dumps(value, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8"))

    def receive_json(self, timeout=1):
        deadline = time.monotonic() + timeout
        fragments, fragment_opcode = [], None
        while not self.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if len(self.buffer) < 2:
                readable, _, _ = select.select([self.sock], [], [], remaining)
                if not readable:
                    return None
            first, second = self._read_exact(2)
            final, opcode = bool(first & 0x80), first & 0x0f
            masked, size = bool(second & 0x80), second & 0x7f
            if size == 126:
                size = struct.unpack("!H", self._read_exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read_exact(8))[0]
            if size > 16 * 1024 * 1024:
                raise KimiWebError("kimi_transport", "WebSocket frame exceeded limit")
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(size)
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 8:
                self.closed = True
                raise KimiWebError("kimi_service_unavailable", "WebSocket closed")
            if opcode == 9:
                self._send_frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode in (1, 2):
                fragments, fragment_opcode = [payload], opcode
            elif opcode == 0 and fragment_opcode is not None:
                fragments.append(payload)
            else:
                continue
            if not final:
                continue
            raw = b"".join(fragments)
            fragments, fragment_opcode = [], None
            if opcode == 2:
                raise KimiWebError("kimi_transport", "unexpected binary WebSocket frame")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise KimiWebError("kimi_transport", "invalid WebSocket JSON: " + str(exc)) from None
            if not isinstance(value, dict):
                raise KimiWebError("kimi_transport", "expected WebSocket JSON object")
            return value
        return None

    def close(self):
        if not self.closed:
            try:
                self._send_frame(8, struct.pack("!H", 1000))
            except OSError:
                pass
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


class KimiEventStream:
    """A subscribe_v2 stream established before prompt admission."""

    def __init__(self, client, session_id, timeout=15):
        self.socket = _WebSocket(client.base_url, client.token, timeout=timeout)
        self.pending = []
        hello = self.socket.receive_json(timeout=timeout)
        protocol = ((hello or {}).get("payload") or {}).get("protocol_version")
        if not hello or hello.get("type") != "server_hello" or protocol != 2:
            self.close()
            raise KimiWebError("kimi_capability_missing", "WebSocket protocol v2 is required")
        request_id = "agents-chat-subscribe-" + secrets.token_hex(8)
        self.socket.send_json({
            "type": "subscribe_v2", "id": request_id,
            "payload": {"session_id": session_id, "transcript": {"*": "delta"}},
        })
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.socket.receive_json(timeout=max(0.05, deadline - time.monotonic()))
            if not event:
                continue
            if event.get("type") == "ack" and event.get("id") == request_id:
                if event.get("code") != 0:
                    self.close()
                    raise KimiWebError("kimi_capability_missing", event.get("msg") or "subscribe failed",
                                       code=event.get("code"), data=event.get("payload"))
                return
            self.pending.append(event)
        self.close()
        raise KimiWebError("kimi_service_unavailable", "subscribe_v2 acknowledgement timed out")

    def get(self, timeout=1):
        if self.pending:
            return self.pending.pop(0)
        return self.socket.receive_json(timeout=timeout)

    def close(self):
        self.socket.close()


class KimiWebClient:
    def __init__(self, base_url=None, token=None):
        self.base_url = (base_url or discover_kimi_urls()[0]).rstrip("/")
        self.token = token if token is not None else discover_kimi_token()
        self._capability_checked = False

    def request(self, path, *, method="GET", body=None, headers=None):
        request_headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + self.token,
            **(headers or {}),
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        return Request(self.base_url + path, data=data, headers=request_headers, method=method)

    def error(self, exc):
        if isinstance(exc, KimiWebError):
            return exc
        if isinstance(exc, HTTPError):
            try:
                data = json.loads(exc.read().decode("utf-8", errors="replace"))
            except (OSError, ValueError):
                data = {}
            code = data.get("code") if isinstance(data, dict) else None
            message = data.get("msg") if isinstance(data, dict) else None
            kind = "kimi_auth_required" if exc.code in (401, 403) else classify_kimi_error(message or exc)
            return KimiWebError(kind, message or str(exc), status=exc.code, code=code, data=data)
        if isinstance(exc, (URLError, TimeoutError, ConnectionError, OSError)):
            return KimiWebError("kimi_service_unavailable", str(exc))
        return KimiWebError(classify_kimi_error(exc), str(exc))

    def raw_json(self, path, *, method="GET", body=None, timeout=20):
        try:
            with urlopen(self.request(path, method=method, body=body), timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise KimiWebError("kimi_transport", "expected JSON object")
                return payload
        except Exception as exc:
            raise self.error(exc) from None

    def data(self, path, *, method="GET", body=None, timeout=20):
        envelope = self.raw_json(path, method=method, body=body, timeout=timeout)
        if envelope.get("code") != 0:
            code, message = envelope.get("code"), envelope.get("msg") or "Kimi request failed"
            if code == 40401:
                kind = "kimi_session_missing"
            elif code in (40901, 40931):
                kind = "desktop_chat_busy"
            elif code in (40101,):
                kind = "kimi_auth_required"
            elif code in (40110, 40113):
                kind = "kimi_model_not_configured"
            else:
                kind = classify_kimi_error(message)
            raise KimiWebError(kind, message, code=code, data=envelope.get("data"))
        return envelope.get("data")

    def _check_capabilities(self):
        if self._capability_checked:
            return
        meta = self.data("/api/v1/meta", timeout=10)
        capabilities = meta.get("capabilities") if isinstance(meta, dict) else None
        if not isinstance(capabilities, dict) or capabilities.get("websocket") is not True:
            raise KimiWebError("kimi_capability_missing", "WebSocket capability")
        openapi = self.raw_json("/openapi.json", timeout=10)
        paths = set((openapi.get("paths") or {}).keys())
        missing = sorted(REQUIRED_PATHS - paths)
        if missing:
            raise KimiWebError("kimi_capability_missing", ", ".join(missing))
        asyncapi = self.raw_json("/asyncapi.json", timeout=10)
        messages = (((asyncapi.get("components") or {}).get("messages")) or {})
        if "subscribe_v2" not in messages or "session_event" not in messages:
            raise KimiWebError("kimi_capability_missing", "subscribe_v2/session_event schema")
        self._capability_checked = True

    def probe(self, session_id=None, cwd=None):
        health = self.data("/api/v1/healthz", timeout=10)
        self._check_capabilities()
        meta = self.data("/api/v1/meta", timeout=10)
        session = self.session(session_id) if session_id else None
        if session is not None:
            workspace = (session.get("metadata") or {}).get("cwd")
            if cwd and not _same_path(workspace, cwd):
                raise KimiWebError(
                    "kimi_workspace_mismatch",
                    "session workspace %s does not match role cwd %s" %
                    (workspace, str(Path(cwd).resolve())))
            if session.get("archived"):
                raise KimiWebError("kimi_session_missing", "session is archived")
            model = (session.get("agent_config") or {}).get("model")
            if not isinstance(model, str) or not model.strip():
                raise KimiWebError("kimi_model_not_configured", "session model is empty")
            transcript = self.transcript(session_id, limit=10)
            turns = [item for item in (transcript.get("items") or []) if
                     isinstance(item, dict) and item.get("kind") == "turn"]
            latest = turns[-1] if turns else None
            if latest and latest.get("state") == "failed":
                error = latest.get("error") or "latest turn failed"
                kind = classify_kimi_error(error)
                if kind in ("kimi_entitlement_required", "kimi_model_not_configured",
                            "kimi_auth_required"):
                    raise KimiWebError(kind, error)
        return {
            "ready": True,
            "experimental": True,
            "health": health,
            "version": meta.get("server_version"),
            "protocol": 2,
            "server_id": meta.get("server_id"),
            "workspace": ((session or {}).get("metadata") or {}).get("cwd"),
            "session": session,
        }

    def session(self, session_id):
        return self.data("/api/v1/sessions/" + quote(session_id, safe=""), timeout=10)

    def messages(self, session_id, limit=100):
        return self.data("/api/v1/sessions/" + quote(session_id, safe="") +
                         "/messages?page_size=" + str(max(1, min(100, int(limit)))), timeout=20)

    def transcript(self, session_id, limit=20):
        return self.data("/api/v1/sessions/" + quote(session_id, safe="") +
                         "/transcript?agent_id=main&page_size=" +
                         str(max(1, min(100, int(limit)))), timeout=20)

    def events(self, session_id, timeout=15):
        return KimiEventStream(self, session_id, timeout=timeout)

    def prompt(self, session_id, text):
        return self.data("/api/v1/sessions/" + quote(session_id, safe="") + "/prompts",
                         method="POST", body={
                             "content": [{"type": "text", "text": text}],
                             "permission_mode": "auto",
                         }, timeout=30)

    def cancel(self, session_id, prompt_id):
        return self.data("/api/v1/sessions/" + quote(session_id, safe="") +
                         "/prompts/" + quote(prompt_id, safe="") + ":abort",
                         method="POST", body={}, timeout=10)


def event_terminal_turn(event, prompt_id):
    if not isinstance(event, dict) or event.get("type") != "transcript.ops":
        return None
    payload = event.get("payload")
    ops = payload.get("ops") if isinstance(payload, dict) else None
    for op in ops if isinstance(ops, list) else []:
        turn = op.get("turn") if isinstance(op, dict) and op.get("op") == "turn.upsert" else None
        if (isinstance(turn, dict) and turn.get("triggerPromptId") == prompt_id and
                turn.get("state") in ("completed", "failed", "cancelled")):
            return turn
    return None


def _message_text(message):
    parts = message.get("content") if isinstance(message, dict) else None
    return "".join(part.get("text", "") for part in parts if
                   isinstance(part, dict) and part.get("type") == "text" and
                   isinstance(part.get("text"), str)) if isinstance(parts, list) else ""


def inspect_kimi_delivery(client, session_id, task_id):
    """Find a stable Agents Chat marker and assistant text after it."""
    markers = ("Agents Chat wakeup " + task_id + ".", "Agents Chat task " + task_id + ".")
    page = client.messages(session_id)
    items = page.get("items") if isinstance(page, dict) else []
    seen, texts = False, []
    for message in reversed(items if isinstance(items, list) else []):
        role, text = message.get("role"), _message_text(message)
        origin = ((message.get("metadata") or {}).get("origin") or {}).get("kind")
        if role == "user" and origin != "injection":
            if any(marker in text for marker in markers):
                seen, texts = True, []
            elif seen:
                break
        elif seen and role == "assistant" and text:
            texts.append(text)
    return {"delivered": seen, "summary": "".join(texts).strip()[:800]}


def kimi_status(session_id=None, cwd=None, url=None):
    candidates = [url.rstrip("/")] if url else discover_kimi_urls()
    failures = []
    for candidate in candidates:
        try:
            client = KimiWebClient(candidate)
            return {"server": client.probe(session_id, cwd), "url": client.base_url}
        except Exception as exc:
            failures.append((candidate, exc))
            if url:
                break
    candidate, exc = failures[-1] if failures else (url or DEFAULT_WEB_URL, "no instance")
    kind = classify_kimi_error(exc)
    return {
        "server": {"ready": False, "experimental": True,
                   "state": kind.removeprefix("kimi_"), "error": str(exc)[:500]},
        "url": candidate,
    }

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bridge.cli import parser
from bridge.config import adapter_for_transport, default_transport, transport_supported
from bridge.kimi import (KimiWebClient, REQUIRED_PATHS, classify_kimi_error,
                         event_terminal_turn, inspect_kimi_delivery)
import kimi_desktop_link


class _ServerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    workspace = ""
    prompt_body = None
    messages = []

    def log_message(self, *_args):
        pass

    def _json(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ok(self, data):
        self._json({"code": 0, "msg": "success", "data": data,
                    "request_id": "req-test"})

    def do_GET(self):
        if self.path == "/api/v1/healthz":
            self._ok({"ok": True})
        elif self.path == "/api/v1/meta":
            self._ok({"server_version": "2.5.3", "server_id": "server-test",
                      "capabilities": {"websocket": True}})
        elif self.path == "/openapi.json":
            self._json({"openapi": "3.1.0", "paths": {path: {} for path in REQUIRED_PATHS}})
        elif self.path == "/asyncapi.json":
            self._json({"asyncapi": "3.0.0", "components": {"messages": {
                "subscribe_v2": {}, "session_event": {}}}})
        elif self.path == "/api/v1/sessions/session-kimi":
            self._ok({"id": "session-kimi", "title": "Kimi test", "archived": False,
                      "busy": False, "metadata": {"cwd": self.workspace},
                      "agent_config": {"model": "kimi-code/kimi-for-coding"}})
        elif self.path.startswith("/api/v1/sessions/session-kimi/messages"):
            self._ok({"items": list(self.messages), "has_more": False})
        elif self.path.startswith("/api/v1/sessions/session-kimi/transcript"):
            self._ok({"agent_id": "main", "items": [], "has_more": False})
        else:
            self._json({"code": 40401, "msg": "session not found", "data": None,
                        "request_id": "req-test"})

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size) or b"{}")
        if self.path == "/api/v1/sessions/session-kimi/prompts":
            type(self).prompt_body = body
            self._ok({"prompt_id": "prompt-kimi-1", "user_message_id": "user-1",
                      "status": "running", "content": body["content"]})
        elif self.path == "/api/v1/sessions/session-kimi/prompts/prompt-kimi-1:abort":
            self._ok({"aborted": True})
        else:
            self._json({"code": 40401, "msg": "session not found", "data": None,
                        "request_id": "req-test"})


class _FakeStream:
    def __init__(self, event):
        self.event = event
        self.closed = False

    def get(self, timeout=1):
        event, self.event = self.event, None
        return event

    def close(self):
        self.closed = True


class _FakeKimi:
    def __init__(self):
        self.subscribed = False
        self.prompted = False
        self.stream = _FakeStream({
            "type": "transcript.ops", "payload": {"seq": 17, "ops": [{
                "op": "turn.upsert", "turn": {"state": "completed",
                "triggerPromptId": "prompt-kimi-1", "turnId": "t0"}}]}})

    def events(self, session_id, timeout=15):
        self.subscribed = True
        return self.stream

    def prompt(self, session_id, text):
        if not self.subscribed:
            raise AssertionError("prompt admitted before subscribe_v2")
        self.prompted = True
        self.text = text
        return {"prompt_id": "prompt-kimi-1"}

    def messages(self, session_id, limit=100):
        if not self.prompted:
            return {"items": [], "has_more": False}
        return {"items": [
            {"role": "assistant", "content": [{"type": "text", "text": "KIMI_NATIVE_OK"}]},
            {"role": "user", "metadata": {"origin": {"kind": "injection"}},
             "content": [{"type": "text", "text": "system reminder"}]},
            {"role": "user", "metadata": {"origin": {"kind": "user"}},
             "content": [{"type": "text", "text": "Agents Chat task task-1.\nTEST"}]},
        ], "has_more": False}

    def cancel(self, session_id, prompt_id):
        return {"aborted": True}


class _FakeHubClient:
    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "desktop_receive":
            return None
        return {"accepted": True}


class KimiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        _ServerHandler.workspace = str(Path(self.temp.name).resolve())
        _ServerHandler.prompt_body = None
        _ServerHandler.messages = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ServerHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def test_transport_mapping_and_status_parser(self):
        self.assertEqual(adapter_for_transport("kimi_native"), "kimi")
        self.assertEqual(default_transport("kimi"), "kimi_native")
        self.assertTrue(transport_supported("kimi", "kimi_native"))
        args = parser().parse_args(["kimi", "status", "--session", "session-kimi",
                                    "--url", self.url])
        self.assertEqual((args.command, args.kimi_command, args.session),
                         ("kimi", "status", "session-kimi"))

    def test_probe_prompt_cancel_and_capability_contract(self):
        client = KimiWebClient(self.url, "test-token")
        probe = client.probe("session-kimi", self.temp.name)
        self.assertTrue(probe["ready"])
        self.assertTrue(probe["experimental"])
        self.assertEqual(probe["protocol"], 2)
        admitted = client.prompt("session-kimi", "hello")
        self.assertEqual(admitted["prompt_id"], "prompt-kimi-1")
        self.assertEqual(_ServerHandler.prompt_body["permission_mode"], "auto")
        self.assertTrue(client.cancel("session-kimi", admitted["prompt_id"])["aborted"])

    def test_connector_subscribes_before_prompt_and_correlates_terminal(self):
        hub, server = _FakeHubClient(), _FakeKimi()
        job = {"action": "run", "session_id": "session-kimi", "task_id": "task-1",
               "nonce": "nonce-1", "prompt": "Agents Chat task task-1.\nTEST",
               "notify": False, "recovery": False, "delivery_timeout": 5,
               "execution_timeout": 10}
        kimi_desktop_link.execute(hub, server, self.temp.name, job, "demo.connectors")
        finish = next(params for method, params in hub.calls if method == "desktop_finish")
        self.assertEqual(finish["state"], "succeeded")
        self.assertEqual(finish["result"]["summary"], "KIMI_NATIVE_OK")
        self.assertEqual(finish["result"]["prompt_id"], "prompt-kimi-1")
        self.assertEqual(finish["result"]["event_sequence"], 17)
        self.assertTrue(server.stream.closed)

    def test_transcript_marker_ignores_injected_user_messages(self):
        _ServerHandler.messages = [
            {"role": "assistant", "content": [{"type": "text", "text": "already done"}]},
            {"role": "user", "metadata": {"origin": {"kind": "injection"}},
             "content": [{"type": "text", "text": "date reminder"}]},
            {"role": "user", "metadata": {"origin": {"kind": "user"}},
             "content": [{"type": "text", "text": "Agents Chat task task-7.\nDo it"}]},
        ]
        result = inspect_kimi_delivery(
            KimiWebClient(self.url, "test-token"), "session-kimi", "task-7")
        self.assertEqual(result, {"delivered": True, "summary": "already done"})

    def test_terminal_and_entitlement_classification(self):
        event = {"type": "transcript.ops", "payload": {"ops": [{
            "op": "turn.upsert", "turn": {"triggerPromptId": "prompt-1",
            "state": "failed", "error": "upgrade your plan"}}]}}
        self.assertEqual(event_terminal_turn(event, "prompt-1")["state"], "failed")
        self.assertEqual(classify_kimi_error(
            "Your current subscription does not have access; upgrade your plan"),
            "kimi_entitlement_required")


if __name__ == "__main__":
    unittest.main()

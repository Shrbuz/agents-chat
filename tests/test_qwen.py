import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bridge.cli import parser
from bridge.config import adapter_for_transport, default_transport, transport_supported
from bridge.qwen import (QwenDaemonClient, event_agent_text, event_prompt_id,
                         inspect_qwen_delivery)
import qwen_desktop_link


class _DaemonHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    workspace = ""
    prompt_event = threading.Event()
    transcript_events = []
    prompt_body = None
    prompt_id = "prompt-qwen-1"

    def log_message(self, *_args):
        pass

    def _json(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._json({"status": "ok"})
        elif self.path == "/capabilities":
            self._json({
                "qwenCodeVersion": "0.27.2", "transports": ["rest"],
                "protocolVersions": {"current": "v1"},
                "features": [
                    "capabilities", "session_status", "session_prompt", "session_events",
                    "session_transcript", "non_blocking_prompt", "typed_event_schema",
                ],
            })
        elif self.path == "/session/session-qwen/status":
            self._json({"sessionId": "session-qwen", "workspaceCwd": self.workspace,
                        "hasActivePrompt": False, "clientCount": 1})
        elif self.path.startswith("/session/session-qwen/transcript"):
            self._json({"v": 1, "sessionId": "session-qwen",
                        "events": list(self.transcript_events), "hasMore": False})
        elif self.path.startswith("/session/session-qwen/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Qwen-Event-Epoch", "epoch-test")
            self.end_headers()
            self.wfile.flush()
            if self.prompt_event.wait(5):
                events = [
                    {"id": 11, "v": 1, "type": "session_update",
                     "promptId": self.prompt_id,
                     "data": {"promptId": self.prompt_id, "update": {
                         "sessionUpdate": "agent_message_chunk",
                         "content": {"type": "text", "text": "QWEN_NATIVE_OK"}}}},
                    {"id": 12, "v": 1, "type": "turn_complete",
                     "promptId": self.prompt_id,
                     "data": {"promptId": self.prompt_id, "sessionId": "session-qwen",
                              "stopReason": "end_turn"}},
                ]
                for event in events:
                    frame = ("id: %s\nevent: %s\ndata: %s\n\n" %
                             (event["id"], event["type"], json.dumps(event))).encode()
                    self.wfile.write(frame)
                    self.wfile.flush()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size) or b"{}")
        if self.path == "/session/session-qwen/prompt":
            type(self).prompt_body = body
            self._json({"promptId": self.prompt_id, "lastEventId": 10,
                        "eventEpoch": "epoch-test"}, 202)
            self.prompt_event.set()
        elif self.path == "/session/session-qwen/cancel":
            self.send_response(204)
            self.end_headers()
        else:
            self._json({"error": "not found"}, 404)


class _FakeHubClient:
    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "desktop_receive":
            return None
        return {"accepted": True}


class QwenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        _DaemonHandler.workspace = str(Path(self.temp.name).resolve())
        _DaemonHandler.prompt_event = threading.Event()
        _DaemonHandler.prompt_body = None
        _DaemonHandler.transcript_events = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _DaemonHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def test_transport_mapping_and_status_parser(self):
        self.assertEqual(adapter_for_transport("qwen_native"), "qwen")
        self.assertEqual(default_transport("qwen"), "qwen_native")
        self.assertTrue(transport_supported("qwen", "qwen_native"))
        args = parser().parse_args(["qwen", "status", "--session", "session-qwen",
                                    "--url", self.url])
        self.assertEqual((args.command, args.qwen_command, args.session),
                         ("qwen", "status", "session-qwen"))

    def test_probe_subscribe_before_prompt_and_correlate_terminal(self):
        client = QwenDaemonClient(self.url)
        probe = client.probe("session-qwen", self.temp.name)
        self.assertTrue(probe["ready"])
        self.assertTrue(probe["experimental"])
        stream = client.events("session-qwen", timeout=10)
        try:
            admitted = client.prompt("session-qwen", "hello", "agents-chat:task-1")
            seen = []
            while len(seen) < 2:
                event = stream.get(timeout=5)
                if event and event_prompt_id(event) == admitted["promptId"]:
                    seen.append(event)
            self.assertEqual(event_agent_text(seen[0]), "QWEN_NATIVE_OK")
            self.assertEqual(seen[1]["type"], "turn_complete")
            self.assertEqual(_DaemonHandler.prompt_body["idempotencyKey"],
                             "agents-chat:task-1")
        finally:
            stream.close()

    def test_connector_returns_only_matching_turn_complete(self):
        hub = _FakeHubClient()
        daemon = QwenDaemonClient(self.url)
        job = {"action": "run", "session_id": "session-qwen", "task_id": "task-1",
               "nonce": "nonce-1", "prompt": "Agents Chat task task-1.\nTEST",
               "notify": False, "recovery": False, "delivery_timeout": 5,
               "execution_timeout": 10}
        qwen_desktop_link.execute(hub, daemon, self.temp.name, job, "demo.connectors")
        finish = next(params for method, params in hub.calls if method == "desktop_finish")
        self.assertEqual(finish["state"], "succeeded")
        self.assertEqual(finish["result"]["summary"], "QWEN_NATIVE_OK")
        self.assertEqual(finish["result"]["prompt_id"], _DaemonHandler.prompt_id)
        self.assertEqual(finish["result"]["event_id"], 12)

    def test_transcript_marker_prevents_duplicate_delivery(self):
        _DaemonHandler.transcript_events = [
            {"type": "session_update", "data": {"sessionUpdate": "user_message_chunk",
             "content": {"type": "text", "text": "Agents Chat task task-7.\nDo it"}}},
            {"type": "session_update", "data": {"sessionUpdate": "agent_message_chunk",
             "content": {"type": "text", "text": "already done"}}},
        ]
        result = inspect_qwen_delivery(QwenDaemonClient(self.url), "session-qwen", "task-7")
        self.assertEqual(result, {"delivered": True, "summary": "already done"})


if __name__ == "__main__":
    unittest.main()

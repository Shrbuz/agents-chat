import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.broker import Broker
from bridge.cli import parser
from bridge.config import adapter_for_transport, default_transport, transport_supported
from bridge.desktop_transport import DesktopHub
from bridge.mcp import serve_mcp, tools_for
from bridge.onboarding import endpoint_for
from bridge.opencode import (discover_opencode_command, opencode_mcp_command,
                             parse_opencode_stream)
from bridge.runtime import command_for
import opencode_desktop_link


class _Input:
    def __init__(self, requests):
        self.buffer = io.BytesIO(("\n".join(json.dumps(item) for item in requests) + "\n").encode())


class _McpClient:
    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        return {"method": method, **params}


class _ConnectorClient:
    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        return {"accepted": True}


class OpenCodeTests(unittest.TestCase):
    def test_transport_mapping_and_runtime_command(self):
        self.assertEqual(adapter_for_transport("opencode_native"), "opencode")
        self.assertEqual(default_transport("opencode"), "opencode_native")
        self.assertTrue(transport_supported("opencode", "opencode_native"))
        endpoint = {"adapter": "opencode", "command": ["opencode.exe"], "args": []}
        self.assertEqual(command_for(endpoint, Path("run")),
                         ["opencode.exe", "run", "--format", "json", "--auto"])

    def test_discovers_explicit_native_cli_and_endpoint_is_push_capable(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "opencode.exe"
            executable.write_bytes(b"")
            with patch.dict(os.environ, {"OPENCODE_CLI": str(executable)}):
                self.assertEqual(discover_opencode_command(), [str(executable.resolve())])
                endpoint = endpoint_for("opencode", Path(temp))
            self.assertEqual(endpoint["mode"], "independent_cli_task")

    def test_mcp_command_uses_native_transport_and_parser_exposes_setup(self):
        command = opencode_mcp_command(["AgentsChat.Cli.exe"], "state", "config.json",
                                       "demo", "review")
        self.assertEqual(command[-4:], ["--agent", "demo.review", "--transport", "opencode_native"])
        args = parser().parse_args(["opencode", "setup", "--project", "demo", "--role", "review"])
        self.assertEqual((args.command, args.opencode_command, args.project_local, args.role),
                         ("opencode", "setup", "demo", "review"))
        register = next(tool for tool in tools_for("demo.review", "opencode_native")
                        if tool["name"] == "bridge_register")
        self.assertEqual(register["inputSchema"]["required"], [])

    def test_mcp_register_binds_real_host_session_and_heartbeats_it(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "bridge_register", "arguments": {},
                "_meta": {"ai.opencode/sessionID": "ses_real_1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "bridge_targets", "arguments": {},
                "_meta": {"ai.opencode/sessionID": "ses_real_1"}}},
        ]
        client, output = _McpClient(), io.StringIO()
        with patch.object(sys, "stdin", _Input(requests)), patch.object(sys, "stdout", output):
            serve_mcp(client, "demo.review", "demo", "opencode_native")
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(client.calls[0], ("desktop_bind", {
            "agent": "demo.review", "project": "demo", "session_id": "ses_real_1",
            "transport": "opencode_native"}))
        self.assertEqual(client.calls[1][0], "desktop_heartbeat")
        self.assertEqual(client.calls[2][0], "targets")

    def test_mcp_rejects_missing_or_invented_session(self):
        for arguments, metadata, expected in (
                ({}, {}, "context_missing"),
                ({"session": "fake"}, {"ai.opencode/sessionID": "real"}, "session_mismatch")):
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "bridge_register", "arguments": arguments, "_meta": metadata}}
            output = io.StringIO()
            with patch.object(sys, "stdin", _Input([request])), patch.object(sys, "stdout", output):
                serve_mcp(_McpClient(), "demo.review", "demo", "opencode_native")
            self.assertIn(expected, output.getvalue())

    def test_health_uses_connector_truth_not_session_row(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = {"endpoints": {"opencode": {
                "adapter": "opencode", "command": [sys.executable], "cwd": str(root)}}}
            broker = Broker(root / "state", raw)
            try:
                broker.call("desktop_bind", {"agent": "opencode", "session_id": "ses_1",
                                              "transport": "opencode_native"})
                broker.call("transport_report", {
                    "agent": "opencode", "transport": "opencode_native",
                    "state": "service_unavailable", "error": "database failed",
                    "failure_kind": "opencode_service_unavailable"})
                health = broker.call("health", {})
                self.assertTrue(health["degraded"])
                self.assertFalse(health["transports"]["opencode_native"]["ok"])
                role = broker.call("roles", {})["projects"][0]["roles"][0]
                self.assertFalse(role["online"])
                self.assertEqual(role["connection_status"], "service_unavailable")
                broker.call("transport_report", {
                    "agent": "opencode", "transport": "opencode_native",
                    "state": "ready", "delivered": True})
                self.assertTrue(broker.call("health", {})["transports"]["opencode_native"]["ok"])
            finally:
                broker.close()

    def test_received_job_can_only_be_reclaimed_by_opt_in_connector(self):
        with tempfile.TemporaryDirectory() as temp:
            hub = DesktopHub(Path(temp))
            result = {}
            task = {"id": "task-1", "target": "opencode", "payload": {
                "prompt": "x", "timeout": 60, "kind": "agent_chat_wakeup"}}
            thread = threading.Thread(target=lambda: result.setdefault("value", hub.dispatch(
                task, {"session_id": "ses_1"}, "prompt", lambda _: False,
                threading.Event(), delivery_timeout=20, execution_timeout=60)))
            thread.start()
            first = hub.receive("opencode", "ses_1", 1, transport="opencode_native")
            self.assertIsNone(hub.receive("opencode", "ses_1", 0, transport="opencode_native"))
            hub.jobs["task-1"]["claimed_at"] = time.monotonic() - 6
            recovered = hub.receive("opencode", "ses_1", 0, transport="opencode_native",
                                    recover_received=True)
            self.assertTrue(recovered["recovery"])
            self.assertEqual(first["nonce"], recovered["nonce"])
            hub.finish(first["task_id"], first["nonce"], "succeeded", {"summary": "once"})
            thread.join(3)
            self.assertEqual(result["value"][0], "succeeded")
            hub.close()

    def test_recovered_wakeup_with_marker_is_not_sent_twice(self):
        client = _ConnectorClient()
        job = {"task_id": "task-1", "nonce": "n", "action": "run",
               "session_id": "ses_1", "prompt": "x", "notify": True, "recovery": True}
        with patch.object(opencode_desktop_link, "inspect_opencode_delivery",
                          return_value={"delivered": True, "summary": ""}), \
                patch.object(opencode_desktop_link.subprocess, "Popen",
                             side_effect=AssertionError("must not send again")):
            opencode_desktop_link.execute(client, ["opencode.exe"], ".", job, "demo.review")
        finish = next(params for method, params in client.calls if method == "desktop_finish")
        self.assertTrue(finish["result"]["deduplicated"])

    def test_parses_official_ndjson_shape(self):
        result = parse_opencode_stream("\n".join((
            json.dumps({"type": "step_start", "sessionID": "ses_1"}),
            json.dumps({"type": "text", "part": {"text": "done"}}))))
        self.assertEqual(result["session_id"], "ses_1")
        self.assertEqual(result["summary"], "done")


if __name__ == "__main__":
    unittest.main()

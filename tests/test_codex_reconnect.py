"""Regression coverage for Codex app restart/runtime drift and wakeup recovery."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from bridge.broker import Broker
from bridge.codex_connection import update_connection


class CodexReconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.node_a = self.root / "node-a.exe"
        self.node_b = self.root / "node-b.exe"
        self.server = self.root / "server.mjs"
        for path in (self.node_a, self.node_b, self.server):
            path.write_text("fixture", encoding="utf-8")
        command = [sys.executable, str(Path(__file__).resolve().parents[1] /
                                       "examples" / "fixture_runtime.py")]
        raw = {"version": 2, "projects": {"demo": {
            "name": "Demo", "root": str(self.root), "roles": {
                "leader": {"adapter": "codex", "command": command, "cwd": "."},
                "tester": {"adapter": "codex", "command": command, "cwd": "."},
                "frontend": {"adapter": "qoder", "command": command, "cwd": "."},
            }}}}
        self.broker = Broker(self.state, raw)

    def tearDown(self):
        self.broker.close()
        self.temp.cleanup()

    def connection(self, pipe, node, origin):
        return {"pipe": pipe, "command": [str(node), str(self.server)],
                "origin_thread": origin, "updated_at": time.time()}

    def bind_codex_roles(self, connection):
        update_connection(self.state, connection)
        self.broker.call("desktop_bind", {"agent": "demo.leader", "session_id": "leader-chat",
                                           "transport": "codex_app_tools"})
        self.broker.call("desktop_bind", {"agent": "demo.tester", "session_id": "tester-chat",
                                           "transport": "codex_app_tools"})
        connection_id = connection.get("connection_id") or json.loads(
            (self.state / "codex-desktop.local.json").read_text(encoding="utf-8"))["connection_id"]
        for agent in ("demo.leader", "demo.tester"):
            self.broker.call("transport_report", {
                "agent": agent, "transport": "codex_app_tools", "state": "ready",
                "connection_id": connection_id})
        return connection_id

    def test_pipe_generation_refreshes_both_roles_without_touching_qoder(self):
        first = self.connection(r"\\.\pipe\codex-app-tools-A", self.node_a, "management-a")
        self.bind_codex_roles(first)
        self.broker.call("desktop_bind", {"agent": "demo.frontend", "session_id": "qoder-chat",
                                           "transport": "qoder_native"})
        (self.state / "qoder-native-status.json").write_text(json.dumps({
            "ready": True, "updated_at": time.time() * 1000}), encoding="utf-8")

        before = self.broker.call("health", {})
        self.assertTrue(before["transports"]["codex_app_tools"]["ok"])
        self.assertTrue(before["transports"]["qoder_native"]["ok"])

        second = self.connection(r"\\.\pipe\codex-app-tools-B", self.node_b, "management-b")
        refreshed = self.broker.call("transport_refresh", {"connection_context": second})
        self.assertTrue(refreshed["updated"])
        during = self.broker.call("health", {})
        self.assertTrue(during["degraded"])
        self.assertEqual({item["state"] for item in
                          during["transports"]["codex_app_tools"]["roles"]}, {"reconnecting"})
        self.assertTrue(during["transports"]["qoder_native"]["ok"])

        connection_id = refreshed["connection_id"]
        for agent in ("demo.leader", "demo.tester"):
            self.broker.call("transport_report", {
                "agent": agent, "transport": "codex_app_tools", "state": "ready",
                "connection_id": connection_id, "delivered": True})
        after = self.broker.call("health", {})
        self.assertTrue(after["transports"]["codex_app_tools"]["ok"])
        self.assertEqual(after["transports"]["codex_app_tools"]["consecutive_failures"], 0)
        self.assertIsNotNone(after["transports"]["codex_app_tools"]["last_success_at"])

    def test_deleted_runtime_degrades_health_immediately_and_role_is_not_online(self):
        connection = self.connection(r"\\.\pipe\codex-app-tools-A", self.node_a, "management")
        self.bind_codex_roles(connection)
        self.node_a.unlink()
        health = self.broker.call("health", {})
        self.assertTrue(health["degraded"])
        self.assertEqual({item["state"] for item in
                          health["transports"]["codex_app_tools"]["roles"]}, {"runtime_missing"})
        roles = self.broker.call("roles", {})["projects"][0]["roles"]
        leader = next(item for item in roles if item["role"] == "leader")
        self.assertFalse(leader["online"])
        self.assertEqual(leader["connection_status"], "runtime_missing")
        session = next(item for item in leader["sessions"] if item["id"] == "desktop:leader-chat")
        self.assertFalse(session["online"])

    def test_failed_result_wakeup_keeps_one_stable_retry_and_attention_count(self):
        connection = self.connection(r"\\.\pipe\codex-app-tools-A", self.node_a, "management")
        self.bind_codex_roles(connection)
        payload = {"prompt": "check inbox", "kind": "agent_chat_wakeup",
                   "event_type": "result", "notify": False}
        task_id, duplicate = self.broker.store.enqueue(
            "agent-chat", "demo.leader", "agentchat:event-1", payload)
        self.assertFalse(duplicate)
        claimed = self.broker.store.claim("demo.leader")
        self.assertEqual(claimed["id"], task_id)
        self.broker.store.requeue_wakeup(task_id, {
            "error": "pipe_missing", "retryable": True,
            "needs_attention": True, "unnotified": True}, 0)
        retried = self.broker.store.task_by_key("agent-chat", "agentchat:event-1")
        self.assertEqual(retried["id"], task_id)
        self.assertEqual(retried["state"], "queued")
        self.assertEqual(self.broker.store.count_tasks_by_key_prefix(
            "agent-chat", "agentchat:event-1"), 1)
        health = self.broker.call("health", {})
        leader = next(item for item in health["transports"]["codex_app_tools"]["roles"]
                      if item["agent"] == "demo.leader")
        self.assertEqual(leader["pending_result_wakeups"], 1)
        self.assertEqual(leader["needs_attention"], 1)


if __name__ == "__main__":
    unittest.main()

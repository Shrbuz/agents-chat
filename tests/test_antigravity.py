import json
import sys
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from bridge.antigravity import deliver, probe_session, TRANSPORT
from bridge.broker import Broker
from bridge.bus import Bus


SESSION = "5734d498-ab91-4403-9792-dde46ec57ef7"


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_probe_checks_the_exact_root_conversation_and_workspace(self):
        workspace = self.root / "project with spaces"
        workspace.mkdir()
        uri = workspace.as_uri()
        if sys.platform == "win32":
            uri = uri.replace(":", "%3A", 2).replace("file%3A", "file:", 1)
        metadata = {"rootConversationId": SESSION, "workspaceUris": [uri]}
        with patch("bridge.antigravity.call_agentapi",
                   return_value={"conversationMetadata": {"metadata": metadata}}):
            self.assertEqual(probe_session([], SESSION, workspace), "ready")
            self.assertEqual(probe_session([], SESSION, self.root), "antigravity_workspace_mismatch")
            metadata["parentConversationId"] = str(uuid.uuid4())
            self.assertEqual(probe_session([], SESSION, workspace), "antigravity_session_missing")

    def test_receipts_prevent_replay_after_success_or_ambiguous_failure(self):
        client = Mock(state_dir=self.root)
        job = {"action": "run", "notify": True, "task_id": str(uuid.uuid4()),
               "nonce": "n", "session_id": SESSION, "prompt": "检查收件箱 & echo literal"}
        with patch("bridge.antigravity.call_agentapi",
                   return_value={"sendMessage": {"recipientId": SESSION}}) as api:
            self.assertTrue(deliver(client, [], job))
            self.assertTrue(deliver(client, [], job))
            api.assert_called_once_with([], "send-message", SESSION, job["prompt"])
        job["task_id"] = str(uuid.uuid4())
        with patch("bridge.antigravity.call_agentapi", side_effect=RuntimeError("lost")) as api:
            self.assertFalse(deliver(client, [], job))
            self.assertFalse(deliver(client, [], job))
            api.assert_called_once()
            result = client.call.call_args.kwargs["result"]
            self.assertTrue(result["delivery_uncertain"])
            self.assertFalse(result["run_delivered"])

    def test_bus_wakeup_routes_through_bound_antigravity_sidecar(self):
        bus_path = self.root / "agent_chat"
        for role in ("leader", "desktop"):
            (bus_path / (role + "_msg")).mkdir(parents=True)
        (bus_path / "project.json").write_text(json.dumps({
            "schema_version": 1, "project_id": "demo",
            "roles": {"leader": "leader_msg", "desktop": "desktop_msg"}
        }), encoding="utf-8")
        broker = Broker(self.root / "state", {"version": 2, "projects": {"demo": {
            "root": str(self.root),
            "agent_chat": {"bus": str(bus_path), "roles": {"leader": "leader", "desktop": "desktop"}},
            "roles": {"leader": {"adapter": "fixture", "command": [sys.executable]},
                      "desktop": {"adapter": "antigravity", "command": [sys.executable]}}
        }}})
        self.addCleanup(broker.close)
        target = {"agent": "demo.desktop", "session_id": SESSION, "transport": TRANSPORT}
        with self.assertRaisesRegex(ValueError, "antigravity_sidecar_offline"):
            broker.call("desktop_bind", target)
        broker.call("antigravity_poll", {})
        with ThreadPoolExecutor(max_workers=1) as pool:
            binding = pool.submit(broker.call, "desktop_bind", target)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                pending = broker.call("antigravity_poll", {})["probes"]
                if pending:
                    probe = pending[0]
                    self.assertEqual(probe["cwd"], str(self.root))
                    broker.call("antigravity_probe_result", {"id": probe["id"], "kind": "ready"})
                    break
                time.sleep(0.02)
            self.assertEqual(binding.result()["session_id"], SESSION)
        broker.call("transport_report", {**target, "state": "ready"})
        self.assertTrue(broker.role_transport_health("demo.desktop")["online"])
        with self.assertRaisesRegex(ValueError, "antigravity_requires_bus"):
            broker.call("send", {"sender": "demo.leader", "target": "demo.desktop",
                                 "prompt": "work", "key": "direct"})

        broker.start()
        bus = Bus(bus_path)
        task = bus.submit("request", "leader", "leader-1", recipient="desktop",
                          data={"title": "Test", "body": "Test", "acceptance": ["Done"]})["task"]
        job = broker.call("desktop_receive", {**target, "seconds": 10})
        self.assertIsNotNone(job)
        self.assertTrue(job["notify"])
        self.assertIn(SESSION, job["prompt"])
        client = Mock(state_dir=broker.state_dir, call=lambda method, **params: broker.call(method, params))
        with patch("bridge.antigravity.call_agentapi",
                   return_value={"sendMessage": {"recipientId": SESSION}}) as api:
            self.assertTrue(deliver(client, [], job))
            api.assert_called_once()
        receipt = broker.call("wait", {"task_id": job["task_id"], "seconds": 3})
        self.assertEqual(receipt["state"], "succeeded")
        self.assertTrue(receipt["result"]["run_delivered"])
        self.assertEqual(bus.load()[1][task["task_id"]]["status"], "pending")
        bus.submit("result", "desktop", SESSION, task_id=task["task_id"],
                   data={"body": "Done", "evidence": ["fixture"]})
        verified = bus.submit("verify", "leader", "leader-1", task_id=task["task_id"],
                              data={"outcome": "pass", "body": "Verified"})
        self.assertEqual(verified["task"]["status"], "closed")
        broker.antigravity.seen = float("-inf")
        self.assertFalse(broker.role_transport_health("demo.desktop")["online"])


if __name__ == "__main__":
    unittest.main()

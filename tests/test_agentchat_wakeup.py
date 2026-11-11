"""Regression tests for automatic agent-chat wakeups.

The P0 report showed a pending request that never produced a wakeup task, which
forced a human to send a desktop notification manually.  These tests pin the
two failure modes: a request that predates the watcher, and a request that
arrives while the role already has queued work.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge.broker import Broker
from bridge.bus import Bus
from bridge.config import read_config

FIXTURE = [sys.executable, str(ROOT / "examples" / "fixture_runtime.py")]
PROJECT_ID = "demo"


def make_world(base):
    project = base / "project"
    bus_root = project / "agent_chat"
    (bus_root / "backend_msg").mkdir(parents=True)
    (bus_root / "frontend_msg").mkdir(parents=True)
    (bus_root / "project.json").write_text(json.dumps({
        "schema_version": 1, "project_id": PROJECT_ID,
        "roles": {"backend": "backend_msg", "frontend": "frontend_msg"},
    }), encoding="utf-8")
    config = base / "config.json"
    config.write_text(json.dumps({
        "version": 2,
        "projects": {"demo": {
            "name": "Demo", "root": str(project),
            "agent_chat": {"bus": str(bus_root),
                           "roles": {"backend": "backend", "frontend": "frontend"}},
            "roles": {
                "backend": {"adapter": "fixture", "command": FIXTURE,
                            "args": ["success"], "cwd": "."},
                "frontend": {"adapter": "fixture", "command": FIXTURE,
                             "args": ["success"], "cwd": "."},
            },
        }},
    }), encoding="utf-8")
    return project, bus_root, config


def submit_request(bus_root):
    return Bus(bus_root).submit(
        "request", "frontend", "fe-1", recipient="backend",
        data={"title": "登录页报错", "body": "点提交后 500",
              "acceptance": ["返回 200"], "priority": "P1"})


class WakeupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project, self.bus_root, self.config = make_world(self.base)
        self.broker = None

    def tearDown(self):
        if self.broker:
            self.broker.close()
        self.temp.cleanup()

    def start_bridge(self):
        self.broker = Broker(self.base / "state", read_config(self.config))
        self.broker.agent_chat.start()
        return self.broker

    def wait_key(self, key):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            task = self.broker.store.task_by_key("agent-chat", key)
            if task is not None:
                return task
            time.sleep(0.1)
        self.fail("no agent-chat wakeup task was created for " + key)

    def wakeup(self, created):
        return self.wait_key("agentchat:" + created["event"]["message_id"])

    def test_pending_request_before_watcher_start_is_reconciled(self):
        created = submit_request(self.bus_root)
        self.start_bridge()
        task = self.wakeup(created)
        self.assertEqual(task["target"], "demo.backend")
        self.assertEqual(task["payload"]["kind"], "agent_chat_wakeup")
        self.assertFalse(task["payload"]["interrupt_on_timeout"])
        self.assertFalse(task["payload"]["notify"])
        self.assertIn("收件箱", task["payload"]["prompt"])

    def test_pending_request_wakes_even_when_role_already_has_queued_work(self):
        self.start_bridge()
        # A long, unrelated task for the same role used to make the scanner
        # break before enqueueing the bus wakeup.
        self.broker.store.enqueue("someone", "demo.backend", "busy-work", {
            "prompt": "busy", "timeout": 1800, "reply_mode": "mailbox", "queue_ttl": 3600})
        created = submit_request(self.bus_root)
        task = self.wakeup(created)
        self.assertEqual(task["payload"]["kind"], "agent_chat_wakeup")
        self.assertEqual(self.broker.store.task_by_key("someone", "busy-work")["state"], "queued")

    def test_failed_delivery_is_retried_but_a_success_is_not(self):
        created = submit_request(self.bus_root)
        self.start_bridge()
        first = self.wakeup(created)
        base = "agentchat:" + created["event"]["message_id"]
        self.broker.store.finish(first["id"], "timed_out",
                                 {"error": "desktop_delivery_timeout"})
        project = self.broker.agent_chat.projects[0]
        self.broker.agent_chat._reconcile_pending(project)
        retry = self.broker.store.latest_task_by_key_prefix("agent-chat", base)
        self.assertEqual(retry["id"], first["id"])
        self.assertEqual(retry["idem"], base)
        self.assertTrue(retry["result"]["unnotified"])
        self.broker.store.finish(retry["id"], "succeeded",
                                 {"summary": "通知已投递", "run_delivered": True})
        self.broker.agent_chat._reconcile_pending(project)
        again = self.broker.store.latest_task_by_key_prefix("agent-chat", base)
        self.assertEqual(again["id"], retry["id"])
        self.assertEqual(self.broker.store.count_tasks_by_key_prefix("agent-chat", base), 1)

    def test_rebind_cancelled_wakeup_is_reopened_for_the_still_pending_event(self):
        created = submit_request(self.bus_root)
        self.start_bridge()
        first = self.wakeup(created)
        dropped = self.broker.store.cancel_wakeups("demo.backend")
        self.assertEqual(dropped, [first["id"]])
        self.assertEqual(self.broker.store.get(first["id"])["state"], "cancelled")

        project = self.broker.agent_chat.projects[0]
        self.broker.agent_chat._reconcile_pending(project)
        retry = self.broker.store.get(first["id"])
        self.assertEqual(retry["state"], "queued")
        self.assertTrue(retry["result"]["unnotified"])

    def test_claim_result_and_verify_each_wake_the_right_role_once(self):
        created = submit_request(self.bus_root)
        self.start_bridge()
        request_wakeup = self.wakeup(created)
        self.assertEqual(request_wakeup["target"], "demo.backend")
        task_id = created["task"]["task_id"]
        bus = Bus(self.bus_root)

        claimed = bus.submit("claim", "backend", "be-1", task_id=task_id)
        claim_wakeup = self.wait_key("agentchat:" + claimed["event"]["message_id"])
        self.assertEqual(claim_wakeup["target"], "demo.frontend")

        token = claimed["event"]["data"]["token"]
        result = bus.submit("result", "backend", "be-1", task_id=task_id, token=token,
                            data={"body": "done", "evidence": ["test passed"]})
        result_wakeup = self.wait_key("agentchat:" + result["event"]["message_id"])
        self.assertEqual(result_wakeup["target"], "demo.frontend")

        verified = bus.submit("verify", "frontend", "fe-1", task_id=task_id,
                              data={"outcome": "pass", "body": "ok"})
        verify_wakeup = self.wait_key("agentchat:" + verified["event"]["message_id"])
        self.assertEqual(verify_wakeup["target"], "demo.backend")

        for event in (created, claimed, result, verified):
            key = "agentchat:" + event["event"]["message_id"]
            self.assertEqual(self.broker.store.count_tasks_by_key_prefix("agent-chat", key), 1)
            self.assertEqual(self.wait_key(key)["payload"]["kind"], "agent_chat_wakeup")

    def test_stale_high_cursor_from_a_rebuilt_bus_is_reset(self):
        created = submit_request(self.bus_root)
        self.broker = Broker(self.base / "state", read_config(self.config))
        # This is the live incident: the file bus restarted at sequence 1 but
        # the persisted cursor still pointed at sequence 579/581/588.
        self.broker.store.set_bus_cursor("demo", json.dumps({
            "frontend": "00000581_old.json", "backend": "00000579_old.json",
            "leader": "00000588_old.json", "tester": "00000582_old.json"}))
        self.broker.agent_chat.start()
        task = self.wakeup(created)
        self.assertEqual(task["target"], "demo.backend")
        health = self.broker.agent_chat.health()
        self.assertGreaterEqual(health["cursor_resets"], 1)

    def test_cursor_does_not_advance_when_enqueue_fails(self):
        self.start_bridge()
        created = submit_request(self.bus_root)
        key = "agentchat:" + created["event"]["message_id"]
        with mock.patch.object(self.broker.store, "enqueue",
                               side_effect=RuntimeError("injected enqueue failure")):
            time.sleep(2)
            cursor = json.loads(self.broker.store.bus_cursor("demo"))["backend"]
            self.assertNotEqual(cursor, next((self.bus_root / "backend_msg").glob("*.json")).name)
            health = self.broker.agent_chat.health()
            self.assertFalse(health["ok"])
            self.assertTrue(health["degraded"])
        task = self.wait_key(key)
        self.assertEqual(task["target"], "demo.backend")

    def test_health_is_degraded_when_the_wakeup_scan_raises(self):
        self.start_bridge()
        with mock.patch.object(self.broker.agent_chat, "_scan",
                               side_effect=RuntimeError("wakeup scan exploded")):
            deadline = time.monotonic() + 5
            health = None
            while time.monotonic() < deadline:
                health = self.broker.agent_chat.health()
                if health["degraded"]:
                    break
                time.sleep(0.2)
        self.assertIsNotNone(health)
        self.assertTrue(health["thread_alive"])
        self.assertFalse(health["ok"])
        self.assertTrue(health["degraded"])
        self.assertIn("wakeup scan exploded", health["projects"][0]["last_error"])
        self.assertTrue(self.broker.call("health", {})["degraded"])

    def test_health_reports_a_dead_wakeup_thread(self):
        self.broker = Broker(self.base / "state", read_config(self.config))

        def dead_loop(watcher):
            watcher._record_error("*", RuntimeError("wakeup thread died"))

        with mock.patch.object(type(self.broker.agent_chat), "_loop", new=dead_loop):
            self.broker.agent_chat.start()
            time.sleep(0.4)
        health = self.broker.agent_chat.health()
        self.assertTrue(health["started"])
        self.assertFalse(health["thread_alive"])
        self.assertFalse(health["ok"])
        self.assertTrue(health["degraded"])

    def test_cursor_already_past_request_is_recovered_by_reconcile(self):
        created = submit_request(self.bus_root)
        self.broker = Broker(self.base / "state", read_config(self.config))
        # Simulate a crash after the cursor was advanced but before enqueue.
        path = next((self.bus_root / "backend_msg").glob("*.json"))
        self.broker.store.set_bus_cursor("demo", json.dumps({"frontend": "", "backend": path.name}))
        self.broker.agent_chat.start()
        task = self.wakeup(created)
        self.assertEqual(task["target"], "demo.backend")


if __name__ == "__main__":
    unittest.main()

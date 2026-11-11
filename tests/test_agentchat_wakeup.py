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
        event = created["event"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            project = self.broker.agent_chat.projects[0]
            role = project["roles"][event["to"]]
            session = (self.broker.agent_chat._bus_session(project, event["to"])
                       or "unbound:" + role["address"])
            key = self.broker.agent_chat._delivery_key(project, event, session)
            tasks = self.broker.store.wakeup_tasks_for_keys([key])
            if key in tasks:
                return tasks[key]
            time.sleep(0.1)
        self.fail("no agent-chat wakeup task was created for event " + event["message_id"])

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
        self.broker.store.finish(first["id"], "timed_out",
                                 {"error": "desktop_delivery_timeout"})
        project = self.broker.agent_chat.projects[0]
        self.broker.agent_chat._reconcile_pending(project)
        retry = self.broker.store.get(first["id"])
        self.assertEqual(retry["id"], first["id"])
        self.assertTrue(retry["idem"].startswith("agentchat-batch:"))
        self.assertTrue(retry["result"]["unnotified"])
        self.broker.store.finish(retry["id"], "succeeded",
                                 {"summary": "通知已投递", "run_delivered": True})
        self.broker.agent_chat._reconcile_pending(project)
        again = self.broker.store.get(first["id"])
        self.assertEqual(again["id"], retry["id"])
        self.assertEqual(self.broker.store.count_tasks_by_key_prefix(
            "agent-chat", "agentchat-batch:"), 1)

    def test_upgrade_adopts_the_legacy_message_id_wakeup(self):
        created = submit_request(self.bus_root)
        self.broker = Broker(self.base / "state", read_config(self.config))
        event = created["event"]
        legacy_id, _ = self.broker.store.enqueue(
            "agent-chat", "demo.backend", "agentchat:" + event["message_id"], {
                "prompt": "check inbox", "timeout": 1800, "delivery_timeout": 30,
                "context": [], "reply_mode": "mailbox", "queue_ttl": 86400,
                "notify": False, "kind": "agent_chat_wakeup",
                "event_type": "request", "bus_event_id": event["message_id"],
                "interrupt_on_timeout": False})
        self.broker.agent_chat.start()
        adopted = self.wakeup(created)
        self.assertEqual(adopted["id"], legacy_id)
        self.assertEqual(self.broker.store.count_tasks_by_key_prefix(
            "agent-chat", "agentchat-batch:"), 0)

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

    def test_claim_and_result_wake_once_but_terminal_verify_is_suppressed(self):
        created = submit_request(self.bus_root)
        self.start_bridge()
        request_wakeup = self.wakeup(created)
        self.assertEqual(request_wakeup["target"], "demo.backend")
        task_id = created["task"]["task_id"]
        bus = Bus(self.bus_root)

        claimed = bus.submit("claim", "backend", "be-1", task_id=task_id)
        claim_wakeup = self.wakeup(claimed)
        self.assertEqual(claim_wakeup["target"], "demo.frontend")

        token = claimed["event"]["data"]["token"]
        result = bus.submit("result", "backend", "be-1", task_id=task_id, token=token,
                            data={"body": "done", "evidence": ["test passed"]})
        result_wakeup = self.wakeup(result)
        self.assertEqual(result_wakeup["target"], "demo.frontend")

        verified = bus.submit("verify", "frontend", "fe-1", task_id=task_id,
                              data={"outcome": "pass", "body": "ok"})
        time.sleep(2)
        project = self.broker.agent_chat.projects[0]
        role = project["roles"]["backend"]
        session = (self.broker.agent_chat._bus_session(project, "backend")
                   or "unbound:" + role["address"])
        verify_key = self.broker.agent_chat._delivery_key(
            project, verified["event"], session)
        self.assertNotIn(verify_key, self.broker.store.wakeup_tasks_for_keys([verify_key]))

        for event in (created, claimed, result):
            self.assertEqual(self.wakeup(event)["payload"]["kind"], "agent_chat_wakeup")

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
        with mock.patch.object(self.broker.store, "enqueue_wakeup",
                               side_effect=RuntimeError("injected enqueue failure")):
            time.sleep(2)
            cursor = json.loads(self.broker.store.bus_cursor("demo"))["backend"]
            self.assertNotEqual(cursor, next((self.bus_root / "backend_msg").glob("*.json")).name)
            health = self.broker.agent_chat.health()
            self.assertFalse(health["ok"])
            self.assertTrue(health["degraded"])
        task = self.wakeup(created)
        self.assertEqual(task["target"], "demo.backend")

    def test_closed_backlog_is_discarded_without_retiring_the_role_session(self):
        first = submit_request(self.bus_root)
        bus = Bus(self.bus_root)
        claimed = bus.submit("claim", "backend", "be-1", task_id=first["task"]["task_id"])
        bus.submit("result", "backend", "be-1", task_id=first["task"]["task_id"],
                   token=claimed["event"]["data"]["token"],
                   data={"body": "done", "evidence": ["ok"]})
        verified = bus.submit("verify", "frontend", "fe-1", task_id=first["task"]["task_id"],
                              data={"outcome": "pass", "body": "accepted"})
        self.start_bridge()
        time.sleep(2)
        for created in (first, claimed, verified):
            event = created["event"]
            project = self.broker.agent_chat.projects[0]
            role = project["roles"][event["to"]]
            session = "unbound:" + role["address"]
            key = self.broker.agent_chat._delivery_key(project, event, session)
            self.assertNotIn(key, self.broker.store.wakeup_tasks_for_keys([key]))
        self.assertEqual(self.broker.store.wakeup_stats()["pending"], 0)

    def test_resolved_backlog_is_one_summary_with_composite_event_keys(self):
        bus = Bus(self.bus_root)
        results = []
        for number in (1, 2):
            created = bus.submit(
                "request", "frontend", "fe-1", recipient="backend",
                data={"title": f"task {number}", "body": "work",
                      "acceptance": ["done"], "priority": "P1"})
            claimed = bus.submit("claim", "backend", "be-1",
                                 task_id=created["task"]["task_id"])
            results.append(bus.submit(
                "result", "backend", "be-1", task_id=created["task"]["task_id"],
                token=claimed["event"]["data"]["token"],
                data={"body": "done", "evidence": ["ok"]}))
        self.start_bridge()
        first = self.wakeup(results[0])
        second = self.wakeup(results[1])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["payload"]["event_type"], "batch")
        self.assertEqual(len(first["payload"]["bus_events"]), 2)
        self.assertEqual(len(first["payload"]["dedupe_keys"]), 2)
        for key in first["payload"]["dedupe_keys"]:
            self.assertIn("demo", key)
            self.assertIn("unbound:demo.frontend", key)
        self.assertIn("已合并为一条摘要", first["payload"]["prompt"])

    def test_delivery_preflight_discards_a_task_closed_after_enqueue(self):
        created = submit_request(self.bus_root)
        self.start_bridge()
        wakeup = self.wakeup(created)
        bus = Bus(self.bus_root)
        claimed = bus.submit("claim", "backend", "be-1", task_id=created["task"]["task_id"])
        bus.submit("result", "backend", "be-1", task_id=created["task"]["task_id"],
                   token=claimed["event"]["data"]["token"],
                   data={"body": "done", "evidence": ["ok"]})
        bus.submit("verify", "frontend", "fe-1", task_id=created["task"]["task_id"],
                   data={"outcome": "pass", "body": "accepted"})
        self.assertIsNone(self.broker.agent_chat.prepare_wakeup(wakeup))
        self.assertEqual(self.broker.store.list_sessions(project="demo"), [])

    def test_delivery_preflight_discards_a_superseded_claim_notification(self):
        created = submit_request(self.bus_root)
        self.start_bridge()
        self.wakeup(created)
        bus = Bus(self.bus_root)
        claimed = bus.submit("claim", "backend", "be-1", task_id=created["task"]["task_id"])
        claim_wakeup = self.wakeup(claimed)
        result = bus.submit(
            "result", "backend", "be-1", task_id=created["task"]["task_id"],
            token=claimed["event"]["data"]["token"],
            data={"body": "done", "evidence": ["ok"]})
        result_wakeup = self.wakeup(result)
        self.assertIsNone(self.broker.agent_chat.prepare_wakeup(claim_wakeup))
        self.assertIsNotNone(self.broker.agent_chat.prepare_wakeup(result_wakeup))

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

"""P0: a delivery wait must never cancel a live desktop chat.

These tests reproduce the Qoder incident: an ordinary mailbox send timed out
and the hub sent a cancel action, which stopped the user's current Qoder round.
Notifications are now delivery-only, and only `interrupt_on_timeout=True`
authorizes a cancel request.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge.broker import Broker
from bridge.config import read_config
from bridge.desktop_transport import DesktopHub


class DesktopTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"endpoints": {
            "qoder": {"adapter": "qoder", "cwd": str(self.root), "command": [sys.executable]},
            "codex": {"adapter": "codex", "cwd": str(self.root), "command": [sys.executable]},
        }}), encoding="utf-8")
        self.broker = Broker(self.root / "state", read_config(self.config))
        self.broker.start()
        self.broker.call("desktop_bind", {"agent": "qoder", "session_id": "chat-1",
                                          "transport": "qoder_native"})

    def tearDown(self):
        self.broker.close()
        self.temp.cleanup()

    def send(self, **params):
        payload = {"sender": "codex", "target": "qoder", "prompt": "检查收件箱",
                   "key": "p0-" + str(time.time_ns()), "timeout": 1,
                   "delivery_timeout": 5}
        payload.update(params)
        return self.broker.call("send", payload)

    def test_mailbox_timeout_stops_waiting_and_never_cancels_the_chat(self):
        task = self.send()
        job = self.broker.call("desktop_receive", {"agent": "qoder", "session_id": "chat-1", "seconds": 5})
        self.assertEqual(job["task_id"], task["task_id"])
        self.assertFalse(job["interrupt_on_timeout"])
        result = self.broker.call("wait", {"task_id": task["task_id"], "seconds": 5})
        self.assertEqual(result["state"], "timed_out")
        self.assertEqual(result["result"]["error"], "desktop_execution_timeout")
        self.assertTrue(result["result"]["run_delivered"])
        self.assertFalse(result["result"]["cancel_requested"])
        self.assertFalse(result["result"]["cancel_acknowledged"])
        # The target is still running, so no cancel action may be waiting.
        self.assertIsNone(self.broker.call("desktop_receive", {
            "agent": "qoder", "session_id": "chat-1", "seconds": 0}))
        # Its eventual result is accepted, not treated as an unknown task.
        late = self.broker.call("desktop_finish", {
            "task_id": task["task_id"], "nonce": job["nonce"], "state": "succeeded",
            "result": {"summary": "收件箱已看完"}})
        self.assertTrue(late["accepted"])
        self.assertTrue(late["late"])
        self.assertEqual(self.broker.call("task", {"task_id": task["task_id"]})["state"], "timed_out")

    def test_interrupt_on_timeout_is_the_only_path_that_sends_cancel(self):
        task = self.send(interrupt_on_timeout=True)
        job = self.broker.call("desktop_receive", {"agent": "qoder", "session_id": "chat-1", "seconds": 5})
        self.assertEqual(job["task_id"], task["task_id"])
        self.assertTrue(job["interrupt_on_timeout"])
        # Block on the cancel action: the dispatcher sends it after the timeout.
        control = self.broker.call("desktop_receive", {
            "agent": "qoder", "session_id": "chat-1", "seconds": 5})
        self.assertIsNotNone(control)
        self.assertEqual(control["action"], "cancel")
        result = self.broker.call("wait", {"task_id": task["task_id"], "seconds": 5})
        self.assertEqual(result["state"], "timed_out")
        self.assertTrue(result["result"]["cancel_requested"])
        self.assertTrue(result["result"]["cancel_acknowledged"])

    def test_notification_finishes_only_after_connector_confirms_real_delivery(self):
        task = self.send(kind="agent_chat_wakeup", timeout=1)
        job = self.broker.call("desktop_receive", {"agent": "qoder", "session_id": "chat-1", "seconds": 5})
        self.assertEqual(job["task_id"], task["task_id"])
        self.assertTrue(job["notify"])
        acknowledged = self.broker.call("desktop_finish", {
            "task_id": task["task_id"], "nonce": job["nonce"], "state": "succeeded",
            "result": {"summary": "通知已写入目标聊天", "notification": True}})
        self.assertTrue(acknowledged["accepted"])
        result = self.broker.call("wait", {"task_id": task["task_id"], "seconds": 5})
        self.assertEqual(result["state"], "succeeded")
        self.assertTrue(result["result"]["run_delivered"])
        self.assertTrue(result["result"]["notification"])
        self.assertFalse(result["result"]["cancel_requested"])
        self.assertIsNone(self.broker.call("desktop_receive", {
            "agent": "qoder", "session_id": "chat-1", "seconds": 0}))
        late = self.broker.call("desktop_finish", {
            "task_id": task["task_id"], "nonce": job["nonce"], "state": "succeeded",
            "result": {"summary": "处理完毕"}})
        self.assertFalse(late["accepted"])
        self.assertTrue(late["task_closed"])

    def test_delivery_timeout_is_reported_separately_from_execution(self):
        task = self.send(timeout=30, delivery_timeout=1)
        result = self.broker.call("wait", {"task_id": task["task_id"], "seconds": 5})
        self.assertEqual(result["state"], "timed_out")
        self.assertEqual(result["result"]["error"], "desktop_delivery_timeout")
        self.assertFalse(result["result"]["run_delivered"])
        self.assertFalse(result["result"]["cancel_requested"])
        self.assertIsNone(self.broker.call("desktop_receive", {
            "agent": "qoder", "session_id": "chat-1", "seconds": 0}))


class QoderConnectorTests(unittest.TestCase):
    """The connector must not turn a sender timeout into a UI stop."""

    class Client:
        def __init__(self):
            self.finishes = []

        def call(self, method, **params):
            if method == "task":
                return {"state": "timed_out"}
            if method == "desktop_finish":
                self.finishes.append(params)
                return {"accepted": True, "late": True}
            raise AssertionError("unexpected call " + method)

    def test_non_interrupt_timeout_keeps_qoder_running(self):
        import qoder_desktop_link as link

        client = self.Client()
        stops = []
        sent = {"value": False}

        def fake_ui(action, *args):
            if action == "inspect":
                if not sent["value"]:
                    return {"busy": False}
                return {"task_id": "task-1", "status": "状态：成功", "summary": "done"}
            if action == "send":
                sent["value"] = True
                return {}
            if action == "stop":
                stops.append(args)
                return {}
            raise AssertionError("unexpected ui action " + action)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(link, "ui", side_effect=fake_ui):
            link.execute(client, Path(tmp), {
                "task_id": "task-1", "nonce": "n", "session_id": "chat-1",
                "prompt": "check inbox", "interrupt_on_timeout": False,
            }, threading.Event())
        self.assertEqual(stops, [])
        self.assertEqual(client.finishes[-1]["state"], "succeeded")

    def test_explicit_interrupt_timeout_stops_qoder(self):
        import qoder_desktop_link as link

        client = self.Client()
        stops = []
        sent = {"value": False}

        def fake_ui(action, *args):
            if action == "inspect":
                return {"busy": False}
            if action == "send":
                sent["value"] = True
                return {}
            if action == "stop":
                stops.append(args)
                return {}
            raise AssertionError("unexpected ui action " + action)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(link, "ui", side_effect=fake_ui):
            link.execute(client, Path(tmp), {
                "task_id": "task-1", "nonce": "n", "session_id": "chat-1",
                "prompt": "run this", "interrupt_on_timeout": True,
            }, threading.Event())
        self.assertEqual(len(stops), 1)
        self.assertEqual(client.finishes, [])


class DesktopHubDispatchTests(unittest.TestCase):
    """Unit-level checks for the acknowledgement flags documented by the CLI."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hub = DesktopHub(Path(self.temp.name))

    def tearDown(self):
        self.hub.close()
        self.temp.cleanup()

    def task(self, kind=None, timeout=30):
        payload = {"prompt": "x", "timeout": timeout}
        if kind:
            payload["kind"] = kind
        return {"id": "task-1", "target": "qoder", "payload": payload}

    def test_delivery_ack_is_tracked(self):
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault(
            "value", self.hub.dispatch(self.task(kind="agent_chat_wakeup"),
                                       {"session_id": "chat-1"}, "x", lambda _: False,
                                       threading.Event(), execution_timeout=60)))
        thread.start()
        job = self.hub.receive("qoder", "chat-1", 2)
        self.assertIsNotNone(job)
        self.hub.finish(job["task_id"], job["nonce"], "succeeded",
                        {"summary": "actually delivered", "notification": True})
        thread.join(3)
        state, value = result["value"]
        self.assertEqual(state, "succeeded")
        self.assertTrue(value["run_delivered"])
        self.assertFalse(value["cancel_requested"])
        self.assertFalse(value["cancel_acknowledged"])


if __name__ == "__main__":
    unittest.main()

import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from bridge.broker import Broker
from bridge.desktop_transport import DesktopHub
from desktop_link import completed_result, completion_cursor, completed_read_result


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.broker = Broker(self.root, {"endpoints": {"qoder": {"adapter":"qoder", "cwd":str(self.root), "command":[sys.executable]},
            "codex": {"adapter":"codex", "cwd":str(self.root), "command":[sys.executable]}}})

    def tearDown(self):
        self.broker.close()
        self.temp.cleanup()

    def bind(self):
        return self.broker.call("desktop_bind", {"agent":"qoder", "session_id":"chat-1", "transport":"qoder_extension"})

    def send(self):
        return self.broker.call("send", {"sender":"codex", "target":"qoder", "prompt":"Read the project", "key":"desktop-1", "timeout":3})

    def test_binding_survives_restart_and_rejects_invalid_transport(self):
        with self.assertRaises(ValueError):
            self.broker.call("desktop_bind", {"agent":"qoder", "session_id":"chat-1", "transport":"codex_app_tools"})
        self.bind()
        restored = DesktopHub(self.root)
        self.assertEqual(restored.bindings["qoder"]["session_id"], "chat-1")
        self.assertEqual(self.send()["mode"], "desktop_session")

    def test_binding_cannot_redirect_accepted_work(self):
        self.send()
        with self.assertRaisesRegex(ValueError, "pending"):
            self.bind()

    def test_native_route_receives_with_composer_automation_disabled(self):
        self.broker.endpoints["qoder"]["desktop_input_enabled"]=False
        self.broker.call("desktop_bind",{"agent":"qoder","session_id":"native-chat","transport":"qoder_native"})
        target=next(t for t in self.broker.call("targets",{}) if t["name"]=="qoder")
        self.assertEqual(target["mode"],"desktop_session")
        self.assertFalse(target["desktop_input_enabled"])
        self.assertTrue(target["desktop_wakeup"])
        self.assertEqual(target["desktop_transport"],"qoder_native")
        task=self.send()
        self.broker.start()
        job=self.broker.call("desktop_receive",{"agent":"qoder","session_id":"native-chat","seconds":2})
        self.assertEqual(job["task_id"],task["task_id"])
        self.broker.call("desktop_finish",{"task_id":job["task_id"],"nonce":job["nonce"],"state":"succeeded","result":{"summary":"native result"}})
        result=self.broker.call("wait",{"task_id":task["task_id"],"seconds":2})
        self.assertEqual(result["state"],"succeeded")

    def test_real_worker_desktop_receipt_and_result_without_cli(self):
        self.bind()
        task = self.send()
        self.broker.start()
        job = self.broker.call("desktop_receive", {"agent":"qoder", "session_id":"chat-1", "seconds":2})
        self.assertEqual(job["task_id"], task["task_id"])
        self.assertIn("Read the project", job["prompt"])
        self.assertIsNone(self.broker.call("desktop_receive", {"agent":"qoder", "session_id":"chat-1", "seconds":0}))
        with self.assertRaisesRegex(ValueError,"receipt"):
            self.broker.call("desktop_finish", {"task_id":job["task_id"], "nonce":"wrong", "state":"succeeded", "result":{"summary":"wrong"}})
        self.broker.call("desktop_finish", {"task_id":job["task_id"], "nonce":job["nonce"], "state":"succeeded", "result":{"summary":"actual desktop result"}})
        result = self.broker.call("wait", {"task_id":task["task_id"], "seconds":2})
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"]["mode"], "desktop_session")
        self.assertEqual(result["result"]["summary"], "actual desktop result")
        self.assertEqual(result["result"]["session_id"], "chat-1")

    def test_cancellation_reaches_connector_without_cli_fallback(self):
        self.bind()
        task = self.send()
        self.broker.start()
        self.broker.call("desktop_receive", {"agent":"qoder", "session_id":"chat-1", "seconds":2})
        self.broker.call("cancel", {"task_id":task["task_id"]})
        job = self.broker.call("desktop_receive", {"agent":"qoder", "session_id":"chat-1", "seconds":2})
        self.assertEqual(job["action"], "cancel")
        result = self.broker.call("wait", {"task_id":task["task_id"], "seconds":2})
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(result["result"]["delivery_uncertain"])

    def test_receive_requires_bound_session_and_finite_wait(self):
        self.bind()
        for params in ({"agent":"qoder", "session_id":"other"}, {"agent":"qoder", "session_id":"chat-1", "seconds":float("nan")}):
            with self.assertRaises(ValueError):
                self.broker.call("desktop_receive", params)

    def test_desktop_roles_can_receive_in_same_workspace_without_wait_deadlock(self):
        self.bind()
        self.broker.call("desktop_bind", {"agent":"codex", "session_id":"chat-2", "transport":"codex_app_tools"})
        qoder = self.send()
        codex = self.broker.call("send", {"sender":"qoder", "target":"codex", "prompt":"Backend interface", "key":"backend-1", "timeout":3})
        self.broker.start()
        first = self.broker.call("desktop_receive", {"agent":"qoder", "session_id":"chat-1", "seconds":2})
        second = self.broker.call("desktop_receive", {"agent":"codex", "session_id":"chat-2", "seconds":2})
        self.assertEqual(first["task_id"],qoder["task_id"])
        self.assertEqual(second["task_id"],codex["task_id"])
        for job in (first,second):
            self.broker.call("desktop_finish", {"task_id":job["task_id"], "nonce":job["nonce"], "state":"succeeded", "result":{"summary":"done"}})

    def test_disconnect_is_explicit(self):
        self.bind()
        self.broker.call("desktop_bind", {"agent":"qoder", "session_id":None})
        self.assertEqual(self.send()["mode"], "independent_cli_task")

    def test_qoder_without_input_can_send_and_receive_result_but_not_be_pushed(self):
        self.broker.endpoints["qoder"]["desktop_input_enabled"] = False
        self.broker.call("desktop_bind", {"agent":"qoder", "session_id":"chat-1", "transport":"qoder_accessibility"})
        target = next(t for t in self.broker.call("targets",{}) if t["name"]=="qoder")
        self.assertEqual(target["mode"],"desktop_mailbox")
        self.assertFalse(target["desktop_wakeup"])
        with self.assertRaisesRegex(ValueError,"input_disabled"):
            self.send()
        with self.assertRaisesRegex(ValueError,"reply_mode_run_unavailable"):
            self.broker.call("send", {"sender":"qoder","target":"codex","prompt":"Implement backend","key":"run-disabled","reply_mode":"run"})
        task = self.broker.call("send", {"sender":"qoder","target":"codex","prompt":"Implement backend","key":"request-1"})
        self.broker.store.claim("codex")
        self.broker.store.finish(task["task_id"],"succeeded",{"summary":"Backend ready at localhost"})
        result=self.broker.call("wait",{"task_id":task["task_id"],"seconds":0})
        self.assertEqual(result["result"]["summary"],"Backend ready at localhost")
        self.assertFalse(self.broker.store.has_pending("qoder"))

    def test_codex_cursor_ignores_old_or_different_turn_output(self):
        def snapshot(turn, message_turn="new", text="actual result"):
            return {"polls":[{"cursor":"cursor-2", "thread":{"id":"chat-1"}, "latestTurn":{"id":turn,"status":"completed"},
                "latestAssistantMessage":{"turnId":message_turn,"phase":"final_answer","text":text}}]}
        self.assertIsNone(completed_result(snapshot("old","old"),"chat-1","old"))
        self.assertIsNone(completed_result(snapshot("new","old"),"chat-1","old"))
        self.assertEqual(completed_result(snapshot("new"),"chat-1","old"),("succeeded","actual result"))
        self.assertEqual(completion_cursor(snapshot("new"),"chat-1"),"cursor-2")

    def test_codex_read_result_requires_new_turn_and_matching_task(self):
        turn = {"id":"new", "status":"completed", "items":[
            {"type":"userMessage","content":[{"type":"text","text":"Agents Chat task task-1. Project: default. From role: review. To role: codex."}]},
            {"type":"agentMessage","phase":"final_answer","text":"actual answer"}]}
        snapshot = {"turns":[turn]}
        self.assertEqual(completed_read_result(snapshot,"old","task-1"),("succeeded","actual answer"))
        self.assertIsNone(completed_read_result(snapshot,"new","task-1"))
        self.assertIsNone(completed_read_result(snapshot,"old","different-task"))
        turn["status"] = "inProgress"
        self.assertIsNone(completed_read_result(snapshot,"old","task-1"))

    def test_codex_desktop_interchat_event_is_pinned_to_observed_turn(self):
        snapshot = {"turns":[{"id":"observed-turn","status":"completed","items":[
            {"type":"functionCallOutput","name":"send_message_to_thread"},
            {"type":"agentMessage","phase":"final_answer","text":"desktop answer"}]}]}
        self.assertIsNone(completed_read_result(snapshot,"old","task-1"))
        self.assertEqual(completed_read_result(snapshot,"old","task-1","observed-turn"),("succeeded","desktop answer"))
        self.assertIsNone(completed_read_result(snapshot,"old","task-1","another-turn"))


if __name__ == "__main__":
    unittest.main()

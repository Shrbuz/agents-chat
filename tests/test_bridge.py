import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bridge.broker import Broker, read_config
from bridge.cli import start
from bridge.client import Client
from bridge.runtime import parse_result
from bridge.store import Store


def wait_process_exit(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32")
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k.OpenProcess(0x100000, False, pid)
        if handle:
            k.WaitForSingleObject(handle, 5000)
            k.CloseHandle(handle)
    else:
        time.sleep(0.2)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "db.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, reply="mailbox"):
        return self.store.enqueue("a", "b", "key", {"prompt": "work", "reply_mode": reply, "timeout": 5})[0]

    def test_duplicate_and_conflict(self):
        first = self.submit()
        self.assertEqual(first, self.submit())
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.store.enqueue("a", "b", "key", {"prompt": "different"})

    def test_only_one_atomic_claim(self):
        self.submit()
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            claims = list(pool.map(lambda _: self.store.claim("b"), range(24)))
        self.assertEqual(sum(t is not None for t in claims), 1)

    def test_terminal_reply_and_callback_once(self):
        task_id = self.submit("run")
        self.store.claim("b")
        self.assertTrue(self.store.finish(task_id, "succeeded", {"summary": "done"}))
        self.assertFalse(self.store.finish(task_id, "failed", {"summary": "late duplicate"}))
        callback = self.store.claim("a")
        self.assertEqual(callback["payload"]["reply_mode"], "mailbox")
        self.store.finish(callback["id"], "succeeded", {"summary": "reviewed"})
        self.assertIsNone(self.store.claim("b"))
        self.assertEqual(len(self.store.inbox("a", "test")["messages"]), 1)

    def test_read_is_not_ack_and_cursor_is_monotonic(self):
        task_id = self.submit()
        self.store.finish(task_id, "succeeded", {"summary": "ok"})
        one = self.store.inbox("a", "test")
        self.assertEqual(one, self.store.inbox("a", "test"))
        with self.assertRaises(ValueError):
            self.store.ack("b", "test", one["next_cursor"])
        self.store.ack("a", "test", one["next_cursor"])
        self.store.ack("a", "test", 0)
        self.assertEqual(self.store.inbox("a", "test")["messages"], [])

    def test_recovery_does_not_reexecute_unknown_work(self):
        task_id = self.submit()
        self.store.claim("b")
        self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.get(task_id)["state"], "interrupted")
        self.assertIsNone(self.store.claim("b"))
        self.assertEqual(self.store.recover(), 0)


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        endpoints = {}
        for mode in ("success", "review", "error", "exit", "invalid", "sleep", "flood", "child", "missing"):
            cwd = cls.base / mode
            cwd.mkdir()
            endpoints[mode] = {"adapter": "fixture", "command": [sys.executable, str(ROOT / "examples" / "fixture_runtime.py")],
                               "args": ["success" if mode == "review" else mode], "cwd": str(cwd), "max_output_bytes": 16384}
        endpoints["missing"]["command"] = [str(cls.base / "absent.exe")]
        endpoints["same-workspace"] = {**endpoints["sleep"], "args": ["success"]}
        cls.config = cls.base / "config.json"
        cls.config.write_text(json.dumps({"endpoints": endpoints}))
        cls.state = cls.base / "state"
        start(cls.state, cls.config)
        cls.client = Client(cls.state)

    @classmethod
    def tearDownClass(cls):
        pid = cls.client.call("health")["pid"]
        cls.client.call("shutdown")
        for _ in range(100):
            if not (cls.state / "endpoint.json").exists():
                break
            time.sleep(0.05)
        wait_process_exit(pid)
        cls.temp.cleanup()

    def send(self, target="success", **params):
        return self.client.call("send", sender="review", target=target, key=str(uuid.uuid4()), prompt="Read values and sum them.", **params)["task_id"]

    def test_execute_and_return(self):
        task_id = self.send()
        task = self.client.wait(task_id, 10)
        self.assertEqual(task["state"], "succeeded", task)
        self.assertIn("sum=42", task["result"]["summary"])
        self.assertTrue((self.base / "success" / f"receipt-{task_id}.txt").exists())
        self.assertNotIn("payload", task)

    def test_automatic_callback_execution(self):
        task = self.client.wait(self.send(reply_mode="run"), 10)
        follow = self.client.wait(task["result"]["callback_task_id"], 10)
        self.assertEqual(follow["state"], "succeeded")
        self.assertIn("callback executed", follow["result"]["summary"])
        self.assertIsNone(follow["result"]["callback_task_id"])

    def test_provider_error_exit_and_invalid_output(self):
        for target in ("error", "exit", "invalid", "missing"):
            with self.subTest(target=target):
                task = self.client.wait(self.send(target), 10)
                self.assertEqual(task["state"], "failed", task)
                self.assertTrue(task["result"]["error"])

    def test_timeout_and_output_cap(self):
        timed = self.client.wait(self.send("sleep", timeout=1), 10)
        self.assertEqual(timed["state"], "timed_out")
        flood = self.client.wait(self.send("flood"), 10)
        self.assertEqual(flood["state"], "failed")
        self.assertEqual(flood["result"]["error"], "output_limit")
        self.assertLessEqual((Path(flood["result"]["logs_dir"]) / "stdout.log").stat().st_size, 16384)

    def test_cancel_running_and_queued(self):
        active = self.send("sleep")
        for _ in range(100):
            if self.client.call("task", task_id=active)["state"] == "running":
                break
            time.sleep(0.02)
        queued = self.send("sleep")
        self.client.call("cancel", task_id=queued)
        self.client.call("cancel", task_id=active)
        self.assertEqual(self.client.wait(active, 10)["state"], "cancelled")
        self.assertEqual(self.client.wait(queued, 10)["state"], "cancelled")

    def test_loopback_auth_and_origin(self):
        endpoint = json.loads((self.state / "endpoint.json").read_text())
        for headers, code in (({}, 401), ({"Authorization": "Bearer " + endpoint["token"], "Origin": "https://example.com"}, 403)):
            req = urllib.request.Request(endpoint["url"] + "/call", data=b'{"method":"health"}', headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req)
            self.assertEqual(exc.exception.code, code)
            exc.exception.close()

    def test_idempotent_http_submission(self):
        params = dict(sender="review", target="success", key=str(uuid.uuid4()), prompt="same")
        first = self.client.call("send", **params)
        second = self.client.call("send", **params)
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertTrue(second["duplicate"])
        with self.assertRaisesRegex(RuntimeError, "idempotency_conflict"):
            self.client.call("send", **{**params, "prompt": "changed"})

    def test_reject_arbitrary_commands_paths_and_large_messages(self):
        base = dict(sender="review", target="success", key=str(uuid.uuid4()), prompt="hello")
        for extra in ({"command": ["calc.exe"]}, {"context": ["../outside.txt"]}, {"prompt": "x" * 6001}, {"timeout": -1}):
            with self.assertRaises(RuntimeError):
                self.client.call("send", **{**base, **extra})

    def test_shell_metacharacters_are_data(self):
        text = '中文 $(New-Item evil) `whoami` & echo BAD > injection.txt'
        task = self.client.wait(self.client.call("send", sender="review", target="success", key=str(uuid.uuid4()), prompt=text)["task_id"], 10)
        self.assertEqual(task["state"], "succeeded")
        self.assertIn(text, (Path(task["result"]["logs_dir"]) / "prompt.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.base / "success" / "injection.txt").exists())

    def test_mcp_initialize_list_and_real_send(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "bridge_send", "arguments": {
                "target": "success", "prompt": "sum", "key": str(uuid.uuid4())}}}]
        proc = subprocess.run([sys.executable, str(ROOT / "bridge.py"), "--state", str(self.state), "mcp", "--agent", "review"],
                              input="\n".join(json.dumps(r) for r in requests) + "\n", capture_output=True, text=True, timeout=10)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(len(responses), 3)
        self.assertEqual(len(responses[1]["result"]["tools"]), 9)
        task_id = json.loads(responses[2]["result"]["content"][0]["text"])["task_id"]
        self.assertEqual(self.client.wait(task_id, 10)["state"], "succeeded")

    def test_mcp_register_and_roles(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "bridge_register", "arguments": {
                "session": "review-mcp-1", "transport": "mcp", "description": "test"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "bridge_roles", "arguments": {}}}]
        proc = subprocess.run([sys.executable, str(ROOT / "bridge.py"), "--state", str(self.state),
                               "--config", str(self.config), "mcp", "--agent", "review", "--project", "default"],
                              input="\n".join(json.dumps(r) for r in requests) + "\n",
                              capture_output=True, text=True, encoding="utf-8", timeout=10)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        registered = json.loads(responses[1]["result"]["content"][0]["text"])
        self.assertEqual(registered["address"], "review")
        self.assertEqual(registered["session"]["transport"], "mcp")
        roles = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertTrue(any(role["address"] == "review" for project in roles["projects"] for role in project["roles"]))
        self.client.call("session_remove", session="review-mcp-1")

    def test_second_broker_cannot_steal_queue(self):
        with self.assertRaisesRegex(RuntimeError, "already_running"):
            Broker(self.state, read_config(self.config))
        self.assertEqual(self.client.call("health")["status"], "running")

    def test_timeout_kills_descendant(self):
        task = self.client.wait(self.send("child", timeout=2), 10)
        self.assertEqual(task["state"], "timed_out")
        pid = int((self.base / "child" / "child.pid").read_text())
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32")
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k.OpenProcess.restype = wintypes.HANDLE
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            handle = k.OpenProcess(0x1000, False, pid)
            if handle:
                code = wintypes.DWORD()
                k.GetExitCodeProcess(handle, ctypes.byref(code))
                k.CloseHandle(handle)
                self.assertNotEqual(code.value, 259, "descendant still running")
        else:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass

    def test_workspace_serialization_and_queue_expiration(self):
        active = self.send("sleep", timeout=2)
        for _ in range(100):
            if self.client.call("task", task_id=active)["state"] == "running":
                break
            time.sleep(0.02)
        queued = self.send("same-workspace", queue_ttl=1)
        time.sleep(0.2)
        self.assertEqual(self.client.call("task", task_id=queued)["state"], "queued")
        self.assertEqual(self.client.wait(active, 10)["state"], "timed_out")
        result = self.client.wait(queued, 10)
        self.assertEqual(result["state"], "timed_out")
        self.assertEqual(result["result"]["error"], "queue_deadline")
        self.assertFalse((self.base / "sleep" / f"receipt-{queued}.txt").exists())


class RestartTests(unittest.TestCase):
    def test_real_process_crash_preserves_queue_and_marks_unknown(self):
        import signal
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "config.json"
            config.write_text(json.dumps({"endpoints": {"worker": {"adapter": "fixture", "cwd": tmp,
                "command": [sys.executable, str(ROOT / "examples" / "fixture_runtime.py")], "args": ["sleep"]}}}))
            state = base / "state"
            health = start(state, config)
            client = Client(state)
            task_id = client.call("send", sender="test", target="worker", key="crash", prompt="wait", timeout=60)["task_id"]
            for _ in range(100):
                if (state / "runs" / task_id / "prompt.txt").exists():
                    break
                time.sleep(0.05)
            queued = client.call("send", sender="test", target="worker", key="after-crash", prompt="run next")["task_id"]
            os.kill(health["pid"], signal.SIGTERM)
            time.sleep(0.4)
            changed = json.loads(config.read_text())
            changed["endpoints"]["worker"]["args"] = ["success"]
            config.write_text(json.dumps(changed))
            restarted = start(state, config)
            try:
                self.assertEqual(restarted["recovered"], 1)
                self.assertEqual(client.call("task", task_id=task_id)["state"], "interrupted")
                self.assertEqual(client.wait(queued, 10)["state"], "succeeded")
                self.assertEqual(len(client.call("inbox", recipient="test")["messages"]), 2)
            finally:
                client.call("shutdown")
                for _ in range(100):
                    if not (state / "endpoint.json").exists():
                        break
                    time.sleep(0.05)
                wait_process_exit(restarted["pid"])


class ParsingTests(unittest.TestCase):
    def test_codex_error_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / "final.txt"
            final.write_text("partial")
            result, ok = parse_result("codex", '{"type":"turn.failed"}', final)
            self.assertFalse(ok)
            self.assertEqual(result["error"], "runtime_turn_failed")

    def test_codex_usage_and_final_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout = '\n'.join([json.dumps({"type":"thread.started","thread_id":"abc"}),
                                json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}),
                                json.dumps({"type":"turn.completed","usage":{"input_tokens":12}})])
            result, ok = parse_result("codex", stdout, Path(tmp) / "no-file")
            self.assertTrue(ok)
            self.assertEqual(result["usage"]["input_tokens"], 12)
            self.assertEqual(result["session_id"], "abc")


if __name__ == "__main__":
    unittest.main(verbosity=2)

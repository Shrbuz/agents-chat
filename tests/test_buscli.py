"""CLI-level tests for `agents-chat bus ...` (no broker, file bus only)."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest

from bridge import buscli

PROJECT_ID = "11111111-1111-1111-1111-111111111111"


def namespace(**values):
    defaults = {
        "bus_command": None, "role": None, "session": None, "token": None,
        "task_id": None, "body": None, "evidence": [], "acceptance": [],
        "priority": "P2", "due_at": None, "data_file": None, "sender_role": None,
        "target_role": None, "title": None, "limit": 50, "message_id": [],
        "outcome": None, "all": False, "sequence": None,
    }
    defaults.update(values)
    return argparse.Namespace(**defaults)


class BusCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "bus"
        self.root.mkdir()
        (self.root / "project.json").write_text(json.dumps({
            "schema_version": 1, "project_id": PROJECT_ID,
            "roles": {"frontend": "frontend_msg", "backend": "backend_msg"},
        }), encoding="utf-8")

    def request(self):
        return buscli.run(namespace(
            bus_command="request", sender_role="frontend", target_role="backend",
            session="fe-1", title="登录页报错", body="提交后 500", acceptance=["HTTP 200"],
        ), self.root)

    def test_cli_covers_the_whole_lifecycle(self):
        created = self.request()
        self.assertEqual(created["status"], "pending")
        task_id = created["task_id"]

        claimed = buscli.run(namespace(
            bus_command="claim", role="backend", session="be-1", task_id=task_id), self.root)
        self.assertEqual(claimed["status"], "in_progress")
        self.assertTrue(claimed["claim_token"])

        reported = buscli.run(namespace(
            bus_command="result", role="backend", session="be-1", task_id=task_id,
            token=claimed["claim_token"], body="已修复", evidence=["测试通过"]), self.root)
        self.assertEqual(reported["status"], "resolved")

        closed = buscli.run(namespace(
            bus_command="verify", role="frontend", session="fe-1", task_id=task_id,
            outcome="pass", body="复测通过"), self.root)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(buscli.run(namespace(bus_command="status"), self.root)["tasks"], [])

    def test_cli_rejects_request_without_acceptance(self):
        with self.assertRaises(ValueError):
            buscli.run(namespace(
                bus_command="request", sender_role="frontend", target_role="backend",
                session="fe-1", title="t", body="b", acceptance=[]), self.root)

    def test_cli_verify_requires_the_requester(self):
        task_id = self.request()["task_id"]
        token = buscli.run(namespace(
            bus_command="claim", role="backend", session="be-1", task_id=task_id), self.root)["claim_token"]
        buscli.run(namespace(
            bus_command="result", role="backend", session="be-1", task_id=task_id,
            token=token, body="done", evidence=["x"]), self.root)
        with self.assertRaises(ValueError):
            buscli.run(namespace(
                bus_command="verify", role="backend", session="be-1", task_id=task_id,
                outcome="pass", body="self-approved"), self.root)

    def test_cli_inbox_and_mark_read(self):
        created = self.request()
        inbox = buscli.run(namespace(
            bus_command="inbox", role="backend", session="be-1"), self.root)
        self.assertEqual(inbox["total_unread"], 1)
        buscli.run(namespace(
            bus_command="mark-read", role="backend", session="be-1",
            message_id=[created["message_id"]]), self.root)
        self.assertEqual(buscli.run(namespace(
            bus_command="inbox", role="backend", session="be-1"), self.root)["total_unread"], 0)

    def test_cli_mark_read_through_sequence(self):
        self.request()
        inbox = buscli.run(namespace(
            bus_command="inbox", role="backend", session="be-1"), self.root)
        sequence = inbox["suggested_mark_read_through_sequence"]
        result = buscli.run(namespace(
            bus_command="mark-read-through-sequence", role="backend",
            session="be-1", sequence=sequence), self.root)
        self.assertEqual(result["marked_read_through_sequence"], sequence)
        self.assertEqual(buscli.run(namespace(
            bus_command="inbox", role="backend", session="be-1"), self.root)["total_unread"], 0)

    def test_cli_progress_and_blocked_stay_within_lease(self):
        task_id = self.request()["task_id"]
        token = buscli.run(namespace(
            bus_command="claim", role="backend", session="be-1", task_id=task_id), self.root)["claim_token"]
        buscli.run(namespace(
            bus_command="progress", role="backend", session="be-1", task_id=task_id,
            token=token, body="正在定位"), self.root)
        blocked = buscli.run(namespace(
            bus_command="blocked", role="backend", session="be-1", task_id=task_id,
            token=token, body="等待部署"), self.root)
        self.assertEqual(blocked["status"], "blocked")
        # blocked releases the claim, so the old token can no longer report.
        with self.assertRaises(ValueError):
            buscli.run(namespace(
                bus_command="result", role="backend", session="be-1", task_id=task_id,
                token=token, body="done", evidence=["x"]), self.root)


if __name__ == "__main__":
    unittest.main()

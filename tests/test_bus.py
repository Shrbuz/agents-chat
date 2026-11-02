"""File-bus protocol tests: lifecycle, leases, damage detection, conformance."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

from bridge.bus import Bus

ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPT = ROOT / "tool-skill" / "agent-chat" / "scripts" / "agent_chat.py"
PROJECT_ID = "11111111-1111-1111-1111-111111111111"
VOLATILE = ("token", "expires_at", "created_at", "updated_at", "message_id", "sequence")


def make_bus(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.json").write_text(json.dumps({
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "roles": {"frontend": "frontend_msg", "backend": "backend_msg"},
    }), encoding="utf-8")
    return Bus(root)


def request(bus, message_id="22222222-2222-2222-2222-222222222222"):
    return bus.submit("request", "frontend", "fe-1", recipient="backend", message_id=message_id, data={
        "title": "登录页报错", "body": "点提交后 500", "acceptance": ["返回 200", "日志无异常"],
        "priority": "P1",
    })


def normalize(events):
    """Compare shape and content while ignoring per-run identifiers and clocks."""
    task_ids = {}
    shaped = []
    for event in events:
        task_ids.setdefault(event["task_id"], f"t{len(task_ids) + 1}")
        data = {k: v for k, v in event["data"].items() if k not in VOLATILE}
        shaped.append({
            "type": event["type"],
            "from": event["from"],
            "to": event["to"],
            "task": task_ids[event["task_id"]],
            "in_reply_to": bool(event.get("in_reply_to")),
            "data": data,
        })
    return shaped


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bus = make_bus(Path(self.tmp.name) / "bus")

    def test_full_lifecycle_closes_on_verify_pass(self):
        bus = self.bus
        created = request(bus)
        task_id = created["task"]["task_id"]
        self.assertEqual(created["task"]["status"], "pending")

        claimed = bus.submit("claim", "backend", "be-1", task_id=task_id)
        token = claimed["event"]["data"]["token"]
        self.assertEqual(claimed["task"]["status"], "in_progress")

        reported = bus.submit("result", "backend", "be-1", task_id=task_id, token=token,
                              data={"body": "已修复", "evidence": ["pytest 通过"]})
        self.assertEqual(reported["task"]["status"], "resolved")

        closed = bus.submit("verify", "frontend", "fe-1", task_id=task_id,
                            data={"outcome": "pass", "body": "复测通过"})
        self.assertEqual(closed["task"]["status"], "closed")
        # verify(pass) records acceptance and closure in one event, so nothing is left open.
        self.assertEqual(bus.status()["tasks"], [])

    def test_verify_fail_returns_task_to_pending(self):
        bus = self.bus
        task_id = request(bus)["task"]["task_id"]
        token = bus.submit("claim", "backend", "be-1", task_id=task_id)["event"]["data"]["token"]
        bus.submit("result", "backend", "be-1", task_id=task_id, token=token,
                   data={"body": "改完了", "evidence": ["日志已贴"]})
        reopened = bus.submit("verify", "frontend", "fe-1", task_id=task_id,
                              data={"outcome": "fail", "body": "仍然 500"})
        self.assertEqual(reopened["task"]["status"], "pending")
        # A new session may claim it again and receives a fresh token.
        again = bus.submit("claim", "backend", "be-2", task_id=task_id)
        self.assertNotEqual(again["event"]["data"]["token"], token)

    def test_only_assignee_claims_and_only_requester_verifies(self):
        bus = self.bus
        task_id = request(bus)["task"]["task_id"]
        with self.assertRaises(ValueError):
            bus.submit("claim", "frontend", "fe-1", task_id=task_id)
        token = bus.submit("claim", "backend", "be-1", task_id=task_id)["event"]["data"]["token"]
        bus.submit("result", "backend", "be-1", task_id=task_id, token=token,
                   data={"body": "ok", "evidence": ["x"]})
        with self.assertRaises(ValueError):
            bus.submit("verify", "backend", "be-1", task_id=task_id, data={"outcome": "pass", "body": "ok"})

    def test_expired_lease_cannot_report_and_new_claim_wins(self):
        bus = self.bus
        task_id = request(bus)["task"]["task_id"]
        token = bus.submit("claim", "backend", "be-1", task_id=task_id,
                           lease_seconds=30)["event"]["data"]["token"]
        # Age the claim on disk; the event log stays exactly as written except the expiry.
        for path in (Path(self.tmp.name) / "bus").glob("*_msg/*.json"):
            event = json.loads(path.read_text(encoding="utf-8"))
            if event["type"] == "claim":
                event["data"]["expires_at"] = "2000-01-01T00:00:00.000000+00:00"
                path.write_text(json.dumps(event, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            bus.submit("result", "backend", "be-1", task_id=task_id, token=token,
                       data={"body": "late", "evidence": ["x"]})
        fresh = bus.submit("claim", "backend", "be-2", task_id=task_id)
        self.assertNotEqual(fresh["event"]["data"]["token"], token)

    def test_inbox_is_session_scoped_and_receipts_persist(self):
        bus = self.bus
        created = request(bus)
        inbox = bus.inbox("backend", "be-1")
        self.assertEqual(inbox["total_unread"], 1)
        bus.mark_read("backend", "be-1", [created["event"]["message_id"]])
        self.assertEqual(bus.inbox("backend", "be-1")["total_unread"], 0)
        # A different session has its own receipts, so it still sees the request.
        self.assertEqual(bus.inbox("backend", "be-2")["total_unread"], 1)
        with self.assertRaises(ValueError):
            bus.mark_read("backend", "be-1", ["33333333-3333-3333-3333-333333333333"])

    def test_duplicate_message_id_is_idempotent_but_not_reusable(self):
        bus = self.bus
        first = request(bus)
        self.assertFalse(first["duplicate"])
        again = request(bus)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["event"]["message_id"], first["event"]["message_id"])
        with self.assertRaises(ValueError):
            bus.submit("request", "frontend", "fe-1", recipient="backend",
                       message_id="22222222-2222-2222-2222-222222222222",
                       data={"title": "别的", "body": "别的", "acceptance": ["x"]})

    def test_request_requires_acceptance_and_priority(self):
        bus = self.bus
        with self.assertRaises(ValueError):
            bus.submit("request", "frontend", "fe-1", recipient="backend",
                       data={"title": "t", "body": "b", "acceptance": []})
        with self.assertRaises(ValueError):
            bus.submit("request", "frontend", "fe-1", recipient="backend",
                       data={"title": "t", "body": "b", "acceptance": ["x"], "priority": "P9"})


class DamageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "bus"
        self.bus = make_bus(self.root)

    def test_gap_in_sequence_is_refused(self):
        task_id = request(self.bus)["task"]["task_id"]
        token = self.bus.submit("claim", "backend", "be-1", task_id=task_id)["event"]["data"]["token"]
        self.bus.submit("result", "backend", "be-1", task_id=task_id, token=token,
                        data={"body": "done", "evidence": ["x"]})
        # Remove the claim in the middle: the remaining sequences 1 and 3 leave a gap,
        # so replay must refuse instead of silently rebuilding a different history.
        ordered = sorted(self.root.glob("*_msg/*.json"), key=lambda p: p.name)
        ordered[1].unlink()
        with self.assertRaises(ValueError):
            self.bus.load()

    def test_deleted_tail_is_not_invented(self):
        # A missing newest event is a consistent prefix; the bus must not fabricate it.
        task_id = request(self.bus)["task"]["task_id"]
        self.bus.submit("claim", "backend", "be-1", task_id=task_id)
        newest = max(self.root.glob("*_msg/*.json"), key=lambda p: p.name)
        newest.unlink()
        _, tasks = self.bus.load()
        self.assertEqual(tasks[task_id]["status"], "pending")


@unittest.skipUnless(SKILL_SCRIPT.is_file(), "bundled agent-chat skill not present")
class ConformanceTests(unittest.TestCase):
    """The native bus must emit what the standalone skill implementation emits."""

    def test_same_sequence_produces_equivalent_events(self):
        spec = importlib.util.spec_from_file_location("skill_agent_chat", SKILL_SCRIPT)
        skill = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(skill)

        with tempfile.TemporaryDirectory() as tmp:
            native_root = Path(tmp) / "native"
            native = make_bus(native_root)

            project = Path(tmp) / "project"
            (project / "agent_chat").mkdir(parents=True)
            (project / "agent_chat" / "project.json").write_text(json.dumps({
                "schema_version": 1, "project_id": PROJECT_ID,
                "roles": {"frontend": "frontend_msg", "backend": "backend_msg"},
                "storage": {"mode": "git-primary"},
            }), encoding="utf-8")
            os.environ["AGENT_CHAT_ROOT"] = str(Path(tmp) / "skillbus")
            self.addCleanup(os.environ.pop, "AGENT_CHAT_ROOT", None)
            reference = skill.Bus(project)

            sequences = []
            for bus in (native, reference):
                created = request(bus)
                task_id = created["task"]["task_id"]
                claimed = bus.submit("claim", "backend", "be-1", task_id=task_id)
                token = claimed["event"]["data"]["token"]
                bus.submit("progress", "backend", "be-1", task_id=task_id, token=token,
                           data={"body": "正在处理"})
                bus.submit("result", "backend", "be-1", task_id=task_id, token=token,
                           data={"body": "已修复", "evidence": ["测试通过"]})
                bus.submit("verify", "frontend", "fe-1", task_id=task_id,
                           data={"outcome": "pass", "body": "复测通过"})
                events, tasks = bus.load()
                sequences.append((normalize(events), {
                    k: v["status"] for k, v in tasks.items()
                }))

            self.assertEqual(sequences[0][0], sequences[1][0])
            self.assertEqual(list(sequences[0][1].values()), list(sequences[1][1].values()))


if __name__ == "__main__":
    unittest.main()

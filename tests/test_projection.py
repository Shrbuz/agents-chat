"""Store projection of the file bus, and the wake cursor living in the store."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from bridge.agentchat_bridge import AgentChatBridge
from bridge.bus import Bus
from bridge.config import read_config
from bridge.store import Store

PROJECT_ID = "11111111-1111-1111-1111-111111111111"


def make_bus(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for folder in ("frontend_msg", "backend_msg"):
        (root / folder).mkdir(exist_ok=True)
    (root / "project.json").write_text(json.dumps({
        "schema_version": 1, "project_id": PROJECT_ID,
        "roles": {"frontend": "frontend_msg", "backend": "backend_msg"},
    }), encoding="utf-8")
    return Bus(root)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.store = Store(base / "state" / "bridge.db")
        self.bus = make_bus(base / "bus")

    def seed(self):
        task_id = self.bus.submit("request", "frontend", "fe-1", recipient="backend", data={
            "title": "登录页报错", "body": "提交后 500", "acceptance": ["HTTP 200"], "priority": "P1",
        })["task"]["task_id"]
        return task_id

    def test_projection_mirrors_replay(self):
        task_id = self.seed()
        _, tasks = self.bus.load()
        self.assertEqual(self.store.sync_bus(PROJECT_ID, tasks), 1)

        rows = self.store.bus_projection(PROJECT_ID)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["task_id"], task_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["priority"], "P1")
        self.assertEqual(row["requester"], "frontend")
        self.assertEqual(row["assignee"], "backend")
        self.assertEqual(row["title"], "登录页报错")

    def test_projection_keeps_closed_tasks_out_of_the_default_view(self):
        task_id = self.seed()
        token = self.bus.submit("claim", "backend", "be-1", task_id=task_id)["event"]["data"]["token"]
        self.bus.submit("result", "backend", "be-1", task_id=task_id, token=token,
                        data={"body": "done", "evidence": ["x"]})
        self.bus.submit("verify", "frontend", "fe-1", task_id=task_id,
                        data={"outcome": "pass", "body": "ok"})
        _, tasks = self.bus.load()
        self.store.sync_bus(PROJECT_ID, tasks)

        self.assertEqual(self.store.bus_projection(PROJECT_ID), [])
        closed = self.store.bus_projection(PROJECT_ID, include_closed=True)
        self.assertEqual(closed[0]["status"], "closed")

    def test_projection_drops_rows_that_left_the_log(self):
        task_id = self.seed()
        _, tasks = self.bus.load()
        self.store.sync_bus(PROJECT_ID, tasks)
        self.assertEqual(len(self.store.bus_projection(PROJECT_ID)), 1)
        # A rebuilt bus no longer contains the task; the mirror must not keep it.
        self.store.sync_bus(PROJECT_ID, {})
        self.assertEqual(self.store.bus_projection(PROJECT_ID), [])

    def test_cursor_roundtrip(self):
        self.assertEqual(self.store.bus_cursor("alpha"), "")
        self.store.set_bus_cursor("alpha", "00000003_x.json")
        self.assertEqual(self.store.bus_cursor("alpha"), "00000003_x.json")
        self.store.set_bus_cursor("alpha", "00000004_y.json")
        self.assertEqual(self.store.bus_cursor("alpha"), "00000004_y.json")


class BridgeProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bus_root = self.root / "bus"
        make_bus(self.bus_root)
        self.state = self.root / "state"
        self.store = Store(self.state / "bridge.db")
        config = {"version": 2, "projects": {"alpha": {
            "name": "Alpha",
            "root": str(self.root / "project"),
            "agent_chat": {"bus": str(self.bus_root),
                           "roles": {"backend": "backend", "frontend": "frontend"}},
            "roles": {"backend": {"adapter": "fixture", "command": ["python", "-c", "print(1)"], "cwd": "."},
                      "frontend": {"adapter": "fixture", "command": ["python", "-c", "print(1)"], "cwd": "."}},
        }}}
        (self.root / "project").mkdir(exist_ok=True)
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.bridge = AgentChatBridge(self.state, read_config(config_path), self.store, lambda: None)

    def test_scan_advances_the_store_cursor_and_fills_the_projection(self):
        project = self.bridge.projects[0]
        self.bridge._prime_cursors()
        # Cursors are per role now, so one stuck role cannot block the others.
        self.assertEqual(json.loads(self.store.bus_cursor("alpha")), {"backend": "", "frontend": ""})

        message_id = str(uuid.uuid4())
        event = {"schema_version": 1, "project_id": PROJECT_ID, "sequence": 1,
                 "message_id": message_id, "task_id": str(uuid.uuid4()), "type": "request",
                 "from": "frontend", "to": "backend", "session": "fe-1",
                 "created_at": "2026-01-01T00:00:00.000000+00:00", "in_reply_to": None,
                 "data": {"title": "实现登录 API", "body": "详情", "acceptance": ["返回 200"]}}
        (self.bus_root / "backend_msg" / f"00000001_{message_id}.json").write_text(
            json.dumps(event, ensure_ascii=False), encoding="utf-8")

        self.bridge._scan(project)

        self.assertEqual(json.loads(self.store.bus_cursor("alpha"))["backend"], f"00000001_{message_id}.json")
        rows = self.store.bus_projection("alpha")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        # The wake cursor is store state now; no side-car JSON file is written.
        self.assertFalse((self.state / "agent-chat-cursors.json").exists())

    def test_scan_survives_a_damaged_bus(self):
        project = self.bridge.projects[0]
        self.bridge._prime_cursors()
        # An event that cannot be replayed must not break the watcher.
        (self.bus_root / "backend_msg" / "00000001_broken.json").write_text("{not json", encoding="utf-8")
        self.bridge._scan(project)
        self.assertEqual(json.loads(self.store.bus_cursor("alpha"))["backend"], "00000001_broken.json")


if __name__ == "__main__":
    unittest.main()


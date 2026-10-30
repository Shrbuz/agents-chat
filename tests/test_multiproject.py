import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import onboarding
from bridge.broker import Broker
from bridge.config import (ConfigError, allowed_target, find_role, normalize_config,
                           projects_summary, read_config)
from bridge.store import Store

FIXTURE = [sys.executable, str(ROOT / "examples" / "fixture_runtime.py")]


def project_config(root):
    (root / "alpha").mkdir(exist_ok=True)
    (root / "beta").mkdir(exist_ok=True)
    return {
        "version": 2,
        "projects": {
            "alpha": {"name": "Alpha", "root": str(root / "alpha"), "roles": {
                "backend": {"adapter": "fixture", "command": FIXTURE, "args": ["success"], "cwd": "."},
                "frontend": {"adapter": "fixture", "command": FIXTURE, "args": ["success"], "cwd": ".",
                             "peers": ["alpha.backend"]}}},
            "beta": {"name": "Beta", "root": str(root / "beta"), "roles": {
                "frontend": {"adapter": "fixture", "command": FIXTURE, "args": ["success"], "cwd": "."}}},
        },
    }


class ConfigTests(unittest.TestCase):
    def test_project_addresses_and_ambiguity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = project_config(root)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(data), encoding="utf-8")
            config = read_config(config_path)
            self.assertEqual(sorted(config["endpoints"]), ["alpha.backend", "alpha.frontend", "beta.frontend"])
            self.assertEqual(find_role(config, "backend"), "alpha.backend")
            self.assertEqual(find_role(config, "frontend", "beta"), "beta.frontend")
            self.assertEqual(find_role(config, "alpha.frontend"), "alpha.frontend")
            with self.assertRaisesRegex(ConfigError, "ambiguous_role"):
                find_role(config, "frontend")
            with self.assertRaisesRegex(ConfigError, "unknown_role"):
                find_role(config, "ops")
            summary = projects_summary(config)
            self.assertEqual({item["id"] for item in summary}, {"alpha", "beta"})

    def test_peer_allow_list_is_project_aware(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(project_config(root)), encoding="utf-8")
            config = read_config(config_path)
            self.assertTrue(allowed_target(config, "alpha.frontend", "alpha.backend"))
            self.assertFalse(allowed_target(config, "alpha.frontend", "beta.frontend"))
            self.assertTrue(allowed_target(config, "alpha.backend", "zeta.unconfigured"))

    def test_legacy_config_keeps_plain_role_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"endpoints": {
                "codex": {"adapter": "fixture", "command": FIXTURE, "args": ["success"], "cwd": str(root)},
                "qoder": {"adapter": "fixture", "command": FIXTURE, "args": ["success"], "cwd": str(root)}}}))
            config = read_config(config_path)
            self.assertEqual(sorted(config["endpoints"]), ["codex", "qoder"])
            self.assertEqual(config["legacy_project"], "default")
            self.assertEqual(find_role(config, "default.codex"), "codex")
            self.assertEqual(config["endpoints"]["codex"]["project"], "default")


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(project_config(self.root)), encoding="utf-8")
        self.broker = Broker(self.root / "state", read_config(self.config_path))
        self.broker.start()

    def tearDown(self):
        self.broker.close()
        self.temp.cleanup()

    def test_cross_project_send_and_inbox(self):
        sent = self.broker.call("send", {"sender": "alpha.backend", "target": "beta.frontend",
                                         "prompt": "cross project", "key": "cross-1", "timeout": 10})
        self.assertEqual(sent["sender"], "alpha.backend")
        self.assertEqual(sent["target"], "beta.frontend")
        result = self.broker.call("wait", {"task_id": sent["task_id"], "seconds": 10})
        self.assertEqual(result["state"], "succeeded")
        inbox = self.broker.call("inbox", {"recipient": "alpha.backend", "consumer": "test"})
        self.assertEqual(inbox["messages"][0]["from"], "beta.frontend")
        self.assertIn("Fixture process", inbox["messages"][0]["summary"])

    def test_cross_project_reply_mode_run(self):
        sent = self.broker.call("send", {"sender": "alpha.backend", "target": "beta.frontend",
                                         "prompt": "work", "key": "callback-1", "timeout": 10,
                                         "reply_mode": "run"})
        completed = self.broker.call("wait", {"task_id": sent["task_id"], "seconds": 10})
        self.assertEqual(completed["state"], "succeeded")
        callback_id = completed["result"]["callback_task_id"]
        self.assertTrue(callback_id)
        callback = self.broker.call("wait", {"task_id": callback_id, "seconds": 10})
        self.assertEqual(callback["state"], "succeeded")
        self.assertEqual(callback["target"], "alpha.backend")

    def test_peers_block_unlisted_target(self):
        with self.assertRaisesRegex(ValueError, "target_not_allowed"):
            self.broker.call("send", {"sender": "alpha.frontend", "target": "beta.frontend",
                                      "prompt": "not allowed", "key": "deny-1"})

    def test_session_registry_and_desktop_binding(self):
        registered = self.broker.call("session_register", {"project": "alpha", "role": "backend",
                                                           "session": "backend-abc", "transport": "chat",
                                                           "description": "test chat"})
        self.assertEqual(registered["address"], "alpha.backend")
        self.assertTrue(registered["session"]["online"])
        listed = self.broker.call("session_list", {"project": "alpha"})
        self.assertEqual([item["id"] for item in listed["sessions"]], ["backend-abc"])
        self.broker.call("session_heartbeat", {"session": "backend-abc"})
        self.broker.call("desktop_bind", {"agent": "alpha.backend", "session_id": "desk-1",
                                          "transport": "codex_app_tools"})
        sessions = self.broker.call("session_list", {"project": "alpha"})["sessions"]
        self.assertIn("desktop:desk-1", [item["id"] for item in sessions])
        self.broker.call("desktop_bind", {"agent": "alpha.backend", "session_id": None})
        sessions = self.broker.call("session_list", {"project": "alpha"})["sessions"]
        self.assertNotIn("desktop:desk-1", [item["id"] for item in sessions])
        self.broker.call("session_remove", {"session": "backend-abc"})
        self.assertEqual(self.broker.call("session_list", {"project": "alpha"})["sessions"], [])

    def test_roles_expose_projects_modes_and_pending(self):
        result = self.broker.call("roles", {})
        projects = {item["id"]: item for item in result["projects"]}
        self.assertEqual(set(projects), {"alpha", "beta"})
        backend = next(role for role in projects["alpha"]["roles"] if role["role"] == "backend")
        self.assertEqual(backend["mode"], "independent_cli_task")
        self.assertFalse(backend["pending"])


class SetupTests(unittest.TestCase):
    def test_setup_is_idempotent_and_writes_agent_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            project.mkdir()
            config_path = root / "config.json"
            result = onboarding.setup(config_path, project, project="demo", roles=[
                {"role": "backend", "adapter": "fixture", "description": "API"},
                {"role": "frontend", "adapter": "fixture", "description": "UI"},
            ], state=root / "state", write_agents=True)
            self.assertEqual(result["created_roles"], ["backend", "frontend"])
            entry = json.loads((project / ".agents-chat/project.json").read_text(encoding="utf-8"))
            self.assertEqual(entry["project_id"], "demo")
            self.assertIn("backend", entry["roles"])
            self.assertTrue((project / ".agents-chat/prompts/backend.md").is_file())
            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("agents-chat:begin", agents)
            again = onboarding.setup(config_path, project, project="demo", roles=[
                {"role": "backend", "adapter": "fixture"}], state=root / "state", write_agents=True)
            self.assertEqual(again["kept_roles"], ["backend"])
            self.assertEqual(again["created_roles"], [])
            text = (project / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(text.count("agents-chat:begin"), 1)

    def test_setup_links_existing_agent_chat_bus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            (project / "agent_chat/backend_msg").mkdir(parents=True)
            (project / "agent_chat/frontend_msg").mkdir(parents=True)
            (project / "agent_chat/project.json").write_text(json.dumps({
                "schema_version": 1, "project_id": "demo",
                "roles": {"backend": "backend_msg", "frontend": "frontend_msg"}}), encoding="utf-8")
            config_path = root / "config.json"
            onboarding.setup(config_path, project, project="demo", roles=[
                {"role": "backend", "adapter": "fixture"},
                {"role": "frontend", "adapter": "fixture"}], state=root / "state", write_agents=False)
            config = read_config(config_path)
            chat = config["projects"]["demo"]["agent_chat"]
            self.assertEqual(chat["roles"], {"backend": "backend", "frontend": "frontend"})
            prompt = (project / ".agents-chat/prompts/backend.md").read_text(encoding="utf-8")
            self.assertIn("agent-chat", prompt)
            self.assertIn(str(project / "agent_chat"), prompt)


class AgentChatBridgeTests(unittest.TestCase):
    def test_new_bus_event_wakes_role_once_without_result_mailbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo"
            bus = project / "agent_chat"
            (bus / "backend_msg").mkdir(parents=True)
            (bus / "frontend_msg").mkdir(parents=True)
            (bus / "project.json").write_text(json.dumps({
                "schema_version": 1, "project_id": "demo",
                "roles": {"backend": "backend_msg", "frontend": "frontend_msg"}}), encoding="utf-8")
            (bus / "backend_msg/00000000_old.json").write_text(json.dumps({
                "schema_version": 1, "message_id": str(uuid.uuid4()), "type": "request",
                "from": "frontend", "to": "backend", "task_id": "old",
                "data": {"title": "old event"}}), encoding="utf-8")
            data = project_config(root)
            data["projects"]["alpha"] = {
                "root": str(project), "agent_chat": {
                    "bus": str(bus), "roles": {"backend": "backend", "frontend": "frontend"}},
                "roles": {
                    "backend": {"adapter": "fixture", "command": FIXTURE, "args": ["success"], "cwd": "."},
                    "frontend": {"adapter": "fixture", "command": FIXTURE, "args": ["success"], "cwd": "."}}}
            config_path = root / "config.json"
            config_path.write_text(json.dumps(data), encoding="utf-8")
            broker = Broker(root / "state", read_config(config_path))
            broker.start()
            try:
                time.sleep(2.0)  # first scan primes cursors and must not replay old.json
                self.assertEqual(len(broker.agent_chat.status()), 1)
                event = {"schema_version": 1, "project_id": "demo", "sequence": 1,
                         "message_id": str(uuid.uuid4()), "task_id": str(uuid.uuid4()),
                         "type": "request", "from": "frontend", "to": "backend",
                         "data": {"title": "实现登录 API", "acceptance": ["返回 200"]}}
                (bus / "backend_msg" / f"00000001_{event['message_id']}.json").write_text(
                    json.dumps(event, ensure_ascii=False), encoding="utf-8")
                deadline = time.monotonic() + 10
                row = None
                while time.monotonic() < deadline:
                    with broker.store.db() as db:
                        row = db.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT 1").fetchone()
                    if row and row["state"] == "succeeded":
                        break
                    time.sleep(0.2)
                self.assertIsNotNone(row)
                self.assertEqual(row["target"], "alpha.backend")
                payload = json.loads(row["payload"])
                self.assertFalse(payload["notify"])
                self.assertEqual(payload["kind"], "agent_chat_wakeup")
                self.assertIn("实现登录 API", payload["prompt"])
                self.assertIn("inbox", payload["prompt"])
                # The wakeup itself must not create an inbox result for the synthetic sender.
                self.assertEqual(broker.store.inbox("agent-chat", "test")["messages"], [])
                self.assertEqual(len(broker.store.list_sessions()), 0)
            finally:
                broker.close()


if __name__ == "__main__":
    unittest.main()

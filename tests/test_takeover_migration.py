"""Regression coverage for adapter-aware role takeover.

The bug these tests pin down: `session takeover` used to write the *old*
adapter together with a transport from the *new* client, leaving a role that
pointed at a Codex chat but claimed to be a DSH role.  Takeover now either stays
inside one adapter (a pure session switch) or performs a complete, recoverable
adapter migration.
"""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import cli
from bridge.broker import Broker
from bridge.codex_connection import normalize_connection
from bridge.config import adapter_for_transport, read_config, transport_supported
from bridge.store import Store

FIXTURE = [sys.executable, str(ROOT / "examples" / "fixture_runtime.py")]


def seed_codex_registry(state, base):
    """Give the migration tests a valid, non-running Codex app generation.

    No legacy codex-desktop.local.json is written: that would make the hub try
    to spawn a real connector. The registry is enough for origin selection.
    """
    node = base / "codex-node.exe"
    server = base / "codex-server.mjs"
    node.write_text("fixture", encoding="utf-8")
    server.write_text("fixture", encoding="utf-8")
    item = normalize_connection({
        "command": [str(node), str(server)],
        "pipe": r"\\.\pipe\agents-chat-test",
        "origin_thread": "management-chat",
        "updated_at": time.time(),
    })
    registry = {"version": 1, "active_connection_id": item["connection_id"],
                "connections": {"management-chat": item}}
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    (state / "codex-connections.local.json").write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class DirectClient:
    """Client that talks to an in-process broker instead of a subprocess."""

    def __init__(self):
        self.broker = None

    def call(self, method, **params):
        if self.broker is None:
            raise RuntimeError("broker_not_running")
        return self.broker.call(method, params)


class InProcessHub:
    """Recreate the hub in the same process so migrations are fast to test."""

    def __init__(self, state, config_path, client):
        self.state = Path(state)
        self.config_path = Path(config_path)
        self.client = client
        self.broker = None
        self.restarts = 0
        self.starts = 0
        self.stops = 0
        self.fail_next_restart = False
        self.fail_next_start = False

    def running(self):
        return self.broker is not None

    def _open(self):
        self.broker = Broker(self.state, read_config(self.config_path))
        self.broker.start()
        self.client.broker = self.broker

    def _close(self):
        if self.broker is not None:
            self.broker.close()
            self.broker = None
        self.client.broker = None

    def start(self):
        self.starts += 1
        if self.fail_next_start:
            self.fail_next_start = False
            raise RuntimeError("injected_hub_start_failure")
        self._open()
        return {"started": True}

    def restart(self):
        self.restarts += 1
        if self.fail_next_restart:
            self.fail_next_restart = False
            raise RuntimeError("injected_hub_restart_failure")
        self._close()
        self._open()
        return {"restarted": True}

    def stop(self):
        self.stops += 1
        self._close()
        return {"stopping": True}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.config_path = None
        self.project = None
        self.bus = None
        self.client = DirectClient()
        self.hub = None

    def tearDown(self):
        if self.hub is not None:
            self.hub.stop()
        self.temp.cleanup()

    def start_role(self, adapter, transport):
        project = self.base / "demo"
        project.mkdir(exist_ok=True)
        bus = project / "agent_chat"
        (bus / "_state").mkdir(parents=True, exist_ok=True)
        self.config_path = self.base / "config.json"
        self.config_path.write_text(json.dumps({
            "version": 2,
            "projects": {"demo": {
                "name": "Demo", "root": str(project),
                "agent_chat": {"bus": str(bus), "roles": {"tester": "tester"}},
                "roles": {"tester": {"adapter": adapter, "command": FIXTURE,
                                     "args": ["success"], "cwd": "."}},
            }},
        }), encoding="utf-8")
        entry_path = project / ".agents-chat" / "project.json"
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        entry_path.write_text(json.dumps({
            "schema": 1, "project_id": "demo", "project_name": "Demo",
            "project_root": str(project),
            "hub": {"config": str(self.config_path.resolve()), "state": str(self.state.resolve())},
            "roles": {"tester": {"address": "demo.tester", "adapter": adapter,
                                 "description": "", "peers": []}},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.project = project
        self.bus = bus
        seed_codex_registry(self.state, self.base)
        self.hub = InProcessHub(self.state, self.config_path, self.client)
        self.hub.start()
        self.client.call("desktop_bind", agent="demo.tester", project="demo",
                         session_id="old-chat", transport=transport)
        return project

    def migrate(self, adapter="auto", transport="codex_app_tools", session="new-chat",
                command=FIXTURE, args=("success",)):
        return cli.migrate_and_takeover(
            self.state, self.config_path, "demo", "tester",
            session=session, transport=transport, adapter=adapter,
            command=command, args=list(args) if args is not None else None,
            client=self.client, hub=self.hub)

    def role(self):
        roles = self.client.call("roles")["projects"]
        project = next(item for item in roles if item["id"] == "demo")
        return next(item for item in project["roles"] if item["role"] == "tester")

    def sessions(self):
        return self.client.call("session_list", project="demo")

    def active_map(self):
        return json.loads((self.bus / "_state" / "active-sessions.json").read_text(encoding="utf-8"))

    def assert_complete(self, result, adapter, transport, old="old-chat", new="new-chat"):
        self.assertTrue(result["adapter_changed"], result)
        self.assertEqual(result["adapter"], adapter)
        self.assertEqual(result["active_session"], new)
        self.assertTrue(result["postconditions"]["ok"], result["postconditions"])
        self.assertTrue(result["postconditions"]["checks"]["role_adapter_matches_client"])
        self.assertTrue(result["postconditions"]["checks"]["session_adapter_matches_role"])
        self.assertTrue(result["postconditions"]["checks"]["transport_matches_adapter"])
        self.assertTrue(result["postconditions"]["checks"]["single_active_session"])
        self.assertTrue(result["postconditions"]["checks"]["old_sessions_retired"])
        config = read_config(self.config_path)
        self.assertEqual(config["endpoints"]["demo.tester"]["adapter"], adapter)
        entry = json.loads((self.project / ".agents-chat" / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(entry["roles"]["tester"]["adapter"], adapter)
        role = self.role()
        self.assertEqual(role["adapter"], adapter)
        self.assertEqual(role["session_id"], new)
        self.assertEqual(role["desktop_transport"], transport)
        self.assertFalse(role["adapter_transport_mismatch"])
        listed = self.sessions()
        self.assertEqual(listed["active"]["tester"], new)
        old_row = next(item for item in listed["sessions"] if item["id"] == "desktop:" + old)
        new_row = next(item for item in listed["sessions"] if item["id"] == "desktop:" + new)
        self.assertTrue(old_row["retired"])
        self.assertFalse(new_row["retired"])
        self.assertEqual(new_row["adapter"], adapter)
        self.assertEqual(new_row["transport"], transport)
        self.assertEqual(self.active_map(), {"tester": new})
        self.assertIn("desktop:" + old, result["retired_sessions"])

    def test_dsh_to_codex_migrates_adapter_and_retires_old_session(self):
        self.start_role("dsh", "dsh_plugin")
        result = self.migrate(transport="codex_app_tools")
        self.assert_complete(result, "codex", "codex_app_tools")
        self.assertTrue(result["hub_restarted"])
        self.assertEqual(self.hub.restarts, 1)

    def test_codex_to_dsh_migrates_adapter_and_retires_old_session(self):
        self.start_role("codex", "codex_app_tools")
        result = self.migrate(transport="dsh_plugin")
        self.assert_complete(result, "dsh", "dsh_plugin")
        self.assertTrue(result["hub_restarted"])
        self.assertEqual(self.hub.restarts, 1)

    def test_codex_to_codex_is_only_a_session_switch(self):
        self.start_role("codex", "codex_app_tools")
        config_before = self.config_path.read_text(encoding="utf-8")
        entry_before = (self.project / ".agents-chat" / "project.json").read_text(encoding="utf-8")
        result = self.migrate(transport="codex_app_tools", command=None, args=None)
        self.assertFalse(result["adapter_changed"])
        self.assertFalse(result["hub_restarted"])
        self.assertEqual(self.hub.restarts, 0)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), config_before)
        self.assertEqual((self.project / ".agents-chat" / "project.json").read_text(encoding="utf-8"),
                         entry_before)
        self.assertTrue(result["postconditions"]["ok"])
        listed = self.sessions()
        self.assertEqual(listed["active"]["tester"], "new-chat")
        old_row = next(item for item in listed["sessions"] if item["id"] == "desktop:old-chat")
        self.assertTrue(old_row["retired"])

    def test_old_takeover_refuses_cross_adapter_without_leaving_half_state(self):
        self.start_role("dsh", "dsh_plugin")
        with self.assertRaisesRegex(ValueError, "adapter_transport_mismatch"):
            self.client.call("session_takeover", agent="demo.tester", project="demo",
                             session_id="codex-chat", transport="codex_app_tools")
        role = self.role()
        self.assertEqual(role["adapter"], "dsh")
        self.assertEqual(role["session_id"], "old-chat")
        self.assertFalse(role["adapter_transport_mismatch"])
        listed = self.sessions()
        self.assertEqual(listed["active"]["tester"], "old-chat")
        self.assertNotIn("desktop:codex-chat", [item["id"] for item in listed["sessions"]])
        old_row = next(item for item in listed["sessions"] if item["id"] == "desktop:old-chat")
        self.assertFalse(old_row["retired"])

    def test_join_migrate_adapter_uses_the_same_full_migration(self):
        self.start_role("dsh", "dsh_plugin")
        result = cli.join_role(self.state, self.config_path, "demo", "tester",
                               session="new-chat", transport="codex_app_tools",
                               migrate_adapter=True, command=FIXTURE, args=["success"],
                               client=self.client, hub=self.hub)
        self.assertTrue(result["joined"])
        self.assert_complete(result, "codex", "codex_app_tools")

    def test_join_without_migration_refuses_cross_client(self):
        self.start_role("dsh", "dsh_plugin")
        with self.assertRaisesRegex(ValueError, "adapter_transport_mismatch"):
            cli.join_role(self.state, self.config_path, "demo", "tester",
                          session="new-chat", transport="codex_app_tools",
                          client=self.client, hub=self.hub)
        self.assertEqual(self.hub.restarts, 0)
        self.assertEqual(self.role()["adapter"], "dsh")

    def test_roles_expose_a_legacy_half_migration_for_the_tray(self):
        self.start_role("dsh", "dsh_plugin")
        binding_path = self.state / "desktop-bindings.json"
        bindings = json.loads(binding_path.read_text(encoding="utf-8"))
        bindings["demo.tester"] = {"session_id": "legacy-codex", "transport": "codex_app_tools"}
        binding_path.write_text(json.dumps(bindings), encoding="utf-8")
        self.hub.restart()
        role = self.role()
        self.assertEqual(role["adapter"], "dsh")
        self.assertEqual(role["desktop_transport"], "codex_app_tools")
        self.assertEqual(role["expected_adapter"], "codex")
        self.assertTrue(role["adapter_transport_mismatch"])

    def test_recovers_the_reported_half_migrated_state(self):
        self.start_role("dsh", "dsh_plugin")
        # Exactly the state produced by the old command: dsh adapter, Codex
        # transport, Codex chat row, old DSH session already retired.
        self.client.broker.store.register_session("desktop:codex-chat", "demo", "tester",
                                                  "dsh", "codex_app_tools")
        self.client.broker.desktop.bind("demo.tester", {"session_id": "codex-chat",
                                                        "transport": "codex_app_tools"})
        role = self.role()
        self.assertTrue(role["adapter_transport_mismatch"])
        result = self.migrate(transport="codex_app_tools", session="codex-chat")
        self.assert_complete(result, "codex", "codex_app_tools", old="old-chat", new="codex-chat")

    def test_takeover_cli_refuses_mismatch_before_touching_the_hub(self):
        self.start_role("dsh", "dsh_plugin")
        with self.assertRaisesRegex(ValueError, "adapter_transport_mismatch"):
            cli.takeover_role(self.state, self.config_path, "demo", "tester",
                              session="codex-chat", transport="codex_app_tools",
                              client=self.client, hub=self.hub)
        self.assertEqual(self.hub.restarts, 0)
        self.assertEqual(self.role()["adapter"], "dsh")

    def test_failure_before_config_write_rolls_back(self):
        self.start_role("dsh", "dsh_plugin")
        before = self.config_path.read_text(encoding="utf-8")
        with mock.patch("bridge.cli._apply_adapter_config",
                        side_effect=RuntimeError("injected_config_failure")):
            with self.assertRaisesRegex(RuntimeError, "migration_failed_rolled_back"):
                self.migrate()
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.hub.restarts, 0)
        listed = self.sessions()
        self.assertEqual(listed["active"]["tester"], "old-chat")
        self.assertFalse(next(item for item in listed["sessions"]
                              if item["id"] == "desktop:old-chat")["retired"])

    def test_failure_after_config_write_restores_project_entry(self):
        self.start_role("dsh", "dsh_plugin")
        config_before = self.config_path.read_text(encoding="utf-8")
        entry_before = (self.project / ".agents-chat" / "project.json").read_text(encoding="utf-8")
        with mock.patch("bridge.cli.update_entry",
                        side_effect=RuntimeError("injected_entry_failure")):
            with self.assertRaisesRegex(RuntimeError, "migration_failed_rolled_back"):
                self.migrate()
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), config_before)
        self.assertEqual((self.project / ".agents-chat" / "project.json").read_text(encoding="utf-8"),
                         entry_before)
        self.assertEqual(self.hub.restarts, 0)
        self.assertEqual(self.sessions()["active"]["tester"], "old-chat")

    def test_failure_restarting_hub_rolls_back_config_and_binding(self):
        self.start_role("dsh", "dsh_plugin")
        config_before = self.config_path.read_text(encoding="utf-8")
        entry_before = (self.project / ".agents-chat" / "project.json").read_text(encoding="utf-8")
        binding_before = json.loads((self.state / "desktop-bindings.json").read_text(encoding="utf-8"))
        self.hub.fail_next_restart = True
        with self.assertRaisesRegex(RuntimeError, "migration_failed_rolled_back"):
            self.migrate()
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), config_before)
        self.assertEqual((self.project / ".agents-chat" / "project.json").read_text(encoding="utf-8"),
                         entry_before)
        self.assertEqual(json.loads((self.state / "desktop-bindings.json").read_text(encoding="utf-8")),
                         binding_before)
        self.assertTrue(self.hub.running())
        listed = self.sessions()
        self.assertEqual(listed["active"]["tester"], "old-chat")
        self.assertFalse(next(item for item in listed["sessions"]
                              if item["id"] == "desktop:old-chat")["retired"])

    def test_failure_binding_new_session_restores_original_role(self):
        self.start_role("dsh", "dsh_plugin")
        original_call = self.client.call

        def failing(method, **params):
            if method == "session_takeover":
                raise RuntimeError("injected_bind_failure")
            return original_call(method, **params)

        with mock.patch.object(self.client, "call", side_effect=failing):
            with self.assertRaisesRegex(RuntimeError, "migration_failed_rolled_back"):
                self.migrate()
        self.assertEqual(read_config(self.config_path)["endpoints"]["demo.tester"]["adapter"], "dsh")
        entry = json.loads((self.project / ".agents-chat" / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(entry["roles"]["tester"]["adapter"], "dsh")
        self.assertTrue(self.hub.running())
        listed = self.sessions()
        self.assertEqual(listed["active"]["tester"], "old-chat")
        self.assertFalse(next(item for item in listed["sessions"]
                              if item["id"] == "desktop:old-chat")["retired"])

    def test_failure_retiring_old_sessions_restores_original_role(self):
        self.start_role("dsh", "dsh_plugin")
        with mock.patch.object(Store, "takeover_session",
                               side_effect=RuntimeError("injected_retire_failure")):
            with self.assertRaisesRegex(RuntimeError, "migration_failed_rolled_back"):
                self.migrate()
        self.assertEqual(read_config(self.config_path)["endpoints"]["demo.tester"]["adapter"], "dsh")
        entry = json.loads((self.project / ".agents-chat" / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(entry["roles"]["tester"]["adapter"], "dsh")
        self.assertTrue(self.hub.running())
        listed = self.sessions()
        self.assertEqual(listed["active"]["tester"], "old-chat")
        self.assertFalse(next(item for item in listed["sessions"]
                              if item["id"] == "desktop:old-chat")["retired"])

    def test_failed_postcondition_verification_rolls_everything_back(self):
        self.start_role("dsh", "dsh_plugin")
        config_before = self.config_path.read_text(encoding="utf-8")
        with mock.patch("bridge.cli.verify_takeover",
                        return_value={"ok": False, "checks": {"injected": False}}):
            with self.assertRaisesRegex(RuntimeError, "migration_failed_rolled_back"):
                self.migrate()
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), config_before)
        self.assertTrue(self.hub.running())
        listed = self.sessions()
        self.assertEqual(listed["active"]["tester"], "old-chat")
        self.assertFalse(next(item for item in listed["sessions"]
                              if item["id"] == "desktop:old-chat")["retired"])


class CliIntegrationTests(unittest.TestCase):
    def test_session_migrate_and_takeover_command_is_atomic_and_verifiable(self):
        from bridge.cli import start, wait_stopped
        from bridge.client import Client

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "demo"
            bus = project / "agent_chat"
            (bus / "_state").mkdir(parents=True)
            config = base / "config.json"
            state = base / "state"
            config.write_text(json.dumps({
                "version": 2,
                "projects": {"demo": {
                    "name": "Demo", "root": str(project),
                    "agent_chat": {"bus": str(bus), "roles": {"tester": "tester"}},
                    "roles": {"tester": {"adapter": "dsh", "command": FIXTURE,
                                         "args": ["success"], "cwd": "."}},
                }},
            }), encoding="utf-8")
            entry = project / ".agents-chat" / "project.json"
            entry.parent.mkdir(parents=True)
            entry.write_text(json.dumps({
                "schema": 1, "project_id": "demo", "project_name": "Demo",
                "project_root": str(project),
                "hub": {"config": str(config.resolve()), "state": str(state.resolve())},
                "roles": {"tester": {"address": "demo.tester", "adapter": "dsh",
                                     "description": "", "peers": []}},
            }), encoding="utf-8")
            seed_codex_registry(state, base)
            start(state, config)
            client = Client(state)
            try:
                client.call("desktop_bind", agent="demo.tester", project="demo",
                            session_id="old-chat", transport="dsh_plugin")
                command = [
                    sys.executable, str(ROOT / "bridge.py"), "--state", str(state),
                    "--config", str(config), "--project", "demo",
                    "session", "migrate-and-takeover", "--role", "tester",
                    "--session", "new-chat", "--transport", "codex_app_tools",
                    "--adapter", "auto", "--exe", sys.executable,
                    "--arg", str(FIXTURE[1]), "--arg", "success",
                ]
                process = subprocess.run(command, cwd=str(ROOT), capture_output=True,
                                         text=True, encoding="utf-8", timeout=60)
                self.assertEqual(process.returncode, 0, process.stderr or process.stdout)
                result = json.loads(process.stdout)
                self.assertTrue(result["adapter_changed"], result)
                self.assertTrue(result["hub_restarted"], result)
                self.assertEqual(result["adapter"], "codex")
                self.assertEqual(result["active_session"], "new-chat")
                self.assertTrue(result["postconditions"]["ok"], result["postconditions"])
                self.assertIn("desktop:old-chat", result["retired_sessions"])
                self.assertEqual(read_config(config)["endpoints"]["demo.tester"]["adapter"], "codex")
                listed = client.call("session_list", project="demo")
                self.assertEqual(listed["active"]["tester"], "new-chat")
                old = next(item for item in listed["sessions"] if item["id"] == "desktop:old-chat")
                self.assertTrue(old["retired"])
            finally:
                try:
                    client.call("shutdown")
                except RuntimeError:
                    pass
                deadline = time.monotonic() + 15
                while not wait_stopped(state, 1) and time.monotonic() < deadline:
                    time.sleep(0.1)


class MappingTests(unittest.TestCase):
    def test_transport_maps_to_exactly_one_adapter(self):
        self.assertEqual(adapter_for_transport("codex_app_tools"), "codex")
        self.assertEqual(adapter_for_transport("qoder_native"), "qoder")
        self.assertEqual(adapter_for_transport("dsh_plugin"), "dsh")
        self.assertIsNone(adapter_for_transport("unknown_transport"))
        self.assertTrue(transport_supported("codex", "codex_app_tools"))
        self.assertFalse(transport_supported("dsh", "codex_app_tools"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.broker import Broker
from bridge.cli import parser
from bridge.mcp import tools_for
from bridge.onboarding import endpoint_for
from bridge.runtime import command_for
from bridge.workbuddy import (discover_workbuddy_command, install_workbuddy_mcp,
                              workbuddy_mcp_entry)


class WorkBuddyTests(unittest.TestCase):
    def test_discovers_embedded_workbuddy_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "WorkBuddy.exe"
            entry = root / "resources/app.asar.unpacked/cli/bin/codebuddy"
            entry.parent.mkdir(parents=True)
            executable.write_bytes(b"")
            entry.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            with patch.dict(os.environ, {"WORKBUDDY_HOME": str(root), "WORKBUDDY_CLI": ""}):
                command = discover_workbuddy_command()
            # Windows temp roots may be reported with an 8.3 short name while
            # discovery returns the resolved long path; compare resolved paths.
            self.assertEqual(command, [str(executable.resolve()), str(entry.resolve())])

    def test_mcp_install_merges_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "mcp.json"
            path.write_text(json.dumps({"mcpServers": {"existing": {"url": "https://example.test"}}}),
                            encoding="utf-8")
            cli = [str(root / "AgentsChat.Cli.exe")]
            first = install_workbuddy_mcp(cli, root / "state", root / "config.json",
                                          "demo", "review", path=path)
            second = install_workbuddy_mcp(cli, root / "state", root / "config.json",
                                           "demo", "review", path=path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("existing", data["mcpServers"])
            self.assertIn("agents-chat-demo-review", data["mcpServers"])
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            args = data["mcpServers"]["agents-chat-demo-review"]["args"]
            self.assertEqual(args[-4:], ["--agent", "demo.review", "--transport", "workbuddy_mcp"])

    def test_default_project_uses_plain_role_address(self):
        entry = workbuddy_mcp_entry(["AgentsChat.Cli.exe"], "state", "config.json",
                                    "default", "workbuddy")
        self.assertIn("workbuddy", entry["args"])
        self.assertNotIn("default.workbuddy", entry["args"])

    def test_runtime_uses_codebuddy_headless_protocol(self):
        endpoint = {"adapter": "workbuddy", "command": ["WorkBuddy.exe", "codebuddy"], "args": []}
        self.assertEqual(command_for(endpoint, Path("run")),
                         ["WorkBuddy.exe", "codebuddy", "-p", "--output-format", "json"])

    def test_generated_workbuddy_role_uses_mcp_pull(self):
        with patch("bridge.workbuddy.discover_workbuddy_command",
                   return_value=["WorkBuddy.exe", "codebuddy"]):
            endpoint = endpoint_for("workbuddy", Path.cwd())
        self.assertEqual(endpoint["mode"], "mcp_pull")

    def test_mcp_and_cli_expose_workbuddy_transport(self):
        register = next(tool for tool in tools_for("workbuddy") if tool["name"] == "bridge_register")
        self.assertIn("workbuddy_mcp", register["inputSchema"]["properties"]["transport"]["enum"])
        self.assertIn("bridge_pull", {tool["name"] for tool in tools_for("workbuddy")})
        self.assertIn("bridge_complete", {tool["name"] for tool in tools_for("workbuddy")})
        args = parser().parse_args(["workbuddy", "setup", "--project", "demo"])
        self.assertEqual((args.command, args.workbuddy_command, args.project_local),
                         ("workbuddy", "setup", "demo"))

    def test_pull_and_complete_are_durable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {"endpoints": {
                "sender": {"adapter": "fixture", "command": ["fixture"], "cwd": str(root)},
                "workbuddy": {"adapter": "workbuddy", "command": ["WorkBuddy.exe"],
                              "cwd": str(root), "mode": "mcp_pull"},
            }}
            broker = Broker(root / "state", config)
            try:
                sent = broker.call("send", {"sender": "sender", "target": "workbuddy",
                                             "key": "one", "prompt": "Review the change"})
                self.assertEqual(sent["mode"], "mcp_pull")
                pulled = broker.call("pull", {"recipient": "workbuddy"})
                self.assertEqual(pulled["task"]["prompt"], "Review the change")
                completed = broker.call("complete", {"recipient": "workbuddy",
                                                       "task_id": sent["task_id"],
                                                       "summary": "Reviewed"})
                self.assertEqual(completed["state"], "succeeded")
                duplicate = broker.call("complete", {"recipient": "workbuddy",
                                                       "task_id": sent["task_id"],
                                                       "summary": "Reviewed"})
                self.assertTrue(duplicate["duplicate"])
                inbox = broker.call("inbox", {"recipient": "sender", "consumer": "test"})
                self.assertEqual(len(inbox["messages"]), 1)
            finally:
                broker.close()


if __name__ == "__main__":
    unittest.main()

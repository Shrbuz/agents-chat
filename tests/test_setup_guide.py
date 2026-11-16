"""setup() must hand back the exact sentences and dispatch mode to use next."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from bridge.onboarding import setup

# A role needs a runnable command; using the fixture adapter keeps these tests
# independent of whether the official Codex/Qoder CLIs are installed.
FIXTURE = [sys.executable, "-c", "print(1)"]
ROLES = [{"role": "backend", "adapter": "fixture", "command": FIXTURE},
         {"role": "frontend", "adapter": "fixture", "command": FIXTURE}]


class SetupGuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "proj").mkdir()
        self.config = self.root / "config.json"
        self.state = self.root / "state"

    def run_setup(self, with_agent_chat=True, collaboration_mode=None):
        return setup(self.config, self.root / "proj", name="demo",
                     roles=[dict(spec) for spec in ROLES],
                     state=self.state, write_agents=False, with_agent_chat=with_agent_chat,
                     collaboration_mode_value=collaboration_mode)

    def test_bus_mode_reports_phrases_for_each_role(self):
        result = self.run_setup()
        self.assertEqual(result["mode"], "bus")
        self.assertEqual(set(result["phrases"]), {"backend", "frontend"})
        self.assertIn("join 成 backend", result["phrases"]["backend"]["join"])
        self.assertIn("给 frontend 派活", result["phrases"]["backend"]["dispatch"])
        self.assertIn("verify pass", result["phrases"]["backend"]["verify"])
        # Each role is pointed at a different peer, never at itself.
        self.assertIn("给 backend 派活", result["phrases"]["frontend"]["dispatch"])

    def test_queue_mode_when_the_bus_is_disabled(self):
        result = self.run_setup(with_agent_chat=False)
        self.assertEqual(result["mode"], "queue")
        self.assertEqual(result["agent_chat"]["status"], "disabled")

    def test_strict_requires_the_durable_bus(self):
        with self.assertRaisesRegex(ValueError, "strict_collaboration_requires_agent_chat_bus"):
            self.run_setup(with_agent_chat=False, collaboration_mode="strict")

    def test_phrases_survive_the_cli_json_round_trip(self):
        result = self.run_setup()
        round_tripped = json.loads(json.dumps(result, ensure_ascii=False))
        self.assertEqual(round_tripped["mode"], "bus")
        self.assertIn("dispatch", round_tripped["phrases"]["backend"])

    def test_fast_mode_is_written_to_entries_and_prompts(self):
        result = self.run_setup(collaboration_mode="fast")
        self.assertEqual(result["collaboration_mode"], "fast")
        entry = json.loads((self.root / "proj/.agents-chat/project.json").read_text(encoding="utf-8"))
        manifest = json.loads((self.root / "proj/agent_chat/project.json").read_text(encoding="utf-8"))
        prompt = (self.root / "proj/.agents-chat/prompts/backend.md").read_text(encoding="utf-8")
        self.assertEqual(entry["collaboration_mode"], "fast")
        self.assertEqual(manifest["collaboration_mode"], "fast")
        self.assertIn("Collaboration mode: `fast`", prompt)
        self.assertIn("不创建 claim/verify", result["phrases"]["backend"]["verify"])


if __name__ == "__main__":
    unittest.main()

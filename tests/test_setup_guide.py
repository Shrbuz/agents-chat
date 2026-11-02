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

    def run_setup(self, with_agent_chat=True):
        return setup(self.config, self.root / "proj", name="demo",
                     roles=[dict(spec) for spec in ROLES],
                     state=self.state, write_agents=False, with_agent_chat=with_agent_chat)

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

    def test_phrases_survive_the_cli_json_round_trip(self):
        result = self.run_setup()
        round_tripped = json.loads(json.dumps(result, ensure_ascii=False))
        self.assertEqual(round_tripped["mode"], "bus")
        self.assertIn("dispatch", round_tripped["phrases"]["backend"])


if __name__ == "__main__":
    unittest.main()

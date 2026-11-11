import json
import tempfile
import time
import unittest
from pathlib import Path

from bridge.cli import _resolve_target_session
from build_portable import write_support_files
from install import write_qoder_native_options


class QoderInstallTests(unittest.TestCase):
    def test_installer_overwrites_a_stale_portable_state_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plugin = root / "connectors" / "qoder-native"
            plugin.mkdir(parents=True)
            (plugin / "bridge.local.json").write_text(json.dumps({
                "state": r"E:\\staging\\AgentsChat-Portable\\data",
                "agent": "old-role",
            }), encoding="utf-8")

            state = root / ".bridge"
            write_qoder_native_options(plugin, state)
            options = json.loads((plugin / "bridge.local.json").read_text(encoding="utf-8"))
            self.assertEqual(options, {"state": str(state.resolve()), "agent": "*"})

    def test_portable_package_uses_a_relocatable_state_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "connectors" / "qoder-native").mkdir(parents=True)
            write_support_files(root)
            options = json.loads((root / "connectors" / "qoder-native" / "bridge.local.json")
                                 .read_text(encoding="utf-8"))
            self.assertEqual(options, {"state": "../../data", "agent": "*"})

    def test_explicit_qoder_session_still_requires_a_live_native_connector(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with self.assertRaisesRegex(ValueError, "qoder_native_offline"):
                _resolve_target_session(state, "qoder", "known-chat", "qoder_native")
            (state / "qoder-native-status.json").write_text(json.dumps({
                "ready": True, "updated_at": time.time() * 1000,
            }), encoding="utf-8")
            self.assertEqual(
                _resolve_target_session(state, "qoder", "known-chat", "qoder_native"),
                "known-chat")


if __name__ == "__main__":
    unittest.main()

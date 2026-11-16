"""Installer shutdown-race regression coverage."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from install import wait_for_broker_stop


class InstallerShutdownTests(unittest.TestCase):
    def test_missing_endpoint_is_already_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(wait_for_broker_stop(Path(tmp), timeout=0.01))

    def test_confirmed_dead_process_removes_stale_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            endpoint = Path(tmp) / "endpoint.json"
            endpoint.write_text(json.dumps({"pid": 123}), encoding="utf-8")
            with mock.patch("install.definitely_stopped", return_value=True):
                self.assertTrue(wait_for_broker_stop(Path(tmp), timeout=0.1))
            self.assertFalse(endpoint.exists())


if __name__ == "__main__":
    unittest.main()

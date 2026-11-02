"""The dsh adapter drives `dsh headless`: stdin task in, final message out."""
from pathlib import Path
import tempfile
import unittest

from bridge.runtime import command_for, parse_result


class DshAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name)
        self.endpoint = {"adapter": "dsh",
                         "command": ["C:\\Fake\\DeepSeek Harness.exe", "--expose-internals", "cli.js"],
                         "cwd": str(self.run_dir), "args": []}

    def test_command_reads_the_task_from_stdin(self):
        command = command_for(self.endpoint, self.run_dir)
        self.assertEqual(command[-2:], ["headless", "-"])
        self.assertEqual(command[0], "C:\\Fake\\DeepSeek Harness.exe")

    def test_stdout_is_the_final_message(self):
        result, ok = parse_result("dsh", "OK\n", self.run_dir / "final.txt")
        self.assertTrue(ok)
        self.assertEqual(result["summary"], "OK")
        self.assertIsNone(result["error"])

    def test_empty_output_is_a_failure(self):
        result, ok = parse_result("dsh", "  \n", self.run_dir / "final.txt")
        self.assertFalse(ok)
        self.assertEqual(result["error"], "empty_runtime_result")

    def test_long_output_is_truncated_to_the_result_budget(self):
        result, ok = parse_result("dsh", "x" * 2000, self.run_dir / "final.txt")
        self.assertTrue(ok)
        self.assertEqual(len(result["summary"]), 800)


if __name__ == "__main__":
    unittest.main()

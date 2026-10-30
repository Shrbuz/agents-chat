import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from bridge.native_result import write_report


class NativeReportTests(unittest.TestCase):
    def test_report_is_atomic_idempotent_and_rejects_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            task,receipt=str(uuid.uuid4()),str(uuid.uuid4())
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results=list(pool.map(lambda _:write_report(directory,task,receipt,"actual result"),range(4)))
            self.assertTrue(all(result["reported"] for result in results))
            report=json.loads((Path(directory)/"native-results"/(task+".json")).read_text(encoding="utf-8"))
            self.assertEqual(report["receipt"],receipt)
            with self.assertRaisesRegex(ValueError,"conflict"):
                write_report(directory,task,receipt,"different result")

    def test_no_path_traversal_or_unbounded_report(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                write_report(directory,"../outside",str(uuid.uuid4()),"result")
            with self.assertRaises(ValueError):
                write_report(directory,str(uuid.uuid4()),str(uuid.uuid4()),"x"*801)

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from build_exe import ROOT, compiler


@unittest.skipUnless(os.name == "nt", "requires WinForms")
class IslandTests(unittest.TestCase):
    def test_hover_paging_and_live_messages(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "IslandInteraction.exe"
            subprocess.run([str(compiler()), "/nologo", "/main:IslandInteraction",
                            "/reference:System.Windows.Forms.dll", "/reference:System.Web.Extensions.dll",
                            "/out:" + str(exe), *map(str, (ROOT / "launcher").glob("*.cs")),
                            str(ROOT / "tests/windows/IslandInteraction.cs")], check=True)
            subprocess.run([str(exe)], cwd=folder, check=True, timeout=30)

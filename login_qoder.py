import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
config = json.loads((root / "config.local.json").read_text(encoding="utf-8-sig"))
command = config["endpoints"]["qoder"]["command"]
raise SystemExit(subprocess.call(command + ["login"]))

"""Deterministic external process for transport demos/tests. This is NOT an AI agent."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

mode = sys.argv[1] if len(sys.argv) > 1 else "success"
prompt = sys.stdin.read()
if mode == "sleep":
    time.sleep(60)
elif mode == "child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])
    Path("child.pid").write_text(str(child.pid))
    time.sleep(90)
elif mode == "flood":
    sys.stdout.write("x" * 200000)
    sys.stdout.flush()
elif mode == "invalid":
    print("not provider JSON")
else:
    source = Path("values.json")
    values = json.loads(source.read_text()) if source.exists() else [19, 23]
    summary = f"Fixture process executed: values={values}; sum={sum(values)}. No AI model was called."
    if "Result of task" in prompt:
        summary = "Fixture callback executed and received the preceding result. No AI model was called."
    task_id = os.environ.get("AGENT_BRIDGE_TASK_ID", "unknown")
    Path(f"receipt-{task_id}.txt").write_text(summary, encoding="utf-8")
    print(json.dumps({"type": "result", "subtype": "error" if mode == "error" else "success",
                      "is_error": mode == "error", "result": summary,
                      "session_id": "fixture-" + task_id, "usage": {"input_tokens": 0, "output_tokens": 0}}))
    if mode == "exit":
        sys.exit(7)

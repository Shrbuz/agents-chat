"""Run a deterministic local demo, or --live for installed/authenticated AI CLIs."""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from bridge.cli import start
from bridge.client import Client

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Calls real models and consumes their quota")
    parser.add_argument("--config", default=str(ROOT / "config.local.json"))
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--state", help="Optional existing broker state directory")
    args = parser.parse_args()
    run_id = str(uuid.uuid4())
    demo_dir = ROOT / "demo-work" / run_id
    demo_dir.mkdir(parents=True)
    state = Path(args.state).resolve() if args.state else demo_dir / "state"
    if args.live:
        config = Path(args.config).resolve()
        sender, target = "codex", "qoder"
        prompt = "Read the local values.json using your file read tool. Compute the sum. Report exact values, sum, and evidence that you read the file. Do not modify files."
    else:
        endpoints = {}
        for name in ("fixture-source", "fixture-target"):
            cwd = demo_dir / name
            cwd.mkdir()
            (cwd / "values.json").write_text("[19,23,7]")
            endpoints[name] = {"adapter": "fixture", "command": [sys.executable, str(ROOT / "examples" / "fixture_runtime.py")], "cwd": str(cwd)}
        config = demo_dir / "config.json"
        config.write_text(json.dumps({"endpoints": endpoints}))
        sender, target = "fixture-source", "fixture-target"
        prompt = "Read values.json and sum the values."
    start(state, config)
    client = Client(state)
    try:
        request = dict(sender=sender, target=target, prompt=prompt, key="demo-" + run_id, reply_mode="run", timeout=180)
        sent = client.call("send", **request)
        duplicate = client.call("send", **request)
        task = client.wait(sent["task_id"], 200)
        callback_id = (task.get("result") or {}).get("callback_task_id")
        callback = client.wait(callback_id, 200) if callback_id else None
        inbox = client.call("inbox", recipient=sender, consumer="demo")
        client.call("ack", recipient=sender, consumer="demo", cursor=inbox["next_cursor"])
        after_ack = client.call("inbox", recipient=sender, consumer="demo")
        report = {"kind": "real_cli" if args.live else "deterministic_fixture_no_model", "state_dir": str(state),
                  "send": sent, "duplicate": duplicate, "execution": task, "callback": callback,
                  "inbox": inbox, "unread_after_ack": len(after_ack["messages"]),
                  "passed": task["state"] == "succeeded" and callback is not None and callback["state"] == "succeeded"
                  and duplicate["duplicate"] and not after_ack["messages"]}
        output = ROOT / ("live-demo-report.json" if args.live else "demo-report.json")
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "kind": report["kind"], "report": str(output)}, ensure_ascii=False))
        if not report["passed"]:
            raise SystemExit(1)
    finally:
        if not args.keep_running:
            client.call("shutdown")
            for _ in range(100):
                if not (state / "endpoint.json").exists():
                    break
                time.sleep(0.05)


if __name__ == "__main__":
    main()

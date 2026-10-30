"""Deterministic Qoder Windows UI connector for explicitly selected existing chats."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from bridge.client import Client

ROOT = Path(__file__).resolve().parent


def ui(action, *arguments):
    if action!="inspect" and (ROOT / ".bridge/qoder-input-disabled").exists():
        raise RuntimeError("Qoder input automation is disabled by user preference")
    # Chromium can invalidate an element during a read. Retrying reads is safe;
    # mutating commands are attempted exactly once, even on an ambiguous error.
    for attempt in range(4 if action=="inspect" else 1):
        process = subprocess.run([str(ROOT / "connectors/QoderDesktop.exe"), action, *arguments],
            capture_output=True, text=True, encoding="utf-8-sig", timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW)
        result = json.loads(process.stdout)
        if not process.returncode and not result.get("error"):
            return result
        error = result.get("error") or "Qoder accessibility read was invalidated"
        if action=="inspect" and attempt<3 and ("ElementNotAvailable" in error or "invalidated" in error):
            time.sleep(.15)
            continue
        raise RuntimeError(error)


def execute(client, state, job, cancelled):
    sent = False
    attempted = False
    try:
        target = job["session_id"]
        # Only operate while the user keeps the explicitly bound chat selected.
        snapshot = ui("inspect", target)
        while snapshot.get("busy"):
            if cancelled.wait(1):
                return
            status = client.call("task",task_id=job["task_id"])
            if status["state"] in ("cancelled","timed_out","interrupted","failed"):
                return
            snapshot = ui("inspect",target)
        directory = state / "runs" / job["task_id"]
        directory.mkdir(parents=True, exist_ok=True)
        prompt = directory / "prompt.txt"
        prompt.write_text(job["prompt"], encoding="utf-8")
        attempted = True
        ui("send", target, str(prompt))
        sent = True
        while not cancelled.wait(1):
            status = client.call("task",task_id=job["task_id"])
            if status["state"] in ("cancelled","timed_out","interrupted","failed"):
                cancelled.set()
                break
            snapshot = ui("inspect", target)
            if snapshot.get("task_id") != job["task_id"]:
                # The renderer can take time to materialize the submitted round.
                continue
            status = snapshot.get("status") or ""
            summary = snapshot.get("summary") or ""
            if "状态：成功" in status and summary:
                client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"], state="succeeded",
                    result={"summary": summary[-800:], "session_id":target, "evidence":"Qoder accessibility: matching task marker and completed round"})
                return
            if "状态：失败" in status or "状态：已停止" in status:
                raise RuntimeError("Qoder desktop round failed or was stopped: " + summary[-600:])
        if sent:
            ui("stop", target)
    except Exception as exc:
        try:
            client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"], state="failed",
                result={"summary":str(exc)[:800], "error":"qoder_desktop_ui", "delivery_uncertain":attempted})
        except RuntimeError:
            pass


def run_connector(state):
    if (state / "qoder-input-disabled").exists():
        return
    from bridge.broker import SingleInstance
    lock = SingleInstance(state / "qoder-desktop.lock")
    client = Client(state)
    running = {}
    offline_since = None
    try:
        while True:
            if (state / "qoder-input-disabled").exists():
                return
            try:
                target = next((t for t in client.call("targets") if t["name"] == "qoder" and t["mode"] == "desktop_session"), None)
                offline_since = None
                if not target:
                    return
                job = client.call("desktop_receive", agent="qoder", session_id=target["session_id"], seconds=40)
                if not job:
                    continue
                if job["action"] == "cancel":
                    event = running.get(job["task_id"])
                    if event:
                        event.set()
                    continue
                event = threading.Event()
                running[job["task_id"]] = event
                def work(job=job, event=event):
                    try:
                        execute(client, state, job, event)
                    finally:
                        running.pop(job["task_id"], None)
                threading.Thread(target=work, daemon=True).start()
            except (RuntimeError, OSError):
                if not (state / "endpoint.json").exists():
                    offline_since = offline_since or time.monotonic()
                    if time.monotonic()-offline_since > 15:
                        return
                time.sleep(3)
    finally:
        for event in running.values():
            event.set()
        lock.close()


if __name__ == "__main__":
    import sys
    run_connector(Path(sys.argv[1]).resolve() if len(sys.argv)>1 else ROOT / ".bridge")

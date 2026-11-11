"""OpenCode native-session connector supervised by the Agents Chat hub."""
from __future__ import annotations

import argparse
import os
import subprocess
import threading
import time
from pathlib import Path

from bridge.client import Client
from bridge.config import find_role, read_config
from bridge.opencode import (classify_opencode_error, inspect_opencode_delivery,
                             parse_opencode_stream, probe_opencode)

ROOT = Path(__file__).resolve().parent


def _role_endpoint(state, agent):
    import json
    descriptor = json.loads((state / "endpoint.json").read_text(encoding="utf-8"))
    config = read_config(descriptor["config"])
    return config["endpoints"][find_role(config, agent)]


def _report(client, agent, state, *, error=None, delivered=False, failure_kind=None):
    try:
        return client.call(
            "transport_report", agent=agent, transport="opencode_native", state=state,
            connection_id=None, error=str(error)[:500] if error else None,
            delivered=delivered, failure_kind=failure_kind)
    except RuntimeError:
        return None


def _state_for(kind):
    if kind == "desktop_chat_busy":
        return "busy"
    return {
        "opencode_service_unavailable": "service_unavailable",
        "opencode_session_missing": "session_missing",
        "opencode_auth_required": "auth_required",
        "opencode_cli_missing": "cli_missing",
    }.get(kind, "degraded")


def _interrupt(command, cwd, session_id):
    try:
        subprocess.run(
            [*command, "api", "POST", "/api/session/" + session_id + "/interrupt"],
            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def execute(client, command, cwd, job, agent):
    if job.get("action") != "run":
        return
    target, process = job["session_id"], None
    sent = False
    try:
        # Every delivery (including a store-level wakeup retry) checks the
        # transcript first.  Proving the stable marker absent before submission
        # is the exact-once boundary for business work.
        previous = inspect_opencode_delivery(command, cwd, target, job["task_id"])
        if previous["delivered"]:
            if job.get("notify"):
                client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                            state="succeeded", result={
                                "summary": "通知此前已投递；连接器恢复后按任务标记确认，未重复发送。",
                                "session_id": target, "notification": True,
                                "run_delivered": True, "deduplicated": True})
                _report(client, agent, "ready", delivered=True)
            elif previous.get("summary"):
                client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                            state="succeeded", result={
                                "summary": previous["summary"], "session_id": target,
                                "run_delivered": True, "deduplicated": True,
                                "evidence": "Recovered from OpenCode session export"})
                _report(client, agent, "ready", delivered=True)
            else:
                client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                            state="failed", result={
                                "summary": "OpenCode 会话中已存在该任务标记；为避免重复执行业务任务，未再次发送。",
                                "error": "opencode_delivery_uncertain", "session_id": target,
                                "run_delivered": True, "delivery_uncertain": True})
            return
        process = subprocess.Popen(
            [*command, "run", "--session", target, "--format", "json", "--auto"],
            cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                           | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0,
            start_new_session=os.name != "nt")
        output = {}

        def communicate():
            output["stdout"], output["stderr"] = process.communicate(input=job["prompt"])

        thread = threading.Thread(target=communicate, daemon=True)
        thread.start()
        sent = True
        cancelled = False
        while thread.is_alive():
            control = client.call("desktop_receive", agent=agent, session_id=target,
                                  transport="opencode_native", seconds=0)
            if control and control.get("action") == "cancel":
                cancelled = True
                _interrupt(command, cwd, target)
                if process.poll() is None:
                    process.terminate()
                break
            thread.join(timeout=1)
        thread.join(timeout=15)
        if thread.is_alive() and process.poll() is None:
            process.kill()
            thread.join(timeout=5)
        if cancelled:
            client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                        state="interrupted", result={
                            "summary": "已向 OpenCode 会话发送中断请求。",
                            "error": "desktop_cancelled", "session_id": target,
                            "run_delivered": True})
            return
        stdout, stderr = output.get("stdout", ""), output.get("stderr", "")
        parsed = parse_opencode_stream(stdout)
        if process.returncode:
            detail = parsed.get("error") or stderr.strip() or stdout.strip() or (
                "OpenCode exited with code " + str(process.returncode))
            raise RuntimeError(detail[:800])
        if job.get("notify"):
            summary = "通知已实际投递到 OpenCode 目标会话。"
        else:
            summary = parsed.get("summary")
            if not summary:
                raise RuntimeError("opencode_transport: completed without assistant result")
        client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                    state="succeeded", result={
                        "summary": summary[:800], "session_id": target,
                        "notification": bool(job.get("notify")), "run_delivered": True,
                        "evidence": "OpenCode run completed in the bound native session"})
        _report(client, agent, "ready", delivered=True)
    except Exception as exc:
        kind = classify_opencode_error(exc)
        _report(client, agent, _state_for(kind), error=exc,
                failure_kind=None if kind == "desktop_chat_busy" else kind)
        retryable = kind in ("desktop_chat_busy", "opencode_service_unavailable",
                             "opencode_session_missing", "opencode_transport")
        try:
            client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                        state="failed", result={
                            "summary": str(exc)[:800], "error": kind,
                            "retryable": retryable, "needs_attention": bool(job.get("notify")),
                            "unnotified": bool(job.get("notify")), "run_delivered": sent,
                            "delivery_uncertain": sent})
        except RuntimeError:
            pass
    finally:
        if process is not None and process.poll() is None:
            process.kill()


def run_connector(state, agent):
    from bridge.broker import SingleInstance
    lock = SingleInstance(state / (agent.replace(".", "-") + "-desktop.lock"))
    client = Client(state)
    next_probe = 0.0
    try:
        endpoint = _role_endpoint(state, agent)
        command, cwd = list(endpoint["command"]), endpoint["cwd"]
        while not (state / "connectors.stop").exists():
            try:
                target = next((item for item in client.call("targets")
                               if item["name"] == agent and item["mode"] == "desktop_session"
                               and item.get("desktop_transport") == "opencode_native"), None)
                if not target:
                    return
                if time.monotonic() >= next_probe:
                    probe_opencode(command, cwd, target["session_id"])
                    _report(client, agent, "ready")
                    next_probe = time.monotonic() + 30
                job = client.call("desktop_receive", agent=agent,
                                  session_id=target["session_id"], transport="opencode_native",
                                  recover_received=True, seconds=30)
                if job:
                    execute(client, command, cwd, job, agent)
            except Exception as exc:
                kind = classify_opencode_error(exc)
                _report(client, agent, _state_for(kind), error=exc,
                        failure_kind=None if kind == "desktop_chat_busy" else kind)
                next_probe = 0
                if (state / "connectors.stop").exists() or not (state / "endpoint.json").exists():
                    return
                time.sleep(3)
    finally:
        lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=ROOT / ".bridge")
    parser.add_argument("--agent", required=True)
    args = parser.parse_args()
    run_connector(args.state.resolve(), args.agent)


if __name__ == "__main__":
    main()

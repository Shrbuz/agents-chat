"""Kimi Code Web connector supervised by the Agents Chat hub."""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from bridge.client import Client
from bridge.config import find_role, read_config
from bridge.kimi import (KimiWebClient, classify_kimi_error, event_terminal_turn,
                         inspect_kimi_delivery)

ROOT = Path(__file__).resolve().parent


def _role_endpoint(state, agent):
    import json
    descriptor = json.loads((state / "endpoint.json").read_text(encoding="utf-8"))
    config = read_config(descriptor["config"])
    return config["endpoints"][find_role(config, agent)]


def _server(endpoint):
    env = endpoint.get("env") if isinstance(endpoint.get("env"), dict) else {}
    return KimiWebClient(endpoint.get("kimi_web_url") or env.get("KIMI_WEB_URL"),
                         env.get("KIMI_WEB_TOKEN"))


def _report(client, agent, state, *, error=None, delivered=False, failure_kind=None,
            summary=None):
    try:
        return client.call(
            "transport_report", agent=agent, transport="kimi_native", state=state,
            connection_id=None, error=str(error)[:500] if error else None,
            summary=summary, delivered=delivered, failure_kind=failure_kind)
    except RuntimeError:
        return None


def _state_for(kind):
    if kind == "desktop_chat_busy":
        return "busy"
    return {
        "kimi_service_unavailable": "service_unavailable",
        "kimi_session_missing": "session_missing",
        "kimi_auth_required": "auth_required",
        "kimi_entitlement_required": "entitlement_required",
        "kimi_model_not_configured": "model_not_configured",
        "kimi_capability_missing": "degraded",
        "kimi_workspace_mismatch": "degraded",
    }.get(kind, "degraded")


def _finish_recovered(client, server, job, agent):
    previous = inspect_kimi_delivery(server, job["session_id"], job["task_id"])
    if not previous["delivered"]:
        return False
    if job.get("notify"):
        client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                    state="succeeded", result={
                        "summary": "通知此前已投递；按 Kimi transcript 中的任务标记确认，未重复发送。",
                        "session_id": job["session_id"], "notification": True,
                        "run_delivered": True, "deduplicated": True})
        _report(client, agent, "ready", delivered=True)
        return True
    if previous.get("summary"):
        client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                    state="succeeded", result={
                        "summary": previous["summary"], "session_id": job["session_id"],
                        "run_delivered": True, "deduplicated": True,
                        "evidence": "Recovered from Kimi session transcript"})
        _report(client, agent, "ready", delivered=True)
        return True
    # A stable user marker proves admission even when a connector died before
    # observing the terminal turn. Never re-submit the same business task.
    raise RuntimeError(
        "kimi_transport: previous delivery marker exists without an assistant result; "
        "refusing duplicate prompt")


def _settled_summary(server, session_id, task_id, timeout=3):
    deadline = time.monotonic() + timeout
    while True:
        result = inspect_kimi_delivery(server, session_id, task_id)
        if result.get("summary") or time.monotonic() >= deadline:
            return result.get("summary", "")
        time.sleep(0.1)


def execute(client, server, cwd, job, agent):
    if job.get("action") != "run":
        return
    target, stream, prompt_id = job["session_id"], None, None
    sent = False
    try:
        if _finish_recovered(client, server, job, agent):
            return
        # Kimi's experimental v2 protocol requires a live subscribe_v2 stream
        # before prompt admission.  Terminal turns are correlated by the exact
        # triggerPromptId returned from POST /prompts.
        stream = server.events(
            target, timeout=min(30, max(5, int(job.get("delivery_timeout") or 30))))
        admitted = server.prompt(target, job["prompt"])
        prompt_id = admitted.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise RuntimeError("kimi_transport: prompt admission omitted prompt_id")
        sent = True
        while True:
            control = client.call("desktop_receive", agent=agent, session_id=target,
                                  transport="kimi_native", seconds=0)
            if control and control.get("action") == "cancel":
                try:
                    server.cancel(target, prompt_id)
                finally:
                    client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                                state="interrupted", result={
                                    "summary": "已向 Kimi Code 会话发送精确 prompt 中断请求。",
                                    "error": "desktop_cancelled", "session_id": target,
                                    "run_delivered": True, "prompt_id": prompt_id})
                return
            event = stream.get(timeout=1)
            if not event:
                continue
            turn = event_terminal_turn(event, prompt_id)
            if turn is None:
                continue
            state = turn.get("state")
            if state == "cancelled":
                client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                            state="interrupted", result={
                                "summary": "Kimi Code 会话中的执行已取消。",
                                "error": "desktop_cancelled", "session_id": target,
                                "run_delivered": True, "prompt_id": prompt_id})
                return
            if state == "failed":
                raise RuntimeError("kimi_transport: " + str(turn.get("error") or "turn failed"))
            if job.get("notify"):
                summary = "通知已实际投递到 Kimi Code 目标会话。"
            else:
                summary = _settled_summary(server, target, job["task_id"])
                if not summary:
                    raise RuntimeError("kimi_transport: completed without assistant result")
            client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                        state="succeeded", result={
                            "summary": summary[:800], "session_id": target,
                            "notification": bool(job.get("notify")), "run_delivered": True,
                            "prompt_id": prompt_id,
                            "event_sequence": ((event.get("payload") or {}).get("seq")),
                            "evidence": "Kimi terminal turn matched the admitted triggerPromptId"})
            _report(client, agent, "ready", delivered=True)
            return
    except Exception as exc:
        kind = classify_kimi_error(exc)
        _report(client, agent, _state_for(kind), error=exc,
                failure_kind=None if kind == "desktop_chat_busy" else kind)
        retryable = kind in (
            "desktop_chat_busy", "kimi_service_unavailable", "kimi_session_missing",
            "kimi_transport")
        try:
            client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                        state="failed", result={
                            "summary": str(exc)[:800], "error": kind,
                            "retryable": retryable, "needs_attention": bool(job.get("notify")),
                            "unnotified": bool(job.get("notify")), "run_delivered": sent,
                            "delivery_uncertain": sent, "session_id": target,
                            **({"prompt_id": prompt_id} if prompt_id else {})})
        except RuntimeError:
            pass
    finally:
        if stream is not None:
            stream.close()


def run_connector(state, agent):
    from bridge.broker import SingleInstance
    lock = SingleInstance(state / (agent.replace(".", "-") + "-desktop.lock"))
    client = Client(state)
    next_probe = 0.0
    server = None
    try:
        endpoint = _role_endpoint(state, agent)
        cwd = endpoint["cwd"]
        while not (state / "connectors.stop").exists():
            try:
                target = next((item for item in client.call("targets")
                               if item["name"] == agent and item["mode"] == "desktop_session"
                               and item.get("desktop_transport") == "kimi_native"), None)
                if not target:
                    return
                if server is None:
                    server = _server(endpoint)
                if time.monotonic() >= next_probe:
                    server.probe(target["session_id"], cwd)
                    _report(client, agent, "ready")
                    next_probe = time.monotonic() + 30
                job = client.call("desktop_receive", agent=agent,
                                  session_id=target["session_id"], transport="kimi_native",
                                  recover_received=True, seconds=30)
                if job:
                    execute(client, server, cwd, job, agent)
            except Exception as exc:
                kind = classify_kimi_error(exc)
                _report(client, agent, _state_for(kind), error=exc,
                        failure_kind=None if kind == "desktop_chat_busy" else kind)
                next_probe = 0
                if kind == "kimi_service_unavailable":
                    server = None
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

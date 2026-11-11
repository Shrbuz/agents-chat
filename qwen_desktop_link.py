"""Qwen Code daemon connector supervised by the Agents Chat hub."""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from bridge.client import Client
from bridge.config import find_role, read_config
from bridge.qwen import (QwenDaemonClient, classify_qwen_error, event_agent_text,
                         event_prompt_id, inspect_qwen_delivery)

ROOT = Path(__file__).resolve().parent


def _role_endpoint(state, agent):
    import json
    descriptor = json.loads((state / "endpoint.json").read_text(encoding="utf-8"))
    config = read_config(descriptor["config"])
    return config["endpoints"][find_role(config, agent)]


def _daemon(endpoint):
    env = endpoint.get("env") if isinstance(endpoint.get("env"), dict) else {}
    return QwenDaemonClient(endpoint.get("qwen_daemon_url") or env.get("QWEN_DAEMON_URL"),
                            env.get("QWEN_DAEMON_TOKEN"))


def _report(client, agent, state, *, error=None, delivered=False, failure_kind=None,
            summary=None):
    try:
        return client.call(
            "transport_report", agent=agent, transport="qwen_native", state=state,
            connection_id=None, error=str(error)[:500] if error else None,
            summary=summary, delivered=delivered, failure_kind=failure_kind)
    except RuntimeError:
        return None


def _state_for(kind):
    if kind == "desktop_chat_busy":
        return "busy"
    return {
        "qwen_daemon_unavailable": "service_unavailable",
        "qwen_session_missing": "session_missing",
        "qwen_auth_required": "auth_required",
        "qwen_capability_missing": "degraded",
        "qwen_workspace_mismatch": "degraded",
    }.get(kind, "degraded")


def _finish_recovered(client, daemon, job, agent):
    previous = inspect_qwen_delivery(daemon, job["session_id"], job["task_id"])
    if not previous["delivered"]:
        return False
    if job.get("notify"):
        client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                    state="succeeded", result={
                        "summary": "通知此前已投递；按 Qwen transcript 中的任务标记确认，未重复发送。",
                        "session_id": job["session_id"], "notification": True,
                        "run_delivered": True, "deduplicated": True})
        _report(client, agent, "ready", delivered=True)
        return True
    if previous.get("summary"):
        client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                    state="succeeded", result={
                        "summary": previous["summary"], "session_id": job["session_id"],
                        "run_delivered": True, "deduplicated": True,
                        "evidence": "Recovered from Qwen session transcript"})
        _report(client, agent, "ready", delivered=True)
        return True
    return False


def execute(client, daemon, cwd, job, agent):
    if job.get("action") != "run":
        return
    target, stream, prompt_id = job["session_id"], None, None
    sent = False
    try:
        if _finish_recovered(client, daemon, job, agent):
            return
        # Qwen's documented contract requires the SSE subscription to be open
        # before POST /prompt; otherwise a fast terminal event can be missed.
        # A recovered delivery replays the ring so an already-settled prompt can
        # still be correlated after idempotent re-admission.
        replay = 0 if job.get("recovery") else None
        stream = daemon.events(target, last_event_id=replay,
                               timeout=max(60, int(job.get("execution_timeout") or 180) + 30))
        admitted = daemon.prompt(
            target, job["prompt"], "agents-chat:" + job["task_id"],
            deadline_ms=min(60000, max(5000, int(job.get("delivery_timeout") or 30) * 1000)))
        prompt_id = admitted.get("promptId")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise RuntimeError("qwen_transport: prompt admission omitted promptId")
        sent = True
        chunks = []
        while True:
            control = client.call("desktop_receive", agent=agent, session_id=target,
                                  transport="qwen_native", seconds=0)
            if control and control.get("action") == "cancel":
                try:
                    daemon.cancel(target, prompt_id)
                finally:
                    client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                                state="interrupted", result={
                                    "summary": "已向 Qwen Code 会话发送中断请求。",
                                    "error": "desktop_cancelled", "session_id": target,
                                    "run_delivered": True})
                return
            event = stream.get(timeout=1)
            if not event:
                continue
            if event.get("type") == "state_resync_required":
                raise RuntimeError("qwen_transport: SSE replay requires session resync")
            if event_prompt_id(event) != prompt_id:
                continue
            text = event_agent_text(event)
            if text:
                chunks.append(text)
            kind = event.get("type")
            if kind == "turn_error":
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                raise RuntimeError("qwen_transport: " + str(
                    data.get("message") or data.get("code") or "turn failed"))
            if kind == "turn_cancelled":
                client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                            state="interrupted", result={
                                "summary": "Qwen Code 会话中的执行已取消。",
                                "error": "desktop_cancelled", "session_id": target,
                                "run_delivered": True})
                return
            if kind != "turn_complete":
                continue
            if job.get("notify"):
                summary = "通知已实际投递到 Qwen Code 目标会话。"
            else:
                summary = "".join(chunks).strip()
                if not summary:
                    summary = inspect_qwen_delivery(
                        daemon, target, job["task_id"]).get("summary", "")
                if not summary:
                    raise RuntimeError("qwen_transport: completed without assistant result")
            client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"],
                        state="succeeded", result={
                            "summary": summary[:800], "session_id": target,
                            "notification": bool(job.get("notify")), "run_delivered": True,
                            "prompt_id": prompt_id,
                            "event_id": event.get("id"),
                            "evidence": "Qwen turn_complete matched the admitted promptId"})
            _report(client, agent, "ready", delivered=True)
            return
    except Exception as exc:
        kind = classify_qwen_error(exc)
        _report(client, agent, _state_for(kind), error=exc,
                failure_kind=None if kind == "desktop_chat_busy" else kind)
        retryable = kind in (
            "desktop_chat_busy", "qwen_daemon_unavailable", "qwen_session_missing",
            "qwen_transport")
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
    try:
        endpoint = _role_endpoint(state, agent)
        daemon, cwd = _daemon(endpoint), endpoint["cwd"]
        while not (state / "connectors.stop").exists():
            try:
                target = next((item for item in client.call("targets")
                               if item["name"] == agent and item["mode"] == "desktop_session"
                               and item.get("desktop_transport") == "qwen_native"), None)
                if not target:
                    return
                if time.monotonic() >= next_probe:
                    probe = daemon.probe(target["session_id"], cwd)
                    _report(client, agent, "ready")
                    next_probe = time.monotonic() + 30
                job = client.call("desktop_receive", agent=agent,
                                  session_id=target["session_id"], transport="qwen_native",
                                  recover_received=True, seconds=30)
                if job:
                    execute(client, daemon, cwd, job, agent)
            except Exception as exc:
                kind = classify_qwen_error(exc)
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

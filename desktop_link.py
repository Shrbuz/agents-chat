"""Local Codex desktop connector. Uses the installed app-tools MCP, not CLI resume."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from bridge.client import Client
from bridge.desktop_transport import CodexDesktop

ROOT = Path(__file__).resolve().parent


def discover_connection():
    pipe = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")
    origin = os.environ.get("CODEX_THREAD_ID")
    node = os.environ.get("CODEX_MCP_NODE_PATH")
    if not pipe or not origin or not node:
        raise RuntimeError("请在 Codex 桌面聊天中执行一次连接命令，以获取宿主提供的连接上下文。")
    plugins = Path.home() / ".codex/plugins/cache/openai-bundled/codex-app-tools"
    candidates = sorted(plugins.glob("*/server.mjs"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("找不到本机 Codex app-tools MCP。")
    return {"command": [node, str(candidates[0])], "pipe": pipe, "origin_thread": origin}


def run_connector(state, connection, agent="codex"):
    from bridge.broker import SingleInstance
    lock = SingleInstance(state / (agent.replace(".", "-") + "-desktop.lock"))
    client = Client(state)
    offline_since = None
    try:
        while True:
            try:
                target = next((t for t in client.call("targets") if t["name"] == agent and t["mode"] == "desktop_session"), None)
                offline_since = None
                if not target:
                    return
                job = client.call("desktop_receive", agent=agent, session_id=target["session_id"], seconds=40)
                if job:
                    execute(client, connection, job, agent)
            except (RuntimeError, OSError):
                # No models are called on a broken connection; do not redeliver uncertain work.
                if not (state / "endpoint.json").exists():
                    offline_since = offline_since or time.monotonic()
                    if time.monotonic()-offline_since > 15:
                        return
                time.sleep(3)
    finally:
        lock.close()


def execute(client, connection, job, agent="codex"):
    if job["action"] != "run":
        return
    desktop = None
    try:
        desktop = CodexDesktop(connection)
        target = job["session_id"]
        # The external app-tools pipe supports read_thread; wait_threads requires executor integration.
        snapshot = desktop.call("read_thread", {"threadId":target,"hostId":"local","turnLimit":1,"maxOutputCharsPerItem":2000})
        turns = snapshot.get("turns",[])
        baseline_turn = turns[0]["id"] if turns else None
        if turns and turns[0].get("status") not in ("completed","failed","interrupted"):
            raise RuntimeError("desktop_chat_busy: task was not sent")
        delivery = desktop.call("send_message_to_thread", {"threadId": target, "hostId": "local", "prompt": job["prompt"]})
        receipt_path = client.state_dir / "runs" / job["task_id"] / "desktop-delivery.json"
        receipt_path.parent.mkdir(parents=True,exist_ok=True)
        receipt_path.write_text(json.dumps(delivery,ensure_ascii=False,indent=2),encoding="utf-8")
        expected_turn = None
        while True:
            control = client.call("desktop_receive", agent=agent, session_id=target, seconds=0)
            if control and control["action"] == "cancel":
                # App tools provide no per-chat stop method. Do not fake interruption.
                client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"], state="interrupted",
                    result={"summary": "Broker stopped waiting. Codex desktop turn may still be running.", "error": "desktop_interrupt_not_supported"})
                return
            result = desktop.call("read_thread", {"threadId":target,"hostId":"local","turnLimit":1,"maxOutputCharsPerItem":2000})
            new_turns = result.get("turns",[])
            if expected_turn is None and new_turns and new_turns[0].get("id")!=baseline_turn and any(i.get("type")=="functionCallOutput" and i.get("name")=="send_message_to_thread" for i in new_turns[0].get("items",[])):
                expected_turn = new_turns[0]["id"]
            completion = completed_read_result(result, baseline_turn, job["task_id"], expected_turn)
            if completion:
                state, summary = completion
                client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"], state=state,
                    result={"summary": summary[:800], "session_id": target, "evidence": "Codex desktop completed the pinned inter-chat delivery turn"})
                return
            time.sleep(2)  # Local code reads compact status; no model request or history injection.
    except Exception as exc:
        try:
            client.call("desktop_finish", task_id=job["task_id"], nonce=job["nonce"], state="failed",
                result={"summary": str(exc)[:800], "error": "codex_desktop_transport", "delivery_uncertain": True})
        except RuntimeError:
            pass
    finally:
        if desktop:
            desktop.close()


def completion_cursor(snapshot, target):
    for item in snapshot.get("polls", []):
        if item.get("thread", {}).get("id") == target:
            return item.get("cursor")
    return None


def completed_read_result(snapshot, baseline_turn, task_id, expected_turn=None):
    turns = snapshot.get("turns",[])
    if not turns or turns[0].get("id")==baseline_turn:
        return None
    turn = turns[0]
    marker = "Agents Chat task " + task_id + "."
    user_text = "\n".join(block.get("text","") for item in turn.get("items",[]) if item.get("type")=="userMessage" for block in item.get("content",[]) if isinstance(block,dict))
    # Desktop inter-chat messages are represented as functionCallOutput, not userMessage.
    linked_turn = expected_turn is not None and turn.get("id")==expected_turn and any(i.get("type")=="functionCallOutput" and i.get("name")=="send_message_to_thread" for i in turn.get("items",[]))
    if marker not in user_text and not linked_turn:
        return None
    if turn.get("status") in ("failed","interrupted"):
        return "failed", str(turn.get("error") or "Desktop turn interrupted.")
    final = [item.get("text","") for item in turn.get("items",[]) if item.get("type")=="agentMessage" and item.get("phase")=="final_answer"]
    if turn.get("status")=="completed" and final:
        return "succeeded", final[-1]
    return None


def latest_turn_id(snapshot, target):
    for item in snapshot.get("polls", []):
        if item.get("thread", {}).get("id") == target:
            return (item.get("latestTurn") or {}).get("id")
    return None


def completed_result(snapshot, target, baseline_turn):
    for item in snapshot.get("polls", []):
        if item.get("thread", {}).get("id") != target:
            continue
        turn = item.get("latestTurn") or {}
        message = item.get("latestAssistantMessage") or {}
        if not turn.get("id") or turn["id"] == baseline_turn:
            continue
        if turn.get("status") == "completed" and message.get("turnId") == turn["id"] and message.get("phase") == "final_answer":
            return "succeeded", message.get("text", "")
        if turn.get("status") in ("failed", "interrupted"):
            return "failed", str(turn.get("error") or "Desktop turn interrupted.")
    if snapshot.get("errors"):
        raise RuntimeError("desktop_wait_failed: " + str(snapshot["errors"])[:500])
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=ROOT / ".bridge")
    parser.add_argument("--thread")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--agent", default="codex", help="Role address, e.g. alpha.backend")
    args = parser.parse_args()
    state = args.state.resolve()
    connection_path = state / "codex-desktop.local.json"
    if args.serve:
        run_connector(state, json.loads(connection_path.read_text(encoding="utf-8")), args.agent)
        return
    if args.prepare:
        connection_path.write_text(json.dumps(discover_connection(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"prepared": True}))
        return
    connection = json.loads(connection_path.read_text(encoding="utf-8")) if connection_path.exists() else discover_connection()
    desktop = CodexDesktop(connection)
    try:
        inventory = desktop.call("list_threads", {"limit": 50})
        threads = inventory.get("pinnedThreads", []) + inventory.get("threads", [])
        if args.list:
            print(json.dumps([{"id": t["id"], "title": t.get("title"), "cwd": t.get("cwd"), "status": t.get("status")} for t in threads if t.get("kind") == "codex"], ensure_ascii=False))
            return
        target = args.thread or connection["origin_thread"]
        candidates = [t for t in threads if t["id"] == target and t.get("kind") == "codex"]
        if len(candidates) != 1:
            raise RuntimeError("桌面聊天不存在，请先在目标项目发起聊天。")
        # Caller context must be a different chat: the host disallows self-messaging.
        if target == connection["origin_thread"]:
            raise RuntimeError("请从安装/管理聊天连接目标测试聊天（--thread），不能把连接器自身作为收件聊天。")
        endpoint = json.loads((state / "endpoint.json").read_text(encoding="utf-8"))
        from bridge.config import read_config, find_role
        config = read_config(endpoint["config"])
        role = config["endpoints"][find_role(config, args.agent)]
        if os.path.normcase(str(Path(candidates[0].get("cwd", "")).resolve())) != os.path.normcase(str(Path(role["cwd"]).resolve())):
            raise RuntimeError("聊天工作目录与该角色端点不一致，拒绝绑定。")
        snapshot = desktop.call("read_thread", {"threadId":target,"hostId":"local","turnLimit":1,"maxOutputCharsPerItem":1000})
        if snapshot.get("thread",{}).get("id")!=target:
            raise RuntimeError("无法确认目标聊天，尚未修改路由。")
        Client(state).call("desktop_bind", agent=args.agent, session_id=target, transport="codex_app_tools")
        connection_path.write_text(json.dumps(connection, ensure_ascii=False, indent=2), encoding="utf-8")
        with (state / (args.agent.replace(".", "-") + "-desktop.log")).open("ab") as log:
            subprocess.Popen([sys.executable, str(ROOT / "desktop_link.py"), "--state", str(state), "--serve", "--agent", args.agent],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        print(json.dumps({"connected": True, "thread_id": target}, ensure_ascii=False))
    finally:
        desktop.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()

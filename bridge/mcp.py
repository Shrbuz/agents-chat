"""Small stdio MCP server. Never writes non-protocol text to stdout."""
import json
import sys

from . import __version__
from .store import compact


def tools_for(agent, default_transport="mcp"):
    definitions = [
        ("send", "Submit one bounded task to a configured role (use project.role for another project). Bound desktop sessions receive it in the existing chat; otherwise a new CLI task runs. Check bridge_targets for mode. Returns a task ID, not completion.",
         {"target": {"type": "string"}, "prompt": {"type": "string", "maxLength": 6000},
          "key": {"type": "string", "description": "Stable key for retries of this exact request."},
          "timeout": {"type": "integer", "minimum": 1, "maximum": 3600},
          "reply_mode": {"type": "string", "enum": ["mailbox", "run"]}}, ["target", "prompt", "key"]),
        ("wait", "Wait for one task using local event notifications; returns compact result. Do not repeatedly poll from the model.",
         {"task_id": {"type": "string"}, "seconds": {"type": "number", "minimum": 0, "maximum": 50}}, ["task_id"]),
        ("task", "Read one task status and compact result; never returns history.", {"task_id": {"type": "string"}}, ["task_id"]),
        ("inbox", "Read up to 10 unacknowledged results for this role. Explicit ack advances the durable cursor.", {}, []),
        ("ack", "Acknowledge inbox results already processed, through this cursor.", {"cursor": {"type": "integer"}}, ["cursor"]),
        ("targets", "List configured roles, projects, sessions and capabilities.", {}, []),
        ("cancel", "Request cancellation of one task. Existing file changes are not rolled back.", {"task_id": {"type": "string"}}, ["task_id"]),
        ("register", "Register or refresh this chat as a role session. Call once at session start, then check bridge_inbox.", {
            "session": {"type": "string", "description": "Stable ID for this chat, e.g. backend-<UUID>."},
            "transport": {"type": "string", "enum": ["chat", "cli", "desktop", "native", "mcp", "workbuddy_mcp", "opencode_native"]},
            "description": {"type": "string", "maxLength": 300}},
         [] if default_transport == "opencode_native" else ["session"]),
        ("pull", "Claim one queued task for this MCP pull-mode role. Call once at session start or when the user asks you to check work; do not poll. Complete the returned task in this turn with bridge_complete.", {}, []),
        ("complete", "Finish a task previously claimed by bridge_pull. Retrying the same completion is idempotent and never creates another business task.", {
            "task_id": {"type": "string"}, "summary": {"type": "string", "maxLength": 800},
            "state": {"type": "string", "enum": ["succeeded", "failed"]},
            "error": {"type": "string", "maxLength": 200}}, ["task_id", "summary"]),
        ("roles", "List projects, roles, session presence and task modes.", {}, []),
    ]
    return [{"name": "bridge_" + name, "description": description,
             "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}
            for name, description, properties, required in definitions]


def serve_mcp(client, agent, project=None, default_transport="mcp"):
    definitions = tools_for(agent, default_transport)
    known = {t["name"]: t for t in definitions}
    role = agent.split(".", 1)[1] if "." in agent else agent
    project = agent.split(".", 1)[0] if "." in agent else project
    for line in sys.stdin.buffer:
        request_id = None
        try:
            if len(line) > 65536:
                raise ValueError("message_too_large")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("invalid_request")
            request_id = request.get("id")
            method = request.get("method")
            if request_id is None:
                continue
            if method == "initialize":
                version = request.get("params", {}).get("protocolVersion")
                if version not in ("2024-11-05", "2025-03-26", "2025-06-18"):
                    version = "2025-06-18"
                pull_instructions = (" and bridge_pull once. If pull returns a task, perform it and "
                                     "call bridge_complete in the same turn"
                                     if default_transport == "workbuddy_mcp" else "")
                register_instructions = (
                    "bridge_register once without inventing a session ID; OpenCode supplies the real current session in MCP metadata"
                    if default_transport == "opencode_native" else
                    "bridge_register once with a stable session ID")
                result = {"protocolVersion": version, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "agents-chat-hub", "version": __version__},
                          "instructions": (f"You are Agents Chat role {agent}. At the start of this chat call "
                                           f"{register_instructions}, then call bridge_inbox"
                                           f"{pull_instructions}. Use bridge_send for peer tasks and bridge_wait for results.")}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": definitions}
            elif method == "tools/call":
                params = request.get("params", {})
                name = params.get("name")
                args = params.get("arguments", {})
                if name not in known or not isinstance(args, dict):
                    raise ValueError("invalid_tool_call")
                schema = known[name]["inputSchema"]
                if set(args) - set(schema["properties"]) or not set(schema["required"]) <= set(args):
                    raise ValueError("invalid_tool_arguments")
                action = name.removeprefix("bridge_")
                args = dict(args)
                metadata = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
                opencode_session = metadata.get("ai.opencode/sessionID")
                if default_transport == "opencode_native":
                    if not isinstance(opencode_session, str) or not opencode_session:
                        raise ValueError("opencode_session_context_missing")
                    supplied = args.get("session")
                    if supplied and supplied != opencode_session:
                        raise ValueError("opencode_session_mismatch: do not invent or copy a session ID")
                if action == "send":
                    args["sender"] = agent
                if action in ("inbox", "ack"):
                    args.update(recipient=agent, consumer="mcp-" + agent)
                if action in ("pull", "complete"):
                    args.update(recipient=agent)
                if action == "register":
                    if not project:
                        raise ValueError("role_project_unknown: start MCP with --project or use a qualified agent address")
                    if default_transport == "opencode_native":
                        value = client.call("desktop_bind", agent=agent, project=project,
                                            session_id=opencode_session,
                                            transport="opencode_native")
                        result = {"content": [{"type": "text", "text": compact(value)}]}
                        reply = {"jsonrpc": "2.0", "id": request_id, "result": result}
                        print(compact(reply), flush=True)
                        continue
                    args.update(project=project, role=role, transport=default_transport)
                    action = "session_register"
                elif default_transport == "opencode_native":
                    client.call("desktop_heartbeat", agent=agent, project=project,
                                session_id=opencode_session, transport="opencode_native")
                try:
                    value = client.call(action, **args)
                    result = {"content": [{"type": "text", "text": compact(value)}]}
                except Exception as exc:
                    result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            else:
                print(compact({"jsonrpc": "2.0", "id": request_id,
                               "error": {"code": -32601, "message": "Method not found"}}), flush=True)
                continue
            reply = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            reply = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)[:300]}}
        print(compact(reply), flush=True)

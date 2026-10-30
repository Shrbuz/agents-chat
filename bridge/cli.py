import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .broker import label, read_config, serve
from .client import Client
from .config import (ConfigError, find_role, project_id, projects_summary,
                     upsert_project, delete_project, delete_role)
from .onboarding import add_role as add_project_role
from .onboarding import cli_command, role_prompt, setup as setup_project, update_entry
from .paths import ROOT, default_config, project_context, resolve_config, resolve_state
from .store import compact

VERSION = "0.5.0"


def start_desktop_connectors(state, config_path=None):
    path = state / "desktop-bindings.json"
    if not path.exists():
        return
    bindings = json.loads(path.read_text(encoding="utf-8"))
    try:
        config = read_config(config_path) if config_path else None
    except ValueError:
        config = None
    for agent, binding in bindings.items():
        endpoint = config["endpoints"].get(agent) if config else None
        adapter = endpoint["adapter"] if endpoint else ("qoder" if agent == "qoder" else "codex")
        safe = agent.replace(".", "-")
        if adapter == "codex":
            script, arguments = "desktop_link.py", ["--state", str(state), "--serve", "--agent", agent]
            if not (state / "codex-desktop.local.json").exists():
                continue
        elif adapter == "qoder" and binding.get("transport") != "qoder_native":
            if (state / "qoder-input-disabled").exists() or agent != "qoder":
                continue
            script, arguments = "qoder_desktop_link.py", [str(state)]
        else:
            continue
        if not (ROOT / script).exists():
            continue
        from .broker import SingleInstance
        try:
            probe = SingleInstance(state / (safe + "-desktop.lock"))
        except RuntimeError:
            continue
        probe.close()
        with (state / (safe + "-desktop.log")).open("ab") as log:
            process = subprocess.Popen([sys.executable, str(ROOT / script), *arguments], stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            threading.Thread(target=process.wait, daemon=True).start()


def start(state, config):
    client = Client(state)
    try:
        return {**client.call("health"), "already_running": True}
    except RuntimeError:
        pass
    read_config(config)
    state.mkdir(parents=True, exist_ok=True)
    with (state / "broker.log").open("ab") as output:
        process = subprocess.Popen([sys.executable, str(ROOT / "bridge.py"), "--state", str(state),
                                    "serve", "--config", str(Path(config).resolve())],
                                   stdout=output, stderr=output, stdin=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                   start_new_session=os.name != "nt")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("broker_start_failed; inspect " + str(state / "broker.log"))
        try:
            health = client.call("health")
            start_desktop_connectors(state, config)
            threading.Thread(target=process.wait, daemon=True).start()
            return health
        except RuntimeError:
            time.sleep(0.1)
    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError("broker_start_timeout")


def stop(state):
    try:
        return Client(state).call("shutdown")
    except RuntimeError:
        return {"stopping": False, "reason": "broker_not_running"}


def wait_stopped(state, seconds=15):
    deadline = time.monotonic() + seconds
    while (state / "endpoint.json").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    return not (state / "endpoint.json").exists()


def restart(state, config):
    state = Path(state)
    try:
        Client(state).call("shutdown")
        if not wait_stopped(state):
            raise RuntimeError("broker_still_running")
    except RuntimeError as exc:
        if "broker_not_running" not in str(exc) and "broker_still_running" in str(exc):
            raise
    return start(state, config)


def live_restart_if_running(state, config, enabled=True):
    if not enabled:
        return {"hub": "restart_skipped"}
    try:
        Client(state).call("health")
    except RuntimeError:
        return {"hub": "offline", "note": "start the hub to load this configuration"}
    return {"hub": "restarted", "health": restart(state, config)}


def doctor(config):
    data = read_config(config)
    results = []
    for name, endpoint in data["endpoints"].items():
        entry = {"name": name, "address": name, "project": endpoint.get("project"),
                 "role": endpoint.get("role"), "adapter": endpoint["adapter"], "cwd": endpoint["cwd"],
                 "mode": "independent_cli_task", "desktop_wakeup": False}
        if endpoint["adapter"] == "fixture":
            entry.update(available=True, version="test fixture; no AI model")
        else:
            try:
                probe = subprocess.run(endpoint["command"] + ["--version"], capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=15,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                entry.update(available=probe.returncode == 0, version=probe.stdout.strip()[:200])
                if probe.returncode:
                    entry["error"] = probe.stderr.strip()[:300]
            except (OSError, subprocess.TimeoutExpired) as exc:
                entry.update(available=False, error=str(exc)[:300])
        results.append(entry)
    return {"runtimes": results, "projects": projects_summary(data),
            "note": "Version probe does not verify login or model access."}


def join_role(state, config, project, role, session=None, transport=None):
    """Bind the current desktop chat to a role.

    Codex exposes its current thread through CODEX_THREAD_ID.  Qoder's current
    selected chat is read through the existing accessibility helper, which is a
    read-only operation even while composer automation is disabled.
    """
    data = read_config(config)
    internal = find_role(data, role, project)
    endpoint = data["endpoints"][internal]
    adapter = endpoint["adapter"]
    if not transport:
        transport = "codex_app_tools" if adapter == "codex" else "qoder_native" if adapter == "qoder" else None
    if not session:
        if adapter == "codex":
            session = os.environ.get("CODEX_THREAD_ID")
            transport = transport or "codex_app_tools"
            if not session:
                raise ValueError("cannot_detect_desktop_chat: run this from the Codex chat, or pass --session")
            connection_path = Path(state) / "codex-desktop.local.json"
            if connection_path.exists():
                connection = json.loads(connection_path.read_text(encoding="utf-8"))
                if connection.get("origin_thread") == session:
                    raise ValueError("cannot_bind_connector_origin_chat: use a different target chat")
        elif adapter == "qoder":
            transport = transport or "qoder_native"
            status_path = Path(state) / "qoder-native-status.json"
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                status = {}
            if not status.get("ready") or time.time() * 1000 - status.get("updated_at", 0) > 15000:
                raise ValueError("qoder_native_offline: start Qoder through the Agents Chat native entry first")
            from qoder_desktop_link import ui
            session = ui("inspect").get("session_id")
            if not session:
                raise ValueError("cannot_detect_qoder_chat: open the target chat and try again")
        else:
            raise ValueError("desktop_bind_unsupported_adapter: " + adapter)
    result = Client(state).call("desktop_bind", agent=internal, project=project,
                                session_id=session, transport=transport)
    warning = None
    if adapter == "codex":
        connection_path = Path(state) / "codex-desktop.local.json"
        if not connection_path.exists():
            warning = "codex_connector_context_missing: prepare it once from a management chat"
        else:
            start_desktop_connectors(Path(state), config)
    return {"joined": True, "address": internal, "session_id": session,
            "transport": transport, "mode": result.get("mode"), "warning": warning,
            "note": "The desktop chat is now the execution target for this role; result returns to the sender inbox."}


def _role_specs(values, default_adapter="codex"):
    specs = []
    for value in values:
        parts = value.split(":", 2)
        if not parts[0]:
            raise ConfigError("role_name_required")
        spec = {"role": parts[0], "adapter": parts[1] if len(parts) > 1 and parts[1] else default_adapter}
        if len(parts) > 2:
            spec["description"] = parts[2]
        specs.append(spec)
    return specs


def _config_for_write(args):
    explicit = getattr(args, "config_command", None) or args.config
    if explicit:
        return Path(explicit).resolve()
    found = default_config()
    if found:
        return found
    return (ROOT / "config.local.json").resolve()


def _project_option(parser):
    parser.add_argument("--project", dest="project_local", default=argparse.SUPPRESS,
                        help="Project ID (may also be given before the subcommand).")


def parser():
    p = argparse.ArgumentParser(description="Agents Chat: multi-project, multi-role local agent collaboration hub",
                                allow_abbrev=False)
    p.add_argument("--state", help="State directory (SQLite, logs, endpoint). Defaults to the config's .bridge/data.")
    p.add_argument("--config", help="Hub configuration file. Also discoverable from .agents-chat/project.json.")
    p.add_argument("--project", help="Project ID for commands that need one.")
    p.add_argument("--version", action="version", version="Agents Chat " + VERSION)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("start", "serve", "doctor"):
        s = sub.add_parser(name)
        s.add_argument("--config", dest="config_command")
    for name in ("health", "targets", "stop", "restart"):
        sub.add_parser(name)
    for name in ("status", "roles"):
        _project_option(sub.add_parser(name))
    s = sub.add_parser("send")
    _project_option(s)
    s.add_argument("--from", dest="sender", required=True)
    s.add_argument("--to", dest="target", required=True)
    group = s.add_mutually_exclusive_group(required=True)
    group.add_argument("--text")
    group.add_argument("--file")
    s.add_argument("--key", required=True, help="Reuse this key if the response is lost; never reuse for different content")
    s.add_argument("--timeout", type=int, default=180)
    s.add_argument("--queue-ttl", type=int, default=3600)
    s.add_argument("--context", action="append", default=[])
    s.add_argument("--reply-mode", choices=("mailbox", "run"), default="mailbox")
    for name in ("task", "cancel", "wait"):
        s = sub.add_parser(name)
        s.add_argument("task_id")
        if name == "wait":
            s.add_argument("--timeout", type=int, default=300)
    for name in ("inbox", "ack"):
        s = sub.add_parser(name)
        _project_option(s)
        s.add_argument("--agent", "--role", dest="agent", required=True)
        s.add_argument("--consumer", default="default")
        if name == "inbox":
            s.add_argument("--after", type=int)
            s.add_argument("--limit", type=int, default=10)
        else:
            s.add_argument("--cursor", type=int, required=True)
    s = sub.add_parser("mcp")
    _project_option(s)
    s.add_argument("--agent", "--role", dest="agent", required=True)
    s = sub.add_parser("native-result")
    s.add_argument("task_id")
    s.add_argument("--receipt", required=True)
    s.add_argument("--text", required=True)
    s = sub.add_parser("register")
    _project_option(s)
    s.add_argument("--role", required=True)
    s.add_argument("--session", required=True)
    s.add_argument("--transport", default="chat")
    s.add_argument("--description", default="")
    s = sub.add_parser("heartbeat")
    s.add_argument("--session", required=True)
    s = sub.add_parser("join")
    _project_option(s)
    s.add_argument("--role", required=True)
    s.add_argument("--session", help="Desktop chat/thread ID; inferred for Codex from CODEX_THREAD_ID")
    s.add_argument("--transport", choices=("codex_app_tools", "qoder_native", "qoder_extension", "qoder_accessibility"))
    s = sub.add_parser("sessions")
    _project_option(s)
    s.add_argument("--role")
    s.add_argument("--online", action="store_true")
    # project commands
    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    add = project_sub.add_parser("add")
    add.add_argument("--id", required=True)
    add.add_argument("--name")
    add.add_argument("--root", required=True)
    add.add_argument("--description", default="")
    add.add_argument("--bus")
    add.add_argument("--restart", action="store_true")
    project_sub.add_parser("list")
    remove = project_sub.add_parser("remove")
    remove.add_argument("--id", required=True)
    remove.add_argument("--force", action="store_true")
    # role commands
    role = sub.add_parser("role")
    role_sub = role.add_subparsers(dest="role_command", required=True)
    add = role_sub.add_parser("add")
    _project_option(add)
    add.add_argument("--role", required=True)
    add.add_argument("--adapter", default="codex", choices=("codex", "qoder", "codebuddy", "fixture"))
    add.add_argument("--cwd")
    add.add_argument("--description", default="")
    add.add_argument("--peer", action="append", default=[])
    add.add_argument("--exe")
    add.add_argument("--arg", action="append", default=[])
    add.add_argument("--sandbox", choices=("read-only", "workspace-write"))
    add.add_argument("--no-restart", action="store_true")
    _project_option(role_sub.add_parser("list"))
    remove = role_sub.add_parser("remove")
    _project_option(remove)
    remove.add_argument("--role", required=True)
    remove.add_argument("--no-restart", action="store_true")
    prompt = role_sub.add_parser("prompt")
    _project_option(prompt)
    prompt.add_argument("--role", required=True)
    bind = role_sub.add_parser("bind")
    _project_option(bind)
    bind.add_argument("--role", required=True)
    bind.add_argument("--session", required=True)
    bind.add_argument("--transport", required=True)
    unbind = role_sub.add_parser("unbind")
    _project_option(unbind)
    unbind.add_argument("--role", required=True)
    # setup
    s = sub.add_parser("setup")
    _project_option(s)
    s.add_argument("--root", required=True, help="Project root folder")
    s.add_argument("--name")
    s.add_argument("--description", default="")
    s.add_argument("--role", action="append", default=[], help="role[:adapter[:description]], repeatable")
    s.add_argument("--adapter", default="codex", choices=("codex", "qoder", "codebuddy", "fixture"))
    s.add_argument("--force", action="store_true", help="Replace existing role runtime settings")
    s.add_argument("--no-agents", action="store_true")
    s.add_argument("--no-restart", action="store_true")
    return p


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    context = project_context()
    project = getattr(args, "project_local", None) or args.project or context.get("project_id")
    try:
        if args.command in ("project", "setup"):
            config = _config_for_write(args)
        else:
            config = resolve_config(getattr(args, "config_command", None) or args.config)
        state = resolve_state(args.state, config)
        client = Client(state)
        if args.command == "serve":
            serve(state, config)
            return
        if args.command == "start":
            result = start(state, config)
        elif args.command == "stop":
            result = stop(state)
        elif args.command == "restart":
            result = restart(state, config)
        elif args.command == "doctor":
            result = doctor(config)
        elif args.command == "status":
            result = {"config": str(config), "state": str(state), "project": project}
            try:
                live = client.call("status")
                result["health"] = live["health"]
                result["roles"] = live["roles"]
            except RuntimeError:
                result["hub"] = "offline"
                result["projects"] = projects_summary(read_config(config))
        elif args.command in ("roles", "targets", "health"):
            try:
                result = client.call(args.command)
            except RuntimeError:
                result = {"hub": "offline", "projects": projects_summary(read_config(config))}
        elif args.command == "mcp":
            from .mcp import serve_mcp
            if not project:
                try:
                    resolved = read_config(config)
                    internal = find_role(resolved, args.agent)
                    project = resolved["endpoints"][internal]["project"]
                except ValueError:
                    project = None
            serve_mcp(client, label(args.agent), project)
            return
        elif args.command == "send":
            prompt = Path(args.file).read_text(encoding="utf-8-sig") if args.file else args.text
            result = client.call("send", sender=args.sender, target=args.target, prompt=prompt, key=args.key,
                                 timeout=args.timeout, context=args.context, reply_mode=args.reply_mode,
                                 queue_ttl=args.queue_ttl, **({"project": project} if project else {}))
        elif args.command == "wait":
            if not 0 <= args.timeout <= 3600:
                raise ValueError("wait_timeout_must_be_0_to_3600")
            result = client.wait(args.task_id, args.timeout)
        elif args.command in ("task", "cancel"):
            result = client.call(args.command, task_id=args.task_id)
        elif args.command == "inbox":
            result = client.call("inbox", recipient=args.agent, consumer=args.consumer,
                                 after=args.after, limit=args.limit,
                                 **({"project": project} if project else {}))
        elif args.command == "ack":
            result = client.call("ack", recipient=args.agent, consumer=args.consumer,
                                 cursor=args.cursor, **({"project": project} if project else {}))
        elif args.command == "native-result":
            from .native_result import write_report
            task = client.call("task", task_id=args.task_id)
            endpoint = read_config(config)["endpoints"][find_role(read_config(config), task["target"])]
            if endpoint["adapter"] != "qoder" or task["state"] != "running":
                raise ValueError("native_task_not_running")
            result = write_report(state, args.task_id, args.receipt, args.text)
        elif args.command == "register":
            if not project:
                raise ValueError("project_required: pass --project or run inside a project entry")
            result = client.call("session_register", project=project, role=args.role, session=args.session,
                                 transport=args.transport, description=args.description)
        elif args.command == "heartbeat":
            result = client.call("session_heartbeat", session=args.session)
        elif args.command == "join":
            if not project:
                raise ValueError("project_required: pass --project or run inside a project entry")
            result = join_role(state, config, project, args.role, args.session, args.transport)
        elif args.command == "sessions":
            result = client.call("session_list", project=project, role=args.role, include_offline=not args.online)
        elif args.command == "project":
            result = project_command(args, config, state)
        elif args.command == "role":
            result = role_command(args, config, state, project)
        elif args.command == "setup":
            roles = _role_specs(args.role, args.adapter)
            result = setup_project(config, args.root, project=getattr(args, "project_local", None) or args.project,
                                   name=args.name,
                                   description=args.description, roles=roles, state=state,
                                   write_agents=not args.no_agents, force=args.force)
            result.update(live_restart_if_running(state, config, not args.no_restart))
        else:
            raise ValueError("unknown_command")
        print(compact(result))
    except (RuntimeError, ValueError, OSError, KeyError) as exc:
        print(compact({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)


def project_command(args, config, state):
    if args.project_command == "add":
        pid = project_id(args.id)
        upsert_project(config, pid, name=args.name, root=args.root, description=args.description, bus=args.bus)
        result = {"project": pid, "config": str(config), "restart_required": True}
        if args.restart:
            result.update(live_restart_if_running(state, config, True))
        return result
    if args.project_command == "list":
        return {"projects": projects_summary(read_config(config)), "config": str(config)}
    if args.project_command == "remove":
        pid = project_id(args.id)
        if not args.force:
            try:
                roles = Client(state).call("roles")["projects"]
            except RuntimeError:
                roles = []
            match = next((p for p in roles if p["id"] == pid), None)
            if match and any(role.get("pending") for role in match["roles"]):
                raise ValueError("project_has_pending_tasks")
        delete_project(config, pid)
        return {"project": pid, "removed": True, "restart_required": True,
                **live_restart_if_running(state, config, True)}
    raise ValueError("unknown_project_command")


def role_command(args, config, state, project):
    if args.role_command == "add":
        if not project:
            raise ValueError("project_required: pass --project or run inside a project entry")
        command = [args.exe, *args.arg] if args.exe else None
        result = add_project_role(config, project, args.role, args.adapter, cwd=args.cwd,
                                  description=args.description, peers=args.peer, command=command,
                                  sandbox=args.sandbox)
        result.update(live_restart_if_running(state, config, not args.no_restart))
        return result
    if args.role_command == "list":
        try:
            return Client(state).call("roles")
        except RuntimeError:
            return {"hub": "offline", "projects": projects_summary(read_config(config))}
    if args.role_command == "remove":
        if not project:
            raise ValueError("project_required")
        normalized = read_config(config)
        project_entry = normalized["projects"].get(project)
        if not project_entry:
            raise ValueError("unknown_project: " + project)
        address = project_entry["roles"].get(args.role)
        if not address:
            raise ValueError("unknown_role: " + args.role)
        try:
            for item in Client(state).call("roles")["projects"]:
                for role in item["roles"]:
                    if role["address"] == address and role["pending"]:
                        raise ValueError("role_has_pending_tasks")
        except RuntimeError:
            pass
        # Remove the desktop binding before deleting the role, otherwise the
        # connector would keep polling a role that no longer exists.
        try:
            Client(state).call("desktop_bind", agent=address, project=project, session_id=None)
        except RuntimeError:
            pass
        delete_role(config, project, args.role)
        entry = update_entry(config, project)
        return {"project": project, "role": args.role, "removed": True,
                "entry": str(entry) if entry else None,
                "restart_required": True, **live_restart_if_running(state, config, not args.no_restart)}
    if args.role_command == "prompt":
        if not project:
            raise ValueError("project_required")
        return {"prompt": role_prompt(config, project, args.role)}
    if args.role_command == "bind":
        if not project:
            raise ValueError("project_required")
        return Client(state).call("desktop_bind", agent=args.role, project=project,
                                  session_id=args.session, transport=args.transport)
    if args.role_command == "unbind":
        if not project:
            raise ValueError("project_required")
        return Client(state).call("desktop_bind", agent=args.role, project=project, session_id=None)
    raise ValueError("unknown_role_command")

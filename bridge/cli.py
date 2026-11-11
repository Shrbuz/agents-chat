import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .broker import label, read_config, serve
from .client import Client
from .codex_connection import capture_current_connection, select_connection
from .config import (ADAPTERS, ConfigError, TRANSPORT_ADAPTERS, adapter_for_transport,
                     convert_raw, default_transport, delete_project, delete_role, find_role,
                     load_raw, normalize_config, project_id, projects_summary,
                     save_raw, session_id as config_session_id, transport_supported,
                     upsert_project)
from .onboarding import add_role as add_project_role
from .onboarding import (cli_command, endpoint_for, role_prompt, setup as setup_project,
                         update_entry)
from .paths import ROOT, default_config, project_context, resolve_config, resolve_state
from .store import Store, compact
from .buscli import add_commands as add_bus_commands, bus_root, run as run_bus

from . import __version__ as VERSION


def start_desktop_connectors(state, config_path=None):
    # If this command is running inside Codex, publish its current ephemeral
    # pipe/runtime before deciding which role connectors can start.
    capture_current_connection(state)
    path = state / "desktop-bindings.json"
    if not path.exists():
        return
    bindings = json.loads(path.read_text(encoding="utf-8"))
    try:
        config = read_config(config_path) if config_path else None
    except ValueError:
        config = None
    try:
        Client(state).call("connectors_start")
    except RuntimeError:
        pass
    for agent, binding in bindings.items():
        endpoint = config["endpoints"].get(agent) if config else None
        adapter = endpoint["adapter"] if endpoint else ("qoder" if agent == "qoder" else "codex")
        safe = agent.replace(".", "-")
        if adapter == "codex":
            # The broker supervises Codex connector processes so a transport
            # refresh/shutdown can replace only those processes and wait for
            # their locks to be released. Qoder keeps its own native lifecycle.
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


# ---------------------------------------------------------------------------
# Adapter migration / takeover
#
# "Take over a role" means the current client owns the whole role.  When the
# client type changes, the role's adapter must change with it.  "Switch/bind a
# session" keeps the adapter and only moves the chat.  These helpers implement
# the first case as a single recoverable transaction.


def _atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".migration.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _read_json_file(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_binding(state, internal, binding):
    path = Path(state) / "desktop-bindings.json"
    data = _read_json_file(path, {})
    if not isinstance(data, dict):
        data = {}
    if binding is None:
        data.pop(internal, None)
    else:
        data[internal] = binding
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _current_client():
    """Best-effort (transport, session) for the client running this command."""
    codex = os.environ.get("CODEX_THREAD_ID")
    if codex:
        return "codex_app_tools", codex
    dsh = os.environ.get("DSH_SESSION_ID") or os.environ.get("DSH_SESSION")
    if dsh:
        return "dsh_plugin", dsh
    transport = os.environ.get("AGENTS_CHAT_TRANSPORT")
    session = os.environ.get("AGENTS_CHAT_SESSION")
    if transport and session:
        return transport, session
    return None, None


def _require_qoder_native(state):
    status_path = Path(state) / "qoder-native-status.json"
    status = _read_json_file(status_path, {}) or {}
    if not status.get("ready") or time.time() * 1000 - status.get("updated_at", 0) > 15000:
        raise ValueError(
            "qoder_native_offline: the native connector is not reporting to this Hub; "
            "restart Qoder after installing or repairing the Agents Chat connector")


def _resolve_target_session(state, adapter, session, transport=None):
    if transport == "qoder_native":
        # An explicit session id proves only that a chat once existed.  Require
        # the native extension heartbeat as well so join/takeover cannot report
        # success for a binding that has no live delivery path.
        _require_qoder_native(state)
    if not session and adapter == "codex":
        session = os.environ.get("CODEX_THREAD_ID")
    elif not session and adapter == "dsh":
        session = os.environ.get("DSH_SESSION_ID") or os.environ.get("AGENTS_CHAT_SESSION")
    elif not session and adapter == "qoder":
        if transport in (None, "qoder_native"):
            _require_qoder_native(state)
        from qoder_desktop_link import ui
        session = ui("inspect").get("session_id")
    if not session:
        raise ValueError("cannot_detect_desktop_chat: run this from the target client chat, "
                         "or pass --session")
    return config_session_id(session)


def _activate_target_client(state, config, adapter):
    """Start the connector that can serve the migrated role, when configured."""
    warning = None
    if adapter == "codex":
        capture_current_connection(state)
        if not select_connection(state):
            return "codex_connector_context_missing: register from a live Codex desktop chat"
    if adapter in ("codex", "qoder"):
        try:
            start_desktop_connectors(Path(state), config)
        except Exception as exc:  # A connector that cannot start must not undo a good migration.
            warning = "connector_not_started: " + str(exc)[:200]
    return warning


def _check_codex_origin(state, adapter, session):
    if adapter != "codex":
        return
    capture_current_connection(state)
    if select_connection(state, session) is None:
        raise ValueError("cannot_bind_connector_origin_chat: register another live Codex chat "
                         "from the current app generation so it can deliver to this target")


def _migrated_endpoint(endpoint, target_adapter, command=None, args=None):
    """A complete runtime description for the new adapter.

    Adapter-specific argv is rebuilt instead of carried over: a dsh command must
    never be left behind in a codex role (or the reverse).
    """
    command_value = None
    if command:
        command_value = [command] if isinstance(command, str) else list(command)
    migrated = endpoint_for(target_adapter, endpoint["cwd"], endpoint.get("description", ""),
                            peers=endpoint.get("peers") or [], command=command_value,
                            sandbox=endpoint.get("sandbox"))
    # Only adapter-neutral settings survive.  This is what prevents a half
    # migration that keeps one adapter's private fields under another adapter.
    for key in ("enabled", "desktop_input_enabled", "max_output_bytes",
                "skill_catalog_tokens", "tool_output_tokens", "name", "mode"):
        if key in endpoint:
            migrated[key] = endpoint[key]
    if args:
        migrated["args"] = list(args)
    return migrated


def _apply_adapter_config(config_path, project, role, target_adapter, command=None, args=None):
    data = read_config(config_path)
    internal = find_role(data, role, project)
    endpoint = data["endpoints"][internal]
    rid = endpoint["role"]
    migrated = _migrated_endpoint(endpoint, target_adapter, command, args)
    raw = convert_raw(load_raw(config_path), config_path)
    project_raw = (raw.get("projects") or {}).get(project)
    if project_raw is None:
        raise ValueError("unknown_project: " + project)
    roles_raw = project_raw.setdefault("roles", {})
    previous = roles_raw.get(rid)
    roles_raw[rid] = migrated
    try:
        # Validate the whole document before it ever reaches disk.
        normalize_config(raw, config_path)
    except Exception:
        if previous is None:
            roles_raw.pop(rid, None)
        else:
            roles_raw[rid] = previous
        raise
    save_raw(config_path, raw)
    return internal


def _sync_project_entry(config_path, project, role, target_adapter):
    data = read_config(config_path)
    root = Path(data["projects"][project]["root"])
    entry_path = root / ".agents-chat" / "project.json"
    if not entry_path.is_file():
        return None
    if update_entry(config_path, project) is None:
        raise ValueError("project_entry_update_failed")
    entry = _read_json_file(entry_path, {}) or {}
    saved = ((entry.get("roles") or {}).get(role) or {}).get("adapter")
    if saved != target_adapter:
        raise ValueError("project_entry_adapter_not_updated: " + role)
    return entry_path


class HubControl:
    """Cycle the hub without assuming whether it was running before."""

    def __init__(self, state, config):
        self.state = Path(state)
        self.config = Path(config)

    def running(self):
        try:
            Client(self.state).call("health")
            return True
        except RuntimeError:
            return False

    def restart(self):
        return restart(self.state, self.config)

    def start(self):
        return start(self.state, self.config)

    def stop(self):
        result = stop(self.state)
        wait_stopped(self.state)
        return result


def verify_takeover(client, config_path, project, role, session, transport, expected_adapter,
                    strict=True, check_entry=True):
    """The postconditions the skill and the tray are allowed to trust.

    ``strict`` additionally requires one active session and retirement of the
    old ones.  A first-time ``join`` binds one role but does not claim to have
    retired chats that were never bound, so it only needs the consistency checks.
    """
    data = read_config(config_path)
    internal = find_role(data, role, project)
    endpoint = data["endpoints"][internal]
    listed = client.call("roles")
    role_data = None
    for item in listed.get("projects", []):
        if item.get("id") != project:
            continue
        role_data = next((role_item for role_item in item.get("roles", [])
                          if role_item.get("address") == internal), None)
    if role_data is None:
        raise ValueError("takeover_role_not_listed: " + internal)
    sessions = role_data.get("sessions") or []
    keep = "desktop:" + session
    row = next((item for item in sessions if item.get("id") in (keep, session)), None)
    others = [item for item in sessions if item.get("id") != keep]
    active = (client.call("session_list", project=project) or {}).get("active") or {}
    # Read the adapter from the running hub, not from the file on disk: a hub
    # that failed to reload the new config must not be reported as migrated.
    role_adapter = role_data.get("adapter")
    checks = {
        "role_adapter_matches_client": (role_adapter == "fixture"
                                        or role_adapter == expected_adapter),
        "session_adapter_matches_role": bool(
            row and (role_adapter == "fixture" or row.get("adapter") == role_adapter)),
        "transport_matches_adapter": (role_adapter == "fixture"
                                      or transport_supported(role_adapter, transport)),
        "single_active_session": (active.get(endpoint["role"]) == session
                                  and not [item for item in others if not item.get("retired")]),
        "old_sessions_retired": all(item.get("retired") for item in others),
    }
    entry_path = Path(data["projects"][project]["root"]) / ".agents-chat" / "project.json"
    entry_adapter = None
    if check_entry and entry_path.is_file():
        entry = _read_json_file(entry_path, {}) or {}
        entry_adapter = ((entry.get("roles") or {}).get(endpoint["role"]) or {}).get("adapter")
        checks["project_entry_adapter_matches"] = entry_adapter == expected_adapter
    retired = [item.get("id") for item in others if item.get("retired")]
    required = checks if strict else {key: value for key, value in checks.items()
                                      if key not in ("single_active_session", "old_sessions_retired")}
    return {
        "ok": all(required.values()),
        "strict": strict,
        "checks": checks,
        "role_adapter": role_adapter,
        "client_adapter": expected_adapter,
        "session_adapter": row.get("adapter") if row else None,
        "project_entry_adapter": entry_adapter,
        "transport": transport,
        "active_session": active.get(endpoint["role"]),
        "retired_sessions": retired,
    }


def _rollback_migration(state, config_path, context):
    """Undo every step in reverse order; never raise over the original error."""
    errors = []
    if context.get("takeover_called") or context.get("hub_touched"):
        try:
            Store(Path(state) / "bridge.sqlite3").restore_session_snapshot(
                context["project"], context["role_id"], context["sessions"])
        except Exception as exc:
            errors.append("sessions:" + str(exc)[:180])
        try:
            _write_binding(state, context["internal"], context["binding"])
        except Exception as exc:
            errors.append("binding:" + str(exc)[:180])
        active_path = context.get("active_path")
        active_text = context.get("active_text")
        if active_path is not None:
            try:
                if active_text is not None:
                    _atomic_write_text(active_path, active_text)
                elif not context.get("active_existed", True):
                    # The migration may have created the map while starting the
                    # hub; an absent map must not survive rollback.
                    active_path.unlink(missing_ok=True)
            except Exception as exc:
                errors.append("active_sessions:" + str(exc)[:180])
    if context.get("config_written"):
        try:
            _atomic_write_text(config_path, context["config_text"])
        except Exception as exc:
            errors.append("config:" + str(exc)[:180])
    if context.get("entry_written") and context.get("entry_path") is not None:
        try:
            _atomic_write_text(context["entry_path"], context["entry_text"])
        except Exception as exc:
            errors.append("project_entry:" + str(exc)[:180])
    if context.get("hub_touched"):
        try:
            if context["was_running"]:
                context["hub"].restart()
            else:
                context["hub"].stop()
        except Exception as exc:
            errors.append("hub:" + str(exc)[:180])
    return errors


def migrate_and_takeover(state, config, project, role, session=None, transport=None,
                         adapter="auto", command=None, args=None,
                         client=None, hub=None):
    """Move a role to this client's adapter and chat in one recoverable step.

    Same-adapter calls are a plain session switch: no config rewrite and no hub
    restart.  A real adapter change snapshots everything first, then updates the
    config, the project entry, restarts the hub, binds the new session, retires
    the old ones and verifies the postconditions.  Any failure restores the
    snapshot before it is reported.
    """
    state = Path(state).resolve()
    config = Path(config).resolve()
    client = client or Client(state)
    hub = hub or HubControl(state, config)
    data = read_config(config)
    internal = find_role(data, role, project)
    endpoint = data["endpoints"][internal]
    current_adapter = endpoint["adapter"]
    project_id, role_id = endpoint["project"], endpoint["role"]
    # The caller may have found the role without a --project (legacy config, or
    # an unambiguous role name); every later step needs the real project.
    project = project_id

    current_transport, current_session = _current_client()
    if not transport:
        if current_transport and (not session or session == current_session):
            transport = current_transport
            if not session:
                session = current_session
        else:
            transport = default_transport(current_adapter)
    if not session and current_session:
        session = current_session
    if not transport:
        raise ValueError("transport_required: pass --transport (codex_app_tools, qoder_native, opencode_native, dsh_plugin)")
    derived = adapter_for_transport(transport)
    if derived is None:
        raise ValueError("unsupported_desktop_transport: " + str(transport))
    target_adapter = derived if adapter in (None, "auto") else adapter
    if target_adapter not in ADAPTERS:
        raise ValueError("unknown_adapter: " + str(target_adapter))
    if not transport_supported(target_adapter, transport):
        raise ValueError("adapter_transport_mismatch: adapter=" + target_adapter
                         + " cannot use transport=" + str(transport))
    if current_transport and session and session == current_session and transport != current_transport:
        raise ValueError("adapter_transport_mismatch: the current client uses transport="
                         + current_transport + ", not " + str(transport))
    session = _resolve_target_session(state, target_adapter, session, transport)
    _check_codex_origin(state, target_adapter, session)

    if target_adapter == current_adapter:
        result = client.call("session_takeover", agent=internal, project=project,
                             session_id=session, transport=transport)
        postconditions = verify_takeover(client, config, project, internal, session,
                                         transport, target_adapter, check_entry=False)
        if not postconditions["ok"]:
            raise ValueError("takeover_postconditions_failed: " + compact(postconditions["checks"]))
        result.update({"adapter": target_adapter, "previous_adapter": current_adapter,
                       "adapter_changed": False, "hub_restarted": False, "hub_started": False,
                       "active_session": postconditions["active_session"] or session,
                       "retired_sessions": postconditions["retired_sessions"],
                       "postconditions": postconditions})
        warning = _activate_target_client(state, config, target_adapter)
        if warning:
            result["warning"] = warning
        return result

    # Fail before writing anything if the target runtime cannot be built at all.
    _migrated_endpoint(endpoint, target_adapter, command, args)

    config_text = config.read_text(encoding="utf-8")
    root = Path(data["projects"][project]["root"])
    entry_path = root / ".agents-chat" / "project.json"
    entry_text = entry_path.read_text(encoding="utf-8") if entry_path.is_file() else None
    bindings = _read_json_file(Path(state) / "desktop-bindings.json", {}) or {}
    previous_binding = bindings.get(internal) if isinstance(bindings, dict) else None
    store = Store(Path(state) / "bridge.sqlite3")
    sessions_snapshot = store.session_snapshot(project_id, role_id)
    chat = data["projects"][project].get("agent_chat") or {}
    active_path = Path(chat["bus"]) / "_state" / "active-sessions.json" if chat.get("bus") else None
    active_existed = bool(active_path and active_path.is_file())
    active_text = active_path.read_text(encoding="utf-8") if active_existed else None
    was_running = hub.running()
    context = {
        "project": project_id, "role_id": role_id, "internal": internal,
        "sessions": sessions_snapshot, "binding": previous_binding,
        "active_path": active_path, "active_text": active_text,
        "active_existed": active_existed,
        "config_text": config_text, "config_written": False,
        "entry_path": entry_path, "entry_text": entry_text, "entry_written": False,
        "hub": hub, "was_running": was_running, "hub_touched": False,
        "takeover_called": False,
    }
    try:
        # Mark them first: a write can fail after replacing the file, and the
        # rollback still has to put the original bytes back.
        context["config_written"] = True
        _apply_adapter_config(config, project, role_id, target_adapter, command, args)
        if entry_text is not None:
            context["entry_written"] = True
            _sync_project_entry(config, project, role_id, target_adapter)
        context["hub_touched"] = True
        if was_running:
            hub.restart()
        else:
            hub.start()
        context["takeover_called"] = True
        result = client.call("session_takeover", agent=internal, project=project,
                             session_id=session, transport=transport)
        postconditions = verify_takeover(client, config, project, internal, session,
                                         transport, target_adapter)
        if not postconditions["ok"]:
            raise ValueError("takeover_postconditions_failed: " + compact(postconditions["checks"]))
        result.update({
            "adapter": target_adapter, "previous_adapter": current_adapter,
            "adapter_changed": True, "hub_restarted": was_running,
            "hub_started": not was_running,
            "active_session": postconditions["active_session"] or session,
            "retired_sessions": postconditions["retired_sessions"],
            "postconditions": postconditions,
        })
        warning = _activate_target_client(state, config, target_adapter)
        if warning:
            result["warning"] = warning
        return result
    except Exception as exc:
        errors = _rollback_migration(state, config, context)
        if errors:
            raise RuntimeError("migration_failed_rollback_incomplete: " + str(exc)
                               + "; " + "; ".join(errors)) from None
        raise RuntimeError("migration_failed_rolled_back: " + str(exc)) from None


def takeover_role(state, config, project, role, session=None, transport=None,
                  adapter="auto", migrate=False, command=None, args=None,
                  client=None, hub=None):
    """Old `session takeover`: session-only unless migration is requested."""
    state = Path(state).resolve()
    config = Path(config).resolve()
    client = client or Client(state)
    hub = hub or HubControl(state, config)
    data = read_config(config)
    internal = find_role(data, role, project)
    endpoint = data["endpoints"][internal]
    project = endpoint["project"]
    current_transport, current_session = _current_client()
    if not transport:
        if current_transport and (not session or session == current_session):
            transport = current_transport
            if not session:
                session = current_session
        else:
            transport = default_transport(endpoint["adapter"])
    if not session and current_session:
        session = current_session
    if not transport:
        raise ValueError("transport_required: pass --transport (codex_app_tools, qoder_native, opencode_native, dsh_plugin)")
    derived = adapter_for_transport(transport)
    if derived is None:
        raise ValueError("unsupported_desktop_transport: " + str(transport))
    if current_transport and session and session == current_session and transport != current_transport:
        raise ValueError("adapter_transport_mismatch: the current client uses transport="
                         + current_transport + ", not " + str(transport))
    if migrate or adapter not in (None, "auto"):
        return migrate_and_takeover(state, config, project, role, session=session,
                                    transport=transport,
                                    adapter=adapter if adapter not in (None, "auto") else derived,
                                    command=command, args=args, client=client, hub=hub)
    if endpoint["adapter"] != "fixture" and derived != endpoint["adapter"]:
        raise ValueError(
            "adapter_transport_mismatch: role adapter=" + endpoint["adapter"]
            + " cannot be taken over with transport=" + str(transport)
            + "; use `session migrate-and-takeover --role " + endpoint["role"]
            + " --adapter auto --transport " + str(transport)
            + "` so the adapter moves with the client")
    session = _resolve_target_session(state, endpoint["adapter"], session, transport)
    _check_codex_origin(state, endpoint["adapter"], session)
    result = client.call("session_takeover", agent=internal, project=project,
                         session_id=session, transport=transport)
    postconditions = verify_takeover(client, config, project, internal, session,
                                     transport, endpoint["adapter"], check_entry=False)
    if not postconditions["ok"]:
        raise ValueError("takeover_postconditions_failed: " + compact(postconditions["checks"]))
    result.update({"adapter": endpoint["adapter"], "previous_adapter": endpoint["adapter"],
                   "adapter_changed": False, "hub_restarted": False, "hub_started": False,
                   "active_session": postconditions["active_session"] or session,
                   "retired_sessions": postconditions["retired_sessions"],
                   "postconditions": postconditions})
    warning = _activate_target_client(state, config, endpoint["adapter"])
    if warning:
        result["warning"] = warning
    return result


def doctor(config):
    data = read_config(config)
    results = []
    for name, endpoint in data["endpoints"].items():
        entry = {"name": name, "address": name, "project": endpoint.get("project"),
                 "role": endpoint.get("role"), "adapter": endpoint["adapter"], "cwd": endpoint["cwd"],
                 "mode": endpoint.get("mode", "independent_cli_task"), "desktop_wakeup": False}
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


def join_role(state, config, project, role, session=None, transport=None,
              migrate_adapter=False, adapter="auto", command=None, args=None,
              client=None, hub=None):
    """Bind the current desktop chat to a role.

    Codex exposes its current thread through CODEX_THREAD_ID.  Qoder's current
    selected chat is read through the existing accessibility helper, which is a
    read-only operation even while composer automation is disabled.

    A join that changes the client type is a takeover, not a bind: without
    ``--migrate-adapter`` it is refused instead of producing a role whose
    adapter and transport disagree.
    """
    state = Path(state).resolve()
    config = Path(config).resolve()
    data = read_config(config)
    internal = find_role(data, role, project)
    endpoint = data["endpoints"][internal]
    project = endpoint["project"]
    role_adapter = endpoint["adapter"]
    current_transport, current_session = _current_client()
    if not transport:
        if current_transport and (not session or session == current_session):
            transport = current_transport
            if not session:
                session = current_session
        else:
            transport = default_transport(role_adapter)
    if not session and current_session:
        session = current_session
    if not transport:
        if role_adapter == "dsh":
            raise ValueError(
                "dsh roles are served by the DSH host plugin and bind themselves. "
                "To move a role into this DSH chat, run session migrate-and-takeover "
                "with --transport dsh_plugin --session <this chat id>")
        raise ValueError("cannot_detect_desktop_chat: run this from the target chat, or pass --session")
    derived = adapter_for_transport(transport)
    if derived is None:
        raise ValueError("unsupported_desktop_transport: " + str(transport))
    if role_adapter != "fixture" and derived != role_adapter:
        if migrate_adapter or adapter not in (None, "auto"):
            result = migrate_and_takeover(state, config, project, role, session=session,
                                          transport=transport,
                                          adapter=adapter if adapter not in (None, "auto") else derived,
                                          command=command, args=args, client=client, hub=hub)
            result["joined"] = True
            result["note"] = ("The role moved to this chat and its adapter changed to "
                              + result["adapter"] + "; old sessions are retired.")
            return result
        raise ValueError(
            "adapter_transport_mismatch: role adapter=" + role_adapter
            + " cannot be joined with transport=" + str(transport)
            + "; rerun with --migrate-adapter (or use session migrate-and-takeover)"
            + " so the adapter moves with the client")
    session = _resolve_target_session(state, role_adapter, session, transport)
    _check_codex_origin(state, role_adapter, session)
    client = client or Client(state)
    result = client.call("desktop_bind", agent=internal, project=project,
                         session_id=session, transport=transport)
    postconditions = verify_takeover(client, config, project, internal, session,
                                     transport, role_adapter, strict=False, check_entry=False)
    if not postconditions["ok"]:
        raise ValueError("join_postconditions_failed: " + compact(postconditions["checks"]))
    warning = None
    if role_adapter == "codex":
        capture_current_connection(state)
        if not select_connection(state, session):
            warning = "codex_connector_context_missing: register another live Codex desktop chat"
        else:
            start_desktop_connectors(state, config)
    return {"joined": True, "address": internal, "session_id": session,
            "transport": transport, "adapter": role_adapter,
            "adapter_changed": False, "hub_restarted": False,
            "active_session": postconditions["active_session"] or session,
            "retired_sessions": postconditions["retired_sessions"],
            "mode": result.get("mode"), "warning": warning,
            "postconditions": postconditions,
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


def _prompt_from_args(args):
    """Read task text without trusting the lossy Windows command line.

    UTF-8 files/stdin are the reliable path for CJK on Windows.  If an argument
    already contains the replacement character, storing it would silently
    persist mojibake, so refuse with a concrete alternative.
    """
    if getattr(args, "text_stdin", False):
        raw = sys.stdin.buffer.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("stdin_must_be_utf8: pipe UTF-8 text into --text-stdin") from None
    elif getattr(args, "file", None):
        text = Path(args.file).read_text(encoding="utf-8-sig")
    else:
        text = args.text
    if not isinstance(text, str):
        raise ValueError("text_required")
    if "\ufffd" in text:
        raise ValueError("text_encoding_loss: the argument already contains U+FFFD; "
                         "pass UTF-8 via --file or --text-stdin instead")
    return text


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
    group.add_argument("--text-stdin", action="store_true",
                       help="Read the task text as UTF-8 from stdin (the safe path for CJK on Windows)")
    s.add_argument("--key", required=True, help="Reuse this key if the response is lost; never reuse for different content")
    s.add_argument("--timeout", type=int, default=180, help="Target execution timeout in seconds")
    s.add_argument("--delivery-timeout", type=int, default=None,
                   help="How long the sender waits for the chat to accept the prompt (default: min(30, timeout))")
    s.add_argument("--interrupt-on-timeout", action="store_true",
                   help="Explicitly allow a timeout to cancel the target chat (off by default)")
    s.add_argument("--queue-ttl", type=int, default=3600)
    s.add_argument("--context", action="append", default=[])
    s.add_argument("--reply-mode", choices=("mailbox", "run"), default="mailbox")
    for name in ("notify", "wakeup"):
        n = sub.add_parser(name, help="Deliver a notification/wakeup; never waits for or cancels execution")
        _project_option(n)
        n.add_argument("--from", dest="sender", required=True)
        n.add_argument("--to", dest="target", required=True)
        n_group = n.add_mutually_exclusive_group(required=True)
        n_group.add_argument("--text")
        n_group.add_argument("--file")
        n_group.add_argument("--text-stdin", action="store_true",
                             help="Read the notification as UTF-8 from stdin")
        n.add_argument("--key", help="Optional idempotency key (generated when omitted)")
        n.add_argument("--timeout", type=int, default=30,
                       help="How long the sender waits for delivery (not target execution)")
        n.add_argument("--queue-ttl", type=int, default=3600)
        n.add_argument("--context", action="append", default=[])
    for name in ("task", "cancel", "wait"):
        s = sub.add_parser(name)
        s.add_argument("task_id")
        if name == "wait":
            s.add_argument("--timeout", type=int, default=1800)
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
    s.add_argument("--transport", choices=("mcp", "workbuddy_mcp", "opencode_native"), default="mcp",
                   help="Session transport injected when bridge_register is called")
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
    s.add_argument("--transport", choices=tuple(sorted(TRANSPORT_ADAPTERS)))
    s.add_argument("--migrate-adapter", action="store_true",
                   help="Allow the role's adapter to move to this client (full takeover)")
    s.add_argument("--adapter", choices=("auto",) + ADAPTERS, default="auto")
    s.add_argument("--exe", help="Runtime executable for the migrated adapter (default: auto-discover)")
    s.add_argument("--arg", action="append", default=[], help="Runtime argument for the migrated adapter")
    s = sub.add_parser("sessions")
    _project_option(s)
    s.add_argument("--role")
    s.add_argument("--online", action="store_true")
    # session lifecycle: take a role over, or retire a chat for good
    session = sub.add_parser("session")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    takeover = session_sub.add_parser(
        "takeover", help="Switch a role to another chat without changing its adapter")
    _project_option(takeover)
    takeover.add_argument("--role", required=True)
    takeover.add_argument("--session")
    takeover.add_argument("--transport", choices=tuple(sorted(TRANSPORT_ADAPTERS)))
    takeover.add_argument("--adapter", choices=("auto",) + ADAPTERS, default="auto",
                          help="auto keeps the role adapter; a concrete adapter migrates")
    takeover.add_argument("--migrate-adapter", action="store_true",
                          help="Atomically move the role's adapter to this client")
    takeover.add_argument("--exe")
    takeover.add_argument("--arg", action="append", default=[])
    migrate = session_sub.add_parser(
        "migrate-and-takeover",
        help="Atomically move a role to this client: adapter, hub, binding, retirement")
    _project_option(migrate)
    migrate.add_argument("--role", required=True)
    migrate.add_argument("--session")
    migrate.add_argument("--transport", choices=tuple(sorted(TRANSPORT_ADAPTERS)))
    migrate.add_argument("--adapter", choices=("auto",) + ADAPTERS, default="auto")
    migrate.add_argument("--exe")
    migrate.add_argument("--arg", action="append", default=[])
    retire = session_sub.add_parser("retire", help="End a session: no more delivery, no more claiming")
    _project_option(retire)
    retire.add_argument("--session", required=True)
    retire.add_argument("--reason", default="retired by user")

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
    add.add_argument("--adapter", default="codex", choices=ADAPTERS)
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
    add_bus_commands(sub)
    s = sub.add_parser("setup")
    _project_option(s)
    s.add_argument("--root", required=True, help="Project root folder")
    s.add_argument("--name")
    s.add_argument("--description", default="")
    s.add_argument("--role", action="append", default=[], help="role[:adapter[:description]], repeatable")
    s.add_argument("--adapter", default="codex", choices=ADAPTERS)
    s.add_argument("--force", action="store_true", help="Replace existing role runtime settings")
    s.add_argument("--no-agents", action="store_true")
    s.add_argument("--no-agent-chat", action="store_true",
                   help="Do not initialize the bundled agent-chat file bus")
    s.add_argument("--no-restart", action="store_true")
    workbuddy = sub.add_parser("workbuddy", help="Connect WorkBuddy to Agents Chat over local MCP")
    workbuddy_sub = workbuddy.add_subparsers(dest="workbuddy_command", required=True)
    workbuddy_setup = workbuddy_sub.add_parser("setup")
    _project_option(workbuddy_setup)
    workbuddy_setup.add_argument("--role", default="workbuddy")
    workbuddy_setup.add_argument("--description", default="WorkBuddy collaboration role")
    workbuddy_setup.add_argument("--mcp-config", help="WorkBuddy mcp.json (default: ~/.workbuddy/mcp.json)")
    workbuddy_setup.add_argument("--server-name")
    workbuddy_setup.add_argument("--no-restart", action="store_true")
    workbuddy_status_parser = workbuddy_sub.add_parser("status")
    workbuddy_status_parser.add_argument("--mcp-config")
    opencode = sub.add_parser("opencode", help="Connect OpenCode's current native session to Agents Chat")
    opencode_sub = opencode.add_subparsers(dest="opencode_command", required=True)
    opencode_setup = opencode_sub.add_parser("setup")
    _project_option(opencode_setup)
    opencode_setup.add_argument("--role", default="opencode")
    opencode_setup.add_argument("--description", default="OpenCode collaboration role")
    opencode_setup.add_argument("--server-name")
    opencode_setup.add_argument("--no-restart", action="store_true")
    opencode_status_parser = opencode_sub.add_parser("status")
    opencode_status_parser.add_argument("--root")
    return p


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
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
            serve_mcp(client, label(args.agent), project, args.transport)
            return
        elif args.command == "send":
            prompt = _prompt_from_args(args)
            delivery_timeout = (args.delivery_timeout if args.delivery_timeout is not None
                                else min(30, args.timeout))
            result = client.call("send", sender=args.sender, target=args.target, prompt=prompt, key=args.key,
                                 timeout=args.timeout, delivery_timeout=delivery_timeout,
                                 interrupt_on_timeout=args.interrupt_on_timeout,
                                 context=args.context, reply_mode=args.reply_mode,
                                 queue_ttl=args.queue_ttl, **({"project": project} if project else {}))
        elif args.command in ("notify", "wakeup"):
            prompt = _prompt_from_args(args)
            key = args.key or "notify:" + uuid.uuid4().hex
            result = client.call("send", sender=args.sender, target=args.target, prompt=prompt, key=key,
                                 timeout=1800, delivery_timeout=args.timeout,
                                 interrupt_on_timeout=False, kind="agent_chat_wakeup",
                                 context=args.context, reply_mode="mailbox",
                                 queue_ttl=args.queue_ttl,
                                 **({"project": project} if project else {}))
            result.update({"notification": True, "delivery_timeout": args.timeout})
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
            result = join_role(state, config, project, args.role, args.session, args.transport,
                               migrate_adapter=args.migrate_adapter, adapter=args.adapter,
                               command=args.exe, args=args.arg)
        elif args.command == "sessions":
            result = client.call("session_list", project=project, role=args.role, include_offline=not args.online)
        elif args.command == "session":
            if args.session_command in ("takeover", "migrate-and-takeover"):
                result = takeover_role(state, config, project, args.role, args.session,
                                       args.transport, adapter=args.adapter,
                                       migrate=(args.session_command == "migrate-and-takeover"
                                                or args.migrate_adapter),
                                       command=args.exe, args=args.arg)
            else:
                result = client.call("session_retire", session_id=args.session, reason=args.reason)
        elif args.command == "project":
            result = project_command(args, config, state)
        elif args.command == "role":
            result = role_command(args, config, state, project)
        elif args.command == "bus":
            result = run_bus(args, bus_root(config, project))
        elif args.command == "setup":
            roles = _role_specs(args.role, args.adapter)
            result = setup_project(config, args.root, project=getattr(args, "project_local", None) or args.project,
                                   name=args.name,
                                   description=args.description, roles=roles, state=state,
                                   write_agents=not args.no_agents, force=args.force,
                                   with_agent_chat=not args.no_agent_chat)
            result.update(live_restart_if_running(state, config, not args.no_restart))
        elif args.command == "workbuddy":
            result = workbuddy_command(args, config, state, project)
        elif args.command == "opencode":
            result = opencode_command(args, config, state, project)
        else:
            raise ValueError("unknown_command")
        print(compact(result))
    except (RuntimeError, ValueError, OSError, KeyError) as exc:
        print(compact({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)


def workbuddy_command(args, config, state, project):
    from .workbuddy import install_workbuddy_mcp, workbuddy_status
    if args.workbuddy_command == "status":
        result = workbuddy_status(args.mcp_config)
        normalized = read_config(config)
        result["roles"] = [address for address, endpoint in normalized["endpoints"].items()
                           if endpoint["adapter"] in ("workbuddy", "codebuddy")]
        return result
    normalized = read_config(config)
    if not project:
        if "default" in normalized["projects"]:
            project = "default"
        elif len(normalized["projects"]) == 1:
            project = next(iter(normalized["projects"]))
        else:
            raise ValueError("project_required: pass --project for the WorkBuddy role")
    project_entry = normalized["projects"].get(project)
    if not project_entry:
        raise ValueError("unknown_project: " + project)
    address = project_entry["roles"].get(args.role)
    role_created = False
    if address:
        adapter = normalized["endpoints"][address]["adapter"]
        if adapter not in ("workbuddy", "codebuddy"):
            raise ValueError(f"role_adapter_conflict: {address} uses {adapter}")
    else:
        added = add_project_role(config, project, args.role, "workbuddy",
                                 description=args.description)
        address = added["address"]
        role_created = True
    installed = install_workbuddy_mcp(cli_command(), state, config, project, args.role,
                                      path=args.mcp_config, server_name=args.server_name)
    result = {"project": project, "role": args.role, "address": address,
              "role_created": role_created, "mcp": installed}
    if role_created:
        result.update(live_restart_if_running(state, config, not args.no_restart))
    else:
        result["hub"] = "unchanged"
    return result


def opencode_command(args, config, state, project):
    from .opencode import install_opencode_mcp, opencode_status
    if args.opencode_command == "status":
        result = opencode_status(args.root)
        normalized = read_config(config)
        result["roles"] = [address for address, endpoint in normalized["endpoints"].items()
                           if endpoint["adapter"] == "opencode"]
        return result
    normalized = read_config(config)
    if not project:
        if "default" in normalized["projects"]:
            project = "default"
        elif len(normalized["projects"]) == 1:
            project = next(iter(normalized["projects"]))
        else:
            raise ValueError("project_required: pass --project for the OpenCode role")
    project_entry = normalized["projects"].get(project)
    if not project_entry:
        raise ValueError("unknown_project: " + project)
    address = project_entry["roles"].get(args.role)
    role_created = False
    if address:
        adapter = normalized["endpoints"][address]["adapter"]
        if adapter != "opencode":
            raise ValueError(f"role_adapter_conflict: {address} uses {adapter}")
    else:
        added = add_project_role(config, project, args.role, "opencode",
                                 description=args.description)
        address = added["address"]
        role_created = True
    installed = install_opencode_mcp(
        cli_command(), state, config, project, args.role, project_entry["root"],
        server_name=args.server_name)
    result = {"project": project, "role": args.role, "address": address,
              "role_created": role_created, "mode": "desktop_session",
              "mcp": installed}
    if role_created:
        result.update(live_restart_if_running(state, config, not args.no_restart))
    else:
        result["hub"] = "unchanged"
    return result


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



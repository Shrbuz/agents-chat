"""Project/role configuration for the multi-project Agents Chat hub.

The on-disk format is version 2::

    {
      "version": 2,
      "projects": {
        "alpha": {
          "name": "Alpha",
          "root": "E:/work/alpha",
          "roles": {
            "backend": {"adapter": "codex", "command": ["codex"], "cwd": "."}
          }
        }
      }
    }

A role is addressed as ``<project>.<role>``.  The single-project 0.4 format
(``{"endpoints": {...}}``) is still accepted and keeps its plain role names so
existing queues, desktop bindings and MCP arguments remain valid.
"""
import json
import os
import re
from pathlib import Path

ENDPOINT_NAME = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")
PROJECT_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")
ROLE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ADAPTERS = ("codex", "qoder", "codebuddy", "fixture")


class ConfigError(ValueError):
    """Configuration is incomplete or contradictory."""


def label(value):
    if not isinstance(value, str) or not ENDPOINT_NAME.fullmatch(value):
        raise ConfigError("invalid_agent_or_consumer_name")
    return value


def project_id(value):
    if not isinstance(value, str) or not PROJECT_ID.fullmatch(value):
        raise ConfigError("invalid_project_id: use 1-32 letters, digits, '_' or '-'")
    return value


def role_id(value):
    if not isinstance(value, str) or not ROLE_ID.fullmatch(value):
        raise ConfigError("invalid_role_id: use 1-48 letters, digits, '_' or '-'")
    return value


def session_id(value):
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        raise ConfigError("invalid_session_id")
    return value


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{name}_must_be_integer_{low}_to_{high}")
    return value


def _resolve_root(value, base):
    path = Path(value)
    if not path.is_absolute():
        path = Path(base) / path
    return path.resolve()


def _normalize_agent_chat(value, root):
    if not value:
        return None
    if isinstance(value, str):
        value = {"bus": value}
    if not isinstance(value, dict):
        raise ConfigError("agent_chat_must_be_path_or_object")
    bus = value.get("bus")
    if not bus:
        return None
    bus_path = _resolve_root(bus, root)
    if bus_path.is_file() and bus_path.name == "project.json":
        bus_path = bus_path.parent
    missing = not bus_path.is_dir()
    roles = value.get("roles", {})
    if not isinstance(roles, dict):
        raise ConfigError("agent_chat_roles_must_be_object")
    mapping = {}
    for bus_role, local_role in roles.items():
        role_id(bus_role)
        role_id(local_role)
        mapping[bus_role] = local_role
    helper = value.get("helper")
    if helper:
        helper = str(_resolve_root(helper, root))
    return {"bus": str(bus_path), "roles": mapping, "helper": helper,
            "missing": missing,
            "enabled": value.get("enabled", True) is not False and not missing}


def _normalize_endpoint(name, data, project, role, base, root, address):
    if not isinstance(data, dict):
        raise ConfigError("endpoint_must_be_object: " + name)
    endpoint = dict(data)
    adapter = endpoint.get("adapter")
    if adapter not in ADAPTERS:
        raise ConfigError("unknown_adapter: " + name)
    if endpoint.get("mode", "independent_cli_task") != "independent_cli_task":
        raise ConfigError("desktop_chat_wakeup_not_supported")
    command = endpoint.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(s, str) and s for s in command):
        raise ConfigError("command_must_be_argv_array: " + name)
    if Path(command[0]).suffix.lower() in (".cmd", ".bat", ".ps1"):
        raise ConfigError("use_native_executable_or_node_plus_script_instead_of_shell_wrapper")
    cwd = _resolve_root(endpoint.get("cwd", root), root)
    endpoint["cwd"] = str(cwd)
    if not cwd.is_dir():
        raise ConfigError("workspace_missing: " + str(cwd))
    if not isinstance(endpoint.get("args", []), list):
        raise ConfigError("args_must_be_strings: " + name)
    if not all(isinstance(a, str) for a in endpoint.get("args", [])):
        raise ConfigError("args_must_be_strings: " + name)
    endpoint.setdefault("args", [])
    if endpoint.get("sandbox", "read-only") not in ("read-only", "workspace-write"):
        raise ConfigError("unsupported_sandbox: " + name)
    _integer(endpoint.get("max_output_bytes", 4194304), 1024, 16777216, "max_output_bytes")
    _integer(endpoint.get("skill_catalog_tokens", 512), 128, 10000, "skill_catalog_tokens")
    _integer(endpoint.get("tool_output_tokens", 1200), 128, 10000, "tool_output_tokens")
    if type(endpoint.get("desktop_input_enabled", True)) is not bool:
        raise ConfigError("desktop_input_enabled_must_be_boolean: " + name)
    if type(endpoint.get("enabled", True)) is not bool:
        raise ConfigError("enabled_must_be_boolean: " + name)
    description = endpoint.get("description", "")
    if not isinstance(description, str) or len(description) > 500:
        raise ConfigError("description_must_be_up_to_500_characters: " + name)
    peers = endpoint.get("peers", [])
    if not isinstance(peers, list) or len(peers) > 64 or not all(isinstance(p, str) and p for p in peers):
        raise ConfigError("peers_must_be_a_role_list: " + name)
    endpoint["project"] = project
    endpoint["role"] = role
    endpoint["address"] = address
    endpoint.setdefault("name", role)
    endpoint["args"] = list(endpoint["args"])
    endpoint["command"] = list(command)
    return endpoint


def normalize_config(raw, path):
    """Return a validated configuration with a flat ``endpoints`` index.

    ``endpoints`` is keyed by the internal address used by the queue, desktop
    bindings and inboxes.  Legacy single-project roles keep their plain names;
    version 2 roles use ``project.role``.
    """
    if not isinstance(raw, dict):
        raise ConfigError("config_must_be_object")
    path = Path(path).resolve()
    base = path.parent
    if "projects" not in raw and "endpoints" in raw:
        return _normalize_legacy(raw, path, base)
    projects_raw = raw.get("projects")
    if not isinstance(projects_raw, dict):
        raise ConfigError("projects_required")
    legacy_project = raw.get("legacy_project")
    if legacy_project is not None:
        project_id(legacy_project)
    config = {"version": 2, "projects": {}, "endpoints": {}, "config_path": str(path)}
    for key, value in raw.items():
        if key not in ("projects", "endpoints", "version"):
            config[key] = value
    for pid, project in projects_raw.items():
        project_id(pid)
        if not isinstance(project, dict):
            raise ConfigError("project_must_be_object: " + pid)
        root = _resolve_root(project.get("root", base), base)
        roles = project.get("roles", {})
        if not isinstance(roles, dict):
            raise ConfigError("roles_must_be_object: " + pid)
        plain = legacy_project == pid
        entry = {"id": pid, "name": project.get("name") or pid, "root": str(root),
                 "description": project.get("description") or "", "bus": project.get("bus"),
                 "plain_addresses": plain, "roles": {},
                 "agent_chat": _normalize_agent_chat(project.get("agent_chat"), root)}
        if not isinstance(entry["name"], str) or not entry["name"].strip():
            raise ConfigError("project_name_required: " + pid)
        if not isinstance(entry["description"], str) or len(entry["description"]) > 1000:
            raise ConfigError("project_description_too_long: " + pid)
        if entry["bus"] is not None:
            entry["bus"] = str(_resolve_root(entry["bus"], root))
        config["projects"][pid] = entry
        for rid, endpoint_data in roles.items():
            role_id(rid)
            address = rid if plain else f"{pid}.{rid}"
            endpoint = _normalize_endpoint(address, endpoint_data, pid, rid, base, root, address)
            if address in config["endpoints"]:
                raise ConfigError("duplicate_role_address: " + address)
            config["endpoints"][address] = endpoint
            entry["roles"][rid] = address
    # Resolve peer lists after every role exists, so projects can reference each other.
    for endpoint in config["endpoints"].values():
        peers = endpoint.get("peers") or []
        endpoint["peers"] = [find_role(config, peer, endpoint["project"]) for peer in peers]
    return config


def _normalize_legacy(raw, path, base):
    endpoints = raw.get("endpoints")
    if not isinstance(endpoints, dict) or not endpoints:
        raise ConfigError("endpoints_required")
    pid = raw.get("project_id", "default")
    project_id(pid)
    config = {"version": 2, "legacy_project": pid, "config_path": str(path),
              "projects": {pid: {"id": pid, "name": raw.get("project_name", "默认项目"),
                                 "root": str(base), "description": "", "bus": None,
                                 "plain_addresses": True, "roles": {}, "agent_chat": None}},
              "endpoints": {}}
    for key, value in raw.items():
        if key not in ("endpoints", "project_id", "project_name", "version", "projects"):
            config[key] = value
    project = config["projects"][pid]
    for name, endpoint_data in endpoints.items():
        label(name)
        endpoint = _normalize_endpoint(name, endpoint_data, pid, name, base, base, name)
        config["endpoints"][name] = endpoint
        project["roles"][name] = name
    for endpoint in config["endpoints"].values():
        endpoint["peers"] = [find_role(config, peer, endpoint["project"]) for peer in endpoint.get("peers") or []]
    return config


def read_config(path):
    path = Path(path).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ConfigError("config_not_found: " + str(path)) from None
    except ValueError as exc:
        raise ConfigError("invalid_config_json: " + str(exc)[:200]) from None
    return normalize_config(raw, path)


def find_role(config, address, project=None):
    """Resolve a user facing address to the internal endpoint key."""
    if not isinstance(address, str) or not address:
        raise ConfigError("role_address_required")
    endpoints = config["endpoints"]
    if address in endpoints:
        return address
    if "." in address:
        prefix, rest = address.split(".", 1)
        matches = [key for key, endpoint in endpoints.items()
                   if endpoint.get("project") == prefix and endpoint.get("role") == rest]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ConfigError("ambiguous_role: " + address)
    if project:
        matches = [key for key, endpoint in endpoints.items()
                   if endpoint.get("project") == project and endpoint.get("role") == address]
        if len(matches) == 1:
            return matches[0]
    matches = [key for key, endpoint in endpoints.items() if endpoint.get("role") == address]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ConfigError("ambiguous_role: qualify with project.role")
    raise ConfigError("unknown_role: " + address)


def endpoint(config, address, project=None):
    return config["endpoints"][find_role(config, address, project)]


def address_of(config, internal):
    return internal


def project_of(config, internal):
    return config["endpoints"][internal].get("project")


def role_summary(config, internal):
    endpoint_data = config["endpoints"][internal]
    return {"address": internal, "project": endpoint_data.get("project"),
            "role": endpoint_data.get("role"), "name": endpoint_data.get("name", internal),
            "adapter": endpoint_data.get("adapter"), "description": endpoint_data.get("description", ""),
            "enabled": endpoint_data.get("enabled", True),
            "peers": [config["endpoints"][p]["address"] if p in config["endpoints"] else p
                      for p in endpoint_data.get("peers", [])]}


def projects_summary(config):
    result = []
    for pid, project in config["projects"].items():
        roles = [role_summary(config, address) for address in project["roles"].values()]
        result.append({"id": pid, "name": project["name"], "root": project["root"],
                       "description": project.get("description", ""), "bus": project.get("bus"),
                       "agent_chat": project.get("agent_chat"), "roles": roles})
    return result


def allowed_target(config, sender_address, target_address):
    """An optional per-role allow-list keeps autonomous dispatch bounded."""
    sender = config["endpoints"].get(sender_address)
    if not sender:
        return True
    peers = sender.get("peers") or []
    return not peers or target_address in peers


# ---------------------------------------------------------------------------
# Editing helpers.  They deliberately preserve the version 2 shape and convert
# the legacy single-project file on the first write so existing roles keep
# their plain addresses.


def load_raw(path):
    path = Path(path)
    if not path.exists():
        return {"version": 2, "projects": {}}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def convert_raw(raw, path):
    if "projects" in raw:
        return raw
    if "endpoints" not in raw:
        raise ConfigError("endpoints_or_projects_required")
    path = Path(path).resolve()
    pid = raw.get("project_id", "default")
    project_id(pid)
    projects = {pid: {"name": raw.get("project_name", "默认项目"), "root": str(path.parent),
                      "roles": raw.get("endpoints", {})}}
    result = {"version": 2, "legacy_project": pid, "projects": projects}
    for key, value in raw.items():
        if key not in ("endpoints", "project_id", "project_name", "version", "projects"):
            result[key] = value
    return result


def save_raw(path, raw):
    path = Path(path).resolve()
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def upsert_project(path, pid, name=None, root=None, description=None, bus=None):
    project_id(pid)
    raw = convert_raw(load_raw(path), path)
    projects = raw.setdefault("projects", {})
    current = projects.get(pid)
    if current is None:
        if root is None:
            raise ConfigError("project_root_required")
        current = {"name": name or pid, "root": str(_resolve_root(root, Path(path).resolve().parent)), "roles": {}}
        projects[pid] = current
    if name is not None:
        current["name"] = name
    if root is not None:
        current["root"] = str(_resolve_root(root, Path(path).resolve().parent))
    if description is not None:
        current["description"] = description
    if bus is not None:
        current["bus"] = str(_resolve_root(bus, current["root"]))
    save_raw(path, raw)
    return current


def delete_project(path, pid):
    raw = convert_raw(load_raw(path), path)
    projects = raw.setdefault("projects", {})
    if pid not in projects:
        raise ConfigError("unknown_project: " + pid)
    del projects[pid]
    save_raw(path, raw)


def upsert_role(path, pid, rid, endpoint_data):
    role_id(rid)
    raw = convert_raw(load_raw(path), path)
    projects = raw.setdefault("projects", {})
    project = projects.get(pid)
    if project is None:
        raise ConfigError("unknown_project: " + pid)
    roles = project.setdefault("roles", {})
    previous = roles.get(rid)
    roles[rid] = endpoint_data
    try:
        # Validate the whole file immediately so a bad role never reaches disk.
        normalize_config(raw, path)
    except Exception:
        if previous is None:
            roles.pop(rid, None)
        else:
            roles[rid] = previous
        raise
    save_raw(path, raw)
    return normalize_config(raw, path)


def delete_role(path, pid, rid):
    raw = convert_raw(load_raw(path), path)
    project = raw.setdefault("projects", {}).get(pid)
    if project is None:
        raise ConfigError("unknown_project: " + pid)
    roles = project.setdefault("roles", {})
    if rid not in roles:
        raise ConfigError("unknown_role: " + rid)
    previous = roles.pop(rid)
    try:
        normalize_config(raw, path)
    except Exception:
        roles[rid] = previous
        raise
    save_raw(path, raw)


def address_for_raw(raw, pid, rid):
    legacy = raw.get("legacy_project") == pid
    return rid if legacy else f"{pid}.{rid}"

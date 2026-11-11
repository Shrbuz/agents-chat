"""Ephemeral Codex desktop connection discovery and local context registry.

The app-tools pipe and bundled Node runtime belong to a running Codex app
instance.  They are deliberately treated as short-lived observations, not as
installation configuration.  Every Codex-side register/join/heartbeat can add
the context it currently sees; connectors always select the newest app
generation and never fall back to an older pipe after a restart.
"""
import hashlib
import json
import os
from pathlib import Path
import time


REGISTRY_NAME = "codex-connections.local.json"
LEGACY_NAME = "codex-desktop.local.json"


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def _connection_id(pipe, command):
    material = "\0".join([str(pipe), *(str(item) for item in command)])
    return hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()[:24]


def _plugin_server(home=None):
    home = Path(home) if home else Path.home()
    plugins = home / ".codex/plugins/cache/openai-bundled/codex-app-tools"
    candidates = sorted(plugins.glob("*/server.mjs"),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("codex_app_tools_server_missing")
    return candidates[0]


def normalize_connection(value, *, require_files=True):
    if not isinstance(value, dict):
        raise ValueError("invalid_codex_connection_context")
    command = value.get("command")
    pipe = value.get("pipe")
    origin = value.get("origin_thread")
    if (not isinstance(command, list) or len(command) < 2
            or not all(isinstance(item, str) and item for item in command)
            or not isinstance(pipe, str) or not pipe
            or not isinstance(origin, str) or not origin):
        raise ValueError("invalid_codex_connection_context")
    if require_files:
        if not Path(command[0]).is_file():
            raise FileNotFoundError("codex_runtime_missing: " + command[0])
        if not Path(command[1]).is_file():
            raise FileNotFoundError("codex_app_tools_server_missing: " + command[1])
    result = {
        "command": list(command),
        "pipe": pipe,
        "origin_thread": origin,
        "connection_id": value.get("connection_id") or _connection_id(pipe, command),
        "updated_at": float(value.get("updated_at") or time.time()),
    }
    return result


def discover_connection(environment=None, home=None):
    environment = os.environ if environment is None else environment
    pipe = environment.get("CODEX_APP_TOOLS_PIPE_PATH")
    origin = environment.get("CODEX_THREAD_ID")
    node = environment.get("CODEX_MCP_NODE_PATH")
    if not pipe or not origin or not node:
        raise RuntimeError("codex_connection_context_unavailable")
    return normalize_connection({
        "command": [node, str(_plugin_server(home))],
        "pipe": pipe,
        "origin_thread": origin,
        "updated_at": time.time(),
    })


def connection_from_environment(environment=None, home=None):
    """Return the live host context, or None outside a Codex desktop process."""
    try:
        return discover_connection(environment, home)
    except (OSError, RuntimeError, ValueError):
        return None


def load_registry(state):
    state = Path(state)
    path = state / REGISTRY_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {"version": 1, "active_connection_id": None, "connections": {}}
    if not isinstance(value, dict):
        value = {"version": 1, "active_connection_id": None, "connections": {}}
    connections = value.get("connections")
    if not isinstance(connections, dict):
        connections = {}
    value = {"version": 1, "active_connection_id": value.get("active_connection_id"),
             "connections": connections}

    # Read the old single-context file as a migration source.  It remains
    # writable below for compatibility with older installed launchers.
    legacy = state / LEGACY_NAME
    if not connections and legacy.is_file():
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8"))
            raw.setdefault("updated_at", legacy.stat().st_mtime)
            item = normalize_connection(raw, require_files=False)
            value["connections"][item["origin_thread"]] = item
            value["active_connection_id"] = item["connection_id"]
        except (OSError, ValueError):
            pass
    return value


def update_connection(state, connection):
    """Record one observed origin and return whether the app generation changed."""
    state = Path(state)
    item = normalize_connection(connection)
    registry = load_registry(state)
    previous_id = registry.get("active_connection_id")
    registry["active_connection_id"] = item["connection_id"]
    registry["connections"][item["origin_thread"]] = item
    # Bound growth: contexts from old chats are only useful within the newest
    # few app observations, and each one contains a sensitive local pipe name.
    ordered = sorted(registry["connections"].items(),
                     key=lambda pair: float((pair[1] or {}).get("updated_at") or 0),
                     reverse=True)[:16]
    registry["connections"] = dict(ordered)
    _atomic_json(state / REGISTRY_NAME, registry)
    _atomic_json(state / LEGACY_NAME, item)
    return {"connection": item, "generation_changed": previous_id != item["connection_id"],
            "previous_connection_id": previous_id}


def capture_current_connection(state, *, required=False, environment=None, home=None):
    connection = connection_from_environment(environment, home)
    if connection is None:
        if required:
            raise RuntimeError("codex_connection_context_unavailable")
        return None
    return update_connection(state, connection)


def active_connection_id(state):
    return load_registry(state).get("active_connection_id")


def select_connection(state, target_session=None, *, require_files=True):
    """Select a context from the newest app generation, excluding self-send."""
    registry = load_registry(state)
    active = registry.get("active_connection_id")
    candidates = []
    for raw in registry.get("connections", {}).values():
        if not isinstance(raw, dict):
            continue
        try:
            item = normalize_connection(raw, require_files=require_files)
        except (OSError, ValueError):
            continue
        if active and item["connection_id"] != active:
            continue
        if target_session and item["origin_thread"] == target_session:
            continue
        candidates.append(item)
    candidates.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
    return candidates[0] if candidates else None


def runtime_problem(state, target_session=None):
    """Cheap health check for deleted/moved Node or app-tools server files."""
    connection = select_connection(state, target_session, require_files=False)
    if connection is None:
        return "connection_context_missing"
    if not Path(connection["command"][0]).is_file():
        return "runtime_missing"
    if not Path(connection["command"][1]).is_file():
        return "app_tools_server_missing"
    return None

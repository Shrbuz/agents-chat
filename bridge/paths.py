"""Locate the hub config/state from the CLI, a project entry, or a portable root."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = Path(".agents-chat") / "project.json"


def project_context(start=None):
    """Walk up from ``start`` looking for a project's .agents-chat/project.json."""
    current = Path(start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / ENTRY
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                data["_path"] = str(candidate)
                data["_root"] = str(directory)
                return data
    return {}


def _first_file(*candidates):
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    return None


def default_config():
    context = project_context()
    hub = context.get("hub") if isinstance(context.get("hub"), dict) else {}
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/AgentsChat"
    return _first_file(
        os.environ.get("AGENTS_CHAT_CONFIG"),
        hub.get("config"),
        ROOT / "config.local.json",
        ROOT / "config.json",
        local / "config.local.json",
        local / "config.json",
    )


def resolve_config(explicit=None):
    path = _first_file(explicit, os.environ.get("AGENTS_CHAT_CONFIG"))
    if path is None:
        path = default_config()
    if path is None:
        raise ValueError("config_not_found: pass --config or create config.local.json")
    return path


def resolve_state(explicit, config):
    context = project_context()
    hub = context.get("hub") if isinstance(context.get("hub"), dict) else {}
    value = explicit or os.environ.get("AGENT_BRIDGE_STATE") or os.environ.get("AGENTS_CHAT_STATE") or hub.get("state")
    if value:
        return Path(value).resolve()
    config = Path(config).resolve()
    parent = config.parent
    if (parent / ".bridge").is_dir() or config.name.endswith(".local.json"):
        return (parent / ".bridge").resolve()
    if (parent / "data").is_dir():
        return (parent / "data").resolve()
    return (parent / ".bridge").resolve()


def context_project():
    value = project_context().get("project_id")
    return value if isinstance(value, str) else None

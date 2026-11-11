"""Official OpenCode CLI integration and project-local MCP installation."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import ConfigError


def discover_opencode_command():
    """Resolve the native OpenCode executable, never a shell wrapper."""
    explicit = os.environ.get("OPENCODE_CLI")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_file() and path.suffix.lower() == ".exe":
            return [str(path)]
        raise ConfigError("opencode_cli_missing_or_not_native: " + str(path))
    found = shutil.which("opencode.exe")
    if found and Path(found).is_file():
        return [str(Path(found).resolve())]
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@opencode/cli/bin/opencode.exe",
        Path.home() / ".opencode/bin/opencode.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Links/opencode.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/opencode/opencode.exe",
    ]
    for path in candidates:
        if path.is_file():
            return [str(path.resolve())]
    raise ConfigError("runtime_not_found: install the official OpenCode CLI or set OPENCODE_CLI")


def classify_opencode_error(value):
    text = str(value or "").lower()
    if "file is not a database" in text or "background service failed" in text or "server exited" in text:
        return "opencode_service_unavailable"
    if "session" in text and ("not found" in text or "does not exist" in text):
        return "opencode_session_missing"
    if "busy" in text or "already running" in text or "in progress" in text:
        return "desktop_chat_busy"
    if "auth" in text or "credential" in text or "provider" in text and "login" in text:
        return "opencode_auth_required"
    if "no such file" in text or "cannot find" in text or "winerror 2" in text:
        return "opencode_cli_missing"
    return "opencode_transport"


def _run(command, arguments, cwd, timeout=30, input_text=None):
    try:
        return subprocess.run(
            [*command, *arguments], cwd=str(Path(cwd).resolve()), input=input_text,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except FileNotFoundError as exc:
        raise RuntimeError("opencode_cli_missing: " + str(exc)) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("opencode_service_timeout") from None


def probe_opencode(command, cwd, session_id=None):
    """Prove both server health and, when supplied, exact session visibility."""
    process = _run(command, ["session", "list", "--format", "json"], cwd, timeout=25)
    if process.returncode:
        detail = (process.stderr or process.stdout).strip()
        kind = classify_opencode_error(detail)
        raise RuntimeError(kind + ": " + detail[:600])
    try:
        payload = json.loads(process.stdout)
    except ValueError:
        raise RuntimeError("opencode_transport: invalid session list") from None
    rows = payload if isinstance(payload, list) else payload.get("sessions", []) if isinstance(payload, dict) else []
    ids = {str(row.get("id") or row.get("sessionID") or row.get("session_id"))
           for row in rows if isinstance(row, dict)}
    if session_id and session_id not in ids:
        raise RuntimeError("opencode_session_missing: " + str(session_id))
    return {"ready": True, "sessions": len(ids), "session_id": session_id}


def export_opencode_session(command, cwd, session_id):
    process = _run(command, ["session", "export", session_id], cwd, timeout=30)
    if process.returncode:
        detail = (process.stderr or process.stdout).strip()
        kind = classify_opencode_error(detail)
        raise RuntimeError(kind + ": " + detail[:600])
    if len(process.stdout.encode("utf-8", errors="replace")) > 32 * 1024 * 1024:
        raise RuntimeError("opencode_session_export_too_large")
    try:
        return json.loads(process.stdout)
    except ValueError:
        raise RuntimeError("opencode_transport: invalid session export") from None


def inspect_opencode_delivery(command, cwd, session_id, task_id):
    marker = "Agents Chat wakeup " + task_id + "."
    task_marker = "Agents Chat task " + task_id + "."
    exported = export_opencode_session(command, cwd, session_id)
    messages = exported.get("messages", []) if isinstance(exported, dict) else []
    seen, summary = False, ""
    for message in messages if isinstance(messages, list) else []:
        serialized = json.dumps(message, ensure_ascii=False)
        if marker in serialized or task_marker in serialized:
            seen = True
            continue
        if not seen or not isinstance(message, dict):
            continue
        info = message.get("info") if isinstance(message.get("info"), dict) else message
        if info.get("role") != "assistant":
            continue
        texts = []
        for part in message.get("parts", []):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        if texts:
            summary = texts[-1]
    if not seen:
        serialized = json.dumps(exported, ensure_ascii=False)
        seen = marker in serialized or task_marker in serialized
    return {"delivered": seen, "summary": summary[:800]}


def session_contains_task(command, cwd, session_id, task_id):
    return inspect_opencode_delivery(command, cwd, session_id, task_id)["delivered"]


def parse_opencode_stream(stdout):
    """Return the final text and session ID from official NDJSON output."""
    texts, session_id, error = [], None, None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        session_id = event.get("sessionID") or event.get("session_id") or session_id
        if event.get("type") == "error":
            detail = event.get("error")
            error = str(detail.get("message") if isinstance(detail, dict) else detail or "OpenCode failed")
        part = event.get("part")
        text = part.get("text") if isinstance(part, dict) else event.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text)
    return {"summary": (texts[-1] if texts else "")[:800], "session_id": session_id,
            "error": error}


def opencode_mcp_command(cli, state, config, project, role):
    address = role if project == "default" else project + "." + role
    return [*map(str, cli), "--state", str(Path(state).resolve()),
            "--config", str(Path(config).resolve()), "--project", project,
            "mcp", "--agent", address, "--transport", "opencode_native"]


def install_opencode_mcp(cli, state, config, project, role, root, server_name=None,
                         opencode_command=None):
    """Use OpenCode's own config writer so existing JSON/JSONC stays valid."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ConfigError("project_root_missing: " + str(root))
    command = opencode_command or discover_opencode_command()
    name = server_name or ("agents-chat-" + project + "-" + role)
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name)[:80]
    config_path = root / "opencode.json"
    before = config_path.read_bytes() if config_path.is_file() else None
    mcp_command = opencode_mcp_command(cli, state, config, project, role)
    process = _run(command, ["mcp", "add", name, "--", *mcp_command], root, timeout=45)
    if process.returncode:
        raise ConfigError("opencode_mcp_install_failed: "
                          + (process.stderr or process.stdout).strip()[:800])
    after = config_path.read_bytes() if config_path.is_file() else None
    return {"path": str(config_path), "server": name, "changed": before != after,
            "command": mcp_command, "output": (process.stdout or "").strip()[:800]}


def opencode_status(root=None):
    try:
        command = discover_opencode_command()
    except ConfigError as exc:
        return {"runtime": {"available": False, "error": str(exc)},
                "service": {"ready": False, "state": "cli_missing"}}
    cwd = Path(root).resolve() if root else Path.cwd()
    process = _run(command, ["service", "status"], cwd, timeout=15)
    reported = (process.stdout or process.stderr).strip().lower() or "unknown"
    try:
        probe_opencode(command, cwd)
        service = {"ready": True, "state": "running", "reported_state": reported, "error": None}
    except RuntimeError as exc:
        kind = classify_opencode_error(exc)
        service = {"ready": False, "state": kind.removeprefix("opencode_"),
                   "reported_state": reported, "error": str(exc)[:500]}
    return {"runtime": {"available": True, "command": command}, "service": service}

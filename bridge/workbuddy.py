"""WorkBuddy discovery and local MCP configuration."""
import json
import os
import shutil
from pathlib import Path

from .config import ConfigError


def _registry_roots():
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    roots = []
    paths = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, key_path in paths:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                count = winreg.QueryInfoKey(key)[0]
                for index in range(count):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, index)) as entry:
                            name = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                            if not name.lower().startswith("workbuddy"):
                                continue
                            try:
                                location = winreg.QueryValueEx(entry, "InstallLocation")[0]
                            except OSError:
                                location = ""
                            if location:
                                roots.append(Path(location))
                                continue
                            uninstall = str(winreg.QueryValueEx(entry, "UninstallString")[0])
                            executable = uninstall.split('"', 2)[1] if uninstall.startswith('"') else uninstall.split()[0]
                            roots.append(Path(executable).parent)
                    except OSError:
                        continue
        except OSError:
            continue
    return roots


def workbuddy_roots():
    roots = []
    configured = os.environ.get("WORKBUDDY_HOME")
    if configured:
        roots.append(Path(configured))
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            roots.extend((Path(base) / "WorkBuddy", Path(base) / "Programs" / "WorkBuddy"))
    roots.extend(_registry_roots())
    seen = set()
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            yield resolved


def discover_workbuddy_command():
    """Return the live WorkBuddy/CodeBuddy CLI command without caching it."""
    explicit = os.environ.get("WORKBUDDY_CLI")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_file():
            return [str(path)]
        raise ConfigError("workbuddy_cli_missing: " + str(path))
    for name in ("workbuddy", "workbuddy.exe", "codebuddy", "codebuddy.exe", "codebuddy-code"):
        found = shutil.which(name)
        if found:
            return [found]
    for root in workbuddy_roots():
        executable = root / "WorkBuddy.exe"
        entry = root / "resources" / "app.asar.unpacked" / "cli" / "bin" / "codebuddy"
        if executable.is_file() and entry.is_file():
            return [str(executable), str(entry)]
    raise ConfigError("runtime_not_found: install WorkBuddy or pass --command")


def _read_object(path, default):
    if not path.is_file():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError("invalid_workbuddy_mcp_config: " + str(path)) from exc
    if not isinstance(value, dict):
        raise ConfigError("invalid_workbuddy_mcp_config: expected an object")
    return value


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".agents-chat.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def workbuddy_mcp_entry(cli, state, config, project, role):
    address = role if project == "default" else project + "." + role
    return {
        "type": "stdio",
        "command": str(Path(cli[0]).resolve()),
        "args": [*map(str, cli[1:]), "--state", str(Path(state).resolve()),
                 "--config", str(Path(config).resolve()), "--project", project,
                 "mcp", "--agent", address, "--transport", "workbuddy_mcp"],
        "timeout": 30000,
    }


def install_workbuddy_mcp(cli, state, config, project, role, path=None, server_name=None):
    path = Path(path).expanduser().resolve() if path else Path.home() / ".workbuddy" / "mcp.json"
    name = server_name or ("agents-chat-" + project + "-" + role)
    data = _read_object(path, {"mcpServers": {}})
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ConfigError("invalid_workbuddy_mcp_config: mcpServers must be an object")
    entry = workbuddy_mcp_entry(cli, state, config, project, role)
    changed = servers.get(name) != entry
    servers[name] = entry
    if changed or not path.is_file():
        _atomic_json(path, data)
    return {"path": str(path), "server": name, "changed": changed, "entry": entry}


def workbuddy_status(path=None):
    path = Path(path).expanduser().resolve() if path else Path.home() / ".workbuddy" / "mcp.json"
    data = _read_object(path, {"mcpServers": {}})
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
    try:
        command = discover_workbuddy_command()
        runtime = {"available": True, "command": command}
    except ConfigError as exc:
        runtime = {"available": False, "error": str(exc)}
    return {"installed": path.is_file(), "path": str(path),
            "servers": sorted(servers), "runtime": runtime}

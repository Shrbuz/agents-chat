"""Per-user Windows installation. Keeps state and local configuration on updates."""
import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def definitely_stopped(pid):
    """Only remove a stale descriptor after Windows confirms that PID is absent."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000,False,int(pid))
    if not handle:
        return ctypes.get_last_error()==87
    kernel.CloseHandle(handle)
    return False


def stop_tray():
    """Ask an existing 0.5 tray to exit before replacing its executable."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.OpenEventW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenEventW(0x0002, False, "Local\\AgentsChatQuit05")
        if handle:
            kernel.SetEvent(handle)
            kernel.CloseHandle(handle)
            time.sleep(2.0)
    except OSError:
        pass


def create_shortcut(path, executable, arguments, working_directory):
    """Create a real Windows shell link, which Start search can index."""
    command = """
$taskShell = New-Object -ComObject WScript.Shell
$taskLink = $taskShell.CreateShortcut($env:AGENTS_CHAT_LINK)
$taskLink.TargetPath = $env:AGENTS_CHAT_EXE
$taskLink.Arguments = $env:AGENTS_CHAT_ARGUMENTS
$taskLink.WorkingDirectory = $env:AGENTS_CHAT_ROOT
$taskLink.Description = 'Agents Chat local multi-project agent collaboration tray'
$taskLink.Save()
"""
    environment = dict(os.environ, AGENTS_CHAT_LINK=str(path), AGENTS_CHAT_EXE=str(executable),
                       AGENTS_CHAT_ARGUMENTS=str(arguments), AGENTS_CHAT_ROOT=str(working_directory))
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode(command.encode("utf-16-le")).decode("ascii")],
                   env=environment, check=True, creationflags=subprocess.CREATE_NO_WINDOW)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "Programs/AgentsChat")
    parser.add_argument("--previous-state", type=Path)
    parser.add_argument("--autostart", action="store_true")
    parser.add_argument("--no-start", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("This installer currently supports Windows only.")
    from bridge.broker import read_config
    from bridge.client import Client
    destination = args.destination.resolve()
    if destination == ROOT or destination in ROOT.parents or ROOT in destination.parents:
        raise SystemExit("安装目录必须与源码目录独立。")
    stop_tray()
    from build_exe import build_all
    launcher, cli = build_all(ROOT)
    # Preflight configuration before stopping an existing service.
    config_path = destination / "config.local.json"
    existing = config_path.exists()
    config = read_config(config_path if existing else ROOT / "config.local.json")
    previous = args.previous_state
    states = [destination / ".bridge"]
    if previous and previous.resolve() != states[0].resolve():
        states.append(previous.resolve())
    live = []
    for state in states:
        try:
            health = Client(state).call("health")
        except RuntimeError:
            if (state / "endpoint.json").exists():
                descriptor = json.loads((state / "endpoint.json").read_text(encoding="utf-8"))
                if definitely_stopped(descriptor.get("pid",0)):
                    (state / "endpoint.json").unlink()
                else:
                    raise SystemExit(f"无法确认旧服务状态，请先检查：{state}")
            continue
        if health.get("states", {}).get("queued", 0) or health.get("states", {}).get("running", 0):
            raise SystemExit("仍有排队或执行中的任务，请完成后再安装。")
        live.append(state)
    for state in live:
        Client(state).call("shutdown")
        deadline = time.monotonic() + 15
        while (state / "endpoint.json").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if (state / "endpoint.json").exists():
            raise SystemExit("旧服务尚未退出，安装已停止。")
    destination.mkdir(parents=True, exist_ok=True)
    for filename in ["bridge.py", "desktop.py", "desktop_link.py", "qoder_desktop_link.py", "launch_qoder_native.py",
                     "uninstall.py", "login_qoder.py", "README.md", "DESIGN.md", "INTEGRATION.md", "VERIFICATION.md",
                     "HANDOFF.md", "install.ps1", "uninstall.ps1", "LICENSE", "VERSION"]:
        if (ROOT / filename).is_file():
            shutil.copy2(ROOT / filename, destination / filename)
    shutil.copy2(launcher, destination / "AgentsChat.exe")
    shutil.copy2(cli, destination / "AgentsChat.Cli.exe")
    (destination / "python-runtime.txt").write_text(str(Path(sys.executable).resolve()) + "\n", encoding="utf-8")
    shutil.copytree(ROOT / "bridge", destination / "bridge", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    (destination / "connectors").mkdir(exist_ok=True)
    for filename in ("QoderDesktop.exe","QoderDesktop.cs"):
        shutil.copy2(ROOT / "connectors" / filename,destination / "connectors" / filename)
    shutil.copytree(ROOT/"connectors/qoder-native",destination/"connectors/qoder-native",dirs_exist_ok=True)
    native_bridge = destination/"connectors/qoder-native/bridge.local.json"
    native_options = json.loads(native_bridge.read_text(encoding="utf-8")) if native_bridge.exists() else {}
    native_options.setdefault("state", str(destination/".bridge"))
    native_options.setdefault("agent", "qoder")
    native_bridge.write_text(json.dumps(native_options,ensure_ascii=False),encoding="utf-8")
    shutil.copytree(ROOT / "tool-skill",destination / "tool-skill",dirs_exist_ok=True)
    for skill_root in (Path.home()/".codex/skills", Path.home()/".qoder/skills", Path.home()/".agents/skills"):
        skill_path = skill_root / "agents-chat-desktop"
        skill_path.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT / "tool-skill/SKILL.md",skill_path / "SKILL.md")
    if not existing:
        shutil.copytree(ROOT / "examples", destination / "examples", dirs_exist_ok=True)
        for endpoint in config["endpoints"].values():
            cwd = Path(endpoint["cwd"])
            if cwd.is_relative_to(ROOT / "examples"):
                endpoint["cwd"] = str(destination / cwd.relative_to(ROOT))
            if endpoint.get("adapter") == "qoder" and len(endpoint["command"]) > 1:
                script = Path(endpoint["command"][1])
                if script.name == "qodercli.js" and script.parent.parent.parent.name == "@qoder-ai":
                    modules = script.parent.parent.parent.parent
                    local_modules = destination / "runtime/qoder/node_modules"
                    shutil.copytree(modules, local_modules, dirs_exist_ok=True)
                    endpoint["command"][1] = str(local_modules / script.relative_to(modules))
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    if not config["endpoints"].get("qoder",{}).get("desktop_input_enabled",True):
        (destination / ".bridge").mkdir(exist_ok=True)
        (destination / ".bridge/qoder-input-disabled").write_text("User preference: do not focus, fill or submit the Qoder composer.\n",encoding="utf-8")
    executable = destination / "AgentsChat.exe"
    command_executable = destination / "AgentsChat.Cli.exe"
    menu = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Agents Chat"
    menu.mkdir(parents=True, exist_ok=True)
    for name, arguments in [("Agents Chat", ""), ("卸载 Agents Chat", "uninstall")]:
        create_shortcut(menu / (name + ".lnk"), executable, arguments, destination)
        (menu / (name + ".vbs")).unlink(missing_ok=True)
    # Resolve the real Desktop, including OneDrive redirection.
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
        desktop = Path(os.path.expandvars(winreg.QueryValueEx(key, "Desktop")[0]))
    desktop.mkdir(parents=True, exist_ok=True)
    create_shortcut(desktop / "Agents Chat.lnk", executable, "", destination)
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AgentsChat") as key:
        for name, value in {"DisplayName": "Agents Chat", "DisplayVersion": "0.5.0", "InstallLocation": str(destination),
                            "DisplayIcon": str(executable),
                            "UninstallString": subprocess.list2cmdline([str(executable), "uninstall"])}.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    for agent in config["endpoints"]:
        mcp = {"mcpServers": {"local_agent_bridge": {"command": str(command_executable), "args": ["--state", str(destination / ".bridge"), "mcp", "--agent", agent]}}}
        (destination / f"mcp-{agent}.local.json").write_text(json.dumps(mcp, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.autostart or (Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/Agents Chat.vbs").exists():
        subprocess.run([sys.executable, "-c", "from desktop import set_startup; set_startup(True)"], cwd=destination, check=True)
    if args.no_start:
        result = {"status": "installed", "tray": "not_started"}
    else:
        # The tray host owns the low-footprint background process and also
        # gives the user a status window.  Fall back to a hidden broker start.
        subprocess.Popen([str(executable)], cwd=str(destination),
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 20
        health = None
        while time.monotonic() < deadline:
            try:
                health = Client(destination / ".bridge").call("health")
                break
            except RuntimeError:
                time.sleep(0.2)
        if health is None:
            launched = subprocess.run([sys.executable, str(destination / "bridge.py"), "--state", str(destination / ".bridge"),
                                       "start", "--config", str(config_path)], capture_output=True, text=True, encoding="utf-8", check=True)
            health = json.loads(launched.stdout)
        result = {"status": "installed", "tray": "started", "health": health}
    print(json.dumps({"installation": str(destination), "health": result, "menu": str(menu)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

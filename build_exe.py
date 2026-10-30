"""Build the small native Windows launcher with the inbox .NET compiler."""
from pathlib import Path
import os
import subprocess
import time


ROOT = Path(__file__).resolve().parent


def compiler():
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = [
        windows / "Microsoft.NET/Framework64/v4.0.30319/csc.exe",
        windows / "Microsoft.NET/Framework/v4.0.30319/csc.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("Windows .NET Framework C# compiler was not found")


def build(output=None, console=False):
    output = Path(output or ROOT / "AgentsChat.exe").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".new.exe")
    source = ROOT / "launcher/AgentsChatLauncher.cs"
    command = [
        str(compiler()), "/nologo", "/target:" + ("exe" if console else "winexe"), "/optimize+", "/platform:anycpu",
        "/reference:System.Windows.Forms.dll", "/reference:System.Web.Extensions.dll",
        "/out:" + str(temporary), str(source),
    ]
    if console:
        command.insert(3, "/define:CLI")
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr).strip())
    # A running tray may still be shutting down and holding the old file.
    for attempt in range(80):
        try:
            os.replace(temporary, output)
            return output
        except PermissionError:
            if attempt == 79:
                raise
            time.sleep(0.25)
    return output


def build_all(directory=None):
    directory = Path(directory or ROOT).resolve()
    return (
        build(directory / "AgentsChat.exe"),
        build(directory / "AgentsChat.Cli.exe", console=True),
    )


if __name__ == "__main__":
    for artifact in build_all():
        print(artifact)

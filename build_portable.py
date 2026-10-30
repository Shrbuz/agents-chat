"""Build a self-contained Windows portable folder and zip.

The app keeps the tested Python hub and ships the official Python embeddable
runtime, so the target machine does not need Python, Node or pip.  The tray
launcher is the same small .NET Framework executable used by the installer.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").is_file() else "0.5.0"
PYTHON_VERSION = "3.14.8"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"


def download_python(cache):
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.is_file() and cache.stat().st_size > 10_000_000:
        return cache
    print("downloading", PYTHON_URL)
    request = urllib.request.Request(PYTHON_URL, headers={"User-Agent": "agents-chat-build"})
    with urllib.request.urlopen(request, timeout=120) as response, cache.open("wb") as output:
        shutil.copyfileobj(response, output)
    if cache.stat().st_size < 10_000_000:
        raise RuntimeError("python embeddable download is incomplete")
    return cache


def extract_python(archive, destination):
    runtime = destination / "runtime" / "python"
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(runtime)
    # Make the application root importable from the embedded interpreter.
    pth = next(runtime.glob("python*._pth"), None)
    if pth is None:
        raise RuntimeError("embedded Python _pth file not found")
    lines = pth.read_text(encoding="utf-8").splitlines()
    for entry in ("..\\..", "../.."):
        if entry not in lines:
            lines.append(entry)
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not (runtime / "python.exe").is_file():
        raise RuntimeError("embedded Python executable not found")
    return runtime


def copy_app(destination):
    from build_exe import build_all
    build_all(ROOT)
    for name in ("bridge.py", "config.example.json", "README.md", "DESIGN.md", "INTEGRATION.md",
                 "VERIFICATION.md", "HANDOFF.md", "install.ps1", "uninstall.ps1", "LICENSE", "VERSION"):
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    shutil.copytree(ROOT / "bridge", destination / "bridge", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "tool-skill", destination / "tool-skill", dirs_exist_ok=True)
    if (ROOT / "examples").is_dir():
        shutil.copytree(ROOT / "examples", destination / "examples", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
    connector = ROOT / "connectors" / "qoder-native"
    if connector.is_dir():
        shutil.copytree(connector, destination / "connectors" / "qoder-native",
                        dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    for exe in ("AgentsChat.exe", "AgentsChat.Cli.exe"):
        shutil.copy2(ROOT / exe, destination / exe)


def write_support_files(destination):
    (destination / "data").mkdir(exist_ok=True)
    (destination / "data" / ".keep").write_text("", encoding="utf-8")
    (destination / "VERSION").write_text(VERSION + "\n", encoding="utf-8")
    (destination / "python-runtime.txt").write_text("runtime\\python\\python.exe\n", encoding="utf-8")
    native = destination / "connectors" / "qoder-native" / "bridge.local.json"
    if native.parent.is_dir():
        native.write_text(json.dumps({"state": str(destination / "data"), "agent": "qoder"}, ensure_ascii=False),
                          encoding="utf-8")
    config = destination / "config.json"
    if not config.exists():
        config.write_text(json.dumps({"version": 2, "projects": {}, "portable": True},
                                     ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / "README-便携版.md").write_text(f"""# Agents Chat {VERSION} 便携版

这是自带 Python 运行时的 Windows 便携目录。解压后直接双击 `AgentsChat.exe`：

- 程序进入系统托盘，并自动启动本地后台。
- 双击托盘图标或再次运行 `AgentsChat.exe ui` 可打开状态窗口。
- 不需要安装 Python、Node、pip，也没有管理员权限要求。

## 让 Agent 自己完成会话配置

把便携目录路径告诉 Agent，让它在这个目录运行 CLI。例如：

```text
AgentsChat.Cli.exe --config <便携目录>\\config.json setup --root <项目目录> --project <项目ID> --role backend:codex --role frontend:qoder
```

`setup` 会写项目内的 `.agents-chat/project.json`、角色提示和 `AGENTS.md` 入口；随后新会话可以自己注册角色并互相派发任务。
如果 Agent 需要 MCP，把生成的 `.agents-chat` 入口或 `data/mcp-*.local.json` 交给对应客户端。

## 项目与角色

- 一个后台可管理多个项目；角色地址是 `<项目>.<角色>`。
- 同一 Agent 可以占多个角色/会话（例如 `alpha.architect` 与 `alpha.builder`）。
- 任务结果自动回到发起角色的收件箱；不要为同一结果重复回发任务。
- 托盘窗口只用于查看状态和启停后台，日常配置由 Agent 通过 CLI 完成。

## 卸载

先退出托盘，再删除本目录即可。也可以运行 `uninstall.ps1 -Destination <便携目录>`；默认保留
`config.json` 和 `data`，`-RemoveData` 才会一起删除。
""", encoding="utf-8")


def zip_folder(destination, output):
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in destination.rglob("*"):
            if path.is_dir():
                continue
            relative = path.relative_to(destination)
            if "__pycache__" in relative.parts:
                continue
            zf.write(path, Path(destination.name) / relative)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "AgentsChat-Portable")
    parser.add_argument("--python-zip", type=Path)
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args()
    destination = args.output.resolve()
    dist = (ROOT / "dist").resolve()
    if destination != dist and dist not in destination.parents:
        raise SystemExit("便携输出目录必须位于项目的 dist 目录下")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    archive = args.python_zip or ROOT / "build" / "cache" / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    extract_python(download_python(archive), destination)
    copy_app(destination)
    write_support_files(destination)
    python = destination / "runtime" / "python" / "python.exe"
    check = subprocess.run([str(python), str(destination / "bridge.py"), "--version"],
                           capture_output=True, text=True, encoding="utf-8")
    if check.returncode:
        raise RuntimeError("portable Python self-check failed: " + check.stdout + check.stderr)
    result = {"portable": str(destination), "python": str(python), "version": check.stdout.strip()}
    if not args.no_zip:
        result["zip"] = str(zip_folder(destination, ROOT / "dist" / f"AgentsChat-Portable-{VERSION}.zip"))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

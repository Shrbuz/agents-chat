"""Create local read-only demo endpoints and an MCP config without changing global settings."""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", help="Absolute path to codex executable")
    parser.add_argument("--qoder-js", help="Absolute path to official npm bundle/qodercli.js")
    parser.add_argument("--qoder-exe", help="Absolute path to native qodercli executable")
    parser.add_argument("--force", action="store_true", help="Replace this project's local runtime config")
    args = parser.parse_args()
    output = ROOT / "config.local.json"
    if output.exists() and not args.force:
        raise SystemExit("config.local.json already exists; use --force only when you want to replace it.")
    codex = args.codex or shutil.which("codex")
    qoder_js = Path(args.qoder_js).resolve() if args.qoder_js else None
    qoder_exe = args.qoder_exe or shutil.which("qodercli.exe")
    if not qoder_js and not qoder_exe:
        candidates = [ROOT.parents[1] / "work/qoder-runtime/node_modules/@qoder-ai/qodercli/bundle/qodercli.js",
                      Path.home() / "AppData/Roaming/npm/node_modules/@qoder-ai/qodercli/bundle/qodercli.js"]
        qoder_js = next((p for p in candidates if p.is_file()), None)
        native = Path.home() / ".qoder/bin/qodercli/qodercli.exe"
        if not qoder_js and native.is_file():
            qoder_exe = str(native)
    if not codex or (not qoder_exe and not qoder_js):
        raise SystemExit("Install the official Codex/Qoder CLI, or pass --codex and --qoder-js/--qoder-exe. No global settings were changed.")
    if qoder_js and (not qoder_js.is_file() or not shutil.which("node")):
        raise SystemExit("Qoder JS bundle or Node.js is missing.")
    endpoints = {}
    for name in ("codex", "qoder"):
        cwd = ROOT / "examples" / "workspaces" / name
        cwd.mkdir(parents=True, exist_ok=True)
        # These are demo-only files shipped with the project; never overwrite user data.
        for filename, content in {"values.json": "[19,23,7]\n",
                                  "AGENTS.md": "This is an isolated read-only Agent Bridge demo workspace. Read values.json when requested. Do not modify files.\n"}.items():
            path = cwd / filename
            if not path.exists():
                path.write_text(content, encoding="utf-8")
        endpoints[name] = {"adapter": name, "cwd": str(cwd), "mode": "independent_cli_task"}
    endpoints["codex"].update(command=[codex], sandbox="read-only", lean=True, args=["--ephemeral"])
    endpoints["qoder"].update(command=[qoder_exe] if qoder_exe else [shutil.which("node"), str(qoder_js)],
        args=["--permission-mode", "dont_ask", "--tools", "Read", "--allowed-tools", "Read", "--strict-mcp-config",
              "--no-session-persistence", "--max-output-tokens", "1000", "--max-model-request-retries", "0"])
    output.write_text(json.dumps({"endpoints": endpoints}, ensure_ascii=False, indent=2), encoding="utf-8")
    for agent in ("codex", "qoder"):
        config = {"mcpServers": {"local_agent_bridge": {"command": sys.executable, "args": [str(ROOT / "bridge.py"),
                   "--state", str(ROOT / ".bridge"), "mcp", "--agent", agent]}}}
        (ROOT / f"mcp-{agent}.local.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Created config.local.json and local MCP examples. Start/restart the broker to use the configuration.")


if __name__ == "__main__":
    main()

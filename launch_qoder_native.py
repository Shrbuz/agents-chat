"""Launch the user's Qoder with an isolated native-development extension setting.

No application bundle edits, global environment changes, signing impersonation,
browser debugging ports, or UI automation. Refuses to stop a running Qoder.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--app",type=Path)
    parser.add_argument("--check",action="store_true")
    args=parser.parse_args()
    state=ROOT/".bridge"
    if args.check:
        try:
            status=json.loads((state/"qoder-native-status.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(json.dumps({"ready":False,"fresh":False,"reason":"Native extension has not started"}))
            return
        status["fresh"]=time.time()*1000-status["updated_at"]<15000
        print(json.dumps(status,ensure_ascii=False))
        return
    app=args.app
    if app is None:
        settings=json.loads((ROOT/"qoder-native-launch.local.json").read_text(encoding="utf-8"))
        app=Path(settings["app"])
    if not app.is_file():
        raise SystemExit("Qoder application not found")
    if args.app:
        (ROOT/"qoder-native-launch.local.json").write_text(json.dumps({"app":str(app.resolve())},ensure_ascii=False),encoding="utf-8")
    running=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",
        "@(Get-Process -Name Qoder -ErrorAction SilentlyContinue).Count"],capture_output=True,text=True,check=True,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if int(running.stdout.strip() or "0"):
        try:
            status=json.loads((state/"qoder-native-status.json").read_text(encoding="utf-8"))
            if status.get("ready") and time.time()*1000-status.get("updated_at",0)<15000:
                print(json.dumps({"ready":True,"already_running":True,"ui_automation":False}))
                return
        except (OSError,ValueError):
            pass
        raise SystemExit("Qoder is still running. Exit Qoder normally before loading the native extension; no process was stopped.")
    plugin=ROOT/"connectors/qoder-native"
    if not (plugin/"dist/node/server.cjs").exists():
        raise SystemExit("Native extension package not found")
    convention=Path.home()/".qoder/dev-extensions"
    if convention.exists() and any(item.is_dir() for item in convention.iterdir()):
        raise SystemExit("Other native development extensions are present. Review them before enabling development loading; nothing was launched.")
    (plugin/"bridge.local.json").write_text(json.dumps({"state":str(state),"agent":"*"},ensure_ascii=False),encoding="utf-8")
    environment=dict(os.environ)
    # Launchers inside Codex/DSH may expose ELECTRON_RUN_AS_NODE=1.  Inheriting
    # it makes Qoder start as plain Node and exit before any window appears.
    environment.pop("ELECTRON_RUN_AS_NODE",None)
    environment.update(QODER_APP_PLUGIN_ALLOW_UNVERIFIED="true",QODER_APP_PLUGIN_DEV_DIRS=str(plugin))
    process=subprocess.Popen([str(app)],env=environment,cwd=app.parent,creationflags=subprocess.CREATE_NO_WINDOW)
    print(json.dumps({"launched":True,"pid":process.pid,"extension":str(plugin),"scope":"this Qoder process only","ui_automation":False},ensure_ascii=False))


if __name__=="__main__":
    main()

"""Small Windows control panel for the local broker; no model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

ROOT = Path(__file__).resolve().parent


def startup_path():
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/Agents Chat.vbs"


def set_startup(enabled):
    path = startup_path()
    if enabled:
        # Prefer the tray host: it starts the hub and stays available in the
        # notification area. Fall back to a hidden hub start on older installs.
        tray = ROOT / "AgentsChat.exe"
        if tray.exists():
            command = subprocess.list2cmdline([str(tray)])
        else:
            launcher = ROOT / "AgentsChat.Cli.exe"
            command = subprocess.list2cmdline(([str(launcher)] if launcher.exists() else [sys.executable, str(ROOT / "bridge.py")]) +
                ["--state", str(ROOT / ".bridge"), "start", "--config", str(ROOT / "config.local.json")])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('CreateObject("WScript.Shell").Run "' + command.replace('"', '""') + '", 0, False\n', encoding="utf-16")
    elif path.exists():
        # Only remove the startup entry belonging to this installation.
        if str(ROOT).lower() not in path.read_text(encoding="utf-16").lower():
            raise RuntimeError("启动项属于另一个安装目录，请先从该目录取消登录启动。")
        path.unlink()


def main():
    window = tk.Tk()
    window.title("Agents Chat · 本地通信")
    window.geometry("640x475")
    window.resizable(False, False)
    status = tk.StringVar(value="正在读取状态…")
    tk.Label(window, text="Agents Chat", font=("Microsoft YaHei UI", 20)).pack(pady=(20, 8))
    tk.Label(window, textvariable=status, wraplength=540, justify="left").pack(pady=8)
    tk.Label(window, text="关闭此窗口后，后台仍可接收和执行任务。\n模型调用仍使用厂商服务。桌面模式需先连接指定聊天。", justify="left").pack(pady=8)
    routes = tk.StringVar(value="")
    tk.Label(window, textvariable=routes, wraplength=600, justify="left").pack(pady=5)
    actions = tk.Frame(window)
    actions.pack(pady=10)
    buttons = []

    def run(action):
        for button in buttons:
            button.config(state="disabled")
        status.set("正在处理…")

        def work():
            from bridge.client import Client
            from bridge.cli import start
            try:
                if action == "start":
                    result = start(ROOT / ".bridge", ROOT / "config.local.json")
                elif action == "stop":
                    result = Client(ROOT / ".bridge").call("shutdown")
                else:
                    result = Client(ROOT / ".bridge").call("health")
                if action == "stop":
                    text = "已接受停止请求；请点击刷新确认进程退出。"
                else:
                    counts = result.get("states", {})
                    text = f"后台运行中 · PID {result.get('pid')} · 等待 {counts.get('queued', 0)} · 执行 {counts.get('running', 0)}"
                    targets = Client(ROOT / ".bridge").call("targets")
                    route_text = "\n".join(f"{t['name']}: " + ("仅接收请求结果（不操作输入框）" if t['mode']=="desktop_mailbox" else ("原生后台聊天 " if t.get('desktop_transport')=="qoder_native" else "桌面聊天 ") + str(t.get('session_id')) if t['mode']=="desktop_session" else "后台 CLI（尚未连接桌面）") for t in targets)
                    window.after(0, lambda: routes.set(route_text))
            except Exception as exc:
                text = "后台未连接：" + str(exc)
            window.after(0, lambda: finish(text))

        threading.Thread(target=work, daemon=True).start()

    def finish(text):
        status.set(text)
        for button in buttons:
            button.config(state="normal")

    for label, action in [("启动后台", "start"), ("停止后台", "stop"), ("刷新状态", "health")]:
        button = tk.Button(actions, text=label, width=12, command=lambda a=action: run(a))
        button.pack(side="left", padx=5)
        buttons.append(button)
    def connect_desktop():
        from tkinter import ttk
        from bridge.desktop_transport import CodexDesktop
        from bridge.client import Client
        dialog = tk.Toplevel(window)
        dialog.title("连接已有桌面聊天")
        dialog.geometry("600x290")
        tk.Label(dialog,text="先在两个产品的目标项目中各发一句话，建立聊天。\n绑定时在 Qoder 选中目标聊天；原生连接后可切换到其他页面。",justify="left").pack(pady=12)
        info = tk.StringVar(value="正在读取 Codex 聊天…")
        tk.Label(dialog,textvariable=info,wraplength=560,justify="left").pack(pady=5)
        selection = ttk.Combobox(dialog,state="readonly",width=65)
        selection.pack(pady=8)
        choices = []

        def load():
            desktop = None
            try:
                connection = json.loads((ROOT / ".bridge/codex-desktop.local.json").read_text(encoding="utf-8"))
                desktop = CodexDesktop(connection)
                inventory = desktop.call("list_threads",{"limit":50})
                config = json.loads((ROOT / "config.local.json").read_text(encoding="utf-8"))
                cwd = os.path.normcase(str(Path(config["endpoints"]["codex"]["cwd"]).resolve()))
                candidates = inventory.get("pinnedThreads",[])+inventory.get("threads",[])
                choices[:] = [t for t in candidates if t.get("kind")=="codex" and t["id"]!=connection["origin_thread"] and os.path.normcase(str(Path(t.get("cwd","")).resolve()))==cwd]
                def show():
                    selection["values"] = [t.get("title",t["id"]) for t in choices]
                    if choices: selection.current(0)
                    info.set("选择 Codex 聊天，然后连接。" if choices else "没有找到该项目的 Codex 聊天，请先在目标项目发一句话，再重新打开此窗口。")
                dialog.after(0,show)
            except Exception as exc:
                message = str(exc)
                dialog.after(0,lambda:info.set("暂时无法连接："+message))
            finally:
                if desktop: desktop.close()

        def connect():
            index = selection.current()
            if index<0: return
            connect_button.config(state="disabled")
            def work():
                try:
                    from qoder_desktop_link import ui
                    qoder = ui("inspect")
                    if not qoder.get("session_id"):
                        raise RuntimeError("请先在 Qoder 中打开目标项目的已有聊天，再点击连接。")
                    native_path=ROOT/".bridge/qoder-native-status.json"
                    native=json.loads(native_path.read_text(encoding="utf-8")) if native_path.exists() else {}
                    native_ready=native.get("ready") and time.time()*1000-native.get("updated_at",0)<15000
                    previous=next((t for t in Client(ROOT/".bridge").call("targets") if t["name"]=="qoder"),{})
                    if previous.get("desktop_transport")=="qoder_native" and not native_ready:
                        raise RuntimeError("Qoder 原生扩展未在线，请从原生连接入口重新启动 Qoder。")
                    process = subprocess.run([sys.executable,str(ROOT / "desktop_link.py"),"--thread",choices[index]["id"]],capture_output=True,text=True,encoding="utf-8",creationflags=subprocess.CREATE_NO_WINDOW)
                    if process.returncode: raise RuntimeError(process.stderr[-1000:])
                    Client(ROOT / ".bridge").call("desktop_bind",agent="qoder",session_id=qoder["session_id"],transport="qoder_native" if native_ready else "qoder_accessibility")
                    disabled = (ROOT / ".bridge/qoder-input-disabled").exists()
                    if not disabled and not native_ready:
                        with (ROOT / ".bridge/qoder-desktop.log").open("ab") as log:
                            subprocess.Popen([sys.executable,str(ROOT / "qoder_desktop_link.py"),str(ROOT / ".bridge")],stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
                    dialog.after(0,lambda:info.set("原生后台连接已绑定，不操作输入框，可切换窗口。" if native_ready else "已连接。Qoder 可发起请求并通过 wait/inbox 接收结果；输入框自动操作已禁用。" if disabled else "两个桌面会话已绑定。请保持 Qoder 目标聊天选中；可开始发送真实任务。"))
                    window.after(0,lambda:run("health"))
                except Exception as exc:
                    message = str(exc)
                    dialog.after(0,lambda:info.set("连接未完成："+message))
                finally:
                    dialog.after(0,lambda:connect_button.config(state="normal"))
            threading.Thread(target=work,daemon=True).start()
        connect_button = tk.Button(dialog,text="连接这两个聊天",command=connect)
        connect_button.pack(pady=10)
        threading.Thread(target=load,daemon=True).start()
    tk.Button(window,text="连接桌面会话…",command=connect_desktop,width=22).pack(pady=5)
    def launch_native():
        command=[sys.executable,str(ROOT/"launch_qoder_native.py")]
        if not (ROOT/"qoder-native-launch.local.json").exists():
            from tkinter import filedialog
            selected=filedialog.askopenfilename(parent=window,title="选择已安装的 Qoder.exe",filetypes=[("Qoder","Qoder.exe")])
            if not selected:return
            command.extend(["--app",selected])
        def work():
            process=subprocess.run(command,capture_output=True,text=True,encoding="utf-8",creationflags=subprocess.CREATE_NO_WINDOW)
            if process.returncode:
                text="请先在 Qoder 中正常退出应用，再点击此入口启动原生连接。\n"+process.stderr.strip()[-300:]
            else:
                text="Qoder 原生连接已启动；加载完成后可刷新状态。"
            window.after(0,lambda:status.set(text))
        threading.Thread(target=work,daemon=True).start()
    tk.Button(window,text="启动 Qoder 原生连接",command=launch_native,width=22).pack(pady=5)
    startup = tk.BooleanVar(value=startup_path().exists())

    def toggle():
        try:
            set_startup(startup.get())
        except Exception as exc:
            startup.set(not startup.get())
            messagebox.showerror("无法更改启动项", str(exc))

    tk.Checkbutton(window, text="登录 Windows 后自动启动后台", variable=startup, command=toggle).pack()
    tk.Label(window, text=str(ROOT), foreground="#666666", wraplength=550).pack(pady=10)
    def refresh():
        if buttons and buttons[0].cget("state")=="normal":
            run("health")
        window.after(5000,refresh)
    run("health")
    window.after(5000,refresh)
    window.mainloop()


if __name__ == "__main__":
    main()

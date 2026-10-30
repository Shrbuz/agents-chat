"""Remove app entry points, stop the broker, preserve configuration and evidence."""
import os
from pathlib import Path
import tkinter as tk
from tkinter import messagebox


def main():
    from desktop import ROOT, set_startup
    from bridge.client import Client
    root = tk.Tk()
    root.withdraw()
    if not messagebox.askyesno("卸载 Agents Chat", "将停止后台并移除开始菜单及登录启动项。\n安装文件、配置、队列和日志将保留，便于恢复。\n是否继续？"):
        return
    try:
        try:
            health = Client(ROOT / ".bridge").call("health")
        except RuntimeError:
            if (ROOT / ".bridge/endpoint.json").exists():
                raise RuntimeError("无法确认后台状态，请先恢复连接。")
        else:
            states = health.get("states", {})
            if states.get("running", 0) or states.get("queued", 0):
                raise RuntimeError("仍有任务，请完成后再卸载。")
            Client(ROOT / ".bridge").call("shutdown")
        set_startup(False)
        menu = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Agents Chat"
        for name in ["Agents Chat.vbs", "卸载 Agents Chat.vbs", "Agents Chat.lnk", "卸载 Agents Chat.lnk"]:
            (menu / name).unlink(missing_ok=True)
        if menu.exists() and not any(menu.iterdir()):
            menu.rmdir()
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            desktop = Path(os.path.expandvars(winreg.QueryValueEx(key, "Desktop")[0]))
        (desktop / "Agents Chat.lnk").unlink(missing_ok=True)
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AgentsChat")
        except FileNotFoundError:
            pass
        messagebox.showinfo("已移除入口", f"已停止后台并移除启动入口。\n保留的数据和程序位于：\n{ROOT}")
    except Exception as exc:
        messagebox.showerror("卸载未完成", str(exc))
    root.destroy()


if __name__ == "__main__":
    main()

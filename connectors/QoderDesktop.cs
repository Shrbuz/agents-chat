// Narrow Windows accessibility adapter for an explicitly bound Qoder chat.
// Uses no app database, authentication file, renderer injection or global keyboard input.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
using System.Windows.Automation;

public class QoderDesktop {
    delegate bool EnumWindow(IntPtr handle,IntPtr data);
    [StructLayout(LayoutKind.Sequential)] struct Rect {public int Left,Top,Right,Bottom;}
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr parent,EnumWindow callback,IntPtr data);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr handle,StringBuilder text,int count);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr handle,out Rect rect);
    [DllImport("user32.dll")] static extern bool PostMessage(IntPtr handle,uint message,IntPtr wparam,IntPtr lparam);
    static JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 2000000 };
    static AutomationElement Window() {
        var windows = new List<AutomationElement>();
        foreach (var p in Process.GetProcessesByName("Qoder")) {
            if (p.MainWindowHandle != IntPtr.Zero) windows.Add(AutomationElement.FromHandle(p.MainWindowHandle));
        }
        if (windows.Count != 1) throw new Exception("Expected one Qoder main window; found " + windows.Count);
        return windows[0];
    }
    static string Value(AutomationElement el) {
        object pattern;
        if (el.TryGetCurrentPattern(ValuePattern.Pattern, out pattern)) return ((ValuePattern)pattern).Current.Value;
        return "";
    }
    static string Session(string value) {
        var match = Regex.Match(value ?? "", @"#/chat/([A-Za-z0-9_-]+)");
        return match.Success ? match.Groups[1].Value : null;
    }
    static AutomationElement Document(AutomationElement window) {
        foreach (AutomationElement el in window.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Document))) {
            if (Value(el).StartsWith("qoder-app://renderer/")) return el;
        }
        throw new Exception("Qoder accessibility document unavailable; keep the Qoder window open and unlocked");
    }
    static AutomationElement RequireSession(AutomationElement window, string id) {
        var doc = Document(window);
        if (Session(Value(doc)) != id) throw new Exception("Qoder selected chat changed; refusing to operate on another chat");
        return doc;
    }
    static AutomationElement[] Named(AutomationElement doc, string name) {
        return doc.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.NameProperty, name)).Cast<AutomationElement>().ToArray();
    }
    static string Normalize(string text) {return text.Replace("\r\n","\n").Replace("\r","\n").Trim();}
    static void NotifyEditor(AutomationElement window, AutomationElement input) {
        // Blink's accessibility SetValue updates DOM text without informing React.
        // Send a space/backspace pair only to the renderer hosting this composer;
        // never use SendInput, the clipboard, or global keyboard shortcuts.
        input.SetFocus();
        if (!input.Current.HasKeyboardFocus) throw new Exception("Composer focus could not be verified");
        var bounds=input.Current.BoundingRectangle;
        var handles=new List<IntPtr>();
        EnumChildWindows(new IntPtr(window.Current.NativeWindowHandle),(handle,data)=>{
            var name=new StringBuilder(128); GetClassName(handle,name,128);
            Rect r;
            if (name.ToString()=="Chrome_RenderWidgetHostHWND" && GetWindowRect(handle,out r)
                && bounds.X+bounds.Width/2>=r.Left && bounds.X+bounds.Width/2<=r.Right
                && bounds.Y+bounds.Height/2>=r.Top && bounds.Y+bounds.Height/2<=r.Bottom) handles.Add(handle);
            return true;
        },IntPtr.Zero);
        if (handles.Count!=1) throw new Exception("Composer renderer is not uniquely identifiable");
        if (!PostMessage(handles[0],0x0102,new IntPtr(32),IntPtr.Zero)) throw new Exception("Renderer input notification failed");
        Thread.Sleep(80);
        if (!input.Current.HasKeyboardFocus) throw new Exception("Composer focus changed; send was not attempted");
        PostMessage(handles[0],0x0102,new IntPtr(8),IntPtr.Zero);
        Thread.Sleep(180);
    }
    static string Text(AutomationElement el) {
        var parts = new List<string>();
        foreach (AutomationElement child in el.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Text))) {
            var text = child.Current.Name;
            if (!String.IsNullOrWhiteSpace(text)) parts.Add(text);
        }
        return String.Join("\n", parts);
    }
    static object Inspect(AutomationElement window, string required) {
        var doc = String.IsNullOrEmpty(required) ? Document(window) : RequireSession(window, required);
        var sessions = new List<object>();
        foreach (AutomationElement el in doc.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Hyperlink))) {
            var id = Session(Value(el));
            if (id != null) sessions.Add(new { id=id, title=el.Current.Name });
        }
        var rounds = Named(doc, "一轮任务");
        var latest = rounds.LastOrDefault() ?? doc;
        var articles = Named(latest, "Agent 回复");
        var statuses = new List<string>();
        foreach (AutomationElement el in latest.FindAll(TreeScope.Descendants, Condition.TrueCondition)) {
            if ((el.Current.Name ?? "").StartsWith("状态：")) statuses.Add(el.Current.Name);
        }
        var summary = articles.Length > 0 ? Text(articles[articles.Length-1]) : "";
        if (summary.Length > 6000) summary = summary.Substring(summary.Length-6000);
        var roundText = Text(latest);
        var marker = Regex.Match(roundText, @"Local Agent Bridge task ([a-f0-9-]{36})\.");
        return new { session_id=Session(Value(doc)), sessions=sessions, replies=articles.Length,
            summary=summary, status=statuses.LastOrDefault(), input_count=Named(doc,"发送任务消息").Length,
            task_id=marker.Success ? marker.Groups[1].Value : null,
            busy=Named(doc,"停止生成").Length > 0 || Named(doc,"停止任务").Length > 0 || Named(doc,"停止").Any(e => e.Current.IsEnabled) };
    }
    static object Select(AutomationElement window, string id) {
        var doc = Document(window);
        if (Session(Value(doc)) == id) return new { selected=true, session_id=id };
        var matches = doc.FindAll(TreeScope.Descendants, new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Hyperlink))
            .Cast<AutomationElement>().Where(e => Session(Value(e)) == id).ToArray();
        if (matches.Length != 1) throw new Exception("Bound Qoder chat is not uniquely visible in the sidebar");
        object pattern;
        if (!matches[0].TryGetCurrentPattern(InvokePattern.Pattern, out pattern)) throw new Exception("Chat link cannot be invoked through accessibility");
        ((InvokePattern)pattern).Invoke();
        for (int i=0;i<40;i++) {
            Thread.Sleep(100);
            try { if (Session(Value(Document(window))) == id) return new {selected=true, session_id=id}; } catch {}
        }
        throw new Exception("Bound Qoder chat did not become selected");
    }
    static object Send(AutomationElement window, string id, string inputFile) {
        var prompt = File.ReadAllText(inputFile, Encoding.UTF8);
        if (prompt.Length == 0 || prompt.Length > 12000) throw new Exception("Invalid prompt size");
        var doc = RequireSession(window, id);
        if (new [] {"停止生成", "停止任务", "停止消息", "停止"}.Any(n => Named(doc,n).Any(e=>e.Current.IsEnabled))) throw new Exception("Qoder chat is busy; task was not sent");
        var input = Named(doc, "发送任务消息");
        if (input.Length != 1) throw new Exception("Expected one chat composer");
        object pattern;
        if (!input[0].TryGetCurrentPattern(ValuePattern.Pattern, out pattern)) throw new Exception("Qoder composer has no accessibility ValuePattern");
        var value = (ValuePattern)pattern;
        if (value.Current.IsReadOnly || !String.IsNullOrEmpty(value.Current.Value)) throw new Exception("Qoder composer is read-only or contains an unsent draft");
        var before = Named(doc,"Agent 回复").Length;
        value.SetValue(prompt);
        Thread.Sleep(150);
        RequireSession(window,id);
        if (Normalize(value.Current.Value) != Normalize(prompt)) throw new Exception("Composer text was not accepted; no send was attempted");
        NotifyEditor(window,input[0]);
        RequireSession(window,id);
        if (Normalize(value.Current.Value)!=Normalize(prompt)) throw new Exception("Composer changed during input notification; draft left for review");
        var buttons=Named(RequireSession(window,id),"发送消息").Where(e=>e.Current.ControlType==ControlType.Button && e.Current.IsEnabled).ToArray();
        if (buttons.Length != 1 || !buttons[0].TryGetCurrentPattern(InvokePattern.Pattern,out pattern)) throw new Exception("Send button is not uniquely available; draft left for review");
        ((InvokePattern)pattern).Invoke();
        return new {sent=true, session_id=id, baseline_replies=before};
    }
    static object ClearOwnedDraft(AutomationElement window,string id,string file) {
        var doc=RequireSession(window,id);
        var input=Named(doc,"发送任务消息");
        object pattern;
        if (input.Length!=1 || !input[0].TryGetCurrentPattern(ValuePattern.Pattern,out pattern)) throw new Exception("Composer unavailable");
        var value=(ValuePattern)pattern;
        if (Normalize(value.Current.Value)!=Normalize(File.ReadAllText(file,Encoding.UTF8))) throw new Exception("Draft differs from our failed delivery; leaving it unchanged");
        value.SetValue("");
        return new {cleared_own_draft=true};
    }
    static object Stop(AutomationElement window, string id) {
        var doc = RequireSession(window,id);
        var buttons = new [] {"停止生成", "停止任务", "停止消息", "停止"}.SelectMany(n=>Named(doc,n))
            .Where(e=>e.Current.ControlType==ControlType.Button && e.Current.IsEnabled).ToArray();
        object pattern;
        if (buttons.Length != 1 || !buttons[0].TryGetCurrentPattern(InvokePattern.Pattern,out pattern)) throw new Exception("No unique stop button; interruption was not performed");
        ((InvokePattern)pattern).Invoke();
        return new {interruption_requested=true, session_id=id};
    }
    [STAThread]
    public static int Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        try {
            var application=Directory.GetParent(Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location)).FullName;
            if (args.Length>0 && args[0]!="inspect" && File.Exists(Path.Combine(application,".bridge","qoder-input-disabled")))
                throw new Exception("Qoder input automation is disabled by user preference");
            var window = Window();
            object result;
            if (args.Length == 0 || args[0] == "inspect") result=Inspect(window,args.Length>1 ? args[1] : null);
            else if (args[0] == "select" && args.Length==2) result=Select(window,args[1]);
            else if (args[0] == "send" && args.Length==3) result=Send(window,args[1],args[2]);
            else if (args[0] == "stop" && args.Length==2) result=Stop(window,args[1]);
            else if (args[0] == "clear-owned-draft" && args.Length==3) result=ClearOwnedDraft(window,args[1],args[2]);
            else throw new Exception("Unsupported desktop action");
            Console.WriteLine(json.Serialize(result));
            return 0;
        } catch(Exception error) { Console.WriteLine(json.Serialize(new {error=error.GetType().Name + ": " + error.Message})); return 1; }
    }
}

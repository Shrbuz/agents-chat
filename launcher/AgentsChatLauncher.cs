using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using System.Web.Script.Serialization;

[assembly: AssemblyTitle("Agents Chat")]
[assembly: AssemblyDescription("Multi-project, multi-role local agent collaboration hub")]
[assembly: AssemblyCompany("Agents Chat")]
[assembly: AssemblyProduct("Agents Chat")]
[assembly: AssemblyVersion("0.7.1.0")]
[assembly: AssemblyFileVersion("0.7.1.0")]

namespace AgentsChatLauncher
{
    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
            try
            {
#if CLI
                // The console entry only runs the Python CLI attached to this console.
                // Python writes UTF-8 (including Chinese task text); tell the
                // console to decode/encode it as UTF-8 instead of the OEM page.
                try
                {
                    Console.OutputEncoding = new UTF8Encoding(false);
                    Console.InputEncoding = new UTF8Encoding(false);
                }
                catch (IOException) { }
                catch (System.Security.SecurityException) { }
                return RunPython(root, "bridge.py", args, false, false);
#else
                if (args.Length > 0 && EqualsCommand(args[0], "uninstall"))
                {
                    try
                    {
                        using (EventWaitHandle quit = EventWaitHandle.OpenExisting(@"Local\AgentsChatQuit05"))
                        {
                            quit.Set();
                            Thread.Sleep(1500);
                        }
                    }
                    catch (WaitHandleCannotBeOpenedException) { }
                    return RunPython(root, "uninstall.py", Slice(args, 1), true, true);
                }
                if (args.Length > 0 && EqualsCommand(args[0], "qoder"))
                {
                    int code = RunPython(root, "launch_qoder_native.py", Slice(args, 1), false, false);
                    if (code != 0)
                        MessageBox.Show("Qoder 原生连接没有修复并启动。\r\n请先完全退出 Qoder（包括托盘图标），再重试修复入口。",
                            "Agents Chat", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return code;
                }
                bool openPanel = args.Length > 0 && (EqualsCommand(args[0], "ui") || EqualsCommand(args[0], "panel"));
                bool openTray = args.Length > 0 && EqualsCommand(args[0], "tray");
                if (openPanel)
                {
                    try
                    {
                        using (EventWaitHandle existing = EventWaitHandle.OpenExisting(@"Local\AgentsChatShow05"))
                        {
                            existing.Set();
                            return 0;
                        }
                    }
                    catch (WaitHandleCannotBeOpenedException) { }
                }
                if (openTray)
                {
                    try
                    {
                        using (EventWaitHandle existing = EventWaitHandle.OpenExisting(@"Local\AgentsChatTrayPanel05"))
                        {
                            existing.Set();
                            return 0;
                        }
                    }
                    catch (WaitHandleCannotBeOpenedException) { }
                }
                bool created;
                using (new Mutex(true, @"Local\AgentsChatTray05", out created))
                {
                    if (!created)
                    {
                        try
                        {
                            using (EventWaitHandle existing = EventWaitHandle.OpenExisting(
                                openTray ? @"Local\AgentsChatTrayPanel05" : @"Local\AgentsChatShow05"))
                                existing.Set();
                        }
                        catch (WaitHandleCannotBeOpenedException)
                        {
                            MessageBox.Show("Agents Chat 已在系统托盘中运行。", "Agents Chat",
                                MessageBoxButtons.OK, MessageBoxIcon.Information);
                        }
                        return 0;
                    }
                    Application.EnableVisualStyles();
                    Application.SetCompatibleTextRenderingDefault(false);
                    using (TrayApp app = new TrayApp(root))
                    {
                        app.Start();
                        if (openPanel)
                            app.OpenPanel();
                        else if (openTray)
                            app.OpenTrayPanel();
                        Application.Run(app);
                    }
                }
                return 0;
#endif
            }
            catch (Exception error)
            {
                string message = "Agents Chat 无法启动：" + error.Message;
                try { Console.Error.WriteLine(message); } catch { }
                try { MessageBox.Show(message, "Agents Chat", MessageBoxButtons.OK, MessageBoxIcon.Error); } catch { }
                return 1;
            }
        }

        internal static bool EqualsCommand(string value, string command)
        {
            return String.Equals(value, command, StringComparison.OrdinalIgnoreCase);
        }

        internal static string[] Slice(string[] values, int start)
        {
            int length = Math.Max(0, values.Length - start);
            string[] result = new string[length];
            Array.Copy(values, start, result, 0, length);
            return result;
        }

        internal static int RunPython(string root, string script, string[] arguments, bool windowed, bool detached)
        {
            string scriptPath = Path.Combine(root, script);
            if (!File.Exists(scriptPath))
                throw new FileNotFoundException("缺少程序组件", scriptPath);
            string python = ResolvePython(root, windowed);
            List<string> command = new List<string>();
            command.Add(scriptPath);
            command.AddRange(arguments);
            if (detached)
            {
                ProcessStartInfo info = new ProcessStartInfo();
                info.FileName = python;
                info.Arguments = JoinArguments(command);
                info.WorkingDirectory = root;
                info.UseShellExecute = false;
                info.CreateNoWindow = true;
                Process process = Process.Start(info);
                if (process == null)
                    throw new InvalidOperationException("无法创建后台进程");
                process.Dispose();
                return 0;
            }
            return RunAttachedPython(python, root, command);
        }

        private static int RunAttachedPython(string python, string root, List<string> command)
        {
            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = python;
            info.Arguments = JoinArguments(command);
            info.WorkingDirectory = root;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardInput = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            Process process = Process.Start(info);
            if (process == null)
                throw new InvalidOperationException("无法创建后台进程");

            Thread output = Relay(process.StandardOutput.BaseStream, Console.OpenStandardOutput(), false);
            Thread error = Relay(process.StandardError.BaseStream, Console.OpenStandardError(), false);
            Relay(Console.OpenStandardInput(), process.StandardInput.BaseStream, true);
            process.WaitForExit();
            output.Join();
            error.Join();
            int exitCode = process.ExitCode;
            process.Dispose();
            return exitCode;
        }

        private static Thread Relay(Stream source, Stream destination, bool closeDestination)
        {
            Thread thread = new Thread(delegate()
            {
                try
                {
                    source.CopyTo(destination);
                    destination.Flush();
                }
                catch (IOException) { }
                catch (ObjectDisposedException) { }
                finally
                {
                    if (closeDestination)
                    {
                        try { destination.Close(); } catch { }
                    }
                }
            });
            thread.IsBackground = true;
            thread.Start();
            return thread;
        }

        internal static string ResolvePython(string root, bool windowed)
        {
            string pointer = Path.Combine(root, "python-runtime.txt");
            string python = null;
            if (File.Exists(pointer))
            {
                python = File.ReadAllText(pointer, Encoding.UTF8).Trim();
                if (python.Length > 0 && !Path.IsPathRooted(python))
                    python = Path.Combine(root, python);
            }
            if (String.IsNullOrEmpty(python) || !File.Exists(python))
            {
                string embedded = Path.Combine(root, "runtime", "python", windowed ? "pythonw.exe" : "python.exe");
                if (File.Exists(embedded))
                    python = embedded;
            }
            if (String.IsNullOrEmpty(python) || !File.Exists(python))
                throw new FileNotFoundException("安装时使用的 Python 已不存在，请重新安装或重新生成便携包",
                    python ?? "python-runtime.txt");
            if (windowed)
            {
                string pythonw = Path.Combine(Path.GetDirectoryName(python), "pythonw.exe");
                if (File.Exists(pythonw))
                    return pythonw;
            }
            return python;
        }

        internal static string JoinArguments(IEnumerable<string> arguments)
        {
            StringBuilder result = new StringBuilder();
            foreach (string argument in arguments)
            {
                if (result.Length > 0) result.Append(' ');
                result.Append(Quote(argument));
            }
            return result.ToString();
        }

        internal static string Quote(string value)
        {
            if (value.Length > 0 && value.IndexOfAny(new char[] { ' ', '\t', '\n', '\v', '"' }) < 0)
                return value;
            StringBuilder result = new StringBuilder("\"");
            int slashes = 0;
            foreach (char character in value)
            {
                if (character == '\\')
                {
                    slashes++;
                    continue;
                }
                if (character == '"')
                {
                    result.Append('\\', slashes * 2 + 1);
                    result.Append('"');
                    slashes = 0;
                    continue;
                }
                result.Append('\\', slashes);
                slashes = 0;
                result.Append(character);
            }
            result.Append('\\', slashes * 2);
            result.Append('"');
            return result.ToString();
        }
    }

    internal sealed class TrayApp : ApplicationContext
    {
        private readonly string root;
        private readonly string configPath;
        private readonly string stateDir;
        private readonly string cliExe;
        private readonly NotifyIcon icon;
        private readonly System.Windows.Forms.Timer timer;
        private readonly object statusLock = new object();
        private Icon trayIcon;
        private TrayVisualState trayVisualState = TrayVisualState.Offline;
        private readonly Form dispatcher = new Form();
        private readonly EventWaitHandle showEvent;
        private readonly EventWaitHandle showTrayEvent;
        private readonly EventWaitHandle quitEvent;
        private StatusForm panel;
        private TrayPopoverForm trayPanel;
        private bool busy;
        private string lastMessage = "";
        private Dictionary<string, object> lastStatus;

        public TrayApp(string root)
        {
            this.root = root;
            configPath = File.Exists(Path.Combine(root, "config.local.json"))
                ? Path.Combine(root, "config.local.json")
                : Path.Combine(root, "config.json");
            stateDir = ResolveState(root, configPath);
            cliExe = Path.Combine(root, "AgentsChat.Cli.exe");
            dispatcher.ShowInTaskbar = false;
            dispatcher.FormBorderStyle = FormBorderStyle.None;
            dispatcher.StartPosition = FormStartPosition.Manual;
            dispatcher.Location = new Point(-32000, -32000);
            IntPtr dispatcherHandle = dispatcher.Handle;
            showEvent = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\AgentsChatShow05");
            Thread watcher = new Thread(delegate()
            {
                try
                {
                    while (showEvent.WaitOne())
                    {
                        try { dispatcher.BeginInvoke(new Action(OpenPanel)); }
                        catch { }
                    }
                }
                catch (ObjectDisposedException) { }
            });
            watcher.IsBackground = true;
            watcher.Start();
            showTrayEvent = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\AgentsChatTrayPanel05");
            Thread trayWatcher = new Thread(delegate()
            {
                try
                {
                    while (showTrayEvent.WaitOne())
                    {
                        try { dispatcher.BeginInvoke(new Action(OpenTrayPanel)); }
                        catch { }
                    }
                }
                catch (ObjectDisposedException) { }
            });
            trayWatcher.IsBackground = true;
            trayWatcher.Start();
            quitEvent = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\AgentsChatQuit05");
            Thread quitWatcher = new Thread(delegate()
            {
                try
                {
                    while (quitEvent.WaitOne())
                    {
                        try { dispatcher.BeginInvoke(new Action(StopHubAndExit)); }
                        catch { }
                    }
                }
                catch (ObjectDisposedException) { }
            });
            quitWatcher.IsBackground = true;
            quitWatcher.Start();

            trayIcon = BuildIcon(TrayVisualState.Offline);
            icon = new NotifyIcon();
            icon.Icon = trayIcon;
            icon.Text = "Agents Chat";
            icon.Visible = true;
            icon.MouseClick += delegate(object sender, MouseEventArgs args)
            {
                if (args.Button == MouseButtons.Left || args.Button == MouseButtons.Right)
                    OpenTrayPanel();
            };
            icon.DoubleClick += delegate
            {
                if (trayPanel != null) trayPanel.Hide();
                OpenPanel();
            };

            timer = new System.Windows.Forms.Timer();
            timer.Interval = 3000;
            timer.Tick += delegate { RefreshAsync(); };
            timer.Start();
        }

        public void Start()
        {
            RefreshAsync();
            EnsureHubAsync();
        }

        private static string ResolveState(string root, string config)
        {
            string value = Environment.GetEnvironmentVariable("AGENT_BRIDGE_STATE");
            if (String.IsNullOrEmpty(value))
                value = Environment.GetEnvironmentVariable("AGENTS_CHAT_STATE");
            if (!String.IsNullOrEmpty(value))
                return Path.GetFullPath(value);
            string preferred = Path.Combine(root, ".bridge");
            string data = Path.Combine(root, "data");
            if (config.EndsWith(".local.json", StringComparison.OrdinalIgnoreCase) || Directory.Exists(preferred))
                return preferred;
            if (Directory.Exists(data))
                return data;
            return preferred;
        }

        private static Icon BuildIcon(TrayVisualState state)
        {
            try { return RelayThemeIcon.Create(state); }
            catch { return SystemIcons.Application; }
        }

        private void OpenFolder(string path)
        {
            try
            {
                if (!Directory.Exists(path)) Directory.CreateDirectory(path);
                Process.Start("explorer.exe", Program.Quote(path));
            }
            catch (Exception error)
            {
                Balloon("无法打开目录", error.Message, ToolTipIcon.Warning);
            }
        }

        private void Balloon(string title, string text, ToolTipIcon kind)
        {
            try
            {
                icon.BalloonTipTitle = title;
                icon.BalloonTipText = text.Length > 240 ? text.Substring(0, 240) : text;
                icon.BalloonTipIcon = kind;
                icon.ShowBalloonTip(4000);
            }
            catch { }
        }

        private void RefreshAsync()
        {
            ThreadPool.QueueUserWorkItem(delegate { Refresh(); });
        }

        private void Refresh()
        {
            lock (statusLock)
            {
                Dictionary<string, object> status = TryStatus();
                lastStatus = status;
                string text;
                bool running = status != null;
                if (!File.Exists(configPath))
                    text = "Agents Chat · 未配置项目（让 Agent 运行 setup）";
                else if (!running)
                    text = "Agents Chat · 后台未运行";
                else
                {
                    Dictionary<string, object> health = Value(status, "health") as Dictionary<string, object>;
                    int projects = health == null ? 0 : Number(health, "projects");
                    int roles = health == null ? 0 : Number(health, "roles");
                    text = "Agents Chat · 运行中 · " + projects + " 项目 / " + roles + " 角色";
                }
                Dictionary<string, object> snapshot = status;
                string message = text;
                bool isRunning = running;
                try
                {
                    dispatcher.BeginInvoke(new Action(delegate { ApplyUi(snapshot, message, isRunning); }));
                }
                catch { }
            }
        }

        private void ApplyUi(Dictionary<string, object> status, string text, bool running)
        {
            if (text != lastMessage)
            {
                lastMessage = text;
                try { icon.Text = text.Length > 63 ? text.Substring(0, 63) : text; } catch { }
            }
            TrayVisualState nextState = VisualState(status, running);
            if (nextState != trayVisualState)
            {
                trayVisualState = nextState;
                Icon nextIcon = BuildIcon(nextState);
                Icon previous = trayIcon;
                trayIcon = nextIcon;
                icon.Icon = nextIcon;
                if (previous != null) previous.Dispose();
            }
            if (panel != null)
                panel.SetStatus(status, configPath, stateDir);
            if (trayPanel != null)
                trayPanel.SetStatus(status, configPath, stateDir);
        }

        private static TrayVisualState VisualState(Dictionary<string, object> status, bool running)
        {
            if (!running || status == null) return TrayVisualState.Offline;
            Dictionary<string, object> health = Value(status, "health");
            if (health != null && health.ContainsKey("degraded") && Convert.ToBoolean(health["degraded"]))
                return TrayVisualState.Warning;
            Dictionary<string, object> states = Value(health, "states");
            if (Number(states, "running") > 0 || Number(states, "queued") > 0)
                return TrayVisualState.Busy;
            return TrayVisualState.Running;
        }

        private Dictionary<string, object> TryStatus()
        {
            try
            {
                string endpointPath = Path.Combine(stateDir, "endpoint.json");
                if (!File.Exists(endpointPath)) return null;
                JavaScriptSerializer serializer = new JavaScriptSerializer();
                Dictionary<string, object> endpoint = serializer.DeserializeObject(File.ReadAllText(endpointPath, Encoding.UTF8)) as Dictionary<string, object>;
                if (endpoint == null) return null;
                object response = Call(endpoint, "status", new Dictionary<string, object>());
                return response as Dictionary<string, object>;
            }
            catch
            {
                return null;
            }
        }

        private static object Call(Dictionary<string, object> endpoint, string method, Dictionary<string, object> parameters)
        {
            JavaScriptSerializer serializer = new JavaScriptSerializer();
            Dictionary<string, object> request = new Dictionary<string, object>();
            request["method"] = method;
            request["params"] = parameters;
            byte[] body = Encoding.UTF8.GetBytes(serializer.Serialize(request));
            HttpWebRequest web = (HttpWebRequest)WebRequest.Create(Convert.ToString(endpoint["url"]) + "/call");
            web.Method = "POST";
            web.ContentType = "application/json";
            web.Headers["Authorization"] = "Bearer " + Convert.ToString(endpoint["token"]);
            web.Proxy = null;
            web.Timeout = 4000;
            web.ReadWriteTimeout = 4000;
            using (Stream stream = web.GetRequestStream())
                stream.Write(body, 0, body.Length);
            using (HttpWebResponse response = (HttpWebResponse)web.GetResponse())
            using (StreamReader reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
            {
                Dictionary<string, object> value = serializer.DeserializeObject(reader.ReadToEnd()) as Dictionary<string, object>;
                if (value == null) throw new InvalidOperationException("invalid broker response");
                object result;
                if (value.TryGetValue("result", out result)) return result;
                throw new InvalidOperationException(Convert.ToString(value["error"]));
            }
        }

        private void EnsureHubAsync()
        {
            ThreadPool.QueueUserWorkItem(delegate
            {
                if (!File.Exists(configPath))
                {
                    Balloon("尚未配置项目", "让 Agent 在项目目录运行 AgentsChat.Cli.exe setup --root <项目路径> --role <角色:适配器>。", ToolTipIcon.Info);
                    return;
                }
                if (TryStatus() != null)
                {
                    Refresh();
                    return;
                }
                RunCli(new string[] { "--state", stateDir, "start", "--config", configPath }, 25000, true);
                Refresh();
            });
        }

        private void ToggleHub()
        {
            if (busy) return;
            busy = true;
            ThreadPool.QueueUserWorkItem(delegate
            {
                try
                {
                    if (TryStatus() != null)
                        RunCli(new string[] { "--state", stateDir, "stop" }, 15000, false);
                    else
                        RunCli(new string[] { "--state", stateDir, "start", "--config", configPath }, 25000, true);
                }
                finally
                {
                    busy = false;
                    Refresh();
                }
            });
        }

        private void StopHubAndExit()
        {
            if (busy) return;
            busy = true;
            ThreadPool.QueueUserWorkItem(delegate
            {
                try { RunCli(new string[] { "--state", stateDir, "stop" }, 15000, false); }
                catch { }
                finally
                {
                    busy = false;
                    try { icon.Visible = false; } catch { }
                    Application.Exit();
                }
            });
        }

        private Tuple<int, string, string> RunCli(string[] arguments, int timeoutMs, bool showErrors)
        {
            if (!File.Exists(cliExe))
                return Tuple.Create(-1, "", "缺少 AgentsChat.Cli.exe");
            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = cliExe;
            info.Arguments = Program.JoinArguments(arguments);
            info.WorkingDirectory = root;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            using (Process process = Process.Start(info))
            {
                if (process == null) return Tuple.Create(-1, "", "无法创建进程");
                StringBuilder output = new StringBuilder();
                StringBuilder error = new StringBuilder();
                Thread outThread = ReadStream(process.StandardOutput, output);
                Thread errThread = ReadStream(process.StandardError, error);
                if (!process.WaitForExit(timeoutMs))
                {
                    try { process.Kill(); } catch { }
                    return Tuple.Create(-1, output.ToString(), "命令超时：" + String.Join(" ", arguments));
                }
                outThread.Join(2000);
                errThread.Join(2000);
                string message = (error.Length > 0 ? error.ToString() : output.ToString()).Trim();
                if (process.ExitCode != 0 && showErrors)
                    Balloon("操作未完成", message.Length > 0 ? message : "请查看数据目录中的日志", ToolTipIcon.Warning);
                return Tuple.Create(process.ExitCode, output.ToString(), error.ToString());
            }
        }

        private static Thread ReadStream(StreamReader reader, StringBuilder destination)
        {
            Thread thread = new Thread(delegate()
            {
                try
                {
                    string line;
                    while ((line = reader.ReadLine()) != null)
                        lock (destination) destination.AppendLine(line);
                }
                catch { }
            });
            thread.IsBackground = true;
            thread.Start();
            return thread;
        }

        internal void OpenPanel()
        {
            if (trayPanel != null) trayPanel.Hide();
            if (panel == null || panel.IsDisposed)
                panel = new StatusForm(
                    delegate { ToggleHub(); },
                    delegate { StopHubAndExit(); },
                    delegate { RefreshAsync(); },
                    delegate (string path) { OpenFolder(path); },
                    delegate (string projectId, string address) { SwitchSession(projectId, address); });
            panel.SetStatus(lastStatus, configPath, stateDir);
            if (!panel.Visible)
                panel.Show();
            else
            {
                if (panel.WindowState == FormWindowState.Minimized)
                    panel.WindowState = FormWindowState.Normal;
                panel.Activate();
            }
        }

        private void OpenPanelForRole(string projectId, string address)
        {
            OpenPanel();
            if (panel != null) panel.SelectRole(projectId, address);
        }

        internal void OpenTrayPanel()
        {
            if (trayPanel == null || trayPanel.IsDisposed)
            {
                trayPanel = new TrayPopoverForm(
                    delegate { OpenPanel(); },
                    delegate { RefreshAsync(); },
                    delegate { ToggleHub(); },
                    delegate { OpenFolder(Path.GetDirectoryName(configPath)); },
                    delegate { OpenFolder(stateDir); },
                    delegate { ExitThread(); },
                    delegate { StopHubAndExit(); },
                    delegate(string projectId, string address) { OpenPanelForRole(projectId, address); });
            }
            trayPanel.SetStatus(lastStatus, configPath, stateDir);
            trayPanel.ShowNearTray(Cursor.Position);
        }

        private Dictionary<string, object> Endpoint()
        {
            string endpointPath = Path.Combine(stateDir, "endpoint.json");
            if (!File.Exists(endpointPath)) return null;
            JavaScriptSerializer serializer = new JavaScriptSerializer();
            return serializer.DeserializeObject(File.ReadAllText(endpointPath, Encoding.UTF8)) as Dictionary<string, object>;
        }

        private void SwitchSession(string projectId, string address)
        {
            Dictionary<string, object> endpoint = Endpoint();
            if (endpoint == null)
            {
                MessageBox.Show("后台没有运行，先启动它。", "切换会话", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            Dictionary<string, object> parameters = new Dictionary<string, object>();
            parameters["project"] = projectId;
            Dictionary<string, object> listed;
            try
            {
                listed = Call(endpoint, "session_list", parameters) as Dictionary<string, object>;
            }
            catch (Exception error)
            {
                MessageBox.Show("读取会话失败：" + error.Message, "切换会话", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            if (listed == null) return;
            List<Dictionary<string, object>> sessions = new List<Dictionary<string, object>>();
            string roleName = address.Substring(address.LastIndexOf('.') + 1);
            foreach (object item in TrayApp.Sequence(listed.ContainsKey("sessions") ? listed["sessions"] : null))
            {
                Dictionary<string, object> session = item as Dictionary<string, object>;
                if (session == null) continue;
                if (Convert.ToString(session["role"]) != roleName) continue;
                sessions.Add(session);
            }
            using (SessionPickerForm picker = new SessionPickerForm(sessions))
            {
                if (picker.ShowDialog() != DialogResult.OK || picker.SelectedSession == null) return;
                Dictionary<string, object> takeover = new Dictionary<string, object>();
                takeover["agent"] = address;
                takeover["project"] = projectId;
                takeover["session_id"] = picker.SelectedSession;
                takeover["transport"] = picker.SelectedTransport;
                try
                {
                    Call(endpoint, "session_takeover", takeover);
                    MessageBox.Show("已把 " + address + " 切换到 " + picker.SelectedSession + "。",
                                    "切换会话", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    RefreshAsync();
                }
                catch (Exception error)
                {
                    MessageBox.Show("切换失败：" + error.Message, "切换会话", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                }
            }
        }
        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                if (timer != null) timer.Dispose();
                if (icon != null) icon.Dispose();
                if (trayIcon != null) trayIcon.Dispose();
                if (panel != null) panel.Dispose();
                if (trayPanel != null) trayPanel.Dispose();
                if (showEvent != null) showEvent.Dispose();
                if (showTrayEvent != null) showTrayEvent.Dispose();
                if (quitEvent != null) quitEvent.Dispose();
            }
            base.Dispose(disposing);
        }

        internal static Dictionary<string, object> Value(Dictionary<string, object> data, string key)
        {
            if (data == null) return null;
            object value;
            return data.TryGetValue(key, out value) ? value as Dictionary<string, object> : null;
        }

        internal static int Number(Dictionary<string, object> data, string key)
        {
            if (data == null) return 0;
            object value;
            if (!data.TryGetValue(key, out value) || value == null) return 0;
            int number;
            return Int32.TryParse(Convert.ToString(value), out number) ? number : 0;
        }

        internal static IEnumerable<object> Sequence(object value)
        {
            IEnumerable enumerable = value as IEnumerable;
            if (enumerable == null || value is string) return new object[0];
            List<object> result = new List<object>();
            foreach (object item in enumerable) result.Add(item);
            return result;
        }
    }

    internal sealed class SessionPickerForm : Form
    {
        public string SelectedSession;
        public string SelectedTransport;

        private readonly ListView list;
        private readonly TextBox manual;
        private readonly ImageList rowHeightImages;

        public SessionPickerForm(List<Dictionary<string, object>> sessions)
        {
            Text = "切换角色会话";
            Width = 760;
            Height = 440;
            MinimumSize = new Size(520, 320);
            StartPosition = FormStartPosition.CenterParent;
            Font = RelayTheme.Font9;
            BackColor = RelayTheme.Window;
            ForeColor = RelayTheme.Text;
            try { Icon = RelayThemeIcon.Create(TrayVisualState.Running); } catch { }

            list = new ListView();
            list.Dock = DockStyle.Fill;
            list.View = View.Details;
            list.FullRowSelect = true;
            list.GridLines = false;
            list.HeaderStyle = ColumnHeaderStyle.Nonclickable;
            list.HideSelection = false;
            list.BackColor = RelayTheme.Surface1;
            list.ForeColor = RelayTheme.Text2;
            list.BorderStyle = BorderStyle.None;
            list.OwnerDraw = true;
            list.DrawColumnHeader += delegate(object sender, DrawListViewColumnHeaderEventArgs args)
            {
                using (Brush brush = new SolidBrush(RelayTheme.Surface2)) args.Graphics.FillRectangle(brush, args.Bounds);
                RelayTheme.TextLine(args.Graphics, args.Header.Text, RelayTheme.Font8Medium, RelayTheme.Text2,
                    new Rectangle(args.Bounds.X + 10, args.Bounds.Y, args.Bounds.Width - 16, args.Bounds.Height),
                    TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
                using (Pen pen = new Pen(RelayTheme.Line)) args.Graphics.DrawLine(pen, args.Bounds.Left, args.Bounds.Bottom - 1, args.Bounds.Right, args.Bounds.Bottom - 1);
            };
            list.DrawItem += delegate { };
            list.DrawSubItem += delegate(object sender, DrawListViewSubItemEventArgs args)
            {
                bool selected = args.Item.Selected;
                using (Brush brush = new SolidBrush(selected ? RelayTheme.Surface3 : RelayTheme.Surface1))
                    args.Graphics.FillRectangle(brush, args.Bounds);
                Color color = args.ColumnIndex == 0 && args.SubItem.Text == "在线" ? RelayTheme.Accent :
                    selected ? RelayTheme.Text : args.Item.ForeColor;
                RelayTheme.TextLine(args.Graphics, args.SubItem.Text, RelayTheme.Font8, color,
                    new Rectangle(args.Bounds.X + 10, args.Bounds.Y, args.Bounds.Width - 16, args.Bounds.Height),
                    TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
                using (Pen pen = new Pen(Color.FromArgb(35, 42, 48))) args.Graphics.DrawLine(pen, args.Bounds.Left, args.Bounds.Bottom - 1, args.Bounds.Right, args.Bounds.Bottom - 1);
            };
            rowHeightImages = new ImageList();
            rowHeightImages.ImageSize = new Size(1, 36);
            list.SmallImageList = rowHeightImages;
            list.Columns.Add("状态", 56);
            list.Columns.Add("会话", 320);
            list.Columns.Add("备注", 190);
            list.Columns.Add("最近活动", 120);
            list.DoubleClick += delegate { Accept(); };
            Controls.Add(list);

            sessions.Sort(delegate(Dictionary<string, object> left, Dictionary<string, object> right)
            {
                bool leftOnline = Convert.ToBoolean(left["online"]);
                bool rightOnline = Convert.ToBoolean(right["online"]);
                if (leftOnline != rightOnline) return leftOnline ? -1 : 1;
                return Convert.ToDouble(right["last_seen"]).CompareTo(Convert.ToDouble(left["last_seen"]));
            });
            foreach (Dictionary<string, object> session in sessions)
            {
                bool online = Convert.ToBoolean(session["online"]);
                string id = Convert.ToString(session["id"]).Replace("desktop:", "");
                ListViewItem row = new ListViewItem(online ? "在线" : "历史");
                row.SubItems.Add(id);
                row.SubItems.Add(Convert.ToString(session["description"]));
                row.SubItems.Add(Stamp(session["last_seen"]));
                row.Tag = session;
                row.ForeColor = online ? RelayTheme.Text2 : RelayTheme.Text3;
                list.Items.Add(row);
            }

            Panel manualRow = new Panel();
            manualRow.Dock = DockStyle.Bottom;
            manualRow.Height = 54;
            manualRow.BackColor = RelayTheme.Window;
            Controls.Add(manualRow);
            Label manualLabel = new Label();
            manualLabel.Text = "或直接填写会话 ID：";
            manualLabel.Left = 16;
            manualLabel.Top = 18;
            manualLabel.Width = 150;
            manualLabel.ForeColor = RelayTheme.Text2;
            manualRow.Controls.Add(manualLabel);
            manual = new TextBox();
            manual.Left = 165;
            manual.Top = 13;
            manual.Width = 550;
            manual.BackColor = RelayTheme.Surface2;
            manual.ForeColor = RelayTheme.Text;
            manual.BorderStyle = BorderStyle.FixedSingle;
            manualRow.Controls.Add(manual);

            Panel buttons = new Panel();
            buttons.Dock = DockStyle.Bottom;
            buttons.Height = 60;
            buttons.BackColor = RelayTheme.Window;
            Controls.Add(buttons);

            Button ok = new Button();
            ok.Text = "切换到这个会话";
            ok.Width = 150;
            ok.Height = 36;
            ok.Left = 16;
            ok.Top = 12;
            ok.FlatStyle = FlatStyle.Flat;
            ok.FlatAppearance.BorderSize = 0;
            ok.BackColor = RelayTheme.Accent;
            ok.ForeColor = RelayTheme.AccentInk;
            ok.Click += delegate { Accept(); };
            buttons.Controls.Add(ok);

            Button cancel = new Button();
            cancel.Text = "取消";
            cancel.Width = 80;
            cancel.Height = 36;
            cancel.Left = 176;
            cancel.Top = 12;
            cancel.FlatStyle = FlatStyle.Flat;
            cancel.FlatAppearance.BorderColor = RelayTheme.LineStrong;
            cancel.BackColor = RelayTheme.Surface2;
            cancel.ForeColor = RelayTheme.Text2;
            cancel.DialogResult = DialogResult.Cancel;
            buttons.Controls.Add(cancel);
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            RelayThemeIcon.UseDarkCaption(Handle);
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing && rowHeightImages != null) rowHeightImages.Dispose();
            base.Dispose(disposing);
        }

        private void Accept()
        {
            string typed = manual == null ? "" : manual.Text.Trim();
            if (typed.Length > 0)
            {
                SelectedSession = typed;
                SelectedTransport = "dsh_plugin";
                DialogResult = DialogResult.OK;
                Close();
                return;
            }
            if (list.SelectedItems.Count == 0) return;
            Dictionary<string, object> session = list.SelectedItems[0].Tag as Dictionary<string, object>;
            if (session == null) return;
            SelectedSession = Convert.ToString(session["id"]).Replace("desktop:", "");
            SelectedTransport = Convert.ToString(session["transport"]);
            DialogResult = DialogResult.OK;
            Close();
        }

        private static string Stamp(object value)
        {
            try
            {
                DateTime time = DateTimeOffset.FromUnixTimeSeconds((long)Convert.ToDouble(value)).ToLocalTime().DateTime;
                return time.ToString("MM-dd HH:mm");
            }
            catch { return ""; }
        }
    }
    internal sealed class LegacyStatusForm : Form
    {
        private readonly ListView list;
        private readonly Label summary;
        private readonly Label paths;
        private readonly Action startStop;
        private readonly Action stopAndExit;
        private readonly Action refresh;
        private readonly Action<string> openFolder;
        private readonly Action<string, string> takeover;

        public LegacyStatusForm(Action startStop, Action stopAndExit, Action refresh, Action<string> openFolder,
                          Action<string, string> takeover)
        {
            this.startStop = startStop;
            this.stopAndExit = stopAndExit;
            this.refresh = refresh;
            this.openFolder = openFolder;
            this.takeover = takeover;
            Text = "Agents Chat · 多项目角色协作";
            Width = 1220;
            Height = 520;
            MinimumSize = new Size(640, 360);
            StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Microsoft YaHei UI", 9F);

            summary = new Label();
            summary.Dock = DockStyle.Top;
            summary.Height = 38;
            summary.Padding = new Padding(12, 10, 12, 4);
            summary.Text = "正在读取状态…";
            Controls.Add(summary);

            list = new ListView();
            list.Dock = DockStyle.Fill;
            list.View = View.Details;
            list.FullRowSelect = true;
            list.GridLines = true;
            list.HeaderStyle = ColumnHeaderStyle.Nonclickable;
            list.HideSelection = false;
            list.ShowItemToolTips = true;
            list.Font = new Font("Segoe UI", 9F);
            list.Columns.Add("项目", 120);
            list.Columns.Add("角色", 90);
            list.Columns.Add("状态", 92);
            list.Columns.Add("适配器", 66);
            list.Columns.Add("传输", 118);
            list.Columns.Add("模式", 128);
            list.Columns.Add("绑定会话", 200);
            list.Columns.Add("队列", 42);
            list.Columns.Add("连续失败", 68);
            list.Columns.Add("最后投递", 108);
            list.Columns.Add("未唤醒结果", 82);
            list.Columns.Add("职责", 260);
            Controls.Add(list);

            Panel buttons = new Panel();
            buttons.Dock = DockStyle.Bottom;
            buttons.Height = 92;
            Controls.Add(buttons);

            // Docking is applied from the highest z-order down, so the Fill list
            // must sit at the bottom of the z-order to receive only the space the
            // Top summary and Bottom buttons leave behind. Otherwise the list is
            // drawn under the summary and its column header is clipped.
            Controls.SetChildIndex(list, 0);

            Button toggle = new Button();
            toggle.Text = "启动 / 停止后台";
            toggle.Width = 140;
            toggle.Left = 12;
            toggle.Top = 10;
            toggle.Click += delegate { this.startStop(); };
            buttons.Controls.Add(toggle);

            Button refreshButton = new Button();
            refreshButton.Text = "刷新";
            refreshButton.Width = 80;
            refreshButton.Left = 162;
            refreshButton.Top = 10;
            refreshButton.Click += delegate { this.refresh(); };
            buttons.Controls.Add(refreshButton);

            Button config = new Button();
            config.Text = "打开配置目录";
            config.Width = 120;
            config.Left = 252;
            config.Top = 10;
            config.Click += delegate { this.openFolder(Path.GetDirectoryName(paths == null ? "" : paths.Tag as string ?? "")); };
            buttons.Controls.Add(config);

            Button switchSession = new Button();
            switchSession.Text = "切换会话…";
            switchSession.Width = 110;
            switchSession.Left = 522;
            switchSession.Top = 10;
            switchSession.Click += delegate
            {
                if (list.SelectedItems.Count == 0)
                {
                    MessageBox.Show(this, "先在列表里选一个角色。", "切换会话", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return;
                }
                string tag = Convert.ToString(list.SelectedItems[0].Tag);
                string[] parts = tag.Split('|');
                if (parts.Length == 2) takeover(parts[0], parts[1]);
            };
            buttons.Controls.Add(switchSession);
            Button quit = new Button();
            quit.Text = "停止后台并退出";
            quit.Width = 140;
            quit.Left = 642;
            quit.Top = 10;
            quit.Click += delegate { this.stopAndExit(); };
            buttons.Controls.Add(quit);

            paths = new Label();
            paths.Left = 12;
            paths.Top = 48;
            paths.Width = 810;
            paths.Height = 36;
            paths.AutoEllipsis = true;
            paths.ForeColor = Color.FromArgb(90, 90, 90);
            buttons.Controls.Add(paths);

            FormClosing += delegate(object sender, FormClosingEventArgs args)
            {
                if (args.CloseReason == CloseReason.UserClosing)
                {
                    args.Cancel = true;
                    Hide();
                }
            };
        }

        public void SetStatus(Dictionary<string, object> status, string configPath, string stateDir)
        {
            if (IsDisposed) return;
            if (InvokeRequired)
            {
                try { BeginInvoke(new Action<Dictionary<string, object>, string, string>(SetStatus), status, configPath, stateDir); }
                catch { }
                return;
            }
            paths.Tag = configPath;
            paths.Text = "配置：" + configPath + "\r\n数据：" + stateDir;
            if (status == null)
            {
                summary.Text = "后台未运行。点击“启动 / 停止后台”启动，或让 Agent 先用 setup 配置项目。";
                list.Items.Clear();
                list.Items.Add(new ListViewItem("（后台未运行）"));
                return;
            }
            Dictionary<string, object> health = TrayApp.Value(status, "health");
            int projects = health == null ? 0 : TrayApp.Number(health, "projects");
            int roles = health == null ? 0 : TrayApp.Number(health, "roles");
            Dictionary<string, object> states = health == null ? null : TrayApp.Value(health, "states");
            bool degraded = health != null && health.ContainsKey("degraded")
                && Convert.ToBoolean(health["degraded"]);
            Dictionary<string, object> transports = health == null ? null : TrayApp.Value(health, "transports");
            int pendingResultWakeups = 0;
            int transportFailures = 0;
            if (transports != null)
            {
                foreach (KeyValuePair<string, object> pair in transports)
                {
                    Dictionary<string, object> item = pair.Value as Dictionary<string, object>;
                    if (item == null) continue;
                    pendingResultWakeups += TrayApp.Number(item, "pending_result_wakeups");
                    transportFailures += TrayApp.Number(item, "consecutive_failures");
                }
            }
            summary.Text = "运行中 · " + projects + " 个项目 / " + roles + " 个角色 · 等待 "
                + (states == null ? 0 : TrayApp.Number(states, "queued")) + " · 执行 "
                + (states == null ? 0 : TrayApp.Number(states, "running"))
                + (health != null && health.ContainsKey("version") ? " · v" + Convert.ToString(health["version"]) : "")
                + (degraded ? " · ⚠ 传输/唤醒链路异常" : "")
                + (transportFailures > 0 ? " · 传输连续失败 " + transportFailures : "")
                + (pendingResultWakeups > 0 ? " · 未送达结果唤醒 " + pendingResultWakeups : "");
            summary.ForeColor = degraded ? Color.Firebrick : SystemColors.ControlText;
            Dictionary<string, object> rolesRoot = TrayApp.Value(status, "roles");
            int mismatchCount = 0;
            list.BeginUpdate();
            try
            {
                list.Items.Clear();
                foreach (object projectObject in TrayApp.Sequence(rolesRoot == null ? null : rolesRoot.ContainsKey("projects") ? rolesRoot["projects"] : null))
                {
                    Dictionary<string, object> project = projectObject as Dictionary<string, object>;
                    if (project == null) continue;
                    foreach (object roleObject in TrayApp.Sequence(project.ContainsKey("roles") ? project["roles"] : null))
                    {
                        Dictionary<string, object> role = roleObject as Dictionary<string, object>;
                        if (role == null) continue;
                        bool mismatch = Convert.ToBoolean(role.ContainsKey("adapter_transport_mismatch")
                            ? role["adapter_transport_mismatch"] : false);
                        string expected = Convert.ToString(role.ContainsKey("expected_adapter")
                            ? role["expected_adapter"] : null);
                        Dictionary<string, object> transportHealth = TrayApp.Value(role, "transport_health");
                        string connectionState = Convert.ToString(role.ContainsKey("connection_status")
                            ? role["connection_status"] : null);
                        ListViewItem row = new ListViewItem(Convert.ToString(project["name"]));
                        row.Tag = Convert.ToString(project["id"]) + "|" + Convert.ToString(role["address"]);
                        row.SubItems.Add(Convert.ToString(role["role"]));
                        row.SubItems.Add(mismatch ? "⚠ 不一致" : ConnectionStateText(connectionState));
                        row.SubItems.Add(Convert.ToString(role["adapter"]));
                        string roleTransport = Convert.ToString(role.ContainsKey("desktop_transport")
                            ? role["desktop_transport"] : null);
                        row.SubItems.Add(string.IsNullOrWhiteSpace(roleTransport) ? "—" : roleTransport);
                        row.SubItems.Add(Convert.ToString(role["mode"]));
                        row.SubItems.Add(BindingText(role));
                        row.SubItems.Add(Convert.ToBoolean(role.ContainsKey("pending") ? role["pending"] : false) ? "有" : "无");
                        row.SubItems.Add(Convert.ToString(transportHealth == null ? 0 : TrayApp.Number(transportHealth, "consecutive_failures")));
                        row.SubItems.Add(LastSuccessText(transportHealth));
                        row.SubItems.Add(Convert.ToString(transportHealth == null ? 0 : TrayApp.Number(transportHealth, "pending_result_wakeups")));
                        row.SubItems.Add(Convert.ToString(role["description"]));
                        if (mismatch)
                        {
                            mismatchCount++;
                            row.ForeColor = Color.Firebrick;
                            row.ToolTipText = "适配器与传输不一致：角色 adapter=" + Convert.ToString(role["adapter"])
                                + "，绑定 transport=" + Convert.ToString(role["desktop_transport"])
                                + "（属于 " + (string.IsNullOrWhiteSpace(expected) ? "未知" : expected) + "）。"
                                + "请用 session migrate-and-takeover 修复。";
                        }
                        else if (!string.IsNullOrWhiteSpace(connectionState)
                                 && connectionState != "ready" && connectionState != "unbound")
                        {
                            if (connectionState != "busy") row.ForeColor = Color.Firebrick;
                            string lastError = Convert.ToString(transportHealth != null && transportHealth.ContainsKey("last_error")
                                ? transportHealth["last_error"] : null);
                            row.ToolTipText = "连接状态：" + ConnectionStateText(connectionState)
                                + (string.IsNullOrWhiteSpace(lastError) ? "" : "；" + lastError);
                        }
                        list.Items.Add(row);
                    }
                }
            }
            finally
            {
                list.EndUpdate();
            }
            if (mismatchCount > 0)
                summary.Text += " · ⚠ " + mismatchCount + " 个角色适配器与传输不一致";
        }

        private static string ConnectionStateText(string state)
        {
            switch (state)
            {
                case "ready": return "正常";
                case "busy": return "忙碌/排队";
                case "reconnecting": return "连接失效/待重连";
                case "pipe_missing": return "Codex 管道失联";
                case "runtime_missing": return "Codex 运行时失效";
                case "app_tools_server_missing": return "App Tools 失效";
                case "connection_context_missing": return "等待连接上下文";
                case "service_unavailable": return "宿主服务不可用";
                case "session_missing": return "宿主会话失效";
                case "auth_required": return "宿主待登录";
                case "entitlement_required": return "套餐无 Kimi Code 权限";
                case "model_not_configured": return "宿主模型未配置";
                case "cli_missing": return "宿主 CLI 缺失";
                case "degraded": return "连接异常";
                case "offline": return "离线";
                case "unbound": return "未绑定";
                default: return string.IsNullOrWhiteSpace(state) ? "正常" : state;
            }
        }

        private static string LastSuccessText(Dictionary<string, object> health)
        {
            if (health == null || !health.ContainsKey("last_success_at") || health["last_success_at"] == null)
                return "—";
            try
            {
                double seconds = Convert.ToDouble(health["last_success_at"]);
                DateTime value = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(seconds).ToLocalTime();
                return value.ToString("MM-dd HH:mm:ss");
            }
            catch { return "—"; }
        }

        private static string ShortId(string value)
        {
            if (string.IsNullOrEmpty(value)) return "";
            return value.Length <= 8 ? value : value.Substring(0, 8) + "…";
        }

        /** Which chat a role is bound to: a readable default plus a short id. */
        private static string BindingText(Dictionary<string, object> role)
        {
            object sessionId = role.ContainsKey("session_id") ? role["session_id"] : null;
            if (sessionId == null || Convert.ToString(sessionId).Length == 0) return "—";
            string id = Convert.ToString(sessionId);
            // The hub resolves the real chat title from the client's own store when
            // it can; that is the name the user recognises.
            string title = Convert.ToString(role.ContainsKey("session_title") ? role["session_title"] : null);
            if (!string.IsNullOrWhiteSpace(title)) return title == id ? id : title + " · " + ShortId(id);
            string label = null;
            foreach (object session in TrayApp.Sequence(role.ContainsKey("sessions") ? role["sessions"] : null))
            {
                Dictionary<string, object> item = session as Dictionary<string, object>;
                if (item == null) continue;
                if (Convert.ToString(item.ContainsKey("id") ? item["id"] : null) != "desktop:" + id) continue;
                string description = Convert.ToString(item.ContainsKey("description") ? item["description"] : null);
                if (!string.IsNullOrWhiteSpace(description) && description != "桌面聊天绑定") label = description;
            }
            // The connectors expose no chat title, so fall back to what the hub
            // already knows: the role and the client it is bound through.
            if (label == null)
            {
                string client = Convert.ToString(role.ContainsKey("desktop_transport") ? role["desktop_transport"] : null);
                if (string.IsNullOrWhiteSpace(client)) client = "desktop";
                label = Convert.ToString(role["role"]) + " · " + client;
            }
            return label + " · " + ShortId(id);
        }

        private static string Pad(string value, int width)
        {
            if (value == null) value = "";
            int visible = 0;
            foreach (char character in value) visible += character > 127 ? 2 : 1;
            if (visible >= width) return value + " ";
            return value + new String(' ', width - visible);
        }
    }
}






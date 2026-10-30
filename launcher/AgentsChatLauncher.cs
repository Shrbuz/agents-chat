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
[assembly: AssemblyVersion("0.5.1.0")]
[assembly: AssemblyFileVersion("0.5.1.0")]

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
                        MessageBox.Show("Qoder 原生连接没有启动。\r\n请先完全退出 Qoder（包括托盘图标），再双击这个快捷方式。",
                            "Agents Chat", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return code;
                }
                bool openPanel = args.Length > 0 && (EqualsCommand(args[0], "ui") || EqualsCommand(args[0], "panel"));
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
                bool created;
                using (new Mutex(true, @"Local\AgentsChatTray05", out created))
                {
                    if (!created)
                    {
                        try
                        {
                            using (EventWaitHandle existing = EventWaitHandle.OpenExisting(@"Local\AgentsChatShow05"))
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
        private readonly ContextMenuStrip menu;
        private readonly ToolStripMenuItem header;
        private readonly ToolStripMenuItem toggle;
        private readonly System.Windows.Forms.Timer timer;
        private readonly object statusLock = new object();
        private readonly Icon trayIcon;
        private readonly Form dispatcher = new Form();
        private readonly EventWaitHandle showEvent;
        private readonly EventWaitHandle quitEvent;
        private StatusForm panel;
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

            menu = new ContextMenuStrip();
            header = new ToolStripMenuItem("Agents Chat · 正在启动…");
            header.Enabled = false;
            menu.Items.Add(header);
            menu.Items.Add(new ToolStripSeparator());
            ToolStripMenuItem open = new ToolStripMenuItem("打开状态窗口…");
            open.Click += delegate { OpenPanel(); };
            menu.Items.Add(open);
            toggle = new ToolStripMenuItem("启动后台");
            toggle.Click += delegate { ToggleHub(); };
            menu.Items.Add(toggle);
            menu.Items.Add(new ToolStripSeparator());
            ToolStripMenuItem openConfig = new ToolStripMenuItem("打开配置目录");
            openConfig.Click += delegate { OpenFolder(Path.GetDirectoryName(configPath)); };
            menu.Items.Add(openConfig);
            ToolStripMenuItem openData = new ToolStripMenuItem("打开数据目录");
            openData.Click += delegate { OpenFolder(stateDir); };
            menu.Items.Add(openData);
            menu.Items.Add(new ToolStripSeparator());
            ToolStripMenuItem quit = new ToolStripMenuItem("退出托盘（后台继续）");
            quit.Click += delegate { ExitThread(); };
            menu.Items.Add(quit);
            ToolStripMenuItem quitAll = new ToolStripMenuItem("停止后台并退出");
            quitAll.Click += delegate { StopHubAndExit(); };
            menu.Items.Add(quitAll);

            trayIcon = BuildIcon();
            icon = new NotifyIcon();
            icon.Icon = trayIcon;
            icon.Text = "Agents Chat";
            icon.Visible = true;
            icon.ContextMenuStrip = menu;
            icon.DoubleClick += delegate { OpenPanel(); };

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

        private static Icon BuildIcon()
        {
            try
            {
                Bitmap bitmap = new Bitmap(32, 32);
                using (Graphics graphics = Graphics.FromImage(bitmap))
                {
                    graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
                    graphics.Clear(Color.Transparent);
                    using (Brush bubble = new SolidBrush(Color.FromArgb(22, 119, 255)))
                        graphics.FillEllipse(bubble, 1, 1, 29, 25);
                    using (Brush dot = new SolidBrush(Color.White))
                    {
                        graphics.FillEllipse(dot, 9, 10, 3, 3);
                        graphics.FillEllipse(dot, 14, 10, 3, 3);
                        graphics.FillEllipse(dot, 19, 10, 3, 3);
                    }
                    Point[] tail = new Point[] { new Point(9, 24), new Point(16, 24), new Point(12, 30) };
                    using (Brush bubble = new SolidBrush(Color.FromArgb(22, 119, 255)))
                        graphics.FillPolygon(bubble, tail);
                }
                IntPtr handle = bitmap.GetHicon();
                try { return (Icon)Icon.FromHandle(handle).Clone(); }
                finally { DestroyIcon(handle); bitmap.Dispose(); }
            }
            catch
            {
                return SystemIcons.Application;
            }
        }

        [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
        private static extern bool DestroyIcon(IntPtr handle);

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
                header.Text = text;
            }
            toggle.Text = running ? "停止后台" : "启动后台";
            if (panel != null)
                panel.SetStatus(status, configPath, stateDir);
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
            if (panel == null || panel.IsDisposed)
                panel = new StatusForm(
                    delegate { ToggleHub(); },
                    delegate { StopHubAndExit(); },
                    delegate { RefreshAsync(); },
                    delegate (string path) { OpenFolder(path); });
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

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                if (timer != null) timer.Dispose();
                if (icon != null) icon.Dispose();
                if (trayIcon != null) trayIcon.Dispose();
                if (menu != null) menu.Dispose();
                if (panel != null) panel.Dispose();
                if (showEvent != null) showEvent.Dispose();
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

    internal sealed class StatusForm : Form
    {
        private readonly TextBox list;
        private readonly Label summary;
        private readonly Label paths;
        private readonly Action startStop;
        private readonly Action stopAndExit;
        private readonly Action refresh;
        private readonly Action<string> openFolder;

        public StatusForm(Action startStop, Action stopAndExit, Action refresh, Action<string> openFolder)
        {
            this.startStop = startStop;
            this.stopAndExit = stopAndExit;
            this.refresh = refresh;
            this.openFolder = openFolder;
            Text = "Agents Chat · 多项目角色协作";
            Width = 860;
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

            list = new TextBox();
            list.Dock = DockStyle.Fill;
            list.Multiline = true;
            list.ReadOnly = true;
            list.WordWrap = false;
            list.ScrollBars = ScrollBars.Vertical;
            list.BackColor = Color.White;
            list.BorderStyle = BorderStyle.FixedSingle;
            list.Font = new Font("Consolas", 9.5F);
            list.Text = "正在读取状态…";
            Controls.Add(list);

            Panel buttons = new Panel();
            buttons.Dock = DockStyle.Bottom;
            buttons.Height = 92;
            Controls.Add(buttons);

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

            Button quit = new Button();
            quit.Text = "停止后台并退出";
            quit.Width = 140;
            quit.Left = 382;
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
                list.Text = "（后台未运行）";
                return;
            }
            Dictionary<string, object> health = TrayApp.Value(status, "health");
            int projects = health == null ? 0 : TrayApp.Number(health, "projects");
            int roles = health == null ? 0 : TrayApp.Number(health, "roles");
            Dictionary<string, object> states = health == null ? null : TrayApp.Value(health, "states");
            summary.Text = "运行中 · " + projects + " 个项目 / " + roles + " 个角色 · 等待 "
                + (states == null ? 0 : TrayApp.Number(states, "queued")) + " · 执行 "
                + (states == null ? 0 : TrayApp.Number(states, "running"));
            StringBuilder text = new StringBuilder();
            text.AppendLine("项目          角色          适配器   模式              会话   队列   职责");
            text.AppendLine(new String('-', 92));
            Dictionary<string, object> rolesRoot = TrayApp.Value(status, "roles");
            foreach (object projectObject in TrayApp.Sequence(rolesRoot == null ? null : rolesRoot.ContainsKey("projects") ? rolesRoot["projects"] : null))
            {
                Dictionary<string, object> project = projectObject as Dictionary<string, object>;
                if (project == null) continue;
                foreach (object roleObject in TrayApp.Sequence(project.ContainsKey("roles") ? project["roles"] : null))
                {
                    Dictionary<string, object> role = roleObject as Dictionary<string, object>;
                    if (role == null) continue;
                    int sessions = 0;
                    foreach (object session in TrayApp.Sequence(role.ContainsKey("sessions") ? role["sessions"] : null))
                    {
                        Dictionary<string, object> item = session as Dictionary<string, object>;
                        if (item != null && Convert.ToBoolean(item.ContainsKey("online") ? item["online"] : false))
                            sessions++;
                    }
                    text.AppendLine(Pad(Convert.ToString(project["name"]), 14)
                        + Pad(Convert.ToString(role["role"]), 14)
                        + Pad(Convert.ToString(role["adapter"]), 9)
                        + Pad(Convert.ToString(role["mode"]), 18)
                        + Pad(sessions.ToString(), 6)
                        + Pad(Convert.ToBoolean(role.ContainsKey("pending") ? role["pending"] : false) ? "有" : "无", 7)
                        + Convert.ToString(role["description"]));
                }
            }
            list.Text = text.ToString();
            list.SelectionStart = 0;
            list.SelectionLength = 0;
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

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace AgentsChatLauncher
{
    internal enum TrayVisualState
    {
        Running,
        Busy,
        Warning,
        Offline
    }

    internal static class RelayTheme
    {
        internal static readonly Color Page = Color.FromArgb(9, 11, 13);
        internal static readonly Color Window = Color.FromArgb(16, 19, 22);
        internal static readonly Color Surface1 = Color.FromArgb(21, 25, 29);
        internal static readonly Color Surface2 = Color.FromArgb(26, 31, 36);
        internal static readonly Color Surface3 = Color.FromArgb(32, 38, 44);
        internal static readonly Color Line = Color.FromArgb(42, 49, 56);
        internal static readonly Color LineStrong = Color.FromArgb(57, 66, 75);
        internal static readonly Color Text = Color.FromArgb(237, 242, 244);
        internal static readonly Color Text2 = Color.FromArgb(169, 179, 186);
        internal static readonly Color Text3 = Color.FromArgb(111, 123, 132);
        internal static readonly Color Accent = Color.FromArgb(99, 221, 208);
        internal static readonly Color Busy = Color.FromArgb(131, 183, 255);
        internal static readonly Color Warning = Color.FromArgb(242, 189, 102);
        internal static readonly Color Bad = Color.FromArgb(240, 124, 120);
        internal static readonly Color Offline = Color.FromArgb(105, 116, 124);
        internal static readonly Color AccentInk = Color.FromArgb(6, 46, 43);

        internal static readonly Font Font10 = NewFont(10F, FontStyle.Regular);
        internal static readonly Font Font10Medium = NewFont(10F, FontStyle.Bold);
        internal static readonly Font Font9 = NewFont(9F, FontStyle.Regular);
        internal static readonly Font Font9Medium = NewFont(9F, FontStyle.Bold);
        internal static readonly Font Font8 = NewFont(8F, FontStyle.Regular);
        internal static readonly Font Font8Medium = NewFont(8F, FontStyle.Bold);
        internal static readonly Font Font14 = NewFont(14F, FontStyle.Bold);
        internal static readonly Font Font18 = NewFont(18F, FontStyle.Bold);
        internal static readonly Font Mono8 = NewMonoFont(8F, FontStyle.Regular);
        internal static readonly Font Mono10 = NewMonoFont(10F, FontStyle.Bold);

        private static Font NewFont(float size, FontStyle style)
        {
            try { return new Font("Microsoft YaHei UI", size, style, GraphicsUnit.Point); }
            catch { return new Font("Segoe UI", size, style, GraphicsUnit.Point); }
        }

        private static Font NewMonoFont(float size, FontStyle style)
        {
            try { return new Font("Cascadia Mono", size, style, GraphicsUnit.Point); }
            catch { return new Font("Consolas", size, style, GraphicsUnit.Point); }
        }

        internal static Color StateColor(TrayVisualState state)
        {
            if (state == TrayVisualState.Busy) return Busy;
            if (state == TrayVisualState.Warning) return Warning;
            if (state == TrayVisualState.Offline) return Offline;
            return Accent;
        }

        internal static GraphicsPath RoundedPath(RectangleF rectangle, float radius)
        {
            GraphicsPath path = new GraphicsPath();
            float diameter = Math.Min(radius * 2F, Math.Min(rectangle.Width, rectangle.Height));
            if (diameter <= 1F)
            {
                path.AddRectangle(rectangle);
                path.CloseFigure();
                return path;
            }
            RectangleF arc = new RectangleF(rectangle.X, rectangle.Y, diameter, diameter);
            path.AddArc(arc, 180, 90);
            arc.X = rectangle.Right - diameter;
            path.AddArc(arc, 270, 90);
            arc.Y = rectangle.Bottom - diameter;
            path.AddArc(arc, 0, 90);
            arc.X = rectangle.X;
            path.AddArc(arc, 90, 90);
            path.CloseFigure();
            return path;
        }

        internal static void FillRounded(Graphics graphics, Brush brush, RectangleF rectangle, float radius)
        {
            using (GraphicsPath path = RoundedPath(rectangle, radius)) graphics.FillPath(brush, path);
        }

        internal static void DrawRounded(Graphics graphics, Pen pen, RectangleF rectangle, float radius)
        {
            using (GraphicsPath path = RoundedPath(rectangle, radius)) graphics.DrawPath(pen, path);
        }

        internal static void Card(Graphics graphics, Rectangle rectangle, float radius, Color fill, Color border)
        {
            using (Brush brush = new SolidBrush(fill)) FillRounded(graphics, brush, rectangle, radius);
            using (Pen pen = new Pen(border, 1F)) DrawRounded(graphics, pen, Inset(rectangle, .5F), radius);
        }

        internal static RectangleF Inset(Rectangle rectangle, float amount)
        {
            return new RectangleF(rectangle.X + amount, rectangle.Y + amount,
                Math.Max(0, rectangle.Width - amount * 2), Math.Max(0, rectangle.Height - amount * 2));
        }

        internal static void TextLine(Graphics graphics, string text, Font font, Color color, Rectangle rectangle,
                                      TextFormatFlags flags)
        {
            TextRenderer.DrawText(graphics, text ?? "", font, rectangle, color,
                flags | TextFormatFlags.NoPrefix | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPadding);
        }

        internal static void Dot(Graphics graphics, PointF center, Color color, float size)
        {
            using (Brush brush = new SolidBrush(color))
                graphics.FillEllipse(brush, center.X - size / 2F, center.Y - size / 2F, size, size);
        }

        internal static void DrawRelayMark(Graphics graphics, RectangleF bounds, Color color, bool fitCanvas)
        {
            const float designSize = 64F;
            const float fittedX = 10F;
            const float fittedY = 6F;
            const float fittedSize = 44F;
            float sourceX = fitCanvas ? fittedX : 0F;
            float sourceY = fitCanvas ? fittedY : 0F;
            float sourceSize = fitCanvas ? fittedSize : designSize;
            float scale = Math.Min(bounds.Width, bounds.Height) / sourceSize;
            GraphicsState state = graphics.Save();
            graphics.TranslateTransform(bounds.X + (bounds.Width - sourceSize * scale) / 2F - sourceX * scale,
                                        bounds.Y + (bounds.Height - sourceSize * scale) / 2F - sourceY * scale);
            graphics.ScaleTransform(scale, scale);
            PointF topLeft = new PointF(18, 18.5F);
            PointF topRight = new PointF(46, 18.5F);
            PointF bottom = new PointF(32, 44);
            PointF center = new PointF(32, 28);
            PointF[] outline = new PointF[] {
                topLeft,
                new PointF(32, 10),
                topRight,
                new PointF(46, 35.3F),
                bottom,
                new PointF(18, 35.3F)
            };
            using (GraphicsPath outlinePath = new GraphicsPath())
            using (Pen pen = new Pen(color, 4F))
            {
                outlinePath.AddPolygon(outline);
                pen.LineJoin = LineJoin.Round;
                graphics.DrawPath(pen, outlinePath);
            }
            using (Brush brush = new SolidBrush(color))
            {
                graphics.FillEllipse(brush, topLeft.X - 5F, topLeft.Y - 5F, 10F, 10F);
                graphics.FillEllipse(brush, topRight.X - 5F, topRight.Y - 5F, 10F, 10F);
                graphics.FillEllipse(brush, bottom.X - 5F, bottom.Y - 5F, 10F, 10F);
                graphics.FillEllipse(brush, center.X - 6.5F, center.Y - 6.5F, 13F, 13F);
            }
            using (Pen cutout = new Pen(Color.FromArgb(15, 19, 22), 3F))
            {
                cutout.StartCap = LineCap.Round;
                cutout.EndCap = LineCap.Round;
                graphics.DrawLine(cutout, new PointF(22.5F, 21.2F), new PointF(27, 24));
                graphics.DrawLine(cutout, new PointF(41.5F, 21.2F), new PointF(37, 24));
                graphics.DrawLine(cutout, new PointF(32, 34.5F), new PointF(32, 39));
            }
            graphics.Restore(state);
        }

        internal static void Glyph(Graphics graphics, string name, Rectangle bounds, Color color)
        {
            graphics.SmoothingMode = SmoothingMode.AntiAlias;
            float cx = bounds.Left + bounds.Width / 2F;
            float cy = bounds.Top + bounds.Height / 2F;
            using (Pen pen = new Pen(color, 1.5F))
            {
                pen.StartCap = LineCap.Round;
                pen.EndCap = LineCap.Round;
                pen.LineJoin = LineJoin.Round;
                if (name == "refresh")
                {
                    graphics.DrawArc(pen, bounds.Left + 3, bounds.Top + 3, bounds.Width - 6, bounds.Height - 6, -35, 250);
                    graphics.DrawLine(pen, bounds.Right - 4, bounds.Top + 5, bounds.Right - 4, bounds.Top + 10);
                    graphics.DrawLine(pen, bounds.Right - 4, bounds.Top + 5, bounds.Right - 9, bounds.Top + 5);
                }
                else if (name == "folder")
                {
                    RectangleF folder = new RectangleF(bounds.Left + 2, bounds.Top + 6, bounds.Width - 4, bounds.Height - 9);
                    graphics.DrawRoundedRectangle(pen, folder, 2F);
                    graphics.DrawLine(pen, bounds.Left + 3, bounds.Top + 6, bounds.Left + 8, bounds.Top + 6);
                    graphics.DrawLine(pen, bounds.Left + 8, bounds.Top + 6, bounds.Left + 10, bounds.Top + 4);
                }
                else if (name == "power")
                {
                    graphics.DrawArc(pen, bounds.Left + 3, bounds.Top + 4, bounds.Width - 6, bounds.Height - 6, -45, 270);
                    graphics.DrawLine(pen, cx, bounds.Top + 2, cx, cy + 2);
                }
                else if (name == "switch")
                {
                    graphics.DrawLine(pen, bounds.Left + 3, cy - 3, bounds.Right - 3, cy - 3);
                    graphics.DrawLine(pen, bounds.Right - 6, cy - 6, bounds.Right - 3, cy - 3);
                    graphics.DrawLine(pen, bounds.Right - 6, cy, bounds.Right - 3, cy - 3);
                    graphics.DrawLine(pen, bounds.Right - 3, cy + 4, bounds.Left + 3, cy + 4);
                    graphics.DrawLine(pen, bounds.Left + 6, cy + 1, bounds.Left + 3, cy + 4);
                    graphics.DrawLine(pen, bounds.Left + 6, cy + 7, bounds.Left + 3, cy + 4);
                }
                else if (name == "copy")
                {
                    graphics.DrawRectangle(pen, bounds.Left + 6, bounds.Top + 3, bounds.Width - 9, bounds.Height - 9);
                    graphics.DrawRectangle(pen, bounds.Left + 3, bounds.Top + 6, bounds.Width - 9, bounds.Height - 9);
                }
                else if (name == "network")
                {
                    graphics.DrawLine(pen, cx, bounds.Top + 4, bounds.Left + 5, bounds.Bottom - 5);
                    graphics.DrawLine(pen, cx, bounds.Top + 4, bounds.Right - 5, bounds.Bottom - 5);
                    graphics.DrawLine(pen, bounds.Left + 5, bounds.Bottom - 5, bounds.Right - 5, bounds.Bottom - 5);
                    Dot(graphics, new PointF(cx, bounds.Top + 4), color, 4);
                    Dot(graphics, new PointF(bounds.Left + 5, bounds.Bottom - 5), color, 4);
                    Dot(graphics, new PointF(bounds.Right - 5, bounds.Bottom - 5), color, 4);
                }
                else
                {
                    Dot(graphics, new PointF(cx - 5, cy), color, 2.4F);
                    Dot(graphics, new PointF(cx, cy), color, 2.4F);
                    Dot(graphics, new PointF(cx + 5, cy), color, 2.4F);
                }
            }
        }

        private static void DrawRoundedRectangle(this Graphics graphics, Pen pen, RectangleF rectangle, float radius)
        {
            using (GraphicsPath path = RoundedPath(rectangle, radius)) graphics.DrawPath(pen, path);
        }
    }

    internal sealed class StatusSnapshot
    {
        internal bool Running;
        internal bool Degraded;
        internal int Queued;
        internal int RunningTasks;
        internal int TransportFailures;
        internal int PendingResultWakeups;
        internal string Version = "";
        internal readonly List<ProjectSnapshot> Projects = new List<ProjectSnapshot>();

        internal IEnumerable<RoleSnapshot> Roles
        {
            get { return Projects.SelectMany(project => project.Roles); }
        }

        internal int TotalRoles { get { return Roles.Count(); } }
        internal int OnlineRoles { get { return Roles.Count(role => role.Online && !role.Mismatch); } }
        internal int BusyRoles { get { return Roles.Count(role => role.IsWorking); } }

        internal static StatusSnapshot From(Dictionary<string, object> status)
        {
            StatusSnapshot snapshot = new StatusSnapshot();
            if (status == null) return snapshot;
            snapshot.Running = true;
            Dictionary<string, object> health = TrayApp.Value(status, "health");
            snapshot.Degraded = Bool(health, "degraded");
            snapshot.Version = StringValue(health, "version");
            Dictionary<string, object> states = TrayApp.Value(health, "states");
            snapshot.Queued = TrayApp.Number(states, "queued");
            snapshot.RunningTasks = TrayApp.Number(states, "running");
            Dictionary<string, object> transports = TrayApp.Value(health, "transports");
            if (transports != null)
            {
                foreach (KeyValuePair<string, object> pair in transports)
                {
                    Dictionary<string, object> item = pair.Value as Dictionary<string, object>;
                    snapshot.TransportFailures += TrayApp.Number(item, "consecutive_failures");
                    snapshot.PendingResultWakeups += TrayApp.Number(item, "pending_result_wakeups");
                }
            }
            Dictionary<string, object> rolesRoot = TrayApp.Value(status, "roles");
            object projectsValue = rolesRoot != null && rolesRoot.ContainsKey("projects") ? rolesRoot["projects"] : null;
            foreach (object projectValue in TrayApp.Sequence(projectsValue))
            {
                Dictionary<string, object> data = projectValue as Dictionary<string, object>;
                if (data == null) continue;
                ProjectSnapshot project = new ProjectSnapshot();
                project.Id = StringValue(data, "id");
                project.Name = StringValue(data, "name");
                if (String.IsNullOrWhiteSpace(project.Name)) project.Name = project.Id;
                object roleValues = data.ContainsKey("roles") ? data["roles"] : null;
                foreach (object roleValue in TrayApp.Sequence(roleValues))
                {
                    Dictionary<string, object> roleData = roleValue as Dictionary<string, object>;
                    if (roleData == null) continue;
                    project.Roles.Add(RoleSnapshot.From(project, roleData));
                }
                snapshot.Projects.Add(project);
            }
            snapshot.Queued = Math.Max(snapshot.Queued, snapshot.Roles.Sum(role => role.QueuedTasks));
            snapshot.RunningTasks = Math.Max(snapshot.RunningTasks, snapshot.Roles.Sum(role => role.RunningTasks));
            return snapshot;
        }

        internal static bool Bool(Dictionary<string, object> data, string key)
        {
            if (data == null || !data.ContainsKey(key) || data[key] == null) return false;
            try { return Convert.ToBoolean(data[key]); }
            catch { return false; }
        }

        internal static string StringValue(Dictionary<string, object> data, string key)
        {
            if (data == null || !data.ContainsKey(key) || data[key] == null) return "";
            return Convert.ToString(data[key]);
        }
    }

    internal sealed class ProjectSnapshot
    {
        internal string Id = "";
        internal string Name = "";
        internal readonly List<RoleSnapshot> Roles = new List<RoleSnapshot>();
    }

    internal sealed class RoleSnapshot
    {
        internal string ProjectId = "";
        internal string ProjectName = "";
        internal string Address = "";
        internal string Role = "";
        internal string Adapter = "";
        internal string Transport = "";
        internal string Mode = "";
        internal string SessionId = "";
        internal string SessionTitle = "";
        internal string Description = "";
        internal string ConnectionState = "";
        internal string LastError = "";
        internal bool Pending;
        internal bool Online;
        internal bool Mismatch;
        internal int QueuedTasks;
        internal int RunningTasks;
        internal int FailureCount;
        internal int PendingResultWakeups;
        internal DateTime? LastSuccess;
        internal string LastSendEventId = "";
        internal string LastReceiveEventId = "";
        internal DateTime? LastSend;
        internal DateTime? LastReceive;
        internal string LastSendKind = "";
        internal string LastReceiveKind = "";
        internal TrayVisualState VisualState;
        internal string StateLabel = "";

        internal bool IsWorking
        {
            get { return RunningTasks > 0 || ConnectionState == "busy"; }
        }

        internal string ShortId
        {
            get
            {
                if (String.IsNullOrWhiteSpace(SessionId)) return "—";
                return SessionId.Length <= 10 ? SessionId : SessionId.Substring(0, 10) + "…";
            }
        }

        internal string Binding
        {
            get
            {
                if (String.IsNullOrWhiteSpace(SessionId)) return "未绑定会话";
                string title = String.IsNullOrWhiteSpace(SessionTitle) ? Role : SessionTitle;
                return title + " · " + ShortId;
            }
        }

        internal static RoleSnapshot From(ProjectSnapshot project, Dictionary<string, object> data)
        {
            RoleSnapshot role = new RoleSnapshot();
            role.ProjectId = project.Id;
            role.ProjectName = project.Name;
            role.Address = StatusSnapshot.StringValue(data, "address");
            role.Role = StatusSnapshot.StringValue(data, "role");
            role.Adapter = StatusSnapshot.StringValue(data, "adapter");
            role.Transport = StatusSnapshot.StringValue(data, "desktop_transport");
            role.Mode = StatusSnapshot.StringValue(data, "mode");
            role.SessionId = StatusSnapshot.StringValue(data, "session_id");
            role.SessionTitle = StatusSnapshot.StringValue(data, "session_title");
            role.Description = StatusSnapshot.StringValue(data, "description");
            role.ConnectionState = StatusSnapshot.StringValue(data, "connection_status");
            role.Pending = StatusSnapshot.Bool(data, "pending");
            role.Online = StatusSnapshot.Bool(data, "online");
            role.Mismatch = StatusSnapshot.Bool(data, "adapter_transport_mismatch");
            role.QueuedTasks = TrayApp.Number(data, "queued_tasks");
            role.RunningTasks = TrayApp.Number(data, "running_tasks");
            role.LastSendEventId = StatusSnapshot.StringValue(data, "last_send_event_id");
            role.LastReceiveEventId = StatusSnapshot.StringValue(data, "last_receive_event_id");
            role.LastSend = TimeValue(data, "last_send_at");
            role.LastReceive = TimeValue(data, "last_receive_at");
            role.LastSendKind = StatusSnapshot.StringValue(data, "last_send_kind");
            role.LastReceiveKind = StatusSnapshot.StringValue(data, "last_receive_kind");
            Dictionary<string, object> transport = TrayApp.Value(data, "transport_health");
            role.FailureCount = TrayApp.Number(transport, "consecutive_failures");
            role.PendingResultWakeups = TrayApp.Number(transport, "pending_result_wakeups");
            role.LastError = StatusSnapshot.StringValue(transport, "last_error");
            role.LastSuccess = TimeValue(transport, "last_success_at");
            role.ResolveState();
            return role;
        }

        private void ResolveState()
        {
            if (Mismatch)
            {
                VisualState = TrayVisualState.Warning;
                StateLabel = "配置不一致";
            }
            else if (FailureCount > 0 || PendingResultWakeups > 0 || IsConnectionError(ConnectionState))
            {
                VisualState = TrayVisualState.Warning;
                StateLabel = ConnectionText(ConnectionState);
            }
            else if (IsWorking)
            {
                VisualState = TrayVisualState.Busy;
                StateLabel = "工作中";
            }
            else if (Online || ConnectionState == "ready")
            {
                VisualState = TrayVisualState.Running;
                StateLabel = QueuedTasks > 0 || Pending ? "待处理" : "在线";
            }
            else
            {
                VisualState = TrayVisualState.Offline;
                StateLabel = ConnectionText(ConnectionState);
            }
        }

        internal static string ConnectionText(string state)
        {
            switch (state)
            {
                case "ready": return "在线";
                case "busy": return "执行中";
                case "reconnecting": return "待重连";
                case "pipe_missing": return "管道失联";
                case "runtime_missing": return "运行时失效";
                case "app_tools_server_missing": return "App Tools 失效";
                case "connection_context_missing": return "等待连接";
                case "service_unavailable": return "服务不可用";
                case "session_missing": return "会话失效";
                case "auth_required": return "等待登录";
                case "cli_missing": return "CLI 缺失";
                case "degraded": return "连接异常";
                case "unbound": return "未绑定";
                case "offline": return "离线";
                default: return String.IsNullOrWhiteSpace(state) ? "离线" : state;
            }
        }

        private static bool IsConnectionError(string state)
        {
            return !String.IsNullOrWhiteSpace(state) && state != "ready" && state != "busy"
                && state != "offline" && state != "unbound";
        }

        private static DateTime? TimeValue(Dictionary<string, object> data, string key)
        {
            if (data == null || !data.ContainsKey(key) || data[key] == null) return null;
            try
            {
                double seconds = Convert.ToDouble(data[key]);
                return new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).AddSeconds(seconds).ToLocalTime();
            }
            catch { return null; }
        }
    }

    internal sealed class StatusForm : Form
    {
        private readonly StatusDashboard dashboard;

        public StatusForm(Action startStop, Action stopAndExit, Action refresh, Action<string> openFolder,
                          Action<string, string> takeover)
        {
            Text = "Agents Chat · 会话状态";
            Width = 1320;
            Height = 800;
            MinimumSize = new Size(1040, 660);
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = RelayTheme.Window;
            Font = RelayTheme.Font9;
            try { Icon = RelayThemeIcon.Create(TrayVisualState.Running); } catch { }
            dashboard = new StatusDashboard(startStop, stopAndExit, refresh, openFolder, takeover);
            dashboard.Dock = DockStyle.Fill;
            Controls.Add(dashboard);
            FormClosing += delegate(object sender, FormClosingEventArgs args)
            {
                if (args.CloseReason == CloseReason.UserClosing)
                {
                    args.Cancel = true;
                    Hide();
                }
            };
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            RelayThemeIcon.UseDarkCaption(Handle);
        }

        public void SetStatus(Dictionary<string, object> status, string configPath, string stateDir)
        {
            dashboard.SetStatus(status, configPath, stateDir);
        }

        public void SelectRole(string projectId, string address)
        {
            dashboard.SelectRole(projectId, address);
        }
    }

    internal static class RelayThemeIcon
    {
        [DllImport("dwmapi.dll")]
        private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);

        internal static Icon Create(TrayVisualState state)
        {
            byte[] data = CreateIcoBytes(state);
            using (MemoryStream stream = new MemoryStream(data))
            using (Icon icon = new Icon(stream))
                return (Icon)icon.Clone();
        }

        internal static byte[] CreateIcoBytes(TrayVisualState state)
        {
            int[] sizes = new int[] { 16, 20, 24, 32, 48, 64 };
            List<byte[]> frames = new List<byte[]>();
            foreach (int size in sizes)
            {
                using (Bitmap bitmap = new Bitmap(size, size, PixelFormat.Format32bppArgb))
                using (Graphics graphics = Graphics.FromImage(bitmap))
                using (MemoryStream frame = new MemoryStream())
                {
                    graphics.SmoothingMode = SmoothingMode.AntiAlias;
                    graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
                    graphics.CompositingQuality = CompositingQuality.HighQuality;
                    graphics.Clear(Color.Transparent);
                    RelayTheme.DrawRelayMark(graphics, new RectangleF(0, 0, size, size), RelayTheme.StateColor(state), true);
                    bitmap.Save(frame, ImageFormat.Png);
                    frames.Add(frame.ToArray());
                }
            }
            using (MemoryStream output = new MemoryStream())
            using (BinaryWriter writer = new BinaryWriter(output))
            {
                writer.Write((ushort)0);
                writer.Write((ushort)1);
                writer.Write((ushort)sizes.Length);
                int offset = 6 + sizes.Length * 16;
                for (int index = 0; index < sizes.Length; index++)
                {
                    writer.Write((byte)sizes[index]);
                    writer.Write((byte)sizes[index]);
                    writer.Write((byte)0);
                    writer.Write((byte)0);
                    writer.Write((ushort)1);
                    writer.Write((ushort)32);
                    writer.Write(frames[index].Length);
                    writer.Write(offset);
                    offset += frames[index].Length;
                }
                foreach (byte[] frame in frames) writer.Write(frame);
                writer.Flush();
                return output.ToArray();
            }
        }

        internal static void UseDarkCaption(IntPtr handle)
        {
            try
            {
                int enabled = 1;
                if (DwmSetWindowAttribute(handle, 20, ref enabled, 4) != 0)
                    DwmSetWindowAttribute(handle, 19, ref enabled, 4);
            }
            catch { }
        }
    }

    internal sealed class HitRegion
    {
        internal Rectangle Bounds;
        internal string Kind = "";
        internal string ProjectId = "";
        internal string Address = "";

        internal HitRegion(Rectangle bounds, string kind)
        {
            Bounds = bounds;
            Kind = kind;
        }
    }

    internal sealed class RouteMotion
    {
        internal string EventId = "";
        internal DateTime StartedUtc = DateTime.MinValue;
    }

    internal sealed class StatusDashboard : Control
    {
        private const double RouteMotionSeconds = 2.2D;
        private readonly Action startStop;
        private readonly Action stopAndExit;
        private readonly Action refresh;
        private readonly Action<string> openFolder;
        private readonly Action<string, string> takeover;
        private readonly Timer animationTimer;
        private readonly List<HitRegion> hits = new List<HitRegion>();
        private readonly Dictionary<string, RouteMotion> sendMotions = new Dictionary<string, RouteMotion>();
        private readonly Dictionary<string, RouteMotion> receiveMotions = new Dictionary<string, RouteMotion>();
        private StatusSnapshot snapshot = new StatusSnapshot();
        private string configPath = "";
        private string stateDir = "";
        private string selectedProject = "";
        private string selectedAddress = "";
        private float phase;
        private string toast = "";
        private DateTime toastUntil;

        internal StatusDashboard(Action startStop, Action stopAndExit, Action refresh,
                                 Action<string> openFolder, Action<string, string> takeover)
        {
            this.startStop = startStop;
            this.stopAndExit = stopAndExit;
            this.refresh = refresh;
            this.openFolder = openFolder;
            this.takeover = takeover;
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint |
                     ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
            BackColor = RelayTheme.Window;
            animationTimer = new Timer();
            animationTimer.Interval = 50;
            animationTimer.Tick += delegate
            {
                phase += .0125F;
                if (phase > 1F) phase -= 1F;
                if (Visible && (snapshot.BusyRoles > 0 || HasActiveRouteMotion() || DateTime.Now < toastUntil))
                    Invalidate();
            };
            animationTimer.Start();
        }

        internal void SetStatus(Dictionary<string, object> status, string config, string state)
        {
            if (IsDisposed) return;
            if (InvokeRequired)
            {
                try { BeginInvoke(new Action<Dictionary<string, object>, string, string>(SetStatus), status, config, state); }
                catch { }
                return;
            }
            StatusSnapshot next = StatusSnapshot.From(status);
            UpdateRouteMotions(next);
            snapshot = next;
            configPath = config ?? "";
            stateDir = state ?? "";
            EnsureSelection();
            Invalidate();
        }

        private void UpdateRouteMotions(StatusSnapshot next)
        {
            DateTime now = DateTime.UtcNow;
            HashSet<string> addresses = new HashSet<string>(next.Roles.Select(role => role.Address));
            foreach (string address in sendMotions.Keys.Where(address => !addresses.Contains(address)).ToList())
                sendMotions.Remove(address);
            foreach (string address in receiveMotions.Keys.Where(address => !addresses.Contains(address)).ToList())
                receiveMotions.Remove(address);
            foreach (RoleSnapshot role in next.Roles)
            {
                UpdateRouteMotion(sendMotions, role.Address, role.LastSendEventId, role.LastSend, now);
                UpdateRouteMotion(receiveMotions, role.Address, role.LastReceiveEventId, role.LastReceive, now);
            }
        }

        private static void UpdateRouteMotion(Dictionary<string, RouteMotion> motions, string address,
                                              string eventId, DateTime? eventTime, DateTime now)
        {
            if (String.IsNullOrWhiteSpace(address) || String.IsNullOrWhiteSpace(eventId)) return;
            RouteMotion motion;
            if (motions.TryGetValue(address, out motion) && motion.EventId == eventId) return;
            motion = new RouteMotion();
            motion.EventId = eventId;
            bool recent = eventTime.HasValue && Math.Abs((now - eventTime.Value.ToUniversalTime()).TotalSeconds) <= 12D;
            motion.StartedUtc = recent ? now : DateTime.MinValue;
            motions[address] = motion;
        }

        private bool HasActiveRouteMotion()
        {
            DateTime now = DateTime.UtcNow;
            return sendMotions.Values.Concat(receiveMotions.Values).Any(motion =>
                motion.StartedUtc != DateTime.MinValue && (now - motion.StartedUtc).TotalSeconds < RouteMotionSeconds);
        }

        internal void SelectRole(string projectId, string address)
        {
            selectedProject = projectId ?? "";
            selectedAddress = address ?? "";
            EnsureSelection();
            Invalidate();
        }

        private void EnsureSelection()
        {
            if (snapshot.Projects.Count == 0)
            {
                selectedProject = "";
                selectedAddress = "";
                return;
            }
            ProjectSnapshot project = snapshot.Projects.FirstOrDefault(item => item.Id == selectedProject);
            if (project == null)
            {
                project = snapshot.Projects[0];
                selectedProject = project.Id;
            }
            RoleSnapshot role = snapshot.Roles.FirstOrDefault(item => item.Address == selectedAddress);
            if (role == null || (selectedProject.Length > 0 && role.ProjectId != selectedProject))
                selectedAddress = project.Roles.Count > 0 ? project.Roles[0].Address : "";
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            Graphics graphics = e.Graphics;
            graphics.SmoothingMode = SmoothingMode.AntiAlias;
            graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
            graphics.Clear(RelayTheme.Page);
            hits.Clear();
            DrawHeader(graphics);
            DrawSidebar(graphics);
            DrawContent(graphics);
            DrawToast(graphics);
        }

        private int SidebarWidth { get { return Width < 1160 ? 200 : 224; } }

        private void DrawHeader(Graphics graphics)
        {
            Rectangle header = new Rectangle(0, 0, Width, 56);
            using (Brush brush = new SolidBrush(Color.FromArgb(17, 21, 24))) graphics.FillRectangle(brush, header);
            using (Pen pen = new Pen(RelayTheme.Line)) graphics.DrawLine(pen, 0, 55, Width, 55);
            RelayTheme.DrawRelayMark(graphics, new RectangleF(16, 12, 32, 32), RelayTheme.StateColor(snapshot.Running ?
                (snapshot.Degraded ? TrayVisualState.Warning : (snapshot.BusyRoles > 0 ? TrayVisualState.Busy : TrayVisualState.Running)) : TrayVisualState.Offline), false);
            RelayTheme.TextLine(graphics, "Agents Chat", RelayTheme.Font10Medium, RelayTheme.Text,
                new Rectangle(56, 10, SidebarWidth - 64, 20), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            RelayTheme.TextLine(graphics, "本地中继", RelayTheme.Mono8, RelayTheme.Text3,
                new Rectangle(56, 30, SidebarWidth - 64, 16), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            using (Pen pen = new Pen(RelayTheme.Line)) graphics.DrawLine(pen, SidebarWidth, 0, SidebarWidth, 56);
            RelayTheme.TextLine(graphics, "会话状态", RelayTheme.Font10Medium, RelayTheme.Text,
                new Rectangle(SidebarWidth + 20, 0, 100, 56), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            Color stateColor = snapshot.Running ? (snapshot.Degraded ? RelayTheme.Warning : RelayTheme.Accent) : RelayTheme.Offline;
            RelayTheme.Dot(graphics, new PointF(SidebarWidth + 132, 28), stateColor, 7);
            RelayTheme.TextLine(graphics, snapshot.Running ? (snapshot.Degraded ? "中枢需留意" : "中枢运行中") : "后台未运行",
                RelayTheme.Font8, RelayTheme.Text2, new Rectangle(SidebarWidth + 142, 0, 150, 56),
                TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            if (!String.IsNullOrWhiteSpace(snapshot.Version))
                RelayTheme.TextLine(graphics, "v" + snapshot.Version, RelayTheme.Mono8, RelayTheme.Text3,
                    new Rectangle(Width - 90, 0, 70, 56), TextFormatFlags.Right | TextFormatFlags.VerticalCenter);
        }

        private void DrawSidebar(Graphics graphics)
        {
            Rectangle sidebar = new Rectangle(0, 56, SidebarWidth, Height - 56);
            using (Brush brush = new SolidBrush(Color.FromArgb(17, 21, 24))) graphics.FillRectangle(brush, sidebar);
            using (Pen pen = new Pen(RelayTheme.Line)) graphics.DrawLine(pen, SidebarWidth, 56, SidebarWidth, Height);
            RelayTheme.TextLine(graphics, "项目", RelayTheme.Font8, RelayTheme.Text3,
                new Rectangle(16, 72, SidebarWidth - 32, 18), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            int y = 96;
            foreach (ProjectSnapshot project in snapshot.Projects)
            {
                DrawProjectRow(graphics, new Rectangle(12, y, SidebarWidth - 24, 36), project.Id, project.Name,
                               project.Roles.Count, project.Id == selectedProject);
                y += 40;
                if (y > Height - 250) break;
            }

            Rectangle health = new Rectangle(12, Height - 170, SidebarWidth - 24, 82);
            RelayTheme.Card(graphics, health, 12, RelayTheme.Surface1, RelayTheme.Line);
            RelayTheme.TextLine(graphics, "中继健康度", RelayTheme.Font8Medium, RelayTheme.Text,
                new Rectangle(health.X + 12, health.Y + 10, health.Width - 24, 18), TextFormatFlags.Left);
            Color healthColor = !snapshot.Running ? RelayTheme.Offline : snapshot.Degraded ? RelayTheme.Warning : RelayTheme.Accent;
            RelayTheme.Dot(graphics, new PointF(health.Right - 16, health.Y + 19), healthColor, 7);
            string healthText = !snapshot.Running ? "后台未运行" : snapshot.Degraded
                ? AttentionText() : snapshot.OnlineRoles + " 个角色在线，链路正常";
            RelayTheme.TextLine(graphics, healthText, RelayTheme.Font8, RelayTheme.Text3,
                new Rectangle(health.X + 12, health.Y + 34, health.Width - 24, 34),
                TextFormatFlags.Left | TextFormatFlags.WordBreak);

            int buttonY = Height - 68;
            int buttonWidth = (SidebarWidth - 40) / 3;
            DrawSmallAction(graphics, new Rectangle(12, buttonY, buttonWidth, 36), "power", snapshot.Running ? "停止" : "启动", "power");
            DrawSmallAction(graphics, new Rectangle(16 + buttonWidth, buttonY, buttonWidth, 36), "refresh", "刷新", "refresh");
            DrawSmallAction(graphics, new Rectangle(20 + buttonWidth * 2, buttonY, buttonWidth, 36), "folder", "配置", "config");
            RelayTheme.TextLine(graphics, snapshot.Version.Length > 0 ? "v" + snapshot.Version : "Agents Chat",
                RelayTheme.Mono8, RelayTheme.Text3, new Rectangle(16, Height - 28, SidebarWidth - 32, 16), TextFormatFlags.Left);
        }

        private string AttentionText()
        {
            List<string> messages = new List<string>();
            if (snapshot.TransportFailures > 0) messages.Add(snapshot.TransportFailures + " 次连续失败");
            if (snapshot.PendingResultWakeups > 0) messages.Add(snapshot.PendingResultWakeups + " 个结果待唤醒");
            int offline = snapshot.TotalRoles - snapshot.OnlineRoles;
            if (offline > 0) messages.Add(offline + " 个角色离线");
            return messages.Count == 0 ? "传输或唤醒链路异常" : String.Join("，", messages.ToArray());
        }

        private void DrawProjectRow(Graphics graphics, Rectangle bounds, string projectId, string name, int count, bool selected)
        {
            if (selected) RelayTheme.Card(graphics, bounds, 9, RelayTheme.Surface2, RelayTheme.Line);
            RelayTheme.Glyph(graphics, projectId.Length == 0 ? "network" : "project", new Rectangle(bounds.X + 10, bounds.Y + 10, 16, 16),
                             selected ? RelayTheme.Text : RelayTheme.Text3);
            RelayTheme.TextLine(graphics, name, RelayTheme.Font9, selected ? RelayTheme.Text : RelayTheme.Text2,
                new Rectangle(bounds.X + 36, bounds.Y, bounds.Width - 70, bounds.Height), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            RelayTheme.TextLine(graphics, Convert.ToString(count), RelayTheme.Mono8, RelayTheme.Text3,
                new Rectangle(bounds.Right - 30, bounds.Y, 20, bounds.Height), TextFormatFlags.Right | TextFormatFlags.VerticalCenter);
            HitRegion hit = new HitRegion(bounds, "project");
            hit.ProjectId = projectId;
            hits.Add(hit);
        }

        private void DrawSmallAction(Graphics graphics, Rectangle bounds, string glyph, string label, string kind)
        {
            RelayTheme.Card(graphics, bounds, 8, RelayTheme.Surface1, RelayTheme.Line);
            RelayTheme.Glyph(graphics, glyph, new Rectangle(bounds.X + 8, bounds.Y + 10, 16, 16), RelayTheme.Text3);
            RelayTheme.TextLine(graphics, label, RelayTheme.Font8, RelayTheme.Text2,
                new Rectangle(bounds.X + 28, bounds.Y, bounds.Width - 32, bounds.Height), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            hits.Add(new HitRegion(bounds, kind));
        }

        private void DrawContent(Graphics graphics)
        {
            int padding = 16;
            int rightWidth = Width < 1180 ? 264 : 292;
            int contentX = SidebarWidth + padding;
            int contentWidth = Width - contentX - padding;
            int summaryY = 72;
            int summaryHeight = 72;
            int gap = 12;
            int metricWidth = (contentWidth - gap * 3) / 4;
            DrawMetric(graphics, new Rectangle(contentX, summaryY, metricWidth, summaryHeight), "在线角色",
                       Convert.ToString(snapshot.OnlineRoles), snapshot.TotalRoles + " 个角色", true);
            DrawMetric(graphics, new Rectangle(contentX + (metricWidth + gap), summaryY, metricWidth, summaryHeight), "执行中",
                       Convert.ToString(Math.Max(snapshot.BusyRoles, snapshot.RunningTasks)), BusyRoleName(), false);
            DrawMetric(graphics, new Rectangle(contentX + (metricWidth + gap) * 2, summaryY, metricWidth, summaryHeight), "等待任务",
                       Convert.ToString(snapshot.Queued), snapshot.Queued == 0 ? "队列已清空" : "等待分配", false);
            DrawMetric(graphics, new Rectangle(contentX + (metricWidth + gap) * 3, summaryY, metricWidth, summaryHeight), "链路状态",
                       snapshot.Degraded ? "注意" : snapshot.Running ? "正常" : "离线",
                       snapshot.TransportFailures > 0 ? snapshot.TransportFailures + " 次失败" : "最近轮询", false);

            int mainY = summaryY + summaryHeight + 16;
            int detailX = Width - padding - rightWidth;
            int centerWidth = detailX - gap - contentX;
            int activityHeight = 142;
            int topologyHeight = Height - mainY - padding - activityHeight - gap;
            if (topologyHeight < 280) topologyHeight = 280;
            Rectangle topology = new Rectangle(contentX, mainY, centerWidth, topologyHeight);
            Rectangle activity = new Rectangle(contentX, topology.Bottom + gap, centerWidth,
                Math.Max(96, Height - padding - (topology.Bottom + gap)));
            Rectangle detail = new Rectangle(detailX, mainY, rightWidth, Height - mainY - padding);
            DrawTopology(graphics, topology);
            DrawActivity(graphics, activity);
            DrawDetail(graphics, detail);
        }

        private string BusyRoleName()
        {
            RoleSnapshot busy = snapshot.Roles.FirstOrDefault(role => role.IsWorking);
            return busy == null ? "无活动会话" : busy.Role;
        }

        private void DrawMetric(Graphics graphics, Rectangle bounds, string label, string value, string note, bool accent)
        {
            RelayTheme.Card(graphics, bounds, 10, RelayTheme.Surface1, RelayTheme.Line);
            RelayTheme.TextLine(graphics, label, RelayTheme.Font8, RelayTheme.Text3,
                new Rectangle(bounds.X + 12, bounds.Y + 10, bounds.Width - 24, 16), TextFormatFlags.Left);
            RelayTheme.TextLine(graphics, value, RelayTheme.Font18, accent ? RelayTheme.Accent : RelayTheme.Text,
                new Rectangle(bounds.X + 12, bounds.Y + 30, bounds.Width / 2, 30), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            RelayTheme.TextLine(graphics, note, RelayTheme.Font8, RelayTheme.Text3,
                new Rectangle(bounds.X + bounds.Width / 2 - 4, bounds.Y + 34, bounds.Width / 2 - 8, 22),
                TextFormatFlags.Right | TextFormatFlags.VerticalCenter);
        }

        private List<RoleSnapshot> VisibleRoles()
        {
            IEnumerable<RoleSnapshot> roles = selectedProject.Length == 0 ? snapshot.Roles :
                snapshot.Projects.Where(project => project.Id == selectedProject).SelectMany(project => project.Roles);
            return roles.OrderBy(role => role.IsWorking ? 0 : role.VisualState == TrayVisualState.Warning ? 1 : role.VisualState == TrayVisualState.Running ? 2 : 3)
                .ThenBy(role => role.Role).Take(6).ToList();
        }

        private void DrawTopology(Graphics graphics, Rectangle bounds)
        {
            RelayTheme.Card(graphics, bounds, 14, RelayTheme.Surface1, RelayTheme.Line);
            RelayTheme.Glyph(graphics, "network", new Rectangle(bounds.X + 14, bounds.Y + 14, 18, 18), RelayTheme.Text2);
            RelayTheme.TextLine(graphics, "实时会话拓扑", RelayTheme.Font9Medium, RelayTheme.Text,
                new Rectangle(bounds.X + 40, bounds.Y, 132, 44), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            string projectName = selectedProject.Length == 0 ? "全部会话" :
                snapshot.Projects.Where(project => project.Id == selectedProject).Select(project => project.Name).FirstOrDefault() ?? selectedProject;
            RelayTheme.TextLine(graphics, projectName, RelayTheme.Mono8, RelayTheme.Text3,
                new Rectangle(bounds.X + 170, bounds.Y, bounds.Width - 186, 44), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            using (Pen pen = new Pen(RelayTheme.Line)) graphics.DrawLine(pen, bounds.X, bounds.Y + 44, bounds.Right, bounds.Y + 44);
            Rectangle stage = new Rectangle(bounds.X + 12, bounds.Y + 52, bounds.Width - 24, bounds.Height - 64);
            using (Pen grid = new Pen(Color.FromArgb(30, 36, 41)))
            {
                for (int x = stage.Left + stage.Width / 4; x < stage.Right; x += Math.Max(1, stage.Width / 4))
                    graphics.DrawLine(grid, x, stage.Top, x, stage.Bottom);
                for (int y = stage.Top + stage.Height / 3; y < stage.Bottom; y += Math.Max(1, stage.Height / 3))
                    graphics.DrawLine(grid, stage.Left, y, stage.Right, y);
            }
            List<RoleSnapshot> roles = VisibleRoles();
            if (roles.Count == 0)
            {
                RelayTheme.TextLine(graphics, snapshot.Running ? "这个项目还没有角色" : "启动后台后显示会话拓扑",
                    RelayTheme.Font10, RelayTheme.Text3, stage, TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
                return;
            }
            int nodeWidth = Math.Min(142, Math.Max(116, stage.Width / 3));
            int nodeHeight = 54;
            Rectangle[] anchors = NodeAnchors(stage, nodeWidth, nodeHeight);
            PointF hub = new PointF(stage.Left + stage.Width / 2F, stage.Top + stage.Height / 2F);
            for (int index = 0; index < roles.Count; index++)
            {
                Rectangle node = anchors[index];
                bool left = node.Left < hub.X;
                float nodeEdge = left ? node.Right : node.Left;
                float hubEdge = left ? hub.X - 28F : hub.X + 28F;
                PointF sendStart = new PointF(nodeEdge, node.Top + node.Height / 2F - 5F);
                PointF sendEnd = new PointF(hubEdge, hub.Y - 5F);
                PointF receiveRole = new PointF(nodeEdge, node.Top + node.Height / 2F + 5F);
                PointF receiveHub = new PointF(hubEdge, hub.Y + 5F);
                PointF[] sendCurve = RouteCurve(sendStart, sendEnd);
                PointF[] receiveRoleCurve = RouteCurve(receiveRole, receiveHub);
                Color routeColor = roles[index].VisualState == TrayVisualState.Warning
                    ? Color.FromArgb(116, 92, 53) : roles[index].VisualState == TrayVisualState.Busy
                    ? Color.FromArgb(67, 111, 130) : Color.FromArgb(47, 58, 65);
                float sendProgress;
                bool sending = TryRouteMotion(sendMotions, roles[index].Address, out sendProgress);
                DrawRoute(graphics, sendCurve[0], sendCurve[1], sendCurve[2], sendCurve[3],
                          routeColor, sending, sendProgress);
                float receiveProgress;
                bool receiving = TryRouteMotion(receiveMotions, roles[index].Address, out receiveProgress);
                DrawRoute(graphics, receiveRoleCurve[3], receiveRoleCurve[2], receiveRoleCurve[1], receiveRoleCurve[0],
                          routeColor, receiving, receiveProgress);
            }
            using (Brush hubBrush = new SolidBrush(Color.FromArgb(19, 29, 31))) graphics.FillEllipse(hubBrush, hub.X - 38, hub.Y - 38, 76, 76);
            using (Pen hubPen = new Pen(RelayTheme.Accent, 1.4F)) graphics.DrawEllipse(hubPen, hub.X - 28, hub.Y - 28, 56, 56);
            RelayTheme.DrawRelayMark(graphics, new RectangleF(hub.X - 17, hub.Y - 17, 34, 34), RelayTheme.Accent, false);
            RelayTheme.TextLine(graphics, "BROKER", RelayTheme.Mono8, RelayTheme.Text3,
                new Rectangle((int)hub.X - 50, (int)hub.Y + 42, 100, 16), TextFormatFlags.HorizontalCenter);
            for (int index = 0; index < roles.Count; index++) DrawNode(graphics, anchors[index], roles[index]);
        }

        private static PointF[] RouteCurve(PointF role, PointF hub)
        {
            float dx = hub.X - role.X;
            return new PointF[] {
                role,
                new PointF(role.X + dx * .44F, role.Y),
                new PointF(hub.X - dx * .22F, hub.Y),
                hub
            };
        }

        private bool TryRouteMotion(Dictionary<string, RouteMotion> motions, string address, out float progress)
        {
            progress = 0F;
            RouteMotion motion;
            if (!motions.TryGetValue(address, out motion) || motion.StartedUtc == DateTime.MinValue) return false;
            double elapsed = (DateTime.UtcNow - motion.StartedUtc).TotalSeconds;
            if (elapsed < 0D || elapsed >= RouteMotionSeconds) return false;
            progress = (float)(elapsed / RouteMotionSeconds);
            return true;
        }

        private static void DrawRoute(Graphics graphics, PointF p0, PointF p1, PointF p2, PointF p3,
                                      Color baseColor, bool active, float progress)
        {
            Color lineColor = active ? Color.FromArgb(150, RelayTheme.Accent) : baseColor;
            using (Pen route = new Pen(lineColor, active ? 1.55F : 1.1F))
            {
                route.StartCap = LineCap.Round;
                route.EndCap = LineCap.Round;
                route.DashStyle = DashStyle.Dash;
                route.DashPattern = active ? new float[] { 3F, 3F } : new float[] { 2F, 5F };
                graphics.DrawBezier(route, p0, p1, p2, p3);
            }
            DrawDirectionArrow(graphics, p0, p1, p2, p3, .68F, lineColor);
            if (!active) return;
            DrawPacket(graphics, p0, p1, p2, p3, progress);
        }

        private static void DrawDirectionArrow(Graphics graphics, PointF p0, PointF p1, PointF p2, PointF p3,
                                               float t, Color color)
        {
            PointF tip = Bezier(p0, p1, p2, p3, t);
            PointF tangent = BezierTangent(p0, p1, p2, p3, t);
            float length = (float)Math.Sqrt(tangent.X * tangent.X + tangent.Y * tangent.Y);
            if (length < .01F) return;
            float ux = tangent.X / length;
            float uy = tangent.Y / length;
            PointF back = new PointF(tip.X - ux * 5F, tip.Y - uy * 5F);
            PointF a = new PointF(back.X - uy * 2.6F, back.Y + ux * 2.6F);
            PointF b = new PointF(back.X + uy * 2.6F, back.Y - ux * 2.6F);
            using (Pen arrow = new Pen(color, 1.2F))
            {
                arrow.StartCap = LineCap.Round;
                arrow.EndCap = LineCap.Round;
                graphics.DrawLine(arrow, tip, a);
                graphics.DrawLine(arrow, tip, b);
            }
        }

        private static void DrawPacket(Graphics graphics, PointF p0, PointF p1, PointF p2, PointF p3, float progress)
        {
            float[] offsets = new float[] { 0F, .035F, .07F };
            float[] sizes = new float[] { 6F, 4F, 2.6F };
            int[] alphas = new int[] { 255, 135, 65 };
            for (int index = 0; index < offsets.Length; index++)
            {
                float local = progress - offsets[index];
                if (local < 0F) continue;
                PointF point = Bezier(p0, p1, p2, p3, local);
                using (Brush brush = new SolidBrush(Color.FromArgb(alphas[index], RelayTheme.Accent)))
                    graphics.FillEllipse(brush, point.X - sizes[index] / 2F, point.Y - sizes[index] / 2F,
                                         sizes[index], sizes[index]);
            }
        }

        private static PointF Bezier(PointF p0, PointF p1, PointF p2, PointF p3, float t)
        {
            float u = 1F - t;
            float x = u * u * u * p0.X + 3F * u * u * t * p1.X + 3F * u * t * t * p2.X + t * t * t * p3.X;
            float y = u * u * u * p0.Y + 3F * u * u * t * p1.Y + 3F * u * t * t * p2.Y + t * t * t * p3.Y;
            return new PointF(x, y);
        }

        private static PointF BezierTangent(PointF p0, PointF p1, PointF p2, PointF p3, float t)
        {
            float u = 1F - t;
            float x = 3F * u * u * (p1.X - p0.X) + 6F * u * t * (p2.X - p1.X) +
                      3F * t * t * (p3.X - p2.X);
            float y = 3F * u * u * (p1.Y - p0.Y) + 6F * u * t * (p2.Y - p1.Y) +
                      3F * t * t * (p3.Y - p2.Y);
            return new PointF(x, y);
        }

        private static Rectangle[] NodeAnchors(Rectangle stage, int width, int height)
        {
            int left = stage.Left + 8;
            int right = stage.Right - width - 8;
            int top = stage.Top + 12;
            int middle = stage.Top + (stage.Height - height) / 2;
            int bottom = stage.Bottom - height - 12;
            return new Rectangle[] {
                new Rectangle(left, top, width, height), new Rectangle(right, top, width, height),
                new Rectangle(left, middle, width, height), new Rectangle(right, middle, width, height),
                new Rectangle(left, bottom, width, height), new Rectangle(right, bottom, width, height)
            };
        }

        private void DrawNode(Graphics graphics, Rectangle bounds, RoleSnapshot role)
        {
            bool selected = role.Address == selectedAddress;
            RelayTheme.Card(graphics, bounds, 10, selected ? Color.FromArgb(24, 33, 32) : Color.FromArgb(22, 27, 31),
                            selected ? Color.FromArgb(67, 106, 102) : role.IsWorking
                            ? Color.FromArgb(55, 91, 105) : RelayTheme.Line);
            if (role.IsWorking) DrawMovingCardBorder(graphics, bounds, 10F, phase, RelayTheme.Busy);
            Rectangle avatar = new Rectangle(bounds.X + 8, bounds.Y + 10, 32, 32);
            RelayTheme.Card(graphics, avatar, 8, RelayTheme.Surface3, RelayTheme.LineStrong);
            string initials = role.Role.Length <= 2 ? role.Role.ToUpperInvariant() : role.Role.Substring(0, 2).ToUpperInvariant();
            RelayTheme.TextLine(graphics, initials, RelayTheme.Mono8, RelayTheme.Text2, avatar,
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
            RelayTheme.Dot(graphics, new PointF(avatar.Right - 1, avatar.Bottom - 1), RelayTheme.StateColor(role.VisualState), 6);
            RelayTheme.TextLine(graphics, role.Role, RelayTheme.Font8Medium, RelayTheme.Text,
                new Rectangle(bounds.X + 50, bounds.Y + 9, bounds.Width - 58, 18), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            string state = role.StateLabel;
            if (role.IsWorking && role.RunningTasks > 1) state += " · " + role.RunningTasks + " 个任务";
            else if (!role.IsWorking && role.QueuedTasks > 0) state += " · " + role.QueuedTasks + " 待处理";
            RelayTheme.TextLine(graphics, state, RelayTheme.Font8,
                RelayTheme.Text3, new Rectangle(bounds.X + 50, bounds.Y + 28, bounds.Width - 58, 17),
                TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            HitRegion hit = new HitRegion(bounds, "role");
            hit.ProjectId = role.ProjectId;
            hit.Address = role.Address;
            hits.Add(hit);
        }

        private static void DrawMovingCardBorder(Graphics graphics, Rectangle bounds, float radius,
                                                 float progress, Color color)
        {
            RectangleF inset = new RectangleF(bounds.X + .8F, bounds.Y + .8F,
                                               bounds.Width - 1.6F, bounds.Height - 1.6F);
            List<PointF> points = new List<PointF>();
            using (GraphicsPath outline = RelayTheme.RoundedPath(inset, radius))
            {
                outline.Flatten(null, .4F);
                points.AddRange(outline.PathPoints);
            }
            if (points.Count < 2) return;
            PointF first = points[0];
            PointF last = points[points.Count - 1];
            if (Math.Abs(first.X - last.X) > .1F || Math.Abs(first.Y - last.Y) > .1F)
                points.Add(first);
            float total = 0F;
            for (int index = 0; index < points.Count - 1; index++)
                total += Distance(points[index], points[index + 1]);
            if (total < 1F) return;
            float start = (progress - (float)Math.Floor(progress)) * total;
            float span = Math.Min(52F, total * .16F);
            using (Pen pen = new Pen(color, 2F))
            {
                pen.StartCap = LineCap.Round;
                pen.EndCap = LineCap.Round;
                pen.LineJoin = LineJoin.Round;
                float end = start + span;
                DrawBorderRange(graphics, pen, points, start, Math.Min(total, end));
                if (end > total) DrawBorderRange(graphics, pen, points, 0F, end - total);
            }
        }

        private static void DrawBorderRange(Graphics graphics, Pen pen, List<PointF> points,
                                            float rangeStart, float rangeEnd)
        {
            using (GraphicsPath segment = new GraphicsPath())
            {
                float cursor = 0F;
                bool started = false;
                PointF previous = PointF.Empty;
                for (int index = 0; index < points.Count - 1; index++)
                {
                    PointF a = points[index];
                    PointF b = points[index + 1];
                    float length = Distance(a, b);
                    if (length < .01F) continue;
                    float from = Math.Max(rangeStart, cursor);
                    float to = Math.Min(rangeEnd, cursor + length);
                    if (to > from)
                    {
                        PointF clippedA = Lerp(a, b, (from - cursor) / length);
                        PointF clippedB = Lerp(a, b, (to - cursor) / length);
                        if (!started || Distance(previous, clippedA) > .2F)
                            segment.StartFigure();
                        segment.AddLine(clippedA, clippedB);
                        previous = clippedB;
                        started = true;
                    }
                    cursor += length;
                    if (cursor >= rangeEnd) break;
                }
                if (started) graphics.DrawPath(pen, segment);
            }
        }

        private static float Distance(PointF a, PointF b)
        {
            float x = b.X - a.X;
            float y = b.Y - a.Y;
            return (float)Math.Sqrt(x * x + y * y);
        }

        private static PointF Lerp(PointF a, PointF b, float t)
        {
            return new PointF(a.X + (b.X - a.X) * t, a.Y + (b.Y - a.Y) * t);
        }

        private void DrawActivity(Graphics graphics, Rectangle bounds)
        {
            RelayTheme.Card(graphics, bounds, 14, RelayTheme.Surface1, RelayTheme.Line);
            RelayTheme.TextLine(graphics, "会话动态", RelayTheme.Font9Medium, RelayTheme.Text,
                new Rectangle(bounds.X + 14, bounds.Y, 100, 38), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            RelayTheme.TextLine(graphics, "状态快照", RelayTheme.Mono8, RelayTheme.Text3,
                new Rectangle(bounds.Right - 100, bounds.Y, 84, 38), TextFormatFlags.Right | TextFormatFlags.VerticalCenter);
            using (Pen pen = new Pen(RelayTheme.Line)) graphics.DrawLine(pen, bounds.X, bounds.Y + 38, bounds.Right, bounds.Y + 38);
            List<RoleSnapshot> roles = snapshot.Roles.OrderBy(role => role.IsWorking ? 0 : role.VisualState == TrayVisualState.Warning ? 1 : 2)
                .ThenByDescending(role => LatestActivity(role)).Take(4).ToList();
            if (roles.Count == 0)
            {
                RelayTheme.TextLine(graphics, "暂无会话状态", RelayTheme.Font9, RelayTheme.Text3,
                    new Rectangle(bounds.X + 14, bounds.Y + 42, bounds.Width - 28, bounds.Height - 48),
                    TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
                return;
            }
            int columns = bounds.Width >= 560 ? 2 : 1;
            int columnWidth = (bounds.Width - 28 - (columns - 1) * 18) / columns;
            int rowHeight = Math.Max(36, (bounds.Height - 46) / (columns == 2 ? 2 : 4));
            for (int index = 0; index < roles.Count; index++)
            {
                int column = columns == 2 ? index % 2 : 0;
                int row = columns == 2 ? index / 2 : index;
                Rectangle item = new Rectangle(bounds.X + 14 + column * (columnWidth + 18), bounds.Y + 42 + row * rowHeight, columnWidth, rowHeight);
                DrawActivityItem(graphics, item, roles[index]);
            }
        }

        private void DrawActivityItem(Graphics graphics, Rectangle bounds, RoleSnapshot role)
        {
            RelayTheme.Dot(graphics, new PointF(bounds.X + 4, bounds.Y + bounds.Height / 2F), RelayTheme.StateColor(role.VisualState), 7);
            string title = role.Role + " · " + ActivityText(role);
            RelayTheme.TextLine(graphics, title, RelayTheme.Font8Medium, RelayTheme.Text2,
                new Rectangle(bounds.X + 16, bounds.Y + 4, bounds.Width - 80, 17), TextFormatFlags.Left);
            string meta = String.IsNullOrWhiteSpace(role.Transport) ? role.Adapter : role.Transport;
            RelayTheme.TextLine(graphics, meta, RelayTheme.Mono8, RelayTheme.Text3,
                new Rectangle(bounds.X + 16, bounds.Y + 21, bounds.Width - 88, 15), TextFormatFlags.Left);
            DateTime? latest = LatestActivity(role);
            string time = latest.HasValue ? latest.Value.ToString("HH:mm:ss") : "—";
            RelayTheme.TextLine(graphics, time, RelayTheme.Mono8, RelayTheme.Text3,
                new Rectangle(bounds.Right - 72, bounds.Y + 4, 68, 17), TextFormatFlags.Right);
        }

        private static string ActivityText(RoleSnapshot role)
        {
            if (role.Mismatch) return "适配器与传输不一致";
            if (role.PendingResultWakeups > 0) return role.PendingResultWakeups + " 个结果唤醒未送达";
            if (role.FailureCount > 0) return "连续失败 " + role.FailureCount;
            if (role.IsWorking) return "工作中";
            if (role.QueuedTasks > 0 || role.Pending) return "有任务等待处理";
            if (role.LastSuccess.HasValue) return "最近投递成功";
            return role.StateLabel;
        }

        private static DateTime? LatestActivity(RoleSnapshot role)
        {
            DateTime? latest = role.LastSuccess;
            if (role.LastSend.HasValue && (!latest.HasValue || role.LastSend.Value > latest.Value)) latest = role.LastSend;
            if (role.LastReceive.HasValue && (!latest.HasValue || role.LastReceive.Value > latest.Value)) latest = role.LastReceive;
            return latest;
        }

        private RoleSnapshot SelectedRole
        {
            get { return snapshot.Roles.FirstOrDefault(role => role.Address == selectedAddress); }
        }

        private void DrawDetail(Graphics graphics, Rectangle bounds)
        {
            RelayTheme.Card(graphics, bounds, 14, RelayTheme.Surface1, RelayTheme.Line);
            RoleSnapshot role = SelectedRole;
            if (role == null)
            {
                RelayTheme.TextLine(graphics, "选择一个会话查看详情", RelayTheme.Font9, RelayTheme.Text3, bounds,
                    TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
                return;
            }
            Rectangle avatar = new Rectangle(bounds.X + 16, bounds.Y + 16, 42, 42);
            RelayTheme.Card(graphics, avatar, 10, RelayTheme.Surface3, RelayTheme.LineStrong);
            string initials = role.Role.Length <= 2 ? role.Role.ToUpperInvariant() : role.Role.Substring(0, 2).ToUpperInvariant();
            RelayTheme.TextLine(graphics, initials, RelayTheme.Mono10, RelayTheme.Text2, avatar,
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
            RelayTheme.Dot(graphics, new PointF(avatar.Right - 1, avatar.Bottom - 1), RelayTheme.StateColor(role.VisualState), 7);
            RelayTheme.TextLine(graphics, role.Role, RelayTheme.Font10Medium, RelayTheme.Text,
                new Rectangle(bounds.X + 70, bounds.Y + 14, bounds.Width - 86, 22), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            RelayTheme.TextLine(graphics, role.ProjectName + "/" + role.Role, RelayTheme.Mono8, RelayTheme.Text3,
                new Rectangle(bounds.X + 70, bounds.Y + 36, bounds.Width - 86, 18), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            Rectangle state = new Rectangle(bounds.X + 16, bounds.Y + 72, bounds.Width - 32, 38);
            RelayTheme.Card(graphics, state, 9, Color.FromArgb(18, 23, 26), RelayTheme.Line);
            RelayTheme.Dot(graphics, new PointF(state.X + 13, state.Y + 19), RelayTheme.StateColor(role.VisualState), 7);
            RelayTheme.TextLine(graphics, role.StateLabel, RelayTheme.Font8Medium, RelayTheme.Text,
                new Rectangle(state.X + 24, state.Y, state.Width - 100, state.Height), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            DateTime? latestActivity = LatestActivity(role);
            string age = latestActivity.HasValue ? latestActivity.Value.ToString("MM-dd HH:mm:ss") : "暂无活动";
            RelayTheme.TextLine(graphics, age, RelayTheme.Mono8, RelayTheme.Text3,
                new Rectangle(state.Right - 112, state.Y, 100, state.Height), TextFormatFlags.Right | TextFormatFlags.VerticalCenter);
            int y = state.Bottom + 18;
            DrawDefinition(graphics, bounds, ref y, "聊天", role.Binding);
            DrawDefinition(graphics, bounds, ref y, "传输", String.IsNullOrWhiteSpace(role.Transport) ? "—" : role.Transport);
            DrawDefinition(graphics, bounds, ref y, "适配器", role.Adapter);
            DrawDefinition(graphics, bounds, ref y, "模式", role.Mode);
            DrawDefinition(graphics, bounds, ref y, "会话 ID", role.ShortId);
            y += 10;
            using (Pen pen = new Pen(RelayTheme.Line)) graphics.DrawLine(pen, bounds.X + 16, y, bounds.Right - 16, y);
            y += 14;
            DrawDefinition(graphics, bounds, ref y, "任务", role.RunningTasks + " 工作中 / " + role.QueuedTasks + " 等待");
            DrawDefinition(graphics, bounds, ref y, "连续失败", Convert.ToString(role.FailureCount));
            DrawDefinition(graphics, bounds, ref y, "未唤醒结果", Convert.ToString(role.PendingResultWakeups));
            y += 10;
            using (Pen pen = new Pen(RelayTheme.Line)) graphics.DrawLine(pen, bounds.X + 16, y, bounds.Right - 16, y);
            y += 12;
            RelayTheme.TextLine(graphics, "职责", RelayTheme.Font8, RelayTheme.Text3,
                new Rectangle(bounds.X + 16, y, bounds.Width - 32, 16), TextFormatFlags.Left);
            y += 20;
            RelayTheme.TextLine(graphics, String.IsNullOrWhiteSpace(role.Description) ? "未填写职责说明" : role.Description,
                RelayTheme.Font8, RelayTheme.Text2, new Rectangle(bounds.X + 16, y, bounds.Width - 32, Math.Max(40, bounds.Bottom - y - 72)),
                TextFormatFlags.Left | TextFormatFlags.WordBreak);
            Rectangle copy = new Rectangle(bounds.X + 16, bounds.Bottom - 50, (bounds.Width - 40) / 2, 34);
            Rectangle change = new Rectangle(copy.Right + 8, copy.Y, bounds.Right - 16 - (copy.Right + 8), 34);
            DrawButton(graphics, copy, "copy", "复制 ID", false, "copy");
            DrawButton(graphics, change, "switch", "切换会话", true, "switch");
        }

        private void DrawDefinition(Graphics graphics, Rectangle bounds, ref int y, string label, string value)
        {
            RelayTheme.TextLine(graphics, label, RelayTheme.Font8, RelayTheme.Text3,
                new Rectangle(bounds.X + 16, y, 72, 22), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            RelayTheme.TextLine(graphics, value, RelayTheme.Mono8, RelayTheme.Text2,
                new Rectangle(bounds.X + 86, y, bounds.Width - 102, 22), TextFormatFlags.Right | TextFormatFlags.VerticalCenter);
            y += 24;
        }

        private void DrawButton(Graphics graphics, Rectangle bounds, string glyph, string label, bool primary, string kind)
        {
            RelayTheme.Card(graphics, bounds, 8, primary ? RelayTheme.Accent : RelayTheme.Surface2,
                primary ? RelayTheme.Accent : RelayTheme.LineStrong);
            Color color = primary ? RelayTheme.AccentInk : RelayTheme.Text2;
            RelayTheme.Glyph(graphics, glyph, new Rectangle(bounds.X + 10, bounds.Y + 9, 16, 16), color);
            RelayTheme.TextLine(graphics, label, RelayTheme.Font8Medium, color,
                new Rectangle(bounds.X + 32, bounds.Y, bounds.Width - 40, bounds.Height), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            hits.Add(new HitRegion(bounds, kind));
        }

        private void DrawToast(Graphics graphics)
        {
            if (DateTime.Now >= toastUntil || String.IsNullOrWhiteSpace(toast)) return;
            Size size = TextRenderer.MeasureText(toast, RelayTheme.Font8, new Size(360, 32), TextFormatFlags.NoPadding);
            Rectangle bounds = new Rectangle((Width - size.Width - 42) / 2, Height - 54, size.Width + 42, 34);
            RelayTheme.Card(graphics, bounds, 9, RelayTheme.Surface3, RelayTheme.LineStrong);
            RelayTheme.Dot(graphics, new PointF(bounds.X + 16, bounds.Y + 17), RelayTheme.Accent, 7);
            RelayTheme.TextLine(graphics, toast, RelayTheme.Font8, RelayTheme.Text2,
                new Rectangle(bounds.X + 28, bounds.Y, bounds.Width - 36, bounds.Height), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
        }

        private void ShowToast(string message)
        {
            toast = message;
            toastUntil = DateTime.Now.AddSeconds(1.8);
            Invalidate();
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            Cursor = hits.Any(hit => hit.Bounds.Contains(e.Location)) ? Cursors.Hand : Cursors.Default;
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            HitRegion hit = hits.LastOrDefault(item => item.Bounds.Contains(e.Location));
            if (hit == null) return;
            if (hit.Kind == "project")
            {
                selectedProject = hit.ProjectId;
                ProjectSnapshot project = snapshot.Projects.FirstOrDefault(item => item.Id == selectedProject);
                selectedAddress = project != null && project.Roles.Count > 0 ? project.Roles[0].Address :
                    snapshot.Roles.Select(role => role.Address).FirstOrDefault() ?? "";
                Invalidate();
            }
            else if (hit.Kind == "role")
            {
                selectedProject = hit.ProjectId;
                selectedAddress = hit.Address;
                Invalidate();
            }
            else if (hit.Kind == "switch" && SelectedRole != null)
                takeover(SelectedRole.ProjectId, SelectedRole.Address);
            else if (hit.Kind == "copy" && SelectedRole != null)
            {
                if (!String.IsNullOrWhiteSpace(SelectedRole.SessionId))
                {
                    try { Clipboard.SetText(SelectedRole.SessionId); ShowToast("会话 ID 已复制"); }
                    catch { ShowToast("无法访问剪贴板"); }
                }
                else ShowToast("这个角色还没有绑定会话");
            }
            else if (hit.Kind == "power") startStop();
            else if (hit.Kind == "refresh") { refresh(); ShowToast("正在刷新状态"); }
            else if (hit.Kind == "config") openFolder(Path.GetDirectoryName(configPath) ?? "");
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing && animationTimer != null) animationTimer.Dispose();
            base.Dispose(disposing);
        }
    }

    internal sealed class TrayPopoverForm : Form
    {
        private readonly TrayPopoverCanvas canvas;
        private readonly Timer fadeTimer;

        internal TrayPopoverForm(Action openPanel, Action refresh, Action toggle, Action openConfig, Action openData,
                                 Action exitTray, Action stopAndExit, Action<string, string> openRole)
        {
            Text = "Agents Chat · 托盘面板";
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            TopMost = true;
            StartPosition = FormStartPosition.Manual;
            Size = new Size(388, 660);
            BackColor = RelayTheme.Window;
            canvas = new TrayPopoverCanvas(this, openPanel, refresh, toggle, openConfig, openData, exitTray, stopAndExit, openRole);
            canvas.Dock = DockStyle.Fill;
            Controls.Add(canvas);
            fadeTimer = new Timer();
            fadeTimer.Interval = 16;
            fadeTimer.Tick += delegate
            {
                Opacity = Math.Min(1D, Opacity + .16D);
                if (Opacity >= 1D) fadeTimer.Stop();
            };
            Deactivate += delegate { Hide(); };
            SizeChanged += delegate { UpdateRegion(); };
        }

        protected override bool ShowWithoutActivation { get { return false; } }

        private void UpdateRegion()
        {
            using (GraphicsPath path = RelayTheme.RoundedPath(new RectangleF(0, 0, Width, Height), 16F))
            {
                Region old = Region;
                Region = new Region(path);
                if (old != null) old.Dispose();
            }
        }

        internal void SetStatus(Dictionary<string, object> status, string configPath, string stateDir)
        {
            canvas.SetStatus(StatusSnapshot.From(status));
        }

        internal void ShowNearTray(Point cursor)
        {
            if (Visible)
            {
                Hide();
                return;
            }
            Rectangle working = Screen.FromPoint(cursor).WorkingArea;
            Location = new Point(working.Right - Width - 12, working.Bottom - Height - 12);
            Opacity = 0D;
            UpdateRegion();
            Show();
            Activate();
            fadeTimer.Start();
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing && fadeTimer != null) fadeTimer.Dispose();
            base.Dispose(disposing);
        }
    }

    internal sealed class TrayPopoverCanvas : Control
    {
        private readonly Form owner;
        private readonly Action openPanel;
        private readonly Action refresh;
        private readonly Action toggle;
        private readonly Action openConfig;
        private readonly Action openData;
        private readonly Action exitTray;
        private readonly Action stopAndExit;
        private readonly Action<string, string> openRole;
        private readonly List<HitRegion> hits = new List<HitRegion>();
        private StatusSnapshot snapshot = new StatusSnapshot();
        private int scrollOffset;
        private int contentHeight;
        private bool showMore;

        internal TrayPopoverCanvas(Form owner, Action openPanel, Action refresh, Action toggle, Action openConfig,
                                   Action openData, Action exitTray, Action stopAndExit, Action<string, string> openRole)
        {
            this.owner = owner;
            this.openPanel = openPanel;
            this.refresh = refresh;
            this.toggle = toggle;
            this.openConfig = openConfig;
            this.openData = openData;
            this.exitTray = exitTray;
            this.stopAndExit = stopAndExit;
            this.openRole = openRole;
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint |
                     ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
            BackColor = RelayTheme.Window;
        }

        internal void SetStatus(StatusSnapshot value)
        {
            snapshot = value ?? new StatusSnapshot();
            scrollOffset = Math.Min(scrollOffset, Math.Max(0, contentHeight));
            Invalidate();
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            Graphics graphics = e.Graphics;
            graphics.SmoothingMode = SmoothingMode.AntiAlias;
            graphics.Clear(Color.FromArgb(17, 21, 24));
            hits.Clear();
            using (Pen border = new Pen(RelayTheme.LineStrong)) RelayTheme.DrawRounded(graphics, border, new RectangleF(.5F, .5F, Width - 1, Height - 1), 16F);
            DrawHeader(graphics);
            DrawRoleList(graphics);
            DrawFooter(graphics);
            if (showMore) DrawMore(graphics);
        }

        private int HeaderHeight { get { return snapshot.Degraded ? 176 : 132; } }
        private int FooterHeight { get { return 62; } }

        private void DrawHeader(Graphics graphics)
        {
            RelayTheme.DrawRelayMark(graphics, new RectangleF(16, 14, 28, 28), RelayTheme.StateColor(snapshot.Running ?
                (snapshot.Degraded ? TrayVisualState.Warning : snapshot.BusyRoles > 0 ? TrayVisualState.Busy : TrayVisualState.Running) : TrayVisualState.Offline), false);
            RelayTheme.TextLine(graphics, "Agents Chat", RelayTheme.Font9Medium, RelayTheme.Text,
                new Rectangle(52, 10, 180, 20), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            Color stateColor = snapshot.Running ? (snapshot.Degraded ? RelayTheme.Warning : RelayTheme.Accent) : RelayTheme.Offline;
            RelayTheme.Dot(graphics, new PointF(55, 38), stateColor, 7);
            RelayTheme.TextLine(graphics, snapshot.Running ? (snapshot.Degraded ? "中枢需留意" : "中枢运行中") : "后台未运行",
                RelayTheme.Font8, RelayTheme.Text3, new Rectangle(64, 29, 190, 18), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            Rectangle more = new Rectangle(Width - 44, 12, 28, 28);
            RelayTheme.Glyph(graphics, "more", new Rectangle(more.X + 5, more.Y + 5, 18, 18), RelayTheme.Text3);
            hits.Add(new HitRegion(more, "more"));
            int metricY = 58;
            int metricWidth = (Width - 48) / 3;
            DrawTrayMetric(graphics, new Rectangle(12, metricY, metricWidth, 54), Convert.ToString(snapshot.OnlineRoles), "在线角色");
            DrawTrayMetric(graphics, new Rectangle(18 + metricWidth, metricY, metricWidth, 54), Convert.ToString(Math.Max(snapshot.BusyRoles, snapshot.RunningTasks)), "执行中");
            DrawTrayMetric(graphics, new Rectangle(24 + metricWidth * 2, metricY, metricWidth, 54), Convert.ToString(snapshot.Queued), "等待任务");
            if (snapshot.Degraded)
            {
                Rectangle warning = new Rectangle(12, 122, Width - 24, 40);
                RelayTheme.Card(graphics, warning, 9, Color.FromArgb(30, 26, 20), Color.FromArgb(93, 76, 49));
                RelayTheme.Dot(graphics, new PointF(warning.X + 14, warning.Y + 20), RelayTheme.Warning, 7);
                RelayTheme.TextLine(graphics, AttentionText(), RelayTheme.Font8, Color.FromArgb(231, 195, 135),
                    new Rectangle(warning.X + 26, warning.Y, warning.Width - 36, warning.Height), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            }
            using (Pen pen = new Pen(RelayTheme.Line)) graphics.DrawLine(pen, 0, HeaderHeight - 1, Width, HeaderHeight - 1);
        }

        private void DrawTrayMetric(Graphics graphics, Rectangle bounds, string value, string label)
        {
            RelayTheme.Card(graphics, bounds, 9, RelayTheme.Surface1, RelayTheme.Line);
            RelayTheme.TextLine(graphics, value, RelayTheme.Font14, RelayTheme.Text,
                new Rectangle(bounds.X + 10, bounds.Y + 6, bounds.Width - 20, 22), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            RelayTheme.TextLine(graphics, label, RelayTheme.Font8, RelayTheme.Text3,
                new Rectangle(bounds.X + 10, bounds.Y + 30, bounds.Width - 20, 16), TextFormatFlags.Left);
        }

        private string AttentionText()
        {
            if (snapshot.PendingResultWakeups > 0) return snapshot.PendingResultWakeups + " 个结果唤醒未送达";
            if (snapshot.TransportFailures > 0) return "传输连续失败 " + snapshot.TransportFailures + " 次";
            int offline = snapshot.TotalRoles - snapshot.OnlineRoles;
            return offline > 0 ? offline + " 个会话离线或未绑定" : "传输或唤醒链路异常";
        }

        private void DrawRoleList(Graphics graphics)
        {
            Rectangle viewport = new Rectangle(1, HeaderHeight, Width - 2, Height - HeaderHeight - FooterHeight);
            GraphicsState saved = graphics.Save();
            graphics.SetClip(viewport);
            int y = HeaderHeight + 8 - scrollOffset;
            if (snapshot.Projects.Count == 0)
            {
                RelayTheme.TextLine(graphics, snapshot.Running ? "尚未配置项目" : "启动后台后显示会话",
                    RelayTheme.Font9, RelayTheme.Text3, new Rectangle(16, y + 24, Width - 32, 60),
                    TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
                contentHeight = 100;
            }
            else
            {
                foreach (ProjectSnapshot project in snapshot.Projects)
                {
                    int online = project.Roles.Count(role => role.Online && !role.Mismatch);
                    RelayTheme.TextLine(graphics, project.Name, RelayTheme.Font8, RelayTheme.Text3,
                        new Rectangle(16, y, Width - 120, 26), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
                    RelayTheme.TextLine(graphics, online + " / " + project.Roles.Count + " 在线", RelayTheme.Mono8, RelayTheme.Text3,
                        new Rectangle(Width - 110, y, 94, 26), TextFormatFlags.Right | TextFormatFlags.VerticalCenter);
                    y += 28;
                    foreach (RoleSnapshot role in project.Roles.OrderBy(item => item.VisualState == TrayVisualState.Warning ? 0 : item.VisualState == TrayVisualState.Busy ? 1 : 2))
                    {
                        Rectangle row = new Rectangle(8, y, Width - 16, 48);
                        DrawTrayRole(graphics, row, role);
                        y += 50;
                    }
                    y += 8;
                }
                contentHeight = y + scrollOffset - HeaderHeight;
            }
            graphics.Restore(saved);
            int maxScroll = Math.Max(0, contentHeight - viewport.Height);
            scrollOffset = Math.Max(0, Math.Min(scrollOffset, maxScroll));
            if (maxScroll > 0)
            {
                float ratio = viewport.Height / (float)contentHeight;
                int thumbHeight = Math.Max(28, (int)(viewport.Height * ratio));
                int thumbY = viewport.Top + (int)((viewport.Height - thumbHeight) * (scrollOffset / (float)maxScroll));
                using (Brush brush = new SolidBrush(RelayTheme.LineStrong)) RelayTheme.FillRounded(graphics, brush,
                    new RectangleF(Width - 5, thumbY, 3, thumbHeight), 1.5F);
            }
        }

        private void DrawTrayRole(Graphics graphics, Rectangle bounds, RoleSnapshot role)
        {
            Rectangle avatar = new Rectangle(bounds.X + 8, bounds.Y + 8, 32, 32);
            RelayTheme.Card(graphics, avatar, 8, RelayTheme.Surface3, RelayTheme.Line);
            string initials = role.Role.Length <= 2 ? role.Role.ToUpperInvariant() : role.Role.Substring(0, 2).ToUpperInvariant();
            RelayTheme.TextLine(graphics, initials, RelayTheme.Mono8, RelayTheme.Text2, avatar,
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
            RelayTheme.Dot(graphics, new PointF(avatar.Right - 1, avatar.Bottom - 1), RelayTheme.StateColor(role.VisualState), 6);
            RelayTheme.TextLine(graphics, role.Role, RelayTheme.Font8Medium, RelayTheme.Text,
                new Rectangle(bounds.X + 50, bounds.Y + 6, bounds.Width - 122, 18), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            string meta = role.Pending ? "有任务等待处理" : role.Binding;
            RelayTheme.TextLine(graphics, meta, RelayTheme.Mono8, RelayTheme.Text3,
                new Rectangle(bounds.X + 50, bounds.Y + 24, bounds.Width - 120, 16), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            RelayTheme.TextLine(graphics, role.StateLabel, RelayTheme.Font8,
                role.VisualState == TrayVisualState.Warning ? RelayTheme.Warning : role.VisualState == TrayVisualState.Busy ? RelayTheme.Busy : RelayTheme.Text3,
                new Rectangle(bounds.Right - 70, bounds.Y, 58, bounds.Height), TextFormatFlags.Right | TextFormatFlags.VerticalCenter);
            HitRegion hit = new HitRegion(bounds, "role");
            hit.ProjectId = role.ProjectId;
            hit.Address = role.Address;
            if (bounds.Bottom >= HeaderHeight && bounds.Top <= Height - FooterHeight) hits.Add(hit);
        }

        private void DrawFooter(Graphics graphics)
        {
            int y = Height - FooterHeight;
            using (Brush brush = new SolidBrush(Color.FromArgb(16, 20, 23))) graphics.FillRectangle(brush, 0, y, Width, FooterHeight);
            using (Pen pen = new Pen(RelayTheme.Line)) graphics.DrawLine(pen, 0, y, Width, y);
            Rectangle open = new Rectangle(12, y + 12, Width - 112, 38);
            RelayTheme.Card(graphics, open, 9, RelayTheme.Accent, RelayTheme.Accent);
            RelayTheme.Glyph(graphics, "network", new Rectangle(open.X + 70, open.Y + 10, 18, 18), RelayTheme.AccentInk);
            RelayTheme.TextLine(graphics, "打开状态窗口", RelayTheme.Font8Medium, RelayTheme.AccentInk,
                new Rectangle(open.X + 94, open.Y, open.Width - 106, open.Height), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            hits.Add(new HitRegion(open, "open"));
            Rectangle reload = new Rectangle(open.Right + 8, open.Y, 38, 38);
            RelayTheme.Card(graphics, reload, 9, RelayTheme.Surface2, RelayTheme.LineStrong);
            RelayTheme.Glyph(graphics, "refresh", new Rectangle(reload.X + 10, reload.Y + 10, 18, 18), RelayTheme.Text2);
            hits.Add(new HitRegion(reload, "refresh"));
            Rectangle more = new Rectangle(reload.Right + 8, reload.Y, 38, 38);
            RelayTheme.Card(graphics, more, 9, RelayTheme.Surface2, RelayTheme.LineStrong);
            RelayTheme.Glyph(graphics, "more", new Rectangle(more.X + 10, more.Y + 10, 18, 18), RelayTheme.Text2);
            hits.Add(new HitRegion(more, "more"));
        }

        private void DrawMore(Graphics graphics)
        {
            Rectangle panel = new Rectangle(Width - 228, Height - FooterHeight - 218, 216, 206);
            RelayTheme.Card(graphics, panel, 12, RelayTheme.Surface2, RelayTheme.LineStrong);
            DrawMoreRow(graphics, new Rectangle(panel.X + 8, panel.Y + 8, panel.Width - 16, 36), "folder", "打开配置目录", "config");
            DrawMoreRow(graphics, new Rectangle(panel.X + 8, panel.Y + 46, panel.Width - 16, 36), "folder", "打开数据目录", "data");
            DrawMoreRow(graphics, new Rectangle(panel.X + 8, panel.Y + 84, panel.Width - 16, 36), "power", snapshot.Running ? "停止后台" : "启动后台", "toggle");
            DrawMoreRow(graphics, new Rectangle(panel.X + 8, panel.Y + 122, panel.Width - 16, 36), "more", "退出托盘，后台继续", "exit");
            DrawMoreRow(graphics, new Rectangle(panel.X + 8, panel.Y + 160, panel.Width - 16, 36), "power", "停止后台并退出", "stop-exit");
        }

        private void DrawMoreRow(Graphics graphics, Rectangle bounds, string glyph, string label, string kind)
        {
            RelayTheme.Glyph(graphics, glyph, new Rectangle(bounds.X + 8, bounds.Y + 9, 18, 18), RelayTheme.Text3);
            RelayTheme.TextLine(graphics, label, RelayTheme.Font8, kind == "stop-exit" ? RelayTheme.Bad : RelayTheme.Text2,
                new Rectangle(bounds.X + 36, bounds.Y, bounds.Width - 44, bounds.Height), TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
            hits.Add(new HitRegion(bounds, kind));
        }

        protected override void OnMouseWheel(MouseEventArgs e)
        {
            base.OnMouseWheel(e);
            int viewportHeight = Height - HeaderHeight - FooterHeight;
            int maxScroll = Math.Max(0, contentHeight - viewportHeight);
            scrollOffset = Math.Max(0, Math.Min(maxScroll, scrollOffset - Math.Sign(e.Delta) * 72));
            Invalidate();
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            Cursor = hits.Any(hit => hit.Bounds.Contains(e.Location)) ? Cursors.Hand : Cursors.Default;
        }

        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            HitRegion hit = hits.LastOrDefault(item => item.Bounds.Contains(e.Location));
            if (hit == null)
            {
                if (showMore) { showMore = false; Invalidate(); }
                return;
            }
            if (hit.Kind == "more")
            {
                showMore = !showMore;
                Invalidate();
                return;
            }
            showMore = false;
            owner.Hide();
            if (hit.Kind == "open") openPanel();
            else if (hit.Kind == "refresh") refresh();
            else if (hit.Kind == "toggle") toggle();
            else if (hit.Kind == "config") openConfig();
            else if (hit.Kind == "data") openData();
            else if (hit.Kind == "exit") exitTray();
            else if (hit.Kind == "stop-exit") stopAndExit();
            else if (hit.Kind == "role") openRole(hit.ProjectId, hit.Address);
        }
    }
}

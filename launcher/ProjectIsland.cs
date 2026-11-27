using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.IO;
using System.Linq;
using System.Windows.Forms;

namespace AgentsChatLauncher
{
    internal sealed class IslandMessage
    {
        internal string Id, Sender, Recipient, Kind;
        internal DateTime At;

        internal static IslandMessage From(Dictionary<string, object> data)
        {
            string id = StatusSnapshot.StringValue(data, "id"),
                   sender = StatusSnapshot.StringValue(data, "sender"),
                   recipient = StatusSnapshot.StringValue(data, "recipient");
            if (id == "" || sender == "" || recipient == "" || sender == recipient)
                return null;
            DateTime? at = RoleSnapshot.TimeValue(data, "at");
            if (!at.HasValue)
                return null;
            return new IslandMessage { Id = id, Sender = sender, Recipient = recipient,
                                       At = at.Value.ToUniversalTime(),
                                       Kind = StatusSnapshot.StringValue(data, "kind") };
        }

        internal string Caption
        {
            get {
                switch (Kind)
                {
                case "request":
                    return "派发任务";
                case "result":
                    return "回传结果";
                case "claim":
                    return "领取任务";
                case "verify":
                    return "验收";
                case "progress":
                    return "更新进度";
                case "blocked":
                    return "报告阻塞";
                case "release":
                    return "交还任务";
                case "reassign":
                    return "重新分派";
                case "cancel":
                    return "取消任务";
                default:
                    return "发送提醒";
                }
            }
        }
    }

    internal sealed class ProjectIsland : Form
    {
        private const int RailWidth = 60, Step = 51, Cell = 91, PaneHeight = 176, Gap = 6, EdgeMargin = 12;
        private const int TransitionInterval = 15;
        private static readonly Color Ink = IslandArt.C(0xf1eee8), Muted = IslandArt.C(0xb2acbd),
                                      Shell = IslandArt.C(0x22212e), Rim = IslandArt.C(0x514d60),
                                      Floor = IslandArt.C(0x373541);
        private readonly Action<string, string> openRole;
        private readonly Action openDetails;
        private readonly string positionFile;
        private readonly IslandIconPreferences icons;
        private readonly Timer timer = new Timer();
        private readonly Stopwatch clock = Stopwatch.StartNew();
        private readonly ToolTip tip = new ToolTip { ShowAlways = true };
        private readonly Dictionary<string, int> styles = new Dictionary<string, int>();
        private readonly Dictionary<string, int> species = new Dictionary<string, int>();
        private readonly Dictionary<int, Bitmap[]> emblems = new Dictionary<int, Bitmap[]>();
        private IslandSurface surface;
        private Region hitArea;
        private readonly List<Flight> flights = new List<Flight>();
        private readonly Dictionary<string, DateTime> seen = new Dictionary<string, DateTime>();
        private DateTime liveSince;
        private bool baseline, left, dragArmed, dragging, docking, closing;
        private Point pressScreen, gripOffset, floatingRail, dockStart;
        private double dockStarted, dockDuration;
        private int firstProject, firstRole, railCapacity, roleCapacity;
        private double anchor = .5, reveal, awaySince = -1, lastTick;
        private string active = "", nextProject = "", tipText = "";
        private StatusSnapshot snapshot = new StatusSnapshot();
        private Screen monitor = Screen.PrimaryScreen;
        private Rectangle rail, pane, fullPane, bridge, prev, next;
        private Rectangle[] projectHits = new Rectangle[0], roleHits = new Rectangle[0];
        private ProjectSnapshot project;
        private readonly IslandContextMenu menu = new IslandContextMenu();
        private bool dismissingMenuClick;
        private sealed class Flight
        {
            internal IslandMessage Message;
            internal double Start;
        }

        internal ProjectIsland(Action<string, string> openRole, Action openDetails, string positionFile)
        {
            this.openRole = openRole;
            this.openDetails = openDetails;
            this.positionFile = positionFile;
            icons = new IslandIconPreferences(Path.Combine(Path.GetDirectoryName(positionFile), "island-icons.local.json"));
            Text = "Agents Chat · 桌面悬浮岛";
            AccessibleName = "Agents Chat 桌面项目与角色";
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.Manual;
            ShowInTaskbar = false;
            TopMost = true;
            AutoScaleMode = AutoScaleMode.None;
            BackColor = Shell;
            menu.Items.Add("会话详情", null, delegate { openDetails(); });
            menu.Items.Add("收起到系统托盘", null, delegate {
                Collapse();
                UpdateReveal(false);
                Hide();
            });
            menu.Closed += delegate(object sender, ToolStripDropDownClosedEventArgs e)
            {
                awaySince = clock.Elapsed.TotalSeconds;
                if (e.CloseReason == ToolStripDropDownCloseReason.AppClicked && IsHandleCreated && !Disposing)
                {
                    // WinForms can dismiss the menu before dispatching the same click to this form.
                    dismissingMenuClick = true;
                    BeginInvoke(new Action(delegate { dismissingMenuClick = false; }));
                }
            };
            ReadPosition();
            timer.Tick += delegate
            {
                TickIsland();
            };
            LayoutIsland();
        }

        protected override bool ShowWithoutActivation
        {
            get {
                return true;
            }
        }
        protected override CreateParams CreateParams
        {
            get {
                CreateParams cp = base.CreateParams;
                cp.ExStyle |= 0x80000 | 0x80; // WS_EX_LAYERED | WS_EX_TOOLWINDOW
                return cp;
            }
        }
        protected override void WndProc(ref Message m)
        {
            const int WM_MOUSEACTIVATE = 0x21, WM_RBUTTONDOWN = 0x204, WM_ACTIVATEAPP = 0x1c;
            const int MA_ACTIVATE = 1, MA_NOACTIVATE = 3;
            if (m.Msg == WM_MOUSEACTIVATE)
            {
                // Only a context-menu gesture needs a foreground owner. Hover and drag keep editor focus.
                int buttonMessage = (int)((m.LParam.ToInt64() >> 16) & 0xffff);
                m.Result = (IntPtr)(buttonMessage == WM_RBUTTONDOWN && !dragArmed ? MA_ACTIVATE : MA_NOACTIVATE);
                return;
            }
            if (m.Msg == WM_ACTIVATEAPP && m.WParam == IntPtr.Zero && menu != null && menu.Visible)
                menu.Close(ToolStripDropDownCloseReason.AppFocusChange);
            base.WndProc(ref m);
            if (m.Msg == 0x7e || m.Msg == 0x2e0)
                LayoutIsland(); // display/DPI change
        }
        protected override void OnVisibleChanged(EventArgs e)
        {
            base.OnVisibleChanged(e);
            if (timer == null)
                return;
            if (Visible)
            {
                lastTick = clock.Elapsed.TotalSeconds;
                timer.Interval = 100;
                timer.Start();
                PresentIsland();
            }
            else
                timer.Stop();
        }
        private void ReadPosition()
        {
            try
            {
                string[] parts = File.ReadAllLines(positionFile);
                if (parts.Length != 3)
                    return;
                monitor = Screen.AllScreens.FirstOrDefault(s => s.DeviceName == parts[0]) ?? Screen.PrimaryScreen;
                left = parts[1] == "left";
                int percent;
                if (Int32.TryParse(parts[2], out percent))
                    anchor = Math.Max(0, Math.Min(1000, percent)) / 1000.0;
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }
        private void SavePosition()
        {
            try
            {
                File.WriteAllLines(positionFile, new[] { monitor.DeviceName, left ? "left" : "right",
                                                         ((int)Math.Round(anchor * 1000)).ToString() });
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
        }
        internal void SetSnapshot(StatusSnapshot value)
        {
            snapshot = value;
            project = snapshot.Projects.FirstOrDefault(p => p.Id == active);
            if (project == null)
                Collapse();
            // Resolve collisions within each project so adjacent animals are distinguishable.
            AssignStyles(snapshot.Projects.Select(p => p.Id), styles, IslandArt.OriginalProjectCount, IslandArt.ProjectCount);
            foreach (ProjectSnapshot p in snapshot.Projects)
                AssignStyles(p.Roles.Select(r => r.Address), species, IslandArt.OriginalAnimalCount, IslandArt.Count);
            DateTime now = DateTime.UtcNow;
            if (!baseline)
                liveSince = now;
            double t = clock.Elapsed.TotalSeconds;
            foreach (string id in seen.Where(pair => (now - pair.Value).TotalSeconds >= 12)
                         .Select(pair => pair.Key)
                         .ToList())
                seen.Remove(id);
            foreach (IslandMessage m in snapshot.Traffic)
            {
                if ((now - m.At).TotalSeconds < -2 || (now - m.At).TotalSeconds >= 12 || seen.ContainsKey(m.Id))
                    continue;
                seen[m.Id] = m.At;
                if (baseline && m.At >= liveSince)
                {
                    double start = t;
                    Flight prior = flights.LastOrDefault(f => f.Message.Sender == m.Sender ||
                                                              f.Message.Recipient == m.Recipient);
                    if (prior != null)
                        start = Math.Min(t + 1.8, Math.Max(t, prior.Start + .38));
                    flights.Add(new Flight { Message = m, Start = start });
                }
            }
            baseline = snapshot.Running;
            flights.RemoveAll(f => t - f.Start > 2.2 || !snapshot.Running);
            LayoutIsland();
            PresentIsland();
        }
        internal bool ShowsAntigravity
        {
            get { return Visible && snapshot.Roles.Any(r => r.Transport == "antigravity_native" && r.Online); }
        }
        internal void SetAntigravityActivity(Dictionary<string, object> observations)
        {
            bool changed = false;
            foreach (RoleSnapshot role in snapshot.Roles)
                changed |= role.UpdateAntigravityActivity(observations);
            if (changed) PresentIsland();
        }
        private static void AssignStyles(IEnumerable<string> keys, Dictionary<string, int> known,
                                         int originalCount, int count)
        {
            List<string> ordered = keys.OrderBy(s => s, StringComparer.Ordinal).ToList();
            HashSet<int> used = new HashSet<int>(ordered.Where(known.ContainsKey).Select(k => known[k]));
            foreach (string key in ordered)
            {
                if (known.ContainsKey(key))
                    continue;
                // Keep the original automatic choices until the initial catalog is exhausted.
                int available = used.Count < originalCount ? originalCount : count;
                int n = IslandArt.Hash(key) % available;
                if (used.Count < available)
                    while (used.Contains(n))
                        n = (n + 1) % available;
                known[key] = n;
                used.Add(n);
            }
        }
        private void Collapse()
        {
            project = null;
            active = "";
            nextProject = "";
            closing = false;
            reveal = 0;
            firstRole = 0;
            awaySince = -1;
        }
        private void SelectProject(ProjectSnapshot p)
        {
            closing = false;
            awaySince = -1;
            if (active == p.Id)
            {
                nextProject = "";
                return;
            }
            if (project != null && reveal > 0)
            {
                nextProject = p.Id;
                timer.Interval = TransitionInterval;
                return;
            }
            if (project == null)
            {
                lastTick = clock.Elapsed.TotalSeconds;
                timer.Interval = TransitionInterval;
            }
            active = p.Id;
            nextProject = "";
            project = p;
            firstRole = 0;
            LayoutIsland();
            PresentIsland();
        }
        private static int PaneTop(int mid, Rectangle work)
        {
            return Clamp(mid - PaneHeight / 2, work.Top + EdgeMargin,
                         Math.Max(work.Top + EdgeMargin, work.Bottom - PaneHeight - EdgeMargin));
        }
        private Point DockPoint(Rectangle work, int height)
        {
            return new Point(left ? work.Left + EdgeMargin : work.Right - EdgeMargin - RailWidth,
                work.Top + EdgeMargin + (int)Math.Round(Math.Max(0, work.Height - height - 2 * EdgeMargin) * anchor));
        }
        private void LayoutIsland()
        {
            if (monitor == null)
                return;
            monitor =
                Screen.AllScreens.FirstOrDefault(s => s.DeviceName == monitor.DeviceName) ?? Screen.PrimaryScreen;
            Rectangle work = monitor.WorkingArea;
            railCapacity = Math.Max(1, (work.Height - 2 * EdgeMargin - 48) / Step);
            int count = Math.Min(railCapacity, snapshot.Projects.Count);
            firstProject = Clamp(firstProject, 0, Math.Max(0, snapshot.Projects.Count - count));
            int height = Math.Max(76, 46 + count * Step);
            Rectangle outerRail = new Rectangle(DockPoint(work, height), new Size(RailWidth, height));
            int available = Math.Max(1, work.Width - RailWidth - 2 * EdgeMargin - Gap);
            roleCapacity = Math.Max(1, (available - 24) / Cell);
            int roles = project == null ? 0 : project.Roles.Count;
            int onPage = Math.Min(roles, roleCapacity);
            int paneWidth = Math.Min(Math.Max(242, 24 + onPage * Cell), available);
            firstRole = Clamp(firstRole, 0, Math.Max(0, roles - onPage));
            int selected = snapshot.Projects.FindIndex(p => p.Id == active) - firstProject;
            int firstMid = outerRail.Top + 27 + Step / 2;
            int mid = firstMid + Clamp(selected, 0, Math.Max(0, count - 1)) * Step;
            // Give the native window one stable coordinate system for all hover frames.
            // The desktop passes clicks through fully transparent pixels.
            Rectangle outer = outerRail;
            if (count > 0)
            {
                int maxRoles = Math.Min(roleCapacity, snapshot.Projects.Max(p => p.Roles.Count));
                int maxWidth = Math.Min(Math.Max(242, 24 + maxRoles * Cell), available);
                int firstTop = PaneTop(firstMid, work);
                int lastTop = PaneTop(firstMid + (count - 1) * Step, work);
                outer = Rectangle.Union(outerRail, new Rectangle(left ? outerRail.Right + Gap :
                    outerRail.Left - Gap - maxWidth, firstTop, maxWidth, lastTop - firstTop + PaneHeight));
            }
            Rectangle oldRail = rail, oldBridge = bridge;
            rail = outerRail;
            rail.Offset(-outer.X, -outer.Y);
            fullPane = new Rectangle(left ? rail.Right + Gap : rail.Left - Gap - paneWidth,
                                     PaneTop(mid, work) - outer.Y, paneWidth, PaneHeight);
            bridge = new Rectangle(left ? rail.Right : fullPane.Right, mid - outer.Y - 11, Gap, 22);
            Rectangle window = outer;
            if (dragging || docking)
                window.Offset(floatingRail.X - outerRail.X, floatingRail.Y - outerRail.Y);
            if (Bounds != window)
                Bounds = window;
            UpdateReveal(rail != oldRail || bridge != oldBridge);
            projectHits = Enumerable.Range(0, count)
                              .Select(i => new Rectangle(rail.X + 4, rail.Y + 25 + i * Step, RailWidth - 8, Step))
                              .ToArray();
            int contentX = fullPane.X;
            roleHits = Enumerable.Range(0, onPage)
                           .Select(i => new Rectangle(contentX + 12 + i * Cell, pane.Y + 37,
                                                      onPage == 1 ? paneWidth - 24 : Cell, 103))
                           .ToArray();
            prev = new Rectangle(contentX + paneWidth - 75, pane.Y + 5, 30, 29);
            next = new Rectangle(contentX + paneWidth - 43, pane.Y + 5, 30, 29);
        }
        private void UpdateReveal(bool geometryChanged)
        {
            double eased = reveal * reveal * (3 - 2 * reveal);
            int width = project == null ? 0 : (int)Math.Round(fullPane.Width * eased);
            Rectangle visible = new Rectangle(left ? fullPane.Left : fullPane.Right - width, fullPane.Top,
                                              width, fullPane.Height);
            if (!geometryChanged && visible == pane)
                return;
            pane = visible;
            using (GraphicsPath shape = RelayTheme.RoundedPath(rail, 28))
            {
                Region region = new Region(shape);
                if (width > 0)
                {
                    using (GraphicsPath wing = RelayTheme.RoundedPath(pane, 19)) region.Union(wing);
                    region.Union(bridge);
                }
                if (hitArea != null) hitArea.Dispose();
                hitArea = region;
            }
        }
        private bool PointerOnIsland(Point point)
        {
            if (hitArea != null && hitArea.IsVisible(point))
                return true;
            // Moving through the narrow gap keeps the panel open without capturing desktop clicks there.
            return pane.Width > 0 && point.X >= bridge.Left && point.X < bridge.Right &&
                   point.Y >= Math.Max(rail.Top, pane.Top) && point.Y < Math.Min(rail.Bottom, pane.Bottom);
        }
        private static int Clamp(int n, int min, int max)
        {
            return Math.Max(min, Math.Min(n, max));
        }
        private void ArmDrag(Point pointer, bool immediate)
        {
            pressScreen = pointer;
            floatingRail = new Point(Left + rail.Left, Top + rail.Top);
            gripOffset = new Point(pointer.X - floatingRail.X, pointer.Y - floatingRail.Y);
            dragging = immediate || docking;
            dragArmed = true;
            docking = false;
            Capture = true;
            if (dragging) BeginDragMotion();
        }
        private void BeginDragMotion()
        {
            dragging = true;
            nextProject = "";
            closing = true;
            awaySince = -1;
            tip.SetToolTip(this, "");
            tipText = "";
            Cursor = Cursors.SizeAll;
            lastTick = clock.Elapsed.TotalSeconds;
            timer.Interval = TransitionInterval;
        }
        private void PlaceFloatingRail(Point location)
        {
            floatingRail = location;
            Point topLeft = new Point(location.X - rail.Left, location.Y - rail.Top);
            if (Location != topLeft) Location = topLeft;
        }
        private void DragTo(Point pointer)
        {
            if (!dragging)
            {
                Size threshold = SystemInformation.DragSize;
                Rectangle deadZone = new Rectangle(pressScreen.X - threshold.Width / 2,
                    pressScreen.Y - threshold.Height / 2, threshold.Width, threshold.Height);
                if (deadZone.Contains(pointer)) return;
                BeginDragMotion();
            }
            PlaceFloatingRail(new Point(pointer.X - gripOffset.X, pointer.Y - gripOffset.Y));
        }
        private void FinishDrag(Point pointer, bool released)
        {
            if (!dragArmed) return;
            if (released) DragTo(pointer);
            dragArmed = false;
            bool moved = dragging;
            dragging = false;
            if (Capture) Capture = false;
            if (!moved) return;
            docking = true;
            if (released)
            {
                monitor = Screen.FromPoint(pointer);
                int mid = monitor.Bounds.Left + monitor.Bounds.Width / 2;
                if (pointer.X != mid) left = pointer.X < mid;
            }
            LayoutIsland(); // Recompute local geometry on the destination display, keeping the rail under the grip.
            Rectangle work = monitor.WorkingArea;
            if (released)
            {
                int travel = Math.Max(1, work.Height - rail.Height - 2 * EdgeMargin);
                anchor = Math.Max(0, Math.Min(1, (floatingRail.Y - work.Top - EdgeMargin) / (double)travel));
                SavePosition();
            }
            dockStart = floatingRail;
            Point end = DockPoint(work, rail.Height);
            double distance = Math.Sqrt(Math.Pow(end.X - dockStart.X, 2) + Math.Pow(end.Y - dockStart.Y, 2));
            dockDuration = Math.Min(.54, .22 + distance * .00022);
            lastTick = dockStarted = clock.Elapsed.TotalSeconds;
            Cursor = Cursors.Default;
            if (distance < 1)
            {
                docking = false;
                LayoutIsland();
            }
            timer.Interval = TransitionInterval;
        }
        private void GlideToEdge(double now)
        {
            Point end = DockPoint(monitor.WorkingArea, rail.Height);
            double u = Math.Min(1, Math.Max(0, (now - dockStarted) / dockDuration));
            // Zero speed at both ends, with a longer, gentle deceleration into the edge.
            double ease = 1 - Math.Pow(1 - u, 4) * (1 + 4 * u);
            PlaceFloatingRail(new Point((int)Math.Round(dockStart.X + (end.X - dockStart.X) * ease),
                                        (int)Math.Round(dockStart.Y + (end.Y - dockStart.Y) * ease)));
            if (u >= 1)
            {
                docking = false;
                LayoutIsland();
            }
        }
        private void TickIsland()
        {
            AdvanceFrame(clock.Elapsed.TotalSeconds, PointToClient(Cursor.Position));
        }
        private void AdvanceFrame(double t, Point pointer)
        {
            double dt = Math.Max(0, Math.Min(.04, t - lastTick));
            lastTick = t;
            flights.RemoveAll(f => t - f.Start > 2.2);
            if (docking) GlideToEdge(t);
            if (project != null && !menu.Visible)
            {
                if (dragging || docking)
                    closing = true;
                else if (PointerOnIsland(pointer))
                {
                    awaySince = -1;
                    closing = false;
                }
                else if (awaySince < 0)
                    awaySince = t;
                else if (t - awaySince > .34)
                    closing = true;
                bool switching = nextProject.Length != 0;
                double step = switching ? -dt / .13 : closing ? -dt / .22 : dt / .31;
                double progress = Math.Max(0, Math.Min(1, reveal + step));
                if (progress != reveal || ((closing || switching) && reveal == 0))
                {
                    reveal = progress;
                    if (reveal == 0 && (closing || switching))
                    {
                        ProjectSnapshot next = switching ? snapshot.Projects.FirstOrDefault(p => p.Id == nextProject) : null;
                        Collapse();
                        if (next != null) SelectProject(next);
                    }
                    UpdateReveal(false);
                }
            }
            int interval = docking || dragging || (project != null && (reveal < 1 || closing || nextProject.Length != 0)) ? TransitionInterval :
                           project == null && flights.Count == 0 ? 130 : 32;
            if (timer.Interval != interval)
                timer.Interval = interval;
            PresentIsland();
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            if (dragArmed)
            {
                DragTo(Cursor.Position);
                return;
            }
            if (docking || menu.Visible) return;
            int i = Array.FindIndex(projectHits, r => r.Contains(e.Location));
            string hint = "";
            if (i >= 0)
                SelectProject(snapshot.Projects[firstProject + i]);
            else if (rail.Contains(e.Location))
                hint = e.Y < rail.Top + 24                      ? "拖动悬浮岛，松开后贴边"
                       : snapshot.Projects.Count > railCapacity ? "向前或向后浏览项目"
                                                                : "会话详情";
            else if (pane.Contains(e.Location) && project != null)
            {
                int j = Array.FindIndex(roleHits, r => r.Contains(e.Location));
                if (j >= 0)
                {
                    RoleSnapshot role = project.Roles[firstRole + j];
                    hint = role.Role + " · 查看详情";
                }
            }
            if (hint != tipText)
            {
                tipText = hint;
                tip.SetToolTip(this, hint);
            }
            Cursor = rail.Contains(e.Location) && e.Y < rail.Top + 24 ? Cursors.SizeAll :
                     i >= 0 || pane.Contains(e.Location) ? Cursors.Hand : Cursors.Default;
        }
        protected override void OnMouseDown(MouseEventArgs e)
        {
            base.OnMouseDown(e);
            if (e.Button != MouseButtons.Left)
                return;
            if (menu.Visible || dismissingMenuClick)
            {
                if (menu.Visible) menu.Close(ToolStripDropDownCloseReason.AppClicked);
                return;
            }
            if (rail.Contains(e.Location))
            {
                if (e.Y < rail.Top + 24)
                    ArmDrag(PointToScreen(e.Location), true);
                else if (e.Y >= rail.Bottom - 21 && snapshot.Projects.Count > railCapacity)
                    ScrollProjects(e.X < rail.Left + RailWidth / 2 ? -1 : 1);
                else if (e.Y >= rail.Bottom - 21 || projectHits.Length == 0)
                    openDetails();
                else
                {
                    int i = Array.FindIndex(projectHits, r => r.Contains(e.Location));
                    if (i >= 0)
                    {
                        if (!docking) SelectProject(snapshot.Projects[firstProject + i]);
                        ArmDrag(PointToScreen(e.Location), false);
                    }
                }
                return;
            }
            if (project == null || reveal < .9 || !pane.Contains(e.Location))
                return;
            if (project.Roles.Count > roleHits.Length && (prev.Contains(e.Location) || next.Contains(e.Location)))
            {
                PageRoles(prev.Contains(e.Location) ? -1 : 1);
                return;
            }
            int index = Array.FindIndex(roleHits, r => r.Contains(e.Location));
            if (index >= 0)
                openRole(project.Id, project.Roles[firstRole + index].Address);
        }
        private int ProjectStyle(ProjectSnapshot p)
        {
            return icons.Resolve("project/" + p.Id, styles[p.Id]);
        }
        private int RoleSpecies(RoleSnapshot r)
        {
            return icons.Resolve("role/" + r.Address, species[r.Address]);
        }
        private void PrepareIconMenu(Point point)
        {
            menu.ClearIconChoices();
            int badge = Array.FindIndex(projectHits, r => r.Contains(point));
            if (badge >= 0)
            {
                ProjectSnapshot target = snapshot.Projects[firstProject + badge];
                string key = "project/" + target.Id;
                var options = Enumerable.Range(0, IslandArt.ProjectCount);
                menu.AddIconChoices(true, options, ProjectStyle(target), icons.HasChoice(key),
                                    index => SaveIcon(key, index));
            }
            else if (project != null && pane.Contains(point))
            {
                int pet = Array.FindIndex(roleHits, r => r.Contains(point));
                if (pet < 0) return;
                RoleSnapshot target = project.Roles[firstRole + pet];
                string key = "role/" + target.Address;
                menu.AddIconChoices(false, Enumerable.Range(0, IslandArt.Count), RoleSpecies(target),
                                    icons.HasChoice(key), index => SaveIcon(key, index));
            }
        }
        private void SaveIcon(string key, int? index)
        {
            try
            {
                icons.Set(key, index);
                PresentIsland();
            }
            catch (IOException ex) { ShowIconSaveError(ex); }
            catch (UnauthorizedAccessException ex) { ShowIconSaveError(ex); }
        }
        private void ShowIconSaveError(Exception ex)
        {
            MessageBox.Show(this, "无法保存图标选择。\n" + ex.Message, "Agents Chat",
                            MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
        protected override void OnMouseUp(MouseEventArgs e)
        {
            base.OnMouseUp(e);
            if (e.Button == MouseButtons.Left) FinishDrag(PointToScreen(e.Location), true);
            if (e.Button == MouseButtons.Right && !dragArmed && hitArea != null && hitArea.IsVisible(e.Location))
            {
                PrepareIconMenu(e.Location);
                menu.Show(this, e.Location);
            }
        }
        protected override void OnMouseCaptureChanged(EventArgs e)
        {
            base.OnMouseCaptureChanged(e);
            if (!Capture && dragArmed) FinishDrag(Cursor.Position, false);
        }
        protected override void OnMouseWheel(MouseEventArgs e)
        {
            base.OnMouseWheel(e);
            int delta = e.Delta < 0 ? 1 : -1;
            if (rail.Contains(e.Location) && snapshot.Projects.Count > railCapacity)
            {
                ScrollProjects(delta);
                OnMouseMove(e);
            }
            else if (project != null && pane.Contains(e.Location))
                PageRoles(delta);
        }
        private void ScrollProjects(int delta)
        {
            firstProject = Clamp(firstProject + delta, 0, snapshot.Projects.Count - railCapacity);
            Collapse();
            LayoutIsland();
            PresentIsland();
        }
        private void PageRoles(int direction)
        {
            firstRole =
                Clamp(firstRole + direction * roleCapacity, 0, Math.Max(0, project.Roles.Count - roleCapacity));
            LayoutIsland();
            PresentIsland();
        }

        private void PresentIsland()
        {
            if (!Visible || !IsHandleCreated || Disposing || IsDisposed || ClientSize.IsEmpty) return;
            if (surface == null || surface.Size != ClientSize)
            {
                IslandSurface nextSurface = new IslandSurface(ClientSize);
                if (surface != null) surface.Dispose();
                surface = nextSurface;
            }
            surface.Present(Handle, DrawIsland);
        }
        internal void DrawIsland(Graphics g)
        {
            g.Clear(Color.Transparent);
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.PixelOffsetMode = PixelOffsetMode.Half;
            g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
            double t = clock.Elapsed.TotalSeconds;
            if (pane.Width > 0)
                using (Brush join = new SolidBrush(Shell)) g.FillRectangle(join, bridge);
            RelayTheme.Card(g, rail, 28, Shell, Rim);
            using (Pen grip = new Pen(IslandArt.C(0x81798d), 2))
            {
                grip.StartCap = grip.EndCap = LineCap.Round;
                for (int i = 0; i < 2; i++)
                    g.DrawLine(grip, rail.X + 23, rail.Y + 10 + i * 4, rail.X + 37, rail.Y + 10 + i * 4);
            }
            for (int i = 0; i < projectHits.Length; i++)
            {
                ProjectSnapshot p = snapshot.Projects[firstProject + i];
                Rectangle hit = projectHits[i];
                int n = ProjectStyle(p);
                Color c = IslandArt.ProjectColor(n);
                Rectangle icon = new Rectangle(hit.X + 7, hit.Y + 5, IslandArt.EmblemSize, IslandArt.EmblemSize);
                Bitmap[] poses;
                if (!emblems.TryGetValue(n, out poses)) emblems[n] = poses = new Bitmap[3];
                int lift = (int)Math.Round(Math.Sin((t + n) * 1.7));
                if (poses[lift + 1] == null) poses[lift + 1] = IslandArt.CreateEmblem(n, lift);
                g.DrawImageUnscaled(poses[lift + 1], icon.Location);
                if (p.Id == active || p.Roles.Any(r => r.IsWorking))
                    using (Pen arc = new Pen(c, 2))
                    {
                        arc.StartCap = arc.EndCap = LineCap.Round;
                        int angle = p.Roles.Any(r => r.IsWorking) ? (int)(t * 58) % 360 : -72;
                        g.DrawArc(arc, icon.X - 3, icon.Y - 3, 44, 44, angle, p.Id == active ? 305 : 100);
                    }
                if (flights.Any(f => t >= f.Start && p.Roles.Any(r => r.Address == f.Message.Recipient)))
                    IslandArt.Paper(g, icon.Right - 2, icon.Bottom - 2, t);
            }
            if (projectHits.Length == 0)
                RelayTheme.Dot(g, new PointF(rail.X + 30, rail.Y + 37),
                               snapshot.Running ? RelayTheme.Accent : RelayTheme.Offline, 8);
            if (snapshot.Projects.Count > railCapacity)
            {
                TextAt(g, "‹", RelayTheme.Font10Medium, Muted,
                       new Rectangle(rail.X + 5, rail.Bottom - 22, 25, 20), true);
                TextAt(g, "›", RelayTheme.Font10Medium, Muted,
                       new Rectangle(rail.X + 30, rail.Bottom - 22, 25, 20), true);
            }
            else
                RelayTheme.Glyph(g, "more", new Rectangle(rail.X + 21, rail.Bottom - 20, 18, 15), Muted);
            if (project != null && pane.Width > 0)
                DrawPane(g, t);
        }
        private static void TextAt(Graphics g, string text, Font font, Color color, Rectangle box, bool center)
        {
            // GDI TextRenderer can clear the alpha channel under glyphs on a layered window.
            using (Brush ink = new SolidBrush(color))
            using (StringFormat format = new StringFormat(StringFormatFlags.NoWrap))
            {
                format.Alignment = center ? StringAlignment.Center : StringAlignment.Near;
                format.LineAlignment = StringAlignment.Center;
                format.Trimming = StringTrimming.EllipsisCharacter;
                g.DrawString(text, font, ink, box, format);
            }
        }
        private void DrawPane(Graphics g, double t)
        {
            Rectangle box = fullPane;
            Color accent = IslandArt.ProjectColor(ProjectStyle(project));
            GraphicsState saved = g.Save();
            using (LinearGradientBrush bg = new LinearGradientBrush(
                       box, IslandArt.C(0x302c3c), IslandArt.C(0x1f222c), 62))
                RelayTheme.FillRounded(g, bg, pane, 19);
            using (GraphicsPath clip = RelayTheme.RoundedPath(RelayTheme.Inset(pane, 1), 18))
                g.SetClip(clip, CombineMode.Intersect);
            bool paged = project.Roles.Count > roleHits.Length;
            int metaX = paged ? prev.Left - 88 : box.Right - 99;
            int busy = project.Roles.Count(r => r.IsWorking);
            RelayTheme.Dot(g, new PointF(box.Left + 18, box.Top + 21), accent, 7);
            TextAt(g, project.Name, RelayTheme.Font10Medium, Ink,
                   new Rectangle(box.Left + 28, box.Y + 4, metaX - box.Left - 28, 33), false);
            TextAt(g, busy > 0 ? busy + " 个干活ing" : project.Roles.Count + " 个角色", RelayTheme.Font8,
                   busy > 0 ? accent : Muted, new Rectangle(metaX, box.Y + 6, 86, 29), true);
            if (paged)
            {
                TextAt(g, "‹", RelayTheme.Font14, firstRole > 0 ? Ink : Muted, prev, true);
                TextAt(g, "›", RelayTheme.Font14, firstRole + roleHits.Length < project.Roles.Count ? Ink : Muted,
                       next, true);
            }
            int floor = box.Y + 97;
            DrawMeadow(g, box, floor, accent);
            if (roleHits.Length == 0)
                TextAt(g, "尚未配置角色", RelayTheme.Font9, Muted,
                       new Rectangle(box.X, box.Y + 45, box.Width, 68), true);
            for (int i = 0; i < roleHits.Length; i++)
            {
                RoleSnapshot r = project.Roles[firstRole + i];
                Rectangle hit = roleHits[i];
                // Neighbours nearest the rail arrive first, then settle onto the same baseline.
                int order = left ? i : roleHits.Length - 1 - i;
                double lag = .18 * order / Math.Max(1, roleHits.Length - 1);
                double entering = Math.Max(0, (reveal - lag) / (1 - lag));
                float offset = (float)((1 - entering) * (1 - entering));
                int drop = (int)Math.Round(6 * offset);
                hit.Offset((int)Math.Round((left ? -9 : 9) * offset), drop);
                int cx = hit.X + hit.Width / 2;
                double gesture = 0;
                foreach (Flight f in flights)
                {
                    double age = t - f.Start;
                    if (f.Message.Sender == r.Address)
                        gesture = Math.Max(gesture, 1 - Math.Abs(age - .14) * 4);
                    if (f.Message.Recipient == r.Address)
                        gesture = Math.Max(gesture, 1 - Math.Abs(age - 1.52) * 4);
                }
                int pet = RoleSpecies(r);
                IslandArt.Pet(g, pet, cx, floor + drop, t + (pet * .86), r.IsWorking,
                              r.VisualState == TrayVisualState.Offline, gesture);
                TextAt(g, r.Role, RelayTheme.Font8Medium, IslandArt.Blend(Ink, Floor, offset * .7f),
                       new Rectangle(hit.X, box.Y + 102 + drop, hit.Width, 18),
                       true);
                Color state = RelayTheme.StateColor(r.VisualState);
                TextAt(g, r.IslandLabel, RelayTheme.Font8, IslandArt.Blend(state, Floor, offset * .7f),
                       new Rectangle(hit.X, box.Y + 120 + drop, hit.Width, 18),
                       true);
            }
            DrawTraffic(g, box, t, accent);
            g.Restore(saved);
            using (Pen rim = new Pen(Rim)) RelayTheme.DrawRounded(g, rim, RelayTheme.Inset(pane, .5f), 19);
        }
        private static void DrawMeadow(Graphics g, Rectangle b, int floor, Color accent)
        {
            using (Brush ground = new SolidBrush(Floor)) g.FillRectangle(ground, b.X, floor + 1, b.Width, 3);
            using (Pen grass = new Pen(IslandArt.Blend(accent, Floor, .70f), 1)) for (int x = b.X + 9;
                                                                                      x < b.Right - 10; x += 37)
            {
                int h = (x - b.X) % 4 + 2;
                g.DrawLine(grass, x, floor, x - 2, floor - h);
                g.DrawLine(grass, x, floor, x + 3, floor - h - 1);
            }
            using (Brush stars = new SolidBrush(Color.FromArgb(48, accent))) for (int x = 17; x < b.Width - 12;
                                                                                  x += 39)
                g.FillRectangle(stars, b.X + x, b.Y + 41 + x * 17 % 33, 2, 2);
        }
        private bool Belongs(IslandMessage m)
        {
            return project.Roles.Any(r => r.Address == m.Sender || r.Address == m.Recipient);
        }
        private int EndpointX(string address, Rectangle b, bool sender)
        {
            int i = project.Roles.FindIndex(r => r.Address == address);
            if (i < 0)
                return sender ? b.Left + 8 : b.Right - 8;
            if (i < firstRole)
                return b.Left + 8;
            if (i >= firstRole + roleHits.Length)
                return b.Right - 8;
            Rectangle rct = roleHits[i - firstRole];
            return rct.X + rct.Width / 2;
        }
        private void DrawTraffic(Graphics g, Rectangle box, double t, Color accent)
        {
            IslandMessage last = snapshot.Traffic.LastOrDefault(Belongs);
            foreach (Flight f in flights)
            {
                double age = t - f.Start;
                if (age < 0 || age > 1.65 || !Belongs(f.Message))
                    continue;
                last = f.Message;
                float a = EndpointX(last.Sender, box, true), b = EndpointX(last.Recipient, box, false);
                if (a == b)
                    continue;
                float v = (float)Math.Max(0, Math.Min(1, (age - .15) / 1.34));
                float lift = Math.Min(33, 12 + Math.Abs(b - a) * .12f);
                float y = box.Y + 67;
                for (int j = 3; j >= 1; j--)
                {
                    float u = v - j * .035f;
                    if (u > 0)
                        RelayTheme.Dot(g, new PointF(a + (b - a) * u, y - lift * 4 * u * (1 - u)),
                                       Color.FromArgb(100 - j * 19, accent), 4 - j * .6f);
                }
                IslandArt.Paper(g, a + (b - a) * v, y - lift * 4 * v * (1 - v), v);
            }
            using (Pen pen = new Pen(IslandArt.C(0x413d4b)))
                g.DrawLine(pen, box.X + 14, box.Bottom - 32, box.Right - 14, box.Bottom - 32);
            if (last == null)
            {
                TextAt(g, "暂无协作消息", RelayTheme.Font8, Muted,
                       new Rectangle(box.X + 15, box.Bottom - 31, box.Width - 30, 28), false);
                return;
            }
            IslandArt.Paper(g, box.X + 20, box.Bottom - 17, 0);
            string label =
                DisplayAddress(last.Sender) + "  →  " + DisplayAddress(last.Recipient) + "   ·   " + last.Caption;
            TextAt(g, label, RelayTheme.Font8, Ink,
                   new Rectangle(box.X + 33, box.Bottom - 31, box.Width - 111, 27), false);
            TextAt(g, last.At.ToLocalTime().ToString("MM-dd HH:mm"), RelayTheme.Mono8, Muted,
                   new Rectangle(box.Right - 79, box.Bottom - 30, 72, 26), true);
        }
        private string DisplayAddress(string address)
        {
            RoleSnapshot r = project.Roles.FirstOrDefault(role => role.Address == address);
            return r == null ? address : r.Role;
        }
        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                timer.Dispose();
                if (surface != null) surface.Dispose();
                if (hitArea != null) hitArea.Dispose();
                foreach (Bitmap[] poses in emblems.Values)
                    foreach (Bitmap badge in poses)
                        if (badge != null) badge.Dispose();
                menu.Dispose();
                tip.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}

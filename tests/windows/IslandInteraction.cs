using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using System.Web.Script.Serialization;
using AgentsChatLauncher;

internal static class IslandInteraction
{
    private const BindingFlags Private = BindingFlags.NonPublic | BindingFlags.Instance;
    [DllImport("user32.dll")]
    private static extern IntPtr SendMessage(IntPtr window, int message, IntPtr wParam, IntPtr lParam);
    private static T Get<T>(ProjectIsland form, string name)
    {
        return (T) typeof(ProjectIsland).GetField(name, Private).GetValue(form);
    }
    private static void Invoke(ProjectIsland form, string name, params object[] args)
    {
        typeof(ProjectIsland).GetMethod(name, Private).Invoke(form, args);
    }
    private static MouseEventArgs Mouse(Rectangle r)
    {
        return new MouseEventArgs(MouseButtons.Left, 1, r.Left + r.Width / 2, r.Top + r.Height / 2, 0);
    }
    private static void Check(bool value, string detail)
    {
        if (!value)
        {
            Console.Error.WriteLine(detail);
            throw new Exception(detail);
        }
    }
    private static void Open(ProjectIsland form, int index)
    {
        Invoke(form, "OnMouseMove", Mouse(Get<Rectangle[]>(form, "projectHits")[index]));
        typeof(ProjectIsland).GetField("reveal", Private).SetValue(form, 1.0);
        Invoke(form, "LayoutIsland");
    }
    private static void CheckHoverFrames(ProjectIsland form, StatusSnapshot snapshot)
    {
        Rectangle bounds = form.Bounds;
        Rectangle badge = Get<Rectangle[]>(form, "projectHits")[0];
        Check(Get<Region>(form, "hitArea").IsVisible(Mouse(badge).Location), "collapsed project is reachable");
        typeof(ProjectIsland).GetField("lastTick", Private).SetValue(form, -10.0); // simulate a long idle
        Invoke(form, "OnMouseMove", Mouse(badge));
        Check(Get<double>(form, "lastTick") >= 0, "hover included time spent idle");
        double time = Get<double>(form, "lastTick");
        Rectangle target = Get<Rectangle>(form, "fullPane");
        Check(!Get<Region>(form, "hitArea").IsVisible(Mouse(target).Location), "unopened pane intercepts the desktop");
        int previousWidth = 0;
        for (int frame = 0; frame < 24; frame++)
        {
            time += .016;
            Invoke(form, "AdvanceFrame", time, Mouse(badge).Location);
            if (frame == 5) form.SetSnapshot(snapshot);
            Rectangle pane = Get<Rectangle>(form, "pane");
            Check(form.Bounds == bounds, "hover resized or moved the window: " + bounds + " -> " + form.Bounds);
            Check(Get<Rectangle[]>(form, "projectHits")[0] == badge, "project moved under the pointer");
            Check(pane.Width >= previousWidth, "opening animation reversed");
            if (frame == 0) Check(pane.Width > 0 && pane.Width < target.Width / 10, "opening jumped on first frame");
            previousWidth = pane.Width;
        }
        Check(previousWidth == target.Width, "project never fully opened");
        Rectangle gap = Get<Rectangle>(form, "bridge");
        Point crossing = new Point(gap.X + 2, gap.Bottom + 3);
        Check(!Get<Region>(form, "hitArea").IsVisible(crossing), "transparent gap should accept desktop clicks");
        for (int frame = 0; frame < 30; frame++)
            Invoke(form, "AdvanceFrame", time += .016, crossing);
        Check(Get<Rectangle>(form, "pane") == target, "panel closed while crossing the gap");
        Invoke(form, "OnMouseMove", Mouse(Get<Rectangle[]>(form, "projectHits")[1]));
        Check(form.Bounds == bounds && Get<Rectangle>(form, "pane") == target, "project switch cut the current frame");
        for (int frame = 0; frame < 36; frame++)
        {
            Invoke(form, "AdvanceFrame", time += .016, Mouse(Get<Rectangle[]>(form, "projectHits")[1]).Location);
            Check(form.Bounds == bounds, "window moved while switching projects");
        }
        Check(Get<string>(form,"active") == "small" && Get<double>(form,"reveal") == 1, "new panel did not settle");
        for (int frame = 0; frame < 48; frame++)
        {
            Invoke(form, "AdvanceFrame", time += .016, new Point(-10, -10));
            Check(form.Bounds == bounds, "window moved while closing");
        }
        Check(Get<Rectangle>(form, "pane").Width == 0 && !Get<Region>(form, "hitArea").IsVisible(Mouse(target).Location),
              "closed panel still intercepts the desktop");
    }
    private static Point RailOrigin(ProjectIsland form)
    {
        Rectangle rail = Get<Rectangle>(form, "rail");
        return new Point(form.Left + rail.Left, form.Top + rail.Top);
    }
    private static void CheckCoverage(Bitmap frame, Rectangle corner)
    {
        var levels = new HashSet<int>();
        for (int y = corner.Top; y < corner.Bottom; y++)
            for (int x = corner.Left; x < corner.Right; x++)
                levels.Add(frame.GetPixel(x, y).A);
        Check(levels.Contains(0) && levels.Contains(255) && levels.Count > 12,
              "curved edge lost intermediate alpha levels");
    }
    private static void CheckEdges(ProjectIsland form)
    {
        Check(form.Region == null, "a binary native window region would clip soft edges");
        using (Bitmap frame = new Bitmap(form.Width, form.Height, PixelFormat.Format32bppPArgb))
        {
            using (Graphics g = Graphics.FromImage(frame)) form.DrawIsland(g);
            Rectangle rail = Get<Rectangle>(form, "rail"), pane = Get<Rectangle>(form, "pane");
            CheckCoverage(frame, new Rectangle(rail.Location, new Size(28, 28)));
            CheckCoverage(frame, new Rectangle(pane.Location, new Size(19, 19)));
            for (int y = pane.Top + 8; y < pane.Top + 32; y++)
                for (int x = pane.Left + 25; x < pane.Left + 130; x++)
                    Check(frame.GetPixel(x, y).A == 255, "panel text punched holes through the surface");
            Rectangle gap = Get<Rectangle>(form, "bridge");
            Check(frame.GetPixel(gap.X + 2, gap.Y + 2).A == 255 &&
                  frame.GetPixel(gap.X + 2, gap.Bottom + 3).A == 0, "bridge or click-through gap lost");
        }
        using (Bitmap badge = IslandArt.CreateEmblem(0, 0))
            CheckCoverage(badge, new Rectangle(0, 0, badge.Width / 2, badge.Height / 2));
    }
    private static void CheckDrag(ProjectIsland form, StatusSnapshot snapshot, bool dropLeft)
    {
        Rectangle work = Screen.PrimaryScreen.WorkingArea;
        int mid = Screen.PrimaryScreen.Bounds.Left + Screen.PrimaryScreen.Bounds.Width / 2;
        Point offset = new Point(23, 13);
        Point origin = RailOrigin(form);
        Point press = new Point(origin.X + offset.X, origin.Y + offset.Y);
        Invoke(form, "OnMouseDown", new MouseEventArgs(MouseButtons.Left, 1, press.X-form.Left, press.Y-form.Top, 0));
        Check(Get<bool>(form,"dragging"), "grip did not start dragging");
        bool before = Get<bool>(form,"left");
        int y = work.Top + work.Height / 3;
        foreach (int x in new[] {mid - 40, mid + 50, dropLeft ? mid - 90 : mid + 90})
        {
            Invoke(form, "DragTo", new Point(x, y));
            form.SetSnapshot(snapshot);
            Check(RailOrigin(form) == new Point(x-offset.X, y-offset.Y) && Get<bool>(form,"left") == before,
                  "crossing the middle or refreshing snapped the rail before release");
        }
        Point start = RailOrigin(form);
        Point release = new Point(start.X+offset.X, start.Y+offset.Y);
        Invoke(form, "OnMouseUp", new MouseEventArgs(MouseButtons.Left, 1, release.X-form.Left, release.Y-form.Top, 0));
        Check(Get<bool>(form,"docking") && !Get<bool>(form,"dragging") && Get<bool>(form,"left") == dropLeft,
              "release did not select the half under the pointer");
        Check(RailOrigin(form) == start, "release teleported the rail");
        int endX = dropLeft ? work.Left+12 : work.Right-12-60;
        double t = Get<double>(form,"dockStarted"), duration = Get<double>(form,"dockDuration");
        int remaining = Math.Abs(start.X-endX);
        for (int frame=1; frame<=12; frame++)
        {
            Invoke(form, "AdvanceFrame", t+duration*frame/12.0+.00001, new Point(-10,-10));
            if (frame==5) form.SetSnapshot(snapshot);
            Point pos = RailOrigin(form);
            int distance = Math.Abs(pos.X-endX);
            Check(distance<=remaining && pos.Y==start.Y, "edge glide reversed or changed drop height");
            if(frame==3) Check(distance>0 && distance<Math.Abs(start.X-endX), "glide missed intermediate positions");
            remaining=distance;
        }
        Check(remaining==0 && !Get<bool>(form,"docking"), "glide did not land on the edge");
        string[] saved=File.ReadAllLines(Get<string>(form,"positionFile"));
        Check(saved[1]==(dropLeft?"left":"right"),"wrong edge was saved");
    }
    private static void CheckRegrab(ProjectIsland form)
    {
        Rectangle badge=Get<Rectangle[]>(form,"projectHits")[0];
        Point pick=form.PointToScreen(Mouse(badge).Location);
        Invoke(form,"OnMouseDown",Mouse(badge));
        Invoke(form,"DragTo",new Point(pick.X+1,pick.Y));
        Check(!Get<bool>(form,"dragging"),"clicking a badge unintentionally dragged it");
        Point drop=new Point(pick.X-130,pick.Y);
        Invoke(form,"DragTo",drop);
        Check(Get<bool>(form,"dragging"),"a badge could not be dragged");
        Invoke(form,"FinishDrag",drop,true);
        double t=Get<double>(form,"dockStarted");
        Invoke(form,"AdvanceFrame",t+.07,new Point(-10,-10));
        Point caught=RailOrigin(form);
        Point grip=new Point(caught.X+25,caught.Y+10);
        Invoke(form,"ArmDrag",grip,true);
        Invoke(form,"AdvanceFrame",t+.8,new Point(-10,-10));
        Check(RailOrigin(form)==caught && !Get<bool>(form,"docking"),"regrabbing the glide did not stop it");
        Invoke(form,"DragTo",new Point(grip.X-50,grip.Y));
        form.Capture=false;
        Check(!Get<bool>(form,"dragArmed") && Get<bool>(form,"docking"),"capture loss left the island stranded");
        Invoke(form,"AdvanceFrame",Get<double>(form,"dockStarted")+1,new Point(-10,-10));
    }
    [STAThread]
    public static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        CheckCatalogArtwork();
        CheckIconChoices();
        string selected = null;
        using (ProjectIsland form =
                   new ProjectIsland((p, r) => selected = r,
                                     () =>
                                     {},
                                     Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString())))
        {
            CheckAntigravityStates(form);
            StatusSnapshot snapshot = new StatusSnapshot { Running = true };
            ProjectSnapshot project = new ProjectSnapshot { Id = "fixture", Name = "Forest" };
            snapshot.Projects.Add(project);
            snapshot.Projects.Add(new ProjectSnapshot { Id = "small", Name = "Grove" });
            for (int i = 0; i < 72; i++)
                project.Roles.Add(RoleSnapshot.From(
                    project, new Dictionary<string, object> { { "role", "fox" + i },
                                                              { "address", "fixture.fox" + i },
                                                              { "online", true },
                                                              { "connection_status", "ready" } }));
            IslandMessage history =
                new IslandMessage { Id = "old", Sender = "fixture.fox1", Recipient = "fixture.fox2",
                                    Kind = "request", At = DateTime.UtcNow.AddSeconds(-5) };
            snapshot.Traffic.Add(history);
            form.SetSnapshot(snapshot);
            Check(Get<Rectangle>(form,"pane").Width == 0 && Get<IList>(form, "flights").Count == 0,
                  "collapsed rail / initial baseline");
            foreach (bool left in new[] { true, false })
            {
                typeof(ProjectIsland).GetField("left", Private).SetValue(form, left);
                Invoke(form, "LayoutIsland");
                CheckHoverFrames(form, snapshot);
            }
            CheckDrag(form, snapshot, false); // started on the right, crossed and came back
            CheckDrag(form, snapshot, true);
            CheckDrag(form, snapshot, false);
            CheckRegrab(form);
            File.Delete(Get<string>(form,"positionFile"));
            Open(form, 0);
            Check(form.Width > 60 && Get<Region>(form, "hitArea").IsVisible(Get<Rectangle[]>(form, "roleHits")[0].Location),
                  "hover expansion / visible hit area");
            CheckEdges(form);
            ToolTip tip = Get<ToolTip>(form, "tip");
            Invoke(form, "OnMouseMove", Mouse(Get<Rectangle[]>(form, "roleHits")[0]));
            Check(tip.GetToolTip(form).Contains("fox0"), "role hover shows role details");
            Invoke(form, "OnMouseMove", Mouse(Get<Rectangle[]>(form, "projectHits")[0]));
            Check(String.IsNullOrEmpty(tip.GetToolTip(form)), "project hover clears the previous tooltip");
            int pages = 0;
            while (Get<int>(form, "firstRole") + Get<Rectangle[]>(form, "roleHits").Length <
                       project.Roles.Count &&
                   pages++ < 72)
                Invoke(form, "OnMouseDown", Mouse(Get<Rectangle>(form, "next")));
            Invoke(form, "OnMouseDown", Mouse(Get<Rectangle[]>(form, "roleHits").Last()));
            Check(selected == "fixture.fox71", "last animal opens the correct role details");
            snapshot.Traffic.Add(new IslandMessage { Id = "new", Sender = history.Recipient,
                                                     Recipient = history.Sender, Kind = "result",
                                                     At = DateTime.UtcNow });
            form.SetSnapshot(snapshot);
            form.SetSnapshot(snapshot);
            Check(Get<IList>(form, "flights").Count == 1, "new message once across refreshes");
            form.SetSnapshot(new StatusSnapshot());
            form.SetSnapshot(snapshot);
            Check(Get<Rectangle>(form,"pane").Width == 0 && Get<IList>(form, "flights").Count == 0,
                  "offline and reconnect without replay");
            if (args.Length == 3)
            {
                var data = new JavaScriptSerializer().DeserializeObject(File.ReadAllText(args[0]))
                               as Dictionary<string, object>;
                StatusSnapshot live = StatusSnapshot.From(data);
                form.SetSnapshot(live);
                Open(form, live.Projects.FindIndex(p => p.Id == args[2]));
                using (Bitmap bitmap = new Bitmap(form.Width, form.Height, PixelFormat.Format32bppPArgb))
                {
                    using (Graphics g = Graphics.FromImage(bitmap)) form.DrawIsland(g);
                    bitmap.Save(args[1], ImageFormat.Png);
                }
            }
        }
        return 0;
    }
    private static ToolStripMenuItem IconChoices(ProjectIsland form, string hits, int index)
    {
        Invoke(form, "PrepareIconMenu", Mouse(Get<Rectangle[]>(form, hits)[index]).Location);
        return (ToolStripMenuItem)Get<IslandContextMenu>(form, "menu").Items[0];
    }
    private static void CheckMenuTextAlignment(IslandContextMenu menu)
    {
        if (SystemInformation.HighContrast) return; // Uses the system renderer and its native padding.
        menu.Size = menu.GetPreferredSize(Size.Empty);
        var painted = new HashSet<ToolStripItem>();
        ToolStripItemTextRenderEventHandler check = (sender, e) =>
        {
            if (String.IsNullOrEmpty(e.Text)) return;
            int top = e.TextRectangle.Top, bottom = e.Item.Height - e.TextRectangle.Bottom;
            Check(Math.Abs(top - bottom) <= 1, "menu text is off-centre in " + e.Text);
            painted.Add(e.Item);
        };
        menu.Renderer.RenderItemText += check;
        try
        {
            using (Bitmap bitmap = new Bitmap(menu.Width, menu.Height))
                menu.DrawToBitmap(bitmap, menu.ClientRectangle);
            Check(painted.Count == menu.Items.Count, "did not render all context menu rows");
        }
        finally { menu.Renderer.RenderItemText -= check; }
    }
    private static void CheckCatalogArtwork()
    {
        MethodInfo draw = typeof(IslandArt).GetMethod("DrawMotif", BindingFlags.Static | BindingFlags.NonPublic);
        foreach (bool project in new[] { true, false })
        {
            var masks = new List<byte[]>();
            var names = new HashSet<string>();
            int count = project ? IslandArt.ProjectCount : IslandArt.Count;
            for (int index = 0; index < count; index++)
            {
                string name = IslandArt.IconName(project, index);
                Check(names.Add(name), "duplicate icon name " + name);
                using (Bitmap bitmap = project ? new Bitmap(40, 40) : IslandArt.IconPreview(false, index))
                {
                    // A common ink removes all differences caused by recoloring the badge.
                    if (project)
                        using (Graphics g = Graphics.FromImage(bitmap))
                        {
                            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
                            draw.Invoke(null, new object[] { g, index, Color.Silver, 0 });
                        }
                    var mask = new byte[bitmap.Width * bitmap.Height];
                    for (int y = 0; y < bitmap.Height; y++)
                        for (int x = 0; x < bitmap.Width; x++)
                        {
                            Color pixel = bitmap.GetPixel(x, y);
                            mask[y * bitmap.Width + x] = project ? pixel.R : pixel.A;
                        }
                    for (int other = 0; other < masks.Count; other++)
                    {
                        int changed = 0;
                        for (int i = 0; i < mask.Length; i++)
                            if (Math.Abs(mask[i] - masks[other][i]) > 24) changed++;
                        Check(changed >= 36, name + " repeats the shape of " + IslandArt.IconName(project, other));
                    }
                    masks.Add(mask);
                }
            }
        }
    }
    private static void CheckIconChoices()
    {
        string folder = Directory.CreateDirectory(Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString())).FullName;
        string position = Path.Combine(folder, "position.txt");
        var snapshot = new StatusSnapshot { Running = true };
        foreach (string id in new[] { "alpha", "beta" })
        {
            var p = new ProjectSnapshot { Id = id, Name = id };
            foreach (string role in new[] { "lead", "test" })
                p.Roles.Add(RoleSnapshot.From(p, new Dictionary<string, object> {
                    { "role", role }, { "address", id + "." + role }, { "online", true } }));
            snapshot.Projects.Add(p);
        }
        try
        {
            string prefs = Path.Combine(folder, "island-icons.local.json");
            File.WriteAllText(prefs, "{\"project/overflow\":13,\"project/pinned\":8,\"role/old.cat\":3}");
            var previous = new IslandIconPreferences(prefs);
            Check(previous.Resolve("project/overflow", -1) == 8 && previous.Resolve("project/pinned", -1) == 8 &&
                  previous.Resolve("role/old.cat", -1) == 3, "could not read legacy icon choices");
            previous.Set("project/camera", 13);
            var current = new IslandIconPreferences(prefs);
            Check(current.Resolve("project/camera", -1) == 13 && current.Resolve("project/overflow", -1) == 8,
                  "camera and legacy generated forest changed after reload");
            int detailsOpened = 0;
            using (var form = new ProjectIsland((p, r) => {}, () => { detailsOpened++; }, position))
            {
                form.SetSnapshot(snapshot);
                // Windows asks the owner whether to activate before delivering a click to an inactive island.
                foreach (int buttonDown in new[] { 0x201, 0x204 })
                {
                    IntPtr result = SendMessage(form.Handle, 0x21, form.Handle, (IntPtr)((buttonDown << 16) | 1));
                    Check(result.ToInt32() == (buttonDown == 0x204 ? 1 : 3),
                          "only a context-menu click should activate the island");
                }
                // ToolStrip's message filter can raise Closed before the same mouse-down reaches the island.
                var menu = Get<IslandContextMenu>(form, "menu");
                typeof(IslandContextMenu).GetMethod("OnClosed", Private).Invoke(menu, new object[] {
                    new ToolStripDropDownClosedEventArgs(ToolStripDropDownCloseReason.AppClicked) });
                Rectangle rail = Get<Rectangle>(form, "rail");
                var footer = new MouseEventArgs(MouseButtons.Left, 1, rail.Left + rail.Width / 2, rail.Bottom - 10, 0);
                Invoke(form, "OnMouseDown", footer);
                Check(detailsOpened == 0, "dismissing the menu also opened details");
                Application.DoEvents();
                Invoke(form, "OnMouseDown", footer);
                Check(detailsOpened == 1, "the next independent click was ignored");
                ToolStripMenuItem first = IconChoices(form, "projectHits", 0);
                Check(first.Text == "更换项目图标" && first.DropDownItems.Count == 15 + 2 &&
                      first.DropDownItems[14].Image != null, "missing badge choices or preview");
                CheckMenuTextAlignment(Get<IslandContextMenu>(form, "menu"));
                // The snapshot can reorder while the user is choosing an icon.
                snapshot.Projects.Reverse();
                form.SetSnapshot(snapshot);
                first.DropDownItems[14].PerformClick();
                ToolStripMenuItem beta = IconChoices(form, "projectHits", 0);
                Check(!beta.DropDownItems[beta.DropDownItems.Count - 1].Enabled, "edited the wrong project after refresh");
                beta.DropDownItems[14].PerformClick();
                Open(form, 1); // alpha
                for (int i = 0; i < 2; i++)
                {
                    ToolStripMenuItem pets = IconChoices(form, "roleHits", i);
                    Check(pets.Text == "更换角色图标" && pets.DropDownItems.Count == 13 + 2 &&
                          pets.DropDownItems[12].Image != null, "missing animal choices or preview");
                    pets.DropDownItems[12].PerformClick();
                }
                Check(!Get<bool>(form, "dragArmed"), "icon picker started a drag");
            }
            using (var form = new ProjectIsland((p, r) => {}, () => {}, position))
            {
                form.SetSnapshot(snapshot);
                for (int i = 0; i < 2; i++)
                    Check(((ToolStripMenuItem)IconChoices(form, "projectHits", i).DropDownItems[14]).Checked,
                          "duplicate project icons did not survive restart");
                Open(form, 0); // beta has the same short role names
                ToolStripMenuItem beta = IconChoices(form, "roleHits", 0);
                Check(!beta.DropDownItems[beta.DropDownItems.Count - 1].Enabled, "role override leaked across projects");
                Invoke(form, "Collapse");
                Open(form, 1);
                ToolStripMenuItem lead = IconChoices(form, "roleHits", 0);
                Check(((ToolStripMenuItem)lead.DropDownItems[12]).Checked, "role icon did not survive restart");
                lead.DropDownItems[lead.DropDownItems.Count - 1].PerformClick();
                lead = IconChoices(form, "roleHits", 0);
                Check(!lead.DropDownItems[lead.DropDownItems.Count - 1].Enabled, "reset did not restore automatic selection");
                ToolStripMenuItem test = IconChoices(form, "roleHits", 1);
                Check(((ToolStripMenuItem)test.DropDownItems[12]).Checked && test.DropDownItems[test.DropDownItems.Count - 1].Enabled,
                      "reset affected the other role with the same animal");
                Invoke(form, "PrepareIconMenu", new Point(-1,-1));
                Check(Get<IslandContextMenu>(form, "menu").Items[0].Text == "会话详情", "ordinary menu kept a stale icon target");
                var many = new StatusSnapshot { Running = true };
                for (int i = 0; i < 17; i++)
                {
                    var p = new ProjectSnapshot { Id = "extra-" + i.ToString("00"), Name = "Extra " + i };
                    many.Projects.Add(p);
                }
                form.SetSnapshot(many);
                var styles = Get<Dictionary<string, int>>(form, "styles");
                var assigned = many.Projects.Select(p => styles[p.Id]).ToList();
                Check(assigned[0] == IslandArt.Hash(many.Projects[0].Id) % IslandArt.OriginalProjectCount,
                      "a new catalog reshuffled an existing automatic badge");
                Check(assigned.Take(15).Distinct().Count() == 15 && assigned.All(n => n >= 0 && n < 15),
                      "automatic assignment skipped a design or produced an invalid icon");
            }
        }
        finally
        {
            File.Delete(Path.Combine(folder, "island-icons.local.json"));
            Directory.Delete(folder);
        }
    }
    private static void CheckAntigravityStates(ProjectIsland form)
    {
        StatusSnapshot snapshot = new StatusSnapshot { Running = true };
        ProjectSnapshot project = new ProjectSnapshot { Id = "activity", Name = "Activity" };
        snapshot.Projects.Add(project);
        foreach (string session in new[] { "first", "second" })
            project.Roles.Add(RoleSnapshot.From(project, new Dictionary<string, object> {
                { "role", session }, { "address", "activity." + session }, { "session_id", session },
                { "desktop_transport", "antigravity_native" }, { "online", true },
                { "connection_status", "ready" }, { "session_activity", "idle" } }));
        form.SetSnapshot(snapshot);
        var report = new Dictionary<string, object> { { "first", "active" }, { "second", "active" } };
        form.SetAntigravityActivity(report);
        Check(snapshot.BusyRoles == 2 && project.Roles.All(r => r.IslandLabel == "干活ing"),
              "two native running sessions must animate with no bus tasks");
        report["first"] = "idle";
        project.Roles[0].RunningTasks = 1;
        form.SetAntigravityActivity(report);
        Check(snapshot.BusyRoles == 1 && project.Roles[0].IslandLabel == "摸鱼ing",
              "idle host session kept working after its run ended");
        report.Remove("second");
        form.SetAntigravityActivity(report);
        Check(snapshot.BusyRoles == 0 && project.Roles[1].IslandLabel == "动静不明",
              "missing host sample left stale work or labelled it idle");
        Check(RoleSnapshot.ConnectionText("connection_context_missing", true) == "等电梯" &&
              RoleSnapshot.ConnectionText("offline", true) == "睡着了" &&
              RoleSnapshot.ConnectionText("service_unavailable", true) == "带病罢工",
              "island connection labels do not match requested copy");
    }
}

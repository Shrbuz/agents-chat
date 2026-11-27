using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;

namespace AgentsChatLauncher
{
    // Keep native menu selection, keyboard navigation and outside-click dismissal.
    internal sealed class IslandContextMenu : ContextMenuStrip
    {
        private readonly Timer frames = new Timer { Interval = 15 };
        private readonly Stopwatch elapsed = new Stopwatch();
        private FadeImage closingImage;
        private double closingOpacity;
        private ToolStripMenuItem iconChoices;
        private bool Animate { get { return SystemInformation.IsMenuAnimationEnabled && !SystemInformation.HighContrast; } }

        internal IslandContextMenu()
        {
            ShowImageMargin = false;
            if (!SystemInformation.HighContrast)
            {
                BackColor = IslandArt.C(0x292733);
                ForeColor = IslandArt.C(0xf1eee8);
                Font = RelayTheme.Font9;
                Renderer = new IslandMenuRenderer();
                Padding = new Padding(5);
            }
            frames.Tick += delegate
            {
                if (closingImage != null)
                {
                    double u = Math.Min(1, elapsed.Elapsed.TotalSeconds / .12);
                    closingImage.Opacity = closingOpacity * (1 - u * u * (3 - 2 * u));
                    if (u >= 1) EndFade();
                }
                else if (Visible)
                {
                    double u = Math.Min(1, elapsed.Elapsed.TotalSeconds / .16);
                    Opacity = .08 + .92 * u * u * (3 - 2 * u);
                    if (u >= 1) frames.Stop();
                }
                else frames.Stop();
            };
        }

        protected override void OnItemAdded(ToolStripItemEventArgs e)
        {
            base.OnItemAdded(e);
            if (!SystemInformation.HighContrast)
                e.Item.Padding = new Padding(0, 5, 0, 5);
        }

        internal void ClearIconChoices()
        {
            if (iconChoices == null) return;
            Items.Remove(iconChoices);
            foreach (ToolStripItem item in iconChoices.DropDownItems)
            {
                Image preview = item.Image;
                item.Image = null;
                if (preview != null) preview.Dispose();
            }
            ToolStripDropDown popup = iconChoices.DropDown;
            iconChoices.Dispose();
            popup.Dispose();
            iconChoices = null;
        }

        internal void AddIconChoices(bool project, IEnumerable<int> options, int current, bool customized,
                                     Action<int?> select)
        {
            ToolStripDropDownMenu popup = new ToolStripDropDownMenu {
                BackColor = BackColor, ForeColor = ForeColor, Font = Font, Renderer = Renderer,
                ShowImageMargin = true, ShowCheckMargin = true, ImageScalingSize = new Size(28, 28)
            };
            iconChoices = new ToolStripMenuItem(project ? "更换项目图标" : "更换角色图标") { DropDown = popup };
            foreach (int index in options)
            {
                int choice = index;
                ToolStripMenuItem item = new ToolStripMenuItem(IslandArt.IconName(project, choice),
                    IslandArt.IconPreview(project, choice), delegate { select(choice); });
                item.Checked = choice == current;
                popup.Items.Add(item);
            }
            popup.Items.Add(new ToolStripSeparator());
            popup.Items.Add("恢复自动分配", null, delegate { select(null); }).Enabled = customized;
            Items.Insert(0, iconChoices);
        }

        protected override void OnSizeChanged(EventArgs e)
        {
            base.OnSizeChanged(e);
            if (Width <= 1 || Height <= 1 || SystemInformation.HighContrast) return;
            using (GraphicsPath shape = RelayTheme.RoundedPath(ClientRectangle, 10))
            {
                Region old = Region;
                Region = new Region(shape);
                if (old != null) old.Dispose();
            }
        }

        protected override void OnOpening(CancelEventArgs e)
        {
            EndFade();
            Opacity = Animate ? .08 : 1;
            base.OnOpening(e);
        }

        protected override void OnOpened(EventArgs e)
        {
            if (Animate) { elapsed.Restart(); frames.Start(); }
            base.OnOpened(e);
        }

        protected override void OnClosing(ToolStripDropDownClosingEventArgs e)
        {
            base.OnClosing(e);
            if (e.Cancel) return;
            EndFade();
            if (!Animate || !Visible || Opacity < .15 ||
                e.CloseReason == ToolStripDropDownCloseReason.AppFocusChange ||
                e.CloseReason == ToolStripDropDownCloseReason.CloseCalled) return;

            Bitmap pixels = new Bitmap(Width, Height);
            DrawToBitmap(pixels, ClientRectangle);
            closingOpacity = Opacity;
            closingImage = new FadeImage(pixels, Bounds, Region, closingOpacity);
        }

        protected override void OnClosed(ToolStripDropDownClosedEventArgs e)
        {
            base.OnClosed(e);
            if (closingImage == null) return;
            closingImage.Show();
            elapsed.Restart();
            frames.Start();
        }

        private void EndFade()
        {
            frames.Stop();
            if (closingImage == null) return;
            closingImage.Dispose();
            closingImage = null;
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing) { EndFade(); frames.Dispose(); ClearIconChoices(); }
            base.Dispose(disposing);
        }

        private sealed class IslandMenuRenderer : ToolStripProfessionalRenderer
        {
            protected override void OnRenderItemText(ToolStripItemTextRenderEventArgs e)
            {
                if (e.ToolStrip is IslandContextMenu)
                {
                    // WinForms adds the item's padding to its hit area but keeps the native text slot at the top.
                    Rectangle text = e.TextRectangle;
                    text.Y = (e.Item.Height - text.Height) / 2;
                    e.TextRectangle = text;
                }
                base.OnRenderItemText(e);
            }
            protected override void OnRenderToolStripBackground(ToolStripRenderEventArgs e)
            {
                e.Graphics.Clear(e.ToolStrip.BackColor);
            }
            protected override void OnRenderImageMargin(ToolStripRenderEventArgs e)
            {
                using (Brush fill = new SolidBrush(e.ToolStrip.BackColor))
                    e.Graphics.FillRectangle(fill, e.AffectedBounds);
            }
            protected override void OnRenderArrow(ToolStripArrowRenderEventArgs e)
            {
                e.ArrowColor = e.Item.ForeColor;
                base.OnRenderArrow(e);
            }
            protected override void OnRenderItemCheck(ToolStripItemImageRenderEventArgs e)
            {
                Rectangle box = e.ImageRectangle;
                float x = box.Left + box.Width / 2f, y = box.Top + box.Height / 2f;
                GraphicsState saved = e.Graphics.Save();
                e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
                using (Pen tick = new Pen(IslandArt.C(0x81dcc1), 1.8f))
                {
                    tick.StartCap = tick.EndCap = LineCap.Round;
                    e.Graphics.DrawLines(tick, new[] { new PointF(x - 4, y), new PointF(x - 1, y + 3),
                                                       new PointF(x + 4, y - 3) });
                }
                e.Graphics.Restore(saved);
            }
            protected override void OnRenderToolStripBorder(ToolStripRenderEventArgs e)
            {
                e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
                using (Pen edge = new Pen(IslandArt.C(0x575363)))
                    RelayTheme.DrawRounded(e.Graphics, edge, RelayTheme.Inset(e.ToolStrip.ClientRectangle, .5f), 10);
            }
            protected override void OnRenderMenuItemBackground(ToolStripItemRenderEventArgs e)
            {
                if (!e.Item.Selected) return;
                e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
                using (Brush hover = new SolidBrush(IslandArt.C(0x464150)))
                    RelayTheme.FillRounded(e.Graphics, hover, new Rectangle(Point.Empty, e.Item.Size), 6);
            }
        }

        // Only the painted copy fades out. The live menu has already released input and focus.
        private sealed class FadeImage : Form
        {
            private readonly Bitmap pixels;
            internal FadeImage(Bitmap image, Rectangle bounds, Region shape, double opacity)
            {
                pixels = image;
                FormBorderStyle = FormBorderStyle.None;
                StartPosition = FormStartPosition.Manual;
                ShowInTaskbar = false;
                TopMost = true;
                Bounds = bounds;
                if (shape != null) Region = shape.Clone();
                Opacity = opacity;
            }
            protected override bool ShowWithoutActivation { get { return true; } }
            protected override CreateParams CreateParams
            {
                get
                {
                    CreateParams cp = base.CreateParams;
                    // Layered + transparent passes mouse input through to the desktop below.
                    cp.ExStyle |= 0x00080000 | 0x20 | 0x80 | 0x08000000;
                    return cp;
                }
            }
            protected override void OnPaintBackground(PaintEventArgs e) { e.Graphics.DrawImageUnscaled(pixels, 0, 0); }
            protected override void Dispose(bool disposing)
            {
                if (disposing) pixels.Dispose();
                base.Dispose(disposing);
            }
        }
    }
}

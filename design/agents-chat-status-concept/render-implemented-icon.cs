using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;

namespace AgentsChatLauncher
{
    internal static class RelayIconPreview
    {
        [STAThread]
        private static void Main()
        {
            byte[] icoBytes = RelayThemeIcon.CreateIcoBytes(TrayVisualState.Running);
            File.WriteAllBytes("launcher/AgentsChat.ico", icoBytes);
            using (Bitmap bitmap = new Bitmap(720, 292, PixelFormat.Format32bppArgb))
            using (Graphics graphics = Graphics.FromImage(bitmap))
            {
                graphics.SmoothingMode = SmoothingMode.AntiAlias;
                graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
                graphics.Clear(RelayTheme.Page);
                TrayVisualState[] states = new TrayVisualState[] {
                    TrayVisualState.Running,
                    TrayVisualState.Busy,
                    TrayVisualState.Warning,
                    TrayVisualState.Offline
                };
                string[] labels = new string[] { "运行中", "执行中", "需留意", "未运行" };
                for (int index = 0; index < states.Length; index++)
                {
                    Rectangle card = new Rectangle(24 + index * 172, 24, 148, 148);
                    RelayTheme.Card(graphics, card, 16, RelayTheme.Surface1, RelayTheme.Line);
                    RelayTheme.DrawRelayMark(graphics, new RectangleF(card.X + 42, card.Y + 24, 64, 64),
                        RelayTheme.StateColor(states[index]), true);
                    RelayTheme.TextLine(graphics, labels[index], RelayTheme.Font9Medium,
                        RelayTheme.StateColor(states[index]), new Rectangle(card.X, card.Y + 102, card.Width, 22),
                        System.Windows.Forms.TextFormatFlags.HorizontalCenter |
                        System.Windows.Forms.TextFormatFlags.VerticalCenter);
                }
                int[] sizes = new int[] { 16, 20, 24, 32, 48, 64 };
                int x = 40;
                foreach (int size in sizes)
                {
                    RelayTheme.DrawRelayMark(graphics, new RectangleF(x, 204 + (64 - size) / 2F, size, size),
                        RelayTheme.Accent, true);
                    RelayTheme.TextLine(graphics, Convert.ToString(size), RelayTheme.Mono8, RelayTheme.Text3,
                        new Rectangle(x - 8, 264, size + 16, 16),
                        System.Windows.Forms.TextFormatFlags.HorizontalCenter);
                    x += size + 54;
                }
                bitmap.Save("design/agents-chat-status-concept/preview-implemented-icons.png", ImageFormat.Png);
            }
        }
    }
}

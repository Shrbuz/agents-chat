using System;
using System.ComponentModel;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

namespace AgentsChatLauncher
{
    // GDI+ and the desktop compositor share this premultiplied, top-down pixel buffer.
    internal sealed class IslandSurface : IDisposable
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct BitmapHeader
        {
            internal uint Size;
            internal int Width, Height;
            internal ushort Planes, BitCount;
            internal uint Compression, SizeImage;
            internal int XPelsPerMeter, YPelsPerMeter;
            internal uint ColorsUsed, ColorsImportant;
        }
        [StructLayout(LayoutKind.Sequential, Pack = 1)]
        private struct BlendFunction
        {
            internal byte Operation, Flags, Opacity, AlphaFormat;
        }
        [DllImport("gdi32.dll", SetLastError = true)]
        private static extern IntPtr CreateCompatibleDC(IntPtr dc);
        [DllImport("gdi32.dll", SetLastError = true)]
        private static extern IntPtr CreateDIBSection(IntPtr dc, ref BitmapHeader header, uint usage,
                                                     out IntPtr bits, IntPtr section, uint offset);
        [DllImport("gdi32.dll", SetLastError = true)]
        private static extern IntPtr SelectObject(IntPtr dc, IntPtr obj);
        [DllImport("gdi32.dll")]
        private static extern bool DeleteDC(IntPtr dc);
        [DllImport("gdi32.dll")]
        private static extern bool DeleteObject(IntPtr obj);
        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool UpdateLayeredWindow(IntPtr window, IntPtr screen, IntPtr destination,
            ref Size size, IntPtr source, ref Point origin, uint colorKey, ref BlendFunction blend, uint flags);

        private IntPtr dc, dib, original;
        private Bitmap pixels;
        internal readonly Size Size;
        internal IslandSurface(Size size)
        {
            Size = size;
            try
            {
                dc = CreateCompatibleDC(IntPtr.Zero);
                if (dc == IntPtr.Zero) throw new Win32Exception();
                BitmapHeader header = new BitmapHeader {
                    Size = (uint)Marshal.SizeOf(typeof(BitmapHeader)), Width = size.Width,
                    Height = -size.Height, Planes = 1, BitCount = 32
                };
                IntPtr bits;
                dib = CreateDIBSection(dc, ref header, 0, out bits, IntPtr.Zero, 0);
                if (dib == IntPtr.Zero) throw new Win32Exception();
                original = SelectObject(dc, dib);
                if (original == IntPtr.Zero) throw new Win32Exception();
                pixels = new Bitmap(size.Width, size.Height, size.Width * 4, PixelFormat.Format32bppPArgb, bits);
            }
            catch { Dispose(); throw; }
        }
        internal void Present(IntPtr window, Action<Graphics> draw)
        {
            using (Graphics g = Graphics.FromImage(pixels)) draw(g);
            Point origin = Point.Empty;
            Size size = Size;
            BlendFunction blend = new BlendFunction { Opacity = 255, AlphaFormat = 1 };
            if (!UpdateLayeredWindow(window, IntPtr.Zero, IntPtr.Zero, ref size, dc, ref origin, 0, ref blend, 2))
                throw new Win32Exception();
        }
        public void Dispose()
        {
            if (pixels != null) { pixels.Dispose(); pixels = null; }
            if (original != IntPtr.Zero) { SelectObject(dc, original); original = IntPtr.Zero; }
            if (dib != IntPtr.Zero) { DeleteObject(dib); dib = IntPtr.Zero; }
            if (dc != IntPtr.Zero) { DeleteDC(dc); dc = IntPtr.Zero; }
        }
    }
}

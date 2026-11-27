using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;

namespace AgentsChatLauncher
{
    // Small, hand-drawn sprites. Every character is one pixel in the master artwork.
    internal static class IslandArt
    {
        internal const int OriginalProjectCount = 10;
        internal const int OriginalAnimalCount = 8;
        private static readonly Color[] Colors = { C(0x81dcc1), C(0xffac80), C(0xb9a4ff), C(0x8abfff),
                                                    C(0xf3ce79), C(0xfa9db2), C(0xb9dd84), C(0x7ad8ef),
                                                    C(0xe6a9ed), C(0xceaa87), C(0xff846f), C(0xf5d79b),
                                                    C(0x85b7a1), C(0xdba4cb), C(0xefb85e) };
        private static readonly string[][] Pets = {
            new[] {// red fox
                   "..##......##.......", "..#o#....#o#.......", "..#ow####wo#.......", "..#ooooooos#.......",
                   ".#oo+oooooos#......", "#ooowoooowoos#...##", "#owwkwwwkwwos#..#w#", ".#wwwwkwwwws#..#ww#",
                   "..##wwwwws##..#oow#", "...#ooowoos#..#oos#", "...#oowwwoos###os#.", "..#ooswwwsoossos#..",
                   "..#osswwwssooos#...", "..#wosssosww###....", "...###..####......."
            },
            new[] {// lop-eared rabbit
                   "...###..##.......", "..#ww#.#ww#......", "..#wp#.#pw#......", "..#wp#.#pw#......",
                   "..#ws###sw#......", "..#wwwwwww##.....", ".#w+wwwwwwws#....", "#wwwkwwwkwwws#...",
                   "#wwppwpwppwws#...", ".#wwwwwwwwws#....", "..###wwwss##.....", "...#wwwwwws#..##.",
                   "...#wswwwwss##ww#", "..#wwwwsswwws#ws#", "..####..#####.##."
            },
            new[] {// bear cub
                   "..###....###.....", ".#oso#..#oso#....", ".#os#####sos#....", "..#oo++oooos#....",
                   ".#oo+ooooooos#...", "#oooooooooooos#..", "#oookwwwkoooss#..", "#ooppwkwpoooss#..",
                   ".#ooowwwoooss#...", "..##oooooos##....", "...#ooowwoos#....", "..#ooowwwwoos##..",
                   "..#ooswwwwsoos#..", "..#ossoossosss#..", "..####...#####..."
            },
            new[] {// calico cat and upright striped tail
                   "..##.....##.......", "..#w#...#o#.......", "..#wp###po#.......", "..#wwwwooos#......",
                   ".#w+wwwoooos#.....", "#wwwwwwwwooss#..##", "#wwwkwwwkwwss#.#oo#", "#wwppwpwppwws#.#ss#",
                   ".#wwwwwwwwws#..#ww#", "..##wwwwss##...#oo#", "...#wwwwwoos#..#ss#", "...#wswwwwos#.#ww#.",
                   "..#wwswwwwoos#oo#..", "..#wwswwsswos###...", "..####...####......"
            },
            new[] {// frog, high eyes and broad smiling mouth
                   "...###....###....", "..#www#..#www#...", ".#wwkw####wkw#...", "..#www+ooowww#...",
                   ".#oo++ooooooos#..", "#ooooooooooooos#.", "#oppkwwwwwkppos#.", ".#ooowwwwwooss#..",
                   "..##ooooooos##...", "...#oowwwwoos#...", "..#ooswwwwsoos#..", ".#oooswwwwsooos#.",
                   ".#sssssssssssss#.", "..####....#####.."
            },
            new[] {// penguin chick, flippers and tangerine feet
                   "......###........", "....##ooo##......", "...#oo++ooos#....", "..#ooooooooss#...",
                   "..#oowwwwwoss#...", ".#owwkwwwkwoss#..", ".#owwwpppwwoss#..", "..#owwwpwwwos#...",
                   "..#oowwwwwoss#...", ".#oowwwwwwwoss#..", "#ooswwwwwwwsoos#.", "#os#wwwwwwws#os#.",
                   ".##swwwwwwwss##..", "...#ssssssss#....", "..#ppp####ppp#...", "..####....####..."
            },
            new[] {// raccoon and ringed tail
                   "..###....###.......", "..#ws####sw#.......", "..#soooooss#.......", ".#oo++ooooos#......",
                   "#ooowwwwwwoos#.....", "#ossswwwsssos#...##", "#oswkswskwsos#..#ww#", ".#ooswkwsoss#..#sss#",
                   "..#wwwwwwws#..#www#.", "...##sss###...#ss#..", "...#ooowwoos##ww#...", "..#ooswwwsoosss#....",
                   "..#ossswwsss###.....", "..#####..####......."
            },
            new[] {// round barn owl
                   "..##......##.....", "..#o##..##o#.....", "..#ooo##ooos#....", ".#o++ooooooos#...",
                   "#oowwwwwwwwoos#..", "#owwkwwwwkwwoos#.", "#owwwwppwwwwoss#.", ".#oowwwpwwwoos#..",
                   "..#oowwwwwoos#...", "..#oswosowsos#...", ".#osswwowwssos#..", ".#ossswwwsssss#..",
                   "..##ssssssss##...", "...#pp#..#pp#....", "...###....###...."
            },
            new[] {// hedgehog, jagged quills and a tapered cream muzzle
                   "......#..#.........", "...#.#+##+#.#......", "..#+##osso##o#.#...",
                   ".#ooso+os+oso##o#..", "..#s+ooso+ooso+s#..", ".#ooso+oso+oossoo#.",
                   "##s+ooso+oos#wwwos#", ".#os+oso+os#wpwwww#", "#osso+osoos#wwkwww#",
                   ".#sosoosso#wwwwwwwk", "..#ssssos#wwwwwww#.", "...##ss#wwwwww###..",
                   "....#pp######pp#..."
            },
            new[] {// whale calf, a forked tail and a two-sided spout
                   "....++..++.........", "......++...........", ".......+...........",
                   "....######.........", "..##++oooo##.......", ".#++oooooooos#.....",
                   "#oooooooooooos#.#.#", "#ookooooooooss##o#o", "#owwwwoooossss#oso#",
                   ".#wwwwwooosssooo##.", "..#wwwww#oo#sss#...", "...##wwwsoo#s##....",
                   ".....####so##......", ".........##........"
            },
            new[] {// garden snail, two eye stalks ahead of a spiral shell
                   ".........#####.....", ".......##++++o##...", "......#++oooooos#..",
                   "......#+ossssooos#.", ".kw..k#oos++sooos#.", "..w..w#ooso+sooos#.",
                   "..wwww#oosooososs#.", ".#wwww#ooossssoos#.", ".#wpwww#oooooooss#.",
                   "..#wwwww#sssssss#..", "...#wwwww#######.##", "...#wwwwwwwwwwwww#.",
                   "....#############.."
            },
            new[] {// elephant calf in profile, broad pink ear, upturned trunk
                   ".........#####.....", ".......##++++o###..", "......#oo+++ooosso#",
                   ".....#ooo#ppp#ooos#", ".....#oo#pppps#oos#", ".....#ko#pppps#oos#",
                   ".##..#ooo#pps#ooos#", ".#o#.#ooww###oooos#", ".#o##oowwwooooooss#",
                   "..#ooooo#oososssos#", "...####.#oos##soos#", "........#oo#..#oos#",
                   "........#ww#..#wws#", "........####..####."
            },
            new[] {// shore crab, two open pincers, stalk eyes and splayed legs
                   ".#..#.........#..#.", "#o##+#.......#+##o#", "#oooo#.......#oooo#",
                   ".#oos#..k.k..#soo#.", "..#os#..w.w..#so#..", "...#o###ooo###o#...",
                   ".##.#oo++oooos#.##.", "#oo#ooooo+oooos#oo#", "..#ooopwwwwpooos#..",
                   "##.#sooooooooos#.##", "...#s#sssssss#s#...", ".##s#.#######.#s##.",
                   "#s#.............#s#"
            }
        };
        private static readonly int[] Fur = { 0xf39954, 0xe5d4ca, 0xb88a69, 0xd79865,
                                              0x8bcb7d, 0x7896b2, 0xabb3c1, 0xc18b65,
                                              0xaa795b, 0x70accc, 0xcb8957, 0x91a8b9, 0xe98162 };
        private static readonly Bitmap[,] Sprites = MakeSprites();
        private static readonly string[] AnimalNames = { "赤狐", "垂耳兔", "小棕熊", "三花猫", "青蛙", "企鹅",
                                                          "浣熊", "猫头鹰", "刺猬", "小鲸鱼", "蜗牛", "小象", "螃蟹" };
        private static readonly string[] ProjectNames = { "薄荷弯月", "杏橙雪峰", "淡紫行星", "晴海帆船", "金色晶石",
            "玫瑰热气球", "四叶草", "潮汐水母", "丁香松林", "黄铜钥匙", "岬角灯塔", "田野风车", "蒸汽机车", "胶片相机", "流沙沙漏" };
        internal static int ProjectCount { get { return ProjectNames.Length; } }
        internal static string IconName(bool project, int index)
        {
            return project ? ProjectNames[index] : AnimalNames[index];
        }
        internal static Bitmap IconPreview(bool project, int index)
        {
            if (project) return CreateEmblem(index, 0);
            Bitmap sprite = Sprites[index, 0];
            Bitmap image = new Bitmap(EmblemSize, EmblemSize, PixelFormat.Format32bppPArgb);
            using (Graphics g = Graphics.FromImage(image))
            {
                g.InterpolationMode = InterpolationMode.NearestNeighbor;
                g.PixelOffsetMode = PixelOffsetMode.Half;
                int w = 2 * sprite.Width, h = 2 * sprite.Height;
                g.DrawImage(sprite, new Rectangle((image.Width - w) / 2, (image.Height - h) / 2, w, h),
                            0, 0, sprite.Width, sprite.Height, GraphicsUnit.Pixel);
            }
            return image;
        }
        internal static int Count
        {
            get {
                return Pets.Length;
            }
        }
        internal static Color C(int rgb)
        {
            return Color.FromArgb(rgb >> 16, (rgb >> 8) & 255, rgb & 255);
        }
        internal static Color ProjectColor(int index)
        {
            return Colors[index];
        }
        internal static Color Blend(Color a, Color b, float f)
        {
            return Color.FromArgb((int)(a.R + (b.R - a.R) * f), (int)(a.G + (b.G - a.G) * f),
                                  (int)(a.B + (b.B - a.B) * f));
        }
        internal static int Hash(string key)
        {
            uint hash = 2166136261;
            foreach (char c in key)
                hash = unchecked((hash ^ c) * 16777619);
            return (int)(hash & 0x7fffffff);
        }

        private static Bitmap[,] MakeSprites()
        {
            Bitmap[,] result = new Bitmap[Pets.Length, 2];
            for (int n = 0; n < Pets.Length; n++)
                for (int dim = 0; dim < 2; dim++)
                {
                    string[] rows = Pets[n];
                    int width = 0;
                    foreach (string line in rows)
                        width = Math.Max(width, line.TrimEnd('.').Length);
                    Bitmap bmp = new Bitmap(width, rows.Length);
                    Color fur = C(Fur[n]);
                    Color[] inks = { C(0x272637),
                                     fur,
                                     Blend(fur, C(0x564254), .43f),
                                     Blend(fur, Color.White, .40f),
                                     C(0xffe9cd),
                                     C(0x322e3b),
                                     n == 5 || n == 7 ? C(0xf5ac62) : C(0xeaa39c) };
                    for (int y = 0; y < rows.Length; y++)
                        for (int x = 0; x < Math.Min(width, rows[y].Length); x++)
                        {
                            int ink = "#os+wkp".IndexOf(rows[y][x]);
                            if (ink >= 0)
                                bmp.SetPixel(x, y, dim == 0 ? inks[ink] : Blend(inks[ink], C(0x4c5060), .56f));
                        }
                    result[n, dim] = bmp;
                }
            return result;
        }

        internal static void Pet(Graphics g, int species, int cx, int floor, double t, bool busy, bool offline,
                                 double gesture)
        {
            double beat = busy ? Math.Abs(Math.Sin(t * 7)) : (1 + Math.Sin(t * 2)) * .5;
            int bounce = offline ? 0 : (int)Math.Round(beat * (busy ? 3 : 1) + gesture * 5);
            Bitmap sprite = Sprites[species, offline ? 1 : 0];
            int y = floor - sprite.Height * 3 - bounce;
            using (Brush shadow = new SolidBrush(C(0x262a35))) g.FillEllipse(shadow, cx - 22, floor - 4, 44, 7);
            GraphicsState save = g.Save();
            g.SmoothingMode = SmoothingMode.None;
            g.InterpolationMode = InterpolationMode.NearestNeighbor;
            g.PixelOffsetMode = PixelOffsetMode.Half;
            g.DrawImage(sprite, new Rectangle(cx - sprite.Width * 3 / 2, y, sprite.Width * 3, sprite.Height * 3),
                        0, 0, sprite.Width, sprite.Height, GraphicsUnit.Pixel);
            if (busy)
            {
                // A cream notebook, a moving pencil and a paw at its tip.
                g.FillRectangle(Brushes.Bisque, cx + 5, floor - 4, 19, 4);
                using (Pen edge = new Pen(C(0xba997e), 2)) g.DrawLine(edge, cx + 5, floor, cx + 24, floor);
                int tip = cx + 10 + (int)(beat * 6);
                using (Pen pencil = new Pen(C(0xf5b85d), 3))
                    g.DrawLine(pencil, tip, floor - 5, tip + 6, floor - 14);
                using (Brush paw = new SolidBrush(C(Fur[species])))
                    g.FillRectangle(paw, tip - 2, floor - 12, 6, 5);
            }
            if (offline)
                using (Pen z = new Pen(C(0x9a9eb2), 1.6f))
                {
                    int x = cx + 18, top = y - 1;
                    g.DrawLines(z, new[] { new Point(x, top), new Point(x + 5, top), new Point(x, top + 5),
                                           new Point(x + 5, top + 5) });
                }
            g.Restore(save);
        }

        internal const int EmblemSize = 38;
        internal static Bitmap CreateEmblem(int motif, int lift)
        {
            const int supersample = 4;
            using (Bitmap master = new Bitmap(EmblemSize * supersample, EmblemSize * supersample,
                                              PixelFormat.Format32bppPArgb))
            {
                using (Graphics g = Graphics.FromImage(master))
                {
                    g.SmoothingMode = SmoothingMode.AntiAlias;
                    g.ScaleTransform(master.Width / 40f, master.Height / 40f);
                    DrawMotif(g, motif, ProjectColor(motif), lift);
                }
                Bitmap badge = new Bitmap(EmblemSize, EmblemSize, PixelFormat.Format32bppPArgb);
                using (Graphics g = Graphics.FromImage(badge))
                {
                    g.InterpolationMode = InterpolationMode.HighQualityBicubic;
                    g.PixelOffsetMode = PixelOffsetMode.Half;
                    g.DrawImage(master, new Rectangle(0, 0, badge.Width, badge.Height),
                                0, 0, master.Width, master.Height, GraphicsUnit.Pixel);
                }
                return badge;
            }
        }
        private static void DrawMotif(Graphics g, int motif, Color accent, int lift)
        {
            Color deep = Blend(accent, C(0x181725), .79f), light = Blend(accent, Color.White, .47f);
            GraphicsState artwork = g.Save();
            using (GraphicsPath clip = new GraphicsPath())
            {
                clip.AddEllipse(1, 1, 38, 38);
                g.SetClip(clip, CombineMode.Intersect);
                using (LinearGradientBrush sky = new LinearGradientBrush(new Point(0, 0), new Point(9, 40), deep,
                                                                         Blend(deep, accent, .27f)))
                    g.FillEllipse(sky, 0, 0, 40, 40);
                using (Brush star = new SolidBrush(light)) using (Brush main = new SolidBrush(accent)) using (
                    Brush shade = new SolidBrush(Blend(accent, deep, .40f)))
                {
                    g.FillRectangle(star, 8, 10 + lift, 2, 2);
                    g.FillRectangle(star, 29, 7 - lift, 2, 2);
                    switch (motif)
                    {
                    case 0: // crescent and ridge
                        g.FillEllipse(star, 16, 8 + lift, 14, 14);
                        using (Brush cut = new SolidBrush(deep)) g.FillEllipse(cut, 21, 5 + lift, 12, 13);
                        g.FillPolygon(shade, new[] { new Point(0, 32), new Point(15, 20), new Point(31, 38),
                                                     new Point(0, 40) });
                        g.FillPolygon(main, new[] { new Point(9, 40), new Point(29, 21), new Point(42, 35),
                                                    new Point(42, 40) });
                        break;
                    case 1: // alpine peak
                        g.FillEllipse(star, 7, 10 + lift, 7, 7);
                        g.FillPolygon(shade, new[] { new Point(0, 35), new Point(14, 18), new Point(30, 35) });
                        g.FillPolygon(main, new[] { new Point(6, 38), new Point(25, 11), new Point(43, 38) });
                        g.FillPolygon(star, new[] { new Point(20, 18), new Point(25, 11), new Point(30, 19),
                                                    new Point(25, 17), new Point(23, 20) });
                        break;
                    case 2: // ringed planet
                        g.FillEllipse(main, 11, 12 + lift, 18, 18);
                        using (Pen ring = new Pen(light, 2))
                        {
                            GraphicsState orbit = g.Save();
                            g.TranslateTransform(20, 21 + lift);
                            g.RotateTransform(-25);
                            g.DrawArc(ring, -15, -4, 30, 8, -25, 230);
                            g.Restore(orbit);
                        }
                        break;
                    case 3:
                        g.TranslateTransform(0, lift);
                        g.FillRectangle(shade, 19, 7, 2, 19);
                        Triangle(g, star, 18, 8, 18, 23, 7, 23);
                        Triangle(g, main, 22, 12, 31, 23, 22, 23);
                        g.FillPolygon(main, new[] { new Point(6, 25), new Point(34, 25), new Point(28, 30),
                                                     new Point(12, 30) });
                        using (Pen water = new Pen(light, 1.4f))
                            g.DrawBezier(water, 7, 32, 14, 34, 21, 30, 32, 32);
                        break;
                    case 8: // pine forest
                        g.FillEllipse(star, 23, 9 + lift, 7, 7);
                        for (int i = 0; i < 3; i++)
                        {
                            int x = 12 + i * 7, y = i == 1 ? 11 : 18;
                            g.FillPolygon(i == 1 ? main : shade,
                                          new[] { new Point(x, y), new Point(x - 7, 32), new Point(x + 7, 32) });
                            g.FillRectangle(i == 1 ? main : shade, x - 1, 30, 3, 5);
                        }
                        break;
                    case 4: // faceted crystal
                        g.FillPolygon(main, new[] { new Point(20, 7 + lift), new Point(30, 20 + lift),
                                                    new Point(20, 33 + lift), new Point(10, 20 + lift) });
                        g.FillPolygon(star, new[] { new Point(20, 7 + lift), new Point(20, 21 + lift),
                                                    new Point(10, 20 + lift) });
                        g.FillPolygon(shade, new[] { new Point(20, 21 + lift), new Point(30, 20 + lift),
                                                     new Point(20, 33 + lift) });
                        break;
                    case 5: // suspended basket and striped balloon
                        g.TranslateTransform(0, lift);
                        using (GraphicsPath silk = new GraphicsPath())
                        {
                            silk.AddBezier(17, 25, 11, 20, 7, 14, 12, 9);
                            silk.AddBezier(12, 9, 17, 3, 27, 5, 30, 11);
                            silk.AddBezier(30, 11, 33, 17, 26, 23, 23, 25);
                            silk.CloseFigure();
                            g.FillPath(main, silk);
                        }
                        using (GraphicsPath stripe = new GraphicsPath())
                        {
                            stripe.AddBezier(19, 7, 14, 12, 15, 18, 19, 24);
                            stripe.AddBezier(19, 24, 18, 19, 21, 10, 23, 7);
                            stripe.CloseFigure();
                            g.FillPath(star, stripe);
                        }
                        using (Pen rigging = new Pen(light, 1.2f))
                        {
                            g.DrawLine(rigging, 16, 24, 18, 30);
                            g.DrawLine(rigging, 24, 24, 22, 30);
                        }
                        g.FillRectangle(main, 17, 29, 7, 4);
                        break;
                    case 6:
                        using (Pen stem = new Pen(accent, 2))
                            g.DrawBezier(stem, 20, 18, 20, 24, 24, 31, 28, 33);
                        g.TranslateTransform(20, 18);
                        g.RotateTransform(-14 + lift * 4);
                        using (GraphicsPath leaf = new GraphicsPath())
                        {
                            leaf.AddBezier(0, 0, -3, -3, -9, -7, -6, -11);
                            leaf.AddBezier(-6, -11, -4, -14, -1, -13, 0, -10);
                            leaf.AddBezier(0, -10, 3, -15, 7, -13, 7, -9);
                            leaf.AddBezier(7, -9, 7, -5, 2, -1, 0, 0);
                            for (int i = 0; i < 4; i++)
                            {
                                g.FillPath((i & 1) == 0 ? main : star, leaf);
                                g.RotateTransform(90);
                            }
                        }
                        break;
                    case 7:
                        using (Pen thread = new Pen(light, 1.8f))
                            for (int i = 0; i < 3; i++)
                            {
                                int x = 13 + 6 * i, sway = ((i & 1) == 0 ? 3 : -3) + lift;
                                g.DrawBezier(thread, x, 18, x - sway, 24, x + 2 * sway, 28, x, 32 - i % 2 * 2);
                            }
                        using (GraphicsPath bell = new GraphicsPath())
                        {
                            bell.AddBezier(7, 21, 6, 3, 32, 2, 33, 21);
                            bell.AddBezier(33, 21, 30, 17, 28, 23, 25, 20);
                            bell.AddBezier(25, 20, 22, 17, 20, 24, 16, 20);
                            bell.AddBezier(16, 20, 13, 17, 10, 23, 7, 21);
                            g.FillPath(main, bell);
                        }
                        using (Pen gleam = new Pen(light, 1.8f))
                            g.DrawBezier(gleam, 12, 16, 13, 12, 16, 10, 19, 10);
                        break;
                    case 9:
                        g.TranslateTransform(20, 20 + lift);
                        g.RotateTransform(-43);
                        using (Pen bow = new Pen(light, 3.4f)) g.DrawEllipse(bow, -15, -6, 12, 12);
                        g.FillRectangle(main, -3, -1.8f, 18, 3.6f);
                        g.FillRectangle(main, 8, 0, 3, 5);
                        g.FillRectangle(star, 13, 0, 3, 7);
                        break;
                    case 10: // a lantern with a beam above a striped coastal tower
                        Triangle(g, shade, 24, 14, 40, 8 + lift * 2, 40, 17 + lift);
                        g.FillPolygon(star, new[] { new Point(17, 17), new Point(23, 17),
                                                    new Point(27, 32), new Point(13, 32) });
                        g.FillPolygon(main, new[] { new Point(16, 23), new Point(24, 20),
                                                    new Point(25, 25), new Point(15, 28) });
                        g.FillRectangle(main, 15, 16, 10, 2);
                        g.FillRectangle(star, 17, 11, 6, 5);
                        Triangle(g, main, 13, 11, 20, 6, 27, 11);
                        using (Brush door = new SolidBrush(deep)) g.FillRectangle(door, 19, 28, 3, 4);
                        g.FillRectangle(shade, 9, 32, 23, 2);
                        break;
                    case 11:
                        g.FillPolygon(shade, new[] { new Point(17, 17), new Point(23, 17),
                                                     new Point(27, 33), new Point(13, 33) });
                        using (Brush door = new SolidBrush(deep)) g.FillRectangle(door, 18, 28, 4, 5);
                        g.TranslateTransform(20, 18);
                        g.RotateTransform(18 + lift * 7);
                        using (Pen spar = new Pen(light, 1.3f))
                            for (int i = 0; i < 4; i++)
                            {
                                g.DrawLine(spar, 0, 0, 0, -13);
                                g.FillPolygon(i % 2 == 0 ? star : main, new[] { new Point(0, -14), new Point(5, -12),
                                                                                 new Point(3, -5), new Point(0, -6) });
                                g.RotateTransform(90);
                            }
                        g.FillEllipse(star, -2, -2, 4, 4);
                        break;
                    case 12:
                        g.FillEllipse(shade, 22 - lift, 8 - lift, 5, 3);
                        g.FillEllipse(star, 26 - lift, 5 - lift, 5, 3);
                        g.FillRectangle(main, 7, 14, 10, 12);
                        g.FillRectangle(star, 6, 12, 12, 3);
                        using (Brush window = new SolidBrush(deep)) g.FillRectangle(window, 10, 16, 4, 5);
                        g.FillRectangle(main, 17, 19, 13, 7);
                        g.FillRectangle(main, 24, 14, 4, 6);
                        g.FillRectangle(star, 23, 13, 6, 2);
                        Triangle(g, shade, 29, 23, 35, 29, 28, 29);
                        g.FillRectangle(shade, 7, 25, 23, 3);
                        using (Pen tire = new Pen(light, 2))
                            for (int i = 0; i < 3; i++) g.DrawEllipse(tire, 8 + 8 * i, 26, 6, 6);
                        break;
                    case 13:
                        g.TranslateTransform(0, lift);
                        g.FillRectangle(shade, 8, 13, 23, 17);
                        g.FillRectangle(main, 7, 15, 26, 13);
                        g.FillPolygon(star, new[] { new Point(14, 15), new Point(17, 10),
                                                    new Point(23, 10), new Point(26, 15) });
                        g.FillRectangle(star, 9, 11, 4, 3);
                        using (Brush lens = new SolidBrush(deep))
                        {
                            g.FillEllipse(lens, 14, 15, 13, 13);
                            g.FillRectangle(lens, 28, 16, 3, 2);
                        }
                        using (Pen glass = new Pen(light, 1.5f)) g.DrawEllipse(glass, 17, 18, 7, 7);
                        g.FillEllipse(star, 19, 19, 2, 2);
                        break;
                    case 14:
                        using (Pen glass = new Pen(light, 1.8f))
                        {
                            g.DrawBezier(glass, 12, 9, 11, 17, 19, 17, 19, 20);
                            g.DrawBezier(glass, 19, 20, 19, 23, 11, 23, 12, 31);
                            g.DrawBezier(glass, 28, 9, 29, 17, 21, 17, 21, 20);
                            g.DrawBezier(glass, 21, 20, 21, 23, 29, 23, 28, 31);
                        }
                        Triangle(g, main, 14, 12 + lift, 26, 12 + lift, 20, 18);
                        Triangle(g, main, 13, 30, 20, 25 - lift, 27, 30);
                        g.FillRectangle(star, 19, 21 + lift, 2, 2);
                        g.FillRectangle(main, 10, 7, 20, 3);
                        g.FillRectangle(main, 10, 30, 20, 3);
                        break;
                    }
                }
            }
            g.Restore(artwork);
            using (Pen rim = new Pen(Color.FromArgb(150, accent), 1.2f)) g.DrawEllipse(rim, 1, 1, 38, 38);
        }

        private static void Triangle(Graphics g, Brush brush, int ax, int ay, int bx, int by, int cx, int cy)
        {
            g.FillPolygon(brush, new[] { new Point(ax, ay), new Point(bx, by), new Point(cx, cy) });
        }

        internal static void Paper(Graphics g, float x, float y, double phase)
        {
            GraphicsState state = g.Save();
            g.TranslateTransform(x, y);
            g.RotateTransform((float)(phase * 235));
            Point[] folds = { new Point(-6, -2), new Point(-3, -6), new Point(3, -5), new Point(6, -1),
                              new Point(4, 5),   new Point(-2, 6),  new Point(-6, 2) };
            using (Brush fill = new SolidBrush(C(0xffedbd))) g.FillPolygon(fill, folds);
            using (Pen crease = new Pen(C(0xab906e), 1.2f))
                g.DrawLines(crease, new[] { folds[1], new Point(1, 0), folds[4], new Point(-1, 2), folds[6] });
            g.Restore(state);
        }
    }
}

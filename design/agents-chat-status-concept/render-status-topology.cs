using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.Threading;
using System.Windows.Forms;

namespace AgentsChatLauncher
{
    internal static class StatusTopologyPreview
    {
        private static Dictionary<string, object> D(params object[] values)
        {
            Dictionary<string, object> result = new Dictionary<string, object>();
            for (int index = 0; index < values.Length; index += 2)
                result[Convert.ToString(values[index])] = values[index + 1];
            return result;
        }

        private static Dictionary<string, object> Role(string name, string adapter, bool working,
                                                        string sendId, string receiveId, double now)
        {
            return D(
                "address", "wengan-cemetery." + name,
                "role", name,
                "adapter", adapter,
                "description", name + " role",
                "desktop_transport", adapter == "codex" ? "codex_app_tools" : "qoder_native",
                "mode", "desktop_session",
                "session_id", name + "-session-01",
                "session_title", name,
                "connection_status", "ready",
                "online", true,
                "pending", working,
                "queued_tasks", 0,
                "running_tasks", working ? 1 : 0,
                "last_send_event_id", sendId,
                "last_send_at", sendId.Length > 0 ? now : 0,
                "last_send_kind", "result",
                "last_receive_event_id", receiveId,
                "last_receive_at", receiveId.Length > 0 ? now : 0,
                "last_receive_kind", "request",
                "transport_health", D("state", "ready", "consecutive_failures", 0,
                                      "pending_result_wakeups", 0, "last_success_at", now));
        }

        [STAThread]
        private static void Main()
        {
            double now = (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds;
            object[] roles = new object[] {
                Role("leader", "codex", false, "leader-send", "", now),
                Role("tester", "codex", false, "", "tester-receive", now),
                Role("backend", "qoder", true, "backend-send", "backend-receive", now),
                Role("frontend", "qoder", true, "", "frontend-receive", now)
            };
            Dictionary<string, object> status = D(
                "health", D("degraded", false, "version", "0.7.0",
                            "states", D("queued", 0, "running", 2),
                            "transports", D()),
                "roles", D("projects", new object[] {
                    D("id", "wengan-cemetery", "name", "wengan-cemetery", "roles", roles)
                }));

            Application.EnableVisualStyles();
            using (StatusDashboard dashboard = new StatusDashboard(
                delegate { }, delegate { }, delegate { }, delegate(string path) { },
                delegate(string project, string address) { }))
            {
                dashboard.Size = new Size(1320, 800);
                dashboard.CreateControl();
                dashboard.SetStatus(status, "", "");
                Thread.Sleep(900);
                using (Bitmap bitmap = new Bitmap(1320, 800, PixelFormat.Format32bppArgb))
                {
                    dashboard.DrawToBitmap(bitmap, new Rectangle(0, 0, bitmap.Width, bitmap.Height));
                    bitmap.Save("design/agents-chat-status-concept/preview-live-topology.png", ImageFormat.Png);
                }
            }
        }
    }
}

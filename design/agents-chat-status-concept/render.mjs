const task = await taskSpace("agents-chat-design-20261109");
const page = task.page("p1");
await page.cdp("Emulation.setDeviceMetricsOverride", {
  width: 1440,
  height: 960,
  deviceScaleFactor: 1,
  mobile: false,
});
await page.goto("file:///E:/WorkSpace/ai/agents-chat/design/agents-chat-status-concept/index.html");
await page.waitForSelector(".app-window");

const output = "E:/WorkSpace/ai/agents-chat/design/agents-chat-status-concept";
await page.screenshot({ path: `${output}/preview-status.png` });
await page.click('button[data-session="tester"]', { label: "select tester session" });
const selectedSession = await page.evaluate(() => ({
  name: document.querySelector("#detail-name")?.textContent,
  status: document.querySelector("#detail-status-text")?.textContent,
  queue: document.querySelector("#detail-queue")?.textContent,
}));
if (selectedSession.name !== "tester" || selectedSession.status !== "待确认" || selectedSession.queue !== "1") {
  throw new Error(`Session interaction failed: ${JSON.stringify(selectedSession)}`);
}
await page.click('button[data-view="tray"]', { label: "show tray design" });
await page.screenshot({ path: `${output}/preview-tray.png` });
await page.click('button[data-view="icons"]', { label: "show icon design" });
await page.screenshot({ path: `${output}/preview-icons.png` });

const pageReport = await page.evaluate(() => ({
  title: document.title,
  views: [...document.querySelectorAll(".view")].map((node) => node.id),
  activeView: document.querySelector(".view:not(.hidden)")?.id,
  viewport: { width: window.innerWidth, height: window.innerHeight },
  horizontalOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
}));
const report = { ...pageReport, selectedSession };
console.log({ taskSpaceId: task.spaceId, report });
await task.finish({ keep: [] });

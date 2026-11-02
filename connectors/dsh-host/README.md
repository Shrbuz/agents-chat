# agents-chat-dsh

DeepSeek Harness 宿主插件：把 Agents Chat 的任务投递成 **DSH 里的真实会话轮次**。

## 它做什么

1. 轮询总线，取本角色的未读 `request`；
2. `bus claim` 领到租约；把任务作为一条普通用户消息，投到一个固定会话；
3. 会话空闲后，从事件日志里取最后一条 `assistant/message`；
4. `bus result` 带上租约令牌把结果回报。

因为走的是 agent loop 的正常入口（`ctx.agents.get(id).followup(...)`），**这些会话在 DSH 界面里就是普通聊天**，可以打开查看、继续追问。

## 它不做什么

- 不注入输入框、不模拟按键；
- 不新建私有 HTTP API；
- 不修改 DSH 本体，卸载插件即恢复。

## 依赖的官方接口

| 接口 | 用途 |
|---|---|
| `ctx.agents.get(id)` | 取会话对应的 agent |
| `agent.followup(message)` | 把任务作为一轮投递并唤醒 |
| `agent.whenIdle()` | 等这一轮结束 |
| `ctx.agents.create/resume({ sessionId })` | 会话不存在时创建 |
| `session/event` → `assistant/message` | 取助手最终答复 |

## 安装

1. 复制 `cordis.patch.yml.example` 为 `cordis.patch.yml`，替换 `<PLUGIN_PATH>` 与 `<CLI_PATH>`；
2. 先用独立实例验证（不影响当前桌面版）：

   ```powershell
   dsh web --patch "<本目录>\cordis.patch.yml"
   ```

   打开它打印的端口，界面里应该出现名为 `agents-chat-dsh` 的会话；
3. 验证通过后，把同样的 `insert` 条目追加到
   `%USERPROFILE%\.dsh\profiles\desktop\cordis.patch.yml`，重启桌面版。

## 回滚

删掉 `insert` 条目、重启即可。插件不写用户数据；`agents-chat-dsh` 这个会话可以单独删除。

## 已知待验证项

- `UserMessage` 的字段形状（`id/role/content/source`）依据文档推断，首次加载需要用真实宿主确认；
- `ctx.sessions.get(id).log` 的事件读取路径同样需要在宿主里验证；若不可用，改为监听 `session/event`；
- 会话归属（`create` 的 `meta.cwd` 等）可能需要按项目补参数。

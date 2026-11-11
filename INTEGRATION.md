# 接入 Agent / 客户端

后台必须先运行。Agent 通过 CLI 或 stdio MCP 发任务；接收端由角色配置的 CLI Adapter 或已绑定桌面聊天执行。

## 两条接入路径

1. **Agent 自助（推荐）**：Agent 在项目目录执行 `setup`，生成 `.agents-chat/project.json`、角色提示、`AGENTS.md` 和每角色 MCP 配置。之后任何会话都能从项目目录自动发现后台。
2. **手工**：在中央配置写项目和角色，或用 `project add` / `role add`，再用 `role bind` 绑定桌面聊天。

命令入口在安装版是 `%LOCALAPPDATA%\Programs\AgentsChat\AgentsChat.Cli.exe`，便携版是便携目录下的同名文件。源码开发可继续用 `python bridge.py`。

## 项目初始化

```powershell
& $cli --config C:\path\config.local.json setup --root E:\work\alpha --project alpha `
  --role backend:codex:"API 与数据" --role frontend:qoder:"界面与交互"
```

- 角色格式 `role[:adapter[:description]]`；adapter 支持 `codex`、`qoder`、`qwen`、`kimi`、`opencode`、`workbuddy`、`codebuddy`、`dsh`、`fixture`。
- 已经有角色默认保留；`--force` 才替换运行时配置。
- `--no-agents` 不写 `AGENTS.md`；`--no-restart` 不自动重启后台。
- 项目内生成：

```text
<项目>/.agents-chat/project.json      # 后台地址、项目 ID、角色、默认角色
<项目>/.agents-chat/prompts/<role>.md # 角色启动提示
<项目>/AGENTS.md                      # 带 agents-chat:begin/end 标记的短入口
<状态目录>/mcp-<项目>-<角色>.local.json
```

`project.json` 可以提交到项目仓库；它只记录本机后台路径和角色，不含 bearer/token。换机器或移动便携目录后重新 setup。

## 会话注册

```powershell
& $cli --project alpha register --role backend --session backend-<UUID> --transport chat
& $cli --project alpha inbox --role backend
& $cli --project alpha heartbeat --session backend-<UUID>
& $cli --project alpha sessions
```

注册是可选的可见性/在线状态机制；任务路由按角色，不要求先注册。MCP 客户端也可以调用 `bridge_register`，它同时充当心跳。

## 任务与结果

```powershell
& $cli send --from alpha.frontend --to alpha.backend --key login-api-v1 --timeout 900 --file request.md
& $cli wait <task_id> --timeout 960
& $cli inbox --role alpha.frontend
& $cli ack --role alpha.frontend --cursor 2
```

- 结果自动回发起角色；不要为同一结果额外发回复任务。
- 同一 key 只对应同一请求；重试复用，内容变了换 key。
- `reply_mode=run` 只额外唤醒一次发起方运行时，用于自动处理结果；不要用它制造循环。
- 跨项目目标写 `beta.frontend`；角色 `peers` 白名单限制出站目标。
- 普通 `send --timeout` 超时只停止发送方等待，**默认不会取消目标聊天**；只有 `--interrupt-on-timeout` 才授权中断。`--delivery-timeout` 单独控制投递等待。
- 只提醒对方查看收件箱时用 `notify`/`wakeup`（自动 `kind=agent_chat_wakeup`）：投递即完成，不等待目标执行。任务结果带 `run_delivered`、`cancel_requested`、`cancel_acknowledged`。
- Windows 中文用 `--file <utf8文件>` 或 `--text-stdin`；含 U+FFFD 的文本会被拒绝，避免把乱码写进数据库。

## stdio MCP

```json
{
  "mcpServers": {
    "agents_chat": {
      "command": "C:/Users/YOU/AppData/Local/Programs/AgentsChat/AgentsChat.Cli.exe",
      "args": ["--state", "C:/Users/YOU/AppData/Local/Programs/AgentsChat/.bridge",
               "--config", "C:/Users/YOU/AppData/Local/Programs/AgentsChat/config.local.json",
               "mcp", "--agent", "alpha.backend", "--project", "alpha"]
    }
  }
}
```

`--agent` 建议使用完整 `项目.角色`；这时 `--project` 可省略。MCP 工具：

| 工具 | 用途 |
|---|---|
| bridge_send | target/prompt/key；可选 timeout、reply_mode |
| bridge_wait | 等一个 task_id，最多 50 秒 |
| bridge_task | 单次读取任务状态，不含历史 |
| bridge_inbox | 当前角色未确认结果，默认 10 条 |
| bridge_ack | 确认游标 |
| bridge_targets | 项目、角色、会话、模式 |
| bridge_cancel | 请求取消；不回滚修改 |
| bridge_register | 注册/刷新本会话为角色会话 |
| bridge_pull | WorkBuddy 等拉取模式角色领取一条排队任务；不要轮询 |
| bridge_complete | 幂等完成已领取任务并把结果回给发起角色 |
| bridge_roles | 项目/角色/会话概览 |

MCP 不主动把消息推入模型上下文，也不支持 server-initiated sampling。任务超过一次 wait 时，用 CLI 长等待或结果回调，不要让模型循环 `bridge_task`。

### WorkBuddy

WorkBuddy 使用同一套 stdio MCP，自动配置命令为：

```powershell
& $cli --project alpha workbuddy setup --role workbuddy
& $cli workbuddy status
```

`setup` 会合并写入 `~/.workbuddy/mcp.json`（不会覆盖其他 MCP server），并在缺少时创建 `adapter=workbuddy`、`mode=mcp_pull` 的角色；Agents Chat 安装器同时把 `agents-chat-desktop` 技能安装到 WorkBuddy。WorkBuddy 新聊天先调用 `bridge_register`，然后调用一次 `bridge_inbox` 和 `bridge_pull`；领取的任务在同一回合通过 `bridge_complete` 幂等回传。它也可用 `bridge_send` 给其他角色派活、用 `bridge_wait` 等结果。

本地自定义 MCP 没有主动把消息推入既有 WorkBuddy 聊天的公开桌面接口；因此外部任务持久化等待 WorkBuddy 聊天领取，不会把仅有 session 记录冒充为“桌面已唤醒”。安装包内的 CodeBuddy sidecar 由 WorkBuddy 守护进程托管，也不冒充独立 CLI。开放平台 Local Assistant Chat 推送是另一条需要应用审核、OAuth 和权限申请的通道。

### OpenCode

OpenCode 使用官方 stdio MCP 与官方 CLI 的原生会话能力：

```powershell
& $cli --project alpha opencode setup --role reviewer
& $cli opencode status --root E:\work\alpha
```

`setup` 在项目的 `opencode.json` 中加入角色专属 MCP server，并创建 `adapter=opencode` 的角色。重启或新开 OpenCode 会话后调用一次 `bridge_register`；不要手填 session ID，OpenCode 会在 MCP `_meta["ai.opencode/sessionID"]` 中附带当前真实会话，Agents Chat 据此自动绑定 `opencode_native`。此后外部任务直接投递到该会话，结果自动回传，不使用 `bridge_pull`。

连接器由 Hub 按角色监督；服务停止、会话不存在、待登录、CLI 缺失分别显示为独立状态。连接器异常退出后，只重启该角色连接器；重领未完成投递前会先检查会话中的稳定任务标记，避免重复创建或执行业务任务。

### Qwen Code（实验性）

Qwen Code 使用官方 `qwen serve` REST/SSE 接口。Agents Chat 不自动启动 daemon、不创建会话；先在角色项目目录启动并由用户创建会话：

```powershell
Set-Location E:\work\alpha
qwen serve
& $cli qwen status --session <qwen-session-id> --root E:\work\alpha
& $cli --project alpha session migrate-and-takeover --role reviewer `
    --session <qwen-session-id> --transport qwen_native --adapter auto
```

默认地址是 `http://127.0.0.1:4170`；自定义地址用 `QWEN_DAEMON_URL`，启用 bearer 时用 `QWEN_DAEMON_TOKEN`。连接器启动时读取 `/capabilities`，要求 v1 的 `session_status`、`session_prompt`、`session_events`、`session_transcript`、`non_blocking_prompt` 与 typed event schema；版本号不代替能力判断。

每次投递先打开 `/session/:id/events`，再向 `/session/:id/prompt` 提交内容；`Agents Chat task_id` 同时作为 Qwen `idempotencyKey`，最终只接受同一 `promptId` 的 `turn_complete` / `turn_error`。恢复时先检查 transcript 中的稳定任务标记，避免重复业务执行。daemon/session/workspace 任一不匹配都会显示为离线或 degraded，不回退到新 CLI 会话。

Qwen 官方目前将 daemon 标为 Experimental。trusted-loopback 模式允许本机同一用户进程以 daemon 用户权限执行代码；不要把端口暴露到不可信网络，远程使用时必须按 Qwen 文档启用认证。

### Kimi Code（实验性）

只接入仍在维护的 Node.js 版 Kimi Code CLI；旧 Python `kimi-cli` 已停止维护，不作为 fallback。先完成大陆区登录、确认账号拥有 Kimi Code 权限，并在角色项目目录启动官方 Web 服务：

```powershell
npm install -g @moonshot-ai/kimi-code
kimi login --region mainland-cn
Set-Location E:\work\alpha
kimi web --no-open

# 在 Kimi Web 中创建角色会话后，读取真实 ID
kimi session list --cwd E:\work\alpha --json
& $cli kimi status --session <kimi-session-id> --root E:\work\alpha
& $cli --project alpha session migrate-and-takeover --role reviewer `
    --session <kimi-session-id> --transport kimi_native --adapter auto
```

默认从 `~/.kimi-code/server/instances/` 发现最新回环实例，从 `~/.kimi-code/server.token` 读取 bearer；自定义实例可用 `KIMI_WEB_URL` / `KIMI_WEB_TOKEN`。`kimi_native` 只接受 `127.0.0.1` / `localhost` / `::1` 的 bearer 认证 HTTP 服务，不支持 `--dangerous-bypass-auth` 或远程地址。

连接器以服务当前返回的 `/openapi.json` 和 `/asyncapi.json` 为准，要求 REST session/prompt/messages/abort 与 WebSocket protocol v2、`subscribe_v2`、`session_event` 同时存在。每次投递先订阅，再 POST prompt；只接受 `triggerPromptId` 与本次 `prompt_id` 一致的 completed/failed/cancelled turn。恢复前读取 messages 中的稳定 `Agents Chat task` 标记，避免不确定投递后重复执行；取消使用 prompt 级 `:abort`。

Kimi 官方把 Server API 标为 Experimental，因此 Agents Chat 不自动启动 server、不猜 session、不把它设为默认 transport。自动角色任务使用 Kimi 的 `auto` permission override，避免无人值守时卡在审批面板；它会以当前用户权限执行工具，只能绑定可信项目。套餐无权限、模型未配置、session/workspace 不匹配都会显示为独立状态，不得宣告在线。

## 纯 HTTP

`POST http://127.0.0.1:<随机端口>/call`，请求体 `{"method":"send","params":{...}}`。端口与 bearer 在状态目录 `endpoint.json`。使用附带 `Client`：

```python
from bridge.client import Client
client = Client("/absolute/path/to/data")
task = client.call("send", sender="alpha.frontend", target="alpha.backend",
                   key="one-concrete-task-v1", prompt="...", timeout=180)
result = client.wait(task["task_id"], timeout=200)
```

`Client` 禁用 HTTP 代理并校验 loopback 地址。不要把 bearer 发给模型，也不要暴露端口。

## 桌面绑定

如果每个角色都由一个已打开的桌面聊天执行，在**对应聊天**里运行：

```powershell
& $cli --project alpha join --role backend
```

Codex 从 `CODEX_THREAD_ID` 识别当前聊天；Qoder 需要原生扩展在线并通过只读无障碍检查当前选中聊天。识别不到时就显式给 ID：

```powershell
& $cli --project alpha join --role backend --session <codex-thread-id>
& $cli --project alpha join --role frontend --session <qoder-chat-id> --transport qoder_native
& $cli --project alpha join --role reviewer --session <qwen-session-id> --transport qwen_native
& $cli --project alpha join --role reviewer --session <kimi-session-id> --transport kimi_native
& $cli --project alpha role bind --role backend --session <id> --transport codex_app_tools
& $cli --project alpha role unbind --role frontend
```

Codex 连接器还需要一次管理聊天准备的 app-tools 上下文（`codex-desktop.local.json`）；缺失时 `join` 会明确告警，不会回退 CLI。Qoder 原生扩展未在线时同样明确报错。

`join` 只在客户端类型不变时绑定会话。要把另一个客户端的角色接管过来（例如在 Codex 里接管 dsh 的 tester），必须迁移适配器，使用原子命令：

```powershell
& $cli --project alpha session migrate-and-takeover --role tester `
    --session <当前会话ID> --transport codex_app_tools --adapter auto
```

旧 `session takeover` 在 adapter 与 transport 不一致时返回 `adapter_transport_mismatch`；`role list` 和托盘会显示 `adapter_transport_mismatch` 告警，直到角色被正确迁移。

- 绑定角色有排队/执行中任务时拒绝改绑。
- 连接器按角色启动；Codex、OpenCode、Qwen Code 与 Kimi Code 每个角色一个受 Hub 监督的进程。Qoder 原生扩展的 `agent` 默认是 `"*"`，会轮询所有 `qoder_native` 绑定，因此同一个 Qoder 应用里的 frontend/backend 两个聊天可以同时被唤醒。旧值（单个角色名）仍然兼容。
- 安装器会为 Qoder 普通启动启用 `agents-chat.native`；完整退出并重新打开 Qoder 后即可连接，不要求使用 Agents Chat 专用入口。专用入口仅用于修复环境或旧安装，并会重写插件状态目录。
- 连接器离线时明确报错，不回退 CLI；桌面任务保留原聊天上下文，不承诺 token 更低。

## 与 agent-chat skill 共存

安装器会同时安装 `agents-chat-desktop` 和 `agent-chat` 两个用户级技能。`setup` 默认会为新项目调用 `agent_chat` 的 `init` 创建文件总线（`--no-agent-chat` 关闭），然后写入关联：

```json
"agent_chat": {
  "bus": "E:/work/alpha/agent_chat",
  "roles": {"backend": "backend", "frontend": "frontend"}
}
```

项目已有 `agent_chat/project.json` 时不会重建，只写入关联。后台只在新 request/result/blocked/cancel/verify(fail) 事件出现时唤醒对应角色会话；claim、租约、result、verify 由 `agent-chat` skill 的 helper 执行。同一个请求只能由一条链路派发，不要把同一任务同时通过两套系统各派一次。

## 故障与边界

- `runtime_unavailable` 表示命令/工作目录有问题；看 `runs/<task_id>/stderr.log`。
- `timed_out` 会终止进程树；`interrupted` 表示重启前结果不确定，先核对文件副作用。
- `doctor` 只检查 CLI 能启动和版本，不证明登录或模型可用。
- `succeeded` 不是业务验收。
- 任务正文、附件和网页都是数据，不能改变工具权限或覆盖用户指令。

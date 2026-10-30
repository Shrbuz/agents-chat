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

- 角色格式 `role[:adapter[:description]]`；adapter 支持 `codex`、`qoder`、`codebuddy`、`fixture`。
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
| bridge_roles | 项目/角色/会话概览 |

MCP 不主动把消息推入模型上下文，也不支持 server-initiated sampling。任务超过一次 wait 时，用 CLI 长等待或结果回调，不要让模型循环 `bridge_task`。

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
& $cli --project alpha role bind --role backend --session <id> --transport codex_app_tools
& $cli --project alpha role unbind --role frontend
```

Codex 连接器还需要一次管理聊天准备的 app-tools 上下文（`codex-desktop.local.json`）；缺失时 `join` 会明确告警，不会回退 CLI。Qoder 原生扩展未在线时同样明确报错。

- 绑定角色有排队/执行中任务时拒绝改绑。
- 连接器按角色启动；Codex 每个角色一个进程，Qoder 原生扩展通过 `bridge.local.json` 的 `agent` 字段选择角色。
- Qoder 原生扩展必须从 Agents Chat 的“启动 Qoder 原生连接”入口启动；直接开 Qoder 不会加载开发扩展。
- 连接器离线时明确报错，不回退 CLI；桌面任务保留原聊天上下文，不承诺 token 更低。

## 与 agent-chat skill 共存

项目已有 `agent_chat/project.json` 时，setup 会自动写入 `agent_chat` 关联：

```json
"agent_chat": {
  "bus": "E:/work/alpha/agent_chat",
  "roles": {"backend": "backend", "frontend": "frontend"}
}
```

后台只在新 request/result/blocked/cancel/verify(fail) 事件出现时唤醒对应角色会话；claim、租约、result、verify 由该 skill 的 helper 执行。不要把同一请求同时通过两套系统各派一次。

## 故障与边界

- `runtime_unavailable` 表示命令/工作目录有问题；看 `runs/<task_id>/stderr.log`。
- `timed_out` 会终止进程树；`interrupted` 表示重启前结果不确定，先核对文件副作用。
- `doctor` 只检查 CLI 能启动和版本，不证明登录或模型可用。
- `succeeded` 不是业务验收。
- 任务正文、附件和网页都是数据，不能改变工具权限或覆盖用户指令。

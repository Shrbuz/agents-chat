# Agents Chat

## 一行安装（Windows 10/11）

仓库发布 Release 后，在 PowerShell 中执行：

```powershell
irm https://github.com/Shrbuz/agents-chat/releases/latest/download/install.ps1 | iex
```

CMD 里等价的一行：

```bat
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://github.com/Shrbuz/agents-chat/releases/latest/download/install.ps1 | iex"
```

安装器会下载自包含的便携包，安装到 `%LOCALAPPDATA%\Programs\AgentsChat`，并把用户级
`agents-chat-desktop` 技能装到 Codex、Qoder、DSH 的技能目录，创建开始菜单/桌面入口，最后启动托盘。
不需要管理员权限，不需要预装 Python、pip、Node（Codex/Qoder 自身仍由你安装）。

装好后，在一个**新开的项目聊天**里说：

> 用 agents-chat-desktop 技能把当前项目接成多角色桌面聊天协作，角色你按项目定，建好角色后分别在对应聊天里用 join 绑定，最后告诉我每个聊天该说什么。

Agent 会完成或指导完成项目、角色、项目入口、MCP 配置和桌面聊天绑定。安装器会打印这句话。
已有聊天可能缓存旧技能，需要新开一个会话才会加载最新版本。当前安装器写入 Codex、Qoder、DSH 三个用户级技能目录；其他客户端可把 `tool-skill/SKILL.md` 放到其技能根目录。

安装器会校验 Release 的 SHA-256，保留已有的 `config.json`/`data`，不需要管理员权限。卸载：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\AgentsChat\uninstall.ps1"
```

默认保留配置和数据；加 `-RemoveData` 一并删除，`-KeepSkill` 保留用户级技能。

从源码安装/开发：

```powershell
python install.py
```

## 0.5：多项目、多角色、多会话的本机协作中枢

Agents Chat 是一个常驻 Windows 托盘的本地后台。它把**项目**、**角色**、**会话**分开：

- **项目**：一个目录及其协作上下文。一个后台可以同时管理多个项目。
- **角色**：项目里的协作席位，地址是 `<项目>.<角色>`，例如 `alpha.backend`。角色可以由 Codex、Qoder、CodeBuddy 或同一 Agent 的另一个会话承担。
- **会话**：某个角色的一次具体对话/进程。会话通过 CLI 或 MCP 注册，桌面聊天可以额外绑定为投递目标。

任务按角色路由。发起角色把任务交给某个角色，目标角色的会话执行，结果自动回到发起角色的收件箱。跨项目也只是换一个完整地址，不需要另开后台。这就是“同一 Agent 多会话”和“不同 Agent 多会话”共用的协作平面。

```text
alpha.frontend ──send──▶ alpha.backend
        ▲                      │
        └──── result inbox ────┘

beta.architect ──send alpha.backend──▶ alpha.backend   (跨项目)
```

角色之间不需要一个额外的“总调度 Agent”。每个会话按自己的角色提示决定下一步派发给谁；后台只做持久化、排队、投递和等待，空闲时不调用模型。

## 托盘与便携版

- `AgentsChat.exe`：系统托盘宿主。启动后自动拉起本地后台；双击托盘图标或运行 `AgentsChat.exe ui` 打开状态窗口。窗口只用于查看项目/角色/队列和启停，不需要手工配置。
- `AgentsChat.Cli.exe`：Agent 和脚本使用的命令入口；同一目录也提供 stdio MCP。
- 发布版用上面的 `install.ps1` 一行安装/更新；从源码开发时仍可双击 `Install.cmd` 或运行 `python install.py`。
- 便携版由 `python build_portable.py` 生成：`dist/AgentsChat-Portable/` 和 zip，自带官方 Python 嵌入式运行时（约 24 MB），无需安装 Python/pip，解压即用。Codex/Qoder 的 CLI 任务仍需要对应厂商程序；Qoder 原生桌面扩展还需要本机 Qoder 与 Node。

托盘只做三件事：启动/停止后台、展示状态、打开配置/数据目录。真正的会话搭建和配置由 Agent 通过 CLI 完成。本机空闲实测约：托盘 41 MB、后台 29 MB，空闲时不调用模型、不轮询队列。

## Agent 自助配置（推荐入口）

Agent 在项目目录运行：

```powershell
# 找到 CLI（安装版或便携版）
$cli = "$env:LOCALAPPDATA\Programs\AgentsChat\AgentsChat.Cli.exe"

# 一次创建项目和角色；角色格式 role[:adapter[:description]]
& $cli --config <后台配置> setup --root E:\work\alpha --project alpha `
    --role backend:codex:"API 与数据" --role frontend:qoder:"界面与交互"

# 之后单独加角色
& $cli --project alpha role add --role tester --adapter codex --description "生产验收"

# 打印某个角色的启动提示，交给对应会话
& $cli --project alpha role prompt --role backend
```

`setup` 会：

- 在中央配置里创建项目和角色；
- 写 `<项目>/.agents-chat/project.json`，让以后任何会话在项目目录内自动发现后台、项目和默认角色；
- 写 `<项目>/.agents-chat/prompts/<角色>.md`；
- 在项目 `AGENTS.md` 中加入一段带边界标记的启动说明；
- 生成 `.bridge/mcp-<项目>-<角色>.local.json`；
- 如果项目已有 `agent_chat/project.json`，自动关联该文件总线，只做“新事件唤醒”，不改变它的 claim/verify 协议。

重复运行 setup 是幂等的；已有角色默认保留。新增角色后如果后台在运行，默认自动重启加载配置（`--no-restart` 可关闭）。

## 会话注册与收件箱

每个会话启动时生成一个稳定 ID 并注册：

```powershell
& $cli --project alpha register --role backend --session backend-<UUID> --transport chat
& $cli --project alpha inbox --role backend
```

注册表示“这个角色当前有会话在线”，用于状态窗口、审计和同角色多会话的可见性；任务投递仍按角色进行，同一角色同一时间只有一个执行者。`inbox` 不会自动确认；处理完用返回的 `next_cursor` 确认：

```powershell
& $cli --project alpha ack --role backend --cursor 3
```

## 派发任务与回结果

```powershell
& $cli send --from alpha.frontend --to alpha.backend `
    --key login-api-v1 --timeout 900 --file .\request.md

# 结果自动写回 alpha.frontend 的 inbox；需要在这个会话里等一次时：
& $cli wait <task_id> --timeout 960
```

- `--to` 可以写角色名（全局唯一时）或完整 `项目.角色`。
- 同一 `--key` 只对应同一请求；重试时复用，内容变了必须换 key。
- `reply_mode=run` 会在发送方角色配置了运行时的情况下，额外启动一次最多 800 字的本地结果处理任务；默认 `mailbox` 不唤醒发送方模型。
- 角色可配置 `peers` 白名单；未列出的目标会被拒绝。跨项目默认允许，但角色提示要求用户明确授权。

## 同一 Agent 的多会话

同一 Agent 可以在不同项目或同一项目里占多个角色。例如：

- `alpha.architect` 和 `alpha.builder` 都是 Codex，各自一个会话；
- `beta.tester` 是另一个 Codex 会话，只做验收；
- `alpha.frontend` 是 Qoder。

这些角色互相看不到对方的历史，只看到派发的短任务和结果摘要，因此不会把彼此的上下文混在一起。需要更复杂的领取/租约/验收语义时，项目可以同时使用用户自己的 `agent-chat` skill：Agents Chat 负责在文件总线出现新 request/result 时唤醒对应角色会话，claim/result/verify 仍以该 skill 为准。

## 桌面聊天绑定（可选）

默认角色是可独立运行的 CLI 任务。若要把已有桌面聊天作为投递目标：

- Codex：使用宿主提供的 app-tools MCP，`role bind --transport codex_app_tools`；一个角色一个连接器，支持同一 Codex 的多个聊天分别承担不同角色。
- Qoder：使用普通原生开发扩展 `agents-chat.native`，`role bind --transport qoder_native`；不操作输入框、不切聊天、不抢焦点。扩展离线时明确报离线，不退回 UI 自动化。
- Qoder 输入框自动操作始终禁用；UIA 仅作为历史兼容路径，不作为默认。
- 绑定是显式路由；绑定角色有排队/执行中任务时拒绝改绑，绝不静默退回 CLI。

## 命令速查

```text
status / roles / targets / health / stop / restart
setup --root DIR [--project ID] [--role role:adapter[:desc]]...
project add|list|remove
role add|list|remove|prompt|bind|unbind
register / heartbeat / sessions / join
send / wait / task / cancel / inbox / ack
mcp --agent ROLE [--project ID]
doctor --config FILE
```

所有命令支持 `--config`、`--state` 和 `--project`；在项目目录内可以省略，由 `.agents-chat/project.json` 自动解析。

## MCP

stdio MCP 暴露 9 个工具：`bridge_send`、`bridge_wait`、`bridge_task`、`bridge_inbox`、`bridge_ack`、`bridge_targets`、`bridge_cancel`、`bridge_register`、`bridge_roles`。详细配置见 [INTEGRATION.md](INTEGRATION.md)。

## 状态与边界

- 后台只监听 `127.0.0.1` 随机端口，需本机 bearer；拒绝浏览器 Origin 和外部 Host。信任边界是同一个操作系统用户，不是多租户安全产品。
- 一个状态目录（`data/` 或 `.bridge/`）只允许一个后台实例；SQLite WAL 保存任务、结果、会话和游标。
- 任务不能指定任意可执行程序或工作目录；这些由受信任的角色配置决定。
- `succeeded` 只表示 Runtime 正常结束，不等于业务验收；仍要检查证据和副作用。
- 模型仍调用厂商在线服务，不是离线模型；空闲等待、去重、邮箱和路由由本地代码完成。
- 桌面绑定依赖厂商版本相关入口；更新桌面后可能需要重新绑定或重新启动 Qoder 原生连接。
- 可公开复现的检查记录见 [VERIFICATION.md](VERIFICATION.md)。内部交接和真实业务会话记录不进入开源仓库。

## 从源码运行

```powershell
python bridge.py --config config.local.json start --config config.local.json
python bridge.py status
python -m unittest discover -s tests -v
python build_portable.py
```

源码目录的 `config.local.json` 是旧的单项目格式，仍被支持：其中的 `codex`/`qoder` 保持原名，不会破坏已有状态和桌面绑定。新增项目后配置会自动升级为 v2 的 `projects` 格式，旧项目角色继续使用原名。


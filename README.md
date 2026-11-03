# Agents Chat

**简体中文** | [English](README.en.md)

[![Release](https://github.com/Shrbuz/agents-chat/actions/workflows/release.yml/badge.svg)](https://github.com/Shrbuz/agents-chat/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Version](https://img.shields.io/badge/version-0.6.8-blue)

让同一台机器上的多个 Agent 聊天互相派活、自动回传结果并验收。项目里的角色（`leader`、`frontend`、`tester`……）各自是一个协作席位，任务按角色路由，不需要人工在两个聊天窗口之间复制粘贴。

- **本地优先**：中枢只监听 `127.0.0.1`，数据存在本地 SQLite 与 JSON 文件里
- **按需启动**：不设开机自启；用到它的会话自己拉起后台
- **看得见**：任务可以投进你正在用的 Qoder / DSH 聊天，过程和结果都在你眼前

---

## 目录

- [快速开始](#快速开始)
- [核心概念](#核心概念)
- [两种协作模式](#两种协作模式)
- [文件总线](#文件总线)
- [DSH 接入](#dsh-接入)
- [桌面聊天绑定](#桌面聊天绑定)
- [CLI 速查](#cli-速查)
- [MCP](#mcp)
- [托盘与便携版](#托盘与便携版)
- [配置文件与数据](#配置文件与数据)
- [升级与卸载](#升级与卸载)
- [安全边界与已知限制](#安全边界与已知限制)
- [从源码开发](#从源码开发)
- [许可证](#许可证)

---

## 快速开始

### Windows 10 / 11

PowerShell：

```powershell
irm https://github.com/Shrbuz/agents-chat/releases/latest/download/install.ps1 | iex
```

CMD 等价写法：

```bat
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://github.com/Shrbuz/agents-chat/releases/latest/download/install.ps1 | iex"
```

安装器会：

- 下载自包含便携包并安装到 `%LOCALAPPDATA%\Programs\AgentsChat`；
- 把 `agents-chat-desktop` 技能写进 Codex / Qoder / DSH 的用户级技能目录；
- 创建开始菜单与桌面入口；
- 校验 Release 的 SHA-256，保留已有的 `config.json` 与数据；
- 不需要管理员权限，不需要预装 Python、pip、Node（Codex / Qoder 自身仍需你安装）。

装好后，在一个**新开的项目聊天**里说：

> 用 agents-chat-desktop 技能把当前项目接成多角色桌面聊天协作，角色按项目定。Codex / Qoder 的聊天用 join 绑定；DSH 的聊天由宿主插件自动绑定、不需要 join。最后告诉我每个聊天该说什么。

已有聊天会缓存旧技能，需要新开会话才会加载最新版本。

### macOS / Linux

目前没有独立便携包，从源码目录运行：

```bash
sh install.sh            # 默认装到 ~/.local/opt/agents-chat
sh install.sh --dry-run  # 只打印将要执行的动作
sh install.sh --uninstall
```

- 需要 `python3` ≥ 3.11；
- 只装一份，再链接进 `~/.codex/skills`、`~/.qoder/skills`、`~/.agents/skills`、`~/.claude/skills`（只链接已存在的目录，不覆盖真实目录）；
- macOS 默认安装 launchd 常驻，`--no-service` 可跳过；Linux 会打印 systemd 用户单元示例；
- **限制**：无托盘、无桌面聊天绑定，角色以 CLI 任务执行。协作本身成立，但看不到消息投进你正在用的聊天窗口。

---

## 核心概念

| 概念 | 含义 |
| --- | --- |
| **项目** | 一个目录及其协作上下文。一个后台可同时管理多个项目。 |
| **角色** | 项目里的协作席位，地址为 `<项目>.<角色>`，例如 `alpha.backend`。 |
| **会话** | 某个角色的一次具体对话或进程。桌面聊天可以额外绑定为投递目标。 |

```text
alpha.frontend ──send──▶ alpha.backend
        ▲                      │
        └──── result inbox ────┘

beta.architect ──send alpha.backend──▶ alpha.backend   (跨项目)
```

角色之间不需要一个额外的“总调度 Agent”。每个会话按自己的角色提示决定下一步派给谁；后台只做持久化、排队、投递和等待，空闲时不调用模型。

---

## 两种协作模式

同一条请求只走一条链路，不存在需要用户裁决的“两套系统”：

| 项目里是否已有 `agent_chat/` 总线 | 派活方式 | 中枢的作用 |
| --- | --- | --- |
| 有 | 用文件总线（`claim` / `result` / `verify`） | 新事件到达时唤醒对应角色的会话 |
| 没有 | 用中枢队列（`send` / `wait`） | 直接投递给目标角色 |

技能会自动判断项目现状并选一条，不会来回问用户。

---

## 文件总线

项目关联 `agent_chat` 总线后，中枢**原生实现**同一套协议：事件是只增不改的 JSON 文件，`_state/bus.lock` 上加 OS 文件锁，写入走临时文件 + fsync + 原子改名，任务状态由事件重放得出。

```powershell
$cli = "$env:LOCALAPPDATA\Programs\AgentsChat\AgentsChat.Cli.exe"

# 派活
& $cli --project alpha bus request --from backend --to frontend --session be-1 `
    --title "登录接口" --body "详情" --acceptance "返回 200" --priority P1

# 领取（返回租约 token）
& $cli --project alpha bus claim <task-id> --role frontend --session fe-1

# 交结果 / 验收
& $cli --project alpha bus result <task-id> --role frontend --session fe-1 --token <token> `
    --body "已完成" --evidence "测试通过"
& $cli --project alpha bus verify <task-id> --role backend --session be-1 --outcome pass --body "复测通过"
```

### 换人接手

| 谁 | 命令 | 效果 |
| --- | --- | --- |
| 正在做的人 | `bus release <task-id> --role <r> --session <s> --token <t> --body "做到哪、为什么交还"` | 任务回到 `pending`，钥匙清空，交接说明留给下一个人 |
| 发起方 | `bus reassign <task-id> --role <发起方> --session <s> --body "为什么收回"` | 强制收回；原会话的 token 立即失效 |

接手的人先 `bus show <task-id>` 看完整事件链，再 `claim` 即刻生效。

### 换一个聊天接管角色

同一个角色换了聊天窗口时，显式接管一次，旧会话立刻失去领取资格：

```powershell
& $cli --project alpha session takeover --role frontend --session <新会话ID> --transport qoder_native
& $cli --project alpha session retire   --session <旧会话ID>
```

托盘状态窗口里也有 **「切换会话…」** 按钮：选中角色 → 在线会话排前、历史在后，也可以直接粘贴会话 ID。

### 坏消息不再拖垮全员

读不懂的消息会被**跳过并记录**，其余消息照常：

```powershell
& $cli --project alpha bus validate          # 列出被跳过或有问题的消息
& $cli --project alpha bus repair            # 预览要隔离哪些文件
& $cli --project alpha bus repair --apply    # 移到 _quarantine-<时间>/，不删除
```

### 规则摘要

- 状态机：`pending → in_progress → resolved → closed`；`verify --outcome fail` 回落 `pending`。
- 租约默认 30 分钟；桌面会话由中枢自动续期，过期后同一会话的迟交结果仍然受理。
- 请求必须带 `--acceptance`，结果必须带 `--evidence`；收件箱按「角色 + 会话」记录已读。
- 写消息请用 CLI 或技能脚本，不要手写文件名——`<序号:08位>_<message_id>.json` 最容易写坏。

---

## DSH 接入

### 推荐：`dsh_plugin`

插件跑在 DSH 宿主里，把任务**作为普通用户消息**送进你已经打开的那个会话，不另起进程。

- 角色来源：按 adapter 自动发现，`adapter=dsh` 的角色由插件接管，新增角色不用改插件配置；
- 绑定会话：读 DSH 会话缓存，按标题匹配角色名（`CMS_Tester` → `tester`）；
- 会话名显示：读 DSH 会话标题，取不到时回落为会话 ID；
- 中枢没跑：插件自己执行 `start` 拉起。

配置示例：`connectors/dsh-host/cordis.patch.yml.example`。一个插件实例可服务多个角色。

### 已弃用：`independent_cli_task`

旧模式会为每条任务新起一个 `dsh headless` 进程，既不在你的聊天里，也读不到 GUI 凭据。现在 dsh 角色未绑定插件时直接报 `dsh_requires_plugin`，不再新起进程。

---

## 桌面聊天绑定

默认角色是可独立运行的 CLI 任务。若要把已有桌面聊天作为投递目标：

- **Codex**：宿主 app-tools MCP，`role bind --transport codex_app_tools`；一个角色一个连接器。
- **Qoder**：原生开发扩展 `agents-chat.native`，`role bind --transport qoder_native`；不操作输入框、不切聊天、不抢焦点。
- **DSH**：由宿主插件自动绑定，不需要 `join`。
- 绑定是显式路由；绑定角色有排队/执行中任务时拒绝改绑，不会静默退回 CLI。

---

## CLI 速查

```text
start / serve / stop / restart / health / doctor
status / roles / targets / sessions
setup --root DIR [--project ID] [--role role:adapter[:desc]]...
project add|list|remove
role add|list|remove|prompt|bind|unbind
register / heartbeat / join / native-result
send / wait / task / cancel / inbox / ack
bus request|claim|renew|progress|blocked|result|verify|cancel|release|reassign|inbox|mark-read|status|show|validate|repair|paths
mcp --agent ROLE [--project ID]
```

所有命令支持 `--config`、`--state` 与 `--project`；在项目目录内可以省略，由 `.agents-chat/project.json` 自动解析。

---

## MCP

stdio MCP 暴露 9 个工具：`bridge_send`、`bridge_wait`、`bridge_task`、`bridge_inbox`、`bridge_ack`、`bridge_targets`、`bridge_cancel`、`bridge_register`、`bridge_roles`。配置方式见 [INTEGRATION.md](INTEGRATION.md)。

---

## 托盘与便携版

- `AgentsChat.exe`：系统托盘宿主，启动后拉起本地后台；双击打开状态窗口（带列头的角色表格、真实聊天名、启停按钮）。
- `AgentsChat.Cli.exe`：Agent 与脚本使用的命令入口，同一目录也提供 stdio MCP。
- 便携版由 `python build_portable.py` 生成 `dist/AgentsChat-Portable/` 与 zip，自带 Python 嵌入式运行时，解压即用。
- 托盘只做三件事：启停后台、展示状态、打开配置/数据目录。本机空闲实测约：托盘 41 MB、后台 29 MB，空闲不调用模型。

---

## 配置文件与数据

| 内容 | Windows 默认位置 |
| --- | --- |
| 配置 | `%LOCALAPPDATA%\Programs\AgentsChat\config.local.json` |
| 状态库 | `%LOCALAPPDATA%\Programs\AgentsChat\.bridge\bridge.sqlite3` |
| 运行日志 | `.bridge\broker.log`、`.bridge\runs\<task-id>\` |
| 技能 | `%USERPROFILE%\.agents\skills\`、`.qoder\skills\`、`.codex\skills\` |

状态目录同一时间只允许一个后台实例。

---

## 升级与卸载

```powershell
# 升级：重跑安装命令即可，配置与数据保留
irm https://github.com/Shrbuz/agents-chat/releases/latest/download/install.ps1 | iex

# 卸载（默认保留配置和数据）
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\AgentsChat\uninstall.ps1"
```

`uninstall.ps1` 支持 `-RemoveData`（连数据一起删）、`-KeepSkill`（保留用户级技能）、`-DryRun`（只打印动作）。安装器和卸载器都会校验目标目录：空路径、盘符根、用户目录及其上级目录一律拒绝。

---

## 安全边界与已知限制

- 后台只监听 `127.0.0.1` 随机端口，需本机 bearer；拒绝浏览器 Origin 与外部 Host。信任边界是同一个操作系统用户，**不是多租户安全产品**。
- 任务不能指定任意可执行程序或工作目录，这些由受信任的角色配置决定。
- `succeeded` 只表示 Runtime 正常结束，不等于业务验收——仍要检查证据和副作用。
- 模型仍调用厂商在线服务；空闲等待、去重、邮箱与路由由本地代码完成。
- 桌面绑定依赖厂商版本相关入口，更新桌面应用后可能需要重新绑定或重启 Qoder 原生连接。
- macOS 只在 Git bash 下验证过 `install.sh`，真实机器待验。
- 可公开复现的检查记录见 [VERIFICATION.md](VERIFICATION.md)。

---

## 从源码开发

```powershell
python install.py                                  # 安装到默认位置
python bridge.py --config config.local.json start  # 直接从源码起后台
python bridge.py status
python -m unittest discover -s tests -v            # 81 项测试
python build_exe.py                                # 编译托盘与 CLI
python build_portable.py                           # 生成便携包
```

设计说明见 [DESIGN.md](DESIGN.md)，集成方式见 [INTEGRATION.md](INTEGRATION.md)。

---

## 许可证

[MIT](LICENSE)


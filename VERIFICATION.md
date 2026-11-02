# 验证记录

本文件只记录可公开复现的检查。真实桌面聊天 ID、任务 ID、业务项目路径和本机截图不进入开源仓库。

## 0.6.0 文件总线协议原生并入

此前中枢只监视 skill 的文件总线并据此创建唤醒任务，`claim`/租约/`result`/`verify` 仍在外部脚本里，任务状态存在两处。本版把协议实现进 `bridge`，总线文件仍是唯一真源。

| 检查 | 结果 |
|---|---|
| 协议实现 | `bridge/bus.py`：每角色目录追加式事件、`_state/bus.lock` 上的 OS 文件锁、临时文件 + fsync + 原子改名；序列缺口、断链、重名、外来消息一律拒绝 |
| 状态机 | `pending → in_progress → resolved → closed`；`verify(fail)` 回落 `pending`；`blocked` 释放领取；租约过期后旧 token 提交被拒，新会话可重新领取 |
| CLI | `agents-chat bus` 提供 request/claim/renew/progress/blocked/result/verify/cancel/inbox/mark-read/status/show/paths |
| 端到端 | 临时项目实测：request → claim（取得 token）→ result → verify pass → closed；`status` 无残留；另一会话 `inbox` 未读 2 条；旧 token 提交被拒 |
| 对照基准 | 同一串操作分别用 `bridge/bus.py` 与随包 skill 的 `agent_chat.py` 执行，归一化后事件序列与最终任务状态逐条一致 |
| 投影 | `store` 新增 `bus_state`（唤醒游标）与 `bus_tasks`（任务投影）；`sync_bus` 按重放结果整体替换，日志中消失的任务不会残留；损坏的总线跳过投影但不影响唤醒 |
| 去重 | 不再写 `agent-chat-cursors.json` 旁路文件，唤醒游标存于 store |
| 测试 | 69 项通过（新增 21 项：协议 10、CLI 5、投影 6） |

## 0.5.3 配套技能的解释器

安装器宣称“不需要预装 Python”，但随包复制的 `agent-chat` 配套技能，其 `scripts/agent_chat.py` 依赖 `python`。目标机没有 Python 时，技能会被装好却无法执行，且没有任何提示。

| 检查 | 结果 |
|---|---|
| 解析顺序 | 安装时依次探测包内 `runtime\python\python.exe` → PATH 的 `python` → `py` 启动器 |
| 落盘 | 把实际路径写入技能目录的 `runtime.json`（`source` 为 `bundled`/`path`/`py-launcher`/`missing`）；`SKILL.md` 要求优先读取该文件 |
| 无解释器 | 写入 `source: "missing"`，并在安装日志明确警告“技能已复制但不可执行”，不静默通过 |
| 保护用户副本 | `agent-chat` 目录已存在时不覆盖；是 Junction 时不写入 `runtime.json` |
| 实测（摘掉 PATH 中的 Python） | 便携包 → `bundled`；去掉包内运行时但有 `py.exe` → `py-launcher`；PATH 仅剩 System32 → `missing` 并输出警告 |
| 回归 | 48 项 Python 测试通过；三个 `.ps1` 的 PowerShell AST 解析无错误 |

## 0.5.2 安装器安全加固（2026-10-31）

一次真实的安装器测试事故暴露了这两个脚本缺护栏：`[System.IO.Path]::GetFullPath("")` 在 Windows PowerShell 5.1 中返回**当前工作目录**，因此空的 `-Destination` 会让后续 `Remove-Item -Recurse -Force` 落在 CWD 上；而两个脚本对 `$Destination` 原本没有任何校验。

| 检查 | 结果 |
|---|---|
| 目标校验 | 拒绝空/相对路径、盘符与共享根、少于 3 层的路径，以及等于或**包含** `USERPROFILE`/`LOCALAPPDATA`/`APPDATA`/`TEMP`/`ProgramFiles`/`WINDIR`、桌面/文档等受保护目录的路径 |
| 拒绝用例 | 空 `-Destination`、`$env:USERPROFILE`、`C:\Users`、`C:\` 四种输入全部被拒绝，并给出可读原因 |
| 删除约束 | 安装器只删 `$Destination` 的直接子项（`Remove-InstallChild`，拒绝 `..` 与路径分隔符）；卸载前要求目录内存在 `AgentsChat.exe` 与 `bridge.py`，否则拒绝删除 |
| 归属保护 | 注册表 `InstallLocation` 与目标不一致时只警告并跳过快捷方式/注册表/技能清理，另一份安装保持可用；`-NoRegistry` 副本仍可正常卸载 |
| 端到端 | 本地 zip 安装到临时目录、重复安装保留 `data` 标记文件、卸载保留配置与数据、`-RemoveData` 清理仅剩数据的目录，全部通过 |
| 预演 | 两个脚本新增 `-DryRun`，只打印将执行的动作，不写入任何文件 |
| 安装器自检 | 拒绝往“非空且没有 `AgentsChat.exe`”的陌生目录安装 |
| 发布物 | `dist/` 与 `AgentsChat-Portable-0.5.2.zip` 已重建，仓库地址占位符 `__REPO__` 由 release workflow 注入 |

## 0.5.2 开箱即用的双技能

| 检查 | 结果 |
|---|---|
| 技能打包 | `tool-skill/agent-chat/` 随源码/便携包发布；安装器同时安装 `agents-chat-desktop` 和 `agent-chat` 到 Codex/Qoder/DSH 用户级技能目录 |
| 用户技能保护 | 已存在或被 junction 管理的 `agent-chat` 目录不会被安装器覆盖 |
| 自动初始化 | 新项目 `setup` 默认调用 `agent_chat.py init` 创建文件总线并写入关联；`--no-agent-chat` 可关闭；已有 `agent_chat/project.json` 不重建 |
| 48 项测试 | 新增“bundled agent-chat 自动初始化”测试；其余 47 项回归通过 |
| 兼容性 | Agents Chat 只唤醒配置了 `agent_chat` 映射的角色，leader 等不映射的角色不会被误跑成 CLI 任务 |

## 0.5.1 Qoder 原生多角色

| 检查 | 结果 |
|---|---|
| 启动修复 | 从 Codex/DSH 终端启动 Qoder 时清除继承的 `ELECTRON_RUN_AS_NODE=1`；否则 Qoder 以 Node 模式启动后立即退出 |
| 原生扩展 | 重启后 `qoder-native-status.json` 为 `ready=true`；扩展改为轮询所有 `qoder_native` 绑定，而不是只认一个 `qoder` 名称 |
| 多聊天绑定 | 同一 Qoder 应用内的 frontend/backend 两个聊天分别绑定，`roles` 均显示 `desktop_session`/`qoder_native`/online |
| 跨角色投递 | frontend 实际收到 Agents Chat 任务并继续执行；backend 收到只读检查任务，51 秒完成并返回 native 回执与 terminal 事件 |
| agent-chat 兼容 | 原 agent-chat request 在 Qoder 重启后完成 result 回传；Agents Chat watcher 只唤醒配置的 frontend/backend，不误唤醒 leader |
| 退出保护 | 关闭 Qoder 时检测到运行中任务并弹出“停止任务并退出”确认；未自动静默丢弃 |

## 0.5 多项目/角色中枢

| 检查 | 结果 |
|---|---|
| Python 自动化 | 48/48 通过：配置与地址解析、跨项目任务与回调、peers 白名单、session 注册/心跳、setup 幂等、bundled agent-chat 初始化、agent-chat 唤醒、MCP 注册、CLI/桌面回归 |
| v2 配置 | 新项目地址为 `项目.角色`；旧单项目配置保持 `codex`/`qoder` 原名；重名必须限定项目 |
| 跨项目任务 | fixture 子进程真实执行：`alpha.backend → beta.frontend` 成功，结果回到 `alpha.backend` 收件箱 |
| 跨项目回调 | `reply_mode=run` 可由另一项目的角色执行，回调目标为原发起角色 |
| 会话与绑定 | register/list/heartbeat/remove；桌面绑定自动登记 `desktop:<chat-id>` 会话，解绑移除 |
| setup | 生成 `.agents-chat/project.json`、角色提示、`AGENTS.md` 标记块和每角色 MCP 示例；重复执行幂等 |
| agent-chat 兼容 | 首次启动不重放旧事件；新 request 只生成一次 `notify=false` 唤醒任务，不写入结果邮箱 |
| `join` | 显式 session 绑定成功并返回 `desktop_session`；缺少连接器上下文时明确告警，不回退 CLI |
| 便携包 | 官方 Python 3.14.8 embeddable；便携 CLI 完成 setup → start → 跨项目 send → wait → inbox → stop；便携托盘可启动后台 |
| 托盘 | 实际启动托盘、自动拉起后台、唤出状态窗口；本机空闲约托盘 41 MB、后台 29 MB |
| 一行安装器 | 本地 zip 安装、更新保留 `config.json`/`data`、卸载 `-RemoveData` 均通过；`install.ps1`/`uninstall.ps1` PowerShell AST 语法解析通过 |
| GitHub Release | `v0.5.0` 已公开；tag push 触发 Actions 成功；Release 含便携 zip、SHA-256、注入仓库地址的 `install.ps1`；从 `releases/latest/download/install.ps1` 实际下载并安装到临时目录通过，SHA-256 校验通过 |
| 安装版更新 | 旧队列和桌面绑定存在时重复安装成功，旧任务计数、桌面绑定和 Qoder 输入禁用哨兵保留 |

fixture 验证不调用真实模型。真实 Codex/Qoder 跨项目往返、Qoder 原生桌面往返和运行中插话/取消仍需在用户环境单独验收；`succeeded` 只表示 Runtime 正常结束，不等于业务验收。

## 历史版本

- 0.4：轻量 EXE 入口（GUI/CLI 分离），EXE stdio MCP 和安装版 health/targets 实际通过；35 项测试。
- 0.3：Qoder 原生扩展 `continueSession` + metadata-only `observeTurn` + `native-result` 回执链路在本机 Qoder Desktop 0.4.11 上验证；普通扩展不能读取聊天全文。
- 0.2：Codex/Qoder 原有桌面聊天的显式绑定与往返；Qoder 输入框自动操作已禁用并持久保存。

历史版本的原始任务/会话记录和截图仅保存在本地，不随开源仓库发布。

# 验证记录

本文件只记录可公开复现的检查。真实桌面聊天 ID、任务 ID、业务项目路径和本机截图不进入开源仓库。

## 0.5 多项目/角色中枢

| 检查 | 结果 |
|---|---|
| Python 自动化 | 47/47 通过：配置与地址解析、跨项目任务与回调、peers 白名单、session 注册/心跳、setup 幂等、agent-chat 唤醒、MCP 注册、CLI/桌面回归 |
| v2 配置 | 新项目地址为 `项目.角色`；旧单项目配置保持 `codex`/`qoder` 原名；重名必须限定项目 |
| 跨项目任务 | fixture 子进程真实执行：`alpha.backend → beta.frontend` 成功，结果回到 `alpha.backend` 收件箱 |
| 跨项目回调 | `reply_mode=run` 可由另一项目的角色执行，回调目标为原发起角色 |
| 会话与绑定 | register/list/heartbeat/remove；桌面绑定自动登记 `desktop:<chat-id>` 会话，解绑移除 |
| setup | 生成 `.agents-chat/project.json`、角色提示、`AGENTS.md` 标记块和每角色 MCP 示例；重复执行幂等 |
| agent-chat 兼容 | 首次启动不重放旧事件；新 request 只生成一次 `notify=false` 唤醒任务，不写入结果邮箱 |
| `join` | 显式 session 绑定成功并返回 `desktop_session`；缺少连接器上下文时明确告警，不回退 CLI |
| 便携包 | 官方 Python 3.14.8 embeddable；便携 CLI 完成 setup → start → 跨项目 send → wait → inbox → stop；便携托盘可启动后台 |
| 托盘 | 实际启动托盘、自动拉起后台、唤出状态窗口；本机空闲约托盘 41 MB、后台 29 MB |
| 一行安装器 | 本地 zip 安装、更新保留 `config.json`/`data`、卸载 `-RemoveData` 均通过；`install.ps1`/`uninstall.ps1` PowerShell AST 语法解析通过；GitHub 远程 Release 下载流程需发布后首次 tag 验证 |
| 安装版更新 | 旧队列和桌面绑定存在时重复安装成功，旧任务计数、桌面绑定和 Qoder 输入禁用哨兵保留 |

fixture 验证不调用真实模型。真实 Codex/Qoder 跨项目往返、Qoder 原生桌面往返和运行中插话/取消仍需在用户环境单独验收；`succeeded` 只表示 Runtime 正常结束，不等于业务验收。

## 历史版本

- 0.4：轻量 EXE 入口（GUI/CLI 分离），EXE stdio MCP 和安装版 health/targets 实际通过；35 项测试。
- 0.3：Qoder 原生扩展 `continueSession` + metadata-only `observeTurn` + `native-result` 回执链路在本机 Qoder Desktop 0.4.11 上验证；普通扩展不能读取聊天全文。
- 0.2：Codex/Qoder 原有桌面聊天的显式绑定与往返；Qoder 输入框自动操作已禁用并持久保存。

历史版本的原始任务/会话记录和截图仅保存在本地，不随开源仓库发布。

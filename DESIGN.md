# 设计与边界

## 0.5 多项目角色中枢

### 为什么不是“子代理”

常规子代理把任务交给同一 Agent 的隔离上下文，结果回到父代理。这里的协作单位是**会话角色**：每个会话有自己的项目、职责和收件箱；同一 Agent 可以占多个角色，不同 Agent 也可以互相派发。角色之间的消息是不可变任务记录，不是共享上下文。这样才能让两个已经打开的桌面聊天、一个后台 CLI 和另一个项目的验证会话同时参与，而不是要求一个父模型持续在线调度。

### 地址与配置

中央配置是 v2：

```json
{
  "version": 2,
  "projects": {
    "alpha": {
      "name": "Alpha",
      "root": "E:/work/alpha",
      "agent_chat": {
        "bus": "E:/work/alpha/agent_chat",
        "roles": {"backend": "backend", "frontend": "frontend"}
      },
      "roles": {
        "backend": {
          "adapter": "codex", "command": ["codex"], "cwd": ".",
          "description": "API 与数据", "peers": ["alpha.frontend"],
          "sandbox": "workspace-write"
        }
      }
    }
  }
}
```

- 内部地址：新项目的角色是 `项目.角色`；旧单项目配置继续使用 `codex`/`qoder` 等原名，保证队列、桌面绑定和 MCP 参数不失效。
- 解析顺序：完整内部地址 → `项目.角色` → 指定项目内的角色 → 全局唯一角色名；重名必须限定项目。
- `peers` 是可选出站白名单。空列表表示不限制；非空时只允许列出的角色。跨项目目标写完整地址。
- 配置写入由 CLI 的 `project/role/setup` 完成，校验后再原子替换；运行中的后台通过重启加载。`setup` 幂等，不覆盖已有角色的运行时设置。

### 队列、会话和路由

一个 SQLite 仍然保存全局任务/结果/事件，新增 `sessions` 表保存会话存在性：

- `tasks`：`(sender, idem)` 唯一，原子领取，`queued → running → terminal`。
- `messages`：每个任务最多一封结果，写入发起角色的邮箱；`reply_mode=run` 只额外排一个 `mailbox` 回调，不递归。
- `sessions`：角色、会话 ID、传输、在线心跳、描述；90 秒无心跳视为离线。桌面绑定也会注册一个 `desktop:<chat-id>` 会话。
- 同一角色同一时间只有一个执行者。同角色多会话是“可见的多个会话”，不是并发抢占；需要租约语义时使用 agent-chat skill 的 claim。
- 每个角色一个 worker；桌面绑定角色按会话串行，不占用 CLI 工作目录锁，避免跨角色互等死锁。
- 同 cwd 的 CLI 角色串行；不同 cwd 可以并行。

### 自主消息传递

`send` 是唯一的派发入口，结果是自动回传，不需要目标角色再发一条“回复任务”：

1. 发起会话调用 `send --to 角色`；
2. 目标角色的 worker 领取，CLI 角色启动短任务，桌面角色把 prompt 投递到绑定聊天；
3. 目标结束时结果写回发起角色的 `messages`；
4. 发起角色在 `inbox` 读取；如果正阻塞等待，`wait` 由本地条件变量唤醒，不轮询模型。

任务正文只包含当前任务、角色地址和最多 8 个本地文件引用；不注入旧邮箱、历史对话或进度事件。`reply_mode=run` 是唯一会额外唤醒发送方模型的情况，且只一次。

### agent-chat 文件总线兼容

用户已有的 `agent_chat/project.json` 保持不动。后台的 `AgentChatBridge` 只做唤醒：

- 首次启动把游标定位到当前最新事件，不重放历史；
- 之后扫描各角色 `*_msg` 目录，只对 `request`、`result`、`blocked`、`cancel` 和 `verify(fail)` 事件生成一次短唤醒任务；
- 唤醒任务带 `notify=false`，不进入结果邮箱，不产生二次派发；
- claim、租约、progress、result、verify 仍由该 skill 的 helper 执行；
- 角色有空闲任务时暂缓下一条唤醒，游标不前移，避免一次堆积大量任务；
- 事件正文和附件都是数据，不扩大工具权限。

这样，“同一 Agent 的多会话”可以直接用 skill 的文件协议；需要跨 Agent 唤醒时由后台补上传输。两套状态不混写。

### 桌面传输

- **Codex**：宿主提供 app-tools MCP。连接器按角色启动，读取精简 turn 状态；跨聊天投递在目标轮次中表现为 `functionCallOutput`，因此按“新增轮次 + 发送工具 + 最终答案”固定。不是公开 REST API，宿主更新后可能需要重新准备。
- **Qoder**：普通原生开发扩展 `agents-chat.native`，权限仅 `continueSession`、`metadata-only observeTurn`、`interruptTurn`。不读聊天全文，不操作输入框，不伪装内置通信扩展。任务完成时目标执行一次本地 `native-result` 短回执，扩展核对同一会话/轮次终态后才提交成功。
- 连接器离线时明确失败，不静默回退 CLI；取消是协作式的，不承诺回滚已发生的文件修改。

### 托盘与便携

`AgentsChat.exe` 是 .NET Framework 4 的小型托盘宿主，不包含业务逻辑：

- 单实例 mutex；`ui/panel` 通过命名事件唤出已运行实例的状态窗口；
- 启动时调用 `AgentsChat.Cli.exe --state ... start` 拉起 Python 后台，停止时调用 `stop`；
- 每 3 秒读取 `endpoint.json` 并调用本地 `status`，只用于展示；
- 状态窗口列出项目/角色/适配器/模式/在线会话/队列/职责；
- 托盘退出可选择“后台继续”或“停止后台并退出”。

便携包使用官方 Python embeddable 运行时（约 24 MB），复制 `bridge`、连接器、托盘/CLI EXE 和空配置；`python-runtime.txt` 指向相对路径，移动目录后仍可运行。项目内 `.agents-chat/project.json` 保存的是生成时的绝对后台路径；移动便携目录后重新运行 setup 或修正 `hub` 字段即可。

### 数据与失败语义

- 一个状态目录只允许一个后台持有 OS 文件锁；WAL 和事务保证队列/结果一致。
- 重启把所有 `running` 标为 `interrupted`，不自动重试有副作用的任务；排队任务恢复调度。
- 超时终止进程树；输出有上限；完整日志在 `runs/<task_id>/`，不进入模型上下文。
- `succeeded` 仅是 Runtime 成功结束；业务验收仍需证据。
- `sender` 是路由标签，不是身份认证；本机 bearer 只保护 loopback 接口，信任边界是同一 OS 用户。
- 任务不能指定可执行程序/工作目录；这些来自受信任配置。正文用 stdin，不拼 shell。

### 与 0.4 的关系

0.4 已实现单项目 Broker、SQLite 队列、Codex/Qoder CLI Adapter、桌面绑定和 Qoder 原生后台。0.5 保留这些执行与传输代码，新增项目/角色/会话层、托盘宿主和便携打包；旧配置自动兼容，不需要迁移数据库。旧的 UIA 输入框路径继续禁用。

### 后续方向

- 同角色多会话的显式领取/租约（可复用 agent-chat 的 claim 语义），而不是只做可见性；
- 更细的计划/预算与结果摘要缓存；
- WorkBuddy 真实模型联调；
- 托盘内的“一键从 agent-chat 项目导入角色”，减少 setup 参数；
- 跨设备时把同一协议放到明确授权的私网链路，当前监听器固定本机。

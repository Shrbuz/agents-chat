# Helper 命令

需要 Python 3.10+，仅使用标准库。下面的 `CLI` 表示实际安装技能下的
`scripts/agent_chat.py` 的绝对路径；`PROJECT` 表示目标项目目录。
所有命令输出 JSON；错误退出码为 2。请用文件保存中文或多行请求，
避免 Windows shell 参数转义损坏内容。

```text
python CLI --project PROJECT doctor
python CLI --project PROJECT paths
python CLI --project PROJECT onboard
python CLI --project PROJECT status --role frontend
python CLI --project PROJECT inbox --role frontend --session frontend-UUID
python CLI --project PROJECT mark-read --role frontend --session frontend-UUID --message MESSAGE_ID
python CLI --project PROJECT show --task TASK_ID
```

`inbox` 返回当前会话未读的消息（不含续期心跳），任务已终结也不会隐藏
通知；最多返回 50 条，同时给出 total_unread，可以用 `--limit` 调整。
每条消息附上当前 task_status，处理旧请求前核对该字段或 show 任务。
只有显式 mark-read 才标记已读；该字段不会写回或改写原始事件。
`status --role` 列出分配给该角色和该角色发出的未关闭任务；需要查看
已关闭/取消任务时加 `--include-closed`。任务列表和消息未读状态相互独立。

## 请求、领取、回报、验收

`request.json` 示例：

```json
{
  "title": "生产版本的创作按钮无法开始任务",
  "body": "复现：登录测试账号，点击开始创作。预期进入访谈；实际一直加载。",
  "acceptance": ["通过真实浏览器点击后进入首轮访谈", "失败时显示可理解的错误提示"],
  "priority": "P1",
  "context": {"environment": "production", "release_id": "从部署回执取得的实际版本"}
}
```

```text
python CLI --project PROJECT send --from tester --to backend --session tester-UUID --data-file /absolute/request.json
python CLI --project PROJECT claim --role backend --session backend-UUID --task TASK_ID
python CLI --project PROJECT renew --role backend --session backend-UUID --task TASK_ID --token TOKEN
python CLI --project PROJECT progress --role backend --session backend-UUID --task TASK_ID --token TOKEN --body "已定位问题，正在修复"
python CLI --project PROJECT blocked --role backend --session backend-UUID --task TASK_ID --token TOKEN --body "等待部署，关联任务 TASK_ID"
```

lease 默认 1800 秒，claim 可用 `--lease-seconds` 修改为 30 至 86400 秒。
有效领取只能由原 session 用 renew 续期；其他会话无法抢占。过期后必须
重新 claim 取得新 token。

结果文件：

```json
{
  "body": "已完成；说明实际变更、测试结果和剩余限制。",
  "evidence": ["commit: 实际提交号", "报告的绝对路径或可核实的测试结果"],
  "context": {"release_id": "实际部署版本"}
}
```

```text
python CLI --project PROJECT result --role backend --session backend-UUID --task TASK_ID --token TOKEN --data-file /absolute/result.json
python CLI --project PROJECT verify --role tester --session tester-UUID --task TASK_ID --outcome pass --body "已核对本次部署并复测通过"
python CLI --project PROJECT verify --role tester --session tester-UUID --task TASK_ID --outcome fail --body "复测仍失败，附复现信息"
python CLI --project PROJECT cancel --role tester --session tester-UUID --task TASK_ID --body "需求已撤回"
```

`send --message-id UUID` 是可选幂等键；同 ID 且内容相同返回原事件，
内容不同会报错。所有后续事件均支持此参数。超时重试应复用同一 ID，
不要生成新 ID 重复下单。claim 的幂等返回保留原 token 和原到期时间。
body 参数和 data-file 互斥；result 必须通过 data-file 给出 evidence。

## 换会话与换客户端接管

同一个客户端换聊天窗口，只替换会话（不改 adapter、不重启中枢）：

```text
AgentsChat.Cli.exe --project PROJECT session takeover --role ROLE \
  --session NEW_CHAT --transport codex_app_tools
```

换客户端接管（例如 DSH 角色由 Codex 接管）必须迁移 adapter，命令是原子的：

```text
AgentsChat.Cli.exe --project PROJECT session migrate-and-takeover --role ROLE \
  --session NEW_CHAT --transport codex_app_tools --adapter auto
```

- `--transport` 只能是当前客户端的传输：`codex_app_tools` / `qoder_native` / `dsh_plugin`；
  `--adapter auto` 按 transport 推导 `codex` / `qoder` / `dsh`。
- 迁移依次完成：主配置 → `.agents-chat/project.json` → 重启中枢 → 绑定新会话 →
  退休旧会话 → 写 `_state/active-sessions.json`；任一步失败会回滚。
- 成功输出含 `adapter_changed`、`hub_restarted`、`active_session`、`retired_sessions`、
  `postconditions`。`postconditions.ok` 必须为 true，且
  `role_adapter == session_adapter == 当前客户端适配器`、`transport` 与 adapter 匹配、
  新会话唯一活跃、旧会话全部 retired。
- 旧命令 `session takeover` 在 adapter 与 transport 不一致时返回
  `adapter_transport_mismatch`，不会制造半迁移状态。
- `join --migrate-adapter` 是同一迁移流程的入口；不带该参数时，跨客户端 join 会被拒绝。

## 通知与超时（中枢 CLI）

只提醒对方查看收件箱时用 `notify`（别名 `wakeup`），它自动标记为
`agent_chat_wakeup`，投递成功即完成，不等待也不中断目标执行：

```text
AgentsChat.Cli.exe --project PROJECT notify --from leader --to backend --text "看看收件箱"
```

- `send --timeout` 是目标执行等待上限；超时默认只停止发送方等待，目标会话继续工作。
- 只有显式 `--interrupt-on-timeout` 才允许超时后发送 cancel。
- `--delivery-timeout` 单独控制目标聊天接住消息的等待时间。
- 任务结果带 `run_delivered`、`cancel_requested`、`cancel_acknowledged`；
  `desktop_delivery_timeout` 与 `desktop_execution_timeout` 是两种不同的超时。
- Windows 中文用 `--file <utf8文件>` 或 `--text-stdin`；含 U+FFFD 的文本会被拒绝。
- agent-chat 总线里的 pending request 会在中枢启动/巡检时自动补发唤醒任务，
  不需要手工 `send` 提醒。
- `claim` 会唤醒 requester，`result` 会唤醒 requester，`verify`（pass/fail）
  会唤醒 assignee；每个事件只生成一个 `agent_chat_wakeup`。
- `health` 里看 `degraded` / `agent_chat_health`；唤醒线程异常或扫描失败时
  不应再把它当作正常。

## 初始化别的项目

```text
python CLI --project /absolute/new-project init --project-id unique-project-id
python CLI --project /absolute/new-project init --project-id unique-project-id --roles builder reviewer
```

上面两种 init 写法二选一：不传 roles 是兼容旧用法的 Web 四角色默认值，
新项目应先按 [onboarding.md](onboarding.md) 分析角色。显式传入 roles 时
只创建这些角色的 `<role>_msg` 目录。角色 ID 使用 1-48 位小写字母、数字、
下划线或连字符，首位为字母，不能重复。所有 --role/--from/--to 都按
当前 project.json 的 roles 验证，而不是固定四个名字。

需要同时保存职责时，用 UTF-8 JSON 文件（角色 ID → 非空职责说明）：

```json
{"analyst": "分析数据与解释指标", "reviewer": "复核数据口径与结论证据"}
```

```text
python CLI --project /absolute/new-project init --project-id unique-project-id --roles-file /absolute/roles.json
python CLI --project /absolute/new-project onboard
```

--roles 与 --roles-file 互斥。职责保存为 role_descriptions，角色路由仍为
roles。onboard 只读输出各配置角色的职责、目录和实际项目开场白，不创建
任务、聊天或后台服务。命令示例不是对实际创建聊天的授权。

init 不覆盖已有项目配置或入口文件，会创建 `agent_chat/project.json`、
选定角色的收件箱、对应消息忽略规则、协议入口及项目启动说明。随后在该项目
已有 `AGENTS.md` 中合并启动说明。不存在 AGENTS.md 时 init 会创建一个
仅用于本流程的入口。保持 global skill 通用，在 project.json 填项目事实。
已配置的自定义角色再次 init 不会被换回四角色；显式角色列表不一致时报错。
角色迁移需另行规划，不直接删改有历史的邮箱或只改某个 worktree 的配置。

同 Git 仓库的 worktree 默认定位主 checkout 下对应项目的 bus，保留
项目相对仓库根目录的位置。例如 `repo/apps/a` 使用
`repo/apps/a/agent_chat`，其 linked worktree 中的 `apps/a` 共用该目录，
`apps/b` 则使用自己的 bus。仓库根项目的路径保持 `repo/agent_chat`。
旧版本曾误写到仓库根 bus 的子项目消息不会自动搬迁；先核对其 project_id
与真实数据位置，再迁移，避免混入其他项目。

不同机器或 clone
通过 `AGENT_CHAT_ROOT` 环境变量或不进 Git 的 `agent_chat/project.local.json`
指定同一个绝对 `bus_root`；对应文件系统必须支持锁和原子 rename。

```json
{"bus_root": "/absolute/shared/agent_chat"}
```

## 安装到两个客户端

Windows：从技能目录运行 `scripts/install.ps1`，它只为不存在的 agent-chat
入口创建目录 junction。已有不同入口时报错，不覆盖。入口链接到安装
脚本所在的技能目录（scripts 的上一级），更新该目录即可供 Codex/Qoder
使用；无论技能放在个人技能库还是其他目录，规则相同。移动或删除该
源目录会使入口失效，迁移后需重新接入。其他机器可把整个技能目录安装
到各客户端的用户技能目录。

## 排查与修复

| 用途 | 命令 |
| --- | --- |
| 列出被跳过或有问题的消息 | `AgentsChat.Cli.exe --project <p> bus validate` |
| 预览要隔离哪些文件 | `... bus repair` |
| 实际隔离（移动到 `_quarantine-<时间>/`，不删除） | `... bus repair --apply` |

读取时坏文件会被跳过并记录，不再让整个总线不可用；隔离后原命令即可正常工作。

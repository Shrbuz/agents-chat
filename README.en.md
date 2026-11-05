# Agents Chat

[简体中文](README.md) | **English**

[![Release](https://github.com/Shrbuz/agents-chat/actions/workflows/release.yml/badge.svg)](https://github.com/Shrbuz/agents-chat/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Version](https://img.shields.io/badge/version-0.6.11-blue)

Let several agent chats on one machine hand work to each other, return results automatically, and verify them. A role in a project (`leader`, `frontend`, `tester`, …) is a collaboration seat; tasks are routed by role, so nobody has to copy text between two chat windows.

- **Local first**: the hub listens on `127.0.0.1` only; data lives in a local SQLite database and JSON files
- **Started on demand**: no login autostart; whichever session needs it starts the hub
- **Visible**: tasks can be delivered into the Qoder / DSH chat you already have open

---

## Contents

- [Quick start](#quick-start)
- [Core concepts](#core-concepts)
- [Two collaboration modes](#two-collaboration-modes)
- [File bus](#file-bus)
- [DSH integration](#dsh-integration)
- [Desktop chat bindings](#desktop-chat-bindings)
- [CLI reference](#cli-reference)
- [MCP](#mcp)
- [Tray and portable build](#tray-and-portable-build)
- [Configuration and data](#configuration-and-data)
- [Upgrade and uninstall](#upgrade-and-uninstall)
- [Security boundaries and known limits](#security-boundaries-and-known-limits)
- [Building from source](#building-from-source)
- [License](#license)

---

## Quick start

### Windows 10 / 11

PowerShell:

```powershell
irm https://github.com/Shrbuz/agents-chat/releases/latest/download/install.ps1 | iex
```

Equivalent in CMD:

```bat
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://github.com/Shrbuz/agents-chat/releases/latest/download/install.ps1 | iex"
```

The installer:

- downloads the self-contained portable archive into `%LOCALAPPDATA%\Programs\AgentsChat`;
- installs the `agents-chat-desktop` skill into the user skill directories of Codex / Qoder / DSH;
- creates Start Menu and desktop entries;
- verifies the SHA-256 of the release and keeps your existing `config.json` and data;
- needs no administrator rights and no preinstalled Python, pip or Node (Codex / Qoder themselves are still yours to install).

Then, in a **newly opened project chat**, say:

> Use the agents-chat-desktop skill to turn this project into multi-role desktop chat collaboration; you decide the roles. Bind Codex / Qoder chats with join; DSH chats are bound automatically by the host plugin and need no join. Finally tell me what to say in each chat.

Existing chats cache the old skill; open a new session to pick up the latest version.

### macOS / Linux

There is no separate portable build yet; run the installer from the source directory:

```bash
sh install.sh            # installs into ~/.local/opt/agents-chat
sh install.sh --dry-run  # print the actions only
sh install.sh --uninstall
```

- requires `python3` ≥ 3.11;
- installs once, then symlinks into `~/.codex/skills`, `~/.qoder/skills`, `~/.agents/skills`, `~/.claude/skills` (existing real directories are never overwritten);
- macOS installs a launchd agent by default, `--no-service` skips it; Linux prints a systemd user unit example;
- **Limitation**: no tray, no desktop chat bindings — roles run as CLI tasks. Collaboration works, but messages never appear inside the chat window you are using.

---

## Core concepts

| Concept | Meaning |
| --- | --- |
| **Project** | A directory and its collaboration context. One hub can host several projects. |
| **Role** | A seat inside a project, addressed as `<project>.<role>`, e.g. `alpha.backend`. |
| **Session** | One concrete conversation or process of a role. A desktop chat can additionally be bound as its delivery target. |

```text
alpha.frontend ──send──▶ alpha.backend
        ▲                      │
        └──── result inbox ────┘

beta.architect ──send alpha.backend──▶ alpha.backend   (cross-project)
```

There is no central dispatcher agent. Each session decides what to hand to whom according to its role prompt; the hub only persists, queues, delivers and waits, and calls no model while idle.

---

## Two collaboration modes

One request always follows one path; there is no "two systems" for the user to arbitrate:

| Does the project have an `agent_chat/` bus? | How work is dispatched | What the hub does |
| --- | --- | --- |
| Yes | File bus (`claim` / `result` / `verify`) | Wakes the role's session when a new event arrives |
| No | Hub queue (`send` / `wait`) | Delivers straight to the target role |

The skill detects which case applies and picks one without asking the user.

### A notification is not a cancellable execution

A normal `send --timeout` is how long the sender waits for the **target to finish**, not permission to kill the target chat. By default a timeout only stops the sender waiting; the target keeps working. Only an explicit `--interrupt-on-timeout` sends a cancel request. `--delivery-timeout` separately controls how long to wait for the chat to accept the prompt.

To remind a role to check its inbox, use the dedicated `notify` (alias `wakeup`). It is marked `agent_chat_wakeup`, completes on delivery, and never waits for or interrupts execution:

```powershell
& $cli --project alpha notify --from backend --to frontend --text "check your inbox"
```

Results report `run_delivered`, `cancel_requested` and `cancel_acknowledged`; `desktop_delivery_timeout` and `desktop_execution_timeout` are distinct states.

On Windows, pass CJK text with `--file` (UTF-8) or `--text-stdin`; text already containing U+FFFD is rejected instead of being stored as mojibake.

---

## File bus

Once a project is linked to an `agent_chat` bus, the hub implements the same protocol natively: events are append-only JSON files, guarded by an OS file lock on `_state/bus.lock`, written via temp file + fsync + atomic rename, with task state rebuilt by replaying events.

The wakeup chain now self-checks: if the bus was rebuilt and its sequence restarted at 1 while the stored cursor still points at an old high sequence, startup/periodic scans reset the cursor and reconcile tasks that still need action. `health` returns `degraded` and `agent_chat_health`; a dead or failing wakeup watcher is shown in the tray as **⚠ wakeup chain degraded**, instead of being hidden behind `workers_alive`.

```powershell
$cli = "$env:LOCALAPPDATA\Programs\AgentsChat\AgentsChat.Cli.exe"

# dispatch
& $cli --project alpha bus request --from backend --to frontend --session be-1 `
    --title "Login API" --body "details" --acceptance "returns 200" --priority P1

# claim (returns a lease token)
& $cli --project alpha bus claim <task-id> --role frontend --session fe-1

# report and verify
& $cli --project alpha bus result <task-id> --role frontend --session fe-1 --token <token> `
    --body "done" --evidence "tests pass"
& $cli --project alpha bus verify <task-id> --role backend --session be-1 --outcome pass --body "rechecked"
```

### Handing work over

| Who | Command | Effect |
| --- | --- | --- |
| The session doing the work | `bus release <task-id> --role <r> --session <s> --token <t> --body "what is done, why handing back"` | Task returns to `pending`, the lease is cleared, the note is kept for the next session |
| The requester | `bus reassign <task-id> --role <requester> --session <s> --body "why pulling back"` | Pulls the task back; the old holder's token stops working immediately |

The next session runs `bus show <task-id>` to read the full event chain, then `claim` takes effect at once.

### Switch chats / hand a role to another client

Two different actions: **switch session** only replaces the chat and keeps the adapter; **take over a role** means the current client owns the whole role, so a client-type change also migrates the adapter.

Same client, new chat window (no adapter rewrite, no hub restart):

```powershell
& $cli --project alpha session takeover --role frontend --session <new-session-id> --transport qoder_native
& $cli --project alpha session retire   --session <old-session-id>
```

Different client (for example a DSH tester taken over by Codex) must use the atomic migration:

```powershell
& $cli --project alpha session migrate-and-takeover --role tester `
    --session <current-session-id> --transport codex_app_tools --adapter auto
```

- `--adapter auto` derives the target: `codex_app_tools→codex`, `qoder_native→qoder`, `dsh_plugin→dsh`.
- The migration updates the main config, `.agents-chat/project.json`, restarts the hub, binds the new session, retires the old sessions and rewrites `active-sessions`; any failure rolls back.
- It returns `adapter_changed`, `hub_restarted`, `active_session`, `retired_sessions` and `postconditions`.
- The old `session takeover` returns `adapter_transport_mismatch` instead of leaving a half-migrated role.
- The tray marks mismatched roles with **⚠ mismatch** (red row plus a count in the header); the **Switch session…** button remains available.

### A damaged message no longer stops everyone

Messages that cannot be read are **skipped and recorded**; everything else keeps working:

```powershell
& $cli --project alpha bus validate          # list skipped or suspicious messages
& $cli --project alpha bus repair            # preview what would be quarantined
& $cli --project alpha bus repair --apply    # move them into _quarantine-<time>/, never delete
```

### Rules in brief

- State machine: `pending → in_progress → resolved → closed`; `verify --outcome fail` returns the task to `pending`.
- The lease is 30 minutes by default; desktop sessions are renewed by the hub, and a late result from the same session is still accepted after expiry.
- Requests must carry `--acceptance`, results must carry `--evidence`; the inbox tracks read state per role-and-session.
- Write messages with the CLI or the skill script, never by hand — `<seq:08d>_<message_id>.json` is the easiest thing to get wrong.

---

## DSH integration

### Recommended: `dsh_plugin`

The plugin runs inside the DSH host and delivers a task **as an ordinary user message** into the session you already have open. No process is spawned.

- Roles are discovered by adapter: any role with `adapter=dsh` is served by the plugin, so adding a role needs no plugin change;
- It binds to your own chat by matching the role name against session titles (`CMS_Tester` → `tester`);
- Session names come from the DSH session title, falling back to the session id;
- If the hub is not running, the plugin starts it.

Example overlay: `connectors/dsh-host/cordis.patch.yml.example`. One plugin instance serves any number of roles.

### Deprecated: `independent_cli_task`

The old mode spawned a `dsh headless` process per task: it ran outside your chat and could not see GUI credentials. A dsh role without a plugin binding now fails with `dsh_requires_plugin` instead of spawning anything.

---

## Desktop chat bindings

Roles run as independent CLI tasks by default. To deliver into an existing desktop chat:

- **Codex**: host app-tools MCP, `role bind --transport codex_app_tools`; one connector per role.
- **Qoder**: native extension `agents-chat.native`, `role bind --transport qoder_native`; it does not type into the composer, switch chats or steal focus.
- **DSH**: bound automatically by the host plugin; `join` is not needed.
- A binding is explicit routing: rebinding is refused while the role has queued or running work, and it never silently falls back to the CLI.

---

## CLI reference

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

Every command accepts `--config`, `--state` and `--project`; inside a project directory they can be omitted and are resolved from `.agents-chat/project.json`.

---

## MCP

The stdio MCP exposes nine tools: `bridge_send`, `bridge_wait`, `bridge_task`, `bridge_inbox`, `bridge_ack`, `bridge_targets`, `bridge_cancel`, `bridge_register`, `bridge_roles`. See [INTEGRATION.md](INTEGRATION.md) for wiring.

---

## Tray and portable build

- `AgentsChat.exe`: tray host; starts the local hub and opens a status window with a real table (columns, real chat names, start/stop buttons).
- `AgentsChat.Cli.exe`: the entry point for agents and scripts; the same directory also serves stdio MCP.
- `python build_portable.py` produces `dist/AgentsChat-Portable/` and a zip with an embedded Python runtime — unpack and run.
- The tray does three things only: start/stop the hub, show status, open the config and data directories. Measured idle on this machine: tray 41 MB, hub 29 MB, no model calls while idle.

---

## Configuration and data

| Item | Default location on Windows |
| --- | --- |
| Config | `%LOCALAPPDATA%\Programs\AgentsChat\config.local.json` |
| State database | `%LOCALAPPDATA%\Programs\AgentsChat\.bridge\bridge.sqlite3` |
| Logs | `.bridge\broker.log`, `.bridge\runs\<task-id>\` |
| Skills | `%USERPROFILE%\.agents\skills\`, `.qoder\skills\`, `.codex\skills\` |

Only one hub instance may use a state directory at a time.

---

## Upgrade and uninstall

```powershell
# Upgrade: rerun the installer; config and data are kept
irm https://github.com/Shrbuz/agents-chat/releases/latest/download/install.ps1 | iex

# Uninstall (config and data kept by default)
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\AgentsChat\uninstall.ps1"
```

`uninstall.ps1` supports `-RemoveData`, `-KeepSkill` and `-DryRun`. Both installer and uninstaller validate the target directory: empty paths, drive roots, the user profile and its parents are refused.

---

## Security boundaries and known limits

- The hub listens on a random `127.0.0.1` port behind a local bearer token and rejects browser origins and foreign hosts. The trust boundary is one OS user, **not a multi-tenant security product**.
- Tasks cannot name arbitrary executables or working directories; those come from trusted role configuration.
- `succeeded` means the runtime exited cleanly, not that the work is accepted — inspect the evidence and side effects.
- Models still call vendor services online; idle waiting, deduplication, mailboxes and routing are local code.
- Desktop bindings depend on vendor-specific entry points; after updating a desktop app you may need to rebind or restart the Qoder native connection.
- `install.sh` for macOS was only exercised under Git bash; a real Mac has not been verified yet.
- Reproducible checks are recorded in [VERIFICATION.md](VERIFICATION.md).

---

## Building from source

```powershell
python install.py                                  # install to the default location
python bridge.py --config config.local.json start  # run the hub from source
python bridge.py status
python -m unittest discover -s tests -v            # 81 tests
python build_exe.py                                # build tray and CLI
python build_portable.py                           # build the portable archive
```

Design notes: [DESIGN.md](DESIGN.md). Integration: [INTEGRATION.md](INTEGRATION.md).

---

## License

[MIT](LICENSE)


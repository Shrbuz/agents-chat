/**
 * agents-chat-dsh — a DeepSeek Harness host plugin that turns Agents Chat
 * tasks into real DSH sessions.
 *
 * The plugin does not know role names. It asks the hub which roles exist and
 * takes the ones whose adapter is `dsh`, so adding `designer`, or running
 * `frontend` inside DSH on some other project, needs no plugin change. Each
 * discovered role keeps its own persistent session (`agents-chat-<role>`), and
 * the list is refreshed periodically so new roles are picked up without a
 * restart.
 *
 * A task becomes an ordinary user turn on that role's session, so the
 * conversation appears in the desktop app like any other chat. When the session
 * goes idle the last `assistant/message` is reported back through the bus
 * protocol (claim -> work -> result).
 *
 * @module agents-chat-dsh
 */

import { execFile } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

export const name = 'agents-chat-dsh'

/** Required services: the agent registry and the session log. */
export const inject = ['agents', 'sessions']

const DEFAULTS = {
  cli: '',
  project: '',
  pollMs: 2000,
  /** How often the role list is re-read from the hub. */
  discoverMs: 60000,
  provider: 'qwen-token-plan-cn',
  model: 'deepseek-v4.1-flash',
  cwd: '',
}

function log(message) {
  console.log('[agents-chat-dsh] ' + message)
}

function cli(config, args) {
  return new Promise((resolve, reject) => {
    execFile(config.cli, ['--project', config.project, ...args], { windowsHide: true }, (error, stdout, stderr) => {
      if (error) {
        reject(new Error(String(stderr || error.message).trim().slice(0, 400)))
        return
      }
      try {
        resolve(JSON.parse(stdout))
      } catch {
        reject(new Error('unparsable CLI output: ' + String(stdout).slice(0, 200)))
      }
    })
  })
}

/** Build the user message the agent loop accepts (source needs a producer kind). */
function userMessage(text, id) {
  return { id, role: 'user', content: [{ type: 'text', text }], source: { kind: 'user' } }
}

/**
 * Sessions DSH keeps on disk, newest first, with the title the user sees.
 * Sessions this plugin created itself are skipped: the point is to attach to
 * the chat a person is actually looking at.
 */
function dshSessions() {
  const dir = path.join(os.homedir(), '.dsh', 'storages', 'session_projcache', 'sessions')
  let names = []
  try {
    names = fs.readdirSync(dir)
  } catch {
    return []
  }
  const found = []
  for (const name of names) {
    if (!name.endsWith('.json')) continue
    const id = name.slice(0, -5)
    if (id.startsWith('agents-chat-')) continue
    const entry = path.join(dir, name)
    try {
      const data = JSON.parse(fs.readFileSync(entry, 'utf8'))
      const title = data?.record?.rows?.title?.val
      if (!title) continue
      const mtime = fs.statSync(entry).mtimeMs
      if (Date.now() - mtime > 7 * 24 * 3600 * 1000) continue
      found.push({ id, title: String(title), mtime })
    } catch {
      // A half-written cache entry is not a reason to skip discovery entirely.
    }
  }
  return found.sort((left, right) => right.mtime - left.mtime)
}

/** Newest session whose title names the role, e.g. CMS_Tester for tester. */
function sessionForRole(role) {
  const needle = role.toLowerCase()
  const match = dshSessions().find((session) => session.title.toLowerCase().includes(needle))
  return match ? match.id : null
}
async function hubCall(config, method, params) {
  const endpointPath = path.join(path.dirname(config.cli), '.bridge', 'endpoint.json')
  const endpoint = JSON.parse(fs.readFileSync(endpointPath, 'utf8'))
  const response = await fetch(endpoint.url + '/call', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + endpoint.token },
    body: JSON.stringify({ method, params }),
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.error || 'HTTP ' + response.status)
  return payload.result
}

export function apply(ctx, rawConfig) {
  const config = { ...DEFAULTS, ...(rawConfig || {}) }
  /** Live role list; replaced whenever discovery runs. */
  let roles = []
  let lastDiscover = 0

  if (!config.cli || !config.project) {
    log('disabled: cli and project are required')
    return
  }

  function shape(role, sessionId, cwd, busy = false) {
    return { role: String(role), sessionId: String(sessionId || 'agents-chat-' + role), cwd: cwd || '', busy }
  }

  /** Roles come from the hub unless the config pins an explicit list. */
  async function discoverRoles() {
    if (Array.isArray(config.roles) && config.roles.length) {
      return config.roles.filter((item) => item && item.role)
        .map((item) => shape(item.role, item.sessionId, item.cwd || config.cwd))
    }
    if (config.role) return [shape(config.role, config.sessionId, config.cwd)]
    const info = await cli(config, ['roles'])
    const project = (info.projects || []).find((item) => item.id === config.project)
    if (!project) return []
    return (project.roles || [])
      .filter((item) => item.adapter === 'dsh')
      .map((item) => {
        // Prefer the user's own chat (CMS_Tester and friends). Creating a fresh
        // agents-chat-<role> session would answer into a chat nobody watches.
        const existing = sessionForRole(item.role)
        return shape(item.role, existing || 'agents-chat-' + item.role, config.cwd || project.root)
      })
  }

  async function refresh(force) {
    const now = Date.now()
    if (!force && now - lastDiscover < config.discoverMs) return
    lastDiscover = now
    try {
      const next = await discoverRoles()
      const busy = new Map(roles.map((item) => [item.role, item.busy]))
      roles = next.map((item) => ({ ...item, busy: busy.get(item.role) || false }))
      for (const target of roles) {
        if (target.bound) continue
        target.bound = true
        bindRole(target)
      }
      if (force || next.length !== busy.size) {
        log('roles: ' + (roles.map((item) => item.role + '@' + item.sessionId).join(', ') || 'none (no role uses the dsh adapter)'))
      }
    } catch (error) {
      log('role discovery failed: ' + (error?.message || error))
    }
  }

  async function ensureAgent(target) {
    const existing = ctx.agents.get(target.sessionId)
    if (existing) return existing
    const options = { provider: config.provider, model: config.model }
    if (typeof ctx.agents.resume === 'function') {
      try {
        const resumed = await ctx.agents.resume({ resumeSessionId: target.sessionId, agentOptions: options })
        if (resumed?.agent) return resumed.agent
      } catch (error) {
        // Keeping the reason matters: a swallowed resume failure turns into a
        // confusing "session already exists" on the create path.
        log('resume ' + target.sessionId + ' failed: ' + (error?.message || error))
      }
    }
    const created = await ctx.agents.create({
      sessionId: target.sessionId,
      agentOptions: options,
      ...(target.cwd ? { meta: { cwd: target.cwd } } : {}),
    })
    return created.agent
  }

  /** Last assistant text committed on a session, from its event log. */
  function lastAssistantText(sessionId) {
    const session = ctx.sessions?.get?.(sessionId)
    const events = session?.log
    if (!Array.isArray(events)) return undefined
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index]
      if (event?.type !== 'assistant/message') continue
      const parts = event.data?.message?.content || []
      const text = parts.filter((part) => part.type === 'text').map((part) => part.text).join('\n')
      if (text.trim()) return text.trim()
    }
    return undefined
  }

  /** Bind the role to the hub as a desktop transport, so send() targets this session. */
  async function bindRole(target) {
    try {
      await hubCall(config, 'desktop_bind', {
        agent: config.project + '.' + target.role,
        project: config.project,
        session_id: target.sessionId,
        transport: 'dsh_plugin',
      })
      log('bound ' + target.role + ' as dsh_plugin (' + target.sessionId + ')')
    } catch (error) {
      const message = String(error?.message || error)
      // Routing already points at this session; only a rebind was refused because
      // queued work would be redirected. Keep serving so the queue drains.
      if (message.includes('target_has_pending_tasks')) {
        log('bound ' + target.role + ' (already routed; draining queued tasks)')
        return
      }
      log('bind ' + target.role + ' failed: ' + message)
      target.bound = false
    }
  }

  /** Take one desktop job for this role and answer it inside its session. */
  async function serveDesktop(target) {
    if (target.serving || !target.bound) return
    target.serving = true
    try {
      const job = await hubCall(config, 'desktop_receive', {
        agent: config.project + '.' + target.role,
        project: config.project,
        session_id: target.sessionId,
        seconds: 10,
      })
      if (!job) return
      const agent = await ensureAgent(target)
      agent.followup(userMessage(job.prompt, job.task_id))
      await agent.whenIdle()
      const answer = lastAssistantText(target.sessionId)
      await hubCall(config, 'desktop_finish', {
        task_id: job.task_id,
        nonce: job.nonce,
        state: answer ? 'succeeded' : 'failed',
        result: { summary: answer || 'DSH 会话没有产生助手消息' },
      })
      log(target.role + ' delivered desktop task ' + job.task_id + (answer ? ' (answered)' : ' (no message)'))
    } catch (error) {
      log(target.role + ' desktop receive failed: ' + (error?.message || error))
    } finally {
      target.serving = false
    }
  }

  async function markRead(target, messageId) {
    await cli(config, ['bus', 'mark-read', '--role', target.role, '--session', target.sessionId, messageId])
      .catch((error) => log('mark-read failed: ' + (error?.message || error)))
  }

  async function handle(target, message) {
    // Resolve the agent before claiming: a setup failure must not strand a lease
    // that blocks every other session for the next 30 minutes.
    const agent = await ensureAgent(target)
    const claim = await cli(config, ['bus', 'claim', message.task_id, '--role', target.role, '--session', target.sessionId])
    try {
      const body = (message.data?.body || '').trim()
      const acceptance = (message.data?.acceptance || []).map((item) => '- ' + item).join('\n')
      const prompt = '# ' + (message.data?.title || 'task') + '\n\n' + body +
        (acceptance ? '\n\n验收条件：\n' + acceptance : '')
      agent.followup(userMessage(prompt, message.message_id))
      await agent.whenIdle()
      const answer = lastAssistantText(target.sessionId)
      if (!answer) {
        // A task with no assistant text is not a success: reporting a placeholder
        // as a resolved result hides the failure from whoever asked.
        throw new Error('DSH 会话没有产生助手消息（可能未配置模型或被中断）')
      }
      await cli(config, ['bus', 'result', message.task_id, '--role', target.role, '--session', target.sessionId,
        '--token', claim.claim_token, '--body', answer.slice(0, 800), '--evidence', 'dsh session ' + target.sessionId])
      log(target.role + ' answered ' + message.task_id + ' (' + answer.length + ' chars)')
    } catch (error) {
      const reason = String(error?.message || error).slice(0, 400)
      log(target.role + ' task ' + message.task_id + ' failed: ' + reason)
      await cli(config, ['bus', 'blocked', message.task_id, '--role', target.role, '--session', target.sessionId,
        '--token', claim.claim_token, '--body', 'DSH 执行失败：' + reason])
        .catch((inner) => log('blocked report failed: ' + (inner?.message || inner)))
    }
  }

  async function pollRole(target) {
    if (target.busy) return
    target.busy = true
    try {
      const inbox = await cli(config, ['bus', 'inbox', '--role', target.role, '--session', target.sessionId])
      for (const message of inbox.messages || []) {
        // Skip anything that is not new work: results, verify requests, and tasks
        // another session already finished must not be claimed again.
        if (message.type !== 'request' || message.task_status !== 'pending') {
          await markRead(target, message.message_id)
          continue
        }
        await handle(target, message)
        await markRead(target, message.message_id)
      }
    } catch (error) {
      log(target.role + ' poll failed: ' + (error?.message || error))
    } finally {
      target.busy = false
    }
  }

  async function poll() {
    await refresh(false)
    for (const target of roles) await pollRole(target)
  }

  ctx.effect(() => {
    const timer = setInterval(poll, config.pollMs)
    return () => clearInterval(timer)
  })

  // Desktop jobs arrive one at a time per role; the receive call long-polls,
  // so a one second tick only starts a wait that is usually still pending.
  ctx.effect(() => {
    const timer = setInterval(() => {
      for (const target of roles) serveDesktop(target)
    }, 1000)
    return () => clearInterval(timer)
  })

  refresh(true)
}


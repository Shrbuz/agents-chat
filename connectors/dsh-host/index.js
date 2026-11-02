/**
 * agents-chat-dsh — a DeepSeek Harness host plugin that turns Agents Chat
 * tasks into real DSH sessions.
 *
 * One plugin instance serves any number of roles: each configured role gets its
 * own persistent session, so `leader`, `tester` and friends can all live in the
 * same host under a single `insert` entry.
 *
 * A task becomes an ordinary user turn on that role's session, so the
 * conversation appears in the desktop app like any other chat. When the session
 * goes idle the last `assistant/message` is reported back through the bus
 * protocol (claim -> work -> result).
 *
 * @module agents-chat-dsh
 */

import { execFile } from 'node:child_process'

export const name = 'agents-chat-dsh'

/** Required services: the agent registry and the session log. */
export const inject = ['agents', 'sessions']

const DEFAULTS = {
  cli: '',
  project: '',
  pollMs: 2000,
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

/** Accept both a single role and a roles list; a role owns one session. */
function normalizeRoles(config) {
  const list = Array.isArray(config.roles) && config.roles.length
    ? config.roles
    : config.role
      ? [{ role: config.role, sessionId: config.sessionId }]
      : []
  return list
    .filter((item) => item && item.role)
    .map((item) => ({
      role: String(item.role),
      sessionId: String(item.sessionId || 'agents-chat-' + item.role),
      cwd: item.cwd || config.cwd || '',
      busy: false,
    }))
}

export function apply(ctx, rawConfig) {
  const config = { ...DEFAULTS, ...(rawConfig || {}) }
  const roles = normalizeRoles(config)

  if (!config.cli || !config.project || roles.length === 0) {
    log('disabled: cli, project and at least one role are required')
    return
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
      const answer = lastAssistantText(target.sessionId) || '(没有产生助手消息)'
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
    for (const target of roles) await pollRole(target)
  }

  ctx.effect(() => {
    const timer = setInterval(poll, config.pollMs)
    return () => clearInterval(timer)
  })

  log('loaded: project=' + config.project + ' roles=' + roles.map((r) => r.role + '@' + r.sessionId).join(', '))
}

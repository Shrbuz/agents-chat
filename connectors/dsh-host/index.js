/**
 * agents-chat-dsh — a DeepSeek Harness host plugin that turns Agents Chat
 * tasks into real DSH sessions.
 *
 * A task becomes an ordinary user turn on one persistent session, so the
 * conversation appears in the desktop app like any other chat. When the
 * session goes idle the last `assistant/message` is reported back through the
 * bus protocol (claim -> work -> result).
 *
 * Transport is the Agents Chat CLI, not a private HTTP API: the plugin claims
 * the task first, so a lease token guards the report, exactly like any other
 * bus client.
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
  role: 'dsh',
  sessionId: 'agents-chat-dsh',
  pollMs: 2000,
  provider: undefined,
  model: undefined,
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

/** Build the user message the agent loop accepts. */
function userMessage(text, id) {
  return { id, role: 'user', content: [{ type: 'text', text }], source: 'agents-chat' }
}

export function apply(ctx, rawConfig) {
  const config = { ...DEFAULTS, ...(rawConfig || {}) }
  let busy = false

  if (!config.cli || !config.project) {
    log('disabled: set both cli and project in the plugin config')
    return
  }

  async function ensureAgent() {
    const existing = ctx.agents.get(config.sessionId)
    if (existing) return existing
    const options = { provider: config.provider, model: config.model }
    const resumed = await ctx.agents
      .resume({ resumeSessionId: config.sessionId, agentOptions: options })
      .catch(() => undefined)
    if (resumed) return resumed.agent
    const created = await ctx.agents.create({ sessionId: config.sessionId, agentOptions: options })
    return created.agent
  }

  /** Last assistant text committed on this session, from the event log. */
  function lastAssistantText() {
    const session = ctx.sessions?.get?.(config.sessionId)
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

  async function handle(message) {
    const claim = await cli(config, ['bus', 'claim', message.task_id, '--role', config.role, '--session', config.sessionId])
    const agent = await ensureAgent()
    const body = (message.data?.body || '').trim()
    const acceptance = (message.data?.acceptance || []).map((item) => '- ' + item).join('\n')
    const prompt = '# ' + (message.data?.title || 'task') + '\n\n' + body +
      (acceptance ? '\n\n验收条件：\n' + acceptance : '')
    agent.followup(userMessage(prompt, message.message_id))
    await agent.whenIdle()
    const answer = lastAssistantText() || '(没有产生助手消息)'
    await cli(config, ['bus', 'result', message.task_id, '--role', config.role, '--session', config.sessionId,
      '--token', claim.claim_token, '--body', answer.slice(0, 800), '--evidence', 'dsh session ' + config.sessionId])
    log('answered ' + message.task_id + ' (' + answer.length + ' chars)')
  }

  async function poll() {
    if (busy) return
    busy = true
    try {
      const inbox = await cli(config, ['bus', 'inbox', '--role', config.role, '--session', config.sessionId])
      for (const message of inbox.messages || []) {
        if (message.type !== 'request') continue
        await handle(message)
      }
    } catch (error) {
      log('poll failed: ' + (error?.message || error))
    } finally {
      busy = false
    }
  }

  ctx.effect(() => {
    const timer = setInterval(poll, config.pollMs)
    return () => clearInterval(timer)
  })

  log('loaded: project=' + config.project + ' role=' + config.role + ' session=' + config.sessionId)
}

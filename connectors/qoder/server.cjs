"use strict";
// Qoder Desktop native extension, targeting the API bundled with 0.4.11.
// No renderer injection, private login files, clipboard, or simulated keystrokes.
const fs = require("node:fs/promises");
const path = require("node:path");
const http = require("node:http");

async function broker(method, params) {
  const state = process.env.AGENT_BRIDGE_STATE || path.join(process.env.LOCALAPPDATA, "Programs", "AgentsChat", ".bridge");
  const endpoint = JSON.parse(await fs.readFile(path.join(state, "endpoint.json"), "utf8"));
  const url = new URL(endpoint.url);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || url.pathname !== "/" || url.username || url.search || url.hash) throw new Error("Invalid local broker address");
  const body = JSON.stringify({method, params});
  return new Promise((resolve, reject) => {
    const request = http.request(new URL("/call", url), {method: "POST", headers: {
      "Content-Type": "application/json", Authorization: "Bearer " + endpoint.token,
      "Content-Length": Buffer.byteLength(body)
    }}, response => {
      let text = "";
      response.on("data", chunk => { text += chunk; if (text.length > 1024 * 1024) request.destroy(new Error("Broker response too large")); });
      response.on("end", () => { try {
        const value = JSON.parse(text);
        if (response.statusCode !== 200) throw new Error(value.error || "Local broker failed");
        resolve(value.result);
      } catch(error) { reject(error); } });
      response.on("error", reject);
    });
    request.setTimeout(55000, () => request.destroy(new Error("Local broker timed out")));
    request.on("error", reject);
    request.end(body);
  });
}

const content = value => ({content: [{type: "text", text: JSON.stringify(value)}]});
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function createRunner(api, callBroker = broker) {
  const running = new Map();
  async function finish(job, state, result) {
    for (let attempt = 0; attempt < 3; attempt++) {
      try { return await callBroker("desktop_finish", {task_id: job.task_id, nonce: job.nonce, state, result}); }
      catch (error) { if (attempt === 2) throw error; await delay(500); }
    }
  }
  async function run(job) {
    let subscription;
    let receipt;
    let resolveCompletion;
    const completion = new Promise(resolve => { resolveCompletion = resolve; });
    const record = {cancel: async () => {
      record.cancelled = true;
      if (receipt) await api.interruptTurn({sessionId: receipt.sessionId, turnId: receipt.turnId});
      resolveCompletion({state: "interrupted", result: {summary: "Interruption requested; check desktop for any completed file changes.", error: "desktop_interrupted"}});
    }};
    running.set(job.task_id, record);
    try {
      const state = await api.getState({sessionId: job.session_id});
      if (["queued", "running"].includes(state.phase)) throw new Error("Target desktop chat is busy; task was not sent");
      receipt = await api.send({sessionId: job.session_id, prompt: job.prompt, requestId: "agents-chat-" + job.task_id});
      if (record.cancelled) await record.cancel();
      subscription = await api.getState({sessionId: receipt.sessionId, turnId: receipt.turnId, include: ["output", "interactions"]}, event => {
        if (event.type === "closed") {
          if (event.reason !== "completed") resolveCompletion({state:"interrupted", result:{summary:"Desktop observation disconnected; execution may continue.", error:"desktop_observation_lost"}});
          return;
        }
        const snapshot = event.snapshot;
        if (!snapshot || snapshot.sessionId !== receipt.sessionId || snapshot.turnId !== receipt.turnId) return;
        const output = snapshot.output?.availability === "available" ? snapshot.output.value : [];
        if (["completed", "failed", "interrupted"].includes(snapshot.phase)) {
          const summary = output.map(item => item.text || "").join("\n").slice(-800);
          resolveCompletion({state: snapshot.phase === "completed" && summary ? "succeeded" : "failed", result: {
            summary: summary || "Desktop finished without an available final answer.",
            session_id: receipt.sessionId, turn_id: receipt.turnId,
            evidence: "Qoder desktop chat.getState turn completion",
            ...(snapshot.phase === "completed" && summary ? {} : {error:"desktop_turn_incomplete"})
          }});
        }
        // Questions and approvals stay visible in Qoder. This connector never auto-answers them.
      });
      const result = await completion;
      await finish(job, result.state, result.result);
    } catch (error) {
      await finish(job, "failed", {summary: String(error.message || error).slice(0,800), error:"qoder_desktop_transport", delivery_uncertain: !!receipt});
    } finally {
      await subscription?.dispose();
      running.delete(job.task_id);
    }
  }
  return {run, async cancel(job) { await running.get(job.task_id)?.cancel(); }, async close() {
    for (const record of running.values()) await record.cancel();
  }};
}

async function activate(context) {
  let closed = false;
  let sessionId;
  const storage = path.join(context.storagePath, "agents-chat-session.json");
  const runner = createRunner(context.api.chat);
  let loop;
  const startLoop = () => {
    if (loop) return;
    loop = (async () => {
      while (!closed && sessionId) {
        try {
          const job = await broker("desktop_receive", {agent:"qoder", session_id:sessionId, seconds:40});
          if (!job) continue;
          if (job.action === "cancel") await runner.cancel(job);
          else void runner.run(job).catch(error => console.error("Agents Chat task:", error.message));
        } catch(error) { if (!closed) await delay(3000); }
      }
    })().finally(() => { loop = undefined; });
  };
  const registration = await context.api.mcp.registerInProcessServer({name:"agents_chat_desktop", version:"0.2.0", tools:[{
    name:"bridge_connect_current_chat",
    description:"Bind this existing local Qoder desktop chat as the qoder receiver for Agents Chat. Use only when the user asks to connect this chat. Does not send a task or create a chat.",
    inputSchema:{type:"object", properties:{}, additionalProperties:false},
    handler: async (_input, source) => {
      if (!source?.sessionId || source.executionLocation === "remote") throw new Error("A local desktop chat is required");
      const result = await broker("desktop_bind", {agent:"qoder", session_id:source.sessionId, transport:"qoder_extension"});
      await fs.mkdir(context.storagePath, {recursive:true});
      await fs.writeFile(storage, JSON.stringify({sessionId:source.sessionId}));
      sessionId = source.sessionId;
      startLoop();
      return content(result);
    }
  }, {
    name:"bridge_send",
    description:"Send a concrete task to Codex through the local Agents Chat broker. A bound desktop target runs in its existing chat. Use a stable key for retries.",
    inputSchema:{type:"object", properties:{prompt:{type:"string", maxLength:6000}, key:{type:"string"}, timeout:{type:"integer", minimum:1,maximum:3600}, reply_mode:{enum:["mailbox","run"]}}, required:["prompt","key"], additionalProperties:false},
    handler: async (input, source) => {
      if (!sessionId || source?.sessionId !== sessionId) throw new Error("Connect this desktop chat first");
      return content(await broker("send", {sender:"qoder", target:"codex", ...input}));
    }
  }, {
    name:"bridge_wait", description:"Wait once for a task result in local code; no message history is loaded.",
    inputSchema:{type:"object", properties:{task_id:{type:"string"}, seconds:{type:"number",minimum:0,maximum:45}}, required:["task_id"],additionalProperties:false},
    handler: async (input, source) => {
      if (!sessionId || source?.sessionId !== sessionId) throw new Error("Connect this desktop chat first");
      return content(await broker("wait", {task_id:input.task_id,seconds:input.seconds ?? 45}));
    }
  }]});
  context.subscriptions.push(registration, {dispose:async () => {closed=true; await runner.close();}});
  try {
    const saved = JSON.parse(await fs.readFile(storage,"utf8"));
    const targets = await broker("targets",{});
    if (targets.some(t=>t.name === "qoder" && t.mode === "desktop_session" && t.session_id === saved.sessionId)) {
      sessionId = saved.sessionId; startLoop();
    }
  } catch { /* A new installation waits for explicit chat binding. */ }
}

module.exports = {activate, createRunner};

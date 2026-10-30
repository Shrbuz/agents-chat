const assert = require('node:assert/strict');
const {createRunner} = require('../connectors/qoder/server.cjs');

(async () => {
  const results = [];
  const sent = [];
  const api = {
    getState: async (input, callback) => {
      if (!callback) return {phase:'completed'};
      // A reply in a different turn must not complete this task.
      callback({type:'snapshot',snapshot:{sessionId:'bound-chat',turnId:'other-turn',phase:'completed',output:{availability:'available',value:[{text:'wrong'}]}}});
      callback({type:'snapshot',snapshot:{sessionId:'bound-chat',turnId:'actual-turn',phase:'completed',output:{availability:'available',value:[{text:'actual answer'}]}}});
      return {dispose:async()=>{}};
    },
    send: async input => {sent.push(input); return {sessionId:'bound-chat',turnId:'actual-turn'};}
  };
  const runner = createRunner(api, async (method, params) => {results.push({method,params});});
  await runner.run({task_id:'task-1',nonce:'receipt',session_id:'bound-chat',prompt:'real task'});
  assert.equal(sent.length,1);
  assert.equal(sent[0].sessionId,'bound-chat');
  assert.equal(sent[0].requestId,'agents-chat-task-1');
  assert.equal(results[0].params.result.summary,'actual answer');
  assert.equal(results[0].params.state,'succeeded');
  let sendCount = 0;
  const busy = createRunner({getState: async()=>({phase:'running'}),send:async()=>{sendCount++;}},async(method,params)=>{results.push({method,params});});
  await busy.run({task_id:'task-2',nonce:'receipt',session_id:'bound-chat',prompt:'task'});
  assert.equal(sendCount,0);
  assert.equal(results.at(-1).params.state,'failed');
  console.log('Qoder desktop connector contract tests passed (mock host; no model call).');
})().catch(error=>{console.error(error);process.exitCode=1;});

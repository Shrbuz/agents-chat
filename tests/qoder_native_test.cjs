const assert=require('node:assert/strict');
const {createRunner}=require('../connectors/qoder-native/dist/node/server.cjs');
const job={task_id:'task-1',nonce:'receipt-1',session_id:'existing-chat',prompt:'Read-only native verification'};
(async()=>{
  const calls=[],results=[];
  const api={
    send(){throw Error('Privileged API must not be used');},
    getState(){throw Error('Privileged state API must not be used');},
    async continueSession(input){calls.push(['continue',input]);return {sessionId:'existing-chat',turnId:'new-turn'};},
    async observeTurn(input,listener){calls.push(['observe',input]);listener({sessionId:'other-chat',turnId:'new-turn',terminalOutcome:'completed'});listener({sessionId:'existing-chat',turnId:'new-turn',terminalOutcome:'completed'});return {dispose:async()=>calls.push(['disposed'])};}
  };
  const runner=createRunner(api,{call:async(method,params)=>results.push({method,params}),readReport:async()=> 'Native result received'});
  await runner.run(job);
  assert.equal(calls[0][1].sessionId,'existing-chat');
  assert.equal(calls[0][1].requestId,'agents-chat-task-1');
  assert.equal(calls[1][1].includeContent,undefined);
  assert.equal(results[0].params.state,'succeeded');
  assert.equal(results[0].params.result.summary,'Native result received');
  assert.equal(results[0].params.result.turn_id,'new-turn');
  const failures=[];
  const denied=createRunner({continueSession:async()=>{throw Error('APP_PLUGIN_PERMISSION_DENIED');}}, {call:async(m,p)=>failures.push(p)});
  await denied.run(job);
  assert.equal(failures[0].state,'failed');
  assert.equal(failures[0].result.delivery_uncertain,false);
  const wrong=createRunner({continueSession:async()=>({sessionId:'unrelated-chat',turnId:'x'})}, {call:async(m,p)=>failures.push(p)});
  await wrong.run(job);
  assert.equal(failures.at(-1).state,'failed');
  assert.equal(failures.at(-1).result.delivery_uncertain,true);
  console.log('Qoder native contracts passed: existing session, metadata only, report + terminal, fail closed on denial/mismatched session. Mock host; not a real desktop verification.');
})().catch(error=>{console.error(error);process.exitCode=1;});

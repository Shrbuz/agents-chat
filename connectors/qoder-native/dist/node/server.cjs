"use strict";
// Qoder Desktop native extension. Uses ordinary plugin APIs only.
// No chat.send/getState, impersonated built-in ID, renderer, UI or authentication reads.
const fs=require("node:fs/promises");
const path=require("node:path");
const http=require("node:http");
const crypto=require("node:crypto");
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));

function localClient(state) {
  return async function call(method,params={}) {
    const endpoint=JSON.parse(await fs.readFile(path.join(state,"endpoint.json"),"utf8"));
    const url=new URL(endpoint.url);
    if(url.protocol!=="http:"||url.hostname!=="127.0.0.1"||url.pathname!=="/"||url.username||url.search||url.hash)throw Error("Invalid local broker address");
    const body=JSON.stringify({method,params});
    return new Promise((resolve,reject)=>{
      const request=http.request(new URL("/call",url),{method:"POST",headers:{"Content-Type":"application/json",Authorization:"Bearer "+endpoint.token,"Content-Length":Buffer.byteLength(body)}},response=>{
        let text="";
        response.on("data",chunk=>{text+=chunk;if(text.length>1024*1024)request.destroy(Error("Broker response too large"));});
        response.on("end",()=>{try{const data=JSON.parse(text);if(response.statusCode!==200)throw Error(data.error||"Broker call failed");resolve(data.result);}catch(error){reject(error);}});
        response.on("error",reject);
      });
      request.setTimeout(55000,()=>request.destroy(Error("Broker timed out")));
      request.on("error",reject);request.end(body);
    });
  };
}

function resultPrompt(job) {
  return job.prompt+"\nNATIVE RESULT REPORT: This transport cannot read your chat text. When this task is done, report a short result once using the local terminal:\n"+
    '& "$env:LOCALAPPDATA\\Programs\\AgentsChat\\AgentsChat.Cli.exe" native-result '+job.task_id+' --receipt '+job.nonce+' --text "<your actual result, under 800 characters>"\n'+
    "Replace the placeholder with your actual result. This writes only a task receipt in the tool's state directory; it does not create another task or model call. Then provide your normal final answer. If blocked, report the blocker rather than claiming completion. Do not send a separate reply task.";
}

function createRunner(api,{state,call,readReport}={}) {
  const running=new Map();
  readReport??=async(job,signal)=>{
    const filename=path.join(state,"native-results",job.task_id+".json");
    while(!signal.aborted) {
      try {
        const report=JSON.parse(await fs.readFile(filename,"utf8"));
        if(report.task_id!==job.task_id||report.receipt!==job.nonce||typeof report.summary!=="string"||!report.summary.trim()||report.summary.length>800)throw Error("Native receipt mismatch");
        return report.summary;
      }catch(error){if(error.code!=="ENOENT")throw error;}
      await delay(250); // Plain local code, no model polling.
    }
    throw Error("Native task stopped");
  };
  async function run(job) {
    let subscription,receipt;
    const controller=new AbortController();
    let cancelResolve;
    const cancellation=new Promise(resolve=>{cancelResolve=resolve;});
    const record={cancel:async()=>{
      controller.abort();
      if(receipt)await api.interruptTurn({sessionId:receipt.sessionId,turnId:receipt.turnId});
      cancelResolve({state:"interrupted",summary:"Native turn interruption requested."});
    }};
    running.set(job.task_id,record);
    try {
      receipt=await api.continueSession({sessionId:job.session_id,prompt:resultPrompt(job),requestId:"agents-chat-"+job.task_id});
      if(receipt.sessionId!==job.session_id||!receipt.turnId)throw Error("Native session receipt mismatch");
      if(controller.signal.aborted)await api.interruptTurn({sessionId:receipt.sessionId,turnId:receipt.turnId});
      let resolveTerminal;
      const terminal=new Promise(resolve=>{resolveTerminal=resolve;});
      subscription=await api.observeTurn({sessionId:receipt.sessionId,turnId:receipt.turnId},snapshot=>{
        if(snapshot.sessionId===receipt.sessionId&&snapshot.turnId===receipt.turnId&&snapshot.terminalOutcome)resolveTerminal(snapshot.terminalOutcome);
      });
      const outcome=await Promise.race([
        (async()=>{
          const terminalOutcome=await terminal;
          if(terminalOutcome!=="completed")return {state:"failed",summary:"Qoder native turn ended: "+terminalOutcome};
          return {state:"succeeded",summary:await readReport(job,controller.signal)};
        })(),cancellation
      ]);
      await call("desktop_finish",{task_id:job.task_id,nonce:job.nonce,state:outcome.state,result:{summary:outcome.summary,session_id:receipt.sessionId,turn_id:receipt.turnId,evidence:"Qoder native continueSession + terminal event + task-scoped report"}});
    }catch(error){
      try{await call("desktop_finish",{task_id:job.task_id,nonce:job.nonce,state:"failed",result:{summary:String(error.message||error).slice(0,800),error:"qoder_native",delivery_uncertain:!!receipt}});}catch{}
    }finally{controller.abort();await subscription?.dispose();running.delete(job.task_id);}
  }
  return {run,async cancel(job){await running.get(job.task_id)?.cancel();},async close(){for(const record of running.values())await record.cancel();}};
}

async function activate(context) {
  const options=JSON.parse(await fs.readFile(path.resolve(__dirname,"../../bridge.local.json"),"utf8"));
  const state=options.state;
  const agent=options.agent||"qoder";
  const call=localClient(state);
  const runner=createRunner(context.api.chat,{state,call});
  let closed=false;
  const status=path.join(state,"qoder-native-status.json");
  const publish=async()=>{
    const value={plugin_id:"agents-chat.native",version:"0.3.0",pid:process.pid,updated_at:Date.now(),ready:!closed,transport:"qoder_native",ui_automation:false};
    const temporary=status+"."+crypto.randomUUID()+".tmp";
    await fs.writeFile(temporary,JSON.stringify(value));await fs.rename(temporary,status);
  };
  await publish();
  const timer=setInterval(()=>void publish().catch(()=>{}),5000);
  timer.unref?.();
  const loop=(async()=>{
    while(!closed) {
      try {
        const targets=await call("targets");
        const target=targets.find(t=>t.name===agent&&t.desktop_transport==="qoder_native");
        if(!target){await delay(2000);continue;}
        const job=await call("desktop_receive",{agent,session_id:target.session_id,seconds:40});
        if(!job)continue;
        if(job.action==="cancel")await runner.cancel(job);
        else void runner.run(job).catch(error=>console.error("Agents Chat native:",error.message));
      }catch(error){if(!closed)await delay(2000);}
    }
  })();
  context.subscriptions.push({dispose:async()=>{closed=true;clearInterval(timer);await runner.close();await publish();}});
  void loop.catch(error=>console.error("Agents Chat native loop:",error.message));
}
module.exports={activate,createRunner,resultPrompt};

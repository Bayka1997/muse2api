// Offline check: node tests/test_batch_retry_once.cjs
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const script=fs.readFileSync(require('node:path').join(__dirname,'..','admin.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
(async()=>{
 for(const finish of ['completed','failed']){
  let posts=0;const states=[];
  const context=vm.createContext({Set,$:()=>({checked:true}),
   renderBatchJob:(b,j)=>states.push(j.status),persistBatchHistory(){},
   rememberBatchAccount(){},loadTasks(){},loadMedia(){},safeBatchMediaUrl:u=>u||'',
   api:async()=>{posts++;return posts===1?{id:'first',status:'failed',error:'Failed'}:{id:'retry',status:finish,result:{url:'/v1/media/new.mp4'},error:'Retry failed'};}
  });
  for(const name of ['resetBatchJobForResume','retryFailedBatchJob','processBatchJob']){
   const match=script.match(new RegExp('(?:async )?function '+name+'\\('));
   const rest=script.slice(match.index),end=rest.search(/\n(?:async )?function /);
   vm.runInContext(rest.slice(0,end),context);
  }
  const runner={token:'run',stop:false,abandoned:false};
  const job={index:1,prompt:'10s video',runToken:'run',narration:'Keep narration'};
  const batch={kind:'video',id:'batch'};
  await context.processBatchJob(batch,job,runner);
  assert.equal(posts,2);
  assert.equal(job.status,finish);
  assert.equal(job.narration,'Keep narration');
  assert.ok(states.includes('submitting'));
  assert.equal(await context.retryFailedBatchJob(batch,job,runner),false);
  assert.equal(posts,2);
  if(finish==='failed')assert.equal(job.error,'Retry failed');
 }
 console.log('Failed generation retries once, enters submitting, preserves text, and never loops.');
})().catch(e=>{console.error(e);process.exitCode=1;});

// Offline check: node tests/test_batch_failure.cjs
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const script=fs.readFileSync(require('node:path').join(__dirname,'..','admin.html'),'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const start=script.indexOf('async function processBatchJob(');
const rest=script.slice(start);
const source=rest.slice(0,rest.search(/\n(?:async )?function /));
(async()=>{
 for(const mode of ['reference','rejected','failed','missing-url']){
  let submissions=0;
  const runner={token:'test',stop:false};
  const job={index:0,prompt:'10-second video',runToken:'test',imageFirst:mode==='reference',phase:'video',imageReferenceKey:'missing'};
  const context=vm.createContext({
   retryFailedBatchJob:async()=>false,
   batchGeneratedImageData:async()=>{throw new Error('Reference missing');},
   renderBatchJob(){},rememberBatchAccount(){},loadTasks(){},loadMedia(){},
   safeBatchMediaUrl:url=>url||'',
   rerunBatchJobs(){throw new Error('Unexpected automatic regeneration');},
   api:async()=>{
    submissions++;
    if(mode==='rejected')throw Object.assign(new Error('Account rejected'),{status:403});
    return mode==='failed'?{id:'task',status:'failed',error:'Original upstream failure'}:{id:'task',status:'completed'};
   },
  });
  vm.runInContext(source,context);
  await context.processBatchJob({kind:'video',id:'batch'},job,runner);
  assert.equal(job.status,'failed',mode);
  assert.equal(submissions,mode==='reference'?0:1,mode);
  if(mode==='failed')assert.equal(job.error,'Original upstream failure');
 }
 console.log('Failures preserve their error without immediately requeuing generation.');
})().catch(error=>{console.error(error);process.exitCode=1;});

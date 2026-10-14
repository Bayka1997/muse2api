// Run: node tests/test_batch_ui.cjs. No Muse accounts, network, or generation credits.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'admin.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace(/\r?\nrefreshAll\(\);\r?\nsetInterval/, '\nsetInterval');

let sharedFolderDb = null;
function memoryFolderDb(){
  const data = new Map();
  let upgraded = false;
  return {open(){
    const request = {};
    const db = {
      objectStoreNames:{contains(){return upgraded;}},
      createObjectStore(){upgraded = true;},
      transaction(){
        const store = {
          put(value, key){data.set(key, value);},
          delete(key){data.delete(key);},
          get(key){
            const getRequest = {};
            setImmediate(() => {
              getRequest.result = data.has(key) ? data.get(key) : undefined;
              if (typeof getRequest.onsuccess === 'function') getRequest.onsuccess();
            });
            return getRequest;
          }
        };
        return {objectStore(){return store;}, set oncomplete(fn){setImmediate(fn);}, set onerror(fn){}};
      },
      close(){}
    };
    setImmediate(() => {
      request.result = db;
      if (!upgraded && typeof request.onupgradeneeded === 'function') request.onupgradeneeded();
      if (typeof request.onsuccess === 'function') request.onsuccess();
    });
    return request;
  }};
}
function setup(request, storage = new Map()) {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {id, value:'', innerHTML:'', textContent:'', disabled:false,
      click(){}, showModal(){this.open=true;},close(){this.open=false;},removeAttribute(name){delete this[name];}, style:{}, options:[], classList:{toggle(){},add(){},remove(){},contains(){return false;}}, appendChild(){},remove(){}});
    return nodes.get(id);
  }
  const context = {
    URL, URLSearchParams, console, AbortController,
    window:{isSecureContext:true},
    location:{origin:'http://localhost:18610',search:''},
    localStorage:{getItem(key){return storage.get(key) || null;},setItem(key,value){storage.set(key,value);}},
    document:{body:node('body'),getElementById:node,querySelectorAll(){return [];},createElement(){return node('toast');}},
    setInterval(){},clearTimeout, setTimeout(callback, delay){return delay === 120000 ? setTimeout(callback, delay) : setImmediate(callback);},
    indexedDB: sharedFolderDb,
    fetch:async (url, opts={}) => {
      const response = await request(url, opts, context);
      return {ok:response.status >= 200 && response.status < 300,status:response.status,
        text:async()=>JSON.stringify(response.body), body:response.stream || null, blob:async()=>response.blob || 'mock-media-bytes',
        headers:{get(name){return response.headers ? response.headers[String(name).toLowerCase()] || null : null;}}};
    },
  };
  vm.createContext(context); vm.runInContext(script, context);
  vm.runInContext('loadTasks = async () => {}; loadMedia = async () => {};', context);
  node('batchKind').value='image';node('batchSize').value='16:9';
  node('batchDuration').value='5';node('batchConcurrency').value='2';
  return {context, node, storage, state:()=>vm.runInContext('activeBatch',context)};
}
const ok = body => ({status:200,body});
const complete = id => ({id,status:'completed',progress:100,data:[{url:'/v1/media/'+id+'.webp'}]});

async function main() {
  {
    let posts=0, collections=0;
    const auto=setup(async(url,opts)=>{
      if (url.endsWith('/collect')) {
        collections++;
        return {status:502,body:{detail:'Chưa có video'}};
      }
      if (opts.method==='POST') {
        posts++;
        return ok({id:'auto-'+posts,status:'failed',error:'Timeout'});
      }
      return ok({status:'failed',error:'Timeout'});
    });
    auto.node('batchAutoRetry').checked=true;
    auto.node('batchKind').value='video';
    auto.node('batchPrompts').value='Retry once';
    await auto.context.runBatch();
    assert.equal(posts,2);
    assert.equal(collections,1);
    assert.equal(auto.context.batchEntries()[0].job.status,'failed');
    assert.equal(auto.context.batchEntries()[0].job.attempt,1);

    let recoveredPosts=0;
    const refill=setup(async(url,opts)=>{
      if (url.endsWith('/collect')) return ok({status:'completed',result:{url:'/v1/media/old.mp4'}});
      if (opts.method==='POST') { recoveredPosts++; return ok({id:'fresh',status:'completed',result:{url:'/v1/media/new.mp4'}}); }
      return ok({status:'failed',error:'Timeout'});
    },new Map([['muse2api_batch_history_v1',JSON.stringify({next:2,batches:[{id:'old',kind:'video',jobs:[
      {index:0,id:'old-task',prompt:'Old failure',status:'failed',error:'Timeout'}
    ]}]})]]));
    await new Promise(resolve=>setImmediate(resolve));
    refill.node('batchAutoRetry').checked=true;
    refill.node('batchKind').value='video';
    refill.node('batchPrompts').value='Fresh prompt';
    await refill.context.runBatch();
    assert.equal(recoveredPosts,1);
    assert.equal(refill.context.batchEntries().filter(({job})=>job.status==='completed').length,2);
    let stoppedPosts=0, releaseCollect;
    const collectGate=new Promise(resolve=>{releaseCollect=resolve;});
    const stopped=setup(async(url,opts)=>{
      if (url.endsWith('/collect')) { await collectGate; return {status:502,body:{detail:'Chưa có video'}}; }
      stoppedPosts++;
      return ok({id:'stop-auto',status:'failed',error:'Timeout'});
    });
    stopped.node('batchAutoRetry').checked=true;
    stopped.node('batchKind').value='video';
    stopped.node('batchPrompts').value='Stop before resubmit';
    const stoppingRun=stopped.context.runBatch();
    await new Promise(resolve=>setImmediate(resolve));
    stopped.context.stopBatch();
    releaseCollect();
    await stoppingRun;
    assert.equal(stoppedPosts,1);
    assert.equal(stopped.context.batchEntries()[0].job.status,'failed');
  }
  {
  const recoveryCalls = [];
  const videoRecovery = setup(async (url, opts) => {
    if (opts.method !== 'POST') return ok({status:'failed',error:url.includes('pending-video') ? 'original timeout' : 'timeout'});
    assert.equal(opts.method, 'POST');
    assert(url.endsWith('/collect'));
    recoveryCalls.push(url);
    if (url.includes('ready-video')) return ok({status:'completed',result:{url:'/v1/media/recovered.mp4'}});
    return {status:502,body:{detail:'Still rendering'}};
  }, new Map([['muse2api_batch_history_v1',JSON.stringify({next:4,batches:[{id:'recover',kind:'video',jobs:[
    {index:0,id:'ready-video',prompt:'Ready',status:'failed',error:'timeout'},
    {index:1,id:'pending-video',prompt:'Pending',status:'failed',error:'original timeout'},
    {index:2,prompt:'Never submitted',status:'failed',error:'No task'}
  ]}]})]]));
  await new Promise(resolve=>setImmediate(resolve));
  videoRecovery.context.setBatchFilter('failed');
  assert(videoRecovery.node('batch-job-0').innerHTML.includes('Lấy lại video'));
  assert.equal(videoRecovery.node('batchRecoverVideos').disabled,false);
  await videoRecovery.context.recoverFailedVideos();
  assert.equal(recoveryCalls.length,2);
  const recoveredJobs = videoRecovery.context.batchEntries().map(({job})=>job);
  assert.equal(recoveredJobs[0].status,'completed');
  assert.equal(videoRecovery.node('batch-job-0').hidden,true);
  assert.equal(recoveredJobs[1].status,'failed');
  assert.equal(recoveredJobs[1].error,'original timeout');
  assert.equal(videoRecovery.node('batch-job-1').hidden,false);
  await videoRecovery.context.recoverFailedVideos(1);
  assert.equal(recoveryCalls.length,3);
  assert.equal(recoveredJobs[1].status,'failed');
  }
  const settingsStore = new Map();
  const settings = setup(async () => ok({}), settingsStore);
  settings.node('batchKind').value = 'video';
  settings.node('batchSize').value = '9:16';
  settings.node('batchDuration').value = '8';
  settings.node('batchConcurrency').value = '5';
  settings.context.saveBatchSettings();
  const reloadedSettings = setup(async () => ok({}), settingsStore);
  reloadedSettings.context.restoreBatchSettings();
  assert.equal(reloadedSettings.node('batchKind').value, 'video');
  assert.equal(reloadedSettings.node('batchSize').value, '9:16');
  assert.equal(reloadedSettings.node('batchDuration').value, '8');
  assert.equal(reloadedSettings.node('batchConcurrency').value, '5');
  assert.equal(reloadedSettings.node('batchDurationField').style.display, '');
  let active=0, peak=0, posts=[];
  const first = setup(async (url, opts) => {
    if (opts.method === 'POST') {
      const body=JSON.parse(opts.body);posts.push({body,headers:opts.headers});
      active++;peak=Math.max(peak,active);
      return ok({id:'job'+posts.length,status:'queued'});
    }
    active--;
    return ok(url.endsWith('/job2') ? {status:'failed',error:'Sample error'} : complete(url.split('/').pop()));
  });
  first.node('batchPrompts').value='  Cảnh một  \r\n\nCảnh hai\n<script>alert(1)</script>\nCảnh bốn';
  await first.context.runBatch();
  assert.equal(posts.length,4);assert.equal(peak,2);
  assert.deepEqual(posts.map(p=>p.body.prompt),['Cảnh một','Cảnh hai','<script>alert(1)</script>','Cảnh bốn']);
  assert.equal(new Set(posts.map(p=>p.headers['Idempotency-Key'])).size,4);
  assert.deepEqual(Array.from(first.state().jobs,j=>j.status),['completed','failed','completed','completed']);
  assert(first.node('batch-job-2').innerHTML.includes('&lt;script&gt;'));
  assert(first.node('batch-job-0').innerHTML.includes('http://localhost:18610/v1/media/job1.webp'));
  assert.equal(first.node('batchStart').disabled,false);

  let maxActive=0, currentActive=0, maxPosts=0;
  const maxBatch=setup(async(url,opts)=>{
    if(opts.method==='POST'){
      maxPosts++;currentActive++;maxActive=Math.max(maxActive,currentActive);
      return ok({id:'max'+maxPosts,status:'queued'});
    }
    currentActive--;return ok(complete(url.split('/').pop()));
  });
  maxBatch.node('batchConcurrency').value='999';
  maxBatch.node('batchPrompts').value=Array.from({length:12},(_,i)=>'Prompt '+i).join('\n');
  await maxBatch.context.runBatch();
  assert.equal(maxPosts,12);assert.equal(maxActive,10);assert.equal(maxBatch.state().concurrency,10);
  assert(maxBatch.state().jobs.every(job=>job.status==='completed'));

  let submits=0, queries=0;
  const retry=setup(async (url, opts)=>{
    if(opts.method==='POST') {submits++;return submits===1 ? {status:429,body:{detail:'Queue full'}} : ok({id:'retry',status:'queued'});}
    queries++;return queries===1 ? {status:503,body:{detail:'Temporary error'}} : ok(complete('retry'));
  });
  retry.node('batchPrompts').value='Thử lại';
  await retry.context.runBatch();
  assert.equal(submits,2);assert.equal(queries,2);assert.equal(retry.state().jobs[0].status,'completed');

  let stoppedPosts=0;
  const stopped=setup(async(url,opts,ctx)=>{
    if(opts.method==='POST'){stoppedPosts++;ctx.stopBatch();return ok({id:'stop',status:'queued'});}
    return ok(complete('stop'));
  });
  stopped.node('batchPrompts').value='Một\nHai\nBa';
  await stopped.context.runBatch();
  assert.equal(stoppedPosts,1);
  assert.deepEqual(Array.from(stopped.state().jobs,j=>j.status),['completed','stopped','stopped']);

  let ambiguousPosts=0;
  const ambiguous=setup(async(url,opts)=>{ambiguousPosts++;throw new Error('Network disconnected');});
  ambiguous.node('batchPrompts').value='Một\nHai';ambiguous.node('batchConcurrency').value='1';
  await ambiguous.context.runBatch();
  assert.equal(ambiguousPosts,1);
  assert.deepEqual(Array.from(ambiguous.state().jobs,j=>j.status),['unconfirmed','stopped']);

  let pollPosts=0, pollQueries=0;
  const uncertain=setup(async(url,opts)=>{
    if(opts.method==='POST'){pollPosts++;return ok({id:'unknown',status:'queued'});}
    pollQueries++;throw new Error('Status unavailable');
  });
  uncertain.node('batchPrompts').value='Một\nHai';uncertain.node('batchConcurrency').value='1';
  await uncertain.context.runBatch();
  assert.equal(pollPosts,1);assert.equal(pollQueries,6);
  assert.deepEqual(Array.from(uncertain.state().jobs,j=>j.status),['unconfirmed','stopped']);

  let rejectedPosts=0;
  const rejected=setup(async(url,opts)=>{
    if(opts.method==='POST'){
      rejectedPosts++;
      return rejectedPosts===1 ? {status:400,body:{detail:'Invalid prompt'}} : ok(complete('valid'));
    }
    throw new Error('Unexpected polling');
  });
  rejected.node('batchPrompts').value='Lỗi\nĐúng';rejected.node('batchConcurrency').value='1';
  await rejected.context.runBatch();
  assert.equal(rejectedPosts,2);
  assert.deepEqual(Array.from(rejected.state().jobs,j=>j.status),['failed','completed']);

  const empty=setup(async()=>{throw new Error('Empty input must not submit');});
  empty.node('batchPrompts').value='  \n\r\n';
  await empty.context.runBatch();
  assert.equal(empty.state(),null);

  let videoBody;
  const video=setup(async(url,opts)=>{
    if(opts.method==='POST'){assert.equal(url,'/v1/videos');videoBody=JSON.parse(opts.body);return ok({id:'video',status:'queued'});}
    assert.equal(url,'/v1/videos/video');return ok({status:'succeeded',result:{url:'/v1/media/video.mp4'}});
  });
  video.node('batchKind').value='video';video.node('batchDuration').value='8';video.node('batchPrompts').value='Một video';
  await video.context.runBatch();
  assert.equal(videoBody.duration,8);
  assert(!video.node('batch-job-0').innerHTML.includes('<video'));
  assert(video.node('batch-job-0').innerHTML.includes('href="http://localhost:18610/v1/media/video.mp4"'));
  assert.equal(video.context.safeBatchMediaUrl('javascript:alert(1)'), '');
  assert.equal(video.context.safeBatchMediaUrl('data:text/html,bad'), '');
  assert.equal(video.state().running,false);
  const files=[];
  const existingFiles = new Set();
  let saves=0, generationPosts=0, denyWrite=true;
  const downloads=setup(async(url,opts)=>{
    if(opts.method==='POST'){generationPosts++;return ok(complete('download'+generationPosts));}
    assert(url.startsWith('http://localhost:18610/v1/media/'));
    return {status:200,blob:'media-content'};
  });
  downloads.context.window.showDirectoryPicker=async options=>{
    assert.equal(options.mode,'readwrite');
    return {name:'My results',getFileHandle:async(name,options)=>{
      if (!options) {
        if (!existingFiles.has(name)) throw Object.assign(new Error('Missing'),{name:'NotFoundError'});
        return {};
      }
      assert.equal(options.create,true);files.push(name);existingFiles.add(name);
      return {createWritable:async()=>{
        if(denyWrite){denyWrite=false;throw new Error('Permission denied');}
        return {write:async bytes=>{assert.equal(bytes,'media-content');},close:async()=>{saves++;},abort:async()=>{}};
      }};
    }};
  };
  await downloads.context.chooseBatchFolder();
  assert(downloads.node('batchFolderLabel').textContent.includes('My results'));
  assert.equal(downloads.node('batchChooseFolder').textContent, 'My results');
  assert.equal(downloads.node('batchChooseFolder').title, 'My results');
  downloads.node('batchPrompts').value='Ảnh một\nẢnh hai';downloads.node('batchConcurrency').value='1';
  await downloads.context.runBatch();
  assert(downloads.state().jobs.every(j=>j.status==='completed'));
  assert.deepEqual(Array.from(downloads.state().jobs,j=>j.saveState),['failed','saved']);
  assert.equal(downloads.node('batchReset').disabled,true);
  assert.equal(saves,1);assert(downloads.node('batch-job-0').innerHTML.includes('Lưu lại'));
  await downloads.context.retryBatchSave(0);
  assert.equal(saves,2);assert.equal(generationPosts,2);
  assert(downloads.state().jobs.every(j=>j.saveState==='saved'));
  assert.notEqual(files[0],files[1]);assert.equal(files[0],files[2]);
  assert(files.every(name=>/^00[12]\.webp$/.test(name)));
  assert(downloads.node('batchSummary').textContent.includes('Đã tải/lưu 2'));
  assert.equal(downloads.node('batchReset').disabled,false);
  downloads.node('batchPrompts').value='Tiếp theo\n\nThêm nữa';
  downloads.context.updateBatchStartLabel();
  assert.equal(downloads.node('batchStart').textContent,'Chạy 2 prompt (STT 3–4)');
  await downloads.context.runBatch();
  assert.deepEqual(Array.from(downloads.state().jobs,j=>j.index),[2,3]);
  assert(files.includes('003.webp'));assert(files.includes('004.webp'));
  assert(downloads.node('batchResults').innerHTML.includes('batch-job-0'));
  assert(downloads.node('batchResults').innerHTML.includes('batch-job-3'));
  const restored=setup(async()=>{throw new Error('Restoring must not submit');},downloads.storage);
  restored.node('batchPrompts').value='Prompt sau tải lại';restored.context.updateBatchStartLabel();
  assert.equal(restored.node('batchStart').textContent,'Chạy 1 prompt (STT 5–5)');
  assert(restored.node('batchResults').innerHTML.includes('batch-job-3'));
  assert.equal(restored.node('batchReset').disabled,false);
  downloads.context.resetBatchNumbering();
  assert(downloads.node('batchResults').innerHTML.includes('Chưa có prompt'));
  assert.equal(downloads.node('batchPrompts').value,'');
  assert.equal(downloads.node('batchStart').textContent,'Chạy 0 prompt');
  downloads.node('batchPrompts').value='Sau reset';downloads.context.updateBatchStartLabel();
  assert.equal(downloads.node('batchStart').textContent,'Chạy 1 prompt (STT 1–1)');
  await downloads.context.runBatch();
  assert.equal(downloads.state().jobs[0].index,0);
  assert.equal(downloads.state().jobs[0].savedName,'001-2.webp');

  downloads.context.window.showDirectoryPicker=async()=>{throw Object.assign(new Error('Cancel'),{name:'AbortError'});};
  await downloads.context.chooseBatchFolder();
  assert(downloads.node('batchFolderLabel').textContent.includes('My results'));
  assert.equal(downloads.node('batchChooseFolder').textContent, 'My results');
  downloads.context.clearBatchFolder();assert.equal(downloads.node('batchClearFolder').disabled,true);
  assert.equal(downloads.node('batchChooseFolder').textContent, 'Chọn thư mục tải xuống');
  downloads.context.window.isSecureContext=false;
  await downloads.context.chooseBatchFolder();
  assert(downloads.node('batchFolderLabel').textContent.includes('chưa hỗ trợ'));

  const recoveryStorage=new Map([['muse2api_batch_history_v1',JSON.stringify({next:2,batches:[{
    id:'restored',kind:'image',jobs:[{index:0,prompt:'Khôi phục',id:'restored-task',status:'processing',checking:true,saveState:'saving'}]
  }]})]]);
  let recoveryQueries=0;
  const recovery=setup(async(url,opts)=>{
    assert.notEqual(opts.method,'POST');assert.equal(url,'/v1/images/tasks/restored-task');
    recoveryQueries++;return ok(complete('restored-task'));
  },recoveryStorage);
  assert.equal(recovery.node('batchReset').disabled,true);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(recoveryQueries,1);assert(recovery.node('batch-job-0').innerHTML.includes('Hoàn tất'));
  assert.equal(recovery.node('batchReset').disabled,true);

  let restoredPolls=0;
  const liveStorage=new Map([['muse2api_batch_history_v1',JSON.stringify({next:4,batches:[{id:'live',kind:'video',jobs:[
    {index:0,prompt:'Running video',id:'live-video',status:'processing',progress:15},
    {index:1,prompt:'Not submitted',status:'waiting'},
    {index:2,prompt:'Unknown submission',status:'submitting'}
  ]}]})]]);
  liveStorage.set('muse2api_batch_prompt_draft', 'Prompt draft must survive history restoration');
  const live=setup(async(url,opts)=>{
    assert.notEqual(opts.method,'POST');assert.equal(url,'/v1/videos/live-video');restoredPolls++;
    if(restoredPolls===1)return ok({id:'live-video',status:'processing',progress:55,elapsed:20});
    if(restoredPolls===2)return {status:503,body:{detail:'Temporary network failure'}};
    return ok({id:'live-video',status:'completed',result:{url:'/v1/media/live.mp4'}});
  },liveStorage);
  assert.equal(live.node('batchPrompts').value, 'Prompt draft must survive history restoration');
  assert.equal(liveStorage.get('muse2api_batch_prompt_draft'), 'Prompt draft must survive history restoration');
  assert.equal(live.context.batchEntries()[0].job.prompt, 'Running video');
  assert.equal(live.context.batchEntries()[0].job.id, 'live-video');
  await new Promise(resolve=>setImmediate(resolve));
  assert(live.node('batch-job-0').innerHTML.includes('55%'));
  assert(live.node('batch-job-0').innerHTML.includes('Đang xử lý'));
  assert(live.node('batch-job-1').innerHTML.includes('Đã bỏ qua'));
  assert.equal(live.node('batchRetryAll').disabled,false);
  assert.equal(live.node('batchRetryFailed').disabled,false);
  await live.context.refreshRecoveredBatchJobs();
  assert(live.node('batch-job-0').innerHTML.includes('Không lấy được trạng thái'));
  await live.context.refreshRecoveredBatchJobs();
  assert(live.node('batch-job-0').innerHTML.includes('Hoàn tất'));
  assert(!live.node('batch-job-0').innerHTML.includes('<video'));
  assert.equal(live.node('batchFilterCompleted').textContent,'Hoàn thành (1)');
  await live.context.refreshRecoveredBatchJobs();assert.equal(restoredPolls,3);
  assert.equal(live.node('batchFilterFailed').textContent,'Lỗi (1)');

  let missingPolls=0;
  const missing=setup(async(url,opts)=>{assert.notEqual(opts.method,'POST');missingPolls++;return {status:404,body:{detail:'Task missing'}};},new Map([['muse2api_batch_history_v1',JSON.stringify({next:2,batches:[{id:'missing',kind:'image',jobs:[{index:0,id:'missing',prompt:'Missing',status:'queued'}]}]})]]));
  await new Promise(resolve=>setImmediate(resolve));
  await missing.context.refreshRecoveredBatchJobs();assert.equal(missingPolls,1);
  assert(missing.node('batch-job-0').innerHTML.includes('không còn tác vụ'));

  let manualPosts=0;
  const manual=setup(async(url,opts)=>{
    if(opts.method==='POST'){manualPosts++;return ok(complete('manual'));}
    return {status:200,blob:'bytes'};
  });
  manual.context.URL=class extends URL {static createObjectURL(){return 'blob:download';}static revokeObjectURL(){}};
  manual.node('batchPrompts').value='Tải qua trình duyệt';
  await manual.context.runBatch();
  assert.equal(manual.node('batchReset').disabled,true);
  await manual.context.retryBatchSave(0);
  assert.equal(manual.state().jobs[0].saveState,'browserRequested');
  assert.equal(manual.node('batchReset').disabled,true);
  assert(manual.node('batch-job-0').innerHTML.includes('Xác nhận đã tải xong'));
  manual.context.confirmBatchDownload(0);
  assert.equal(manual.node('batchReset').disabled,false);assert.equal(manualPosts,1);

  let streamed=false;
  const streamSave=setup(async(url,opts)=>{
    if(opts.method==='POST')return ok({id:'stream',status:'completed',result:{url:'/v1/media/stream.mp4'}});
    return {status:200,stream:{pipeTo:async(writer,options)=>{assert(options.signal);await writer.write('streamed');await writer.close();}}};
  });
  streamSave.context.window.showDirectoryPicker=async()=>({name:'Videos',getFileHandle:async(name,options)=>{if(!options)throw Object.assign(new Error('Missing'),{name:'NotFoundError'});return {createWritable:async()=>({write:async data=>{streamed=data==='streamed';},close:async()=>{},abort:async()=>{}})};}});
  await streamSave.context.chooseBatchFolder();
  streamSave.node('batchKind').value='video';streamSave.node('batchPrompts').value='Video mẫu';
  await streamSave.context.runBatch();
  assert(streamed);assert.equal(streamSave.state().jobs[0].saveState,'saved');
  assert(streamSave.state().jobs[0].savedName.endsWith('.mp4'));

  let mediaReads=0;
  const localFiles=new Map();
  const localPreview=setup(async(url,opts)=>{
    if(opts.method==='POST') return ok({id:'local-preview',status:'completed',result:{url:'/v1/media/local-preview.mp4'}});
    mediaReads++;
    assert(String(url).startsWith('http://localhost:18610/v1/media/'));
    return {status:200,blob:'local-video-bytes'};
  });
  localPreview.context.URL=class extends URL {
    static createObjectURL(file){assert.equal(file.bytes,'local-video-bytes');return 'blob:local-preview';}
    static revokeObjectURL(){}
  };
  localPreview.context.window.showDirectoryPicker=async()=>({name:'PreviewFolder',getFileHandle:async(name,options)=>{
    if(!options){
      if(!localFiles.has(name)) throw Object.assign(new Error('Missing'),{name:'NotFoundError'});
      return {getFile:async()=>localFiles.get(name)};
    }
    return {createWritable:async()=>({write:async bytes=>{localFiles.set(name,{name,bytes});},close:async()=>{},abort:async()=>{}})};
  }});
  await localPreview.context.chooseBatchFolder();
  localPreview.node('batchKind').value='video';
  localPreview.node('batchPrompts').value='Video đã lưu';
  await localPreview.context.runBatch();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(mediaReads,1);
  const previewHtml=localPreview.node('batch-job-0').innerHTML;
  assert(previewHtml.includes('<video src="blob:local-preview"'));
  assert(!previewHtml.includes('<video src="http'));
  assert(!previewHtml.includes('/v1/media/local-preview.mp4" controls'));
  const reloaded=setup(async()=>{throw new Error('Reloaded preview must not fetch');},localPreview.storage);
  assert(reloaded.node('batch-job-0').innerHTML.includes('Chọn lại thư mục để xem'));
  assert(!reloaded.node('batch-job-0').innerHTML.includes('<video src='));
  reloaded.context.URL=localPreview.context.URL;
  reloaded.context.window.showDirectoryPicker=localPreview.context.window.showDirectoryPicker;
  await reloaded.context.chooseBatchFolder();
  await new Promise(resolve=>setImmediate(resolve));
  assert(reloaded.node('batch-job-0').innerHTML.includes('<video src="blob:local-preview"'));
  assert.equal(mediaReads,1);

  let imageReads=0;
  const imageFiles=new Map();
  const imagePreview=setup(async(url,opts)=>{
    if(opts.method==='POST') return ok({id:'local-image',status:'completed',result:{url:'/v1/media/local-image.webp'}});
    imageReads++;
    assert(String(url).startsWith('http://localhost:18610/v1/media/'));
    return {status:200,blob:'local-image-bytes'};
  });
  imagePreview.context.URL=class extends URL {
    static createObjectURL(file){assert.equal(file.bytes,'local-image-bytes');return 'blob:local-image';}
    static revokeObjectURL(){}
  };
  imagePreview.context.window.showDirectoryPicker=async()=>({name:'Images',getFileHandle:async(name,options)=>{
    if(!options){
      if(!imageFiles.has(name)) throw Object.assign(new Error('Missing'),{name:'NotFoundError'});
      return {getFile:async()=>imageFiles.get(name)};
    }
    return {createWritable:async()=>({write:async bytes=>{imageFiles.set(name,{name,bytes});},close:async()=>{},abort:async()=>{}})};
  }});
  await imagePreview.context.chooseBatchFolder();
  imagePreview.node('batchKind').value='image';
  imagePreview.node('batchPrompts').value='Ảnh đã lưu';
  await imagePreview.context.runBatch();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(imageReads,1);
  const imageHtml=imagePreview.node('batch-job-0').innerHTML;
  assert(imageHtml.includes('openBatchImagePreview(0)'));
  assert(imageHtml.includes('<img src="blob:local-image"'));
  assert(!imageHtml.includes('<img src="http'));
  imagePreview.context.openBatchImagePreview(0);
  assert.equal(imagePreview.node('batchImagePreview').hidden,false);
  assert.equal(imagePreview.node('batchImagePreviewImg').src,'blob:local-image');
  assert(imagePreview.node('batchImagePreviewCaption').textContent.includes('Ảnh đã lưu'));
  assert(imagePreview.node('batchImagePreviewCaption').textContent.includes('001.webp'));
  assert.equal(imageReads,1);
  imagePreview.context.closeBatchImagePreview();
  assert.equal(imagePreview.node('batchImagePreview').hidden,true);
  assert.equal(imagePreview.node('batchImagePreviewImg').src,undefined);
  imagePreview.context.openBatchImagePreview(0);
  imagePreview.context.deleteBatchJob(0);
  assert.equal(imagePreview.node('batchImagePreview').hidden,true);

  const translatedRequests=[];
  const translated=setup(async(url,opts)=>{
    translatedRequests.push({url,body:JSON.parse(opts.body)});
    assert.equal(url,'/v1/videos');
    return ok({id:'english-video',status:'completed',result:{url:'/v1/media/english.mp4'}});
  });
  translated.node('batchKind').value='video';translated.node('batchPrompts').value='Mèo cam đi trên bãi cỏ';
  await translated.context.runBatch();
  assert.equal(translatedRequests.length,1);
  assert.equal(translatedRequests[0].body.prompt,'Mèo cam đi trên bãi cỏ');
  assert(!translatedRequests.some(request=>request.url==='/v1/chat/completions'));
  assert(translated.node('batch-job-0').innerHTML.includes('<td'));
  assert(!translated.node('batch-job-0').innerHTML.includes('<video'));
  assert(translated.node('batch-job-0').innerHTML.includes('href="http://localhost:18610/v1/media/english.mp4"'));
  assert.equal(translated.node('batchFilterCompleted').textContent,'Hoàn thành (1)');
  translated.context.setBatchFilter('failed');assert.equal(translated.node('batch-job-0').hidden,true);
  translated.context.setBatchFilter('completed');assert.equal(translated.node('batch-job-0').hidden,false);
  translated.node('batchSearch').value='không trùng';translated.context.applyBatchFilter();
  assert.equal(translated.node('batch-job-0').hidden,true);
  translated.node('batchSearch').value='';translated.context.applyBatchFilter();
  await translated.context.rerunBatchJobs('one',0);
  assert.equal(translatedRequests.length,2);assert.equal(translated.state().jobs[0].index,0);
  assert.equal(translated.state().jobs[0].attempt,1);
  assert.equal(translated.state().jobs[0].saveState,undefined);
  assert.equal(translated.node('batchDeleteDone').disabled,true);
  translated.context.deleteFinishedBatchJobs();assert(translated.node('batchResults').innerHTML.includes('batch-job-0'));
  translated.context.deleteAllBatchJobs();
  assert.equal(translated.node('batchFilterAll').textContent,'Tất cả (0)');
  translated.node('batchPrompts').value='New prompt';translated.context.updateBatchStartLabel();
  assert.equal(translated.node('batchStart').textContent,'Chạy 1 prompt (STT 1–1)');

  let redoPosts=0;
  const redo=setup(async(url,opts)=>{
    assert.equal(opts.method,'POST');redoPosts++;
    return redoPosts===1 ? {status:400,body:{detail:'Invalid generation'}} : ok(complete('redo'+redoPosts));
  });
  redo.node('batchPrompts').value='Một\nHai';await redo.context.runBatch();
  assert.equal(redo.node('batchFilterFailed').textContent,'Lỗi (1)');
  await redo.context.rerunBatchJobs('failed');assert.equal(redoPosts,3);
  assert(redo.node('batch-job-0').innerHTML.includes('Lần tạo 2'));
  await redo.context.rerunBatchJobs('all');assert.equal(redoPosts,5);
  assert.equal(redo.node('batchFilterCompleted').textContent,'Hoàn thành (2)');
  assert.deepEqual(Array.from(redo.state().jobs,j=>j.index),[0,1]);
  assert(redo.node('batchResults').innerHTML.includes('<tr id="batch-job-1">'));
  const nextBefore=vm.runInContext('batchNextNumber',redo.context);
  vm.runInContext("batchHistory[0].jobs[0].saveState='saved'",redo.context);
  redo.context.deleteFinishedBatchJobs();
  assert(!redo.node('batchResults').innerHTML.includes('batch-job-0'));
  assert(redo.node('batchResults').innerHTML.includes('batch-job-1'));
  assert.equal(vm.runInContext('batchNextNumber',redo.context),nextBefore);
  redo.context.deleteBatchJob(1);assert.equal(redo.node('batchFilterAll').textContent,'Tất cả (0)');

  const errorLink=setup(async(url,opts)=>{
    if(opts.method==='POST')return ok({id:'error-link',status:'queued'});
    return ok({id:'error-link',status:'failed',error:'Timeout',account_label:'Muse account A',muse_url:'https://muse.ai/thread/error-123?key=hidden'});
  });
  errorLink.node('batchPrompts').value='Prompt lỗi';await errorLink.context.runBatch();
  const linkedRow=errorLink.node('batch-job-0').innerHTML;
  assert(linkedRow.includes('href="https://muse.ai/thread/error-123"'));
  assert(linkedRow.includes('copyBatchThreadLink(0)'));
  assert(linkedRow.includes('Copy link'));
  assert(!linkedRow.includes('Xem phiên máy chủ'));
  assert(!linkedRow.includes('Mở cuộc trò chuyện Muse'));
  assert(!linkedRow.includes('openMuseThread'));
  assert(linkedRow.includes('Muse account A'));
  assert(!linkedRow.includes('key=hidden'));
  assert.equal(errorLink.context.safeMuseThreadUrl('https://evil.test/thread/error-123'),'');
  assert.equal(errorLink.context.safeMuseThreadUrl('https://muse.ai/thread/new'),'');

  const missingLink=setup(async(url,opts)=>{
    if(opts.method==='POST')return ok({id:'no-link',status:'failed',error:'Failed before conversation creation'});
    return ok({id:'no-link',status:'failed',error:'Failed before conversation creation'});
  });
  missingLink.node('batchPrompts').value='Không có URL';await missingLink.context.runBatch();
  assert(missingLink.node('batch-job-0').innerHTML.includes('Lấy lại link'));
  assert(!missingLink.node('batch-job-0').innerHTML.includes('muse-thread-url'));
  assert(!missingLink.node('batch-job-0').innerHTML.includes('Copy link'));

  let metadataReads=0;
  const oldFailure=setup(async(url,opts)=>{
    assert.notEqual(opts.method,'POST');metadataReads++;
    return ok({id:'old-failure',status:'failed',error:'Timeout',account_label:'Account B',muse_url:'https://muse.ai/thread/old-conversation'});
  },new Map([['muse2api_batch_history_v1',JSON.stringify({next:2,batches:[{id:'old',kind:'video',jobs:[{index:0,prompt:'Cũ',id:'old-failure',status:'failed',error:'Timeout'}]}]})]]));
  await new Promise(resolve=>setImmediate(resolve));
  assert(oldFailure.node('batch-job-0').innerHTML.includes('href="https://muse.ai/thread/old-conversation"'));
  assert(!oldFailure.node('batch-job-0').innerHTML.includes('openMuseThread'));
  await oldFailure.context.refreshRecoveredBatchJobs();assert.equal(metadataReads,1);

  const recovered=[];
  let created=0;
  const recover=setup(async(url,opts)=>{
    recovered.push({url,method:opts.method||'GET'});
    if(opts.method==='POST' && url==='/v1/videos'){created++;return ok({id:'vid'+created,status:'queued'});}
    if(opts.method==='POST' && /\/admin\/tasks\/vid\d+\/collect$/.test(url)){
      const id=url.split('/')[3];
      return ok({id,status:'completed',account_label:'Account B',muse_url:'https://muse.ai/thread/'+id,result:{url:'/v1/media/'+id+'.mp4'},url:'/v1/media/'+id+'.mp4'});
    }
    const id=url.split('/').filter(Boolean).pop();
    return ok({id,status:'failed',error:'Timeout',account_label:'Account B',muse_url:'https://muse.ai/thread/'+id});
  });
  recover.node('batchKind').value='video';
  recover.node('batchPrompts').value='Video một\nVideo hai';
  await recover.context.runBatch();
  assert.equal(recovered.filter(item=>item.method==='POST' && item.url==='/v1/videos').length,2);
  assert.equal(recovered.filter(item=>item.method==='POST' && item.url.endsWith('/collect')).length,2);
  const recoveredJobs=Array.from(recover.context.batchEntries(), ({job})=>job);
  assert.equal(recoveredJobs.map(job=>job.status).join(','), 'completed,completed');
  assert(recoveredJobs.every(job=>String(job.url).includes('/v1/media/vid')));
  assert(recover.node('batchCollectRendered').disabled);

  let videoPosts=0, collectPosts=0;
  const fallback=setup(async(url,opts)=>{
    if(opts.method==='POST' && url.endsWith('/collect')){
      collectPosts++;
      return {status:502,body:{detail:'Cuộc trò chuyện chưa có video đã render để lấy về.'}};
    }
    if(opts.method==='POST' && url==='/v1/videos'){
      videoPosts++;
      const body=JSON.parse(opts.body);
      assert.equal(body.prompt,'Video chưa có file');
      if(videoPosts===1) return ok({id:'retry-video',status:'queued'});
      return ok({id:'retry-video',status:'completed',result:{url:'/v1/media/retry-video.mp4'},muse_url:'https://muse.ai/thread/retry-video'});
    }
    return ok({id:'retry-video',status:'failed',error:'Timeout',account_label:'Account B',muse_url:'https://muse.ai/thread/retry-video'});
  });
  fallback.node('batchKind').value='video';
  fallback.node('batchPrompts').value='Video chưa có file';
  await fallback.context.runBatch();
  assert.equal(videoPosts,1);
  assert.equal(collectPosts,1);
  assert.equal(fallback.state().jobs[0].status,'failed');
  await fallback.context.rerunBatchJobs('failed');
  assert.equal(collectPosts,2);
  assert.equal(videoPosts,2);
  assert.equal(fallback.state().jobs[0].status,'completed');
  assert(String(fallback.state().jobs[0].url).includes('/v1/media/retry-video.mp4'));

  assert(html.includes('value="10" selected'));
  assert(html.includes('id="vidDur" value="10"'));
  let defaultDuration;
  const durationDefault=setup(async(url,opts)=>{
    if(opts.method==='POST'){defaultDuration=JSON.parse(opts.body).duration;return ok({id:'dur',status:'completed',result:{url:'/v1/media/dur.mp4'}});}
    throw new Error('Unexpected status poll');
  });
  durationDefault.node('batchKind').value='video';
  durationDefault.node('batchDuration').value='';
  durationDefault.node('batchPrompts').value='Mặc định 10 giây';
  await durationDefault.context.runBatch();
  assert.equal(defaultDuration,10);
  assert.equal(durationDefault.context.batchEntries()[0].batch.duration,10);

  let editedPosts=0, editCollects=0;
  const edit=setup(async(url,opts)=>{
    if(opts.method==='POST' && url.endsWith('/collect')){editCollects++;return {status:502,body:{detail:'Cuộc trò chuyện chưa có video đã render để lấy về.'}};}
    if(opts.method==='POST' && url==='/v1/videos'){
      editedPosts++;
      const body=JSON.parse(opts.body);
      if(editedPosts===1){assert.equal(body.prompt,'Prompt cũ');return ok({id:'edit-1',status:'queued'});}
      assert.equal(body.prompt,'Prompt đã sửa');
      return ok({id:'edit-2',status:'completed',result:{url:'/v1/media/edit-2.mp4'}});
    }
    return ok({id:'edit-1',status:'failed',error:'Timeout',muse_url:'https://muse.ai/thread/edit-1'});
  });
  edit.node('batchKind').value='video';
  edit.node('batchPrompts').value='Prompt cũ';
  await edit.context.runBatch();
  assert.equal(editedPosts,1);
  assert.equal(editCollects,1);
  assert.equal(edit.state().jobs[0].status,'failed');
  assert(edit.node('batch-job-0').innerHTML.includes('Sửa prompt'));
  edit.context.startBatchPromptEdit(0);
  assert(edit.node('batch-job-0').innerHTML.includes('batch-prompt-edit-0'));
  assert(edit.node('batch-job-0').innerHTML.includes('Gửi lại'));
  assert(!JSON.parse(edit.storage.get('muse2api_batch_history_v1')).batches[0].jobs[0].editing);
  edit.context.rememberBatchPromptDraft(0,'   ');
  await edit.context.submitBatchPromptEdit(0);
  assert.equal(editedPosts,1);
  assert.equal(edit.state().jobs[0].prompt,'Prompt cũ');
  assert.equal(edit.state().jobs[0].editing,true);
  edit.context.cancelBatchPromptEdit(0);
  assert.equal(edit.state().jobs[0].editing,undefined);
  assert(!edit.node('batch-job-0').innerHTML.includes('batch-prompt-edit-0'));
  edit.context.startBatchPromptEdit(0);
  edit.context.rememberBatchPromptDraft(0,'Prompt đã sửa');
  await edit.context.submitBatchPromptEdit(0);
  assert.equal(editCollects,1);
  assert.equal(editedPosts,2);
  assert.equal(edit.state().jobs[0].prompt,'Prompt đã sửa');
  assert.equal(edit.state().jobs[0].status,'completed');
  assert.equal(edit.state().jobs[0].attempt,1);

  let watchCollects=0, watchPosts=0;
  const watch=setup(async(url,opts)=>{
    if(opts.method==='POST' && url.endsWith('/collect')){
      watchCollects++;
      if(watchCollects===1) return {status:502,body:{detail:'Cuộc trò chuyện chưa có video đã render để lấy về.'}};
      return ok({id:'watch-1',status:'completed',muse_url:'https://muse.ai/thread/watch-1',result:{url:'/v1/media/watch.mp4'}});
    }
    if(opts.method==='POST'){watchPosts++;throw new Error('watcher must not send a new prompt');}
    throw new Error('watcher must not poll the task API');
  }, new Map([['muse2api_batch_history_v1',JSON.stringify({next:2,batches:[{id:'watch',kind:'video',jobs:[{index:0,prompt:'Chờ file',id:'watch-1',status:'failed',error:'仅返回文本',museUrl:'https://muse.ai/thread/watch-1'}]}]})]]));
  assert(watch.node('batch-job-0').innerHTML.includes('Đang tự mở lại cuộc trò chuyện'));
  await watch.context.watchRenderedFailures();
  assert.equal(watchCollects,1);
  assert.equal(watchPosts,0);
  assert.equal(watch.context.batchEntries()[0].job.status,'failed');
  await watch.context.watchRenderedFailures();
  assert.equal(watchCollects,1);
  watch.context.batchEntries()[0].job.watchNext=0;
  await watch.context.watchRenderedFailures();
  assert.equal(watchCollects,2);
  assert.equal(watchPosts,0);
  assert.equal(watch.context.batchEntries()[0].job.status,'completed');
  assert(String(watch.context.batchEntries()[0].job.url).includes('/v1/media/watch.mp4'));

  let releaseOld, releaseNew, racePosts = 0;
  const oldGate = new Promise(resolve => { releaseOld = resolve; });
  const newGate = new Promise(resolve => { releaseNew = resolve; });
  const race = setup(async (url, opts) => {
    if (opts.method === 'POST' && url === '/v1/images/tasks') {
      racePosts++;
      if (racePosts === 1) { await oldGate; return {status:500, body:{detail:'old worker'}}; }
      await newGate;
      return ok({id:'new-job', status:'queued'});
    }
    return ok(complete('new-job'));
  });
  race.node('batchPrompts').value = 'Một\nHai';
  race.node('batchConcurrency').value = '1';
  const firstRun = race.context.runBatch();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(racePosts, 1);
  race.context.stopBatch();
  assert.equal(race.context.batchEntries().map(({job}) => job.status).join(','), 'submitting,stopped');
  const rerun = race.context.rerunBatchJobs('failed');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(racePosts, 2);
  assert.equal(race.node('batchStop').disabled, false);
  assert(race.node('batch-job-1').innerHTML.includes('Đang gửi'));
  releaseOld();
  await new Promise(resolve => setImmediate(resolve));
  assert.notEqual(race.context.batchEntries()[1].job.status, 'stopped');
  assert.equal(race.node('batchStop').disabled, false);
  releaseNew();
  await rerun;
  await firstRun;
  assert.equal(race.context.batchEntries().map(({job}) => job.status).join(','), 'unconfirmed,completed');
  assert.equal(racePosts, 2);

  const lanePosts = [];
  const lanes = setup(async (url, opts) => {
    if (opts.method === 'POST') {
      lanePosts.push(JSON.parse(opts.body).account_id);
      return ok({id:'lane'+lanePosts.length, status:'queued'});
    }
    return ok(complete(url.split('/').pop()));
  });
  vm.runInContext('accounts = [{id:"cookie-a", enabled:true}, {id:"cookie-b", enabled:true}]', lanes.context);
  lanes.node('batchKind').value = 'video';
  lanes.node('batchConcurrency').value = '30';
  lanes.node('batchPrompts').value = ['Một','Hai','Ba','Bốn'].join('\n');
  await lanes.context.runBatch();
  assert.equal(lanes.state().concurrency, 10);
  assert.equal(lanePosts.filter(id => id === 'cookie-a').length, 2);
  assert.equal(lanePosts.filter(id => id === 'cookie-b').length, 2);
  assert(!html.includes('<option value="30">30</option>'));
  assert(html.includes('value="10" selected'));
  vm.runInContext('accounts = [{id:"cookie-a", enabled:true}]', lanes.context);
  assert.equal(vm.runInContext('batchAccountLanes(10).length', lanes.context), 5);
  vm.runInContext('applyBatchServerLimits({max_concurrency:6, per_account_concurrency:2})', lanes.context);
  assert.equal(vm.runInContext('batchAccountLanes(10).length', lanes.context), 2);
  assert.equal(vm.runInContext('batchParallelLimit()', lanes.context), 6);
  assert(!html.includes('8 (tối đa)'));

  let reconnectPosts = 0, reconnectCollects = 0;
  const reconnect = setup(async (url, opts) => {
    if (opts.method === 'POST' && url.endsWith('/collect')) {
      reconnectCollects++;
      assert.equal(JSON.parse(opts.body).muse_url, 'https://muse.ai/thread/old-task');
      return ok({id:'old-task', status:'completed', muse_url:'https://muse.ai/thread/old-task', result:{url:'/v1/media/old-task.mp4'}});
    }
    if (opts.method === 'POST') { reconnectPosts++; throw new Error('reconnect must not send a prompt'); }
    throw new Error('reconnect must not poll before collect');
  }, new Map([['muse2api_batch_history_v1', JSON.stringify({next:2, batches:[{id:'cut', kind:'video', jobs:[{index:0, prompt:'Bị ngắt', id:'old-task', status:'failed', error:'服务重启中断了任务，请重新提交', museUrl:'https://muse.ai/thread/old-task'}]}]})]]));
  assert(reconnect.node('batch-job-0').innerHTML.includes('Nối lại'));
  assert(reconnect.node('batch-job-0').innerHTML.includes('không gửi prompt mới'));
  assert.equal(reconnect.node('batchReconnect').disabled, false);
  await reconnect.context.reconnectInterrupted();
  assert.equal(reconnectPosts, 0);
  assert.equal(reconnectCollects, 1);
  assert.equal(reconnect.context.batchEntries()[0].job.status, 'completed');

  let pendingPosts = 0, pendingCollects = 0;
  const pending = setup(async (url, opts) => {
    if (opts.method === 'POST' && url.endsWith('/collect')) {
      pendingCollects++;
      return {status:502, body:{detail:'Cuộc trò chuyện chưa có video đã render để lấy về.'}};
    }
    if (opts.method === 'POST') { pendingPosts++; throw new Error('pending reconnect must not send a prompt'); }
    throw new Error('pending reconnect must not poll');
  }, new Map([['muse2api_batch_history_v1', JSON.stringify({next:3, batches:[{id:'cut2', kind:'video', jobs:[
    {index:0, prompt:'Chưa xong', id:'pending-task', status:'failed', error:'服务重启中断了任务，请重新提交', museUrl:'https://muse.ai/thread/pending-task'},
    {index:1, prompt:'Mất cuộc', id:'missing-task', status:'failed', error:'服务重启中断了任务，请重新提交'}
  ]}]})]]));
  await pending.context.reconnectInterrupted(0);
  assert.equal(pendingPosts, 0);
  assert.equal(pendingCollects, 1);
  assert.equal(pending.context.batchEntries()[0].job.status, 'failed');
  assert(pending.node('batch-job-0').innerHTML.includes('không gửi prompt mới'));
  await pending.context.reconnectInterrupted(1);
  assert.equal(pendingPosts, 0);
  assert(pending.node('batch-job-1').innerHTML.includes('Bấm Tạo lại để gửi prompt mới'));

  let clipHeads = 0, clipPrompts = 0;
  const clip = setup(async (url, opts) => {
    if (opts.method === 'HEAD') {
      clipHeads++;
      return {status:200, body:{}, headers:{'content-length':'357801'}};
    }
    if (opts.method === 'POST' && String(url).endsWith('/collect')) {
      return ok({id:'clip-task', status:'completed', muse_url:'https://muse.ai/thread/clip-task', result:{url:'/v1/media/real.mp4'}});
    }
    if (opts.method === 'POST') { clipPrompts++; throw new Error('clip row must not send a prompt'); }
    throw new Error('unexpected clip request ' + url);
  }, new Map([['muse2api_batch_history_v1', JSON.stringify({next:2, batches:[{id:'clip', kind:'video', jobs:[
    {index:0, prompt:'Nhầm file', id:'clip-task', status:'completed', url:'/v1/media/page.mp4', museUrl:'https://muse.ai/thread/clip-task'}
  ]}]})]]));
  for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(clipHeads, 1);
  assert.equal(clip.context.batchEntries()[0].job.status, 'failed');
  assert(clip.node('batch-job-0').innerHTML.includes('Nối lại'));
  assert.equal(clip.node('batchReconnect').disabled, false);
  await clip.context.reconnectInterrupted();
  assert.equal(clipPrompts, 0);
  assert.equal(clip.context.batchEntries()[0].job.status, 'completed');
  assert.equal(clip.context.batchEntries()[0].job.url, '/v1/media/real.mp4');

  assert(html.includes('Tải toàn bộ video chưa tải'));
  const fetched = [];
  const bulk = setup(async (url, opts) => {
    if (opts && opts.method === 'HEAD') return {status:200, body:{}, headers:{'content-length':'100'}};
    fetched.push(String(url));
    return {status:200, blob:'video-bytes'};
  }, new Map([['muse2api_batch_history_v1', JSON.stringify({next:5, batches:[
    {id:'vids', kind:'video', jobs:[
      {index:0, prompt:'Đã lưu', status:'completed', url:'/v1/media/saved.mp4', saveState:'saved', savedName:'001.mp4'},
      {index:1, prompt:'Chưa tải', status:'completed', url:'/v1/media/new.mp4'},
      {index:2, prompt:'Lỗi lưu', status:'completed', url:'/v1/media/retry.mp4', saveState:'failed'},
      {index:3, prompt:'Đang chạy', status:'processing'}
    ]},
    {id:'imgs', kind:'image', jobs:[
      {index:4, prompt:'Ảnh', status:'completed', url:'/v1/media/pic.webp'}
    ]}
  ]})]]));
  for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve));
  bulk.context.URL = class extends URL { static createObjectURL(){ return 'blob:bulk'; } static revokeObjectURL(){} };
  assert.equal(bulk.node('batchDownloadPending').disabled, false);
  await bulk.context.downloadPendingVideos();
  assert.deepEqual(fetched, [
    'http://localhost:18610/v1/media/new.mp4',
    'http://localhost:18610/v1/media/retry.mp4'
  ]);
  assert.equal(bulk.context.batchEntries().map(({job}) => job.saveState).join(','), 'saved,browserRequested,browserRequested,,');
  assert.equal(bulk.node('batchDownloadPending').disabled, true);

  const pair = setup(async (url, opts) => {
    assert.notEqual(opts.method, 'POST');
    return ok({id:'pair-task', status:'failed', error:'任务准备超时，已停止发送', account_label:'muse-main-2'});
  }, new Map([['muse2api_batch_history_v1', JSON.stringify({next:2, batches:[{id:'pair', kind:'video', jobs:[
    {index:0, prompt:'Sai tài khoản', id:'pair-task', status:'failed', error:'timeout',
     museUrl:'https://muse.ai/thread/8711f530-aac2-459e-a743-d4c22647eaa1', accountLabel:'muse-main-1'}
  ]}]})]]));
  await pair.context.checkBatchResult(0);
  assert.equal(pair.context.batchEntries()[0].job.accountLabel, 'muse-main-1');
  assert.equal(pair.context.batchEntries()[0].job.museUrl, 'https://muse.ai/thread/8711f530-aac2-459e-a743-d4c22647eaa1');
  assert(pair.node('batch-job-0').innerHTML.includes('muse-main-1'));
  assert(!pair.node('batch-job-0').innerHTML.includes('muse-main-2'));

  sharedFolderDb = memoryFolderDb();
  const remembered = setup(async () => ({status:200, body:{}}));
  remembered.context.window.showDirectoryPicker = async () => ({name:'Ket qua video', queryPermission: async () => 'granted', getFileHandle: async () => { throw Object.assign(new Error('Missing'), {name:'NotFoundError'}); }});
  await remembered.context.chooseBatchFolder();
  assert(remembered.node('batchFolderLabel').textContent.includes('Ket qua video'));
  assert(remembered.node('batchFolderLabel').textContent.includes('giữ sau khi tải lại'));
  assert.equal(remembered.node('batchChooseFolder').textContent, 'Ket qua video');
  const restoredFolder = setup(async () => { throw new Error('Reload must not fetch to restore the folder'); });
  for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));
  assert(restoredFolder.node('batchFolderLabel').textContent.includes('Ket qua video'));
  assert.equal(restoredFolder.node('batchChooseFolder').textContent, 'Ket qua video');
  assert.equal(restoredFolder.node('batchClearFolder').disabled, false);
  restoredFolder.context.clearBatchFolder();
  for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));
  const forgotten = setup(async () => { throw new Error('Cleared folder must not be restored'); });
  for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(forgotten.node('batchFolderLabel').textContent, '');
  assert.equal(vm.runInContext('batchDirectory', forgotten.context), null);
  sharedFolderDb = null;

  {
    sharedFolderDb = memoryFolderDb();
    let readable=true, authorized=false, pickerCalls=0;
    const directory={name:'Remembered videos',
      queryPermission:async({mode})=>authorized || (mode==='read' && readable) ? 'granted' : 'prompt',
      requestPermission:async()=>{authorized=true;return 'granted';},
      getFileHandle:async()=>({getFile:async()=>({name:'001.mp4'})})};
    const seed=setup(async()=>ok({}));
    await seed.context.persistBatchFolder(directory);
    const savedHistory=new Map([['muse2api_batch_history_v1',JSON.stringify({next:2,batches:[{id:'saved-folder',kind:'video',jobs:[
      {index:0,prompt:'Saved video',status:'completed',saveState:'saved',savedName:'001.mp4',clipChecked:true}
    ]}]})]]);
    const reopened=setup(async()=>{throw new Error('Local preview must not fetch');},savedHistory);
    reopened.context.URL=class extends URL {static createObjectURL(){return 'blob:restored-video';} static revokeObjectURL(){}};
    for(let i=0;i<10;i++) await new Promise(resolve=>setImmediate(resolve));
    assert(reopened.node('batch-job-0').innerHTML.includes('blob:restored-video'));
    assert.equal(vm.runInContext('batchDirectory',reopened.context),null);
    reopened.context.clearBatchFolder();
    assert(!reopened.node('batch-job-0').innerHTML.includes('blob:restored-video'));
    await seed.context.persistBatchFolder(directory);
    readable=false;
    const lockedFolder=setup(async()=>{throw new Error('Permission recovery must not fetch');},savedHistory);
    lockedFolder.context.URL=class extends URL {static createObjectURL(){return 'blob:authorized-video';} static revokeObjectURL(){}};
    lockedFolder.context.window.showDirectoryPicker=async()=>{pickerCalls++;return directory;};
    for(let i=0;i<10;i++) await new Promise(resolve=>setImmediate(resolve));
    assert(lockedFolder.node('batch-job-0').innerHTML.includes('Remembered videos'));
    assert(lockedFolder.node('batch-job-0').innerHTML.includes('Cho phép mở video đã lưu'));
    await lockedFolder.context.chooseBatchFolder();
    for(let i=0;i<4;i++) await new Promise(resolve=>setImmediate(resolve));
    assert.equal(pickerCalls,0);
    assert(lockedFolder.node('batch-job-0').innerHTML.includes('blob:authorized-video'));
    sharedFolderDb=null;
  }

  console.log('PASS: bounded concurrency, per-line jobs, failure isolation, queue retries, polling retries, stop, uncertain submissions, video settings, escaping, safe links, folder selection, cancellation, local saving, streaming and save retries without regeneration, cumulative numbering, preview counts, reload persistence, reset gating, collision avoidance, table filters/search, retries, guarded deletion and automatic post-reload status recovery without duplicate submissions.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

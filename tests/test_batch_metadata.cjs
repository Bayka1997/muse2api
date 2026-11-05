// Offline checks: node tests/test_batch_metadata.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '..', 'admin.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const context = vm.createContext({
  renderBatchJob(){}, dropLocalBatchPreview(){}, toast(){}, batchDirectory:null,
});
for (const name of ['resetBatchJobForResume', 'moveBatchJobToUnsent', 'enqueueWaitingBatchRetry']) {
  const start = script.indexOf('function ' + name + '(');
  const rest = script.slice(start);
  const end = rest.search(/\n(?:async )?function /);
  vm.runInContext(end < 0 ? rest : rest.slice(0, end), context);
}
const makeJob = () => ({prompt:'Video prompt', narration:'Thuyết minh cảnh 1',
  vietnamesePrompt:'Prompt tiếng Việt', needsPromptReview:true, index:0,
  imageReferenceKey:'uploaded-reference', status:'failed', error:'Old error', id:'old-task'});
function check(job) {
  assert.equal(job.narration, 'Thuyết minh cảnh 1');
  assert.equal(job.vietnamesePrompt, 'Prompt tiếng Việt');
  assert.equal(job.needsPromptReview, true);
  assert.equal(job.imageReferenceKey, 'uploaded-reference');
  assert.equal(job.id, undefined);
}
let job = makeJob();
context.resetBatchJobForResume(job, 'resume-token'); check(job);
job = makeJob(); context.moveBatchJobToUnsent({}, job); check(job);
job = makeJob(); const batch = {jobs:[job]};
check(context.enqueueWaitingBatchRetry(batch, job, null));
// Every destructive job reset must restore the reference text fields.
const resets = [...script.matchAll(/(?:Object\.keys\(job\)\.forEach\([^\n]+|for \(const key of Object\.keys\(job\)\) delete job\[key\];)\s*Object\.assign\(job,\s*\{([^}]+)\}/g)];
assert.equal(resets.length, 6);
for (const [,fields] of resets) {
  assert.match(fields, /\bnarration\b/);
  assert.match(fields, /\bvietnamesePrompt\b/);
  assert.match(fields, /\bneedsPromptReview\b/);
}
console.log('Batch metadata preserved across resume, cancel, retry, and all job reset paths.');
const pendingJob = {index:7, prompt:'New prompt'};
context.batchDraftJobByIndex = () => pendingJob;
context.renderBatchDraftList = () => {};
for (const name of ['batchPromptReviewButton', 'confirmBatchPromptReview']) {
  const start = script.indexOf('function ' + name + '(');
  const rest = script.slice(start);
  vm.runInContext(rest.slice(0, rest.search(/\n(?:async )?function /)), context);
}
assert.match(context.batchPromptReviewButton(pendingJob), /Đã duyệt lại/);
context.confirmBatchPromptReview(7);
assert.equal(pendingJob.needsPromptReview, false);
assert.equal(context.batchPromptReviewButton(pendingJob), '');
const restoredJob = JSON.parse(JSON.stringify(pendingJob));
context.resetBatchJobForResume(restoredJob, 'resume');
assert.equal(restoredJob.needsPromptReview, false);
console.log('Unreviewed prompts default to pending; explicit approval survives persistence and resume.');

// Isolated reproductions of extracted control-flow/algorithms, NOT repository integration tests.
import assert from 'node:assert/strict';
import { resolve, dirname } from 'node:path';
const results = [];
const check = async (name, run) => { await run(); results.push({name,status:'passed'}); };

await check('HEAD is overwritten by queryClient.get', () => {
  const transport = (init) => init.method;
  const get = (options) => transport({...options, method: 'GET', requestClass: 'query'});
  const legacy = (init) => ['GET','HEAD'].includes(init.method) ? get(init) : null;
  assert.equal(legacy({method:'HEAD'}), 'GET');
  const patched = (init) => init.method === 'HEAD' ? transport({...init,method:'HEAD',expectNoContent:true}) : get(init);
  assert.equal(patched({method:'HEAD'}), 'HEAD');
});

await check('Admission after body await needs a fresh drain check', async () => {
  async function scenario(patched) {
    let draining=false, starts=0, release;
    const body = new Promise(r => release=r);
    const admission = (async () => {
      if (draining) return false;
      await body;
      if (patched && draining) return false;
      starts++; return true;
    })();
    draining=true; release();
    await admission;
    return starts;
  }
  assert.equal(await scenario(false),1);
  assert.equal(await scenario(true),0);
});

await check('IME Escape closes palette before composition guard', () => {
  function legacy(e) {
    if(e.key==='Escape') return 'closed';
    if(e.nativeEvent.isComposing) return 'unchanged';
    return 'unchanged';
  }
  const e={key:'Escape',nativeEvent:{isComposing:true}};
  assert.equal(legacy(e),'closed');
  const fixed=e=>e.nativeEvent.isComposing?'unchanged':legacy(e);
  assert.equal(fixed(e),'unchanged');
});

function order(a,b) { return a===b?0:a>b?1:-1; }
function docVersion(doc) {
  return {
    timestamp:Math.max(...doc.recent_executions.map(e=>Date.parse(e.updated_at)),
      ...doc.active_runs.map(r=>Date.parse(r.updated_at)),Number.NEGATIVE_INFINITY),
    sequence:Math.max(...doc.selections.map(s=>s.revision),
      ...doc.active_runs.map(r=>r.last_event_seq),Number.NEGATIVE_INFINITY)
  };
}
function compareLegacy(a,b) {
  const x=docVersion(a),y=docVersion(b),td=x.timestamp-y.timestamp;
  return td!==0?td:x.sequence-y.sequence;
}
function compareFixed(a,b) {
  const x=docVersion(a),y=docVersion(b);
  return order(x.timestamp,y.timestamp)||order(x.sequence,y.sequence);
}
await check('Empty projection timestamps yield NaN and drop missing selections', () => {
  const base={revision:1,recent_executions:[],active_runs:[]};
  const current={...base,selections:[{node_id:'a',revision:5},{node_id:'b',revision:3}]};
  const incoming={...base,selections:[{node_id:'a',revision:4}]};
  assert.equal(Number.isNaN(compareLegacy(current,incoming)),true);
  assert.equal(compareLegacy(current,incoming)>0,false);
  assert.equal(compareFixed(current,incoming),1);
  assert.equal(order(-Infinity,-Infinity),0);
});

function sortLegacy(value) {
  if(Array.isArray(value)) return value.map(sortLegacy);
  if(value===null || typeof value!=='object') return value;
  const sorted={};
  for(const key of Object.keys(value).sort()) sorted[key]=sortLegacy(value[key]);
  return sorted;
}
function sortFixed(value) {
  if(Array.isArray(value)) return value.map(sortFixed);
  if(value===null || typeof value!=='object') return value;
  const sorted=Object.create(null);
  for(const key of Object.keys(value).sort()) sorted[key]=sortFixed(value[key]);
  return sorted;
}
await check('Own __proto__ key is lost from semantic fingerprint', () => {
  const a=JSON.parse('{"__proto__":{"x":1},"text":"same"}');
  const b=JSON.parse('{"__proto__":{"x":2},"text":"same"}');
  assert.equal(JSON.stringify(sortLegacy(a)),JSON.stringify(sortLegacy(b)));
  assert.notEqual(JSON.stringify(sortFixed(a)),JSON.stringify(sortFixed(b)));
  assert.equal(Object.hasOwn(sortFixed(a),'__proto__'),true);
  assert.equal(Object.prototype.x,undefined); // not global prototype pollution
});

await check('Fresh key on manual resend bypasses key-based deduplication', () => {
  const accepted = new Map(); let executions=0;
  const server=key=>{if(!accepted.has(key)) accepted.set(key,++executions);return accepted.get(key)};
  server('first-key'); server('first-key'); // same-attempt automatic replay
  assert.equal(executions,1);
  server('new-manual-key');
  assert.equal(executions,2);
});

await check('Agent delivery classifier misses a 504 compared to shared policy', () => {
  const e={status:504,code:'upstream_error'};
  const legacy=e=>e.status===0||e.code==='network_error'||e.code==='request_timeout';
  const shared=e=>e.status===408||e.status===425||e.status===429||e.status>=500;
  assert.equal(legacy(e),false); assert.equal(shared(e),true);
});

await check('Tailwind source path resolves below app instead of web/src', () => {
  const sheet='/repo/apps/web/src/app/globals.css';
  assert.equal(resolve(dirname(sheet),'./src'),'/repo/apps/web/src/app/src');
  assert.equal(resolve(dirname(sheet),'../'),'/repo/apps/web/src');
});
console.log(JSON.stringify({kind:'isolated algorithm/control-flow reproductions',node:process.version,tests:results},null,2));

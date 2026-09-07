const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const MINT = '8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump';
const OTHER = '8KC4HMFfE6BPAPV1zzLpag6Brc5vBuqojfCj7wWApump';
function harness(fetcher, search = '') {
  function element() { return {value:'', textContent:'', attrs:{}, events:{}, setAttribute(k,v){this.attrs[k]=v;}, addEventListener(k,f){this.events[k]=f;}, focus(){}}; }
  const ids = Object.fromEntries(['verifyInput','verifyStatus','verifyStatusTitle','verifyStatusBody'].map(k=>[k,element()]));
  const form = element(), calls = [], navigations = [], timers = new Map(); let timer = 0;
  const window = {location:{search, assign:p=>navigations.push(p)}};
  vm.runInNewContext(fs.readFileSync('web/assets/verify.js','utf8'), {
    window, document:{querySelector:()=>form, getElementById:k=>ids[k]}, URLSearchParams, AbortController,
    fetch:(url,options)=>{calls.push(url);return fetcher(url,options);},
    setTimeout:fn=>{timers.set(++timer,fn);return timer;},clearTimeout:id=>timers.delete(id)
  });
  return {ids,form,calls,navigations,timers,window,submit:async(mint=MINT)=>{ids.verifyInput.value=mint;await form.events.submit({preventDefault(){}});},title:()=>ids.verifyStatusTitle.textContent};
}
const record = mint => ({mint,observed_at:1788784374,error:null,checks:[{name:'CONFIG_MINT',status:'PASS'}]});
const response = data => ({ok:true,status:200,json:async()=>data});
test('query and input never request a read',()=>{
  const h=harness(()=>{throw Error('unexpected read');},'?mint='+MINT);
  assert.equal(h.ids.verifyInput.value,MINT);h.ids.verifyInput.events.input();assert.equal(h.calls.length,0);
});
test('canonical 32-byte validation rejects alphabet-only and overflowing addresses',async()=>{
  for (const mint of ['0'.repeat(32),'1'.repeat(33),'z'.repeat(44),'2'.repeat(32),'not a mint']) {
    const h=harness(()=>{}); await h.submit(mint);assert.equal(h.calls.length,0);assert.match(h.title(),/doesn't look/);
  }
  const h=harness(()=>response(record('1'.repeat(32))));await h.submit('1'.repeat(32));assert.equal(h.calls.length,1);
});
test('successful record navigates to the server report without client figures',async()=>{
  const h=harness(()=>response(record(MINT)));await h.submit(' '+MINT+' ');
  assert.deepEqual(h.navigations,['/verify/'+MINT]);assert.equal(h.timers.size,0);
});
test('HTTP 200 failed reads never navigate or show a coin verdict',async()=>{
  const h=harness(()=>response({...record(MINT),error:'RPC unavailable',error_kind:'rpc_unavailable'}));await h.submit();
  assert.equal(h.navigations.length,0);assert.match(h.title(),/couldn't read/);assert.match(h.ids.verifyStatusBody.textContent,/not with this coin/);
});
test('typed no-split records use the server report',async()=>{
  const h=harness(()=>response({...record(MINT),error:'Wording may change',error_kind:'no_sharing_config'}));await h.submit();
  assert.match(h.title(),/doesn't split/);assert.deepEqual(h.navigations,['/verify/'+MINT]);
});
test('non-JSON 429 does not parse and locally backs off',async()=>{
  const h=harness(()=>({status:429,ok:false,json(){throw Error('must not parse');}}));await h.submit();await h.submit();
  assert.equal(h.calls.length,1);assert.match(h.title(),/very quickly/);assert.equal(h.navigations.length,0);
});
test('HTTP failures, malformed JSON and mismatched or incomplete records do not navigate',async()=>{
  for (const reply of [{status:503,ok:false},{ok:true,status:200,json:async()=>{throw Error('bad JSON');}}, response(record(OTHER)),response({mint:MINT}),response({...record(MINT),checks:[]})]) {
    const h=harness(()=>reply);await h.submit();assert.equal(h.navigations.length,0);assert.match(h.title(),/couldn't read/);
  }
});
test('20-second abort has its own read-failure state',async()=>{
  const h=harness((url,{signal})=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(Error('aborted')))));
  const pending=h.submit();[...h.timers.values()][0]();await pending;assert.match(h.title(),/too long/);assert.equal(h.navigations.length,0);
});
test('superseded responses cannot overwrite the new request',async()=>{
  const pending=[];const h=harness(()=>new Promise(resolve=>pending.push(resolve)));
  const first=h.submit(MINT);const second=h.submit(OTHER);
  pending[1](response(record(OTHER)));await second;pending[0](response(record(MINT)));await first;
  assert.deepEqual(h.navigations,['/verify/'+OTHER]);assert.equal(h.timers.size,0);
});

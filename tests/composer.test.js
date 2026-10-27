const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');
const source = fs.readFileSync(require.resolve('../static/app.js'), 'utf8');
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b; }); return {promise, resolve, reject}; };
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness(api, session='session') {
  const nodes = new Map(), rendered = [], notices = [];
  const node = () => ({value:'', checked:true, hidden:false, disabled:false, textContent:'', handlers:{}, children:[],
    addEventListener(name, handler) {this.handlers[name]=handler;},
    append(...items) {this.children.push(...items);},
    replaceWith(item) {rendered.push(item);}, remove() {}, focus() {}, querySelectorAll() {return [];} });
  const $ = selector => { if (!nodes.has(selector)) nodes.set(selector,node()); return nodes.get(selector); };
  $('#daily-message').value = '解释功率因数';
  const context = vm.createContext({$, api, AbortController, crypto:{randomUUID},
    el:(tag, cls, text) => Object.assign(node(),{text}), dailyBubble:(role,text,label,images) => ({role,text,label,images,remove(){}}),
    assistantBubble:result => ({answer:result.answer}), refreshSessions:async()=>[], savedSetting(){},
    toast:message=>notices.push(message)});
  vm.runInContext(`let dailyBusy=false, imageBusy=false, dailyOperation=null, pendingImages=[{name:'image',url:'data:image'}], pendingRequest=null, activeSession=${JSON.stringify(session)};
    function renderImagePreviews(){syncDailyControls();}
    ${source.slice(source.indexOf('function syncDailyControls()'),source.indexOf('function renderImagePreviews()'))}
    ${source.slice(source.indexOf('$("#daily-stop").onclick'), source.indexOf('PowerChat.bindSend($("#daily-message")'))}`,context);
  return {$, rendered, notices, send:()=>$('#daily-form').handlers.submit({preventDefault(){}}),
    stop:()=>$('#daily-stop').onclick(), state:()=>vm.runInContext('({dailyBusy,pendingImages,dailyOperation,pendingRequest})',context)};
}

test('send clears text and image draft immediately, not after reply', async () => {
  const reply=deferred(); const ui=harness(()=>reply.promise);
  const done=ui.send();
  assert.equal(ui.$('#daily-message').value,'');
  assert.equal(ui.state().pendingImages.length,0);
  assert.equal(ui.$('#daily-stop').hidden,false);
  reply.resolve({answer:'解释'}); await done;
  assert.equal(ui.rendered[0].answer,'解释');
  assert.equal(ui.$('#daily-stop').hidden,true);
  assert.equal(ui.state().dailyBusy,false);
});

test('failed send restores the original text and attachments for retry', async () => {
  const reply=deferred(); const ui=harness(()=>reply.promise);
  const done=ui.send(); reply.reject(new Error('网络错误')); await done;
  assert.equal(ui.$('#daily-message').value,'解释功率因数');
  assert.equal(ui.state().pendingImages[0].url,'data:image');
  assert.equal(ui.$('#daily-error').hidden,false);
});

test('stop confirms cancellation on server before aborting and allows next send', async () => {
  const calls=[];
  const ui=harness((path,method,body,signal)=>{
    calls.push({path,body});
    if(path.endsWith('/cancel')) return Promise.resolve({status:'cancelled'});
    return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted'))));
  });
  const done=ui.send(); await ui.stop(); await done;
  assert.equal(calls[1].path,'/api/assistant/cancel');
  assert.equal(calls[0].body.request_id,calls[1].body.request_id);
  assert.match(ui.rendered[0].text,/已停止生成/);
  assert.equal(ui.$('#daily-message').value,'');
  assert.equal(ui.state().dailyBusy,false);
  assert.equal(ui.state().pendingRequest,null);
});

test('stop while creating a session prevents any model request', async () => {
  const create=deferred(), paths=[];
  const ui=harness(path=>{paths.push(path); return create.promise;},'');
  const done=ui.send(); await ui.stop(); create.resolve({id:'created'}); await done;
  assert.deepEqual(paths,['/api/conversations']);
  assert.match(ui.rendered[0].text,/已停止生成/);
});

test('reply committed before stop remains a successful reply', async () => {
  const reply=deferred();
  const ui=harness(path=>path.endsWith('/cancel') ? Promise.resolve({status:'completed'}) : reply.promise);
  const done=ui.send(); await ui.stop(); reply.resolve({answer:'已完成'}); await done;
  assert.equal(ui.rendered[0].answer,'已完成');
});

test('failed stop stays pending and permits retry instead of claiming stopped', async () => {
  const reply=deferred();
  const ui=harness(path=>path.endsWith('/cancel') ? Promise.reject(new Error('断网')) : reply.promise);
  const done=ui.send(); await ui.stop(); await tick();
  assert.equal(ui.state().dailyBusy,true);
  assert.equal(ui.$('#daily-stop').disabled,false);
  assert.equal(ui.rendered.length,0);
  reply.resolve({answer:'正常完成'}); await done;
  assert.equal(ui.rendered[0].answer,'正常完成');
});

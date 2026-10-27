const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync(require.resolve('../static/login.js'),'utf8');
function setup(fetch) {
  const nodes = new Map(), navigated=[];
  const get = name => {
    if (!nodes.has(name)) nodes.set(name,{value:'',type:'password',hidden:true,disabled:false,textContent:'',handlers:{},attrs:{},
      addEventListener(event,fn){this.handlers[event]=fn;},setAttribute(k,v){this.attrs[k]=v;},focus(){this.focused=true;}});
    return nodes.get(name);
  };
  get('#username').value='guest'; get('#password').value='secret-test';
  vm.runInNewContext(code,{document:{querySelector:get},fetch,AbortSignal,window:{location:{replace:path=>navigated.push(path)}}});
  return {get,navigated,submit:()=>get('#login-form').handlers.submit({preventDefault(){}})};
}
test('login toggles password visibility without changing its contents',()=>{
  const ui=setup(); ui.get('#password-toggle').handlers.click();
  assert.equal(ui.get('#password').type,'text');
  assert.equal(ui.get('#password-toggle').attrs['aria-pressed'],'true');
  ui.get('#password-toggle').handlers.click();
  assert.equal(ui.get('#password').type,'password');
  assert.equal(ui.get('#password').value,'secret-test');
});
test('login submits credentials in JSON, prevents duplicate send and clears password on success',async()=>{
  let resolve, calls=0;
  const ui=setup((path,options)=>{
    calls++; assert.equal(path,'/api/auth/login');
    assert.equal(JSON.parse(options.body).password,'secret-test');
    return new Promise(r=>resolve=r);
  });
  const pending=ui.submit(); await ui.submit(); assert.equal(calls,1);
  assert.equal(ui.get('#login-submit').disabled,true);
  resolve({ok:true,json:async()=>({ok:true})}); await pending;
  assert.deepEqual(ui.navigated,['/']); assert.equal(ui.get('#password').value,'');
});
test('login displays server error and allows correction without navigating',async()=>{
  const ui=setup(async()=>({ok:false,json:async()=>({error:'账号或密码不正确'})}));
  await ui.submit();
  assert.equal(ui.get('#login-error').hidden,false);
  assert.match(ui.get('#login-error').textContent,/密码不正确/);
  assert.equal(ui.get('#login-submit').disabled,false);
  assert.equal(ui.get('#password').focused,true);
  assert.deepEqual(ui.navigated,[]);
});

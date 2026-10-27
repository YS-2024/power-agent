const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../static/app.js'),'utf8');

test('saved conversation and memory preferences are scoped by account',()=>{
  const storage = new Map();
  const context = vm.createContext({localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)}});
  vm.runInContext('let currentUser={id:"alice"};'+source.slice(source.indexOf('const savedSetting'),source.indexOf('const el =')),context);
  vm.runInContext('savedSetting("power-active-session","alice-private-chat"); currentUser={id:"bob"};',context);
  assert.equal(vm.runInContext('savedSetting("power-active-session")',context),undefined);
  vm.runInContext('savedSetting("power-active-session","bob-private-chat"); currentUser={id:"alice"};',context);
  assert.equal(vm.runInContext('savedSetting("power-active-session")',context),'alice-private-chat');
});

function setup() {
  const nodes = new Map(), calls=[], navigation=[];
  const get = key => {
    if (!nodes.has(key)) nodes.set(key,{value:'',checked:true,disabled:false,textContent:'',hidden:false,children:[],
      append(...items){this.children.push(...items);},replaceChildren(){this.children=[];},reset(){},showModal(){},close(){}});
    return nodes.get(key);
  };
  const context = vm.createContext({$:get,document:{createTextNode:text=>({text})},
    el:(tag,cls,text)=>({tag,text,children:[],append(...items){this.children.push(...items);}}),
    api:async(path,method,body)=>{calls.push({path,method,body});return method==='POST'?{username:body.username||'bob'}:[];},
    toast(){},confirm:()=>true,window:{location:{replace:path=>navigation.push(path)}}});
  vm.runInContext('let currentUser={id:"admin"},passwordTarget=null;'+source.slice(source.indexOf('async function refreshAccounts()'),source.indexOf('async function initialize()')),context);
  return {get,calls,navigation,context};
}
test('admin creates an account with explicit role and public permission, then clears password',async()=>{
  const ui=setup();
  ui.get('#account-name').value='alice'; ui.get('#account-password').value='Alice-password-123';
  ui.get('#account-role').value='member'; ui.get('#account-public').checked=false;
  await ui.get('#account-create-form').onsubmit({preventDefault(){},submitter:{}});
  assert.equal(ui.calls[0].path,'/api/users');
  assert.equal(ui.calls[0].body.username,'alice');
  assert.equal(ui.calls[0].body.can_read_public,false);
  assert.equal(ui.get('#account-password').value,'');
});
test('self password change validates confirmation and sends old password before logging out',async()=>{
  const ui=setup();
  ui.get('#current-password').value='old-password-123';
  ui.get('#new-password').value='new-password-123'; ui.get('#confirm-password').value='different';
  const event={preventDefault(){},submitter:{}};
  await ui.get('#password-form').onsubmit(event);
  assert.equal(ui.calls.length,0);
  ui.get('#confirm-password').value='new-password-123';
  await ui.get('#password-form').onsubmit(event);
  assert.equal(ui.calls[0].path,'/api/auth/password');
  assert.equal(ui.calls[0].body.current_password,'old-password-123');
  assert.deepEqual(ui.navigation,['/login']);
});

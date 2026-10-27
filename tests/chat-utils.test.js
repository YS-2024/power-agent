const {test} = require('node:test');
const assert = require('node:assert/strict');
const chat = require('../static/chat-utils.js');

test('Enter sends; Shift+Enter and IME confirmation do not', () => {
  assert.equal(chat.shouldSend({key:'Enter'}), true);
  assert.equal(chat.shouldSend({key:'Enter', ctrlKey:true}), true);
  for (const event of [{key:'Enter', shiftKey:true}, {key:'Enter', isComposing:true}, {key:'Enter', keyCode:229}, {key:'Enter', repeat:true}, {key:'Enter', altKey:true}, {key:'a'}]) assert.equal(chat.shouldSend(event), false);
  assert.equal(chat.shouldSend({key:'Enter'}, true), false);
});
test('keyboard binding respects composition, busy state and newline default', () => {
  const handlers = {}; let sent = 0, prevented = 0, busy = false;
  chat.bindSend({addEventListener:(name, fn) => {handlers[name] = fn;}}, {requestSubmit:() => {sent++;}}, () => busy);
  const enter = {key:'Enter', preventDefault:() => {prevented++;}};
  handlers.compositionstart(); handlers.keydown(enter); assert.equal(sent, 0);
  handlers.compositionend(); handlers.keydown({...enter, shiftKey:true}); assert.equal(prevented, 0);
  handlers.keydown(enter); assert.equal(sent, 1); assert.equal(prevented, 1);
  busy = true; handlers.keydown(enter); assert.equal(sent, 1); assert.equal(prevented, 2);
});
const pair = i => [{role:'user', content:`question${i}`, images:['a','b','c','d']}, {role:'assistant', content:'answer'}];
test('image budgets drop complete old turns and retain latest images', () => {
  const original = [...pair(1), ...pair(2)];
  const result = chat.context(original, ['new']);
  assert.deepEqual(result, pair(2)); assert.equal(original.length, 4);
  const large = 'x'.repeat(2700000);
  assert.deepEqual(chat.context([{role:'user', content:'q', images:[large,large,large,large]}, {role:'assistant',content:'a'}], [large,large]), []);
});
test('text and turn budgets are independent of images', () => {
  const history = Array.from({length:11}, (_, i) => [{role:'user',content:String(i)},{role:'assistant',content:'ok'}]).flat();
  assert.equal(chat.context(history).length, 20);
  assert.equal(chat.context(history)[0].content, '1');
  assert.deepEqual(chat.context([{role:'user',content:'x'.repeat(24000)},{role:'assistant',content:'ok'}]), []);
});

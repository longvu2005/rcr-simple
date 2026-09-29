// State/async regression tests. No npm dependencies or browser installation required.
// Run: node --test tests/test_ui.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor() {
    this.value = ''; this.textContent = ''; this.children = []; this.options = [];
    this.style = {}; this.dataset = {}; this.hidden = false; this.disabled = false;
    this.classList = { toggle() {}, add() {}, remove() {} };
  }
  addEventListener() {}
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  querySelector() { return this.button ||= new Element(); }
  reset() { this.wasReset = true; }
  remove(index) { this.options.splice(index, 1); }
  getBoundingClientRect() { return { width: 600, height: 400, left: 0, top: 0 }; }
  click() {}
}

function harness() {
  const nodes = new Map(), storage = new Map(), calls = [], pendingTimers = new Map();
  const element = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  let timerId = 0;
  const context = vm.createContext({
    document: { getElementById: element, createElement: () => new Element(),
      createTextNode: text => text, querySelectorAll: () => [], addEventListener() {} },
    window: { addEventListener() {} },
    localStorage: { getItem: key => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    setTimeout: fn => { const id = ++timerId; pendingTimers.set(id, fn); return id; },
    clearTimeout: id => pendingTimers.delete(id),
    URL, URLSearchParams, AbortController, Blob, console, confirm: () => true, alert() {},
    FormData: class { constructor(form) { return Object.entries(form.fields || {}); } },
    fetch: async (url, options) => {
      calls.push({ url, options });
      return { ok: true, status: 200, json: async () => ({}) };
    },
  });
  let source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
  source = source.replace('api("/api/me").then(showApp).catch(() => showLogin());', '');
  vm.runInContext(source, context);
  const run = source => vm.runInContext(source, context);
  const state = run('state');
  state.user = { id: 7, username: 'worker', role: 'ANNOTATOR' }; state.csrf = 'csrf';
  const task = sid => ({ sample_id: sid, revision: 1, status: 'ASSIGNED', case_type: 'INDIVIDUAL',
    candidate_identity_ids: ['1', '2'], initial_subjects: [],
    query: { boxes: [], image_url: '/q.png' }, target: { boxes: [], image_url: '/t.png' } });
  state.task = task('first'); state.current = 'first'; state.queue = [state.task, task('second')];
  run('fillAnnotation({case_type:"INDIVIDUAL",subjects:[{subject_id:1,identity_ids:["1"]}],select_texts:["the person"],target_condition:"Subject 1 is seated"})');
  return { run, context, state, element, storage, calls, task };
}
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
};
const tick = () => new Promise(resolve => setImmediate(resolve));
const reply = data => ({ ok: true, status: 200, json: async () => data });

test('logout waits for unsaved draft before ending the session', async () => {
  const h = harness(), gate = deferred(), order = [];
  h.run('markDirty()');
  h.context.fetch = async (url, options) => {
    order.push(url);
    if (url.endsWith('/draft')) {
      await gate.promise;
      return reply({ task: { ...h.state.task, revision: 2, status: 'IN_PROGRESS' }, annotation: JSON.parse(options.body).annotation });
    }
    return reply({ ok: true });
  };
  const logout = h.element('logout').onclick();
  await tick();
  assert.equal(order.length, 1); assert.ok(order[0].endsWith('/draft'));
  assert.equal(h.element('select1').readOnly, true);
  gate.resolve(); await logout;
  assert.ok(order[1].endsWith('/logout')); assert.equal(h.state.user, null);
  assert.equal(h.storage.has('rcr:draft:7:first'), false);
});

test('failed save keeps the session and browser draft intact', async () => {
  const h = harness(); h.run('markDirty()');
  h.context.fetch = async () => { throw new Error('offline'); };
  await h.element('logout').onclick();
  assert.equal(h.state.user.id, 7); assert.equal(h.state.dirty, true);
  assert.equal(h.storage.has('rcr:draft:7:first'), true);
  assert.equal(h.element('logout').disabled, false);
});

test('switching tasks locks editing until the selected task arrives', async () => {
  const h = harness(), gate = deferred();
  h.context.fetch = async () => { await gate.promise; return reply({ task: h.task('second'), annotation: null }); };
  const load = h.run('switchTask("second")'); await tick();
  assert.equal(h.element('select1').readOnly, true);
  h.run('toggleIdentity("2")'); assert.equal(h.state.assignment.has('2'), false);
  gate.resolve(); await load;
  assert.equal(h.state.current, 'second'); assert.equal(h.element('select1').readOnly, false);
});

test('reload keeps a conflicting local draft until explicit recovery', async () => {
  const h = harness(); h.element('select1').value = 'my unsaved description'; h.run('markDirty()');
  const serverAnnotation = { case_type: 'INDIVIDUAL', subjects: [{subject_id: 1, identity_ids: ['1']}], select_texts: ['server description'], target_condition: 'Subject 1 is standing' };
  h.context.fetch = async () => reply({task: {...h.task('first'), revision: 5}, annotation: serverAnnotation});
  await h.run('loadTask("first")');
  assert.equal(h.element('select1').value, 'server description');
  assert.ok(h.state.recovery); assert.equal(h.element('select1').readOnly, true);
  h.run('restoreLocalDraft()');
  assert.equal(h.element('select1').value, 'my unsaved description');
  assert.equal(h.state.task.revision, 5); assert.equal(h.state.dirty, true);
});

test('expiry clears account state but preserves that user draft', async () => {
  const h = harness(); h.run('markDirty()');
  h.context.fetch = async () => ({ ok: false, status: 401, json: async () => ({error: 'expired'}) });
  await assert.rejects(h.run('flushDraft()'), /expired/);
  assert.equal(h.state.user, null); assert.equal(h.state.task, null);
  assert.equal(h.state.queue.length, 0); assert.equal(h.storage.has('rcr:draft:7:first'), true);
});

test('edits made while a save is pending are included in the next save', async () => {
  const h = harness(), gate = deferred(), payloads = [];
  h.context.fetch = async (url, options) => {
    const payload = JSON.parse(options.body); payloads.push(payload);
    if (payloads.length === 1) await gate.promise;
    return reply({task: {...h.task('first'), revision: payload.expected_revision + 1, status:'IN_PROGRESS'}, annotation: payload.annotation});
  };
  h.run('markDirty()'); const saving = h.run('flushDraft()'); await tick();
  h.element('select1').value = 'newer text'; h.run('markDirty()');
  gate.resolve(); await saving;
  assert.equal(payloads.length, 2); assert.equal(payloads[1].expected_revision, 2);
  assert.equal(payloads[1].annotation.select_texts[0], 'newer text'); assert.equal(h.state.dirty, false);
});

test('user creation retains the form across await (event.currentTarget expires)', async () => {
  const h = harness(), gate = deferred(), form = h.element('createUser');
  form.fields = { username: 'new-user', password: 'password' };
  h.context.fetch = async url => {
    if (url === '/api/admin/users' && !gate.done) { await gate.promise; gate.done = true; return reply({ok:true}); }
    return reply({users: []});
  };
  const event = {currentTarget: form, preventDefault() {}};
  const pending = form.onsubmit(event); event.currentTarget = null;
  gate.resolve(); await pending;
  assert.equal(form.wasReset, true); assert.equal(form.querySelector().disabled, false);
  assert.match(h.element('adminMessage').textContent, /Đã tạo/);
});

test('missing identities cannot be assigned; blocked localStorage does not break task loading', async () => {
  const h = harness(); h.run('toggleIdentity("unknown")'); assert.equal(h.state.assignment.has('unknown'), false);
  h.context.localStorage.getItem = () => { throw new Error('blocked'); };
  h.context.localStorage.setItem = () => { throw new Error('blocked'); };
  h.context.fetch = async () => reply({task: h.task('second'), annotation:null});
  await h.run('loadTask("second")'); assert.equal(h.state.current, 'second');
  h.run('markDirty()'); assert.equal(h.state.storageWarning, true);
});

test('a stale request from the previous login cannot update the new session', async () => {
  const h = harness(), gate = deferred();
  h.context.fetch = async () => { await gate.promise; return reply({ tasks: [h.task('first')] }); };
  const pending = h.run('api("/api/work/tasks")'); h.run('showLogin()');
  gate.resolve(); await assert.rejects(pending, /Phiên đăng nhập/);
});

test('HTML/error responses are reported instead of being treated as success', async () => {
  const h = harness(); h.context.fetch = async () => ({ok:true,status:200,json:async()=>{throw new Error('html');}});
  await assert.rejects(h.run('api("/api/work/tasks")'), /Phản hồi server/);
});

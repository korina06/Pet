/*
 * 用最小 DOM 桩跑 app.js，验证邮箱窗口：登入、分组列表、搜索、星标、切文件夹、退出与 401 回登录页。
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const appJs = fs.readFileSync(path.join(ROOT, 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(ROOT, 'static', 'index.html'), 'utf8');

/* ------------------------------------------------------------ DOM 桩 */

/** 当前焦点在哪个节点上。
 *
 *  以前桩里没有焦点概念（`focus()` 只设了个 `_focused` 布尔），
 *  于是"重建之后焦点有没有回到原处"**根本没法测**——
 *  而第 61 步要验证的正是这件事，所以这里补一个真的 `document.activeElement`。
 */
let activeFocusNode = null;

function makeElement(id, tag) {
  const classes = new Set();
  let ownText = '';
  const node = {
    id,
    tagName: (tag || 'div').toUpperCase(),
    hidden: false,
    value: '',
    title: '',
    disabled: false,
    // 滚动几何：桩必须给初值，否则 app.js 里的算术会得到 NaN
    // （NaN 比较永远为 false，于是"滚到底自动加载"在桩里静默失效、测不出来）
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
    children: [],
    dataset: {},
    style: { setProperty() {}, removeProperty() {} },
    attributes: {},
    _handlers: {},
  };
  // textContent 要像浏览器那样把子节点的文字也算进来，否则
  // "按钮里套了个 <span>文字</span>" 读出来是空字符串——测出来的是桩的毛病，
  // 不是 app.js 的毛病。（赋值时按规范清空子节点，也照抄。）
  Object.defineProperty(node, 'textContent', {
    get: () => {
      if (!node.children.length) return ownText;
      return ownText + node.children.map((child) => child.textContent).join('');
    },
    set: (value) => {
      ownText = String(value);
      node.children = [];
    },
  });
  // className 与 classList 共用同一份集合：浏览器里两者是同一件事，
  // 桩里要是各存一份，classList.add 之后再读 className 就看不到（测不准）。
  Object.defineProperty(node, 'className', {
    get: () => [...classes].join(' '),
    set: (value) => {
      classes.clear();
      String(value || '').split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
    },
  });
  node.classList = {
    add: (c) => classes.add(c),
    remove: (c) => classes.delete(c),
    contains: (c) => classes.has(c),
    toggle: (c, force) => {
      const want = force === undefined ? !classes.has(c) : Boolean(force);
      if (want) classes.add(c); else classes.delete(c);
      return want;
    },
    _set: classes,
  };
  node.appendChild = (child) => {
    node.children.push(child);
    if (child) child.parentNode = node;
    return child;
  };
  node.append = (...items) => {
    items.forEach((child) => {
      node.children.push(child);
      if (child) child.parentNode = node;
    });
  };
  node.replaceChildren = (...items) => {
    node.children = [...items];
    node.children.forEach((child) => { if (child) child.parentNode = node; });
    // 浏览器行为：把滚动容器的内容清空，内容高度塌成 0，scrollTop 会被夹到 0。
    // 桩里**必须**模拟这一条：不模拟的话，"重建之后有没有把滚动位置恢复回来"
    // 就根本测不出来 —— 把 app.js 里那行恢复代码删掉，测试照样全绿。
    // （第 61 步做变异测试时就是这么发现的：第一版场景 17 是假通过。）
    if (node.children.length === 0) node.scrollTop = 0;
  };
  node.removeChild = (child) => {
    const at = node.children.indexOf(child);
    if (at >= 0) node.children.splice(at, 1);
    if (child) child.parentNode = null;
    return child;
  };
  node.remove = () => { if (node.parentNode) node.parentNode.removeChild(node); };
  node.insertBefore = (child, ref) => {
    const at = ref ? node.children.indexOf(ref) : -1;
    if (at < 0) node.children.push(child);
    else node.children.splice(at, 0, child);
    return child;
  };
  node.addEventListener = (type, handler) => {
    (node._handlers[type] = node._handlers[type] || []).push(handler);
  };
  node.removeEventListener = () => {};
  node.setAttribute = (name, value) => { node.attributes[name] = String(value); };
  node.getAttribute = (name) => (name in node.attributes ? node.attributes[name] : null);
  node.focus = () => {
    node._focused = true;
    activeFocusNode = node;             // 浏览器里这叫 document.activeElement
  };
  node.blur = () => {
    if (activeFocusNode === node) activeFocusNode = null;
  };
  /** 和浏览器一样：自己或自己的后代是否包含 child。 */
  node.contains = (child) => {
    let current = child || null;
    while (current) {
      if (current === node) return true;
      current = current.parentNode || null;
    }
    return false;
  };
  /** 够用的 closest：只认 `.class` 选择器（本脚本只用到这种）。 */
  node.closest = (selector) => {
    const want = String(selector);
    let current = node;
    while (current) {
      if (want.startsWith('.')
          && current.classList
          && current.classList.contains(want.slice(1))) {
        return current;
      }
      current = current.parentNode || null;
    }
    return null;
  };
  node.click = (target) => {
    (node._handlers.click || []).forEach((h) =>
      h({ preventDefault() {}, target: target || node }));
  };
  node.submit = () => {
    (node._handlers.submit || []).forEach((h) => h({ preventDefault() {} }));
  };
  node.fire = (type, event) => {
    (node._handlers[type] || []).forEach((h) =>
      h(Object.assign({ preventDefault() {}, target: node }, event || {})));
  };
  node.querySelectorAll = () => [];
  node.querySelector = () => null;
  node.getBoundingClientRect = () =>
    ({ left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 });
  node.setPointerCapture = () => {};
  node.releasePointerCapture = () => {};
  return node;
}

/** 按 index.html 里出现的 id 造一份全新的 DOM 桩。 */
function makeDom() {
  activeFocusNode = null;               // 每份新 DOM 都从"没有焦点"开始
  const byId = new Map();
  for (const match of html.matchAll(/id="([^"]+)"/g)) {
    const id = match[1];
    const tagMatch = html.match(new RegExp(`<([a-zA-Z]+)[^>]*id="${id}"`));
    const node = makeElement(id, tagMatch ? tagMatch[1] : 'div');

    // 让桩贴近浏览器：开标签上带 hidden 就应当是隐藏的
    const start = html.lastIndexOf('<', html.indexOf(`id="${id}"`));
    const end = html.indexOf('>', html.indexOf(`id="${id}"`));
    const fullTag = start >= 0 && end > start ? html.slice(start, end + 1) : '';
    if (/\shidden(\s|>|=)/.test(fullTag)) node.hidden = true;

    byId.set(id, node);
  }

  const local = new Map();
  const session = new Map();
  const documentStub = {
    getElementById: (id) => byId.get(id) || null,
    createElement: (tag) => makeElement('', tag),
    createTextNode: (text) => {
      const node = makeElement('', '#text');
      node.textContent = String(text);
      return node;
    },
    addEventListener: (type, handler) => {
      (documentStub._handlers[type] = documentStub._handlers[type] || []).push(handler);
    },
    _handlers: {},
    documentElement: { style: { setProperty() {} } },
    body: makeElement('body', 'body'),
  };
  // activeElement 是只读属性，用 getter 暴露当前焦点
  Object.defineProperty(documentStub, 'activeElement', {
    get: () => activeFocusNode,
  });
  documentStub.pressEscape = () => {
    (documentStub._handlers.keydown || []).forEach((h) =>
      h({ key: 'Escape', preventDefault() {} }));
  };
  documentStub.querySelectorAll = () => [];

  const windowStub = {
    innerWidth: 1280,
    innerHeight: 800,
    addEventListener: () => {},
    setTimeout: (fn) => { fn(); return 0; },
  // 第 151 步：待机动画用 setInterval；桩里同样立即执行
  setInterval: (fn) => { fn(); return 0; },
  clearInterval: () => {},
  matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    clearTimeout: () => {},
    requestAnimationFrame: (fn) => { fn(); return 0; },
    getComputedStyle: () => ({
      getPropertyValue: (n) => (n === '--pet-w' ? '120px' : n === '--pet-h' ? '80px' : ''),
    }),
    localStorage: {
      getItem: (k) => (local.has(k) ? local.get(k) : null),
      setItem: (k, v) => local.set(k, v),
      removeItem: (k) => local.delete(k),
    },
    crypto: { randomUUID: () => 'uuid' },
    sessionStorage: {
      getItem: (k) => (session.has(k) ? session.get(k) : null),
      setItem: (k, v) => session.set(k, v),
      removeItem: (k) => session.delete(k),
    },
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
  };

  return { byId, documentStub, windowStub };
}

/* ------------------------------------------------------------ 后端桩 */

const PROVIDERS = [{
  key: 'qq',
  label: 'QQ 邮箱',
  host: 'imap.qq.com',
  port: 993,
  help_text: 'QQ 邮箱要填的不是 QQ 密码，而是「授权码」……',
  domains: ['qq.com', 'foxmail.com'],
}];

const FOLDERS = [
  { key: 'inbox', label: '收件箱', mailbox: 'INBOX', unread: 2, total: 109 },
  { key: 'flagged', label: '星标邮件', mailbox: null, unread: 0, total: 0 },
  { key: 'drafts', label: '草稿箱', mailbox: 'Drafts', unread: 0, total: 1 },
  { key: 'trash', label: '垃圾桶', mailbox: 'Deleted Messages', unread: 0, total: 0 },
];

function message(uid, subject, iso, extra) {
  return Object.assign({
    uid: String(uid),
    mailbox: 'INBOX',
    folder: 'inbox',
    from_name: '张三',
    from_address: 'zhang@qq.com',
    subject,
    snippet: '这是正文预览',
    received: iso,
    unread: true,
    flagged: false,
  }, extra || {});
}

/** 造一批"相对今天"的邮件，好稳定地测出分组。 */
function buildMessages() {
  const now = new Date();
  const at = (daysAgo, hour) => {
    const when = new Date(now.getFullYear(), now.getMonth(), now.getDate() - daysAgo, hour, 30);
    return when.toISOString();
  };
  return [
    message(9, '今天的信', at(0, 9)),
    message(8, '昨天的信', at(1, 9)),
    message(7, '三天前的信', at(3, 9), { unread: false }),
    message(6, '十天前的信', at(10, 9)),
    message(5, '很旧的信', at(40, 9)),
  ];
}

/** 造 count 封"一天一封、越往后越早"的邮件，用来测翻页。 */
function buildManyMessages(count) {
  const now = new Date();
  const rows = [];
  for (let i = 0; i < count; i += 1) {
    const when = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i, 12, 0);
    rows.push(message(String(5000 - i), `第${i + 1}封`, when.toISOString()));
  }
  return rows;
}

const backend = {
  loggedIn: false,
  loginFails: null,          // 非 null 时 /login 返回 400
  authorized: true,          // false 时读接口返回 401
  statusFails: false,
  flags: {},                 // uid -> flagged
  messages: buildMessages(),
  calls: [],
};

/** 某个文件夹里"服务器上"有哪些邮件。
 *
 *  **收件箱以外的文件夹刻意给一批完全不同的 UID**：不然"邮件串到别的文件夹界面上"
 *  这种 bug 在断言里看起来是一样的，测试就白写了（第 36 步的教训）。
 */
function messagesForFolder(key) {
  if (key === 'inbox') return backend.messages;
  if (key === 'flagged') return backend.messages.filter((m) => backend.flags[m.uid]);
  return backend.messages.map((m, index) => message(
    String(7000 + index),
    `${key} 里的第${index + 1}封`,
    m.received,
    { mailbox: key.toUpperCase(), folder: key },
  ));
}

function backendFetch(url, options) {
  const method = (options && options.method) || 'GET';
  const text = String(url);
  backend.calls.push({ method, url: text, body: options && options.body });

  const json = (status, payload) => Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(payload),
  });

  if (text.includes('/api/tools')) return json(200, { tools: [] });

  if (text.includes('/api/mail/providers')) {
    return json(200, { providers: PROVIDERS, default: 'qq' });
  }

  if (text.includes('/api/mail/login')) {
    if (backend.loginFails) return json(400, { detail: backend.loginFails });
    backend.loggedIn = true;
    return json(200, {
      address: '12345@qq.com', provider: 'qq', provider_label: 'QQ 邮箱',
      host: 'imap.qq.com', logged_in: true,
    });
  }

  if (text.includes('/api/mail/logout')) {
    backend.loggedIn = false;
    return json(200, { status: 'logged_out', session_id: 'uuid' });
  }

  if (text.includes('/api/mail/status')) {
    if (backend.statusFails) return json(500, { detail: '炸了' });
    return json(200, backend.loggedIn
      ? { logged_in: true, account: { address: '12345@qq.com', provider: 'qq',
          provider_label: 'QQ 邮箱', host: 'imap.qq.com' } }
      : { logged_in: false, account: null });
  }

  if (text.includes('/api/mail/folders')) {
    if (!backend.authorized) return json(401, { detail: '尚未登入邮箱，请先添加邮箱' });
    const rows = FOLDERS.map((f) => ({
      ...f,
      unread: f.key === 'inbox' ? 2 : 0,
      total: f.key === 'inbox' ? 109 : f.total,
    }));
    return json(200, { folders: rows });
  }

  if (text.includes('/api/mail/check')) {
    if (!backend.authorized) return json(401, { detail: '尚未登入邮箱，请先添加邮箱' });
    const folder = /folder=([^&]*)/.exec(text);
    const key = folder ? decodeURIComponent(folder[1]) : 'inbox';
    const since = Number((/since_uid=(\d+)/.exec(text) || [])[1] || 0);
    const all = messagesForFolder(key);

    if (key === 'flagged') {
      return json(200, {
        folder: key, messages: all.slice(0, 50), total: all.length,
        unread: 0, reset: true,
      });
    }
    const fresh = all.filter((m) => Number(m.uid) > since);
    return json(200, {
      folder: key, messages: fresh, total: all.length,
      unread: all.filter((m) => m.unread).length, reset: false,
    });
  }

  if (text.includes('/api/mail/messages')) {
    if (!backend.authorized) return json(401, { detail: '尚未登入邮箱，请先添加邮箱' });
    const folder = /folder=([^&]*)/.exec(text);
    const key = folder ? decodeURIComponent(folder[1]) : 'inbox';
    const limit = Number((/limit=(\d+)/.exec(text) || [])[1] || 50);
    const offset = Number((/offset=(\d+)/.exec(text) || [])[1] || 0);

    const all = messagesForFolder(key);
    const page = all.slice(offset, offset + limit);
    return json(200, {
      folder: key,
      messages: page,
      total: all.length,
      has_more: offset + page.length < all.length,
    });
  }

  if (text.includes('/api/mail/flag')) {
    if (!backend.authorized) return json(401, { detail: '尚未登入邮箱，请先添加邮箱' });
    const body = JSON.parse((options && options.body) || '{}');
    backend.flags[body.uid] = body.flagged;
    return json(200, { uid: body.uid, flagged: body.flagged });
  }

  return json(200, {});
}

/** 每次 boot 都用一份新 DOM + 重置后端场景。
 *
 *  `fetchImpl` 可以用来替换取数实现（比如让某个请求一直挂起，
 *  好复现"响应迟到"的竞态）。
 */
function boot(scenario, fetchImpl) {
  Object.assign(backend, {
    loggedIn: false,
    loginFails: null,
    authorized: true,
    statusFails: false,
    flags: {},
    messages: buildMessages(),
    calls: [],
  }, scenario || {});

  const dom = makeDom();
  new Function(
    'document', 'window', 'fetch', 'localStorage', 'sessionStorage',
    'AbortController', 'requestAnimationFrame', 'getComputedStyle', 'crypto',
    appJs
  )(
    dom.documentStub, dom.windowStub,
    fetchImpl || backendFetch,
    dom.windowStub.localStorage, dom.windowStub.sessionStorage,
    dom.windowStub.AbortController, dom.windowStub.requestAnimationFrame,
    dom.windowStub.getComputedStyle, dom.windowStub.crypto
  );
  return dom;
}

/* ------------------------------------------------------------ 断言 */

let failures = 0;
function check(label, ok, detail) {
  if (!ok) failures += 1;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? '  |  ' + detail : ''}`);
}
function finish() {
  console.log(`\n失败 ${failures} 项`);
  process.exitCode = failures ? 1 : 0;
}

/** 用 Node 的真定时器排空微任务队列。
 *
 *  app.js 里那些 async 函数（prepareMail / loadMailFolders / …）会连着 await
 *  好几层 fetch → json；`await null` 数次数太脆，而**真** setTimeout 的回调
 *  一定在微任务队列清空之后才跑，所以等一次就够稳。
 *  （window.setTimeout 被桩成同步执行，那是给 app.js 用的，不影响这里。）
 */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

function rowsOf(list) {
  return list.children.filter((n) => n.classList && n.classList.contains('mail-item'));
}
function headingsOf(list) {
  return list.children.filter((n) => n.classList && n.classList.contains('mail-group'));
}
function starOf(row) {
  return row.children[row.children.length - 1];
}
function folderButton(rail, key) {
  return rail.children.find((n) => n.dataset && n.dataset.key === key) || null;
}
function textsOf(nodes) {
  return nodes.map((n) => n.textContent);
}

/* ------------------------------------------------------------ 各场景 */

/** 打开邮箱窗口（点胶囊），返回各元素。 */
async function openMailWindow(dom) {
  const chips = dom.byId.get('chat-chips');
  const mail = chips.children.find((c) => c.dataset.key === 'mail');
  mail.click();
  await settle();
  return dom.byId.get('mail-modal');
}

async function scenarioDefaults() {
  console.log('\n==== 1. 打开窗口：默认停在「添加邮箱」页 ====');
  const dom = boot();
  const modal = await openMailWindow(dom);

  const loginPage = dom.byId.get('mail-page-login');
  const inboxPage = dom.byId.get('mail-page-inbox');

  check('邮箱窗口已打开', modal.hidden === false, `hidden=${modal.hidden}`);
  check('停在「添加邮箱」页',
    loginPage.hidden === false && inboxPage.hidden === true,
    `login=${loginPage.hidden} inbox=${inboxPage.hidden}`);
  check('收件箱那批元素默认不显示',
    dom.byId.get('mail-list').children.length === 0);

  // 服务商提示：空输入时显示当前选中的服务商
  check('空输入时提示当前服务商',
    dom.byId.get('mail-provider-hint').textContent.includes('QQ 邮箱'),
    `hint=${dom.byId.get('mail-provider-hint').textContent}`);

  // 输入 QQ 邮箱 → 识别出来
  const address = dom.byId.get('mail-address');
  address.value = '12345@qq.com';
  address.fire('input');
  check('输入 QQ 邮箱后显示已识别',
    dom.byId.get('mail-provider-hint').textContent.includes('已识别')
    && dom.byId.get('mail-provider-hint').textContent.includes('imap.qq.com'),
    `hint=${dom.byId.get('mail-provider-hint').textContent}`);

  // 输入别家邮箱 → 明说暂不支持
  address.value = 'someone@gmail.com';
  address.fire('input');
  check('输入别家邮箱时明说只支持 QQ',
    dom.byId.get('mail-provider-hint').textContent.includes('暂时') ||
    dom.byId.get('mail-provider-hint').textContent.includes('只能登入'),
    `hint=${dom.byId.get('mail-provider-hint').textContent}`);
}

async function scenarioValidation() {
  console.log('\n==== 2. 表单校验：不该发请求就别发 ====');
  const dom = boot();
  await openMailWindow(dom);

  const form = dom.byId.get('mail-form');
  const address = dom.byId.get('mail-address');
  const secret = dom.byId.get('mail-secret');
  const error = dom.byId.get('mail-error');
  const loginCalls = () => backend.calls.filter((c) => c.url.includes('/api/mail/login'));

  // 什么都不填
  form.submit();
  await settle();
  check('空表单给出提示且不请求', error.hidden === false && loginCalls().length === 0,
    `hidden=${error.hidden} calls=${loginCalls().length}`);

  // 账号不完整
  address.value = '12345';
  secret.value = 'abcdef';
  form.submit();
  await settle();
  check('账号不完整时提示要写完整', error.textContent.includes('@'),
    `text=${error.textContent}`);
  check('账号不完整时不请求', loginCalls().length === 0);

  // 缺授权码
  address.value = '12345@qq.com';
  secret.value = '   ';
  form.submit();
  await settle();
  check('缺授权码时提示授权码', error.textContent.includes('授权码'),
    `text=${error.textContent}`);
  check('缺授权码时不请求', loginCalls().length === 0);
}

async function scenarioLoginAndList() {
  console.log('\n==== 3. 登入成功 → 收件箱列表 ====');
  const dom = boot();
  await openMailWindow(dom);

  const form = dom.byId.get('mail-form');
  const address = dom.byId.get('mail-address');
  const secret = dom.byId.get('mail-secret');

  address.value = '12345@qq.com';
  secret.value = 'abcdef123456';
  form.submit();
  await settle();
  await settle();

  const loginCall = backend.calls.find((c) => c.url.includes('/api/mail/login'));
  check('真的发了登入请求', Boolean(loginCall), `calls=${backend.calls.length}`);
  check('登入请求带上了账号与授权码',
    Boolean(loginCall) && loginCall.body.includes('12345@qq.com')
    && loginCall.body.includes('abcdef123456'));

  check('切到了收件箱页',
    dom.byId.get('mail-page-inbox').hidden === false
    && dom.byId.get('mail-page-login').hidden === true,
    `inbox=${dom.byId.get('mail-page-inbox').hidden}`);
  check('授权码框已清空（不留在 DOM 里）', secret.value === '',
    `value=${secret.value}`);
  check('左下角显示当前账号',
    dom.byId.get('mail-account').textContent === '12345@qq.com',
    `text=${dom.byId.get('mail-account').textContent}`);

  // 文件夹
  const rail = dom.byId.get('mail-folders');
  // 取每个按钮的**第一个子节点**（文字标签）：按钮整体的 textContent
  // 会把后面的未读数字也拼进来（"收件箱2"），那是浏览器的正常行为。
  const folderLabels = rail.children.map((n) => n.children[0].textContent);
  check('渲染了四个能真实取到的文件夹',
    JSON.stringify(folderLabels) ===
      JSON.stringify(['收件箱', '星标邮件', '草稿箱', '垃圾桶']),
    `labels=${JSON.stringify(folderLabels)}`);
  check('未读数显示在收件箱上',
    folderButton(rail, 'inbox').children.some((c) => c.textContent === '2'));
  check('收件箱是默认选中项',
    folderButton(rail, 'inbox').classList.contains('is-active'));

  // 列表与分组
  const list = dom.byId.get('mail-list');
  const rows = rowsOf(list);
  const headings = headingsOf(list);
  check('列表渲染出全部邮件', rows.length === 5, `rows=${rows.length}`);
  check('最新在前', rows.length > 1 && rows[0].dataset.uid === '9',
    `first=${rows[0] && rows[0].dataset.uid}`);
  check('分组标题带条数',
    headings.length > 0 && /^.+ \(\d+封\)$/.test(headings[0].textContent),
    `headings=${JSON.stringify(textsOf(headings))}`);
  check('分组里有「今天」「昨天」',
    textsOf(headings).some((t) => t.startsWith('今天')) &&
    textsOf(headings).some((t) => t.startsWith('昨天')),
    `headings=${JSON.stringify(textsOf(headings))}`);
  check('很久以前的归到「更早」',
    textsOf(headings).some((t) => t.startsWith('更早')),
    `headings=${JSON.stringify(textsOf(headings))}`);
  check('邮箱接口取数时不显示占位提示', dom.byId.get('mail-state').hidden === true,
    `state=${dom.byId.get('mail-state').textContent}`);
}

async function scenarioSearch() {
  console.log('\n==== 4. 搜索框只在已加载的那批里筛 ====');
  const dom = boot({ loggedIn: true });
  await openMailWindow(dom);
  await settle();

  const list = dom.byId.get('mail-list');
  const search = dom.byId.get('mail-search');
  const before = rowsOf(list).length;

  search.value = '昨天的信';
  search.fire('input');
  check('匹配到的只剩一行', rowsOf(list).length === 1,
    `rows=${rowsOf(list).length}（筛前 ${before}）`);

  search.value = '张三';
  search.fire('input');
  check('发件人也能搜到', rowsOf(list).length === before,
    `rows=${rowsOf(list).length}`);

  search.value = '根本不存在的东西';
  search.fire('input');
  check('搜不到时说清楚是在"已加载的"里没找到',
    rowsOf(list).length === 0
    && dom.byId.get('mail-state').textContent.includes('已加载'),
    `state=${dom.byId.get('mail-state').textContent}`);

  search.value = '';
  search.fire('input');
  check('清空搜索后全部回来', rowsOf(list).length === before);
}

async function scenarioStar() {
  console.log('\n==== 5. 星标：点一下真的改 IMAP 标记 ====');
  const dom = boot({ loggedIn: true });
  await openMailWindow(dom);
  await settle();

  const list = dom.byId.get('mail-list');
  const rail = dom.byId.get('mail-folders');
  const firstRow = rowsOf(list)[0];
  const star = starOf(firstRow);

  check('默认未加星', star.textContent === '☆', `star=${star.textContent}`);
  star.click();
  await settle();

  const flagCall = backend.calls.find((c) => c.url.includes('/api/mail/flag'));
  check('发了星标请求', Boolean(flagCall));
  check('请求带上真实邮箱名和 uid',
    Boolean(flagCall) && flagCall.body.includes('INBOX')
    && flagCall.body.includes(firstRow.dataset.uid),
    `body=${flagCall && flagCall.body}`);

  const starred = rowsOf(list)[0];
  check('星标变成实心', starOf(starred).textContent === '★',
    `star=${starOf(starred).textContent}`);

  // 切到「星标邮件」：刚才那封应当出现
  folderButton(rail, 'flagged').click();
  await settle();
  await settle();
  check('「星标邮件」里有刚加星的那封',
    rowsOf(list).length === 1 && rowsOf(list)[0].dataset.uid === firstRow.dataset.uid,
    `rows=${rowsOf(list).length}`);

  // 在「星标邮件」里取消星标 → 该行立即消失
  starOf(rowsOf(list)[0]).click();
  await settle();
  check('在星标文件夹里取消后该行消失', rowsOf(list).length === 0,
    `rows=${rowsOf(list).length}`);
}

async function scenarioFolders() {
  console.log('\n==== 6. 切换文件夹会重新拉取 ====');
  const dom = boot({ loggedIn: true });
  await openMailWindow(dom);
  await settle();

  const rail = dom.byId.get('mail-folders');
  const before = backend.calls.filter((c) => c.url.includes('/api/mail/messages')).length;

  folderButton(rail, 'drafts').click();
  await settle();
  await settle();

  const after = backend.calls.filter((c) => c.url.includes('/api/mail/messages'));
  check('切文件夹触发了新请求', after.length === before + 1,
    `before=${before} after=${after.length}`);
  check('请求带上了新的 folder', after[after.length - 1].url.includes('folder=drafts'),
    `url=${after[after.length - 1].url}`);
  check('选中态跟着切过去',
    folderButton(rail, 'drafts').classList.contains('is-active')
    && !folderButton(rail, 'inbox').classList.contains('is-active'));
}

async function scenarioServices() {
  console.log('\n==== 7.「选择邮箱服务」展开 / 收起 ====');
  const dom = boot();
  await openMailWindow(dom);

  const services = dom.byId.get('mail-services');
  const toggle = dom.byId.get('mail-service-toggle');
  check('默认收起', services.hidden === true, `hidden=${services.hidden}`);

  toggle.click();
  check('点一下展开', services.hidden === false, `hidden=${services.hidden}`);
  check('展开后列出了 QQ 邮箱', services.children.length > 0
    && services.children[0].textContent.includes('QQ 邮箱'),
    `count=${services.children.length}`);
  check('带上了服务器地址', services.children[0].textContent.includes('imap.qq.com'));
  check('说明里写了去哪拿授权码',
    textsOf(services.children).some((t) => t.includes('授权码')),
    `texts=${JSON.stringify(textsOf(services.children))}`);
  check('aria-expanded 跟着变',
    toggle.getAttribute('aria-expanded') === 'true',
    `aria=${toggle.getAttribute('aria-expanded')}`);

  toggle.click();
  check('再点一下收起', services.hidden === true, `hidden=${services.hidden}`);
}

async function scenarioLogout() {
  console.log('\n==== 8. 退出 → 回到登入页 ====');
  const dom = boot({ loggedIn: true });
  await openMailWindow(dom);
  await settle();
  check('已登入时直接进收件箱',
    dom.byId.get('mail-page-inbox').hidden === false,
    `inbox=${dom.byId.get('mail-page-inbox').hidden}`);

  dom.byId.get('mail-logout').click();
  await settle();

  check('发了 logout 请求',
    backend.calls.some((c) => c.url.includes('/api/mail/logout')));
  check('回到「添加邮箱」页',
    dom.byId.get('mail-page-login').hidden === false
    && dom.byId.get('mail-page-inbox').hidden === true,
    `login=${dom.byId.get('mail-page-login').hidden}`);
  check('列表与文件夹被清空',
    dom.byId.get('mail-list').children.length === 0
    && dom.byId.get('mail-folders').children.length === 0);
  check('账号显示已清空', dom.byId.get('mail-account').textContent === '');
}

async function scenarioUnauthorized() {
  console.log('\n==== 9. 会话失效（401）→ 退回登入页并说明原因 ====');
  const dom = boot({ loggedIn: true, authorized: false });
  await openMailWindow(dom);
  await settle();

  check('退回「添加邮箱」页',
    dom.byId.get('mail-page-login').hidden === false,
    `login=${dom.byId.get('mail-page-login').hidden}`);
  check('说明了为什么（服务重启会清空）',
    dom.byId.get('mail-error').textContent.includes('失效'),
    `error=${dom.byId.get('mail-error').textContent}`);
}

async function scenarioLoginFailure() {
  console.log('\n==== 10. 登入失败：显示后端给的中文原因，留在原页 ====');
  const dom = boot({ loginFails: '账号或授权码不正确。请确认填的是 16 位授权码。' });
  await openMailWindow(dom);

  dom.byId.get('mail-address').value = '12345@qq.com';
  dom.byId.get('mail-secret').value = 'wrong';
  dom.byId.get('mail-form').submit();
  await settle();
  await settle();

  check('留在「添加邮箱」页',
    dom.byId.get('mail-page-login').hidden === false,
    `login=${dom.byId.get('mail-page-login').hidden}`);
  check('原样显示后端给的原因',
    dom.byId.get('mail-error').textContent.includes('授权码'),
    `text=${dom.byId.get('mail-error').textContent}`);
  check('按钮恢复可点（不能一直卡在"正在连接"）',
    dom.byId.get('mail-submit').disabled === false
    && dom.byId.get('mail-submit').textContent === '添加',
    `disabled=${dom.byId.get('mail-submit').disabled} text=${dom.byId.get('mail-submit').textContent}`);
}

async function scenarioSameWindowAndEsc() {
  console.log('\n==== 11. 登入前后同一窗口 + 关闭方式 ====');
  const dom = boot();
  const modal = await openMailWindow(dom);

  // 登入前后都在同一个 #mail-modal 里：全程只有一个窗口元素
  check('只有窗口里的两页在切换（没有新开窗口）',
    dom.byId.get('mail-page-login') !== null
    && dom.byId.get('mail-page-inbox') !== null);

  dom.documentStub.pressEscape();
  check('Esc 能关掉邮箱窗口', modal.hidden === true, `hidden=${modal.hidden}`);

  await openMailWindow(dom);
  dom.byId.get('mail-close').click();
  check('× 能关掉邮箱窗口', modal.hidden === true, `hidden=${modal.hidden}`);

  await openMailWindow(dom);
  modal.click(modal);
  check('点遮罩能关掉', modal.hidden === true, `hidden=${modal.hidden}`);

  await openMailWindow(dom);
  modal.click(dom.byId.get('mail-page-login'));
  check('点窗口内部不关', modal.hidden === false, `hidden=${modal.hidden}`);
}

/* ------------------------------------------------------------ 跑 */

async function scenarioPaging() {
  console.log('\n==== 12. 滚到底自动加载历史邮件（分页）====');
  const dom = boot({ loggedIn: true, messages: buildManyMessages(120) });
  await openMailWindow(dom);
  await settle();
  await settle();

  const list = dom.byId.get('mail-list');
  const pages = () => backend.calls.filter((c) => c.url.includes('/api/mail/messages'));
  const uids = () => rowsOf(list).map((row) => row.dataset.uid);
  const footText = () => {
    const foot = list.children.find((n) => n.classList.contains('mail-more'));
    return foot ? foot.textContent : '(没有)';
  };

  check('第一页只取 50 封', rowsOf(list).length === 50, `rows=${rowsOf(list).length}`);
  check('第一页带了 offset=0', pages()[0].url.includes('offset=0'),
    `url=${pages()[0].url}`);
  check('底部提示还能继续往下', footText().includes('继续往下滚动'),
    `foot=${footText()}`);

  // 模拟"滚到接近底部"：给桩一个真实的滚动几何，再发 scroll 事件
  list.scrollHeight = 2600;
  list.clientHeight = 600;
  list.scrollTop = 2600;

  list.fire('scroll');
  await settle();
  await settle();

  check('自动发出了第二页请求', pages().length === 2, `pages=${pages().length}`);
  check('第二页带 offset=50', pages()[1].url.includes('offset=50'),
    `url=${pages()[1].url}`);
  check('列表接上了第二页（100 行）', rowsOf(list).length === 100,
    `rows=${rowsOf(list).length}`);
  check('两页之间没有重复', new Set(uids()).size === 100,
    `unique=${new Set(uids()).size}`);
  check('顺序仍然是最新在前（第一页的行没被挪走）', uids()[0] === '5000',
    `first=${uids()[0]}`);

  // 没滚到底时不该重复请求
  list.scrollTop = 0;
  list.fire('scroll');
  await settle();
  check('没到底时不乱发请求', pages().length === 2, `pages=${pages().length}`);

  // 再滚到底 → 第三页，取完 120 封
  list.scrollTop = 2600;
  list.fire('scroll');
  await settle();
  await settle();
  check('第三页带 offset=100', pages()[2].url.includes('offset=100'),
    `url=${pages()[2].url}`);
  check('列表变成 120 行', rowsOf(list).length === 120, `rows=${rowsOf(list).length}`);
  check('到底后提示「已经到底了」', footText().includes('已经到底了'),
    `foot=${footText()}`);

  // 已经到底了，再怎么滚都不该再发请求
  list.fire('scroll');
  await settle();
  check('到底后不再发请求', pages().length === 3, `pages=${pages().length}`);
  check('全部 120 封都在，且不重复',
    new Set(uids()).size === 120 && uids().length === 120);

  // 换文件夹要回到第一页，不能带着上一个文件夹的 offset
  const rail = dom.byId.get('mail-folders');
  folderButton(rail, 'drafts').click();
  await settle();
  await settle();
  const latest = pages()[pages().length - 1];
  check('换文件夹后重新从 offset=0 开始', latest.url.includes('offset=0'),
    `url=${latest.url}`);
}

async function scenarioNoRefetchOnRevisit() {
  console.log('\n==== 13. 切走再回来：先用缓存，只默默查新（问题 1）====');
  const dom = boot({ loggedIn: true });
  await openMailWindow(dom);
  await settle();
  await settle();

  const rail = dom.byId.get('mail-folders');
  const list = dom.byId.get('mail-list');
  const loads = () => backend.calls.filter((c) => c.url.includes('/api/mail/messages'));
  // 只数**窗口自己**发的查新：带 watch=1 的是后台轮询（第 198 步加的标记），
  // 它按 localStorage 里的游标查，和窗口按 cache.newestUid 合并是两条路径。
  const checks = () => backend.calls.filter(
    (c) => c.url.includes('/api/mail/check') && !c.url.includes('watch=1')
  );

  check('首次进入收件箱加载了一次', loads().length === 1, `loads=${loads().length}`);
  const rowsAfterFirst = rowsOf(list).length;

  // 切到草稿箱，再切回收件箱
  folderButton(rail, 'drafts').click();
  await settle();
  await settle();
  const loadsAfterDrafts = loads().length;

  folderButton(rail, 'inbox').click();
  // 还没等任何响应：列表就该已经用缓存摆好了，不该出现"正在收取邮件…"
  check('切回来时立刻用缓存渲染（不等网络）',
    rowsOf(list).length === rowsAfterFirst
    && dom.byId.get('mail-state').hidden === true,
    `rows=${rowsOf(list).length} state=${dom.byId.get('mail-state').textContent}`);

  await settle();
  await settle();

  check('切回收件箱**没有**重新加载邮件',
    loads().length === loadsAfterDrafts,
    `之前 ${loadsAfterDrafts}，现在 ${loads().length}`);
  check('只发了一次后台查新', checks().length === 1, `checks=${checks().length}`);
  check('查新带上了基准 UID（手上最大的那个）',
    checks()[0].url.includes('since_uid=9'), `url=${checks()[0].url}`);
  check('查新后列表内容不变', rowsOf(list).length === rowsAfterFirst,
    `rows=${rowsOf(list).length}`);

  // 草稿箱也只加载了一次
  folderButton(rail, 'drafts').click();
  await settle();
  await settle();
  const draftsLoads = loads().filter((c) => c.url.includes('folder=drafts'));
  check('草稿箱也只加载一次', draftsLoads.length === 1, `drafts=${draftsLoads.length}`);
}

async function scenarioNewMailArrivesInBackground() {
  console.log('\n==== 14. 后台查新：有新邮件时插到最前面 ====');
  const dom = boot({ loggedIn: true });
  await openMailWindow(dom);
  await settle();
  await settle();

  const rail = dom.byId.get('mail-folders');
  const list = dom.byId.get('mail-list');
  const before = rowsOf(list).length;

  // 切换到草稿箱期间，"服务器上"来了一封新邮件
  folderButton(rail, 'drafts').click();
  await settle();
  await settle();
  backend.messages = [
    message(99, '刚到的新邮件', new Date().toISOString()),
    ...backend.messages,
  ];

  folderButton(rail, 'inbox').click();
  await settle();
  await settle();

  check('新邮件插到了最前面',
    rowsOf(list).length === before + 1 && rowsOf(list)[0].dataset.uid === '99',
    `rows=${rowsOf(list).length} first=${rowsOf(list)[0] && rowsOf(list)[0].dataset.uid}`);
  check('旧邮件一封都没少', rowsOf(list).length === before + 1);
}

async function scenarioRapidSwitchDoesNotCrossContaminate() {
  console.log('\n==== 15. 加载途中快速切文件夹：邮件不能串过去（问题 2）====');
  // 让收件箱的第一个请求**一直挂起**，这样才能复现"响应迟到"的竞态
  let releaseInbox = null;
  const controlledFetch = (url, options) => {
    const text = String(url);
    if (text.includes('/api/mail/messages') && text.includes('folder=inbox')) {
      return new Promise((resolve) => {
        releaseInbox = () => resolve(backendFetch(url, options));
      });
    }
    return backendFetch(url, options);
  };

  const dom = boot({ loggedIn: true }, controlledFetch);
  await openMailWindow(dom);
  await settle();
  await settle();

  const list = dom.byId.get('mail-list');
  const rail = dom.byId.get('mail-folders');

  check('收件箱请求被挂起（列表还是空的）', rowsOf(list).length === 0,
    `rows=${rowsOf(list).length}`);

  // 挂起期间切到草稿箱，草稿箱正常返回
  folderButton(rail, 'drafts').click();
  await settle();
  await settle();
  const draftsUids = rowsOf(list).map((r) => r.dataset.uid).join();
  check('切到草稿箱后显示草稿箱的内容', draftsUids.length > 0, `uids=${draftsUids}`);

  // 现在才让收件箱那个慢响应回来——它**绝不能**画到草稿箱界面上
  releaseInbox();
  await settle();
  await settle();

  check('收件箱的迟到响应没有覆盖草稿箱的列表',
    rowsOf(list).map((r) => r.dataset.uid).join() === draftsUids,
    `现在是 ${rowsOf(list).map((r) => r.dataset.uid)}，应当是 ${draftsUids}`);
  check('草稿箱没有被重新请求（迟到的响应不算数）',
    backend.calls.filter((c) => c.url.includes('folder=drafts')).length === 1);

  // 切回收件箱：迟到的那批数据已经安全落进收件箱自己的缓存
  const loadsBefore = backend.calls
    .filter((c) => c.url.includes('/api/mail/messages')).length;
  folderButton(rail, 'inbox').click();
  check('切回收件箱立刻有内容（迟到的数据在它自己的缓存里）',
    rowsOf(list).length > 0, `rows=${rowsOf(list).length}`);
  await settle();
  await settle();
  check('而且不需要重新加载那一页',
    backend.calls.filter((c) => c.url.includes('/api/mail/messages')).length
      === loadsBefore,
    '切回来时不该再发 /messages');
}

async function scenarioStarDoesNotRebuildList() {
  console.log('\n==== 16. 点星标不重建列表（不丢滚动、不丢焦点）====');
  const dom = boot({ loggedIn: true });
  await openMailWindow(dom);
  await settle();
  await settle();

  const list = dom.byId.get('mail-list');
  const firstRow = rowsOf(list)[0];
  const star = starOf(firstRow);
  const heading = list.children[0];

  // 模拟"已经滚到下面、焦点在某个星标上"
  list.scrollHeight = 2600;
  list.clientHeight = 600;
  list.scrollTop = 900;
  star.focus();
  check('焦点先落在星标上', dom.documentStub.activeElement === star);

  star.click();
  await settle();

  check('列表节点没有被重建（还是同一个对象）', rowsOf(list)[0] === firstRow,
    '重建的话这里会是一个新对象');
  check('分组标题也没被重建', list.children[0] === heading);
  check('滚动位置没被动', list.scrollTop === 900, `scrollTop=${list.scrollTop}`);
  check('焦点还在那个星标上', dom.documentStub.activeElement === star,
    `active=${dom.documentStub.activeElement && dom.documentStub.activeElement.className}`);
  check('星标确实变成了实心', star.textContent === '★', `star=${star.textContent}`);
  check('aria-pressed 跟着变', star.getAttribute('aria-pressed') === 'true',
    `aria=${star.getAttribute('aria-pressed')}`);
}

async function scenarioPagingKeepsScrollAndFocus() {
  console.log('\n==== 17. 翻页追加后，滚动位置与焦点都保留 ====');
  const dom = boot({ loggedIn: true, messages: buildManyMessages(120) });
  await openMailWindow(dom);
  await settle();
  await settle();

  const list = dom.byId.get('mail-list');
  check('第一页 50 行', rowsOf(list).length === 50, `rows=${rowsOf(list).length}`);

  // 滚到"接近底部"，并且把焦点放在第 10 行的星标上
  const target = rowsOf(list)[9];
  const star = starOf(target);
  star.focus();
  list.scrollHeight = 2600;
  list.clientHeight = 600;
  list.scrollTop = 2400;

  list.fire('scroll');
  await settle();
  await settle();

  check('确实追加了第二页', rowsOf(list).length === 100, `rows=${rowsOf(list).length}`);
  check('滚动位置没被打回顶部', list.scrollTop === 2400, `scrollTop=${list.scrollTop}`);
  check('焦点回到了原来那一行的星标上',
    dom.documentStub.activeElement !== null
    && dom.documentStub.activeElement.classList.contains('mail-star')
    && dom.documentStub.activeElement.closest('.mail-item') !== null
    && dom.documentStub.activeElement.closest('.mail-item').dataset.uid
      === target.dataset.uid,
    `uid=${dom.documentStub.activeElement
      && dom.documentStub.activeElement.closest('.mail-item')
      && dom.documentStub.activeElement.closest('.mail-item').dataset.uid}`);
}

async function scenarioCheckMergesDiscardedWhenListChanged() {
  console.log('\n==== 18. 查新迟到、期间列表变过 → 这次合并不算数 ====');
  let releaseCheck = null;
  const controlledFetch = (url, options) => {
    const text = String(url);
    // 后台轮询（第 198 步加的 watch=1）不参与这个场景：它按自己的游标走，
    // 挤进这个"故意挂住窗口那次查新"的闸门里只会把闸门抢走。这里直接放行空结果。
    if (text.includes('/api/mail/check') && text.includes('watch=1')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        async json() { return { folder: 'inbox', messages: [], total: 0, unread: 0, reset: false }; },
      });
    }
    if (text.includes('/api/mail/check') && !releaseCheck) {
      return new Promise((resolve) => {
        releaseCheck = () => resolve(backendFetch(url, options));
      });
    }
    return backendFetch(url, options);
  };

  const dom = boot({ loggedIn: true, messages: buildManyMessages(120) }, controlledFetch);
  await openMailWindow(dom);
  await settle();
  await settle();

  const list = dom.byId.get('mail-list');
  const rail = dom.byId.get('mail-folders');

  // 切走再切回：走缓存路径，于是会发起一次（被我们挂住的）后台查新
  folderButton(rail, 'drafts').click();
  await settle();
  await settle();
  folderButton(rail, 'inbox').click();
  await settle();
  check('查新请求被挂住了', releaseCheck !== null && rowsOf(list).length === 50,
    `rows=${rowsOf(list).length}`);

  // 查新还没回来，用户滚到底翻了第二页 —— 列表内容变了
  list.scrollHeight = 2600;
  list.clientHeight = 600;
  list.scrollTop = 2400;
  list.fire('scroll');
  await settle();
  await settle();
  check('期间列表已经翻到 100 行', rowsOf(list).length === 100,
    `rows=${rowsOf(list).length}`);

  // 现在让迟到的查新回来，并让它"带回一封新邮件"
  backend.messages = [
    message(9999, '迟到的查新带回来的', new Date().toISOString()),
    ...backend.messages,
  ];
  releaseCheck();
  await settle();
  await settle();

  const uids = rowsOf(list).map((row) => row.dataset.uid);
  check('迟到的合并被丢弃了（列表仍是 100 行）', rowsOf(list).length === 100,
    `rows=${rowsOf(list).length}`);
  check('没有出现重复邮件', new Set(uids).size === uids.length,
    `unique=${new Set(uids).size} / ${uids.length}`);
  check('那封迟到的邮件没有插进来', !uids.includes('9999'),
    `first=${uids[0]}`);

  // 但不该丢数据：checkedAt 被归零，下一次切回来会立刻重试并拿到它
  folderButton(rail, 'drafts').click();
  await settle();
  await settle();
  folderButton(rail, 'inbox').click();
  await settle();
  await settle();
  check('下次查新把那封邮件补回来了',
    rowsOf(list).some((row) => row.dataset.uid === '9999'),
    `first=${rowsOf(list)[0] && rowsOf(list)[0].dataset.uid}`);
}

async function main() {
  await scenarioDefaults();
  await scenarioValidation();
  await scenarioLoginAndList();
  await scenarioSearch();
  await scenarioStar();
  await scenarioFolders();
  await scenarioServices();
  await scenarioLogout();
  await scenarioUnauthorized();
  await scenarioLoginFailure();
  await scenarioSameWindowAndEsc();
  await scenarioPaging();
  await scenarioNoRefetchOnRevisit();
  await scenarioNewMailArrivesInBackground();
  await scenarioRapidSwitchDoesNotCrossContaminate();
  await scenarioStarDoesNotRebuildList();
  await scenarioPagingKeepsScrollAndFocus();
  await scenarioCheckMergesDiscardedWhenListChanged();
  finish();
}

main().catch((error) => {
  console.error('验证脚本自身出错：', error);
  process.exitCode = 1;
});

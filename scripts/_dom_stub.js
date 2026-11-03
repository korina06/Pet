/**
 * 各 verify_*.js 共用的 DOM 桩：元素、document / window、启动 app.js，以及等几个微任务的 settle()。
 *
 * 只放与场景无关的部分，断言留给各个验证脚本；不引第三方库，只要装了 node 就能跑。
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const appJs = fs.readFileSync(path.join(ROOT, 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(ROOT, 'static', 'index.html'), 'utf8');

/** 绑过的监听，用来断言"某元素有没有被绑定"（也方便排查重复绑定）。 */
const listeners = [];

function makeElement(id, tag) {
  const node = {
    id,
    tagName: (tag || 'div').toUpperCase(),
    hidden: false,
    value: '',
    textContent: '',
    title: '',
    children: [],
    dataset: {},
    className: '',
    style: { setProperty() {}, removeProperty() {} },
    attributes: {},
    _handlers: {},
  };
  const classes = new Set();
  // 真实浏览器里 `className` 和 `classList` 是**同一份数据**：`classList.add` 之后
  // 读 `className` 能看到新类名。桩以前把两者分开存，于是「加类名再读 className」
  // 这种写法在桩里永远读不到（第 227 步实测：破图的标记类名读不出来）。
  Object.defineProperty(node, 'className', {
    get: () => Array.from(classes).join(' '),
    set: (value) => {
      classes.clear();
      String(value || '')
        .split(/\s+/)
        .filter(Boolean)
        .forEach((name) => classes.add(name));
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
  // 浏览器的规则：把 DocumentFragment 传给这些方法时，插进去的是**它的子节点**，
  // 而不是 fragment 本身。桩必须照做，否则"历史整块换上"在桩里会变成一个空节点
  // （第 108 步实测：restoreChatHistory 的 replaceChildren(fragment) 断言全空）。
  const flatten = (items) => {
    const out = [];
    const visit = (item) => {
      if (!item) return;
      if (item.tagName === '#FRAGMENT' && Array.isArray(item.children)) {
        item.children.forEach(visit);
      } else {
        out.push(item);
      }
    };
    items.forEach(visit);
    return out;
  };

  const adopt = (items) => {
    items.forEach((c) => { if (c) c.parentNode = node; });
    return items;
  };

  node.appendChild = (c) => {
    adopt(flatten([c])).forEach((child) => node.children.push(child));
    return c;                            // 浏览器返回被插入的节点（fragment 时是它）
  };
  node.append = (...i) => {
    adopt(flatten(i)).forEach((child) => node.children.push(child));
  };
  node.prepend = (...i) => {
    const nodes = adopt(flatten(i));
    node.children.unshift(...nodes);
  };
  node.replaceChildren = (...i) => {
    node.children = adopt(flatten(i));
  };
  node.removeChild = (c) => {
    const at = node.children.indexOf(c);
    if (at >= 0) node.children.splice(at, 1);
    if (c) c.parentNode = null;
    return c;
  };
  node.remove = () => {
    if (node.parentNode) node.parentNode.removeChild(node);
  };
  node.insertBefore = (child, ref) => {
    const at = ref ? node.children.indexOf(ref) : -1;
    if (at < 0) node.children.push(child);
    else node.children.splice(at, 0, child);
    return child;
  };
  node.addEventListener = (type, handler) => {
    (node._handlers[type] = node._handlers[type] || []).push(handler);
    listeners.push({ target: id, type });
  };
  node.removeEventListener = () => {};
  // 触发任意事件（第 227 步）：图片的 `error`、视频的 `ended` 这类不是 click 的事件
  // 也得能测。真实浏览器由浏览器派发，桩里必须有人手动按一下。
  node.emit = (type, event = {}) => {
    (node._handlers[String(type)] || []).forEach((handler) => handler(event));
  };
  node.setAttribute = (n, v) => { node.attributes[n] = String(v); };
  node.getAttribute = (n) => (n in node.attributes ? node.attributes[n] : null);
  node.focus = () => { node._focused = true; };
  node.click = (target) => {
    // 第 154 步：事件对象要带 stopPropagation —— 真实浏览器里"点外面关浮层"
    // 是靠在按钮处理函数里 stopPropagation 挡掉的；桩不给这个方法，
    // "刚打开就被自己关掉"这类 bug 在桩里就永远看不见。
    node._stopped = false;
    const event = {
      preventDefault() {},
      target: target || node,
      stopPropagation() { node._stopped = true; },
    };
    (node._handlers.click || []).forEach((h) => h(event));
  };
  node.submit = () => {
    (node._handlers.submit || []).forEach((h) => h({ preventDefault() {} }));
  };
  node.querySelectorAll = () => [];
  node.querySelector = (selector) => {
    const want = String(selector);
    return node.children.find((child) => {
      if (want.startsWith('.') && child.className === want.slice(1)) return true;
      return false;
    }) || null;
  };
  node.getBoundingClientRect = () => ({ left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 });
  node.setPointerCapture = () => {};
  node.releasePointerCapture = () => {};
  node._escaped = String(id).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return node;
}

/**
 * 按 `index.html` 里的 id 造一份 DOM 桩。
 *
 * 会解析三样东西（不然很多断言测不到）：
 * - 开标签上的 `hidden` → `node.hidden`；
 * - 勾选框的 `value`；
 * - 元素内**只有纯文本**时的 `textContent`。
 *
 * 每次调用返回**全新**的一套（`localStorage` / 监听都是新的），
 * 所以同一个脚本里可以反复"开机"。
 */
function buildDom() {
  const ids = [...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
  const byId = new Map();

  for (const id of ids) {
    const tagMatch = html.match(new RegExp(`<([a-zA-Z]+)[^>]*id="${id}"`));
    const node = makeElement(id, tagMatch ? tagMatch[1] : 'div');

    const start = html.lastIndexOf('<', html.indexOf(`id="${id}"`));
    const end = html.indexOf('>', html.indexOf(`id="${id}"`));
    const fullTag = start >= 0 && end > start ? html.slice(start, end + 1) : '';
    if (/\shidden(\s|>|=)/.test(fullTag)) node.hidden = true;

    const valueMatch = fullTag.match(/\svalue="([^"]*)"/);
    if (valueMatch) node.value = valueMatch[1];

    const tagName = tagMatch ? tagMatch[1] : 'div';
    const closeTag = `</${tagName}>`;
    const closeAt = html.indexOf(closeTag, end + 1);
    if (closeAt > end) {
      const inner = html.slice(end + 1, closeAt);
      if (inner.trim() && !inner.includes('<')) node.textContent = inner.trim();
    }

    byId.set(id, node);
  }

  const documentStub = {
    getElementById: (id) => byId.get(id) || null,
    createElement: (tag) => makeElement('', tag),
    createTextNode: (text) => {
      const node = makeElement('', '#text');
      node.textContent = String(text);
      return node;
    },
    createDocumentFragment: () => makeElement('', '#fragment'),
    addEventListener: (type, handler) => {
      (documentStub._handlers[type] = documentStub._handlers[type] || []).push(handler);
      listeners.push({ target: '#document', type });
    },
    _handlers: {},
    documentElement: { style: { setProperty() {} } },
    head: makeElement('head', 'head'),
    body: makeElement('body', 'body'),
    querySelectorAll: () => [],
  };

  /** 模拟按下 Esc：走真实的全局 keydown 处理链。 */
  documentStub.pressEscape = () => {
    (documentStub._handlers.keydown || []).forEach((h) =>
      h({ key: 'Escape', preventDefault() {} })
    );
  };

  const local = new Map();
  const session = new Map();
  const windowStub = {
    innerWidth: 1280,
    innerHeight: 800,
    addEventListener: (type) => listeners.push({ target: '#window', type }),
    setTimeout: (fn) => { fn(); return 0; },
    clearTimeout: () => {},
    // 第 151 步：待机动画用 setInterval 每 10 秒叫一次。桩里同样**立即执行**，
    // 这样"有没有排上、有没有换 URL"在桩里就能断言（真实节奏交给烟测）。
    setInterval: (fn) => { fn(); return 0; },
    clearInterval: () => {},
    // 桩里默认"没开减少动态效果"；要用的话在测试里覆写
    matchMedia: () => ({ matches: false, addEventListener: () => {} }),
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

  return { byId, ids, documentStub, windowStub, local, session };
}

/**
 * 把 `app.js` 放进这套桩里跑一遍（等价于"页面加载完成"）。
 *
 * `fetchImpl` 由调用方给：每个场景需要不同的后端行为，
 * 这正是抽出这个模块的目的 —— 桩不再是"长死在某个脚本里的那一个"。
 */
function bootApp(dom, fetchImpl) {
  new Function(
    'document', 'window', 'fetch', 'localStorage', 'sessionStorage',
    'AbortController', 'requestAnimationFrame', 'getComputedStyle', 'crypto',
    appJs
  )(
    dom.documentStub, dom.windowStub,
    fetchImpl,
    dom.windowStub.localStorage, dom.windowStub.sessionStorage,
    dom.windowStub.AbortController, dom.windowStub.requestAnimationFrame,
    dom.windowStub.getComputedStyle, dom.windowStub.crypto
  );
}

/** 等 `app.js` 里的 async 链跑完。
 *
 *  以前每轮只 `await null`（纯微任务）。第 274 步那次 CI 红说明这样不够稳：
 *  同一份代码本机（node 24）全绿、runner（node 22）红，症状是气泡不出现、
 *  pending 没记账、`/mail/check` 一次都没发出去。把这里的等待砍到十分之一，
 *  本机就能复现出**一模一样**的症状 —— 差的是"让出事件循环的次数"，不是逻辑。
 *  所以每轮补一次 `setImmediate`：把这条链排队的微任务和已就绪的宏任务都放完
 *  再返回。桩里没有真实定时器（setTimeout/setInterval 都是立即执行），
 *  因此这一步不会改变任何用例的语义，只是把"等多久"从版本相关变成版本无关。
 */
async function settle(ticks = 3) {
  for (let i = 0; i < ticks; i += 1) {
    await null;
    await new Promise((resolve) => { setImmediate(resolve); });
  }
}

module.exports = {
  ROOT,
  appJs,
  html,
  listeners,
  makeElement,
  buildDom,
  bootApp,
  settle,
};

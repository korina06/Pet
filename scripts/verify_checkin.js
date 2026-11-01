/*
 * 用最小 DOM 桩加载 app.js，验证「签到」窗口：日历铺格、签到与补签、
 * 翻月、失败提示与关窗。「今天」由桩钉死，避免断言随日期漂移。
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const appJs = fs.readFileSync(path.join(ROOT, 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(ROOT, 'static', 'index.html'), 'utf8');

//: 桩里的"今天"。选 2026-03-18（周三）：1 号是周日 → 月首要空 6 格，
//: 正好能测出"空格补得对不对"这一格之差。
const FIXED_TODAY = '2026-03-18';
const STATUS_NOTES = { done: '已签', todo: '未签', future: '' };
const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日'];

/* ------------------------------------------------------------ DOM 桩 */

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
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
    children: [],
    style: { setProperty() {}, removeProperty() {} },
    attributes: {},
    _handlers: {},
  };

  // dataset 与 data-* 属性是同一件事（浏览器如此）：app.js 里
  // `cell.dataset.date = x` 之后要能用 `dataset.date` 找回来。
  node.dataset = new Proxy({}, {
    set(target, key, value) {
      target[key] = String(value);
      node.attributes[`data-${String(key)}`] = String(value);
      return true;
    },
    get(target, key) {
      return Object.prototype.hasOwnProperty.call(target, key) ? target[key] : undefined;
    },
  });
  // textContent 要像浏览器那样把子节点的文字也算进来，否则
  // "按钮里套了个 <span>已签</span>" 读出来是空字符串——测出来的是桩的毛病。
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
  // 桩里要是各存一份，`node.className = 'cal-day'` 之后再问
  // classList.contains('cal-day') 就会说没有（测不准）。
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
  node.focus = () => { node._focused = true; };
  // ⚠️ 这里比 verify_mail.js 的桩多做两件事，都是本文件要测的行为：
  //
  // 1. **禁用的按钮点不动**：浏览器里 disabled 的元素不派发 click，
  //    而"还没到的日期不可点"正是要测的——桩要是不认 disabled，那条断言就成了假通过。
  // 2. **事件会冒泡**：日期格是现画的，app.js 用事件委托把 click 绑在
  //    <tbody> 上。桩要是不冒泡，点格子就永远没人接，看起来像 app.js 没写。
  node.click = (target) => {
    if (node.disabled) { node._clickBlocked = true; return; }
    const event = { preventDefault() {}, target: target || node };
    let current = node;
    while (current) {
      (current._handlers.click || []).forEach((handler) => handler(event));
      current = current.parentNode;
    }
  };
  node.fire = (type, event) => {
    const payload = Object.assign({ preventDefault() {}, target: node }, event || {});
    let current = node;
    while (current) {
      (current._handlers[type] || []).forEach((handler) => handler(payload));
      current = current.parentNode;
    }
  };
  // closest：事件委托（点日期格）要用。桩只按 className 找，
  // 够用是因为 app.js 只问 ".cal-day"。
  node.closest = (selector) => {
    const want = String(selector).replace(/^\./, '');
    let current = node;
    while (current) {
      if (want && String(current.className || '').split(/\s+/).includes(want)) return current;
      current = current.parentNode;
    }
    return null;
  };
  node.querySelectorAll = () => [];
  node.querySelector = () => null;
  node.getBoundingClientRect = () =>
    ({ left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 });
  node.setPointerCapture = () => {};
  node.releasePointerCapture = () => {};
  return node;
}

/** 只给 app.js 看的那两种用法：new Date() 与 new Date(iso)。 */
function fixedDateClass(iso) {
  const fixed = new Date(`${iso}T00:00:00Z`).getTime();
  class StubDate extends Date {
    constructor(...args) {
      if (args.length === 0) super(fixed);
      else super(...args);
    }
  }
  return StubDate;
}

/** 按 index.html 里出现的 id 造一份全新的 DOM 桩。 */
function makeDom(seed) {
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

  // 桩默认是"一堆平铺的元素"，只看 id 不看层级；但 <table> 是例外：
  // thead/tbody 在浏览器里是它的子节点，不把层级接上，
  // `collect(grid, …)` 就一个格子也找不到（第一版就踩了这个坑，
  // 断言"31 个日期格"报 0，看起来像 app.js 没画 —— 其实是桩的毛病）。
  const hold = (parentId, ...childIds) => {
    const parent = byId.get(parentId);
    if (!parent) return;
    childIds.forEach((childId) => {
      const child = byId.get(childId);
      if (child) parent.appendChild(child);
    });
  };
  hold('checkin-grid', 'checkin-caption', 'checkin-weekdays', 'checkin-days');

  const session = new Map(Object.entries((seed && seed.session) || {}));
  // 第 94 步：会话 id 改存 localStorage（跨重启），桩要能种值
  const local = new Map(Object.entries((seed && seed.local) || {}));
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
    crypto: { randomUUID: () => 'stub-uuid' },
    sessionStorage: {
      getItem: (k) => (session.has(k) ? session.get(k) : null),
      setItem: (k, v) => session.set(k, v),
      removeItem: (k) => session.delete(k),
    },
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
  };

  return { byId, documentStub, windowStub, session };
}

/* ------------------------------------------------------------ 假后端 */

/* 下面这段**照抄 core/checkin.py 的算法**（不是真相，是替身）。
   真值由 tests/test_checkin.py 与 tests/test_checkin_api.py 用真代码钉住；
   本文件测的是"拿到这样一份数据，界面画得对不对"。 */

const backend = {
  today: FIXED_TODAY,
  checked: new Set(),
  cards: 0,               // 补签卡余量（用户第 4 条：补签要消耗道具卡）
  monthFails: 0,          // 非 0 时 /month 用这个状态码失败
  checkinFails: null,     // { status, detail }
  calls: [],
};

function isoShift(iso, days) {
  const date = new Date(`${iso}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function isoMonthShift(iso, months) {
  const date = new Date(`${iso}T00:00:00Z`);
  const year = date.getUTCFullYear();
  const month = date.getUTCMonth() + months;
  const day = date.getUTCDate();
  const last = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  return new Date(Date.UTC(year, month, Math.min(day, last))).toISOString().slice(0, 10);
}

function daysInMonth(year, month /* 1-12 */) {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

//: 农历日名的桩（真实名表在 core/lunar.py；这里只需要"形状对"）。
const LUNAR_DAYS = [
  '初一', '初二', '初三', '初四', '初五', '初六', '初七', '初八', '初九', '初十',
  '十一', '十二', '十三', '十四', '十五', '十六', '十七', '十八', '十九', '二十',
  '廿一', '廿二', '廿三', '廿四', '廿五', '廿六', '廿七', '廿八', '廿九', '三十',
];

//: 桩里的"休 / 班"：照抄 2026 年 10 月的官方安排（国庆 10/1–7 放假、10/10 补班）。
//: 其它月份一律不给标记 —— 与"没有官方数据的年份就不显示休/班"一致。
const REST_MAP = {
  '2026-10-01': 'off', '2026-10-02': 'off', '2026-10-03': 'off',
  '2026-10-04': 'off', '2026-10-05': 'off', '2026-10-06': 'off',
  '2026-10-07': 'off',
  '2026-10-10': 'work',
};

//: 桩里的节日：只放几个真实的固定日期节日（3/8、3/12、10/1）。
const FESTIVAL_MAP = {
  '2026-03-08': '妇女节',
  '2026-03-12': '植树节',
  '2026-10-01': '国庆节',
};

/** 与 core/checkin.py 的 month_view 同构（用来喂前端）。 */
function fakeMonthView(viewMonth /* 'YYYY-MM' */) {
  const [year, month] = viewMonth.split('-').map(Number);
  const first = `${viewMonth}-01`;
  const firstWeekday = new Date(`${first}T00:00:00Z`).getUTCDay();   // 0=周日
  const leading = (firstWeekday + 6) % 7;                            // 周一 = 0

  const days = [];
  for (let i = 0; i < leading; i += 1) {
    days.push({
      date: '', day: 0, status: null, note: '',
      clickable: false, is_today: false, past: false,
      lunar: '', festival: '', calendar_tip: '', rest: '', rest_label: '',
    });
  }

  const total = daysInMonth(year, month);
  for (let number = 1; number <= total; number += 1) {
    const iso = `${viewMonth}-${String(number).padStart(2, '0')}`;
    let status;
    if (iso > backend.today) status = 'future';
    else if (backend.checked.has(iso)) status = 'done';
    else status = 'todo';
    const rest = REST_MAP[iso] || '';
    days.push({
      date: iso,
      day: number,
      status,
      note: STATUS_NOTES[status],
      clickable: status !== 'future',
      is_today: iso === backend.today,
      // 早于今天 = 点它就是补签（要消耗一张卡）。前端靠这个字段决定弹不弹确认。
      past: iso < backend.today,
      lunar: LUNAR_DAYS[(number - 1) % 30],
      festival: FESTIVAL_MAP[iso] || '',
      calendar_tip: [FESTIVAL_MAP[iso] || '', LUNAR_DAYS[(number - 1) % 30]]
        .filter(Boolean).join(' '),
      rest,
      rest_label: rest === 'off' ? '休' : (rest === 'work' ? '班' : ''),
    });
  }

  // 连续天数：今天签了从今天数，没签从昨天数（与 core/checkin.py 一致）
  let cursor = backend.checked.has(backend.today) ? backend.today : isoShift(backend.today, -1);
  let streak = 0;
  while (backend.checked.has(cursor)) {
    streak += 1;
    cursor = isoShift(cursor, -1);
  }

  const viewFirst = `${viewMonth}-01`;
  // 翻月范围（第 146 步）：下限 2020-01、上限 2050-12，与 core/checkin.py 一致。
  // 不再卡"本月"——用户可以往后翻看未来的日历（只是日子不可点）。
  return {
    session_id: 'stub',
    month: viewMonth,
    month_label: `${year} 年 ${month} 月`,
    leading,
    days,
    weekdays: WEEKDAYS,
    streak,
    prev_month: isoMonthShift(viewFirst, -1).slice(0, 7),
    next_month: isoMonthShift(viewFirst, 1).slice(0, 7),
    can_prev: viewFirst > '2020-01-01',
    can_next: viewFirst < '2050-12-01',
    // 补签确认框在**发请求之前**就要用到卡名，所以月视图里也带一份
    card_name: '补签卡',
    // 还剩几张卡（评价15 T1-5）：前端要在弹框**之前**就判断，没卡时不弹框。
    cards: backend.cards,
    card_note: '未持有补签卡',
    rest_labels: { off: '休', work: '班' },
    status_labels: { done: '已签', todo: '未签' },
  };
}

function json(status, payload) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(payload),
  });
}

function backendFetch(url, options) {
  const method = (options && options.method) || 'GET';
  const text = String(url);
  const body = () => {
    try { return JSON.parse((options && options.body) || '{}'); } catch (_) { return {}; }
  };
  backend.calls.push({ method, url: text, body: options && options.body });

  if (text.includes('/api/checkin/month')) {
    if (backend.monthFails) {
      return json(backend.monthFails, { detail: '签到日历临时取不到。' });
    }
    const found = /month=([0-9-]+)/.exec(text);
    const viewMonth = found ? found[1] : backend.today.slice(0, 7);
    return json(200, fakeMonthView(viewMonth));
  }

  // 补签 = 签的是过去的日子（照抄 api/checkin_routes.py 的判断），要扣一张卡。
  // 失败时也**连整月视图一起回**（照抄 _fail）：界面要把提示挂到那一格下面。
  const fail = (detail, date) => json(400, {
    session_id: 'stub-session',
    date,
    fresh: false,
    message: '',
    ok: false,
    detail,
    cards: backend.cards,
    month: fakeMonthView(date.slice(0, 7)),
  });

  if (text.includes('/api/checkin')) {
    if (backend.checkinFails) {
      // 假后端允许直接指定失败，用来测"detail 原样显示"这条路
      const detail = backend.checkinFails.detail;
      const failedDate = (body().date) || backend.today;
      return json(backend.checkinFails.status, {
        session_id: 'stub-session',
        date: failedDate,
        fresh: false,
        message: '',
        ok: false,
        detail,
        cards: backend.cards,
        month: fakeMonthView(failedDate.slice(0, 7)),
      });
    }
    const payload = body();
    const date = payload.date;
    const already = backend.checked.has(date);
    const makeup = date < backend.today && !already;
    if (makeup) {
      if (backend.cards <= 0) {
        return fail('未持有补签卡', date);          // 用户第 4 条指定的文案
      }
      backend.cards -= 1;
    }
    const fresh = !already;
    backend.checked.add(date);
    return json(200, {
      session_id: payload.session_id,
      date,
      fresh,
      ok: true,
      makeup,
      cards: backend.cards,
      card_name: '补签卡',
      card_note: '未持有补签卡',
      message: fresh ? `${date} 签到成功。` : `${date} 已经签过了。`,
      month: fakeMonthView(date.slice(0, 7)),
    });
  }

  if (text.includes('/api/tools')) return json(200, { tools: [] });
  if (text.includes('/api/mail/status')) return json(200, { logged_in: false, account: null });
  return json(200, {});
}

/** 每次 boot 都用一份新 DOM + 重置后端场景。
 *
 *  scenario.checked **一旦给了就整份替换**（不是增量）：假后端上"签过的日子"
 *  是共享状态，前一个场景签过的日期会留到后一个场景；只有显式给一份清单，
 *  每个场景才真的从自己声明的那份状态开始（第一版就是漏了这一点，
 *  于是 scenarioGrid 里 17 号莫名其妙变成"已签"——不是 app.js 的毛病）。
 */
function boot(config) {
  const scenario = config || {};
  backend.today = scenario.today || FIXED_TODAY;
  backend.checked = new Set(scenario.checked || []);
  //: 补签卡余量。不给就是 0（= core/checkin.py 的 MAKEUP_CARDS_START），
  //: 也就是"还没做道具系统"时的真实状态：补签会被拒，提示"未持有补签卡"。
  backend.cards = scenario.cards || 0;
  backend.monthFails = scenario.monthFails || 0;
  backend.checkinFails = scenario.checkinFails || null;
  backend.calls = [];

  const dom = makeDom(scenario.storage || { local: { 'pet.session_id': 'stub-session' } });
  // window.confirm 是给"补签要消耗一张卡"的确认用的。桩默认点"确定"，
  // 要测"点取消"的场景再临时换掉（见 scenarioBackfillCancelled）。
  dom.windowStub.confirm = () => true;
  new Function(
    'document', 'window', 'fetch', 'localStorage', 'sessionStorage',
    'AbortController', 'requestAnimationFrame', 'getComputedStyle', 'crypto', 'Date',
    appJs
  )(
    dom.documentStub, dom.windowStub,
    scenario.fetchImpl || backendFetch,
    dom.windowStub.localStorage, dom.windowStub.sessionStorage,
    dom.windowStub.AbortController, dom.windowStub.requestAnimationFrame,
    dom.windowStub.getComputedStyle, dom.windowStub.crypto,
    fixedDateClass(backend.today)
  );
  return dom;
}

/* ------------------------------------------------------------ 断言 */

let failures = 0;
let total = 0;
function check(label, ok, detail) {
  total += 1;
  if (!ok) failures += 1;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? '  |  ' + detail : ''}`);
}
function finish() {
  console.log(`\n共 ${total} 项断言`);
  console.log(`失败 ${failures} 项`);
  process.exitCode = failures ? 1 : 0;
}

/** 用 Node 的真定时器排空微任务队列（app.js 里的 await 链有好几层）。 */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const callsTo = (fragment) => backend.calls.filter((call) => call.url.includes(fragment));
const monthCalls = () => callsTo('/api/checkin/month');

function hasClass(node, cls) {
  return Boolean(node && node.classList && node.classList.contains(cls));
}

function collect(root, predicate) {
  const found = [];
  (function visit(node) {
    if (!node) return;
    if (predicate(node)) found.push(node);
    (node.children || []).forEach(visit);
  })(root);
  return found;
}

/** 按 data-date 找某一天的格子（只在日历表格里找，避免撞上别处的元素）。 */
function dayCell(dom, iso) {
  return collect(dom.byId.get('checkin-grid'), (n) => n.dataset && n.dataset.date === iso)[0] || null;
}

/** 点轮盘上的「签到」小圆并等取数回来，返回窗口元素。 */
async function openCheckinWindow(dom) {
  const items = dom.byId.get('menu-list').children;
  const item = items.find((b) => b.dataset.key === 'checkin');
  item.click();
  await settle();
  await settle();
  return dom.byId.get('checkin-modal');
}

/* ------------------------------------------------------------ 各场景 */

async function scenarioOpenDefaults() {
  console.log('\n==== 1. 点轮盘「签到」：打开窗口，默认停在当月 ====');
  const dom = boot({});
  const items = dom.byId.get('menu-list').children;
  const item = items.find((b) => b.dataset.key === 'checkin');

  check('轮盘里有「签到」一项', Boolean(item), `项=${items.map((b) => b.dataset.key)}`);
  check('「签到」可点（不再是占位）', Boolean(item) && item.disabled === false);
  // 小圆里的文字包在 .wheel-label 里，所以按 label 的子节点取文案；
  // 直接拼 textContent 会把图标也算进去（"📅签到"），断言就永远对不上。
  const labelText = (node) => (node.children || [])
    .filter((c) => c.className === 'wheel-label')
    .map((c) => c.textContent)
    .join('');
  check('「签到」文案正确', Boolean(item) && labelText(item) === '签到',
    `文案=${item && labelText(item)}`);

  const modal = dom.byId.get('checkin-modal');
  check('打开前窗口是隐藏的', modal.hidden === true);
  check('打开前没有取过签到接口（取数是"打开"触发的）',
    monthCalls().length === 0, `calls=${backend.calls.length}`);

  const chatLogBefore = dom.byId.get('chat-log').children.length;
  await openCheckinWindow(dom);
  check('点「签到」弹出窗口', modal.hidden === false, `hidden=${modal.hidden}`);
  check('点「签到」不发消息（它开窗口，不是发意图）',
    dom.byId.get('chat-log').children.length === chatLogBefore,
    `消息数 ${dom.byId.get('chat-log').children.length}`);
  check('开窗口会取一次当月视图',
    monthCalls().length === 1, `calls=${monthCalls().length}`);
  check('当月视图的请求里带了 session_id',
    monthCalls()[0].url.includes('session_id=stub-session'), monthCalls()[0].url);
  check('月度标题来自后端', dom.byId.get('checkin-month-label').textContent === '2026 年 3 月',
    `label=${dom.byId.get('checkin-month-label').textContent}`);
  check('日历表已显示、取数提示已收起',
    dom.byId.get('checkin-grid').hidden === false
    && dom.byId.get('checkin-state').hidden === true);
}

async function scenarioGrid() {
  console.log('\n==== 2. 日历按"号数 + 已签/未签"铺格，未到的日子不算 ====');
  const dom = boot({ checked: ['2026-03-16', '2026-03-18'] });
  await openCheckinWindow(dom);

  const grid = dom.byId.get('checkin-grid');
  const tbody = dom.byId.get('checkin-days');

  check('周一列头齐全（7 个）', dom.byId.get('checkin-weekdays').children.length === 7,
    `列头=${dom.byId.get('checkin-weekdays').children.map((c) => c.textContent)}`);
  check('第一列是「一」', dom.byId.get('checkin-weekdays').children[0].textContent === '一');

  // 2026-03-01 是周日 → 前面要空 6 格（周一到周六）
  const cells = collect(grid, (n) => hasClass(n, 'cal-day'));
  const blanks = collect(grid, (n) => hasClass(n, 'cal-blank'));
  check('当月有 31 个日期格', cells.length === 31, `格子=${cells.length}`);
  check('1 号前空 6 格（3 月 1 日是周日）', blanks.length === 6, `空格=${blanks.length}`);

  const rowCounts = tbody.children.map((row) => row.children.length);
  // 6 空格 + 31 天 = 37 格 → 5 行满 7 列 + 最后一行剩 2 格。
  // 断言"除最后一行外每行都满 7 列"，而不是要求每行都满——
  // 一个月本来就常常填不满最后一行，要求每行 7 列等于要求日历说谎。
  check('除最后一行外每行都是 7 列',
    rowCounts.slice(0, -1).every((n) => n === 7), `行=${rowCounts}`);
  check('最后一行是余数（2 格）', rowCounts[rowCounts.length - 1] === 2,
    `行=${rowCounts}`);
  check('共 6 行（6 空格 + 31 天 = 37 格 → 6 行）', rowCounts.length === 6,
    `行数=${rowCounts.length}`);

  // 1 号必须落在第 7 列（周日），这是"空 6 格"的直接结果
  const firstRow = tbody.children[0];
  check('1 号落在第一行第 7 列（周日）',
    firstRow.children[6].children[0].textContent.includes('1')
    && !firstRow.children[0].children[0].textContent.includes('1'),
    `第 7 列=${firstRow.children[6].children[0].textContent}`);

  const done = dayCell(dom, '2026-03-18');
  const todo = dayCell(dom, '2026-03-17');
  const future = dayCell(dom, '2026-03-19');

  // 桩的 textContent 是各 span 直接拼起来的，所以断言按这个拼法写，别去猜空格。
  check('已签那天的格子写了「已签」',
    done.textContent.includes('18') && done.textContent.includes('已签'),
    `text=${done.textContent}`);
  check('已签的格子带 is-done', hasClass(done, 'is-done'));
  check('今天（18 号）带 is-today',
    hasClass(done, 'is-today'), `class=${done.className}`);
  // 用户第 2 条：今天只用蓝色特效，**不写"今天"两个字**
  check('今天那格不写「今天」字样',
    !done.textContent.includes('今天'),
    `text=${done.textContent}`);
  check('未签那天的格子写了「未签」',
    todo.textContent.includes('17') && todo.textContent.includes('未签'),
    `text=${todo.textContent}`);
  check('未签的格子带 is-todo', hasClass(todo, 'is-todo'));
  check('悬停提示带日期与状态（还有农历）',
    done.title.startsWith('2026-03-18 · ') && done.title.endsWith('已签'),
    `title=${done.title}`);

  // 用户第 1 条：日期数字下面要有节日名与农历
  const festivalDay = dayCell(dom, '2026-03-08');
  check('有节日的那天写了节日名（妇女节）',
    festivalDay.textContent.includes('妇女节'),
    `text=${festivalDay.textContent}`);
  check('节日名单独一层 .cal-fest',
    Boolean(collect(festivalDay, (n) => hasClass(n, 'cal-fest')).length));
  check('每天都有农历那一层 .cal-lunar',
    Boolean(collect(todo, (n) => hasClass(n, 'cal-lunar')).length),
    `text=${todo.textContent}`);

  // 用户第 4 条：过去的日子带 is-past（悬停/点击时前端用灰蓝标出）
  check('过去的日子带 is-past', hasClass(todo, 'is-past'));
  check('今天不是 is-past（今天属于"现在"）', !hasClass(done, 'is-past'));

  // 用户原话："还没到的日期不算" —— 不写状态、不可点
  check('还没到的日期不写状态（没有「已签/未签」）',
    !future.textContent.includes('已签') && !future.textContent.includes('未签'),
    `text=${future.textContent}`);
  check('还没到的日期带 is-future', hasClass(future, 'is-future'));
  check('还没到的日期是 disabled', future.disabled === true);
  check('过去与今天都可点（过去是补签）',
    todo.disabled === false && done.disabled === false);

  // 用户第 1、2、3 条的三处减法：没有图例、没有表格 caption、没有统计行
  check('没有图例元素（用户第 1 条）',
    !dom.byId.get('checkin-legend') && !html.includes('checkin-legend'));
  check('表格没有 caption（用户第 2 条）',
    !dom.byId.get('checkin-caption') && !html.includes('checkin-caption'));
  check('没有"本月已签/未签"统计行（用户第 3 条）',
    !dom.byId.get('checkin-summary') && !html.includes('checkin-summary'));
}

async function scenarioSignToday() {
  console.log('\n==== 3. 点今天 → 签到，整月重画 ====');
  const dom = boot({ checked: ['2026-03-16', '2026-03-17'] });
  await openCheckinWindow(dom);

  const todayCell = dayCell(dom, '2026-03-18');
  check('签到前今天显示「未签」', todayCell.textContent.includes('未签'),
    `text=${todayCell.textContent}`);

  todayCell.click();
  await settle();
  await settle();

  const posts = backend.calls.filter((c) => c.method === 'POST' && c.url.includes('/api/checkin'));
  check('发出了一次签到请求', posts.length === 1, `POST=${posts.length}`);
  check('请求体里日期是今天',
    JSON.parse(posts[0].body).date === '2026-03-18',
    `body=${posts[0].body}`);
  check('请求体里带了 session_id',
    JSON.parse(posts[0].body).session_id === 'stub-session',
    `body=${posts[0].body}`);

  const after = dayCell(dom, '2026-03-18');
  check('签到后今天变成「已签」', after.textContent.includes('已签'),
    `text=${after.textContent}`);
  check('签到后今天带 is-done', hasClass(after, 'is-done'));
  check('点今天不消耗补签卡（只有补签才扣）',
    backend.cards === 0, `cards=${backend.cards}`);
  check('签到没有失败提示（那格只有「已签」）',
    !after.textContent.includes('未持有') && !hasClass(after, 'has-card-note'),
    `text=${after.textContent}`);
}

async function scenarioRepeat() {
  console.log('\n==== 4. 重复点已签的今天：不算错，也不该再扣卡 ====');
  // 18 号是"今天"且已签 → 后端回 fresh=false，而且它**不是补签**，不扣卡
  const dom = boot({ checked: ['2026-03-18'] });
  await openCheckinWindow(dom);

  dayCell(dom, '2026-03-18').click();
  await settle();
  await settle();

  const posts = backend.calls.filter((c) => c.method === 'POST' && c.url.includes('/api/checkin'));
  check('重复点也发了请求（由后端判断是不是重复）', posts.length === 1,
    `POST=${posts.length}`);
  const repeated = dayCell(dom, '2026-03-18');
  check('重复点今天没有失败提示（不是错，是"已经签过了"）',
    !hasClass(repeated, 'has-card-note') && !repeated.textContent.includes('未持有'),
    `text=${repeated.textContent}`);
  check('已签的那天仍然是「已签」',
    repeated.textContent.includes('已签'));
  check('重复点不消耗补签卡', backend.cards === 0, `cards=${backend.cards}`);
}

async function scenarioBackfillNeedsCard() {
  console.log('\n==== 5. 没有卡时补签：不弹注定失败的确认框，就地说明 ====');
  const dom = boot({ checked: [], cards: 0 });
  await openCheckinWindow(dom);

  const past = dayCell(dom, '2026-03-05');
  check('过去的日期初始是「未签」', past.textContent.includes('未签'),
    `text=${past.textContent}`);
  check('过去的日期可点（要补签，先由后端判有没有卡）', past.disabled === false);
  check('过去的未签日期带补签标记（要弹确认）', past.dataset.makeup === '1');
  check('今天不带补签标记（点今天不花道具）',
    !dayCell(dom, '2026-03-18').dataset.makeup,
    `makeup=${dayCell(dom, '2026-03-18').dataset.makeup}`);

  const before = backend.calls.length;
  past.click();
  await settle();
  await settle();

  // 评价15 T1-5：一张卡都没有时，"弹确认框 → 点确定 → 被拒"是注定失败的交互
  check('没卡时不弹确认框（不做一个注定失败的确认）',
    dom.byId.get('checkin-confirm').hidden === true,
    `hidden=${dom.byId.get('checkin-confirm').hidden}`);
  check('没卡时也不发请求（省一次必被拒的往返）',
    backend.calls.length === before, `多出 ${backend.calls.length - before} 个请求`);

  // 用户第 2 条：红字写在**想补签的那一天**的"未签"下面
  const cell = dayCell(dom, '2026-03-05');
  check('提示直接挂在那一格「未签」下面（不是窗口底部）',
    cell.textContent.includes('未签') && cell.textContent.includes('未持有补签卡'),
    `text=${cell.textContent}`);
  check('那一天带 has-card-note（红框用）', hasClass(cell, 'has-card-note'));
  check('其它日期格没有被牵连',
    !dayCell(dom, '2026-03-06').textContent.includes('未持有补签卡'),
    `text=${dayCell(dom, '2026-03-06').textContent}`);
  check('窗口底部那行文字没有变成错误（错误已就地下沉到格子）',
    dom.byId.get('checkin-state').hidden === true,
    `state=${dom.byId.get('checkin-state').textContent}`);
  check('没有卡时那天不会变成「已签」',
    cell.textContent.includes('未签'), `text=${cell.textContent}`);
}

async function scenarioStaleCardCount() {
  console.log('\n==== 5e. 月视图里的卡数过期了：前端以为有、后端说没有 ====');
  const dom = boot({ checked: [], cards: 1 });
  await openCheckinWindow(dom);

  // 模拟"别处已经用掉了这张卡，但这个月视图还是旧的"
  backend.cards = 0;

  const before = backend.calls.length;
  dayCell(dom, '2026-03-04').click();
  await settle();
  check('月视图说有卡 → 仍然先弹确认框',
    dom.byId.get('checkin-confirm').hidden === false);
  dom.byId.get('checkin-confirm-ok').click();
  await settle();
  await settle();

  check('后端说没有 → 请求照发、被拒一次',
    backend.calls.length === before + 1,
    `多出 ${backend.calls.length - before} 个请求`);
  const cell = dayCell(dom, '2026-03-04');
  check('被拒的原因落在那一格（前端不自己编文案）',
    cell.textContent.includes('未持有补签卡'), `text=${cell.textContent}`);
  check('被拒的那天没有变成「已签」', cell.textContent.includes('未签'));
}

async function scenarioWarningClearsAfterRetry() {
  console.log('\n==== 5d. 补签失败后提示会跟着下一次操作清掉 ====');
  const dom = boot({ checked: [], cards: 0 });
  await openCheckinWindow(dom);

  dayCell(dom, '2026-03-05').click();   // 没卡 → 不弹框，直接在格子上说
  await settle();
  await settle();
  check('先失败一次，提示在 5 号那格',
    dayCell(dom, '2026-03-05').textContent.includes('未持有补签卡'));

  // 改点今天：成功了 → 上一次的补签提示不该留在 5 号那格
  dayCell(dom, '2026-03-18').click();
  await settle();
  await settle();
  check('点了今天并成功之后，5 号那格的旧提示被清掉',
    !dayCell(dom, '2026-03-05').textContent.includes('未持有补签卡'),
    `text=${dayCell(dom, '2026-03-05').textContent}`);
  check('今天是「已签」', dayCell(dom, '2026-03-18').textContent.includes('已签'));
}

async function scenarioBackfillWithCard() {
  console.log('\n==== 5b. 有卡时补签成功，并扣掉一张 ====');
  const dom = boot({ checked: [], cards: 1 });
  await openCheckinWindow(dom);

  dayCell(dom, '2026-03-05').click();
  await settle();
  dom.byId.get('checkin-confirm-ok').click();
  await settle();
  await settle();

  check('补签后那天变成「已签」',
    dayCell(dom, '2026-03-05').textContent.includes('已签'),
    `text=${dayCell(dom, '2026-03-05').textContent}`);
  check('补签那天带 is-done', hasClass(dayCell(dom, '2026-03-05'), 'is-done'));
  check('补签扣掉了一张卡（1 → 0）', backend.cards === 0, `cards=${backend.cards}`);
  check('成功后没有留下失败提示',
    !dayCell(dom, '2026-03-05').textContent.includes('未持有补签卡'));

  // 卡用完了 → 再补一天就不该弹框了（评价15 T1-5），提示直接落在那一天
  dayCell(dom, '2026-03-04').click();
  await settle();
  await settle();
  check('卡用完后另一天补不了，提示落在 4 号那格',
    dayCell(dom, '2026-03-04').textContent.includes('未持有补签卡'),
    `text=${dayCell(dom, '2026-03-04').textContent}`);
  check('被拒的那天没有变成「已签」',
    dayCell(dom, '2026-03-04').textContent.includes('未签'));
}

async function scenarioBackfillCancelled() {
  console.log('\n==== 5c. 在确认框点"取消"：不发请求、不扣卡 ====');
  const dom = boot({ checked: [], cards: 3 });
  await openCheckinWindow(dom);

  const before = backend.calls.length;
  dayCell(dom, '2026-03-05').click();
  await settle();

  check('点过去的日子弹出了确认框', dom.byId.get('checkin-confirm').hidden === false);
  dom.byId.get('checkin-confirm-cancel').click();   // 点「取消」
  await settle();
  await settle();

  check('点取消后确认框收起', dom.byId.get('checkin-confirm').hidden === true);
  check('点取消后不发任何请求', backend.calls.length === before,
    `多出 ${backend.calls.length - before} 个请求`);
  check('点取消后卡数不变', backend.cards === 3, `cards=${backend.cards}`);
  check('点取消后那天仍是「未签」',
    dayCell(dom, '2026-03-05').textContent.includes('未签'));
  check('点取消不留提示', !dayCell(dom, '2026-03-05').textContent.includes('未持有'));

  // Esc 也算取消（不能把整个签到窗口一起关掉）
  dayCell(dom, '2026-03-05').click();
  await settle();
  dom.documentStub.pressEscape();
  check('Esc 先关确认框（等于取消）',
    dom.byId.get('checkin-confirm').hidden === true
    && dom.byId.get('checkin-modal').hidden === false,
    `confirm=${dom.byId.get('checkin-confirm').hidden} modal=${dom.byId.get('checkin-modal').hidden}`);
  check('Esc 取消也不扣卡', backend.cards === 3, `cards=${backend.cards}`);
}

async function scenarioFutureBlocked() {
  console.log('\n==== 6. 还没到的日期点不动（也不花道具） ====');
  const dom = boot({});
  await openCheckinWindow(dom);

  const before = backend.calls.length;
  const future = dayCell(dom, '2026-03-25');
  check('未来的格子是 disabled（浏览器不会派发 click）', future.disabled === true);
  future.click();
  check('点了也没反应（桩记录了被拦下的点击）', future._clickBlocked === true);
  check('禁用的格子没发出任何请求', backend.calls.length === before,
    `calls=${backend.calls.length - before}`);
}

async function scenarioMonthNav() {
  console.log('\n==== 7. 翻月：按那一月的数据画 ====');
  const dom = boot({ checked: ['2026-02-27', '2026-02-28'] });
  await openCheckinWindow(dom);

  const prev = dom.byId.get('checkin-prev');
  const next = dom.byId.get('checkin-next');
  // 第 146 步：允许往后翻（用户要能看未来的日历），只是未来的日子不可点
  check('当月也能往后翻（可以看未来）', next.disabled === false);
  check('当月可以往前翻', prev.disabled === false);

  prev.click();
  await settle();
  await settle();

  check('翻月会重新取数', monthCalls().length === 2, `calls=${monthCalls().length}`);
  check('请求里带了目标月份',
    monthCalls()[1].url.includes('month=2026-02'), monthCalls()[1].url);
  check('标题换成 2 月', dom.byId.get('checkin-month-label').textContent === '2026 年 2 月',
    `label=${dom.byId.get('checkin-month-label').textContent}`);
  check('2 月的已签两天照原样画出来',
    dayCell(dom, '2026-02-27').textContent.includes('已签')
    && dayCell(dom, '2026-02-28').textContent.includes('已签'));
  check('2 月里"今天"不在这个月，所以没有 is-today 的格子',
    collect(dom.byId.get('checkin-grid'), (n) => hasClass(n, 'is-today')).length === 0);

  dom.byId.get('checkin-next').click();
  await settle();
  await settle();
  check('翻回 3 月', dom.byId.get('checkin-month-label').textContent === '2026 年 3 月',
    `label=${dom.byId.get('checkin-month-label').textContent}`);
  check('翻回后今天又带 is-today',
    hasClass(dayCell(dom, '2026-03-18'), 'is-today'));
}

/** 往后翻到未来的月份：日子都在、但不可点、也没有已签/未签。 */
async function scenarioFutureMonth() {
  console.log('\n==== 7a. 往后翻到未来月份：能看，不能签 ====');
  const dom = boot({});
  await openCheckinWindow(dom);

  dom.byId.get('checkin-next').click();
  await settle();
  await settle();

  check('往后翻到 4 月（未来月份）',
    dom.byId.get('checkin-month-label').textContent === '2026 年 4 月',
    `label=${dom.byId.get('checkin-month-label').textContent}`);
  const futureCell = dayCell(dom, '2026-04-15');
  check('未来月份里的日子仍然画出来（有农历/号数）',
    Boolean(futureCell) && futureCell.textContent.includes('15'),
    `text=${futureCell && futureCell.textContent}`);
  check('未来月份的日子带 is-future、不可点',
    hasClass(futureCell, 'is-future') && futureCell.disabled === true);
  check('未来月份的日子没有「已签/未签」',
    !futureCell.textContent.includes('未签') && !futureCell.textContent.includes('已签'));
}

/** 往前翻到 2050 之后 / 2020 之前，按钮该禁用（范围与农历表对齐）。 */
async function scenarioMonthBounds() {
  console.log('\n==== 7b. 翻月的两头：2020-01 与 2050-12 ====');
  const dom = boot({});
  await openCheckinWindow(dom);

  // 直接跳到 2050-12 太慢（要走 300 个月），这里只验证按钮逻辑：
  // 桩的 can_next 由日期字符串比较得出，翻到 2050-12 后 next 必须禁用。
  let guard = 400;
  while (!dom.byId.get('checkin-next').disabled && guard > 0) {
    // 一次点一下，靠 settle 等每次取数；走不到 2050 就是逻辑错了
    dom.byId.get('checkin-next').click();
    await settle();
    await settle();
    guard -= 1;
  }

  check('能一直往后翻', guard < 400, `剩余 guard=${guard}`);
  check('翻到 2050 年 12 月就停住',
    dom.byId.get('checkin-month-label').textContent === '2050 年 12 月',
    `label=${dom.byId.get('checkin-month-label').textContent}`);
  check('到上限后 › 变禁用', dom.byId.get('checkin-next').disabled === true);
  check('到上限后 ‹ 仍可用（能翻回来）',
    dom.byId.get('checkin-prev').disabled === false);
}

async function scenarioLoadFailure() {
  console.log('\n==== 8. 取数失败（500）→ 用一句可读的中文顶替日历 ====');
  const dom = boot({ monthFails: 500 });
  await openCheckinWindow(dom);

  const stateEl = dom.byId.get('checkin-state');
  check('显示后端给的 detail（不是英文状态码）',
    stateEl.hidden === false && stateEl.textContent === '签到日历临时取不到。',
    `text=${stateEl.textContent}`);
  check('失败的那句话标了 is-error（红色）',
    hasClass(stateEl, 'is-error'), `class=${stateEl.className}`);
  check('失败时不留一张空表格',
    dom.byId.get('checkin-grid').hidden === true);
  check('失败时也能关掉窗口', (dom.byId.get('checkin-close').click(), true));
}

async function scenarioCheckinFailure() {
  console.log('\n==== 9. 签到失败（400）→ 原样用后端那句 detail ====');
  const dom = boot({
    // 后端在失败时**连整月视图一起回**（见 _fail），所以假后端也这么给——
    // 只有这样提示才能挂到出错的那一格下面。
    checkinFails: { status: 400, detail: '这一天还没到（2026-03-25），到了再签。' },
  });
  await openCheckinWindow(dom);

  dayCell(dom, '2026-03-18').click();
  await settle();
  await settle();

  const cell = dayCell(dom, '2026-03-18');
  check('失败原因写在点的那一格下面',
    cell.textContent.includes('这一天还没到（2026-03-25），到了再签。'),
    `text=${cell.textContent}`);
  check('失败的那格带 has-card-note',
    hasClass(cell, 'has-card-note'), `class=${cell.className}`);
  check('失败时窗口顶部那行没有变成错误（提示在格子里）',
    dom.byId.get('checkin-state').hidden === true,
    `state=${dom.byId.get('checkin-state').textContent}`);

  // 后端恢复 → 再点一次应当能签上，并且把提示清掉
  backend.checkinFails = null;
  await openCheckinWindow(dom);
  dayCell(dom, '2026-03-18').click();
  await settle();
  await settle();
  check('后端恢复后能正常签上',
    dayCell(dom, '2026-03-18').textContent.includes('已签'),
    `text=${dayCell(dom, '2026-03-18').textContent}`);
  check('恢复后失败提示被清掉',
    !dom.byId.get('checkin-days').textContent.includes('还没到'),
    `days=${dom.byId.get('checkin-days').textContent}`);
}

async function scenarioClose() {
  console.log('\n==== 10. × / 点遮罩 / Esc 都能关 ====');
  const dom = boot({});
  const modal = dom.byId.get('checkin-modal');

  await openCheckinWindow(dom);
  dom.byId.get('checkin-close').click();
  check('× 能关掉签到窗口', modal.hidden === true, `hidden=${modal.hidden}`);

  await openCheckinWindow(dom);
  modal.click(modal);
  check('点遮罩能关掉', modal.hidden === true, `hidden=${modal.hidden}`);

  await openCheckinWindow(dom);
  modal.click(dom.byId.get('checkin-title'));
  check('点窗口内部不关', modal.hidden === false, `hidden=${modal.hidden}`);

  dom.documentStub.pressEscape();
  check('Esc 也能关掉', modal.hidden === true, `hidden=${modal.hidden}`);

  // 关掉再打开：回到当月（上一次可能翻到几个月前补签）
  await openCheckinWindow(dom);
  dom.byId.get('checkin-prev').click();
  // 等这一次翻月的请求回来再断言：翻月是异步的，不等的话读到的还是旧标题
  await settle();
  await settle();
  check('先翻到上个月', dom.byId.get('checkin-month-label').textContent === '2026 年 2 月',
    `label=${dom.byId.get('checkin-month-label').textContent}`);
  dom.byId.get('checkin-close').click();
  await openCheckinWindow(dom);
  check('关掉再打开回到当月（不留在上个月）',
    dom.byId.get('checkin-month-label').textContent === '2026 年 3 月',
    `label=${dom.byId.get('checkin-month-label').textContent}`);
}

/** 休 / 班 小标：用 2026 年 10 月的真实安排（国庆 10/1–7 放假、10/10 补班）。 */
async function scenarioRestBadges() {
  console.log('\n==== 7c. 休 / 班 小标（用 2026 年 10 月的真实安排）====');
  const dom = boot({ today: '2026-10-25' });
  await openCheckinWindow(dom);

  check('标题是 2026 年 10 月',
    dom.byId.get('checkin-month-label').textContent === '2026 年 10 月',
    `label=${dom.byId.get('checkin-month-label').textContent}`);

  const holiday = dayCell(dom, '2026-10-01');
  check('国庆当天有「休」小标',
    holiday.textContent.includes('休'), `text=${holiday.textContent}`);
  check('放假那天带 is-rest-off', hasClass(holiday, 'is-rest-off'));
  check('10/7 也是「休」',
    dayCell(dom, '2026-10-07').textContent.includes('休'));

  const workday = dayCell(dom, '2026-10-10');
  check('10/10 调休当天有「班」小标',
    workday.textContent.includes('班'), `text=${workday.textContent}`);
  check('补班那天带 is-rest-work', hasClass(workday, 'is-rest-work'));

  check('普通日子没有休/班小标',
    !dayCell(dom, '2026-10-15').textContent.includes('休')
    && !dayCell(dom, '2026-10-15').textContent.includes('班'),
    `text=${dayCell(dom, '2026-10-15').textContent}`);
}

/* ------------------------------------------------------------ 跑 */

async function main() {
  await scenarioOpenDefaults();
  await scenarioGrid();
  await scenarioSignToday();
  await scenarioRepeat();
  await scenarioBackfillNeedsCard();
  await scenarioStaleCardCount();
  await scenarioWarningClearsAfterRetry();
  await scenarioBackfillWithCard();
  await scenarioBackfillCancelled();
  await scenarioFutureBlocked();
  await scenarioMonthNav();
  await scenarioFutureMonth();
  await scenarioMonthBounds();
  await scenarioRestBadges();
  await scenarioLoadFailure();
  await scenarioCheckinFailure();
  await scenarioClose();
  finish();
}

main().catch((error) => {
  console.error('验证脚本自身出错：', error);
  process.exitCode = 1;
});

/*
 * 简化说明（桩里没模拟的浏览器行为，免得读者高估覆盖率）：
 *
 * 1. 只认 id：makeDom 按 index.html 里的 id 造元素，不看层级、不看 CSS。
 *    所以"窗口到底多大、颜色对不对"测不出来——那些是 CSS 的事，
 *    由 tests/test_checkin_ui.py 直接读样式文件钉住关键数值。
 * 2. 没有布局与滚动：格子高度、7 列宽度都不参与断言。
 * 3. 禁用的元素点不动（刻意模拟，不是简化）：浏览器里 disabled 元素
 *    不派发 click，桩照做，否则"还没到的日期不可点"会假通过。
 * 4. 假后端是**照抄** core/checkin.py 的算法，不是那份代码本身。
 *    它的"真值"锚点有两个：tests/test_checkin.py（直接测 Python 函数）
 *    与 tests/test_checkin_api.py（真 FastAPI 应用跑接口契约）。
 *    本文件只负责"拿到这样一份数据，界面画得对不对"。
 */

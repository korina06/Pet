/*
 * 用最小 DOM 桩加载 app.js，验证「食谱」窗口：卡片、详情浮层、右栏标签、
 * 对话检索、错误与关窗行为。不依赖浏览器与第三方库。
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const appJs = fs.readFileSync(path.join(ROOT, 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(ROOT, 'static', 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(ROOT, 'static', 'style.css'), 'utf8');

/* ------------------------------------------------------------ DOM 桩 */

function makeElement(id, tag) {
  const classes = new Set();
  // 记下 classList 的每一次**真实变化**（+class / -class）。
  // 用途：showRecipeView('chat') 先给首页加 .is-leaving 播滑走动画，
  // 再在 window.setTimeout 里隐藏首页并移除这个 class；桩里的 setTimeout
  // 是同步执行的，等 click 返回时 class 已经被移除了。没有这份日志，
  // "首页曾加上过 .is-leaving"就只能靠猜。（见场景 6。）
  const classLog = [];
  let ownText = '';
  const node = {
    id,
    tagName: (tag || 'div').toUpperCase(),
    hidden: false,
    value: '',
    title: '',
    disabled: false,
    // 滚动几何：桩必须给初值，否则 app.js 里的算术会得到 NaN
    // （app.js 用 scrollTop/scrollHeight 让对话滚到底，NaN 比较永远 false）
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
    add: (c) => {
      if (!classes.has(c)) { classes.add(c); classLog.push(`+${c}`); }
    },
    remove: (c) => {
      if (classes.has(c)) { classes.delete(c); classLog.push(`-${c}`); }
    },
    contains: (c) => classes.has(c),
    toggle: (c, force) => {
      const want = force === undefined ? !classes.has(c) : Boolean(force);
      if (want) {
        if (!classes.has(c)) { classes.add(c); classLog.push(`+${c}`); }
      } else if (classes.has(c)) {
        classes.delete(c); classLog.push(`-${c}`);
      }
      return want;
    },
    _set: classes,
  };
  node._classLog = classLog;
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
  // ⚠️ 比 verify_mail.js 的桩多一条：**禁用的按钮点不动**。
  // 浏览器里 disabled 的元素不派发 click，而"发送中按钮是禁用的"正是
  // 本文件要测的行为（场景 8）——桩要是不认 disabled，那条断言就成了假通过。
  node.click = (target) => {
    if (node.disabled) { node._clickBlocked = true; return; }
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

/** 按 index.html 里出现的 id 造一份全新的 DOM 桩。
 *
 *  `opts.local` / `opts.session` 是**预置的存储内容**：app.js 从
 *  localStorage 的 `pet.recipe_session` 读会话 id，预置了才能断言
 *  "请求里带的正是我存的那个 id"，而不是自己生成的随机串。
 *
 *  桩**没有**解析 HTML 上的 value/min/max 属性（只解析 id、标签名、hidden），
 *  所以所有 input 的初始 value 都是空串。本轮已经不再测天数输入框，
 *  这一点只影响 `#recipe-input`（发送前由测试自己赋值）。
 */
function makeDom(opts) {
  const seed = opts || {};
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

  const local = new Map(Object.entries(seed.local || {}));
  const session = new Map(Object.entries(seed.session || {}));
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
    // ⚠️ 同步执行：app.js 里 window.setTimeout 是给它自己用的"稍后再做"，
    // 桩里立刻跑掉。当前只有 showRecipeView 的滑走动画用它；场景 6 靠
    // `_classLog` 证明 .is-leaving 真的加上过。
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
    AbortController: class {
      constructor() { this.signal = {}; }
      abort() {}
    },
  };

  return { byId, documentStub, windowStub, local, session };
}

/* ------------------------------------------------------------ 后端桩
 *
 * 只保留新流程真正用到的响应：
 *   GET  /api/recipe/schema
 *   GET  /api/recipe/session
 *   POST /api/recipe/ask
 *   GET  /api/recipe/library?limit=6&shuffle=1
 *   GET  /api/recipe/library/{id}
 *   POST /api/recipe/library/seed
 *   GET/PUT /api/recipe/library/preferences
 *
 * 旧的「需求收集 / 多天生成」那套（/message、/plan、profile、missing、
 * ready、plan、days）已经从桩里删掉：留着会让"前端还在用旧接口"这种回归
 * 意外通过。两个旧路径仍会命中一个 410 分支，并照常记进 backend.calls，
 * 任何回退都会在 `messageCalls()` / `planCalls()` 的断言里暴露。
 *
 * 响应形状照抄 api/recipe_routes.py 与 api/recipe_library_routes.py：
 * 键名一个都不能自己编，测的是"前端能不能把后端真会给的东西画出来"。
 */

//: 第 50 步起开场白去掉了 markdown 星号（textContent 渲染会原样显示 **）。
const GREETING =
  '你好！我是菜谱助手。想吃点什么？跟我说一句就行。'
  + '另外，忌口和过敏请一定说出来——这比什么都重要，我会尽量避开；如果是严重过敏，请你也再确认一下用料。';
//: 免责声明**仍在 schema 里返回**，但第 50 步起界面不再展示它。
const DISCLAIMER =
  '菜谱来自本地菜谱库与网络参考，仅供参考，不构成医疗或营养建议；'
  + '有过敏史或慢性病请以医生意见为准。';

//: 输入框的示例句（后端 `core/recipes.py` 的 SAMPLE_MESSAGE，走 schema.example）
const SAMPLE_MESSAGE = '我在减脂，想让菜清淡一点';

//: core/recipe_library.py 的 AVOID_OPTIONS：「一直记住 · 不吃」那组的按钮。
const AVOID_OPTIONS = ['香菜', '海鲜', '内脏', '牛羊肉', '辣', '蛋', '奶', '坚果'];
//: core/recipe_library.py 的 WANT_GROUPS：「这次想要」按维度分行。
//: 50f 换库后「工具」组换成了「菜系」「场景」——原来的小电锅/电饭煲
//: 在 96 道地方菜里一道都筛不出来。
const WANT_GROUPS = [
  ['口味', ['清淡', '微辣', '辣', '酸甜'], false],
  ['菜系', ['川菜', '粤菜', '湘菜', '鲁菜', '苏菜', '浙菜', '闽菜', '徽菜'], false],
  ['场景', ['下饭', '主食', '汤', '素'], false],
  // 第 52 步：加了「30 分钟以上」，并标记互斥（组内只能亮一个）
  ['时间', ['15 分钟内', '30 分钟内', '30 分钟以上'], true],
  ['怎么吃', ['自己做', '外卖', '都行'], true],
];

/* ------------------------------------------------------------ 菜谱库桩
 *
 * 形状照 api/recipe_library_routes.py：
 *   GET  /api/recipe/library?limit=6&shuffle=1 → RecipeListResponse
 *   GET  /api/recipe/library/{id}              → RecipeOut
 *   POST /api/recipe/library/seed              → SeedResponse
 *
 * `libraryBatches` 用来模拟「换一批」：每次 GET list 依次取下一条，最后一条
 * 会一直重复（所以只配两批时，点两次「换一批」不会越界）。
 */

const LIBRARY = [
  {
    id: 11, name: '番茄炒蛋', ingredients: ['番茄 2 个', '鸡蛋 3 个'],
    steps: ['番茄切块', '鸡蛋炒散后下番茄'], tags: ['家常', '下饭'],
    time_minutes: 15, source: 'seed', origin_url: '', note: '',
  },
  {
    id: 12, name: '小米南瓜粥', ingredients: ['小米 60g', '南瓜 200g'],
    steps: ['小米淘洗', '和南瓜一起煮 25 分钟'], tags: ['早餐', '清淡'],
    time_minutes: 30, source: 'seed', origin_url: '', note: '',
  },
  {
    id: 13, name: '蒜蓉油麦菜', ingredients: ['油麦菜 1 把', '蒜 2 瓣'],
    steps: ['蒜末爆香', '下油麦菜快炒 2 分钟'], tags: ['素菜', '下饭'],
    time_minutes: 10, source: 'seed', origin_url: '', note: '别炒太久，容易出水。',
  },
  {
    id: 14, name: '香菇滑鸡', ingredients: ['鸡腿 2 个', '香菇 6 朵'],
    steps: ['鸡腿切块', '和香菇一起蒸 20 分钟'], tags: ['蒸菜', '高蛋白'],
    time_minutes: 35, source: 'seed', origin_url: '', note: '',
  },
  {
    id: 15, name: '冬瓜排骨汤', ingredients: ['冬瓜 300g', '排骨 400g'],
    steps: ['排骨焯水', '小火炖 40 分钟后下冬瓜'], tags: ['汤', '清淡'],
    time_minutes: 55, source: 'seed', origin_url: '', note: '',
  },
  {
    id: 16, name: '葱油拌面', ingredients: ['面条 150g', '小葱 1 把'],
    steps: ['小葱炸到焦香', '拌入煮好的面'], tags: ['主食', '下饭'],
    time_minutes: 20, source: 'seed', origin_url: '', note: '',
  },
];

/** 假后端给「换一批」的第二批：名字与 id 都不同，方便断言内容真的变了。 */
const LIBRARY_ALT = [
  { id: 101, name: '土豆炖牛腩' },
  { id: 102, name: '清蒸鲈鱼' },
  { id: 103, name: '凉拌黄瓜' },
  { id: 104, name: '鸡蛋羹' },
  { id: 105, name: '青椒肉丝' },
  { id: 106, name: '紫菜蛋花汤' },
].map((row) => ({
  ingredients: ['食材 A', '食材 B'],
  steps: ['第一步', '第二步'],
  tags: ['第二批'],
  time_minutes: 20,
  source: 'seed',
  origin_url: '',
  note: '',
  ...row,
}));

/** RecipeListResponse（字段名照 api/recipe_library_routes.py）。 */
function libraryPayload(recipes) {
  return {
    recipes: recipes.map((recipe) => ({ ...recipe })),
    total: recipes.length,
    limit: 6,
    offset: 0,
    source_counts: recipes.length ? { seed: recipes.length } : {},
  };
}

//: /ask 的默认回复与这次要画的两道菜。
//: 两道菜刻意在库里（id=12 / id=15），点卡片时假后端才能按 id 取到详情。
const DEFAULT_ASK_REPLY = '给你找了两道，先看看合不合口味。';
const ASK_RECIPES = [LIBRARY[1], LIBRARY[4]];

//: 一份"旧后端还可能返回"的多天计划。SessionResponse 里仍有 plan 字段，
//: 但第 50 步的前端完全不读它——场景 9 拿它验证"就算后端给了也不画"。
const OLD_PLAN = {
  session_id: 'recipe-sess-1',
  days: [{
    day: 1,
    title: '第 1 天 · 旧计划',
    meals: [{ slot: '早餐', dishes: [{ name: '小米粥' }] }],
  }],
};

const RECIPE_SESSION_KEY = 'pet.recipe_session';
const CHAT_SESSION_KEY = 'pet.session_id';
const RECIPE_SESSION_ID = 'recipe-sess-1';
const CHAT_SESSION_ID = 'chat-sess-1';

//: 假后端。每个场景由 boot() 重置成一份干净的。
const backend = {
  messages: [],
  schemaFails: 0,
  sessionFails: 0,
  //: /session 里要不要塞一份旧的多天计划（场景 9 用它证明前端不再读）
  sessionPlan: null,
  //: /ask 的失败分支：{ status, detail }；设成 null 就恢复正常
  askFails: null,
  //: /ask 成功时返回的 reply / recipes（数组或函数）
  askReply: DEFAULT_ASK_REPLY,
  askRecipes: ASK_RECIPES.slice(),
  library: LIBRARY.slice(),  // 当前库内容（空库场景改成 []）
  libraryBatches: null,      // 配了就让每次 list 依次取一批（模拟 shuffle）
  libraryListCount: 0,
  seedBatch: LIBRARY.slice(),// POST /seed 之后换成这一批
  //: 右栏标签（第 50 步）：已经记住的忌口词条，以及"PUT 时故意失败"的开关。
  aversions: [],
  preferencesPutFails: null, // { status, detail }
  calls: [],
};

/** PreferencesResponse（字段名照 api/recipe_library_routes.py）。 */
function preferencesPayload() {
  return {
    aversions: backend.aversions.slice(),
    avoid_options: AVOID_OPTIONS.slice(),
    want_groups: WANT_GROUPS.map(([label, tags, exclusive]) => ({
      label,
      tags: tags.slice(),
      exclusive: Boolean(exclusive),
    })),
  };
}

/** 第 74 步：把一串事件包成**真的** ReadableStream（SSE 响应）。
 *
 *  为什么要真的流，而不是"一次性返回整段文本"：那样只能测到"最后画对了"，
 *  测不到**边到边画** —— 而边到边正是这一步的全部意义。
 *  `pace` 毫秒为 0 时一口气推完；大于 0 时每帧之间让出一次宏任务，
 *  测试才能在流没结束时观察到"只画了一半"。
 */
function sse(events, pace) {
  const frames = events.map((event) => `data: ${JSON.stringify(event)}\n\n`);
  const encoder = new TextEncoder();
  let index = 0;
  const stream = new ReadableStream({
    pull(controller) {
      if (index >= frames.length) {
        controller.close();
        return undefined;
      }
      controller.enqueue(encoder.encode(frames[index]));
      index += 1;
      if (pace > 0 && index < frames.length) {
        return new Promise((resolve) => setTimeout(resolve, pace));
      }
      return undefined;
    },
  });
  const joined = frames.join("");
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => "text/event-stream" },
    body: stream,
    text: () => Promise.resolve(joined),
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** 轮询等待某个条件成立（流是异步的，固定 sleep 会不稳）。 */
async function waitFor(predicate, timeoutMs = 3000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (predicate()) return true;
    await sleep(10);
  }
  return false;
}

function json(status, payload) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(payload),
  });
}

/** 没显式给 streamScript 时，按 askReply 切几段当成 delta（场景 8 用）。 */
function defaultStreamEvents(payload) {
  const reply = backend.askReply;
  const recipes = (typeof backend.askRecipes === "function"
    ? backend.askRecipes(payload)
    : backend.askRecipes) || [];
  const chunks = String(reply).match(/[^，。]{1,6}/g) || [String(reply)];
  return [
    ...chunks.map((text) => ({ type: "delta", text })),
    {
      type: "meta",
      payload: {
        session_id: payload.session_id,
        reply,
        recipes,
        tool_calls: ["search_recipes"],
      },
    },
  ];
}

/** /session 的响应：字段名照 api/recipe_routes.py 的 SessionResponse。 */
function sessionPayload(sessionId) {
  return {
    session_id: sessionId,
    messages: backend.messages.map((message) => ({ ...message })),
    groups: [],
    missing: [],
    ready: true,
    // 后端仍保留多天计划字段，但本轮前端完全不读它（restoreRecipeSession
    // 只画 messages）——场景 9 会塞一份 OLD_PLAN 进来验证这一点。
    plan: backend.sessionPlan,
    plan_count: backend.sessionPlan ? 1 : 0,
  };
}

function backendFetch(url, options) {
  const method = (options && options.method) || 'GET';
  const text = String(url);
  backend.calls.push({ method, url: text, body: options && options.body });

  const body = () => {
    try { return JSON.parse((options && options.body) || '{}'); } catch (_) { return {}; }
  };

  if (text.includes('/api/recipe/schema')) {
    if (backend.schemaFails) {
      return json(backend.schemaFails, { detail: '内部错误' });
    }
    // 第 51 步起 schema 只有三个字段（开场白 / 示例句 / 免责声明）。
    // 旧的多天结构（fields/groups/days/quick_prompts/meals）已经删掉了，
    // 桩里也一起删——留着它反而会掩盖"前端还在读旧字段"这种真问题。
    return json(200, {
      greeting: GREETING,
      example: SAMPLE_MESSAGE,
      disclaimer: DISCLAIMER,
    });
  }

  // ---- 右栏标签（第 50 步）：GET 取选项与已记住的忌口；PUT 整份替换忌口。
  // 必须排在 list 之前：/api/recipe/library/preferences 也包含 /api/recipe/library。
  if (text.includes('/api/recipe/library/preferences')) {
    if (method === 'PUT') {
      if (backend.preferencesPutFails) {
        return json(backend.preferencesPutFails.status,
          { detail: backend.preferencesPutFails.detail });
      }
      const payload = body();
      // 照 core 的 set_aversions：整份替换，返回替换后的结果
      backend.aversions = Array.isArray(payload.aversions)
        ? payload.aversions.slice()
        : [];
      return json(200, preferencesPayload());
    }
    return json(200, preferencesPayload());
  }

  // ---- 菜谱库（第 48 步）：先匹配 /seed，再匹配 /{id}，最后才是列表。
  // 顺序不能反：/api/recipe/library/seed 也包含 /api/recipe/library。
  if (text.includes('/api/recipe/library/seed')) {
    const before = backend.library.length;
    backend.library = backend.seedBatch.map((recipe) => ({ ...recipe }));
    backend.libraryBatches = null;
    return json(200, {
      added: backend.library.length - before,
      skipped: 0,
      total: backend.library.length,
      library_total: backend.library.length,
    });
  }

  const detailMatch = /\/api\/recipe\/library\/(\d+)/.exec(text);
  if (detailMatch) {
    const recipe = backend.library.find(
      (row) => String(row.id) === detailMatch[1]
    );
    if (!recipe) return json(404, { detail: `菜谱不存在（id=${detailMatch[1]}）` });
    return json(200, { ...recipe });
  }

  if (text.includes('/api/recipe/library')) {
    let batch;
    if (backend.libraryBatches) {
      const at = Math.min(backend.libraryListCount, backend.libraryBatches.length - 1);
      batch = backend.libraryBatches[at];
    } else {
      batch = backend.library;
    }
    backend.libraryListCount += 1;
    return json(200, libraryPayload(batch));
  }

  // ---- 第 50 步 50d：对话式检索。
  // 故意把 FastAPI 的字段校验也模拟出来：前端漏发 session_id / message 时
  // 这里返回 422，断言会从"请求体不对"直接暴露，而不是静默返回 200。
  // ---- 第 74 步：流式问答。**必须排在 /api/recipe/ask 之前**：
  // '/api/recipe/ask/stream' 也包含 '/api/recipe/ask' 这个子串。
  if (text.includes('/api/recipe/ask/stream')) {
    // 失败场景沿用 askFails：前端现在打的是流式接口，
    // 若只认 streamFails，那些"失败要显示红字"的老场景就会变成空转（实测抓到过）。
    const failure = backend.streamFails || backend.askFails;
    if (failure) {
      return json(failure.status, { detail: failure.detail });
    }
    const payload = body();
    if (typeof payload.session_id !== "string" || !payload.session_id
      || typeof payload.message !== "string" || !payload.message) {
      return json(422, { detail: "session_id 与 message 都必须给" });
    }
    const events = backend.streamScript || defaultStreamEvents(payload);
    backend.messages.push({ role: "user", content: payload.message });
    backend.messages.push({ role: "assistant", content: backend.askReply });
    return sse(events, backend.streamPace || 0);
  }

  if (text.includes('/api/recipe/ask')) {
    if (backend.askFails) {
      return json(backend.askFails.status, { detail: backend.askFails.detail });
    }
    const payload = body();
    if (typeof payload.session_id !== 'string' || !payload.session_id
      || typeof payload.message !== 'string' || !payload.message) {
      return json(422, {
        detail: [{ loc: ['body'], msg: 'session_id 与 message 都必须给', input: payload }],
      });
    }
    const reply = backend.askReply;
    const recipes = (typeof backend.askRecipes === 'function'
      ? backend.askRecipes(payload)
      : backend.askRecipes) || [];
    backend.messages.push({ role: 'user', content: payload.message });
    backend.messages.push({ role: 'assistant', content: reply });
    return json(200, {
      session_id: payload.session_id,
      reply,
      recipes: recipes.map((recipe) => ({ ...recipe })),
      tool_calls: ['search_recipes'],
    });
  }

  if (text.includes('/api/recipe/session')) {
    if (backend.sessionFails) return json(backend.sessionFails, { detail: '会话取不到了' });
    const found = /session_id=([^&]*)/.exec(text);
    return json(200, sessionPayload(found ? decodeURIComponent(found[1]) : ''));
  }

  // ---- 本轮已删除的旧接口：真后端里 /message、/plan 还在，但前端不该再碰。
  // 桩给 410 而不是 200，任何回退都会立刻表现为界面报错 + calls 非空。
  if (text.includes('/api/recipe/message') || text.includes('/api/recipe/plan')) {
    return json(410, { detail: '这个旧接口已经不在界面上了' });
  }

  // 其它接口：本文件不测它们，给最小响应即可（app.js 启动时会取 /api/tools，
  // 开食谱窗口不会碰邮箱接口，但留着免得将来多一步取数就报"连不上后端"）。
  if (text.includes('/api/tools')) return json(200, { tools: [] });
  if (text.includes('/api/mail/status')) return json(200, { logged_in: false, account: null });

  return json(200, {});
}

/** 每次 boot 都用一份新 DOM + 重置后端场景。 */
function boot(scenario, fetchImpl) {
  const config = scenario || {};

  backend.messages = (config.messages || []).map((message) => ({ ...message }));
  backend.schemaFails = config.schemaFails || 0;
  backend.sessionFails = config.sessionFails || 0;
  backend.sessionPlan = config.sessionPlan || null;
  backend.askFails = config.askFails || null;
  backend.askReply = config.askReply || DEFAULT_ASK_REPLY;
  backend.askRecipes = (config.askRecipes === undefined ? ASK_RECIPES : config.askRecipes);
  if (typeof backend.askRecipes !== 'function') {
    backend.askRecipes = (backend.askRecipes || []).map((recipe) => ({ ...recipe }));
  }
  backend.library = (config.library === undefined ? LIBRARY : config.library)
    .map((recipe) => ({ ...recipe }));
  backend.libraryBatches = (config.libraryBatches || null)
    && config.libraryBatches.map((batch) => batch.map((recipe) => ({ ...recipe })));
  backend.libraryListCount = 0;
  backend.seedBatch = (config.seedBatch || LIBRARY)
    .map((recipe) => ({ ...recipe }));
  backend.aversions = (config.aversions || []).slice();
  backend.streamScript = config.streamScript || null;
  backend.streamPace = config.streamPace || 0;
  backend.streamFails = config.streamFails || null;
  backend.preferencesPutFails = config.preferencesPutFails || null;
  backend.calls = [];

  // 预置存储：`storage: {}` 表示"本地什么都没存过"（测 app.js 自己生成 id）
  const storage = config.storage === undefined
    ? { local: { [RECIPE_SESSION_KEY]: RECIPE_SESSION_ID },
        session: { [CHAT_SESSION_KEY]: CHAT_SESSION_ID } }
    : config.storage;

  const dom = makeDom(storage);
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
let total = 0;
function check(label, ok, detail) {
  total += 1;
  if (!ok) failures += 1;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? '  |  ' + detail : ''}`);
}
function finish() {
  console.log(`\n共 ${total} 项断言`);
  console.log(`失败 ${failures} 项`);
  // 退出码：有失败就是 1，全通过是 0。这里用 exitCode 而不是 process.exit()——
  // 后者在管道里可能来不及把最后几行 stdout 冲出去，而验收要看的
  // 恰恰是"失败 0 项"这一行（verify_mail.js 也是这么做的）。
  process.exitCode = failures ? 1 : 0;
}

/** 用 Node 的真定时器排空微任务队列。
 *
 *  app.js 里那些 async 函数（prepareRecipe / loadRecipeSchema / sendRecipeMessage）
 *  会连着 await 好几层 fetch → json；`await null` 数次数太脆，而**真**
 *  setTimeout 的回调一定在微任务队列清空之后才跑，所以等一次就够稳。
 *  （window.setTimeout 被桩成同步执行，那是给 app.js 用的，不影响这里。）
 */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

const callsTo = (fragment) => backend.calls.filter((call) => call.url.includes(fragment));
const schemaCalls = () => callsTo('/api/recipe/schema');
const sessionCalls = () => callsTo('/api/recipe/session');
const askCalls = () => callsTo('/api/recipe/ask');
//: 这两个是本轮**删掉**的旧调用；仍保留取数函数，用来断言它们一次都没发生。
const messageCalls = () => callsTo('/api/recipe/message');
const planCalls = () => callsTo('/api/recipe/plan');
const libraryCalls = () => callsTo('/api/recipe/library?');
const detailCalls = () => backend.calls.filter(
  (call) => /\/api\/recipe\/library\/\d+/.test(call.url)
);
const seedCalls = () => callsTo('/api/recipe/library/seed');
//: 右栏标签（第 50 步）：GET 取选项、PUT 存忌口。两者 URL 相同，靠 method 区分。
const preferencesCalls = () => callsTo('/api/recipe/library/preferences')
  .filter((call) => call.method === 'GET');
const preferencesPutCalls = () => callsTo('/api/recipe/library/preferences')
  .filter((call) => call.method === 'PUT');

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
function textsOf(nodes) {
  return nodes.map((node) => node.textContent);
}
function messagesOf(log) {
  return collect(log, (n) => hasClass(n, 'recipe-msg'));
}
function textsByKind(log, kind) {
  return messagesOf(log)
    .filter((n) => hasClass(n, `recipe-msg--${kind}`))
    .map((n) => n.textContent);
}

/** 首页卡片（按钮）列表。 */
function cardsOf(dom) {
  return collect(dom.byId.get('recipe-cards'),
    (n) => hasClass(n, 'recipe-card-item'));
}
/** 每张卡片里的菜名（.recipe-card-name 的文字）。 */
function cardNames(cards) {
  return cards.map((card) => {
    const name = collect(card, (n) => hasClass(n, 'recipe-card-name'))[0];
    return name ? name.textContent : '';
  });
}

/** 回答下面的卡片行 / 卡片（第 50 步 50d）。 */
function answerRowsOf(dom) {
  return collect(dom.byId.get('recipe-log'),
    (n) => hasClass(n, 'recipe-answer-cards'));
}
function answerCardsOf(dom) {
  return collect(dom.byId.get('recipe-log'),
    (n) => hasClass(n, 'recipe-answer-card'));
}
function answerCardNames(cards) {
  return cards.map((card) => {
    const name = collect(card, (n) => hasClass(n, 'recipe-answer-name'))[0];
    return name ? name.textContent : '';
  });
}
function answerCardFoot(card) {
  const foot = collect(card, (n) => hasClass(n, 'recipe-answer-foot'))[0];
  return foot ? foot.textContent : '';
}

/** 右栏标签按钮（第 50 步）：忌口组 / 「这次想要」组。 */
function avoidTagsOf(dom) {
  return collect(dom.byId.get('recipe-tags-avoid'),
    (n) => hasClass(n, 'recipe-tag'));
}
function wantTagsOf(dom) {
  return collect(dom.byId.get('recipe-tags-want'),
    (n) => hasClass(n, 'recipe-tag'));
}
function wantRowsOf(dom) {
  return collect(dom.byId.get('recipe-tags-want'),
    (n) => hasClass(n, 'recipe-want-group'));
}
function avoidTagByTerm(dom, term) {
  return avoidTagsOf(dom).find((node) => node.dataset.term === term) || null;
}
function wantTagByTag(dom, tag) {
  return wantTagsOf(dom).find((node) => node.dataset.tag === tag) || null;
}
/** 桩把 aria-* 存在 attributes 里，用 getAttribute 读，跟浏览器一致。 */
function pressed(node) {
  return node ? node.getAttribute('aria-pressed') : null;
}

/** 食谱窗口那一段 HTML（先剥掉注释，注释里的设计说明不算标记）。 */
function recipeBlock() {
  const start = html.indexOf('id="recipe-modal"');
  const end = html.indexOf('id="pet"');
  if (start < 0 || end <= start) return '';
  return html.slice(start, end).replace(/<!--[\s\S]*?-->/g, '');
}

/** app.js 里从"食谱窗口"分节到 listOf 之间的代码（静态断言用）。 */
function recipeCode() {
  const start = appJs.indexOf('/* -------------------------------------------------- 食谱窗口');
  const end = start < 0 ? -1 : appJs.indexOf('function listOf(', start);
  return start >= 0 && end > start ? appJs.slice(start, end) : '';
}

/** 打开食谱窗口（点胶囊），返回 modal 元素。 */
async function openRecipeWindow(dom) {
  const chips = dom.byId.get('chat-chips');
  const chip = chips.children.find((c) => c.dataset.key === 'recipe');
  chip.click();
  await settle();
  return dom.byId.get('recipe-modal');
}

/** 切到对话页，往输入框填一句再提交。 */
async function sendOneLine(dom, text) {
  dom.byId.get('recipe-chat-entry').click();
  await settle();
  const input = dom.byId.get('recipe-input');
  input.value = text;
  dom.byId.get('recipe-composer').submit();
  await settle();
}

/* ------------------------------------------------------------ 各场景 */

async function scenarioOpenDefaults() {
  console.log('\n==== 1. 点「食谱」胶囊：打开窗口，默认停在首页 ====');
  const dom = boot({});
  const chips = dom.byId.get('chat-chips');
  const chip = chips.children.find((c) => c.dataset.key === 'recipe');

  check('「食谱」胶囊已由 CHIP_ENTRIES 渲染出来', Boolean(chip),
    `胶囊=${JSON.stringify(textsOf(chips.children))}`);
  check('胶囊文字是「食谱」', Boolean(chip) && chip.textContent === '食谱');
  check('胶囊可点（有 panel 就不该是禁用态）',
    Boolean(chip) && chip.disabled === false);
  check('开窗口之前没有取过食谱接口（取数是"打开"触发的）',
    schemaCalls().length === 0 && libraryCalls().length === 0
    && preferencesCalls().length === 0 && sessionCalls().length === 0,
    `schema=${schemaCalls().length} library=${libraryCalls().length} `
    + `preferences=${preferencesCalls().length} session=${sessionCalls().length}`);

  chip.click();
  await settle();

  const modal = dom.byId.get('recipe-modal');
  const home = dom.byId.get('recipe-home');
  const chatPage = dom.byId.get('recipe-chat-page');
  check('食谱窗口打开（hidden=false）', modal.hidden === false, `hidden=${modal.hidden}`);
  check('默认停在首页：首页可见、对话页隐藏',
    home.hidden === false && chatPage.hidden === true,
    `home=${home.hidden} chat=${chatPage.hidden}`);

  // 第 48 步删掉了左栏两项导航与"两页切换"。这里**反过来**断言它们不存在，
  // 免得将来有人又把这套骨架加回去（tests/test_recipe_ui.py 也查 HTML）。
  check('旧的左栏导航 id 已不存在（#recipe-nav / -nav-chat / -nav-plan）',
    ['recipe-nav', 'recipe-nav-chat', 'recipe-nav-plan']
      .every((id) => dom.documentStub.getElementById(id) === null),
    ['recipe-nav', 'recipe-nav-chat', 'recipe-nav-plan']
      .map((id) => `${id}=${dom.documentStub.getElementById(id)}`).join(' '));
  check('旧的两页 id 已不存在（#recipe-page-chat / #recipe-page-plan）',
    ['recipe-page-chat', 'recipe-page-plan']
      .every((id) => dom.documentStub.getElementById(id) === null));
  check('窗口 HTML 里也没有 recipe-nav-* 之类的残留 id',
    !/id="recipe-nav/.test(html));

  // 第 50 步把右栏合并成一个框：旧的两张卡、免责声明、「还差：…」都删了。
  // 按项目规矩，删掉的界面要**反过来断言不存在**，而不是只把旧断言删掉。
  const gone = ['recipe-profile', 'recipe-prompts', 'recipe-disclaimer', 'recipe-missing'];
  check('旧右栏三张卡与「还差：…」都不存在（#recipe-profile / -prompts / -disclaimer / -missing）',
    gone.every((id) => dom.documentStub.getElementById(id) === null),
    gone.map((id) => `${id}=${dom.documentStub.getElementById(id)}`).join(' '));
  check('index.html 里也没有这些残留 id',
    !/id="recipe-(profile|prompts|disclaimer|missing)"/.test(html));
  check('app.js 里不再有 renderRecipeMissing（函数已删）',
    !/renderRecipeMissing/.test(appJs));
  check('右栏两个容器都在（#recipe-tags-avoid / #recipe-tags-want）',
    Boolean(dom.byId.get('recipe-tags-avoid'))
    && Boolean(dom.byId.get('recipe-tags-want')));
  check('两个分组的标题写在 HTML 里（「一直记住 · 不吃」「这次想要」）',
    html.includes('一直记住 · 不吃') && html.includes('这次想要'));

  // 旧的「生成 / 天数 / 计划 / 进度 / 警告 / 空占位」控件也不该在。
  // 逐个场景的完整清单见场景 9，这里只确认打开窗口时它们没有回来。
  const multiDayIds = ['recipe-generate', 'recipe-days', 'recipe-plan'];
  check('旧的「生成食谱 / 天数 / 计划」控件都不在（多天流程已删）',
    multiDayIds.every((id) => dom.documentStub.getElementById(id) === null),
    multiDayIds.map((id) => `${id}=${dom.documentStub.getElementById(id)}`).join(' '));

  check('真的请求了 GET /api/recipe/schema', schemaCalls().length === 1,
    `calls=${schemaCalls().length}`);
  check('schema 是 GET', Boolean(schemaCalls()[0]) && schemaCalls()[0].method === 'GET');
  check('真的请求了 GET /api/recipe/library（首页卡片）',
    libraryCalls().length === 1, `calls=${libraryCalls().length}`);
  check('library 是 GET，且带上 limit=6 与 shuffle=1',
    Boolean(libraryCalls()[0]) && libraryCalls()[0].method === 'GET'
    && libraryCalls()[0].url.includes('limit=6')
    && libraryCalls()[0].url.includes('shuffle=1'),
    libraryCalls()[0] && libraryCalls()[0].url);
  check('真的请求了 GET /api/recipe/library/preferences（右栏标签）',
    preferencesCalls().length === 1 && preferencesCalls()[0].method === 'GET',
    `calls=${preferencesCalls().length}`);
  // 第 50 步：schema → 首页卡片 → 右栏 preferences → 会话恢复，四步依次来。
  // 顺序用 findIndex 取真实下标；四个都必须存在，否则 -1 会让比较意外成立。
  const order = ['/api/recipe/schema', '/api/recipe/library?',
    '/api/recipe/library/preferences', '/api/recipe/session']
    .map((fragment) => backend.calls.findIndex((call) => call.url.includes(fragment)));
  check('取数顺序是 schema → library → preferences → session',
    order.every((index) => index >= 0)
    && order[0] < order[1] && order[1] < order[2] && order[2] < order[3],
    backend.calls.map((call) => call.url).join(' → '));

  check('真的请求了 GET /api/recipe/session', sessionCalls().length === 1,
    `calls=${sessionCalls().length}`);
  check('session 问的是 localStorage 里那个食谱会话 id',
    Boolean(sessionCalls()[0])
    && sessionCalls()[0].url.includes(`session_id=${RECIPE_SESSION_ID}`),
    sessionCalls()[0] && sessionCalls()[0].url);
  check('食谱会话不跟聊天共用 id',
    Boolean(sessionCalls()[0]) && !sessionCalls()[0].url.includes(CHAT_SESSION_ID));

  // 打开时只会恢复历史文字，不会凭空画回答卡片。
  check('刚打开时对话区里没有回答卡片行', answerRowsOf(dom).length === 0,
    `rows=${answerRowsOf(dom).length}`);
  check('开场白已经画进对话区（空会话时来自 schema.greeting）',
    textsByKind(dom.byId.get('recipe-log'), 'bot').join('') === GREETING,
    JSON.stringify(textsByKind(dom.byId.get('recipe-log'), 'bot')));

  const cards = cardsOf(dom);
  check('首页卡片渲染出 6 张', cards.length === 6, `cards=${cards.length}`);
  check('每张卡片都是 <button>（整张卡片可点，不是只有菜名可点）',
    cards.length === 6 && cards.every(
      (card) => card.tagName === 'BUTTON' && card.type === 'button'
    ));
  check('每张卡片都有菜名，且与 /api/recipe/library 返回的一致',
    cardNames(cards).join('|') === LIBRARY.map((recipe) => recipe.name).join('|'),
    cardNames(cards).join('|'));
  check('「换一批」可见且可点，卡片空态隐藏',
    dom.byId.get('recipe-refresh').hidden === false
    && dom.byId.get('recipe-refresh').disabled === false
    && dom.byId.get('recipe-cards-empty').hidden === true,
    `refresh=${dom.byId.get('recipe-refresh').hidden}`
    + ` empty=${dom.byId.get('recipe-cards-empty').hidden}`);

  // 缩略图（第 59 步）：卡片顶部一张小图，src 指向该菜的图片接口。
  // 图片来自独立的只读图片库，按菜名关联——所以这里只断言接口路径，
  // 图本身的正确性由 Python 侧（test_recipe_images）钉住。
  const thumbs = cards.map((card) => collect(card, (n) => hasClass(n, 'recipe-card-thumb'))[0]);
  check('每张卡片顶部都有缩略图', thumbs.every(Boolean),
    thumbs.map((thumb) => (thumb ? 'ok' : '缺')).join(','));
  check('缩略图的 src 指向该菜的图片接口',
    thumbs.every((thumb, index) => thumb
      && thumb.src === `/api/recipe/library/${LIBRARY[index].id}/image`),
    thumbs.map((thumb) => thumb && thumb.src).join(' | '));
  check('缩略图是懒加载（一屏 6 张不要一起抢带宽）',
    thumbs.every((thumb) => thumb && thumb.loading === 'lazy'));

  // 加载失败 → 缩略图移除、卡片加 --no-photo，文字区撑满
  const firstCard = cards[0];
  const firstThumb = thumbs[0];
  const errorHandlers = (firstThumb._handlers && firstThumb._handlers.error) || [];
  check('缩略图挂了 error 处理', errorHandlers.length === 1,
    `handlers=${errorHandlers.length}`);
  if (errorHandlers.length === 1) {
    errorHandlers[0]({ target: firstThumb });
    check('图片加载失败时缩略图被移除',
      collect(firstCard, (n) => hasClass(n, 'recipe-card-thumb')).length === 0);
    check('图片加载失败时卡片打上 --no-photo（文字区居中撑满）',
      hasClass(firstCard, 'recipe-card-item--no-photo'));
    check('菜名还在（不能因为没图就把卡片搞空）',
      collect(firstCard, (n) => hasClass(n, 'recipe-card-name')).length === 1);
  }
}

async function scenarioSessionKey() {
  console.log('\n==== 1b. 本地没存过 id 时：app.js 自己生成并写回 pet.recipe_session ====');
  const dom = boot({ storage: {} });
  check('预置存储确实是空的',
    dom.local.get(RECIPE_SESSION_KEY) === undefined);

  await openRecipeWindow(dom);

  const stored = dom.local.get(RECIPE_SESSION_KEY);
  check('打开窗口后 pet.recipe_session 被写进 localStorage',
    typeof stored === 'string' && stored.length > 0, `stored=${stored}`);
  check('请求里带的就是刚写进去的那个 id',
    Boolean(sessionCalls()[0])
    && sessionCalls()[0].url.includes(`session_id=${stored}`),
    sessionCalls()[0] && sessionCalls()[0].url);
}

async function scenarioLibraryMatch() {
  console.log('\n==== 2. 卡片内容与 GET /api/recipe/library 的返回一致 ====');
  const dom = boot({});
  await openRecipeWindow(dom);

  const cards = cardsOf(dom);
  check('每张卡片带 data-id，且与后端返回的 id 一一对应',
    cards.map((card) => card.dataset.id).join('|')
      === LIBRARY.map((recipe) => String(recipe.id)).join('|'),
    cards.map((card) => card.dataset.id).join('|'));
  check('卡片菜名顺序与后端返回一致',
    cardNames(cards).join('|') === LIBRARY.map((recipe) => recipe.name).join('|'));
  check('卡片副标题把时间与首个标签拼起来（来自后端字段）',
    textsOf(collect(dom.byId.get('recipe-cards'),
      (n) => hasClass(n, 'recipe-card-foot'))).join('|')
      === LIBRARY.map((recipe) => `${recipe.time_minutes} 分钟 · ${recipe.tags[0]}`).join('|'),
    textsOf(collect(dom.byId.get('recipe-cards'),
      (n) => hasClass(n, 'recipe-card-foot'))).join('|'));
}

async function scenarioRefresh() {
  console.log('\n==== 3. 「换一批」：再发一次 library，卡片内容真的变了 ====');
  const dom = boot({ libraryBatches: [LIBRARY, LIBRARY_ALT] });
  await openRecipeWindow(dom);

  const beforeNames = cardNames(cardsOf(dom));
  check('（前置）第一批是 LIBRARY', beforeNames.join('|')
    === LIBRARY.map((recipe) => recipe.name).join('|'));

  dom.byId.get('recipe-refresh').click();
  await settle();

  check('点「换一批」又发了一次 GET /api/recipe/library',
    libraryCalls().length === 2, `calls=${libraryCalls().length}`);
  check('第二次仍然带 limit=6 与 shuffle=1',
    Boolean(libraryCalls()[1])
    && libraryCalls()[1].url.includes('limit=6')
    && libraryCalls()[1].url.includes('shuffle=1'),
    libraryCalls()[1] && libraryCalls()[1].url);

  const afterCards = cardsOf(dom);
  check('卡片换成了假后端给的第二批（菜名变了）',
    cardNames(afterCards).join('|')
      === LIBRARY_ALT.map((recipe) => recipe.name).join('|'),
    cardNames(afterCards).join('|'));
  check('data-id 也跟着换成第二批',
    afterCards.map((card) => card.dataset.id).join('|')
      === LIBRARY_ALT.map((recipe) => String(recipe.id)).join('|'),
    afterCards.map((card) => card.dataset.id).join('|'));
  check('换完后「换一批」恢复可点', dom.byId.get('recipe-refresh').disabled === false);
}

async function scenarioEmptyLibrary() {
  console.log('\n==== 4. 空库：空态 + 载入示例菜谱 → POST seed → 重新拉卡片 ====');
  const dom = boot({ library: [] });
  await openRecipeWindow(dom);

  const cardsBox = dom.byId.get('recipe-cards');
  const emptyBox = dom.byId.get('recipe-cards-empty');
  const refresh = dom.byId.get('recipe-refresh');
  const seed = dom.byId.get('recipe-seed');

  check('空库时显示 #recipe-cards-empty', emptyBox.hidden === false,
    `hidden=${emptyBox.hidden}`);
  check('空库时卡片栏与「换一批」都隐藏',
    cardsBox.hidden === true && refresh.hidden === true,
    `cards=${cardsBox.hidden} refresh=${refresh.hidden}`);
  check('卡片栏里没有残留卡片', cardsOf(dom).length === 0,
    `cards=${cardsOf(dom).length}`);
  check('「载入示例菜谱」按钮可见且可点',
    seed.hidden === false && seed.disabled === false,
    `hidden=${seed.hidden} disabled=${seed.disabled}`);
  check('空态里没有发过 seed 请求', seedCalls().length === 0);

  seed.click();
  await settle();

  check('点「载入示例菜谱」发了 POST /api/recipe/library/seed',
    seedCalls().length === 1 && seedCalls()[0].method === 'POST',
    seedCalls()[0] && `${seedCalls()[0].method} ${seedCalls()[0].url}`);
  check('seed 之后重新拉了一次卡片',
    libraryCalls().length === 2, `calls=${libraryCalls().length}`);
  check('卡片出现了 6 张、菜名来自种子批次',
    cardsOf(dom).length === 6
    && cardNames(cardsOf(dom)).join('|')
      === LIBRARY.map((recipe) => recipe.name).join('|'),
    cardNames(cardsOf(dom)).join('|'));
  check('空态隐藏、卡片栏与「换一批」恢复显示',
    emptyBox.hidden === true && cardsBox.hidden === false && refresh.hidden === false,
    `empty=${emptyBox.hidden} cards=${cardsBox.hidden} refresh=${refresh.hidden}`);
  check('seed 按钮恢复可点', seed.disabled === false);
}

async function scenarioCardDetail() {
  console.log('\n==== 5. 卡片详情浮层：拉详情、渲染材料/做法、三种关法 ====');
  const dom = boot({});
  await openRecipeWindow(dom);

  const detail = dom.byId.get('recipe-detail');
  const card = cardsOf(dom)[2];          // id=13 蒜蓉油麦菜
  card.click();
  await settle();

  check('卡片请求了 GET /api/recipe/library/{id}',
    detailCalls().length === 1 && detailCalls()[0].method === 'GET'
    && detailCalls()[0].url.includes(`/api/recipe/library/${LIBRARY[2].id}`),
    detailCalls()[0] && detailCalls()[0].url);
  check('详情浮层显示出来', detail.hidden === false, `hidden=${detail.hidden}`);
  check('浮层标题就是点的那张卡片的菜名',
    dom.byId.get('recipe-detail-title').textContent === LIBRARY[2].name,
    dom.byId.get('recipe-detail-title').textContent);
  check('meta 里写了时间与标签',
    dom.byId.get('recipe-detail-meta').textContent.includes('约 10 分钟')
    && dom.byId.get('recipe-detail-meta').textContent.includes('素菜'),
    dom.byId.get('recipe-detail-meta').textContent);
  check('source=seed 时 meta 里明确写了「示例菜谱」',
    dom.byId.get('recipe-detail-meta').textContent.includes('示例菜谱'),
    dom.byId.get('recipe-detail-meta').textContent);

  const body = dom.byId.get('recipe-detail-body');
  check('两段小标题是「所需材料」「制作流程」',
    textsOf(collect(body, (n) => hasClass(n, 'recipe-detail-section-title'))).join('|')
      === '所需材料|制作流程',
    textsOf(collect(body, (n) => hasClass(n, 'recipe-detail-section-title'))).join('|'));
  const lists = collect(body, (n) => hasClass(n, 'recipe-detail-list'));
  check('材料逐条渲染',
    lists.length === 2
    && textsOf(lists[0].children).join('|') === LIBRARY[2].ingredients.join('|'),
    lists[0] && textsOf(lists[0].children).join('|'));
  check('做法逐条渲染',
    lists.length === 2
    && textsOf(lists[1].children).join('|') === LIBRARY[2].steps.join('|'),
    lists[1] && textsOf(lists[1].children).join('|'));
  check('note 也渲染出来', textsOf(collect(body, (n) => hasClass(n, 'recipe-detail-note')))
    .includes(LIBRARY[2].note));

  // 实拍图（第 58 步）：来自独立图片库，src 指向 /library/{id}/image，
  // 并且**加载失败时整块移除**（没图的菜不该留个破图占位）。
  const figures = collect(body, (n) => n.tagName === 'FIGURE'
    && hasClass(n, 'recipe-detail-photo'));
  check('详情页有图片块', figures.length === 1, `figures=${figures.length}`);
  if (figures.length) {
    const img = collect(figures[0], (n) => n.tagName === 'IMG')[0];
    check('img 的 src 指向该菜的图片接口',
      Boolean(img) && img.src === `/api/recipe/library/${LIBRARY[2].id}/image`,
      img && img.src);
    check('图片带出处说明',
      textsOf(collect(figures[0], (n) => hasClass(n, 'recipe-detail-photo-note')))
        .join('').includes('网络检索'));
    // 模拟加载失败：整块应当消失，详情页回到纯文字。
    // 注意取 `_handlers`（stub 的 addEventListener 存在这里）；
    // 之前写 `__handlers` 取不到，断言其实没真正跑过——这类"假绿"必须避免。
    const detailErrorHandlers = (img && img._handlers && img._handlers.error) || [];
    check('详情图挂了 error 处理', detailErrorHandlers.length === 1,
      `handlers=${detailErrorHandlers.length}`);
    if (detailErrorHandlers.length === 1) {
      detailErrorHandlers[0]({ target: img });
      check('图片加载失败时整块移除（不留破图）',
        collect(body, (n) => hasClass(n, 'recipe-detail-photo')).length === 0);
    }
  }

  dom.byId.get('recipe-detail-close').click();
  check('点 × 关掉浮层，食谱窗口还开着',
    detail.hidden === true && dom.byId.get('recipe-modal').hidden === false,
    `detail=${detail.hidden} modal=${dom.byId.get('recipe-modal').hidden}`);

  // 再开一次，点浮层的空白处（event.target === 浮层本身）应当关闭；
  // 点内容里的小标题则不该关闭（这里只测空白处这条分支）。
  cardsOf(dom)[0].click();
  await settle();
  check('（前置）浮层重新打开', detail.hidden === false);
  detail.click(detail);
  check('点浮层空白处也能关',
    detail.hidden === true && dom.byId.get('recipe-modal').hidden === false);

  // Esc 的优先级：详情浮层盖在食谱窗口内部，一次 Esc 只收浮层，
  // 不能顺手调用 closeDialog() 把整个食谱窗口一起关掉（app.js:2545）。
  cardsOf(dom)[0].click();
  await settle();
  check('（前置）浮层又打开了', detail.hidden === false);
  dom.documentStub.pressEscape();
  check('Esc 关掉详情浮层', detail.hidden === true, `hidden=${detail.hidden}`);
  check('Esc 没有顺手关掉整个食谱窗口',
    dom.byId.get('recipe-modal').hidden === false,
    `modal=${dom.byId.get('recipe-modal').hidden}`);

  // 回归：详情浮层是"窗口内部的覆盖层"，不是独立 modal。曾经关窗时只隐藏
  // #recipe-modal、不复位 #recipe-detail，于是重开窗口会有一层旧详情直接
  // 盖在首页上。closeDialog 现在会一并 closeRecipeDetail()（app.js:406-417）。
  cardsOf(dom)[0].click();
  await settle();
  check('（回归前置）浮层打开着，准备关窗', detail.hidden === false);
  dom.byId.get('recipe-close').click();
  check('（回归）关窗时食谱窗口隐藏、详情浮层一起复位',
    dom.byId.get('recipe-modal').hidden === true && detail.hidden === true,
    `modal=${dom.byId.get('recipe-modal').hidden} detail=${detail.hidden}`);

  await openRecipeWindow(dom);
  check('（回归）重开窗口后 #recipe-detail 仍是 hidden（旧详情不盖在首页上）',
    detail.hidden === true, `hidden=${detail.hidden}`);
  check('（回归）重开后首页正常可见',
    dom.byId.get('recipe-home').hidden === false
    && dom.byId.get('recipe-chat-page').hidden === true,
    `home=${dom.byId.get('recipe-home').hidden}`
    + ` chat=${dom.byId.get('recipe-chat-page').hidden}`);
}

async function scenarioHomeChatSwitch() {
  console.log('\n==== 6. 首页 → 对话页（滑走动画）→ 返回首页 ====');
  const dom = boot({});
  await openRecipeWindow(dom);

  const home = dom.byId.get('recipe-home');
  const chatPage = dom.byId.get('recipe-chat-page');
  const entry = dom.byId.get('recipe-chat-entry');
  const input = dom.byId.get('recipe-input');
  const fetchesBefore = backend.calls.length;

  // 桩不解析 HTML 的层级与静态文字（只按 id 造空元素），所以按钮里
  // <span>您好，想吃点什么？</span> 这层文案要去 HTML 源码里确认。
  const entryHtmlTail = html.slice(html.indexOf('id="recipe-chat-entry"'));
  const entryHtml = entryHtmlTail.slice(0, entryHtmlTail.indexOf('</button>'));
  check('对话入口文案是「您好，想吃点什么？」',
    entryHtml.includes('您好，想吃点什么？'), entryHtml.replace(/\s+/g, ' ').trim());
  check('（前置）首页可见、对话页隐藏',
    home.hidden === false && chatPage.hidden === true);

  entry.click();
  await settle();

  // 桩里的 window.setTimeout 同步执行，所以这里看的是"过渡结束后"的结果；
  // 为了证明 .is-leaving 真的加上过，makeElement 记录了 classList 的真实变化。
  check('首页曾被加上 .is-leaving（滑走动画的 class）',
    home._classLog.includes('+is-leaving'), home._classLog.join(' '));
  check('.is-leaving 在过渡结束后被移除',
    home._classLog.includes('-is-leaving') && !home.classList.contains('is-leaving'),
    home._classLog.join(' '));
  check('过渡结束后首页隐藏、对话页显示',
    home.hidden === true && chatPage.hidden === false,
    `home=${home.hidden} chat=${chatPage.hidden}`);
  check('对话输入框自动聚焦', input._focused === true);
  check('切页本身不发请求（纯前端切 hidden）',
    backend.calls.length === fetchesBefore, `${fetchesBefore} → ${backend.calls.length}`);

  dom.byId.get('recipe-back').click();
  await settle();

  check('点 #recipe-back 回到首页：对话页隐藏、首页显示',
    home.hidden === false && chatPage.hidden === true,
    `home=${home.hidden} chat=${chatPage.hidden}`);
  check('返回后首页不再带 .is-leaving', !home.classList.contains('is-leaving'),
    home.className);
}

/* 右栏（第 50 步）只有两组标签按钮。下面四个场景覆盖：
 *   7  渲染（选项、分组、已记住的忌口）
 *   7b 忌口：PUT 保存 → 用响应重画 → 取消
 *   7c 忌口：PUT 失败 → 退回原样
 *   7d 「这次想要」：纯前端切换，不发请求
 */

async function scenarioTagPanel() {
  console.log('\n==== 7. 右栏标签框：忌口按 avoid_options、这次想要按 want_groups 渲染 ====');
  const dom = boot({ aversions: ['香菜', '海鲜'] });
  await openRecipeWindow(dom);

  const avoidBox = dom.byId.get('recipe-tags-avoid');
  const wantBox = dom.byId.get('recipe-tags-want');
  const avoid = avoidTagsOf(dom);
  const wants = wantTagsOf(dom);
  const rows = wantRowsOf(dom);

  check('（前置）右栏两个容器都在',
    Boolean(avoidBox) && Boolean(wantBox) && avoidBox !== wantBox);

  check('忌口组按 avoid_options 渲染出同样多的按钮',
    avoid.length === AVOID_OPTIONS.length, `buttons=${avoid.length}`);
  check('忌口按钮文案是「不吃X」，dataset.term 是词条本身',
    textsOf(avoid).join('|') === AVOID_OPTIONS.map((term) => `不吃${term}`).join('|')
    && avoid.map((node) => node.dataset.term).join('|') === AVOID_OPTIONS.join('|'),
    textsOf(avoid).join('|'));
  check('忌口按钮都是 <button type=button>',
    avoid.every((node) => node.tagName === 'BUTTON' && node.type === 'button'));

  // 后端记住的两个词条应当一打开就亮着（.is-on + aria-pressed=true）
  const litAvoid = avoid.filter((node) => hasClass(node, 'is-on'));
  check('后端记住的两个忌口亮着（.is-on + aria-pressed=true）',
    litAvoid.map((node) => node.dataset.term).join('|') === '香菜|海鲜'
    && litAvoid.every((node) => pressed(node) === 'true'),
    `lit=${litAvoid.map((node) => node.dataset.term).join('|')}`);
  check('没记住的忌口都是 aria-pressed=false 且没有 .is-on',
    avoid.filter((node) => !hasClass(node, 'is-on')).length
      === AVOID_OPTIONS.length - 2
    && avoid.filter((node) => !hasClass(node, 'is-on'))
      .every((node) => pressed(node) === 'false'));

  check('「这次想要」按 want_groups 分行（每组一行）',
    rows.length === WANT_GROUPS.length, `rows=${rows.length}`);
  check('每行的组标题与 want_groups.label 一致（顺序也一致）',
    textsOf(collect(wantBox, (node) => hasClass(node, 'recipe-want-label'))).join('|')
      === WANT_GROUPS.map(([label]) => label).join('|'),
    textsOf(collect(wantBox, (node) => hasClass(node, 'recipe-want-label'))).join('|'));
  const rowsOk = rows.every((row, index) => {
    const tags = collect(row, (node) => hasClass(node, 'recipe-tag'));
    return tags.map((node) => node.dataset.tag).join('|')
      === WANT_GROUPS[index][1].join('|')
      && textsOf(tags).join('|') === WANT_GROUPS[index][1].join('|');
  });
  check('每组标签的文案与 dataset.tag 都和 want_groups.tags 一致', rowsOk);
  check('「这次想要」的按钮总数等于四组之和',
    wants.length === WANT_GROUPS.reduce((sum, [, tags]) => sum + tags.length, 0),
    `buttons=${wants.length}`);
  check('「这次想要」初始都没选中（state.recipeWanted 是空的）',
    wants.every((node) => !hasClass(node, 'is-on') && pressed(node) === 'false'));

  check('两组选项都来自后端：GET preferences 只发了一次',
    preferencesCalls().length === 1, `calls=${preferencesCalls().length}`);
  check('渲染标签本身没有再发 PUT', preferencesPutCalls().length === 0,
    `puts=${preferencesPutCalls().length}`);
}

async function scenarioStatedAversionButton() {
  console.log('\n==== 7e. 词表外的忌口也要能看见、能取消（第 64 步）====');
  // 「花生」不在 AVOID_OPTIONS 里 —— 它是"用户在对话里说出来的"那种词。
  // 改之前它会被存进库、每轮生效，但右栏没有它的按钮，也就永远删不掉。
  const dom = boot({ aversions: ['花生'] });
  await openRecipeWindow(dom);

  const stated = avoidTagByTerm(dom, '花生');
  check('词表外的忌口也出成了按钮',
    Boolean(stated) && stated.textContent === '不吃花生',
    textsOf(avoidTagsOf(dom)).join('|'));
  check('按钮总数 = 固定 8 个 + 词表外 1 个',
    avoidTagsOf(dom).length === AVOID_OPTIONS.length + 1,
    `buttons=${avoidTagsOf(dom).length}`);
  check('标了来源（dataset.origin=stated），用户才知道它为什么在这儿',
    Boolean(stated) && stated.dataset.origin === 'stated',
    stated ? `origin=${stated.dataset.origin}` : '(没有这个按钮)');
  check('它一进来就是亮的（因为库里确实有它）',
    Boolean(stated) && hasClass(stated, 'is-on') && pressed(stated) === 'true');

  // 点掉它 —— 这才是"取消勾选即删除"覆盖到**全部**词条的证据
  stated.click();
  check('点它就发 PUT', preferencesPutCalls().length === 1,
    `puts=${preferencesPutCalls().length}`);
  const put = preferencesPutCalls()[0];
  check('PUT body 里已经没有「花生」（整份替换，不靠逐条 DELETE）',
    !JSON.parse(put.body).aversions.includes('花生'), put.body);

  await settle();
  check('响应回来后这个词的按钮消失了',
    !avoidTagByTerm(dom, '花生'), textsOf(avoidTagsOf(dom)).join('|'));
  check('剩下还是那 8 个固定词',
    avoidTagsOf(dom).length === AVOID_OPTIONS.length,
    `buttons=${avoidTagsOf(dom).length}`);
  check('后端也真的删掉了', backend.aversions.length === 0,
    `backend=${JSON.stringify(backend.aversions)}`);
}

async function scenarioAvoidToggle() {  console.log('\n==== 7b. 点忌口 → PUT 保存 → 用响应重画；再点一次取消 ====');
  const dom = boot({ aversions: [] });
  await openRecipeWindow(dom);

  const before = avoidTagByTerm(dom, '香菜');
  check('（前置）香菜一开始没选中',
    Boolean(before) && !hasClass(before, 'is-on') && pressed(before) === 'false');

  before.click();
  check('点忌口立刻发 PUT /api/recipe/library/preferences',
    preferencesPutCalls().length === 1,
    `puts=${preferencesPutCalls().length}`);
  const firstPut = preferencesPutCalls()[0];
  check('请求方法是 PUT，且 body 里 aversions 含刚点的词条',
    firstPut.method === 'PUT'
    && JSON.parse(firstPut.body).aversions.includes('香菜'),
    `method=${firstPut.method} body=${firstPut.body}`);
  check('body 里 aversions 是"当前亮着的全部"（此刻只有香菜）',
    JSON.parse(firstPut.body).aversions.join('|') === '香菜',
    firstPut.body);
  check('保存时不再重复 GET preferences（选项表已经在手里）',
    preferencesCalls().length === 1, `gets=${preferencesCalls().length}`);
  // 乐观更新：请求还没回来按钮就先亮起来（响应回来后再用后端结果重画）
  check('点下去先乐观点亮（不等响应）',
    hasClass(avoidTagByTerm(dom, '香菜'), 'is-on'));
  const optimisticNode = avoidTagByTerm(dom, '香菜');

  await settle();
  const settledNode = avoidTagByTerm(dom, '香菜');
  check('成功后用 PUT 的响应重画了整个框（按钮节点被换新，不是只留乐观 class）',
    Boolean(settledNode) && settledNode !== optimisticNode);
  check('用 PUT 的响应重画后仍然带 .is-on',
    hasClass(settledNode, 'is-on'));
  check('aria-pressed 也变成 true', pressed(settledNode) === 'true');
  check('后端记住了这个词条', backend.aversions.join('|') === '香菜',
    `backend=${backend.aversions.join('|')}`);
  check('重画后按钮数量不变（没有重复铺）',
    avoidTagsOf(dom).length === AVOID_OPTIONS.length,
    `buttons=${avoidTagsOf(dom).length}`);

  // 再点一次：取消选中，整份提交空列表
  avoidTagByTerm(dom, '香菜').click();
  check('再点一次又发了一次 PUT', preferencesPutCalls().length === 2,
    `puts=${preferencesPutCalls().length}`);
  const secondPut = preferencesPutCalls()[1];
  check('第二次 body 里不再包含香菜',
    !JSON.parse(secondPut.body).aversions.includes('香菜'), secondPut.body);

  await settle();
  check('响应回来后 .is-on 消失',
    !hasClass(avoidTagByTerm(dom, '香菜'), 'is-on'));
  check('aria-pressed 回到 false',
    pressed(avoidTagByTerm(dom, '香菜')) === 'false');
  check('后端也清空了', backend.aversions.length === 0,
    `backend=${JSON.stringify(backend.aversions)}`);
  check('全程没碰对话 / 旧生成接口',
    askCalls().length === 0 && messageCalls().length === 0 && planCalls().length === 0,
    `ask=${askCalls().length} message=${messageCalls().length} plan=${planCalls().length}`);
}

async function scenarioAvoidRollback() {
  console.log('\n==== 7c. PUT 失败（500）→ 界面退回原样，并显示 #recipe-error ====');
  const dom = boot({
    aversions: ['海鲜'],
    preferencesPutFails: { status: 500, detail: '忌口没存上，稍后再试一次？' },
  });
  await openRecipeWindow(dom);

  const error = dom.byId.get('recipe-error');
  check('（前置）海鲜亮着、错误提示是收起的',
    hasClass(avoidTagByTerm(dom, '海鲜'), 'is-on') && error.hidden === true);

  avoidTagByTerm(dom, '香菜').click();
  check('（前置）请求确实发出去了，body 里两个词条',
    preferencesPutCalls().length === 1
    && JSON.parse(preferencesPutCalls()[0].body).aversions.join('|') === '海鲜|香菜',
    preferencesPutCalls()[0] && preferencesPutCalls()[0].body);
  await settle();

  check('失败后香菜退回未选中',
    !hasClass(avoidTagByTerm(dom, '香菜'), 'is-on'));
  check('失败后香菜的 aria-pressed 也退回 false',
    pressed(avoidTagByTerm(dom, '香菜')) === 'false');
  check('原来选中的海鲜仍然是亮的（不是把整栏清空）',
    hasClass(avoidTagByTerm(dom, '海鲜'), 'is-on')
    && pressed(avoidTagByTerm(dom, '海鲜')) === 'true');
  check('亮着的仍然只有一个（没留下乐观更新）',
    avoidTagsOf(dom).filter((node) => hasClass(node, 'is-on')).length === 1,
    `lit=${avoidTagsOf(dom).filter((node) => hasClass(node, 'is-on')).length}`);
  check('#recipe-error 显示后端给的 detail',
    error.hidden === false && error.textContent === '忌口没存上，稍后再试一次？',
    `hidden=${error.hidden} text=${error.textContent}`);
  check('后端状态没被改（失败就是没存）', backend.aversions.join('|') === '海鲜',
    `backend=${backend.aversions.join('|')}`);

  // 取消勾选也一样要能回滚：点海鲜 → PUT 失败 → 它还得亮着
  avoidTagByTerm(dom, '海鲜').click();
  await settle();
  check('取消勾选的请求确实发了（body 是空数组）',
    preferencesPutCalls().length === 2
    && JSON.parse(preferencesPutCalls()[1].body).aversions.length === 0,
    `puts=${preferencesPutCalls().length}`);
  check('取消失败后海鲜仍是亮的（回滚方向对称）',
    hasClass(avoidTagByTerm(dom, '海鲜'), 'is-on'));
}

async function scenarioWantToggle() {
  console.log('\n==== 7d. 「这次想要」：纯前端切换选中态，不发任何请求 ====');
  const dom = boot({});
  await openRecipeWindow(dom);

  const callsBefore = backend.calls.length;
  check('（前置）清淡 / 微辣 一开始都没选中',
    Boolean(wantTagByTag(dom, '清淡')) && Boolean(wantTagByTag(dom, '微辣'))
    && !hasClass(wantTagByTag(dom, '清淡'), 'is-on')
    && !hasClass(wantTagByTag(dom, '微辣'), 'is-on'));

  // 每次点击都会 replaceChildren 重画，所以断言时要**重新取节点**，
  // 不能拿点击之前那个已经被换掉的引用。
  wantTagByTag(dom, '清淡').click();
  check('点一下选中：加 .is-on', hasClass(wantTagByTag(dom, '清淡'), 'is-on'));
  check('aria-pressed 同步变成 true',
    pressed(wantTagByTag(dom, '清淡')) === 'true');
  check('真的一个请求都没发', backend.calls.length === callsBefore,
    `${callsBefore} → ${backend.calls.length}`);

  wantTagByTag(dom, '微辣').click();
  check('同组再选一个：两个都亮着',
    hasClass(wantTagByTag(dom, '清淡'), 'is-on')
    && hasClass(wantTagByTag(dom, '微辣'), 'is-on'));
  check('选第二个标签也不发请求', backend.calls.length === callsBefore,
    `${callsBefore} → ${backend.calls.length}`);

  wantTagByTag(dom, '清淡').click();
  check('再点一次取消：清淡的 .is-on 消失',
    !hasClass(wantTagByTag(dom, '清淡'), 'is-on'));
  check('取消后 aria-pressed 回到 false',
    pressed(wantTagByTag(dom, '清淡')) === 'false');
  check('没被取消的那个还亮着（不是整组重来）',
    hasClass(wantTagByTag(dom, '微辣'), 'is-on'));
  check('取消也不发请求', backend.calls.length === callsBefore,
    `${callsBefore} → ${backend.calls.length}`);

  // 换个组再选一个（原来用的是「工具」组的小电锅，50f 后那类菜已经没有了，
  // 换成「菜系」组的川菜）
  wantTagByTag(dom, '川菜').click();
  check('别的组也能单独选，互不影响',
    hasClass(wantTagByTag(dom, '川菜'), 'is-on')
    && hasClass(wantTagByTag(dom, '微辣'), 'is-on'));
  check('「这次想要」没有牵连忌口组（没 PUT、没有亮起的忌口）',
    preferencesPutCalls().length === 0
    && avoidTagsOf(dom).every((node) => !hasClass(node, 'is-on')));
  check('没有冒出错误提示', dom.byId.get('recipe-error').hidden === true);

  // ---- 第 52 步：互斥组（时间）组内只能亮一个 ----
  // 用户要求："三个标签只能亮一个，不能一起选"
  const beforeExclusive = backend.calls.length;
  wantTagByTag(dom, '15 分钟内').click();
  check('（前置）时间组的「15 分钟内」亮着',
    hasClass(wantTagByTag(dom, '15 分钟内'), 'is-on'));

  wantTagByTag(dom, '30 分钟以上').click();
  check('选「30 分钟以上」后，「15 分钟内」自动灭掉',
    !hasClass(wantTagByTag(dom, '15 分钟内'), 'is-on'));
  check('互斥组里新选的那个亮着',
    hasClass(wantTagByTag(dom, '30 分钟以上'), 'is-on'));
  check('互斥组里只亮一个',
    ['15 分钟内', '30 分钟内', '30 分钟以上']
      .filter((tag) => hasClass(wantTagByTag(dom, tag), 'is-on')).length === 1);
  check('互斥切换也不发请求', backend.calls.length === beforeExclusive,
    `${beforeExclusive} → ${backend.calls.length}`);

  // 非互斥组不受影响：口味可以多选
  // 注意「微辣」在前面场景里就已经点亮了，这里只补一个「清淡」，
  // 断言两者同时亮着（再点一次微辣反而会把它关掉——第一版就是这么写错的）
  wantTagByTag(dom, '清淡').click();
  check('非互斥组（口味）仍然可以两个都亮',
    hasClass(wantTagByTag(dom, '清淡'), 'is-on')
    && hasClass(wantTagByTag(dom, '微辣'), 'is-on'));
  check('时间组互斥不影响口味组',
    hasClass(wantTagByTag(dom, '30 分钟以上'), 'is-on'));
}

async function scenarioAskFlow() {
  console.log('\n==== 8. 发一句 → POST /api/recipe/ask → 回复气泡 + 真实菜谱卡片 ====');
  // 挂起第一次 /ask：这样能在响应回来之前检查"用户气泡已经加上、卡片还没画"，
  // 也能验证发送中按钮禁用、第二次提交被挡下。
  const pending = { seen: 0, url: null, method: null, body: null, release: null };
  const controlledFetch = (url, options) => {
    if (String(url).includes('/api/recipe/ask') && pending.seen === 0) {
      pending.seen += 1;
      pending.url = String(url);
      pending.method = (options && options.method) || 'GET';
      pending.body = options && options.body;
      return new Promise((resolve) => {
        pending.release = () => resolve(backendFetch(url, options));
      });
    }
    return backendFetch(url, options);
  };

  const dom = boot({ askReply: DEFAULT_ASK_REPLY, askRecipes: ASK_RECIPES }, controlledFetch);
  await openRecipeWindow(dom);
  dom.byId.get('recipe-chat-entry').click();
  await settle();

  const log = dom.byId.get('recipe-log');
  const input = dom.byId.get('recipe-input');
  const send = dom.byId.get('recipe-send');
  const error = dom.byId.get('recipe-error');

  input.value = '晚饭想吃鱼，清淡一点';
  dom.byId.get('recipe-composer').submit();

  check('提交真的发出 POST /api/recipe/ask（请求已挂起）',
    Boolean(pending.url) && pending.url.includes('/api/recipe/ask'), `url=${pending.url}`);
  check('方法是 POST（不是旧的 GET/POST /message）', pending.method === 'POST',
    `method=${pending.method}`);
  const sentBody = JSON.parse(pending.body || '{}');
  check('body.session_id 是食谱会话 id',
    sentBody.session_id === RECIPE_SESSION_ID, `body=${pending.body}`);
  check('body.message 就是输入框里那句话',
    sentBody.message === '晚饭想吃鱼，清淡一点', `body=${pending.body}`);
  check('body 里没有旧的多天字段（days）', !('days' in sentBody),
    `keys=${Object.keys(sentBody).join(',')}`);
  // 第 50 步 50e：多了一个 wanted（右栏「这次想要」的勾选），
  // 除此之外不该再塞别的东西。
  check('请求体是 session_id + message + wanted',
    Object.keys(sentBody).sort().join(',') === 'message,session_id,wanted',
    `keys=${Object.keys(sentBody).sort().join(',')}`);
  check('wanted 来自右栏勾选（数组）', Array.isArray(sentBody.wanted),
    `wanted=${JSON.stringify(sentBody.wanted)}`);

  check('用户气泡先加上了（响应还没回来）',
    textsByKind(log, 'user').join('|') === '晚饭想吃鱼，清淡一点',
    `users=${JSON.stringify(textsByKind(log, 'user'))}`);
  check('响应没回来时不提前显示助手回复（只有开场白）',
    textsByKind(log, 'bot').length === 1,
    `bots=${JSON.stringify(textsByKind(log, 'bot'))}`);
  check('响应没回来时不渲染回答卡片', answerCardsOf(dom).length === 0,
    `cards=${answerCardsOf(dom).length}`);
  check('发送中按钮禁用', send.disabled === true, `disabled=${send.disabled}`);
  check('发送中输入框已清空', input.value === '', `value=${input.value}`);

  // 生成中再提交一次：state.recipeSending 会挡下，不能多出一个用户气泡。
  input.value = '再补一句';
  dom.byId.get('recipe-composer').submit();
  check('发送中再提交不会发第二个 /ask', pending.seen === 1, `seen=${pending.seen}`);
  check('发送中再提交也不会多一个用户气泡',
    textsByKind(log, 'user').length === 1,
    `users=${textsByKind(log, 'user').length}`);
  check('被挡下的那句话没有被吞掉（还留在输入框里）',
    input.value === '再补一句', `value=${input.value}`);
  check('挂起期间后端调用记录里还没有 /ask（确实被挂起了）',
    askCalls().length === 0, `askCalls=${askCalls().length}`);

  pending.release();
  await settle();
  await settle();

  check('响应回来后总共只发了一次 /ask', askCalls().length === 1,
    `askCalls=${askCalls().length}`);
  check('后端收到的 body 与挂起前看到的一致',
    JSON.parse(askCalls()[0].body).message === '晚饭想吃鱼，清淡一点'
    && JSON.parse(askCalls()[0].body).session_id === RECIPE_SESSION_ID,
    askCalls()[0] && askCalls()[0].body);

  const bots = textsByKind(log, 'bot');
  check('助手回复气泡显示 data.reply（接在开场白之后）',
    bots.length === 2 && bots[1] === DEFAULT_ASK_REPLY,
    `bots=${JSON.stringify(bots)}`);
  check('回答卡片行只画了一行', answerRowsOf(dom).length === 1,
    `rows=${answerRowsOf(dom).length}`);
  check('data.recipes 有几道就画几张 .recipe-answer-card',
    answerCardsOf(dom).length === ASK_RECIPES.length,
    `cards=${answerCardsOf(dom).length} recipes=${ASK_RECIPES.length}`);
  check('卡片菜名与响应里的 recipes 一一对应',
    answerCardNames(answerCardsOf(dom)).join('|')
      === ASK_RECIPES.map((recipe) => recipe.name).join('|'),
    answerCardNames(answerCardsOf(dom)).join('|'));
  check('卡片副标题是「时长 · 第一个标签」',
    answerCardsOf(dom).map(answerCardFoot).join('|')
      === ASK_RECIPES.map(
        (recipe) => `${recipe.time_minutes} 分钟 · ${recipe.tags[0]}`
      ).join('|'),
    answerCardsOf(dom).map(answerCardFoot).join('|'));
  check('回答卡片都是 <button type=button>（整张可点）',
    answerCardsOf(dom).every(
      (card) => card.tagName === 'BUTTON' && card.type === 'button'
    ));
  check('回复下面没有旧的多天菜单节点（.recipe-day / .recipe-dish / .recipe-slot）',
    collect(log, (n) => hasClass(n, 'recipe-day')
      || hasClass(n, 'recipe-dish')
      || hasClass(n, 'recipe-slot')).length === 0);

  // DOM 顺序：用户气泡 → 助手回复 → 卡片行。
  const userNodes = messagesOf(log).filter((n) => hasClass(n, 'recipe-msg--user'));
  const botNodes = messagesOf(log).filter((n) => hasClass(n, 'recipe-msg--bot'));
  const userIndex = log.children.indexOf(userNodes[userNodes.length - 1]);
  const replyIndex = log.children.indexOf(botNodes[botNodes.length - 1]);
  const rowIndex = log.children.indexOf(answerRowsOf(dom)[0]);
  check('DOM 顺序是 用户气泡 → 助手回复 → 卡片行',
    userIndex >= 0 && replyIndex > userIndex && rowIndex > replyIndex,
    `user=${userIndex} reply=${replyIndex} row=${rowIndex}`);

  check('不再渲染「我记下了：…」回执（旧的 fresh 字段）',
    textsByKind(log, 'note').length === 0,
    `notes=${JSON.stringify(textsByKind(log, 'note'))}`);
  check('全程没有调用旧的 /api/recipe/message',
    messageCalls().length === 0, `messageCalls=${messageCalls().length}`);
  check('全程没有调用旧的 /api/recipe/plan',
    planCalls().length === 0, `planCalls=${planCalls().length}`);
  check('成功后错误提示保持收起', error.hidden === true,
    `hidden=${error.hidden}`);
  check('发送按钮恢复可点', send.disabled === false, `disabled=${send.disabled}`);
  check('焦点回到输入框（可以直接说下一句）', input._focused === true);

  // 点第一张卡片 → 按 id 拉详情 → 浮层标题是这道菜。
  const detail = dom.byId.get('recipe-detail');
  const detailTitle = dom.byId.get('recipe-detail-title');
  const firstCard = answerCardsOf(dom)[0];
  firstCard.click();
  await settle();

  check('点回答卡片发了 GET /api/recipe/library/{id}',
    detailCalls().length === 1 && detailCalls()[0].method === 'GET',
    detailCalls()[0] && detailCalls()[0].url);
  check('详情请求用的是第一张卡片的 id',
    detailCalls()[0].url.endsWith(`/${ASK_RECIPES[0].id}`),
    detailCalls()[0].url);
  check('详情浮层打开（hidden=false）', detail.hidden === false,
    `hidden=${detail.hidden}`);
  check('浮层标题就是第一张卡片的菜名',
    detailTitle.textContent === ASK_RECIPES[0].name, detailTitle.textContent);

  dom.byId.get('recipe-detail-close').click();
  check('点 × 关掉浮层，食谱窗口还开着',
    detail.hidden === true && dom.byId.get('recipe-modal').hidden === false,
    `detail=${detail.hidden} modal=${dom.byId.get('recipe-modal').hidden}`);

  answerCardsOf(dom)[1].click();
  await settle();
  check('点第二张卡片用的是第二道菜的 id',
    detailCalls().length === 2
    && detailCalls()[1].url.endsWith(`/${ASK_RECIPES[1].id}`),
    detailCalls()[1] && detailCalls()[1].url);
  check('浮层标题换成第二道菜',
    detailTitle.textContent === ASK_RECIPES[1].name, detailTitle.textContent);
  dom.byId.get('recipe-detail-close').click();
}

async function scenarioAskStream() {
  console.log('\n==== 8b. 流式：边到边画 / reset 丢掉废话 / meta 覆盖 ====');
  const reply = '清蒸鲈鱼比较清淡，二十分钟就好。';
  const script = [
    { type: 'delta', text: '我查' },
    { type: 'delta', text: '一下' },
    { type: 'reset' },
    { type: 'delta', text: '清蒸' },
    { type: 'delta', text: '鲈鱼比较清淡，二十分钟就好。' },
    {
      type: 'meta',
      payload: {
        session_id: RECIPE_SESSION_ID,
        reply,
        recipes: ASK_RECIPES,
        tool_calls: ['search_recipes'],
      },
    },
  ];
  const dom = boot({
    askReply: reply,
    askRecipes: ASK_RECIPES,
    streamScript: script,
    streamPace: 60,
  });
  await openRecipeWindow(dom);
  dom.byId.get('recipe-chat-entry').click();
  await settle();

  const log = dom.byId.get('recipe-log');
  const input = dom.byId.get('recipe-input');
  const send = dom.byId.get('recipe-send');

  input.value = '晚饭想吃鱼';
  dom.byId.get('recipe-composer').submit();

  check(
    '走的是流式接口 /api/recipe/ask/stream',
    askCalls().some((call) => call.url.includes('/api/recipe/ask/stream')),
    `urls=${askCalls().map((call) => call.url).join('|')}`,
  );

  // 只让前两帧到达：此时应当"已经画了一半"，但卡片还没出现
  await sleep(80);
  const midway = textsByKind(log, 'bot');
  check(
    '流没结束时气泡里已经有字（边到边画，不是最后一次性冒出来）',
    midway.length >= 2 && midway[midway.length - 1].includes('我查'),
    `bots=${JSON.stringify(midway)}`,
  );
  check(
    '流没结束时还没有卡片',
    answerCardsOf(dom).length === 0,
    `cards=${answerCardsOf(dom).length}`,
  );

  const done = await waitFor(() => answerCardsOf(dom).length > 0);
  check('流结束后卡片出来了', done, `cards=${answerCardsOf(dom).length}`);

  const finalBots = textsByKind(log, 'bot');
  check(
    '最终气泡以 meta 的 reply 为准（覆盖一次，不是拼接）',
    finalBots[finalBots.length - 1] === reply,
    `last=${JSON.stringify(finalBots[finalBots.length - 1])}`,
  );
  check(
    'reset 把工具步的废话清掉了（不许留在回答里）',
    !finalBots.some((textValue) => textValue.includes('我查')),
    `bots=${JSON.stringify(finalBots)}`,
  );
  check(
    '回答卡片数量与 meta 一致',
    answerCardsOf(dom).length === ASK_RECIPES.length,
    `cards=${answerCardsOf(dom).length}`,
  );
  check('流结束后发送按钮恢复', send.disabled === false, `disabled=${send.disabled}`);
}

async function scenarioAskEmptyRecipes() {
  console.log('\n==== 8b. /ask 返回 recipes: [] → 不渲染卡片行（不留空容器） ====');
  const dom = boot({
    askReply: '这次没找到合适的菜，换个说法再问问我？',
    askRecipes: [],
  });
  await openRecipeWindow(dom);
  await sendOneLine(dom, '来点不存在的菜');

  const log = dom.byId.get('recipe-log');
  check('确实调用了 POST /api/recipe/ask', askCalls().length === 1,
    `askCalls=${askCalls().length}`);
  check('回复气泡照常显示',
    textsByKind(log, 'bot').join('|')
      === `${GREETING}|这次没找到合适的菜，换个说法再问问我？`,
    JSON.stringify(textsByKind(log, 'bot')));
  check('没有 .recipe-answer-cards 行', answerRowsOf(dom).length === 0,
    `rows=${answerRowsOf(dom).length}`);
  check('也没有 .recipe-answer-card 卡片', answerCardsOf(dom).length === 0,
    `cards=${answerCardsOf(dom).length}`);
  check('对话区只有开场白 + 用户 + 回复三条（不多一个空容器）',
    log.children.length === 3, `children=${log.children.length}`);
  check('没有冒出错误提示', dom.byId.get('recipe-error').hidden === true);
  check('发送按钮恢复可点', dom.byId.get('recipe-send').disabled === false);
}

async function scenarioAskFailure502() {
  console.log('\n==== 8c. /ask 502 → 显示 detail、按钮恢复、能接着发下一句 ====');
  const detail = '模型这次没接上话，再跟我说一遍？';
  const dom = boot({ askFails: { status: 502, detail } });
  await openRecipeWindow(dom);
  await sendOneLine(dom, '我在减脂');

  const log = dom.byId.get('recipe-log');
  const input = dom.byId.get('recipe-input');
  const send = dom.byId.get('recipe-send');
  const error = dom.byId.get('recipe-error');

  check('确实调用了 POST /api/recipe/ask', askCalls().length === 1,
    `askCalls=${askCalls().length}`);
  check('#recipe-error 原样显示后端给的 detail',
    error.hidden === false && error.textContent === detail,
    `hidden=${error.hidden} text=${error.textContent}`);
  check('用户那句话没有被吞掉（还在对话里）',
    textsByKind(log, 'user').join('') === '我在减脂',
    JSON.stringify(textsByKind(log, 'user')));
  check('失败时没有多出助手回复（开场白之外是空的）',
    textsByKind(log, 'bot').join('') === GREETING,
    `bots=${JSON.stringify(textsByKind(log, 'bot'))}`);
  check('失败时没有渲染回答卡片', answerCardsOf(dom).length === 0,
    `cards=${answerCardsOf(dom).length}`);
  check('发送按钮恢复可点', send.disabled === false, `disabled=${send.disabled}`);
  check('焦点回到输入框（可以直接说下一句）', input._focused === true);

  // 后端恢复 → 再发一句应当正常。
  backend.askFails = null;
  backend.askReply = '好的，这次接上了。';
  backend.askRecipes = [LIBRARY[0]];
  await sendOneLine(dom, '继续');

  check('后端恢复后能正常发出去（第二次 /ask）', askCalls().length === 2,
    `askCalls=${askCalls().length}`);
  check('成功时错误提示被清掉', error.hidden === true,
    `hidden=${error.hidden}`);
  check('第二次成功：助手回复出现',
    textsByKind(log, 'bot').join('|') === `${GREETING}|好的，这次接上了。`,
    JSON.stringify(textsByKind(log, 'bot')));
  check('第二次成功：回答卡片也画出来了', answerCardsOf(dom).length === 1,
    `cards=${answerCardsOf(dom).length}`);
}

async function scenarioAskFailure400() {
  console.log('\n==== 8d. /ask 400 → 同样显示 detail、按钮恢复、能重试 ====');
  const detail = '还没有配置 DEEPSEEK_API_KEY，菜谱助手暂时用不了。';
  const dom = boot({ askFails: { status: 400, detail } });
  await openRecipeWindow(dom);
  await sendOneLine(dom, '晚饭吃什么');

  const log = dom.byId.get('recipe-log');
  const error = dom.byId.get('recipe-error');

  check('确实调用了 POST /api/recipe/ask', askCalls().length === 1,
    `askCalls=${askCalls().length}`);
  check('400 的 detail 原样显示出来',
    error.hidden === false && error.textContent === detail,
    `text=${error.textContent}`);
  check('用户气泡还在，没被 400 清掉',
    textsByKind(log, 'user').join('') === '晚饭吃什么');
  check('400 时不渲染回答卡片', answerCardsOf(dom).length === 0,
    `cards=${answerCardsOf(dom).length}`);
  check('发送按钮恢复可点，能重试',
    dom.byId.get('recipe-send').disabled === false);

  backend.askFails = null;
  backend.askReply = '这次有结果了。';
  backend.askRecipes = [LIBRARY[2]];
  await sendOneLine(dom, '再试一次');

  check('重试真的又发了一次 /ask', askCalls().length === 2,
    `askCalls=${askCalls().length}`);
  check('重试成功后错误提示消失', error.hidden === true);
  check('重试成功后回复与卡片都出现',
    textsByKind(log, 'bot').join('|') === `${GREETING}|这次有结果了。`
    && answerCardsOf(dom).length === 1,
    `bots=${JSON.stringify(textsByKind(log, 'bot'))} cards=${answerCardsOf(dom).length}`);
}

async function scenarioMultiDayUiRemoved() {
  console.log('\n==== 9. 多天菜单整套已删除：反向断言旧 id / 旧函数 / 旧接口都不在 ====');
  // 故意让 /session 带一份旧的多天计划：新前端应当完全无视它。
  const dom = boot({ sessionPlan: OLD_PLAN });
  await openRecipeWindow(dom);

  const LEGACY_IDS = [
    'recipe-legacy', 'recipe-days', 'recipe-generate',
    'recipe-progress', 'recipe-warnings', 'recipe-plan', 'recipe-empty',
  ];
  LEGACY_IDS.forEach((id) => {
    check(`旧控件 #${id} 不在 DOM 里`,
      dom.documentStub.getElementById(id) === null,
      `${id}=${dom.documentStub.getElementById(id)}`);
  });
  check('index.html 的食谱段里也没有这些 id',
    LEGACY_IDS.every((id) => !recipeBlock().includes(`id="${id}"`)),
    LEGACY_IDS.filter((id) => recipeBlock().includes(`id="${id}"`)).join(',') || '（无）');
  check('index.html 里不再有旧流程入口「生成多天菜单」',
    !recipeBlock().includes('生成多天菜单'));
  check('session 里带着旧 plan 也不画多天菜单（没有 .recipe-day / .recipe-dish / .recipe-slot）',
    collect(dom.byId.get('recipe-log'), (n) => hasClass(n, 'recipe-day')
      || hasClass(n, 'recipe-dish')
      || hasClass(n, 'recipe-slot')).length === 0
    && dom.documentStub.getElementById('recipe-plan') === null);

  const REMOVED_SYMBOLS = [
    'generateRecipePlan', 'renderRecipePlan', 'setRecipeGenerating',
    'recipeDaysValue', 'renderRecipeWarnings', 'showRecipeEmpty',
    'recipeMeal', 'recipeSourceLinks',
  ];
  REMOVED_SYMBOLS.forEach((symbol) => {
    check(`app.js 里不再有 ${symbol}（函数已删）`,
      !new RegExp(`\\b${symbol}\\b`).test(appJs));
  });

  check('app.js 不再调用 /api/recipe/message（旧的逐步收集需求）',
    !appJs.includes('/api/recipe/message'));
  check('app.js 不再调用 /api/recipe/plan（多天生成已删）',
    !appJs.includes('/api/recipe/plan'));
  // 断言写成"接口路径片段出现在源码里"还不够：注释里也有路径，改注释就会误通过/误失败。
  // 这里直接断言拼接出来的调用写法，确认它真的发请求。
  check('app.js 调用的是 /api/recipe/ask/stream（新对话式检索）',
    appJs.includes('${API}/recipe/ask/stream'));
  check('app.js 里有新的回答卡片渲染 appendRecipeAnswerCards',
    appJs.includes('appendRecipeAnswerCards'));
  check('appendRecipeAnswerCards 里点卡片会 openRecipeDetail(recipe.id)',
    /appendRecipeAnswerCards[\s\S]{0,900}openRecipeDetail\(recipe\.id\)/.test(appJs));

  const code = recipeCode();
  check('定位到了 app.js 的食谱代码段（静态断言的前提）', code.length > 0,
    `length=${code.length}`);
  check('食谱代码段里不再有「我记下了：…」回执',
    !code.includes('我记下了'));
  check('食谱代码段里不再读旧的 fresh 字段', !/\bfresh\b/.test(code));
  check('回答卡片样式存在（.recipe-answer-card）',
    /\.recipe-answer-card\s*\{/.test(css));
  check('回答卡片是手型光标（cursor:pointer）',
    (() => {
      const rule = /\.recipe-answer-card\s*\{([^}]*)\}/.exec(css);
      return Boolean(rule) && rule[1].includes('cursor: pointer');
    })());
  check('回答卡片行样式存在（.recipe-answer-cards）',
    /\.recipe-answer-cards\s*\{/.test(css));
}

async function scenarioReopen() {
  console.log('\n==== 10. 关掉再打开：历史消息不重复，开场白不重复添加 ====');
  const dom = boot({
    aversions: ['香菜'],
    messages: [
      { role: 'user', content: '我在减脂，海鲜过敏' },
      { role: 'assistant', content: '好的，海鲜我会全部避开。' },
    ],
  });
  const modal = await openRecipeWindow(dom);
  const log = dom.byId.get('recipe-log');

  check('历史会话有几条就画几条（没有额外插一句开场白）',
    messagesOf(log).length === 2, `msgs=${messagesOf(log).length}`);
  check('开场白没有被重复添加', !textsOf(messagesOf(log)).includes(GREETING));
  check('历史里的对话原样出现',
    textsByKind(log, 'user').join() === '我在减脂，海鲜过敏'
    && textsByKind(log, 'bot').join() === '好的，海鲜我会全部避开。');
  check('右栏的忌口也恢复了（后端记住的香菜亮着）',
    hasClass(avoidTagByTerm(dom, '香菜'), 'is-on'));
  check('（已知限制）历史恢复只画文字，回答卡片不会恢复',
    answerRowsOf(dom).length === 0, `rows=${answerRowsOf(dom).length}`);
  check('重开前没有调用 /ask（打开窗口不会自己发消息）',
    askCalls().length === 0, `askCalls=${askCalls().length}`);

  dom.byId.get('recipe-close').click();
  check('点 × 关掉窗口', modal.hidden === true, `hidden=${modal.hidden}`);

  await openRecipeWindow(dom);
  check('重开后对话条数没有翻倍',
    messagesOf(log).length === 2, `msgs=${messagesOf(log).length}`);
  check('重开后开场白仍然只可能是历史里的内容',
    !textsOf(messagesOf(log)).includes(GREETING));
  check('重开后默认停在首页',
    dom.byId.get('recipe-home').hidden === false
    && dom.byId.get('recipe-chat-page').hidden === true,
    `home=${dom.byId.get('recipe-home').hidden}`
    + ` chat=${dom.byId.get('recipe-chat-page').hidden}`);
  check('schema 只取一次（缓存住了）', schemaCalls().length === 1,
    `schema=${schemaCalls().length}`);
  check('每次打开都会重新问一次 session', sessionCalls().length === 2,
    `session=${sessionCalls().length}`);
  check('每次打开都会重新拉一次首页卡片（换一批是同一个调用）',
    libraryCalls().length === 2, `library=${libraryCalls().length}`);
  check('每次打开也会重新取一次 preferences（右栏选项/忌口）',
    preferencesCalls().length === 2, `preferences=${preferencesCalls().length}`);
  check('重开也没有触发 /ask', askCalls().length === 0);
}

async function scenarioEmptySession() {
  console.log('\n==== 11. 空会话（messages:[]）→ 显示 schema 的开场白 ====');
  const dom = boot({ messages: [] });
  await openRecipeWindow(dom);

  const log = dom.byId.get('recipe-log');
  check('只有一条消息：开场白', messagesOf(log).length === 1,
    `msgs=${messagesOf(log).length}`);
  check('开场白就是 schema.greeting（不是前端硬编码的）',
    textsByKind(log, 'bot').join('') === GREETING,
    textsByKind(log, 'bot').join(''));
  check('开场白里没有 markdown 星号（纯文本渲染会原样显示 **）',
    !textsByKind(log, 'bot').join('').includes('*'),
    textsByKind(log, 'bot').join(''));
  check('开场白是助手气泡（不是用户气泡）',
    textsByKind(log, 'user').length === 0);
  check('空会话里不会冒出「我记下了：…」回执',
    textsByKind(log, 'note').length === 0);
  check('空会话里没有回答卡片',
    answerRowsOf(dom).length === 0 && answerCardsOf(dom).length === 0);
  check('旧的三块界面都不在了（需求 / 还差 / 免责声明）',
    ['recipe-profile', 'recipe-missing', 'recipe-disclaimer']
      .every((id) => dom.documentStub.getElementById(id) === null));
  check('右栏标签框在空会话里照样可用（忌口按钮已渲染）',
    avoidTagsOf(dom).length === AVOID_OPTIONS.length,
    `buttons=${avoidTagsOf(dom).length}`);
  check('右栏没有任何忌口被点亮（后端还没记住任何词条）',
    avoidTagsOf(dom).every((node) => !hasClass(node, 'is-on')));
  // 第 51 步起 schema 只回 example 一个字段（不再是 13 个维度的 fields）
  check('输入框示例来自 schema.example',
    dom.byId.get('recipe-input').placeholder === '例如：我在减脂，想让菜清淡一点',
    dom.byId.get('recipe-input').placeholder);
}

async function scenarioSchemaFailure() {
  console.log('\n==== 12. schema 500 → 窗口不崩，给一句可读提示 ====');
  const dom = boot({ schemaFails: 500 });
  const modal = await openRecipeWindow(dom);

  const error = dom.byId.get('recipe-error');
  check('窗口照样打开（不是整页瘫掉）', modal.hidden === false, `hidden=${modal.hidden}`);
  check('给出了可读提示（不是把 HTTP 500 直接甩给用户）',
    error.hidden === false && error.textContent.length > 0
    && error.textContent !== 'HTTP 500' && /[\u4e00-\u9fa5]/.test(error.textContent),
    `text=${error.textContent}`);
  check('取不到 schema 就不再问 session（不做无意义的第二次请求）',
    sessionCalls().length === 0, `session=${sessionCalls().length}`);
  check('取不到 schema 也不再拉首页卡片（没有 schema 的窗口只剩一句提示）',
    libraryCalls().length === 0, `library=${libraryCalls().length}`);
  check('取不到 schema 就不再取右栏 preferences',
    preferencesCalls().length === 0, `preferences=${preferencesCalls().length}`);
  check('取不到 schema 也不会去调 /ask',
    askCalls().length === 0, `askCalls=${askCalls().length}`);
  check('右栏没有残留半截标签',
    avoidTagsOf(dom).length === 0 && wantTagsOf(dom).length === 0,
    `avoid=${avoidTagsOf(dom).length} want=${wantTagsOf(dom).length}`);
  check('窗口里没有残留半截内容（对话区是空的）',
    messagesOf(dom.byId.get('recipe-log')).length === 0);
  check('窗口仍停在首页，且首页/对话页没有两屏同显',
    dom.byId.get('recipe-home').hidden === false
    && dom.byId.get('recipe-chat-page').hidden === true,
    `home=${dom.byId.get('recipe-home').hidden}`
    + ` chat=${dom.byId.get('recipe-chat-page').hidden}`);
}

/* ------------------------------------------------------------ 跑 */

async function main() {
  await scenarioOpenDefaults();
  await scenarioLibraryMatch();
  await scenarioRefresh();
  await scenarioEmptyLibrary();
  await scenarioCardDetail();
  await scenarioHomeChatSwitch();
  await scenarioSessionKey();
  await scenarioTagPanel();
  await scenarioStatedAversionButton();
  await scenarioAvoidToggle();
  await scenarioAvoidRollback();
  await scenarioWantToggle();
  await scenarioAskFlow();
  await scenarioAskStream();
  await scenarioAskEmptyRecipes();
  await scenarioAskFailure502();
  await scenarioAskFailure400();
  await scenarioMultiDayUiRemoved();
  await scenarioReopen();
  await scenarioEmptySession();
  await scenarioSchemaFailure();
  finish();
}

main().catch((error) => {
  console.error('验证脚本自身出错：', error);
  process.exitCode = 1;
});

/*
 * 简化说明（桩里没模拟的浏览器行为，免得读者高估覆盖率）：
 *
 * 1. 只认 id：makeDom 按 index.html 里的 id 造元素，不看层级、不看 CSS，
 *    也不解析标签里的**静态文字**。所以：
 *      - "挪了个位置导致样式错乱"这类问题测不出来，也不该由这个文件测；
 *      - `#recipe-chat-entry` 里那句「您好，想吃点什么？」写在子 <span> 里，
 *        桩读不到，场景 6 改成直接在 index.html 源码里确认（仍是真文案断言）；
 *      - 右栏两个分组标题（「一直记住 · 不吃」「这次想要」）同样是静态文字，
 *        场景 1 也在 index.html 源码里确认。
 * 2. HTML 上的 value / min / max 属性没有解析（只解析 id、标签名、hidden），
 *    桩里元素的 value 初始都是空串。本轮已经不再测天数输入框；发送消息时
 *    由测试自己给 `#recipe-input` 赋值。
 * 3. className 属性也没有解析：桩里所有元素一开始都没有 class。classList
 *    仍然是一份真集合，另外把**每次真实增删**记进 `_classLog`（测试专用，
 *    浏览器没有这个属性）。场景 6 靠它证明 `.is-leaving` 曾经加上过——
 *    因为桩里的 window.setTimeout 是同步执行的，click 返回时这个 class
 *    已经被 showRecipeView 的回调移除了。
 * 4. window.setTimeout 同步执行：当前只有首页滑走动画用到它，副作用很小。
 *    场景 6 依赖"click 返回即过渡结束"来断言 hidden 与焦点。
 * 5. 没有布局、可见性与滚动：
 *      - scrollTop/scrollHeight 只有初值，"滚到底"不测；
 *      - hidden 只改属性，桩**不阻止**点击 hidden 容器里的元素。
 *    CSS 过渡本身也没有跑，`.is-leaving` 只验证"加过 / 移除过"；
 *    新卡片的样式只用静态规则确认存在，不检查实际排版。
 * 6. 后端桩只覆盖本轮前端真正调用的接口（schema / session / ask /
 *    library 列表与详情 / seed / preferences）。旧的 /message、/plan
 *    仍有 410 分支只为让回退立刻暴露，不模拟任何业务。具体是：
 *      - 库：list / get / seed，不模拟真实的随机排序、去重与 FTS 检索；
 *        「换一批」的"不同批次"是配置出来的；
 *      - /ask：回复与 recipes 由配置给（`askReply` / `askRecipes`），
 *        不跑 Agent、不调模型，也不验证模型的选菜质量；
 *      - 右栏：GET / PUT preferences。PUT 只在内存里整份替换 aversions
 *        （不做 core 的 _clean_avoid_term 去空白/去重，也不读写 SQLite）；
 *        失败分支由 preferencesPutFails 配置出来。
 *    aria-pressed 用 getAttribute 读，桩把 aria-* 原样存在 attributes 里。
 * 7. 禁用的元素点不动（刻意模拟，不是简化）：浏览器里 disabled 元素不派发
 *    click，桩照做。场景 8 靠它验证"发送中再点也发不出第二个 /ask"。
 * 8. 回答卡片的点击是用按钮的 click 处理器直接触发的；"卡片在真实布局里
 *    是否被别的元素盖住"属于布局问题，不在本文件的覆盖范围。
 *
 * 已知问题（本轮只改验证脚本，**没有改前端**）：
 * - 忌口保存失败后，再成功保存一次时旧的 #recipe-error 不会消失。
 *   app.js:2989-3027 的 toggleAvoidTerm 只在失败时 setRecipeError(...)，
 *   成功路径没有 setRecipeError('')。复现步骤：PUT 返回 500 → 界面提示
 *   "忌口没存上，稍后再试一次？"；把假后端恢复成 200 再点一次 →
 *   .is-on 正确更新，但 error.hidden 仍是 false、文案没变。
 *   期望：成功保存后清掉上一次的错误提示（像 sendRecipeMessage 开头那样）。
 * - 「这次想要」的选中态只存在 state.recipeWanted 里，从未进入任何请求——
 *   sendRecipeMessage 只发 session_id/message（app.js:3126-3130）。
 *   按本轮接口约定（/ask 的请求体就是这两个字段）这不是前端能单独修的；
 *   若产品期望标签影响这一轮回答，需要先扩接口。
 * - RECIPE_TIMEOUT_MS（app.js:2714）已经没有地方用了：旧的 /plan 流程
 *   才有 180 秒超时，新的 /ask 没有 AbortController / 超时。后端若一直
 *   不回，发送按钮会一直禁用（state.recipeSending 卡在 true）。
 * - 关掉再打开时，历史里的助手回复只有文字：session 只回 messages，
 *   没有回答菜谱的 id，因此之前的 .recipe-answer-card 不会恢复
 *   （appendRecipeAnswerCards 只在实时响应里调用，app.js:3139）。
 * - style.css 里还留着 .recipe-legacy / .recipe-days / .recipe-generate /
 *   .recipe-progress / .recipe-warnings / .recipe-plan / .recipe-empty
 *   这些已删界面的孤儿样式（约 1788-2150 行）。只是死代码，不影响行为。
 */

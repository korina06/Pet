/* 桌宠前端：轮盘菜单、聊天与各功能区窗口的交互与渲染。

入口文案与意图全部来自后端接口，前端不硬编码工具名；所有窗口共用一套开关、Esc 与
遮罩点击的约定。聊天接口返回的 tool_invocations 与 steps 只用于排查，界面上不显示。 */
(() => {
  'use strict';

  const API = '/api';
  const SESSION_KEY = 'pet.session_id';

  /* ------------------------------------------------ 鉴权（第 254 步）
   *
   * 后端配了 AUTH_TOKENS 时，所有 /api/* 都要 Authorization: Bearer。
   * token 只存在本机 localStorage；没配 token 的部署里它为空，
   * 于是这里不加头、不弹框 —— 本机模式的行为与以前完全一致。
   */
  const AUTH_TOKEN_KEY = 'karo.authToken';
  //: `<img src>` 带不了 Authorization 头（缩略图、路线图都会 401），
  //: 所以同一个 token 也写一份同源 cookie；后端两种都认（第 255 步）。
  const AUTH_COOKIE = 'karo_auth';
  const nativeFetch = fetch;

  function readAuthToken() {
    try {
      return localStorage.getItem(AUTH_TOKEN_KEY) || '';
    } catch (_) {
      return '';
    }
  }

  function writeAuthCookie(token) {
    try {
      document.cookie = token
        ? `${AUTH_COOKIE}=${encodeURIComponent(token)}; path=/; SameSite=Strict`
        : `${AUTH_COOKIE}=; path=/; Max-Age=0; SameSite=Strict`;
    } catch (_) {
      // 禁 cookie 的环境：fetch 仍走 Authorization 头，只影响图片
    }
  }

  function writeAuthToken(token) {
    try {
      if (token) localStorage.setItem(AUTH_TOKEN_KEY, token);
      else localStorage.removeItem(AUTH_TOKEN_KEY);
    } catch (_) {
      // 隐私模式下写不了：退化成「这次会话有效」，不阻断使用
    }
    writeAuthCookie(token);
  }

  function withAuthHeader(init) {
    const token = readAuthToken();
    if (!token) return init;
    const opts = Object.assign({}, init || {});
    const headers = Object.assign({}, opts.headers || {});
    headers.Authorization = 'Bearer ' + token;
    opts.headers = headers;
    return opts;
  }

  let tokenPrompt = null;

  /** 弹一次 token 输入框；并发的 401 共用同一个 Promise，不会叠好几层。 */
  function askForToken() {
    if (tokenPrompt) return tokenPrompt;
    tokenPrompt = new Promise(function (resolve) {
      const overlay = elWith('div', 'auth-overlay');
      const card = elWith('div', 'auth-card');
      const title = elWith('h3', 'auth-title', '需要访问令牌');
      const hint = elWith(
        'p',
        'auth-hint',
        '这台服务开着鉴权。请输入 .env 里 AUTH_TOKENS 配置的 token（只保存在本机浏览器）。'
      );
      const input = document.createElement('input');
      input.type = 'password';
      input.className = 'auth-input';
      input.placeholder = 'token';
      const error = elWith('p', 'auth-error', '');
      const submit = elWith('button', 'auth-submit', '记住并继续');
      submit.type = 'button';

      function finish() {
        const value = String(input.value || '').trim();
        if (!value) {
          error.textContent = 'token 不能为空';
          return;
        }
        writeAuthToken(value);
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        tokenPrompt = null;
        resolve(value);
      }

      submit.addEventListener('click', finish);
      input.addEventListener('keydown', function (event) {
        if (event && event.key === 'Enter') finish();
      });
      card.append(title, hint, input, error, submit);
      overlay.append(card);
      document.body.append(overlay);
      if (typeof input.focus === 'function') input.focus();
    });
    return tokenPrompt;
  }

  /** 所有 /api 调用都走这里：带 token；401 时问一次并**只重试一次**（避免死循环）。
   *
   * 只有响应带 `X-Auth-Required: 1` 才算「缺令牌」—— 邮箱登录失败这类 401
   * 不能被误当成鉴权问题，否则会对着用户弹一个填 token 的框。
   */
  async function apiFetch(input, init) {
    const response = await nativeFetch(input, withAuthHeader(init));
    const headers = response.headers;
    const needsToken =
      response.status === 401 &&
      headers &&
      typeof headers.get === 'function' &&
      headers.get('X-Auth-Required') === '1';
    if (!needsToken) return response;
    await askForToken();
    return nativeFetch(input, withAuthHeader(init));
  }

  // 启动时把已存的 token 同步成 cookie —— 否则刷新后 <img>（缩略图/路线图）全是 401。
  if (readAuthToken()) writeAuthCookie(readAuthToken());

  const ICONS = { chat: '💬' };
  const DEFAULT_ICON = '🔧';
  const CHAT_ICON = '💬';

  /* 聊天框上方的胶囊（第 24 步）。
   *
   * 第 23 步时胶囊是"工具的快捷方式"（点了发一句 intent）；现在它变成
   * "打开一个功能"的入口，所以不再由 /api/tools 驱动，改为这张条目表：
   *   panel 有值 → 点了弹出对应窗口；没有 panel → 占位，暂时点不动。
   * 标签由前端定义是因为它属于界面结构；将来出行窗口里要调哪些工具
   * 才是后端的事（下一步做）。
   *
   * 第 37 步起三个胶囊**都能点开**了：「食谱」也接上了自己的窗口。
   * 那个"没有 panel 就 disabled"的分支仍然保留：它是这张表的通用规则，
   * 将来再添占位项时不必重新想一遍（也免得出现"看着能点却没反应"）。 */
  const CHIP_ENTRIES = [
    { id: 'travel', label: '出行', panel: 'travel' },
    { id: 'mail', label: '邮箱', panel: 'mail' },
    { id: 'recipe', label: '食谱', panel: 'recipe' },
  ];

  /* 轮盘里与「对话」同级的入口（第 38 步加的选项，第 40 步起「签到」有内容了）。
   *
   * 「签到」现在有自己的窗口，所以和「对话」一样带 run()，渲染出来就是可点项；
   * 「状态」「任务」暂时**只有选项、没有内容**，按项目既有约定
   * （见 renderChips 的占位处理）做成禁用并提示"还没做"：
   * 用户会明白是自己点不动，而不是界面坏了。
   * 等这两项各自有了功能，同样把 disabled 换成 run() 即可，不必改结构。
   */
  const WHEEL_CHECKIN = {
    key: 'checkin',
    icon: '📅',
    label: '签到',
    run: () => openDialog('checkin'),
  };

  /* 轮盘里的「模型」（第 100 步）：位置**紧跟「对话」**，因为它改的是
     对话和查菜谱用的那一家。一家一个按钮，等于一组单选开关。 */
  const WHEEL_MODEL = {
    key: 'model',
    icon: '🧠',
    label: '模型',
    run: () => {
      openDialog('model');
      void refreshModelPanel();
    },
  };

  /* 桌宠的「动作」（第 150 步）。
   *
   * 原来这里是「状态」占位，用户要求改成"存放桌宠动作的地方"：
   * 点轮盘里的「动作」→ 桌宠旁边弹出一个**右键菜单样式**的竖排列表 → 点哪一项演哪个动作。
   *
   * 做成数组而不是写死在渲染里：以后加动作只加一项 + 一个 run()，
   * 菜单、事件绑定、关闭逻辑都不用动。
   *
   * 目前只有一个「擦擦灰」，而且是 **CSS 演的**（抖一下 + 几粒灰）。
   * 等有了真正的动作素材（例如待机视频里那段擦灰），把 `run` 换掉即可。
   */
  /* 动作池（第 162 步，照 dsh-pet 的"分池 + 权重"）：
   *
   *   - `pool: 'idle'`   —— 待机时会被自动抽到；
   *   - `pool: 'manual'` —— **只在你从菜单点它时才播**；
   *   - `weight`         —— 在 idle 池里被抽到的相对权重（默认 1，按权重轮盘抽）。
   *
   *  菜单始终列出**全部**动作（手动点不受池限制）。
   *
   *  第 178 步起：**idle 池里只有「待机」**（呼吸 10.13 秒），其余全部 manual ——
   *  平时就一直呼吸，别的动作只有你点菜单才演。想让她自己干点别的，
   *  把对应动作的 `pool` 改回 `'idle'` 并给个 `weight` 即可（数据在
   *  `static/actions/<名字>.json`，改完刷新页面，不用动代码）。
   */
  const PET_ACTIONS = [
    {
      id: 'tea',
      icon: '🍵',
      label: '喝茶',
      pool: 'manual',     // 10.1 秒，同上（第 171 步，b3）
      weight: 1,
      // 有素材的动作：播一次 webm（完整 10.13 秒，152 帧 @15fps）
      asset: 'actions/tea.webm',
      durationMs: 10130,
    },
    {
      id: 'moyu',
      icon: '💤',
      label: '摸鱼',
      pool: 'manual',     // 11.5 秒太长，待机里自己演会"占戏"（第 171 步，b3）
      weight: 2,
      // 11.5 秒的长动作
      asset: 'actions/moyu.webm',
      durationMs: 11530,
    },
    {
      id: 'idle',
      icon: '🌿',
      label: '待机',
      pool: 'idle',
      weight: 6,          // idle 池里唯一的一个：平时就一直呼吸
      // 10.13 秒的呼吸动画（首尾帧接近，接力不跳）
      asset: 'actions/idle.webm',
      durationMs: 10130,
    },
    {
      id: 'wipe',
      icon: '🧹',
      label: '擦擦灰',
      pool: 'manual',     // 第 178 步：改进手动池，平时只待机
      weight: 2,
      // 这段是**视频素材**（8 秒）：她抬手在身上擦来擦去
      asset: 'actions/wipe.webm',
      durationMs: 8000,
    },
  ];

  /* 拖拽姿势（第 163 步）：**不在动作菜单里**，只在"真的拖动起来"时循环播放。
   *
   *  参考 dsh-pet 的分池做法：它的 `drag` 池是独立的一档，只由"被拖拽"触发，
   *  不进随机链、也不在动作菜单里。我们照做：
   *  - 位移超过阈值（真的在拖）→ 先播「被拎起来」，再循环播「来回荡」；
   *  - 松手 → 停、回静帧，并把下一次待机排上（复用 finishAction）。
   */
  const DRAG_LIFT = {
    id: 'drag-lift',
    asset: 'actions/drag-lift.webm',   // 被拎起来的瞬间（3.2 秒，播**一次**）
    durationMs: 3200,
  };
  const DRAG_SWING = {
    id: 'drag-swing',
    asset: 'actions/drag-swing.webm',  // 拎在手里来回荡（1.2 秒，**循环**）
    durationMs: 1200,
  };

  const WHEEL_ACTIONS = {
    key: 'actions',
    icon: '✨',
    label: '动作',
    // ⚠️ 必须把事件收下再传：`() => openActionMenu()` 会丢掉参数，
    // 于是 openActionMenu 里的 stopPropagation() 永远不执行 ——
    // 点「动作」时这次点击会冒泡到 document，被"点外面关菜单"的监听当场关掉，
    // 表现就是"菜单闪一下就消失"（真实浏览器里必现，桩里看不出来）。
    run: (event) => openActionMenu(event),
  };

  const WHEEL_PLACEHOLDERS = [
    { key: 'quest', icon: '🎯', label: '任务' },
  ];

  /* 行程数据全部由后端给（第 41 步）。
   *
   * 这里原来是一份写死的「示例内容池」+ 示例酒店，用它渲染每日行程。
   * 它已经被删除，原因是它与后端真调回来的坐标/天气/POI 完全脱节：
   * 屏幕上那份行程和后端真实取到的数据没有任何关系，而且后端整条链路
   * 失败时它照样渲染出一份"漂亮的北京三日游"——静默、自信、错误。
   *
   * 现在：POST /api/trip 返回结构化的 Itinerary，state.trip 是唯一数据源；
   * 没有数据源的字段（门票价、人均消费、酒店、游览时长之外的一切）
   * **在 schema 里根本不存在**，所以没有地方可以编。
   */
  const el = {
    pet: document.getElementById('pet'),
    petButton: document.getElementById('pet-button'),
    petIdle: document.getElementById('pet-idle'),
    actionMenu: document.getElementById('action-menu'),
    bubble: document.getElementById('bubble'),
  petNote: document.getElementById('pet-note'),
  petNoteText: document.getElementById('pet-note-text'),
    menu: document.getElementById('menu'),
    menuList: document.getElementById('menu-list'),
    chat: document.getElementById('chat'),
    chatLog: document.getElementById('chat-log'),
    chatChips: document.getElementById('chat-chips'),
  chatTools: document.getElementById('chat-tools'),
  chatMore: document.getElementById('chat-more'),
    chatClose: document.getElementById('chat-close'),
  chatClear: document.getElementById('chat-clear'),
  modelModal: document.getElementById('model-modal'),
  modelClose: document.getElementById('model-close'),
  modelProviders: document.getElementById('model-providers'),
  modelProbe: document.getElementById('model-probe'),
  modelProbeResult: document.getElementById('model-probe-result'),
  modelUsage: document.getElementById('model-usage'),
  chatConfirm: document.getElementById('chat-confirm'),
  chatConfirmText: document.getElementById('chat-confirm-text'),
  chatConfirmOk: document.getElementById('chat-confirm-ok'),
  chatConfirmCancel: document.getElementById('chat-confirm-cancel'),
    composer: document.getElementById('composer'),
    input: document.getElementById('user-input'),
    send: document.getElementById('send-btn'),
    travelModal: document.getElementById('travel-modal'),
    travelClose: document.getElementById('travel-close'),
    travelForm: document.getElementById('travel-form'),
    travelCity: document.getElementById('travel-city'),
  // 第 231 步：出发地（可选），只用于记录与展示，不参与排程
  travelOrigin: document.getElementById('travel-origin'),
  // 目的地候选清单（第 219 步）
  travelCityOptions: document.getElementById('travel-city-options'),
    travelStart: document.getElementById('travel-start'),
    travelEnd: document.getElementById('travel-end'),
    travelDays: document.getElementById('travel-days'),
    travelTransport: document.getElementById('travel-transport'),
    travelStay: document.getElementById('travel-stay'),
    travelNote: document.getElementById('travel-note'),
    travelError: document.getElementById('travel-error'),
    travelSubmit: document.getElementById('travel-submit'),
    travelResume: document.getElementById('travel-resume'),
    travelResumeText: document.getElementById('travel-resume-text'),
    travelResumeOpen: document.getElementById('travel-resume-open'),
    travelResumeDrop: document.getElementById('travel-resume-drop'),
    travelTabChat: document.getElementById('travel-tab-chat'),
    travelTabForm: document.getElementById('travel-tab-form'),
    travelTabs: document.getElementById('travel-tabs'),
    travelTabPlan: document.getElementById('travel-tab-plan'),
    travelTabPlanLabel: document.getElementById('travel-tab-plan-label'),
    travelTabPlanClose: document.getElementById('travel-tab-plan-close'),
    travelPageChat: document.getElementById('travel-page-chat'),
    travelChatLog: document.getElementById('travel-chat-log'),
    travelChatForm: document.getElementById('travel-chat-form'),
    travelChatInput: document.getElementById('travel-chat-input'),
    travelChatSend: document.getElementById('travel-chat-send'),
    travelPageForm: document.getElementById('travel-page-form'),
    travelPagePlan: document.getElementById('travel-page-plan'),
    travelPageDay: document.getElementById('travel-page-day'),
    travelOverview: document.getElementById('travel-overview'),
    overviewTitle: document.getElementById('overview-title'),
    overviewDates: document.getElementById('overview-dates'),
    overviewAdvice: document.getElementById('overview-advice'),
    dayList: document.getElementById('day-list'),
    dayTitle: document.getElementById('day-title'),
    dayBody: document.getElementById('day-body'),
    // 第 46 步：`day-edit` / `day-export` 两个 disabled 占位按钮已按用户要求删除，
    // 所以 el 里不再有这两项（el 只映射页面上真实存在的元素）。
    planProgress: document.getElementById('plan-progress'),
    planNotice: document.getElementById('plan-notice'),
    // 地图（第 224 步）：卡片默认隐藏，配好 key 且点了「展开地图」才真正加载高德脚本
    travelMapCard: document.getElementById('travel-map-card'),
    travelMapCanvas: document.getElementById('travel-map'),
    travelMapDays: document.getElementById('travel-map-days'),
    travelMapToggle: document.getElementById('travel-map-toggle'),
    travelMapNote: document.getElementById('travel-map-note'),
    // 邮箱窗口（第 33 步）：登入前 / 登入后是同一个窗口里的两页
    mailModal: document.getElementById('mail-modal'),
    mailClose: document.getElementById('mail-close'),
    mailPageLogin: document.getElementById('mail-page-login'),
    mailPageInbox: document.getElementById('mail-page-inbox'),
    mailForm: document.getElementById('mail-form'),
    mailAddress: document.getElementById('mail-address'),
    mailSecret: document.getElementById('mail-secret'),
    mailProviderHint: document.getElementById('mail-provider-hint'),
    mailError: document.getElementById('mail-error'),
    mailSubmit: document.getElementById('mail-submit'),
    mailServiceToggle: document.getElementById('mail-service-toggle'),
    mailServices: document.getElementById('mail-services'),
    mailFolders: document.getElementById('mail-folders'),
    mailAccount: document.getElementById('mail-account'),
    mailLogout: document.getElementById('mail-logout'),
    mailSearch: document.getElementById('mail-search'),
    mailList: document.getElementById('mail-list'),
    mailState: document.getElementById('mail-state'),
    // 食谱窗口（第 37 步起；第 48 步改版为"首页推荐 + 对话页"两个界面）
    recipeModal: document.getElementById('recipe-modal'),
    recipeClose: document.getElementById('recipe-close'),
    // 首页：卡片横栏 + 换一批 + 底部对话入口
    recipeHome: document.getElementById('recipe-home'),
    recipeCards: document.getElementById('recipe-cards'),
    recipeCardsEmpty: document.getElementById('recipe-cards-empty'),
    recipeSeed: document.getElementById('recipe-seed'),
    recipeRefresh: document.getElementById('recipe-refresh'),
    recipeChatEntry: document.getElementById('recipe-chat-entry'),
    // 对话页
    recipeChatPage: document.getElementById('recipe-chat-page'),
    recipeBack: document.getElementById('recipe-back'),
    // 卡片详情浮层
    recipeDetail: document.getElementById('recipe-detail'),
    recipeDetailTitle: document.getElementById('recipe-detail-title'),
    recipeDetailMeta: document.getElementById('recipe-detail-meta'),
    recipeDetailBody: document.getElementById('recipe-detail-body'),
    recipeDetailClose: document.getElementById('recipe-detail-close'),
    recipeLog: document.getElementById('recipe-log'),
    recipeComposer: document.getElementById('recipe-composer'),
    recipeInput: document.getElementById('recipe-input'),
    recipeSend: document.getElementById('recipe-send'),
    recipeError: document.getElementById('recipe-error'),
    // 上传图片识菜（第 60 步）
    recipePhotoBtn: document.getElementById('recipe-photo-btn'),
    recipePhotoInput: document.getElementById('recipe-photo-input'),
    recipePhotoPreview: document.getElementById('recipe-photo-preview'),
    recipePhotoThumb: document.getElementById('recipe-photo-thumb'),
    recipePhotoName: document.getElementById('recipe-photo-name'),
    recipePhotoRemove: document.getElementById('recipe-photo-remove'),
  // 买菜清单（第 126 步）
  shoppingBar: document.getElementById('shopping-bar'),
  shoppingCount: document.getElementById('shopping-count'),
  shoppingOpen: document.getElementById('shopping-open'),
  shoppingPanel: document.getElementById('shopping-panel'),
  shoppingClose: document.getElementById('shopping-close'),
  shoppingItems: document.getElementById('shopping-items'),
  shoppingHint: document.getElementById('shopping-hint'),
  shoppingCopy: document.getElementById('shopping-copy'),
  shoppingStatus: document.getElementById('shopping-status'),
  //: 工具条上的 ×（第 137 步）：清空已选、整条收起
  shoppingClear: document.getElementById('shopping-clear'),
  // 行程导出（第 130 步）
  tripExport: document.getElementById('trip-export'),
  // 行程导出成文件（第 218 步）
  tripMarkdown: document.getElementById('trip-markdown'),
  tripExportStatus: document.getElementById('trip-export-status'),
  // 食谱「新建会话」（第 131 步）
  recipeNew: document.getElementById('recipe-new'),
  recipeConfirm: document.getElementById('recipe-confirm'),
  recipeConfirmText: document.getElementById('recipe-confirm-text'),
  recipeConfirmOk: document.getElementById('recipe-confirm-ok'),
  recipeConfirmCancel: document.getElementById('recipe-confirm-cancel'),
    // 右栏那个"装标签的框"（第 50 步）：忌口组 + 这次想要组
    recipeTagsAvoid: document.getElementById('recipe-tags-avoid'),
    recipeTagsWant: document.getElementById('recipe-tags-want'),
    checkinModal: document.getElementById('checkin-modal'),
    checkinClose: document.getElementById('checkin-close'),
    checkinPrev: document.getElementById('checkin-prev'),
    checkinNext: document.getElementById('checkin-next'),
    checkinMonthLabel: document.getElementById('checkin-month-label'),
    checkinState: document.getElementById('checkin-state'),
    checkinGrid: document.getElementById('checkin-grid'),
    checkinWeekdays: document.getElementById('checkin-weekdays'),
    checkinDays: document.getElementById('checkin-days'),
    checkinConfirm: document.getElementById('checkin-confirm'),
    checkinConfirmText: document.getElementById('checkin-confirm-text'),
    checkinConfirmOk: document.getElementById('checkin-confirm-ok'),
    checkinConfirmCancel: document.getElementById('checkin-confirm-cancel'),
  };

  // 排查口子（第 273 步）：气泡用例在别人机器上挂过一次，本机四种跑法都绿。
  // 挂的现象是「气泡不出现」，它有两个完全不同的原因——节点没绑上，或绑上了又被自动收收走。
  // 光看断言分不出来，所以把绑定结果暴露出来给验证脚本打印。不影响正常路径。
  if (typeof window !== 'undefined') {
    window.__petNoteDebug = () => ({
      note: Boolean(el.petNote),
      text: Boolean(el.petNoteText),
      sticky: Boolean(window.__petNoteSticky),
    });
  }

  const state = {
    tools: [],
    // 轮盘用的工具（第 24 步起胶囊不再由工具驱动，所以只留这一组）
    menuTools: [],
    menuOpen: false,
    chatOpen: false,
    // 弹出的小窗：null 表示没有开，否则是 'travel' / 'mail' / 'recipe' / 'checkin' / 'model'
    // （第 209 步删掉了侧边栏，'about' / 'settings' / 'login' 三种随之消失）
    openDialogKind: null,
    // 出行窗口（第 25 步）：当前是第几页 / 是否正在生成
    travelPage: 'form',
    travelGenerating: false,
    // 本次行程（第 41 步）：**整个对象由后端 POST /api/trip 返回**。
    // 前端不再自己算天数/日期，也不再有任何写死的示例内容——
    // 它是唯一数据源，没数据源的字段在 schema 里根本不存在。
    trip: null,
    /// 出行的"代次"（评价12 P4-2）：每次重开窗口 +1，用来丢弃**上一次**还在飞的 /trip 响应。
    /// 没有它的话，切走再切回时那个迟到响应会覆盖 state.trip，
    /// 概览里显示的行程和用户刚填的表单对不上。
    tripEpoch: 0,
    /// 上次生成的那份行程（第 197 步 / 评价15 B2）：从后端取回来后**先放这里**，
    /// 用户点「接着看」才搬进 `state.trip`。分开放是为了让「表单页」永远能干净地开始。
    resumeTrip: null,
    // 当前选中的是第几天；null 表示不在详情页
    travelDay: null,
    // 已经打开的「第x天」标签（键是第几天，值是那个标签按钮）
    dayTabs: {},
    // 出行对话正在等后端（第 216 步）：期间不接收第二条，避免乱序回答
    travelChatBusy: false,
    sending: false,
    // 拖拽相关（第 17 步）
    dragging: false,
    movedBeyondThreshold: false,
    dragEndedAt: 0,           // 拖拽结束时刻（用于限定抑制 click 的时间窗）
    startX: 0,
    startY: 0,
    originX: 0,
    originY: 0,
    dragX: 0,
    dragY: 0,
    // 邮箱窗口（第 33 步）。登入前后是同一窗口里的两页，只切页不新开窗口
    mailPage: 'login',          // 'login' | 'inbox'
    mailBusy: false,
    mailProviders: [],          // 由 /api/mail/providers 给，前端不硬编码服务商
    mailProvider: 'qq',
    mailDefaultProvider: 'qq',
    mailAccount: '',
    mailFolder: 'inbox',
    mailFolders: [],
    // 按文件夹各自缓存（第 36 步）。以前只有一份共享状态，
    // 于是"A 文件夹的响应回来时用户已经切到 B" → 邮件串到 B 的界面上。
    // 形状：{ [folderKey]: { messages, offset, hasMore, total, newestUid,
    //                        loaded, loading, token, checkedAt, revision } }
    mailCache: {},
    // 搜索框的防抖定时器（第 61 步）：以前每敲一个字符就全量重建列表
    mailSearchTimer: 0,
    // 后台查新（第 198 步 / 评价15 B6）：只在登入之后才转起来。
    // timer 是 setInterval 的句柄（0 = 没转），busy 挡住上一轮还没回来时的重入。
    mailWatch: { timer: 0, busy: false, armed: false },
    // 桌宠气泡的自动消失定时器，以及"点它要干什么"
    petNoteTimer: 0,
    petNoteOpen: null,
    // 打开邮箱窗口时那次 prepareMail 的句柄（第 198 步）
    mailPrepare: null,
    // 列表上方那条要一直留着的提示（第 198 步：点气泡后找不到那封邮件）
    mailHint: '',
    // 食谱窗口（第 37 步起；第 48 步改成"首页 / 对话页"两个互斥界面）
    recipeView: 'home',          // 'home' | 'chat'
    recipeCards: [],             // 首页这批卡片（供"换一批"与调试）
    recipeSchema: null,          // GET /api/recipe/schema 的原文（只取一次）
    recipeAversions: [],        // 已记住的忌口词条（存库）
    recipeAvoidOptions: [],     // 忌口的大众选项（后端给）
    recipeWantGroups: [],       // 「这次想要」的分组标签（后端给）
    recipeWanted: [],           // 这次选中的「想要」标签（不持久化）
    recipeSending: false,
  //: 食谱会话的"代次"：每次用户发消息 +1。用来判断"历史回来得晚"该不该覆盖
  recipeEpoch: 0,
  //: 勾选的菜（id -> 菜名）：第 126 步的买菜清单用它
  pickedRecipes: new Map(),
  //: 服务端渲染好的清单文本（复制用）
  shoppingText: '',
    //: 在途的 /ask 取消器（关窗时 abort；防止旧响应画进新窗口）
    //: 「清空对话」确认框的 resolver（第 95 步）。
  //  它是**状态**不是元素：放进 `el` 会让启动自检把它当成「HTML 里找不到的元素」报警（第 108 步的行为验证发现的）。
  //: 历史请求是否正在飞（第 108 步）。**必须有它**：sendMessage 里也会调
  //  openChat()，于是每发一句话都会再触发一次 restoreChatHistory ——
  //  两个请求都回来时历史会被插入两遍（行为验证实测：4 条变 8 条）。
  chatHistoryLoading: false,
  //: 在途的聊天流（关窗时要中断，服务端据此停止）
  chatController: null,
  //: 用户是否刚刚清空过对话（第 102 步）：晚到的历史响应不许再画回来
  chatCleared: false,
  //: 功能栏（出行/邮箱/食谱）是否展开（第 146 步）
  chatToolsOpen: false,
  //: 动作菜单是否打开（第 150 步）
  actionMenuOpen: false,
  //: 待机动画演到第几轮（第 151 步）：每次换 URL 才能从头播
  idleCycle: 0,
  //: 上一个演过的动作 id（随机时避免连着演同一个）
  lastActionId: null,
  //: 当前是否有动作在演（第 159 步：同一时刻只演一个）
  actionPlaying: false,
  //: 待机计时器 / 动作兜底计时器
  idleTimer: null,
  actionTimer: null,
  //: 上一次手动点击的时间（调试用；也便于将来做"手动后冷却"）
  lastManualAt: 0,
  //: 调度重入深度（防自激；见 scheduleNextIdle）
  scheduleDepth: 0,
  //: 是否正在演"被拖拽"的姿势（第 163 步）；stage = lift | swing | idle（第 165 步）
  dragPose: false,
  dragPoseStage: 'idle',
  //: 当前动作的"代号"（第 176 步）：每次换动作 +1，用来作废**上一任**留下的回调
  actionToken: 0,
  //: 本次动作开始的时刻（第 177 步）：用来丢弃"刚换 src 就来的"迟到 ended
  actionStartedAt: 0,
  //: 抛掷（第 171 步）：最近 120ms 的指针轨迹 + 当前速度
  pointerTrail: [],
  throwing: false,
  throwVX: 0,
  throwVY: 0,
  //: "提起 → 来回荡"的兜底计时器（第 167 步；见 startDragPose 里的说明）
  dragSwitchTimer: null,
  //: 这次 abort 是「清空对话」引起的（第 145 步）：catch 里不该冒错误气泡
  chatAborted: false,
  //: 模型列表与当前选择（第 100 步）
  model: null,
  //: 聊天历史是否已经从后端取过一次（一个页面生命周期只取一次）
  chatHistoryLoaded: false,
  recipeAskController: null,
    // 待发送的图片（第 60 步）：只在内存里，发完就清掉，不落盘
    recipePhoto: null,
    recipePhotoUrl: '',
    // 签到窗口（第 40 步）。真值在后端的 SQLite（pet.db 的 checkin_days /
    // checkin_cards，第 130 步落的盘），前端只存"当前在看哪个月"
    // 和最近一次拿到的整月视图。
    checkinMonth: null,          // null = 当月；'YYYY-MM' = 用户翻到的那一月
    checkin: null,               // GET/POST /api/checkin* 返回的整月视图
    checkinBusy: false,
    // 上一次补签失败的原因，形状 { date, text }。画日历时会把它写到
    // "那一天"的格子里（用户要求提示跟着出问题的日期走）。
    checkinCardNote: null,
  };

  /* --------------------------------------- 弹出的小窗（出行 / 邮箱 / 食谱 …） */
  /* 第 209 步：侧边栏连同它的「关于」「设置」两个标签一起删掉了 ——
     桌宠本体加几个子功能就够了，左上角那个 ☰ 和它拉出来的一列入口是多余的。
     小窗本身还在，只是不再有「标签按钮」这种开关方式，全部由轮盘/胶囊打开。 */

  /** 小窗的元素；kind 允许 'travel' / 'mail' / 'recipe' / 'checkin' / 'model'。 */
  function dialogEls(kind) {
    if (kind === 'mail') {
      // 邮箱窗口也由胶囊打开，没有对应的"标签"，toggle 为空
      return { modal: el.mailModal, toggle: null };
    }
    if (kind === 'recipe') {
      // 食谱窗口同样由胶囊打开，没有对应的"标签"
      return { modal: el.recipeModal, toggle: null };
    }
    if (kind === 'model') {
      // 模型窗口由轮盘打开（第 100 步），没有对应的"标签"
      return { modal: el.modelModal, toggle: null };
    }
    if (kind === 'checkin') {
      // 签到窗口由轮盘小圆打开（第 40 步），同样没有对应的"标签"
      return { modal: el.checkinModal, toggle: null };
    }
    // 出行窗口由胶囊打开，没有对应的"标签"，所以 toggle 为空——
    // 关闭时不还焦点（closeDialog 里会跳过）。
    return { modal: el.travelModal, toggle: null };
  }

  function openDialog(kind) {
    const { modal } = dialogEls(kind);
    if (!modal) return;
    // 两个小窗互斥：开一个就把另一个收掉，避免两窗叠在一起。
    // 注意这里直接改上一个窗口的 hidden 而不是调用 closeDialog()——
    // 后者会把焦点还给上一个标签，接着又要移到新窗口，焦点会闪一下。
    if (state.openDialogKind && state.openDialogKind !== kind) {
      const previous = dialogEls(state.openDialogKind).modal;
      if (previous) previous.hidden = true;
    }
    state.openDialogKind = kind;
    modal.hidden = false;

    // 出行窗口每次都从「聊聊」这一页开始（第 216 步）：先能说话，再谈填表。
    // 上一版的「填写信息」页挪到第二个标签，内容没动；「计划概览」照旧收起。
    if (kind === 'travel') {
      clearDayTabs();
      reattachPlanTab();               // 关掉过就挂回去，再收起
      if (el.travelTabPlan) el.travelTabPlan.hidden = true;
      state.tripEpoch += 1;            // 在途的旧响应作废（评价12 P4-2）
      state.trip = null;
      state.resumeTrip = null;
      state.travelDay = null;
      resetPhotoAssignment();
      if (el.planNotice) el.planNotice.hidden = true;
      showTravelPage('chat');
      setTravelError('');
      // 第 197 步（评价15 B2）：先问一句「上次那份还在吗」，在就挂一条可以点回去的提示。
      // 注意顺序：必须在上面清完 `state.trip` 之后，否则恢复出来的行程会被自己清掉。
      void offerResumeTrip();
    }

    // 邮箱窗口（第 33 步）：每次打开先清掉上次的报错，再问后端"这个会话还登着吗"。
    // 还登着就直接进收件箱，否则停在「添加邮箱」页。
    // prepareMail 是 async，但第一句是同步执行的，所以不会闪一下空白。
    if (kind === 'mail') {
      // 留一个句柄（第 198 步）：桌宠气泡点进来时要等这次准备完再定位邮件，
      // 否则会在"列表还没加载"的时候去找那一行，必然找不到
      state.mailPrepare = prepareMail();
    }

    // 食谱窗口（第 37 步）：每次打开先取一次 schema（只取一次），
    // 再问后端"这个会话之前聊到哪了"。服务重启后内存清空 →
    // 后端说"什么都没有"，于是回到开场白，**而不是留一个点不动的界面**。
    if (kind === 'recipe') {
      void prepareRecipe();
    }

    // 签到窗口（第 40 步）：每次都回到**当月**再取数。
    // 为什么强制回当月：上一次可能翻到了几个月前在补签，关掉再打开时
    // 停在那个月会让人以为"今天怎么不见了"。
    if (kind === 'checkin') {
      state.checkinMonth = null;
      state.checkin = null;
      state.checkinCardNote = null;
      answerCheckinConfirm(false);            // 万一下次打开时确认框还挂着
      setCheckinState('正在打开日历…');
      if (el.checkinDays) el.checkinDays.replaceChildren();
      if (el.checkinMonthLabel) el.checkinMonthLabel.textContent = '';
      if (el.checkinGrid) el.checkinGrid.hidden = true;
      void prepareCheckin();
    }
  }

  function closeDialog() {
    const kind = state.openDialogKind;
    if (!kind) return;
    const { modal, toggle } = dialogEls(kind);
    state.openDialogKind = null;
    if (modal) modal.hidden = true;
    // 菜谱详情是**窗口内部的覆盖层**，不是独立的 modal：关窗时它不会跟着消失。
    // 不复位的话，下次打开食谱窗口会有一层旧详情直接盖在首页上（自查抓到的缺陷）。
    closeRecipeDetail();
    // 关窗时把还在飞的 /ask 掐掉：否则它回来以后会往**新开的**窗口里追加
    // 一条"没有对应提问"的回复和卡片（行为验证抓到的串台缺陷）。
    abortRecipeAsk();
    // 关闭后把焦点还给打开它的那个标签，键盘操作不至于丢失位置
    if (toggle) toggle.focus();
  }

  /* -------------------------------------------------- 出行窗口（第 25 步） */

  /** 收集表单为一次出行请求的 JSON 体（第 41 步）。
   *
   *  这里**不再自己算天数与日期**：那是一个业务决定，已挪到后端
   *  （core/itinerary.py 的 build_trip_dates）。前端算一份、后端也算一份，
   *  正是"两套真相源"的老毛病——屏幕上那份和后端那份迟早不一致。
   */
  function collectTravelForm() {
    return {
      session_id: tripSessionId(),
      city: (el.travelCity ? el.travelCity.value : '').trim(),
      origin_city: (el.travelOrigin ? el.travelOrigin.value : '').trim(),
      start_date: el.travelStart ? el.travelStart.value : '',
      end_date: el.travelEnd ? el.travelEnd.value : '',
      transport: el.travelTransport ? el.travelTransport.value : '',
      stay: el.travelStay ? el.travelStay.value : '',
      preferences: selectedPreferences(),
      note: (el.travelNote ? el.travelNote.value : '').trim(),
    };
  }



  /** 切到出行的某一页：'form' 填信息 / 'plan' 计划概览 / 'day' 某天的详情。
   *
   *  标签语义（第 27 步起、第 30 步完善）：像浏览器——
   *  - 「填写信息」是常驻标签，**不可关闭**；
   *  - 「计划概览」点过「确定」后出现，可关闭；
   *  - 每打开一天就多一个「第x天」标签，**互不替换**，各自可关闭。
   */
  function showTravelPage(page) {
    state.travelPage = page;

    // 标签**一旦打开就一直在**，只有点 × 才消失（浏览器的做法）：
    // 切页只改高亮，绝不隐藏别的标签。
    // 第 31 步这里犯过错：把「计划概览」的显隐跟当前页绑在一起，
    // 于是点了「第1天」就把概览标签顶掉了——用户要的是都保留。
    if (page === 'plan') showPlanTab();

    if (el.travelPageChat) el.travelPageChat.hidden = page !== 'chat';
    if (el.travelPageForm) el.travelPageForm.hidden = page !== 'form';
    if (el.travelPagePlan) el.travelPagePlan.hidden = page !== 'plan';
    if (el.travelPageDay) el.travelPageDay.hidden = page !== 'day';

    const active = {
      chat: el.travelTabChat,
      form: el.travelTabForm,
      plan: el.travelTabPlan,
      day: state.travelDay ? state.dayTabs[state.travelDay] : null,
    }[page];

    allTravelTabs().forEach((tab) => {
      const on = tab === active;
      tab.classList.toggle('is-active', on);
      tab.setAttribute('aria-selected', String(on));
    });
  }

  /** 标签栏里所有"出行"标签（填写信息 / 计划概览 / 各天的）。 */
  function allTravelTabs() {
    const tabs = [el.travelTabChat, el.travelTabForm, el.travelTabPlan];
    Object.values(state.dayTabs).forEach((tab) => tabs.push(tab));
    return tabs.filter(Boolean);
  }

  // ---- 出行对话（第 216 步）---------------------------------------------
  /** 往出行对话里加一条消息。直接复用聊天气泡的样式（msg / msg-user / msg-pet）。 */
  function addTravelMessage(kind, text) {
    if (!el.travelChatLog) return null;
    const node = document.createElement('div');
    node.className = `msg msg-${kind}`;
    node.textContent = text;            // 一律 textContent，杜绝 XSS
    el.travelChatLog.append(node);
    el.travelChatLog.scrollTop = el.travelChatLog.scrollHeight;
    return node;
  }

  /** 往出行对话里贴一张路线图（第 227 步）。
   *  图本身由服务端画（`/api/map/route.png`）：前端只负责显示，
   *  不拼地址、不碰 key —— 这条线在哪画全是服务端的事。 */
  function addTravelRouteImage(route) {
    if (!el.travelChatLog || !route || !route.url) return null;
    const figure = document.createElement('figure');
    figure.className = 'msg-travel-map';

    const img = document.createElement('img');
    img.className = 'travel-map-img';
    img.alt = '路线图';
    img.loading = 'lazy';                 // 聊天记录里可能有好几张，别一次全拉
    img.decoding = 'async';
    img.src = String(route.url);
    img.addEventListener('error', () => {
      // 图挂了不能留一个看不见的空框：换成一句人话
      figure.classList.add('travel-map--broken');
      img.remove();
      const note = document.createElement('p');
      note.className = 'travel-map-note';
      note.textContent = '路线图没加载出来（上面的路线文字还在）';
      figure.append(note);
    });
    figure.append(img);

    if (route.text) {
      const caption = document.createElement('figcaption');
      caption.textContent = String(route.text);
      figure.append(caption);
    }

    el.travelChatLog.append(figure);
    el.travelChatLog.scrollTop = el.travelChatLog.scrollHeight;
    return figure;
  }

  /** 出行对话：说一句 → 服务端查工具 → 回一段话（外加「没查到的名字」提醒）。
   *  为什么不做流式：工具调用 + 事后核对都在服务端一次做完，
   *  这里流式反而会把"正在查"和"查完了"混在一起。 */
  async function onTravelChatSubmit(event) {
    if (event && event.preventDefault) event.preventDefault();
    if (!el.travelChatInput || state.travelChatBusy) return;
    const text = String(el.travelChatInput.value || '').trim();
    if (!text) {
      // 只有空格就不发（也别把空格留在框里，看着像卡住了）
      el.travelChatInput.value = '';
      return;
    }

    addTravelMessage('user', text);
    el.travelChatInput.value = '';
    state.travelChatBusy = true;
    if (el.travelChatSend) el.travelChatSend.disabled = true;
    const pending = addTravelMessage('pet', '我去查一下……');

    try {
      const response = await apiFetch(`${API}/trip/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, session_id: tripSessionId() }),
      });
      if (response.status === 503) {
        if (pending) pending.textContent = '出行对话还没准备好（后端没接上），稍后再试。';
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const answer = String((data && data.content) || '').trim()
        || '这次没想好怎么答，换个说法再说一次？';
      if (pending) pending.textContent = answer;
      else addTravelMessage('pet', answer);

      const suspects = (data && data.suspects) || [];
      if (suspects.length) {
        // 只有「工具查过、但没有这个名字」时后端才会给 suspects，
        // 所以这里可以放心提醒；工具全失败时它能返回空。
        addTravelMessage(
          'bot',
          `这几个地方我没有查到，先别当成真的：${suspects.join('、')}`
        );
      }

      // 查过路线就把图画出来（第 227 步）：宁可多一张图，也别让用户
      // 读完「驾车约 144 公里」还要自己去地图里找这条路。
      const routes = (data && data.routes) || [];
      routes.forEach((route) => addTravelRouteImage(route));
    } catch (error) {
      // describeError 自带句末标点，别再拼一个「。」进去（会变成「？。」）
      const reason = String(describeError(error) || '').replace(/[。！？]+$/, '');
      const message = `没连上（${reason}）。要不要试试「自己填」那边？`;
      if (pending) pending.textContent = message;
      else addTravelMessage('pet', message);
    } finally {
      state.travelChatBusy = false;
      if (el.travelChatSend) el.travelChatSend.disabled = false;
    }
  }

  /** 造一个可关闭的标签按钮（用于「计划概览」与「第x天」）。
   *  × 是独立按钮：点它只关标签，不会顺带切过去。 */
  function makeTravelTab(label, onSelect, onClose) {
    const tab = document.createElement('span');
    tab.className = 'tab tab--closable';
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', 'false');

    const text = document.createElement('button');
    text.type = 'button';
    text.className = 'tab-label';
    text.textContent = label;
    text.addEventListener('click', onSelect);

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'tab-close';
    close.textContent = '×';
    close.title = `关闭「${label}」`;
    close.setAttribute('aria-label', `关闭「${label}」标签`);
    close.addEventListener('click', onClose);

    tab.append(text, close);
    return tab;
  }

  /** 把「计划概览」标签挂回标签栏（它可能被关掉过、已从 DOM 移除）。
   *  只负责"挂回去"，不动 hidden —— 显示与否由调用方决定。 */
  function reattachPlanTab() {
    if (!el.travelTabPlan || el.travelTabPlan.parentNode || !el.travelTabs) return;
    // 插在「填写信息」后面，位置和初始一致
    const anchor = el.travelTabForm ? el.travelTabForm.nextSibling : null;
    if (anchor && typeof el.travelTabs.insertBefore === 'function') {
      el.travelTabs.insertBefore(el.travelTabPlan, anchor);
    } else {
      el.travelTabs.append(el.travelTabPlan);
    }
  }

  /** 显示「计划概览」标签（需要时先挂回去）。 */
  function showPlanTab() {
    if (!el.travelTabPlan) return;
    reattachPlanTab();
    el.travelTabPlan.hidden = false;
  }

  /** 打开（或聚焦）第 index 天的标签页。 */
  function openDayTab(index) {
    if (!el.travelTabs) return;
    let tab = state.dayTabs[index];
    if (!tab) {
      tab = makeTravelTab(
        `第${index}天`,
        () => openDayTab(index),
        () => closeDayTab(index)
      );
      state.dayTabs[index] = tab;
      el.travelTabs.append(tab);
    }
    renderDayDetail(index);
  }

  /** 关闭某一天的标签；如果关的正是当前页，就退到概览（没有概览就退到表单）。 */
  function closeDayTab(index) {
    const tab = state.dayTabs[index];
    if (!tab) return;
    delete state.dayTabs[index];
    if (tab.parentNode && typeof tab.parentNode.removeChild === 'function') {
      tab.parentNode.removeChild(tab);
    } else if (typeof tab.remove === 'function') {
      tab.remove();
    }

    if (state.travelPage === 'day' && Number(state.travelDay) === Number(index)) {
      state.travelDay = null;
      showTravelPage(el.travelTabPlan && !el.travelTabPlan.hidden ? 'plan' : 'form');
    }
  }

  /** 关闭「计划概览」标签：一并收掉它下面的每日详情标签（浏览器关父标签的语义）。 */
  function closePlanTab() {
    Object.keys(state.dayTabs).forEach((key) => closeDayTab(Number(key)));
    if (el.travelTabPlan) {
      el.travelTabPlan.hidden = true;
      if (el.travelTabPlan.parentNode
          && typeof el.travelTabPlan.parentNode.removeChild === 'function') {
        el.travelTabPlan.parentNode.removeChild(el.travelTabPlan);
      } else if (typeof el.travelTabPlan.remove === 'function') {
        el.travelTabPlan.remove();
      }
    }
    state.trip = null;
    state.travelDay = null;
    showTravelPage('form');
  }


  /* ------------------------------------------------ 概览页与详情页（第 41 步）
   *
   *  下面所有渲染都只读 state.trip —— 后端 POST /api/trip 返回的结构化行程。
   *  三条硬规矩，都是为了不再出现"看起来成功其实是假的"：
   *  1. 没有数据源的字段**根本不渲染**（门票价、人均消费、酒店推荐、景点描述）；
   *  2. 估算值与真实值分得开：游览时长写"约 X（估算）"，
   *     营业时间/评分是高德给的真实值，直接用；
   *  3. 问题直接摆出来（降级原因、当天塞不下、缺坐标），不藏、不静默。
   */

  /** 逐日预报的一句话（第 234 步）。
   *
   *  标签必须写「预报」：和风给的是预报，不是观测。没数据就返回空串，
   *  界面宁可不显示，也不拿相邻两天凑一个温度出来。
   */
  function forecastText(forecast) {
    if (!forecast) return '';
    const formatTemp = (value) => {
      if (value === null || value === undefined || value === '') return '';
      const number = Number(value);
      if (!Number.isFinite(number)) return '';
      return String(Math.round(number * 10) / 10);
    };
    const bits = [];
    const textDay = String(forecast.text_day || '').trim();
    const textNight = String(forecast.text_night || '').trim();
    if (textDay) bits.push(textDay);
    else if (textNight) bits.push(textNight);
    if (textNight && textNight !== textDay) bits.push(`夜间${textNight}`);
    const unit = forecast.temp_unit || '°C';
    const high = formatTemp(forecast.temp_max);
    const low = formatTemp(forecast.temp_min);
    if (low && high) bits.push(`${low}~${high}${unit}`);
    else if (high) bits.push(`${high}${unit}`);
    else if (low) bits.push(`最低 ${low}${unit}`);
    const probability = forecast.precip_prob;
    if (probability !== null && probability !== undefined && probability !== '') {
      const percent = Math.round(Number(probability) * 100);
      if (Number.isFinite(percent) && percent >= 30) bits.push(`降水概率 ${percent}%`);
    }
    if (!bits.length) return '';
    return `预报 ${bits.join(' ')}`;
  }

  /** 概览页「建议」：只说有来源的事。 */
  function adviceText(trip) {
    const parts = [];
    // 第 231 步：写了出发地就先说清「从哪出发」——这是用户自己填的事实，
    // 不是我们的推断；没填就不出现，不占地方也不编。
    const origin = String(trip.origin_city || '').trim();
    if (origin) parts.push(`从${origin}出发`);
    const weather = trip.weather;
    if (weather) {
      const bits = [];
      if (weather.location) bits.push(weather.location);
      if (weather.temp !== null && weather.temp !== undefined) {
        bits.push(`${weather.temp}${weather.temp_unit || '°C'}`);
      }
      if (weather.text) bits.push(weather.text);
      parts.push(`实况天气 ${bits.join(' ')}`);
    } else {
      parts.push('这次没拿到实况天气');
    }
    parts.push(`按你选的交通方式（${trip.transport || '不限'}）安排`);
    return parts.join('；') + '。';
  }

  /** 需要让用户看见的问题。静默降级是不允许的，所以它们显示在概览页上。 */
  function tripNotices(trip) {
    const notes = [];
    if (trip.status === 'degraded') {
      const reasons = trip.degraded_reasons || [];
      notes.push(`这次有部分数据没拿到：${reasons.join('；')}`);
    }
    const issues = trip.issues || [];
    if (issues.length) notes.push(`需要留意：${issues.join('；')}`);
    return notes;
  }

  /** 渲染概览页：标题卡（日期/天气）+ 每日行程列表。 */
  function renderOverview() {
    const trip = state.trip;
    if (!trip) return;

    if (el.overviewTitle) el.overviewTitle.textContent = `${trip.city}旅行计划`;
    if (el.overviewDates) {
      const span = trip.start_date && trip.end_date
        ? `${trip.start_date} 至 ${trip.end_date}`
        : '';
      el.overviewDates.textContent = span
        ? `${span}（共 ${trip.days.length} 天）`
        : `共 ${trip.days.length} 天`;
    }
    if (el.overviewAdvice) el.overviewAdvice.textContent = adviceText(trip);

    // 降级与校验问题放在最显眼的位置：用户必须知道自己看到的东西缺了什么
    if (el.planNotice) {
      const notes = tripNotices(trip);
      el.planNotice.textContent = notes.join(' ');
      el.planNotice.hidden = notes.length === 0;
    }

    if (el.dayList) {
      el.dayList.replaceChildren();
      trip.days.forEach((day) => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'day-item';
        item.dataset.day = String(day.index);

        const arrow = document.createElement('span');
        arrow.className = 'day-arrow';
        arrow.setAttribute('aria-hidden', 'true');
        arrow.textContent = '›';

        const name = document.createElement('span');
        name.className = 'day-name';
        name.textContent = `第${day.index}天`;

        const date = document.createElement('span');
        date.className = 'day-date';
        date.textContent = day.date;

        item.append(arrow, name, date);
        // 每一天的天气单独标「预报」；这一天没有预报就不出现（第 234 步）。
        const forecast = forecastText(day.forecast);
        if (forecast) item.append(elWith('span', 'day-forecast', forecast));
        el.dayList.append(item);
      });
    }

    showPlanTab();
    if (el.travelOverview) el.travelOverview.hidden = false;
    // 地图入口按需出现：先问配置，没配就不显示这张卡（不占地方、也不报错）
    prepareTravelMap(trip).catch(() => {});
  }

  /* ------------------------------------------------ 行程地图（第 224 步） */

  /// 三件事分开做：先问配置（能不能用）→ 画出按天的按钮 → 用户点了才加载高德脚本。
  /// 为什么要这么绕：高德 JS API 的脚本约 1 MB，
  /// 没点开地图的人不该为它付流量（这个取舍写在 static/map.js 开头）。
  const mapState = { config: null, loaded: false, focus: null, pending: false, epoch: 0, signature: '' };

  async function loadMapConfig() {
    if (mapState.config) return mapState.config;
    try {
      const response = await apiFetch('/api/map/config', {
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      mapState.config = await response.json();
    } catch (_) {
      // 取不到配置就当没配：卡片不出现，用户也不会看到一个半死不活的控件
      mapState.config = { enabled: false, key: '', service_host: '/_AMapService', note: '' };
    }
    return mapState.config;
  }

  function setMapNote(text) {
    if (!el.travelMapNote) return;
    el.travelMapNote.textContent = text || '';
    el.travelMapNote.hidden = !text;
  }

  function mapDayChips(trip) {
    if (!el.travelMapDays || !trip) return;
    el.travelMapDays.replaceChildren();
    const make = (label, day) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'ov-map-day';
      button.dataset.day = day === null ? '' : String(day);
      button.setAttribute('role', 'tab');
      button.setAttribute('aria-selected', mapState.focus === day ? 'true' : 'false');
      button.textContent = label;
      return button;
    };
    el.travelMapDays.append(make('全部', null));
    trip.days.forEach((day) => el.travelMapDays.append(make(`第${day.index}天`, day.index)));
  }

  /// 同一份行程的指纹。用来分辨"真的换了一份行程"和"同一份又被渲染了一遍"。
  function mapSignature(trip) {
    if (!trip) return '';
    return `${trip.city || ''}|${trip.start_date || ''}|${(trip.days || []).length}`;
  }

  async function prepareTravelMap(trip) {
    if (!el.travelMapCard) return;
    const signature = mapSignature(trip);
    // 已经为这份行程展开过了：**不要再把它收起来**。
    // 第 224 步实测发现的坑：概览页会渲染不止一次（提交一次、切回概览也可能再渲染一次），
    // 第二次进来把 hidden / 按钮文案全复位，用户正看着的地图就自己收起来了。
    if (mapState.loaded && mapState.signature === signature) return;
    const epoch = ++mapState.epoch;
    const config = await loadMapConfig();
    // 等配置的这段时间里可能又来了新的一份：这次作废，不然会把更新的那份盖掉
    if (epoch !== mapState.epoch) return;
    el.travelMapCard.hidden = !config.enabled;
    if (!config.enabled) return;
    mapState.focus = null;
    mapState.loaded = false;
    mapState.signature = signature;
    mapDayChips(trip);
    setMapNote('');
    if (el.travelMapCanvas) el.travelMapCanvas.hidden = true;
    if (el.travelMapToggle) {
      el.travelMapToggle.setAttribute('aria-expanded', 'false');
      el.travelMapToggle.textContent = '展开地图';
    }
  }

  async function expandTravelMap() {
    const trip = state.trip;
    if (!trip || !el.travelMapCanvas || mapState.pending) return;
    if (!window.TravelMap) {
      setMapNote('地图脚本没加载上（map.js），刷新一下再试');
      return;
    }
    mapState.pending = true;
    el.travelMapCanvas.hidden = false;
    setMapNote('正在加载地图…');
    try {
      await window.TravelMap.render(el.travelMapCanvas, trip.days, {
        config: mapState.config,
        focus: mapState.focus,
        onPick: (point) =>
          setMapNote(`${point.name}（第${point.day}天 · 第${point.order}站）`),
        // 底图迟迟不出来时由 map.js 判断原因并回一句话（第 230 步）。
        // 这里**不能**在 render 之后无条件 setMapNote('')：那句解释是 6 秒后才到的，
        // 当场抹掉的话，用户看到的就还是一块白板。
        onNote: (note) => setMapNote(note || ''),
      });
      mapState.loaded = true;
    } catch (error) {
      // 地图挂了要说清原因，而不是留一块空白
      el.travelMapCanvas.hidden = true;
      setMapNote(`地图没能显示：${error.message}`);
    } finally {
      mapState.pending = false;
    }
  }

  function toggleTravelMap() {
    if (!el.travelMapCanvas || !el.travelMapToggle) return;
    const open = el.travelMapToggle.getAttribute('aria-expanded') === 'true';
    if (open) {
      el.travelMapToggle.setAttribute('aria-expanded', 'false');
      el.travelMapToggle.textContent = '展开地图';
      el.travelMapCanvas.hidden = true;
      return;
    }
    el.travelMapToggle.setAttribute('aria-expanded', 'true');
    el.travelMapToggle.textContent = '收起地图';
    expandTravelMap();
  }

  function focusTravelMap(day) {
    mapState.focus = day;
    mapDayChips(state.trip);
    const expanded =
      el.travelMapToggle && el.travelMapToggle.getAttribute('aria-expanded') === 'true';
    if (!mapState.loaded || !expanded) {
      // 还没展开（或还没渲染过）：直接展开并按这一天的范围画
      if (el.travelMapToggle) {
        el.travelMapToggle.setAttribute('aria-expanded', 'true');
        el.travelMapToggle.textContent = '收起地图';
      }
      expandTravelMap();
      return;
    }
    if (window.TravelMap) window.TravelMap.setFocus(day);
  }

  /* ------------------------------------------------ 上次的行程（第 197 步 / 评价15 B2） */

  /// 上次生成的 trip_id 记在浏览器里。**只记 id，不记整份行程**：
  /// 行程的唯一数据源是 `state.trip`（第 41 步定的），前端再存一份就会出现两份可能不一致的真话；
  /// 而服务端 `GET /api/trip/{id}` 本来就有（当初就是给"刷新后恢复"留的），只是一直没人用。
  const TRIP_KEY = 'pet.trip.last';

  function rememberTripId(tripId) {
    if (!tripId) return;
    try {
      window.localStorage.setItem(TRIP_KEY, String(tripId));
    } catch (_) { /* 隐私模式：记不住就算了，不影响本次使用 */ }
  }

  function forgetTripId() {
    try {
      window.localStorage.removeItem(TRIP_KEY);
    } catch (_) { /* 忽略 */ }
  }

  function rememberedTripId() {
    try {
      return String(window.localStorage.getItem(TRIP_KEY) || '');
    } catch (_) {
      return '';
    }
  }

  /** 开窗时问后端「上次那份还在吗」：在就挂提示条，不在就安静地把记下的 id 忘掉。
   *
   *  三种「不在」要分开处理，因为它们的下一步完全不同：
   *  - 从没记住过 → 什么都不做（大多数人是这种情况）；
   *  - 记住了但后端说 404 → 服务重启过，内存里那份真没了：忘掉 id 且**不挂提示条**。
   *    挂一条点进去必然失败的提示，正是评价15 T1-5 要消灭的那种交互；
   *  - 请求本身失败（断网 / 后端挂了）→ 留着 id 下次再试，也不挂提示条。
   */
  async function offerResumeTrip() {
    if (el.travelResume) el.travelResume.hidden = true;
    state.resumeTrip = null;
    const tripId = rememberedTripId();
    if (!tripId) return;

    const epoch = state.tripEpoch;
    let response;
    try {
      response = await apiFetch(`${API}/trip/${encodeURIComponent(tripId)}`);
    } catch (_) {
      return;                                  // 网络问题：留着 id，下次开窗再试
    }
    if (epoch !== state.tripEpoch) return;     // 窗口被重开过：这次结果作废
    if (response.status === 404) {
      forgetTripId();                          // 服务重启过：这份真没了，别再惦记
      return;
    }
    if (!response.ok) return;

    const data = await readJsonSafely(response);
    if (epoch !== state.tripEpoch) return;
    const trip = data && data.itinerary;
    if (!trip || !Array.isArray(trip.days)) {
      forgetTripId();
      return;
    }

    state.resumeTrip = trip;
    if (el.travelResumeText) {
      const days = trip.days.length;
      const city = trip.city || '上次';
      el.travelResumeText.textContent = days
        ? `上次的行程还在：${city} · ${days} 天`
        : `上次的行程还在：${city}`;
    }
    if (el.travelResume) el.travelResume.hidden = false;
  }

  /** 点「接着看」：把取回来的那份搬进 `state.trip` 再渲染。
   *
   *  必须显式 `showTravelPage('plan')`：正常提交时这一步是"生成中"顺手做的
   *  （`onTravelSubmit` 里 `setTravelGenerating(true)` 之后那句），恢复路径没有那一步，
   *  只调 `renderOverview()` 会把概览渲染在看不见的第一页上（DOM 桩当场抓到过）。
   */
  function resumeTrip() {
    const trip = state.resumeTrip;
    if (!trip) return;
    state.resumeTrip = null;
    state.trip = trip;
    resetPhotoAssignment();      // 恢复的行程也要配图，分配从零开始
    if (el.travelResume) el.travelResume.hidden = true;
    renderOverview();
    showTravelPage('plan');
  }

  /** 点「不要了」：只忘掉浏览器里那个 id，**不删后端那份**（别的窗口可能还在看）。 */
  function dropResumeTrip() {
    state.resumeTrip = null;
    if (el.travelResume) el.travelResume.hidden = true;
    forgetTripId();
  }

  /** 分钟 -> "X 小时 Y 分钟"。 */
  function formatMinutes(minutes) {
    const value = Number(minutes);
    if (!value || value <= 0) return '';
    const hours = Math.floor(value / 60);
    const rest = value % 60;
    if (!hours) return `${rest} 分钟`;
    if (!rest) return `${hours} 小时`;
    return `${hours} 小时 ${rest} 分钟`;
  }

  /** 通行段的说法。驾车值来自高德，直线估算是本地算的——必须分得开。 */
  function legText(leg) {
    const parts = [];
    if (leg.distance_m) parts.push(`${(leg.distance_m / 1000).toFixed(1)} 公里`);
    // 口径由后端决定（第 67 步）：公共交通走公交接口、步行+打车短途走步行接口。
    // 文案必须跟着 mode 变，否则"公交行程里写着驾车 25 分钟"就自相矛盾。
    const mode = leg.mode || 'driving';
    const modeName = { driving: '驾车', walking: '步行', transit: '公交' }[mode] || '驾车';
    if (leg.duration_min) parts.push(`${modeName}约 ${leg.duration_min} 分钟`);
    else if (leg.walk_estimate_min) parts.push(`步行约 ${leg.walk_estimate_min} 分钟`);
    // 过路费（第 235 步）：只有驾车路线会给。为 0 就是没有这回事，
    // 写「0 元」只会让人多问一句；没查到（null）更不能估一个数出来。
    if (mode === 'driving' && typeof leg.tolls === 'number' && leg.tolls > 0) {
      parts.push(`过路费约 ${Math.round(leg.tolls)} 元`);
    }
    const detail = parts.length ? parts.join('，') : '距离未知';
    const fromAmap = String(leg.source || '').startsWith('amap_');
    return fromAmap ? `${detail}（高德${modeName}）` : `${detail}（直线估算）`;
  }

  /** 当天概要：只用真实内容拼，不编"上午故宫下午景山"这种安排。 */
  function daySummary(day) {
    const names = (day.spots || []).map((spot) => spot.name);
    if (!names.length) return '这一天没有排到景点。';
    const total = day.total_minutes
      ? `，预计共约 ${formatMinutes(day.total_minutes)}`
      : '';
    return `安排 ${names.length} 个地点：${names.join('、')}${total}。`;
  }


  function elWith(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /* 景点配图（第 29 步）：走后端 /api/photo 拿 Unsplash 的图。
   *
   * 三条约定：
   * 1. 拿不到图就**什么都不显示**——配图是装饰，不能因为它失败而影响卡片；
   * 2. 署名是 Unsplash 条款要求的，必须显示摄影师名（链接到其主页）与 Unsplash；
   * 3. 同一景点只请求一次（内存里记一份），省下每小时 50 次的搜索额度。 */
  //: 配图候选缓存：关键词 → { candidates, expiresAt }（只存 URL 与署名，不存图片字节）。
  //: `expiresAt === 0` 表示不过期（真的搜到图了）；空结果给短 TTL，见下面说明。
  const photoCache = new Map();
  //: 空结果缓存多久。**与后端 api/routes.py 的 PHOTO_FAIL_TTL 同口径（120 秒）**：
  //: 后端自己就会在 120 秒后重试 Unsplash，前端不该把那个 `[]` 记一辈子 ——
  //: 以前 `photoCache.set(query, [])` 无条件写，一次 5xx 就让景点永远"暂无图片"（评价12 P3-2）。
  const PHOTO_EMPTY_TTL_MS = 120 * 1000;
  //: 缓存条数上限（评价12 P4-4）。Map 的插入顺序当 LRU 用，超了就丢最久没用的那条。
  const PHOTO_CACHE_MAX = 200;

  /** 本次行程里已经用掉的图片地址，以及每个景点最终定下的那张。
   *
   * 两个容器各解决一个问题（第 44 步）：
   * - `usedPhotoUrls`：同一份行程内不许出现同一张图；
   * - `photoAssignment`：切回某一天会重建 DOM、重跑 attachSpotPhoto，
   *   若每次都按"没用过"重挑，来回切页时同一景点会换图。
   *   所以按景点名记住第一次的分配结果，重渲染时直接复用。
   */
  const photoAssignment = new Map();
  const usedPhotoUrls = new Set();

  /// 每份行程一个"代数"。清空时 +1，用来**丢弃上一份行程还在飞的**配图结果：
  /// 重新生成时旧请求的回调可能晚于清空才回来，它会把旧分配写进新行程，
  /// 于是两个景点拿到同一张图（第 36 步邮箱"邮件串台"是同一类问题）。
  let photoGeneration = 0;

  function resetPhotoAssignment() {
    photoGeneration += 1;
    photoAssignment.clear();
    usedPhotoUrls.clear();
  }

  /** 取某个关键词的候选图。**只有拿到了后端的正常响应才缓存**。
   *
   *  三种结果分开对待（评价12 P3-2）：
   *  - 有图 → 缓存且不过期（后端那份是 24 小时，前端没必要更短）；
   *  - 200 但空 → 缓存 120 秒（和后端 PHOTO_FAIL_TTL 同口径，到点自己会重试）；
   *  - 非 2xx / 网络异常 → **不缓存**（限流、5xx、断网都是临时的，
   *    缓存住就等于让这个景点在整个页面生命周期里都没有图）。
   */
  async function loadPhotoCandidates(query) {
    const hit = photoCache.get(query);
    if (hit) {
      if (hit.expiresAt === 0 || Date.now() < hit.expiresAt) {
        // 命中：挪到末尾（Map 的插入顺序当 LRU）
        photoCache.delete(query);
        photoCache.set(query, hit);
        return hit.candidates;
      }
      photoCache.delete(query);        // 过期的空结果：删掉，下面重新请求
    }

    let candidates = [];
    let cacheable = false;
    try {
      const response = await apiFetch(`${API}/photo?query=${encodeURIComponent(query)}`);
      if (response.ok) {
        const data = await response.json();
        if (Array.isArray(data.candidates)) {
          candidates = data.candidates.filter((item) => item && item.url);
        }
        if (!candidates.length && data.photo && data.photo.url) {
          candidates = [data.photo];    // 旧响应形状的兜底
        }
        cacheable = true;
        if (!candidates.length) {
          // Unsplash 真没搜到不算错误（还有高德兜底），但必须留痕，否则排查摸黑
          console.warn(`[桌宠] Unsplash 没拿到配图：${query}`);
        }
      } else {
        console.warn(`[桌宠] 配图接口异常（HTTP ${response.status}），本次不缓存：${query}`);
      }
    } catch (error) {
      console.warn(`[桌宠] 配图请求失败（不缓存）：${query}`, error);
    }

    if (cacheable) {
      photoCache.set(query, {
        candidates,
        expiresAt: candidates.length ? 0 : Date.now() + PHOTO_EMPTY_TTL_MS,
      });
      while (photoCache.size > PHOTO_CACHE_MAX) {
        const oldest = photoCache.keys().next().value;
        if (oldest === undefined) break;
        photoCache.delete(oldest);
      }
    }
    return candidates;
  }

  /** 取一张景点配图并插进卡片。
   *
   * 顺序：**Unsplash 优先 → 高德自带照片兜底 → 占位**（第 43 步），
   * 并且**同一份行程内不重复用同一张图**（第 44 步）。
   *
   * 两条来源的署名必须不同：Unsplash 是条款强制要求的摄影师 + 双链接；
   * 高德标"图片来源：高德地图"。给高德的图挂 Unsplash 的署名就是假署名。
   */
  async function attachSpotPhoto(card, spot, cityName) {
    const spotName = (spot && spot.name) ? spot.name : '';
    const query = [spotName, cityName].filter(Boolean).join(' ');
    if (!query) return;

    // 已经给这个景点定过图就复用：切页重渲染时不许换图
    const decided = photoAssignment.get(spotName);
    if (decided) {
      renderSpotFigure(card, decided);
      return;
    }

    const generation = photoGeneration;
    const candidates = await loadPhotoCandidates(query);
    // 这份行程已经换掉了：本次结果作废，不写进任何状态
    if (generation !== photoGeneration) return;

    // 挑第一张没用过的。**检查与占用之间不能有 await**：
    // 多个景点是并发取图的，中间一旦让出执行权，两个景点就会挑到同一张。
    let chosen = null;
    for (const candidate of candidates) {
      if (candidate && candidate.url && !usedPhotoUrls.has(candidate.url)) {
        chosen = candidate;
        break;
      }
    }
    // 候选都用过了：**宁可重复一张，也不要显示"暂无图片"**。
    // 去重是"能挑就挑"的偏好，不是硬约束——有图永远好过没图。
    if (!chosen && candidates.length) {
      chosen = candidates[0];
    }
    if (chosen) {
      usedPhotoUrls.add(chosen.url);
      const decision = {
        url: chosen.url,
        alt: chosen.alt || spotName,
        source: 'unsplash',
        credit: chosen,
      };
      photoAssignment.set(spotName, decision);
      renderSpotFigure(card, decision);
      return;
    }

    // 退回高德自带的地点照片（来自 POI 的 show_fields=photos）
    const fallback = (spot && Array.isArray(spot.amap_photos) && spot.amap_photos.length)
      ? spot.amap_photos[0]
      : '';
    if (fallback) {
      usedPhotoUrls.add(fallback);
      const decision = { url: fallback, alt: spotName, source: 'amap' };
      photoAssignment.set(spotName, decision);
      renderSpotFigure(card, decision);
      return;
    }

    // 两边都没有：**显示占位**而不是留一片空白。
    // 空白让人分不清"这个景点没有图"还是"功能坏了"（第 32 步用户报"图没了"）。
    // 这一路不写进 photoAssignment：它没有"选中的图"，重渲染再走一遍结果一样。
    if (!card.querySelector('.spot-photo-placeholder')) {
      const placeholder = document.createElement('p');
      placeholder.className = 'spot-photo-placeholder';
      placeholder.textContent = '暂无图片';
      card.insertBefore(placeholder, card.querySelector('.spot-desc') || null);
    }
  }

  /** 把一张图连同**对应来源**的署名插进卡片。 */
  function renderSpotFigure(card, options) {
    const figure = document.createElement('figure');
    figure.className = 'spot-photo';
    // 标出来源，方便验证与将来按来源统计（也让测试能断言走的是哪条路）
    figure.dataset.source = options.source;

    const img = document.createElement('img');
    img.src = options.url;
    img.alt = options.alt || '';
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';   // 不把来源地址带给第三方图床

    const credit = document.createElement('figcaption');
    credit.className = 'spot-credit';

    if (options.source === 'unsplash') {
      const photo = options.credit || {};
      // 署名是 Unsplash 条款的硬要求，两个链接都不能省
      credit.append(document.createTextNode('📷 '));
      const authorLink = document.createElement('a');
      authorLink.href = photo.author_url || 'https://unsplash.com';
      authorLink.target = '_blank';
      authorLink.rel = 'noopener noreferrer';
      authorLink.textContent = photo.author || 'Unsplash';
      credit.append(authorLink, document.createTextNode(' / '));
      const sourceLink = document.createElement('a');
      sourceLink.href = photo.source_url || 'https://unsplash.com';
      sourceLink.target = '_blank';
      sourceLink.rel = 'noopener noreferrer';
      sourceLink.textContent = 'Unsplash';
      credit.append(sourceLink);
    } else {
      // 高德的图不能挂 Unsplash 的署名——那是假署名
      credit.append(document.createTextNode('图片来源：高德地图'));
    }

    figure.append(img, credit);
    // 插到描述之前，视觉上跟标题连着
    card.insertBefore(figure, card.querySelector('.spot-desc') || null);
  }

  /** 渲染某一天的详情页：景点安排 / 通行 / 餐饮 / 当天的校验问题。 */
  function renderDayDetail(index) {
    const trip = state.trip;
    if (!trip) return;
    const day = trip.days.find((item) => item.index === Number(index));
    if (!day) return;

    state.travelDay = day.index;
    if (el.dayTitle) el.dayTitle.textContent = `第${day.index}天 · ${day.date}`;
    if (!el.dayBody) return;

    el.dayBody.replaceChildren();

    // 基本信息：概要 / 交通方式 / 住宿偏好（住宿偏好只是"倾向"，不是酒店推荐）
    const base = elWith('div', 'day-base');
    const baseRows = [
      ['📝 行程概要', daySummary(day)],
      ['🚌 交通方式', trip.transport || '不限'],
      ['🏨 住宿偏好', trip.stay ? `${trip.stay}（只是偏好，不是推荐）` : '不限'],
    ];
    // 第 234 步：每天的天气来自和风逐日预报，**标签写「预报」**，不冒充实况。
    const dayForecast = forecastText(day.forecast);
    if (dayForecast) baseRows.push(['🌦 当天天气', `${dayForecast}（不是实况）`]);
    // 第 79 步：备注以前只存不显（"收了不用"）。既然收了，就让用户看见它，
    // 并且**在界面上标明它不参与筛选**——避免"写了过敏以为会生效"。
    if (trip.note) baseRows.push(['🗒 你的备注', `${trip.note}（只做记录，不参与筛选）`]);
    baseRows.forEach(([label, value]) => {
      const row = elWith('div', 'day-base-row');
      row.append(elWith('span', 'day-base-label', label), elWith('span', 'day-base-value', value));
      base.append(row);
    });
    el.dayBody.append(base);

    // 景点安排：名称/评分/地址/预估游览时长/营业时间 + 从上一站的通行
    el.dayBody.append(elWith('h4', 'day-section-title', '🎡 景点安排'));
    const grid = elWith('div', 'spot-grid');
    (day.spots || []).forEach((spot, i) => {
      const card = elWith('div', 'spot-card');
      const head = elWith('div', 'spot-head');
      head.append(
        elWith('span', 'spot-index', String(i + 1)),
        elWith('span', 'spot-name', spot.name)
      );
      // 评分是高德给的真实值；没有就不显示，不留占位数字
      if (spot.rating !== null && spot.rating !== undefined) {
        head.append(elWith('span', 'spot-cost', `${spot.rating} ⭐`));
      }

      const rows = [['地址', spot.address || '暂无数据']];
      if (spot.duration_min) {
        // 估算是估的：写清"约"和来源，不让它看起来像实测
        rows.push(['游览时长', `约 ${formatMinutes(spot.duration_min)}（估算）`]);
      }
      rows.push(['营业时间', spot.opentime || '暂无数据']);
      const leg = day.legs ? day.legs[i - 1] : null;
      if (leg) rows.push(['从上一站', legText(leg)]);

      const meta = elWith('dl', 'spot-meta');
      rows.forEach(([label, value]) => {
        meta.append(elWith('dt', '', label), elWith('dd', '', value));
      });
      card.append(head, meta);
      if (!spot.verified) {
        card.append(elWith('p', 'spot-desc', '模型建议：高德没有返回过这个地点，未经核实。'));
      }
      grid.append(card);
      // 配图单独异步取：拿不到就显示占位，不影响卡片其它内容
      attachSpotPhoto(card, spot, trip.city);
    });
    if (!(day.spots || []).length) {
      el.dayBody.append(elWith('p', 'day-empty', '这一天没有排到景点。'));
    }
    el.dayBody.append(grid);

    // 餐饮安排：真实餐厅。没有"人均消费"这种数据源，所以整列都不存在。
    el.dayBody.append(elWith('h4', 'day-section-title', '🍽 餐饮安排'));
    if (!(day.meals || []).length) {
      el.dayBody.append(elWith('p', 'day-empty', '这一带这次没搜到餐厅。'));
    } else {
      const meals = elWith('table', 'meal-table');
      day.meals.forEach((meal) => {
        const tr = document.createElement('tr');
        // 餐次是我们**按位置与顺序建议**的（第 125 步），高德不返回这个字段，
        // 所以必须写成"建议"而不是"计划"—— 用户自己会决定几点吃饭。
        const label = meal.slot ? `建议${meal.slot}：${meal.name}` : meal.name;
        tr.append(elWith('th', '', label));
        tr.append(elWith('td', '', meal.address || '暂无地址'));
        if (meal.rating !== null && meal.rating !== undefined) {
          tr.append(elWith('td', '', `${meal.rating} ⭐`));
        }
        meals.append(tr);
      });
      el.dayBody.append(meals);
    }

    // 住宿候选：高德给的**真实酒店**（名称/地址/评分/营业时间）。
    // **没有价格、没有星级** —— 高德不返回这两项，所以这里也不显示，更不编。
    el.dayBody.append(elWith('h4', 'day-section-title', '🏨 住宿候选'));
    if (!(day.hotels || []).length) {
      el.dayBody.append(elWith('p', 'day-empty', '这一带这次没搜到住宿候选。'));
    } else {
      day.hotels.forEach((hotel) => {
        const card = elWith('div', 'hotel-card');
        card.append(elWith('div', 'hotel-name', hotel.name));
        const meta = elWith('dl', 'hotel-meta');
        const rows = [['地址', hotel.address || '暂无数据']];
        if (hotel.rating !== null && hotel.rating !== undefined) {
          rows.push(['评分', `${hotel.rating} ⭐`]);
        }
        if (hotel.opentime) rows.push(['营业时间', hotel.opentime]);
        if (hotel.distance_m) {
          rows.push(['距当天首个景点', `${(hotel.distance_m / 1000).toFixed(1)} 公里`]);
        }
        rows.forEach(([label, value]) => {
          meta.append(elWith('dt', '', label), elWith('dd', '', value));
        });
        card.append(meta);
        el.dayBody.append(card);
      });
      el.dayBody.append(elWith('p', 'day-empty',
        '住宿候选来自高德；房价与星级高德不提供，所以不显示。'));
    }
    // 当天的校验问题：塞不下 / 缺坐标 / 重复 —— 直接摆出来
    if ((day.issues || []).length) {
      el.dayBody.append(elWith('h4', 'day-section-title', '⚠️ 需要留意'));
      const list = elWith('ul', 'day-issues');
      day.issues.forEach((issue) => list.append(elWith('li', '', issue)));
      el.dayBody.append(list);
    }

    showTravelPage('day');
  }

  /** 按开始/结束日期算出旅行天数（含首尾两天），填进只读输入框。 */
  function updateTravelDays() {
    if (!el.travelDays || !el.travelStart || !el.travelEnd) return;
    const start = el.travelStart.value;
    const end = el.travelEnd.value;
    if (!start || !end) {
      el.travelDays.value = '';
      return;
    }
    const startAt = new Date(`${start}T00:00:00`);
    const endAt = new Date(`${end}T00:00:00`);
    const days = Math.round((endAt - startAt) / 86400000) + 1;
    el.travelDays.value = days > 0 ? `${days} 天` : '';
  }

  function setTravelError(message) {
    if (!el.travelError) return;
    el.travelError.textContent = message || '';
    el.travelError.hidden = !message;
  }

  /** 收集表单里的偏好多选项。 */
  function selectedPreferences() {
    const scope = el.travelForm || (typeof document !== 'undefined' ? document : null);
    if (!scope || typeof scope.querySelectorAll !== 'function') return [];
    return Array.from(scope.querySelectorAll('input[type="checkbox"]'))
      .filter((box) => box.checked && box.value)
      .map((box) => box.value);
  }



  /** 校验表单，返回错误信息（通过则返回空串）。 */
  function validateTravelForm() {
    const city = (el.travelCity ? el.travelCity.value : '').trim();
    const start = el.travelStart ? el.travelStart.value : '';
    const end = el.travelEnd ? el.travelEnd.value : '';

    if (!city) return '请填写目的地城市。';
    if (!start) return '请选择开始日期。';
    if (!end) return '请选择结束日期。';
    if (end < start) return '结束日期不能早于开始日期。';
    return '';
  }

  /** 点「确定」：本地校验 → POST /api/trip → 按 status 决定渲染什么。
   *
   *  第 41 步最关键的改动在这里：**status === "failed" 时不渲染行程**，
   *  而是退回第一页并说清缺了什么。以前后端整条链路挂掉（key 错、配额用完），
   *  这里照样渲染出一份漂亮的假行程——静默、自信、错误。
   */
  async function onTravelSubmit(event) {
    event.preventDefault();
    // 万一这个处理器又没被接上（元素缺失 / 缓存不同步），控制台里要能看到
    console.log('[桌宠] 出行：收到提交，开始生成');
    if (state.travelGenerating) return;

    const problem = validateTravelForm();
    if (problem) {
      setTravelError(problem);
      return;
    }
    setTravelError('');
    state.trip = null;
    state.travelDay = null;
    clearDayTabs();
    rememberDestination(el.travelCity ? el.travelCity.value : '');   // 第 219 步
    setTravelGenerating(true);
    showTravelPage('plan');
    const epoch = state.tripEpoch;      // 这次请求属于哪一代（评价12 P4-2）

    try {
      // 第 135 步：带幂等键。一次"提交"对应一个 key —— 中途被代理/客户端重发时，
      // 服务端会回放上一份行程，而不是再生成一份（生成一次要 2~25 秒 + 外部配额）。
      // 用户再点一次"确定"是**新的提交**，所以每次进来都生成新 key。
      const idempotencyKey =
        typeof crypto !== 'undefined' && crypto.randomUUID
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random()}`;
      const response = await apiFetch(`${API}/trip`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': idempotencyKey,
        },
        body: JSON.stringify(collectTravelForm()),
      });

      if (epoch !== state.tripEpoch) return;   // 窗口被重开过：这次响应作废
      if (!response.ok) {
        // 400 = 表单不合规（后端也校验一遍，两边的规则必须一致）；500 = 内部错误
        setTravelError(await readDetail(response));
        showTravelPage('form');
        return;
      }

      if (epoch !== state.tripEpoch) return;
      const data = await readJsonSafely(response);
      const trip = data && data.itinerary;
      if (!trip) {
        setTravelError('服务这次没有返回行程内容。');
        showTravelPage('form');
        return;
      }

      if (trip.status === 'failed') {
        // 拿不到真数据：不渲染行程，明说缺了什么。绝不退回假数据。
        const why = (trip.degraded_reasons || []).join('；') || '没能取到真实数据。';
        setTravelError(`这次没能生成行程：${why}`);
        showTravelPage('form');
        return;
      }

      if (epoch !== state.tripEpoch) return;
      state.trip = trip;
      // 第 197 步：记住这份的 id —— 关掉窗口再打开时能点「接着看」回来。
      // 前提是它真的被存进了后端内存（trip_id 由后端生成），所以直接用返回值里的。
      rememberTripId(data.trip_id || trip.trip_id);
      state.resumeTrip = null;
      if (el.travelResume) el.travelResume.hidden = true;
      resetPhotoAssignment();     // 新行程：配图分配与去重都重新开始
      renderOverview();
    } catch (error) {
      if (epoch !== state.tripEpoch) return;
      setTravelError(describeError(error));
      showTravelPage('form');
    } finally {
      // 作废的那一代不许动按钮状态：新一代可能正在生成中
      if (epoch === state.tripEpoch) setTravelGenerating(false);
    }
  }

  /* ----------------------------------------------------- 目的地候选（第 219 步） */

  /** 常见目的地。**这是本地清单，不是"推荐系统"**：
   *  它不联网、不看天气、不猜预算 —— 只是在输入框里少打几个字。
   *  没列出来的城市照样能填，权威判断在后端的地理编码。
   */
  const POPULAR_DESTINATIONS = [
    '北京', '上海', '广州', '深圳', '杭州', '苏州', '南京', '成都', '重庆',
    '西安', '武汉', '长沙', '厦门', '青岛', '大连', '天津', '济南', '郑州',
    '昆明', '大理', '丽江', '桂林', '三亚', '海口', '贵阳', '兰州', '敦煌',
    '哈尔滨', '长春', '沈阳', '呼和浩特', '乌鲁木齐', '拉萨', '西宁', '银川',
    '合肥', '南昌', '福州', '南宁', '珠海', '佛山', '东莞', '无锡', '宁波',
    '绍兴', '扬州', '洛阳', '开封', '平遥', '婺源', '黄山',
  ];
  const RECENT_DESTINATIONS_KEY = 'pet.trip.recentDestinations';
  const MAX_RECENT_DESTINATIONS = 5;

  function recentDestinations() {
    try {
      const list = JSON.parse(watchStoreGet(RECENT_DESTINATIONS_KEY, '[]'));
      if (!Array.isArray(list)) return [];
      return list
        .map((item) => String(item || '').trim())
        .filter((item) => item)
        .slice(0, MAX_RECENT_DESTINATIONS);
    } catch (_) {
      return [];           // 存的东西坏了就当没有，不该让输入框炸掉
    }
  }

  /** 记住这次填的城市（去重、只留最近几个）。存不进 localStorage 就算了。 */
  function rememberDestination(city) {
    const name = String(city || '').trim();
    if (!name) return;
    const rest = recentDestinations().filter((item) => item !== name);
    watchStoreSet(
      RECENT_DESTINATIONS_KEY,
      JSON.stringify([name, ...rest].slice(0, MAX_RECENT_DESTINATIONS)),
    );
  }

  /** 把「最近去过的」+「常见城市」填进 datalist（去重，最近的在前面）。 */
  function fillDestinationOptions() {
    if (!el.travelCityOptions) return;
    const seen = new Set();
    el.travelCityOptions.textContent = '';
    [...recentDestinations(), ...POPULAR_DESTINATIONS].forEach((name) => {
      if (seen.has(name)) return;
      seen.add(name);
      const option = document.createElement('option');
      option.value = name;
      el.travelCityOptions.appendChild(option);
    });
  }

  /** 复制行程文本（第 130 步）。
   *
   *  文本由**服务端**渲染：前端自己拼会变成两套格式（导出一套、界面一套）。
   *  失败时**不复制**、也明说原因 —— 不要把"复制成功"当成默认。
   */
  async function copyTripText() {
    if (!state.trip) return;
    setTripExportStatus('');
    try {
      const response = await apiFetch(`${API}/trip/text`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ itinerary: state.trip }),
      });
      if (!response.ok) {
        setTripExportStatus('导出失败：' + (await readDetail(response)));
        return;
      }
      const data = await response.json();
      const text = String(data.text || '');
      if (!text) {
        setTripExportStatus('这次没有可导出的内容');
        return;
      }
      await navigator.clipboard.writeText(text);
      setTripExportStatus('已复制到剪贴板');
    } catch (error) {
      // 复制失败（无权限/非 https）也要说实话，别显示"已复制"
      setTripExportStatus('复制失败：' + describeError(error));
    }
  }

  function setTripExportStatus(text) {
    if (el.tripExportStatus) el.tripExportStatus.textContent = text || '';
  }

  /** 把行程存成 .md（第 218 步）。
   *
   *  内容仍然由**服务端**渲染（和「复制行程」同一份渲染器），这里只负责
   *  把它变成一次下载。为什么不自己拼 Markdown：两套格式迟早会漂移。
   *  浏览器不给下载时（老环境没有 createObjectURL）明说原因，不假装存好了。
   */
  async function downloadTripMarkdown() {
    if (!state.trip) return;
    setTripExportStatus('');
    try {
      const response = await apiFetch(`${API}/trip/markdown`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ itinerary: state.trip }),
      });
      if (!response.ok) {
        setTripExportStatus('导出失败：' + (await readDetail(response)));
        return;
      }
      const data = await response.json();
      const text = String(data.markdown || '');
      if (!text) {
        setTripExportStatus('这次没有可导出的内容');
        return;
      }
      if (typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') {
        setTripExportStatus('这个环境不支持存文件，请用「复制行程」');
        return;
      }
      const filename = String(data.filename || '行程.md');
      const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }));
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
      setTripExportStatus('已存为 ' + filename);
    } catch (error) {
      setTripExportStatus('导出失败：' + describeError(error));
    }
  }

  /** 关掉所有「第x天」标签（重新生成行程时用）。 */
  function clearDayTabs() {
    Object.keys(state.dayTabs).forEach((key) => closeDayTab(Number(key)));
  }

  /** 生成中：按钮禁用并改文案，计划页显示进度条而不是空输出区。 */
  function setTravelGenerating(generating) {
    state.travelGenerating = generating;
    if (el.travelSubmit) {
      el.travelSubmit.disabled = generating;
      el.travelSubmit.textContent = generating ? '正在生成…' : '确定';
    }
    if (el.planProgress) el.planProgress.hidden = !generating;
  }

  /* ------------------------------------------------------------ 会话 id */

  function makeSessionId() {
    // 非 HTTPS 的 localhost 上 randomUUID 可能不可用，逐级降级
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    if (window.crypto && typeof window.crypto.getRandomValues === 'function') {
      const bytes = window.crypto.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      const hex = [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }
    return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }

  /** 聊天会话 id。
   *
   *  **第 94 步（S4）从 sessionStorage 换成 localStorage**：
   *  sessionStorage 在关掉页面/窗口时就失效，于是每次打开都是**新会话 id**，
   *  后端就算存了历史也找不到 —— 这是"每次打开都是从头开始"的直接原因。
   *
   *  代价：同一浏览器里所有标签共享一个 id（桌宠是单窗口 webview，无所谓）；
   *  换浏览器/清缓存会开一段新会话（可接受，而且历史仍在库里，只是没有入口翻）。
   */
  function sessionId() {
    let id = null;
    try { id = window.localStorage.getItem(SESSION_KEY); } catch (_) { /* 隐私模式 */ }
    if (!id) {
      id = makeSessionId();
      try { window.localStorage.setItem(SESSION_KEY, id); } catch (_) { /* 忽略 */ }
    }
    return id;
  }

  /** 出行窗口**自己的**会话 id（第 41 步）。
   *
   *  刻意与聊天的 SESSION_KEY 分开（照食谱 pet.recipe_session 的做法）：
   *  出行是"被桌宠带在身边的雨伞"，共用一个会话会让聊天内容渗进出行的上下文，
   *  将来做"查看历史行程"时也分不清哪个 id 属于谁。
   */
  function tripSessionId() {
    let id = null;
    try { id = window.sessionStorage.getItem('pet.trip_session'); } catch (_) { /* 隐私模式 */ }
    if (!id) {
      id = makeSessionId();
      try { window.sessionStorage.setItem('pet.trip_session', id); } catch (_) { /* 忽略 */ }
    }
    return id;
  }

  /* ------------------------------------------------------------ 轮盘菜单 */

  /** 轮盘项点击时发送的意图文本。文案由后端给（工具的 intent 字段），前端只兜底。 */
  function intentOf(tool) {
    return tool.intent || `帮我用${tool.label || tool.name}看看。`;
  }

  /** 把第 index 个小圆摆到环绕桌宠的**上方弧线**上。
   *
   *  为什么位置要在 JS 里算：CSS 无法对任意数量做三角函数运算，
   *  而"环绕"本质是极坐标布局（半径 + 角度）。
   *  这里只算两个偏移量交给 CSS 变量，外观（圆形外框、悬停显字）仍归 CSS。
   *
   *  弧线范围取 40° → 140°（以正上方 90° 为中心，左右对称）：
   *  五个小圆在圆周上等距排列，每个到轮盘中心的距离都是 `--orbit`。
   *  轮盘整体「视觉居中」交给 CSS 的 `--wheel-shift-x`（桌宠素材的可视中心
   *  不在画布正中，见 style.css 的注释）——这里只负责对称的弧，不掺平移。
   *  角度换算：0°=正右、逆时针为正，故 90°=正上、140°=左上、40°=右上。 */
  const ARC_START = 40;
  const ARC_END = 140;

  function placeOnOrbit(node, index, total) {
    const radius = parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue('--orbit')
    ) || 96;

    // 均匀铺满整段弧：1 项居中，2 项分居两端，n 项等距
    const t = total > 1 ? index / (total - 1) : 0.5;
    const angle = ARC_START + (ARC_END - ARC_START) * t;
    const rad = (angle * Math.PI) / 180;

    // 屏幕坐标 y 轴朝下，所以 y 取负
    node.style.setProperty('--tx', `${(Math.cos(rad) * radius).toFixed(1)}px`);
    node.style.setProperty('--ty', `${(-Math.sin(rad) * radius).toFixed(1)}px`);
  }

  /** 用工具清单 + 内置项重建轮盘。
   *
   *  布局（第 39 步起）：桌宠本体居中，各子功能做成**环绕它的小圆**，
   *  圆形外框里只放图标；文字默认隐藏，鼠标停在该图标上才浮出来。
   *  位置交给 CSS grid（见 .wheel-list 的 3 列扇形），这里不算坐标。
   *
   *  两类条目：
   *    - 有 run 的：可点击，点击后收起菜单并执行
   *    - 只有 label/icon 的（WHEEL_PLACEHOLDERS）：占位，做成禁用，
   *      与 renderChips 的占位处理保持一致——避免"点了没反应"被当成坏了。 */
  function renderWheel() {
    const actions = state.menuTools.map((tool) => ({
      key: tool.name,
      icon: ICONS[tool.name] || DEFAULT_ICON,
      label: tool.label || tool.name,
      run: () => sendMessage(intentOf(tool)),
    }));

    actions.push({
      key: 'chat',
      icon: CHAT_ICON,
      label: '对话',
      run: () => openChat(true),
    });

    // 与「对话」同级的入口：模型（第 100 步）、签到（第 40 步起有内容）+ 两个占位项
    actions.push(WHEEL_MODEL, WHEEL_CHECKIN, WHEEL_ACTIONS, ...WHEEL_PLACEHOLDERS);

    el.menuList.replaceChildren();
    actions.forEach((action, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'wheel-item';
      button.dataset.key = action.key;
      button.style.setProperty('--delay', `${index * 45}ms`);
      // 始终带 title：即便文字默认隐藏，悬停时也有系统提示兜底
      button.title = action.label;
      // 摆到环绕圆周上（角度由序号算出）
      placeOnOrbit(button, index, actions.length);

      const icon = document.createElement('span');
      icon.className = 'wheel-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = action.icon;

      // 文字包一层，才能只靠 CSS 控制"悬停才出现"
      const label = document.createElement('span');
      label.className = 'wheel-label';
      label.textContent = action.label;

      button.append(icon, label);

      if (action.run) {
        button.addEventListener('click', (event) => {
          closeMenu();
          action.run(event);
        });
      } else {
        // 占位：还没有内容。禁用比"点了没反应"更清楚。
        // 不禁用 hover 显字——文案本来就该能看到，用灰一档表示不可点。
        button.disabled = true;
      }

      el.menuList.append(button);
    });
  }

  /* -------------------------------------------------------- 快捷胶囊 */

  // 点菜单外面关掉它（第 150 步）。桌面右键菜单的老规矩：点别处就收。
  document.addEventListener('click', (event) => {
    if (!state.actionMenuOpen) return;
    const target = event && event.target;
    if (el.actionMenu && target && el.actionMenu.contains && el.actionMenu.contains(target)) {
      return;                     // 点在菜单自己身上：菜单项的处理函数会负责关
    }
    closeActionMenu();
  });

  if (el.chatMore) {
    el.chatMore.addEventListener('click', () => toggleChatTools());
    // 显式初始化一次：让 aria-expanded / is-open / chips.hidden 三者从第一帧起就一致
    // （DOM 桩里没有 HTML 属性，靠这一句才拿到 "false"）
    toggleChatTools(false);
  }

  /** 按 CHIP_ENTRIES 重建胶囊行。
   *  有 panel 的点了打开功能窗口；没有 panel 的是占位，标成禁用。 */
  function renderChips() {
    if (!el.chatChips) return;
    el.chatChips.replaceChildren();

    CHIP_ENTRIES.forEach((entry) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'chip';
      button.dataset.key = entry.id;
      button.textContent = entry.label;

      if (entry.panel) {
        button.addEventListener('click', () => {
          toggleChatTools(false);   // 选完就收起（第 146 步）
          openDialog(entry.panel);
        });
      } else {
        // 占位：本轮还没有功能。做成禁用而不是"点了没反应"，
        // 用户至少知道是自己点不动，而不是界面坏了。
        button.disabled = true;
        button.title = '还没做';
      }

      el.chatChips.append(button);
    });

    updateChips();
  }

  /** 决定这一行此刻该不该显示：用户已经在打字时收起。 */
  function updateChips() {
    if (!el.chatChips) return;
    const typing = Boolean(el.input && el.input.value.trim());
    // 两个条件：正在打字要收起（保持原有行为）**并且**用户点了「›」才展开（第 146 步）
    el.chatChips.hidden = typing || !state.chatToolsOpen;
  }

  /** 展开/收起功能栏。`open` 省略时取反。 */
  function toggleChatTools(open) {
    const next = typeof open === 'boolean' ? open : !state.chatToolsOpen;
    state.chatToolsOpen = next;
    if (el.chatTools) el.chatTools.classList.toggle('is-open', next);
    if (el.chatMore) el.chatMore.setAttribute('aria-expanded', next ? 'true' : 'false');
    updateChips();
  }

  function isMenuOpen() { return state.menuOpen; }

  function openMenu() {
    state.menuOpen = true;
    el.menu.hidden = false;
    // 先让浏览器应用 hidden=false 再触发过渡，否则动画不会播
    requestAnimationFrame(() => el.menu.classList.add('is-open'));
    el.petButton.setAttribute('aria-expanded', 'true');
  }

  function closeMenu(focusPet = false) {
    state.menuOpen = false;
    el.menu.classList.remove('is-open');
    el.petButton.setAttribute('aria-expanded', 'false');
    window.setTimeout(() => { if (!state.menuOpen) el.menu.hidden = true; }, 180);
    if (focusPet) el.petButton.focus();
  }

  /* -------------------------------------------------------- 动作菜单（第 150 步） */

  /** 按 PET_ACTIONS 重建菜单项。每次都重建：动作集合将来会变。 */
  function renderActionMenu() {
    if (!el.actionMenu) return;
    el.actionMenu.replaceChildren();
    PET_ACTIONS.forEach((action) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'action-item';
      item.dataset.action = action.id;
      item.setAttribute('role', 'menuitem');
      item.textContent = `${action.icon} ${action.label}`;
      item.addEventListener('click', () => {
        closeActionMenu();
        // 留一个标记：桩里定时器是同步的，点完可能立刻被待机链再演一次，
        // 只有"手动点过哪一个"是不会被覆盖的事实（也方便排查）。
        if (el.pet) el.pet.dataset.lastManual = action.id;
        playAction(action, { manual: true });
      });
      el.actionMenu.append(item);
    });
  }

  function openActionMenu(event) {
    if (!el.actionMenu) return;
    // 挡住同一次点击的冒泡：下面的"点外面关菜单"监听挂在 document 上，
    // 不挡的话菜单刚开就被自己这一下关掉（很隐蔽的一个坑）。
    if (event && event.stopPropagation) event.stopPropagation();
    closeMenu();                       // 轮盘让位，免得两层浮层叠在一起
    renderActionMenu();
    el.actionMenu.hidden = false;
    state.actionMenuOpen = true;
    requestAnimationFrame(() => el.actionMenu.classList.add('is-open'));
    const first = el.actionMenu.children[0];
    if (first && first.focus) first.focus();
  }

  function closeActionMenu() {
    if (!el.actionMenu || !state.actionMenuOpen) return;
    state.actionMenuOpen = false;
    el.actionMenu.classList.remove('is-open');
    window.setTimeout(() => {
      if (!state.actionMenuOpen) el.actionMenu.hidden = true;
    }, 120);
  }


  /* ------------------------------------------------------ 待机动画（第 151 步）

   * 素材是 APNG（`static/pet-idle.png`），**只播一次**（编码时 `-plays 1`），
   * 所以想"再演一遍"必须让它重新加载。做法是每次换一个查询串：
   * 浏览器把 `pet-idle.png?cycle=3` 当成**另一个资源**，于是从头播。
   *
   * 为什么不用 blob / canvas：这里是本地文件，一次 2.3MB 的磁盘读可以忽略；
   * 而换 URL 这一招在桩里也能断言（看 src 变没变），blob 就没法测了。
   */
  // 节奏：**演完一个动作 → 静 10 秒 → 随机演下一个动作**（用户第 151 步的口径）。
  //: 两个动作之间的间隔。**0 = 播完立刻接下一个**（第 174 步，b1）。
  //:
  //: 照 dsh-pet 的做法：它的 README 写"每个动画（含待机）播完即按权重选下一个"，中间没有停顿，
  //: 所以看起来"一直在动"。它们的待机呼吸是 10 秒一段，idle 权重 10，`首尾相接`不跳。
  //: 我们现在也有一条 10.13 秒的待机素材（`actions/idle.webm`，权重 6），
  //: 所以把间隔设为 0：呼吸 → 呼吸 → 擦擦灰 → 呼吸 …… 看上去就是连续活着。
  //: 想让它"演一次歇一会儿"就把这里改回 10000。
  const IDLE_GAP_MS = 0;

  /* ---------------------------------------------------- 动作调度（第 159 步）

   * 规则只有一条，但它解决了用户报的两个 bug：
   *
   * > **同一时刻只演一个动作，且手动点击永远优先。**
   *
   * 具体地：
   *
   * 1. 手动点菜单里的动作 → 先**取消待机计时**，再播；播放期间待机不会插进来。
   *    （以前待机是 `setInterval` 硬排的，所以在播「擦擦灰」的 8 秒里它照样会触发，
   *    把视频换成别的动作 —— 用户看到的就是"做一半被重置"。）
   * 2. 播放期间再点别的动作 → 允许打断（用户点了就该有反应），但旧的计时器要被清掉。
   * 3. 一个动作**自然播完**后才安排下一次待机（间隔 IDLE_GAP_MS）。
   *    视频用 `ended` 事件收尾（比 durationMs 准），其它素材用定时器兜底。
   *
   * 这和 dsh-pet 的做法是一致的：它把动画分成 idle / clicks / drag / events 几个池，
   * 同一时刻只播一个，非 idle 的动作结束后才回到 idle 池。
   */

  function clearActionTimers() {
    if (state.idleTimer) {
      window.clearTimeout(state.idleTimer);
      state.idleTimer = null;
    }
    if (state.actionTimer) {
      window.clearTimeout(state.actionTimer);
      state.actionTimer = null;
    }
    if (state.dragSwitchTimer) {
      window.clearTimeout(state.dragSwitchTimer);
      state.dragSwitchTimer = null;
    }
  }

  /** 一个动作结束：收起动作层，并把下一次待机排上。
   *
   *  `token` 是"这个回调属于哪个动作"。第 176 步的 bug 就在这儿：
   *  以前 `finishAction()` 无条件执行，而 `playAction` 排的兜底计时器（`durationMs + 400`）
   *  在 `ended` 先到的情况下**没有被清掉** —— 它到点后会把**下一个正在演的动作**掐断。
   *  用户看到的就是"待机做一半被擦擦灰打断、擦擦灰没做完又回待机"。
   */
  function finishAction(token) {
    if (token !== undefined && token !== state.actionToken) return;   // 上一任留下的回调
    if (!state.actionPlaying) return;
    state.actionPlaying = false;
    // 关键：**真清掉**兜底计时器，不只是把引用置空
    if (state.actionTimer) {
      window.clearTimeout(state.actionTimer);
      state.actionTimer = null;
    }
    if (el.pet) el.pet.classList.remove('is-idle-playing');
    if (el.petIdle && typeof el.petIdle.pause === 'function') {
      try {
        el.petIdle.pause();
      } catch (_error) {
        /* 暂停失败无所谓，下一次换 src 会覆盖 */
      }
    }
    state.actionTimer = null;
    scheduleNextIdle();
  }

  /** 排下一次待机（**只在没动作在演时**才会真的触发）。 */
  function scheduleNextIdle() {
    if (!el.pet || !el.petIdle) return;
    if (state.idleTimer) window.clearTimeout(state.idleTimer);
    state.idleTimer = window.setTimeout(() => {
      state.idleTimer = null;
      // 防自激：有些环境（DOM 桩、被 polyfill 掉的定时器）里 `setTimeout` 是**同步**的 ——
      // 回调里又排下一次，层层嵌套就是无限递归。浏览器里回调都是新任务，
      // 进来时深度永远是 0，所以这个上限不影响真实行为。
      if (state.scheduleDepth >= 2) return;
      state.scheduleDepth += 1;
      try {
        if (state.actionPlaying) return;    // 还在演别人的动作：跳过这一轮
        runRandomAction();
      } finally {
        state.scheduleDepth -= 1;
      }
    }, IDLE_GAP_MS);
  }

  /** 演一个动作。
   *
   *  `manual` = 用户点的（永远优先）；否则是待机挑的。
   *  两种素材格式：
   *  - `.webm`（VP9-alpha）：`<video>`，换 src + `currentTime = 0` + `play()`；
   *  - `.png`（APNG）：换 URL（`?cycle=N`）才会从头播。
   *  没有 `asset` 的动作跑它自己的 `run()`（给"暂时没有素材"的动作留的路）。
   */
  function playAction(action, options) {
    if (!action || !el.pet) return;
    const manual = Boolean(options && options.manual);

    // 手动点击先掐掉待机计时：否则它会在动作播到一半时抢戏
    clearActionTimers();
    if (manual) state.lastManualAt = Date.now();

    state.actionPlaying = true;
    state.lastActionId = action.id;
    state.actionToken += 1;                    // 作废上一任的回调（第 176 步）
    state.actionStartedAt = Date.now();        // 记开始时刻（第 177 步）
    el.pet.classList.add('is-idle-playing');
    // 演过多少个动作（第 175 步）。`IDLE_GAP_MS = 0` 之后"动作层收起"这个瞬间不存在了
    // （刚收起就接下一个），所以烟测改成看"这个计数在涨"，比读 class 稳。
    if (el.pet) {
      el.pet.dataset.actions = String(Number(el.pet.dataset.actions || 0) + 1);
    }

    if (action.asset && el.petIdle) {
      const isVideo = /\.webm$/i.test(action.asset);
      if (isVideo) {
        el.petIdle.src = action.asset;
        el.petIdle.currentTime = 0;
        if (typeof el.petIdle.play === 'function') {
          const started = el.petIdle.play();
          if (started && typeof started.catch === 'function') {
            started.catch(() => {});      // 自动播放被拦：静帧继续显示，不报错
          }
        }
      } else {
        state.idleCycle += 1;
        el.petIdle.src = `${action.asset}?cycle=${state.idleCycle}`;
      }
    } else if (typeof action.run === 'function') {
      action.run();
    }

    // 收尾：视频优先用 ended（真实时长），其余按 durationMs 兜底。
    // 兜底计时器也保证"ended 没来"（例如素材损坏）时不会永久锁住待机。
    const token = state.actionToken;
    state.actionTimer = window.setTimeout(
      () => finishAction(token),
      (action.durationMs || 2700) + 400
    );
  }

  /** 从后端取动作目录（第 172 步，c1）。
   *
   *  加载成功后**顶掉**写死的那份：`static/actions/<名字>.json` 里写什么就播什么，
   *  以后加动作只需丢一对 `名字.webm` + `名字.json`，不用改这个文件。
   *
   *  取不到（接口没起、404、json 坏了）就**继续用写死的那份** ——
   *  桌宠不能因为一个接口挂了就没动作。所以这里只 catch、不报错。
   */
  async function loadPetActions() {
    try {
      const response = await apiFetch(`${API}/pet/actions`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();

      const actions = Array.isArray(data.actions) ? data.actions : [];
      const usable = actions.filter(
        (action) => action && action.id && action.asset
      );
      if (usable.length) {
        PET_ACTIONS.splice(0, PET_ACTIONS.length, ...usable);
      }

      const drag = (data && data.drag) || {};
      const lift = drag['drag-lift'];
      if (lift && lift.asset) {
        DRAG_LIFT.asset = lift.asset;
        DRAG_LIFT.durationMs = Number(lift.durationMs) || DRAG_LIFT.durationMs;
      }
      const swing = drag['drag-swing'];
      if (swing && swing.asset) {
        DRAG_SWING.asset = swing.asset;
        DRAG_SWING.durationMs = Number(swing.durationMs) || DRAG_SWING.durationMs;
      }
    } catch (error) {
      console.warn('[桌宠] 动作目录拉取失败，用内置那份', error);
    }
  }

  /** 播「拎在手里来回荡」这一段（循环，一直播到手松开）。 */
  function playDragSwing() {
    if (!state.dragPose || !el.petIdle) return;
    el.petIdle.loop = true;
    el.petIdle.src = DRAG_SWING.asset;
    el.petIdle.currentTime = 0;
    if (typeof el.petIdle.play === 'function') {
      const started = el.petIdle.play();
      if (started && typeof started.catch === 'function') started.catch(() => {});
    }
  }

  /** 开始拖拽姿势（第 165 步）：
   *
   *  1. **强制接管**：正在演的其它动作立刻取消，而且**不记录、不恢复** ——
   *     "拖拽结束也不会继续播放没做完的动作"（用户明确要求）。
   *  2. 先播「被拎起来」那 3.2 秒（只播一次）；
   *  3. 它播完后（`ended`）自动切到「来回荡」并**循环**，这样长时间按着不会回地面。
   */
  /** 提起播完 -> 切到"来回荡"。`ended` 与兜底计时器都会调用，所以要做幂等判断。 */
  function switchDragToSwing() {
    if (!state.dragPose || state.dragPoseStage !== 'lift') return;
    state.dragPoseStage = 'swing';
    if (state.dragSwitchTimer) {
      window.clearTimeout(state.dragSwitchTimer);
      state.dragSwitchTimer = null;
    }
    playDragSwing();
  }

  function startDragPose() {
    if (!el.pet || !el.petIdle) return;

    // 取消一切在途的安排与正在演的动作（不恢复到拖拽结束）
    clearActionTimers();
    if (el.petIdle.pause) {
      try {
        el.petIdle.pause();
      } catch (_error) {
        /* 暂停失败无所谓：下面会换 src */
      }
    }

    state.actionPlaying = true;
    state.actionToken += 1;                    // 拖拽接管：作废之前动作的回调
    state.dragPose = true;
    state.dragPoseStage = 'lift';
    el.petIdle.loop = false;
    el.petIdle.src = DRAG_LIFT.asset;
    el.petIdle.currentTime = 0;
    if (typeof el.petIdle.play === 'function') {
      const started = el.petIdle.play();
      if (started && typeof started.catch === 'function') started.catch(() => {});
    }
    el.pet.classList.add('is-idle-playing');

    // 兜底：**不依赖 `ended` 一定会来**。
    // 无头浏览器（烟测用的就是）不会真的推进视频播放，`ended` 永远不触发 ——
    // 那样"提起完切到荡"就永远不会发生，长时间按住会停在第 3.2 秒的最后一帧。
    // 计时器到点同样切换；`switchDragToSwing()` 是幂等的，两者谁先到都一样。
    if (state.dragSwitchTimer) window.clearTimeout(state.dragSwitchTimer);
    state.dragSwitchTimer = window.setTimeout(
      switchDragToSwing,
      DRAG_LIFT.durationMs
    );
  }

  /** 松手：停掉拖拽姿势，回到静帧，并排下一次待机（**不恢复**被打断的动作）。 */
  function stopDragPose() {
    if (!state.dragPose) return;
    state.dragPose = false;
    state.dragPoseStage = 'idle';
    if (state.dragSwitchTimer) {
      window.clearTimeout(state.dragSwitchTimer);
      state.dragSwitchTimer = null;
    }
    if (el.petIdle) el.petIdle.loop = false;
    finishAction();
  }

  /** 待机时挑一个动作：**只在 idle 池里**、按 weight 加权，且不连着演同一个。 */
  function pickRandomAction() {
    const idlePool = PET_ACTIONS.filter(
      (action) => (action.pool || 'idle') === 'idle'
    );
    if (!idlePool.length) return null;

    const withoutLast = idlePool.filter((action) => action.id !== state.lastActionId);
    const candidates = withoutLast.length ? withoutLast : idlePool;

    const total = candidates.reduce(
      (sum, action) => sum + Math.max(0, Number(action.weight ?? 1)),
      0
    );
    if (total <= 0) return candidates[0];

    let roll = Math.random() * total;
    for (const action of candidates) {
      roll -= Math.max(0, Number(action.weight ?? 1));
      if (roll <= 0) return action;
    }
    return candidates[candidates.length - 1];
  }

  function runRandomAction() {
    const action = pickRandomAction();
    if (!action) return;
    playAction(action, { manual: false });
  }

  function startIdleLoop() {
    if (!el.pet || !el.petIdle) return;
    // 系统开了"减少动态效果"就不演：这不是装饰性动画，是持续动的东西
    const media = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
    if (media && media.matches) return;
    // 视频自然播完 -> 收尾（比定时器准）
    if (typeof el.petIdle.addEventListener === 'function') {
      el.petIdle.addEventListener('ended', () => {
        // 拖拽中：提起那段播完 -> 切到"来回荡"并循环（仍然拎着）
        if (state.dragPose) {
          switchDragToSwing();
          return;
        }
        // 第 177 步：丢掉"刚换 src 就冒出来的" ended。
        // 换 src / abort 时某些浏览器会补发一个 ended，它属于**上一段**素材；
        // 若照单全收，刚开演的动作会被瞬间结束（用户感觉"好多动作抢着触发"）。
        // 真实素材最短 1.2 秒，300ms 之内不可能播完。
        if (Date.now() - state.actionStartedAt < 300) return;
        finishAction(state.actionToken);
      });
    }
    scheduleNextIdle();          // 先静 10 秒，再随机演一个
  }

  function toggleMenu() {
    if (isMenuOpen()) closeMenu();
    else openMenu();
  }

  /** 轮盘内的方向键 / Home / End 导航。 */
  function onMenuKeydown(event) {
    const items = [...el.menuList.querySelectorAll('.wheel-item')];
    if (items.length === 0) return;
    const current = items.indexOf(document.activeElement);
    let next = null;

    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
      next = current < 0 ? 0 : (current + 1) % items.length;
    } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
      next = current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length;
    } else if (event.key === 'Home') {
      next = 0;
    } else if (event.key === 'End') {
      next = items.length - 1;
    }

    if (next !== null) {
      event.preventDefault();
      items[next].focus();
    }
  }

  /* ---------------------------------------------------------- 模型窗口
   *
   * 第 100 步：轮盘里的「模型」打开它。一家一个按钮，等于一组单选开关 ——
   * 后端只记一个 current，所以"点 GLM 就把 DeepSeek 关掉"这件事由数据本身保证，
   * 不需要前端去同步两个开关的状态。
   *
   * 用不了的那家会**列出来但禁用**，原因写在按钮里（比如 MiMo 缺接口地址）。
   */

  function setModelResult(text, ok) {
    if (!el.modelProbeResult) return;
    el.modelProbeResult.textContent = text;
    el.modelProbeResult.dataset.ok = String(Boolean(ok));
  }

  async function refreshModelPanel() {
    try {
      const response = await apiFetch(`${API}/model`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      state.model = await response.json();
    } catch (error) {
      setModelResult(`拿不到模型列表：${describeError(error)}`, false);
      return;
    }
    renderModelPanel();
    setModelResult('', false);
    void refreshUsageSummary();
  }

  /** 今日用量（第 111 步）：按功能分开显示 —— 只给一个总数看不出钱花在哪儿。 */
  async function refreshUsageSummary() {
    if (!el.modelUsage) return;
    try {
      const response = await apiFetch(`${API}/usage/summary?days=1`);
      if (!response.ok) return;
      const data = await response.json();
      const rows = (data && data.features) || [];
      const total = (data && data.total) || {};
      if (!rows.length) {
        el.modelUsage.textContent = '今天还没有用量记录';
        return;
      }
      const parts = rows.map(
        (row) => `${row.feature} ${row.total}`
      );
      // 评价6 第 5 条：缺 usage 的调用要说出次数，否则界面看起来像"花了 0"
      const missing = total.missing || 0;
      const tail = missing ? `，另有 ${missing} 次没有用量记录` : '';
      el.modelUsage.textContent =
        `今日：${parts.join(' · ')}（共 ${total.total || 0} token${tail}）`;
    } catch (_error) {
      el.modelUsage.textContent = '';
    }
  }

  function renderModelPanel() {
    if (!el.modelProviders) return;
    const data = state.model || { active: null, providers: [] };
    el.modelProviders.replaceChildren();

    (data.providers || []).forEach((provider) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'model-item';
      button.dataset.name = provider.name;
      const isActive = provider.name === data.active;
      button.dataset.active = String(isActive);
      button.setAttribute('aria-pressed', String(isActive));
      button.disabled = !provider.available;
      if (!provider.available) button.title = provider.reason;

      const name = document.createElement('span');
      name.className = 'model-name';
      name.textContent = provider.label;
      const meta = document.createElement('span');
      meta.className = 'model-meta';
      meta.textContent = provider.available ? provider.model : provider.reason;
      button.append(name, meta);

      button.addEventListener('click', () => { void chooseModel(provider.name); });
      el.modelProviders.append(button);
    });
  }

  async function chooseModel(name) {
    setModelResult('正在切换…', true);
    try {
      const response = await apiFetch(`${API}/model`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      const data = await response.json();
      if (!response.ok) {
        // 400 的 detail 是给人看的（"MiMo 现在用不了：…"），直接显示
        setModelResult(String(data.detail || `切换失败（HTTP ${response.status}）`), false);
        return;
      }
      state.model = data;
      renderModelPanel();
      const active = (data.providers || []).find((item) => item.name === data.active);
      setModelResult(`已切换到 ${active ? active.label : data.active}`, true);
    } catch (error) {
      setModelResult(describeError(error), false);
    }
  }

  /** 真正发一句最小请求确认这家能用 —— 只由用户点击触发（不自动探测，省额度）。 */
  async function probeModel() {
    setModelResult('正在试…', true);
    try {
      const response = await apiFetch(`${API}/model/probe`, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) {
        setModelResult(String(data.detail || `探测失败（HTTP ${response.status}）`), false);
        return;
      }
      setModelResult(data.message || (data.ok ? '可用' : '不可用'), Boolean(data.ok));
    } catch (error) {
      setModelResult(describeError(error), false);
    }
  }

  /* ------------------------------------------------------------ 对话面板 */

  function openChat(focusInput = false) {
    state.chatOpen = true;
    el.chat.hidden = false;
    updateChips();
    void restoreChatHistory();
    if (focusInput) el.input.focus();
  }

  /** 打开窗口时把之前的对话画回来（第 94 步 / S4）。
   *
   *  - 只做一次（`chatHistoryLoaded`），失败则下次打开重试；
   *  - **空会话不改动界面**：聊天本来就没有欢迎语，空着是正确的；
   *  - 恢复失败**不打扰用户**：照常可以聊，只是看不到旧记录。
   */
  async function restoreChatHistory() {
    // 已经取过 / 正在取：都直接返回。少了后半句，发送消息时会并发发第二次请求。
    if (state.chatHistoryLoaded || state.chatHistoryLoading) return;
    // 记下"此刻界面上有几条"，await 回来再比对 —— 变了说明用户已经动手了
    const beforeCount = el.chatLog.children.length;
    state.chatHistoryLoading = true;
    let data = null;
    try {
      const response = await apiFetch(
        `${API}/session/${encodeURIComponent(sessionId())}`
      );
      if (!response.ok) return;
      data = await response.json();
    } catch (_error) {
      return;                      // 网络/后端问题：不提示，下次再试
    } finally {
      state.chatHistoryLoading = false;   // 失败也要解锁，下次打开还能重试
    }
    const messages = (data && data.messages) || [];
    // 竞态（第 102 步）：这个 fetch 是异步的，期间用户可能已经发了一句、或者点了清空。
    // 以前无条件 `replaceChildren()`，会把用户刚发的那条**从界面上抹掉**。
    if (state.chatCleared) {
      // 用户刚清空：晚到的历史绝不能画回来（否则"清空了又冒出来"）
      state.chatHistoryLoaded = true;
      return;
    }
    if (messages.length) {
      const fragment = document.createDocumentFragment();
      messages.forEach((message) => {
        fragment.append(
          makeMessageNode(
            message.role === 'user' ? 'user' : 'bot',
            message.content || ''
          )
        );
      });
      if (el.chatLog.children.length === beforeCount) {
        // 期间没人动过：整块换上历史（正常路径）
        el.chatLog.replaceChildren(fragment);
      } else {
        // 期间用户已经发了消息：把历史**插到他上面**，别抹掉他的
        el.chatLog.prepend(fragment);
      }
      scrollToBottom();
    } else if (
      // 第 143 步：历史为空时显示开场白（由后端给，前端只画）。
      // 三个前提缺一不可：用户没在期间发过消息、没点清空、窗口里还是空的。
      data &&
      data.greeting &&
      el.chatLog.children.length === beforeCount &&
      !state.chatCleared
    ) {
      addMessage('bot', data.greeting);
    }
    state.chatHistoryLoaded = true;
  }

  function closeChat() {
    state.chatOpen = false;
    // 关窗 = 不看了：中断在途的流（服务端会在下次检查时停止，第 116 步）
    if (state.chatController) {
      state.chatController.abort();
      state.chatController = null;
    }
    el.chat.hidden = true;
    el.petButton.focus();
  }

  //: 「清空对话」确认框的 resolver（第 95 步）。
  //  它是**运行时状态**，不是 HTML 元素 —— 放进 `el` 会让启动自检把它当成
  //  「找不到的元素」并报警（第 108 步新增的行为验证抓到的）。
  let chatConfirmResolve = null;

  /** 在**聊天窗口内部**弹一句确认，返回 Promise<boolean>（第 95 步）。
   *
   *  刻意不用浏览器原生 confirm：它跟页面无关、把整个窗口遮住、还阻塞脚本
   *  —— 这和第 86 步签到补签时用户否掉原生框的理由一模一样。
   */
  function openChatConfirm(text) {
    return new Promise((resolve) => {
      if (!el.chatConfirm) {
        resolve(false);                  // 元素缺失时保守处理：当作取消
        return;
      }
      if (el.chatConfirmText) el.chatConfirmText.textContent = text;
      // 按钮文字也由 JS 填：静态 HTML 里留"清空/取消"会撞上
      // "常驻区域不许有文字"那条约束（第 3 步），而它们只在弹框时才有意义。
      if (el.chatConfirmOk) el.chatConfirmOk.textContent = '清空';
      if (el.chatConfirmCancel) el.chatConfirmCancel.textContent = '取消';
      el.chatConfirm.hidden = false;
      if (el.chatConfirmOk) el.chatConfirmOk.focus();
      chatConfirmResolve = resolve;
    });
  }

  /** 关掉确认框并回答。 */
  function answerChatConfirm(answer) {
    const resolve = chatConfirmResolve;
    chatConfirmResolve = null;
    if (el.chatConfirm) el.chatConfirm.hidden = true;
    if (resolve) resolve(Boolean(answer));
  }

  function isChatConfirmOpen() {
    return Boolean(el.chatConfirm && !el.chatConfirm.hidden);
  }

  /** 清空这段对话（第 95 步 / S5）。
   *
   *  为什么要它：历史现在落库了，没有出口就只增不减。
   *
   *  三条边界：
   *  - 只删**这个会话**的聊天记录（`scope=chat`）；长期忌口、菜谱库都不动；
   *  - **会话 id 不变** —— 换 id 等于"新建会话"，那是另一个功能，别混为一谈；
   *  - 先二次确认：删掉的聊天记录没有回收站。
   */
  async function clearChat() {
    if (!(await openChatConfirm('清空这段对话？清空后无法恢复。'))) return;
    // 第 145 步：**先中断在途的流**。
    // 不然旧回答会继续往刚清空的窗口里画（stillMine 只看 chatOpen，而清空后窗口还开着），
    // 而且服务端有机会在 DELETE 之后再把 assistant 消息写回去 —— "清空后无法恢复"就破了。
    if (state.chatController) {
      state.chatAborted = true;
      state.chatController.abort();
      state.chatController = null;
    }
    el.chatClear.disabled = true;
    try {
      const response = await apiFetch(
        `${API}/sessions/${encodeURIComponent(sessionId())}`,
        { method: 'DELETE' }
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      el.chatLog.replaceChildren();
      state.chatHistoryLoaded = true;      // 已经清空，不必再拉历史
      state.chatCleared = true;            // 晚到的历史响应不许再画回来
      addMessage('bot', '好的，这段对话清空啦。');
    } catch (error) {
      addMessage('bot', describeError(error));
    } finally {
      el.chatClear.disabled = false;
    }
  }

  function scrollToBottom() {
    el.chatLog.scrollTop = el.chatLog.scrollHeight;
  }

  /** 造一条消息节点（不插入）。恢复历史时要用它做 fragment。 */
  function makeMessageNode(kind, text) {
    const node = document.createElement('div');
    node.className = `msg msg-${kind}`;
    node.textContent = text;          // 一律用 textContent，杜绝 XSS
    return node;
  }

  function addMessage(kind, text) {
    const node = makeMessageNode(kind, text);
    el.chatLog.append(node);
    scrollToBottom();
    return node;
  }

  function showThinking() { el.bubble.hidden = false; }
  function hideThinking() { el.bubble.hidden = true; }

  function setSending(sending) {
    state.sending = sending;
    el.send.disabled = sending;
    el.input.disabled = sending;
  }

  /** 把各种失败翻译成一句人话。 */
  function describeError(error) {
    if (error && error.name === 'AbortError') return '等了太久没有响应，我先停下了。';
    if (error instanceof TypeError) return '连不上后端服务，确认它启动了吗？';
    return '出了点问题，稍后再试一次？';
  }

  /** 读 JSON；响应不是 JSON 时返回 {}，**不抛**。
   *
   *  反代返回 502 的 HTML、进程崩掉返回空体，这些 `response.json()` 都会抛 ——
   *  抛完就只剩一句通用网络文案，后端的 detail 反而丢了（评价12 P4-1）。
   */
  async function readJsonSafely(response) {
    try {
      return await response.json();
    } catch (_error) {
      return {};
    }
  }

  async function readDetail(response) {
    try {
      const body = await response.json();
      const detail = body && body.detail;
      if (typeof detail === 'string') return detail;
      if (Array.isArray(detail) && detail.length) return detail[0].msg || '请求有问题。';
    } catch (_) { /* 响应体不是 JSON，回落到默认文案 */ }
    return `服务返回了错误（HTTP ${response.status}）。`;
  }

  /* ------------------------------------------------------------ 发送消息 */

  /** 发一条消息，边收边显示（第 117 步：聊天也走 SSE）。
   *
   *  与食谱同一套协议：`delta` / `reset` / `error` / `meta`。
   *  收尾仍然以 `meta` 为准覆盖一次 —— delta 可能被截断，也可能中途重连。
   */
  async function sendMessage(text) {
    const message = String(text || '').trim();
    if (!message || state.sending) return;

    openChat();
    addMessage('user', message);
    setSending(true);
    showThinking();

    const controller = new AbortController();
    state.chatController = controller;
    const timer = window.setTimeout(() => controller.abort(), 90000);

    let bubble = null;
    let streamed = '';
    const paint = (value) => {
      if (!bubble) bubble = addMessage('pet', '');
      bubble.textContent = value;
      scrollToBottom();
    };

    try {
      const response = await apiFetch(`${API}/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId(), message }),
        signal: controller.signal,
      });

      if (!response.ok) {
        addMessage('error', await readDetail(response));
        return;
      }

      let meta = null;
      await streamSse(response, {
        // 关掉窗口就不再往里面画字（reader 也会被 cancel，服务端随之停止）
        stillMine: () => state.chatOpen,
        onActivity: hideThinking,
        onEvent: (event) => {
          if (!event) return;
          if (event.type === 'delta') {
            streamed += event.text || '';
            paint(streamed);
          } else if (event.type === 'reset') {
            // 这一轮要调工具：刚才流的字不是给用户看的答案
            streamed = '';
            if (bubble) paint('');
          } else if (event.type === 'error') {
            streamed = event.message || '';
            paint(streamed);
          } else if (event.type === 'meta') {
            meta = event.payload || null;
          }
        },
      });

      const finalText = (meta && meta.content) || streamed;
      if (finalText) paint(finalText);
      else if (!bubble) addMessage('pet', '（没有拿到回复内容）');
    } catch (error) {
      // abort 有两种来源，都不该冒出错误气泡：关窗、以及**清空对话**（第 145 步）
      if (error && error.name === 'AbortError' && (!state.chatOpen || state.chatAborted)) {
        state.chatAborted = false;
        return;
      }
      addMessage('error', describeError(error));
    } finally {
      window.clearTimeout(timer);
      state.chatController = null;
      hideThinking();
      setSending(false);
      el.input.focus();
    }
  }

  /* -------------------------------------------------------------- 拖拽 */

  /* 位置用 CSS 变量 --pet-x / --pet-y 表达，用像素而不是百分比：
     像素便于精确限制在屏幕内；未设置时 CSS 回落到 50%，即默认居中。 */
  const POS_KEY = 'pet.position';
  //: 位移超过这个像素数才算拖拽；否则视作点击。
  //: 阈值不能省：没有它的话手一抖 1~2px 就被判成拖拽，菜单会点不出来。
  const DRAG_THRESHOLD = 6;
  //: 拖拽结束后，只在这个时间窗内抑制 click。
  //: 用"时间窗"而不是"一个布尔标志等下次点击来消费"——
  //: 后者若因事件顺序没被消费掉，会把下一次普通点击也吞掉（点图标没反应）。
  const CLICK_SUPPRESS_MS = 300;
  //: 量不到尺寸时的兜底（与 style.css 的 --pet-w/--pet-h 默认值一致）
  const PET_FALLBACK_W = 104;
  const PET_FALLBACK_H = 150;

  function savePosition() {
    try {
      window.localStorage.setItem(
        POS_KEY, JSON.stringify({ x: state.dragX, y: state.dragY })
      );
    } catch (_) { /* 隐私模式等，忽略即可 */ }
  }

  function loadPosition() {
    try {
      const raw = window.localStorage.getItem(POS_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      const x = Number(parsed.x);
      const y = Number(parsed.y);
      if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
      return { x, y };
    } catch (_) {
      return null;
    }
  }

  /** 读一个 CSS 像素变量，带有效性校验。 */
  function readSizeVar(name, fallback) {
    const raw = window.getComputedStyle(document.documentElement)
      .getPropertyValue(name);
    const value = parseFloat(raw);
    return Number.isFinite(value) && value > 0 ? value : fallback;
  }

  /** 宠物尺寸（宽, 高）。
   *  刻意从 CSS 变量读，而不是用 getBoundingClientRect()：
   *  页面刚加载、布局未完成时后者会返回全 0，
   *  会让初始位置被记成 (0,0)，第一次拖拽就"飞向左上角"（已踩过）。 */
  function petSize() {
    return {
      w: readSizeVar('--pet-w', PET_FALLBACK_W),
      h: readSizeVar('--pet-h', PET_FALLBACK_H),
    };
  }

  /** 把中心点限制在视口内，保证宠物整体可见（含自身宽高）。
   *  纯函数，只依赖入参，便于单独验证。 */
  function clampToBounds(x, y, w, h, vw, vh) {
    // 尺寸非法时退回兜底，避免算出 [0, vw] 这种退化的约束区间，
    // 否则宠物会被拖到只露一角。
    const width = Number.isFinite(w) && w > 0 ? w : PET_FALLBACK_W;
    const height = Number.isFinite(h) && h > 0 ? h : PET_FALLBACK_H;
    const viewportW = Number.isFinite(vw) && vw > 0 ? vw : width;
    const viewportH = Number.isFinite(vh) && vh > 0 ? vh : height;

    const halfW = width / 2;
    const halfH = height / 2;
    // 视口比宠物还小时，max < min，用 max(half, ...) 让中心点至少留在半个宠物处
    const maxX = Math.max(halfW, viewportW - halfW);
    const maxY = Math.max(halfH, viewportH - halfH);
    return {
      x: Math.min(Math.max(x, halfW), maxX),
      y: Math.min(Math.max(y, halfH), maxY),
    };
  }

  /* ---------------------------------------------------- 抛掷（第 171 步）

   *  参考 dsh-pet：松手时按**拖动速度**给一个初速度，靠摩擦慢慢停下，碰到屏幕边缘反弹。
   *
   *  速度用"最近 120 毫秒的指针轨迹"估，不用两次 pointermove 的瞬时差 ——
   *  后者一次抖动或一帧卡顿就能给出荒谬的速度（宠物瞬间飞出屏幕）。
   */
  const THROW_TRAIL_MS = 120;
  const THROW_MIN_SPEED = 0.35;     // px/ms：低于这个就当"轻轻放下"，不抛
  const THROW_FRICTION = 0.92;      // 每帧速度衰减
  const THROW_BOUNCE = 0.55;        // 撞边后保留多少速度
  const THROW_FRAME_MS = 16;
  const THROW_MAX_FRAMES = 150;     // 兜底：最多跑 2.4 秒，参数不对也不会一直飞

  function recordPointer(x, y) {
    const now = Date.now();
    state.pointerTrail.push({ t: now, x, y });
    while (state.pointerTrail.length > 2) {
      const oldest = state.pointerTrail[0];
      if (now - oldest.t <= THROW_TRAIL_MS) break;
      state.pointerTrail.shift();
    }
  }

  /** 松手瞬间的速度（px/ms）。轨迹不足两点就是 0。 */
  function releaseSpeed() {
    const trail = state.pointerTrail;
    if (trail.length < 2) return { vx: 0, vy: 0 };
    const first = trail[0];
    const last = trail[trail.length - 1];
    const dt = Math.max(1, last.t - first.t);      // 同一毫秒内也算 1ms，避免除零
    return { vx: (last.x - first.x) / dt, vy: (last.y - first.y) / dt };
  }

  function stopThrow() {
    state.throwing = false;
  }

  function startThrow(vx, vy) {
    // 留个可观测标记：DOM 桩里 CSS 变量是空实现，读不到位置，只能靠它验"抛了"
    if (el.pet) {
      el.pet.dataset.threw = String(Number(el.pet.dataset.threw || 0) + 1);
    }
    state.throwing = true;
    state.throwVX = vx;
    state.throwVY = vy;
    let frames = 0;

    const step = () => {
      if (!state.throwing) return;                 // 用户又按住了：立刻停
      frames += 1;
      const speed = Math.hypot(state.throwVX, state.throwVY);
      if (speed < 0.02 || frames > THROW_MAX_FRAMES) {
        state.throwing = false;
        savePosition();
        return;
      }

      const { w, h } = petSize();
      const halfW = w / 2;
      const halfH = h / 2;
      const maxX = Math.max(halfW, window.innerWidth - halfW);
      const maxY = Math.max(halfH, window.innerHeight - halfH);

      let nextX = state.dragX + state.throwVX * THROW_FRAME_MS;
      let nextY = state.dragY + state.throwVY * THROW_FRAME_MS;

      if (nextX < halfW) {
        nextX = halfW;
        state.throwVX = -state.throwVX * THROW_BOUNCE;
      } else if (nextX > maxX) {
        nextX = maxX;
        state.throwVX = -state.throwVX * THROW_BOUNCE;
      }
      if (nextY < halfH) {
        nextY = halfH;
        state.throwVY = -state.throwVY * THROW_BOUNCE;
      } else if (nextY > maxY) {
        nextY = maxY;
        state.throwVY = -state.throwVY * THROW_BOUNCE;
      }

      applyPosition(nextX, nextY, false);
      state.throwVX *= THROW_FRICTION;
      state.throwVY *= THROW_FRICTION;
      window.setTimeout(step, THROW_FRAME_MS);
    };

    step();
  }

  function clampPosition(x, y) {
    const { w, h } = petSize();
    return clampToBounds(x, y, w, h, window.innerWidth, window.innerHeight);
  }

  function applyPosition(x, y, persist) {
    state.dragX = x;
    state.dragY = y;
    document.documentElement.style.setProperty('--pet-x', `${x}px`);
    document.documentElement.style.setProperty('--pet-y', `${y}px`);
    if (persist) savePosition();
  }

  /** 当前宠物中心点。没记录过就按视口正中算（与 CSS 的 50% 回落一致）。 */
  function centerOfViewport() {
    return { x: window.innerWidth / 2, y: window.innerHeight / 2 };
  }

  function onPetPointerDown(event) {
    // 只处理主键（鼠标左键 / 触摸），右键不参与
    if (event.button !== undefined && event.button !== 0) return;

    // 每次按下都重新对齐"当前真实位置"，这样即使中途出现过异常也不会累积偏差
    if (state.dragX === 0 && state.dragY === 0) {
      const fallback = centerOfViewport();
      state.dragX = fallback.x;
      state.dragY = fallback.y;
    }

    stopThrow();                       // 抛掷中又被按住：立刻接管（第 171 步）
    state.dragging = true;
    state.pointerTrail = [];
    state.movedBeyondThreshold = false;
    state.startX = event.clientX;
    state.startY = event.clientY;
    recordPointer(event.clientX, event.clientY);
    state.originX = state.dragX;
    state.originY = state.dragY;

    // 捕获指针：拖出按钮范围甚至拖出窗口也能继续收到事件
    try { el.petButton.setPointerCapture(event.pointerId); } catch (_) { /* 忽略 */ }
    el.pet.classList.add('is-dragging');
  }

  function onPetPointerMove(event) {
    if (!state.dragging) return;

    const dx = event.clientX - state.startX;
    const dy = event.clientY - state.startY;

    if (!state.movedBeyondThreshold) {
      if (Math.hypot(dx, dy) < DRAG_THRESHOLD) return;  // 还在"点击"的手抖范围内
      state.movedBeyondThreshold = true;
      closeMenu();   // 开始拖了就先把菜单收起来，避免跟着飘
      startDragPose();   // 真拖起来了：切到"被拎着"的循环姿势（第 163 步）
      recordPointer(event.clientX, event.clientY);
    }

    recordPointer(event.clientX, event.clientY);
    const next = clampPosition(state.originX + dx, state.originY + dy);
    applyPosition(next.x, next.y, false);
  }

  function onPetPointerUp(event) {
    if (!state.dragging) return;
    state.dragging = false;
    el.pet.classList.remove('is-dragging');
    try { el.petButton.releasePointerCapture(event.pointerId); } catch (_) { /* 忽略 */ }

    if (state.movedBeyondThreshold) {
      // 记录拖拽结束时刻：紧随其后（极短时间内）的 click 才需要抑制
      state.dragEndedAt = Date.now();
      const { vx, vy } = releaseSpeed();
      stopDragPose();    // 松手：回到静帧，排下一次待机（第 163 步）
      if (Math.hypot(vx, vy) >= THROW_MIN_SPEED) {
        startThrow(vx, vy);       // 甩出去：按速度飞一段再停（第 171 步）
      } else {
        savePosition();           // 轻轻放下：停在这儿
      }
    }
  }

  /** 这次 click 是否属于"刚拖完的余波"。 */
  function shouldSuppressClick() {
    if (!state.dragEndedAt) return false;
    const elapsed = Date.now() - state.dragEndedAt;
    return elapsed >= 0 && elapsed < CLICK_SUPPRESS_MS;
  }

  function restorePosition() {
    const saved = loadPosition();
    if (!saved) {
      // 没存过位置：CSS 里的 50% 回落已经让它居中，
      // 这里只需把"中心点"记成视口正中，供拖拽起点使用。
      // 不用 getBoundingClientRect —— 布局未完成时它会给全 0。
      const center = centerOfViewport();
      state.dragX = center.x;
      state.dragY = center.y;
      return;
    }
    // 存过位置：先约束一次再应用，避免旧数据或缩小窗口后把宠物放到屏幕外
    const next = clampPosition(saved.x, saved.y);
    applyPosition(next.x, next.y, next.x !== saved.x || next.y !== saved.y);
  }

  /* ------------------------------------------------------ 邮箱窗口（第 33 步）
   *
   * 登入前（「添加邮箱」）与登入后（收件箱）是**同一个窗口里的两页**，
   * 只切 hidden、不新开窗口（原文第 4 条）。
   *
   * 邮件全部来自后端的真实 IMAP 收信，前端只负责摆版式：
   *   GET  /api/mail/providers -> { providers: [{key,label,host,port,help_text,domains}], default }
   *   POST /api/mail/login     -> { address, provider, provider_label, host }
   *   POST /api/mail/logout    -> { status, session_id }
   *   GET  /api/mail/status    -> { logged_in, account }
   *   GET  /api/mail/folders   -> { folders: [{key,label,mailbox,unread,total}] }
   *   GET  /api/mail/messages  -> { folder, messages: [...] }
   *   POST /api/mail/flag      -> { uid, flagged }
   * 失败一律是非 2xx + { detail: "给用户看的中文" }，前端直接把 detail 显示出来。
   *
   * 服务商名、服务器地址、帮助文案全部来自后端——前端不硬编码任何一家，
   * 以后加服务商只要动 core/mailbox.py 的 PROVIDERS。
   */

  //: 一次取回多少封。滚到列表底部会自动再取下一页，
  //: 所以这个数只决定"一次拉多少"，不再决定"最多能看到多少"。
  //: 搜索只在**已加载的那批**里筛（README 里记了原因：IMAP 的 SEARCH CHARSET
  //: 各家支持不一，中文关键词容易踩坑）。
  const MAIL_PAGE_SIZE = 50;

  //: 切回一个**已经缓存过**的文件夹时，后台"查有没有新邮件"的节流（第 36 步）。
  //: 真实文件夹的查新只要一次 STATUS（不下载邮件），所以可以查得勤；
  //: 「星标邮件」是跨文件夹搜出来的虚拟文件夹，没有 UIDNEXT 可用、只能整份重算，
  //: 所以要压得更久，免得来回点的时候反复跨文件夹搜。
  const MAIL_CHECK_MIN_GAP_MS = 5000;
  const MAIL_CHECK_THROTTLE_MS = 30000;

  //: 搜索框防抖（第 61 步）。以前每敲一个字符就全量重建列表：
  //: 加载了 500 封时，每次按键要重建约 3000 个 DOM 节点，输入明显卡顿。
  //: 120ms 是"打字时感觉不到延迟、又能把连续按键合并成一次"的经验值。
  const MAIL_SEARCH_DEBOUNCE_MS = 120;

  const MAIL_WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

  /* ------------------------------------------------ 桌宠报新邮件（第 198 步 / 评价15 B6）
     **为什么是 5 分钟**：查新一次只发一个 STATUS（见上面 MAIL_CHECK_* 的注释），代价很小，
     但它是"后台自动跑"的动作 —— 短于 5 分钟就是在替用户决定"你要多频繁地想起有邮箱"。
     分钟级的延迟对"有新邮件"这件事完全够用；真想马上看到，点开机箱本来就会立刻查。
     **为什么不是邮件推送**：IMAP IDLE 要长连接 + 服务端支持，QQ/网易都时有时无，
     为一个提醒把它们全踩一遍不值得（DECISIONS 第 198 步记了这条取舍）。 */
  const MAIL_WATCH_INTERVAL_MS = 5 * 60 * 1000;

  /*: 静默时段 22:00–08:00（用户定的规则 1）：这段时间**只记账、不冒泡**。
     夜里来的邮件记进 pending，第二天 8 点后第一次轮询时合并成一句说出来，
     所以"漏说"不会发生、"半夜弹窗"也不会发生。 */
  const MAIL_QUIET_FROM = 22;
  const MAIL_QUIET_TO = 8;

  /*: 每封只说一次（规则 2）——已提醒过的 UID 存在本地。
     为什么不用服务端记：提醒是**这台机器上的这张脸**的行为，换台机器再说一次不算错；
     存本地也让"清掉浏览器数据"成为一个可用的复位手段。 */
  const MAIL_LAST_UID_KEY = 'pet.mail.last_uid';
  const MAIL_NOTIFIED_KEY = 'pet.mail.notified';
  const MAIL_PENDING_KEY = 'pet.mail.pending';

  //: 气泡自己消失的时间。够看清一行字，又不至于一直挂着挡桌宠。
  const PET_NOTE_MS = 15000;

  //: 没有真头像时给发件人画一个色块，颜色由地址哈希决定（同一个人恒定同色）
  const MAIL_AVATAR_COLORS = [
    '#4f7cf7', '#e07a5f', '#3d9970', '#b07cc6',
    '#c9803a', '#5b8def', '#d1495b', '#2a9d8f',
  ];

  function showMailPage(page) {
    state.mailPage = page;
    const inbox = page === 'inbox';
    if (el.mailPageLogin) el.mailPageLogin.hidden = inbox;
    if (el.mailPageInbox) el.mailPageInbox.hidden = !inbox;
  }

  function setMailError(message) {
    if (!el.mailError) return;
    el.mailError.textContent = message || '';
    el.mailError.hidden = !message;
  }

  /** 列表区的提示。
   *
   *  overlay=true（默认）用于"整块区域没有内容"的时刻：正在收取 / 这个文件夹是空的 /
   *  读取失败——这时把列表收起来，提示居中说一句话。
   *  overlay=false 用于"列表还在，只是顺带说一句"（例如星标没保存成功），
   *  提示变成顶上一条细横幅，**不能因为一次星标失败就把整个列表藏掉**。
   */
  function setMailState(message, overlay = true) {
    if (!el.mailState) return;
    el.mailState.textContent = message || '';
    el.mailState.hidden = !message;
    el.mailState.classList.toggle('is-centered', Boolean(message) && overlay);
    if (el.mailList) el.mailList.hidden = Boolean(message) && overlay;
  }

  async function mailErrorText(response) {
    let detail = '';
    try {
      const data = await response.json();
      detail = data && data.detail ? String(data.detail) : '';
    } catch (_) { /* 不是 JSON 就算了，下面给通用文案 */ }
    if (detail) return detail;
    if (response.status === 401) return '登入状态已失效，请重新添加邮箱。';
    return `HTTP ${response.status}`;
  }

  /** 401 = 后端那个会话没了（服务重启会清空内存）。
   *  这时退回登入页并说明原因，而不是留一个点不动的收件箱。 */
  function handleMailUnauthorized(response) {
    if (response.status !== 401) return false;
    state.mailAccount = '';
    state.mailCache = {};
    showMailPage('login');
    setMailState('');
    setMailError('登入状态已失效（服务重启会清空），请重新添加邮箱。');
    return true;
  }

  /* ---------------------------------------------------------- 服务商 */

  function currentMailProvider() {
    return state.mailProviders.find((p) => p.key === state.mailProvider) || null;
  }

  /** 按域名认服务商。认不出返回 null——**不要瞎猜**，让用户看到"暂不支持"。 */
  function providerForAddress(address) {
    const at = String(address || '').indexOf('@');
    if (at < 0) return null;
    const domain = String(address).slice(at + 1).trim().toLowerCase();
    if (!domain) return null;
    return state.mailProviders.find(
      (p) => Array.isArray(p.domains) && p.domains.includes(domain)
    ) || null;
  }

  function updateMailProviderHint() {
    if (!el.mailProviderHint) return;
    const typed = el.mailAddress ? el.mailAddress.value.trim() : '';

    if (!typed) {
      const picked = currentMailProvider();
      el.mailProviderHint.textContent = picked
        ? `邮箱服务：${picked.label}（${picked.host}）`
        : '';
      return;
    }
    const matched = providerForAddress(typed);
    if (matched) {
      el.mailProviderHint.textContent = `已识别：${matched.label} · ${matched.host}`;
      return;
    }
    el.mailProviderHint.textContent = typed.includes('@')
      ? '目前只能登入 QQ 邮箱（qq.com / foxmail.com）。'
      : '';
  }

  function renderMailServices() {
    if (!el.mailServices) return;
    el.mailServices.replaceChildren();

    if (!state.mailProviders.length) {
      const note = document.createElement('p');
      note.className = 'mail-service-help';
      note.textContent = '服务商清单没能取到，请关掉窗口再打开一次。';
      el.mailServices.append(note);
      return;
    }

    state.mailProviders.forEach((provider) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'mail-service';
      button.dataset.key = provider.key;
      if (provider.key === state.mailProvider) button.classList.add('is-active');

      const label = document.createElement('span');
      label.textContent = provider.label;

      const host = document.createElement('span');
      host.className = 'mail-service-host';
      host.textContent = provider.host;

      button.append(label, host);
      button.addEventListener('click', () => {
        state.mailProvider = provider.key;
        renderMailServices();
        updateMailProviderHint();
      });
      el.mailServices.append(button);
    });

    const active = currentMailProvider();
    if (active && active.help_text) {
      const help = document.createElement('p');
      help.className = 'mail-service-help';
      help.textContent = active.help_text;
      el.mailServices.append(help);
    }
  }

  function toggleMailServices() {
    if (!el.mailServices) return;
    const open = el.mailServices.hidden;
    el.mailServices.hidden = !open;
    if (el.mailServiceToggle) {
      el.mailServiceToggle.setAttribute('aria-expanded', String(open));
    }
  }

  async function loadMailProviders() {
    try {
      const response = await apiFetch(`${API}/mail/providers`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      state.mailProviders = Array.isArray(data.providers) ? data.providers : [];
      state.mailDefaultProvider = data.default || 'qq';
    } catch (_) {
      state.mailProviders = [];
      state.mailDefaultProvider = 'qq';
    }
    state.mailProvider = state.mailProviders.some((p) => p.key === state.mailProvider)
      ? state.mailProvider
      : state.mailDefaultProvider;
    renderMailServices();
    updateMailProviderHint();
  }

  /* ---------------------------------------------------------- 登入/退出 */

  function setMailBusy(busy) {
    state.mailBusy = busy;
    if (!el.mailSubmit) return;
    el.mailSubmit.disabled = busy;
    el.mailSubmit.textContent = busy ? '正在连接…' : '添加';
  }

  function renderMailAccount() {
    if (el.mailAccount) el.mailAccount.textContent = state.mailAccount || '';
  }

  async function onMailSubmit(event) {
    if (event) event.preventDefault();
    if (state.mailBusy) return;

    const address = el.mailAddress ? el.mailAddress.value.trim() : '';
    const secret = el.mailSecret ? el.mailSecret.value : '';

    setMailError('');
    if (!address) { setMailError('请填写邮箱账号'); return; }
    if (!address.includes('@')) {
      setMailError('邮箱账号要写完整，例如 12345@qq.com');
      return;
    }
    if (!secret.trim()) {
      setMailError('请填写授权码（不是 QQ 密码）');
      return;
    }

    const matched = providerForAddress(address);
    setMailBusy(true);
    try {
      const response = await apiFetch(`${API}/mail/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId(),
          address,
          secret,
          provider: matched ? matched.key : state.mailProvider,
        }),
      });
      if (!response.ok) {
        setMailError(await mailErrorText(response));
        return;
      }
      const data = await response.json().catch(() => ({}));
      // 授权码用完了就清掉，别留在 DOM 里
      if (el.mailSecret) el.mailSecret.value = '';
      state.mailAccount = data.address || address;
      state.mailFolder = 'inbox';
      state.mailCache = {};          // 换了账号，上一份缓存全作废
      showMailPage('inbox');
      setMailError('');
      renderMailAccount();
      await loadMailFolders();
    } catch (error) {
      setMailError(describeError(error));
    } finally {
      setMailBusy(false);
    }
  }

  async function logoutMail() {
    setMailError('');
    try {
      await apiFetch(`${API}/mail/logout`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId() }),
      });
    } catch (_) {
      // 网络失败也照样退回登入页：内存里的会话由后端按容量上限回收
    }
    state.mailAccount = '';
    state.mailFolders = [];
    state.mailCache = {};           // 退出就把缓存全丢掉，别让下一个账号看到
    state.mailFolder = 'inbox';
    state.mailHint = '';
    // 退出登入就把后台查新停掉（第 198 步）：否则这个计时器还会拿旧 session 接着问，
    // 白白失败，还把上一轮的游标留在本地
    state.mailWatch.armed = false;
    if (state.mailWatch.timer && typeof window.clearInterval === 'function') {
      window.clearInterval(state.mailWatch.timer);
      state.mailWatch.timer = 0;
    }
    hidePetNote();
    cancelMailSearch();
    if (el.mailFolders) el.mailFolders.replaceChildren();
    if (el.mailList) el.mailList.replaceChildren();
    if (el.mailSearch) el.mailSearch.value = '';
    setMailState('');
    renderMailAccount();
    showMailPage('login');
  }

  /* ---------------------------------------- 后台查新 → 桌宠气泡（第 198 步 / 评价15 B6） */

  function watchStoreGet(key, fallback) {
    try {
      const raw = window.localStorage.getItem(key);
      return raw === null ? fallback : raw;
    } catch (_) {
      return fallback;          // 隐私模式读不到：提醒退化成"只在本次会话里生效"
    }
  }

  function watchStoreSet(key, value) {
    try { window.localStorage.setItem(key, String(value)); } catch (_) { /* 忽略 */ }
  }

  function mailLastUid() {
    const value = Number(watchStoreGet(MAIL_LAST_UID_KEY, 0));
    return Number.isFinite(value) && value > 0 ? value : 0;
  }

  function mailNotifiedUids() {
    try {
      const list = JSON.parse(watchStoreGet(MAIL_NOTIFIED_KEY, '[]'));
      return Array.isArray(list)
        ? list.map(Number).filter((uid) => Number.isFinite(uid))
        : [];
    } catch (_) {
      return [];                // 存坏了就当没记过，最多多提醒一次
    }
  }

  function rememberNotified(uids) {
    const merged = mailNotifiedUids().concat(uids.map(Number));
    // 只留最近 300 个：够覆盖"用户还没删的邮件"，又不至于把 localStorage 撑大
    watchStoreSet(MAIL_NOTIFIED_KEY, JSON.stringify(merged.slice(-300)));
  }

  function isQuietHour(now = mailNow()) {
    const hour = now.getHours();
    return MAIL_QUIET_FROM > MAIL_QUIET_TO
      ? (hour >= MAIL_QUIET_FROM || hour < MAIL_QUIET_TO)
      : (hour >= MAIL_QUIET_FROM && hour < MAIL_QUIET_TO);
  }

  /** 现在几点。
   *  单独抽出来只为一个理由：DOM 桩里没有可控的 `Date`，"静默时段"这条规则
   *  如果直接 `new Date()` 就没法被测到 —— 测出来的结果取决于跑测试的时刻，
   *  凌晨跑绿、白天跑红。桩可以塞 `window.__petMailNow` 指一个固定时刻。 */
  function mailNow() {
    const hook = window.__petMailNow;
    return typeof hook === 'function' ? new Date(hook()) : new Date();
  }

  function pendingMailCount() {
    return Math.max(0, Math.floor(Number(watchStoreGet(MAIL_PENDING_KEY, 0)) || 0));
  }

  function setPendingMailCount(count) {
    watchStoreSet(MAIL_PENDING_KEY, Math.max(0, Math.floor(count)));
  }

  function startMailWatch() {
    if (state.mailWatch.armed) return;
    // 先立旗再挂定时器：DOM 桩里的 setInterval 是**立即同步执行一次**的，
    // 如果等拿到句柄再算"已启动"，那一轮回调会看到 timer===0 直接返回（真的踩过）。
    state.mailWatch.armed = true;
    state.mailWatch.timer = window.setInterval(
      () => { void pollMailOnce(); },
      MAIL_WATCH_INTERVAL_MS
    );
    void pollMailOnce();      // 登入后马上问一次，省得用户等满 5 分钟
  }

  function hidePetNote() {
    if (state.petNoteTimer && typeof window.clearTimeout === 'function') {
      window.clearTimeout(state.petNoteTimer);
    }
    state.petNoteTimer = 0;
    state.petNoteOpen = null;
    if (el.petNote) el.petNote.hidden = true;
  }

  function showPetNote(text, onOpen) {
    if (!el.petNote || !el.petNoteText) return;
    el.petNoteText.textContent = text;
    el.petNote.title = `${text}\n点一下去看`;
    el.petNote.setAttribute('aria-label', `有新邮件：${text}，点一下去看`);
    el.petNote.hidden = false;
    // 点击行为存起来，由 bindEvents 里那一个常驻监听器统一调用
    // （常驻监听器内部会 stopPropagation：气泡在 .pet 里面，别顺手把轮盘菜单一起触发）
    state.petNoteOpen = onOpen;
    if (state.petNoteTimer && typeof window.clearTimeout === 'function') {
      window.clearTimeout(state.petNoteTimer);
    }
    // 桩里 setTimeout 是立即执行的，气泡刚显示就会被"自动收起"抹掉，于是
    // "显示着的时候点它"这条路根本测不到。`__petNoteSticky` 让桩可以选择不自动收。
    if (!window.__petNoteSticky) {
      state.petNoteTimer = window.setTimeout(hidePetNote, PET_NOTE_MS);
    }
  }

  function mailFromText(message) {
    return message.from_name || message.from_address || '（未知发件人）';
  }

  /** 一行字里只说一件事：一封就说发件人 + 主题，多封就只说数量。 */
  function announceMail(messages) {
    const newest = messages.reduce(
      (best, item) => (Number(item.uid) > Number(best.uid) ? item : best),
      messages[0]
    );
    const text = messages.length === 1
      ? `新邮件：${mailFromText(newest)} · ${shortMailSubject(newest)}`
      : `来了 ${messages.length} 封新邮件`;
    showPetNote(text, () => { void openMailAt(Number(newest.uid)); });
  }

  function shortMailSubject(message) {
    const subject = (message.subject || '（无主题）').trim();
    return subject.length > 16 ? `${subject.slice(0, 16)}…` : subject;
  }

  /** 后台查新这一轮停在哪一步。
   *  只为一件事：`pollMailOnce` 抛错是故意吞掉的（后台动作不打扰用户），吞掉之后
   *  界面上"没冒泡"这一个现象对应五六种原因（没登入 / 没新邮件 / 都提醒过了 / 静默 /
   *  真的抛了），从外面全靠猜。CI 上就猜错过一次：同一份代码本机绿、runner 上红，
   *  除了"没冒泡"什么都拿不到。这里把原因留在一个变量上，桩读它打进诊断行。 */
  function mailDiag(note) {
    window.__petMailDiag = String(note);
  }

  async function pollMailOnce() {
    const watch = state.mailWatch;
    if (watch.busy) return;
    mailDiag('开始 busy=false');

    // 静默时段攒下的先处理：过了 8 点，第一轮就把它说出来，然后清零
    const pending = pendingMailCount();
    if (pending > 0) {
      if (isQuietHour()) { mailDiag(`有 ${pending} 封攒着，但还在静默时段`); return; }
      setPendingMailCount(0);
      showPetNote(`夜里来了 ${pending} 封新邮件`, () => { void openMailAt(0); });
    }

    // 没启动（没登入 / 已退出）的时候后台查新必须是哑的：不弹气泡、不动界面、不报错
    if (!state.mailWatch.armed) { mailDiag('看新没启动（没登入或已退出）'); return; }

    watch.busy = true;
    try {
      const status = await apiFetch(
        `${API}/mail/status?session_id=${encodeURIComponent(sessionId())}`
      );
      if (!status.ok) { mailDiag(`status 不是 2xx：${status.status}`); return; }
      const info = await status.json();
      if (!info.logged_in) { mailDiag('status 说没登入'); return; }

      const since = mailLastUid();
      // `watch=1` 是给这一条请求做的标记（第 198 步）：邮箱窗口自己也会打 /mail/check
      // （见 checkForNewMail），两条路径的游标语义不同（窗口按 cache.newestUid，
      // 轮询按 localStorage 里的 last_uid）。服务端日志和行为测试都要能把
      // 「后台轮询」和「窗口合并」分开，否则只能靠调用顺序猜（真的踩过）。
      const response = await apiFetch(
        `${API}/mail/check?session_id=${encodeURIComponent(sessionId())}`
        + `&folder=inbox&since_uid=${since}&watch=1`
      );
      if (!response.ok) { mailDiag(`check 不是 2xx：${response.status}`); return; }
      const data = await response.json();
      const fresh = (Array.isArray(data.messages) ? data.messages : [])
        .filter((message) => Number(message.uid) > since);
      if (!fresh.length) {
        const got = Array.isArray(data.messages) ? data.messages.length : '不是数组';
        mailDiag(`没有 since=${since} 之后的新邮件（收到 ${got} 封）`);
        return;
      }

      // 游标先往前走：不管这轮冒不冒泡，同一封都不该被再处理一次
      const newest = Math.max(...fresh.map((message) => Number(message.uid)));
      watchStoreSet(MAIL_LAST_UID_KEY, newest);

      const notified = new Set(mailNotifiedUids());
      const unseen = fresh.filter((message) => !notified.has(Number(message.uid)));
      if (!unseen.length) { mailDiag('这几封之前提醒过了'); return; }
      rememberNotified(unseen.map((message) => message.uid));

      if (isQuietHour()) {
        setPendingMailCount(pendingMailCount() + unseen.length);   // 规则 1：只记账
        mailDiag(`静默时段：记了 ${unseen.length} 封的账`);
        return;
      }
      mailDiag(`冒泡：${unseen.length} 封`);
      announceMail(unseen);
    } catch (error) {
      // 断网 / 服务重启 / 登入失效 —— 后台动作一律不打扰用户。
      // 但原因要留痕，不然下次又只能靠"没冒泡"这一个现象猜。
      mailDiag(`抛错：${error && error.message ? error.message : error}`);
    } finally {
      watch.busy = false;
    }
  }

  /** 点气泡：开邮箱，并尽量把那一封指出来（规则 3）。 */
  async function openMailAt(uid) {
    // 不在这里再调一次 prepareMail：openDialog('mail') 内部已经 void 了一个，
    // 再调一次就是两遍 status + folders 请求，还会互相覆盖界面状态
    openDialog('mail');
    if (state.mailPrepare && typeof state.mailPrepare.then === 'function') {
      await state.mailPrepare;                   // 等那次准备走完（含第一页列表）
    }
    if (state.mailPage !== 'inbox') return;      // 没登入：停在登入页，不硬切
    if (state.mailFolder !== 'inbox') selectMailFolder('inbox');
    await flashMailRow(Number(uid));
  }

  /** 高亮 + 滚到某一行。**找不到就明说**：列表是分页的，那封可能根本不在这一页。 */
  async function flashMailRow(uid) {
    if (!el.mailList || !el.mailList.children || !uid) return;
    // 按 children 走而不是 querySelectorAll：一是和本文件里其它"就地改行"的写法一致
    // （见 repaintMailStars），二是 DOM 桩的 querySelectorAll 只支持极窄的语义
    const find = () => {
      const rows = el.mailList.children || [];
      for (let i = 0; i < rows.length; i += 1) {
        const row = rows[i];
        if (row && row.dataset && Number(row.dataset.uid) === uid) return row;
      }
      return null;
    };

    let row = find();
    for (let attempt = 0; attempt < 8 && !row; attempt += 1) {
      // 列表可能正在加载或正在重渲染，给几轮机会（最多约两秒）
      await new Promise((resolve) => window.setTimeout(resolve, 250));
      row = find();
    }
    if (!row) {
      // 存进 state 里再显示：列表还会因为"后台查新合并"重渲染一次，那会把状态行清掉，
      // 于是这句话会闪一下就没（真踩过）。renderMailList 每次重画时会把 hint 补回去。
      state.mailHint = '那封新邮件不在这页（可能已经翻到后面了），往下翻翻看。';
      setMailState(state.mailHint, false);      // overlay=false：别把列表藏起来
      return;
    }
    if (row.classList && typeof row.classList.add === 'function') {
      row.classList.add('is-flash');
    }
    if (typeof row.scrollIntoView === 'function') {
      row.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
    window.setTimeout(() => {
      if (row.classList && typeof row.classList.remove === 'function') {
        row.classList.remove('is-flash');
      }
    }, 2600);
  }

  /** 打开窗口时问一句后端：这个会话还登着吗？ */
  async function prepareMail() {
    setMailError('');
    setMailState('');
    if (el.mailSecret) el.mailSecret.value = '';
    // 已经知道登着哪个账号就别把界面闪回登入页，只刷新一下列表
    if (!state.mailAccount) showMailPage('login');

    await loadMailProviders();

    try {
      const response = await apiFetch(
        `${API}/mail/status?session_id=${encodeURIComponent(sessionId())}`
      );
      if (!response.ok) return;
      const data = await response.json();
      if (!data.logged_in) {
        if (!state.mailAccount) showMailPage('login');
        return;
      }
      state.mailAccount = (data.account && data.account.address) || state.mailAccount;
      showMailPage('inbox');
      renderMailAccount();
      await loadMailFolders();
      // 登入确认之后才让后台查新转起来（第 198 步）：没登入时它必须是哑的
      startMailWatch();
    } catch (_) {
      // 问不到就停在登入页，不打扰用户
    }
  }

  /* ---------------------------------------------------------- 文件夹与列表 */

  function renderMailFolders() {
    if (!el.mailFolders) return;
    el.mailFolders.replaceChildren();

    state.mailFolders.forEach((folder) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'mail-folder';
      button.dataset.key = folder.key;
      if (folder.key === state.mailFolder) button.classList.add('is-active');

      const label = document.createElement('span');
      label.textContent = folder.label;
      button.append(label);

      // 图 1 只在收件箱、草稿箱上带数字，其余不带——未读为 0 就不显示
      const unread = Number(folder.unread) || 0;
      if (unread > 0) {
        const badge = document.createElement('span');
        badge.className = 'mail-folder-count';
        badge.textContent = String(unread);
        button.append(badge);
      }

      button.addEventListener('click', () => selectMailFolder(folder.key));
      el.mailFolders.append(button);
    });
  }

  /** 取某个文件夹的缓存槽（没有就建一个空的）。 */
  function mailCacheFor(key) {
    let cache = state.mailCache[key];
    if (!cache) {
      cache = {
        messages: [],
        offset: 0,
        hasMore: false,
        total: 0,
        newestUid: 0,
        loaded: false,      // 有没有成功加载过第一页
        loading: false,     // 正在请求（挡住重入）
        token: 0,           // 自增；响应回来时对不上就说明它过期了
        checkedAt: 0,       // 上次静默查新的时刻（虚拟文件夹要限流）
        // 每次 success 写入 messages 就 +1。后台查新在 await 前记下它，
        // 回来后发现变了就丢弃这次合并 —— 否则会拿旧口径改新列表，
        // 把 offset 算错、翻页时重复或跳段（第 61 步）。
        revision: 0,
      };
      state.mailCache[key] = cache;
    }
    return cache;
  }

  function mailNewestUid(messages) {
    let newest = 0;
    messages.forEach((message) => {
      const uid = Number(message.uid);
      if (Number.isFinite(uid) && uid > newest) newest = uid;
    });
    return newest;
  }

  function selectMailFolder(key) {
    state.mailFolder = key;
    state.mailHint = '';                 // 换文件夹：上一条"找不到那封"的提示就作废了
    cancelMailSearch();                  // 换文件夹时上一次没触发的搜索重排作废
    if (el.mailSearch) el.mailSearch.value = '';
    renderMailFolders();

    const cache = mailCacheFor(key);
    if (cache.loaded) {
      // 有缓存：**立刻**渲染出来（不再闪"正在收取邮件…"），
      // 之后只在后台默默问一句有没有新邮件（第 36 步）。
      // 换了文件夹，滚动位置自然该回到顶部。
      renderMailListSafely('top');
      void checkForNewMail(key);
      return;
    }
    loadMailMessages();
  }

  async function loadMailFolders() {
    try {
      const response = await apiFetch(
        `${API}/mail/folders?session_id=${encodeURIComponent(sessionId())}`
      );
      if (handleMailUnauthorized(response)) return;
      if (!response.ok) throw new Error(await mailErrorText(response));
      const data = await response.json();
      state.mailFolders = Array.isArray(data.folders) ? data.folders : [];
    } catch (error) {
      state.mailFolders = [];
      renderMailFolders();
      setMailState(`文件夹没能读出来：${error.message}`);
      return;
    }

    // 当前选中的文件夹这次没出现（例如草稿箱被删了）就退回第一个
    if (!state.mailFolders.some((f) => f.key === state.mailFolder)) {
      state.mailFolder = state.mailFolders.length ? state.mailFolders[0].key : 'inbox';
    }
    renderMailFolders();
    await loadMailMessages();
  }

  /** 取一页邮件，写进**发起请求时那个文件夹**的缓存。
   *
   *  reset=true：这个文件夹第一次进来，从第一页开始（会清空它的缓存）。
   *  reset=false：滚到底时的"加载下一页"，接在这个文件夹已有的列表后面。
   *
   *  这里有三个防串台的措施（第 36 步修的问题 2）：
   *   1. `folder` 在**发请求前**就固定下来，响应回来时写回**那个**缓存，
   *      而不是写什么共享的"当前列表"；
   *   2. 每个缓存有自己的 `token`，同一文件夹的重复请求只有最后一次算数；
   *   3. 只有"我现在还在看这个文件夹"时才渲染。
   */
  async function loadMailMessages(reset = true) {
    const folder = state.mailFolder;
    const cache = mailCacheFor(folder);
    if (cache.loading) return;            // 滚得快时会连着触发，挡住重入
    cache.loading = true;
    cache.token += 1;
    const token = cache.token;

    const offset = reset ? 0 : cache.offset;
    if (reset) {
      cache.messages = [];
      cache.offset = 0;
      cache.hasMore = false;
      cache.total = 0;
      if (folder === state.mailFolder) setMailState('正在收取邮件…');
    } else if (folder === state.mailFolder) {
      renderMailListSafely('keep');       // 让底部的"正在加载更多…"先显示出来
    }

    let rows = [];
    let failed = null;
    try {
      const response = await apiFetch(
        `${API}/mail/messages?session_id=${encodeURIComponent(sessionId())}`
        + `&folder=${encodeURIComponent(folder)}`
        + `&limit=${MAIL_PAGE_SIZE}&offset=${offset}`
      );
      if (handleMailUnauthorized(response)) {
        cache.loading = false;
        return;
      }
      if (!response.ok) throw new Error(await mailErrorText(response));
      const data = await response.json();
      rows = Array.isArray(data.messages) ? data.messages : [];
      cache.total = Number(data.total) || 0;
      cache.hasMore = Boolean(data.has_more);
    } catch (error) {
      failed = error;
    }

    if (token !== cache.token) return;    // 已经有更新的请求了，这份结果作废
    cache.loading = false;

    if (failed) {
      if (reset) {
        cache.messages = [];
        cache.hasMore = false;
        if (folder === state.mailFolder) setMailState(`邮件没能取回来：${failed.message}`);
        return;
      }
      // 追加失败：已经拿到的列表不动，只说明后面这批没取到，并停止自动加载，
      // 否则滚动会一直重试同一个失败请求。
      cache.hasMore = false;
      if (folder === state.mailFolder) {
        renderMailListSafely('keep');
        setMailState(`后面这批没能取回来：${failed.message}`, false);
      }
      return;
    }

    cache.messages = reset ? rows : [...cache.messages, ...rows];
    cache.offset = offset + rows.length;
    cache.newestUid = mailNewestUid(cache.messages);
    // 列表内容真的变了：告诉"在途的后台查新"，它那份口径已经作废
    cache.revision += 1;
    if (reset) cache.loaded = true;

    // 只有还在看这个文件夹才动界面；否则数据已经安全落进它自己的缓存了。
    // 首屏/换文件夹回到顶部，翻页则保持用户当前看到的位置。
    if (folder === state.mailFolder) {
      renderMailListSafely(reset ? 'top' : 'keep');
    }
  }

  /** 后台静默查新：**先拿缓存渲染，再问一句有没有新邮件**。
   *
   *  没有新邮件时后端只发一次 STATUS，**一封都不下载**，这里也就什么都不做；
   *  有新邮件才把它们插到列表最前面，已经加载的旧邮件一封都不重新下。
   *
   *  「星标邮件」没有 UIDNEXT 这种全局序号可用（它是跨文件夹搜出来的），
   *  只能整份重算，所以要限流，免得来回点的时候反复跨文件夹搜。
   */
  async function checkForNewMail(folder) {
    const cache = mailCacheFor(folder);
    if (cache.loading) return;

    const now = Date.now();
    const isVirtual = folder === 'flagged';
    const gap = isVirtual ? MAIL_CHECK_THROTTLE_MS : MAIL_CHECK_MIN_GAP_MS;
    if (now - cache.checkedAt < gap) return;
    cache.checkedAt = now;
    // 记下"我出发时列表是什么样"。await 期间列表可能已经被整页换过或追加过，
    // 那种情况下这次合并的口径（尤其 offset）就不对了，必须丢弃（第 61 步）。
    const revision = cache.revision;

    let data = null;
    try {
      const response = await apiFetch(
        `${API}/mail/check?session_id=${encodeURIComponent(sessionId())}`
        + `&folder=${encodeURIComponent(folder)}`
        + `&since_uid=${cache.newestUid}`
      );
      if (handleMailUnauthorized(response)) return;
      if (!response.ok) return;           // 查新失败不打扰用户：缓存还在，列表照常
      data = await response.json();
    } catch (_) {
      return;                             // 网络抖一下不该把已有列表搞坏
    }

    if (cache.revision !== revision) {
      // 期间列表已经变过：这次合并作废。**不丢数据**——新邮件下次查新还会拿到，
      // 所以把 checkedAt 归零，让下一次切回来能立刻重试。
      cache.checkedAt = 0;
      return;
    }

    const rows = Array.isArray(data.messages) ? data.messages : [];
    const total = Number(data.total);
    if (Number.isFinite(total)) cache.total = total;

    if (data.reset) {
      // 虚拟文件夹：整份替换第一页
      cache.messages = rows;
      cache.offset = rows.length;
      cache.newestUid = mailNewestUid(rows);
      cache.revision += 1;
    } else if (rows.length) {
      const known = new Set(cache.messages.map((message) => message.uid));
      const fresh = rows.filter((message) => !known.has(message.uid));
      if (fresh.length) {
        cache.messages = [...fresh, ...cache.messages];
        cache.offset += fresh.length;
        cache.newestUid = mailNewestUid(cache.messages);
        cache.revision += 1;
      }
    }

    // 未读数跟着服务端走：左侧数字要能自己更新
    const unread = Number(data.unread);
    if (Number.isFinite(unread)) {
      const row = state.mailFolders.find((item) => item.key === folder);
      if (row && row.unread !== unread) {
        row.unread = unread;
        renderMailFolders();
      }
    }

    // 查新不该把用户的滚动位置/焦点弄丢
    if (folder === state.mailFolder) renderMailListSafely('keep');
  }

  /** 搜索框输入：防抖后再重排，别每敲一个字符就重建整张列表。 */
  function onMailSearchInput() {
    if (state.mailSearchTimer) {
      window.clearTimeout(state.mailSearchTimer);
      state.mailSearchTimer = 0;
    }
    state.mailSearchTimer = window.setTimeout(() => {
      state.mailSearchTimer = 0;
      // 换了搜索词就是换了一组结果，回到顶部才符合预期
      renderMailListSafely('top');
    }, MAIL_SEARCH_DEBOUNCE_MS);
  }

  /** 取消还没触发的搜索重排（换文件夹 / 退出 / 关窗口时用）。 */
  function cancelMailSearch() {
    if (!state.mailSearchTimer) return;
    window.clearTimeout(state.mailSearchTimer);
    state.mailSearchTimer = 0;
  }

  /** 列表滚到接近底部时自动取下页。不需要按钮，也就不新增参考图之外的控件。 */
  function onMailListScroll() {
    const cache = mailCacheFor(state.mailFolder);
    if (cache.loading || !cache.hasMore) return;
    if (!el.mailList) return;
    const list = el.mailList;
    const nearBottom =
      list.scrollTop + list.clientHeight >= list.scrollHeight - 60;
    if (nearBottom) loadMailMessages(false);
  }

  /** 把邮件按"今天 / 昨天 / 星期X / 上周 / 更早"分组（图 1 的分组标题）。
   *  进来的顺序已经是"最新在前"，所以分组自然就是有序的。 */
  function mailGroupLabel(iso) {
    const when = iso ? new Date(iso) : null;
    if (!when || Number.isNaN(when.getTime())) return '时间未知';

    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const that = new Date(when.getFullYear(), when.getMonth(), when.getDate());
    // 用 round 而不是 floor：夏令时下两地相隔可能是 23/25 小时
    const days = Math.round((today - that) / 86400000);

    if (days <= 0) return '今天';
    if (days === 1) return '昨天';
    if (days < 7) return MAIL_WEEKDAYS[when.getDay()];
    if (days < 14) return '上周';
    return '更早';
  }

  function mailTimeText(iso) {
    const when = iso ? new Date(iso) : null;
    if (!when || Number.isNaN(when.getTime())) return '';
    const now = new Date();
    if (when.getFullYear() !== now.getFullYear()) {
      return `${when.getMonth() + 1}/${when.getDate()}`;
    }
    // 今天的只显示时间，更早的显示月/日 —— 与图 1 的 "20:49 / 10/5" 一致
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    if (when >= today) {
      const two = (n) => String(n).padStart(2, '0');
      return `${two(when.getHours())}:${two(when.getMinutes())}`;
    }
    return `${when.getMonth() + 1}/${when.getDate()}`;
  }

  function mailMatches(message, query) {
    return [
      message.from_name, message.from_address, message.subject, message.snippet,
    ].join(' ').toLowerCase().includes(query);
  }

  function mailAvatarColor(seed) {
    const text = String(seed || '');
    let hash = 0;
    for (let i = 0; i < text.length; i += 1) {
      hash = (hash * 31 + text.charCodeAt(i)) % 100000;
    }
    return MAIL_AVATAR_COLORS[hash % MAIL_AVATAR_COLORS.length];
  }

  function mailAvatarGlyph(name) {
    const text = String(name || '').trim();
    return text ? text.slice(0, 1).toUpperCase() : '?';
  }

  /* ------------------------------------------------ 重排列表但别弄丢用户的位置 */

  /** 找到一行里的星标按钮。只用 `children` + `classList`，
   *  这样浏览器和 DOM 桩里的行为一致（桩没有完整的 querySelector）。 */
  function findMailStar(row) {
    if (!row || !row.children) return null;
    for (let i = 0; i < row.children.length; i += 1) {
      const child = row.children[i];
      if (child && child.classList && child.classList.contains('mail-star')) return child;
    }
    return null;
  }

  /** 记下"现在焦点在列表里的哪个东西上"，供重建后还原。 */
  function describeMailFocus() {
    if (!el.mailList || typeof el.mailList.contains !== 'function') return null;
    const active = document.activeElement;
    if (!active || !el.mailList.contains(active)) return null;
    const row = typeof active.closest === 'function'
      ? active.closest('.mail-item')
      : null;
    if (!row || !row.dataset) return null;
    return {
      uid: row.dataset.uid || '',
      onStar: Boolean(active.classList && active.classList.contains('mail-star')),
    };
  }

  /** 重建后把焦点放回原来那一行的同一个位置。 */
  function restoreMailFocus(remembered) {
    if (!remembered || !remembered.uid || !el.mailList || !el.mailList.children) return;
    let row = null;
    for (let i = 0; i < el.mailList.children.length; i += 1) {
      const child = el.mailList.children[i];
      if (child && child.dataset && child.dataset.uid === remembered.uid) {
        row = child;
        break;
      }
    }
    if (!row) return;                       // 那一行没了（被搜索过滤掉等），不强求
    const target = remembered.onStar ? findMailStar(row) : row;
    if (target && typeof target.focus === 'function') target.focus();
  }

  /** 重排列表，并且**别把滚动位置和键盘焦点弄丢**（第 61 步）。
   *
   *  以前所有调用点都直接 `renderMailList()`，而它是 `replaceChildren()` 全量重建：
   *  清空滚动容器会把 `scrollTop` 归零、重建后不会自动恢复；全量重建还会销毁 DOM，
   *  焦点回到 `<body>`。于是"滚到下面点个星标 → 视图跳回顶部"，
   *  键盘用户按一次星标就失去位置。
   *
   *  place：
   *    'keep' —— 保持滚动位置与焦点（查新 / 翻页 / 星标）
   *    'top'  —— 回到顶部并放开焦点（换文件夹 / 换搜索词：这时保持旧位置反而是错的）
   */
  function renderMailListSafely(place = 'keep') {
    if (!el.mailList) {
      renderMailList();
      return;
    }
    if (place === 'top') {
      renderMailList();
      el.mailList.scrollTop = 0;
      return;
    }
    const scrollTop = el.mailList.scrollTop;
    const remembered = describeMailFocus();
    renderMailList();
    el.mailList.scrollTop = scrollTop;
    restoreMailFocus(remembered);
  }

  function renderMailList() {
    if (!el.mailList) return;
    el.mailList.replaceChildren();

    // 渲染的**永远是当前文件夹自己的那份缓存**
    const cache = mailCacheFor(state.mailFolder);
    const query = (el.mailSearch ? el.mailSearch.value : '').trim().toLowerCase();
    const rows = query
      ? cache.messages.filter((message) => mailMatches(message, query))
      : cache.messages;

    if (!rows.length) {
      if (query) {
        setMailState(`已加载的 ${cache.messages.length} 封里，没有匹配「${query}」的邮件。`);
      } else if (cache.total > 0) {
        // "服务器说有 N 封，却一封都没解析出来" = 解析出问题了，不是文件夹空。
        // 第 35 步就是这么栽的：界面只说"还没有邮件"，把真正的 bug 藏了整整一轮。
        setMailState(
          `这个文件夹有 ${cache.total} 封邮件，但一封都没能读出来。`
          + '这是解析出了问题，不是文件夹为空，请把后端的终端日志发给我。'
        );
      } else {
        setMailState('这个文件夹还没有邮件。');
      }
      return;
    }
    // 上一次"找不到那封邮件"的提示要留着：列表重画不该把它抹掉
    setMailState(state.mailHint, false);

    // 先分组再渲染：标题上要带条数（"今天 (2封)"），边走边渲染拿不到总数
    const groups = [];
    const index = new Map();
    rows.forEach((message) => {
      const label = mailGroupLabel(message.received);
      let bucket = index.get(label);
      if (!bucket) {
        bucket = { label, rows: [] };
        index.set(label, bucket);
        groups.push(bucket);
      }
      bucket.rows.push(message);
    });

    groups.forEach((bucket) => {
      const heading = document.createElement('p');
      heading.className = 'mail-group';
      heading.textContent = `${bucket.label} (${bucket.rows.length}封)`;
      el.mailList.append(heading);
      bucket.rows.forEach((message) => el.mailList.append(mailRow(message)));
    });

    // 列表末尾那句：正在加载 / 已经到底。
    // 没有它的话，滚到底不动会让人分不清"到底了"还是"卡住了"（第 32 步的教训）。
    // 搜索时不显示——搜索结果和"有没有下一页"不是一回事。
    if (!query) {
      const note = document.createElement('p');
      note.className = 'mail-more';
      if (cache.loading) note.textContent = '正在加载更多…';
      else if (cache.hasMore) note.textContent = '继续往下滚动，加载更早的邮件';
      else note.textContent = '已经到底了';
      el.mailList.append(note);
    }
  }

  /** 一行邮件。**整行不可点**——本轮明确不做正文页（README 第 33 步），
   *  行内唯一能点的是右边的星标，所以没有把整行做成按钮。 */
  function mailRow(message) {
    const row = document.createElement('div');
    row.className = 'mail-item';
    row.dataset.uid = message.uid;
    if (message.unread) row.classList.add('is-unread');

    const avatar = document.createElement('span');
    avatar.className = 'mail-avatar';
    avatar.setAttribute('aria-hidden', 'true');
    avatar.textContent = mailAvatarGlyph(message.from_name);
    avatar.style.background = mailAvatarColor(message.from_address || message.from_name);

    const from = document.createElement('span');
    from.className = 'mail-from';
    from.textContent = message.from_name || '（未知发件人）';
    from.title = message.from_address || '';

    const subject = document.createElement('span');
    subject.className = 'mail-subject';
    subject.textContent = message.subject || '（无主题）';

    const snippet = document.createElement('span');
    snippet.className = 'mail-snippet';
    snippet.textContent = message.snippet || '';

    const time = document.createElement('span');
    time.className = 'mail-time';
    time.textContent = mailTimeText(message.received);

    const starred = Boolean(message.flagged);
    const star = document.createElement('button');
    star.type = 'button';
    star.className = starred ? 'mail-star is-on' : 'mail-star';
    star.textContent = starred ? '★' : '☆';
    star.title = starred ? '取消星标' : '加星标';
    star.setAttribute('aria-label', `${star.title}：${message.subject || ''}`);
    star.setAttribute('aria-pressed', String(starred));
    star.addEventListener('click', () => toggleMailStar(message));

    row.append(avatar, from, subject, snippet, time, star);
    return row;
  }

  /** 星标的笔画只改**那一个按钮**，不重建列表。
   *
   *  重建会 `replaceChildren()` 掉所有节点：滚动位置被清空、焦点回 `<body>`，
   *  用户点个星标就被弹回顶部（第 61 步修的就是这个）。原地改元素既没有这个问题，
   *  也便宜得多。返回是否真的找到了按钮（没找到说明这一行不在视野内的列表里）。
   */
  function paintStarButton(star, starred) {
    if (!star) return false;
    star.classList.toggle('is-on', starred);
    star.textContent = starred ? '★' : '☆';
    star.title = starred ? '取消星标' : '加星标';
    star.setAttribute('aria-pressed', String(starred));
    return true;
  }

  /** 把当前列表里所有指向这封邮件的星标按钮就地重画。 */
  function repaintMailStars(message) {
    if (!el.mailList || !el.mailList.children) return false;
    let touched = false;
    for (let i = 0; i < el.mailList.children.length; i += 1) {
      const row = el.mailList.children[i];
      if (!row || !row.dataset || row.dataset.uid !== message.uid) continue;
      touched = paintStarButton(findMailStar(row), Boolean(message.flagged)) || touched;
    }
    return touched;
  }

  async function toggleMailStar(message) {
    const next = !message.flagged;
    try {
      const response = await apiFetch(`${API}/mail/flag`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId(),
          mailbox: message.mailbox,
          uid: message.uid,
          flagged: next,
        }),
      });
      if (handleMailUnauthorized(response)) return;
      if (!response.ok) throw new Error(await mailErrorText(response));
    } catch (error) {
      // 列表还在，只在顶上顺带说一句，不把整个列表藏掉
      setMailState(`星标没能保存：${error.message}`, false);
      return;
    }

    // 星标状态要同步到**所有**缓存里（同一封邮件可能在收件箱和星标邮件里各有一份）
    Object.values(state.mailCache).forEach((cache) => {
      cache.messages.forEach((row) => {
        if (row.uid === message.uid && row.mailbox === message.mailbox) {
          row.flagged = next;
        }
      });
    });

    // 在「星标邮件」里取消星标，这一封就该从那个列表里消失。
    // 这是唯一需要"整行消失"的情况，只能重排——但仍然保持滚动位置。
    if (!next) {
      const flagged = state.mailCache.flagged;
      if (flagged && flagged.messages.some((row) => row.uid === message.uid)) {
        flagged.messages = flagged.messages.filter((row) => row.uid !== message.uid);
        flagged.offset = Math.max(0, flagged.offset - 1);
        flagged.newestUid = mailNewestUid(flagged.messages);
        flagged.revision += 1;
        if (state.mailFolder === 'flagged') {
          renderMailListSafely('keep');
          return;
        }
      }
    }

    // 普通情况：只重画星标，**列表不动** → 滚动位置和焦点都不会丢
    repaintMailStars(message);
  }

  /* ------------------------------------------------------------ 事件绑定 */

  function onComposerSubmit(event) {
    event.preventDefault();
    const text = el.input.value;
    el.input.value = '';
    updateChips();          // 输入框空了，快捷意图可以重新露出来
    sendMessage(text);
  }

  function onGlobalKeydown(event) {
    if (event.key !== 'Escape') return;
    // 优先级：最上层的浮层先收，一次 Esc 只关掉一个。
    // 补签确认框排在**最前面**：它就在签到窗口里头，Esc 应该先收它
    // （等于点了"取消"），而不是把整个签到窗口一起关掉。
    if (isCheckinConfirmOpen()) answerCheckinConfirm(false);
    // 清空确认同理：先收它（等于点了取消），别一下把聊天窗口关掉
    else if (isChatConfirmOpen()) answerChatConfirm(false);
    // 菜谱详情浮层盖在食谱窗口内部，Esc 先收它，别一下就关掉整个窗口
    else if (el.recipeDetail && !el.recipeDetail.hidden) closeRecipeDetail();
    else if (state.actionMenuOpen) closeActionMenu();
    else if (isMenuOpen()) closeMenu(true);
    else if (state.chatOpen) closeChat();
    else if (state.openDialogKind) closeDialog();
  }

  /** 安全地给元素绑事件。
   *
   *  为什么必须有这个：曾经因为 HTML 里少了一个 <button>，
   *  `bindEvents()` 里对 null 元素直接加了监听、抛 TypeError，
   *  于是它之后的所有绑定都没执行（点不动），
   *  连 init() 里后续的 restorePosition()/loadTools() 也一起没跑。
   *  **一个元素缺失连带弄坏三个功能**——单个元素问题绝不该让整页瘫痪。
   */
  function on(target, type, handler) {
    if (!target) return;          // 元素不存在就跳过，不影响其它绑定
    target.addEventListener(type, handler);
  }

  /** 启动自检：把取值为 null 的元素名列出来，便于一眼发现 HTML 缺了什么。 */
  function warnMissingElements() {
    const missing = Object.keys(el).filter((key) => !el[key]);
    if (missing.length) {
      console.warn(
        '[桌宠] 以下元素在 HTML 里找不到，相关功能会不可用：' + missing.join(', ')
      );
    }
    return missing;
  }

  function bindEvents() {
    // 桌宠气泡（第 198 步）：用 addEventListener 而不是 onclick —— 监听器一次挂好、
    // 行为由 state.petNoteOpen 决定，收起时把那个引用清掉就行（不用反复增删监听）。
    if (el.petNote) {
      on(el.petNote, 'click', (event) => {
        if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
        const open = state.petNoteOpen;
        hidePetNote();
        if (typeof open === 'function') open();
      });
    }
    on(el.petButton, 'click', () => {
      // 刚拖完的那一下 click 要忽略；但用"时间窗"判断而非"一次性标志"，
      // 否则标志一旦没被消费掉，下一次普通点击也会被吞（点图标没反应）。
      if (shouldSuppressClick()) return;
      // 保险：此刻若仍在拖拽、或本次手势已超过阈值，一律不开菜单。
      // 即"静止点击才打开菜单"。
      if (state.dragging || state.movedBeyondThreshold) return;
      toggleMenu();
    });
    on(el.petButton, 'pointerdown', onPetPointerDown);
    on(el.petButton, 'pointermove', onPetPointerMove);
    on(el.petButton, 'pointerup', onPetPointerUp);
    on(el.petButton, 'pointercancel', onPetPointerUp);
    // 窗口缩放后把宠物拉回屏幕内
    window.addEventListener('resize', () => {
      const next = clampPosition(state.dragX, state.dragY);
      applyPosition(next.x, next.y, true);
    });

    on(el.menu, 'keydown', onMenuKeydown);
    on(el.chatClose, 'click', closeChat);
    on(el.chatClear, 'click', clearChat);
    on(el.modelClose, 'click', closeDialog);
    on(el.modelProbe, 'click', () => { void probeModel(); });
    on(el.chatConfirmOk, 'click', () => answerChatConfirm(true));
    on(el.chatConfirmCancel, 'click', () => answerChatConfirm(false));
    on(el.composer, 'submit', onComposerSubmit);
    // 用户一开始打字就收起快捷意图，避免和输入内容抢注意力（第 23 步）
    on(el.input, 'input', updateChips);

    // 「出行」窗口（第 24 步）：由胶囊打开，同样支持 × 与点遮罩关闭
    on(el.travelClose, 'click', closeDialog);
    on(el.travelModal, 'click', (event) => {
      if (event.target === el.travelModal) closeDialog();
    });

    // 出行表单（第 25 步）：日期变化自动算天数；提交后交给模型生成
    on(el.travelStart, 'change', updateTravelDays);
    on(el.travelEnd, 'change', updateTravelDays);
    on(el.travelForm, 'submit', onTravelSubmit);
    // 第 197 步（评价15 B2）：上次行程提示条的两个按钮
    on(el.travelResumeOpen, 'click', resumeTrip);
    on(el.travelResumeDrop, 'click', dropResumeTrip);
    on(el.travelTabChat, 'click', () => showTravelPage('chat'));
    on(el.travelTabForm, 'click', () => showTravelPage('form'));
    on(el.travelChatForm, 'submit', onTravelChatSubmit);
    on(el.travelTabPlanLabel, 'click', () => showTravelPage('plan'));
    on(el.travelTabPlanClose, 'click', closePlanTab);
    // 点概览页的「第x天」→ 打开（或聚焦）那一天的标签页（第 28/30 步）
    on(el.dayList, 'click', (event) => {
      const item = event.target && event.target.closest
        ? event.target.closest('.day-item')
        : null;
      if (item && item.dataset && item.dataset.day) {
        openDayTab(Number(item.dataset.day));
      }
    });

    // 地图（第 224 步）：展开/收起，以及上面那排「全部 / 第N天」按钮
    on(el.travelMapToggle, 'click', toggleTravelMap);
    on(el.travelMapDays, 'click', (event) => {
      const button = event.target && event.target.closest
        ? event.target.closest('.ov-map-day')
        : null;
      if (!button) return;
      const raw = button.dataset ? button.dataset.day : '';
      focusTravelMap(raw ? Number(raw) : null);
    });

    // 「邮箱」窗口（第 33 步）：胶囊打开、× 与点遮罩关闭；窗口内是"登入页 / 收件箱"两页
    on(el.mailClose, 'click', closeDialog);
    on(el.mailModal, 'click', (event) => {
      if (event.target === el.mailModal) closeDialog();
    });
    on(el.mailForm, 'submit', onMailSubmit);
    // 边打字边认服务商，用户不用点「选择邮箱服务」也知道认成了哪家
    on(el.mailAddress, 'input', updateMailProviderHint);
    on(el.mailServiceToggle, 'click', toggleMailServices);
    on(el.mailSearch, 'input', onMailSearchInput);
    on(el.mailList, 'scroll', onMailListScroll);
    on(el.mailLogout, 'click', logoutMail);

    // 「食谱」窗口（第 37 步起；第 48 步改成首页 + 对话页）：胶囊打开、
    // × 与点遮罩关闭。首页里点卡片看详情、点底部入口进对话，
    // 对话页左上角的 ← 回到首页。
    on(el.recipeClose, 'click', closeDialog);
    on(el.recipeModal, 'click', (event) => {
      if (event.target === el.recipeModal) closeDialog();
    });
    on(el.recipeComposer, 'submit', onRecipeSubmit);
    bindRecipePhotoInput();
    on(el.recipeChatEntry, 'click', () => showRecipeView('chat'));
    on(el.recipeBack, 'click', () => showRecipeView('home'));
    on(el.recipeRefresh, 'click', () => { void loadRecipeCards(); });
    on(el.recipeSeed, 'click', () => { void seedRecipeLibrary(); });
    on(el.recipeDetailClose, 'click', closeRecipeDetail);
    // 点详情浮层的空白处也关掉（点内容不关）
    on(el.recipeDetail, 'click', (event) => {
      if (event.target === el.recipeDetail) closeRecipeDetail();
    });

    // 「签到」窗口（第 40 步）：由轮盘小圆打开，同样支持 × 与点遮罩关闭；
    // 日期格是现画的，所以点击走事件委托（onCheckinDayClick）。
    on(el.checkinClose, 'click', closeDialog);
    on(el.checkinModal, 'click', (event) => {
      if (event.target === el.checkinModal) closeDialog();
    });
    on(el.checkinDays, 'click', onCheckinDayClick);
    on(el.checkinPrev, 'click', () => shiftCheckinMonth(-1));
    on(el.checkinNext, 'click', () => shiftCheckinMonth(1));
    // 补签确认框的两个按钮：确定 = 花这张卡，取消 = 什么都不做
    on(el.checkinConfirmOk, 'click', () => answerCheckinConfirm(true));
    on(el.checkinConfirmCancel, 'click', () => answerCheckinConfirm(false));

    document.addEventListener('keydown', onGlobalKeydown);
  }

  /* -------------------------------------------------- 食谱窗口（第 37 步） */

  /* 这个窗口做两件事：
   *   1) 听需求。用户**一句一句说**（"我在减脂，海鲜过敏，只有一个小电锅"），
   *      后端把话归到 requires.txt 那四组维度上，右栏那张卡就是听出来的画像。
   *      所以这里没有任何表单字段——需求不是"填表"，是"说"。
   *   2) 排菜单。按画像固定搜 4 次网上菜谱，再让模型排出未来 N 天的菜单。
   *
   * 前端的职责：把话送过去、把结果画出来、把可疑的地方如实标出来。
   * 第 50 步起不再显示"还差哪个维度"这类提示——模型自己记，
   * 不在界面上拦人（用户明确要求）。所有中文内容（开场白、餐次等）
   * 都从后端来，前端不硬编码。 */

  //: 食谱窗口有自己的会话 id，**不与聊天共用**：
  //: 需求画像跟"和 karo 闲聊"那段记忆混在一起会互相污染；
  //: 单独一个 key 也让将来的"创建新会话"变成一行（换个新 id 就行）。
  const RECIPE_SESSION_KEY = 'pet.recipe_session';

  //: 对话式检索（`/ask`）的超时。45 秒还没回来就该告诉用户"等太久了"，
  //: 并把发送按钮还给他——不能用生成菜单那套 180 秒的等待。
  const RECIPE_ASK_TIMEOUT_MS = 45000;

  function recipeSessionId() {
    let id = window.localStorage.getItem(RECIPE_SESSION_KEY);
    if (!id) {
      id = makeSessionId();
      window.localStorage.setItem(RECIPE_SESSION_KEY, id);
    }
    return id;
  }

  //: 首页横栏一屏放几张。6 张 ≈ 一屏宽（卡片 148 + 间距 12），
  //: 再多就得滑很久才看完，所以翻页交给「换一批」。
  const RECIPE_CARDS_PER_BATCH = 6;

  /* ---------------------------------------------------------- 首页 / 对话页
   *
   * 第 48 步改版：窗口里是**两个互斥的整屏界面**。
   *
   *   首页  —— 中上是可横滑的菜谱卡片（点开看材料与做法），底部是对话入口。
   *            默认停在这里，打开窗口马上有东西可看，不必先说话。
   *   对话页 —— 点底部入口进入：首页整块向上滑走，对话页从下方升起。
   *
   * 两条路**分开**（用户明确要求）：只想挑一道菜的，看卡片就行，不用对话。
   */

  /** 当前界面：'home'（推荐卡片）| 'chat'（完整对话）。 */
  function showRecipeView(view) {
    const isChat = view === 'chat';
    state.recipeView = isChat ? 'chat' : 'home';

    if (isChat) {
      // 首页向上滑走再隐藏：先加 class 播动画，动画结束再切 hidden。
      // 直接切 hidden 就没有过渡，用户只看到"啪"一下换屏。
      if (el.recipeHome) el.recipeHome.classList.add('is-leaving');
      const finish = () => {
        if (el.recipeHome) {
          el.recipeHome.hidden = true;
          el.recipeHome.classList.remove('is-leaving');
        }
        if (el.recipeChatPage) el.recipeChatPage.hidden = false;
        // 露出来之后才知道真实高度：等一帧再滚，否则 scrollHeight 还是 0
        if (typeof window.requestAnimationFrame === 'function') {
          window.requestAnimationFrame(scrollRecipeLogToBottom);
        } else {
          scrollRecipeLogToBottom();
        }
        if (el.recipeInput) el.recipeInput.focus();
      };
      // 与 CSS 的 .22s 对齐；prefers-reduced-motion 下 CSS 把过渡压成瞬时，
      // 这里照旧等一次，逻辑上不会错。
      window.setTimeout(finish, 220);
    } else {
      if (el.recipeChatPage) el.recipeChatPage.hidden = true;
      if (el.recipeHome) {
        el.recipeHome.hidden = false;
        el.recipeHome.classList.remove('is-leaving');
      }
    }
  }

  /* ------------------------------------------------------ 首页：菜谱卡片 */

  /** 拉一批卡片。「换一批」= 同一个调用，后端 shuffle 换一组。 */
  async function loadRecipeCards() {
    if (!el.recipeCards) return;
    if (el.recipeRefresh) el.recipeRefresh.disabled = true;
    try {
      const response = await apiFetch(
        `${API}/recipe/library?limit=${RECIPE_CARDS_PER_BATCH}&shuffle=1`
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      renderRecipeCards(await response.json());
    } catch (error) {
      setRecipeError(describeError(error));
    } finally {
      if (el.recipeRefresh) el.recipeRefresh.disabled = false;
    }
  }

  /** 渲染卡片。**库里一道菜都没有**时显示"载入示例菜谱"，而不是留一片空白。 */
  function renderRecipeCards(payload) {
    const recipes = (payload && payload.recipes) || [];
    if (el.recipeCards) el.recipeCards.replaceChildren();
    const empty = recipes.length === 0;

    if (el.recipeCardsEmpty) el.recipeCardsEmpty.hidden = !empty;
    if (el.recipeCards) el.recipeCards.hidden = empty;
    if (el.recipeRefresh) el.recipeRefresh.hidden = empty;

    if (empty) {
      state.recipeCards = [];
      return;
    }

    state.recipeCards = recipes;
    recipes.forEach((recipe) => {
      const card = elWith('button', 'recipe-card-item');
      card.type = 'button';
      card.dataset.id = String(recipe.id);

      // 缩略图（第 59 步）：来自独立图片库，按菜名关联。
      // 缩略图裁成统一比例（cover），卡片才整齐；加载失败就整块去掉，
      // 让卡片退回纯文字——用户自己录的菜没有图，不能留个破图占位。
      const thumb = document.createElement('img');
      thumb.className = 'recipe-card-thumb';
      thumb.src = `${API}/recipe/library/${recipe.id}/image`;
      thumb.alt = '';
      thumb.loading = 'lazy';
      thumb.decoding = 'async';
      on(thumb, 'error', () => {
        thumb.remove();
        card.classList.add('recipe-card-item--no-photo');
      });
      card.appendChild(thumb);

      const text = elWith('div', 'recipe-card-text');
      text.appendChild(elWith('span', 'recipe-card-name', recipe.name || '（未命名）'));

      const bits = [];
      if (recipe.time_minutes) bits.push(`${recipe.time_minutes} 分钟`);
      if (recipe.tags && recipe.tags.length) bits.push(recipe.tags[0]);
      text.appendChild(elWith('span', 'recipe-card-foot', bits.join(' · ') || '看看做法'));
      card.appendChild(text);

      on(card, 'click', () => { void openRecipeDetail(recipe.id); });
      el.recipeCards.appendChild(card);
    });
  }

  /** 空库时的一次性动作：导入内置示例菜谱，然后重新拉卡片。 */
  async function seedRecipeLibrary() {
    if (el.recipeSeed) el.recipeSeed.disabled = true;
    try {
      const response = await apiFetch(`${API}/recipe/library/seed`, { method: 'POST' });
      if (!response.ok) {
        setRecipeError(await readDetail(response));
        return;
      }
      await loadRecipeCards();
    } catch (error) {
      setRecipeError(describeError(error));
    } finally {
      if (el.recipeSeed) el.recipeSeed.disabled = false;
    }
  }

  /* ------------------------------------------------------ 卡片的详情浮层 */

  /** 点卡片 → 盖一层详情（材料 + 做法）；× 或 Esc 关掉。 */
  async function openRecipeDetail(recipeId) {
    if (!el.recipeDetail) return;
    setRecipeError('');
    try {
      const response = await apiFetch(`${API}/recipe/library/${Number(recipeId)}`);
      if (!response.ok) {
        setRecipeError(await readDetail(response));
        return;
      }
      renderRecipeDetail(await response.json());
      el.recipeDetail.hidden = false;
      if (el.recipeDetailClose) el.recipeDetailClose.focus();
    } catch (error) {
      setRecipeError(describeError(error));
    }
  }

  function closeRecipeDetail() {
    if (el.recipeDetail) el.recipeDetail.hidden = true;
  }

  function renderRecipeDetail(recipe) {
    if (!el.recipeDetailTitle || !el.recipeDetailBody) return;
    el.recipeDetailTitle.textContent = recipe.name || '（未命名）';

    const meta = [];
    if (recipe.time_minutes) meta.push(`约 ${recipe.time_minutes} 分钟`);
    if (recipe.tags && recipe.tags.length) meta.push(recipe.tags.join(' · '));
    // 示例菜谱与用户自己录的必须能一眼区分（source=seed）
    if (recipe.source === 'seed') meta.push('示例菜谱（AI 整理，未逐条核对）');
    if (el.recipeDetailMeta) el.recipeDetailMeta.textContent = meta.join(' ｜ ');

    const body = el.recipeDetailBody;
    body.replaceChildren();

    // 实拍图（第 58 步）：来自**独立的图片库**，按菜名关联。
    // 没有图是正常状态（用户自己录的菜就没有），所以 onerror 时整块移除，
    // 不留破图占位——详情页回到原来的纯文字样式。
    if (recipe.id) {
      const figure = elWith('figure', 'recipe-detail-photo');
      const img = document.createElement('img');
      img.src = `${API}/recipe/library/${recipe.id}/image`;
      img.alt = `${recipe.name || '菜谱'}的实拍图`;
      img.loading = 'lazy';
      on(img, 'error', () => { figure.remove(); });
      figure.appendChild(img);
      figure.appendChild(elWith(
        'figcaption', 'recipe-detail-photo-note', '图片来自网络检索，仅供参考'
      ));
      body.appendChild(figure);
    }

    const materials = elWith('section', '');
    materials.appendChild(elWith('h3', 'recipe-detail-section-title', '所需材料'));
    if (recipe.ingredients && recipe.ingredients.length) {
      materials.appendChild(listOf('recipe-detail-list', recipe.ingredients));
    } else {
      materials.appendChild(elWith('p', 'recipe-detail-note', '这道菜还没有填食材。'));
    }
    body.appendChild(materials);

    const steps = elWith('section', '');
    steps.appendChild(elWith('h3', 'recipe-detail-section-title', '制作流程'));
    if (recipe.steps && recipe.steps.length) {
      steps.appendChild(listOf('recipe-detail-list', recipe.steps));
    } else {
      steps.appendChild(elWith('p', 'recipe-detail-note', '这道菜还没有填做法。'));
    }
    body.appendChild(steps);

    if (recipe.note) {
      body.appendChild(elWith('p', 'recipe-detail-note', recipe.note));
    }
  }

  function setRecipeError(message) {
    if (!el.recipeError) return;
    el.recipeError.textContent = message || '';
    el.recipeError.hidden = !message;
  }

  /** 把食谱对话滚到最后一条（第 137 步）。
   *
   *  为什么必须单独抽出来：恢复历史时对话区还是 `hidden`（`scrollHeight` 是 0），
   *  `appendRecipeMessage` 里那句"滚到底"写进去是 0 —— 等它被露出来，
   *  浏览器仍停在顶部。用户每次打开都要自己往下翻。
   *  所以要在**露出来之后**再滚一次（见 showRecipeView 的 finish）。
   */
  function scrollRecipeLogToBottom() {
    if (!el.recipeLog) return;
    el.recipeLog.scrollTop = el.recipeLog.scrollHeight;
  }

  function appendRecipeMessage(kind, text) {
    if (!el.recipeLog || !text) return null;
    const node = elWith('div', `recipe-msg recipe-msg--${kind}`, text);
    el.recipeLog.appendChild(node);
    el.recipeLog.scrollTop = el.recipeLog.scrollHeight;
    return node;
  }

  /** 开场白由后端给：改人设只改 core/recipes.py，前端不动。 */
  function renderRecipeGreeting() {
    const greeting = state.recipeSchema && state.recipeSchema.greeting;
    if (greeting) appendRecipeMessage('bot', greeting);
  }

  /* ---------------------------------------------------- 右栏：标签按钮（第 50 步）
   *
   * 右栏现在只有**一个框、两组标签按钮**（原来的「你的需求」「试试这样说」
   * 两张卡和免责声明都删掉了）。
   *
   * 两组的分工是用户定的：
   *   一直记住 · 不吃 —— 忌口/讨厌的东西，**存进库**，重开还在；
   *   这次想要       —— 口味/怎么吃/工具/时间，**只影响这一次**
   *                      （"人的口味有时很随机"，记下来反而碍事）。
   *
   * 选项文案与分组全部来自 GET /api/recipe/library/preferences，前端不硬编码。
   * 点一下切换选中态：选中的按钮加深颜色（见 CSS 的 .is-on）。
   */

  /** 画整个右栏；`selected` 是当前亮着的忌口词条。 */
  function renderRecipeTags(preferences) {
    const data = preferences || {};
    state.recipeAversions = Array.isArray(data.aversions) ? data.aversions : [];

    const avoidBox = el.recipeTagsAvoid;
    if (avoidBox) {
      avoidBox.replaceChildren();
      const options = Array.isArray(data.avoid_options) ? data.avoid_options : [];

      /** 造一个忌口按钮。`stated` 表示这词是**用户在对话里说出来的**（第 64 步）。 */
      const makeAvoidButton = (term, stated) => {
        const on_ = state.recipeAversions.includes(term);
        // 「不吃辣」而不是「辣」：这样和下面"这次想要"里的「辣」不会看混
        const button = elWith('button', 'recipe-tag', `不吃${term}`);
        button.type = 'button';
        button.dataset.term = term;
        if (stated) {
          // 标出来源：用户才知道这个词为什么会出现在这儿（它不是固定词表里的）
          button.dataset.origin = 'stated';
          button.title = '从对话里记下的忌口，点一下可以取消';
        }
        button.setAttribute('aria-pressed', on_ ? 'true' : 'false');
        if (on_) button.classList.add('is-on');
        on(button, 'click', () => { void toggleAvoidTerm(term); });
        return button;
      };

      options.forEach((term) => avoidBox.appendChild(makeAvoidButton(term, false)));

      // 固定词表**装不下**的词也要能看见、能取消（第 64 步）。
      // 否则用户在对话里说的「花生」会永久生效却永远删不掉 ——
      // 那等于"取消勾选即删除"这条语义只覆盖了 8 个常用词。
      // 只有真的存在这种词时才多出按钮，默认外观不变。
      state.recipeAversions
        .filter((term) => term && !options.includes(term))
        .forEach((term) => avoidBox.appendChild(makeAvoidButton(term, true)));
    }

    const wantBox = el.recipeTagsWant;
    if (wantBox) {
      wantBox.replaceChildren();
      (data.want_groups || []).forEach((group) => {
        const row = elWith('div', 'recipe-want-group');
        row.appendChild(elWith('span', 'recipe-want-label', group.label || ''));
        const tags = elWith('div', 'recipe-tags');
        (group.tags || []).forEach((tag) => {
          const on_ = state.recipeWanted.includes(tag);
          const button = elWith('button', 'recipe-tag', tag);
          button.type = 'button';
          button.dataset.tag = tag;
          button.setAttribute('aria-pressed', on_ ? 'true' : 'false');
          if (on_) button.classList.add('is-on');
          on(button, 'click', () => toggleWantTag(tag));
          tags.appendChild(button);
        });
        row.appendChild(tags);
        wantBox.appendChild(row);
      });
    }
  }

  function toggleWantTag(tag) {
    const at = state.recipeWanted.indexOf(tag);
    if (at >= 0) {
      state.recipeWanted.splice(at, 1);
    } else {
      // 互斥组（时间/怎么吃）：先把同组其它选项清掉，再点亮这一个。
      // 哪一组互斥由**后端**给（exclusive 字段），前端不写死。
      const group = (state.recipeWantGroups || []).find(
        (item) => (item.tags || []).includes(tag)
      );
      if (group && group.exclusive) {
        state.recipeWanted = state.recipeWanted.filter(
          (value) => !(group.tags || []).includes(value)
        );
      }
      state.recipeWanted.push(tag);
    }
    // 只重画右栏（不重新请求）：这次想要的标签是纯前端状态
    renderRecipeTags({
      aversions: state.recipeAversions,
      avoid_options: state.recipeAvoidOptions,
      want_groups: state.recipeWantGroups,
    });
  }

  /** 忌口词条：点一下 → 立刻存库（"一直记住"就得真的记住）。 */
  async function toggleAvoidTerm(term) {
    const before = state.recipeAversions.slice();
    const at = state.recipeAversions.indexOf(term);
    if (at >= 0) state.recipeAversions.splice(at, 1);
    else state.recipeAversions.push(term);
    renderRecipeTags({
      aversions: state.recipeAversions,
      avoid_options: state.recipeAvoidOptions,
      want_groups: state.recipeWantGroups,
    });

    try {
      const response = await apiFetch(`${API}/recipe/library/preferences`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ aversions: state.recipeAversions }),
      });
      if (!response.ok) {
        // 存不上就把界面退回原样——不能让用户以为"记住了"其实没记住
        state.recipeAversions = before;
        renderRecipeTags({
          aversions: before,
          avoid_options: state.recipeAvoidOptions,
          want_groups: state.recipeWantGroups,
        });
        setRecipeError(await readDetail(response));
        return;
      }
      renderRecipeTags(await response.json());
      // 存上了就把上一次的失败提示清掉：否则"改好了但红字还挂着"，
      // 用户会以为仍然没记住（行为验证抓到的缺陷）。
      setRecipeError('');
    } catch (error) {
      state.recipeAversions = before;
      renderRecipeTags({
        aversions: before,
        avoid_options: state.recipeAvoidOptions,
        want_groups: state.recipeWantGroups,
      });
      setRecipeError(describeError(error));
    }
  }

  async function loadRecipePreferences() {
    try {
      const response = await apiFetch(`${API}/recipe/library/preferences`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      // 记住选项表本身，供 toggleWantTag 局部重画时用
      state.recipeAvoidOptions = data.avoid_options || [];
      state.recipeWantGroups = data.want_groups || [];
      renderRecipeTags(data);
    } catch (error) {
      setRecipeError(describeError(error));
    }
  }

  /** schema 只取一次：它是静态结构，一个窗口会话里不会变。 */
  async function loadRecipeSchema() {
    if (state.recipeSchema) return state.recipeSchema;

    const response = await apiFetch(`${API}/recipe/schema`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const schema = await response.json();
    state.recipeSchema = schema;

    // 第 50 步起免责声明不再展示（用户要求去掉）；schema 里那个字段仍留着。

    // 输入框的示例句由后端给（第 51 步起 schema 只回 example 一个字段，
    // 不再是 13 个需求维度的 fields 列表）
    if (el.recipeInput && schema.example) {
      el.recipeInput.placeholder = `例如：${schema.example}`;
    }
    return schema;
  }

  /** 打开窗口时跑：取 schema + 恢复会话 + 拉首页那批卡片。
   *
   *  第 48 步起窗口默认停在**首页**（有卡片可看），所以这里先把首页准备好；
   *  会话恢复（对话记录、画像）仍照旧做——用户点进对话页时才有东西可看。 */
  async function prepareRecipe() {
    setRecipeError('');
    showRecipeView('home');

    try {
      await loadRecipeSchema();
    } catch (error) {
      setRecipeError(describeError(error));
      return;
    }

    // 首页卡片与对话历史互不依赖，任何一个失败都不该挡住另一个
    await loadRecipeCards();
    // 右栏的标签框（忌口 + 这次想要）也要在有窗口时准备好
    await loadRecipePreferences();

    // 评价8 抓到的真实竞态：`restoreRecipeSession` 会 `replaceChildren()`，
    // 而它在窗口刚打开时是**异步**回来的。用户在它回来之前发了一句，
    // 那句连同刚到的卡片就会被清掉 —— 界面上像是"消息被吃了"。
    // 用"代次"判断：发过消息就不再拿旧历史覆盖。
    const epoch = state.recipeEpoch;
    try {
      const response = await apiFetch(
        `${API}/recipe/session?session_id=${encodeURIComponent(recipeSessionId())}`
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      // 两种情况下**不能**重建历史（重建会 replaceChildren() 把在途的流式气泡摘掉）：
      // 1. 用户已经发过消息（`recipeEpoch` 变了）；
      // 2. 这一轮还在流式输出中（评价12 P3-1：切走再切回时，恢复历史会把
      //    正在打字的气泡连根拔走，后续 delta 全写进游离节点 → 回答"消失"）。
      if (state.recipeEpoch !== epoch || state.recipeSending) return;
      restoreRecipeSession(data);
    } catch (error) {
      if (state.recipeEpoch !== epoch || state.recipeSending) return;
      // 拿不到历史不影响用，但开场白必须在，否则窗口是一片空白
      if (el.recipeLog) el.recipeLog.replaceChildren();
      renderRecipeGreeting();
      setRecipeError(describeError(error));
    }
  }

  function restoreRecipeSession(data) {
    if (el.recipeLog) el.recipeLog.replaceChildren();

    const messages = (data && data.messages) || [];
    if (!messages.length) {
      renderRecipeGreeting();
    } else {
      // 每条答复都带 `recipes`（第 137 步）。先把文字全部铺好，
      // 再按 id 批量把卡片补回去 —— 这样刷新/关窗重开后卡片不会消失。
      const rows = [];
      messages.forEach((message) => {
        appendRecipeMessage(message.role === 'user' ? 'user' : 'bot', message.content);
        const ids = Array.isArray(message.recipes) ? message.recipes : [];
        if (ids.length) rows.push(ids);
      });
      rows.forEach((ids) => { void restoreRecipeAnswerCards(ids); });
    }
    updateShoppingBar();
    // 旧的"多天菜单"不再恢复：第 50 步把它整套去掉了（用户只要这一餐）。
    // 页面已经可见的情况（历史晚到 / 新建会话后重绘）也要停在最后一条
    scrollRecipeLogToBottom();
  }

  /** 按 id 取回菜谱并补一行卡片（恢复历史用，第 137 步）。
   *
   *  取不到（菜被删了/接口挂了）就静静不画 —— 历史的文字还在，
   *  不能因为卡片失败让整段对话报错。
   */
  async function restoreRecipeAnswerCards(ids) {
    try {
      const response = await apiFetch(
        `${API}/recipe/library/by-ids?ids=${encodeURIComponent(ids.join(','))}`
      );
      if (!response.ok) return;
      const recipes = await response.json();
      if (Array.isArray(recipes) && recipes.length) {
        appendRecipeAnswerCards(recipes);
        // 卡片是**异步**补回来的（第 137 步）：它们长出来之后，刚才那次
        // "滚到底"就过期了。补一次，否则用户看到的仍不是最后一条。
        scrollRecipeLogToBottom();
      }
    } catch (_error) {
      /* 卡片恢复失败不影响对话 */
    }
  }

  /** 对话式检索（第 50 步 50d）：一句话 → 查库 → 回复 + 真实菜谱卡片。
   *
   *  和旧流程的区别：不再"一步步问需求"，而是模型先调 `search_recipes` 查库，
   *  拿着真实菜谱回话；返回的 `recipes` 是**后端按 id 从库里取的**，
   *  所以卡片上的菜一定真实存在，模型编不出来。
   */
  async function sendRecipeMessage(text) {
    const message = String(text || '').trim();
    if (!message || state.recipeSending) return;

    state.recipeEpoch += 1;      // 从这一刻起，旧历史不许再覆盖
    setRecipeError('');
    appendRecipeMessage('user', message);
    state.recipeSending = true;
    if (el.recipeSend) el.recipeSend.disabled = true;
    if (el.recipeInput) el.recipeInput.value = '';

    // 超时 + 可取消。没有这两样时，后端一旦挂住，发送按钮会**永久**变灰
    // （只有 finally 会恢复它）；关窗也不打断在途请求，响应回来会往新窗口里
    // 追加一条"没有对应提问"的回复和卡片（都是行为验证抓到的真缺陷）。
    const controller = new AbortController();
    state.recipeAskController = controller;
    // 超时改成"**静默**超时"（第 74 步）：只要还在吐字就续期。
    // 以前是固定 45 秒总预算 —— 流式下较长的回答会被腰斩，
    // 而那正是"已经在正常输出"的时刻，掐掉最没道理。
    let timer = null;
    const armTimer = () => {
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(() => controller.abort(), RECIPE_ASK_TIMEOUT_MS);
    };
    armTimer();

    try {
      const response = await apiFetch(`${API}/recipe/ask/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // wanted：右栏「这次想要」勾着的标签（第 50 步 50e）。
        // 只影响这一顿，后端把它翻译成 search_recipes 的参数。
        body: JSON.stringify({
          session_id: recipeSessionId(),
          message,
          wanted: state.recipeWanted,
        }),
        signal: controller.signal,
      });

      // 已经被关窗/换会话掐掉了：结果属于上一次，不能画进当前对话
      if (state.recipeAskController !== controller) return;

      if (!response.ok) {
        setRecipeError(await readDetail(response));
        return;
      }

      // 边读边画；返回 meta.payload（与原来 POST /ask 的响应体同构），
      // 所以下面卡片渲染这段一行都不用改。
      const data = await consumeRecipeStream(response, {
        stillMine: () => state.recipeAskController === controller,
        onActivity: armTimer,
      });
      if (!data) return;      // 被掐掉了 / 流里没有任何内容
      appendRecipeAnswerCards((data && data.recipes) || []);
    } catch (error) {
      // abort 引起的 AbortError 不当作错误展示（超时除外——那要告诉用户"等太久了"）
      if (state.recipeAskController !== controller) return;
      setRecipeError(describeError(error));
    } finally {
      window.clearTimeout(timer);
      if (state.recipeAskController === controller) {
        state.recipeAskController = null;
      }
      state.recipeSending = false;
      if (el.recipeSend) el.recipeSend.disabled = false;
      if (el.recipeInput) el.recipeInput.focus();
    }
  }

  /** 建一条**空**的机器人气泡，供流式增量填充（第 74 步）。
   *
   *  为什么不复用 `appendRecipeMessage`：它对空文本直接返回 null
   *  （防止误调用画出空行），而流式恰恰需要"先有个容器，再往里写字"。
   */
  function createStreamingBubble() {
    if (!el.recipeLog) return null;
    const node = elWith('div', 'recipe-msg recipe-msg--bot', '');
    el.recipeLog.appendChild(node);
    return node;
  }

  /** 更新流式气泡的文字并贴着底滚动。 */
  function setRecipeBubbleText(node, text) {
    if (!node) return;
    node.textContent = text;
    if (el.recipeLog) {
      el.recipeLog.scrollTop = el.recipeLog.scrollHeight;
    }
  }

  /** 读 SSE 流、边到边画，最后用 meta 覆盖一次（第 74 步）。
   *
   *  为什么"最后还要覆盖"：delta 只保证"看得见在长"，不保证完整
   *  （浏览器可能缓冲、可能中途断流）。meta 里是服务端认定的完整回答，
   *  以它为准覆盖一次，前端不必担心自己拼错。
   *
   *  三种情况都有兜底：
   *  - 拿不到 `body.getReader`（老浏览器 / 测试桩）→ 整段 `response.text()` 再解析，
   *    降级成"一次性出现"，功能不会坏；
   *  - `reset`（这一轮其实要调工具）→ 清空气泡，别把"我查一下"当成回答留下；
   *  - 一条 delta 都没来也没有 meta → 不留空气泡。
   *
   *  返回 meta.payload；被掐掉时返回 null。
   */
  /** 通用的 SSE 解析器（第 117 步抽出来的）。
   *
   *  为什么抽：聊天接流式之后，两处（食谱 / 聊天）要的是**同一套解析** ——
   *  `data:` 行、空行分块、半截块丢弃、客户端走人时 cancel reader。
   *  各自的"事件怎么画"不同，所以这里只负责解析与派发。
   */
  async function streamSse(response, options) {
    const opts = options || {};
    const stillMine = typeof opts.stillMine === 'function' ? opts.stillMine : () => true;
    const onActivity = typeof opts.onActivity === 'function' ? opts.onActivity : null;
    const onEvent = typeof opts.onEvent === 'function' ? opts.onEvent : () => {};

    const body = response.body;
    const reader =
      body && typeof body.getReader === 'function' ? body.getReader() : null;

    let buffer = '';
    const drain = (chunkText) => {
      buffer += chunkText;
      let at = buffer.indexOf('\n\n');
      while (at >= 0) {
        const block = buffer.slice(0, at);
        buffer = buffer.slice(at + 2);
        block.split('\n').forEach((line) => {
          if (!line.startsWith('data:')) return;
          const raw = line.slice(5).trim();
          if (!raw) return;
          try {
            onEvent(JSON.parse(raw));
          } catch (_error) {
            // 半截的 / 非 JSON 的心跳行：跳过，不中断整条流
          }
        });
        at = buffer.indexOf('\n\n');
      }
    };

    if (reader) {
      const decoder = new TextDecoder();
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        if (!stillMine()) {
          try { await reader.cancel(); } catch (_error) { /* 已经断了 */ }
          return;
        }
        if (onActivity) onActivity();
        drain(decoder.decode(chunk.value, { stream: true }));
      }
      drain(decoder.decode());
    } else {
      drain(await response.text());
    }
  }

  async function consumeRecipeStream(response, options) {
    const opts = options || {};
    const stillMine = typeof opts.stillMine === 'function' ? opts.stillMine : () => true;

    let bubble = null;
    let text = '';
    let meta = null;
    let streamed = false;

    await streamSse(response, {
      stillMine,
      onActivity: opts.onActivity,
      onEvent: (event) => {
        if (!event || !stillMine()) return;
        if (event.type === 'delta') {
          streamed = true;
          if (!bubble) bubble = createStreamingBubble();
          text += event.text || '';
          setRecipeBubbleText(bubble, text);
        } else if (event.type === 'reset') {
          // 这一轮其实要去调工具：刚才流的字不是给用户的答案
          text = '';
          setRecipeBubbleText(bubble, '');
        } else if (event.type === 'error') {
          // 服务端给的就是完整人话，别再拼一次前缀
          text = '';
          streamed = true;
          if (!bubble) bubble = createStreamingBubble();
          setRecipeBubbleText(bubble, event.message || '');
        } else if (event.type === 'meta') {
          meta = event.payload || null;
        }
      },
    });

    if (!stillMine()) return null;

    if (meta) {
      if (bubble) setRecipeBubbleText(bubble, meta.reply || '');
      else if (meta.reply) appendRecipeMessage('bot', meta.reply);
    } else if (!bubble && !streamed) {
      return null;
    }
    return meta || {};
  }

  /** 掐掉在途的 /ask（关窗时调用；也用于"换会话"这种需要清场的场景）。 */
  function abortRecipeAsk() {
    const controller = state.recipeAskController;
    if (!controller) return;
    state.recipeAskController = null;
    controller.abort();
  }

  /** 把这一轮检索到的菜画成一行小卡片（点开看材料与做法）。
   *
   *  卡片内容来自后端按 id 取的库数据，不是模型说的话——
   *  这条是"不许编造"的落地方式。 */
  function appendRecipeAnswerCards(recipes) {
    if (!el.recipeLog || !recipes.length) return;
    const row = elWith('div', 'recipe-answer-cards');
    recipes.forEach((recipe) => {
      const card = elWith('button', 'recipe-answer-card');
      card.type = 'button';
      card.appendChild(elWith('span', 'recipe-answer-name', recipe.name || ''));
      const bits = [];
      if (recipe.time_minutes) bits.push(`${recipe.time_minutes} 分钟`);
      if (recipe.tags && recipe.tags.length) bits.push(recipe.tags[0]);
      card.appendChild(elWith('span', 'recipe-answer-foot', bits.join(' · ')));

      on(card, 'click', () => { void openRecipeDetail(recipe.id); });

      // 勾选框（第 126 步）：勾了就能生成买菜清单。
      // 必须 stopPropagation —— 否则点勾选框会同时打开菜谱详情。
      // 放在点击绑定**之后**，是为了让"点卡片打开详情"这句仍然紧挨着卡片构造。
      const pick = document.createElement('input');
      pick.type = 'checkbox';
      pick.className = 'recipe-pick';
      pick.title = '加入买菜清单';
      pick.setAttribute('aria-label', `把「${recipe.name || ''}」加入买菜清单`);
      pick.checked = state.pickedRecipes.has(recipe.id);
      on(pick, 'click', (event) => event.stopPropagation());
      on(pick, 'change', () => {
        if (pick.checked) state.pickedRecipes.set(recipe.id, recipe.name || '');
        else state.pickedRecipes.delete(recipe.id);
        updateShoppingBar();
      });
      card.appendChild(pick);
      row.appendChild(card);
    });
    el.recipeLog.appendChild(row);
    el.recipeLog.scrollTop = el.recipeLog.scrollHeight;
  }

  //: 食谱确认框的回答回调。
  //: 刻意**不放进 el**：`el` 里的东西会被启动自检当成"元素"，运行时状态放进去会误报
  //: （第 108 步的行为验证就是这么抓到一个假警告的）。
  let recipeConfirmResolve = null;

  /** 在**食谱窗口内部**弹一句确认，返回 Promise<boolean>（第 131 步）。
   *
   *  与对话清空用同一个理由：不用浏览器原生 confirm（跟页面无关、阻塞脚本）。
   */
  function openRecipeConfirm(text) {
    return new Promise((resolve) => {
      if (!el.recipeConfirm) {
        resolve(false);                 // 元素缺失时保守处理：当作取消
        return;
      }
      if (el.recipeConfirmText) el.recipeConfirmText.textContent = text;
      if (el.recipeConfirmOk) el.recipeConfirmOk.textContent = '新建';
      if (el.recipeConfirmCancel) el.recipeConfirmCancel.textContent = '取消';
      el.recipeConfirm.hidden = false;
      if (el.recipeConfirmOk) el.recipeConfirmOk.focus();
      recipeConfirmResolve = resolve;
    });
  }

  function answerRecipeConfirm(answer) {
    const resolve = recipeConfirmResolve;
    recipeConfirmResolve = null;
    if (el.recipeConfirm) el.recipeConfirm.hidden = true;
    if (resolve) resolve(Boolean(answer));
  }

  /** 新建会话：清掉这段食谱对话（第 131 步）。
   *
   *  三条边界（与后端 `POST /api/recipe/reset` 的语义对齐）：
   *  - 只清**对话**；长期忌口（存库）与菜谱库都不动；
   *  - **会话 id 不变**：新建的是"这段对话从空白开始"，不是换身份；
   *  - 先二次确认 —— 清掉的对话没有回收站。
   */
  async function startNewRecipeSession() {
    if (!(await openRecipeConfirm('新建会话？当前这段食谱对话会被清空（忌口与菜谱库不受影响）。'))) {
      return;
    }
    // 第 145 步：先掐掉在途的那一轮。
    // `abortRecipeAsk()` 会把 controller 置空，在途请求的 catch 见 controller 不匹配就静默返回 ——
    // 否则旧回答会出现在新会话里，而且 recipeSending 要等旧流结束才恢复。
    abortRecipeAsk();
    if (el.recipeNew) el.recipeNew.disabled = true;
    try {
      const response = await apiFetch(`${API}/recipe/reset`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: recipeSessionId() }),
      });
      if (!response.ok) {
        setRecipeError(await readDetail(response));
        return;
      }
      // 代次 +1：晚到的历史响应不许再把旧对话画回来（第 124 步的竞态防护）
      state.recipeEpoch += 1;
      if (el.recipeLog) el.recipeLog.replaceChildren();
      renderRecipeGreeting();
      // 勾选也一起清：清单属于上一段对话
      state.pickedRecipes.clear();
      updateShoppingBar();
      setRecipeError('');
      if (el.recipeInput) el.recipeInput.focus();
    } catch (error) {
      setRecipeError(describeError(error));
    } finally {
      if (el.recipeNew) el.recipeNew.disabled = false;
    }
  }

  /** 勾选数量变了：更新工具条（没勾就整条隐藏，别留空白） */
  function updateShoppingBar() {
    if (!el.shoppingBar) return;
    const count = state.pickedRecipes.size;
    el.shoppingBar.hidden = count === 0;
    if (el.shoppingCount) {
      const names = Array.from(state.pickedRecipes.values()).slice(0, 3).join('、');
      el.shoppingCount.textContent =
        count === 0 ? '' : `已选 ${count} 道：${names}${count > 3 ? ' 等' : ''}`;
    }
  }

  /** 清空已选（第 137 步）：工具条上的 × 。
   *
   *  以前这条只能靠"新建会话"消失 —— 用户既不想清对话、又想关掉它时没有出路。
   *  取消勾选要连**卡片上的勾选框**一起改，否则会出现"条没了但框还勾着"。
   */
  function clearRecipePicks() {
    state.pickedRecipes.clear();
    if (el.recipeLog && typeof el.recipeLog.querySelectorAll === 'function') {
      el.recipeLog.querySelectorAll('.recipe-pick').forEach((box) => {
        box.checked = false;
      });
    }
    updateShoppingBar();
  }

  /** 生成买菜清单并显示（第 126 步）。
   *
   *  **合并与排版都在服务端做**：数量不做加法（"姜 1 大块" + "姜 1 块" 加起来是错的），
   *  只按名字归组、数量原样保留。前端只负责画。
   */
  async function openShoppingList() {
    const ids = Array.from(state.pickedRecipes.keys());
    if (!ids.length) return;
    setRecipeError('');
    try {
      const response = await apiFetch(`${API}/recipe/library/shopping-list`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ recipe_ids: ids }),
      });
      if (!response.ok) {
        setRecipeError(await readDetail(response));
        return;
      }
      const data = await response.json();
      renderShoppingList(data);
    } catch (error) {
      setRecipeError(describeError(error));
    }
  }

  function renderShoppingList(data) {
    if (!el.shoppingItems) return;
    state.shoppingText = data.text || '';
    el.shoppingItems.replaceChildren();

    (data.items || []).forEach((item) => {
      const row = elWith('li', 'shopping-item');
      row.appendChild(elWith('span', 'shopping-item-name', item.name || ''));
      const amounts = (item.amounts || []).join('、');
      row.appendChild(
        elWith('span', 'shopping-item-amount', amounts || '按需')
      );
      row.appendChild(
        elWith('span', 'shopping-item-from', (item.recipes || []).join('、'))
      );
      el.shoppingItems.appendChild(row);
    });

    if (el.shoppingHint) {
      const missing = (data.missing || []).length;
      el.shoppingHint.textContent =
        '数量按原菜谱保留，没有相加 —— 单位不同（根/块/勺）加起来会是错的。' +
        (missing ? `另有 ${missing} 道菜已经不在库里了。` : '');
    }
    if (el.shoppingStatus) el.shoppingStatus.textContent = '';
    if (el.shoppingPanel) el.shoppingPanel.hidden = false;
    if (el.shoppingCopy) el.shoppingCopy.focus();
  }

  async function copyShoppingList() {
    const text = state.shoppingText || '';
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      if (el.shoppingStatus) el.shoppingStatus.textContent = '已复制';
    } catch (_error) {
      // 老浏览器 / 无权限：明确说"没复制成功"，不要假装成功
      if (el.shoppingStatus) el.shoppingStatus.textContent = '复制失败，请手动选择';
    }
  }

  function closeShoppingList() {
    if (el.shoppingPanel) el.shoppingPanel.hidden = true;
    if (el.shoppingOpen) el.shoppingOpen.focus();
  }

  function onRecipeSubmit(event) {
    event.preventDefault();
    // 有图就先发图（第 60 步）：识图是独立功能，不走 /recipe/ask
    if (state.recipePhoto) {
      void sendRecipePhoto(state.recipePhoto, el.recipeInput ? el.recipeInput.value : '');
      return;
    }
    void sendRecipeMessage(el.recipeInput ? el.recipeInput.value : '');
  }


  /* ------------------------------------------------ 上传图片识菜（第 60 步）
     与文字那条路完全独立：发图走 POST /recipe/photo，不碰 /recipe/ask、
     不碰右栏勾选、也不往检索会话里写东西。识图答得好不好都不影响原来的菜谱功能。 */

  const RECIPE_PHOTO_MAX_BYTES = 8 * 1024 * 1024;
  // 实测一次识图约 24 秒（上传 + Lens 12~15s + 搜索 + 模型），前端给 90 秒
  const RECIPE_PHOTO_TIMEOUT_MS = 90000;

  function setRecipePhoto(file) {
    if (!file) return;
    const type = String(file.type || '');
    if (type && !/^image\//.test(type)) {
      setRecipeError('只能发图片（JPEG / PNG / WebP）');
      return;
    }
    if (file.size > RECIPE_PHOTO_MAX_BYTES) {
      setRecipeError(`图片太大了（${(file.size / 1024 / 1024).toFixed(1)} MB），上限 8 MB`);
      return;
    }
    clearRecipePhoto();
    state.recipePhoto = file;
    state.recipePhotoUrl = URL.createObjectURL(file);
    if (el.recipePhotoThumb) el.recipePhotoThumb.src = state.recipePhotoUrl;
    if (el.recipePhotoName) el.recipePhotoName.textContent = file.name || '图片';
    if (el.recipePhotoPreview) el.recipePhotoPreview.hidden = false;
    setRecipeError('');
  }

  function clearRecipePhoto() {
    if (state.recipePhotoUrl) URL.revokeObjectURL(state.recipePhotoUrl);
    state.recipePhoto = null;
    state.recipePhotoUrl = '';
    if (el.recipePhotoPreview) el.recipePhotoPreview.hidden = true;
    if (el.recipePhotoThumb) el.recipePhotoThumb.removeAttribute('src');
    if (el.recipePhotoInput) el.recipePhotoInput.value = '';
  }

  function readPhotoBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const text = String(reader.result || '');
        resolve(text.includes(',') ? text.slice(text.indexOf(',') + 1) : text);
      };
      reader.onerror = () => reject(new Error('读不到这个文件'));
      reader.readAsDataURL(file);
    });
  }

  async function sendRecipePhoto(file, note) {
    if (!file || state.recipeSending) return;

    setRecipeError('');
    appendRecipeMessage('user', `（图片）${file.name || '照片'}`);
    state.recipeSending = true;
    if (el.recipeSend) el.recipeSend.disabled = true;
    if (el.recipePhotoBtn) el.recipePhotoBtn.disabled = true;

    const controller = new AbortController();
    state.recipeAskController = controller;
    const timer = window.setTimeout(
      () => controller.abort(), RECIPE_PHOTO_TIMEOUT_MS
    );

    let succeeded = false;
    try {
      const imageBase64 = await readPhotoBase64(file);
      const response = await apiFetch(`${API}/recipe/photo`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image_base64: imageBase64, note: String(note || '') }),
        signal: controller.signal,
      });
      if (state.recipeAskController !== controller) return;
      if (!response.ok) {
        setRecipeError(await readDetail(response));
        return;
      }
      const data = await response.json();
      // 后端把所有失败也翻译成了人话，统一显示在气泡里
      appendRecipeMessage('bot', data.reply || '没认出来。');
      succeeded = true;
    } catch (error) {
      if (error && error.name === 'AbortError') {
        // 关窗打断不算超时：那时 controller 已经被 abortRecipeAsk() 置空。
        // 报"超时"会让人以为是她太慢（评价12 P4-5 的另一半）。
        if (state.recipeAskController === controller) {
          setRecipeError('识图超时了，稍后再试一次吧。');
        }
      } else {
        setRecipeError('识图请求失败了，稍后再试一次吧。');
      }
    } finally {
      window.clearTimeout(timer);
      // 复位**不能写在条件里**：关窗时 abortRecipeAsk() 先把 controller 置空，
      // 条件不成立 → recipeSending 永远停在 true，整个窗口再也发不出消息。
      if (state.recipeAskController === controller) state.recipeAskController = null;
      state.recipeSending = false;
      if (el.recipeSend) el.recipeSend.disabled = false;
      if (el.recipePhotoBtn) el.recipePhotoBtn.disabled = false;
      // 只有真成功才清输入框和已选图片；失败/被打断时留着，方便直接重试
      if (succeeded) {
        if (el.recipeInput) el.recipeInput.value = '';
        clearRecipePhoto();
      }
    }
  }

  /** 上传按钮 + 拖拽：两个入口都只是"选一张图"，发送仍走同一个提交键。 */
  function bindRecipePhotoInput() {
    on(el.recipePhotoBtn, 'click', () => {
      if (el.recipePhotoInput) el.recipePhotoInput.click();
    });
    on(el.recipePhotoInput, 'change', () => {
      const files = el.recipePhotoInput && el.recipePhotoInput.files;
      if (files && files[0]) setRecipePhoto(files[0]);
    });
    on(el.recipePhotoRemove, 'click', clearRecipePhoto);
  // 买菜清单（第 126 步）
  on(el.shoppingOpen, 'click', () => { void openShoppingList(); });
  on(el.shoppingClose, 'click', closeShoppingList);
  on(el.shoppingCopy, 'click', () => { void copyShoppingList(); });
  // 行程导出（第 130 步）
  on(el.tripExport, 'click', () => { void copyTripText(); });
  on(el.tripMarkdown, 'click', () => { void downloadTripMarkdown(); });
  // 食谱「新建会话」（第 131 步）
  on(el.recipeNew, 'click', () => { void startNewRecipeSession(); });
  on(el.shoppingClear, 'click', clearRecipePicks);
  on(el.recipeConfirmOk, 'click', () => answerRecipeConfirm(true));
  on(el.recipeConfirmCancel, 'click', () => answerRecipeConfirm(false));

    // 拖拽。**必须 preventDefault**，否则浏览器会把图片当成新页面打开，
    // 整个桌宠窗口就没了（最常踩的坑）。
    const page = el.recipeChatPage;
    if (!page) return;
    const hasFiles = (event) => {
      const types = event && event.dataTransfer && event.dataTransfer.types;
      if (!types) return false;
      return Array.prototype.indexOf.call(types, 'Files') >= 0;
    };
    on(page, 'dragenter', (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      page.classList.add('is-dragover');
    });
    on(page, 'dragover', (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      page.classList.add('is-dragover');
    });
    on(page, 'dragleave', (event) => {
      event.preventDefault();
      page.classList.remove('is-dragover');
    });
    on(page, 'drop', (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      page.classList.remove('is-dragover');
      const files = event.dataTransfer && event.dataTransfer.files;
      if (files && files[0]) setRecipePhoto(files[0]);
    });
  }


  function listOf(className, items) {
    const list = elWith('ul', className);
    (items || []).forEach((text) => list.appendChild(elWith('li', '', text)));
    return list;
  }


  /* ------------------------------------------------------------ 签到窗口 */

  /* 第 40 步。窗口里是一张**日历表**：
       - 每个日期格上面是"几号"，下面是"已签 / 未签"；
       - **还没到的日期不算**：不写状态、灰掉、也不可点；
       - 今天点一下就签到；**过去的日子是补签，要消耗一张「补签卡」**，
         没有卡就提示"未持有补签卡"（用户第 4 条，道具系统后面再做）。

     两处刻意不在前端做的事：

     1. **日历网格不由前端拼**：月初要空几格、1 号落在周几，全按后端返回的
        days 顺序铺。前端用 new Date() 自己算月首偏移是日历最常见的错源
        （时区、月末、闰年各差一格），交给 Python 的 date.weekday() 又准又好测。
     2. **"今天"与"有没有卡"都以服务端为准**：前端不拿本机时钟判"这个能不能
        签"（否则改一下系统时间就能签明天），也不自己扣卡——卡在后端，
        签不成就不该少一张。
  */

  /** 日历固定 7 列（周一 → 周日），与后端返回的 weekdays 顺序一致。 */
  const WEEKDAY_COLUMNS = 7;

  /** 拼接某个月的查询串。month 为空表示"当月"，由后端决定是哪个月。 */
  function checkinMonthQuery(month) {
    const id = encodeURIComponent(sessionId());
    return month
      ? `session_id=${id}&month=${encodeURIComponent(month)}`
      : `session_id=${id}`;
  }

  /** 那一行文字：取数前是"正在打开日历…"，取数失败时改成红色的失败原因。 */
  function setCheckinState(message, isError = false) {
    if (!el.checkinState) return;
    el.checkinState.textContent = message || '';
    el.checkinState.classList.toggle('is-error', Boolean(isError));
    el.checkinState.hidden = !message;
  }

  /** 把异常翻成界面上的那句话。
   *
   *  **刻意不用 describeError**：它会把后端写好的中文 detail（"这一天还没到…"）
   *  统一换成"出了点问题，稍后再试一次？"，等于把"为什么失败"丢掉。
   *  这里只兜住真正的网络层错误（fetch 抛 TypeError），其余原样透出——
   *  错误文案来自 readDetail（detail 优先，数组取第一项 msg）。
   */
  function checkinFailureText(error) {
    if (error instanceof TypeError) return '连不上后端服务，确认它启动了吗？';
    const message = error && error.message ? String(error.message) : '';
    return message || '签到没成，稍后再试一次。';
  }

  /** 取某个月的视图；month 为空则取当月。翻月与首次打开都走这里。 */
  async function loadCheckinMonth(month) {
    try {
      const response = await apiFetch(`${API}/checkin/month?${checkinMonthQuery(month)}`);
      if (!response.ok) throw new Error(await readDetail(response));
      renderCheckinCalendar(await response.json());
    } catch (error) {
      // 连日历都没拿到 → 没有哪一格可以挂提示，只能在这一行里说清原因
      state.checkinCardNote = null;
      showCheckinFailure(checkinFailureText(error));
    }
  }

  /** 打开窗口时的入口：先把界面清成"取数中"，再去拿当月视图。 */
  async function prepareCheckin() {
    await loadCheckinMonth(state.checkinMonth);
  }

  /** 取数失败：窗口不能只剩一片空白，要说清"发生了什么"。 */
  function showCheckinFailure(message) {
    if (el.checkinGrid) el.checkinGrid.hidden = true;
    setCheckinState(message || '签到日历没取到，稍后再试。', true);
  }

  /** 把一个日期格做成按钮。

   *  第 146 步按用户的五条要求重排了格子内容（自上而下）：
   *    1. 日期数字（最大）；
   *    2. 节日名（有节日才显示）；
   *    3. 农历日期（初一显示"九月"这种月份名）；
   *    4. 已签 / 未签（最小，放在农历/节日**下面** —— 用户第 2 条）。
   *  节日的"休 / 班"小标挂在右上角，也是用户要求的。
   */
  function checkinDayCell(day, view) {
    const cell = document.createElement('button');
    cell.type = 'button';
    cell.className = 'cal-day';

    const status = day && day.status;
    if (status) cell.classList.add(`is-${status}`);
    if (day && day.is_today) cell.classList.add('is-today');
    // 过去的日子加一个类：鼠标指向/点击时用灰蓝色标出来（用户第 4 条），
    // 与"今天"的蓝色、以及"还没到"的透明格区分开。
    if (day && day.past) cell.classList.add('is-past');

    // 状态那一行：文字由后端给（status_labels / note），前端不硬编码中文。
    // 空白格没有日期，所以先判 day.date 再取 dataset——不然会写出 undefined。
    const labels = (view && view.status_labels) || {};
    if (status && day.date) {
      cell.dataset.date = day.date;
      // 悬停提示：节日全部 + 农历全称 + 假期名，格子里放不下的都收在这
      const tip = (day.calendar_tip || '').trim();
      cell.title = [day.date, tip, day.note || labels[status] || '']
        .filter(Boolean).join(' · ');
    }
    // 补签标记：早于今天、且还没签过的日子。点它会消耗一张道具卡，
    // 所以先弹一句确认（见 onCheckinDayClick / askMakeup）。
    if (day && day.past && status !== 'done') {
      cell.dataset.makeup = '1';
    }
    if (!day || !day.clickable) {
      // 还没到的日期：禁用。浏览器不会给 disabled 的元素派发 click，
      // 所以"能不能点"这件事由这里一次定死，onCheckinDayClick 里再判也不迟。
      cell.disabled = true;
    }

    // 上一次补签失败的原因（例如"未持有补签卡"）就写在**这一格**的"未签"下面
    // （用户第 2 条）：提示跟着出问题的那天走，用户不用自己找是哪天。
    const note = state.checkinCardNote;
    const rejected = Boolean(note && day && day.date === note.date);

    // 休 / 班：右上角小标。文字来自后端（月视图的 rest_labels）。
    const restLabels = (view && view.rest_labels) || {};
    const restText = (day && day.rest_label) || restLabels[day && day.rest] || '';
    if (restText) {
      cell.classList.add(`is-rest-${day.rest || 'off'}`);
      cell.appendChild(elWith('span', 'cal-badge', restText));
    }

    cell.appendChild(elWith('span', 'cal-num', day && day.day ? String(day.day) : ''));
    if (day && day.festival) {
      cell.appendChild(elWith('span', 'cal-fest', day.festival));
    }
    if (day && day.lunar) {
      cell.appendChild(elWith('span', 'cal-lunar', day.lunar));
    }
    if (status && day.note) {
      cell.appendChild(elWith('span', 'cal-note', day.note));
    }
    if (rejected) {
      cell.classList.add('has-card-note');
      cell.appendChild(elWith('span', 'cal-card-note', note.text));
    }
    return cell;
  }

  function checkinSlot(cell) {
    const slot = document.createElement('td');
    slot.className = 'cal-slot';
    slot.appendChild(cell);
    return slot;
  }

  /** 按 view.days 的顺序铺进 7 列网格。 */
  function renderCheckinDays(view) {
    if (!el.checkinDays) return;
    el.checkinDays.replaceChildren();

    const days = (view && view.days) || [];
    let row = document.createElement('tr');
    let used = 0;

    days.forEach((day) => {
      if (used === WEEKDAY_COLUMNS) {
        el.checkinDays.appendChild(row);
        row = document.createElement('tr');
        used = 0;
      }
      if (!day || !day.date) {
        const slot = document.createElement('td');
        slot.className = 'cal-slot';
        slot.appendChild(elWith('div', 'cal-blank'));
        row.appendChild(slot);
      } else {
        row.appendChild(checkinSlot(checkinDayCell(day, view)));
      }
      used += 1;
    });

    if (used) el.checkinDays.appendChild(row);
  }

  function renderCheckinWeekdays(view) {
    if (!el.checkinWeekdays) return;
    el.checkinWeekdays.replaceChildren();
    ((view && view.weekdays) || []).forEach((name) => {
      el.checkinWeekdays.appendChild(elWith('th', 'cal-week', name));
    });
  }

  /** 月份标题、翻月按钮的可用状态 + 日期网格。

   *  刻意**没有**统计行（用户第 3 条：不要"本月已签 / 未签多少天"），
   *  也没有表格 caption（第 2 条：日历上不要那行小时间提醒）。
   *  失败提示也不在这里统一显示——它挂到出问题的那一格下面（见 checkinDayCell）。
   */
  function renderCheckinCalendar(view) {
    state.checkin = view || null;
    state.checkinMonth = (view && view.month) || null;

    setCheckinState('');
    if (el.checkinMonthLabel) el.checkinMonthLabel.textContent = (view && view.month_label) || '';

    // 翻月按钮：能不能翻由后端算（最早到一年前或首条记录那个月，最晚到本月）
    if (el.checkinPrev) el.checkinPrev.disabled = !view.can_prev;
    if (el.checkinNext) el.checkinNext.disabled = !view.can_next;

    renderCheckinWeekdays(view);
    renderCheckinDays(view);
    if (el.checkinGrid) el.checkinGrid.hidden = false;
  }

  function shiftCheckinMonth(step) {
    const view = state.checkin;
    // 按钮的禁用态已经挡住"翻不过去"，这里再判一次是防呆：
    // 万一日历还没取回来（view 为空）用户就点了，不该发出 month=undefined 的请求。
    if (!view) return;
    const target = step < 0 ? view.prev_month : view.next_month;
    const allowed = step < 0 ? view.can_prev : view.can_next;
    if (!target || !allowed) return;
    state.checkinCardNote = null;        // 换月了，上一月的提示不该跟着跑
    void loadCheckinMonth(target);
  }

  /* 补签确认（用户第 1 条：弹在**这个窗口里面**的小框，不要浏览器原生 confirm）。

     原生框有三个问题：长得跟页面无关、把整个窗口遮住、还要等它返回才继续。
     这里做成本窗口内的一层小框，用 Promise 等用户点哪个按钮。

     注意：卡够不够**不由这个框决定**——它只问"要不要花一张"，
     真正扣卡的是后端（没卡时会回"未持有补签卡"）。 */

  function openCheckinConfirm(text) {
    return new Promise((resolve) => {
      if (!el.checkinConfirm) {
        resolve(false);                  // 元素缺失时保守处理：当作取消
        return;
      }
      if (el.checkinConfirmText) el.checkinConfirmText.textContent = text;
      el.checkinConfirm.hidden = false;
      // 焦点给「确定」：键盘用户回车就能继续；按 Esc 走全局那条链（算取消）
      el.checkinConfirmOk.focus();
      el.checkinConfirmPending = resolve;
    });
  }

  /** 关掉确认框并回答。answer 为 true 表示"花这张卡"。 */
  function answerCheckinConfirm(answer) {
    const resolve = el.checkinConfirmPending;
    el.checkinConfirmPending = null;
    if (el.checkinConfirm) el.checkinConfirm.hidden = true;
    if (resolve) resolve(Boolean(answer));
  }

  function isCheckinConfirmOpen() {
    return Boolean(el.checkinConfirm && !el.checkinConfirm.hidden);
  }

  /** 弹一句确认："补签 2026-10-12 要消耗一张「补签卡」，现在补吗？" */
  function askMakeup(date) {
    const name = (state.checkin && state.checkin.card_name) || '';
    return openCheckinConfirm(`补签 ${date} 要消耗一张「${name}」，现在补吗？`);
  }

  /** 点某一天 → 签到。

   *  `makeup` 为真表示这是补签（点的是过去的日子）：先问一句，再交给后端；
   *  扣不扣得动卡由后端说了算，没卡时它回 400 + "未持有补签卡"，
   *  这里把这句话写到**那一格**下面（见 checkinDayCell 里的 checkinCardNote）。
   */
  async function checkinDay(date, makeup) {
    if (!date || state.checkinBusy) return;

    // 补签前先看手里还有没有卡（评价15 T1-5）：月视图里现成的 `cards`。
    // 一张都没有时弹确认框是**注定失败**的交互 —— 用户点"确定"，请求必被
    // 后端拒一次，然后才在格子上说"未持有补签卡"。不如直接说：少一次往返，
    // 也少一次"我明明点了确定却没补上"的困惑。
    if (makeup) {
      const left = Number((state.checkin && state.checkin.cards) || 0);
      if (left <= 0) {
        const name = (state.checkin && state.checkin.card_name) || '';
        const note = (state.checkin && state.checkin.card_note) || '';
        state.checkinCardNote = { date, text: note || `没有「${name}」了。` };
        if (state.checkin) renderCheckinCalendar(state.checkin);
        return;
      }
    }

    if (makeup && !(await askMakeup(date))) return;    // 用户点了取消

    state.checkinBusy = true;
    state.checkinCardNote = null;                      // 重新试一次，先清掉上次的提示
    try {
      const response = await apiFetch(`${API}/checkin`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId(), date, makeup: Boolean(makeup) }),
      });
      const data = await readJsonSafely(response);

      if (!response.ok || data.ok === false) {
        // 失败时后端**连整月视图一起回**（见 api/checkin_routes.py 的 _fail），
        // 就是为了把这句话挂到出问题的那一格下面。日期不在这张视图里
        // （比如翻月后又跨月失败）就退回窗口顶部那行文字，别让提示凭空消失。
        const reason = data.detail || `服务返回了错误（HTTP ${response.status}）。`;
        state.checkinCardNote = { date, text: reason };
        if (data.month && data.month.days) {
          renderCheckinCalendar(data.month);
        } else {
          state.checkinCardNote = null;
          setCheckinState(reason, true);
        }
        return;
      }

      // 成功后整月重画：补签会同时改动"这一天"和"连续天数"，
      // 让后端算一次、前端重画一次，比在两处分别打补丁更不容易漂移。
      renderCheckinCalendar(data.month);
    } catch (error) {
      // 连后端都没连上（fetch 抛错）：没有可挂提示的格子，退回那一行文字
      state.checkinCardNote = null;
      showCheckinFailure(checkinFailureText(error));
    } finally {
      state.checkinBusy = false;
    }
  }

  /** 事件委托：日期格是现画的，逐个绑定会在每次重画后失效。 */
  function onCheckinDayClick(event) {
    const cell = event.target && event.target.closest
      ? event.target.closest('.cal-day')
      : null;
    if (!cell || !cell.dataset || !cell.dataset.date) return;
    // data-makeup="1" 是 app.js 画格子时按后端的 past 标记的（不是自己算日期）
    void checkinDay(cell.dataset.date, cell.dataset.makeup === '1');
  }

  /* ------------------------------------------------------------ 初始化 */

  async function loadTools() {
    try {
      const response = await apiFetch(`${API}/tools`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const tools = Array.isArray(data.tools) ? data.tools : [];
      state.tools = tools;
      // /api/tools 只返回轮盘项（menu=true 的那些）
      state.menuTools = tools;
    } catch (_) {
      // 拿不到工具清单也不该让轮盘少一项：至少"对话"还能用
      state.tools = [];
      state.menuTools = [];
    }
    renderWheel();
  }

  function init() {
    startIdleLoop();
    // 第一次待机在 10 秒之后，所以这次请求通常来得及在它之前落地
    void loadPetActions();
    // 每一步都相互隔离：任一步出错也不该让整页瘫痪
    // （曾经因一个元素缺失抛错，导致后面的绑定与恢复全都没跑）。
    try {
      warnMissingElements();
    } catch (_) { /* 自检本身不该影响启动 */ }

    try {
      bindEvents();
    } catch (error) {
      console.error('[桌宠] 绑定事件失败：', error);
    }

    try {
      renderWheel();   // 立刻摆出"对话"，不必等接口返回
      renderChips();   // 胶囊是固定条目，也立刻渲染
    } catch (error) {
      console.error('[桌宠] 渲染入口失败：', error);
    }

    try {
      restorePosition();  // 恢复上次拖到的位置（没有则保持默认居中）
    } catch (error) {
      console.error('[桌宠] 恢复位置失败：', error);
    }

    sessionId();     // 预热会话 id
    loadTools();     // 用后端工具刷新轮盘（内部已自带兜底）

    try {
      fillDestinationOptions();  // 目的地候选（第 219 步，纯本地清单）
    } catch (error) {
      console.error('[桌宠] 目的地候选填充失败：', error);
    }
    // 初始状态：轮盘与聊天框都由 HTML 的 hidden 保持关闭，
    // 页面上只会看到桌宠本身。
  }

  init();
})();

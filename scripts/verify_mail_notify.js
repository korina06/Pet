/**
 * 桌宠报新邮件的行为验证：登入后自动查新、同一封只说一次、点气泡定位那封、
 * 静默时段只记账不冒泡、没登入时是哑的、退出后计时器停掉。
 *
 * 桩里的 setInterval 立即执行，所以这里测不到"每 5 分钟"，只测定时器一响之后的行为。
 */

'use strict';

const { buildDom, bootApp, settle } = require('./_dom_stub');

let failures = 0;
function check(label, ok, detail) {
  if (!ok) failures += 1;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? '  |  ' + detail : ''}`);
}

const INBOX_OLD = {
  folder: 'inbox',
  messages: [
    {
      uid: 41, mailbox: 'inbox', from_name: '旧邮件', from_address: 'old@example.com',
      subject: '上周的事', snippet: '', received: '2026-10-26T10:00:00+08:00',
      unread: false, flagged: false,
    },
  ],
  total: 1,
  has_more: false,
};

const NEW_MAIL = {
  uid: 42, mailbox: 'inbox', from_name: '小夏', from_address: 'xia@example.com',
  subject: '周末来吃饭吗？我做了红烧肉', snippet: '', received: '2026-10-28T02:00:00+08:00',
  unread: true, flagged: false,
};

/** 一份"已登入 + 收到一封新邮件"的 fetch 桩。 */
function mailFetch({ checkMessages, loggedIn = true, onCall = () => {} } = {}) {
  return async (url, options) => {
    const path = String(url).split('?')[0];
    onCall(path, options);
    const json = (body) => ({
      ok: true,
      status: 200,
      async json() { return body; },
    });

    if (path.endsWith('/mail/status')) {
      return json({ logged_in: loggedIn, account: loggedIn ? { address: 'me@example.com' } : null });
    }
    if (path.endsWith('/mail/folders')) {
      return json({
        folders: [
          { key: 'inbox', label: '收件箱', unread: 1 },
          { key: 'flagged', label: '星标邮件', unread: 0 },
        ],
      });
    }
    if (path.endsWith('/mail/messages')) return json(INBOX_OLD);
    if (path.endsWith('/mail/check')) {
      return json({
        folder: 'inbox',
        messages: checkMessages,
        total: 2,
        unread: checkMessages.length ? 1 : 0,
        reset: false,
      });
    }
    return json({});
  };
}

/** 打开邮箱窗口（气泡只在"登入之后"才可能冒，所以要先把窗口打开一次）。
 *
 *  入口在轮盘/胶囊里（`data-key="mail"`）。第 150 步之后邮箱在主界面不再占一格，
 *  所以两个容器都找一遍，哪个先有就用哪个。 */
async function openMailWindow(dom, byId) {
  const findEntry = () => {
    for (const key of ['menu-list', 'chat-chips']) {
      const node = byId.get(key);
      const items = (node && node.children) || [];
      const hit = items.find((item) => item.dataset && item.dataset.key === 'mail');
      if (hit) return hit;
    }
    return null;
  };
  const entry = findEntry();
  if (entry) entry.click();
  await settle(8);
}

/**
 * 把"现在"钉在**本地钟点** 14:00，并让气泡不自动收。
 *
 * 两个都是给测试留的口子（app.js 里注释写了为什么）：
 * - 时间不钉住，静默时段那条规则就只能在凌晨测绿；
 * - 不关掉自动收，桩里 `setTimeout` 会**立即**把气泡收起，"显示着的时候点它"测不到。
 *
 * 为什么按"本地分量"拼而不是传一个带时区的 ISO 串：`app.js` 的 `isQuietHour()` 读的是
 * `getHours()`，那是**本地钟点**。以前这里传 `'2026-10-28T14:00:00+08:00'`——是一个绝对
 * 时刻，在 UTC 的机器上换算成 06:00，正好落在静默时段（22:00–08:00）里，"白天该冒泡"
 * 就变成了"静默只记账"。GitHub 的 windows-latest 是 UTC、本机是 UTC+8：同一份代码
 * 本地绿、runner 红 7 条，全都是这一个原因（第 276 步）。用本地分量构造，
 * 任何时区下 `getHours()` 都返回 14。
 */
function pinDaytime(dom, hour = 14, minute = 0) {
  const at = new Date(2026, 9, 28, hour, minute, 0, 0).getTime();
  dom.windowStub.__petMailNow = () => at;
  dom.windowStub.__petNoteSticky = true;
}

/**
 * 失败时最想知道的三件事：气泡那两个节点到底建出来没有、自动收有没有被关掉、
 * 后台查新这一轮停在哪一步。三者的表现都是"气泡不出现"，但原因完全不同
 * （DOM 桩没建出节点 / 气泡被立刻收走 / 查新自己提前返回或抛错）。
 */
function diagnose(dom, tag) {
  const note = dom.byId.get('pet-note');
  const text = dom.byId.get('pet-note-text');
  const bound = typeof dom.windowStub.__petNoteDebug === 'function'
    ? dom.windowStub.__petNoteDebug()
    : '(app.js 没有暴露 __petNoteDebug)';
  const diag = dom.windowStub.__petMailDiag || '(app.js 没写 __petMailDiag)';
  console.log(
    `  诊断[${tag}] pet-note=${Boolean(note)} pet-note-text=${Boolean(text)}`
    + ` hidden=${note ? note.hidden : 'n/a'} sticky=${Boolean(dom.windowStub.__petNoteSticky)}`
    + ` app绑定=${JSON.stringify(bound)} 查新=${diag}`
  );
  return { note, text };
}

async function scenarioFirstTime() {
  console.log('\n[1] 登入后收到新邮件 → 桌宠冒一次气泡');
  const dom = buildDom();
  const { byId } = dom;
  let checks = 0;
  pinDaytime(dom);

  bootApp(dom, mailFetch({
    checkMessages: [NEW_MAIL],
    onCall: (path) => { if (path.endsWith('/mail/check')) checks += 1; },
  }));
  await settle(5);
  await openMailWindow(dom, byId);
  await settle(10);

  const note = byId.get('pet-note');
  const noteText = byId.get('pet-note-text');
  diagnose(dom, '场景1');
  check('查新真的发了 /mail/check', checks >= 1, `次数=${checks}`);
  // 先断结构，再断行为：节点不在时，"气泡没显示"这句话说不清是 app.js 的问题还是桩的问题。
  check('DOM 里同时有 #pet-note 与 #pet-note-text', Boolean(note) && Boolean(noteText));
  check('桌宠气泡显示出来了', Boolean(note) && note.hidden === false);
  check(
    '气泡里是发件人 + 主题（一封的形态）',
    Boolean(noteText) && /新邮件：小夏/.test(noteText.textContent) && /红烧肉/.test(noteText.textContent),
    noteText ? String(noteText.textContent) : '(空)'
  );

  // 点气泡 → 开邮箱窗口
  const mailModal = byId.get('mail-modal');
  if (mailModal) mailModal.hidden = true;
  note.click();
  await settle(24);
  check('点气泡之后气泡收起', note.hidden === true);
  check('点气泡之后邮箱窗口打开', Boolean(mailModal) && mailModal.hidden === false);

  // 列表里没有这一封（桩里列表是旧的）→ 要明说，而不是静默不动
  const state = byId.get('mail-state');
  check(
    '定位不到时明确说出来（不假装成功）',
    Boolean(state) && /不在这页/.test(String(state.textContent)),
    state ? String(state.textContent) : '(空)'
  );
}

async function scenarioSaysOnce() {
  console.log('\n[2] 同一封只说一次（再扫一轮不重复说）');
  const dom = buildDom();
  const { byId } = dom;
  let checks = 0;
  pinDaytime(dom);

  bootApp(dom, mailFetch({
    checkMessages: [NEW_MAIL],
    onCall: (path) => { if (path.endsWith('/mail/check')) checks += 1; },
  }));
  await settle(5);
  // 先手动把"已提醒 42"记进本地，模拟"上一轮说过"
  dom.windowStub.localStorage.setItem('pet.mail.notified', JSON.stringify([42]));
  dom.windowStub.localStorage.setItem('pet.mail.last_uid', '41');

  await openMailWindow(dom, byId);
  await settle(10);

  const note = byId.get('pet-note');
  check('确实发过查新', checks >= 1, `次数=${checks}`);
  check('已经说过的 UID 不再冒泡', Boolean(note) && note.hidden === true);
}

async function scenarioQuietHours() {
  console.log('\n[3] 静默时段：只记账不冒泡，天亮后合并成一句');
  const dom = buildDom();
  const { byId } = dom;

  // 把"现在"钉在本地 23:30（静默时段内）。app.js 通过 window.__petMailNow 取时间，
  // 就是给这个场景留的口子 —— 否则这条规则只能在凌晨才能测绿。
  // 同样按本地分量拼：这条用例断言的就是"本地钟点落在静默区间"，给绝对时刻会在
  // UTC 的机器上变成 15:30（非静默），整条用例失效（第 276 步）。
  const NIGHT = new Date(2026, 9, 28, 23, 30, 0, 0).getTime();
  dom.windowStub.__petMailNow = () => NIGHT;

  bootApp(dom, mailFetch({ checkMessages: [NEW_MAIL] }));
  await settle(5);
  await openMailWindow(dom, byId);
  await settle(10);

  const note = byId.get('pet-note');
  check('静默时段不冒泡', Boolean(note) && note.hidden === true);
  check(
    '但记了账（pending=1）',
    dom.windowStub.localStorage.getItem('pet.mail.pending') === '1',
    `pending=${dom.windowStub.localStorage.getItem('pet.mail.pending')}`
  );

  // 天亮之后（另开一份 DOM，时间设成 09:00），pending 应该被说出来
  const day = buildDom();
  const dayBy = day.byId;
  const MORNING = new Date(2026, 9, 29, 9, 0, 0, 0).getTime();
  day.windowStub.__petMailNow = () => MORNING;
  day.windowStub.__petNoteSticky = true;
  day.windowStub.localStorage.setItem('pet.mail.pending', '1');
  day.windowStub.localStorage.setItem('pet.mail.last_uid', '41');

  bootApp(day, mailFetch({ checkMessages: [] }));
  await settle(5);
  await openMailWindow(day, dayBy);
  await settle(10);

  const morningNote = dayBy.get('pet-note');
  const morningText = dayBy.get('pet-note-text');
  check(
    '天亮后把夜里攒的说出来',
    Boolean(morningNote) && morningNote.hidden === false
      && Boolean(morningText) && /夜里来了 1 封新邮件/.test(String(morningText.textContent)),
    morningText ? String(morningText.textContent) : '(空)'
  );
  check(
    '说过之后 pending 清零',
    day.windowStub.localStorage.getItem('pet.mail.pending') === '0',
    `pending=${day.windowStub.localStorage.getItem('pet.mail.pending')}`
  );
}

async function scenarioNotLoggedIn() {
  console.log('\n[4] 没登入时是哑的');
  const dom = buildDom();
  const { byId } = dom;
  pinDaytime(dom);
  bootApp(dom, mailFetch({ checkMessages: [NEW_MAIL], loggedIn: false }));
  await settle(5);
  await openMailWindow(dom, byId);
  await settle(10);

  const note = byId.get('pet-note');
  check('没登入不冒泡', Boolean(note) && note.hidden === true);
  const page = byId.get('mail-page-login');
  check('邮箱窗口停在登入页', Boolean(page) && page.hidden === false);
}

async function scenarioLogoutStopsWatcher() {
  console.log('\n[5] 退出登入 → 后台计时器停掉');
  const dom = buildDom();
  const { byId } = dom;
  pinDaytime(dom);
  let checksAfterLogout = 0;
  let loggedIn = true;

  const fetchImpl = mailFetch({
    checkMessages: [NEW_MAIL],
    onCall: (path) => {
      if (path.endsWith('/mail/logout')) loggedIn = false;
      if (path.endsWith('/mail/check') && !loggedIn) checksAfterLogout += 1;
    },
  });
  bootApp(dom, async (url, options) => {
    if (String(url).includes('/mail/status')) {
      return {
        ok: true,
        status: 200,
        async json() { return { logged_in: loggedIn, account: loggedIn ? { address: 'me@example.com' } : null }; },
      };
    }
    return fetchImpl(url, options);
  });
  await settle(5);
  await openMailWindow(dom, byId);
  await settle(10);

  const logoutButton = byId.get('mail-logout');
  check('页面上有「退出」按钮', Boolean(logoutButton));
  if (logoutButton) {
    logoutButton.click();
    await settle(10);
  }
  check(
    '退出之后后台不再查新',
    checksAfterLogout === 0,
    `退出后查新次数=${checksAfterLogout}`
  );
}

async function main() {
  // 诊断先行（第 273 步）：这条用例在别人机器上挂过一次，而本地怎么跑都绿。
  // 挂的时候最需要知道的是"当时环境长什么样"，而不是失败摘要。
  console.log(`环境：node ${process.version} | 平台 ${process.platform}`);
  await scenarioFirstTime();
  await scenarioSaysOnce();
  await scenarioQuietHours();
  await scenarioNotLoggedIn();
  await scenarioLogoutStopsWatcher();

  console.log('');
  if (failures) {
    console.log(`失败 ${failures} 条`);
    process.exitCode = 1;
  } else {
    console.log('全部通过');
  }
}

main();

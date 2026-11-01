/**
 * 聊天历史恢复的行为验证：让历史慢回来，断言用户先发的那条不消失、点了清空后历史不被画回来。
 *
 * 结构测试只能证明代码里出现过某些词，这里要证的是时序下的真实结果。
 */

'use strict';

const { buildDom, bootApp, settle } = require('./_dom_stub');

let failures = 0;
function check(label, ok, detail) {
  if (!ok) failures += 1;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? '  |  ' + detail : ''}`);
}

const HISTORY = [
  { role: 'user', content: '旧问题' },
  { role: 'assistant', content: '旧回答' },
];

/**
 * 造一个可控制的 fetch：
 * - `/api/tools` 立刻返回空清单；
 * - `/api/session/...` 返回一个**由调用方决定何时 resolve** 的 Promise；
 * - `/api/chat` 立刻返回一句话；
 * - `DELETE /api/sessions/...` 立刻成功。
 */
function makeFetch() {
  const pending = {};
  let resolveHistory;
  pending.history = new Promise((resolve) => { resolveHistory = resolve; });

  const impl = (url, options) => {
    const target = String(url);
    const method = (options && options.method) || 'GET';
    if (target.includes('/api/session/')) {
      return pending.history;
    }
    if (method === 'DELETE' && target.includes('/api/sessions/')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ status: 'cleared' }),
      });
    }
    if (method === 'POST' && target.includes('/api/chat')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ content: '嗯，我听着呢。', tool_invocations: [] }),
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ tools: [] }),
    });
  };

  return { impl, resolveHistory: (messages) => resolveHistory({
    ok: true,
    status: 200,
    json: () => Promise.resolve({ session_id: 's', messages }),
  }) };
}

function texts(log) {
  return log.children.map((node) => node.textContent);
}

/** 点轮盘里的某一项（打开对话 / 打开模型窗口都走它）。 */
function clickWheelItem(byId, key) {
  const item = byId.get('menu-list').children.find((node) => node.dataset.key === key);
  if (!item) throw new Error(`轮盘里没有 ${key} 这一项`);
  item.click();
  return item;
}

function sendUserMessage(byId, text) {
  byId.get('user-input').value = text;
  byId.get('composer').submit();
}

async function scenarioLateHistoryKeepsTheNewMessage() {
  console.log('\n=== 场景 1：历史慢回来，用户已经先说了一句 ===');
  const dom = buildDom();
  const transport = makeFetch();
  bootApp(dom, transport.impl);
  await settle();

  const chatLog = dom.byId.get('chat-log');
  clickWheelItem(dom.byId, 'chat');      // 打开对话 -> 发起历史请求（挂着）
  sendUserMessage(dom.byId, '我先说一句');
  await settle();

  const before = texts(chatLog);
  check('用户的消息先出现在界面上', before.includes('我先说一句'), JSON.stringify(before));

  transport.resolveHistory(HISTORY);      // 历史这时候才回来
  await settle();

  const after = texts(chatLog);
  check('历史回来时不许抹掉用户刚发的那条', after.includes('我先说一句'), JSON.stringify(after));
  check('历史插在最上面，用户那条排在它后面',
    after[0] === '旧问题'
      && after[1] === '旧回答'
      && after.indexOf('我先说一句') === 2,
    JSON.stringify(after));
  check('一共 4 条（2 条历史 + 用户 1 条 + 回复 1 条）', after.length === 4, String(after.length));
}

async function scenarioClearBeatsLateHistory() {
  console.log('\n=== 场景 2：历史慢回来，用户先清空了 ===');
  const dom = buildDom();
  const transport = makeFetch();
  bootApp(dom, transport.impl);
  await settle();

  const chatLog = dom.byId.get('chat-log');
  clickWheelItem(dom.byId, 'chat');       // 打开对话 -> 历史请求挂着
  sendUserMessage(dom.byId, '说点废话');
  await settle();

  dom.byId.get('chat-clear').click();     // 点 🗑
  const confirmOpen = !dom.byId.get('chat-confirm').hidden;
  check('🗑 弹出的是**窗口内**确认框', confirmOpen);
  dom.byId.get('chat-confirm-ok').click(); // 确认清空
  await settle();

  const cleared = texts(chatLog);
  check('清空后界面上没有刚才那句话', !cleared.includes('说点废话'), JSON.stringify(cleared));

  transport.resolveHistory(HISTORY);      // 清空之后历史才回来
  await settle();

  const after = texts(chatLog);
  check('清空之后晚到的历史**不许**再画回来',
    !after.includes('旧问题') && !after.includes('旧回答'), JSON.stringify(after));
}

async function scenarioHistoryArrivesFirst() {
  console.log('\n=== 场景 3：历史先回来（正常路径）===');
  const dom = buildDom();
  const transport = makeFetch();
  bootApp(dom, transport.impl);
  await settle();

  const chatLog = dom.byId.get('chat-log');
  clickWheelItem(dom.byId, 'chat');
  transport.resolveHistory(HISTORY);
  await settle();

  const after = texts(chatLog);
  check('整块恢复成历史', after[0] === '旧问题' && after[1] === '旧回答', JSON.stringify(after));
  check('一共 2 条', after.length === 2, String(after.length));
}

async function main() {
  await scenarioLateHistoryKeepsTheNewMessage();
  await scenarioClearBeatsLateHistory();
  await scenarioHistoryArrivesFirst();
  console.log(`\n失败 ${failures} 项`);
  process.exitCode = failures ? 1 : 0;
}

main().catch((error) => {
  console.error('验证脚本自身出错：', error);
  process.exitCode = 1;
});

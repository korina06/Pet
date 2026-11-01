/**
 * 聊天流式的行为验证：delta 往气泡里长、reset 清空、meta 用服务端完整回答覆盖，
 * 且走的是 `/api/chat/stream` 而不是旧的 `/api/chat`。
 */

'use strict';

const { buildDom, bootApp, settle } = require('./_dom_stub');

let failures = 0;
function check(label, ok, detail) {
  if (!ok) failures += 1;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? '  |  ' + detail : ''}`);
}

/** 一个够用的 ReadableStream 替身：按块吐出字符串。 */
function makeBody(chunks) {
  let index = 0;
  return {
    getReader() {
      return {
        async read() {
          if (index >= chunks.length) return { done: true, value: undefined };
          const value = new TextEncoder().encode(chunks[index]);
          index += 1;
          return { done: false, value };
        },
        async cancel() {},
      };
    },
  };
}

function makeFetch() {
  const seen = [];
  const frames = [
    { type: 'delta', text: '我查' },
    { type: 'delta', text: '一下' },
    { type: 'reset' },                       // 这一轮要调工具
    { type: 'delta', text: '查好了：' },
    { type: 'delta', text: '有 3 道' },
    {
      type: 'meta',
      payload: { session_id: 's', content: '查好了：有 3 道', tool_invocations: [], steps: 1 },
    },
  ];
  const text = frames.map((f) => 'data: ' + JSON.stringify(f) + '\n\n');
  // 拆成不规则的多块，模拟真实的网络分片（同时验证 buffer 拼接）
  const chunks = [text.slice(0, 2).join(''), text.slice(2, 4).join(''), text.slice(4).join('')];

  const impl = (url, options) => {
    const target = String(url);
    seen.push(`${(options && options.method) || 'GET'} ${target}`);
    if (target.includes('/api/chat/stream')) {
      return Promise.resolve({ ok: true, status: 200, body: makeBody(chunks) });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ tools: [] }),
    });
  };
  return { impl, seen };
}

async function main() {
  console.log('=== 场景：聊天走 SSE（delta -> reset -> delta -> meta）===');
  const dom = buildDom();
  const transport = makeFetch();
  bootApp(dom, transport.impl);
  await settle();

  const chatLog = dom.byId.get('chat-log');
  const input = dom.byId.get('user-input');
  input.value = '今天吃什么';
  dom.byId.get('composer').submit();
  await settle(8);

  const bubbles = chatLog.children.map((node) => node.textContent);

  check(
    '打到 /api/chat/stream',
    transport.seen.some((line) => line.includes('/api/chat/stream')),
    JSON.stringify(transport.seen)
  );
  check('用户那句先出现', bubbles[0] === '今天吃什么', JSON.stringify(bubbles));
  check(
    'reset 之后气泡里没有半句废话',
    !bubbles.some((text) => text === '我查一下'),
    JSON.stringify(bubbles)
  );
  check(
    'meta 覆盖后的完整回答在气泡里',
    bubbles.some((text) => text === '查好了：有 3 道'),
    JSON.stringify(bubbles)
  );

  console.log(`\n失败 ${failures} 项`);
  process.exitCode = failures ? 1 : 0;
}

main().catch((error) => {
  console.error('验证脚本自身出错：', error);
  process.exitCode = 1;
});

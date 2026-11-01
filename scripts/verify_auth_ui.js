/**
 * 鉴权前端的验证脚本。
 *
 * 用共享 DOM 桩真跑 app.js：没有 token 时不带 Authorization（单机模式行为不变）、
 * 有 token 时所有请求带 Bearer、401 会弹一次输入框并在填完后自动重试。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { buildDom, bootApp, ROOT } = require('./_dom_stub');

const APP_JS = fs.readFileSync(path.join(ROOT, 'static', 'app.js'), 'utf8');
const TOKEN_KEY = 'karo.authToken';

async function flush(rounds = 20) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

/** 在桩树里按 class 找元素（documentStub 没有 querySelector）。 */
function findByClass(node, className) {
  if (!node) return null;
  if (String(node.className || '').split(/\s+/).includes(className)) return node;
  for (const child of node.children || []) {
    const hit = findByClass(child, className);
    if (hit) return hit;
  }
  return null;
}

function findByTag(node, tagName) {
  if (!node) return null;
  if (String(node.tagName || '').toUpperCase() === tagName.toUpperCase()) return node;
  for (const child of node.children || []) {
    const hit = findByTag(child, tagName);
    if (hit) return hit;
  }
  return null;
}

function jsonResponse(data, status = 200, extraHeaders = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name) => {
        const key = String(name || '').toLowerCase();
        return Object.prototype.hasOwnProperty.call(extraHeaders, key)
          ? extraHeaders[key]
          : null;
      },
    },
    body: null,
    async json() {
      return data;
    },
    async text() {
      return JSON.stringify(data);
    },
  };
}

function headersOf(init) {
  const raw = (init && init.headers) || {};
  const out = {};
  Object.keys(raw).forEach((key) => {
    out[String(key).toLowerCase()] = raw[key];
  });
  return out;
}

async function main() {
  const failures = [];
  async function check(name, fn) {
    try {
      await fn();
      console.log(`  ✓ ${name}`);
    } catch (error) {
      failures.push(`${name}：${error.message}`);
      console.log(`  ✗ ${name}：${error.message}`);
    }
  }

  // 结构：源码里必须有 token 读写与统一包装，否则行为测试可能是别的东西在起作用
  await check('app.js 里所有 API 调用都走 apiFetch（没有漏网的 fetch）', () => {
    assert.ok(APP_JS.includes('function apiFetch('), '缺少 apiFetch');
    assert.ok(APP_JS.includes(TOKEN_KEY), '缺少 token 键');
    const offenders = APP_JS.split('\n')
      .map((line, index) => ({ line, index: index + 1 }))
      .filter(({ line }) => /(^|[^.\w])fetch\(/.test(line))
      .filter(({ line }) => !line.includes('nativeFetch') && !line.includes('function apiFetch'));
    assert.deepStrictEqual(
      offenders.map((item) => item.index),
      [],
      `还有直接调用 fetch 的地方：${offenders.map((item) => item.index).join(', ')}`
    );
  });

  // 1) 没有 token：不加头（本机模式行为不变）
  await check('没有 token 时不加 Authorization', async () => {
    const dom = buildDom();
    const calls = [];
    const fetchImpl = async (url, init) => {
      calls.push({ url: String(url), headers: headersOf(init) });
      if (String(url).includes('/pet/actions')) return jsonResponse({ actions: [] });
      return jsonResponse({ tools: [] });
    };
    bootApp(dom, fetchImpl);
    await flush();

    const apiCalls = calls.filter((call) => call.url.startsWith('/api'));
    assert.ok(apiCalls.length >= 1, '启动时应当至少请求一次 /api');
    apiCalls.forEach((call) => {
      assert.strictEqual(call.headers.authorization, undefined, `${call.url} 不该带 token`);
    });
  });

  // 2) 已有 token：每个 /api 请求都带上
  await check('localStorage 里有 token 时所有 /api 都带 Bearer', async () => {
    const dom = buildDom();
    dom.windowStub.localStorage.setItem(TOKEN_KEY, 'token-abc');
    const calls = [];
    const fetchImpl = async (url, init) => {
      calls.push({ url: String(url), headers: headersOf(init) });
      if (String(url).includes('/pet/actions')) return jsonResponse({ actions: [] });
      return jsonResponse({ tools: [] });
    };
    bootApp(dom, fetchImpl);
    await flush();

    const apiCalls = calls.filter((call) => call.url.startsWith('/api'));
    assert.ok(apiCalls.length >= 1);
    apiCalls.forEach((call) => {
      assert.strictEqual(call.headers.authorization, 'Bearer token-abc', `${call.url} 缺 token`);
    });
    // <img src> 带不了 Authorization 头，只能靠 cookie；启动时就要同步好
    assert.ok(
      String(dom.documentStub.cookie || '').includes('karo_auth=token-abc'),
      '启动时要把已存的 token 同步成 cookie，否则缩略图/路线图仍会 401'
    );
  });

  // 3) 401 → 浮层 → 输入 → 重试并记住
  await check('401 时弹输入框，填完自动重试并记住 token', async () => {
    const dom = buildDom();
    const calls = [];
    let deniedOnce = true;
    const fetchImpl = async (url, init) => {
      calls.push({ url: String(url), headers: headersOf(init) });
      if (String(url).includes('/pet/actions')) return jsonResponse({ actions: [] });
      if (String(url).includes('/api/tools')) {
        if (deniedOnce) {
          deniedOnce = false;
          // 只有带这个头的 401 才算「缺令牌」—— 邮箱登录失败那类 401 没有它
          return jsonResponse({ detail: 'missing token' }, 401, {
            'x-auth-required': '1',
          });
        }
        return jsonResponse({ tools: [] });
      }
      return jsonResponse({});
    };
    bootApp(dom, fetchImpl);
    await flush();

    const overlay = findByClass(dom.documentStub.body, 'auth-overlay');
    assert.ok(overlay, '401 后应当出现输入浮层');
    const input = findByTag(overlay, 'input');
    const button = findByTag(overlay, 'button');
    assert.ok(input && button, '浮层里要有输入框和按钮');

    input.value = 'secret-token';
    button.click();
    await flush();

    assert.ok(
      String(dom.documentStub.cookie || '').includes('karo_auth=secret-token'),
      '填完 token 也要写 cookie，<img> 才能跟着带上'
    );
    assert.strictEqual(
      dom.windowStub.localStorage.getItem(TOKEN_KEY),
      'secret-token',
      'token 要存进 localStorage'
    );
    const toolsCalls = calls.filter((call) => call.url.includes('/api/tools'));
    assert.strictEqual(toolsCalls.length, 2, '应当只重试一次');
    assert.strictEqual(
      toolsCalls[1].headers.authorization,
      'Bearer secret-token',
      '重试必须带上新 token'
    );
    assert.strictEqual(
      findByClass(dom.documentStub.body, 'auth-overlay'),
      null,
      '填完之后浮层要收起来'
    );
  });

  if (failures.length) {
    console.log(`\n鉴权前端：${failures.length} 条失败`);
    failures.forEach((item) => console.log(`  - ${item}`));
    process.exitCode = 1;
    return;
  }
  console.log('\n鉴权前端：全部通过');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

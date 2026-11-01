/**
 * 出行对话的行为验证：默认停在「聊聊」页、发一句真的打到 `/api/trip/chat`、
 * 未查到的地名会多出一条提醒、503 与断网都说人话、空输入与重复点击不发请求、两个标签能切。
 */

'use strict';

const { buildDom, bootApp, settle } = require('./_dom_stub');

let failures = 0;
function check(label, ok, detail) {
  if (!ok) failures += 1;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? '  |  ' + detail : ''}`);
}

/** 造一个 fetch 桩：只认 /trip/chat，其余请求一律给空 JSON。 */
function tripFetch({ reply = '第一天：苏州博物馆 → 拙政园。', suspects = [], status = 200,
                     routes = [], throwOnTrip = false, onCall = () => {} } = {}) {
  return async (url, options) => {
    const path = String(url).split('?')[0];
    if (path.endsWith('/trip/chat')) {
      onCall(options);
      if (throwOnTrip) throw new Error('Failed to fetch');
      if (status !== 200) {
        return { ok: false, status, async json() { return { detail: '不行' }; } };
      }
      return {
        ok: true,
        status: 200,
        async json() { return { content: reply, suspects, routes, tools_used: [] }; },
      };
    }
    return { ok: true, status: 200, async json() { return {}; } };
  };
}

/** 打开出行窗口（入口在快捷胶囊里，`data-key="travel"`）。 */
async function openTravelWindow(dom) {
  const chips = dom.byId.get('chat-chips');
  const entry = (chips && chips.children || [])
    .find((item) => item.dataset && item.dataset.key === 'travel');
  if (entry) entry.click();
  await settle(10);
}

/** 输入一句话并提交。 */
async function say(dom, text) {
  const input = dom.byId.get('travel-chat-input');
  const form = dom.byId.get('travel-chat-form');
  input.value = text;
  form.submit();
  await settle(14);
}

/** 出行对话里所有气泡的文本（按出现顺序）。 */
function bubbles(dom) {
  const log = dom.byId.get('travel-chat-log');
  return ((log && log.children) || []).map((node) => String(node.textContent));
}

async function scenarioDefaultPage() {
  console.log('\n[1] 打开出行窗口 → 默认在「聊聊」');
  const dom = buildDom();
  const { byId } = dom;
  bootApp(dom, tripFetch());
  await settle(6);
  await openTravelWindow(dom);

  const modal = byId.get('travel-modal');
  check('窗口打开了', Boolean(modal) && modal.hidden === false);
  check('聊天页显示', byId.get('travel-page-chat').hidden === false);
  check('表单页收起来', byId.get('travel-page-form').hidden === true);
  check(
    '「聊聊」标签高亮',
    byId.get('travel-tab-chat').getAttribute('aria-selected') === 'true',
    String(byId.get('travel-tab-chat').getAttribute('aria-selected'))
  );
  check(
    '「自己填」不再高亮',
    byId.get('travel-tab-form').getAttribute('aria-selected') === 'false'
  );
  check('一开始没有任何气泡', bubbles(dom).length === 0, bubbles(dom).join(' / '));
}

async function scenarioAskAndAnswer() {
  console.log('\n[2] 说一句 → 真的请求了 /trip/chat，界面出现两段话');
  const dom = buildDom();
  const { byId } = dom;
  const bodies = [];
  bootApp(dom, tripFetch({
    onCall: (options) => bodies.push(JSON.parse(options.body)),
  }));
  await settle(6);
  await openTravelWindow(dom);
  await say(dom, '下周末去苏州，两天，想少走路');

  check('发了一次 /trip/chat', bodies.length === 1, `次数=${bodies.length}`);
  check('body 里是原话', bodies[0] && bodies[0].message === '下周末去苏州，两天，想少走路');
  check('body 里带 session_id', Boolean(bodies[0] && bodies[0].session_id));

  const texts = bubbles(dom);
  check('界面里有两条（我说的 + 它答的）', texts.length === 2, texts.join(' / '));
  check('我说的话原样在上', texts[0] === '下周末去苏州，两天，想少走路', texts[0]);
  check('它答的是服务端那段', texts[1] === '第一天：苏州博物馆 → 拙政园。', texts[1]);
  check('输入框清空了', byId.get('travel-chat-input').value === '');
  check('发送按钮恢复可用', byId.get('travel-chat-send').disabled === false);
}

async function scenarioSuspectsWarning() {
  console.log('\n[3] 后端说「这名字没查到」→ 界面多一条提醒');
  const dom = buildDom();
  bootApp(dom, tripFetch({ suspects: ['虎丘塔', '寒山寺'] }));
  await settle(6);
  await openTravelWindow(dom);
  await say(dom, '苏州两天');

  const texts = bubbles(dom);
  check('多出一条提醒', texts.length === 3, texts.join(' / '));
  check(
    '提醒里点名了那几个地方',
    /虎丘塔/.test(texts[2]) && /寒山寺/.test(texts[2]),
    texts[2]
  );
  check('提醒说的是"没查到"而不是"编造"', /没有查到/.test(texts[2]), texts[2]);
}

async function scenarioNoSuspectsNoNoise() {
  console.log('\n[4] 没有可疑名字 → 不多嘴');
  const dom = buildDom();
  bootApp(dom, tripFetch({ suspects: [] }));
  await settle(6);
  await openTravelWindow(dom);
  await say(dom, '苏州两天');
  check('只有两条气泡', bubbles(dom).length === 2, bubbles(dom).join(' / '));
}

async function scenarioServiceUnavailable() {
  console.log('\n[5] 后端没接好（503）→ 说人话');
  const dom = buildDom();
  const { byId } = dom;
  bootApp(dom, tripFetch({ status: 503 }));
  await settle(6);
  await openTravelWindow(dom);
  await say(dom, '苏州两天');

  const texts = bubbles(dom);
  check('先是我说的，再是解释', texts.length === 2, texts.join(' / '));
  check('解释了没准备好', /没准备好/.test(texts[1]), texts[1]);
  check('发送按钮恢复可用', byId.get('travel-chat-send').disabled === false);
}

async function scenarioNetworkError() {
  console.log('\n[6] 网络断了 → 说"没连上"，不抛异常');
  const dom = buildDom();
  const { byId } = dom;
  bootApp(dom, tripFetch({ throwOnTrip: true }));
  await settle(6);
  await openTravelWindow(dom);
  await say(dom, '苏州两天');

  const texts = bubbles(dom);
  check('有一条解释', texts.length === 2, texts.join(' / '));
  check('明确说没连上', /没连上/.test(texts[1]), texts[1]);
  check('发送按钮恢复可用', byId.get('travel-chat-send').disabled === false);
}

async function scenarioEmptyInput() {
  console.log('\n[7] 空输入不发请求');
  const dom = buildDom();
  const { byId } = dom;
  let calls = 0;
  bootApp(dom, tripFetch({ onCall: () => { calls += 1; } }));
  await settle(6);
  await openTravelWindow(dom);
  await say(dom, '   ');
  check('没有发请求', calls === 0, `次数=${calls}`);
  check('也没有气泡', bubbles(dom).length === 0, bubbles(dom).join(' / '));
  check('输入框还是空的', byId.get('travel-chat-input').value === '');
}

async function scenarioNoDoubleSend() {
  console.log('\n[8] 上一句还没回来 → 再点发送也只发一次');
  const dom = buildDom();
  let calls = 0;
  // 一个"永远不回"的 fetch：模拟慢请求
  const hanging = (url, options) => {
    const path = String(url).split('?')[0];
    if (path.endsWith('/trip/chat')) {
      calls += 1;
      return new Promise(() => {});
    }
    return Promise.resolve({ ok: true, status: 200, async json() { return {}; } });
  };
  bootApp(dom, hanging);
  await settle(6);
  await openTravelWindow(dom);

  const input = dom.byId.get('travel-chat-input');
  const form = dom.byId.get('travel-chat-form');
  input.value = '第一句';
  form.submit();
  await settle(6);
  input.value = '第二句';
  form.submit();
  await settle(6);

  check('只发了一次', calls === 1, `次数=${calls}`);
  check(
    '等待中发送按钮是禁用的',
    dom.byId.get('travel-chat-send').disabled === true
  );
}

async function scenarioTabSwitch() {
  console.log('\n[9] 两个标签能来回切');
  const dom = buildDom();
  const { byId } = dom;
  bootApp(dom, tripFetch());
  await settle(6);
  await openTravelWindow(dom);

  byId.get('travel-tab-form').click();
  await settle(4);
  check('切到「自己填」：表单显示', byId.get('travel-page-form').hidden === false);
  check('切到「自己填」：聊天收起', byId.get('travel-page-chat').hidden === true);
  check(
    '「自己填」标签高亮',
    byId.get('travel-tab-form').getAttribute('aria-selected') === 'true'
  );

  byId.get('travel-tab-chat').click();
  await settle(4);
  check('切回「聊聊」：聊天显示', byId.get('travel-page-chat').hidden === false);
  check('切回「聊聊」：表单收起', byId.get('travel-page-form').hidden === true);
}

/** 找出对话里所有「路线图」节点（第 227 步）。 */
function maps(dom) {
  const log = dom.byId.get('travel-chat-log');
  return ((log && log.children) || []).filter((node) => node.className === 'msg-travel-map');
}

async function scenarioRouteMap() {
  console.log('\n[10] 查过路线 → 对话里贴出地图');
  const dom = buildDom();
  const { byId } = dom;
  bootApp(dom, tripFetch({
    reply: '南京到上海，开车约 144 公里。',
    routes: [
      { mode: 'driving', url: '/api/map/route.png?origin=118.79,32.06&destination=121.47,31.23&mode=driving', text: '驾车约 144.0 公里，约 102 分钟' },
      { mode: 'walking', url: '/api/map/route.png?origin=1,1&destination=2,2&mode=walking', text: '' },
    ],
  }));
  await settle(6);
  await openTravelWindow(dom);
  await say(dom, '南京到上海开车多远');

  const figures = maps(dom);
  check('两张图都贴出来了', figures.length === 2, `实际 ${figures.length}`);
  const first = figures[0];
  const img = (first && first.children || []).find((node) => node.tagName === 'IMG');
  check('图用的是服务端给的地址', Boolean(img) && img.src.startsWith('/api/map/route.png?'));
  check('带 alt（读屏能念出来）', Boolean(img) && img.alt === '路线图');
  check('懒加载（聊天里不该一次拉全部）', Boolean(img) && img.loading === 'lazy');
  const caption = (first && first.children || []).find((node) => node.tagName === 'FIGCAPTION');
  check('图注是工具那句人话', Boolean(caption) && caption.textContent.includes('144.0 公里'));
  const second = figures[1];
  const secondCaption = (second && second.children || []).find((node) => node.tagName === 'FIGCAPTION');
  check('没有图注的就不硬加一行空字', !secondCaption);
}

async function scenarioRouteMapBroken() {
  console.log('\n[11] 图没画出来 → 换一句人话，不留空框');
  const dom = buildDom();
  const { byId } = dom;
  bootApp(dom, tripFetch({
    routes: [{ mode: 'driving', url: '/api/map/route.png?x=1', text: '驾车约 10 公里' }],
  }));
  await settle(6);
  await openTravelWindow(dom);
  await say(dom, '南京到上海开车多远');

  const figure = maps(dom)[0];
  const img = (figure && figure.children || []).find((node) => node.tagName === 'IMG');
  check('先有图才有得坏', Boolean(img));
  if (img) img.emit('error');
  check('坏图标上类名', Boolean(figure) && figure.className.includes('travel-map--broken'));
  check('破图被移走', !(figure.children || []).some((node) => node.tagName === 'IMG'));
  const note = (figure && figure.children || []).find((node) => node.className === 'travel-map-note');
  check(
    '换成一句人话且不猜原因',
    Boolean(note) &&
      note.textContent.includes('没加载出来') &&
      !note.textContent.includes('配额') &&
      !note.textContent.includes('额度')
  );
}

async function scenarioNoRoutesNoMap() {
  console.log('\n[12] 没查路线 → 一张图都不贴');
  const dom = buildDom();
  const { byId } = dom;
  bootApp(dom, tripFetch({ routes: [] }));
  await settle(6);
  await openTravelWindow(dom);
  await say(dom, '苏州两天怎么玩');
  check('没有 routes 就没有图', maps(dom).length === 0);
}

async function main() {
  await scenarioDefaultPage();
  await scenarioAskAndAnswer();
  await scenarioSuspectsWarning();
  await scenarioNoSuspectsNoNoise();
  await scenarioServiceUnavailable();
  await scenarioNetworkError();
  await scenarioEmptyInput();
  await scenarioNoDoubleSend();
  await scenarioTabSwitch();
  await scenarioRouteMap();
  await scenarioRouteMapBroken();
  await scenarioNoRoutesNoMap();

  console.log('');
  if (failures === 0) {
    console.log('全部通过');
  } else {
    console.log(`${failures} 条失败`);
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

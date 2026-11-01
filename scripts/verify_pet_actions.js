/**
 * 桌宠动作菜单的行为验证：轮盘里有「动作」项、点开是竖排菜单、点项执行并收起、Esc 与点外面也能收。
 *
 * 抖动类样式在桩里观察不到，那部分交给真浏览器烟测。
 */

'use strict';

const { buildDom, bootApp, settle } = require('./_dom_stub');

let failures = 0;
function check(label, ok, detail) {
  if (!ok) failures += 1;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? '  |  ' + detail : ''}`);
}

function makeFetch() {
  return async () => ({
    ok: true,
    status: 200,
    async json() {
      return {};
    },
  });
}

async function main() {
  const dom = buildDom();
  const { byId } = dom;
  const el = (id) => byId.get(id) || null;

  bootApp(dom, makeFetch());
  await settle(5);

  const menu = el('action-menu');
  const idleRef = el('pet-idle');
  const pet = el('pet');
  const petButton = el('pet-button');
  const menuList = el('menu-list');

  check('页面上有动作菜单容器', Boolean(menu));

  // 轮盘里那一项：第 150 步把「状态」换成了「动作」
  const wheelItems = (menuList && menuList.children) || [];
  const actionsItem = wheelItems.find((b) => b.dataset.key === 'actions');
  check('轮盘里有「动作」一项', Boolean(actionsItem),
    `项=${wheelItems.map((b) => b.dataset.key).join(',')}`);
  if (!actionsItem) {
    console.log(`\n通过 部分断言，失败 ${failures} 项`);
    process.exit(1);
  }

  // ---- 点「动作」-> 菜单弹出 ----
  //
  // 这里要**模拟浏览器的冒泡**：真实点击会先走按钮自己的处理函数，然后冒泡到 document。
  // 桩不会自动冒泡，而 app.js 的"点外面关菜单"正好挂在 document 上 —— 不平移这一步，
  // "菜单刚开就被自己关掉"这类 bug 在桩里永远看不见（我这次就栽在这里）。
  const bubbleToDocument = () => {
    const handlers = (dom.documentStub._handlers || {}).click || [];
    handlers.forEach((handler) => handler({ target: actionsItem, stopPropagation() {} }));
  };

  actionsItem.click();
  const stopped = actionsItem._stopped === true;
  if (!stopped) bubbleToDocument();        // 没挡住冒泡才交给 document（与浏览器一致）
  await settle(3);
  check('点「动作」后菜单出现', menu.hidden === false);
  check('打开菜单时挡住了冒泡（否则会被"点外面"关掉）', stopped,
    `stopPropagation ${stopped ? 'called' : 'missing'}`);
  // 动作池会一直加（喝茶/摸鱼/擦擦灰……），所以**不写死数量**：
  // 只要求"至少有动作、每项都带 data-action"（写死数量的坑已踩三次）。
  check('菜单里至少有动作', menu.children.length >= 2,
    `项数=${menu.children.length}`);
  const items = menu.children;
  // 「擦擦灰」是视频素材（id=wipe）
  const dustItem = items.find((item) => item.dataset.action === 'wipe');
  check('有「擦擦灰」这一项', Boolean(dustItem),
    items.map((item) => item.dataset.action).join(','));
  check('动作项文案含「擦擦灰」',
    Boolean(dustItem) && String(dustItem.textContent).includes('擦擦灰'),
    dustItem ? String(dustItem.textContent) : '无');
  const labels = items.map((item) => String(item.textContent));
  check('菜单里有「喝茶」', labels.some((text) => text.includes('喝茶')),
    labels.join(','));
  check('所有动作项都带 data-action',
    items.every((item) => Boolean(item.dataset.action)),
    items.map((item) => item.dataset.action).join(','));
  const firstItem = dustItem || items[0];

  // ---- 点动作 -> 执行 + 收起 ----
  firstItem.click();
  await settle(3);
  check('点动作后菜单收起', menu.hidden === true);
  // 桩里定时器是同步的：点完可能立刻被待机链再演一次，所以看"手动点过哪个"
  check('动作真的执行了（手动点过 wipe）',
    Boolean(pet) && pet.dataset.lastManual === 'wipe',
    pet ? String(pet.dataset.lastManual) : '无');

  // ---- Esc 收起 ----
  actionsItem.click({ stopPropagation() {} });
  await settle(2);
  check('再点「动作」能再次弹出', menu.hidden === false);
  dom.documentStub.pressEscape();
  await settle(2);
  check('Esc 也能收起动作菜单', menu.hidden === true);

  // ---- 点外面收起 ----
  actionsItem.click({ stopPropagation() {} });
  await settle(2);
  const outside = el('chat-log');
  check('点外面之前菜单是开着的', menu.hidden === false);
  // 桩没有 dispatch：直接调登记在 document 上的 click 处理器
  // （app.js 的"点外面关菜单"就是挂在 document 上的）
  const clickHandlers = (dom.documentStub._handlers || {}).click || [];
  check('document 上确实登记了 click 处理（点外面关菜单）', clickHandlers.length > 0,
    `处理器 ${clickHandlers.length} 个`);
  clickHandlers.forEach((handler) => handler({ target: outside, stopPropagation() {} }));
  await settle(2);
  check('点菜单外面能收起', menu.hidden === true);

  check('桌宠按钮仍在（动作没有把页面弄坏）', Boolean(petButton));

  // ---- 拖拽姿势（第 163 步）：不在菜单里，拖动时才循环播 ----
  const petButtonEl = el('pet-button');
  const idleEl = el('pet-idle');
  const fire = (type, init) => {
    (petButtonEl._handlers[type] || []).forEach((handler) => handler(init));
  };

  idleEl.src = '';
  idleEl.loop = false;
  fire('pointerdown', { button: 0, clientX: 100, clientY: 100, pointerId: 1 });
  // 位移超过阈值（DRAG_THRESHOLD = 6）才算真拖
  fire('pointermove', { clientX: 140, clientY: 160, pointerId: 1 });
  // 桩里定时器是**同步**的：startDragPose 里那条 3.2 秒兜底计时器会立刻触发，
  // 所以这里观察不到"先 lift 再 swing"的先后 —— 那是真浏览器（烟测）才能验的事。
  // 桩里能钉的是：拖拽期间动作层确实指向拖拽素材、以及松手后能停干净。
  check('拖动时播的是拖拽素材', String(idleEl.src).indexOf('drag-') >= 0,
    String(idleEl.src));

  // 无论走 ended 还是兜底计时器，最终都应当落在「来回荡」并循环
  (idleEl._handlers.ended || []).forEach((handler) => handler({}));
  check('提起播完后切到「来回荡」', String(idleEl.src).indexOf('drag-swing.webm') >= 0,
    String(idleEl.src));
  check('「来回荡」是循环的（长时间按着也不回地面）', idleEl.loop === true,
    `loop=${idleEl.loop}`);

  fire('pointerup', { clientX: 140, clientY: 160, pointerId: 1 });
  check('松手后不再循环', idleEl.loop === false, `loop=${idleEl.loop}`);
  check('松手后回到静帧（动作层收起）',
    Boolean(pet) && !pet.classList.contains('is-idle-playing'));

  // ---- 抛掷（第 171 步）：甩出去要飞一段再停，且不许飞出屏幕 ----
  // 桩里 CSS 变量是空实现（setProperty 不做任何事），读不到坐标；
  // 所以用 data-threw 标记 + 落盘位置（localStorage）来验。
  //: 桩里 `local` 是个 Map（不是 localStorage 接口），直接 get 就行
  const savedPos = () => {
    const raw = dom.local.get('pet.position');
    return raw ? JSON.parse(raw) : null;
  };

  pet.dataset.threw = '0';
  fire('pointerdown', { button: 0, clientX: 400, clientY: 300, pointerId: 1 });
  fire('pointermove', { clientX: 401, clientY: 300, pointerId: 1 });
  fire('pointerup', { clientX: 401, clientY: 300, pointerId: 1 });
  check('轻轻放下不抛掷', pet.dataset.threw === '0', `threw=${pet.dataset.threw}`);
  check('放下后位置被保存', Boolean(savedPos()));

  pet.dataset.threw = '0';
  fire('pointerdown', { button: 0, clientX: 400, clientY: 300, pointerId: 1 });
  fire('pointermove', { clientX: 520, clientY: 380, pointerId: 1 });
  fire('pointerup', { clientX: 520, clientY: 380, pointerId: 1 });
  check('甩一下会抛出去', Number(pet.dataset.threw) >= 1, `threw=${pet.dataset.threw}`);

  const landed = savedPos();
  check('抛完落在屏幕内',
    Boolean(landed)
      && landed.x >= 0 && landed.x <= dom.windowStub.innerWidth
      && landed.y >= 0 && landed.y <= dom.windowStub.innerHeight,
    landed ? `${Math.round(landed.x)},${Math.round(landed.y)}` : '无记录');

  console.log(`\n通过 ${failures === 0 ? '全部' : '部分'}断言，失败 ${failures} 项`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  console.error('脚本异常：', error);
  process.exit(1);
});

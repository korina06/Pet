/**
 * 待机动画的行为验证：待机层存在、定时器排上之后开始播、播完把层收掉。
 *
 * 时间相关的断言靠桩把 setTimeout 立即执行来观察，节奏本身由真机和烟测兜底。
 */

'use strict';

const { buildDom, bootApp, settle } = require('./_dom_stub');

let failures = 0;
function check(label, ok, detail) {
  if (!ok) failures += 1;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? '  |  ' + detail : ''}`);
}

function makeFetch() {
  return async () => ({ ok: true, status: 200, async json() { return {}; } });
}

async function main() {
  // ---- 正常启动：定时器排上、立刻演一次 ----
  // 第 151 步改成了"随机挑一个动作"，所以这里把 Math.random 钉死：
  // 0 → 第一个动作（晃晃，有素材）；0.999 → 最后一个（擦擦灰，CSS 效果）。
  const originalRandom = Math.random;
  Math.random = () => 0;

  const dom = buildDom();
  const { byId } = dom;
  bootApp(dom, makeFetch());
  await settle(4);

  const pet = byId.get('pet');
  const idle = byId.get('pet-idle');

  check('页面上有待机动画层', Boolean(idle));
  // 随机可能挑到「喝茶」或「晃晃」，所以只断言"指向 actions/ 下的 webm"
  check('动作层指向 webm 素材',
    Boolean(idle) && /actions\/[a-z]+\.webm/.test(String(idle.src)),
    idle ? String(idle.src) : '无');
  // 注意：桩里 setTimeout 立即执行，"正在演"这个中间态在桩里**看不到**
  // （加上类同一拍就摘掉了）。真浏览器里由烟测断言，这里只钉"牌打出去了"。
  // 第 153 步起素材是 webm：重播靠 `currentTime = 0`，不是换 URL。
  // （桩里没有 play()，所以这里只验"播放位置被归零"这条状态。）
  check('视频动作把播放位置归零（否则重播会接在结尾）',
    Boolean(idle) && idle.currentTime === 0,
    idle ? String(idle.currentTime) : '无');

  // 桩里 setTimeout 立即执行 -> 类已经被摘掉了
  check('演完把类摘掉（静帧重新露出来）',
    Boolean(pet) && !pet.classList.contains('is-idle-playing'));

  Math.random = originalRandom;

  // ---- 减少动态效果：不该排定时器 ----
  const calm = buildDom();
  const originalWindow = calm.windowStub;
  originalWindow.matchMedia = () => ({ matches: true, addEventListener: () => {} });
  bootApp(calm, makeFetch());
  await settle(4);
  const calmPet = calm.byId.get('pet');
  const calmIdle = calm.byId.get('pet-idle');
  check('减少动态效果时不演',
    Boolean(calmPet) && !calmPet.classList.contains('is-idle-playing'));
  check('减少动态效果时不设素材',
    Boolean(calmIdle) && !calmIdle.src,
    calmIdle ? String(calmIdle.src) : '无');

  console.log(`\n通过 ${failures === 0 ? '全部' : '部分'}断言，失败 ${failures} 项`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  console.error('脚本异常：', error);
  process.exit(1);
});

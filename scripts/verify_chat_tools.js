/**
 * 功能栏「›」折叠的行为验证：默认收起、点开与再收起、选完胶囊自己收起、打字时保持收起。
 */

'use strict';

const { buildDom, bootApp, settle } = require('./_dom_stub');

let failures = 0;
function check(label, ok, detail) {
  if (!ok) failures += 1;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${label}${detail ? '  |  ' + detail : ''}`);
}

/** `/api/tools` 返回三个有面板的工具；其它路径给最小可用响应。 */
function makeFetch() {
  return async (url) => {
    const path = String(url);
    if (path.indexOf('/api/tools') >= 0) {
      return {
        ok: true,
        status: 200,
        async json() {
          return {
            tools: [
              { name: 'plan_trip', label: '出行', panel: 'travel', menu: true },
              { name: 'mail', label: '邮箱', panel: 'mail', menu: true },
              { name: 'recipe', label: '食谱', panel: 'recipe', menu: true },
            ],
          };
        },
      };
    }
    if (path.indexOf('/api/session/') >= 0) {
      return {
        ok: true,
        status: 200,
        async json() {
          return { session_id: 's', messages: [], greeting: '' };
        },
      };
    }
    return {
      ok: true,
      status: 200,
      async json() {
        return {};
      },
    };
  };
}

async function main() {
  const dom = buildDom();
  const { byId } = dom;                    // byId 是个 Map，不是函数
  const el = (id) => byId.get(id) || null;

  bootApp(dom, makeFetch());
  await settle(5);

  const chips = el('chat-chips');
  const more = el('chat-tools') && el('chat-more');
  const tools = el('chat-tools');
  const input = el('user-input');

  check('找到「›」按钮', Boolean(more));
  check('找到胶囊行', Boolean(chips));
  if (!more || !chips || !tools) {
    console.log(`\n通过 0 项，失败 ${failures} 项`);
    process.exit(1);
  }

  // ---- 1) 默认收起 ----
  check('默认收起：胶囊行 hidden', chips.hidden === true);
  check(
    '默认收起：aria-expanded=false',
    more.getAttribute('aria-expanded') === 'false',
    String(more.getAttribute('aria-expanded'))
  );

  // ---- 2) 点一下展开 ----
  more.click();
  await settle(2);
  check('点开后胶囊行出现', chips.hidden === false);
  check(
    '点开后 aria-expanded=true',
    more.getAttribute('aria-expanded') === 'true',
    String(more.getAttribute('aria-expanded'))
  );
  check('点开后容器带 is-open（箭头转向）', tools.classList.contains('is-open'));
  check('胶囊行里确实有按钮', chips.children.length >= 3, `${chips.children.length} 个`);

  // ---- 3) 再点收起 ----
  more.click();
  await settle(2);
  check('再点一下收回', chips.hidden === true);
  check('收回后 aria-expanded=false', more.getAttribute('aria-expanded') === 'false');

  // ---- 4) 点胶囊：窗口要开、功能栏要自己收起 ----
  more.click();
  await settle(2);
  const firstChip = chips.children[0];
  firstChip.click();
  await settle(3);
  check('点胶囊后功能栏自动收起', chips.hidden === true);

  // ---- 5) 打字时保持收起 ----
  more.click();
  await settle(2);
  if (input) {
    input.value = '在打字';
    input._handlers.input.forEach((handler) => handler({ target: input }));
    await settle(2);
    check('打字时胶囊行收起', chips.hidden === true);
    input.value = '';
    input._handlers.input.forEach((handler) => handler({ target: input }));
    await settle(2);
    check('清空输入后又回来（仍然展开状态）', chips.hidden === false);
  }

  console.log(`\n通过 ${failures === 0 ? '全部' : '部分'}断言，失败 ${failures} 项`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  console.error('脚本异常：', error);
  process.exit(1);
});

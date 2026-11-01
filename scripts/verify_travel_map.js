/**
 * 行程地图的验证脚本：用假的高德对象跑 map.js 的真实渲染路径（编号、按天的线、
 * 颜色、聚焦），并验 app.js 的接线（默认不出现、能按天切、点开才加载）。
 *
 * 瓦片、拖动、缩放只能人眼看，不在这里。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { buildDom, bootApp, ROOT } = require('./_dom_stub');

/**
 * 把挂起的异步链路彻底跑完（微任务 + 宏任务都推进）。
 *
 * 为什么不用 settle()：它只是连续 await null 若干次，而「拉配置 → 解析 JSON →
 * 渲染概览 → 展开地图」这条链上每一步都要好几个微任务，靠数次数非常脆
 * （第一版就错在这：断言打在中间态上，失败信息还指向了别的断言）。
 * setImmediate 轮转能保证链上每一步都真的跑完。
 */
async function flush(rounds = 12) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

const MAP_JS = fs.readFileSync(path.join(ROOT, 'static', 'map.js'), 'utf8');

/** 在给定 window/document 上执行一遍 map.js（每次都是全新的闭包，互不干扰）。 */
function loadMap(windowStub, documentStub) {
  new Function('window', 'document', MAP_JS)(windowStub, documentStub);
  return windowStub.TravelMap;
}

/** 一套够用的高德替身：只实现 map.js 真正调用到的那些方法。 */
function makeFakeAmap() {
  const log = { markers: [], polylines: [], maps: 0, fitViews: 0, zoomCenters: [], destroyed: false };

  class Pixel {
    constructor(x, y) {
      this.x = x;
      this.y = y;
    }
  }

  class Marker {
    constructor(opts) {
      this.opts = opts;
      this.handlers = {};
      log.markers.push(this);
    }
    on(type, handler) {
      this.handlers[type] = handler;
      return this;
    }
    getPosition() {
      return this.opts.position;
    }
  }

  class Polyline {
    constructor(opts) {
      this.opts = opts;
      log.polylines.push(this);
    }
  }

  class Map {
    constructor(container, opts) {
      this.container = container;
      this.opts = opts;
      this.added = [];
      log.maps += 1;
    }
    add(overlay) {
      this.added.push(overlay);
    }
    remove(overlay) {
      const at = this.added.indexOf(overlay);
      if (at >= 0) this.added.splice(at, 1);
    }
    setFitView(list) {
      log.fitViews += 1;
      log.lastFit = list;
    }
    setZoomAndCenter(zoom, center) {
      log.zoomCenters.push([zoom, center]);
    }
    destroy() {
      log.destroyed = true;
    }
  }

  return { amap: { Map, Marker, Polyline, Pixel }, log };
}

function sampleDays() {
  // 坐标顺序与真实 `/api/trip` 一致：**[纬度, 经度]**（core/itinerary._place_location）。
  // 第 243 步之前这里写的是 [经度, 纬度]，正好把 map.js 的顺序错误盖住了。
  return [
    {
      index: 1,
      date: '2026-10-30',
      spots: [
        { name: '外滩', location: [31.241701, 121.490317], verified: true },
        { name: '豫园', location: [31.227, 121.492], verified: true },
      ],
    },
    {
      index: 2,
      date: '2026-10-31',
      spots: [
        { name: '上海博物馆', location: [31.229, 121.475], verified: true },
        // 没有坐标的景点：画不出来，必须被跳过而不是画到 (0,0)
        { name: '待核实的地点', location: null, verified: false },
        { name: '武康路', location: [31.204, 121.437], verified: true },
      ],
    },
  ];
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

  // ---------------------------------------------------------------- 纯函数
  {
    const dom = buildDom();
    const map = loadMap(dom.windowStub, dom.documentStub);

    await check('坐标顺序：后端 [纬度, 经度] → 高德 [经度, 纬度]（第 243 步）', () => {
      // 苏州的真实值：传反了会变成「纬度 120.58」，被钳到 90，地图跑到北极
      assert.deepStrictEqual(map.lnglatOf([31.298058, 120.581819]), [
        120.581819,
        31.298058,
      ]);
      assert.deepStrictEqual(map.lnglatOf([31.298058, 120.581819])[0], 120.581819);
      assert.strictEqual(map.lnglatOf(null), null);
      assert.strictEqual(map.lnglatOf([]), null);
      assert.strictEqual(map.lnglatOf(['a', 'b']), null, '非数字不能静默变成 NaN');
    });

    await check('景点按跨天连续编号，没坐标的直接跳过', () => {
      const points = map.pointList(sampleDays());
      assert.strictEqual(points.length, 4, '4 个有坐标的景点');
      assert.deepStrictEqual(
        points.map((point) => point.order),
        [1, 2, 3, 4]
      );
      assert.deepStrictEqual(
        points.map((point) => point.name),
        ['外滩', '豫园', '上海博物馆', '武康路']
      );
      assert.deepStrictEqual(
        points.map((point) => point.day),
        [1, 1, 2, 2]
      );
    });

    await check('按天取折线：只取这一天的点，顺序不乱', () => {
      const points = map.pointList(sampleDays());
      // 这条同时钉住「纬度在前」的输入被翻成 [经度, 纬度]：
      // 输入是 [31.241701, 121.490317]，输出必须是 [121.490317, 31.241701]。
      assert.deepStrictEqual(map.polylineFor(points, 1), [
        [121.490317, 31.241701],
        [121.492, 31.227],
      ]);
      assert.strictEqual(map.polylineFor(points, 2).length, 2);
      assert.deepStrictEqual(map.polylineFor(points, 9), []);
    });

    await check('每天都拿到自己的颜色，第 8 天开始循环', () => {
      const colors = [1, 2, 3, 4, 5].map((day) => map.colorFor(day));
      assert.strictEqual(new Set(colors).size, 5, '前 5 天颜色必须互不相同');
      assert.strictEqual(map.colorFor(8), map.colorFor(1));
    });

    await check('配置不可用时给出人话原因', () => {
      assert.strictEqual(map.disabledNote({ enabled: true }), '');
      assert.ok(map.disabledNote({ enabled: false, note: '还没配 key' }).includes('key'));
      assert.ok(!map.disabledNote(null).includes('undefined'));
    });

    await check('底图没出来时能说清是哪一步被拦（第 230 步）', () => {
      // 瓦片域名被拦：要能点出「被拦或超时」，而不是笼统的"加载失败"
      const tile = map.blankNote(['https://webrd01.is.autonavi.com/appmaptile?x=1']);
      assert.ok(tile.includes('webrd01.is.autonavi.com'), tile);
      assert.ok(tile.includes('被拦'), tile);
      // 别的域名失败也要报出来，但措辞不猜原因
      const other = map.blankNote(['http://127.0.0.1:8000/_AMapService/v4/map/styles?x=1']);
      assert.ok(other.includes('127.0.0.1'), other);
      // 一条失败都没有：只说"还没出来"，不许编原因、不许出现 undefined
      const none = map.blankNote([]);
      assert.ok(none.includes('还没加载出来'), none);
      assert.ok(!none.includes('undefined'));
      assert.strictEqual(map.blankNote(undefined), map.blankNote([]));
    });

    await check('只把「会让底图变白」的失败算进来（第 240 步）', () => {
      assert.ok(map.isMapResource('https://webrd01.is.autonavi.com/appmaptile?x=1'));
      assert.ok(map.isMapResource('https://webapi.amap.com/maps?v=2.0&key=k'));
      assert.ok(map.isMapResource('http://127.0.0.1:8000/_AMapService/v4/map/styles'));
      assert.ok(
        !map.isMapResource('https://images.unsplash.com/photo-1.jpg'),
        '景点配图失败与底图无关，不能拿来解释白板'
      );
      assert.ok(!map.isMapResource('https://picsum.photos/200'));
      assert.ok(!map.isMapResource(''));
    });

    await check('没配 key 时 render 直接失败并说明原因（不去加载脚本）', async () => {
      let rejected = null;
      await map
        .render(dom.documentStub.createElement('div'), sampleDays(), {
          config: { enabled: false, note: '没配 key' },
        })
        .catch((error) => {
          rejected = error;
        });
      assert.ok(rejected, '必须 reject，不能静默什么也不做');
      assert.ok(String(rejected.message).includes('没配 key'));
    });
  }

  // ------------------------------------- 脚本在页面上、AMap 却没挂上（第 240 步）
  {
    const dom = buildDom();
    dom.windowStub.location = { origin: 'http://127.0.0.1:8765' };
    const fake = makeFakeAmap();
    // 页面里已经有一个 amap-js-api 节点（上次加载留下的），但 window.AMap 是空的
    const stale = dom.documentStub.createElement('script');
    stale.id = 'amap-js-api';
    dom.byId.set('amap-js-api', stale);
    const map = loadMap(dom.windowStub, dom.documentStub);
    const container = dom.documentStub.createElement('div');

    await check('AMap 缺失时失败，且下一次会真的重试（第 240 步）', async () => {
      const first = map.render(container, sampleDays(), {
        config: { enabled: true, key: 'k', service_host: '/_AMapService' },
      });
      stale.emit('load'); // 脚本"加载完"了，但 window.AMap 仍然没有
      let rejected = null;
      await first.catch((error) => {
        rejected = error;
      });
      assert.ok(rejected, 'AMap 没挂上不能假装成功');
      assert.ok(String(rejected.message).includes('没有 AMap'));

      // 关键：state.loading 必须复位。否则第二次直接把同一个失败对象交回来，
      // 用户再点「展开地图」永远不会重新加载。
      dom.windowStub.AMap = fake.amap;
      const second = map.render(container, sampleDays(), {
        config: { enabled: true, key: 'k', service_host: '/_AMapService' },
      });
      stale.emit('load');
      const instance = await second;
      assert.ok(instance, '第二次应当成功建图');
      assert.strictEqual(fake.log.maps, 1);
    });
  }

  // ------------------------------------------------------- 渲染（假高德）
  {
    const dom = buildDom();
    dom.windowStub.location = { origin: 'http://127.0.0.1:8765' };
    const map = loadMap(dom.windowStub, dom.documentStub);
    const fake = makeFakeAmap();
    const container = dom.documentStub.createElement('div');
    const picked = [];

    const instance = await map.render(container, sampleDays(), {
      config: { enabled: true, key: 'k', service_host: '/_AMapService' },
      amap: fake.amap,
      onPick: (point) => picked.push(point.name),
    });

    await check('地图只创建一次，容器用的是传进来的那个', () => {
      assert.strictEqual(fake.log.maps, 1);
      assert.strictEqual(instance.container, container);
    });

    await check('4 个有坐标的景点 → 4 个编号标记，编号就是顺序', () => {
      assert.strictEqual(fake.log.markers.length, 4);
      const numbers = fake.log.markers.map((marker) =>
        (marker.opts.content.match(/>(\d+)</) || [])[1]
      );
      assert.deepStrictEqual(numbers, ['1', '2', '3', '4']);
      // 标记必须落在 [经度, 纬度]：经度 ~121、纬度 ~31。
      // 传反时这里会是 [31, 121] —— 高德把纬度 121 钳到 90，地图跑到北极。
      fake.log.markers.forEach((marker) => {
        const [lng, lat] = marker.opts.position;
        assert.ok(lng > 100 && lng < 130, `经度应当在 121 附近，实际 ${lng}`);
        assert.ok(lat > 20 && lat < 40, `纬度应当在 31 附近，实际 ${lat}`);
      });
      assert.deepStrictEqual(fake.log.markers[0].opts.position, [121.490317, 31.241701]);
    });

    await check('每天一条虚线，颜色用的是当天颜色', () => {
      assert.strictEqual(fake.log.polylines.length, 2);
      assert.strictEqual(fake.log.polylines[0].opts.strokeStyle, 'dashed');
      assert.strictEqual(fake.log.polylines[0].opts.strokeColor, map.colorFor(1));
      assert.strictEqual(fake.log.polylines[1].opts.strokeColor, map.colorFor(2));
      assert.strictEqual(fake.log.polylines[0].opts.path.length, 2);
    });

    await check('渲染后自动框住全部内容', () => {
      assert.ok(fake.log.fitViews >= 1, 'setFitView 必须被调用，否则用户看到一片空白');
    });

    await check('点标记会把景点交回给调用方', () => {
      fake.log.markers[0].handlers.click();
      assert.deepStrictEqual(picked, ['外滩']);
    });

    await check('聚焦某天只画那天的线，并把视野框到那天的点上', () => {
      map.setFocus(2);
      assert.strictEqual(fake.log.polylines.length, 3, '重新画了一次（第二条是新增的）');
      const latest = fake.log.polylines[fake.log.polylines.length - 1];
      assert.strictEqual(latest.opts.strokeColor, map.colorFor(2));
      const fitted = fake.log.lastFit || [];
      assert.ok(
        fitted.some((overlay) => overlay.opts && overlay.opts.content),
        '框选的对象里要有标记'
      );
    });

    await check('destroy 之后地图真的被销毁', () => {
      map.destroy();
      assert.strictEqual(fake.log.destroyed, true);
    });
  }

  // ------------------------------------------ app.js 接线（真的填表 → 提交）
  //
  // 这一段不用「直接调 render」那种作弊法：它走的是真人路径
  // 填表 → submit → POST /api/trip → 概览 → 点「展开地图」。
  // 只有这样才能证明"地图卡片到底会不会出现在用户眼前"。
  const tripPayload = {
    city: '上海',
    start_date: '2026-10-30',
    end_date: '2026-10-31',
    transport: '公共交通',
    days: sampleDays(),
    status: 'ok',
    issues: [],
    degraded_reasons: [],
    weather: { location: '上海', temp: 21, temp_unit: '°C', text: '晴' },
    budget: null,
    summary: '测试用行程',
  };

  async function bootWithTrip({ configEnabled }) {
    const dom = buildDom();
    dom.windowStub.location = { origin: 'http://127.0.0.1:8765' };
    const calls = [];
    const json = (payload) => ({
      ok: true,
      status: 200,
      async json() {
        return payload;
      },
    });
    const fetchImpl = async (url, options) => {
      const target = String(url);
      calls.push({ url: target, method: (options && options.method) || 'GET' });
      if (target.includes('/api/map/config')) {
        return json(
          configEnabled
            ? { enabled: true, key: 'k', service_host: '/_AMapService', note: '' }
            : { enabled: false, key: '', service_host: '/_AMapService', note: '没配 key' }
        );
      }
      if (target.endsWith('/api/trip')) return json({ itinerary: tripPayload });
      return json({});
    };

    const seen = { renders: [], focuses: [], containers: [] };
    dom.windowStub.TravelMap = {
      render(container, days, options) {
        seen.containers.push(container);
        seen.renders.push({ days, options });
        return Promise.resolve({});
      },
      setFocus(day) {
        seen.focuses.push(day);
      },
      destroy() {},
    };

    bootApp(dom, fetchImpl);
    await flush();

    dom.byId.get('travel-city').value = '上海';
    dom.byId.get('travel-start').value = '2026-10-30';
    dom.byId.get('travel-end').value = '2026-10-31';
    dom.byId.get('travel-form').submit();
    await flush();

    return { dom, calls, seen };
  }

  {
    const { dom, calls, seen } = await bootWithTrip({ configEnabled: true });
    const card = dom.byId.get('travel-map-card');
    const toggle = dom.byId.get('travel-map-toggle');
    const chips = dom.byId.get('travel-map-days');
    const canvas = dom.byId.get('travel-map');

    await check('文案提交后真的去问了地图配置（说明这条路径是通的）', () => {
      assert.ok(
        calls.some((call) => call.url.includes('/api/map/config')),
        '没有请求 map-config，说明这段测试根本没走到地图那条路'
      );
      assert.ok(
        calls.some((call) => call.method === 'POST' && call.url.endsWith('/api/trip')),
        '没有提交行程'
      );
    });

    await check('有配置时地图卡片出现，且每个「第N天」都有按钮', () => {
      assert.strictEqual(card.hidden, false);
      const labels = chips.children.map((child) => child.textContent);
      assert.deepStrictEqual(labels, ['全部', '第1天', '第2天']);
    });

    await check('默认不加载地图：画布是隐藏的，按钮写着「展开地图」', () => {
      assert.strictEqual(canvas.hidden, true);
      assert.strictEqual(toggle.textContent, '展开地图');
      assert.strictEqual(toggle.getAttribute('aria-expanded'), 'false');
      assert.strictEqual(seen.renders.length, 0, '用户没点开就不该加载高德脚本');
    });

    await check('点「展开地图」才真去渲染，并且用的是行程里的天', async () => {
      toggle.click();
      // 展开是异步的（要等 map.js 的 render 落定）；真实用户两次点击之间一定有时间差，
      // 测试里必须自己把微任务跑完，否则下一句会撞上"还没画完"的中间态。
      await flush();
      assert.strictEqual(seen.renders.length, 1);
      assert.strictEqual(seen.renders[0].days.length, 2);
      assert.strictEqual(seen.renders[0].days[1].spots.length, 3);
      assert.strictEqual(canvas.hidden, false);
      assert.strictEqual(toggle.textContent, '收起地图');
      assert.strictEqual(seen.containers[0], canvas, '要把画布本身交给 TravelMap');
    });

    await check('点「第2天」会让地图聚焦到那一天（已展开时只调 setFocus）', () => {
      const fakeTarget = { dataset: { day: '2' }, closest: () => fakeTarget };
      chips.click(fakeTarget);
      assert.deepStrictEqual(seen.focuses, [2]);
      assert.strictEqual(seen.renders.length, 1, '已经画过了就不该整张重画');
    });

    await check('再点一次「收起地图」就藏起来（不销毁，省得下次重新加载脚本）', () => {
      toggle.click();
      assert.strictEqual(canvas.hidden, true);
      assert.strictEqual(toggle.textContent, '展开地图');
      assert.strictEqual(toggle.getAttribute('aria-expanded'), 'false');
    });
  }

  {
    const { dom, seen } = await bootWithTrip({ configEnabled: false });
    const card = dom.byId.get('travel-map-card');
    await check('没配 key 时整张卡片都不出现（而不是出现一个点不动的空壳）', () => {
      assert.strictEqual(card.hidden, true);
      assert.strictEqual(seen.renders.length, 0);
    });
  }

  console.log('');
  if (failures.length) {
    console.log(`行程地图：${failures.length} 条失败`);
    failures.forEach((line) => console.log('  - ' + line));
    process.exitCode = 1;
    return;
  }
  console.log('行程地图：全部通过');
}

main().catch((error) => {
  console.error('脚本本身出错：', error);
  process.exitCode = 1;
});

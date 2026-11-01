/**
 * 出行窗口的地图：懒加载高德 JS API 并按天渲染编号、连线与相机。
 *
 * 高德脚本约 1 MB，只有用户点开地图时才加载。key 从服务端现取；安全密钥永远只在
 * 服务端，数据请求走同源 `/_AMapService/**` 由后端补上。
 */
(function () {
  'use strict';

  var SCRIPT_ID = 'amap-js-api';
  var SCRIPT_URL = 'https://webapi.amap.com/maps?v=2.0&key=';
  /** 底图迟迟不出来的等待上限（第 230 步）：超过它就给一句人话，而不是留一块白板。 */
  var BLANK_TIMEOUT_MS = 6000;
  /** 页面里失败的资源请求（瓦片、样式、脚本），用来告诉用户"拦在哪一步"。 */
  var failures = [];
  var watchingFailures = false;
  // 每天一条线、一种颜色；超过 5 天就循环（行程上限 7 天，肉眼够区分）
  var DAY_COLORS = ['#2f6fed', '#e2574c', '#2fa35b', '#a259d9', '#e08a00', '#0f9b9b', '#c2477f'];

  var state = {
    key: '',
    serviceHost: '',
    amap: null,
    map: null,
    container: null,
    overlays: [],
    days: [],
    focus: null,
  };

  /**
   * 后端给的是 `[纬度, 经度]`（`core/itinerary._place_location` 返回 `(lat, lon)`），
   * AMap 要的是 `[经度, 纬度]` —— **这一步必须翻**。
   *
   * 第 243 步的真事故：这里原样传了 `[location[0], location[1]]`，
   * 苏州的 `[31.29, 120.58]` 被当成「纬度 120.58」钳到 90，
   * 地图定位到北极、瓦片请求被高德以 OVERSEA_FORBIDDEN 拒绝，只剩白底和编号标记。
   * 之所以一直没发现：网页里的瓦片是 XHR 200 + JSON 错误，不会触发 `<img>` 的 error，
   * `complete` 事件照常触发，所以连「白板提示」都不会出现。
   */
  function lnglatOf(location) {
    if (!Array.isArray(location) || location.length < 2) return null;
    var lat = Number(location[0]);
    var lng = Number(location[1]);
    if (!isFinite(lat) || !isFinite(lng)) return null;
    return [lng, lat]; // 只在这里翻一次，别处一律当 [lat, lon] 用
  }

  /** 纯函数：把行程里的景点摊平成「第几天第几个」的列表（方便测）。 */
  function pointList(days) {
    var points = [];
    (days || []).forEach(function (day) {
      (day.spots || []).forEach(function (spot) {
        var lnglat = lnglatOf(spot.location);
        if (!lnglat) return; // 没有坐标 / 坐标不是数字的点画不出来，直接跳过
        points.push({
          day: day.index,
          date: day.date,
          name: spot.name,
          lnglat: lnglat,
          verified: spot.verified !== false,
          order: points.length + 1,
        });
      });
    });
    return points;
  }

  /** 纯函数：某一天的折线（按当前顺序）。少于 2 个点就没有线可画。 */
  function polylineFor(points, dayIndex) {
    return points
      .filter(function (point) {
        return point.day === dayIndex;
      })
      .map(function (point) {
        return point.lnglat;
      });
  }

  function colorFor(dayIndex) {
    var index = Math.max(0, Number(dayIndex) - 1);
    return DAY_COLORS[index % DAY_COLORS.length];
  }

  /** 配置不可用时给用户一句人话（纯函数，便于测试）。 */
  function disabledNote(config) {
    if (!config || config.enabled) return '';
    return (config && config.note) || '地图还没配置好';
  }

  /**
   * 底图没出来时给用户一句人话（纯函数，便于测试）。
   *
   * 为什么需要这个：第 230 步用户看到的是「一张白板 + 几个编号」，页面不报错、控制台
   * 也没有异常 —— 因为**瓦片是 `<img>` 加载的，失败不会抛错**。把失败的资源地址
   * 收集起来，就能区分「底图被拦了」和「网络慢」这两种完全不同的情况。
   */
  function blankNote(list) {
    var items = list || [];
    for (var i = 0; i < items.length; i += 1) {
      var match = /^https?:\/\/([^/?#]+)/.exec(String(items[i] || ''));
      var host = match ? match[1] : '';
      if (!host) continue;
      if (/autonavi|amap\.com/i.test(host)) {
        return '底图没加载出来（' + host + ' 被拦或超时）。点位是对的，检查网络/代理设置';
      }
      return '底图没加载出来（' + host + ' 请求失败）';
    }
    return '底图还没加载出来：网络慢或被拦，稍等一下或刷新重试';
  }

  /** 记下页面里失败的资源请求（只在第一次安装监听器）。 */
  function watchFailures() {
    if (watchingFailures || !window.addEventListener) return;
    watchingFailures = true;
    window.addEventListener(
      'error',
      function (event) {
        var target = event && event.target;
        var url = target && (target.src || target.href);
        // 第 240 步：只收「会让底图变白」的资源。以前是全页面照单全收——
        // 行程里的景点配图（Unsplash/高德 POI 图）失败也会进来，blankNote 拿第一条
        // 去猜原因，就会报出「某图片域名被拦」，其实底图只是慢。
        if (url && isMapResource(url)) failures.push(String(url));
      },
      true // 捕获阶段：`<img>` 的加载失败不会冒泡到 window
    );
  }

  /**
   * 这个失败的资源会不会让底图变白？（纯函数，便于测试）
   *
   * 三类算：高德/高德的瓦片与样式域名、经同源代理 `/_AMapService/**` 的请求、
   * 以及 JS API 脚本本身。别的（用户配图、头像、食谱图）都不算。
   */
  function isMapResource(url) {
    var text = String(url || '').trim();
    if (!text) return false;
    if (text.indexOf('/_AMapService') !== -1) return true;
    if (text.indexOf('webapi.amap.com/maps') !== -1) return true;
    var match = /^https?:\/\/([^/?#]+)/i.exec(text);
    var host = match ? match[1] : '';
    return /autonavi|amap\.com/i.test(host);
  }

  /**
   * 等底图就绪；超时就给一句解释。
   *
   * `complete` 是高德「地图渲染完成」的事件。它不来，基本只有三种可能：
   * 瓦片被拦、网络慢、或者容器尺寸是 0（第 230 步专门处理了最后一种）。
   */
  function watchLoaded(map, onNote) {
    var done = false;
    function finish(note) {
      if (done) return;
      done = true;
      if (typeof onNote === 'function') onNote(note);
    }
    try {
      map.on('complete', function () {
        finish('');
      });
    } catch (error) {
      /* 桩里没有 on()，忽略 */
    }
    var timer =
      typeof window.setTimeout === 'function'
        ? window.setTimeout(function () {
            finish(blankNote(failures));
          }, BLANK_TIMEOUT_MS)
        : null;
    return function cancel() {
      done = true;
      if (timer && typeof window.clearTimeout === 'function') window.clearTimeout(timer);
    };
  }

  function loadScript(key, serviceHost) {
    if (window.AMap) return Promise.resolve(window.AMap);
    if (state.loading) return state.loading;

    // 官方接法：数据请求全部走我们自己的同源代理，密钥不出服务端
    window._AMapSecurityConfig = { serviceHost: serviceHost };

    state.loading = new Promise(function (resolve, reject) {
      var existing = document.getElementById(SCRIPT_ID);
      if (existing) {
        // 第 240 步：脚本已经在页面里，但 AMap 没挂上（被拦、被 CSP 吃掉、
        // 加载到一半刷新）→ reject 并**复位 state.loading**，
        // 否则这个 Promise 会一直是已拒绝状态，之后再点「展开地图」永远拿到同一个失败。
        existing.addEventListener('load', function () {
          if (window.AMap) {
            resolve(window.AMap);
            return;
          }
          state.loading = null;
          reject(new Error('高德脚本加载完了但没有 AMap'));
        });
        existing.addEventListener('error', function () {
          state.loading = null;
          reject(new Error('高德脚本加载失败'));
        });
        return;
      }
      var script = document.createElement('script');
      script.id = SCRIPT_ID;
      script.async = true;
      script.src = SCRIPT_URL + encodeURIComponent(key);
      script.onload = function () {
        if (window.AMap) {
          resolve(window.AMap);
          return;
        }
        state.loading = null;
        reject(new Error('高德脚本加载完了但没有 AMap'));
      };
      script.onerror = function () {
        state.loading = null;
        reject(new Error('高德脚本加载失败'));
      };
      document.head.appendChild(script);
    });
    return state.loading;
  }

  function clearOverlays() {
    state.overlays.forEach(function (overlay) {
      try {
        state.map.remove(overlay);
      } catch (error) {
        /* 已经移除过就算了 */
      }
    });
    state.overlays = [];
  }

  function draw(amap, days, focus) {
    var map = state.map;
    clearOverlays();

    var points = pointList(days);
    var markerInfos = [];
    points.forEach(function (point) {
      var marker = new amap.Marker({
        position: point.lnglat,
        // 编号方块 + 水滴：和常见行程地图一个观感，但数字是**我们自己算的顺序**
        content:
          '<div class="map-pin" style="background:' +
          // 颜色按天取（和当天那条虚线一致），而不是在 CSS 里写死一个绿：
          // CSS 里那个值是 fallback，写死颜色在暗色主题下会变成一块不听话的亮斑。
          colorFor(point.day) +
          '" title="' +
          escapeHtml(point.name) +
          '"><span>' +
          point.order +
          '</span></div>',
        offset: new amap.Pixel(-13, -13),
        zIndex: 100 + point.order,
      });
      marker.on('click', function () {
        if (typeof state.onPick === 'function') state.onPick(point);
      });
      map.add(marker);
      state.overlays.push(marker);
      markerInfos.push({ marker: marker, point: point });
    });

    var dayIndexes = [];
    (days || []).forEach(function (day) {
      if (dayIndexes.indexOf(day.index) === -1) dayIndexes.push(day.index);
    });

    dayIndexes.forEach(function (dayIndex) {
      // 聚焦某一天时，其它天的线不画——避免图上太乱（marker 仍保留，方便对照）
      if (focus !== null && focus !== undefined && dayIndex !== focus) return;
      var path = polylineFor(points, dayIndex);
      if (path.length < 2) return;
      var line = new amap.Polyline({
        path: path,
        strokeColor: colorFor(dayIndex),
        strokeWeight: 3,
        strokeOpacity: 0.85,
        strokeStyle: 'dashed',
        strokeDasharray: [8, 6],
        lineJoin: 'round',
        zIndex: 50,
      });
      map.add(line);
      state.overlays.push(line);
    });

    var focusMarkers = markerInfos
      .filter(function (info) {
        return focus === null || focus === undefined || info.point.day === focus;
      })
      .map(function (info) {
        return info.marker;
      });

    if (focus !== null && focus !== undefined) {
      // 单点没法"框"，直接居中放大；多点才用 setFitView
      if (focusMarkers.length === 1) {
        map.setZoomAndCenter(14, focusMarkers[0].getPosition());
      } else if (focusMarkers.length > 1) {
        map.setFitView(focusMarkers, false, [60, 60, 60, 60]);
      }
    } else if (state.overlays.length) {
      map.setFitView(state.overlays, false, [60, 60, 60, 60]);
    }
  }

  function escapeHtml(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, function (char) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char];
    });
  }

  /**
   * 渲染地图。返回 Promise，失败时 reject —— 调用方负责把失败写在页面上，
   * 这里不吞异常（"地图没出来也不知道为什么"是这个项目明确反对的做法）。
   *
   * @param {HTMLElement} container 放置地图的容器
   * @param {Array} days Itinerary.days
   * @param {Object} options {config, focus, onPick, amap}
   */
  function render(container, days, options) {
    var opts = options || {};
    var config = opts.config || {};
    if (!config.enabled) return Promise.reject(new Error(disabledNote(config)));
    if (!window.AMap && opts.amap) window.AMap = opts.amap; // 测试用的注入点
    // 第 240 步：每次渲染重新收集。上一次的失败留在数组里，
    // 会让这次的白板提示指向上一次的原因。
    failures = [];
    state.onPick = opts.onPick;
    state.days = days || [];

    // 用 window.location 而不是裸 location：脚本在浏览器里跑没问题，
    // 但单测是在 node 里塞一个假 window 执行它，裸全局会直接 ReferenceError。
    var origin = (window.location && window.location.origin) || '';

    return loadScript(config.key, origin + (config.service_host || '/_AMapService')).then(
      function (amap) {
        state.amap = amap;
        watchFailures();
        // 容器刚 `hidden = false`：这一帧它的尺寸可能还是 0（第 230 步实测的坑）。
        // 0 尺寸建图 → 所有点被投影到同一条线上、瓦片一张也不请求，看起来就是"白板"。
        // 所以：等一帧真的有了尺寸再建图，建完再主动 resize 一次。
        return nextFrame().then(function () {
          if (!state.map) {
            state.map = new amap.Map(container, {
              zoom: 11,
              resizeEnable: true,
              viewMode: '2D',
            });
            state.container = container;
          }
          if (typeof state.map.resize === 'function') state.map.resize();
          state.cancelWatch = watchLoaded(state.map, opts.onNote);
          draw(amap, state.days, opts.focus);
          return state.map;
        });
      }
    );
  }

  /** 等一帧（浏览器里用 rAF，node 里立刻兑现）。 */
  function nextFrame() {
    return new Promise(function (resolve) {
      if (typeof window.requestAnimationFrame !== 'function') {
        resolve();
        return;
      }
      window.requestAnimationFrame(function () {
        resolve();
      });
    });
  }

  function setFocus(dayIndex) {
    state.focus = dayIndex;
    if (!state.map) return;
    draw(state.amap, state.days, dayIndex);
  }

  function destroy() {
    if (state.map) {
      try {
        state.map.destroy();
      } catch (error) {
        /* 忽略 */
      }
    }
    state.map = null;
    state.container = null;
    state.overlays = [];
  }

  window.TravelMap = {
    render: render,
    setFocus: setFocus,
    destroy: destroy,
    // 纯函数，测试直接调
    pointList: pointList,
    lnglatOf: lnglatOf,
    polylineFor: polylineFor,
    colorFor: colorFor,
    disabledNote: disabledNote,
    blankNote: blankNote,
    isMapResource: isMapResource,
  };
})();

/* HopDrop 前端：同步（WebSocket 推送 + 快照对齐）。
 *
 * 完全按方案 4.3 的客户端职责表实现：
 *
 * | 场景                                  | 动作                        |
 * |---------------------------------------|-----------------------------|
 * | 首次加载、重连、收到 rev 缺口          | 拉 `/api/snapshot` 全量重建 |
 * | 收到 `changed` 且 rev == 本地 rev + 1 | 增量应用，本地 rev 更新      |
 * | 收到 `changed` 但 rev > 本地 rev + 1  | 放弃增量，改拉快照           |
 *
 * **这套模型的关键性质是"推送全丢也能回到一致"。** 所以这里没有任何重试
 * 发送、补发、本地事件队列——多一份"等待丢失推送"的状态，就多一条会长期
 * 不一致的路径。收到推不动的东西时唯一的动作是拉快照，而拉快照永远有效。
 *
 * 连接层还补了两件事，都是方案没写但实测一定会撞上的：
 *
 * 1. **心跳**：每 25 秒 `ping`（方案 10 写明的）。它同时也是"连接还活着"的
 *    证据——服务端 60 秒收不到活动就关连接。
 * 2. **看门狗**：90 秒没收到**任何**消息就把连接判死并重连。这一条针对的是
 *    "半开连接"：笔记本合盖、切换网络之后，TCP 没断、`readyState` 还是
 *    OPEN，但报文已经到不了。只靠 onclose 是发现不了的，症状是"页面一直
 *    显示已同步，但内容不动了"——最难排查的一类问题。
 */

(function () {
  'use strict';

  var HD = (window.HD = window.HD || {});
  var store = HD.store;

  var PING_INTERVAL_MS = 25000;
  var WATCHDOG_INTERVAL_MS = 10000;
  // 比心跳周期长得多：偶尔丢一两个 pong 不该触发重连。
  var STALE_AFTER_MS = 90000;
  var BACKOFF_STEPS_MS = [500, 1000, 2000, 4000, 8000, 15000, 30000];

  var socket = null;
  var pingTimer = null;
  var watchdogTimer = null;
  var reconnectTimer = null;
  var reconnectAttempt = 0;
  var lastMessageAt = 0;
  // **初值必须是 `false`，不能是 `true`。**
  //
  // `stopped` 的语义是"这个同步层已经被关掉了"（页面即将卸载、会话被撤销），
  // 而不是"还没 start"。启动流程是**先拉 HTTP 快照、再连 WebSocket**
  // （见 app.js 的 boot：快照是基线，推送只是加速器），所以 `resync()` 一定
  // 会先于 `start()` 被调用。初值为 `true` 时，`resync` 开头的守卫会直接
  // 返回一个已 resolve 的空 Promise —— 首屏快照被静默吃掉，`store` 一直是空
  // 的、`rev` 一直是 0，界面只能靠后续 WS 事件一条条往外长，刷新后更是全空。
  // 而且**没有任何报错**：链式调用照常往下走，WebSocket 照样连上，连接状态
  // 照样显示"已连接"。
  var stopped = false;
  var resyncing = false;
  var resyncQueued = false;
  var notify = null;

  function report(code, detail) {
    if (typeof notify === 'function') {
      notify(code, detail);
    }
  }

  function wsUrl() {
    var scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return scheme + '//' + window.location.host + '/ws';
  }

  // ---------------------------------------------------------------- 快照

  /**
   * 拉全量快照。**并发调用会被合并**：断网恢复的一瞬间，rev 缺口、握手
   * 不一致、用户点重试可能同时要求重拉，发三次是纯浪费，而且后到的响应
   * 可能先被处理，把更旧的数据盖上去。
   */
  function resync(reason) {
    if (stopped) {
      return Promise.resolve();
    }
    if (resyncing) {
      resyncQueued = true;
      return Promise.resolve();
    }
    resyncing = true;
    store.setConnection('syncing');

    return HD.api.snapshot().then(
      function (data) {
        store.applySnapshot(data);
        resyncing = false;
        if (resyncQueued) {
          resyncQueued = false;
          return resync('排队中的重拉');
        }
        store.setConnection(socket && socket.readyState === 1 ? 'online' : 'offline');
        return undefined;
      },
      function (error) {
        resyncing = false;
        resyncQueued = false;
        store.setConnection('offline');
        // 401 由 api 层统一处理（回首页），这里只报其他失败。
        if (error.code !== 'unauthorized') {
          report('resync_failed', { reason: reason, message: error.message });
        }
        return undefined;
      }
    );
  }

  // ---------------------------------------------------------------- 消息

  function handleHello(message) {
    if (typeof message.rev !== 'number') {
      return;
    }
    if (message.rev !== store.state.rev) {
      // 握手时服务端报的 rev 与本地不同：离线期间一定有变更。
      resync('握手 rev 不一致（本地 ' + store.state.rev + '，服务端 ' + message.rev + '）');
      return;
    }
    store.setConnection('online');
  }

  function handleChanged(message) {
    var rev = message.rev;
    if (typeof rev !== 'number') {
      return;
    }
    if (rev <= store.state.rev) {
      // 重复推送或过期的推送。不做任何事——这不是错误。
      return;
    }
    if (rev > store.state.rev + 1) {
      resync('rev 缺口（本地 ' + store.state.rev + '，推送 ' + rev + '）');
      return;
    }

    var result = store.applyEvent(message);
    if (result.ok) {
      // **只有在增量确实应用成功了才推进本地 rev。** 推进了就等于承认
      // "我知道 rev 这一版的内容"，而那正是要求重拉时不知道的事。
      store.state.rev = rev;
      store.emit('push');
      store.setConnection('online');
    } else {
      resync(result.resync);
    }
  }

  function handleMessage(raw) {
    lastMessageAt = Date.now();
    var message;
    try {
      message = JSON.parse(raw);
    } catch (error) {
      return;
    }
    if (!message || typeof message.t !== 'string') {
      return;
    }

    if (message.t === 'hello.ok') {
      handleHello(message);
    } else if (message.t === 'changed') {
      handleChanged(message);
    } else if (message.t === 'pong') {
      // 收到就说明连接活着，lastMessageAt 已经在上面更新过了。
    } else if (message.t === 'error') {
      handleServerError(message);
    }
  }

  function handleServerError(message) {
    if (message.code === 'session_revoked') {
      // 方案 11.3：权限被撤销 → 清除本地状态并返回首页。
      // 先停掉重连，否则页面在跳转前还会再来一次。
      stop();
      if (typeof notify === 'function') {
        notify('session_revoked', { message: message.message });
      }
      return;
    }
    if (message.code === 'room_connection_limit' || message.code === 'ip_connection_limit') {
      // 连接数超限。这类拒绝重试也没用，交给页面提示用户关掉别的页面。
      report('connection_limit', { code: message.code, message: message.message });
      return;
    }
    report('server_error', { code: message.code, message: message.message });
  }

  // ---------------------------------------------------------------- 连接

  function sendPing() {
    if (socket && socket.readyState === 1) {
      socket.send(JSON.stringify({ t: 'ping' }));
    }
  }

  function scheduleReconnect() {
    if (stopped || reconnectTimer !== null) {
      return;
    }
    var base = BACKOFF_STEPS_MS[Math.min(reconnectAttempt, BACKOFF_STEPS_MS.length - 1)];
    // 加抖动：四台设备同时掉线时，让它们不要在同一毫秒一起回来。
    var delay = base + Math.floor(Math.random() * 250);
    reconnectAttempt += 1;
    reconnectTimer = window.setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function connect() {
    if (stopped) {
      return;
    }
    if (socket && (socket.readyState === 0 || socket.readyState === 1)) {
      return;
    }

    var ws;
    try {
      ws = new WebSocket(wsUrl());
    } catch (error) {
      store.setConnection('offline');
      scheduleReconnect();
      return;
    }
    socket = ws;

    ws.onopen = function () {
      // 连接建立不等于同步完成：rev 是否对齐要到 `hello.ok` 才知道。
      // 这里刻意不把状态设成 online——那会让界面在真正对齐之前就宣称
      // "已同步"，而这个工具最不该做的事就是对用户撒谎说内容是最新的。
      lastMessageAt = Date.now();
      if (pingTimer !== null) {
        window.clearInterval(pingTimer);
      }
      pingTimer = window.setInterval(sendPing, PING_INTERVAL_MS);
    };

    ws.onmessage = function (event) {
      handleMessage(event.data);
    };

    ws.onclose = function () {
      if (socket === ws) {
        socket = null;
      }
      if (pingTimer !== null) {
        window.clearInterval(pingTimer);
        pingTimer = null;
      }
      if (stopped) {
        return;
      }
      store.setConnection('offline');
      scheduleReconnect();
    };

    ws.onerror = function () {
      // onerror 之后浏览器一定会再触发 onclose，重连逻辑只写在 onclose 里，
      // 避免两条路径各自安排一次重连。
    };
  }

  /**
   * 看门狗。只做一件事：发现"连接看起来还在、其实已经死了"。
   */
  function watchdog() {
    if (stopped) {
      return;
    }
    if (!socket || socket.readyState !== 1) {
      return;
    }
    if (Date.now() - lastMessageAt < STALE_AFTER_MS) {
      return;
    }
    // 主动关掉它，走 onclose 的常规重连路径——保持"重连只有一条路径"。
    try {
      socket.close();
    } catch (error) {
      socket = null;
      scheduleReconnect();
    }
  }

  function onVisibilityChange() {
    if (document.visibilityState !== 'visible' || stopped) {
      return;
    }
    // 回到前台时立刻判断一次，不等下一个看门狗周期。
    if (!socket || socket.readyState !== 1) {
      reconnectAttempt = 0;
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      connect();
    } else {
      watchdog();
    }
  }

  function onOnline() {
    if (stopped) {
      return;
    }
    reconnectAttempt = 0;
    if (!socket || socket.readyState !== 1) {
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      connect();
    }
  }

  function start(options) {
    options = options || {};
    notify = options.onNotice || null;
    if (!window.WebSocket) {
      store.state.unsupported = 'websocket';
      store.setConnection('offline');
      return;
    }
    stopped = false;
    reconnectAttempt = 0;
    lastMessageAt = Date.now();
    store.setConnection('connecting');

    window.addEventListener('online', onOnline);
    document.addEventListener('visibilitychange', onVisibilityChange);
    watchdogTimer = window.setInterval(watchdog, WATCHDOG_INTERVAL_MS);

    connect();
  }

  function stop() {
    stopped = true;
    if (pingTimer !== null) {
      window.clearInterval(pingTimer);
      pingTimer = null;
    }
    if (watchdogTimer !== null) {
      window.clearInterval(watchdogTimer);
      watchdogTimer = null;
    }
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    window.removeEventListener('online', onOnline);
    document.removeEventListener('visibilitychange', onVisibilityChange);
    if (socket) {
      try {
        socket.close();
      } catch (error) {
        // 已经没有对端了。
      }
      socket = null;
    }
  }

  HD.sync = {
    start: start,
    stop: stop,
    resync: resync,
    setNoticeHandler: function (handler) {
      notify = handler;
    },
    STALE_AFTER_MS: STALE_AFTER_MS,
  };
})();

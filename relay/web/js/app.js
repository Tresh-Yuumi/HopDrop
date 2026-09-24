/* HopDrop 前端：装配与渲染调度。
 *
 * 这一层只做三件事：把 `store` / `render` / `actions` 接起来、把界面反馈
 * （状态行、浮层、抽屉）实现出来、在启动时把一次性的事情做掉（能力探测、
 * 偏好读取、首屏快照）。
 *
 * **渲染策略是"任何变化都全量重绘"**，不做局部更新。理由：单个区域最多
 * 显示一百来条消息，重建这点 DOM 的耗时在毫秒级；而"只更新变化的那一条"
 * 要求每处改动都记得同步所有派生显示（计数、顺序、置顶位置、编辑态），
 * 漏一处就是界面和状态对不上——那类问题比一次多余的重绘昂贵得多。
 *
 * 唯一需要额外照顾的是**滚动位置**：全量重绘会把 `scrollTop` 归零，所以
 * 重绘前后要把它放回去。见 `renderNotes`。
 */

(function () {
  'use strict';

  var HD = (window.HD = window.HD || {});
  var store = HD.store;
  var util = HD.util;
  var actions = HD.actions;
  var render = HD.render;

  // 滚动位置在这个值以内就认为是"贴在顶部"。贴在顶部时新消息到达应当留在
  // 顶部（看得见最新那条）；否则恢复原来的位置，不打断用户正在读的地方。
  var STICKY_TOP_PX = 80;

  var SETTING_PASTE_TO_SEND = 'hopdrop.pasteToSend';
  // 上次停留的区域。刷新后回到原处，而不是每次都跳回第一个区域——对一个
  // "常驻在某个区域里收发文本"的工具来说，跳回去等于每次刷新都要重新找。
  var SETTING_ACTIVE_BOARD = 'hopdrop.activeBoard';

  var appEl = null;
  var toastTimer = null;
  var pasteToSend = false;
  var els = {};

  function byId(id) {
    return document.getElementById(id);
  }

  // ---------------------------------------------------------------- 界面反馈

  function setStatus(text, kind) {
    if (!els.status) {
      return;
    }
    els.status.textContent = text || '';
    els.status.className = 'composer__status' + (kind ? ' composer__status--' + kind : '');
  }

  function toast(text, kind) {
    if (!els.toast) {
      return;
    }
    els.toast.textContent = text;
    els.toast.className = 'toast' + (kind ? ' toast--' + kind : '');
    els.toast.hidden = false;
    if (toastTimer !== null) {
      window.clearTimeout(toastTimer);
    }
    toastTimer = window.setTimeout(function () {
      els.toast.hidden = true;
      toastTimer = null;
    }, 2600);
  }

  var CONNECTION_LABELS = {
    connecting: '连接中',
    syncing: '同步中',
    online: '已同步',
    offline: '重连中',
  };

  var ui = {
    status: setStatus,
    toast: toast,

    setSendEnabled: function (enabled) {
      if (!els.send) {
        return;
      }
      var board = store.activeBoard();
      els.send.disabled = !enabled || !store.canAppend(board);
    },

    setMoreBusy: function (busy) {
      if (els.moreBtn) {
        els.moreBtn.disabled = busy;
        els.moreBtn.textContent = busy ? '加载中…' : '加载更早的消息';
      }
    },

    focusInput: function () {
      if (els.input && document.visibilityState === 'visible') {
        els.input.focus();
      }
    },

    renderDevices: function (data) {
      if (!els.devices || !data) {
        return;
      }
      util.clear(els.devices);
      els.devices.appendChild(
        render.deviceList(document, data.devices || [], {
          now: util.nowSeconds(),
          onRename: actions.renameDevice,
          onRevoke: actions.revokeDevice,
        })
      );
    },

    /**
     * 展示新生成的主人链接。用绝对 URL——这条链接大概率要在**另一台设备**
     * 上打开，给相对路径等于让用户自己拼域名。
     */
    showRotateResult: function (pairPath) {
      if (!els.rotateResult) {
        return;
      }
      var absolute = window.location.origin + pairPath;
      util.clear(els.rotateResult);
      els.rotateResult.hidden = false;
      els.rotateResult.appendChild(
        util.el(document, 'span', { text: '新主人链接（只显示这一次，请立即保存）：' })
      );
      els.rotateResult.appendChild(util.el(document, 'br'));
      var link = util.el(document, 'code', { text: absolute });
      els.rotateResult.appendChild(link);
      els.rotateResult.appendChild(util.el(document, 'br'));
      var copyBtn = util.el(document, 'button', {
        type: 'button',
        className: 'btn btn--tiny',
        text: '复制链接',
      });
      copyBtn.addEventListener('click', function () {
        util.copyText(absolute).then(function (ok) {
          if (ok) {
            copyBtn.textContent = '已复制 ✓';
            window.setTimeout(function () {
              copyBtn.textContent = '复制链接';
            }, 1500);
          } else if (util.selectNode(link)) {
            setStatus('内容已选中，请按 Ctrl/⌘+C 复制', 'error');
          } else {
            setStatus('复制失败，请手动选中', 'error');
          }
        });
      });
      els.rotateResult.appendChild(copyBtn);
    },

    openSettings: function () {
      if (!els.settings) {
        return;
      }
      els.settings.hidden = false;
      if (els.scrim) {
        els.scrim.hidden = false;
      }
      if (els.settingsToggle) {
        els.settingsToggle.setAttribute('aria-expanded', 'true');
      }
      actions.refreshDevices();
    },

    closeSettings: function () {
      if (!els.settings) {
        return;
      }
      els.settings.hidden = true;
      if (els.scrim) {
        els.scrim.hidden = true;
      }
      if (els.settingsToggle) {
        els.settingsToggle.setAttribute('aria-expanded', 'false');
      }
    },
  };
  HD.ui = ui;

  // ---------------------------------------------------------------- 渲染

  function renderConnection() {
    if (!els.conn) {
      return;
    }
    var state = store.state.connection;
    els.conn.setAttribute('data-state', state);
    els.conn.textContent = CONNECTION_LABELS[state] || state;
  }

  function renderTabs() {
    if (!els.boards) {
      return;
    }
    util.clear(els.boards);
    els.boards.appendChild(
      render.boardTabs(document, store.state.boards, store.state.activeBoardId, function (boardId) {
        store.setActiveBoard(boardId);
      })
    );
  }

  function renderNotices() {
    if (!els.notices) {
      return;
    }
    // 只清掉脚本生成的那一条，服务端渲染的提示（开发模式警告、不支持
    // WebSocket）保留——它们在页面生命周期里不会变。
    var dynamic = els.notices.querySelector('[data-dynamic="1"]');
    if (dynamic) {
      els.notices.removeChild(dynamic);
    }

    var board = store.activeBoard();
    if (!board || !board.expiringSoon || board.expired) {
      return;
    }

    // 方案 11.3：区域即将到期 → 顶部提醒条提供续期、导出和立即归档。
    // **"立即归档"在 M5 不做**：`POST /api/boards/{id}/archive` 属于 M9，
    // 这里不渲染一个点了会 404 的按钮。
    var bar = util.el(document, 'div', { className: 'notice', dataset: { dynamic: '1' } });
    bar.appendChild(
      util.el(document, 'span', {
        text:
          '这个区域将在 ' +
          util.formatRemaining(board.expiresAt, util.nowSeconds()) +
          '后到期，到期后只能导出，不能继续写入。',
      })
    );
    var renew = util.el(document, 'button', {
      type: 'button',
      className: 'btn btn--tiny',
      text: '续期',
    });
    renew.addEventListener('click', function () {
      actions.boardAction('retention-' + (board.retention || 60), board);
    });
    bar.appendChild(renew);
    var exportBtn = util.el(document, 'button', {
      type: 'button',
      className: 'btn btn--tiny',
      text: '导出',
    });
    exportBtn.addEventListener('click', function () {
      actions.boardAction('export-txt', board);
    });
    bar.appendChild(exportBtn);
    els.notices.insertBefore(bar, els.notices.firstChild);
  }

  function renderBoardBar() {
    if (!els.boardbar) {
      return;
    }
    util.clear(els.boardbar);
    els.boardbar.appendChild(
      render.boardBar(document, store.activeBoard(), {
        now: util.nowSeconds(),
        canManageBoards: store.canManageBoards(),
        onAction: actions.boardAction,
      })
    );
  }

  function renderNotes() {
    if (!els.notes) {
      return;
    }
    var previous = els.notes.scrollTop;
    var board = store.activeBoard();
    var notes = board ? store.visibleNotes(board.id) : [];

    var fragment = render.messageList(document, {
      notes: notes,
      now: util.nowSeconds(),
      editingId: store.state.editingId,
      canModify: store.canModify,
      canPin: store.canPin,
      onCopy: actions.copyNote,
      onStartEdit: actions.startEdit,
      onCancelEdit: actions.cancelEdit,
      onSaveEdit: actions.saveEdit,
      onDelete: actions.removeNote,
      onTogglePin: actions.togglePin,
    });

    util.clear(els.notes);
    els.notes.appendChild(fragment);

    // 全量重绘会把滚动位置归零。贴在顶部时保持 0（新消息应当出现在眼前），
    // 否则放回原处——否则每收到一条推送，正在读旧消息的人就被弹回顶部。
    els.notes.scrollTop = previous <= STICKY_TOP_PX ? 0 : previous;
  }

  function renderMore() {
    if (!els.more) {
      return;
    }
    var board = store.activeBoard();
    // 服务端的 noteCount 是"这个区域一共多少条（不含已删除）"，本地 notes
    // 是"手里有多少条"。两者相等就说明全在手里了，按钮收起来。
    var hasMore =
      !!board && !board.reachedEnd && board.notes.length > 0 && board.noteCount > board.notes.length;
    els.more.hidden = !hasMore;
  }

  function renderComposer() {
    var board = store.activeBoard();
    var writable = store.canAppend(board);
    if (els.composer) {
      if (writable) {
        els.composer.classList.remove('composer--disabled');
      } else {
        els.composer.classList.add('composer--disabled');
      }
    }
    if (els.input) {
      els.input.disabled = !writable;
      if (!board) {
        els.input.setAttribute('placeholder', '还没有可用的区域');
      } else if (board.expired) {
        els.input.setAttribute('placeholder', '这个区域已到期，请在"更多操作"里续期');
      } else if (board.status !== 'active') {
        els.input.setAttribute('placeholder', '这个区域已归档，只读');
      } else {
        els.input.setAttribute('placeholder', '粘贴或输入文本，Ctrl + Enter 发送');
      }
    }
    ui.setSendEnabled(true);
  }

  function renderRoleText() {
    if (!els.roleText) {
      return;
    }
    els.roleText.textContent =
      store.state.role === 'owner'
        ? '这台设备是主人，可以管理区域、置顶消息和撤销其他设备。'
        : '这台设备是访客，只能看到访客区，且只能修改自己发出的消息。';
  }

  function renderAll() {
    renderConnection();
    renderTabs();
    renderBoardBar();
    renderNotices();
    renderNotes();
    renderMore();
    renderComposer();
  }

  // ---------------------------------------------------------------- 事件

  function onInputChanged() {
    actions.updateCount();
  }

  function onPaste(event) {
    if (!pasteToSend || !els.input) {
      return;
    }
    // 已经有草稿时不抢这次粘贴：那时用户的意图是"用剪贴板内容替换草稿"，
    // 直接发出去会把还没写完的东西也一起发走。
    if (els.input.value.trim()) {
      return;
    }
    var text = event.clipboardData ? event.clipboardData.getData('text/plain') : '';
    if (!text) {
      return;
    }
    event.preventDefault();
    els.input.value = text;
    actions.updateCount();
    actions.send();
  }

  function onKeyDown(event) {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      actions.send();
    }
  }

  function onDocumentKeyDown(event) {
    if (event.key === 'Escape') {
      ui.closeSettings();
    }
  }

  function onTogglePasteToSend(event) {
    pasteToSend = !!event.currentTarget.checked;
    util.writePref(SETTING_PASTE_TO_SEND, pasteToSend ? '1' : '0');
  }

  function bind() {
    if (els.send) {
      els.send.addEventListener('click', actions.send);
    }
    if (els.input) {
      els.input.addEventListener('input', onInputChanged);
      els.input.addEventListener('paste', onPaste);
      els.input.addEventListener('keydown', onKeyDown);
    }
    if (els.readClip) {
      els.readClip.addEventListener('click', actions.readClipboard);
    }
    if (els.moreBtn) {
      els.moreBtn.addEventListener('click', actions.loadMore);
    }
    if (els.settingsToggle) {
      els.settingsToggle.addEventListener('click', ui.openSettings);
    }
    if (els.settingsClose) {
      els.settingsClose.addEventListener('click', ui.closeSettings);
    }
    if (els.scrim) {
      els.scrim.addEventListener('click', ui.closeSettings);
    }
    if (els.pasteSend) {
      els.pasteSend.addEventListener('change', onTogglePasteToSend);
    }
    if (els.revokeOthers) {
      els.revokeOthers.addEventListener('click', actions.revokeOtherDevices);
    }
    if (els.rotateSecret) {
      els.rotateSecret.addEventListener('click', actions.rotateOwnerSecret);
    }
    if (els.logout) {
      els.logout.addEventListener('click', actions.logout);
    }
    document.addEventListener('keydown', onDocumentKeyDown);

    store.subscribe(renderAll);
    // 单独一个订阅者做持久化，不塞进 renderAll：渲染是"把状态画出来"，
    // 写 localStorage 是副作用，混在一起会让"只重画一次"变成"写一次盘"。
    store.subscribe(function (reason) {
      if (reason === 'active-board') {
        util.writePref(SETTING_ACTIVE_BOARD, store.state.activeBoardId || '');
      }
    });
  }

  /**
   * 能力探测（方案 5.3 的验收项）。
   *
   * **不支持读取剪贴板时按钮不出现**，而不是渲染一个点了报错的按钮。这里的
   * 探测只看方法是否存在；方法存在但被用户拒绝授权的情况，由调用时那条
   * 降级路径处理（提示手动粘贴）。
   */
  function detectCapabilities() {
    if (els.readClip && util.hasClipboardRead()) {
      els.readClip.hidden = false;
    }
    // 没有 WebSocket 时页面仍然可用（HTTP 接口都在），只是不会自动更新。
    // 服务端把那条提示预先渲染好了，这里只决定显不显示。
    if (!window.WebSocket) {
      var unsupported = byId('hd-unsupported');
      if (unsupported) {
        unsupported.hidden = false;
      }
    }
  }

  function restorePrefs() {
    pasteToSend = util.readPref(SETTING_PASTE_TO_SEND, '0') === '1';
    if (els.pasteSend) {
      els.pasteSend.checked = pasteToSend;
    }
  }

  /**
   * 会话失效（Cookie 被撤销或过期）时的收尾。
   *
   * 方案 11.3：**清除本地状态并返回首页**。刻意不弹"是否重新登录"——这台
   * 设备已经没有凭证了，除了重新配对没有别的出路，多一次询问只是拖时间。
   */
  function handleUnauthorized() {
    HD.sync.stop();
    // 这台设备已经失去凭证了，下次进来必然重新配对——很可能是另一个房间。
    // 把上次停留的区域留着没有意义，而且在换房间后会指向一个不存在的区域。
    util.removePref(SETTING_ACTIVE_BOARD);
    toast('这台设备已被移除，正在返回首页', 'error');
    window.setTimeout(function () {
      window.location.replace('/');
    }, 1200);
  }

  function boot() {
    appEl = byId('hd-app');
    if (!appEl) {
      return;
    }
    els = {
      conn: byId('hd-conn'),
      boards: byId('hd-boards'),
      boardbar: byId('hd-boardbar'),
      notices: byId('hd-notices'),
      notes: byId('hd-notes'),
      more: byId('hd-more'),
      moreBtn: byId('hd-more-btn'),
      composer: byId('hd-composer'),
      input: byId('hd-input'),
      count: byId('hd-count'),
      status: byId('hd-status'),
      send: byId('hd-send'),
      readClip: byId('hd-read-clip'),
      settings: byId('hd-settings'),
      settingsToggle: byId('hd-settings-toggle'),
      settingsClose: byId('hd-settings-close'),
      scrim: byId('hd-scrim'),
      toast: byId('hd-toast'),
      devices: byId('hd-devices'),
      roleText: byId('hd-role-text'),
      pasteSend: byId('hd-paste-send'),
      revokeOthers: byId('hd-revoke-others'),
      rotateSecret: byId('hd-rotate-secret'),
      rotateResult: byId('hd-rotate-result'),
      logout: byId('hd-logout'),
    };
    store.init({
      role: appEl.getAttribute('data-role'),
      roomName: appEl.getAttribute('data-room-name'),
      deviceName: appEl.getAttribute('data-device-name'),
      activeBoardId: util.readPref(SETTING_ACTIVE_BOARD, ''),
    });

    HD.api.setUnauthorizedHandler(handleUnauthorized);
    restorePrefs();
    detectCapabilities();
    bind();
    renderRoleText();
    renderConnection();

    HD.sync.setNoticeHandler(function (code, detail) {
      if (code === 'session_revoked') {
        handleUnauthorized();
      } else if (code === 'connection_limit') {
        toast(detail.message || '连接数已达上限，请先关闭其他页面', 'error');
      } else if (code === 'resync_failed') {
        setStatus('同步失败：' + detail.message + '，稍后会自动重试', 'error');
      } else if (code === 'server_error') {
        toast(detail.message || '服务端提示了一条错误', 'error');
      }
    });

    // 首屏先走一次 HTTP 快照：**它是基线，WebSocket 只是加速器。**
    // 顺序反过来的话，浏览器不支持或拦掉 WebSocket 时页面就永远空着。
    HD.sync.resync('首屏加载').then(function () {
      return actions.refreshDevices();
    }).then(function () {
      HD.sync.start();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();

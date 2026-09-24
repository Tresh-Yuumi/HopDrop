/* HopDrop 前端：用户操作。
 *
 * 每个动作都是"请求 → 用响应就地更新状态 → 交给渲染"。**不等推送回来再更新**
 * 是刻意的：网络正常时两种做法看不出差别，但推送丢一次，用户就会看到"我发的
 * 消息没出现"，然后以为发送失败了。HTTP 响应本身就是权威结果，直接用它。
 *
 * 与服务端推送不会打架：状态更新走 `store.upsertNote`，它按 id 去重，
 * 响应和推送谁先到都收敛到同一个结果。
 */

(function () {
  'use strict';

  var HD = (window.HD = window.HD || {});
  var store = HD.store;
  var util = HD.util;
  var api = HD.api;

  /** 已发送但还没落定的那一次提交。见 `send()` 里对幂等键的说明。 */
  var pendingSend = null;
  var sending = false;

  function ui() {
    // 运行时取，避免加载顺序造成耦合。
    return HD.ui;
  }

  function describe(error) {
    if (error && error.code === 'network_error') {
      return '网络不可达，草稿还在输入框里，恢复后再点发送即可';
    }
    return (error && error.message) || '操作失败';
  }

  // ---------------------------------------------------------------- 发消息

  /**
   * 发送输入框里的内容。
   *
   * **幂等键的处理是这里唯一需要小心的东西。** 请求发出去但没收到响应时
   * （断网、切后台被系统冻结），消息到底有没有写进库里是不知道的。此时重试
   * 若换一个新的 `mutationId`，就会产生第二条重复消息；沿用同一个键，服务端
   * 认出这是重放，返回原结果并且**不递增 rev**（方案 4.3）。
   *
   * 所以规则是：**内容没变就复用上次的键，内容变了才换新键。** 内容相同且
   * 上一次已经成功，说明用户是想再发一条一样的内容——那时 `pendingSend`
   * 已经在成功分支里清掉了，新键，行为正确。
   */
  function send() {
    var input = document.getElementById('hd-input');
    if (!input || sending) {
      return;
    }
    var content = input.value;
    if (!content.trim()) {
      ui().status('内容不能为空', 'error');
      return;
    }

    var board = store.activeBoard();
    if (!board) {
      ui().status('没有可用的区域', 'error');
      return;
    }
    if (!store.canAppend(board)) {
      ui().status(
        board.expired ? '这个区域已到期，请在"更多操作"里续期' : '这个区域已归档，不能再写入',
        'error'
      );
      return;
    }

    if (pendingSend && pendingSend.content === content) {
      // 沿用上次的幂等键。
    } else {
      pendingSend = { content: content, mutationId: util.randomId() };
    }
    var attempt = pendingSend;

    sending = true;
    ui().setSendEnabled(false);
    ui().status('发送中…');

    api.createNote(board.id, content, attempt.mutationId).then(
      function (result) {
        sending = false;
        ui().setSendEnabled(true);
        pendingSend = null;
        if (result && result.note) {
          store.upsertNote(result.note);
          store.emit('send');
        }
        input.value = '';
        updateCount();
        ui().status('');
        ui().focusInput();
      },
      function (error) {
        sending = false;
        ui().setSendEnabled(true);
        // 草稿留在输入框里。**不清空也不自动重试**——自动重试会让用户在
        // 不知情的情况下发出消息，而这条消息可能是他不打算发的。
        ui().status(describe(error), 'error');
      }
    );
  }

  function updateCount() {
    var input = document.getElementById('hd-input');
    var count = document.getElementById('hd-count');
    if (!input || !count) {
      return;
    }
    var text = input.value;
    if (!text) {
      count.textContent = '';
      return;
    }
    var bytes = util.utf8Bytes(text);
    var app = document.getElementById('hd-app');
    var max = app ? parseInt(app.getAttribute('data-note-max-bytes'), 10) || 0 : 0;
    count.textContent = max ? bytes + ' / ' + max + ' 字节' : bytes + ' 字节';
  }

  // ---------------------------------------------------------------- 复制

  function copyWithFeedback(element, text, fallbackNode) {
    return util.copyText(text).then(function (ok) {
      if (ok) {
        if (element) {
          var original = element.textContent;
          element.textContent = '已复制 ✓';
          element.disabled = true;
          window.setTimeout(function () {
            element.textContent = original;
            element.disabled = false;
          }, 1500);
        } else {
          ui().status('已复制', 'ok');
        }
        return true;
      }
      // 降级：选中内容，让用户自己按 Ctrl/⌘+C（方案 5.1）。
      if (fallbackNode && util.selectNode(fallbackNode)) {
        ui().status('浏览器未授权剪贴板写入，内容已选中，请按 Ctrl/⌘+C 复制', 'error');
      } else {
        ui().status('复制失败，请手动选中后复制', 'error');
      }
      return false;
    });
  }

  function copyNote(note, element) {
    // 从按钮往上找它所属的那张卡片，不按 id 拼选择器——拼选择器要依赖
    // "id 里没有特殊字符"这个外部事实，而向上找是结构关系，永远成立。
    var article = element && element.closest ? element.closest('.note') : null;
    var body = article ? article.querySelector('.note__body') : null;
    return copyWithFeedback(element, note.content, body);
  }

  // ---------------------------------------------------------------- 剪贴板

  function readClipboard() {
    var input = document.getElementById('hd-input');
    if (!input) {
      return;
    }
    if (!util.hasClipboardRead()) {
      ui().status('当前浏览器不支持读取剪贴板，请用 Ctrl/⌘+V 粘贴', 'error');
      return;
    }
    navigator.clipboard.readText().then(
      function (text) {
        if (!text) {
          ui().status('剪贴板里没有文本');
          return;
        }
        input.value = text;
        updateCount();
        // 读取成功之后**不自动发送**，即使开了"粘贴即发送"——方案 5.1 让
        // 这个开关管的是"粘贴"这个动作，而点"读取剪贴板"是另一个明确的
        // 动作，它的下一步理应是让用户看一眼。
        input.focus();
        ui().status('已读取剪贴板，确认后点发送');
      },
      function (error) {
        ui().status('读取剪贴板被拒绝，请在输入框里按 Ctrl/⌘+V 粘贴', 'error');
      }
    );
  }

  // ---------------------------------------------------------------- 编辑

  function startEdit(note) {
    store.setEditing(note.id);
  }

  function cancelEdit() {
    store.setEditing(null);
  }

  function saveEdit(note, content) {
    if (content === note.content) {
      store.setEditing(null);
      return;
    }
    if (!content.trim()) {
      ui().status('内容不能为空', 'error');
      return;
    }
    api.updateNote(note.id, { content: content }).then(
      function (result) {
        store.setEditing(null);
        if (result && result.note) {
          store.upsertNote(result.note);
          store.emit('edit');
        }
      },
      function (error) {
        ui().status(describe(error), 'error');
      }
    );
  }

  function removeNote(note) {
    if (!window.confirm('删除这条消息？它会进入回收站，7 天后彻底删除。')) {
      return;
    }
    api.deleteNote(note.id).then(
      function () {
        store.removeNote(note.boardId, note.id);
        var board = store.getBoard(note.boardId);
        if (board) {
          board.noteCount = Math.max(0, board.noteCount - 1);
        }
        store.emit('delete');
      },
      function (error) {
        ui().status(describe(error), 'error');
      }
    );
  }

  function togglePin(note) {
    api.updateNote(note.id, { pinned: !note.pinned }).then(
      function (result) {
        if (result && result.note) {
          store.upsertNote(result.note);
          store.emit('pin');
        }
      },
      function (error) {
        ui().status(describe(error), 'error');
      }
    );
  }

  // ---------------------------------------------------------------- 历史

  /**
   * 加载更早的消息。
   *
   * 游标的来源有两个，见 `store.appendOlderNotes` 的说明：翻过第一页之后用
   * 服务端给的游标；**第一页没有游标**（快照不带），那时用本地最旧那条消息
   * 的 id 当锚点。
   */
  function loadMore() {
    var board = store.activeBoard();
    if (!board || !board.notes.length) {
      return;
    }
    var cursor = board.nextCursor || board.notes[board.notes.length - 1].id;
    ui().setMoreBusy(true);

    // 这里是分页接口，响应里不含 rev——它只是一个读取动作，不改变状态，
    // 所以不需要（也不应该）去动本地 rev。
    api.boardNotes(board.id, cursor).then(
      function (result) {
        ui().setMoreBusy(false);
        if (!result) {
          return;
        }
        // 响应里带了最新的 board（含 noteCount），用它刷新计数——不然
        // "共 N 条"会在删改之后一直停在旧值上。
        if (result.board) {
          var current = store.getBoard(result.board.id);
          if (current) {
            current.noteCount = result.board.noteCount;
            current.expired = result.board.expired;
            current.expiresAt = result.board.expiresAt;
          }
        }
        store.appendOlderNotes(board.id, result.notes || [], result.nextCursor);
      },
      function (error) {
        ui().setMoreBusy(false);
        ui().status(describe(error), 'error');
      }
    );
  }

  // ---------------------------------------------------------------- 区域

  function createBoard() {
    var name = window.prompt('新区域的名字（最多 64 个字）', '');
    if (name === null) {
      return;
    }
    if (!name.trim()) {
      ui().status('区域名不能为空', 'error');
      return;
    }
    var tier = window.prompt('保留期：输入 30、60，或 0 表示永久', '60');
    if (tier === null) {
      return;
    }
    var retention = parseInt(tier, 10);
    if (retention !== 0 && retention !== 30 && retention !== 60) {
      ui().status('保留期只能是 30、60 或 0（永久）', 'error');
      return;
    }
    api.createBoard(name.trim(), retention).then(
      function (result) {
        if (!result || !result.board) {
          return;
        }
        // 用响应直接建出来，**不等推送**：这样新建的区域立刻出现，用户不用
        // 盯着界面猜。之后到达的 `board.created` 推送会因为区域已存在而
        // 直接返回成功（`applyEvent` 里有去重），不会出现两个。
        store.addBoard(result.board);
        store.setActiveBoard(result.board.id);
      },
      function (error) {
        ui().status(describe(error), 'error');
      }
    );
  }

  function renameBoard(board) {
    var name = window.prompt('新的区域名', board.name);
    if (name === null || name === board.name) {
      return;
    }
    if (!name.trim()) {
      ui().status('区域名不能为空', 'error');
      return;
    }
    api.updateBoard(board.id, { name: name.trim() }).then(
      function (result) {
        applyBoardResult(result);
      },
      function (error) {
        ui().status(describe(error), 'error');
      }
    );
  }

  function changeRetention(board, retention) {
    var label = retention === 0 ? '永久' : retention + ' 天';
    if (!window.confirm('把「' + board.name + '」的保留期改为' + label + '？到期时间会从此刻重新计算。')) {
      return;
    }
    api.updateBoard(board.id, { retention: retention }).then(
      function (result) {
        applyBoardResult(result);
      },
      function (error) {
        ui().status(describe(error), 'error');
      }
    );
  }

  /** 区域改名/续期的响应直接合并进状态；推送会把同一份数据再送一次，无害。 */
  function applyBoardResult(result) {
    if (!result || !result.board) {
      return;
    }
    var target = store.getBoard(result.board.id);
    if (!target) {
      return;
    }
    Object.keys(result.board).forEach(function (key) {
      target[key] = result.board[key];
    });
    store.emit('board-updated');
  }

  function downloadExport(board, format) {
    var anchor = document.createElement('a');
    anchor.href = api.exportUrl(board.id, format);
    anchor.download = '';
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
  }

  /**
   * 复制整区文本。
   *
   * 走导出接口而不是把界面上已加载的消息拼起来：界面上只有最近 N 条，而
   * "复制整区"要的是整个区域。失败时降级为下载导出文件——用户要的东西
   * 仍然拿得到。
   */
  function copyBoardText(board) {
    ui().status('正在取整区内容…');
    window
      .fetch(api.exportUrl(board.id, 'txt'), { credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('导出失败（HTTP ' + response.status + '）');
        }
        return response.text();
      })
      .then(function (text) {
        return util.copyText(text).then(function (ok) {
          if (ok) {
            ui().status('已复制整区内容（' + text.length + ' 字符）', 'ok');
          } else {
            ui().status('浏览器未授权写入剪贴板，已改为下载导出文件', 'error');
            downloadExport(board, 'txt');
          }
          return undefined;
        });
      })
      .catch(function (error) {
        ui().status((error && error.message) || '导出失败', 'error');
      });
  }

  function boardAction(value, board) {
    if (value === 'board-new') {
      createBoard();
    } else if (value === 'board-rename') {
      renameBoard(board);
    } else if (value.indexOf('retention-') === 0) {
      changeRetention(board, parseInt(value.slice('retention-'.length), 10));
    } else if (value === 'export-txt') {
      downloadExport(board, 'txt');
    } else if (value === 'export-md') {
      downloadExport(board, 'md');
    } else if (value === 'copy-board') {
      copyBoardText(board);
    }
  }

  // ---------------------------------------------------------------- 设备

  function refreshDevices() {
    return api.devices().then(
      function (data) {
        ui().renderDevices(data);
        return data;
      },
      function (error) {
        ui().status(describe(error), 'error');
        return null;
      }
    );
  }

  function renameDevice(device) {
    var name = window.prompt('设备名', device.name);
    if (name === null || name === device.name) {
      return;
    }
    if (!name.trim()) {
      ui().status('设备名不能为空', 'error');
      return;
    }
    api.renameDevice(device.id, name.trim()).then(
      function () {
        return refreshDevices();
      },
      function (error) {
        ui().status(describe(error), 'error');
      }
    );
  }

  function revokeDevice(device) {
    if (
      !window.confirm(
        '撤销「' + device.name + '」？这台设备会立即退出，需要重新配对才能回来。'
      )
    ) {
      return;
    }
    api.revokeDevice(device.id).then(
      function () {
        ui().toast('已撤销「' + device.name + '」');
        return refreshDevices();
      },
      function (error) {
        ui().status(describe(error), 'error');
      }
    );
  }

  function revokeOtherDevices() {
    if (!window.confirm('撤销除当前设备外的全部设备？它们需要重新配对才能回来。')) {
      return;
    }
    api.revokeOtherDevices().then(
      function (result) {
        ui().toast('已撤销 ' + (result ? result.revoked : 0) + ' 台设备');
        return refreshDevices();
      },
      function (error) {
        ui().status(describe(error), 'error');
      }
    );
  }

  function rotateOwnerSecret() {
    if (
      !window.confirm(
        '重置主人链接？旧链接会立即失效，已配对的设备不受影响。新链接只会显示这一次。'
      )
    ) {
      return;
    }
    api.rotateOwnerSecret().then(
      function (result) {
        ui().showRotateResult(result.pairPath);
      },
      function (error) {
        ui().status(describe(error), 'error');
      }
    );
  }

  function logout() {
    if (!window.confirm('退出当前设备？其他设备不受影响。')) {
      return;
    }
    api
      .logout()
      .then(function () {
        // 用 replace 而不是 href：退出的页面不该留在浏览器的返回栈里，
        // 否则按返回键会看到已经失效的界面。
        window.location.replace('/');
      })
      .catch(function () {
        // 即使请求失败也回首页——登出接口是幂等的，而让用户卡在一个
        // 删不掉的登录态里更糟。
        window.location.replace('/');
      });
  }

  HD.actions = {
    send: send,
    updateCount: updateCount,
    copyNote: copyNote,
    readClipboard: readClipboard,
    startEdit: startEdit,
    cancelEdit: cancelEdit,
    saveEdit: saveEdit,
    removeNote: removeNote,
    togglePin: togglePin,
    loadMore: loadMore,
    boardAction: boardAction,
    refreshDevices: refreshDevices,
    renameDevice: renameDevice,
    revokeDevice: revokeDevice,
    revokeOtherDevices: revokeOtherDevices,
    rotateOwnerSecret: rotateOwnerSecret,
    logout: logout,
  };
})();

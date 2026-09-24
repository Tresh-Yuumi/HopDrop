/* HopDrop 前端：本地状态。
 *
 * **状态的唯一来源是服务端快照。** 这个模块不发明任何东西，它只做四件事：
 * 装快照、按推送做增量、提供派生视图（显示顺序、权限判断）、通知订阅者。
 *
 * 增量应用有一条重要的边界：**拿不准的时候宁可要求重拉快照，也不要猜。**
 * 猜错的代价是"界面上少一条消息"或"顺序错乱"，而这些都是很难被用户描述
 * 清楚、也很难复现的问题；重拉的代价只是一次请求。所以 `applyEvent` 会明确
 * 返回"我处理不了"，由 `sync.js` 去拉快照。
 *
 * 显示顺序**不在这里算**，但排序规则定义在这里（`visibleNotes`）——顺序是
 * 派生数据，不存进数组。存了就要在增量的每条分支里维护它，那是 bug 的温床。
 */

(function () {
  'use strict';

  var HD = (window.HD = window.HD || {});
  var util = HD.util;

  var state = {
    ready: false,
    rev: 0,
    role: 'owner',
    room: { id: '', name: '' },
    device: { id: '', name: '' },
    /** 每个区域形如 `{...board, notes: [], windowLimit: n, nextCursor: null}` */
    boards: [],
    activeBoardId: null,
    /** connecting | online | offline */
    connection: 'connecting',
    /**
     * 正在被编辑的消息 id。放在状态里而不是各处的局部变量里，是因为渲染是
     * 全量重绘——编辑框要被重绘重建，它的"打开"状态就必须能从状态里读出来。
     */
    editingId: null,
    unsupported: null,
  };

  var listeners = [];

  function subscribe(listener) {
    listeners.push(listener);
  }

  function emit(reason) {
    for (var i = 0; i < listeners.length; i += 1) {
      listeners[i](reason);
    }
  }

  function init(seed) {
    state.role = seed.role || 'owner';
    state.room.name = seed.roomName || '';
    state.device.name = seed.deviceName || '';
    // 上次停留的区域，由 app.js 从本地偏好里读出来传进来。
    //
    // **必须在这里（快照到达之前）就写进 state。** applySnapshot 对
    // activeBoardId 的规则是"只要它指向的区域还在，就保留不动"；等到快照
    // 处理完再设，就只能被当成一次普通切换，而且首屏会先闪一下第一个区域。
    state.activeBoardId = seed.activeBoardId || null;
  }

  // ---------------------------------------------------------------- 查询

  function getBoard(boardId) {
    for (var i = 0; i < state.boards.length; i += 1) {
      if (state.boards[i].id === boardId) {
        return state.boards[i];
      }
    }
    return null;
  }

  function findNote(board, noteId) {
    for (var i = 0; i < board.notes.length; i += 1) {
      if (board.notes[i].id === noteId) {
        return board.notes[i];
      }
    }
    return null;
  }

  function getNote(noteId) {
    for (var i = 0; i < state.boards.length; i += 1) {
      var found = findNote(state.boards[i], noteId);
      if (found) {
        return found;
      }
    }
    return null;
  }

  function activeBoard() {
    return getBoard(state.activeBoardId) || state.boards[0] || null;
  }

  /**
   * 显示顺序：**置顶在前，其余按时间倒序**。
   *
   * 服务端只按 `created_at DESC, id DESC` 给，置顶只作为一个字段带出来。
   * 方案 4.1 只写了"主人可置顶消息"，没说是否浮到顶部；这里取"浮到顶部"，
   * 因为不浮起来的置顶没有任何可感知的效果。
   *
   * **一条已知边界**：置顶的消息只有落在快照窗口（最近 N 条）里才会浮上来。
   * 置顶一条早已滚出窗口的老消息，它不会凭空出现在界面上。要彻底解决得让
   * 服务端在快照里单独带上置顶消息，当前不做。
   */
  function visibleNotes(boardId) {
    var board = getBoard(boardId);
    if (!board) {
      return [];
    }
    var notes = board.notes.slice();
    notes.sort(function (left, right) {
      if (left.pinned !== right.pinned) {
        return left.pinned ? -1 : 1;
      }
      if (left.createdAt !== right.createdAt) {
        return right.createdAt - left.createdAt;
      }
      return left.id < right.id ? 1 : left.id > right.id ? -1 : 0;
    });
    return notes;
  }

  // ---------------------------------------------------------------- 权限

  /** 能不能往这个区域追加消息（方案 4.2：归档后不能写，到期未归档也不能写）。 */
  function canAppend(board) {
    if (!board) {
      return false;
    }
    return board.status === 'active' && !board.expired;
  }

  /**
   * 能不能改这条消息。服务端是最终裁判（403），这里只决定按钮渲不渲染——
   * 渲染一个点了必然报错的按钮比不渲染更差。
   */
  function canModify(note) {
    var board = getBoard(note.boardId);
    if (!board || board.status !== 'active') {
      return false;
    }
    if (state.role === 'owner') {
      return true;
    }
    return !!note.authorId && note.authorId === state.device.id;
  }

  function canPin() {
    return state.role === 'owner';
  }

  function canManageBoards() {
    return state.role === 'owner';
  }

  // ---------------------------------------------------------------- 写入

  function makeBoard(raw) {
    return {
      id: raw.id,
      name: raw.name,
      retention: raw.retention,
      expiresAt: raw.expiresAt === undefined ? null : raw.expiresAt,
      status: raw.status,
      isGuest: !!raw.isGuest,
      sortOrder: raw.sortOrder,
      createdAt: raw.createdAt,
      noteCount: raw.noteCount || 0,
      expired: !!raw.expired,
      expiringSoon: !!raw.expiringSoon,
      notes: [],
      // 快照一次给多少条。用第一次拿到的条数当窗口上限：之后每次收到
      // `note.created` 就往头部插一条，超出上限就丢掉最旧的。不这样做
      // 的话，一个挂了三天的页面会把这段时间的所有新消息都攒在内存里。
      windowLimit: 0,
      /** 翻到过最旧一条之后置真，用来收掉"加载更早的消息"按钮。 */
      reachedEnd: false,
    };
  }

  function applySnapshot(data) {
    state.rev = data.rev || 0;
    state.role = data.role || state.role;
    state.room = data.room || state.room;
    state.device = data.device || state.device;
    state.ready = true;

    var previousActive = state.activeBoardId;
    var boards = [];
    for (var i = 0; i < (data.boards || []).length; i += 1) {
      var raw = data.boards[i];
      var board = makeBoard(raw);
      board.notes = (raw.notes || []).slice();
      board.windowLimit = Math.max(board.notes.length, 20);
      boards.push(board);
    }
    state.boards = boards;

    if (!getBoard(previousActive)) {
      state.activeBoardId = boards.length ? boards[0].id : null;
    }
    if (!getBoard(state.activeBoardId) && boards.length) {
      state.activeBoardId = boards[0].id;
    }
    // 快照会重建全部区域，之前打开着的编辑框对应的消息可能已经不在了。
    if (state.editingId && !getNote(state.editingId)) {
      state.editingId = null;
    }
    emit('snapshot');
  }

  /**
   * 建一个本地区域。**幂等**——HTTP 响应与推送都会调到这里，谁先到都一样。
   *
   * `windowLimit` 给 20 而不是 0：新区域没有快照带来的历史窗口，但之后每条
   * `note.created` 都要往它里面放，窗口为 0 会让消息一进来就被裁掉。
   */
  function addBoard(raw) {
    if (!raw || getBoard(raw.id)) {
      return null;
    }
    var board = makeBoard(raw);
    board.windowLimit = 20;
    state.boards.push(board);
    if (!state.activeBoardId) {
      state.activeBoardId = board.id;
    }
    return board;
  }

  function upsertNote(note) {
    var board = getBoard(note.boardId);
    if (!board) {
      return false;
    }
    var existing = findNote(board, note.id);
    if (existing) {
      // 原地替换，保留数组位置——数组位置不参与显示顺序（顺序由
      // visibleNotes 现算），所以不需要为了排序去挪动它。
      var index = board.notes.indexOf(existing);
      board.notes[index] = note;
      return true;
    }
    board.notes.unshift(note);
    if (board.notes.length > board.windowLimit) {
      board.notes.length = board.windowLimit;
    }
    return true;
  }

  function removeNote(boardId, noteId) {
    var board = getBoard(boardId);
    if (!board) {
      return false;
    }
    var note = findNote(board, noteId);
    if (!note) {
      return false;
    }
    board.notes.splice(board.notes.indexOf(note), 1);
    return true;
  }

  /**
   * 应用一条已提交的变更。返回 `{ok: true}` 或 `{resync: '原因'}`。
   *
   * 调用方（`sync.js`）只有在 `ok` 时才把本地 rev 推进到这条推送的 rev；
   * 要求重拉时不推进——推进了就等于承认"我知道这一版的内容"，而实际上不知道。
   */
  function applyEvent(message) {
    var event = message.event;
    var payload = message.payload || {};

    if (event === 'board.created') {
      // 不在这里 emit：`sync.js` 在应用成功后统一发一次 'push'，两处都发会让
      // 订阅者收到两次通知、白渲染一遍。
      addBoard(payload.board);
      return { ok: true };
    }

    if (event === 'board.updated') {
      var incoming = payload.board;
      var target = incoming && getBoard(incoming.id);
      if (!target) {
        // 广播里的区域本地没有。可能是刚被别人删掉、也可能只是我们还没同步。
        // 两种情况都靠一次快照收场，不猜。
        return { resync: '收到了本地不存在的区域更新' };
      }
      // 逐字段覆盖，**不能整体替换**——整体替换会把 `notes` / `nextCursor`
      // 这些只有客户端才有的字段抹掉。
      Object.keys(incoming).forEach(function (key) {
        target[key] = incoming[key];
      });
      return { ok: true };
    }

    if (event === 'note.created') {
      var added = payload.note;
      if (!added) {
        return { ok: true };
      }
      var boardOfAdded = getBoard(added.boardId);
      if (!boardOfAdded) {
        // 不可见区域的推送（例如撤销访客设备前的残留连接）。忽略。
        return { ok: true };
      }
      if (!findNote(boardOfAdded, added.id)) {
        boardOfAdded.noteCount += 1;
      }
      upsertNote(added);
      return { ok: true };
    }

    if (event === 'note.updated') {
      var changed = payload.note;
      if (!changed) {
        return { ok: true };
      }
      var boardOfChanged = getBoard(changed.boardId);
      if (!boardOfChanged) {
        return { ok: true };
      }
      if (!findNote(boardOfChanged, changed.id)) {
        // 本地窗口里没有这条消息。**不能简单忽略**：置顶会把一条老消息
        // 提到显示顶部，忽略它就等于"在别的设备上置顶，这里看不到"。
        // 而这一条改动确实把 rev 推进了，所以我们的状态是旧的。
        return { resync: '更新的是本地窗口外的消息' };
      }
      upsertNote(changed);
      return { ok: true };
    }

    if (event === 'note.deleted') {
      if (removeNote(payload.boardId, payload.noteId)) {
        var boardOfRemoved = getBoard(payload.boardId);
        boardOfRemoved.noteCount = Math.max(0, boardOfRemoved.noteCount - 1);
      }
      return { ok: true };
    }

    // 不认识的变更类型：多半是版本不一致（客户端旧、服务端新）。此时本地
    // 状态是否还有效无法判断，拉一次快照至少能让其他部分是准的。
    return { resync: '不认识的变更类型：' + event };
  }

  /**
   * 追加一页更早的消息。
   *
   * `nextCursor` 由服务端给；为空表示已经翻到最旧一条。
   *
   * **游标为什么要单独管**：快照只给"最近 N 条"，不带游标，所以第一次
   * "加载更早"时没有游标可用——那时改用**本地最旧那条消息的 id** 作为游标。
   * 服务端的分页键是 `(created_at DESC, id DESC)`，用最旧那条做锚点正好
   * 接着往下取。这一段逻辑放在 `actions.loadMore` 里，因为"最旧那条"要看
   * 实际拥有的数据，而不是初始化时的形状。
   */
  function appendOlderNotes(boardId, rows, nextCursor) {
    var board = getBoard(boardId);
    if (!board) {
      return 0;
    }
    var appended = 0;
    for (var i = 0; i < rows.length; i += 1) {
      var row = rows[i];
      if (!findNote(board, row.id)) {
        board.notes.push(row);
        appended += 1;
      }
    }
    board.reachedEnd = !nextCursor;
    // 主动翻过历史之后，窗口上限跟着放宽——用户明确要看更多，就不该再按
    // 首屏那个条数裁掉内存里的消息。
    board.windowLimit += appended;
    emit('history');
    return appended;
  }

  function setActiveBoard(boardId) {
    if (state.activeBoardId === boardId) {
      return;
    }
    state.activeBoardId = boardId;
    state.editingId = null;
    emit('active-board');
  }

  function setConnection(value) {
    if (state.connection === value) {
      return;
    }
    state.connection = value;
    emit('connection');
  }

  function setEditing(noteId) {
    state.editingId = noteId;
    emit('editing');
  }

  HD.store = {
    state: state,
    init: init,
    subscribe: subscribe,
    emit: emit,

    getBoard: getBoard,
    getNote: getNote,
    activeBoard: activeBoard,
    visibleNotes: visibleNotes,

    canAppend: canAppend,
    canModify: canModify,
    canPin: canPin,
    canManageBoards: canManageBoards,

    applySnapshot: applySnapshot,
    applyEvent: applyEvent,
    addBoard: addBoard,
    upsertNote: upsertNote,
    removeNote: removeNote,
    appendOlderNotes: appendOlderNotes,

    setActiveBoard: setActiveBoard,
    setConnection: setConnection,
    setEditing: setEditing,
  };
})();

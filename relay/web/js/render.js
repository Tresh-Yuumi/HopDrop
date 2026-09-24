/* HopDrop 前端：渲染。
 *
 * **这个模块是"可复用渲染"的落点（方案 5.4 约束 1、阶段 1 的完成标准）。**
 * 悬浮窗通过 Document Picture-in-Picture 打开的是一个全新的 `Document`，
 * 主窗口与浮窗各自渲染一份 DOM（PiP 是把节点**移动**过去而不是复制，移动
 * 之后主窗口就没有了）。所以消息列表的渲染必须能被两份页面同时调用，且
 * 不能假设自己在哪个文档里。
 *
 * 为了让这条约束真的成立，这里的每个函数都遵守两条：
 *
 * 1. **不读全局 `document`，`doc` 一律由参数传入。** 节点用 `doc.createElement`
 *    建，选择用 `node.ownerDocument` 取。
 * 2. **不自己绑事件到全局。** 所有行为通过 `options` 里的回调交出去，调用方
 *    决定"点了之后做什么"。浮窗只传 `onCopy`，主窗口传全套——同一个函数，
 *    两种行为，而不是两份渲染代码。
 *
 * 安全上的一条硬规则：**全程 `textContent`，永不拼 HTML。** 消息正文是用户
 * 输入（方案 12 把"纯文本渲染"列为主防线），链接是唯一被结构化解析的东西，
 * 而且只认 `http://` 与 `https://` 两种协议（方案 4.1）。
 */

(function () {
  'use strict';

  var HD = (window.HD = window.HD || {});
  var util = HD.util;

  // 只匹配 http / https。写成这样而不是"匹配任意 URL 再检查协议"，是因为
  // 靠协议白名单把 `javascript:`、`data:` 这类从**入口**挡住——后面
  // `a.href = url` 时就不需要再判一次，也就不存在"漏判某个协议"的可能。
  var URL_PATTERN = /https?:\/\/[^\s<>"'`]+/g;
  // URL 末尾常跟着句读。中英文标点都要去掉，否则链接会多带一个句号。
  var TRAILING_PUNCTUATION = /[.,;:!?)\]}>'"，。；：！？）】》”’]+$/;

  /**
   * 把纯文本切成"文本 + 链接"的片段。
   *
   * 注意末尾标点的处理：若匹配到的整段里有一部分是标点，那部分**不消费**，
   * 留给下一段文本节点输出，这样原文一个字符都不会丢。
   */
  function linkifiedBody(doc, text) {
    var frag = doc.createDocumentFragment();
    var source = String(text);
    var cursor = 0;
    var match;

    URL_PATTERN.lastIndex = 0;
    while ((match = URL_PATTERN.exec(source)) !== null) {
      var raw = match[0];
      var url = raw.replace(TRAILING_PUNCTUATION, '');
      if (!url) {
        continue;
      }
      if (match.index > cursor) {
        frag.appendChild(doc.createTextNode(source.slice(cursor, match.index)));
      }
      var anchor = doc.createElement('a');
      anchor.href = url;
      anchor.textContent = url;
      anchor.target = '_blank';
      // noopener 防 `window.opener` 被反向操作；noreferrer 与全局的
      // Referrer-Policy 一致，避免把房间与区域的路径带到外站。
      anchor.rel = 'noopener noreferrer';
      frag.appendChild(anchor);
      cursor = match.index + url.length;
    }

    if (cursor < source.length) {
      frag.appendChild(doc.createTextNode(source.slice(cursor)));
    }
    return frag;
  }

  function button(doc, label, options) {
    var node = util.el(doc, 'button', {
      type: 'button',
      className: 'btn btn--tiny' + (options.modifier ? ' ' + options.modifier : ''),
      text: label,
      title: options.title || null,
    });
    if (options.onClick) {
      node.addEventListener('click', function (event) {
        event.preventDefault();
        options.onClick(node);
      });
    }
    return node;
  }

  function metaLine(doc, note, options) {
    var children = [
      util.el(doc, 'span', { className: 'note__author', text: note.authorName || '未知设备' }),
    ];
    if (note.pinned) {
      children.push(util.el(doc, 'span', { className: 'flag flag--accent', text: '置顶' }));
    }
    if (note.edited) {
      children.push(util.el(doc, 'span', { className: 'flag', text: '已编辑' }));
    }
    children.push(
      util.el(doc, 'span', {
        className: 'note__time',
        text: util.formatTime(note.createdAt, options.now),
        // 悬停看到精确时间。展示用相对时间是为了扫读，精确值仍然随手可取。
        title: util.formatAbsolute(note.createdAt),
      })
    );
    return util.el(doc, 'div', { className: 'note__meta' }, children);
  }

  /**
   * 一条消息。
   *
   * `options.compact` 是给悬浮窗用的紧凑形态（方案 11.4）：只保留内容 + 复制 +
   * 时间，删除与更多操作留在主窗口，避免在浮窗里误触。
   */
  function message(doc, note, options) {
    options = options || {};
    var node = util.el(doc, 'article', {
      className:
        'note' + (note.pinned ? ' note--pinned' : '') + (options.compact ? ' note--compact' : ''),
      dataset: { noteId: note.id },
    });

    node.appendChild(metaLine(doc, note, options));

    if (options.editingId === note.id && options.compact !== true) {
      var textarea = util.el(doc, 'textarea', {
        className: 'note__edit',
        rows: '4',
        text: note.content,
      });
      textarea.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') {
          event.preventDefault();
          if (options.onCancelEdit) {
            options.onCancelEdit(note);
          }
        } else if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
          event.preventDefault();
          if (options.onSaveEdit) {
            options.onSaveEdit(note, textarea.value);
          }
        }
      });
      node.appendChild(textarea);
      node.appendChild(
        util.el(doc, 'div', { className: 'note__actions' }, [
          button(doc, '保存', {
            modifier: 'btn--primary',
            onClick: function () {
              if (options.onSaveEdit) {
                options.onSaveEdit(note, textarea.value);
              }
            },
          }),
          button(doc, '取消', {
            onClick: function () {
              if (options.onCancelEdit) {
                options.onCancelEdit(note);
              }
            },
          }),
          util.el(doc, 'span', { className: 'muted', text: 'Esc 取消，Ctrl + Enter 保存' }),
        ])
      );
      return node;
    }

    node.appendChild(
      util.el(doc, 'div', { className: 'note__body' }, [linkifiedBody(doc, note.content)])
    );

    var actions = [];
    actions.push(
      button(doc, '复制', {
        onClick: function (element) {
          if (options.onCopy) {
            options.onCopy(note, element);
          }
        },
      })
    );

    if (options.compact !== true) {
      if (options.canModify && options.canModify(note)) {
        actions.push(
          button(doc, '编辑', {
            onClick: function () {
              if (options.onStartEdit) {
                options.onStartEdit(note);
              }
            },
          })
        );
      }
      if (options.canPin && options.canPin()) {
        actions.push(
          button(doc, note.pinned ? '取消置顶' : '置顶', {
            onClick: function () {
              if (options.onTogglePin) {
                options.onTogglePin(note);
              }
            },
          })
        );
      }
      if (options.canModify && options.canModify(note)) {
        actions.push(
          button(doc, '删除', {
            modifier: 'btn--danger',
            onClick: function () {
              if (options.onDelete) {
                options.onDelete(note);
              }
            },
          })
        );
      }
    }

    if (actions.length) {
      node.appendChild(util.el(doc, 'div', { className: 'note__actions' }, actions));
    }
    return node;
  }

  /**
   * 消息列表。返回 DocumentFragment——调用方一次 appendChild 就挂上，
   * 中间不会触发多余的布局计算。
   */
  function messageList(doc, options) {
    var notes = options.notes || [];
    if (!notes.length) {
      var hint = options.compact
        ? '这个区域还没有内容'
        : '这个区域还没有内容。在下面输入或粘贴文本，点发送。';
      return util.fragment(doc, [util.el(doc, 'p', { className: 'notes__empty', text: hint })]);
    }
    var children = [];
    for (var i = 0; i < notes.length; i += 1) {
      children.push(message(doc, notes[i], options));
    }
    return util.fragment(doc, children);
  }

  /** 区域标签（窄屏横滑 / 宽屏竖排，布局交给 CSS）。 */
  function boardTabs(doc, boards, activeId, onSelect) {
    var children = [];
    for (var i = 0; i < boards.length; i += 1) {
      var board = boards[i];
      var isActive = board.id === activeId;
      var tab = util.el(doc, 'button', {
        type: 'button',
        className: 'boards__item' + (board.isGuest ? ' boards__item--guest' : ''),
        dataset: { boardId: board.id },
        'aria-current': isActive ? 'true' : 'false',
        title: board.name,
      });
      tab.appendChild(util.el(doc, 'span', { text: board.name }));
      tab.appendChild(
        util.el(doc, 'span', {
          className: 'boards__count',
          text: board.noteCount ? ' ' + board.noteCount : '',
        })
      );
      if (onSelect) {
        tab.addEventListener('click', function (event) {
          var id = event.currentTarget.getAttribute('data-board-id');
          onSelect(id);
        });
      }
      children.push(tab);
    }
    return util.fragment(doc, children);
  }

  /** 区域信息行：名称、条数、保留期 / 到期提醒（方案 11.3）。 */
  function boardBar(doc, board, options) {
    options = options || {};
    var meta;
    if (!board) {
      meta = '没有可用的区域';
    } else {
      var parts = ['共 ' + board.noteCount + ' 条'];
      if (board.retention === 0) {
        parts.push('永久保留');
      } else if (board.expiresAt) {
        parts.push(
          (board.expired ? '已到期' : '保留 ' + board.retention + ' 天 · ' + util.formatRemaining(board.expiresAt, options.now))
        );
      }
      if (board.isGuest) {
        parts.push('访客区，有新内容就自动续期');
      }
      meta = parts.join(' · ');
    }

    var children = [
      util.el(doc, 'div', { className: 'boardbar__main' }, [
        util.el(doc, 'div', {
          className: 'boardbar__name',
          text: board ? board.name : '—',
        }),
        util.el(doc, 'div', { className: 'boardbar__meta', text: meta }),
      ]),
    ];
    if (board && options.onAction) {
      children.push(boardActions(doc, board, options));
    }
    return util.fragment(doc, children);
  }

  /**
   * 区域操作菜单。
   *
   * 用一个 `<select>` 而不是自绘菜单：CSP 下不能内联样式，自绘菜单需要一套
   * 定位与关闭逻辑；而 `<select>` 原生支持键盘、触屏与屏幕阅读器，代码量是
   * 自绘的十分之一。选完立刻复位到占位项，下次点开还是同一个菜单。
   */
  function boardActions(doc, board, options) {
    var entries = [];
    if (options.canManageBoards && !board.isGuest) {
      entries.push({ value: 'board-new', label: '新建区域' });
      entries.push({ value: 'board-rename', label: '重命名当前区域' });
      entries.push({ value: 'retention-30', label: '保留期改为 30 天' });
      entries.push({ value: 'retention-60', label: '保留期改为 60 天' });
      entries.push({ value: 'retention-0', label: '保留期改为永久' });
    }
    entries.push({ value: 'export-txt', label: '导出 .txt' });
    entries.push({ value: 'export-md', label: '导出 .md' });
    entries.push({ value: 'copy-board', label: '复制整区文本' });

    var select = util.el(doc, 'select', {
      className: 'btn btn--tiny',
      'aria-label': '区域操作',
    });
    select.appendChild(util.el(doc, 'option', { value: '', text: '更多操作…' }));
    for (var i = 0; i < entries.length; i += 1) {
      select.appendChild(
        util.el(doc, 'option', { value: entries[i].value, text: entries[i].label })
      );
    }
    select.addEventListener('change', function (event) {
      var value = event.currentTarget.value;
      event.currentTarget.value = '';
      if (value) {
        options.onAction(value, board);
      }
    });
    return select;
  }

  /** 设备列表（设置面板）。 */
  function deviceList(doc, devices, options) {
    options = options || {};
    var children = [];
    for (var i = 0; i < devices.length; i += 1) {
      var device = devices[i];
      var line = [
        util.el(doc, 'span', { className: 'devices__name', text: device.name }),
      ];
      if (device.isCurrent) {
        line.push(util.el(doc, 'span', { className: 'flag flag--accent', text: '当前设备' }));
      }
      line.push(util.el(doc, 'span', { className: 'flag', text: device.role === 'owner' ? '主人' : '访客' }));

      var rows = [
        util.el(doc, 'div', { className: 'devices__line' }, line),
        util.el(doc, 'div', {
          className: 'devices__time',
          text: device.lastSeenAt
            ? '最近在线 ' + util.formatTime(device.lastSeenAt, options.now)
            : '尚未在线',
        }),
      ];

      var actions = [];
      if (options.onRename) {
        actions.push(
          button(doc, '改名', {
            onClick: function () {
              options.onRename(device);
            },
          })
        );
      }
      if (options.onRevoke) {
        actions.push(
          button(doc, '撤销', {
            modifier: 'btn--danger',
            title: '撤销后这台设备立即退出，需要重新配对',
            onClick: function () {
              options.onRevoke(device);
            },
          })
        );
      }
      if (actions.length) {
        rows.push(util.el(doc, 'div', { className: 'row' }, actions));
      }
      children.push(util.el(doc, 'li', null, rows));
    }
    return util.fragment(doc, children);
  }

  HD.render = {
    linkifiedBody: linkifiedBody,
    message: message,
    messageList: messageList,
    boardTabs: boardTabs,
    boardBar: boardBar,
    boardActions: boardActions,
    deviceList: deviceList,
  };
})();

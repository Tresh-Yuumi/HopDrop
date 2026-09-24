/* HopDrop 前端：命名空间、DOM 构造、时间与剪贴板工具。
 *
 * **为什么要一个 `HD.util.el` 而不是拼字符串。** 消息正文是用户输入，方案
 * 12 的安全主防线是"一律按纯文本渲染"。用 `textContent` 赋值是**结构性**
 * 安全的——浏览器不会把字符串当 HTML 解析，所以不存在"哪个转义函数漏了一层"
 * 的可能。而拼 innerHTML 则要求每一处调用点都记得转义，任何一处漏掉就是一个
 * XSS。这里的选择是让"忘了转义"这件事在代码里根本无法表达。
 *
 * 因此：**全项目不使用 innerHTML / outerHTML / insertAdjacentHTML / document.write。**
 *
 * 语法基线是 Chromium 87。可用 `?.`、`??`、`||=`、`String.replaceAll`、
 * `Promise.any`；**不可用** `Array.prototype.at`（92）、`Object.hasOwn`（93）、
 * `crypto.randomUUID`（92，且要求安全上下文）、顶层 `await`（89）、
 * `structuredClone`（98）。
 */

(function () {
  'use strict';

  var HD = (window.HD = window.HD || {});

  var util = {};

  // ---------------------------------------------------------------- DOM

  function isNode(value) {
    return value && typeof value === 'object' && typeof value.nodeType === 'number';
  }

  function appendChildren(node, children) {
    if (children === null || children === undefined || children === false) {
      return;
    }
    if (Array.isArray(children)) {
      for (var i = 0; i < children.length; i += 1) {
        appendChildren(node, children[i]);
      }
      return;
    }
    if (isNode(children)) {
      node.appendChild(children);
      return;
    }
    // 数字与字符串走文本节点——**永远**不解析成 HTML。
    node.appendChild(node.ownerDocument.createTextNode(String(children)));
  }

  /**
   * 建元素。`el(doc, 'div', {className: 'note', text: '正文'}, [子节点, '文本'])`
   *
   * `doc` 必须显式传入，不能默认取全局 `document`：M10 的悬浮窗要在另一个
   * `Document` 上渲染同一套消息列表（方案 5.4 约束 1），渲染函数一旦读了全局
   * `document`，那个页面就复用不了这套代码，只能再抄一份。
   *
   * 属性规则：值为 `null` / `undefined` / `false` 的键跳过（方便写条件属性）；
   * `text` 是 `textContent` 的简写；`className` 写 class；`dataset` 写 `data-*`
   * ——后两个都要走 DOM 的属性赋值器，理由见下面。
   */
  util.el = function (doc, tag, attrs, children) {
    var node = doc.createElement(tag);
    if (attrs) {
      var keys = Object.keys(attrs);
      for (var i = 0; i < keys.length; i += 1) {
        var key = keys[i];
        var value = attrs[key];
        if (value === null || value === undefined || value === false) {
          continue;
        }
        if (key === 'text') {
          node.textContent = String(value);
        } else if (key === 'className') {
          // **必须写 `class`，不能写 `setAttribute('className', ...)`。**
          //
          // HTML 元素上的 `setAttribute` 会把属性名小写化，所以后者落下去的是
          // 一个名叫 `classname` 的属性——**不是 `class`**。CSS 里所有
          // `.boards__item`、`.note`、`.btn` 于是全部匹配不上：界面看起来"能跑"
          // （文字、结构都在），但一行样式都没生效，而且**控制台一个错都不报**。
          node.setAttribute('class', String(value));
        } else if (key === 'dataset') {
          // 同理不能写 `setAttribute('data-' + key)`：键 `boardId` 会变成属性名
          // `data-boardId`，被小写成 `data-boardid`，于是
          // `getAttribute('data-board-id')` 永远返回 null——区域标签点了没反应。
          var dataKeys = Object.keys(value);
          for (var j = 0; j < dataKeys.length; j += 1) {
            node.dataset[dataKeys[j]] = String(value[dataKeys[j]]);
          }
        } else if (value === true) {
          node.setAttribute(key, '');
        } else {
          node.setAttribute(key, String(value));
        }
      }
    }
    appendChildren(node, children);
    return node;
  };

  /** 清空一个节点。用循环删子节点，不用 `innerHTML = ''`（见文件头）。 */
  util.clear = function (node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  };

  util.fragment = function (doc, children) {
    var frag = doc.createDocumentFragment();
    appendChildren(frag, children);
    return frag;
  };

  // ---------------------------------------------------------------- 时间

  function pad2(value) {
    return String(value).padStart(2, '0');
  }

  /**
   * 相对当前时间的展示格式。时间戳统一是 Unix 秒（方案 9.1）。
   *
   * `now` 由调用方传入而不是内部取 `Date.now()`，是为了让这个函数可测——
   * "刚刚 / 昨天"这类分支靠真实时钟很难稳定地测。
   */
  util.formatTime = function (seconds, nowSeconds) {
    var now = nowSeconds === undefined ? Math.floor(Date.now() / 1000) : nowSeconds;
    var diff = now - seconds;
    if (diff >= 0 && diff < 60) {
      return '刚刚';
    }

    var date = new Date(seconds * 1000);
    var today = new Date(now * 1000);
    var clock = pad2(date.getHours()) + ':' + pad2(date.getMinutes());

    var sameDay =
      date.getFullYear() === today.getFullYear() &&
      date.getMonth() === today.getMonth() &&
      date.getDate() === today.getDate();
    if (sameDay) {
      return clock;
    }

    var startOfToday = new Date(
      today.getFullYear(),
      today.getMonth(),
      today.getDate()
    ).getTime();
    if (seconds * 1000 >= startOfToday - 86400000) {
      return '昨天 ' + clock;
    }

    var monthDay = date.getMonth() + 1 + '月' + date.getDate() + '日';
    if (date.getFullYear() === today.getFullYear()) {
      return monthDay + ' ' + clock;
    }
    return date.getFullYear() + '年' + monthDay + ' ' + clock;
  };

  /** 完整的绝对时间，用于 `title` 属性——悬停时能看到精确值。 */
  util.formatAbsolute = function (seconds) {
    var date = new Date(seconds * 1000);
    return (
      date.getFullYear() +
      '-' +
      pad2(date.getMonth() + 1) +
      '-' +
      pad2(date.getDate()) +
      ' ' +
      pad2(date.getHours()) +
      ':' +
      pad2(date.getMinutes()) +
      ':' +
      pad2(date.getSeconds())
    );
  };

  /** 剩余时间的人话表达。用于区域到期提醒（方案 11.3）。 */
  util.formatRemaining = function (expiresAt, nowSeconds) {
    var now = nowSeconds === undefined ? Math.floor(Date.now() / 1000) : nowSeconds;
    var left = expiresAt - now;
    if (left <= 0) {
      return '已到期';
    }
    var days = Math.floor(left / 86400);
    if (days >= 1) {
      return '剩 ' + days + ' 天';
    }
    var hours = Math.floor(left / 3600);
    if (hours >= 1) {
      return '剩 ' + hours + ' 小时';
    }
    return '剩不到 1 小时';
  };

  // ---------------------------------------------------------------- 其他

  /**
   * 幂等键。
   *
   * **不能直接用 `crypto.randomUUID`**：它要求 Chromium 92 以上**且**处于
   * 安全上下文。而本项目的前端基线是 87——麒麟机器上如果是那个旧内核，
   * 直接调用会抛错，症状是"发送失败但看不出原因"。所以先探测再用，兜底走
   * `getRandomValues` 手拼（这个从很早的版本就有，且不要求安全上下文）。
   *
   * 格式落在服务端 `MUTATION_ID_ALLOWED` 的字符集内（字母数字加 `-` `_`），
   * 长度也远小于 64 的上限。
   */
  util.randomId = function () {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      try {
        return window.crypto.randomUUID();
      } catch (error) {
        // 某些定制浏览器上这个函数存在但会抛（安全上下文判定不一致）。
        // 落到下面的兜底路径，不把异常抛给调用方。
      }
    }
    var bytes = new Uint8Array(16);
    if (window.crypto && typeof window.crypto.getRandomValues === 'function') {
      window.crypto.getRandomValues(bytes);
    } else {
      for (var k = 0; k < 16; k += 1) {
        bytes[k] = Math.floor(Math.random() * 256);
      }
    }
    var hex = '';
    for (var i = 0; i < 16; i += 1) {
      hex += (bytes[i] + 256).toString(16).slice(1);
    }
    return hex;
  };

  util.nowSeconds = function () {
    return Math.floor(Date.now() / 1000);
  };

  util.utf8Bytes = function (text) {
    if (window.TextEncoder) {
      return new TextEncoder().encode(text).length;
    }
    // TextEncoder 从 Chromium 38 就有，这里只是不让它在极旧环境里炸。
    return unescape(encodeURIComponent(text)).length;
  };

  /** localStorage 在隐私模式下会抛异常，读写都包一层。 */
  util.readPref = function (key, fallback) {
    try {
      var raw = window.localStorage.getItem(key);
      return raw === null ? fallback : raw;
    } catch (error) {
      return fallback;
    }
  };

  util.writePref = function (key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (error) {
      // 存不下就算了。这里放的都是本地偏好（粘贴即发送、上次停留的区域），
      // 不是数据；丢了只影响一点顺手程度，不影响正确性。
    }
  };

  util.removePref = function (key) {
    try {
      window.localStorage.removeItem(key);
    } catch (error) {
      // 同上，删不掉也不必因此中断收尾流程。
    }
  };

  util.hasClipboardRead = function () {
    return !!(
      window.navigator &&
      window.navigator.clipboard &&
      typeof window.navigator.clipboard.readText === 'function'
    );
  };

  /**
   * 复制文本。方案 5.1：失败时**选中文本并提示手动复制**，不走 `execCommand`
   * ——那个 API 已废弃，行为在定制浏览器上不可预期，而且它要求先往 DOM 里插
   * 一个临时节点，多出来的东西比它解决的问题更多。
   *
   * 返回 Promise，成功 resolve(true)，失败 resolve(false)。**不 reject**：
   * 调用方需要的是"要不要降级"，而不是一个异常分支。
   */
  util.copyText = function (text) {
    var clipboard = window.navigator && window.navigator.clipboard;
    if (!clipboard || typeof clipboard.writeText !== 'function') {
      return Promise.resolve(false);
    }
    return clipboard.writeText(text).then(
      function () {
        return true;
      },
      function () {
        return false;
      }
    );
  };

  /** 选中某个节点的全部文本内容，作为复制失败时的降级路径。 */
  util.selectNode = function (node) {
    var doc = node.ownerDocument;
    var selection = doc.getSelection ? doc.getSelection() : window.getSelection();
    if (!selection) {
      return false;
    }
    var range = doc.createRange();
    range.selectNodeContents(node);
    selection.removeAllRanges();
    selection.addRange(range);
    return true;
  };

  HD.util = util;
})();

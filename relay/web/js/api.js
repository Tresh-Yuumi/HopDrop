/* HopDrop 前端：接口封装。
 *
 * **只做三件事**：拼 URL、按统一信封解析错误、把 401 变成一次全局事件。
 * 业务判断一律留在调用方——`api.js` 不知道"区域已归档"该怎么办，它只负责
 * 把 `code` 原样交给上层。
 *
 * 错误对象带的是**稳定的 `code`**（方案 9.1 的约定），调用方按 code 分支，
 * 绝不按 message 匹配字符串——那是会随文案改动而静默失效的写法。
 */

(function () {
  'use strict';

  var HD = (window.HD = window.HD || {});

  /** 接口错误。`code` 来自服务端信封；网络层失败用 `network_error`。 */
  function ApiError(status, code, message, requestId) {
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.message = message;
    this.requestId = requestId;
  }

  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  var onUnauthorized = null;

  /** 任何请求返回 401 时调用一次。由 app.js 注册，用于清空本地状态并回首页。 */
  function setUnauthorizedHandler(handler) {
    onUnauthorized = handler;
  }

  function request(method, path, body) {
    var init = {
      method: method,
      // 会话在 HttpOnly Cookie 里，必须带上。同源请求默认就会带，显式写出
      // 是为了让"这个接口需要凭证"在代码里看得见。
      credentials: 'same-origin',
      headers: {},
    };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    return window.fetch(path, init).then(
      function (response) {
        // 204（登出、删除设备）没有响应体，不能走解析。
        if (response.status === 204) {
          return null;
        }
        return response.text().then(function (text) {
          var data = null;
          if (text) {
            try {
              data = JSON.parse(text);
            } catch (error) {
              // 非 JSON 响应（例如反向代理返回的 HTML 错误页）。当成服务端
              // 错误处理，但**不要把 HTML 塞进 message 里**——那会是一坨
              // 看不懂的东西，还可能带出内部路径。
              data = null;
            }
          }

          if (response.ok) {
            return data;
          }

          var envelope = (data && data.error) || {};
          var code = envelope.code || 'http_' + response.status;
          var message = envelope.message || '请求失败（HTTP ' + response.status + '）';

          if (response.status === 401 && typeof onUnauthorized === 'function') {
            onUnauthorized(code);
          }
          throw new ApiError(response.status, code, message, envelope.requestId);
        });
      },
      function (error) {
        // 网络层失败（断网、请求被拦截）。给一个明确的 code，让调用方能把它
        // 和业务错误分开——它的正确处理是重试，而不是提示用户操作有误。
        throw new ApiError(0, 'network_error', '网络不可达，请检查连接', null);
      }
    );
  }

  HD.api = {
    request: request,
    ApiError: ApiError,
    setUnauthorizedHandler: setUnauthorizedHandler,

    snapshot: function () {
      return request('GET', '/api/snapshot');
    },

    boardNotes: function (boardId, cursor, limit) {
      var query = [];
      if (cursor) {
        query.push('cursor=' + encodeURIComponent(cursor));
      }
      if (limit) {
        query.push('limit=' + encodeURIComponent(limit));
      }
      var suffix = query.length ? '?' + query.join('&') : '';
      return request('GET', '/api/boards/' + encodeURIComponent(boardId) + '/notes' + suffix);
    },

    createNote: function (boardId, content, mutationId) {
      return request('POST', '/api/boards/' + encodeURIComponent(boardId) + '/notes', {
        content: content,
        mutationId: mutationId,
      });
    },

    updateNote: function (noteId, fields) {
      return request('PATCH', '/api/notes/' + encodeURIComponent(noteId), fields);
    },

    deleteNote: function (noteId) {
      return request('DELETE', '/api/notes/' + encodeURIComponent(noteId));
    },

    createBoard: function (name, retention) {
      return request('POST', '/api/boards', { name: name, retention: retention });
    },

    updateBoard: function (boardId, fields) {
      return request('PATCH', '/api/boards/' + encodeURIComponent(boardId), fields);
    },

    devices: function () {
      return request('GET', '/api/devices');
    },

    renameDevice: function (deviceId, name) {
      return request('PATCH', '/api/devices/' + encodeURIComponent(deviceId), { name: name });
    },

    revokeDevice: function (deviceId) {
      return request('DELETE', '/api/devices/' + encodeURIComponent(deviceId));
    },

    revokeOtherDevices: function () {
      return request('DELETE', '/api/devices');
    },

    rotateOwnerSecret: function () {
      return request('POST', '/api/owner-secret/rotate');
    },

    logout: function () {
      return request('POST', '/api/session/logout');
    },

    /**
     * 导出用的**绝对路径**。返回值交给 `<a download>` 去取，不经过 fetch——
     * 浏览器自己处理下载、文件名与 Content-Disposition，比在 JS 里造 Blob
     * 少一层出错的可能（Blob 方案还要处理内存里多一份完整副本）。
     */
    exportUrl: function (boardId, format) {
      return (
        '/api/boards/' +
        encodeURIComponent(boardId) +
        '/export?format=' +
        encodeURIComponent(format)
      );
    },
  };
})();

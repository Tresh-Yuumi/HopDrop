"""页面：首页与应用页外壳（方案 11.1、11.2）。

**这一层只渲染"外壳"，不渲染任何业务内容。** 区域、消息、设备列表都由前端
脚本拿到数据后填进去。三条理由，没有一条是风格偏好：

1. **CSP 不允许内联。** `style-src 'self'` / `script-src 'self'` 会拦掉内联
   样式与脚本，所以页面里不能有 `<style>`、不能有 `<script>代码</script>`、
   不能有 `onclick=`。要传给前端的数据只能走 `data-*` 属性——注意
   `<script type="application/json">` 这种"数据岛"**同样会被拦**，CSP 管的
   是 `<script>` 元素本身，不看 type。服务端渲染的 HTML 也就不用去转义
   JSON 里的 `</script>`，一个坑直接不存在。
2. **不引模板引擎。** 方案 2.2 的依赖只有四个；多一个模板引擎就多一份要跟
   着 Python 版本升级的东西，而这里要拼的模板总共两个。
3. **外壳不随内容变化。** 页面结构对所有人是同一份，只有 `data-*` 里的几个
   会话字段不同。这样"某台设备看到的是缓存的旧页面"这类问题不会出现，
   也省掉了按用户拼 HTML 的整条路径。

**M5 只做文本闭环，两处刻意留空：**

- 首页的**访客码入口**要等 M9——`POST /api/guest/enter` 尚未实现，渲染一个
  点了没反应的输入框比不渲染更糟（方案把访客能力放在阶段 3）。
- 区域导航里的**文件 / 已归档 / 回收站**要等 M7 与 M9，导航当前只展示
  快照里真实存在的区域。

M5 自己的临时页面（M2 起用于验证登录状态的那两个）在这里被正式替换掉。
"""

from __future__ import annotations

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..config import Config
from ..identity import resolve_session

router = APIRouter()

_OWNER_LINK_HINT = "请使用主人链接（形如 /pair/xxxx）完成首次配对。"
_NOT_PAIRED_HINT = "这台设备还没有配对。"

# 前端脚本，按依赖顺序加载。
#
# **刻意不用 ES 模块**（`type="module"`）。模块要经过一条独立的获取与解析
# 路径，而政企定制浏览器（奇安信、红莲花这类）可能内置代理与脚本策略，
# 那条路径被拦时的表现是"整页空白、控制台一句话"，排查成本远高于收益。
# 经典脚本按顺序执行、共享一个全局命名空间，行为在四端浏览器上完全一致，
# 而且没有构建步骤——与方案 5.3「不引入构建步骤」一致。
_APP_SCRIPTS = (
    "/static/js/util.js",
    "/static/js/api.js",
    "/static/js/store.js",
    "/static/js/render.js",
    "/static/js/sync.js",
    "/static/js/actions.js",
    "/static/js/app.js",
)


def _esc(value: object) -> str:
    """HTML 转义（含属性上下文）。

    `quote=True` 会一并转义 `"`，所以拼进 `data-*="..."` 也是安全的。
    前端渲染消息正文时走的是 `textContent`，不经过这里——这里只处理服务端
    直接写进 HTML 的那几个会话字段。
    """
    return html.escape(str(value), quote=True)


def _layout(*, title: str, body: str, scripts: tuple[str, ...] = ()) -> str:
    tags = "".join(f'<script src="{_esc(src)}" defer></script>' for src in scripts)
    return (
        "<!doctype html>"
        '<html lang="zh-CN">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        # 个人工具，任何一页都不该被搜索引擎收录。首页同样加上：
        # 备案要求首页可公开访问，但"可访问"不等于"要出现在搜索结果里"。
        '<meta name="robots" content="noindex, nofollow">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{_esc(title)}</title>"
        # 图标显式声明，不要留空让浏览器去猜 `/favicon.ico`。猜出来的那个请求
        # 必然 404（本项目没有 .ico），在每个页面的控制台里留一条红字——看起来
        # 像故障，实际是个噪音，会把真正的错误淹掉。SVG 图标不需要二进制资源，
        # 也不受 CSP 限制（`img-src 'self'`）。
        '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">'
        '<link rel="stylesheet" href="/static/app.css">'
        "</head>"
        f"<body>{body}{tags}</body>"
        "</html>"
    )


def _retention_lines(cfg: Config) -> str:
    """首页的保留规则说明。数字全部来自配置，不写死。"""
    items = [
        f"文件保留 <strong>{cfg.file_ttl_days}</strong> 天，按上传完成时间起算",
        "文本区可选 <strong>30 天 / 60 天 / 永久</strong> 三档保留期，"
        f"到期前 7 天提醒，归档后保留 <strong>{cfg.archive_ttl_days}</strong> 天",
        "访客区为 <strong>30 天滑动</strong>：每次有新内容写入就重新计时",
        "删除的文本进入回收站，<strong>7 天</strong>后彻底删除",
    ]
    return "".join(f"<li>{item}</li>" for item in items)


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    """首页（方案 11.1）。

    未配对时是纯静态说明页——没有脚本、没有数据请求，浏览器关掉 JS 也能读。
    已配对时多一个进入应用页的入口。
    """
    cfg: Config = request.app.state.config
    context = await resolve_session(request)

    if context is not None:
        role = "主人" if context.is_owner else "访客"
        owner_block = (
            "<p>这台设备已配对："
            f"<strong>{_esc(context.device_name)}</strong>（{_esc(role)}）</p>"
            '<p><a class="btn btn--primary" href="/app">进入应用页</a></p>'
        )
    else:
        owner_block = (
            f"<p>首次使用请打开主人链接（形如 <code>/pair/xxxx</code>）。</p>"
            "<p class=\"muted\">这条链接只在生成时显示一次，服务端只存它的哈希，"
            "丢了只能重新生成。请离线保存。</p>"
        )

    # 合规信息（方案 13）。未配置的项不渲染，也不编一个值上去。
    compliance: list[str] = []
    if cfg.icp_license:
        compliance.append(
            f'<p>{_esc(cfg.icp_license)}'
            '<a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">'
            "工业和信息化部政务服务平台</a></p>"
        )
    if cfg.contact:
        compliance.append(f"<p>违规内容删除联系方式：{_esc(cfg.contact)}</p>")
    compliance_html = (
        '<section class="card"><h2>合规</h2>' + "".join(compliance) + "</section>"
        if compliance
        else ""
    )

    body = (
        '<main class="page">'
        '<h1 class="page__title">HopDrop</h1>'
        '<p class="page__lead">跨设备文件与文本中转。手机、平板、电脑之间传文本'
        "和不超过 "
        f"{cfg.max_file_bytes // (1024 * 1024)} MiB 的文件——"
        "<strong>打开即看、粘贴即发、一键复制</strong>，文件不需要两端同时在线。</p>"
        '<section class="card"><h2>我是主人</h2>'
        f"{owner_block}</section>"
        '<section class="card"><h2>保留规则</h2>'
        f'<ul class="rules">{_retention_lines(cfg)}</ul></section>'
        '<section class="card"><h2>需要知道的两件事</h2>'
        "<p>服务端可以读取暂存内容，<strong>不提供端到端加密</strong>；"
        "也不做病毒扫描、内容识别或敏感词审核。</p>"
        "<p>请勿用它传身份证、密码、合同扫描件这类敏感资料，"
        "也不要作为永久网盘使用。</p></section>"
        f"{compliance_html}"
        "</main>"
    )
    return HTMLResponse(_layout(title="HopDrop", body=body))


@router.get("/app", response_class=HTMLResponse)
async def app_page(request: Request) -> HTMLResponse:
    """应用页外壳（方案 11.2）。

    宽屏左侧区域列表、右侧消息流；窄屏顶部横滑标签 + 单列。响应式完全由
    CSS 媒体查询完成，服务端不区分设备——同一份 HTML 在四端都是对的。
    """
    cfg: Config = request.app.state.config
    context = await resolve_session(request)

    if context is None:
        body = (
            '<main class="page">'
            '<h1 class="page__title">尚未配对</h1>'
            f"<p>{_esc(_NOT_PAIRED_HINT)}</p>"
            f"<p>{_esc(_OWNER_LINK_HINT)}</p>"
            '<p><a class="btn" href="/">返回首页</a></p>'
            "</main>"
        )
        return HTMLResponse(_layout(title="HopDrop · 未配对", body=body), status_code=401)

    notices: list[str] = []
    if not cfg.cookie_secure:
        notices.append(
            '<p class="notice notice--dev">Cookie 的 Secure 属性已关闭：'
            "此状态仅可用于本地开发，生产环境必须开启。</p>"
        )
    notices.append(
        '<p class="notice" id="hd-unsupported" hidden>'
        "当前浏览器缺少 WebSocket 支持，页面不会自动更新，请手动刷新。</p>"
    )

    body = (
        f'<div id="hd-app" class="app"'
        f' data-room-name="{_esc(context.room_name)}"'
        f' data-device-name="{_esc(context.device_name)}"'
        f' data-role="{_esc(context.role)}"'
        # 单条消息的字节上限由服务端给，前端不写死。写死就会出现"配置调到
        # 128 KiB，前端还在 64 KiB 处拦"这种谁都没想到要同步的地方。
        f' data-note-max-bytes="{_esc(cfg.note_max_bytes)}">'

        '<header class="topbar">'
        f'<span class="topbar__room" id="hd-room-name">{_esc(context.room_name)}</span>'
        '<span class="conn" id="hd-conn" data-state="connecting">连接中</span>'
        '<button type="button" class="btn btn--icon" id="hd-settings-toggle"'
        ' aria-controls="hd-settings" aria-expanded="false">设置</button>'
        "</header>"

        '<div class="layout">'
        '<nav class="boards" id="hd-boards" aria-label="区域"></nav>'
        '<main class="panel">'
        f'<div class="notices" id="hd-notices">{"".join(notices)}</div>'
        '<div class="boardbar" id="hd-boardbar"></div>'
        '<div class="notes" id="hd-notes"></div>'
        '<div class="more" id="hd-more" hidden>'
        '<button type="button" class="btn" id="hd-more-btn">加载更早的消息</button>'
        "</div>"
        '<div class="composer" id="hd-composer">'
        '<label class="sr-only" for="hd-input">消息内容</label>'
        '<textarea id="hd-input" class="composer__input" rows="3"'
        ' placeholder="粘贴或输入文本，Ctrl + Enter 发送"></textarea>'
        '<div class="composer__bar">'
        # 读取剪贴板是增强项：方法不存在时它必须**不出现**，而不是点了报错
        # （方案 5.3 的验收项）。所以这里先给 hidden，由脚本检测后再显示。
        '<button type="button" class="btn btn--ghost" id="hd-read-clip" hidden>'
        "读取剪贴板</button>"
        '<span class="composer__status" id="hd-status"></span>'
        '<span class="composer__count" id="hd-count"></span>'
        '<button type="button" class="btn btn--primary" id="hd-send">发送</button>'
        "</div>"
        "</div>"
        "</main>"
        "</div>"

        '<aside class="drawer" id="hd-settings" hidden aria-label="设置">'
        '<div class="drawer__head"><h2>设置</h2>'
        '<button type="button" class="btn btn--ghost" id="hd-settings-close">关闭</button>'
        "</div>"
        '<div class="drawer__body">'
        '<section class="group"><h3>当前设备</h3>'
        f'<p id="hd-device-name">{_esc(context.device_name)}</p>'
        '<p class="muted" id="hd-role-text"></p>'
        "</section>"
        '<section class="group"><h3>输入</h3>'
        '<label class="check"><input type="checkbox" id="hd-paste-send">'
        " 粘贴后立即发送</label>"
        '<p class="muted">默认是"粘贴进输入框、先看一眼、再点发送"，'
        "避免误发验证码或密码。开启后粘贴的内容会直接发出。"
        "这个开关只影响当前这台设备。</p>"
        "</section>"
        '<section class="group"><h3>设备</h3>'
        '<ul class="devices" id="hd-devices"></ul>'
        '<div class="row">'
        '<button type="button" class="btn" id="hd-revoke-others">撤销其他设备</button>'
        '<button type="button" class="btn" id="hd-rotate-secret">重置主人链接</button>'
        "</div>"
        '<p class="muted" id="hd-rotate-result" hidden></p>'
        "</section>"
        '<section class="group"><h3>会话</h3>'
        '<button type="button" class="btn btn--danger" id="hd-logout">'
        "退出当前设备</button>"
        '<p class="muted">退出只影响这一台设备，不会撤销其他设备。</p>'
        "</section>"
        "</div>"
        "</aside>"

        '<div class="scrim" id="hd-scrim" hidden></div>'
        '<div class="toast" id="hd-toast" hidden role="status" aria-live="polite"></div>'

        "<noscript><p class=\"notice\">这个页面需要 JavaScript 才能收发内容。</p></noscript>"
        "</div>"
    )
    return HTMLResponse(_layout(title=f"HopDrop · {context.room_name}", body=body, scripts=_APP_SCRIPTS))

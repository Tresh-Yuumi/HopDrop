# HopDrop

跨设备文件与文本中转工具。手机、平板、电脑、银河麒麟桌面之间传文本、图片和不超过 20 MiB 的文件：**打开即看、粘贴即发、一键复制，文件不需要两端同时在线。**

设计文档在 `C:\Users\yuzhe\Documents\Codex\2026-09-21\wo\outputs\`，当前版本 v1.4（文件名含版本号）。本仓库是它的实现，**实现与文档不一致时以文档为准，并应更新文档**。

## 目录

```
relay/                  服务端
  app/                  应用代码
    api/                HTTP 接口，每个模块一个 router
    boards.py           文本区：可见性、可写性、生命周期
    cli.py              管理命令：init / rotate
    config.py           RELAY_* 环境变量加载与校验
    db.py               单连接 + 全局锁的数据访问层
    devices.py          设备与配对的存取
    errors.py           统一错误信封
    events.py           提交后要广播的变更（数据层与传输层的中立类型）
    export.py           区域导出（txt / md）
    identity.py         会话解析与权限依赖
    migrations.py       顺序 SQL 迁移执行器
    notes.py            消息：幂等、编辑、软删除
    origin.py           同源判定（HTTP 写请求与 WebSocket 握手共用）
    realtime.py         WebSocket 连接表与按房间广播
    rooms.py            房间引导（含两个初始区域）与 rev 递增
    security.py         随机值与哈希
    security_headers.py 安全响应头（与 deploy 下的反代配置同源）
    snapshot.py         全量快照组装
    state.py            进程内运行时状态
  migrations/           NNNN_名称.sql，启动时自动应用
  web/                  前端（无构建步骤，直接由 /static 提供）
    app.css             唯一一份样式
    js/                 按职责拆分，靠 <script defer> 的顺序装配
      util.js           命名空间、DOM 构建、时间格式化
      api.js            接口封装与错误信封
      store.js          客户端状态与事件应用
      render.js         可复用渲染（消息列表为 M10 悬浮窗留口子）
      sync.js           WebSocket 与快照对齐
      actions.js        用户操作
      app.js            装配与事件绑定
  tests/                pytest
  relay.env.example     配置样例
deploy/                 部署件（用法见 deploy/RUNBOOK.md）
  RUNBOOK.md            部署手册：目标机现状、首次部署、发布、回滚、验收、故障处置
  install.sh            服务器初始化（幂等；--check / --prefix 演练）
  release.sh            开发机侧发布：打包 + 校验 + 推送 + 调 install.sh
  rollback.sh           服务器侧回滚：代码与数据库
  certbot-hopdrop.sh    签证书（certonly --webroot，不改写 nginx 配置）
  Caddyfile             备用反代配置（目标机用的是 nginx，见 install.sh）
  relay.service         systemd unit
  backup.sh             每日数据库备份
  hopdrop-healthcheck.sh / .service / .timer   每分钟健康检查
```

## 本地跑起来

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r relay/requirements-dev.txt   # Windows
# .venv/bin/pip install -r relay/requirements-dev.txt                    # Linux
```

第一步是**创建房间**。产品没有注册接口，房间是运维动作，所以走命令行：

```bash
cd relay && ../.venv/Scripts/python.exe -m app.cli init
```

它会创建房间和两个初始区域（**日常**，永久；**访客区**，30 天滑动），并打印主人链接。**这条链接要离线保存**——服务端只存它的哈希，丢了只能重新生成：

```bash
python -m app.cli rotate        # 重置主人链接；已配对设备不受影响
```

然后启动服务。本地必须关掉 Cookie 的 `Secure` 属性，否则浏览器会直接丢弃它（生产是 HTTPS，保持默认开启即可）：

```bash
cd relay && RELAY_COOKIE_SECURE=0 ../.venv/Scripts/python.exe -m app
```

浏览器打开上面打印的 `/pair/...` 路径即可完成配对，随后会跳到 `/app`。

另开一个终端验证：

```bash
curl -i 127.0.0.1:8080/healthz
```

本地开发不需要 HTTPS：浏览器把 `http://127.0.0.1` 视为安全上下文，剪贴板 API 照常可用。但 `Secure` 与 `SameSite=Strict` 两个 Cookie 属性在纯 HTTP 下会拦住会话，所以本地开发必须显式设置 `RELAY_COOKIE_SECURE=0`。

测试：

```bash
cd relay && ../.venv/Scripts/python.exe -m pytest -q
```

改过前端之后再跑一次**真实浏览器冒烟测试**。它起真服务、开真浏览器，把跨设备文本闭环走一遍，并断言"JS 建出来的元素真的套上了 CSS"。默认跳过（需要浏览器，约 40 秒）：

```bash
cd relay && RELAY_UI_E2E=1 ../.venv/Scripts/python.exe -m pytest tests/test_ui_smoke.py -q
```

浏览器默认按常见位置找 Chrome/Edge，也可以用 `RELAY_CHROME` 指定可执行文件路径；加 `RELAY_UI_SCREENSHOT=<路径>` 可以把最后一屏存成 PNG（排样式问题时有用）。

本次验证通过的依赖版本：Python 3.13.14、fastapi 0.141.1、uvicorn 0.53.0、aiosqlite 0.22.1、python-multipart 0.0.32。

刻意不放 lock 文件：开发机是 Windows、部署机是 Linux，`uvicorn[standard]` 的平台依赖不同（Linux 才有 uvloop），Windows 生成的 freeze 反而会误导服务器。部署时按 `requirements.txt` 的版本区间安装即可。

## 配置

全部参数来自 `RELAY_*` 环境变量，没有配置文件。逐项说明见 `relay/relay.env.example`。代码里的默认值面向本地开发；生产由 `/etc/relay/relay.env` 覆盖。

## 部署

看 **`deploy/RUNBOOK.md`**：首次部署的前置条件、验收清单（对照方案第 18 节的完成定义）、日常发布、回滚、备份恢复演练、故障处置。

一句话版本：

```bash
bash deploy/release.sh --host root@<服务器> [--domain <域名>]
```

没有 Linux 机器时也能验证部署脚本本身——`install.sh` 提供了 `--prefix` 与 `--no-system`：

```bash
bash deploy/install.sh --prefix /tmp/hopdrop-drill
```

它跑的是真实的文件生成、权限与幂等逻辑，只把 `apt` / `ufw` / `systemctl` / `useradd` 这些需要真系统的步骤让开。`tests/test_deploy.py` 把这条演练固化成测试。

## 不可妥协的约束

这些不是风格偏好，改了会坏：

1. **uvicorn 不能加 `--workers`。** 多 worker 会分裂 WebSocket 连接表和全局写锁，一致性直接失效，而且症状很隐蔽。
2. **数据库只用一个连接。** 所有读写都经过 `app/db.py` 的 `Database`，用一个全局 `asyncio.Lock` 串行化。锁不可重入——`write()`/`read()` 块内部不得再调用它们。
3. **迁移只增不改。** 已经用过的迁移文件不得修改，结构变更一律新增文件。文件名必须是 `NNNN_名称.sql`。
4. **DDL 的关键约束不动。** 尤其是 `files.size` 的 1..20971520、`boards` 的两条 CHECK、`uq_boards_guest` 部分唯一索引。
5. **文件只有在完整校验并原子落盘后才算上传成功**；权限校验基于 `file.room_id`；文本与其他用户输入一律 HTML 转义，纯文本渲染。
6. **`security.hash_token` 只用于高熵凭证**（会话令牌、主人 secret）。6 位访客码是 10^6 的取值空间，用它等于明文存储，M9 必须改用带盐的慢哈希（`hashlib.scrypt`）。
7. **权限从会话反查，不信任客户端传入的 `roomId` / `role`**；跨房间的资源一律返回 404（不是 403），避免泄漏存在性。

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 工程骨架与数据库基线：config / db / 迁移 / healthz / 错误信封 / 部署件 | 已完成 |
| M2 | 身份、配对与长期登录：房间引导 / `/pair` / 会话 / 设备管理 / 来源校验 | 已完成 |
| M3 | 文本区与消息 CRUD（含 `mutation_id` 幂等与 `rooms.rev`） | 已完成 |
| M4 | WebSocket 推送与快照对齐 | 已完成 |
| M5 | 前端页面（文本闭环可点通） | 已完成 |
| M6 | 首次真部署（与既有 nginx 共存 + systemd + HTTPS） | 部署件与演练完成；真机执行待域名与备案号 |
| M7 | 文件上传与下载 | 待做 |
| M8 | 配额与清理任务 | 待做 |
| M9 | 访客与生命周期（可延后，不返工） | 待做 |
| M10 | 悬浮窗与打磨（可延后） | 待做 |

M1–M6 构成方案的阶段 1（文本闭环）。M9 与 M10 是纯新增模块，不改核心数据模型，可整体延后。

### M1 完成范围与已知缺口

已完成：迁移建出 8 张表 + `schema_version`；`/healthz` 按方案 14.5 返回结构化 JSON 并在不健康时返回 503；统一错误信封与 `X-Request-Id`；`/api/*` 响应 `Cache-Control: no-store`；反向代理配置、systemd unit、备份脚本。

尚未接入，属后续里程碑：

- **清理任务**（M8）。`/healthz` 的 `lastCleanupAt` 现在是 `null`，代码不会假装它跑过。接入后 14.5 里"清理任务超过 3 小时未成功即 degraded"才开始生效。
- **WebSocket**（M4）。`websocketConnections` 现在恒为 `0`。
- **OpenAPI / Swagger UI 已关闭**。文档页要从 CDN 取资源，既被本项目的 CSP 挡住，也多一处对外接口面。接口验证靠测试。
- **`/` 与 `/app` 在 M5 已替换为正式页面**（`app/api/pages.py`）。M1–M4 期间它们是临时页面，存在的唯一原因是让"长期登录"能在浏览器里被看见。正式页面同样不使用任何内联样式或脚本（会被部署时的 CSP 拦掉），也不引入模板引擎。

### M2 完成范围与已知缺口

已完成：`python -m app.cli init/rotate`；`GET /pair/{secret}` 全自动配对（303 跳转，secret 不进地址栏）；Cookie 会话（HttpOnly / Secure / SameSite=Strict / Max-Age，库中只存 SHA-256）；`GET|PATCH|DELETE /api/devices`、`POST /api/owner-secret/rotate`、`POST /api/session/logout`；写请求的 Origin 校验；`last_seen_at` 节流刷新。

尚未接入，属后续里程碑：

- **限流**（M9）。方案 12 的"主人读 120 / 写 60 次每分钟"等还没实现。
- **访客码**（M9）。`POST /api/guest/enter` 与 `guest_codes` 表已就位但无代码路径。**实现时必须用带盐的慢哈希**，不得复用 `security.hash_token`（6 位数字用 SHA-256 等于明文，见该模块的说明）。
- **WebSocket 的撤销生效**（M4）。目前撤销对 HTTP 是立即生效；方案 3.3 要求"现有 WebSocket 在下一次心跳或权限检查时关闭"，等 M4 有了连接表再实现。

**两处对方案的补充**：

1. **`DELETE /api/devices`**（集合级）= 撤销除当前设备外的全部设备。方案 3.3 要求这个操作，但 9.2 的接口表里只有按 ID 的单台撤销，没有集合入口。
2. **`RELAY_COOKIE_SECURE`** 配置项，默认 `true`。方案要求 `Secure`，这对生产（HTTPS）是唯一正确取值；但本地 `http://127.0.0.1` 下浏览器会丢弃带 `Secure` 的 Cookie，开发时需要一个显式开关。启动时若它被关闭会打一条 warning。

### M3 完成范围与已知缺口

已完成：`GET|POST /api/boards`、`PATCH /api/boards/{id}`（改名 / 排序 / 续期）；`GET|POST /api/boards/{id}/notes`、`PATCH|DELETE /api/notes/{id}`；`rooms.rev` 单调递增（`rooms.bump_rev`，与业务写入同事务）；`notes.mutation_id` 幂等；访客区的滑动续期与不可变更；keyset 分页。

尚未接入，属后续里程碑：

- **归档、恢复、清空访客区**（M9）：`POST /api/boards/{id}/archive|restore|clear`。到期区域目前会拒绝追加消息（`409 board_expired`），但转成 `archived` 状态要等 M8 的清理任务。
- **回收站恢复**（M9）：`POST /api/notes/{id}/restore`。软删除已经生效，7 天后的硬删除在 M8。
- **搜索**（M10）、**导出**（M9）、**限流**（M9）。

### M4 完成范围与已知缺口

已完成：`GET /api/snapshot`（可见区域 + 每区最近 N 条 + 当前 `rev`，且**同一持锁内读完**）；`/ws` 连接（Origin 校验 → Cookie 会话校验 → `hello.ok` → 心跳 / 重鉴权）；写事务提交后按房间广播 `changed`；`DELETE /api/devices` 与 `DELETE /api/devices/{id}` 会**当场**断掉被撤销设备的连接；`/healthz` 的 `websocketConnections` 反映真实连接数。

三条实现上的取定：

1. **广播是尽力而为的，一致性由快照兜底。** 推送不重试、不落库、不补发；客户端发现本地 `rev` 与服务端对不上就拉一次 `/api/snapshot`。因此"丢推送"不是错误路径，而是一条需要客户端收尾的正常路径——这也是本项目不做持久化事件流的原因（方案 4.3）。事件在**事务提交之后、全局写锁释放之后**才发出：回滚不留痕迹，`rev` 不会出现空洞，慢发送也不会卡住写入。
2. **`/api/snapshot` 在同一次持锁里读完 `rev` 与内容。** 分两次读会产出"rev = N，但内容是 N+1"的快照，客户端据此会把一条变更应用两遍、界面上出现重复消息。
3. **WebSocket 只收心跳与握手，不接受任何业务写入。** 业务写一律走 HTTP（方案 10 最后一条）：两条通道都能写就得把幂等、来源校验、可见性实现两遍，而两份实现迟早分叉。

尚未接入，属后续里程碑：

- **`files` 恒为空数组**（M7）。快照里保留这个键而不是省掉它，是为了让 M5 的前端按最终形状写渲染逻辑，M7 接进来时不需要改前端。
- **二进制帧只回 `bad_message`，不断开**（无计划）。协议里没有它的位置，但为一条打错的帧断线只会制造无谓的重连。
- **重鉴权间隔 60 秒**（`REAUTH_INTERVAL_SEC`）。这是"绕过接口直接改库"的兜底；正常撤销路径是接口当场断开，不走这里。

**两处对方案的补充**：

1. **服务端 `accept` 之后主动发一次 `hello.ok`，不等客户端的 `hello`。** 省掉一个往返，前端也不必处理"连上了但迟迟没有 hello.ok"的超时分支。客户端的 `hello` 仍会被响应（内容相同），所以按方案时序实现的客户端也能工作——代价是 `hello.ok` 可能出现两次，客户端必须幂等处理。
2. **撤销设备时立即断开 WebSocket，不等下一次心跳。** 方案 3.3 的下限是"下一次心跳或权限检查时关闭"，但撤销的用意就是马上切断：在那最多 60 秒的窗口里，被撤销的设备仍在接收房间的全部推送。HTTP 侧本来就已经是立即 401，WebSocket 侧没有理由慢一拍。

为了让连接限额可测，新增了三个配置项 `RELAY_WS_ROOM_LIMIT`（默认 20）、`RELAY_WS_IP_LIMIT`（默认 10）、`RELAY_WS_IDLE_TIMEOUT_SEC`（默认 60）。生产保持方案默认值。

### M5 完成范围与已知缺口

已完成：首页（用途说明、保留规则、主人入口、备案号可配置）与应用页外壳（顶栏 / 区域导航 / 消息流 / 输入区 / 设置抽屉），**服务端渲染、零内联样式与脚本**；`relay/web/` 下的 `app.css` 与 8 个按职责拆分的脚本，**无构建步骤**，语法限制在 Chromium 87 子集；`GET /api/boards/{id}/export?format=txt|md`；安全响应头在代码内统一下发（不依赖反向代理也在，且有测试比对反代配置防止两份漂移）。

四条实现上的取定（第 5 条是**方案没要求、但真机跑一遍就会发现不该没有**的一处补充）：

1. **消息列表渲染抽成 `render.messageList(doc, options)`，`doc` 必须显式传入。** 方案 5.4 要求悬浮窗复用同一套渲染，而悬浮窗在另一个 `Document` 上。渲染函数一旦读全局 `document`，那个页面就只能再抄一份代码——所以这个约束从 M5 第一次写渲染时就落地，而不是等 M10 回头重构。2. **导出用 `rowid` 做排序兜底，不用 `id`。** `notes.id` 是随机 TEXT 主键，同秒写入的两条消息只按 `created_at` 排序时相对顺序不确定（导出两次可能不一样），而用 `id` 排序得到的是随机顺序而不是插入顺序。`rowid` 既是插入顺序，又正好是 `idx_notes_board_time` 的物理顺序，不需要额外排序。快照与分页同步改成 `rowid`（`(created_at, rowid)` 与 `(created_at, id)` 在全序意义上等价，但前者不需要额外比较随机字符串）。
3. **"已编辑"改为按 `updated_at > created_at` 判定，且写入时保证严格大于。** 原来同秒内的"创建 + 编辑"不会显示"已编辑"，属秒级精度的固有结果；但这一条能修——`updated_at` 取 `max(now, created_at + 1)`。置顶不推进 `updated_at`（否则每条被置顶的消息都会挂上"已编辑"）。
4. **首页声明 `favicon.svg`。** 不声明的话浏览器会自己去请求 `/favicon.ico`，必然 404，在每个页面留一条红色控制台错误——它会把真正的错误淹掉（见下）。
5. **记住上次停留的区域**（`localStorage`，方案未要求）。刷新后回到原处而不是跳回第一个区域。对一个"常驻在某个区域里收发文本"的工具来说，每次刷新都要重新找一遍区域，用起来是钝的。实现上必须在快照到达**之前**把它写进 `store`：快照对"激活区域"的规则是"只要它还指得着就保留不动"，晚一步设就只能被当成一次普通切换，首屏还会先闪一下第一个区域。会话失效时清掉这个偏好——那时必然要重新配对，很可能换房间，留着只会指向一个不存在的区域。

**三处只在真实浏览器里才显形的问题**，都是这次真机验证抓到的，静态检查全绿：

1. `util.el` 用 `setAttribute('data-' + 'boardId')` 写 `data-*`。HTML 元素上的 `setAttribute` 会把属性名小写化，落下去的是 `data-boardid`，于是 `getAttribute('data-board-id')` 永远返回 `null` —— **区域标签点了没反应**。
2. `util.el` 用 `setAttribute('className', ...)` 写 class，落下去的是 `classname`。CSS 里那几十条规则一条都没命中，**整个界面完全没有样式**，而控制台一个错都不报。
3. `sync.js` 的 `stopped` 初值是 `true`，而 `resync()` 开头的守卫在 `stopped` 时直接返回。启动流程是**先拉 HTTP 快照、再连 WebSocket**（快照是基线，推送只是加速器），所以首屏那次快照被静默吃掉：`store` 恒空、`rev` 恒 0，界面只能靠后续 WS 事件一条条往外长，刷新后一片空白——而连接状态照样显示"已连接"。

三条的共同点是**没有任何错误信息**，因此补了两层防护：`test_webassets.py` 锁住"属性名有没有走负责转换的 API"，`tests/test_ui_smoke.py` 用真实浏览器断言"JS 建出来的元素真的套上了 CSS"。脚本里的 `stopped` 初值也加了详细注释——它看起来像个无害的初始化值。

尚未接入，属后续里程碑：

- **区域归档 / 恢复 / 清空访客区**（M9）：界面上有入口的位置，但动作接口没实现，所以"更多操作"里只保留导出与复制全文。
- **搜索**（M10）、**限流**（M9）、**回收站恢复**（M9）。
- **`files` 区仍是空数组**（M7）：快照里保留这个键就是为了让前端按最终形状写渲染逻辑。
- **悬浮窗**（M10）：`render.messageList` 已经按"能渲染到另一个 Document"写，但浮窗页面本身还没有。

### M6 完成范围与已知缺口

已完成：`deploy/install.sh`（幂等，含 `--check` 与 `--prefix/--no-system` 本机演练）、`deploy/release.sh`（打包 → 远端校验 sha256 → 备份旧代码 → 铺新代码 → 调 install.sh）、`deploy/rollback.sh`（代码与数据库，数据库回滚会清 WAL）、`deploy/certbot-hopdrop.sh`（`certonly --webroot`，不改写 nginx 配置）、`hopdrop-healthcheck` 与每分钟的 systemd timer、`deploy/RUNBOOK.md`、`relay/tests/test_deploy.py` 与 `relay/tests/test_nginx_site.py`。

**部署动作本身还没在真机上执行过**——缺域名与备案号，见 RUNBOOK 第一节。部署件、演练与验收清单都已就绪，拿到域名即可执行。

**一条改变了整体设计的约束：目标机的 80/443 上是既有的 nginx，不是空的。** 那台机器上还跑着 minitalk（占着 80 端口的 `default_server`）、fortranslate（:18787）、phound、postgres、memos，可用内存只剩三百多 MiB。所以 HopDrop 不是"装一个反代"，而是"作为既有 nginx 的一个 server 块挂进去"。由此派生出的取定：

1. **只 reload、从不 restart nginx。** reload 是平滑的，既有连接不断；restart 会把这台机器上所有站点一起瞬断——而它们跟这次发布毫无关系。有测试锁住 `install.sh` 里不出现 `restart nginx`。
2. **流程固化为"写文件 → `nginx -t` → 通过才 reload"。** 顺序反了的话，一份语法错误的配置会在 reload 那一刻把所有站点一起打挂。校验失败时**自动回滚文件、明确不执行 reload**，并打印「nginx 的状态与本脚本运行前一致」——留着坏配置的话，下一次任何原因触发的 reload（比如 certbot 续期）都会把它读进去，那时已经离本次部署很远了。
3. **`X-Forwarded-For` 用 `$remote_addr` 覆盖写，不用 nginx 文档里最常见的 `$proxy_add_x_forwarded_for`。** 应用侧的 `client_ip()` 取这个头的**第一个**值当来源 IP，用于方案 10 的单 IP 连接上限。追加式写法会把客户端自带的伪造值排在真实地址前面——**任何人加一个请求头就能绕过连接上限**。原来 `client_ip()` 的注释写着"由 Caddy 覆写"，而 Caddy 是自动覆盖的；换成 nginx 后这个前提不再自动成立，所以既改了站点配置，也改了那段注释。
4. **站点自带 `client_max_body_size 24m`。** nginx 全局没设这一项（默认 1 MiB），而应用的单文件上限是 20 MiB。不放开的话，超过 1 MiB 的上传会被 nginx 直接 413，请求根本到不了应用——应用自己那条清晰的错误信息一条都不会出现。24m 是对 20 MiB 留出的余量，有测试断言"反代上限必须大于应用上限"。
5. **HSTS 显式写在 443 段，且不带 `includeSubDomains`。** Caddy 在 HTTPS 下会自动加 HSTS，nginx 不会——切过来之后这一条最容易静默丢失：域名照常能开、证书照样有效，只是"浏览器从此只用 HTTPS"这层保护没了。而带上 `includeSubDomains` 会把 `devilsarchive.cn` 下的其它子域（translate / phound / minitalk）一起拖进强制 HTTPS，那些站点不归 HopDrop 管，给别人的域名下强制策略属于越界。所以既加了 HSTS，也用测试锁住"不得带 includeSubDomains / preload"。
6. **不装 ufw/fail2ban、不改时区与 swap。** `ufw enable` 会按默认策略 deny incoming，把共存服务的端口一起挡掉；fail2ban 装上会启用 sshd jail，可能把部署者自己 ban 掉。时区、swap、内核参数一律只报告不修改——脚本里出现 `timedatectl set-timezone` 是给部署者的提示文字，有测试区分"提到"和"执行"。

另外三点：

- **`relay.service` 加了内存护栏 `MemoryHigh=192M` / `MemoryMax=256M`。** 可用内存只有三百多 MiB，没有上限时 HopDrop 一旦内存泄漏，内核会在整机范围内挑进程杀——被挑中的可能是 minitalk 或 postgres。设了 cgroup 上限之后，被杀的只可能是 HopDrop 自己（`Restart=on-failure` 会拉起来）。同时 `TMPDIR` 指到 `/var/lib/relay/tmp`：`PrivateTmp` 给的 `/tmp` 是 tmpfs，那 20 MiB 的上传会实打实吃掉 20 MiB 内存。
- **`--proxy-headers --forwarded-allow-ips 127.0.0.1`。** 让 uvicorn 采用反代传来的头，但只信来自回环的，也就是只信本机的 nginx。
- **健康检查脚本用 `curl --fail-with-body` 而不是 `-f`。** `-f` 在 503 时**不给响应体**，而 degraded 的原因恰好就在响应体里——排查时最需要的字段被丢掉了。这是写测试时发现的：断言 `reasons` 出现在日志，实际拿到 `reasons=[]`。

还有两处只在真机上才会暴露、提前堵掉的坑：

- **本机 `core.autocrlf=true` 会把 `deploy/*.sh` 以 CRLF 检出**，到 Linux 上直接 `bad interpreter: No such file or directory`——报错信息完全指不到真正的原因。加了 `.gitattributes` 钉死 `eol=lf`，并在全新 clone 里复验过。
- **首次部署时站点是在写完域名之后才生成的。** 站点文件按 `RELAY_DOMAIN` 渲染，而 `relay.env` 是 `install.sh` 第一次跑时从模板生成的（那一项为空，于是跳过站点生成）。所以 `release.sh` 在写完域名后**必须再跑一次 `install.sh`**；少了这一步，域名填了、vhost 却不存在，80 端口的请求会落到这台机器的 `default_server`（minitalk）上，症状是"用自己的域名访问，看到的却是别人的站点"。

尚未接入，属后续里程碑：

- **清理任务**（M8）。`/healthz` 的 `lastCleanupAt` 仍是 `null`，所以 14.5 里"清理任务超过 3 小时未成功即 degraded"这条还没有触发源。
- **限流**（M9）、**文件上传**（M7）。

### 四处对方案的补充与取定

方案 9.3 的接口表没有覆盖到实现时必须做决定的地方，这几条是取定的结果：

1. **`GET /api/boards?status=archived`**。"已归档"是导航里的一个入口（方案 11.2），但 9.3 没有单列归档列表接口。放在同一个区域资源上用查询参数表达，不另造 `/api/archives`。
2. **幂等重放返回 `200`，首次写入返回 `201`。** 方案 9.1 只说"成功创建返回 201，幂等重放返回原结果"，没说重放用哪个状态码。分开表达是因为两者的界面反馈不该一样——重放不该再弹一次"已发送"。
3. **单条消息的 64 KiB 按 UTF-8 字节计**（方案 4.1 写的是"64 KiB UTF-8"）。中文一个字符占 3 字节，按字符限长会让实际占用变成限制值的三倍。
4. **区域数量上限 20，新建默认保留期 60 天。** 后者是方案 4.2 写明的；前者方案未规定，但区域列表要参与每次快照，"支持新建"不能等于"无限新建"。

另外**动作级权限返回 403、资源级可见性返回 404**，这条分界在 M3 被明确下来：访客改名主人的区域是 403（他不能修改任何区域，这个判断在读库之前就成立，因此不泄漏存在性），而访客读取主人的区域消息是 404（这个资源对他不存在）。

### 两处需要产品侧确认的默认值

- **初始区域"日常"取永久保留**（`retention = 0`）。方案只说"新建时选 30/60/永久"，没说初始区域的寿命。取永久的理由是 0.3 的红线——默认落地页静默归档会让用户直接失去写入能力。
- **设备的 `last_seen_at` 刷新、以及所有不影响快照内容的写入，都不递增 `rooms.rev`。** `rev` 的语义是"快照内容变了"，不是"执行了几个 UPDATE"。若把设备在线时间算进去，每台设备每 60 秒就会引发一次全客户端快照重拉。

### 一处已知的体验限制

- **归档区域是完全只读的**：不能追加、不能编辑、不能删除，只能导出和恢复。归档的语义是"封存",允许在封存件上做局部修改会让"导出内容与归档时一致"这条不成立。

## 已知环境注意事项

- **国内网络**：`go.dev` 在本机不通；pip 建议走清华或阿里云镜像（`-i https://pypi.tuna.tsinghua.edu.cn/simple`）。服务器上同理。
- **本机 PowerShell 的 stdout 不回传**，探测类命令请把结果写入文件再读。这只影响交互式排查，不影响服务本身。
- **本机跑全量测试时给一个固定的 `--basetemp`**（例如 `--basetemp="$TEMP/hopdrop-pytest"`，放在系统临时目录下）。pytest 默认会在 `%TEMP%\pytest-of-*` 累积每次运行的目录，收尾时一次性删除几十个条目会被本机的沙箱删除守卫拦下并让整个会话以 `SystemExit` 结束。这不是项目的问题，但会让"全量测试跑不过"看起来像代码坏了。
- `deploy/backup.sh` 依赖系统 `sqlite3` CLI，部署时要在方案 14.1 的系统初始化里装上。

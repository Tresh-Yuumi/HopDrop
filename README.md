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
    identity.py         会话解析与权限依赖
    migrations.py       顺序 SQL 迁移执行器
    notes.py            消息：幂等、编辑、软删除
    rooms.py            房间引导（含两个初始区域）与 rev 递增
    security.py         随机值与哈希
    state.py            进程内运行时状态
  migrations/           NNNN_名称.sql，启动时自动应用
  tests/                pytest
  relay.env.example     配置样例
deploy/                 Caddyfile、systemd unit、备份脚本
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

本次验证通过的依赖版本：Python 3.13.14、fastapi 0.141.1、uvicorn 0.53.0、aiosqlite 0.22.1、python-multipart 0.0.32。

刻意不放 lock 文件：开发机是 Windows、部署机是 Linux，`uvicorn[standard]` 的平台依赖不同（Linux 才有 uvloop），Windows 生成的 freeze 反而会误导服务器。部署时按 `requirements.txt` 的版本区间安装即可。

## 配置

全部参数来自 `RELAY_*` 环境变量，没有配置文件。逐项说明见 `relay/relay.env.example`。代码里的默认值面向本地开发；生产由 `/etc/relay/relay.env` 覆盖。

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
| M4 | WebSocket 推送与快照对齐 | 待做 |
| M5 | 前端页面（文本闭环可点通） | 待做 |
| M6 | 首次真部署（Caddy + systemd + HTTPS） | 待做 |
| M7 | 文件上传与下载 | 待做 |
| M8 | 配额与清理任务 | 待做 |
| M9 | 访客与生命周期（可延后，不返工） | 待做 |
| M10 | 悬浮窗与打磨（可延后） | 待做 |

M1–M6 构成方案的阶段 1（文本闭环）。M9 与 M10 是纯新增模块，不改核心数据模型，可整体延后。

### M1 完成范围与已知缺口

已完成：迁移建出 8 张表 + `schema_version`；`/healthz` 按方案 14.5 返回结构化 JSON 并在不健康时返回 503；统一错误信封与 `X-Request-Id`；`/api/*` 响应 `Cache-Control: no-store`；Caddyfile、systemd unit、备份脚本。

尚未接入，属后续里程碑：

- **清理任务**（M8）。`/healthz` 的 `lastCleanupAt` 现在是 `null`，代码不会假装它跑过。接入后 14.5 里"清理任务超过 3 小时未成功即 degraded"才开始生效。
- **WebSocket**（M4）。`websocketConnections` 现在恒为 `0`。
- **OpenAPI / Swagger UI 已关闭**。文档页要从 CDN 取资源，既被本项目的 CSP 挡住，也多一处对外接口面。接口验证靠测试。
- **`/` 与 `/app` 目前是临时页面**（`app/api/pages.py`，M5 替换）。它们存在的唯一原因是让 M2 的"长期登录"能在浏览器里被看见，因此不使用任何内联样式或脚本（会被部署时的 CSP 拦掉），也不引入模板引擎。

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
- **快照**（M4）：`GET /api/snapshot`。前端暂时只能用 `/api/boards` + `/api/boards/{id}/notes` 拼出界面。
- **搜索**（M10）、**导出**（M9）、**限流**（M9）。

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

### 两处已知的体验限制

- **同一秒内的"创建 + 编辑"不会显示"已编辑"。** 时间字段统一是 Unix 秒（方案 9.1），`updatedAt > createdAt` 是判定依据，秒级精度下两者会相等。这是精度的固有结果，不是判定的缺陷。
- **归档区域是完全只读的**：不能追加、不能编辑、不能删除，只能导出和恢复。归档的语义是"封存",允许在封存件上做局部修改会让"导出内容与归档时一致"这条不成立。

## 已知环境注意事项

- **国内网络**：`go.dev` 在本机不通；pip 建议走清华或阿里云镜像（`-i https://pypi.tuna.tsinghua.edu.cn/simple`）。服务器上同理。
- **本机 PowerShell 的 stdout 不回传**，探测类命令请把结果写入文件再读。这只影响交互式排查，不影响服务本身。
- `deploy/backup.sh` 依赖系统 `sqlite3` CLI，部署时要在方案 14.1 的系统初始化里装上。

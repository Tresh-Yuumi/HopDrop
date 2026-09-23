# HopDrop

跨设备文件与文本中转工具。手机、平板、电脑、银河麒麟桌面之间传文本、图片和不超过 20 MiB 的文件：**打开即看、粘贴即发、一键复制，文件不需要两端同时在线。**

设计文档在 `C:\Users\yuzhe\Documents\Codex\2026-09-21\wo\outputs\`，当前版本 v1.4（文件名含版本号）。本仓库是它的实现，**实现与文档不一致时以文档为准，并应更新文档**。

## 目录

```
relay/                  服务端
  app/                  应用代码
    api/                HTTP 接口，每个模块一个 router
    config.py           RELAY_* 环境变量加载与校验
    db.py               单连接 + 全局锁的数据访问层
    migrations.py       顺序 SQL 迁移执行器
    errors.py           统一错误信封
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

cd relay && ../.venv/Scripts/python.exe -m app
```

另开一个终端验证：

```bash
curl -i 127.0.0.1:8080/healthz
```

本地开发不需要 HTTPS：浏览器把 `http://127.0.0.1` 视为安全上下文，剪贴板 API 照常可用。

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

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 工程骨架与数据库基线：config / db / 迁移 / healthz / 错误信封 / 部署件 | 已完成 |
| M2 | 身份、配对与长期登录 | 待做 |
| M3 | 文本区与消息 CRUD（含 `mutation_id` 幂等与 `rooms.rev`） | 待做 |
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
- **根路径 `/` 尚未提供页面**（M5）。在 M5 之前访问会返回 404 信封。

## 已知环境注意事项

- **国内网络**：`go.dev` 在本机不通；pip 建议走清华或阿里云镜像（`-i https://pypi.tuna.tsinghua.edu.cn/simple`）。服务器上同理。
- **本机 PowerShell 的 stdout 不回传**，探测类命令请把结果写入文件再读。这只影响交互式排查，不影响服务本身。
- `deploy/backup.sh` 依赖系统 `sqlite3` CLI，部署时要在方案 14.1 的系统初始化里装上。

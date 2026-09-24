# HopDrop 部署手册

面向方案 v1.4 第 14 章与第 18 章。这里只写**操作**，设计理由在各脚本顶部的注释里。

一句话流程：

```
开发机                                  服务器
──────────────────────────────────      ──────────────────────────────
bash deploy/release.sh --host root@IP   ← 打包、校验、推送
   └─ 跑测试                             
   └─ 生成 dist/hopdrop-<版本>-<时间>.tar.gz
   └─ scp + 远端 sha256sum -c            
   └─ 远端：备份旧代码 → 铺新代码 ──────→ /opt/relay
                    └─ 调 install.sh ───→ 系统配置 + 重启服务
```

`install.sh` 是幂等的，可以随时手工重跑；`release.sh` 才是每次发布要执行的那条命令。

---

## 一、部署前必须确认的参数

方案第 17 节里标"待填"的项，**部署动作之前**必须落实。前三项没有的话，`install.sh`
会以纯 HTTP 模式起来——能验收服务，但配不上对（原因见下）。

| 参数 | 用在哪 | 当前值 |
|---|---|---|
| 服务器 SSH 目标 | `release.sh --host` | `root@47.116.136.58`（`~/.ssh/config` 里已配 minitalk 密钥） |
| 域名及其解析 | `RELAY_DOMAIN` → Caddy 自动签证书 | 待填 |
| 域名解析是否已指向该 IP | Caddy 签证书的前提 | 待填 |
| 备案号 | `RELAY_ICP_LICENSE`，首页展示（方案 11.1） | 待填 |
| 违规内容举报联系方式 | `RELAY_CONTACT`，首页展示（方案 13） | 待填 |
| 公网带宽 | 只影响上传耗时预期，不影响部署 | 待填 |
| 同机共存服务占用 | 决定磁盘预留是否够（方案 7.1） | 待填 |

**为什么域名是硬前提**：会话 Cookie 带 `Secure`（方案 12），浏览器只在 HTTPS 下保存它。
没有域名就没有 HTTPS，Cookie 被丢弃，症状是"首页打得开、扫码配对也提示成功，刷新后
又回到未登录"。排查这个现象很费时间，因为它看起来像会话逻辑的 bug。

---

## 二、首次部署

### 1. 只读盘查（先做，别跳过）

```bash
ssh root@47.116.136.58 'cat /etc/os-release | head -3; uname -m; \
  df -h /; free -h; \
  ss -lntp | head -20; \
  systemctl list-units --type=service --state=running --no-pager | head -20'
```

要确认三件事：

- **80 / 443 没被别的服务占着**。同机有共存服务，端口冲突会让 Caddy 起不来。
- **/ 的可用空间 ≥ 6 GB**（`install.sh` 的预检会用 3 GB 的软线提醒）。
- **内存够**：2 GiB 跑 uvicorn + SQLite 没问题，但要确认共存服务还剩多少。

### 2. 部署

```bash
bash deploy/release.sh --host root@47.116.136.58 --domain relay.example.com
```

`--domain` 只在首次需要；它会写进 `/etc/relay/relay.env` 并重启 relay 与 caddy。
不带 `--domain` 时服务会以纯 HTTP 起来（见下节验收的注意事项）。

`release.sh` 默认会先跑一遍测试。跳过用 `--skip-tests`，只打包用 `--pack-only`。

### 3. 填备案信息

```bash
ssh root@47.116.136.58
vi /etc/relay/relay.env        # 填 RELAY_ICP_LICENSE 与 RELAY_CONTACT
systemctl restart relay
```

这两项留空时首页不渲染对应段落（而不是渲染空占位），启动日志与 `install.sh` 都会提醒。
**备案号是监管信息，不要在没拿到之前先填一个看起来像样的值。**

### 4. 初始化房间

产品没有注册接口，房间是运维动作（方案 3.1）：

```bash
sudo -u relay /opt/relay/venv/bin/python -m app.cli init --name "我的空间"
```

它会打印**主人链接**。服务端只保存它的哈希，**丢了只能 `rotate` 重新生成**——
把这条链接离线存好（密码管理器里存一份）。

---

## 三、验收清单

对照方案第 18 节的 10 条完成定义，逐条给出可执行的验证方式。

| # | 检查项 | 命令 / 操作 | 通过标准 |
|---|---|---|---|
| 1 | 服务健康 | `curl -sS -i http://127.0.0.1:8080/healthz` | `200`，且 `"status":"ok"` |
| 2 | 结构符合方案 14.5 | 同上 | 含 `database` / `diskFreeBytes` / `dataDirBytes` / `lastCleanupAt` 等字段 |
| 3 | 数据库可写 | 同上 | `"database":"ok"` |
| 4 | 磁盘余量与配额 | 同上 | `diskFreeBytes` > 预留值、`dataDirBytes` < 配额 |
| 5 | Caddy 生效 | `curl -sSI https://<域名>/` | `200` 或 `303`；响应头含 `Content-Security-Policy` |
| 6 | 证书已签发 | `curl -sSI https://<域名>/ \| grep -i strict-transport` | Caddy 自动加 HSTS；证书出现在 `journalctl -u caddy` 里 |
| 7 | 安全头两处一致 | 见 `tests/test_security_headers.py` | 测试通过（比对 Caddyfile 与代码） |
| 8 | 首页备案信息 | 浏览器打开 `/` | 显示备案号与联系方式 |
| 9 | 配对闭环 | 手机浏览器打开主人链接 | 跳到 `/app`，刷新后仍是已配对 |
| 10 | 断线恢复 | 拔网线 → 恢复 | 界面自动重连，内容补齐（不重复） |
| 11 | 重启自恢复 | `reboot` 后等 1 分钟 | relay / caddy / 健康检查 timer 全部 active |
| 12 | 备份 | `sudo /usr/local/bin/hopdrop-backup` | 打印 `backup ok: ...`，`/var/lib/relay/backup/` 出现新文件 |
| 13 | 备份可恢复 | 见下节演练 | `PRAGMA integrity_check` 返回 `ok` |

**注意第 5、6 条依赖域名**。没有域名时 `install.sh` 会让 Caddy 降级到 `:80`，
此时第 5 条要改成 `curl -sSI http://<公网IP>/`；第 6 条无法通过。
另外这种模式下第 9 条也过不了（Cookie 被丢弃），这是预期行为，不是缺陷。

---

## 四、日常发布

```bash
bash deploy/release.sh --host root@47.116.136.58
```

就这一条。`--domain` 不需要再传。

发布包留在 `dist/`：

```
dist/hopdrop-0.1.0-20260925-013000.tar.gz
dist/hopdrop-0.1.0-20260925-013000.tar.gz.sha256
dist/MANIFEST.txt          # 版本、commit、构建时间、迁移数量、回滚命令
```

包内结构（解包到 `/opt/relay` 后正好落位）：

```
./app/ ./web/ ./migrations/ ./requirements.txt ./relay.env.example
./deploy/
./MANIFEST.txt
```

**部署时 `/etc/relay/relay.env` 不会被覆盖**，`/var/lib/relay`（数据库与文件）也不动。
`install.sh` 会报告模板里新增而本地缺失的配置项。

### 发布动作做/不做什么

- 做：跑测试 → 打包 → 远端校验 sha256 → 备份旧代码到 `/opt/relay.prev` →
  删除并重铺 `app/` `web/` `migrations/` `deploy/` → 跑 `install.sh` → 重启 relay。
- 不做：不动 `relay.env`、不动数据库、不动 `/var/lib/relay`。

`app/` `web/` `migrations/` 是"先删再铺"而不是覆盖式解包：覆盖式会把新版本里
已删除或改名的模块留在原地，Python 还能 import 到它们，症状是"改了代码但行为没变"。

---

## 五、回滚

```bash
ssh root@47.116.136.58 'bash /opt/relay/deploy/rollback.sh --list'   # 先看有什么
ssh root@47.116.136.58 'bash /opt/relay/deploy/rollback.sh'          # 回滚代码
```

代码回滚用的是上一次发布留下的 `/opt/relay.prev`。回滚后 `/opt/relay.prev` 与
`/opt/relay` 内容互换，**再执行一次就滚回去了**——所以它本身也是可逆的。

数据库回滚要显式指定备份并确认：

```bash
ssh root@47.116.136.58
bash /opt/relay/deploy/rollback.sh --db relay-20260925-030000.db --yes
```

回滚数据库会**丢掉备份时间点之后的所有消息**，执行前脚本会二次确认并把当前库
另存为 `relay.db.before-rollback-<时间戳>`。它会删除 `relay.db-wal` / `-shm`
——不删的话，旧库启动时会把新 WAL 里的事务重放上去，得到一个两边都不像的库。

**什么时候必须连数据库一起回滚**：新版本的迁移不只是加表加列，还改写了已有数据。
本项目约定迁移只增不改（README 的硬约束 3），所以正常情况下代码回滚就够了；
但"只增"不保证旧代码能读懂新表语义，遇到这种情况按上一条操作。

---

## 六、备份与恢复演练

备份每天 03:00 由 cron 执行，保留最近 7 份，磁盘不足 512 MiB 时跳过而不是失败。

```bash
# 手工备份一次
sudo /usr/local/bin/hopdrop-backup

# 看现有备份
sudo ls -lh /var/lib/relay/backup/
```

**恢复演练**（方案 18 第 5 条要求，建议每季度做一次）：

```bash
# 1. 在真实服务上留一条可辨认的数据（比如往某个区域发一句「演练-<日期>」）

# 2. 备份
sudo /usr/local/bin/hopdrop-backup
LATEST=$(sudo ls -1t /var/lib/relay/backup/relay-*.db | head -1)

# 3. 校验备份本身完好（不碰生产库）
sudo sqlite3 "$LATEST" "PRAGMA integrity_check;"
sudo sqlite3 "$LATEST" "SELECT COUNT(*) FROM notes WHERE deleted_at IS NULL;"

# 4. 走一次真实恢复
sudo bash /opt/relay/deploy/rollback.sh --db "$LATEST" --yes

# 5. 确认演练数据还在、服务正常
curl -sS http://127.0.0.1:8080/healthz
```

演练会在 `/var/lib/relay` 下留下 `relay.db.before-rollback-*`，确认无误后自行清理
（它占的正是那 6 GB 里的一份）。

---

## 七、故障处置

先看健康检查的意见，它每分钟跑一次并把结论写进 journal：

```bash
journalctl -t hopdrop-healthcheck -n 20 --no-pager
cat /var/lib/relay/healthcheck.state
```

`/healthz` 返回 `503` 时，`reasons` 字段直接给出原因：

| reason | 含义 | 处置 |
|---|---|---|
| `database_not_writable` | 库不可写 | 查 `/var/lib/relay` 属主与磁盘；`systemctl status relay` |
| `data_dir_not_writable` | 数据目录不可写 | 同上。注意 `ProtectSystem=strict` 只放行了 `/var/lib/relay` |
| `disk_free_below_reserve` | 磁盘余量低于 3 GB | 清 `/var/lib/relay/backup` 旧份、清 apt 缓存；确认共存服务的增长 |
| `data_dir_over_quota` | 数据目录超过 3 GB | 等清理任务，或调 `RELAY_DATA_QUOTA_GB`（要同步确认磁盘） |
| `cleanup_stale` | 清理任务 3 小时没成功 | M8 才接入清理任务，接入前这个 reason 不会出现 |

### 证书签不下来

```
journalctl -u caddy -n 50 --no-pager | grep -i acme
```

按顺序排除：域名解析是否指向本机（`dig +short <域名>`）→ 80 端口是否公网可达
（备案未完成时会被拦）→ 443 是否被防火墙放行。Caddy 会持续重试，不用手工触发。

### 服务起不来

```bash
systemctl status relay --no-pager -l
journalctl -u relay -n 50 --no-pager
```

最常见的两类：`ModuleNotFoundError: app`（代码没铺到 `/opt/relay/app`，或铺成了
`/opt/relay/relay/app`——`install.sh` 的预检会直接拦这种情况）、依赖没装
（`bash /opt/relay/deploy/install.sh` 重跑一次）。

### 页面能开但配不上对

按顺序查：`RELAY_DOMAIN` 是否配了（没有就是纯 HTTP）→ `RELAY_COOKIE_SECURE`
是否为 1（纯 HTTP 下必须是 0 才能验收）→ 浏览器是否拦截了 Cookie。

这条几乎总是"域名没配"，而不是会话逻辑的问题。

---

## 八、已知限制

- **单 worker**。`relay.service` 里绝不能加 `--workers`（方案 2.2 第 1 条）。
- **健康检查默认不自动重启**。磁盘满、超配额这类 degraded 重启修不好，只会清掉现场。
  要开就改 `hopdrop-healthcheck.service` 里那行注释掉的 `--restart-after=3`。
- **`ufw` 只放行不启用**。同机有共存服务，`ufw enable` 会按默认策略挡掉别的端口，
  这个决定留给运维。`install.sh` 会提示但不动手。
- **服务器上没有 git 仓库**。代码由 `release.sh` 铺上去，靠 `MANIFEST.txt` 记版本。
- **文件不做备份**（方案 7.5）。files 目录 7 天过期靠用户重新上传；归档数据在数据库里，
  所以备份数据库就等于备份了归档。
- **清理任务尚未接入**（M8）。`lastCleanupAt` 现在是 `null`，代码不假装它跑过。

---

## 九、本机演练（没有 Linux 机器时）

`install.sh` 支持两种参数让自己能在开发机上跑：

```bash
# 构造一个和服务器同构的目录树
DRILL=/tmp/hopdrop-drill
rm -rf "$DRILL" && mkdir -p "$DRILL/opt/relay"
cp -a relay/app relay/web relay/migrations relay/requirements.txt relay/relay.env.example "$DRILL/opt/relay/"
cp -a deploy "$DRILL/opt/relay/deploy"

bash deploy/install.sh --prefix "$DRILL"            # 真跑，只跳过 apt/ufw/systemd/useradd
bash deploy/install.sh --prefix "$DRILL"            # 再跑一次，应当全是"无变化"
bash deploy/install.sh --prefix "$DRILL" --check     # 只报告
```

`--prefix` 会把所有绝对路径挂到该目录下，`--no-system`（`--prefix` 会自动打开）
跳过需要真实系统与 root 的步骤。跑的是真实的文件生成、权限与幂等逻辑。

`tests/test_deploy.py` 把上面这段演练固化成测试，所以 CI 与本地都会覆盖到。

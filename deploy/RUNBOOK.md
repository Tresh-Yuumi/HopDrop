# HopDrop 部署手册

面向方案 v1.4 第 14 章与第 18 章。这里只写**操作**，设计理由在各脚本顶部的注释里。

一句话流程：

```
开发机（Git Bash）                        服务器 47.116.136.58
────────────────────────────────────      ──────────────────────────────
bash deploy/release.sh --host root@IP
   ├─ 跑测试
   ├─ 生成 dist/hopdrop-<版本>-<时间>.tar.gz
   ├─ scp + 远端 sha256sum -c              （包在服务器上再校验一次）
   ├─ 远端：备份旧代码 → 铺新代码 ───────→ /opt/relay
   └─ 远端：调 install.sh ───────────────→ 建用户/venv/systemd/nginx 站点
                                            ↑ 只 reload nginx，从不 restart
```

`install.sh` 是幂等的，可以随时手工重跑；`release.sh` 才是每次发布要执行的那条命令。

---

## 〇、目标机现状（部署前先读，它决定了后面所有的"为什么"）

这台机器上**不止 HopDrop 一个服务**。下面的表是实际盘查结果（2026-09-25），
部署动作必须在这个前提下做：

| 项 | 实际值 | 对 HopDrop 的影响 |
|---|---|---|
| 80 / 443 | **nginx 1.28.3**（不是 Caddy） | HopDrop 只做它的一个 server 块，不装反代、不抢端口 |
| 80 端口 default_server | `server_name _;` → **minitalk** | HopDrop 必须有明确 `server_name`，否则请求会串到 minitalk |
| 443 已有站点 | `translate.devilsarchive.cn`、`phound.devilsarchive.cn` | 新增域名走同一个 nginx，互不影响 |
| 同机服务 | minitalk(107M) / fortranslate(72M) / postgres / memos / 2×node | 内存是硬约束 |
| 内存 | 1607 MiB 总量，**可用约 344 MiB**，swap 2 GiB | `relay.service` 设了 MemoryMax=256M，防止 HopDrop 拖垮整机 |
| 磁盘 `/` | 40 GiB，已用 6.3 GiB，**剩 31 GiB** | 富余（`install.sh` 的下限是 3 GiB） |
| **8080** | **空闲** | HopDrop 监听 `127.0.0.1:8080`，不冲突 |
| `/opt/relay` | 不存在 | 全新目录，不覆盖任何东西 |
| `relay` 用户 | 不存在 | 由 `install.sh` 创建 |
| 时区 | `Asia/Shanghai` | 脚本只报告不改（改它会影响同机服务的日志时间戳） |
| ufw | `inactive` | 脚本不装也不启用（`ufw enable` 会按默认策略挡掉别人的端口） |
| certbot | 4.0.0 + 自动续期 timer | 复用现有的，不新装 |

**结论：HopDrop 是"加进去"，不是"换掉"。** 全部动作只有三处会碰到系统：
建 `relay` 用户、写 `/etc/nginx/sites-available/hopdrop`（并软链到 `sites-enabled`）、
注册 `relay` 与健康检查两个 systemd unit。80/443 只是多了一个 vhost。

---

## 一、部署前必须确认的参数

| 参数 | 用在哪 | 当前值 |
|---|---|---|
| 服务器 SSH 目标 | `release.sh --host` | `root@47.116.136.58`（`~/.ssh/config` 里已配） |
| **域名** | `RELAY_DOMAIN`，决定 nginx 的 `server_name` 与证书 | **待定**（建议 `drop.devilsarchive.cn` 或 `relay.devilsarchive.cn`） |
| 域名解析 | 必须指向 `47.116.136.58`，证书才能签发 | 待配 |
| **备案号** | `RELAY_ICP_LICENSE`，首页展示（方案 11.1） | **待填**（`devilsarchive.cn` 已备案，去阿里云备案控制台取原文） |
| 违规内容举报联系方式 | `RELAY_CONTACT`，首页展示（方案 13） | 待填 |

**为什么域名是硬前提**：会话 Cookie 带 `Secure`（方案 12），浏览器只在 HTTPS 下保存它。
没有域名就没有 HTTPS，Cookie 被丢弃，症状是"首页打得开、配对也提示成功，刷新后又回到
未登录"。这个现象看起来像会话逻辑的 bug，排查起来很费时间。

**备案号必须拿到再填**。这两项留空时首页不渲染对应段落（而不是渲染空占位），
`install.sh` 与启动日志都会提醒。**不要先编一个看起来像样的值填进去。**

---

## 二、首次部署（按顺序执行）

### 0. 开发机：先提交，再打包

`release.sh` 会把构建时的 commit 写进 `MANIFEST.txt`，工作区不干净时会标 `-dirty`
（包仍然是能用的，只是事后对着版本号查不出对应代码）。

```bash
cd /d/MyFile/workbuddy/HopDrop
git status --short          # 有改动就先 commit
```

### 1. 配域名解析（在域名服务商后台做，不在这台机器上）

加一条 A 记录：`<你选的子域>.devilsarchive.cn` → `47.116.136.58`。

验证（在**服务器上**跑，确保解析已生效到公网）：

```bash
ssh root@47.116.136.58 'getent ahostsv4 <你的域名> | head -2'
```

出现 `47.116.136.58` 即可继续。解析没生效就往下走，证书一定签不下来
（Let's Encrypt 会失败，而失败次数是有配额的）。

### 2. 演练打包（不上服务器，纯本机检查）

```bash
cd /d/MyFile/workbuddy/HopDrop
bash deploy/release.sh --pack-only
```

这一步会跑一遍测试、产出 `dist/` 下的包与校验值。确认没有 FAILED 再往下。

### 3. 服务器：只读预检（**关键步骤，不改任何东西**）

把包传上去解到临时目录，用 `--check` 看它打算做什么：

```bash
cd /d/MyFile/workbuddy/HopDrop
PKG=$(ls -1t dist/hopdrop-*.tar.gz | head -1)

scp "$PKG" root@47.116.136.58:/tmp/hopdrop-check.tar.gz
ssh root@47.116.136.58 \
  "rm -rf /tmp/hopdrop-check && mkdir -p /tmp/hopdrop-check && \
   tar -xzf /tmp/hopdrop-check.tar.gz -C /tmp/hopdrop-check && \
   bash /tmp/hopdrop-check/deploy/install.sh --check"
```

`--check` **不写任何文件、不动任何服务**，只报告计划。输出里要能确认这几条：

- 「反代：探测到 80/443 由 **nginx** 持有」——不是 caddy、不是 none
- 「磁盘剩余 …」大于 3072 MiB
- 「将创建 /opt/relay …」「将创建 relay 用户」
- 站点那一段报「计划写入 /etc/nginx/sites-available/hopdrop」

此时 `RELAY_DOMAIN` 还没配，所以**站点分支会跳过**——这是对的，看到
「RELAY_DOMAIN 未配置，无法生成 server_name」就是正常的。

**把这段输出留着。** 它是"这次部署会做什么"的完整清单，出问题时对照用。

### 4. 服务器：真部署

```bash
cd /d/MyFile/workbuddy/HopDrop
bash deploy/release.sh --host root@47.116.136.58 --domain <你的域名>
```

这一条命令做完：上传校验 → 备份旧代码（首次没有，会提示）→ 铺代码到 `/opt/relay`
→ 建 `relay` 用户与 `/var/lib/relay` → 建 venv 装依赖 → 写 `/etc/relay/relay.env`
→ 注册 systemd unit → 写 nginx 站点（此时只有 80 段，没有证书）→ `nginx -t` →
reload nginx → 启动 relay → 写域名 → **再跑一次 install.sh 生成站点**。

最后那一步是必需的：站点文件按 `RELAY_DOMAIN` 渲染，而第一次 `install.sh` 跑到时
`relay.env` 里那一项还是空的。少了它，域名填了、vhost 却不存在，访问自己的域名会
落到这台机器的 `default_server`（minitalk）上。

### 5. 服务器：签证书

```bash
ssh root@47.116.136.58 'bash /opt/relay/deploy/certbot-hopdrop.sh <你的域名> <你的邮箱>'
```

用 `certonly --webroot` 而不是 `certbot --nginx`：后者会改写 nginx 配置并打上
`managed by Certbot` 标记，而站点文件是每次发布都由 `install.sh` 整体重写的——
两个来源会互相覆盖，且不报错。脚本会在请求证书**之前**先做本地 webroot 自检，
避免白白消耗 ACME 的失败配额。

签成功后，再跑一次 `install.sh` 让站点换成带 443 的版本（脚本会在结尾提示）：

```bash
ssh root@47.116.136.58 'bash /opt/relay/deploy/install.sh'
```

### 6. 服务器：初始化房间

产品没有注册接口，房间是运维动作（方案 3.1）：

```bash
ssh root@47.116.136.58
sudo -u relay /opt/relay/venv/bin/python -m app.cli init --name "我的空间"
```

它会打印**主人链接**。服务端只保存它的哈希，**丢了只能 `rotate` 重新生成**——
把这条链接离线存好（密码管理器里存一份）。

### 7. 填备案信息

```bash
ssh root@47.116.136.58
vi /etc/relay/relay.env        # 填 RELAY_ICP_LICENSE 与 RELAY_CONTACT
systemctl restart relay        # 这个只重启 HopDrop，不碰 nginx
```

---

## 三、验收清单

对照方案第 18 节的 10 条完成定义。**在服务器上或从本机都能跑。**

| # | 检查项 | 命令 | 通过标准 |
|---|---|---|---|
| 1 | 服务健康 | `curl -sS -i http://127.0.0.1:8080/healthz` | `200`，`"status":"ok"` |
| 2 | 健康结构 | 同上 | 含 `database` / `diskFreeBytes` / `dataDirBytes` / `lastCleanupAt` 等字段 |
| 3 | 数据库可写 | 同上 | `"database":"ok"` |
| 4 | 磁盘余量与配额 | 同上 | `diskFreeBytes` 高于预留、`dataDirBytes` 低于配额 |
| 5 | nginx 站点生效 | `curl -sSI https://<域名>/ -H 'Host: <域名>' --resolve <域名>:443:127.0.0.1` | `200` 或 `303`；响应头含 `Content-Security-Policy` |
| 6 | 证书与 HSTS | `curl -sSI https://<域名>/ --resolve <域名>:443:127.0.0.1 \| grep -i strict-transport` | 有 `max-age=`；且**不含** `includeSubDomains` |
| 7 | 没影响别的站点 | `curl -sSI https://translate.devilsarchive.cn/ \| head -1`；`curl -sSI https://phound.devilsarchive.cn/ \| head -1` | 都还是正常状态码（各站点照旧） |
| 8 | nginx 没重启过 | `systemctl show nginx -p ActiveEnterTimestamp` | 时间戳**早于**本次部署（说明只 reload 了） |
| 9 | 首页备案信息 | 浏览器打开 `https://<域名>/` | 显示备案号与联系方式 |
| 10 | 配对闭环 | 手机浏览器打开主人链接 | 跳到 `/app`，刷新后仍是已配对 |
| 11 | 断线恢复 | 手机切飞行模式 → 恢复 | 界面自动重连，内容补齐（不重复） |
| 12 | 重启自恢复 | `reboot` 后等 1 分钟 | `relay`、`nginx`、`hopdrop-healthcheck.timer` 全部 active |
| 13 | 备份 | `sudo /usr/local/bin/hopdrop-backup` | 打印 `backup ok: …`，`/var/lib/relay/backup/` 出现新文件 |
| 14 | 备份可恢复 | 见第六节演练 | `PRAGMA integrity_check` 返回 `ok` |

**第 5、6 条依赖域名**。没有域名时站点停在纯 HTTP，第 5 条改成 `curl -sSI http://127.0.0.1/ -H 'Host: <域名>'`；
第 6 条无法通过（HTTP 不下发 HSTS）；第 10 条也过不了（Cookie 被丢），这是预期行为，不是缺陷。

---

## 四、日常发布

```bash
cd /d/MyFile/workbuddy/HopDrop
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

- **做**：跑测试 → 打包 → 远端校验 sha256 → 备份旧代码到 `/opt/relay.prev` →
  删除并重铺 `app/` `web/` `migrations/` `deploy/` → 跑 `install.sh` →
  写 nginx 站点 → `nginx -t` → **reload** nginx → 重启 relay。
- **不做**：不动 `relay.env`（除 `--domain` 指定的那一行）、不动数据库、
  不动 `/var/lib/relay`、**不 restart nginx**、不装 ufw/fail2ban、不改时区与 swap。

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

**回滚不影响其它站点**：整个过程只碰 `/opt/relay` 与 `/var/lib/relay`，
nginx 站点文件不变，也不需要 reload。

**什么时候必须连数据库一起回滚**：新版本的迁移不只是加表加列，还改写了已有数据。
本项目约定迁移只增不改（README 的硬约束 3），所以正常情况下代码回滚就够了。

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

演练会在 `/var/lib/relay` 下留下 `relay.db.before-rollback-*`，确认无误后自行清理。

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

```bash
bash /opt/relay/deploy/certbot-hopdrop.sh <域名> <邮箱>    # 重跑，它会逐条检查前置条件
```

按顺序排除：域名解析是否指向本机（`getent ahostsv4 <域名>`）→ 80 端口是否公网可达
（**备案未完成时会被云厂商拦掉**，典型症状是"境内连不上、境外能连"）→ 云控制台的
**安全组**是否放行 80（这和机器上的 ufw 是两回事）→ 站点文件的 `server_name` 是否
写成了别的域名（写错的话请求会掉到 80 的 `default_server`，也就是 minitalk 上）。

### 改了 nginx 配置之后

```bash
nginx -t                                  # 一定先校验
systemctl reload nginx                    # reload 平滑；不要 restart
```

`install.sh` 已经固化了这个顺序，并且在校验失败时**自动回滚文件、不执行 reload**，
所以正常部署不需要手工做这一步。

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
是否为 1 → 浏览器是否拦截了 Cookie。

这条几乎总是"域名没配"，而不是会话逻辑的问题。

---

## 八、已知限制

- **单 worker**。`relay.service` 里绝不能加 `--workers`（方案 2.2 第 1 条）。
- **内存上限 256 MiB**。正常 RSS 在 60–90 MiB；触顶时被内核杀的是 HopDrop 自己
  （`Restart=on-failure` 会拉起来），而不是同机的 minitalk / postgres。
- **健康检查默认不自动重启**。磁盘满、超配额这类 degraded 重启修不好，只会清掉现场。
  要开就改 `hopdrop-healthcheck.service` 里那行注释掉的 `--restart-after=3`。
- **`ufw` 不装不启用**。同机有共存服务，`ufw enable` 会按默认策略挡掉别的端口。
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

bash deploy/install.sh --prefix "$DRILL"            # 真跑，只跳过 apt/systemd/useradd
bash deploy/install.sh --prefix "$DRILL"            # 再跑一次，应当全是"无变化"
bash deploy/install.sh --prefix "$DRILL" --check     # 只报告
```

`--prefix` 会把所有绝对路径挂到该目录下并自动打开 `--no-system`，跳过需要真实系统与
root 的步骤。跑的是真实的文件生成、权限与幂等逻辑。填了域名之后，生成的 nginx 站点
就在 `$DRILL/etc/nginx/sites-available/hopdrop`，可以直接看。

`tests/test_deploy.py` 与 `tests/test_nginx_site.py` 把上面这段演练和站点渲染都固化成了
测试，所以本地与 CI 都会覆盖到。

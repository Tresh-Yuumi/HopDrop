#!/usr/bin/env bash
#
# HopDrop 发布（方案 18 第 10 条：发布包含版本号、迁移脚本、回滚方法、校验值）。
#
# **在开发机上跑**。它做三件事：打包、推送、让服务器完成安装。
#
#   bash deploy/release.sh --host root@1.2.3.4
#   bash deploy/release.sh --host root@1.2.3.4 --domain relay.example.com
#   bash deploy/release.sh --pack-only            # 只产出发布包与校验值，不上传
#   bash deploy/release.sh --host root@1.2.3.4 --dry-run
#
# 为什么不用 git 拉代码：服务器要为此再配一份部署密钥与仓库权限，而这就是
# 一个单人项目、一台机器，把仓库的 relay/ 目录内容铺到 /opt/relay 就够了。
# 代价是仓库的 .git 不会上去，服务器上查不到提交历史——MANIFEST.txt 里记了
# 构建时的 commit，够定位版本。
#
# 目录布局为什么是"铺平"的：deploy/relay.service 里写着
#   WorkingDirectory=/opt/relay
#   ExecStart=/opt/relay/venv/bin/python -m uvicorn app.main:app
# 也就是要求 /opt/relay/app/main.py 存在。而仓库里 relay/ 与 deploy/ 是同级
# 目录，所以打包时以 relay/ 为根（-C relay .），deploy/ 另外挂进去。

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SRC_DIR")"
# 可用环境变量改到别处，测试会指到临时目录，免得往仓库里丢发布包。
DIST_DIR="${RELAY_DIST_DIR:-$REPO_DIR/dist}"

HOST=""
DOMAIN=""
APP_DIR="/opt/relay"
DRY_RUN=0
PACK_ONLY=0
SKIP_TESTS=0

usage() {
	cat <<'EOF'
用法：bash deploy/release.sh --host root@服务器 [选项]

  --host USER@HOST    目标服务器（ssh 别名或 user@ip）
  --domain DOMAIN     首次部署时写入 /etc/relay/relay.env 的 RELAY_DOMAIN
  --app-dir DIR       远端部署目录，默认 /opt/relay
  --pack-only         只在本机产出 dist/ 下的发布包与 sha256，不连接服务器
  --skip-tests        发布前不跑测试（默认会跑，见下）
  --dry-run           打印将要执行的步骤，不做任何改动
  -h, --help          显示本帮助
EOF
}

die() {
	printf '错误：%s\n' "$*" >&2
	exit 1
}

step() { printf '\n== %s\n' "$*"; }
say() { printf '  %s\n' "$*"; }

while [ $# -gt 0 ]; do
	case "$1" in
	--host)
		shift
		HOST="${1:-}"
		;;
	--host=*) HOST="${1#--host=}" ;;
	--domain)
		shift
		DOMAIN="${1:-}"
		;;
	--domain=*) DOMAIN="${1#--domain=}" ;;
	--app-dir)
		shift
		APP_DIR="${1:-}"
		;;
	--app-dir=*) APP_DIR="${1#--app-dir=}" ;;
	--pack-only) PACK_ONLY=1 ;;
	--skip-tests) SKIP_TESTS=1 ;;
	--dry-run) DRY_RUN=1 ;;
	-h | --help)
		usage
		exit 0
		;;
	*) die "未知参数：$1（--help 看用法）" ;;
	esac
	shift
done

if [ "$PACK_ONLY" = 0 ] && [ -z "$HOST" ]; then
	die "缺少 --host（或用 --pack-only 只打包）"
fi

# ---------------------------------------------------------------- 打包

VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$REPO_DIR/relay/app/__init__.py")"
[ -n "$VERSION" ] || die "没能从 relay/app/__init__.py 读出 __version__"

COMMIT="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
DIRTY=""
if ! git -C "$REPO_DIR" diff --quiet 2>/dev/null; then
	DIRTY="-dirty"
fi

STAMP="$(date -u +%Y%m%d-%H%M%S)"
PKG_NAME="hopdrop-${VERSION}-${STAMP}.tar.gz"
PKG_PATH="$DIST_DIR/$PKG_NAME"

step "发布前检查"

# 发布前跑测试是这个脚本里唯一"多余"的步骤，但它的性价比最高：一次带着
# 回归缺陷的发布，排查成本远高于在这里等两分钟。
if [ "$SKIP_TESTS" = 1 ]; then
	say "已跳过测试（--skip-tests）"
elif [ "$DRY_RUN" = 1 ]; then
	say "（dry-run）将执行：pytest -q"
elif [ -x "$REPO_DIR/.venv/Scripts/python.exe" ] || [ -x "$REPO_DIR/.venv/bin/python" ]; then
	PY="$REPO_DIR/.venv/Scripts/python.exe"
	[ -x "$PY" ] || PY="$REPO_DIR/.venv/bin/python"
	say "跑测试：$PY -m pytest -q"
	# 固定 basetemp 到系统临时目录：pytest 默认会在 %TEMP% 累积每次运行的
	# 目录，收尾一次性删除会被本机沙箱的删除守卫拦下（README 有记）。
	(cd "$REPO_DIR/relay" && "$PY" -m pytest -q --basetemp="${TMPDIR:-/tmp}/hopdrop-release-pytest")
else
	say "找不到 .venv，跳过测试"
fi

if [ "$DRY_RUN" = 1 ]; then
	say "（dry-run）将校验工作区是否干净"
elif [ -n "$DIRTY" ]; then
	say "警告：工作区有未提交改动，发布包会带上它们（版本号后标记 -dirty）"
fi

step "打包"

MANIFEST="$DIST_DIR/MANIFEST.txt"
mkdir -p "$DIST_DIR"
{
	printf 'version=%s\n' "$VERSION"
	printf 'commit=%s%s\n' "$COMMIT" "$DIRTY"
	printf 'built_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
	printf 'built_on=%s\n' "$(uname -s)"
	printf 'migrations=%s\n' "$(ls -1 "$REPO_DIR/relay/migrations"/*.sql 2>/dev/null | wc -l | tr -d ' ')"
	# 回滚方法写进包里，而不是只写在 RUNBOOK 里：出事的那一刻，手上有的
	# 就是这个包，不会有人先去翻文档。
	printf 'rollback=%s\n' "bash $APP_DIR/deploy/rollback.sh"
} >"$MANIFEST"

if [ "$DRY_RUN" = 1 ]; then
	say "（dry-run）将生成 $PKG_PATH"
	say "（dry-run）将生成 $PKG_PATH.sha256"
else
	# 排除 tests / data / 缓存 / 开发依赖：服务器不需要，更不能把本地开发
	# 数据覆盖到生产数据目录上。
	#
	# 注意 `__pycache__` 必须写成 `*/__pycache__`：--exclude 是拿完整成员名去
	# 匹配的，根一级的 `./__pycache__` 匹配不到 `./app/__pycache__`。写错的
	# 后果不是报错，而是把本机编译的字节码一起发到服务器上——Python 会优先
	# 用它，于是改了源码但行为没变，而且没人会想到去看 __pycache__。
	#
	# -C relay . 让包内路径是 ./app/... 而不是 ./relay/app/...，解包到
	# /opt/relay 之后就正好落在该在的位置。
	tar \
		--exclude='*/__pycache__' \
		--exclude='./__pycache__' \
		--exclude='*.pyc' \
		--exclude='./tests' \
		--exclude='./data' \
		--exclude='./.pytest_cache' \
		--exclude='./pytest.ini' \
		--exclude='./requirements-dev.txt' \
		--exclude='*.db' \
		--exclude='*.db-wal' \
		--exclude='*.db-shm' \
		-czf "$PKG_PATH" \
		-C "$REPO_DIR/relay" . \
		-C "$REPO_DIR" deploy \
		-C "$DIST_DIR" MANIFEST.txt

	(
		cd "$DIST_DIR"
		if command -v sha256sum >/dev/null 2>&1; then
			sha256sum "$PKG_NAME" >"$PKG_NAME.sha256"
		else
			shasum -a 256 "$PKG_NAME" >"$PKG_NAME.sha256"
		fi
	)
	say "已生成 $PKG_PATH"
	say "校验值 $(cat "$PKG_PATH.sha256")"
fi

if [ "$PACK_ONLY" = 1 ]; then
	printf '\n只打包，未上传。\n'
	exit 0
fi

# ---------------------------------------------------------------- 推送与部署

step "推送到 $HOST"

if [ "$DRY_RUN" = 1 ]; then
	say "（dry-run）将执行：scp $PKG_PATH $HOST:/tmp/hopdrop-upload.tar.gz"
	say "（dry-run）将执行：ssh $HOST 解包 + 备份旧代码 + 跑 install.sh"
	exit 0
fi

REMOTE_PKG="/tmp/hopdrop-upload.tar.gz"
REMOTE_SHA="/tmp/hopdrop-upload.tar.gz.sha256"

# 先传校验文件再传包，然后**在服务器上校验**。只在本机算一次 sha256 是
# 不够的：传输出错、磁盘写满截断，都会得到一个"本地校验值正确"的坏包。
scp -q "$PKG_PATH" "$HOST:$REMOTE_PKG"
scp -q "$PKG_PATH.sha256" "$HOST:$REMOTE_SHA"
# 校验文件里的文件名是本地包名，远端要改成实际落地的名字。
ssh "$HOST" "sed -i 's| .*| ${REMOTE_PKG#/tmp/}|' $REMOTE_SHA && sha256sum -c $REMOTE_SHA"
say "远端校验通过"

# 远端脚本通过 stdin 传给远端 bash，避免多层引号转义。
# 变量用位置参数传，不用 ssh 的环境变量（ssh 默认不传环境）。
ssh "$HOST" "bash -s -- '$APP_DIR' '$VERSION' '$REMOTE_PKG'" <<'REMOTE'
set -euo pipefail
APP_DIR="$1"
VERSION="$2"
PKG="$3"
PREV_DIR="/opt/relay.prev"
STAGE="$(mktemp -d /tmp/hopdrop-stage-XXXXXX)"

say() { printf '  %s\n' "$*"; }

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

tar -xzf "$PKG" -C "$STAGE"

if [ ! -f "$STAGE/app/main.py" ]; then
	printf '错误：发布包里没有 app/main.py，包结构不对\n' >&2
	exit 1
fi

# 备份当前代码，供 rollback.sh 使用。不含 venv 与 data：
# venv 与版本无关，data 是用户数据，都不该参与代码回滚。
if [ -f "$APP_DIR/app/main.py" ]; then
	rm -rf "$PREV_DIR"
	mkdir -p "$PREV_DIR"
	for item in app web migrations deploy requirements.txt relay.env.example MANIFEST.txt; do
		if [ -e "$APP_DIR/$item" ]; then
			cp -a "$APP_DIR/$item" "$PREV_DIR/"
		fi
	done
	say "旧代码已备份到 $PREV_DIR"
else
	say "首次部署，无旧代码可备份"
fi

mkdir -p "$APP_DIR"

# app/web/migrations 三个目录先删再铺。tar 是覆盖式解包，不删的话，
# 新版本里被删掉或改名的模块会留在原地——Python 还能 import 到它们，
# 症状是"改了代码但行为没变"，很难查。
for item in app web migrations; do
	rm -rf "$APP_DIR/$item"
	cp -a "$STAGE/$item" "$APP_DIR/$item"
done

# deploy/ 也整体替换：这里的东西都是"由仓库管理"的，不期望在服务器上手改。
rm -rf "$APP_DIR/deploy"
cp -a "$STAGE/deploy" "$APP_DIR/deploy"

for item in requirements.txt relay.env.example MANIFEST.txt; do
	if [ -e "$STAGE/$item" ]; then
		cp -a "$STAGE/$item" "$APP_DIR/$item"
	fi
done

# 目录属主：relay 用户要能读代码。deploy/ 里的脚本要可执行。
if id -u relay >/dev/null 2>&1; then
	chown -R relay:relay "$APP_DIR/app" "$APP_DIR/web" "$APP_DIR/migrations" 2>/dev/null || true
	for item in requirements.txt relay.env.example MANIFEST.txt; do
		[ -e "$APP_DIR/$item" ] && chown relay:relay "$APP_DIR/$item" 2>/dev/null || true
	done
fi
chmod 755 "$APP_DIR/deploy"/*.sh 2>/dev/null || true

say "代码已铺到 $APP_DIR（version=$VERSION）"
say "--- 执行 install.sh ---"
bash "$APP_DIR/deploy/install.sh"
REMOTE

step "部署完成"

if [ -n "$DOMAIN" ]; then
	# 只更新 RELAY_DOMAIN 一行，其余保持原样。写在 install.sh 之后：
	# install.sh 生成 relay.env 时用的是模板，模板里这一项是空的。
	# 监听地址（RELAY_ADDR）不动，那是反向代理指向的上游。
	ssh "$HOST" "bash -s -- '$DOMAIN' '$APP_DIR'" <<'REMOTE'
set -euo pipefail
DOMAIN="$1"
APP_DIR="$2"
ENV_FILE=/etc/relay/relay.env
[ -f "$ENV_FILE" ] || { printf 'relay.env 不存在，跳过域名写入\n' >&2; exit 0; }
if grep -q '^RELAY_DOMAIN=' "$ENV_FILE"; then
	sed -i "s|^RELAY_DOMAIN=.*|RELAY_DOMAIN=${DOMAIN}|" "$ENV_FILE"
else
	printf '\n# 由 release.sh --domain 写入\nRELAY_DOMAIN=%s\n' "$DOMAIN" >>"$ENV_FILE"
fi
printf 'RELAY_DOMAIN=%s 已写入 %s\n' "$DOMAIN" "$ENV_FILE"

# 站点文件是按 RELAY_DOMAIN 渲染的，而上面那次 install.sh 跑到时这一项还是空的
# （relay.env 刚由它自己从模板生成）。所以必须再跑一次——这是唯一会生成 nginx
# 站点的地方。少了这一步：域名填了、vhost 却没有，80 端口的请求会掉到这台机器
# 的 default_server（minitalk）上，症状是"用自己的域名访问，看到的却是别人的站"。
printf '  %s\n' "重跑 install.sh，按新域名生成 nginx 站点"
bash "$APP_DIR/deploy/install.sh"
REMOTE
	say "域名已配置：$DOMAIN"
fi

printf '\n发布完成。下一步：\n'
printf '  健康检查  ssh %s "curl -sS -i http://127.0.0.1:8080/healthz"\n' "$HOST"
printf '  服务日志  ssh %s "journalctl -u relay -n 30 --no-pager"\n' "$HOST"
printf '  回滚      ssh %s "bash %s/deploy/rollback.sh"\n' "$HOST" "$APP_DIR"

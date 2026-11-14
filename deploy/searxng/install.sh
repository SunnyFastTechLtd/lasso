#!/usr/bin/env bash
# 安装 / 更新 SearXNG（Lasso 天工 L2 默认搜索），可重复执行
#
# 幂等口径：
#   - 源码固定到 $SEARXNG_COMMIT，已在该 commit 时不重复拉取；
#   - /etc/searxng/settings.yml 已存在时不覆盖（线上改配置直接改这个文件），
#     只有显式 --reset-config 才用模板重新渲染；
#   - venv 已可用时不重建，pip 已满足时不重复下载；
#   - 结尾统一 daemon-reload + enable + restart + 健康检查，重复执行得到同一状态。
#
# 用法：
#   sudo deploy/searxng/install.sh                 # 安装或更新到固定 commit
#   sudo deploy/searxng/install.sh --reset-config  # 同时用模板重置 settings.yml（会换 secret_key）
#
# 可用环境变量覆盖默认值：
#   SEARXNG_COMMIT     源码 commit，默认 a7128d5d1d5af2837a68fbeaaee9c3e8391aa9cf
#   SEARXNG_REPO       源码仓库，默认 https://github.com/searxng/searxng
#   SEARXNG_SRC        源码目录，默认 /opt/searxng
#   SEARXNG_CONF_DIR   配置目录，默认 /etc/searxng
#   SEARXNG_CACHE_DIR  缓存目录，默认 /var/cache/searxng
#   SEARXNG_USER       运行用户，默认 searxng
#   SEARXNG_PYTHON     基础解释器，默认 /opt/cpython-3.11/bin/python3.11
#   PIP_INDEX_URL      pip 镜像，默认 https://pypi.tuna.tsinghua.edu.cn/simple
#                      （.36 上 pypi.org 直连超时，必须走镜像）
#   SEARXNG_PROXY_BING / _DUCKDUCKGO / _WIKIPEDIA / _GITHUB / _STACKOVERFLOW
#                      首次渲染时写进各引擎的代理地址；留空=直连。
#                      默认仅 DDG 与维基走 http://192.0.2.10:7890
#   LASSO_USER         平台运行用户，默认 lasso：脚本会授予它 settings.yml 的
#                      读写 ACL，并写 sudoers 只放行 systemctl restart searxng
#   SEARXNG_BACKUP_DIR 平台应用配置前的备份目录，默认 /var/backups/lasso-searxng
set -euo pipefail

SEARXNG_REPO="${SEARXNG_REPO:-https://github.com/searxng/searxng}"
SEARXNG_COMMIT="${SEARXNG_COMMIT:-a7128d5d1d5af2837a68fbeaaee9c3e8391aa9cf}"
SRC_DIR="${SEARXNG_SRC:-/opt/searxng}"
CONF_DIR="${SEARXNG_CONF_DIR:-/etc/searxng}"
CACHE_DIR="${SEARXNG_CACHE_DIR:-/var/cache/searxng}"
RUN_USER="${SEARXNG_USER:-searxng}"
PYTHON_BIN="${SEARXNG_PYTHON:-/opt/cpython-3.11/bin/python3.11}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PORT="${SEARXNG_PORT:-8888}"

CONF_FILE="$CONF_DIR/settings.yml"
VENV="$SRC_DIR/venv"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SRC="$HERE/searxng.service"
UNIT_DST="/etc/systemd/system/searxng.service"
RESET_CONFIG=0

LASSO_USER="${LASSO_USER:-lasso}"
BACKUP_DIR="${SEARXNG_BACKUP_DIR:-/var/backups/lasso-searxng}"
PROXY_BING="${SEARXNG_PROXY_BING:-}"
PROXY_DUCKDUCKGO="${SEARXNG_PROXY_DUCKDUCKGO:-http://192.0.2.10:7890}"
PROXY_WIKIPEDIA="${SEARXNG_PROXY_WIKIPEDIA:-http://192.0.2.10:7890}"
PROXY_GITHUB="${SEARXNG_PROXY_GITHUB:-}"
PROXY_STACKOVERFLOW="${SEARXNG_PROXY_STACKOVERFLOW:-}"

for arg in "$@"; do
  case "$arg" in
    --reset-config) RESET_CONFIG=1 ;;
    -h|--help) awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数：$arg（只支持 --reset-config / --help）" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root 权限：sudo $0" >&2
  exit 1
fi

for cmd in git curl; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "缺少命令：$cmd" >&2; exit 1; }
done
[ -x "$PYTHON_BIN" ] || { echo "解释器不可执行：$PYTHON_BIN（用 SEARXNG_PYTHON 指定）" >&2; exit 1; }
[ -f "$UNIT_SRC" ] || { echo "找不到 systemd 单元模板：$UNIT_SRC" >&2; exit 1; }

# ─── 1. 运行用户 ────────────────────────────────────────────────
getent group "$RUN_USER" >/dev/null || groupadd --system "$RUN_USER"
id -u "$RUN_USER" >/dev/null 2>&1 || \
  useradd --system --gid "$RUN_USER" --home-dir "$SRC_DIR" --no-create-home \
          --shell /sbin/nologin "$RUN_USER"

# ─── 2. 目录 ────────────────────────────────────────────────────
mkdir -p "$SRC_DIR" "$CONF_DIR" "$CACHE_DIR"
chown "$RUN_USER:$RUN_USER" "$CACHE_DIR"
chmod 755 "$CACHE_DIR"

# ─── 3. 源码：固定 commit ───────────────────────────────────────
if [ -d "$SRC_DIR/.git" ]; then
  if [ -n "$(git -C "$SRC_DIR" status --porcelain --untracked-files=no)" ]; then
    echo "$SRC_DIR 有未提交的源码改动，先处理（git -C $SRC_DIR status）再重跑" >&2
    exit 1
  fi
elif [ -n "$(ls -A "$SRC_DIR" 2>/dev/null)" ]; then
  echo "$SRC_DIR 已存在但不是 git 仓库，拒绝覆盖；确认无用后手工清空再重跑" >&2
  exit 1
else
  git -C "$SRC_DIR" init -q
fi

git -C "$SRC_DIR" remote get-url origin >/dev/null 2>&1 || \
  git -C "$SRC_DIR" remote add origin "$SEARXNG_REPO"
git -C "$SRC_DIR" remote set-url origin "$SEARXNG_REPO"

current="$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null || true)"
if [ "$current" != "$SEARXNG_COMMIT" ]; then
  echo "拉取 $SEARXNG_COMMIT ..."
  # GitHub 支持按 commit 浅拉；失败时退回完整拉取再 checkout
  git -C "$SRC_DIR" fetch --quiet --depth 1 origin "$SEARXNG_COMMIT" || {
    echo "按 commit 浅拉失败，改为完整 fetch" >&2
    git -C "$SRC_DIR" fetch --quiet origin
  }
  git -C "$SRC_DIR" checkout --quiet --detach "$SEARXNG_COMMIT"
fi
echo "源码：$(git -C "$SRC_DIR" rev-parse HEAD)"

# ─── 4. venv + 依赖（镜像源） ───────────────────────────────────
if [ ! -x "$VENV/bin/python" ] || \
   ! "$VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)' 2>/dev/null; then
  echo "创建 venv：$VENV（$PYTHON_BIN）"
  "$PYTHON_BIN" -m venv --clear "$VENV"
fi

"$VENV/bin/pip" install --quiet --disable-pip-version-check \
  --index-url "$PIP_INDEX_URL" --upgrade pip
"$VENV/bin/pip" install --quiet --disable-pip-version-check \
  --index-url "$PIP_INDEX_URL" -r "$SRC_DIR/requirements.txt"

# ─── 5. 配置（首次渲染，之后不覆盖） ────────────────────────────
render_settings() { # $1=输出文件 $2=secret_key
  "$PYTHON_BIN" - "$HERE/settings.yml" "$1" "$2" \
    "$PROXY_BING" "$PROXY_DUCKDUCKGO" "$PROXY_WIKIPEDIA" "$PROXY_GITHUB" "$PROXY_STACKOVERFLOW" <<'PY'
import sys

src, dst, secret, *urls = sys.argv[1:]
names = ['bing', 'duckduckgo', 'wikipedia', 'github', 'stackoverflow']
text = open(src, encoding='utf-8').read().replace('__SEARXNG_SECRET_KEY__', secret)
for name, url in zip(names, urls):
    marker = f'# __PROXY_{name}__'
    if url:
        text = text.replace('    ' + marker, f'    proxies:\n      all://:\n        - {url}')
    else:
        text = text.replace('    ' + marker + '\n', '')
open(dst, 'w', encoding='utf-8').write(text)
PY
}

if [ ! -f "$CONF_FILE" ] || [ "$RESET_CONFIG" -eq 1 ]; then
  secret="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(32))')"
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT
  render_settings "$tmp" "$secret"
  install -o "$RUN_USER" -g "$RUN_USER" -m 0600 "$tmp" "$CONF_FILE"
  echo "已写入配置：$CONF_FILE（secret_key 随机生成，权限 600）"
else
  echo "配置已存在，保持不变：$CONF_FILE（需要按模板重置请加 --reset-config）"
fi

# 重启前先让 SearXNG 自己校验一遍配置，避免带着坏配置拉起
if ! SEARXNG_SETTINGS_PATH="$CONF_FILE" PYTHONPATH="$SRC_DIR" \
     SEARXNG_CACHE_DIR="$CACHE_DIR" PYTHONDONTWRITEBYTECODE=1 \
     "$VENV/bin/python" -c 'from searx import settings' >/dev/null 2>&1; then
  echo "配置校验失败：$CONF_FILE 未通过 searx 加载，服务未重启" >&2
  echo "排查：sudo -u $RUN_USER SEARXNG_SETTINGS_PATH=$CONF_FILE PYTHONPATH=$SRC_DIR $VENV/bin/python -c 'from searx import settings'" >&2
  exit 1
fi

# ─── 5b. 平台托管前置：ACL + sudoers（部署步骤，缺一平台改不了配置） ──
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
mkdir -p "$BACKUP_DIR"
if id -u "$LASSO_USER" >/dev/null 2>&1; then
  chown "$LASSO_USER:$LASSO_USER" "$BACKUP_DIR"
  if command -v setfacl >/dev/null 2>&1; then
    setfacl -m "u:$LASSO_USER:rw" "$CONF_FILE"
    echo "已授予 $LASSO_USER 对 $CONF_FILE 的读写 ACL（属主/权限不变）"
  else
    echo "缺少 setfacl（acl 包）：平台将无法改写配置，请安装 acl 后重跑" >&2
  fi
  if [ -d /etc/sudoers.d ] && command -v visudo >/dev/null 2>&1; then
    sudoers_tmp="$(mktemp)"
    printf '%s ALL=(root) NOPASSWD: %s restart searxng\n' "$LASSO_USER" "$SYSTEMCTL" > "$sudoers_tmp"
    if visudo -cf "$sudoers_tmp" >/dev/null 2>&1; then
      install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/lasso-searxng
      echo "已写入 /etc/sudoers.d/lasso-searxng（只放行 restart searxng）"
    else
      echo "sudoers 内容校验失败，未写入；平台应用会报 searxng_restart_failed" >&2
    fi
    rm -f "$sudoers_tmp"
  else
    echo "缺少 /etc/sudoers.d 或 visudo，跳过 sudoers 配置" >&2
  fi
else
  echo "未找到用户 $LASSO_USER，跳过平台托管前置（ACL / sudoers）"
fi

# ─── 6. systemd ────────────────────────────────────────────────
install -o root -g root -m 0644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable searxng >/dev/null 2>&1
systemctl restart searxng

# ─── 7. 健康检查 ───────────────────────────────────────────────
healthy=0
for _ in $(seq 1 20); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done
if [ "$healthy" -ne 1 ]; then
  # 老版本没有 /healthz 时退化为一次 JSON 检索
  if curl -fsS -m 5 "http://127.0.0.1:$PORT/search?q=ping&format=json" >/dev/null 2>&1; then
    healthy=1
  fi
fi
if [ "$healthy" -ne 1 ]; then
  echo "服务已启动但健康检查未通过，最近日志：" >&2
  journalctl -u searxng -n 30 --no-pager >&2 || true
  exit 1
fi

echo "完成：searxng 在 127.0.0.1:$PORT 健康（健康检查通过）"
echo "改配置后只需：sudo systemctl restart searxng"
echo "平台托管的代理配置：登录「系统设置 → 网络代理」保存后自动应用（备份目录 $BACKUP_DIR）"

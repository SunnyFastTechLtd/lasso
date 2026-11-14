#!/usr/bin/env bash
# 安装/检查天工 L3：Playwright + Chromium（Bing 无头浏览器保底）
#
# 这是「部署前置」脚本，不会被应用自动调用：
#   deploy/install_playwright.sh --check        # 只检查，不安装（默认行为）
#   sudo deploy/install_playwright.sh --yes     # 安装 Python 包与 Chromium
#   sudo deploy/install_playwright.sh --yes --with-deps   # 额外安装系统库（dnf/apt）
#
# 依赖安装沿用部署口径：虚拟环境由 uv 创建，通常没有 pip。
# 优先使用 `uv pip install`；找不到 uv 时回退到 venv 自带 pip；
# 两者都不可用时以可读错误退出，不会碰系统 Python。
#
# 体积（参考值，实测以 du 为准）：
#   playwright wheel 约 40 MB；
#   Chromium（headless 构建）下载约 170 MB，落盘约 300~500 MB；
#   运行时单实例常驻内存约 200~400 MB，L3 按单并发串行使用。
#
# 浏览器目录：默认 $LASSO_APP_DIR/.cache/ms-playwright（参考部署为 /srv/lasso），
# 可用 PLAYWRIGHT_BROWSERS_PATH 覆盖。应用侧不会自动发现其他用户的缓存目录，
# 因此服务进程与安装脚本必须使用同一个路径。
#
# 校验方式：
#   1. venv 内 `import playwright` 成功；
#   2. `playwright.chromium.executable_path` 指向的文件真实存在；
#   3. 管理员在天工设置页点「检查浏览器」，或调用
#      POST /api/knowledge/tiangong-search/bing/check/（会真正启动一次 headless 浏览器）。
#   本脚本执行 1、2 两项并以非零退出码报告缺失项。
#
# 缺装时的降级行为（应用侧已实现，不需要脚本兜底）：
#   - L3 开关默认关闭，未打开时完全不触发；
#   - 缺 Playwright 记 bing_unavailable，缺 Chromium 记 bing_browser_missing；
#   - 原因写入天工设置页「Bing 保底」状态区与运行聚合记录，L4 来源库抓取照常执行。
#
# 本轮只提供脚本，不在服务器上执行安装。
set -euo pipefail

ASSUME_YES=0
WITH_DEPS=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    --check) ASSUME_YES=0 ;;
    --with-deps) WITH_DEPS=1 ;;
    -h|--help)
      sed -n '2,37p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "未知参数：$arg（支持 --check / --yes / --with-deps）" >&2
      exit 2
      ;;
  esac
done

APP_DIR="${LASSO_APP_DIR:-/srv/lasso}"
APP_USER="${LASSO_APP_USER:-lasso}"
VENV_PY="$APP_DIR/.venv/bin/python"
REQ_FILE="$(cd "$(dirname "$0")" && pwd)/requirements-playwright.txt"
BROWSERS_DIR="${PLAYWRIGHT_BROWSERS_PATH:-$APP_DIR/.cache/ms-playwright}"
export PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_DIR"

run_as_app() {
  if [ "$(id -u)" -eq 0 ] && id "$APP_USER" >/dev/null 2>&1; then
    runuser -u "$APP_USER" -- env PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_DIR" "$@"
  else
    "$@"
  fi
}

if [ ! -x "$VENV_PY" ]; then
  echo "未找到虚拟环境 Python：$VENV_PY" >&2
  echo "请先按 docs/DEPLOYMENT.md 部署后端，或用 LASSO_APP_DIR 指定应用目录。" >&2
  exit 1
fi

# ─── 现状检查 ───
PKG_OK=0
BROWSER_OK=0
if run_as_app "$VENV_PY" - <<'PY' >/tmp/lasso-playwright-check.txt 2>&1
import os
import sys

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:  # noqa: BLE001
    print(f'import_failed: {type(exc).__name__}: {exc}')
    sys.exit(3)

with sync_playwright() as pw:
    path = pw.chromium.executable_path
print(f'executable: {path}')
print(f'exists: {str(bool(path) and os.path.exists(path)).lower()}')
PY
then
  PKG_OK=1
  if grep -qx 'exists: true' /tmp/lasso-playwright-check.txt; then
    BROWSER_OK=1
  fi
  echo "playwright：已安装"
  sed 's/^/  /' /tmp/lasso-playwright-check.txt
else
  echo "playwright：未安装或不完整"
  sed 's/^/  /' /tmp/lasso-playwright-check.txt
fi

if [ "$PKG_OK" -eq 1 ] && [ "$BROWSER_OK" -eq 1 ]; then
  echo "浏览器目录：$BROWSERS_DIR"
  echo "校验通过：L3 Bing 保底具备运行条件（仍需在天工设置页显式开启）。"
  exit 0
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  echo
  echo "未安装项未做任何改动。执行安装："
  echo "  sudo $0 --yes            # 安装 Python 包 + Chromium"
  echo "  sudo $0 --yes --with-deps # 额外补齐系统依赖（dnf/apt）"
  exit 3
fi

# ─── 安装 Python 包 ───
if [ "$PKG_OK" -ne 1 ]; then
  echo "安装 playwright（$REQ_FILE）…"
  if [ -f "$REQ_FILE" ]; then
    req_args=(-r "$REQ_FILE")
  else
    req_args=("playwright==1.55.0")
  fi
  if command -v uv >/dev/null 2>&1; then
    run_as_app uv pip install --python "$VENV_PY" "${req_args[@]}"
  elif run_as_app "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    pip_args=()
    if [ -n "${PIP_INDEX_URL:-}" ]; then
      pip_args=(--index-url "$PIP_INDEX_URL")
    fi
    run_as_app "$VENV_PY" -m pip install "${pip_args[@]}" "${req_args[@]}"
  else
    echo "未找到 uv，且 venv 没有 pip。" >&2
    echo "请先安装 uv，再执行：uv pip install --python $VENV_PY ${req_args[*]}" >&2
    exit 4
  fi
fi

# ─── 安装 Chromium ───
echo "安装 Chromium 到 $BROWSERS_DIR …"
mkdir -p "$BROWSERS_DIR"
if [ "$(id -u)" -eq 0 ] && id "$APP_USER" >/dev/null 2>&1; then
  chown -R "$APP_USER" "$(dirname "$BROWSERS_DIR")"
fi
if [ "$WITH_DEPS" -eq 1 ]; then
  run_as_app "$VENV_PY" -m playwright install --with-deps chromium
else
  run_as_app "$VENV_PY" -m playwright install chromium
fi

# ─── 复核 ───
if run_as_app "$VENV_PY" - <<'PY'
import os
import sys
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    path = pw.chromium.executable_path
ok = bool(path) and os.path.exists(path)
print(f'executable: {path}')
print('校验通过：Chromium 可执行文件存在。' if ok else '校验失败：Chromium 可执行文件不存在。')
sys.exit(0 if ok else 5)
PY
then
  echo "浏览器目录：$BROWSERS_DIR"
  echo "下一步：管理员在天工设置页打开「Bing 无头浏览器保底」，再点「检查浏览器」。"
  exit 0
else
  echo "安装后校验未通过，请检查 $BROWSERS_DIR 与系统依赖（可重试 --with-deps）。" >&2
  exit 5
fi

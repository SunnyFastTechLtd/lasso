#!/usr/bin/env bash
# 卸载 / 回滚 SearXNG（可重复执行）
#
# 默认（只摘服务，保留数据）：
#   停止并禁用 searxng、移除 /etc/systemd/system/searxng.service、daemon-reload；
#   /opt/searxng、/etc/searxng、/var/cache/searxng 与 searxng 用户全部保留，
#   需要恢复时重跑 install.sh 即可（配置不会被覆盖）。
#
# 彻底卸载（--purge --yes）：
#   在默认动作之外，先把 settings.yml 备份到 /var/backups/searxng-<时间戳>/，
#   再删除上面三个目录与 searxng 用户。必须显式带 --yes。
#
# 用法：
#   sudo deploy/searxng/uninstall.sh
#   sudo deploy/searxng/uninstall.sh --purge --yes
set -euo pipefail

UNIT="/etc/systemd/system/searxng.service"
SRC_DIR="${SEARXNG_SRC:-/opt/searxng}"
CONF_DIR="${SEARXNG_CONF_DIR:-/etc/searxng}"
CACHE_DIR="${SEARXNG_CACHE_DIR:-/var/cache/searxng}"
RUN_USER="${SEARXNG_USER:-searxng}"
PURGE=0
ASSUME_YES=0

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数：$arg（支持 --purge / --yes / --help）" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root 权限：sudo $0" >&2
  exit 1
fi

if [ "$PURGE" -eq 1 ] && [ "$ASSUME_YES" -ne 1 ]; then
  echo "彻底卸载会删除 $SRC_DIR / $CONF_DIR / $CACHE_DIR 与用户 $RUN_USER。" >&2
  echo "确认无误后重跑：sudo $0 --purge --yes" >&2
  exit 1
fi

# ─── 1. 停服务、摘单元 ─────────────────────────────────────────
if systemctl list-unit-files searxng.service >/dev/null 2>&1; then
  systemctl disable --now searxng >/dev/null 2>&1 || true
fi
if [ -f "$UNIT" ]; then
  rm -f "$UNIT"
  systemctl daemon-reload
  systemctl reset-failed searxng >/dev/null 2>&1 || true
  echo "已移除单元：$UNIT（服务已停止并禁用）"
else
  echo "单元不存在，跳过：$UNIT"
fi

if [ "$PURGE" -ne 1 ]; then
  echo "已保留源码/配置/缓存与用户 $RUN_USER；恢复服务重跑 install.sh 即可。"
  exit 0
fi

# ─── 2. 彻底卸载：先备份配置 ───────────────────────────────────
backup_dir="/var/backups/searxng-$(date +%Y%m%d-%H%M%S)"
if [ -f "$CONF_DIR/settings.yml" ]; then
  mkdir -p "$backup_dir"
  cp -a "$CONF_DIR/settings.yml" "$backup_dir/"
  echo "配置已备份：$backup_dir/settings.yml"
fi

rm -rf "$SRC_DIR" "$CONF_DIR" "$CACHE_DIR"
# 平台托管前置（install.sh 写入）：删掉只放行 restart 的 sudoers 规则
rm -f /etc/sudoers.d/lasso-searxng
rm -rf "${SEARXNG_BACKUP_DIR:-/var/backups/lasso-searxng}"
if id -u "$RUN_USER" >/dev/null 2>&1; then
  userdel "$RUN_USER" 2>/dev/null || true
fi
getent group "$RUN_USER" >/dev/null && groupdel "$RUN_USER" 2>/dev/null || true

echo "已彻底卸载（源码、配置、缓存与用户均已删除）"

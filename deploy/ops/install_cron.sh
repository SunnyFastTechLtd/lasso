#!/usr/bin/env bash
# 安装 Lasso 清理任务的 cron 条目（可重复执行，条目不会被重复插入）
#
# 时区口径：本机系统时区为 Asia/Shanghai，下列条目里的时间就是北京时间（东八区）。
# 不使用 CRON_TZ，也不做任何纽约/EST 折算；如需改执行时间，直接改下面的表达式。
#
# 用法：
#   sudo deploy/ops/install_cron.sh
# 可用环境变量覆盖默认值：
#   LASSO_APP_DIR  应用目录，默认 /srv/lasso
#   LASSO_PYTHON   解释器路径，默认 $LASSO_APP_DIR/.venv/bin/python
#   LASSO_CRON_LOG 清理日志，默认 /var/log/lasso-clean.log
#   LASSO_CRON_FILE cron 文件，默认 /etc/cron.d/lasso-clean
set -euo pipefail

APP_DIR="${LASSO_APP_DIR:-/srv/lasso}"
PYTHON_BIN="${LASSO_PYTHON:-$APP_DIR/.venv/bin/python}"
LOG_FILE="${LASSO_CRON_LOG:-/var/log/lasso-clean.log}"
CRON_FILE="${LASSO_CRON_FILE:-/etc/cron.d/lasso-clean}"

if [ "$(id -u)" -ne 0 ]; then
  echo "需要 root 权限写入 $CRON_FILE" >&2
  exit 1
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "解释器不可执行：$PYTHON_BIN（用 LASSO_PYTHON 指定实际路径）" >&2
  exit 1
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

# 整个文件由本脚本生成，重复执行时整篇覆盖，因此不会出现重复条目；
# cron.d 文件必须是 root 所有、0644、且不带扩展名，否则 crond 会忽略。
cat >"$tmp" <<EOF
# 由 deploy/ops/install_cron.sh 生成，请勿手工修改
# 时区：系统时区 Asia/Shanghai，以下时间均为北京时间
# 03:00 clean_chat_files 保留 7 天；03:10 clean_tool_bodies 保留 90 天
SHELL=/bin/bash
0 3 * * * root cd $APP_DIR && $PYTHON_BIN manage.py clean_chat_files >> $LOG_FILE 2>&1
10 3 * * * root cd $APP_DIR && $PYTHON_BIN manage.py clean_tool_bodies >> $LOG_FILE 2>&1
EOF

if [ -f "$CRON_FILE" ] && cmp -s "$tmp" "$CRON_FILE"; then
  echo "已是最新，无变化：$CRON_FILE"
  exit 0
fi

install -o root -g root -m 0644 "$tmp" "$CRON_FILE"
echo "已写入 $CRON_FILE（北京时间 03:00 / 03:10）"

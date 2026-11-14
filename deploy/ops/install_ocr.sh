#!/usr/bin/env bash
# 安装/检查知识库 OCR 前置：tesseract 与中英文语言包
#
# 这是「部署前置」脚本，不会被应用自动调用：
#   deploy/ops/install_ocr.sh --check   # 只检查，不安装（默认行为）
#   sudo deploy/ops/install_ocr.sh --yes  # 真正安装
#
# 体积（CentOS Stream 8 实测）：
#   tesseract（含 leptonica 等依赖）安装后约 28.8 MB，自带 eng.traineddata
#   tesseract-langpack-chi_sim 安装后约 2.5 MB
#   /usr/share/tesseract/tessdata 合计约 25 MB；首次安装下载合计约 17 MB
#
# 校验方式：安装后执行 `tesseract --list-langs`，输出需同时包含 chi_sim 与 eng；
# 本脚本会自动校验并以非零退出码报告缺失项。
#
# CentOS / RHEL 8 的约束（实测）：
#   - AppStream 只有 tesseract 4.1.1，中文语言包 tesseract-langpack-chi_sim 只在 EPEL；
#   - 服务器通常未启用 EPEL，且官方 epel-next 仓库已 404，因此脚本用
#     --repofrompath 临时指向镜像的 EPEL 8（默认阿里云），不写 /etc/yum.repos.d；
#   - EPEL 包需要 Fedora EPEL GPG key，缺失时脚本自动下载并 rpm --import；
#   - tesseract 包自带 eng.traineddata，再装 tesseract-langpack-eng 会因同一路径
#     文件冲突导致 dnf 事务失败，因此只装 chi_sim，eng 由 tesseract 提供；
#   - 安装前会 `dnf clean all`，避免缓存里旧事务的半成品包导致 “Already downloaded”
#     假失败；清理的只是缓存，不改变系统启用的装源。
#
# 缺装时的降级行为（应用侧已实现，不需要脚本兜底）：
#   - 配置了多模态模型时，OCR 优先走模型，tesseract 缺失不影响入库；
#   - 模型不可用且 tesseract 缺失：该页记 failed（reason=tesseract_not_installed
#     或 tesseract_missing_lang），Document.extraction_report.failed_pages 列出页号；
#   - 若整份文档都无文字层且 OCR 全部失败，文档状态为 failed 并写明未解析页，
#     不会出现 status=completed 但零切片的空壳。
#
# 本轮只提供脚本，不在服务器上执行安装。
set -euo pipefail

ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    --check) ASSUME_YES=0 ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "未知参数：$arg（支持 --check / --yes）" >&2
      exit 2
      ;;
  esac
done

LANGS="${KNOWLEDGE_OCR_LANG:-chi_sim+eng}"
# EPEL 语言包来源：默认阿里云镜像，可用环境变量覆盖；
# 只作为本次 dnf 的 --repofrompath，不写入 /etc/yum.repos.d
EPEL_REPO_URL="${EPEL_REPO_URL:-https://mirrors.aliyun.com/epel/8/Everything/x86_64/}"
EPEL_GPG_KEY_URL="${EPEL_GPG_KEY_URL:-https://mirrors.aliyun.com/epel/RPM-GPG-KEY-EPEL-8}"

# ─── 现状检查 ───
if ! command -v tesseract >/dev/null 2>&1; then
  echo "tesseract：未安装"
  INSTALLED=0
else
  echo "tesseract：$(command -v tesseract)（$(tesseract --version 2>/dev/null | head -n1)）"
  INSTALLED=1
fi

if [ "$INSTALLED" -eq 1 ]; then
  present="$(tesseract --list-langs 2>/dev/null || true)"
  missing=()
  IFS='+' read -r -a want <<<"$LANGS"
  for lang in "${want[@]}"; do
    [ -z "$lang" ] && continue
    if ! grep -qx "$lang" <<<"$present"; then
      missing+=("$lang")
    fi
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    echo "语言包：已具备 $LANGS"
    echo "校验通过：OCR 本机回退可用。"
    exit 0
  fi
  echo "语言包：缺少 ${missing[*]}"
else
  missing=()
  IFS='+' read -r -a want <<<"$LANGS"
  for lang in "${want[@]}"; do [ -n "$lang" ] && missing+=("$lang"); done
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  echo
  echo "未安装完整（缺少：tesseract ${missing[*]:-}）。"
  echo "如需安装：sudo $0 --yes"
  echo "CentOS/RHEL 8 注意：chi_sim 只在 EPEL，脚本会用 --repofrompath 临时指向"
  echo "  $EPEL_REPO_URL"
  echo "  并导入 EPEL GPG key；eng 由 tesseract 包自带，不单独安装。"
  echo "在安装前，OCR 会优先走多模态模型；模型不可用时该页按未解析记录。"
  exit 1
fi

# ─── 安装 ───
if [ "$(id -u)" -ne 0 ]; then
  echo "安装需要 root：sudo $0 --yes" >&2
  exit 1
fi

if command -v dnf >/dev/null 2>&1; then
  PKGS=(tesseract)
  NEED_EPEL=0
  for lang in "${missing[@]}"; do
    case "$lang" in
      chi_sim) PKGS+=("tesseract-langpack-chi_sim"); NEED_EPEL=1 ;;
      eng)
        # RHEL/CentOS 的 tesseract 包自带 eng.traineddata；
        # 再装 tesseract-langpack-eng 会因同一路径文件冲突导致 dnf 事务失败
        echo "eng：由 tesseract 包自带，不单独安装（tesseract-langpack-eng 会与它冲突）"
        ;;
      *)       echo "未知语言包 $lang，跳过自动安装（请手工安装对应 tesseract-langpack-$lang）" >&2 ;;
    esac
  done

  DNF_ARGS=(--disablerepo='epel*')
  if [ "$NEED_EPEL" -eq 1 ]; then
    # 语言包只在 EPEL：临时 repofrompath 指向镜像，不落 /etc/yum.repos.d
    if ! rpm -q gpg-pubkey --qf '%{SUMMARY}\n' 2>/dev/null | grep -q 'Fedora EPEL'; then
      key_tmp="$(mktemp)"
      echo "导入 EPEL GPG key：$EPEL_GPG_KEY_URL"
      if curl -fsSL "$EPEL_GPG_KEY_URL" -o "$key_tmp"; then
        rpm --import "$key_tmp"
      else
        echo "EPEL GPG key 下载失败，可用 EPEL_GPG_KEY_URL 指定可访问地址后重跑" >&2
      fi
      rm -f "$key_tmp"
    fi
    DNF_ARGS+=(--repofrompath="epel8,$EPEL_REPO_URL")
    # 缓存里可能残留上一次失败事务的半成品包，清缓存后再装（只清缓存，不改装源）
    dnf clean all >/dev/null 2>&1 || true
  fi

  echo "dnf install -y ${DNF_ARGS[*]} ${PKGS[*]}"
  dnf install -y "${DNF_ARGS[@]}" "${PKGS[@]}"
  SIZE_CMD=(rpm -q --queryformat '%{NAME} %{SIZE}\n' "${PKGS[@]}")
elif command -v apt-get >/dev/null 2>&1; then
  PKGS=(tesseract-ocr)
  for lang in "${missing[@]}"; do
    case "$lang" in
      chi_sim) PKGS+=("tesseract-ocr-chi-sim") ;;
      eng)     PKGS+=("tesseract-ocr-eng") ;;
      *)       echo "未知语言包 $lang，跳过自动安装" >&2 ;;
    esac
  done
  echo "apt-get install -y ${PKGS[*]}"
  apt-get update
  apt-get install -y "${PKGS[@]}"
  SIZE_CMD=(dpkg-query -W -f='${Package} ${Installed-Size}\n' "${PKGS[@]}")
else
  echo "未识别的包管理器（需要 dnf/yum 或 apt-get），请手工安装 tesseract 与 $LANGS 语言包" >&2
  exit 1
fi

echo
echo "安装体积（${SIZE_CMD[*]}）："
"${SIZE_CMD[@]}" 2>/dev/null || true

# ─── 装后复验 ───
present="$(tesseract --list-langs 2>/dev/null || true)"
failed=0
IFS='+' read -r -a want <<<"$LANGS"
for lang in "${want[@]}"; do
  [ -z "$lang" ] && continue
  if grep -qx "$lang" <<<"$present"; then
    echo "✓ $lang"
  else
    echo "✗ $lang 仍缺失（检查语言包是否安装成功）"
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "校验失败：OCR 本机回退将不可用，缺少的页会按未解析记录。" >&2
  exit 1
fi
echo "校验通过：tesseract 本机回退可用（$LANGS）。"

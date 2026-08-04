#!/usr/bin/env bash
set -Eeuo pipefail

# Upgrade the LLMCtl control plane without reinstalling or restarting the
# deployed model workers. Downloads are pinned to one Git commit and every
# installed control-plane file is backed up before replacement.

readonly PROGRAM_NAME="$(basename "$0")"
readonly GITHUB_REPOSITORY="chatop2020/LLMCtl"
readonly GITHUB_BRANCH="main"
readonly GITHUB_API="https://api.github.com/repos/${GITHUB_REPOSITORY}"
readonly GITHUB_WEB="https://github.com/${GITHUB_REPOSITORY}"
readonly CONFIG_DIR="/etc/llm-cluster"
readonly PROXY_ENV="${CONFIG_DIR}/proxy.env"
readonly CLUSTER_ENV="${CONFIG_DIR}/cluster.env"
readonly RELEASE_ENV="/var/lib/llm-cluster/control-plane-version.env"
readonly BACKUP_ROOT="/var/backups/llmctl"
readonly ACCOUNT_SERVICE="llm-account.service"
readonly MANAGED_NGINX_CONFIG="/etc/nginx/conf.d/llm-cluster.conf"
readonly DEFAULT_NO_PROXY="127.0.0.1,localhost,::1"
readonly KEEPWARM_UNIT_SOURCE_DIR="/usr/local/lib/llm-cluster/systemd"
readonly KEEPWARM_SERVICE_UNIT="/etc/systemd/system/llm-keepwarm.service"
readonly KEEPWARM_TIMER_UNIT="/etc/systemd/system/llm-keepwarm.timer"
readonly WORKFLOW_UNIT_SOURCE="/usr/local/lib/llm-cluster/systemd/llm-workflow.service"
readonly WORKFLOW_SERVICE_UNIT="/etc/systemd/system/llm-workflow.service"

LANG_CODE=""
LOCAL_ZIP=""
PROXY_URL=""
PROXY_NO_PROXY="${DEFAULT_NO_PROXY}"
SAVE_PROXY=0
ASSUME_YES=0
NON_INTERACTIVE=0
CHECK_ONLY=0
FORCE=0
WORK_DIR=""
ARCHIVE_PATH=""
ARCHIVE_SHA256=""
SOURCE_ROOT=""
SOURCE_COMMIT=""
BACKUP_DIR=""
ACCOUNT_WAS_ACTIVE=0
WORKFLOW_WAS_ACTIVE=0
DEPLOYMENT_STARTED=0
DEPLOYMENT_COMPLETE=0
UPGRADE_CANCELLED=0

l10n() {
  if [[ "${LANG_CODE}" == "en" ]]; then printf '%s' "$2"; else printf '%s' "$1"; fi
}

log() { printf '[llmctl-upgrade] %s\n' "$*"; }
warn() { printf '[llmctl-upgrade] WARNING: %s\n' "$*" >&2; }
die() { printf '[llmctl-upgrade] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
用法 / Usage:
  sudo llmctl upgrade [选项]
  sudo llmctl upgrade --from-zip /path/to/LLMCtl.zip

选项 / Options:
  --from-zip FILE          使用本地 ZIP，不访问 GitHub
  --proxy URL              本次维护代理，例如 http://192.168.1.20:7890
  --save-proxy             保存代理供后续 llmctl 维护使用
  --force                  即使提交记录相同也重新安装
  --check                  只下载、校验和预检，不安装
  --yes                    确认从 GitHub 获取最新版并执行升级
  --non-interactive        禁止提问；需要与 --yes 及必要参数配合
  --lang zh|en             交互语言
  -h, --help               显示帮助

升级范围：llmctl 命令、LLMCtl Python 工具、账户门户后端与已构建前端。
明确保留：模型权重、Worker 配置与服务、网关配置和数据、密钥、门户数据库，
以及现有 Nginx 的其他站点。升级成功后若检测到 LLMCtl 生成的 Nginx 配置，
会事务式刷新该文件并平滑 reload；不会停止或重启 Router、Docker 或 GPU Worker，
也不会生成域名、80/443、证书或 TLS 配置。
EOF
}

need_value() {
  (( $# >= 2 )) || die "$(l10n "参数 $1 缺少值" "Option $1 requires a value")"
}

parse_args() {
  while (( $# )); do
    case "$1" in
      --from-zip) need_value "$@"; LOCAL_ZIP="$2"; shift 2 ;;
      --proxy) need_value "$@"; PROXY_URL="$2"; shift 2 ;;
      --save-proxy) SAVE_PROXY=1; shift ;;
      --force) FORCE=1; shift ;;
      --check) CHECK_ONLY=1; shift ;;
      --yes) ASSUME_YES=1; shift ;;
      --non-interactive) NON_INTERACTIVE=1; shift ;;
      --lang) need_value "$@"; LANG_CODE="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "$(l10n "未知参数：$1" "Unknown option: $1")" ;;
    esac
  done
}

select_language() {
  if [[ -n "${LANG_CODE}" ]]; then
    [[ "${LANG_CODE}" == "zh" || "${LANG_CODE}" == "en" ]] || die "--lang must be zh or en"
    return
  fi
  if (( NON_INTERACTIVE )) || [[ ! -t 0 ]]; then LANG_CODE="zh"; return; fi
  printf '请选择语言 / Choose language:\n  1) 中文（默认）\n  2) English\n'
  local answer
  read -r -p '选择 / Choice [1]: ' answer
  case "${answer:-1}" in 2) LANG_CODE="en" ;; *) LANG_CODE="zh" ;; esac
}

confirm() {
  local prompt="$1" default_yes="${2:-0}" answer
  if (( ASSUME_YES )); then
    (( default_yes == 1 ))
    return
  fi
  if (( NON_INTERACTIVE )) || [[ ! -t 0 ]]; then
    (( default_yes == 1 ))
    return
  fi
  read -r -p "${prompt}" answer
  if (( default_yes == 1 )); then
    [[ -z "${answer}" || "${answer}" =~ ^[Yy]$ ]]
  else
    [[ "${answer}" =~ ^[Yy]$ ]]
  fi
}

cleanup_work_dir() {
  [[ -z "${WORK_DIR}" || ! -d "${WORK_DIR}" ]] || rm -rf -- "${WORK_DIR}"
}

restore_control_plane() {
  set +e
  warn "$(l10n '升级未通过验收，正在自动恢复旧控制面。' 'Upgrade acceptance failed; restoring the previous control plane automatically.')"
  systemctl stop "${ACCOUNT_SERVICE}" >/dev/null 2>&1 || true
  if [[ -r "${BACKUP_DIR}/manifest.tsv" ]]; then
    while read -r entry_type source destination mode restart; do
      [[ -n "${entry_type}" && "${entry_type}" != \#* ]] || continue
      rm -rf -- "${destination}"
      if [[ -e "${BACKUP_DIR}/files${destination}" ]]; then
        install -d -m 0755 "$(dirname "${destination}")"
        cp -a "${BACKUP_DIR}/files${destination}" "${destination}"
      fi
    done <"${BACKUP_DIR}/manifest.tsv"
  fi
  restore_managed_systemd_units >/dev/null 2>&1 || true
  if [[ -e "${BACKUP_DIR}/control-plane-version.env" ]]; then
    install -d -m 0755 "$(dirname "${RELEASE_ENV}")"
    cp -a "${BACKUP_DIR}/control-plane-version.env" "${RELEASE_ENV}"
  else
    rm -f -- "${RELEASE_ENV}"
  fi
  if (( ACCOUNT_WAS_ACTIVE )); then systemctl start "${ACCOUNT_SERVICE}" >/dev/null 2>&1 || true; fi
  if (( WORKFLOW_WAS_ACTIVE )) && [[ -e "${WORKFLOW_SERVICE_UNIT}" ]]; then
    systemctl start llm-workflow.service >/dev/null 2>&1 || true
  fi
  warn "$(l10n "已从 ${BACKUP_DIR} 恢复；Worker、模型、网关和数据库始终未修改。" "Restored from ${BACKUP_DIR}; workers, models, gateway, and databases were never modified.")"
}

on_exit() {
  local status=$?
  if (( DEPLOYMENT_STARTED == 1 && DEPLOYMENT_COMPLETE == 0 )); then restore_control_plane; fi
  cleanup_work_dir
  unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy || true
  return "${status}"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

validate_proxy_url() {
  local url="$1" port
  [[ "${url}" =~ ^https?://([A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\]):([0-9]{1,5})$ ]] || return 1
  port="${BASH_REMATCH[2]}"
  (( port >= 1 && port <= 65535 ))
}

export_proxy() {
  [[ -n "${PROXY_URL}" ]] || return 0
  export HTTP_PROXY="${PROXY_URL}" HTTPS_PROXY="${PROXY_URL}"
  export http_proxy="${PROXY_URL}" https_proxy="${PROXY_URL}"
  export NO_PROXY="${PROXY_NO_PROXY}" no_proxy="${PROXY_NO_PROXY}"
}

curl_direct() {
  env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy \
    curl --noproxy '*' "$@"
}

curl_maintenance() {
  export_proxy
  curl "$@"
}

probe_direct() {
  curl_direct -fsS --connect-timeout 5 --max-time 12 -o /dev/null \
    -H "User-Agent: LLMCtl-upgrader" "${GITHUB_API}/commits/${GITHUB_BRANCH}" >/dev/null 2>&1 && \
  curl_direct -fsSIL --connect-timeout 5 --max-time 12 -o /dev/null \
    -H "User-Agent: LLMCtl-upgrader" "${GITHUB_WEB}" >/dev/null 2>&1
}

probe_proxy() {
  validate_proxy_url "${PROXY_URL}" || return 1
  curl_maintenance -fsS --connect-timeout 5 --max-time 15 -o /dev/null \
    -H "User-Agent: LLMCtl-upgrader" "${GITHUB_API}/commits/${GITHUB_BRANCH}" >/dev/null 2>&1 && \
  curl_maintenance -fsSIL --connect-timeout 5 --max-time 15 -o /dev/null \
    -H "User-Agent: LLMCtl-upgrader" "${GITHUB_WEB}" >/dev/null 2>&1
}

load_saved_proxy() {
  [[ -r "${PROXY_ENV}" ]] || return 0
  local saved_proxy saved_no_proxy
  saved_proxy=$(awk -F= '$1 == "MAINTENANCE_PROXY" {sub(/^[^=]*=/, ""); print; exit}' "${PROXY_ENV}")
  saved_no_proxy=$(awk -F= '$1 == "MAINTENANCE_NO_PROXY" {sub(/^[^=]*=/, ""); print; exit}' "${PROXY_ENV}")
  if [[ -n "${saved_proxy}" ]] && validate_proxy_url "${saved_proxy}"; then
    PROXY_URL="${saved_proxy}"
    PROXY_NO_PROXY="${saved_no_proxy:-${DEFAULT_NO_PROXY}}"
  fi
}

save_proxy() {
  (( EUID == 0 )) || die "$(l10n '保存系统维护代理需要 root' 'Saving the system maintenance proxy requires root')"
  install -d -m 0700 "${CONFIG_DIR}"
  local temporary="${PROXY_ENV}.new.$$"
  printf 'MAINTENANCE_PROXY=%s\nMAINTENANCE_NO_PROXY=%s\n' \
    "${PROXY_URL}" "${PROXY_NO_PROXY}" >"${temporary}"
  chmod 0600 "${temporary}"
  mv -f "${temporary}" "${PROXY_ENV}"
  log "$(l10n "代理已保存到 ${PROXY_ENV}，仅用于显式维护操作。" "Proxy saved to ${PROXY_ENV} for explicit maintenance operations only.")"
}

prompt_new_proxy() {
  local reason="${1:-direct}" host port scheme prompt
  if [[ "${reason}" == "transfer" ]]; then
    prompt="$(l10n 'GitHub 实际下载失败，是否现在设置代理并重试？[Y/n] ' 'The GitHub transfer failed. Configure a proxy and retry now? [Y/n] ')"
  else
    prompt="$(l10n 'GitHub 无法直连，是否现在设置代理？[Y/n] ' 'GitHub is not directly reachable. Configure a proxy now? [Y/n] ')"
  fi
  confirm "${prompt}" 1 || \
    die "$(l10n '未配置代理，升级尚未下载或修改任何文件。' 'No proxy was configured; the upgrade has not downloaded or modified files.')"
  read -r -p "$(l10n '代理 IP/主机名: ' 'Proxy IP/hostname: ')" host
  read -r -p "$(l10n '代理端口: ' 'Proxy port: ')" port
  read -r -p "$(l10n '协议 [http]: ' 'Scheme [http]: ')" scheme
  scheme="${scheme:-http}"
  [[ "${host}" =~ ^[A-Za-z0-9._-]+$ ]] || die "$(l10n '代理主机名格式无效' 'Invalid proxy hostname')"
  [[ "${port}" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) || die "$(l10n '代理端口无效' 'Invalid proxy port')"
  [[ "${scheme}" == "http" || "${scheme}" == "https" ]] || die "$(l10n '代理协议只能是 http 或 https' 'Proxy scheme must be http or https')"
  PROXY_URL="${scheme}://${host}:${port}"
  probe_proxy || die "$(l10n '代理后的 GitHub 复测失败；没有修改现有部署。' 'The GitHub retest through the proxy failed; the existing deployment was not modified.')"
  log "$(l10n '代理后的 GitHub 网络测试通过。' 'GitHub connectivity through the proxy passed.')"
  if (( SAVE_PROXY )) || confirm "$(l10n '是否保存为以后 llmctl 维护使用？[y/N] ' 'Save for future llmctl maintenance? [y/N] ')" 0; then save_proxy; fi
}

network_preflight() {
  command -v curl >/dev/null 2>&1 || die "$(l10n '缺少 curl，无法从 GitHub 升级' 'curl is required for a GitHub upgrade')"
  if [[ -n "${PROXY_URL}" ]]; then
    validate_proxy_url "${PROXY_URL}" || die "$(l10n '代理格式应为 http://主机:端口 或 https://主机:端口' 'Proxy must be http://HOST:PORT or https://HOST:PORT')"
    probe_proxy || die "$(l10n '指定代理无法访问 GitHub' 'The supplied proxy cannot reach GitHub')"
    log "$(l10n '指定代理的 GitHub 网络测试通过。' 'GitHub connectivity through the supplied proxy passed.')"
    (( SAVE_PROXY == 0 )) || save_proxy
    return
  fi
  log "$(l10n '正在测试 GitHub 直连能力…' 'Testing direct GitHub connectivity…')"
  if probe_direct; then
    log "$(l10n 'GitHub 直连测试通过。' 'Direct GitHub connectivity passed.')"
    return
  fi
  warn "$(l10n 'GitHub 直连失败，正在检查已保存的维护代理。' 'Direct GitHub access failed; checking the saved maintenance proxy.')"
  load_saved_proxy
  if [[ -n "${PROXY_URL}" ]]; then
    if probe_proxy; then
      log "$(l10n "已保存代理可用：${PROXY_URL}" "The saved proxy is usable: ${PROXY_URL}")"
      return
    fi
    warn "$(l10n '已保存代理不可用。' 'The saved proxy is unavailable.')"
    PROXY_URL=""
  fi
  (( NON_INTERACTIVE == 0 )) || die "$(l10n '无人值守升级需要通过 --proxy 提供可用代理' 'A non-interactive upgrade requires a usable --proxy')"
  prompt_new_proxy
}

github_curl() {
  if [[ -n "${PROXY_URL}" ]]; then curl_maintenance "$@"; else curl_direct "$@"; fi
}

recover_transfer_with_proxy() {
  local label="$1" curl_status="$2" failed_proxy="${PROXY_URL}"
  if [[ -n "${failed_proxy}" ]]; then
    warn "$(l10n "${label}通过当前代理失败（curl=${curl_status}）：${failed_proxy}" "${label} failed through the current proxy (curl=${curl_status}): ${failed_proxy}")"
    PROXY_URL=""
  else
    warn "$(l10n "${label}直连失败（curl=${curl_status}），正在检查已保存的维护代理。" "${label} failed directly (curl=${curl_status}); checking the saved maintenance proxy.")"
    load_saved_proxy
    if [[ -n "${PROXY_URL}" ]]; then
      if probe_proxy; then
        log "$(l10n "已保存代理可用，将用它重试下载：${PROXY_URL}" "The saved proxy is usable; retrying the transfer through ${PROXY_URL}")"
        return 0
      fi
      warn "$(l10n '已保存代理无法同时访问 GitHub API 与下载站点。' 'The saved proxy cannot reach both the GitHub API and download host.')"
      PROXY_URL=""
    fi
  fi
  (( NON_INTERACTIVE == 0 )) || \
    die "$(l10n "${label}失败；无人值守升级请通过 --proxy http://主机:端口 提供可用代理。现有部署未修改。" "${label} failed; provide a usable --proxy http://HOST:PORT for a non-interactive upgrade. The existing deployment was not modified.")"
  prompt_new_proxy transfer
}

fetch_github_file() {
  local label="$1" output="$2" first_status retry_status
  shift 2
  rm -f -- "${output}"
  if github_curl "$@" -o "${output}"; then
    return 0
  else
    first_status=$?
  fi
  rm -f -- "${output}"
  recover_transfer_with_proxy "${label}" "${first_status}"
  log "$(l10n "正在通过代理重试${label}…" "Retrying ${label} through the proxy…")"
  if github_curl "$@" -o "${output}"; then
    return 0
  else
    retry_status=$?
  fi
  rm -f -- "${output}"
  die "$(l10n "${label}通过代理重试仍然失败（curl=${retry_status}）；现有控制面、Worker、模型和数据库均未修改。" "${label} still failed through the proxy (curl=${retry_status}); the existing control plane, workers, models, and databases were not modified.")"
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

current_commit() {
  [[ -r "${RELEASE_ENV}" ]] || return 0
  awk -F= '$1 == "LLMCTL_COMMIT" {sub(/^[^=]*=/, ""); print; exit}' "${RELEASE_ENV}"
}

download_github_archive() {
  if (( NON_INTERACTIVE && ASSUME_YES == 0 )); then
    die "$(l10n '无人值守 GitHub 升级必须显式添加 --yes' 'A non-interactive GitHub upgrade requires explicit --yes')"
  fi
  confirm "$(l10n "是否从 GitHub 获取 ${GITHUB_REPOSITORY} 的最新 ${GITHUB_BRANCH} 并升级 LLMCtl 控制面？[Y/n] " "Fetch the latest ${GITHUB_REPOSITORY} ${GITHUB_BRANCH} from GitHub and upgrade the LLMCtl control plane? [Y/n] ")" 1 || {
    log "$(l10n '用户取消升级；没有修改任何文件。' 'Upgrade cancelled; no files were modified.')"
    UPGRADE_CANCELLED=1
    return
  }
  network_preflight
  WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/llmctl-upgrade.XXXXXX")
  local commit_json="${WORK_DIR}/commit.json" archive_url existing
  fetch_github_file "$(l10n 'GitHub 提交信息下载' 'GitHub commit metadata download')" "${commit_json}" \
    -fL --retry 2 --retry-delay 2 --connect-timeout 10 --max-time 60 \
    -H "Accept: application/vnd.github+json" -H "User-Agent: LLMCtl-upgrader" \
    "${GITHUB_API}/commits/${GITHUB_BRANCH}"
  SOURCE_COMMIT=$(python3 - "${commit_json}" <<'PY'
import json, re, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    sha = json.load(handle).get("sha", "")
if not re.fullmatch(r"[0-9a-f]{40}", sha):
    raise SystemExit("GitHub response did not contain a valid commit SHA")
print(sha)
PY
  )
  existing=$(current_commit)
  if [[ "${existing}" == "${SOURCE_COMMIT}" && ${FORCE} -eq 0 ]]; then
    log "$(l10n "当前控制面已经是最新提交 ${SOURCE_COMMIT:0:12}。" "The control plane is already at the latest commit ${SOURCE_COMMIT:0:12}.")"
    UPGRADE_CANCELLED=1
    return
  fi
  ARCHIVE_PATH="${WORK_DIR}/llmctl-${SOURCE_COMMIT}.zip"
  archive_url="https://github.com/${GITHUB_REPOSITORY}/archive/${SOURCE_COMMIT}.zip"
  log "$(l10n "下载并锁定提交 ${SOURCE_COMMIT:0:12}…" "Downloading pinned commit ${SOURCE_COMMIT:0:12}…")"
  fetch_github_file "$(l10n 'GitHub 升级包下载' 'GitHub upgrade archive download')" "${ARCHIVE_PATH}" \
    -fL --retry 3 --retry-delay 2 --connect-timeout 10 --max-time 300 "${archive_url}"
}

use_local_archive() {
  [[ -f "${LOCAL_ZIP}" ]] || die "$(l10n "本地 ZIP 不存在：${LOCAL_ZIP}" "Local ZIP not found: ${LOCAL_ZIP}")"
  WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/llmctl-upgrade.XXXXXX")
  ARCHIVE_PATH="${WORK_DIR}/llmctl-local.zip"
  cp "${LOCAL_ZIP}" "${ARCHIVE_PATH}"
  SOURCE_COMMIT="local-archive"
}

extract_archive_safely() {
  local extract_dir="${WORK_DIR}/source"
  mkdir -p "${extract_dir}"
  ARCHIVE_SHA256=$(sha256_file "${ARCHIVE_PATH}")
  python3 - "${ARCHIVE_PATH}" "${extract_dir}" <<'PY'
import os, re, shutil, stat, sys, zipfile
from pathlib import Path, PurePosixPath

archive = Path(sys.argv[1])
destination = Path(sys.argv[2]).resolve()
try:
    handle = zipfile.ZipFile(archive)
except (OSError, zipfile.BadZipFile) as exc:
    raise SystemExit(f"invalid ZIP: {exc}")
infos = handle.infolist()
if not infos or len(infos) > 20000:
    raise SystemExit("ZIP is empty or contains too many entries")
if sum(item.file_size for item in infos) > 512 * 1024 * 1024:
    raise SystemExit("ZIP uncompressed size exceeds 512 MiB")
for item in infos:
    name = item.filename.replace("\\", "/")
    path = PurePosixPath(name)
    mode = (item.external_attr >> 16) & 0o170000
    if item.flag_bits & 0x1:
        raise SystemExit(f"encrypted ZIP entry is not allowed: {name}")
    if path.is_absolute() or re.match(r"^[A-Za-z]:", name) or ".." in path.parts:
        raise SystemExit(f"unsafe ZIP path: {name}")
    if mode == stat.S_IFLNK:
        raise SystemExit(f"symbolic links are not allowed: {name}")
    target = destination.joinpath(*path.parts).resolve()
    if os.path.commonpath((destination, target)) != str(destination):
        raise SystemExit(f"ZIP path escapes extraction directory: {name}")
    if item.is_dir():
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        with handle.open(item) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
        # The hardened extractor deliberately avoids extractall(), but still has
        # to preserve the executable bit carried by GitHub archives.  Ignore
        # ownership/special bits and apply only rwx permission bits.
        permissions = (item.external_attr >> 16) & 0o777
        os.chmod(target, permissions or 0o644)
handle.close()
PY
  local -a manifests=()
  local manifest
  while IFS= read -r manifest; do manifests+=("${manifest}"); done < <(find "${extract_dir}" -type f -name upgrade-manifest.tsv -print)
  (( ${#manifests[@]} == 1 )) || die "$(l10n '升级包必须且只能包含一份 upgrade-manifest.tsv' 'The upgrade archive must contain exactly one upgrade-manifest.tsv')"
  SOURCE_ROOT=$(dirname "${manifests[0]}")
}

validate_manifest() {
  local manifest="${SOURCE_ROOT}/upgrade-manifest.tsv" count=0
  local entry_type source destination mode restart source_path
  while read -r entry_type source destination mode restart; do
    [[ -n "${entry_type}" && "${entry_type}" != \#* ]] || continue
    ((count += 1))
    [[ "${entry_type}" == "file" || "${entry_type}" == "dir" ]] || die "$(l10n "升级清单类型无效：${entry_type}" "Invalid manifest type: ${entry_type}")"
    [[ "${source}" != /* && "${source}" != *..* ]] || die "$(l10n "升级清单源路径不安全：${source}" "Unsafe manifest source path: ${source}")"
    case "${destination}" in
      /usr/local/sbin/llmctl|/usr/local/lib/llm-cluster/*) ;;
      *) die "$(l10n "升级清单目标不在 LLMCtl 控制面范围：${destination}" "Manifest destination is outside the LLMCtl control-plane scope: ${destination}")" ;;
    esac
    [[ "${mode}" =~ ^0[0-7]{3}$ ]] || die "$(l10n "升级清单权限无效：${mode}" "Invalid manifest mode: ${mode}")"
    [[ "${restart}" == "none" || "${restart}" == "account" ]] || die "$(l10n "升级清单重启范围无效：${restart}" "Invalid manifest restart scope: ${restart}")"
    source_path="${SOURCE_ROOT}/${source}"
    if [[ "${entry_type}" == "file" ]]; then [[ -f "${source_path}" ]] || die "$(l10n "升级包缺少文件：${source}" "Upgrade archive is missing file: ${source}")"
    else [[ -d "${source_path}" ]] || die "$(l10n "升级包缺少目录：${source}" "Upgrade archive is missing directory: ${source}")"; fi
  done <"${manifest}"
  (( count >= 7 )) || die "$(l10n '升级清单不完整' 'The upgrade manifest is incomplete')"
  grep -Eq '^file[[:space:]]+llmctl\.sh[[:space:]]+/usr/local/sbin/llmctl' "${manifest}" || die "$(l10n '升级清单缺少 llmctl 命令' 'The manifest does not include the llmctl command')"
}

validate_source() {
  validate_manifest
  bash -n "${SOURCE_ROOT}/llmctl.sh" "${SOURCE_ROOT}/upgrade-llmctl.sh"
  grep -q '^ExecStart=/usr/local/sbin/llmctl _keepwarm-tick$' "${SOURCE_ROOT}/systemd/llm-keepwarm.service" || die "$(l10n '保活 service 单元无效' 'Invalid keep-warm service unit')"
  grep -q '^Unit=llm-keepwarm.service$' "${SOURCE_ROOT}/systemd/llm-keepwarm.timer" || die "$(l10n '保活 timer 单元无效' 'Invalid keep-warm timer unit')"
  python3 -m py_compile \
    "${SOURCE_ROOT}/lib/model_catalog.py" \
    "${SOURCE_ROOT}/lib/runtime_optimizer.py" \
    "${SOURCE_ROOT}/lib/gateway_config.py" \
    "${SOURCE_ROOT}/lib/account_portal.py" \
    "${SOURCE_ROOT}/lib/llm_benchmark.py" \
    "${SOURCE_ROOT}/lib/workflow_config.py"
  grep -q '^ExecStart=/usr/local/lib/llm-cluster/workflowd/llm-workflowd ' "${SOURCE_ROOT}/systemd/llm-workflow.service" || die "$(l10n '工作流 service 单元无效' 'Invalid workflow service unit')"
  [[ -x "${SOURCE_ROOT}/lib/workflowd/llm-workflowd" && -x "${SOURCE_ROOT}/lib/workflowd/llm-workflowd-linux-amd64" && -x "${SOURCE_ROOT}/lib/workflowd/llm-workflowd-linux-arm64" ]] || die "$(l10n '工作流运行时不完整' 'The workflow runtime is incomplete')"
  python3 - "${SOURCE_ROOT}/lib/account_portal_ui" <<'PY'
import re, sys
from pathlib import Path
root = Path(sys.argv[1])
html = (root / "index.html").read_text(encoding="utf-8")
assets = re.findall(r'(?:src|href)=["\'](?:/ui/)?(assets/[^"\']+)', html)
if not assets:
    raise SystemExit("portal index does not reference built assets")
missing = [name for name in assets if not (root / name).is_file()]
if missing:
    raise SystemExit("portal index references missing assets: " + ", ".join(missing))
PY
  log "$(l10n "升级包验证通过；SHA256=${ARCHIVE_SHA256}" "Upgrade archive validation passed; SHA256=${ARCHIVE_SHA256}")"
}

read_cluster_value() {
  local key="$1" default_value="$2" value=""
  if [[ -r "${CLUSTER_ENV}" ]]; then value=$(awk -F= -v key="${key}" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "${CLUSTER_ENV}"); fi
  printf '%s\n' "${value:-${default_value}}"
}

apply_keepwarm_timer_state() {
  local enabled
  systemctl daemon-reload
  enabled=$(read_cluster_value KEEPWARM_ENABLED 0)
  if [[ "${enabled}" == 1 && -r "${KEEPWARM_SERVICE_UNIT}" && -r "${KEEPWARM_TIMER_UNIT}" ]]; then
    install -d -m 0700 /var/lib/llm-cluster/keepwarm
    systemctl enable --now llm-keepwarm.timer
  else
    systemctl disable --now llm-keepwarm.timer >/dev/null 2>&1 || true
  fi
}

configure_keepwarm_timer() {
  local service_source="${KEEPWARM_UNIT_SOURCE_DIR}/llm-keepwarm.service"
  local timer_source="${KEEPWARM_UNIT_SOURCE_DIR}/llm-keepwarm.timer"
  [[ -r "${service_source}" && -r "${timer_source}" ]] || \
    die "$(l10n '升级后的控制面缺少 Worker 保活单元' 'The upgraded control plane is missing Worker keep-warm units')"
  install -m 0644 "${service_source}" "${KEEPWARM_SERVICE_UNIT}"
  install -m 0644 "${timer_source}" "${KEEPWARM_TIMER_UNIT}"
  apply_keepwarm_timer_state
}

restore_managed_systemd_units() {
  local unit
  rm -f -- "${KEEPWARM_SERVICE_UNIT}" "${KEEPWARM_TIMER_UNIT}" "${WORKFLOW_SERVICE_UNIT}"
  for unit in llm-keepwarm.service llm-keepwarm.timer llm-workflow.service; do
    if [[ -e "${BACKUP_DIR}/systemd/${unit}" ]]; then
      cp -a "${BACKUP_DIR}/systemd/${unit}" "/etc/systemd/system/${unit}"
    fi
  done
  systemctl daemon-reload
  apply_keepwarm_timer_state
}

# Keep the historical function name as a compatibility seam for deployments,
# tests and operator tooling written before the optional workflow unit existed.
restore_keepwarm_systemd_units() {
  restore_managed_systemd_units
}

refresh_workflow_unit_if_installed() {
  [[ -e "${WORKFLOW_SERVICE_UNIT}" ]] || return 0
  [[ -r "${WORKFLOW_UNIT_SOURCE}" ]] || die "$(l10n '已启用工作流，但升级包缺少 service 模板' 'The workflow is installed but its upgraded service template is missing')"
  local was_active=0
  systemctl is-active --quiet llm-workflow.service && was_active=1 || true
  install -m 0644 "${WORKFLOW_UNIT_SOURCE}" "${WORKFLOW_SERVICE_UNIT}"
  systemctl daemon-reload
  if (( was_active )); then
    systemctl restart llm-workflow.service
    systemctl is-active --quiet llm-workflow.service || die "$(l10n '升级后的工作流服务未能恢复运行' 'The workflow service failed to return after upgrade')"
  fi
}

wait_for_account_portal() {
  local port="$1" elapsed=0
  while (( elapsed < 30 )); do
    if systemctl is-active --quiet "${ACCOUNT_SERVICE}" && \
       curl --noproxy '*' -fsS --max-time 3 "http://127.0.0.1:${port}/health" >/dev/null 2>&1 && \
       curl --noproxy '*' -fsS --max-time 3 "http://127.0.0.1:${port}/ui/" 2>/dev/null | grep -q LLMCtl; then return 0; fi
    sleep 2
    elapsed=$((elapsed + 2))
    log "$(l10n "等待账户门户就绪：${elapsed}s/30s" "Waiting for the account portal: ${elapsed}s/30s")"
  done
  return 1
}

refresh_managed_nginx() {
  [[ -f "${MANAGED_NGINX_CONFIG}" ]] || return 0
  if ! grep -q '^# Generated by LLMCtl ' "${MANAGED_NGINX_CONFIG}"; then
    log "$(l10n '检测到同名 Nginx 配置但并非 LLMCtl 生成，保持不变。' 'The same-name Nginx configuration is not LLMCtl-generated and was left unchanged.')"
    return 0
  fi
  log "$(l10n '正在事务式刷新 LLMCtl Nginx 入口；域名、80/443、证书和 TLS 仍由外部出口管理。' 'Transactionally refreshing the LLMCtl Nginx front door; the external edge still owns domains, ports 80/443, certificates, and TLS.')"
  if /usr/local/sbin/llmctl nginx apply; then
    log "$(l10n 'LLMCtl Nginx 入口已刷新并平滑 reload。' 'The LLMCtl Nginx front door was refreshed with a graceful reload.')"
  else
    warn "$(l10n 'Nginx 刷新失败；llmctl 已恢复修改前配置。控制面升级仍已完成，请运行 llmctl nginx apply 查看详细错误。' 'Nginx refresh failed and llmctl restored the previous configuration. The control-plane upgrade remains complete; run llmctl nginx apply for details.')"
  fi
  return 0
}

backup_control_plane() {
  local timestamp destination entry_type source mode restart backup_path unit
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  BACKUP_DIR="${BACKUP_ROOT}/control-plane-${timestamp}"
  install -d -m 0700 "${BACKUP_DIR}/files"
  cp "${SOURCE_ROOT}/upgrade-manifest.tsv" "${BACKUP_DIR}/manifest.tsv"
  while read -r entry_type source destination mode restart; do
    [[ -n "${entry_type}" && "${entry_type}" != \#* ]] || continue
    backup_path="${BACKUP_DIR}/files${destination}"
    if [[ -e "${destination}" ]]; then
      install -d -m 0700 "$(dirname "${backup_path}")"
      cp -a "${destination}" "${backup_path}"
    fi
  done <"${SOURCE_ROOT}/upgrade-manifest.tsv"
  install -d -m 0700 "${BACKUP_DIR}/systemd"
  for unit in llm-keepwarm.service llm-keepwarm.timer llm-workflow.service; do
    [[ ! -e "/etc/systemd/system/${unit}" ]] || cp -a "/etc/systemd/system/${unit}" "${BACKUP_DIR}/systemd/${unit}"
  done
  [[ ! -e "${RELEASE_ENV}" ]] || cp -a "${RELEASE_ENV}" "${BACKUP_DIR}/control-plane-version.env"
  printf 'created_at=%s\nsource_commit=%s\narchive_sha256=%s\n' \
    "${timestamp}" "${SOURCE_COMMIT}" "${ARCHIVE_SHA256}" >"${BACKUP_DIR}/upgrade.txt"
  chmod 0600 "${BACKUP_DIR}/upgrade.txt"
  log "$(l10n "旧控制面已备份到 ${BACKUP_DIR}" "The previous control plane was backed up to ${BACKUP_DIR}")"
}

install_control_plane() {
  (( EUID == 0 )) || die "$(l10n '安装升级需要 root；可用 --check 仅执行预检' 'Installing an upgrade requires root; use --check for validation only')"
  command -v systemctl >/dev/null 2>&1 || die "systemctl unavailable"
  [[ -r "${CLUSTER_ENV}" ]] || die "$(l10n '未检测到现有 LLMCtl 部署' 'No existing LLMCtl deployment was detected')"
  backup_control_plane
  local router_before router_after workers_before workers_after account_port
  local entry_type source destination mode restart source_path restart_account=0
  router_before=$(systemctl is-active llm-router.service 2>/dev/null || true)
  workers_before=$(systemctl list-units 'llm-worker@*.service' --state=running --no-legend 2>/dev/null | wc -l | tr -d ' ')
  systemctl is-active --quiet "${ACCOUNT_SERVICE}" && ACCOUNT_WAS_ACTIVE=1 || ACCOUNT_WAS_ACTIVE=0
  systemctl cat "${ACCOUNT_SERVICE}" >/dev/null 2>&1 || ACCOUNT_WAS_ACTIVE=0
  systemctl is-active --quiet llm-workflow.service && WORKFLOW_WAS_ACTIVE=1 || WORKFLOW_WAS_ACTIVE=0
  systemctl cat llm-workflow.service >/dev/null 2>&1 || WORKFLOW_WAS_ACTIVE=0

  DEPLOYMENT_STARTED=1
  if (( ACCOUNT_WAS_ACTIVE )); then
    log "$(l10n '仅短暂停止账户门户；Router、Nginx、Docker 和 GPU Worker 保持运行。' 'Stopping only the account portal briefly; Router, Nginx, Docker, and GPU workers remain running.')"
    systemctl stop "${ACCOUNT_SERVICE}"
  fi
  while read -r entry_type source destination mode restart; do
    [[ -n "${entry_type}" && "${entry_type}" != \#* ]] || continue
    source_path="${SOURCE_ROOT}/${source}"
    install -d -m 0755 "$(dirname "${destination}")"
    rm -rf -- "${destination}"
    if [[ "${entry_type}" == "file" ]]; then install -m "${mode}" "${source_path}" "${destination}"
    else cp -a "${source_path}" "${destination}"; chmod "${mode}" "${destination}"; fi
    [[ "${restart}" != "account" ]] || restart_account=1
  done <"${SOURCE_ROOT}/upgrade-manifest.tsv"

  /usr/local/sbin/llmctl version >/dev/null
  configure_keepwarm_timer
  refresh_workflow_unit_if_installed
  if (( ACCOUNT_WAS_ACTIVE && restart_account )); then
    systemctl start "${ACCOUNT_SERVICE}"
    account_port=$(read_cluster_value ACCOUNT_PORT 8001)
    if ! wait_for_account_portal "${account_port}"; then
      journalctl -u "${ACCOUNT_SERVICE}" -n 50 --no-pager >&2 || true
      die "$(l10n '新账户门户未通过健康与静态资源验收' 'The new account portal failed health and static-asset acceptance')"
    fi
  fi

  install -d -m 0755 "$(dirname "${RELEASE_ENV}")"
  local release_temp="${RELEASE_ENV}.new.$$" upgraded_at
  upgraded_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  printf 'LLMCTL_COMMIT=%s\nLLMCTL_ARCHIVE_SHA256=%s\nLLMCTL_UPGRADED_AT=%s\n' \
    "${SOURCE_COMMIT}" "${ARCHIVE_SHA256}" "${upgraded_at}" >"${release_temp}"
  chmod 0600 "${release_temp}"
  mv -f "${release_temp}" "${RELEASE_ENV}"
  DEPLOYMENT_COMPLETE=1

  refresh_managed_nginx

  router_after=$(systemctl is-active llm-router.service 2>/dev/null || true)
  workers_after=$(systemctl list-units 'llm-worker@*.service' --state=running --no-legend 2>/dev/null | wc -l | tr -d ' ')
  [[ "${router_before}" == "${router_after}" ]] || warn "$(l10n "Router 状态由 ${router_before} 变为 ${router_after}；升级器未操作它，请检查。" "Router state changed from ${router_before} to ${router_after}; the upgrader did not operate it.")"
  [[ "${workers_before}" == "${workers_after}" ]] || warn "$(l10n "运行 Worker 数由 ${workers_before} 变为 ${workers_after}；升级器未操作 Worker，请检查。" "Running worker count changed from ${workers_before} to ${workers_after}; the upgrader did not operate workers.")"
  log "$(l10n "LLMCtl 控制面升级完成：${SOURCE_COMMIT:0:12}" "LLMCtl control-plane upgrade completed: ${SOURCE_COMMIT:0:12}")"
  log "$(l10n "备份目录：${BACKUP_DIR}" "Backup directory: ${BACKUP_DIR}")"
  log "$(l10n "Router=${router_after}，运行 Worker=${workers_after}；两者均未重启。" "Router=${router_after}, running workers=${workers_after}; neither was restarted.")"
}

main() {
  parse_args "$@"
  select_language
  command -v python3 >/dev/null 2>&1 || die "python3 unavailable"
  if [[ -n "${LOCAL_ZIP}" ]]; then use_local_archive; else download_github_archive; fi
  (( UPGRADE_CANCELLED == 0 )) || return 0
  extract_archive_safely
  validate_source
  if (( CHECK_ONLY )); then
    log "$(l10n '预检完成：没有修改现有控制面或服务。' 'Preflight complete: no existing control-plane files or services were modified.')"
    return 0
  fi
  install_control_plane
}

main "$@"

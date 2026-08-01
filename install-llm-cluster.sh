#!/usr/bin/env bash
# Hardware-aware vLLM cluster deployment for Ubuntu 24.04 and NVIDIA GPUs.
# Runtime is deliberately offline: temporary proxy settings are removed before
# systemd starts any inference container. Images and model revisions are pinned.

set -Eeuo pipefail
IFS=$'\n\t'

readonly INSTALLER_VERSION="2.0.2"
readonly CONFIG_DIR="/etc/llm-cluster"
readonly LEGACY_CONFIG_DIR="/etc/ornith"
readonly STATE_DIR="/var/lib/llm-cluster"
readonly RETAINED_SECRETS="${STATE_DIR}/retained-secrets.env"
readonly WORKER_ENV_DIR="${CONFIG_DIR}/workers"
readonly CLUSTER_ENV="${CONFIG_DIR}/cluster.env"
readonly SECRETS_ENV="${CONFIG_DIR}/secrets.env"
readonly PROXY_ENV="${CONFIG_DIR}/proxy.env"
readonly INSTALL_PROXY_DROPIN="/etc/systemd/system/docker.service.d/90-llm-cluster-install-proxy.conf"
readonly VALIDATED_MODEL_ID="protoLabsAI/Ornith-1.0-35B-FP8"
readonly VALIDATED_MODEL_REVISION="eeecbda6ce16f5525bc0f19e253fd9933e1cccc1"
readonly OFFICIAL_MODEL_ID="deepreinforce-ai/Ornith-1.0-35B-FP8"
readonly OFFICIAL_MODEL_REVISION="1ab57ce0b44950e498a88756f40ad1ed4d0f30ca"

MODEL_SOURCE="catalog"
MODEL_HUB="huggingface"
MODEL_ID="${VALIDATED_MODEL_ID}"
MODEL_REVISION="${VALIDATED_MODEL_REVISION}"
MODEL_ROOT="/data/llm-cluster/models"
SERVED_MODEL_NAME="ornith-1.0-35b-fp8"
MODEL_ARCHITECTURE="Qwen3_5MoeForConditionalGeneration"
MODEL_TASK="vision"
MODEL_PRECISION="fp8"
MODEL_WEIGHT_BYTES=37616436048
MODEL_PARAMS=35109172336
MODEL_NATIVE_CONTEXT=262144
TOOL_CALL_PARSER="qwen3_xml"
REASONING_PARSER="qwen3"
SUPPORTS_IMAGE_INPUT=1
SUPPORTS_OCR=1
SUPPORTS_TOOL_CALLING=1
SUPPORTS_REASONING=1
SUPPORTS_THINKING_TOGGLE=1
TRUST_REMOTE_CODE=1
VLLM_IMAGE="vllm/vllm-openai:v0.22.1"
LITELLM_IMAGE="ghcr.io/berriai/litellm:v1.94.0"
POSTGRES_IMAGE="postgres:16-alpine"
TP_SIZE=1
PHYSICAL_GPU_COUNT=0
INSTANCE_COUNT=0
ACTIVE_INSTANCE_COUNT=0
STARTUP_PARALLELISM=0
MAX_MODEL_LEN=0
MAX_NUM_SEQS=7
ESTIMATED_MAX_NUM_SEQS=7
MAX_NUM_BATCHED_TOKENS=8192
GPU_MEMORY_UTILIZATION="0.92"
MM_LIMIT='{"image":8,"video":0}'
ROUTING_STRATEGY="least-busy"
WORKER_BASE_PORT=8100
API_BIND="0.0.0.0"
API_PORT=8000
LITELLM_DB_PORT=15432
UI_USERNAME="admin"
UI_PASSWORD="llm-admin"
START_TIMEOUT=1800
PROXY_URL=""
PROXY_NO_PROXY="127.0.0.1,localhost,::1"
SAVE_PROXY=0
ASSUME_YES=0
NON_INTERACTIVE=0
NO_START=0
SKIP_DOWNLOAD=0
SKIP_PACKAGES=0
FORCE_RECONFIGURE=0
INSTALL_PROXY_APPLIED=0
MODEL_SELECTION_EXPLICIT=0
MODEL_PLAN_APPLIED=0
MODEL_ID_EXPLICIT=0
MODEL_REVISION_EXPLICIT=0
MODEL_HUB_EXPLICIT=0
TP_EXPLICIT=0
SEQS_EXPLICIT=0
MAX_LEN_EXPLICIT=0
MODEL_ROOT_EXPLICIT=0
ACTIVE_COUNT_EXPLICIT=0
STARTUP_PARALLELISM_EXPLICIT=0
UI_USERNAME_EXPLICIT=0
UI_PASSWORD_EXPLICIT=0
INTERFACE_LANGUAGE="zh"
INTERFACE_LANGUAGE_EXPLICIT=0
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
MANAGER_SOURCE="${SCRIPT_DIR}/llmctl.sh"
CATALOG_SOURCE="${SCRIPT_DIR}/lib/model_catalog.py"
CATALOG_QUERY=""
CATALOG_TASK="auto"
CATALOG_LIMIT=10
CATALOG_RESULTS=""
HOST_PROFILE_JSON=""
UNEXPECTED_ERROR_REPORTED=0

log()  { printf '[install-llm] %s\n' "$*"; }
warn() { printf '[install-llm] WARNING: %s\n' "$*" >&2; }
l10n() {
  if [[ "${INTERFACE_LANGUAGE}" == en ]]; then printf '%s' "${2:-}"; else printf '%s' "${1:-}"; fi
}
die()  {
  UNEXPECTED_ERROR_REPORTED=1
  printf '[install-llm] ERROR: %s\n' "$*" >&2
  exit 1
}

report_unexpected_error() {
  local status="${1:-1}" line="${2:-unknown}"
  (( UNEXPECTED_ERROR_REPORTED == 0 )) || return 0
  UNEXPECTED_ERROR_REPORTED=1
  printf '[install-llm] ERROR: %s\n' "$(l10n \
    "安装器在第 ${line} 行意外失败（退出码 ${status}）；已停止，未继续执行后续安装步骤。" \
    "Installer failed unexpectedly at line ${line} (exit ${status}); it stopped before running later installation steps.")" >&2
}

trap 'report_unexpected_error "$?" "${LINENO}"' ERR

usage() {
  if [[ "${INTERFACE_LANGUAGE}" == en ]]; then
    cat <<'EOF'
Universal vLLM cluster installer

Interactive (recommended):
  sudo bash install-llm-cluster.sh

Common unattended options:
  --lang zh|en                   Installer/catalog language; default zh
  --yes                          Accept recommended defaults; skip the selection wizard
  --non-interactive              Never prompt; fail when required information is absent
  --model-source catalog|validated|official|huggingface|modelscope
  --model-id OWNER/REPO          Select a Hub model directly
  --model-revision REV           HF pins the current SHA; ModelScope defaults to master
  --catalog-query TEXT           Catalog search term for unattended use
  --catalog-task auto|text|vision
  --model-root DIR               Model directory; default /data/llm-cluster/models
  --tp-size 1|2|4|8              Planned from model size and local GPUs by default
  --max-num-seqs N               Estimated from KV-cache capacity by default
  --active-instances N           Activate the first N replicas after install and at boot
  --startup-parallelism N        Concurrent worker loads; defaults to the host plan
  --max-model-len N              Planned from native context and VRAM by default
  --gpu-memory-utilization 0.70-0.96
  --max-num-batched-tokens N     Default 8192
  --api-bind IP                  Default 0.0.0.0
  --api-port PORT                Default 8000
  --worker-base-port PORT        Default 8100
  --ui-username USER             LiteLLM Web admin; default admin
  --ui-password PASSWORD         Initial password; default llm-admin
  --database-port PORT           Local PostgreSQL port; default 15432
  --proxy URL                    Used only during installation/downloads
  --save-proxy                   Save for future llmctl download/update operations
  --no-start                     Install and register services without starting them
  --skip-download                Reuse an exact, verified MODEL_ROOT/current model
  --skip-packages                Do not install Docker/NVIDIA Container Toolkit
  --force-reconfigure            Permit replacing existing LLM cluster configuration
  --vllm-image IMAGE             Override the pinned vLLM image
  --litellm-image IMAGE          Override the pinned LiteLLM image
  --postgres-image IMAGE         Override the PostgreSQL 16 image

Example:
  sudo bash install-llm-cluster.sh --lang en --yes --model-source modelscope \
    --model-id Qwen/Qwen3-8B --tp-size 1 --max-num-seqs 7 \
    --proxy http://192.168.1.20:7890

Notes:
  - Before recommendations, the wizard performs a read-only OS, CPU, memory,
    NVIDIA driver/GPU, PCIe/topology, NUMA, and disk preflight.
  - Catalog results must pass task, complete-weight, non-platform-specific
    format, vLLM architecture, GPU memory, and minimum-context gates.
  - Existing weights are reused only after identity, revision, architecture,
    completeness, and size checks; matching files are not copied.
  - The installer does not install Conda or change the NVIDIA driver.
EOF
    return
  fi
  cat <<'EOF'
通用 vLLM 集群自动安装器

交互运行（推荐）：
  sudo bash install-llm-cluster.sh

常用无人值守参数：
  --lang zh|en                    安装器/模型目录语言，默认 zh
  --yes                           接受推荐默认值，不显示选择向导
  --non-interactive               禁止提问；缺少代理等信息时直接失败
  --model-source catalog|validated|official|huggingface|modelscope
  --model-id OWNER/REPO           直接指定 Hub 模型
  --model-revision REV            HF 默认固定为当前 SHA；ModelScope 默认 master
  --catalog-query TEXT            无人值守目录搜索关键字
  --catalog-task auto|text|vision 搜索用途
  --model-root DIR                模型下载目录，默认 /data/llm-cluster/models
  --tp-size 1|2|4|8               默认由模型大小和本机 GPU 自动规划
  --max-num-seqs N                默认由 KV Cache 估算
  --active-instances N            安装后及开机激活前 N 个实例
  --startup-parallelism N         每批并行启动 Worker 数，默认使用本机规划值
  --max-model-len N               默认按模型原生长度与显存规划
  --gpu-memory-utilization 0.70-0.96
  --max-num-batched-tokens N       默认 8192
  --api-bind IP                   默认 0.0.0.0
  --api-port PORT                 默认 8000
  --worker-base-port PORT         默认 8100
  --ui-username USER              LiteLLM Web 管理员，默认 admin
  --ui-password PASSWORD          LiteLLM Web 初始密码，默认 llm-admin
  --database-port PORT            PostgreSQL 本机监听端口，默认 15432
  --proxy URL                     仅安装/下载阶段使用的代理
  --save-proxy                    保存供以后 llmctl download/update 使用
  --no-start                      安装并注册服务，但不立即启动
  --skip-download                 已有 MODEL_ROOT/current 时跳过下载
  --skip-packages                 不安装 Docker/NVIDIA Container Toolkit
  --force-reconfigure             允许覆盖已有 LLM 集群配置/服务
  --vllm-image IMAGE              覆盖锁定的 vLLM 镜像
  --litellm-image IMAGE           覆盖锁定的 LiteLLM 镜像
  --postgres-image IMAGE          覆盖 PostgreSQL 16 镜像

示例：
  sudo bash install-llm-cluster.sh --yes --model-source modelscope \
    --model-id Qwen/Qwen3-8B --tp-size 1 --max-num-seqs 7 \
    --proxy http://192.168.1.20:7890

说明：
  - 推荐前会只读检测操作系统、CPU、内存、NVIDIA 驱动/GPU、PCIe/拓扑、
    NUMA 和磁盘，并用同一份快照生成部署计划。
  - 目录只列出通过任务、完整权重、非平台专用格式、vLLM 架构、显存和
    最低上下文门禁的模型；下载后还会用固定镜像二次校验。
  - 本地权重只有在身份、revision、架构、完整性和体积核验通过后才复用；
    匹配时不复制权重。
  - validated/official 保留原 Ornith 配置档，便于兼容现有使用方式。
  - 不安装 Conda，不改 NVIDIA 驱动；推理依赖封装在固定 Docker 镜像中。
  - Web UI 默认位于 http://服务器IP:8000/ui，首次登录后请立即修改通用密码。
EOF
}

need_value() { (($# >= 2)) || die "$(l10n "$1 缺少值" "$1 requires a value")"; }

preparse_language() {
  local previous="" argument
  for argument in "$@"; do
    if [[ "${previous}" == --lang || "${previous}" == --language ]]; then
      INTERFACE_LANGUAGE="${argument}"
      INTERFACE_LANGUAGE_EXPLICIT=1
      return
    fi
    previous="${argument}"
  done
}

parse_args() {
  while (($#)); do
    case "$1" in
      --yes) ASSUME_YES=1; shift ;;
      --non-interactive) NON_INTERACTIVE=1; ASSUME_YES=1; shift ;;
      --lang|--language)
        need_value "$@"; INTERFACE_LANGUAGE="$2"; INTERFACE_LANGUAGE_EXPLICIT=1; shift 2 ;;
      --model-source)
        need_value "$@"; MODEL_SOURCE="$2"; MODEL_SELECTION_EXPLICIT=1
        case "$2" in huggingface|modelscope) MODEL_HUB="$2"; MODEL_HUB_EXPLICIT=1 ;; esac
        shift 2 ;;
      --model-id) need_value "$@"; MODEL_ID="$2"; MODEL_SELECTION_EXPLICIT=1; MODEL_ID_EXPLICIT=1; shift 2 ;;
      --model-revision) need_value "$@"; MODEL_REVISION="$2"; MODEL_REVISION_EXPLICIT=1; shift 2 ;;
      --catalog-query) need_value "$@"; CATALOG_QUERY="$2"; shift 2 ;;
      --catalog-task) need_value "$@"; CATALOG_TASK="$2"; shift 2 ;;
      --model-root) need_value "$@"; MODEL_ROOT="$2"; MODEL_ROOT_EXPLICIT=1; shift 2 ;;
      --tp-size) need_value "$@"; TP_SIZE="$2"; TP_EXPLICIT=1; shift 2 ;;
      --max-num-seqs) need_value "$@"; MAX_NUM_SEQS="$2"; SEQS_EXPLICIT=1; shift 2 ;;
      --active-instances) need_value "$@"; ACTIVE_INSTANCE_COUNT="$2"; ACTIVE_COUNT_EXPLICIT=1; shift 2 ;;
      --startup-parallelism) need_value "$@"; STARTUP_PARALLELISM="$2"; STARTUP_PARALLELISM_EXPLICIT=1; shift 2 ;;
      --max-model-len) need_value "$@"; MAX_MODEL_LEN="$2"; MAX_LEN_EXPLICIT=1; shift 2 ;;
      --gpu-memory-utilization) need_value "$@"; GPU_MEMORY_UTILIZATION="$2"; shift 2 ;;
      --max-num-batched-tokens) need_value "$@"; MAX_NUM_BATCHED_TOKENS="$2"; shift 2 ;;
      --api-bind) need_value "$@"; API_BIND="$2"; shift 2 ;;
      --api-port) need_value "$@"; API_PORT="$2"; shift 2 ;;
      --worker-base-port) need_value "$@"; WORKER_BASE_PORT="$2"; shift 2 ;;
      --ui-username) need_value "$@"; UI_USERNAME="$2"; UI_USERNAME_EXPLICIT=1; shift 2 ;;
      --ui-password) need_value "$@"; UI_PASSWORD="$2"; UI_PASSWORD_EXPLICIT=1; shift 2 ;;
      --database-port) need_value "$@"; LITELLM_DB_PORT="$2"; shift 2 ;;
      --proxy) need_value "$@"; PROXY_URL="$2"; shift 2 ;;
      --save-proxy) SAVE_PROXY=1; shift ;;
      --no-start) NO_START=1; shift ;;
      --skip-download) SKIP_DOWNLOAD=1; shift ;;
      --skip-packages) SKIP_PACKAGES=1; shift ;;
      --force-reconfigure) FORCE_RECONFIGURE=1; shift ;;
      --vllm-image) need_value "$@"; VLLM_IMAGE="$2"; shift 2 ;;
      --litellm-image) need_value "$@"; LITELLM_IMAGE="$2"; shift 2 ;;
      --postgres-image) need_value "$@"; POSTGRES_IMAGE="$2"; shift 2 ;;
      --help|-h) usage; exit 0 ;;
      --version) printf '%s\n' "${INSTALLER_VERSION}"; exit 0 ;;
      *) die "$(l10n "未知参数：$1（使用 --help 查看帮助）" "Unknown option: $1 (use --help for help)")" ;;
    esac
  done
  if (( MODEL_ID_EXPLICIT )) && [[ "${MODEL_SOURCE}" == catalog ]]; then
    MODEL_SOURCE="${MODEL_HUB}"
  fi
}

select_language_interactively() {
  if (( ! INTERFACE_LANGUAGE_EXPLICIT && ! NON_INTERACTIVE && ! ASSUME_YES )); then
    local choice
    cat >&2 <<'EOF'

请选择语言 / Select language:
  1) 中文（默认）
  2) English
EOF
    read -r -p '选择 / Choice [1]: ' choice
    case "${choice:-1}" in
      1|zh|ZH) INTERFACE_LANGUAGE=zh ;;
      2|en|EN) INTERFACE_LANGUAGE=en ;;
      *) die "$(l10n "语言选择无效：${choice}" "Invalid language choice: ${choice}")" ;;
    esac
  fi
  [[ "${INTERFACE_LANGUAGE}" == zh || "${INTERFACE_LANGUAGE}" == en ]] || \
    die "$(l10n "language 只能是 zh 或 en" "language must be zh or en")"
  export LLMCTL_LANG="${INTERFACE_LANGUAGE}"
}

ask() {
  local prompt="$1" default="${2:-}" answer
  if (( NON_INTERACTIVE || ASSUME_YES )); then
    printf '%s\n' "${default}"
    return
  fi
  read -r -p "${prompt}" answer
  printf '%s\n' "${answer:-${default}}"
}

confirm() {
  local prompt="$1" default_no="${2:-1}" answer
  if (( NON_INTERACTIVE || ASSUME_YES )); then
    (( default_no == 0 ))
    return
  fi
  read -r -p "${prompt}" answer
  if (( default_no )); then [[ "${answer}" =~ ^[Yy]$ ]]; else [[ ! "${answer}" =~ ^[Nn]$ ]]; fi
}

select_model_interactively() {
  (( ASSUME_YES || NON_INTERACTIVE || MODEL_SELECTION_EXPLICIT )) && return 0
  local choice hub query task index result_count
  while true; do
    if [[ "${INTERFACE_LANGUAGE}" == en ]]; then
      cat >&2 <<EOF

Choose how to discover a model:
  1) Search Hugging Face and ModelScope, then recommend for this host (recommended)
  2) Search ModelScope only (usually directly reachable in China)
  3) Search Hugging Face only
  4) Use the validated profile ${VALIDATED_MODEL_ID}
  5) Enter a Hub, model ID, and optional revision directly
  0) Cancel installation
EOF
    else
      cat >&2 <<EOF

请选择模型发现方式：
  1) 同时搜索 Hugging Face 与 ModelScope，并按本机硬件推荐（推荐）
  2) 只搜索 ModelScope（国内网络通常可直连）
  3) 只搜索 Hugging Face
  4) 使用已验证配置 ${VALIDATED_MODEL_ID}
  5) 直接输入 Hub、模型 ID 和可选 revision
  0) 退出安装
EOF
    fi
    read -r -p "$(l10n '选择 [1]: ' 'Choice [1]: ')" choice
    case "${choice:-1}" in
      0|q|Q|quit|QUIT) log "$(l10n '已取消，尚未执行安装。' 'Cancelled before installation started.')"; exit 0 ;;
      1) MODEL_SOURCE=catalog; hub=all ;;
      2) MODEL_SOURCE=catalog; hub=modelscope ;;
      3) MODEL_SOURCE=catalog; hub=huggingface ;;
      4)
        MODEL_SOURCE=validated
        MODEL_HUB=huggingface
        MODEL_ID="${VALIDATED_MODEL_ID}"
        MODEL_REVISION="${VALIDATED_MODEL_REVISION}"
        if plan_single_model_interactively; then return 0; else continue; fi
        ;;
      5)
      read -r -p "$(l10n '来源 huggingface/modelscope [modelscope]（b 返回）: ' 'Source huggingface/modelscope [modelscope] (b to go back): ')" hub
      hub="${hub:-modelscope}"
      case "${hub}" in b|B|back|BACK) continue ;; esac
      [[ "${hub}" == huggingface || "${hub}" == modelscope ]] || {
        warn "$(l10n '来源只能是 huggingface 或 modelscope，请重新选择。' 'Source must be huggingface or modelscope; choose again.')"
        continue
      }
      read -r -p "$(l10n 'MODEL_ID（OWNER/REPO，b 返回）: ' 'MODEL_ID (OWNER/REPO, b to go back): ')" MODEL_ID
      case "${MODEL_ID}" in b|B|back|BACK) continue ;; esac
      read -r -p "$(l10n 'revision（可留空；HF 会固定到当前 commit，ModelScope 默认 master）: ' 'Revision (optional; HF pins the current commit, ModelScope defaults to master): ')" MODEL_REVISION
      MODEL_SOURCE="${hub}"
      MODEL_HUB="${hub}"
      MODEL_ID_EXPLICIT=1
      [[ -z "${MODEL_REVISION}" ]] || MODEL_REVISION_EXPLICIT=1
      if plan_single_model_interactively; then return 0; else continue; fi
      ;;
      *) warn "$(l10n "无效模型选择：${choice}，请重新选择。" "Invalid model choice: ${choice}; choose again.")"; continue ;;
    esac

    read -r -p "$(l10n '搜索关键词（可留空查看热门模型）: ' 'Search term (leave blank for popular models): ')" query
    read -r -p "$(l10n '用途 auto/text/vision [auto]: ' 'Task auto/text/vision [auto]: ')" task
    CATALOG_QUERY="${query}"
    CATALOG_TASK="${task:-auto}"
    search_catalog "${hub}"
    result_count=$(catalog_result_count "${CATALOG_RESULTS}") || die "$(l10n '无法读取候选模型数量' 'Could not read the candidate model count')"
    while true; do
      read -r -p "$(l10n "选择模型序号 [1]（1-${result_count}，0/b 返回，q 退出）: " "Model number [1] (1-${result_count}, 0/b back, q quit): ")" index
      index="${index:-1}"
      case "${index}" in
        0|b|B|back|BACK)
          discard_catalog_results
          printf '%s\n' "$(l10n '已返回模型发现方式。' 'Returned to model discovery.')" >&2
          break
          ;;
        q|Q|quit|QUIT)
          discard_catalog_results
          log "$(l10n '已取消，尚未执行安装。' 'Cancelled before installation started.')"
          exit 0
          ;;
      esac
      if [[ ! "${index}" =~ ^[0-9]+$ ]] || (( index < 1 || index > result_count )); then
        warn "$(l10n "模型序号必须在 1-${result_count}；输入 0 返回。" "Model number must be 1-${result_count}; enter 0 to go back.")"
        continue
      fi
      show_catalog_selection_summary "${CATALOG_RESULTS}" "${index}" || \
        die "$(l10n '无法生成所选模型的详细计划' 'Could not generate a detailed plan for the selected model')"
      while true; do
        read -r -p "$(l10n '确认此模型与计划？[Y] 接受 / b 返回列表 / s 重新搜索 / q 退出: ' 'Confirm this model and plan? [Y] accept / b back to list / s search again / q quit: ')" choice
        case "${choice:-Y}" in
          y|Y|yes|YES)
            apply_catalog_selection "${CATALOG_RESULTS}" "${index}"
            return 0
            ;;
          b|B|back|BACK|n|N|no|NO)
            printf '%s\n' "$(l10n '已返回候选模型列表。' 'Returned to the candidate model list.')" >&2
            break
            ;;
          s|S|search|SEARCH|0)
            discard_catalog_results
            printf '%s\n' "$(l10n '已返回模型发现方式。' 'Returned to model discovery.')" >&2
            break 2
            ;;
          q|Q|quit|QUIT)
            discard_catalog_results
            log "$(l10n '已取消，尚未执行安装。' 'Cancelled before installation started.')"
            exit 0
            ;;
          *) warn "$(l10n '请输入 Y、b、s 或 q。' 'Enter Y, b, s, or q.')" ;;
        esac
      done
    done
  done
}

plan_single_model_interactively() {
  local result choice
  local -a plan_args=(
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
    --model-root "${MODEL_ROOT}"
  )
  [[ -z "${HOST_PROFILE_JSON}" ]] || plan_args+=(--hardware-json "${HOST_PROFILE_JSON}")
  (( MAX_LEN_EXPLICIT == 0 )) || plan_args+=(--max-model-len "${MAX_MODEL_LEN}")
  result=$(mktemp /tmp/llm-inspect.XXXXXX.json)
  if ! run_catalog_with_retry inspect "${MODEL_HUB}" "${MODEL_ID}" ${MODEL_REVISION:+"${MODEL_REVISION}"} \
    --json "${plan_args[@]}" >"${result}"; then
    rm -f -- "${result}"
    warn "$(l10n '该模型未通过元数据、vLLM 架构或本机硬件门禁；已返回模型发现。' 'This model did not pass metadata, vLLM architecture, or host hardware gates; returning to model discovery.')"
    return 1
  fi
  show_catalog_selection_summary "${result}" 1 || {
    rm -f -- "${result}"
    return 1
  }
  while true; do
    read -r -p "$(l10n '确认此模型与计划？[Y] 接受 / b 返回 / q 退出: ' 'Confirm this model and plan? [Y] accept / b back / q quit: ')" choice
    case "${choice:-Y}" in
      y|Y|yes|YES)
        apply_catalog_selection "${result}" 1
        return 0
        ;;
      b|B|back|BACK|n|N|no|NO)
        rm -f -- "${result}"
        printf '%s\n' "$(l10n '已返回模型发现方式。' 'Returned to model discovery.')" >&2
        return 1
        ;;
      q|Q|quit|QUIT)
        rm -f -- "${result}"
        log "$(l10n '已取消，尚未执行安装。' 'Cancelled before installation started.')"
        exit 0
        ;;
      *) warn "$(l10n '请输入 Y、b 或 q。' 'Enter Y, b, or q.')" ;;
    esac
  done
}

show_catalog_selection_summary() {
  python3 "${CATALOG_SOURCE}" --lang "${INTERFACE_LANGUAGE}" select "$1" "$2" --summary
}

catalog_result_count() {
  python3 - "$1" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle)
if not isinstance(value, list) or not value:
    raise SystemExit(1)
print(len(value))
PY
}

discard_catalog_results() {
  if [[ -n "${CATALOG_RESULTS}" ]]; then
    rm -f -- "${CATALOG_RESULTS}"
    CATALOG_RESULTS=""
  fi
}

run_catalog_with_retry() {
  local -a command=("$@")
  if python3 "${CATALOG_SOURCE}" --lang "${INTERFACE_LANGUAGE}" "${command[@]}"; then return 0; fi
  warn "$(l10n '模型目录请求失败；若当前网络没有国际出口，将请求临时代理后重试。' 'The model catalog request failed; if this host lacks international access, the installer will ask for a temporary proxy and retry.')"
  prompt_proxy_if_needed
  export_proxy
  python3 "${CATALOG_SOURCE}" --lang "${INTERFACE_LANGUAGE}" "${command[@]}"
}

search_catalog() {
  local source="${1:-all}" query="${CATALOG_QUERY}" task="${CATALOG_TASK}"
  local -a plan_args=(
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
    --model-root "${MODEL_ROOT}"
  )
  [[ -z "${HOST_PROFILE_JSON}" ]] || plan_args+=(--hardware-json "${HOST_PROFILE_JSON}")
  if (( MAX_LEN_EXPLICIT )); then
    plan_args+=(--max-model-len "${MAX_MODEL_LEN}")
  fi
  [[ "${task}" =~ ^(auto|text|vision)$ ]] || die "$(l10n 'catalog-task 只能是 auto、text 或 vision' 'catalog-task must be auto, text, or vision')"
  CATALOG_RESULTS=$(mktemp /tmp/llm-catalog.XXXXXX.json)
  log "$(l10n "读取 ${source} 模型目录并按本机完整硬件快照规划，请稍候..." "Reading the ${source} catalog and planning from the complete host snapshot; please wait...")"
  run_catalog_with_retry search "${query}" --source "${source}" --task "${task}" \
    --limit "${CATALOG_LIMIT}" --output-json "${CATALOG_RESULTS}" "${plan_args[@]}" || \
    die "$(l10n '没有取得可保守部署的候选模型' 'No conservatively deployable model candidate was found')"
}

apply_catalog_assignments() {
  local assignments_file="${1:?}" saved_tp="${TP_SIZE}" saved_seqs="${MAX_NUM_SEQS}" saved_len="${MAX_MODEL_LEN}"
  local saved_startup="${STARTUP_PARALLELISM}"
  # The assignments are emitted by our local, root-owned helper and every value
  # is shell-quoted with shlex.quote before eval.
  # shellcheck disable=SC1090
  eval "$(<"${assignments_file}")"
  local planned_tp="${TP_SIZE}" planned_len="${MAX_MODEL_LEN}"
  if (( TP_EXPLICIT )) && (( saved_tp < planned_tp )); then
    die "$(l10n "指定 TP=${saved_tp} 小于模型在当前显存下所需的 TP=${planned_tp}" "Requested TP=${saved_tp} is smaller than TP=${planned_tp}, which this model requires on the available VRAM")"
  fi
  if (( MAX_LEN_EXPLICIT )) && (( saved_len != planned_len )); then
    die "$(l10n "指定上下文 ${saved_len} 无法保守容纳；当前计划最多 ${planned_len}" "Requested context ${saved_len} does not fit conservatively; the current plan allows at most ${planned_len}")"
  fi
  if (( SEQS_EXPLICIT )) && (( saved_seqs > ESTIMATED_MAX_NUM_SEQS )); then
    die "$(l10n "指定 max-num-seqs=${saved_seqs} 超过当前硬件估算上限 ${ESTIMATED_MAX_NUM_SEQS}" "Requested max-num-seqs=${saved_seqs} exceeds the host estimate of ${ESTIMATED_MAX_NUM_SEQS}")"
  fi
  (( TP_EXPLICIT )) && TP_SIZE="${saved_tp}"
  (( SEQS_EXPLICIT )) && MAX_NUM_SEQS="${saved_seqs}"
  (( MAX_LEN_EXPLICIT )) && MAX_MODEL_LEN="${saved_len}"
  (( STARTUP_PARALLELISM_EXPLICIT )) && STARTUP_PARALLELISM="${saved_startup}"
  MODEL_PLAN_APPLIED=1
}

apply_catalog_selection() {
  local input="${1:?}" index="${2:?}" assignments
  assignments=$(mktemp /tmp/llm-plan.XXXXXX.env)
  python3 "${CATALOG_SOURCE}" --lang "${INTERFACE_LANGUAGE}" select "${input}" "${index}" --shell >"${assignments}" || die "$(l10n '模型选择无效' 'Invalid model selection')"
  apply_catalog_assignments "${assignments}"
  rm -f "${assignments}" "${input}"
  CATALOG_RESULTS=""
}

inspect_and_plan_model() {
  local assignments
  local -a plan_args=(
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
    --model-root "${MODEL_ROOT}"
  )
  [[ -z "${HOST_PROFILE_JSON}" ]] || plan_args+=(--hardware-json "${HOST_PROFILE_JSON}")
  if (( MAX_LEN_EXPLICIT )); then
    plan_args+=(--max-model-len "${MAX_MODEL_LEN}")
  fi
  assignments=$(mktemp /tmp/llm-plan.XXXXXX.env)
  if ! run_catalog_with_retry inspect "${MODEL_HUB}" "${MODEL_ID}" ${MODEL_REVISION:+"${MODEL_REVISION}"} \
    --shell "${plan_args[@]}" >"${assignments}"; then
    rm -f "${assignments}"
    die "$(l10n "${MODEL_HUB}:${MODEL_ID} 未通过元数据、vLLM 架构或本机硬件门禁" "${MODEL_HUB}:${MODEL_ID} did not pass metadata, vLLM architecture, or host hardware gates")"
  fi
  apply_catalog_assignments "${assignments}"
  rm -f "${assignments}"
}

apply_model_source_defaults() {
  case "${MODEL_SOURCE}" in
    validated)
      MODEL_HUB=huggingface
      MODEL_ID="${VALIDATED_MODEL_ID}"
      MODEL_REVISION="${VALIDATED_MODEL_REVISION}"
      (( MODEL_PLAN_APPLIED )) || inspect_and_plan_model
      ;;
    official)
      MODEL_HUB=huggingface
      MODEL_ID="${OFFICIAL_MODEL_ID}"
      MODEL_REVISION="${OFFICIAL_MODEL_REVISION}"
      (( ASSUME_YES || NON_INTERACTIVE )) && warn "$(l10n '已选择存在公开兼容性报告的 official FP8 仓库。' 'Selected the official FP8 repository, which has a public compatibility report.')"
      (( MODEL_PLAN_APPLIED )) || inspect_and_plan_model
      ;;
    catalog)
      if (( ! MODEL_PLAN_APPLIED )); then
        search_catalog "${MODEL_HUB_EXPLICIT:+${MODEL_HUB}}"
        apply_catalog_selection "${CATALOG_RESULTS}" 1
      fi
      ;;
    huggingface|modelscope|custom)
      [[ "${MODEL_SOURCE}" != custom ]] || MODEL_SOURCE=huggingface
      MODEL_HUB="${MODEL_SOURCE}"
      if (( ! MODEL_ID_EXPLICIT )); then
        search_catalog "${MODEL_HUB}"
        apply_catalog_selection "${CATALOG_RESULTS}" 1
        return 0
      fi
      if (( ! MODEL_PLAN_APPLIED )); then
        (( MODEL_REVISION_EXPLICIT )) || MODEL_REVISION=""
        inspect_and_plan_model
      fi
      ;;
    *) die "$(l10n '--model-source 只能是 catalog、validated、official、huggingface 或 modelscope' '--model-source must be catalog, validated, official, huggingface, or modelscope')" ;;
  esac
}

select_topology_interactively() {
  (( ASSUME_YES || NON_INTERACTIVE )) && return 0
  local choice seqs
  if (( ! TP_EXPLICIT )); then
    read -r -p "$(l10n "Tensor Parallel 大小 [硬件推荐 ${TP_SIZE}]: " "Tensor Parallel size [host recommendation ${TP_SIZE}]: ")" choice
    TP_SIZE="${choice:-${TP_SIZE}}"
  fi
  if (( ! SEQS_EXPLICIT )); then
    read -r -p "$(l10n "每实例 max-num-seqs [显存估算 ${MAX_NUM_SEQS}]: " "max-num-seqs per replica [VRAM estimate ${MAX_NUM_SEQS}]: ")" seqs
    MAX_NUM_SEQS="${seqs:-${MAX_NUM_SEQS}}"
  fi
}

select_storage_interactively() {
  (( ASSUME_YES || NON_INTERACTIVE || MODEL_ROOT_EXPLICIT )) && return 0
  local chosen suggested=""
  suggested=$(find_reusable_model_root || true)
  if [[ -n "${suggested}" && "${suggested}" != "${MODEL_ROOT}" ]]; then
    log "$(l10n "检测到身份与 revision 完全匹配的本地模型：${suggested}；将其作为默认目录，不复制权重。" "Found a local model with an exact identity and revision match at ${suggested}; it is now the default and weights will not be copied.")"
    MODEL_ROOT="${suggested}"
  fi
  read -r -p "$(l10n "模型下载/存放目录 [${MODEL_ROOT}]: " "Model download/storage directory [${MODEL_ROOT}]: ")" chosen
  MODEL_ROOT="${chosen:-${MODEL_ROOT}}"
}

expected_model_directory() {
  local root="${1:?}" safe_name
  safe_name="${MODEL_ID//\//--}-${MODEL_REVISION:0:12}"
  printf '%s/%s\n' "${root%/}" "${safe_name}"
}

model_manifest_matches_selection() {
  local root="${1:?}" manifest="${1%/}/current.manifest"
  [[ -r "${manifest}" ]] || return 1
  grep -Fqx "MODEL_ID=${MODEL_ID}" "${manifest}" || return 1
  grep -Fqx "MODEL_REVISION=${MODEL_REVISION}" "${manifest}" || return 1
  if grep -q '^MODEL_HUB=' "${manifest}"; then
    grep -Fqx "MODEL_HUB=${MODEL_HUB}" "${manifest}"
    return
  fi
  # Legacy Ornith manifests predate MODEL_HUB. They are known Hugging Face
  # installs and are accepted only for the two pinned Ornith identities.
  [[ -f "${root%/}/.ornith-model-root" && "${MODEL_HUB}" == huggingface ]] || return 1
  [[ "${MODEL_ID}" == "${VALIDATED_MODEL_ID}" || "${MODEL_ID}" == "${OFFICIAL_MODEL_ID}" ]]
}

directory_size_bytes() {
  python3 - "${1:?}" <<'PY'
import os
import sys
total = 0
for root, _, files in os.walk(sys.argv[1]):
    for name in files:
        try:
            total += os.stat(os.path.join(root, name), follow_symlinks=False).st_size
        except OSError:
            pass
print(total)
PY
}

model_files_match_selection() {
  local directory="${1:?}" bytes expected_min
  [[ -r "${directory}/config.json" ]] || return 1
  python3 - "${directory}/config.json" "${MODEL_ARCHITECTURE}" <<'PY' || return 1
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)
architectures = config.get("architectures") or (config.get("text_config") or {}).get("architectures") or []
raise SystemExit(0 if sys.argv[2] in architectures else 1)
PY
  find "${directory}" -maxdepth 2 -type f \( -name '*.safetensors' -o -name 'pytorch_model*.bin' -o -name 'model*.bin' \) -print -quit | grep -q . || return 1
  bytes=$(directory_size_bytes "${directory}" 2>/dev/null) || return 1
  [[ "${bytes}" =~ ^[0-9]+$ ]] || return 1
  expected_min=$((MODEL_WEIGHT_BYTES * 7 / 10))
  (( bytes >= expected_min ))
}

reusable_model_directory() {
  local root="${1:?}" expected candidate resolved root_resolved
  model_manifest_matches_selection "${root}" || return 1
  expected=$(expected_model_directory "${root}")
  root_resolved=$(cd -P -- "${root}" 2>/dev/null && pwd) || return 1
  for candidate in "${expected}" "${root%/}/current"; do
    [[ -r "${candidate}/config.json" ]] || continue
    resolved=$(cd -P -- "${candidate}" 2>/dev/null && pwd) || continue
    [[ "$(dirname "${resolved}")" == "${root_resolved}" ]] || continue
    model_files_match_selection "${resolved}" || continue
    printf '%s\n' "${resolved}"
    return 0
  done
  return 1
}

reusable_model_at_root() {
  reusable_model_directory "${1:?}" >/dev/null
}

find_reusable_model_root() {
  local candidate
  for candidate in "${MODEL_ROOT}" /data/ornith/models; do
    reusable_model_at_root "${candidate}" || continue
    printf '%s\n' "${candidate%/}"
    return 0
  done
  return 1
}

validate_model_root() {
  [[ "${MODEL_ROOT}" == /* ]] || die "$(l10n 'model-root 必须是绝对路径' 'model-root must be an absolute path')"
  [[ "${MODEL_ROOT}" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "$(l10n 'model-root 只允许字母、数字、点、下划线、连字符和斜杠' 'model-root may contain only letters, digits, dots, underscores, hyphens, and slashes')"
  local normalized="${MODEL_ROOT%/}"
  [[ -n "${normalized}" ]] || normalized="/"
  case "${normalized}" in
    /|/data|/mnt|/media|/srv|/opt|/var|/home|/root|/usr)
      die "$(l10n "model-root 过于宽泛：${normalized}；请指定专用子目录，例如 /data/llm-cluster/models" "model-root is too broad: ${normalized}; use a dedicated subdirectory such as /data/llm-cluster/models")"
      ;;
  esac
  MODEL_ROOT="${normalized}"
}

validate_scalar_config() {
  validate_model_root
  [[ "${MODEL_ID}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "$(l10n "MODEL_ID 格式无效：${MODEL_ID}" "Invalid MODEL_ID: ${MODEL_ID}")"
  [[ "${MODEL_REVISION}" =~ ^[A-Za-z0-9._/-]+$ ]] || die "$(l10n 'MODEL_REVISION 格式无效' 'Invalid MODEL_REVISION')"
  [[ "${MODEL_HUB}" =~ ^(huggingface|modelscope)$ ]] || die "$(l10n "MODEL_HUB 无效：${MODEL_HUB}" "Invalid MODEL_HUB: ${MODEL_HUB}")"
  [[ "${MODEL_ARCHITECTURE}" =~ ^[A-Za-z0-9_]+$ ]] || die "$(l10n 'MODEL_ARCHITECTURE 格式无效' 'Invalid MODEL_ARCHITECTURE')"
  [[ "${MODEL_TASK}" =~ ^(text|vision)$ ]] || die "$(l10n 'MODEL_TASK 只能是 text 或 vision' 'MODEL_TASK must be text or vision')"
  [[ "${MODEL_PRECISION}" =~ ^[A-Za-z0-9._-]+$ ]] || die "$(l10n 'MODEL_PRECISION 格式无效' 'Invalid MODEL_PRECISION')"
  [[ "${MODEL_WEIGHT_BYTES}" =~ ^[0-9]+$ ]] && (( MODEL_WEIGHT_BYTES > 0 )) || die "$(l10n 'MODEL_WEIGHT_BYTES 无效' 'Invalid MODEL_WEIGHT_BYTES')"
  [[ "${TOOL_CALL_PARSER}" =~ ^[A-Za-z0-9_.-]*$ ]] || die "$(l10n 'TOOL_CALL_PARSER 格式无效' 'Invalid TOOL_CALL_PARSER')"
  [[ "${REASONING_PARSER}" =~ ^[A-Za-z0-9_.-]*$ ]] || die "$(l10n 'REASONING_PARSER 格式无效' 'Invalid REASONING_PARSER')"
  local capability
  for capability in SUPPORTS_IMAGE_INPUT SUPPORTS_OCR SUPPORTS_TOOL_CALLING SUPPORTS_REASONING SUPPORTS_THINKING_TOGGLE TRUST_REMOTE_CODE; do
    [[ "${!capability}" =~ ^[01]$ ]] || die "$(l10n "${capability} 必须为 0 或 1" "${capability} must be 0 or 1")"
  done
  [[ "${TP_SIZE}" =~ ^(1|2|4|8)$ ]] || die "$(l10n 'TP_SIZE 只能是 1、2、4 或 8' 'TP_SIZE must be 1, 2, 4, or 8')"
  [[ "${MAX_NUM_SEQS}" =~ ^[0-9]+$ ]] && (( MAX_NUM_SEQS >= 1 && MAX_NUM_SEQS <= 16 )) || die "$(l10n 'max-num-seqs 范围 1-16' 'max-num-seqs must be between 1 and 16')"
  [[ "${ESTIMATED_MAX_NUM_SEQS}" =~ ^[0-9]+$ ]] && (( ESTIMATED_MAX_NUM_SEQS >= 1 && ESTIMATED_MAX_NUM_SEQS <= 16 )) || die "$(l10n '目录返回的序列容量估算无效' 'The catalog returned an invalid sequence-capacity estimate')"
  (( MAX_NUM_SEQS <= ESTIMATED_MAX_NUM_SEQS )) || die "$(l10n "max-num-seqs=${MAX_NUM_SEQS} 超过当前模型/显存估算上限 ${ESTIMATED_MAX_NUM_SEQS}" "max-num-seqs=${MAX_NUM_SEQS} exceeds the model/VRAM estimate of ${ESTIMATED_MAX_NUM_SEQS}")"
  [[ "${MAX_MODEL_LEN}" =~ ^[0-9]+$ ]] && (( MAX_MODEL_LEN >= 8192 && MAX_MODEL_LEN <= 262144 )) || die "$(l10n 'max-model-len 范围 8192-262144' 'max-model-len must be between 8192 and 262144')"
  [[ "${MAX_NUM_BATCHED_TOKENS}" =~ ^[0-9]+$ ]] && (( MAX_NUM_BATCHED_TOKENS >= 1024 && MAX_NUM_BATCHED_TOKENS <= 65536 )) || die "$(l10n 'max-num-batched-tokens 范围 1024-65536' 'max-num-batched-tokens must be between 1024 and 65536')"
  if (( STARTUP_PARALLELISM_EXPLICIT )); then
    [[ "${STARTUP_PARALLELISM}" =~ ^[0-9]+$ ]] && (( STARTUP_PARALLELISM >= 1 && STARTUP_PARALLELISM <= 8 )) || die "$(l10n 'startup-parallelism 范围 1-8' 'startup-parallelism must be between 1 and 8')"
  fi
  awk -v v="${GPU_MEMORY_UTILIZATION}" 'BEGIN{exit !(v>=0.70 && v<=0.96)}' || die "$(l10n 'gpu-memory-utilization 范围 0.70-0.96' 'gpu-memory-utilization must be between 0.70 and 0.96')"
  [[ "${API_BIND}" =~ ^[0-9a-fA-F:.]+$ ]] || die "$(l10n 'api-bind 只能是 IP 地址' 'api-bind must be an IP address')"
  [[ "${API_PORT}" =~ ^[0-9]+$ ]] && (( API_PORT >= 1024 && API_PORT <= 65535 )) || die "$(l10n 'api-port 范围 1024-65535' 'api-port must be between 1024 and 65535')"
  [[ "${WORKER_BASE_PORT}" =~ ^[0-9]+$ ]] && (( WORKER_BASE_PORT >= 1024 && WORKER_BASE_PORT <= 65000 )) || die "$(l10n 'worker-base-port 范围 1024-65000' 'worker-base-port must be between 1024 and 65000')"
  [[ "${LITELLM_DB_PORT}" =~ ^[0-9]+$ ]] && (( LITELLM_DB_PORT >= 1024 && LITELLM_DB_PORT <= 65535 )) || die "$(l10n 'database-port 范围 1024-65535' 'database-port must be between 1024 and 65535')"
  (( API_PORT < WORKER_BASE_PORT || API_PORT >= WORKER_BASE_PORT + 16 )) || die "$(l10n 'API 端口与 Worker 端口范围冲突' 'The API port conflicts with the worker port range')"
  (( LITELLM_DB_PORT != API_PORT )) || die "$(l10n 'database-port 不能与 API 端口相同' 'database-port cannot equal the API port')"
  (( LITELLM_DB_PORT < WORKER_BASE_PORT || LITELLM_DB_PORT >= WORKER_BASE_PORT + 16 )) || die "$(l10n 'database-port 与 Worker 端口范围冲突' 'database-port conflicts with the worker port range')"
  [[ "${UI_USERNAME}" =~ ^[A-Za-z0-9._@-]{1,64}$ ]] || die "$(l10n 'ui-username 只允许字母、数字、点、下划线、@ 和连字符' 'ui-username may contain only letters, digits, dots, underscores, @, and hyphens')"
  [[ "${UI_PASSWORD}" =~ ^[A-Za-z0-9._@-]{8,128}$ ]] || die "$(l10n 'ui-password 需 8-128 位，且只允许字母、数字、点、下划线、@ 和连字符' 'ui-password must be 8-128 characters using letters, digits, dots, underscores, @, and hyphens')"
  [[ "${VLLM_IMAGE}" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die "$(l10n 'vLLM 镜像名格式无效' 'Invalid vLLM image name')"
  [[ "${LITELLM_IMAGE}" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die "$(l10n 'LiteLLM 镜像名格式无效' 'Invalid LiteLLM image name')"
  [[ "${POSTGRES_IMAGE}" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die "$(l10n 'PostgreSQL 镜像名格式无效' 'Invalid PostgreSQL image name')"
  if [[ -n "${PROXY_URL}" ]]; then
    [[ "${PROXY_URL}" =~ ^https?://[A-Za-z0-9._:-]+$ ]] || die "$(l10n '代理格式应为 http://IP:PORT 或 https://IP:PORT' 'Proxy must be formatted as http://IP:PORT or https://IP:PORT')"
  fi
}

check_discovery_host() {
  [[ ${EUID} -eq 0 ]] || die "$(l10n '请使用 sudo 运行安装器' 'Run the installer with sudo')"
  [[ -r /etc/os-release ]] || die "$(l10n '无法识别操作系统' 'Could not identify the operating system')"
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04* ]] || die "$(l10n "仅支持 Ubuntu 24.04；检测到 ${PRETTY_NAME:-unknown}" "Only Ubuntu 24.04 is supported; detected ${PRETTY_NAME:-unknown}")"
  [[ -r "${MANAGER_SOURCE}" ]] || die "$(l10n 'llmctl.sh 必须与安装脚本放在同一目录' 'llmctl.sh must be in the same directory as the installer')"
  [[ -r "${CATALOG_SOURCE}" ]] || die "$(l10n 'lib/model_catalog.py 必须与安装脚本放在同一目录' 'lib/model_catalog.py must be in the same directory as the installer')"
  command -v python3 >/dev/null 2>&1 || die "$(l10n '未发现 python3' 'python3 was not found')"
  command -v nvidia-smi >/dev/null 2>&1 || die "$(l10n '未发现 nvidia-smi；请先正确安装 NVIDIA 驱动' 'nvidia-smi was not found; install the NVIDIA driver first')"
  nvidia-smi -L >/dev/null 2>&1 || die "$(l10n 'NVIDIA 驱动已安装，但 GPU 当前不可用' 'The NVIDIA driver is installed, but the GPUs are unavailable')"
  local driver
  driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1 | tr -d ' ')
  [[ -n "${driver}" ]] || die "$(l10n '无法读取 NVIDIA 驱动版本' 'Could not read the NVIDIA driver version')"
  if [[ "$(printf '%s\n' 580.0 "${driver}" | sort -V | head -n1)" != 580.0 ]]; then
    die "$(l10n "驱动 ${driver} 过旧；CUDA 13 容器至少需要 R580 驱动分支。安装器不会自动改驱动。" "Driver ${driver} is too old; the CUDA 13 container requires at least the R580 branch. The installer will not change the driver automatically.")"
  fi
  HOST_PROFILE_JSON=$(python3 "${CATALOG_SOURCE}" --lang "${INTERFACE_LANGUAGE}" hardware \
    --model-root "${MODEL_ROOT}" --json) || die "$(l10n '本机硬件体检失败' 'Host hardware preflight failed')"
  python3 "${CATALOG_SOURCE}" --lang "${INTERFACE_LANGUAGE}" hardware \
    --hardware-json "${HOST_PROFILE_JSON}" --model-root "${MODEL_ROOT}" --summary || \
    die "$(l10n '无法显示本机硬件体检结果' 'Could not display host preflight results')"
}

check_host() {
  [[ ${EUID} -eq 0 ]] || die "$(l10n '请使用 sudo 运行安装器' 'Run the installer with sudo')"
  [[ -r /etc/os-release ]] || die "$(l10n '无法识别操作系统' 'Could not identify the operating system')"
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04* ]] || die "$(l10n "仅支持 Ubuntu 24.04；检测到 ${PRETTY_NAME:-unknown}" "Only Ubuntu 24.04 is supported; detected ${PRETTY_NAME:-unknown}")"
  [[ -x "${MANAGER_SOURCE}" ]] || [[ -r "${MANAGER_SOURCE}" ]] || die "$(l10n 'llmctl.sh 必须与安装脚本放在同一目录' 'llmctl.sh must be in the same directory as the installer')"
  [[ -r "${CATALOG_SOURCE}" ]] || die "$(l10n 'lib/model_catalog.py 必须与安装脚本放在同一目录' 'lib/model_catalog.py must be in the same directory as the installer')"
  command -v python3 >/dev/null 2>&1 || die "$(l10n '未发现 python3' 'python3 was not found')"
  command -v nvidia-smi >/dev/null 2>&1 || die "$(l10n '未发现 nvidia-smi；请先正确安装 NVIDIA 驱动' 'nvidia-smi was not found; install the NVIDIA driver first')"

  local driver
  driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1 | tr -d ' ')
  [[ -n "${driver}" ]] || die "$(l10n '无法读取 NVIDIA 驱动版本' 'Could not read the NVIDIA driver version')"
  if [[ "$(printf '%s\n' 580.0 "${driver}" | sort -V | head -n1)" != 580.0 ]]; then
    die "$(l10n "驱动 ${driver} 过旧；CUDA 13 容器至少需要 R580 驱动分支。安装器不会自动改驱动。" "Driver ${driver} is too old; the CUDA 13 container requires at least the R580 branch. The installer will not change the driver automatically.")"
  fi
  mapfile -t GPU_MEMORY_MIB < <(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | tr -d ' ')
  PHYSICAL_GPU_COUNT=${#GPU_MEMORY_MIB[@]}
  (( PHYSICAL_GPU_COUNT >= 1 )) || die "$(l10n '没有检测到 GPU' 'No GPU was detected')"
  (( PHYSICAL_GPU_COUNT % TP_SIZE == 0 )) || die "$(l10n "GPU 数 ${PHYSICAL_GPU_COUNT} 不能被 TP=${TP_SIZE} 整除" "GPU count ${PHYSICAL_GPU_COUNT} is not divisible by TP=${TP_SIZE}")"
  INSTANCE_COUNT=$((PHYSICAL_GPU_COUNT / TP_SIZE))
  local i min_gpu_mib="${GPU_MEMORY_MIB[0]}"
  for ((i = 1; i < PHYSICAL_GPU_COUNT; i++)); do
    (( GPU_MEMORY_MIB[i] < min_gpu_mib )) && min_gpu_mib="${GPU_MEMORY_MIB[i]}"
  done
  if (( ACTIVE_INSTANCE_COUNT == 0 )); then ACTIVE_INSTANCE_COUNT=${INSTANCE_COUNT}; fi
  (( ACTIVE_INSTANCE_COUNT >= 1 && ACTIVE_INSTANCE_COUNT <= INSTANCE_COUNT )) || die "$(l10n "active-instances 范围 1-${INSTANCE_COUNT}" "active-instances must be between 1 and ${INSTANCE_COUNT}")"
  if (( STARTUP_PARALLELISM == 0 )); then
    if (( INSTANCE_COUNT >= 2 )); then STARTUP_PARALLELISM=2; else STARTUP_PARALLELISM=1; fi
  fi
  (( STARTUP_PARALLELISM >= 1 && STARTUP_PARALLELISM <= INSTANCE_COUNT )) || die "$(l10n "startup-parallelism 范围 1-${INSTANCE_COUNT}" "startup-parallelism must be between 1 and ${INSTANCE_COUNT}")"

  local disk_available disk_probe="${MODEL_ROOT}" disk_required
  while [[ ! -e "${disk_probe}" && "${disk_probe}" != / ]]; do disk_probe=$(dirname "${disk_probe}"); done
  disk_available=$(df -PB1 "${disk_probe}" 2>/dev/null | awk 'NR==2{print $4}' || true)
  if reusable_model_at_root "${MODEL_ROOT}"; then
    disk_required=$((2 * 1024 * 1024 * 1024))
    log "$(l10n '磁盘预算按复用本地权重计算，不再预留重复下载空间。' 'Disk budgeting accounts for local-weight reuse; no duplicate download space is reserved.')"
  else
    disk_required=$((MODEL_WEIGHT_BYTES + MODEL_WEIGHT_BYTES / 3 + 10 * 1024 * 1024 * 1024))
  fi
  [[ -z "${disk_available}" ]] || (( disk_available >= disk_required )) || \
    die "$(l10n "${disk_probe} 可用空间不足；模型下载与临时文件至少需要约 $((disk_required / 1024 / 1024 / 1024)) GiB" "${disk_probe} has insufficient free space; model download and temporary files need about $((disk_required / 1024 / 1024 / 1024)) GiB")"
  log "$(l10n "宿主机复核：driver=${driver}，GPU=${PHYSICAL_GPU_COUNT}，单卡最低显存=$((min_gpu_mib / 1024))GiB，TP=${TP_SIZE}，实例=${INSTANCE_COUNT}。" "Host recheck: driver=${driver}, GPUs=${PHYSICAL_GPU_COUNT}, minimum VRAM per GPU=$((min_gpu_mib / 1024))GiB, TP=${TP_SIZE}, replicas=${INSTANCE_COUNT}.")"
}

check_ports_available() {
  command -v ss >/dev/null 2>&1 || { warn "$(l10n '缺少 ss，跳过端口占用预检。' 'ss is unavailable; skipping the port-occupancy preflight.')"; return 0; }
  local port id
  local -a ports=("${API_PORT}" "${LITELLM_DB_PORT}")
  for ((id = 0; id < INSTANCE_COUNT; id++)); do ports+=("$((WORKER_BASE_PORT + id))"); done
  for port in "${ports[@]}"; do
    if ss -H -ltn "sport = :${port}" 2>/dev/null | grep -q .; then
      die "$(l10n "TCP 端口 ${port} 已被占用；请停止冲突服务或选择其他端口" "TCP port ${port} is in use; stop the conflicting service or choose another port")"
    fi
  done
}

internet_available() {
  command -v curl >/dev/null 2>&1 || return 1
  local url
  for url in https://huggingface.co https://github.com https://ghcr.io/v2/ https://nvidia.github.io; do
    curl --noproxy '*' -sS --connect-timeout 5 --max-time 8 -o /dev/null "${url}" || return 1
  done
}

prompt_proxy_if_needed() {
  internet_available && return 0
  if [[ -z "${PROXY_URL}" ]]; then
    (( NON_INTERACTIVE )) && die "$(l10n '需要国际出口；请加 --proxy http://IP:PORT' 'International network access is required; add --proxy http://IP:PORT')"
    local ip port scheme
    printf '\n服务器当前无法直连国外资源；安装阶段需要局域网代理。\n' >&2
    read -r -p "$(l10n '代理 IP/主机名: ' 'Proxy IP/hostname: ')" ip
    read -r -p "$(l10n '代理端口: ' 'Proxy port: ')" port
    read -r -p "$(l10n '协议 [http]: ' 'Protocol [http]: ')" scheme
    scheme="${scheme:-http}"
    [[ "${ip}" =~ ^[A-Za-z0-9._:-]+$ ]] || die "$(l10n '代理主机名格式无效' 'Invalid proxy hostname')"
    [[ "${port}" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) || die "$(l10n '代理端口无效' 'Invalid proxy port')"
    [[ "${scheme}" == http || "${scheme}" == https ]] || die "$(l10n '代理协议只能是 http 或 https' 'Proxy protocol must be http or https')"
    PROXY_URL="${scheme}://${ip}:${port}"
  fi
  if (( ! ASSUME_YES && ! NON_INTERACTIVE && ! SAVE_PROXY )); then
    confirm '是否保存为以后 llmctl 维护时使用的代理？[y/N] ' 1 && SAVE_PROXY=1 || true
  fi
}

export_proxy() {
  [[ -n "${PROXY_URL}" ]] || return 0
  export HTTP_PROXY="${PROXY_URL}" HTTPS_PROXY="${PROXY_URL}"
  export http_proxy="${PROXY_URL}" https_proxy="${PROXY_URL}"
  export NO_PROXY="${PROXY_NO_PROXY}" no_proxy="${PROXY_NO_PROXY}"
}

running_container_count() {
  command -v docker >/dev/null 2>&1 || { printf '0\n'; return; }
  docker ps -q 2>/dev/null | wc -l
}

apply_docker_proxy() {
  [[ -n "${PROXY_URL}" ]] || return 0
  local running
  running=$(running_container_count)
  (( running == 0 )) || warn "$(l10n "Docker 当前有 ${running} 个容器；配置临时代理会重启 Docker daemon，请确认其重启策略。" "Docker currently has ${running} containers; applying the temporary proxy restarts the Docker daemon, so check their restart policies.")"
  install -d -m 755 "$(dirname "${INSTALL_PROXY_DROPIN}")"
  cat >"${INSTALL_PROXY_DROPIN}" <<EOF
[Service]
Environment="HTTP_PROXY=${PROXY_URL}"
Environment="HTTPS_PROXY=${PROXY_URL}"
Environment="NO_PROXY=${PROXY_NO_PROXY}"
EOF
  INSTALL_PROXY_APPLIED=1
  systemctl daemon-reload
  systemctl restart docker
}

clear_install_proxy() {
  unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy || true
  if [[ -e "${INSTALL_PROXY_DROPIN}" ]]; then
    rm -f "${INSTALL_PROXY_DROPIN}"
    systemctl daemon-reload
    systemctl restart docker 2>/dev/null || true
  fi
  INSTALL_PROXY_APPLIED=0
}

cleanup_on_exit() {
  local status=$?
  if (( INSTALL_PROXY_APPLIED )); then
    warn "$(l10n '清除安装阶段 Docker 代理。' 'Removing the installation-only Docker proxy.')"
    clear_install_proxy
  fi
  exit "${status}"
}

install_packages() {
  (( SKIP_PACKAGES )) && { log "$(l10n '按参数跳过系统包安装。' 'Skipping system package installation as requested.')"; return 0; }
  log "$(l10n '安装 Ubuntu 基础依赖与 Docker...' 'Installing Ubuntu prerequisites and Docker...')"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl gnupg jq openssl file tar python3 python3-venv
  if ! command -v docker >/dev/null 2>&1; then
    apt-get install -y --no-install-recommends docker.io
  fi
  systemctl enable --now docker

  if ! command -v nvidia-ctk >/dev/null 2>&1; then
    log "$(l10n '安装 NVIDIA Container Toolkit...' 'Installing NVIDIA Container Toolkit...')"
    install -d -m 755 /usr/share/keyrings
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
      gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
      sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update
    apt-get install -y --no-install-recommends nvidia-container-toolkit
  fi
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
}

verify_container_runtime() {
  local tool_name
  for tool_name in curl jq openssl file base64 sha256sum timeout; do
    command -v "${tool_name}" >/dev/null 2>&1 || die "$(l10n "缺少运行依赖：${tool_name}" "Missing runtime dependency: ${tool_name}")"
  done
  command -v docker >/dev/null 2>&1 || die "$(l10n 'Docker 未安装' 'Docker is not installed')"
  command -v nvidia-ctk >/dev/null 2>&1 || die "$(l10n 'NVIDIA Container Toolkit 未安装' 'NVIDIA Container Toolkit is not installed')"
  docker info >/dev/null || die "$(l10n 'Docker daemon 不可用' 'The Docker daemon is unavailable')"
}

pull_images() {
  log "$(l10n "拉取并锁定 vLLM 镜像：${VLLM_IMAGE}" "Pulling and pinning the vLLM image: ${VLLM_IMAGE}")"
  docker pull "${VLLM_IMAGE}"
  log "$(l10n "拉取并锁定 LiteLLM 镜像：${LITELLM_IMAGE}" "Pulling and pinning the LiteLLM image: ${LITELLM_IMAGE}")"
  docker pull "${LITELLM_IMAGE}"
  log "$(l10n "拉取 PostgreSQL 镜像：${POSTGRES_IMAGE}" "Pulling the PostgreSQL image: ${POSTGRES_IMAGE}")"
  docker pull "${POSTGRES_IMAGE}"
}

verify_gpu_in_container() {
  local expected="${PHYSICAL_GPU_COUNT}" actual
  actual=$(docker run --rm --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=all \
    --entrypoint python3 "${VLLM_IMAGE}" -c 'import torch; print(torch.cuda.device_count())')
  [[ "${actual}" == "${expected}" ]] || die "$(l10n "容器只看到 ${actual} 张 GPU，宿主机看到 ${expected} 张" "The container sees ${actual} GPUs while the host sees ${expected}")"
  log "$(l10n "NVIDIA 容器运行时验证通过：${actual} 张 GPU。" "NVIDIA container runtime validation passed: ${actual} GPUs.")"
}

verify_model_architecture_in_image() {
  log "$(l10n "在固定 vLLM 镜像内核验架构：${MODEL_ARCHITECTURE}" "Verifying architecture in the pinned vLLM image: ${MODEL_ARCHITECTURE}")"
  docker run --rm --entrypoint python3 "${VLLM_IMAGE}" -c '
import sys
from vllm.model_executor.models import ModelRegistry
arch = sys.argv[1]
supported = set(ModelRegistry.get_supported_archs())
if arch not in supported:
    raise SystemExit(f"unsupported architecture in pinned vLLM image: {arch}")
print(arch)
' "${MODEL_ARCHITECTURE}" >/dev/null || \
    die "$(l10n "固定镜像 ${VLLM_IMAGE} 不支持 ${MODEL_ARCHITECTURE}；已在下载大权重前停止" "Pinned image ${VLLM_IMAGE} does not support ${MODEL_ARCHITECTURE}; stopped before downloading large weights")"
}

ensure_modelscope_downloader() {
  local venv="/opt/llm-cluster/hub-venv"
  if [[ ! -x "${venv}/bin/ms-hub" ]]; then
    log "$(l10n '在独立维护环境安装 ModelScope 下载器 modelscope-hub==0.1.8...' 'Installing modelscope-hub==0.1.8 in an isolated maintenance environment...')" >&2
    python3 -m venv "${venv}" || die "$(l10n "无法创建 ${venv}；请安装 python3-venv" "Could not create ${venv}; install python3-venv")"
    "${venv}/bin/pip" install --disable-pip-version-check "modelscope-hub==0.1.8" || die "$(l10n 'ModelScope 下载器安装失败' 'ModelScope downloader installation failed')"
  fi
  printf '%s\n' "${venv}/bin/ms-hub"
}

validate_downloaded_model() {
  local directory="${1:?}" bytes weights expected_min
  [[ -r "${directory}/config.json" ]] || die "$(l10n '模型缺少 config.json' 'The model is missing config.json')"
  python3 - "${directory}/config.json" "${MODEL_ARCHITECTURE}" <<'PY' || \
    die "$(l10n '本地模型 config.json 的架构与所选模型不匹配' 'The local model config.json architecture does not match the selected model')"
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)
architectures = config.get("architectures") or (config.get("text_config") or {}).get("architectures") or []
if sys.argv[2] not in architectures:
    raise SystemExit(1)
PY
  weights=$(find "${directory}" -maxdepth 2 -type f \( -name '*.safetensors' -o -name 'pytorch_model*.bin' -o -name 'model*.bin' \) | wc -l)
  (( weights >= 1 )) || die "$(l10n '模型目录中没有完整权重文件' 'The model directory contains no complete weight files')"
  bytes=$(directory_size_bytes "${directory}")
  expected_min=$((MODEL_WEIGHT_BYTES * 7 / 10))
  (( bytes >= expected_min )) || die "$(l10n "模型文件体积异常：实际 ${bytes}，目录元数据预期 ${MODEL_WEIGHT_BYTES}" "Model size is abnormal: actual ${bytes}, catalog expectation ${MODEL_WEIGHT_BYTES}")"
}

download_model() {
  local safe_name target partial current_link reusable_target=""
  safe_name="${MODEL_ID//\//--}-${MODEL_REVISION:0:12}"
  target="${MODEL_ROOT}/${safe_name}"
  partial="${target}.partial"
  current_link="${MODEL_ROOT}/current"
  install -d -m 755 "${MODEL_ROOT}"
  cat >"${MODEL_ROOT}/.llm-cluster-model-root" <<EOF
managed_by=install-llm-cluster.sh
created_at=$(date -u +%FT%TZ)
EOF
  reusable_target=$(reusable_model_directory "${MODEL_ROOT}" || true)
  if (( SKIP_DOWNLOAD )); then
    [[ -r "${current_link}/config.json" ]] || die "$(l10n "--skip-download 需要 ${current_link}/config.json 已存在" "--skip-download requires ${current_link}/config.json")"
    model_manifest_matches_selection "${MODEL_ROOT}" || \
      die "$(l10n '--skip-download 的本地模型 Hub、MODEL_ID 或 revision 与安装选择不一致' 'The local model Hub, MODEL_ID, or revision does not match the --skip-download selection')"
    [[ -n "${reusable_target}" ]] || die "$(l10n '--skip-download 的 current 链接未指向可核验的所选模型目录' 'The current symlink does not point to a verifiable directory for the selected model')"
    target="${reusable_target}"
    validate_downloaded_model "${current_link}"
    log "$(l10n "跳过模型下载，使用现有 ${current_link}。" "Skipping model download and using ${current_link}.")"
  fi
  if [[ -n "${reusable_target}" ]]; then
    target="${reusable_target}"
    validate_downloaded_model "${target}"
    log "$(l10n "已验证本地模型 ${target}，身份与 revision 匹配；跳过下载且不复制权重。" "Validated local model ${target}; identity and revision match, so download and weight copying are skipped.")"
  elif (( SKIP_DOWNLOAD )); then
    die "$(l10n '未找到可安全复用的所选模型' 'No safely reusable copy of the selected model was found')"
  elif [[ -e "${target}" ]]; then
    die "$(l10n "本地目录 ${target} 已存在，但缺少匹配清单或完整权重；为防止混用模型，拒绝覆盖。" "Local directory ${target} exists but lacks a matching manifest or complete weights; refusing to overwrite it to prevent model mix-ups.")"
  else
    log "$(l10n "从 ${MODEL_HUB} 下载 ${MODEL_ID}@${MODEL_REVISION}（目录权重约 $((MODEL_WEIGHT_BYTES / 1024 / 1024 / 1024))GiB，支持断点续传）..." "Downloading ${MODEL_ID}@${MODEL_REVISION} from ${MODEL_HUB} (about $((MODEL_WEIGHT_BYTES / 1024 / 1024 / 1024))GiB of weights, resumable)...")"
    install -d -m 755 "${partial}"
    if [[ "${MODEL_HUB}" == huggingface ]]; then
      local -a proxy_args=() token_args=()
      if [[ -n "${PROXY_URL}" ]]; then
        proxy_args+=(
          -e "HTTP_PROXY=${PROXY_URL}" -e "HTTPS_PROXY=${PROXY_URL}"
          -e "NO_PROXY=${PROXY_NO_PROXY}"
        )
      fi
      [[ -z "${HF_TOKEN:-}" ]] || token_args+=(-e "HF_TOKEN=${HF_TOKEN}")
      docker run --rm --network host \
        -v "${MODEL_ROOT}:/models" \
        -e "LLM_MODEL_ID=${MODEL_ID}" \
        -e "LLM_MODEL_REVISION=${MODEL_REVISION}" \
        -e "LLM_MODEL_LOCAL=/models/$(basename "${partial}")" \
        "${proxy_args[@]}" "${token_args[@]}" \
        --entrypoint python3 "${VLLM_IMAGE}" -c '
import os
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id=os.environ["LLM_MODEL_ID"],
    revision=os.environ["LLM_MODEL_REVISION"],
    local_dir=os.environ["LLM_MODEL_LOCAL"],
)
' || die "$(l10n "Hugging Face 下载失败；${partial} 已保留，下次可续传" "Hugging Face download failed; ${partial} was retained for a later resume")"
    else
      local ms_hub
      ms_hub=$(ensure_modelscope_downloader)
      "${ms_hub}" download "${MODEL_ID}" --revision "${MODEL_REVISION}" \
        --local-dir "${partial}" --max-workers 8 || \
        die "$(l10n "ModelScope 下载失败；${partial} 已保留，下次可续传" "ModelScope download failed; ${partial} was retained for a later resume")"
    fi
    validate_downloaded_model "${partial}"
    mv "${partial}" "${target}"
  fi
  validate_downloaded_model "${target}"
  ln -sfn "$(basename "${target}")" "${current_link}"
  cat >"${MODEL_ROOT}/current.manifest" <<EOF
MODEL_ID=${MODEL_ID}
MODEL_REVISION=${MODEL_REVISION}
MODEL_HUB=${MODEL_HUB}
MODEL_ARCHITECTURE=${MODEL_ARCHITECTURE}
LOCAL_DIR=${target}
INSTALLED_AT=$(date -u +%FT%TZ)
CONFIG_SHA256=$(sha256sum "${target}/config.json" | awk '{print $1}')
EOF
  log "$(l10n "模型已固定：current -> $(basename "${target}")" "Model pinned: current -> $(basename "${target}")")"
}

make_active_workers() {
  local i out=""
  for ((i = 0; i < ACTIVE_INSTANCE_COUNT; i++)); do out+="${out:+,}${i}"; done
  printf '%s\n' "${out}"
}

write_configuration() {
  if [[ -e "${CLUSTER_ENV}" && ${FORCE_RECONFIGURE} -eq 0 ]]; then
    die "$(l10n "已存在 ${CLUSTER_ENV}；如要重配，请先备份并使用 --force-reconfigure" "${CLUSTER_ENV} already exists; back it up and use --force-reconfigure to replace it")"
  fi
  install -d -m 750 "${CONFIG_DIR}" "${WORKER_ENV_DIR}"
  install -d -m 755 "${STATE_DIR}" "${MODEL_ROOT}"
  local active_workers
  active_workers=$(make_active_workers)
  cat >"${CLUSTER_ENV}" <<EOF
# Generated by install-llm-cluster.sh ${INSTALLER_VERSION}
MODEL_SOURCE=${MODEL_SOURCE}
MODEL_HUB=${MODEL_HUB}
MODEL_ID=${MODEL_ID}
MODEL_REVISION=${MODEL_REVISION}
SERVED_MODEL_NAME=${SERVED_MODEL_NAME}
MODEL_ROOT=${MODEL_ROOT}
MODEL_ARCHITECTURE=${MODEL_ARCHITECTURE}
MODEL_TASK=${MODEL_TASK}
MODEL_PRECISION=${MODEL_PRECISION}
MODEL_WEIGHT_BYTES=${MODEL_WEIGHT_BYTES}
MODEL_PARAMS=${MODEL_PARAMS}
MODEL_NATIVE_CONTEXT=${MODEL_NATIVE_CONTEXT}
TOOL_CALL_PARSER=${TOOL_CALL_PARSER}
REASONING_PARSER=${REASONING_PARSER}
SUPPORTS_IMAGE_INPUT=${SUPPORTS_IMAGE_INPUT}
SUPPORTS_OCR=${SUPPORTS_OCR}
SUPPORTS_TOOL_CALLING=${SUPPORTS_TOOL_CALLING}
SUPPORTS_REASONING=${SUPPORTS_REASONING}
SUPPORTS_THINKING_TOGGLE=${SUPPORTS_THINKING_TOGGLE}
TRUST_REMOTE_CODE=${TRUST_REMOTE_CODE}
VLLM_IMAGE=${VLLM_IMAGE}
LITELLM_IMAGE=${LITELLM_IMAGE}
POSTGRES_IMAGE=${POSTGRES_IMAGE}
PHYSICAL_GPU_COUNT=${PHYSICAL_GPU_COUNT}
TP_SIZE=${TP_SIZE}
INSTANCE_COUNT=${INSTANCE_COUNT}
ACTIVE_WORKERS=${active_workers}
WORKER_BASE_PORT=${WORKER_BASE_PORT}
API_BIND=${API_BIND}
API_PORT=${API_PORT}
LITELLM_DB_PORT=${LITELLM_DB_PORT}
MAX_MODEL_LEN=${MAX_MODEL_LEN}
MAX_NUM_SEQS=${MAX_NUM_SEQS}
ESTIMATED_MAX_NUM_SEQS=${ESTIMATED_MAX_NUM_SEQS}
MAX_NUM_BATCHED_TOKENS=${MAX_NUM_BATCHED_TOKENS}
GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION}
MM_LIMIT='${MM_LIMIT}'
ROUTING_STRATEGY=${ROUTING_STRATEGY}
START_TIMEOUT=${START_TIMEOUT}
STARTUP_PARALLELISM=${STARTUP_PARALLELISM}
INTERFACE_LANGUAGE=${INTERFACE_LANGUAGE}
EOF
  chmod 640 "${CLUSTER_ENV}"
  chown root:root "${CLUSTER_ENV}"

  local requested_ui_username="${UI_USERNAME}" requested_ui_password="${UI_PASSWORD}"
  local existing_litellm_key="" existing_backend_key="" existing_salt_key=""
  local existing_ui_username="" existing_ui_password="" existing_postgres_password=""
  local secrets_source=""
  if [[ -r "${SECRETS_ENV}" ]]; then
    secrets_source="${SECRETS_ENV}"
  elif [[ -r "${RETAINED_SECRETS}" ]] && docker volume inspect llm-cluster-litellm-postgres >/dev/null 2>&1; then
    secrets_source="${RETAINED_SECRETS}"
    log "$(l10n '检测到保留的 LiteLLM 数据库和恢复凭据，将继续使用原管理数据。' 'Found a retained LiteLLM database and recovery credentials; reusing the existing administration data.')"
  fi
  if [[ -n "${secrets_source}" ]]; then
    # shellcheck disable=SC1090
    source "${secrets_source}"
    existing_litellm_key="${LITELLM_MASTER_KEY:-}"
    existing_backend_key="${BACKEND_API_KEY:-}"
    existing_salt_key="${LITELLM_SALT_KEY:-}"
    existing_ui_username="${UI_USERNAME:-}"
    existing_ui_password="${UI_PASSWORD:-}"
    existing_postgres_password="${POSTGRES_PASSWORD:-}"
  fi
  local postgres_user="llmadmin" postgres_db="litellm" postgres_password
  local final_ui_username final_ui_password
  postgres_password="${existing_postgres_password:-$(openssl rand -hex 32)}"
  if (( UI_USERNAME_EXPLICIT )); then final_ui_username="${requested_ui_username}"; else final_ui_username="${existing_ui_username:-${requested_ui_username}}"; fi
  if (( UI_PASSWORD_EXPLICIT )); then final_ui_password="${requested_ui_password}"; else final_ui_password="${existing_ui_password:-${requested_ui_password}}"; fi
  umask 077
  cat >"${SECRETS_ENV}" <<EOF
LITELLM_MASTER_KEY=${existing_litellm_key:-sk-$(openssl rand -hex 32)}
BACKEND_API_KEY=${existing_backend_key:-sk-backend-$(openssl rand -hex 32)}
LITELLM_SALT_KEY=${existing_salt_key:-sk-salt-$(openssl rand -hex 32)}
UI_USERNAME=${final_ui_username}
UI_PASSWORD=${final_ui_password}
POSTGRES_USER=${postgres_user}
POSTGRES_PASSWORD=${postgres_password}
POSTGRES_DB=${postgres_db}
DATABASE_URL=postgresql://${postgres_user}:${postgres_password}@127.0.0.1:${LITELLM_DB_PORT}/${postgres_db}
EOF
  chmod 600 "${SECRETS_ENV}"
  chown root:root "${SECRETS_ENV}"
  if [[ "${secrets_source}" == "${RETAINED_SECRETS}" ]]; then
    rm -f "${RETAINED_SECRETS}"
  fi

  local i start gpu_devices
  rm -f "${WORKER_ENV_DIR}"/*.env
  for ((i = 0; i < INSTANCE_COUNT; i++)); do
    start=$((i * TP_SIZE))
    gpu_devices=""
    local j
    for ((j = 0; j < TP_SIZE; j++)); do gpu_devices+="${gpu_devices:+,}$((start + j))"; done
    cat >"${WORKER_ENV_DIR}/${i}.env" <<EOF
GPU_DEVICES=${gpu_devices}
WORKER_PORT=$((WORKER_BASE_PORT + i))
EOF
    chmod 640 "${WORKER_ENV_DIR}/${i}.env"
  done

  if (( SAVE_PROXY )) && [[ -n "${PROXY_URL}" ]]; then
    umask 077
    cat >"${PROXY_ENV}" <<EOF
MAINTENANCE_PROXY=${PROXY_URL}
MAINTENANCE_NO_PROXY=${PROXY_NO_PROXY}
EOF
    chmod 600 "${PROXY_ENV}"
  elif [[ -e "${PROXY_ENV}" && ${FORCE_RECONFIGURE} -eq 1 ]]; then
    rm -f "${PROXY_ENV}"
  fi
}

install_manager() {
  install -m 755 "${MANAGER_SOURCE}" /usr/local/sbin/llmctl
  install -d -m 755 /usr/local/lib/llm-cluster
  install -m 755 "${CATALOG_SOURCE}" /usr/local/lib/llm-cluster/model_catalog.py
}

write_systemd_units() {
  cat >/etc/systemd/system/llm-worker@.service <<'EOF'
[Unit]
Description=vLLM model worker instance %i
After=docker.service network-online.target
Requires=docker.service
PartOf=llm-cluster.service
StartLimitIntervalSec=600
StartLimitBurst=3

[Service]
Type=simple
EnvironmentFile=/etc/llm-cluster/cluster.env
EnvironmentFile=/etc/llm-cluster/secrets.env
EnvironmentFile=/etc/llm-cluster/workers/%i.env
ExecStartPre=-/usr/bin/docker rm -f llm-worker-%i
ExecStart=/usr/local/sbin/llmctl _worker-start %i
ExecStop=-/usr/bin/docker stop --time 90 llm-worker-%i
Restart=on-failure
RestartSec=10
TimeoutStartSec=infinity
TimeoutStopSec=120

[Install]
WantedBy=llm-cluster.service
EOF

  cat >/etc/systemd/system/llm-database.service <<'EOF'
[Unit]
Description=PostgreSQL database for LiteLLM Admin UI
After=docker.service network-online.target
Requires=docker.service
PartOf=llm-cluster.service
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
EnvironmentFile=/etc/llm-cluster/cluster.env
EnvironmentFile=/etc/llm-cluster/secrets.env
ExecStartPre=-/usr/bin/docker rm -f llm-database
ExecStartPre=/usr/bin/docker volume create llm-cluster-litellm-postgres
ExecStart=/usr/bin/docker run --rm --name llm-database --network bridge --env-file /etc/llm-cluster/secrets.env -p 127.0.0.1:${LITELLM_DB_PORT}:5432 -v llm-cluster-litellm-postgres:/var/lib/postgresql/data ${POSTGRES_IMAGE}
ExecStop=-/usr/bin/docker stop --time 60 llm-database
Restart=on-failure
RestartSec=5
TimeoutStartSec=120
TimeoutStopSec=90
NoNewPrivileges=true

[Install]
WantedBy=llm-cluster.service
EOF

  cat >/etc/systemd/system/llm-router.service <<'EOF'
[Unit]
Description=LiteLLM router for local vLLM cluster
After=docker.service network-online.target llm-database.service
PartOf=llm-cluster.service
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
EnvironmentFile=/etc/llm-cluster/cluster.env
EnvironmentFile=/etc/llm-cluster/secrets.env
ExecStartPre=-/usr/bin/docker rm -f llm-router
ExecStartPre=/bin/bash -c 'for i in {1..60}; do /usr/bin/docker exec llm-database pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" && exit 0; sleep 2; done; exit 1'
ExecStart=/usr/bin/docker run --rm --name llm-router --network host --env-file /etc/llm-cluster/secrets.env -v /etc/llm-cluster/litellm.yaml:/app/config.yaml:ro ${LITELLM_IMAGE} --config /app/config.yaml --host ${API_BIND} --port ${API_PORT}
ExecStop=-/usr/bin/docker stop --time 30 llm-router
Restart=on-failure
RestartSec=5
TimeoutStartSec=120
TimeoutStopSec=60
NoNewPrivileges=true

[Install]
WantedBy=llm-cluster.service
EOF

  cat >/etc/systemd/system/llm-cluster.service <<'EOF'
[Unit]
Description=Hardware-aware local LLM inference cluster
After=docker.service network-online.target llm-database.service
Requires=docker.service
Wants=network-online.target llm-database.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/llmctl _boot-start
ExecStop=/usr/local/sbin/llmctl _boot-stop
TimeoutStartSec=infinity
TimeoutStopSec=900

[Install]
WantedBy=multi-user.target
EOF
  chmod 644 /etc/systemd/system/llm-worker@.service /etc/systemd/system/llm-database.service /etc/systemd/system/llm-router.service /etc/systemd/system/llm-cluster.service
  systemctl daemon-reload
}

prepare_worker_cache() {
  install -d -m 755 "${STATE_DIR}/cache/shared"
}

start_cluster_with_progress() {
  # `systemctl start` normally blocks while the oneshot controller waits for
  # every large model load, but it does not stream the service journal to the
  # caller. Start asynchronously and provide visible progress instead.
  systemctl start --no-block llm-cluster.service
  local timeout
  timeout=$((START_TIMEOUT * ((ACTIVE_INSTANCE_COUNT + STARTUP_PARALLELISM - 1) / STARTUP_PARALLELISM) + 300))
  /usr/local/sbin/llmctl startup watch --timeout "${timeout}" --interval 10
}

print_summary() {
  # shellcheck disable=SC1090
  source "${SECRETS_ENV}"
  local host_for_url="${API_BIND}"
  [[ "${host_for_url}" == 0.0.0.0 || "${host_for_url}" == :: ]] && host_for_url="$(l10n '<服务器IP>' '<server-IP>')"
  if [[ "${INTERFACE_LANGUAGE}" == en ]]; then
    cat <<EOF

Installation complete
=====================
Model:             ${MODEL_ID}@${MODEL_REVISION}
Source/arch:       ${MODEL_HUB} / ${MODEL_ARCHITECTURE} / ${MODEL_PRECISION}
Model directory:   ${MODEL_ROOT}
Image:             ${VLLM_IMAGE}
Topology:          ${PHYSICAL_GPU_COUNT} GPUs / TP=${TP_SIZE} / ${INSTANCE_COUNT} replicas
Concurrency:       max-num-seqs=${MAX_NUM_SEQS} per replica; $((INSTANCE_COUNT * MAX_NUM_SEQS)) total scheduling slots
Image input:       $([[ ${SUPPORTS_IMAGE_INPUT} -eq 1 ]] && printf 'supported, up to %s images/request' "$(jq -r '.image' <<<"${MM_LIMIT}")" || printf 'not declared by this model')
Tool calling:      $([[ ${SUPPORTS_TOOL_CALLING} -eq 1 ]] && printf 'supported (parser=%s)' "${TOOL_CALL_PARSER}" || printf 'disabled')
Reasoning parser:  $([[ ${SUPPORTS_REASONING} -eq 1 ]] && printf 'supported (parser=%s)' "${REASONING_PARSER}" || printf 'disabled')
Boot activation:   first ${ACTIVE_INSTANCE_COUNT} replicas
Startup parallel:  up to ${STARTUP_PARALLELISM} workers per batch
Load balancing:    LiteLLM ${ROUTING_STRATEGY}
API:               http://${host_for_url}:${API_PORT}/v1
Web UI:            http://${host_for_url}:${API_PORT}/ui
Administrator:     ${UI_USERNAME}
Initial password:  ${UI_PASSWORD}
Served model:      ${SERVED_MODEL_NAME}
API key:           ${LITELLM_MASTER_KEY}

Common commands:
  sudo llmctl status
  sudo llmctl health
  sudo llmctl logs worker 0 -f
  sudo llmctl smoke --full
  sudo llmctl restart all
  sudo llmctl scale ${ACTIVE_INSTANCE_COUNT}
  sudo llmctl key show
  sudo llmctl admin show
  sudo llmctl admin set-password 'new-strong-password'

Only capabilities shown as supported are added to vLLM arguments and smoke tests.
If reasoning can be disabled, add "reasoning_effort":"none" to Chat Completions JSON.
Vision requests use messages[].content.image_url; data:base64 is allowed by default to limit SSRF exposure.

Note: ${INSTANCE_COUNT}×${MAX_NUM_SEQS} is a scheduling-slot count, not ${INSTANCE_COUNT}×${MAX_NUM_SEQS} simultaneous 256K requests.
LiteLLM least-busy routes by unfinished request count; it does not read GPU VRAM/KV cache.
Change the shared initial Web UI password immediately with llmctl admin set-password.
EOF
    return
  fi
  cat <<EOF

安装完成
========
模型：       ${MODEL_ID}@${MODEL_REVISION}
来源/架构：  ${MODEL_HUB} / ${MODEL_ARCHITECTURE} / ${MODEL_PRECISION}
模型目录：   ${MODEL_ROOT}
镜像：       ${VLLM_IMAGE}
拓扑：       ${PHYSICAL_GPU_COUNT} GPU / TP=${TP_SIZE} / ${INSTANCE_COUNT} 实例
并发参数：   每实例 max-num-seqs=${MAX_NUM_SEQS}，总调度槽上限=$((INSTANCE_COUNT * MAX_NUM_SEQS))
图片能力：   $([[ ${SUPPORTS_IMAGE_INPUT} -eq 1 ]] && printf '支持，每请求上限 %s 张' "$(jq -r '.image' <<<"${MM_LIMIT}")" || printf '模型未声明图片输入能力')
工具调用：   $([[ ${SUPPORTS_TOOL_CALLING} -eq 1 ]] && printf '支持（parser=%s）' "${TOOL_CALL_PARSER}" || printf '未启用')
思考解析：   $([[ ${SUPPORTS_REASONING} -eq 1 ]] && printf '支持（parser=%s）' "${REASONING_PARSER}" || printf '未启用')
开机激活：   前 ${ACTIVE_INSTANCE_COUNT} 个实例
启动并行度： 每批 ${STARTUP_PARALLELISM} 个 Worker
负载均衡：   LiteLLM ${ROUTING_STRATEGY}
API：        http://${host_for_url}:${API_PORT}/v1
Web UI：     http://${host_for_url}:${API_PORT}/ui
管理员：     ${UI_USERNAME}
初始密码：   ${UI_PASSWORD}
模型名：     ${SERVED_MODEL_NAME}
API key：    ${LITELLM_MASTER_KEY}

常用命令：
  sudo llmctl status
  sudo llmctl health
  sudo llmctl logs worker 0 -f
  sudo llmctl smoke --full
  sudo llmctl restart all
  sudo llmctl scale ${ACTIVE_INSTANCE_COUNT}
  sudo llmctl key show
  sudo llmctl admin show
  sudo llmctl admin set-password '新的强密码'

能力说明：只有上方标为支持的能力才会加入 vLLM 启动参数并参与冒烟测试。
若模型支持可关闭思考，可在 Chat Completions JSON 加 "reasoning_effort":"none"。
图片模型使用 messages[].content.image_url；默认只接受 data:base64，避免外部 URL SSRF。

注意：${INSTANCE_COUNT}×${MAX_NUM_SEQS} 是调度槽，不表示所有请求都能同时使用 256K。
LiteLLM least-busy 按未完成请求数路由，不读取 GPU 显存/KV Cache。
Web UI 使用通用初始密码；首次登录后请立即通过 llmctl admin set-password 修改。
EOF
}

preflight_existing_install() {
  [[ ${EUID} -eq 0 ]] || die "$(l10n '请使用 sudo 运行安装器' 'Run the installer with sudo')"
  if [[ -r "${LEGACY_CONFIG_DIR}/cluster.env" && ! -r "${CLUSTER_ENV}" ]]; then
    die "$(l10n "检测到旧版 Ornith 集群 ${LEGACY_CONFIG_DIR}。为避免两套服务抢占 GPU/端口，本版拒绝并行覆盖；请先保留现场并使用旧 llmctl 停服/备份，待执行显式迁移。" "A legacy Ornith cluster was found at ${LEGACY_CONFIG_DIR}. To prevent GPU/port conflicts, this installer will not overwrite it in place; retain the system state and stop/back up the old cluster before an explicit migration.")"
  fi
  [[ -e "${CLUSTER_ENV}" ]] || return 0
  (( FORCE_RECONFIGURE )) || die "$(l10n '检测到已有 LLM 集群配置；请使用 llmctl 管理，或备份后加 --force-reconfigure 重配' 'An existing LLM cluster configuration was found; manage it with llmctl or back it up and add --force-reconfigure')"
  warn "$(l10n '将重配现有集群；先停止服务，模型文件不会自动删除。' 'Reconfiguring the existing cluster; services will be stopped first and model files will not be deleted automatically.')"
  systemctl stop llm-cluster.service 2>/dev/null || true
}

main() {
  preparse_language "$@"
  parse_args "$@"
  select_language_interactively
  preflight_existing_install
  check_discovery_host
  select_model_interactively
  apply_model_source_defaults
  select_topology_interactively
  select_storage_interactively
  validate_scalar_config
  check_host
  check_ports_available

  if (( ! ASSUME_YES && ! NON_INTERACTIVE && ! ACTIVE_COUNT_EXPLICIT )); then
    local active_answer
    read -r -p "$(l10n "安装后激活多少个实例 [${INSTANCE_COUNT}]: " "How many replicas should be active after installation [${INSTANCE_COUNT}]: ")" active_answer
    ACTIVE_INSTANCE_COUNT="${active_answer:-${INSTANCE_COUNT}}"
    (( ACTIVE_INSTANCE_COUNT >= 1 && ACTIVE_INSTANCE_COUNT <= INSTANCE_COUNT )) || die "$(l10n '激活实例数量超出范围' 'Active replica count is out of range')"
  fi

  if (( ! ASSUME_YES && ! NON_INTERACTIVE && ! STARTUP_PARALLELISM_EXPLICIT )); then
    local parallel_answer
    if [[ "${INTERFACE_LANGUAGE}" == en ]]; then
      cat >&2 <<EOF

Concurrent worker loads per batch (range 1-${INSTANCE_COUNT}):
  The host plan recommends ${STARTUP_PARALLELISM}. Higher values start faster but increase peak CPU, memory, and disk reads.
  Enter ${INSTANCE_COUNT} to start all workers concurrently under the current topology.
EOF
    else
      cat >&2 <<EOF

每批并行启动多少个 Worker（范围 1-${INSTANCE_COUNT}）：
  本机规划推荐 ${STARTUP_PARALLELISM}；更高值启动更快，但 CPU、内存和磁盘读取峰值更高。
  填 ${INSTANCE_COUNT} 表示当前拓扑下全部 Worker 同时开始加载。
EOF
    fi
    read -r -p "$(l10n "并行启动数 [${STARTUP_PARALLELISM}]: " "Startup parallelism [${STARTUP_PARALLELISM}]: ")" parallel_answer
    STARTUP_PARALLELISM="${parallel_answer:-${STARTUP_PARALLELISM}}"
    [[ "${STARTUP_PARALLELISM}" =~ ^[0-9]+$ ]] && (( STARTUP_PARALLELISM >= 1 && STARTUP_PARALLELISM <= INSTANCE_COUNT )) || die "$(l10n "并行启动数必须在 1-${INSTANCE_COUNT}" "Startup parallelism must be between 1 and ${INSTANCE_COUNT}")"
  fi

  log "$(l10n "部署计划：${MODEL_ID}，TP=${TP_SIZE}，实例=${INSTANCE_COUNT}，max-num-seqs=${MAX_NUM_SEQS}，每批并行启动=${STARTUP_PARALLELISM}。" "Deployment plan: ${MODEL_ID}, TP=${TP_SIZE}, replicas=${INSTANCE_COUNT}, max-num-seqs=${MAX_NUM_SEQS}, startup parallelism=${STARTUP_PARALLELISM}.")"
  (( TRUST_REMOTE_CODE == 0 )) || warn "$(l10n '模型配置要求 trust_remote_code；将执行固定 revision 中的仓库代码，请确认来源已审核。' 'The model requires trust_remote_code; code from the pinned revision will run, so confirm that its source has been reviewed.')"
  if (( ! ASSUME_YES && ! NON_INTERACTIVE )); then
    confirm "$(l10n '继续安装？[Y/n] ' 'Continue installation? [Y/n] ')" 0 || { log "$(l10n '已取消。' 'Cancelled.')"; exit 0; }
  fi

  prompt_proxy_if_needed
  export_proxy
  trap cleanup_on_exit EXIT
  install_packages
  verify_container_runtime
  apply_docker_proxy
  pull_images
  verify_gpu_in_container
  verify_model_architecture_in_image
  download_model

  # Runtime must not inherit an international proxy. A saved proxy is read only
  # by explicit llmctl maintenance operations.
  clear_install_proxy
  trap - EXIT

  write_configuration
  install_manager
  prepare_worker_cache
  write_systemd_units

  systemctl enable llm-cluster.service
  if (( NO_START )); then
    log "$(l10n '按 --no-start 未启动；稍后运行 sudo systemctl start llm-cluster。' 'Services were not started because of --no-start; run sudo systemctl start llm-cluster later.')"
  else
    if ! start_cluster_with_progress; then
      systemctl disable llm-cluster.service 2>/dev/null || true
      /usr/local/sbin/llmctl shutdown --timeout 180 || true
      die "$(l10n '集群启动失败；已取消开机自启，请查看 journalctl -u llm-cluster' 'Cluster startup failed and boot activation was disabled; inspect journalctl -u llm-cluster')"
    fi
    if ! /usr/local/sbin/llmctl health || ! /usr/local/sbin/llmctl smoke --full; then
      systemctl disable llm-cluster.service 2>/dev/null || true
      /usr/local/sbin/llmctl shutdown --timeout 180 || true
      die "$(l10n '部署验收失败；服务已停止且取消自启，请检查 llmctl logs' 'Deployment acceptance checks failed; services were stopped and boot activation disabled. Inspect llmctl logs.')"
    fi
  fi
  print_summary
}

if [[ "${INSTALLER_SOURCE_ONLY:-0}" != 1 ]]; then
  main "$@"
fi

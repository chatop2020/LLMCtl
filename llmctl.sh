#!/usr/bin/env bash
# 面向 Ubuntu 24.04 与 NVIDIA GPU 的硬件感知 vLLM 集群管理器。
# 安装脚本会把本文件安装为 /usr/local/sbin/llmctl。

set -Eeuo pipefail
IFS=$'\n\t'

readonly CTL_VERSION="3.6.12"
readonly CONFIG_DIR="${LLM_CLUSTER_CONFIG_DIR:-/etc/llm-cluster}"
readonly STATE_DIR="${LLM_CLUSTER_STATE_DIR:-/var/lib/llm-cluster}"
readonly CACHE_DIR="${STATE_DIR}/cache"
readonly RETAINED_SECRETS="${STATE_DIR}/retained-secrets.env"
readonly CLUSTER_ENV="${CONFIG_DIR}/cluster.env"
readonly SECRETS_ENV="${CONFIG_DIR}/secrets.env"
readonly PROXY_ENV="${CONFIG_DIR}/proxy.env"
readonly RUNTIME_PROXY_ENV="${CONFIG_DIR}/runtime-proxy.env"
readonly LITELLM_CONFIG="${CONFIG_DIR}/litellm.yaml"
readonly BIFROST_DIR="${CONFIG_DIR}/bifrost"
readonly BIFROST_CONFIG="${BIFROST_DIR}/config.json"
readonly NEWAPI_PLAN="${CONFIG_DIR}/newapi-plan.json"
readonly OMNIROUTE_PLAN="${CONFIG_DIR}/omniroute-plan.json"
readonly OMNIROUTE_SQLITE="${STATE_DIR}/omniroute/gateway/storage.sqlite"
readonly OMNIROUTE_MAINTENANCE_STATE_DIR="${STATE_DIR}/omniroute-maintenance"
readonly OMNIROUTE_MAINTENANCE_BACKUP_DIR="${LLM_OMNIROUTE_BACKUPS_DIR:-/var/backups/llmctl/omniroute}"
readonly ACCOUNT_SQLITE="${STATE_DIR}/omniroute/portal/account-portal.db"
readonly ACCOUNT_HELPER="${LLM_ACCOUNT_HELPER:-/usr/local/lib/llm-cluster/account_portal.py}"
readonly ACCOUNT_MYSQL_VENV="${STATE_DIR}/omniroute/portal/mysql-venv"
readonly ACCOUNT_MYSQL_CONFIG_DIR="${STATE_DIR}/omniroute/portal/Config"
readonly ACCOUNT_MYSQL_CAPABILITY="${ACCOUNT_MYSQL_CONFIG_DIR}/mysql-capability.json"
readonly ACCOUNT_DATABASE_CONFIG="${ACCOUNT_MYSQL_CONFIG_DIR}/database.json"
readonly ACCOUNT_DATABASE_MIGRATION="${ACCOUNT_MYSQL_CONFIG_DIR}/database-migration.json"
readonly ACCOUNT_MYSQL_DROPIN_DIR="/etc/systemd/system/llm-account.service.d"
readonly ACCOUNT_MYSQL_DROPIN="${ACCOUNT_MYSQL_DROPIN_DIR}/50-llmctl-mysql.conf"
readonly CONTROL_PLANE_UPDATER="${LLM_CONTROL_PLANE_UPDATER:-/usr/local/lib/llm-cluster/upgrade-llmctl.sh}"
readonly CONTROL_PLANE_RELEASE="${LLM_CONTROL_PLANE_RELEASE:-/var/lib/llm-cluster/control-plane-version.env}"
readonly DOCKER_PROXY_DROPIN="/etc/systemd/system/docker.service.d/90-llm-cluster-temporary-proxy.conf"
readonly CATALOG_HELPER="${LLM_CATALOG_HELPER:-/usr/local/lib/llm-cluster/model_catalog.py}"
readonly OPTIMIZER_HELPER="${LLM_OPTIMIZER_HELPER:-/usr/local/lib/llm-cluster/runtime_optimizer.py}"
readonly GATEWAY_HELPER="${LLM_GATEWAY_HELPER:-/usr/local/lib/llm-cluster/gateway_config.py}"
readonly OPTIMIZATION_DIR="${STATE_DIR}/optimization"
readonly SMOKE_DIAGNOSTIC_DIR="${STATE_DIR}/diagnostics/smoke"
readonly NGINX_CONFIG="/etc/nginx/conf.d/llm-cluster.conf"
readonly NGINX_STATE_DIR="${STATE_DIR}/nginx"
readonly KEEPWARM_STATE_DIR="${STATE_DIR}/keepwarm"
readonly KEEPWARM_STATE_FILE="${KEEPWARM_STATE_DIR}/last-run.json"
readonly KEEPWARM_LOCK_FILE="${KEEPWARM_STATE_DIR}/run.lock"
readonly KEEPWARM_UNIT_SOURCE_DIR="${LLM_KEEPWARM_UNIT_SOURCE_DIR:-/usr/local/lib/llm-cluster/systemd}"
readonly KEEPWARM_SERVICE_UNIT="/etc/systemd/system/llm-keepwarm.service"
readonly KEEPWARM_TIMER_UNIT="/etc/systemd/system/llm-keepwarm.timer"
readonly WORKFLOW_STATE_DIR="${STATE_DIR}/workflow"
readonly WORKFLOW_CONFIG="${WORKFLOW_STATE_DIR}/workflow.json"
readonly WORKFLOW_ENV="${CONFIG_DIR}/workflow.env"
readonly WORKFLOW_HELPER="${LLM_WORKFLOW_HELPER:-/usr/local/lib/llm-cluster/workflow_config.py}"
readonly WORKFLOW_RUNTIME="${LLM_WORKFLOW_RUNTIME:-/usr/local/lib/llm-cluster/workflowd/llm-workflowd}"
readonly WORKFLOW_UNIT_SOURCE="${LLM_WORKFLOW_UNIT_SOURCE:-/usr/local/lib/llm-cluster/systemd/llm-workflow.service}"
readonly WORKFLOW_SERVICE_UNIT="/etc/systemd/system/llm-workflow.service"
readonly MODEL_CONTROL_RUNTIME="${LLM_MODEL_CONTROL_RUNTIME:-/usr/local/lib/llm-cluster/model_deployment.py}"
readonly MODEL_CONTROL_SOCKET="${LLM_MODEL_CONTROL_SOCKET:-/run/llm-cluster/model-control.sock}"
readonly MODEL_CONTROL_UNIT_SOURCE="${LLM_MODEL_CONTROL_UNIT_SOURCE:-/usr/local/lib/llm-cluster/systemd/llm-model-control.service}"
readonly MODEL_CONTROL_SERVICE_UNIT="/etc/systemd/system/llm-model-control.service"
readonly MODEL_CONTROL_STATE_DIR="${LLM_MODEL_CONTROL_STATE_DIR:-${STATE_DIR}/model-control}"
readonly MODEL_CONTROL_BACKUP_DIR="${LLM_MODEL_BACKUPS_DIR:-/var/backups/llmctl/model-deployments}"
readonly MAX_OUTPUT_TOKENS_CEILING=32768

OPTIMIZER_ROLLBACK_ACTIVE=0
OPTIMIZER_ROLLBACK_FILE=""
OPTIMIZER_ROLLBACK_WORKERS=""
MAINTENANCE_PROXY_DECLINED=0

log()  { printf '[llmctl] %s\n' "$*"; }
warn() { printf '[llmctl] WARNING: %s\n' "$*" >&2; }
die()  { printf '[llmctl] ERROR: %s\n' "$*" >&2; exit 1; }
ctl_l10n() {
  if [[ "${INTERFACE_LANGUAGE:-zh}" == en ]]; then printf '%s' "${2:-}"; else printf '%s' "${1:-}"; fi
}

ensure_docker_network() {
  /usr/bin/docker network inspect "${DOCKER_NETWORK}" >/dev/null 2>&1 && return 0
  /usr/bin/docker network create "${DOCKER_NETWORK}" >/dev/null 2>&1 || \
    /usr/bin/docker network inspect "${DOCKER_NETWORK}" >/dev/null 2>&1 || \
    die "无法创建 Docker 内部网络 ${DOCKER_NETWORK}"
}

require_root() {
  [[ ${EUID} -eq 0 ]] || die "此操作需要 root，请使用 sudo llmctl ..."
}

require_installed() {
  [[ -r "${CLUSTER_ENV}" ]] || die "未找到 ${CLUSTER_ENV}；请先执行安装脚本。"
  [[ -r "${SECRETS_ENV}" ]] || die "未找到 ${SECRETS_ENV}；安装不完整。"
}

load_config() {
  require_installed
  # 这些文件由安装器或管理器生成并校验，且只允许 root 修改。
  # shellcheck disable=SC1090
  source "${CLUSTER_ENV}"
  # shellcheck disable=SC1090
  source "${SECRETS_ENV}"

  # 兼容此前只部署 Ornith 的版本。
  STARTUP_PARALLELISM="${STARTUP_PARALLELISM:-1}"
  # 升级环境需要显式启用；3.2 及以后全新安装会写入 1。
  KEEPWARM_ENABLED="${KEEPWARM_ENABLED:-0}"
  KEEPWARM_INTERVAL_SECONDS="${KEEPWARM_INTERVAL_SECONDS:-300}"
  KEEPWARM_TIMEOUT_SECONDS="${KEEPWARM_TIMEOUT_SECONDS:-90}"
  # 工作流路由是独立管理的可选数据面。旧环境升级控制面后仍保持关闭，
  # 直到管理员明确执行 `llmctl workflow init/enable`。
  WORKFLOW_ENABLED="${WORKFLOW_ENABLED:-0}"
  WORKFLOW_LISTEN="${WORKFLOW_LISTEN:-127.0.0.1:18100}"
  WORKFLOW_ROUTE_MODEL="${WORKFLOW_ROUTE_MODEL:-llmctl-workflow-${SERVED_MODEL_NAME:-gdn-inside}}"
  MODEL_HUB="${MODEL_HUB:-huggingface}"
  MODEL_ARCHITECTURE="${MODEL_ARCHITECTURE:-Qwen3_5MoeForConditionalGeneration}"
  MODEL_TASK="${MODEL_TASK:-vision}"
  MODEL_PRECISION="${MODEL_PRECISION:-fp8}"
  MODEL_WEIGHT_BYTES="${MODEL_WEIGHT_BYTES:-0}"
  MODEL_PARAMS="${MODEL_PARAMS:-0}"
  MODEL_NATIVE_CONTEXT="${MODEL_NATIVE_CONTEXT:-${MAX_MODEL_LEN:-32768}}"
  # 旧控制面没有该键。升级后统一补成 32K，并拒绝通过手工配置绕过
  # 服务端安全上限；客户端仍可为普通请求选择更小的 max_tokens。
  MAX_OUTPUT_TOKENS="${MAX_OUTPUT_TOKENS:-${MAX_OUTPUT_TOKENS_CEILING}}"
  ESTIMATED_MAX_NUM_SEQS="${ESTIMATED_MAX_NUM_SEQS:-${MAX_NUM_SEQS:-7}}"
  PLE_CPU_OFFLOAD="${PLE_CPU_OFFLOAD:-0}"
  ENABLE_EXPERT_PARALLEL="${ENABLE_EXPERT_PARALLEL:-0}"
  ENABLE_PREFIX_CACHING="${ENABLE_PREFIX_CACHING:-1}"
  ENABLE_FLASHINFER_AUTOTUNE="${ENABLE_FLASHINFER_AUTOTUNE:-1}"
  DISABLE_CUSTOM_ALL_REDUCE="${DISABLE_CUSTOM_ALL_REDUCE:-0}"
  MTP_SPECULATIVE_TOKENS="${MTP_SPECULATIVE_TOKENS:-0}"
  KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-auto}"
  YARN_FACTOR="${YARN_FACTOR:-1}"
  TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-qwen3_xml}"
  REASONING_PARSER="${REASONING_PARSER:-qwen3}"
  SUPPORTS_IMAGE_INPUT="${SUPPORTS_IMAGE_INPUT:-1}"
  SUPPORTS_OCR="${SUPPORTS_OCR:-1}"
  SUPPORTS_TOOL_CALLING="${SUPPORTS_TOOL_CALLING:-1}"
  SUPPORTS_REASONING="${SUPPORTS_REASONING:-1}"
  SUPPORTS_THINKING_TOGGLE="${SUPPORTS_THINKING_TOGGLE:-1}"
  TRUST_REMOTE_CODE="${TRUST_REMOTE_CODE:-1}"
  GATEWAY_KIND="${GATEWAY_KIND:-litellm}"
  LITELLM_IMAGE="${LITELLM_IMAGE:-ghcr.io/berriai/litellm:v1.94.0}"
  NEWAPI_IMAGE="${NEWAPI_IMAGE:-calciumion/new-api:v1.0.0-rc.22}"
  BIFROST_IMAGE="${BIFROST_IMAGE:-maximhq/bifrost:v1.6.7}"
  OMNIROUTE_IMAGE="${OMNIROUTE_IMAGE:-diegosouzapw/omniroute:3.8.49}"
  case "${GATEWAY_KIND}" in
    newapi) GATEWAY_IMAGE="${GATEWAY_IMAGE:-${NEWAPI_IMAGE}}" ;;
    litellm) GATEWAY_IMAGE="${GATEWAY_IMAGE:-${LITELLM_IMAGE}}" ;;
    bifrost) GATEWAY_IMAGE="${GATEWAY_IMAGE:-${BIFROST_IMAGE}}" ;;
    omniroute) GATEWAY_IMAGE="${GATEWAY_IMAGE:-${OMNIROUTE_IMAGE}}" ;;
    *) die "GATEWAY_KIND 必须是 newapi、litellm、bifrost 或 omniroute" ;;
  esac
  GATEWAY_DB_PORT="${GATEWAY_DB_PORT:-${LITELLM_DB_PORT:-15432}}"
  GATEWAY_API_KEY="${GATEWAY_API_KEY:-${LITELLM_MASTER_KEY:-}}"
  LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-${GATEWAY_API_KEY}}"
  DATABASE_URL="${DATABASE_URL:-${SQL_DSN:-}}"
  ACCOUNT_PORT="${ACCOUNT_PORT:-8001}"
  ACCOUNT_BIND="${ACCOUNT_BIND:-127.0.0.1}"
  GATEWAY_INTERNAL_PORT="${GATEWAY_INTERNAL_PORT:-18000}"
  DOCKER_NETWORK="${DOCKER_NETWORK:-llm-cluster-net}"
  ACCOUNT_PUBLIC_URL="${ACCOUNT_PUBLIC_URL:-}"
  ACCOUNT_API_PUBLIC_URL="${ACCOUNT_API_PUBLIC_URL:-}"
  ACCOUNT_PORTAL_TITLE="${ACCOUNT_PORTAL_TITLE:-LLMCtl}"
  ACCOUNT_PUBLISHED_ORIGIN="${ACCOUNT_PUBLISHED_ORIGIN:-}"
  ACCOUNT_ADMIN_USERNAME_B64="${ACCOUNT_ADMIN_USERNAME_B64:-}"
  if [[ -n "${ACCOUNT_ADMIN_USERNAME_B64}" ]]; then
    ACCOUNT_ADMIN_USERNAME=$(python3 -c 'import base64,sys; print(base64.b64decode(sys.argv[1], validate=True).decode("utf-8"), end="")' "${ACCOUNT_ADMIN_USERNAME_B64}") || \
      die "ACCOUNT_ADMIN_USERNAME_B64 无效"
  else
    ACCOUNT_ADMIN_USERNAME="${ACCOUNT_ADMIN_USERNAME:-${ACCOUNT_ADMIN_EMAIL:-admin}}"
  fi
  ACCOUNT_DB_PATH="${ACCOUNT_DB_PATH:-${ACCOUNT_SQLITE}}"
  ACCOUNT_REGISTRATION_ENABLED="${ACCOUNT_REGISTRATION_ENABLED:-0}"
  ACCOUNT_ALLOWED_EMAIL_DOMAINS="${ACCOUNT_ALLOWED_EMAIL_DOMAINS:-}"
  ACCOUNT_DEFAULT_WELCOME_BALANCE="${ACCOUNT_DEFAULT_WELCOME_BALANCE:-0}"
  ACCOUNT_DEFAULT_QUOTA_TOKENS="${ACCOUNT_DEFAULT_QUOTA_TOKENS:-0}"
  ACCOUNT_QUOTA_RESET="${ACCOUNT_QUOTA_RESET:-monthly}"
  ACCOUNT_QUOTA_RESET_TIME="${ACCOUNT_QUOTA_RESET_TIME:-00:00}"
  SMTP_HOST="${SMTP_HOST:-}"
  SMTP_PORT="${SMTP_PORT:-587}"
  SMTP_SECURITY="${SMTP_SECURITY:-starttls}"
  SMTP_USERNAME="${SMTP_USERNAME:-}"
  SMTP_PASSWORD="${SMTP_PASSWORD:-}"
  SMTP_FROM="${SMTP_FROM:-}"

  : "${PHYSICAL_GPU_COUNT:?PHYSICAL_GPU_COUNT missing}"
  : "${TP_SIZE:?TP_SIZE missing}"
  : "${INSTANCE_COUNT:?INSTANCE_COUNT missing}"
  : "${ACTIVE_WORKERS:?ACTIVE_WORKERS missing}"
  : "${WORKER_BASE_PORT:?WORKER_BASE_PORT missing}"
  : "${API_BIND:?API_BIND missing}"
  : "${API_PORT:?API_PORT missing}"
  : "${GATEWAY_INTERNAL_PORT:?GATEWAY_INTERNAL_PORT missing}"
  : "${MODEL_ROOT:?MODEL_ROOT missing}"
  : "${SERVED_MODEL_NAME:?SERVED_MODEL_NAME missing}"
  : "${VLLM_IMAGE:?VLLM_IMAGE missing}"
  : "${GATEWAY_IMAGE:?GATEWAY_IMAGE missing}"
  : "${POSTGRES_IMAGE:?POSTGRES_IMAGE missing}"
  : "${GATEWAY_DB_PORT:?GATEWAY_DB_PORT missing}"
  : "${MAX_MODEL_LEN:?MAX_MODEL_LEN missing}"
  : "${MAX_NUM_SEQS:?MAX_NUM_SEQS missing}"
  : "${MAX_NUM_BATCHED_TOKENS:?MAX_NUM_BATCHED_TOKENS missing}"
  : "${GPU_MEMORY_UTILIZATION:?GPU_MEMORY_UTILIZATION missing}"
  : "${MM_LIMIT:?MM_LIMIT missing}"
  : "${ROUTING_STRATEGY:?ROUTING_STRATEGY missing}"
  : "${START_TIMEOUT:?START_TIMEOUT missing}"
  : "${GATEWAY_API_KEY:?GATEWAY_API_KEY missing}"
  : "${BACKEND_API_KEY:?BACKEND_API_KEY missing}"
  : "${LITELLM_SALT_KEY:?LITELLM_SALT_KEY missing}"
  : "${UI_USERNAME:?UI_USERNAME missing}"
  : "${UI_PASSWORD:?UI_PASSWORD missing}"
  : "${POSTGRES_USER:?POSTGRES_USER missing}"
  : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing}"
  : "${POSTGRES_DB:?POSTGRES_DB missing}"
  : "${DATABASE_URL:?DATABASE_URL missing}"
  [[ -x "${GATEWAY_HELPER}" ]] || die "网关配置助手不可执行：${GATEWAY_HELPER}"
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    [[ -x "${ACCOUNT_HELPER}" ]] || die "账户门户不可执行：${ACCOUNT_HELPER}"
    : "${OMNIROUTE_JWT_SECRET:?OMNIROUTE_JWT_SECRET missing}"
    : "${OMNIROUTE_API_KEY_SECRET:?OMNIROUTE_API_KEY_SECRET missing}"
    : "${OMNIROUTE_STORAGE_ENCRYPTION_KEY:?OMNIROUTE_STORAGE_ENCRYPTION_KEY missing}"
    : "${ACCOUNT_ADMIN_PASSWORD:?ACCOUNT_ADMIN_PASSWORD missing}"
  fi
  [[ "${STARTUP_PARALLELISM}" =~ ^[0-9]+$ ]] && (( STARTUP_PARALLELISM >= 1 && STARTUP_PARALLELISM <= INSTANCE_COUNT )) || die "STARTUP_PARALLELISM 必须在 1-${INSTANCE_COUNT}"
  [[ "${KEEPWARM_ENABLED}" == 0 || "${KEEPWARM_ENABLED}" == 1 ]] || die "KEEPWARM_ENABLED 必须是 0 或 1"
  [[ "${KEEPWARM_INTERVAL_SECONDS}" =~ ^[0-9]+$ ]] && (( KEEPWARM_INTERVAL_SECONDS >= 60 && KEEPWARM_INTERVAL_SECONDS <= 86400 )) || die "KEEPWARM_INTERVAL_SECONDS 范围 60-86400"
  [[ "${KEEPWARM_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]] && (( KEEPWARM_TIMEOUT_SECONDS >= 5 && KEEPWARM_TIMEOUT_SECONDS <= 300 )) || die "KEEPWARM_TIMEOUT_SECONDS 范围 5-300"
  [[ "${WORKFLOW_ENABLED}" == 0 || "${WORKFLOW_ENABLED}" == 1 ]] || die "WORKFLOW_ENABLED 必须是 0 或 1"
  [[ "${MAX_OUTPUT_TOKENS}" =~ ^[0-9]+$ ]] && (( MAX_OUTPUT_TOKENS >= 1 && MAX_OUTPUT_TOKENS <= MAX_OUTPUT_TOKENS_CEILING )) || \
    die "MAX_OUTPUT_TOKENS 范围 1-${MAX_OUTPUT_TOKENS_CEILING}"
  local runtime_switch
  for runtime_switch in PLE_CPU_OFFLOAD ENABLE_EXPERT_PARALLEL ENABLE_PREFIX_CACHING ENABLE_FLASHINFER_AUTOTUNE DISABLE_CUSTOM_ALL_REDUCE; do
    [[ "${!runtime_switch}" == 0 || "${!runtime_switch}" == 1 ]] || die "${runtime_switch} 必须是 0 或 1"
  done
  [[ "${MTP_SPECULATIVE_TOKENS}" =~ ^[0-8]$ ]] || die "MTP_SPECULATIVE_TOKENS 范围 0-8"
  [[ "${KV_CACHE_DTYPE}" =~ ^(auto|bfloat16|fp8|fp8_e4m3|nvfp4)$ ]] || die "KV_CACHE_DTYPE 无效"
  [[ "${YARN_FACTOR}" =~ ^(1|1\.0|2|2\.0|4|4\.0)$ ]] || die "YARN_FACTOR 只能是 1、2 或 4"
}

cmd_gateway_start() {
  require_root
  load_config
  local name
  local -a runtime_proxy_args=()
  name=$(gateway_display_name)
  runtime_proxy_docker_args runtime_proxy_args
  export UI_USERNAME UI_PASSWORD
  log "启动 ${name}：镜像=${GATEWAY_IMAGE}，内部入口=127.0.0.1:${GATEWAY_INTERNAL_PORT}，公开入口由 Nginx ${API_BIND}:${API_PORT} 提供。"
  ensure_docker_network
  case "${GATEWAY_KIND}" in
    newapi)
      docker volume create llm-cluster-gateway-data >/dev/null
      exec /usr/bin/docker run --rm --name llm-router --network "${DOCKER_NETWORK}" \
        -p "127.0.0.1:${GATEWAY_INTERNAL_PORT}:${GATEWAY_INTERNAL_PORT}" \
        --env-file "${SECRETS_ENV}" "${runtime_proxy_args[@]}" -e UI_USERNAME -e UI_PASSWORD \
        -e "PORT=${GATEWAY_INTERNAL_PORT}" -e "TZ=${TZ:-Asia/Shanghai}" \
        -e ERROR_LOG_ENABLED=true -e BATCH_UPDATE_ENABLED=true \
        -v llm-cluster-gateway-data:/data \
        "${GATEWAY_IMAGE}" --log-dir /data/logs
      ;;
    litellm)
      exec /usr/bin/docker run --rm --name llm-router --network "${DOCKER_NETWORK}" \
        -p "127.0.0.1:${GATEWAY_INTERNAL_PORT}:${GATEWAY_INTERNAL_PORT}" \
        --env-file "${SECRETS_ENV}" "${runtime_proxy_args[@]}" -e UI_USERNAME -e UI_PASSWORD \
        -v "${LITELLM_CONFIG}:/app/config.yaml:ro" \
        "${GATEWAY_IMAGE}" --config /app/config.yaml --host 0.0.0.0 --port "${GATEWAY_INTERNAL_PORT}"
      ;;
    bifrost)
      docker volume create llm-cluster-gateway-data >/dev/null
      exec /usr/bin/docker run --rm --name llm-router --network "${DOCKER_NETWORK}" \
        -p "127.0.0.1:${GATEWAY_INTERNAL_PORT}:${GATEWAY_INTERNAL_PORT}" \
        --env-file "${SECRETS_ENV}" "${runtime_proxy_args[@]}" -e UI_USERNAME -e UI_PASSWORD \
        -e APP_DIR=/app/data -e APP_HOST=0.0.0.0 -e "APP_PORT=${GATEWAY_INTERNAL_PORT}" \
        -v llm-cluster-gateway-data:/app/data \
        -v "${BIFROST_CONFIG}:/app/data/config.json:ro" \
        "${GATEWAY_IMAGE}"
      ;;
    omniroute)
      install -d -m 770 -o 1000 -g 1000 "${STATE_DIR}/omniroute/gateway"
      exec /usr/bin/docker run --rm --name llm-router --network "${DOCKER_NETWORK}" \
        -p "127.0.0.1:${GATEWAY_INTERNAL_PORT}:${GATEWAY_INTERNAL_PORT}" \
        --env-file "${SECRETS_ENV}" "${runtime_proxy_args[@]}" -e UI_USERNAME -e UI_PASSWORD \
        -e HOST=0.0.0.0 -e API_HOST=0.0.0.0 \
        -e "PORT=${GATEWAY_INTERNAL_PORT}" -e "DASHBOARD_PORT=${GATEWAY_INTERNAL_PORT}" -e "API_PORT=${GATEWAY_INTERNAL_PORT}" \
        -e "INITIAL_PASSWORD=${UI_PASSWORD}" -e "JWT_SECRET=${OMNIROUTE_JWT_SECRET}" \
        -e "API_KEY_SECRET=${OMNIROUTE_API_KEY_SECRET}" \
        -e "STORAGE_ENCRYPTION_KEY=${OMNIROUTE_STORAGE_ENCRYPTION_KEY}" \
        -e STORAGE_ENCRYPTION_KEY_VERSION=v1 -e REQUIRE_API_KEY=true \
        -e AUTH_COOKIE_SECURE=false -e OMNIROUTE_ALLOW_LOCAL_PROVIDER_URLS=true \
        -e ALLOW_API_KEY_REVEAL=true \
        -e ALLOW_MULTI_CONNECTIONS_PER_COMPAT_NODE=true \
        -e CALL_LOG_RETENTION_DAYS=30 \
        -e CALL_LOGS_TABLE_MAX_ROWS=100000 \
        -e CALL_LOG_PIPELINE_CAPTURE_STREAM_CHUNKS=false \
        -e CALL_LOG_PIPELINE_MAX_SIZE_KB=4096 \
        -e CHAT_LOG_TEXT_LIMIT=1048576 \
        -v "${STATE_DIR}/omniroute/gateway:/app/data" \
        "${GATEWAY_IMAGE}"
      ;;
  esac
}

cmd_worker_start() {
  require_root
  load_config
  local id="${1:?缺少 Worker ID}" worker_env="${CONFIG_DIR}/workers/${1}.env"
  [[ "${id}" =~ ^[0-9]+$ ]] && (( id >= 0 && id <= 255 )) || die "Worker ID 超出范围"
  if [[ -r "${worker_env}" ]]; then
    # shellcheck disable=SC1090
    source "${worker_env}"
  else
    # 旧版部署没有 Worker 私有配置，继续从全局参数推导，升级本身不改变运行方式。
    GPU_DEVICES=$(worker_devices "${id}")
    WORKER_PORT=$(worker_port "${id}")
  fi
  : "${GPU_DEVICES:?缺少 GPU_DEVICES}"
  : "${WORKER_PORT:?缺少 WORKER_PORT}"
  local runtime_switch numa_binding="" numa_node="" numa_cpus=""
  for runtime_switch in PLE_CPU_OFFLOAD ENABLE_EXPERT_PARALLEL ENABLE_PREFIX_CACHING ENABLE_FLASHINFER_AUTOTUNE DISABLE_CUSTOM_ALL_REDUCE; do
    [[ "${!runtime_switch}" == 0 || "${!runtime_switch}" == 1 ]] || die "Worker ${id} 的 ${runtime_switch} 必须是 0 或 1"
  done
  [[ "${MTP_SPECULATIVE_TOKENS}" =~ ^[0-8]$ ]] || die "Worker ${id} 的 MTP 草稿 Token 范围是 0-8"
  [[ "${KV_CACHE_DTYPE}" =~ ^(auto|bfloat16|fp8|fp8_e4m3|nvfp4)$ ]] || die "Worker ${id} 的 KV Cache 精度无效"
  [[ "${YARN_FACTOR}" =~ ^(1|1\.0|2|2\.0|4|4\.0)$ ]] || die "Worker ${id} 的 YaRN 比例无效"
  local normalized_model_id="${MODEL_ID,,}"
  normalized_model_id="${normalized_model_id//[^a-z0-9]/}"
  if [[ "${normalized_model_id}" == *qwen38flashnext* ]]; then
    (( TP_SIZE >= 2 )) || die "Worker ${id} 的 Qwen3.8 Flash Next 至少需要 TP2"
    (( PLE_CPU_OFFLOAD == 1 )) || die "Worker ${id} 的 Qwen3.8 Flash Next 必须启用 PLE CPU offload"
    if (( MAX_MODEL_LEN <= 262144 )); then
      [[ "${YARN_FACTOR}" == 1 || "${YARN_FACTOR}" == 1.0 ]] || die "Worker ${id} 在原生 262K 范围不应启用 YaRN"
    else
      awk -v len="${MAX_MODEL_LEN}" -v factor="${YARN_FACTOR}" 'BEGIN{exit !(factor==2 || factor==4) || !(len<=262144*factor)}' || \
        die "Worker ${id} 的超长上下文与 YaRN 比例不匹配"
    fi
  fi
  MODEL_LOCAL_DIR="${MODEL_LOCAL_DIR:-${MODEL_ROOT}/current}"
  MODEL_LOCAL_DIR=$(readlink -f "${MODEL_LOCAL_DIR}" 2>/dev/null || true)
  [[ -n "${MODEL_LOCAL_DIR}" && -d "${MODEL_LOCAL_DIR}" ]] || die "Worker ${id} 的模型目录不存在"
  ensure_docker_network

  # vLLM 原生支持多个服务模型名。升级部署把新版本名放在首位，同时保留
  # 旧内部名，避免尚未更新的 OpenAI 兼容客户端在切换后立即收到 404。
  local alias
  local -a configured_served_aliases=() served_model_names=("${SERVED_MODEL_NAME}")
  IFS=',' read -r -a configured_served_aliases <<<"${SERVED_MODEL_ALIASES:-}"
  for alias in "${configured_served_aliases[@]}"; do
    [[ -z "${alias}" || "${alias}" == "${SERVED_MODEL_NAME}" ]] && continue
    [[ "${alias}" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$ ]] || \
      die "Worker ${id} 的服务模型兼容别名非法：${alias}"
    served_model_names+=("${alias}")
  done

  local -a docker_args=(
    /usr/bin/docker run --rm --name "llm-worker-${id}"
    --network "${DOCKER_NETWORK}" --ipc host --runtime=nvidia
    -p "127.0.0.1:${WORKER_PORT}:${WORKER_PORT}"
    -e "NVIDIA_VISIBLE_DEVICES=${GPU_DEVICES}"
    -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1
    -e VLLM_NO_USAGE_STATS=1 -e VLLM_MEDIA_URL_ALLOW_REDIRECTS=0
    -v "${MODEL_LOCAL_DIR}:/model:ro"
    -v "${CACHE_DIR}/shared:/root/.cache"
  )
  if (( PLE_CPU_OFFLOAD == 1 )) && numa_binding=$(worker_numa_binding "${GPU_DEVICES}"); then
    IFS=$'\t' read -r numa_node numa_cpus <<<"${numa_binding}"
    docker_args+=(--cpuset-mems "${numa_node}" --cpuset-cpus "${numa_cpus}")
  fi
  if (( PLE_CPU_OFFLOAD == 1 )); then
    docker_args+=(--cap-add SYS_PTRACE -e VLLM_PLE_CPU_OFFLOAD=1)
  fi
  if [[ "${YARN_FACTOR}" != 1 && "${YARN_FACTOR}" != 1.0 ]]; then
    docker_args+=(-e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1)
  fi
  docker_args+=(
    "${VLLM_IMAGE}" /model
    --served-model-name "${served_model_names[@]}"
    --host 0.0.0.0 --port "${WORKER_PORT}"
    --api-key "${BACKEND_API_KEY}"
    --tensor-parallel-size "${TP_SIZE}"
    --max-model-len "${MAX_MODEL_LEN}"
    --override-generation-config "{\"max_new_tokens\":${MAX_OUTPUT_TOKENS}}"
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
    --max-num-seqs "${MAX_NUM_SEQS}"
    --max-num-batched-tokens "${MAX_NUM_BATCHED_TOKENS}"
    --kv-cache-dtype "${KV_CACHE_DTYPE}"
    --enable-chunked-prefill
  )
  (( ENABLE_EXPERT_PARALLEL == 0 )) || docker_args+=(--enable-expert-parallel)
  if (( ENABLE_PREFIX_CACHING == 1 )); then
    docker_args+=(--enable-prefix-caching)
  else
    docker_args+=(--no-enable-prefix-caching)
  fi
  (( ENABLE_FLASHINFER_AUTOTUNE == 1 )) || docker_args+=(--no-enable-flashinfer-autotune)
  (( DISABLE_CUSTOM_ALL_REDUCE == 0 )) || docker_args+=(--disable-custom-all-reduce)
  if (( MTP_SPECULATIVE_TOKENS > 0 )); then
    docker_args+=(--speculative-config "{\"method\":\"mtp\",\"num_speculative_tokens\":${MTP_SPECULATIVE_TOKENS}}")
  fi
  if [[ "${YARN_FACTOR}" != 1 && "${YARN_FACTOR}" != 1.0 ]]; then
    docker_args+=(--hf-overrides "{\"rope_parameters\":{\"rope_type\":\"yarn\",\"factor\":${YARN_FACTOR},\"original_max_position_embeddings\":262144}}")
  fi
  (( TRUST_REMOTE_CODE == 0 )) || docker_args+=(--trust-remote-code)
  if (( SUPPORTS_TOOL_CALLING == 1 )) && [[ -n "${TOOL_CALL_PARSER}" ]]; then
    docker_args+=(--enable-auto-tool-choice --tool-call-parser "${TOOL_CALL_PARSER}")
  fi
  if (( SUPPORTS_REASONING == 1 )) && [[ -n "${REASONING_PARSER}" ]]; then
    docker_args+=(--reasoning-parser "${REASONING_PARSER}")
  fi
  if (( SUPPORTS_IMAGE_INPUT == 1 )); then
    docker_args+=(--limit-mm-per-prompt "${MM_LIMIT}" --allowed-media-domains llm.invalid)
  fi
  log "Worker ${id} 启动：GPU=${GPU_DEVICES}，TP=${TP_SIZE}，NUMA=${numa_node:-系统默认}，ctx=${MAX_MODEL_LEN}，output<=${MAX_OUTPUT_TOKENS}，seq=${MAX_NUM_SEQS}，KV=${KV_CACHE_DTYPE}，MTP=${MTP_SPECULATIVE_TOKENS}，PLE=${PLE_CPU_OFFLOAD}，模型=${MODEL_ID}"
  exec "${docker_args[@]}"
}

image_supports_architecture() {
  local image="${1:?}" architecture="${2:?}"
  docker run --rm --entrypoint python3 "${image}" -c '
import sys
from vllm.model_executor.models import ModelRegistry
raise SystemExit(0 if sys.argv[1] in set(ModelRegistry.get_supported_archs()) else 2)
' "${architecture}" >/dev/null
}

usage() {
  cat <<'EOF'
通用 vLLM 集群管理器

用法：
  llmctl info [--redact]                      完整恢复清单；默认含所有明文密码和密钥
  llmctl status [all|0,1,...]                集群、GPU、Router 和 Worker 状态
  llmctl health                               检查 Router 与所有已运行 Worker
  llmctl startup status                      显示一次聚合启动进度
  llmctl startup watch [--timeout 秒]         持续显示启动进度，适合 SSH 重连后观察
  llmctl start [all|0,1,...]                 按配置并行度分批启动（all=持久激活的 Worker）
  llmctl stop [all|0,1,...]                  停止；不改变下次开机激活列表
  llmctl restart [all|0,1,...]               按配置并行度分批重启并等待健康
  llmctl shutdown [--timeout 秒]             并发停止整个集群并持续显示进度
  llmctl enable <0,1,...|all>                 加入开机激活列表，不立即启动
  llmctl disable <0,1,...|all>                移出开机激活列表，不立即停止
  llmctl activate <0,1,...|all>               enable + start + 加入负载均衡
  llmctl deactivate <0,1,...|all>             移出负载均衡 + stop + disable
  llmctl scale <1-N>                          持久调整集群为前 N 个实例
  llmctl autostart <enable|disable|status>     管理整个集群的开机自启
  llmctl keepwarm status                       查看周期保活配置与逐 Worker 最近结果
  llmctl keepwarm run [all|0,1,...]            立即直连指定 Worker 执行 1-token 预热
  llmctl keepwarm enable [间隔秒数]             启用周期保活（60-86400 秒）并立即预热
  llmctl keepwarm disable                      关闭周期保活，不停止 Worker
  llmctl keepwarm interval <秒数>               在线修改保活间隔，无需重启 Worker
  llmctl workflow init [选项]                  初始化默认关闭的可插拔工作流；可导入本机或填写远程 Worker URL
  llmctl workflow target add <池> <ID> <URL> [密钥环境变量]
  llmctl workflow target remove <池> <ID>      在线维护显式资源端点，不依赖本机 Docker
  llmctl workflow target discover <URL> [密钥环境变量] [模型ID]
  llmctl workflow model set <公开ID> <底层ID> <池> [transparent|agent] [工具ID列表]
  llmctl workflow adapter set <ID> <URL> <工具定义JSON> [密钥环境变量]
  llmctl workflow secret set <环境变量> [值]   写入独立、root-only 的工作流密钥文件
  llmctl workflow <enable|disable|reload|check|status|show|health|logs>
                                                独立管理 Go 数据面；默认不修改现有网关映射
  llmctl model init                            初始化多模型注册表和本机控制服务；不重启 Worker
  llmctl model status                          显示 GPU、部署、后台任务和控制服务状态
  llmctl model plan <JSON文件|->               校验部署并显示受影响 Worker/GPU，不做修改
  llmctl model deploy <JSON文件|->             提交已确认的后台部署任务
  llmctl model publish                         仅重试当前注册表到 AI 接入层，不重启 Worker
  llmctl model upgrade plan <部署ID> [选项]   解析固定 SHA 并显示 Ornith 升级/回退计划
  llmctl model upgrade apply <部署ID> [选项]  确认后提交升级；支持 --yes
  llmctl model upgrade rollback <任务ID>      恢复到该升级任务执行前的模型版本
  llmctl model job <任务ID>                    查询部署阶段、进度、日志与回滚结果
  llmctl model cancel <任务ID>                 请求在安全检查点取消并回滚部署
  llmctl model rollback <任务ID>               恢复到该成功部署执行前的快照
  llmctl responses status                      检查公开模型 ID 是否可被 Responses API 原生解析
  llmctl responses repair                      备份数据并修复 Responses API 原生 Combo 与用户权限
  llmctl router <start|stop|restart|reconcile|status> 管理或在线同步所选接入层
  llmctl omniroute status|backup|backups           查看状态或管理可校验 SQLite 备份
  llmctl omniroute update [固定镜像] [--yes]       备份后升级；失败自动恢复镜像和数据库
  llmctl omniroute rollback <备份ID> [--yes]       回滚前再备份，并恢复镜像与 SQLite
  llmctl omniroute sqlite assess [--deep]          评估完整性、WAL、空间和备份准备度
  llmctl omniroute sqlite maintain online|compact  在线优化或维护窗 VACUUM
  llmctl database <start|stop|restart|status>  管理接入层 PostgreSQL
  llmctl database enable-mysql                激活门户 MySQL 驱动；连接配置与迁移在 WebUI 完成
  llmctl account <start|stop|restart|status|url> 管理 OmniRoute 账户门户
  llmctl nginx <apply|test|status>              应用、校验或查看 LLMCtl Nginx 公开入口
  llmctl timezone show|set [时区]              查看或设置系统时区（默认 Asia/Shanghai）

  llmctl logs [all] [-f]                      全部组件日志（默认）
  llmctl logs worker <ID> [-f]                Worker 日志
  llmctl logs router [-f]                     所选接入层日志
  llmctl logs database [-f]                   PostgreSQL 日志
  llmctl logs account [-f]                    OmniRoute 账户门户日志
  llmctl logs workflow [-f]                   可插拔 Go 工作流日志
  llmctl logs model [-f]                      多模型部署控制服务日志
  llmctl smoke [--worker ID] [--full]         文本/思考/工具；--full 另测 OCR/单请求6图
  llmctl ocr <图片文件> [提示词]               通过集群入口执行 OCR
  llmctl bench [--concurrency N] [--requests N] [--max-tokens N]
                                                并发吞吐验收（默认 25/50/512）
  llmctl optimize analyze [--profile PROFILE] [--quick]
                                                只测试当前配置并给出建议，不修改配置
  llmctl optimize run [--profile PROFILE] [--quick] [--yes]
                                                确认后试验候选、自动择优、验收并可回滚
  llmctl optimize report [--json]               显示最近一次调优报告
  llmctl optimize restore [latest|RUN_ID] [--yes]
                                                恢复调优前配置并重启、完整验收
    PROFILE：latency（延迟优先）、balanced（默认）、throughput（吞吐优先）

  llmctl tune show                            显示推理与路由参数
  llmctl tune set <键> <值>                   修改参数（修改后需重启 Worker）
    可修改键：max-model-len, gpu-memory-utilization, max-num-seqs,
              max-num-batched-tokens, max-images, routing-strategy,
              api-bind, api-port, startup-parallelism, keepwarm-interval-seconds

  llmctl key show                             显示调用地址、模型名和 API key
  llmctl key rotate [新KEY]                   轮换入口 key（New API 不接受自定义 KEY）
  llmctl admin show                           显示 Web UI 地址和管理员凭据
  llmctl admin set-username USER              修改所有支持用户名的管理员入口
  llmctl admin set-password [PASSWORD]        修改关联管理员密码；省略时安全交互输入
  llmctl admin set-portal-username USER       仅修改账户门户管理员登录名
  llmctl admin set-portal-password [PASSWORD] 仅修改账户门户管理员密码
  llmctl admin set-gateway-username USER      仅修改原生网关用户名
  llmctl admin set-gateway-password [PASSWORD] 仅修改原生网关密码
  llmctl proxy set <IP> <端口> [http|https]    保存“仅维护使用”的代理
  llmctl proxy show|clear|test                 查看/清除/测试维护代理
  llmctl runtime-proxy set <IP> <端口> [http|https] [NO_PROXY]
                                                配置网关与工作流的国际出口；不注入 Worker
  llmctl runtime-proxy show|clear|test|apply   查看、清除、测试或重新应用运行时代理

  llmctl models hardware                       显示 GPU 与显存
  llmctl models search [QUERY] [--source all|huggingface|modelscope]
                                                搜索并仅列出本机可部署模型
  llmctl models inspect <SOURCE> <MODEL_ID> [REVISION]
                                                检查能力、兼容性和推荐拓扑
  llmctl download [MODEL_ID] [REVISION]        重新核验或补齐当前模型文件
  llmctl update [--vllm-image IMG] [--gateway-image IMG] [--postgres-image IMG]
                                                显式拉取镜像；绝不自动更新
  llmctl upgrade [--yes] [--proxy URL] [--save-proxy]
                                                从 GitHub 升级 LLMCtl 控制面，不重启 Worker
  llmctl upgrade --from-zip FILE                从本地 ZIP 离线升级 LLMCtl 控制面
  llmctl rollback <备份目录>                    恢复控制面及可用的门户/网关数据快照
  llmctl offline export <目录>                 导出镜像、模型和清单
  llmctl offline import <目录>                 从离线包导入

  llmctl uninstall [--purge-model] [--purge-images] [--purge-database] [--yes]
                                                默认保留模型与管理数据库
  llmctl version

思考开关（仅支持该能力的模型）：
  关闭时在 OpenAI Chat Completions JSON 中加入：
  "reasoning_effort": "none"
  也兼容："chat_template_kwargs": {"enable_thinking": false}
EOF
}

csv_normalize() {
  local input="${1:-}" item seen=","
  local -a result=()
  input="${input// /,}"
  IFS=',' read -r -a raw_items <<<"${input}"
  for item in "${raw_items[@]}"; do
    [[ -n "${item}" ]] || continue
    [[ "${item}" =~ ^[0-9]+$ ]] || die "无效实例 ID: ${item}"
    (( item >= 0 && item < INSTANCE_COUNT )) || die "实例 ID ${item} 超出范围 0-$((INSTANCE_COUNT - 1))"
    if [[ "${seen}" != *",${item},"* ]]; then
      result+=("${item}")
      seen+="${item},"
    fi
  done
  ((${#result[@]} > 0)) || die "未指定实例 ID"
  local joined
  joined=$(IFS=,; printf '%s' "${result[*]}")
  printf '%s\n' "${joined}"
}

all_instance_ids() {
  local i out=""
  for ((i = 0; i < INSTANCE_COUNT; i++)); do
    out+="${out:+,}${i}"
  done
  printf '%s\n' "${out}"
}

resolve_ids() {
  local spec="${1:-all}"
  if [[ "${spec}" == "all" ]]; then
    [[ -n "${ACTIVE_WORKERS}" ]] || die "持久激活列表为空"
    printf '%s\n' "${ACTIVE_WORKERS}"
  else
    csv_normalize "${spec}"
  fi
}

csv_has() {
  local csv="${1:-}" needle="${2:?}"
  [[ ",${csv}," == *",${needle},"* ]]
}

csv_add() {
  local csv additions id result_csv
  csv="${1:-}"
  additions="${2:-}"
  result_csv="${csv}"
  IFS=',' read -r -a ids <<<"${additions}"
  for id in "${ids[@]}"; do
    [[ -n "${id}" ]] || continue
    csv_has "${result_csv}" "${id}" || result_csv+="${result_csv:+,}${id}"
  done
  printf '%s\n' "${result_csv}"
}

csv_remove() {
  local csv="${1:-}" removals="${2:-}" id out=""
  IFS=',' read -r -a ids <<<"${csv}"
  for id in "${ids[@]}"; do
    [[ -n "${id}" ]] || continue
    csv_has "${removals}" "${id}" || out+="${out:+,}${id}"
  done
  printf '%s\n' "${out}"
}

set_env_value() {
  local file="${1:?}" key="${2:?}" value="${3-}" tmp quoted
  tmp=$(mktemp "${file}.XXXXXX")
  awk -F= -v key="${key}" '$1 != key {print}' "${file}" >"${tmp}"
  printf -v quoted '%q' "${value}"
  printf '%s=%s\n' "${key}" "${quoted}" >>"${tmp}"
  chmod --reference="${file}" "${tmp}" 2>/dev/null || chmod 600 "${tmp}"
  chown --reference="${file}" "${tmp}" 2>/dev/null || true
  mv -f "${tmp}" "${file}"
}

worker_config_value() {
  local id="${1:?}" name="${2:?}" fallback="${3-}" worker_env="${CONFIG_DIR}/workers/${1}.env"
  (
    [[ ! -r "${worker_env}" ]] || source "${worker_env}"
    printf '%s\n' "${!name:-${fallback}}"
  )
}

worker_port() {
  local id="${1:?}"
  worker_config_value "${id}" WORKER_PORT "$((WORKER_BASE_PORT + id))"
}
worker_unit() { printf 'llm-worker@%s.service\n' "$1"; }

worker_devices() {
  local instance configured start i out=""
  instance="${1:?}"
  configured=$(worker_config_value "${instance}" GPU_DEVICES "")
  if [[ -n "${configured}" ]]; then
    printf '%s\n' "${configured}"
    return
  fi
  start=$((instance * TP_SIZE))
  for ((i = 0; i < TP_SIZE; i++)); do out+="${out:+,}$((start + i))"; done
  printf '%s\n' "${out}"
}

worker_served_model() {
  worker_config_value "${1:?}" SERVED_MODEL_NAME "${SERVED_MODEL_NAME}"
}

worker_supports_thinking_toggle() {
  worker_config_value "${1:?}" SUPPORTS_THINKING_TOGGLE "${SUPPORTS_THINKING_TOGGLE}"
}

worker_is_active() {
  systemctl is-active --quiet "$(worker_unit "$1")"
}

worker_health() {
  local id="${1:?}" port
  port=$(worker_port "${id}")
  curl --noproxy '*' -fsS --max-time 3 \
    -H "Authorization: Bearer ${BACKEND_API_KEY}" \
    "http://127.0.0.1:${port}/health" >/dev/null 2>&1
}

worker_health_fast() {
  local id="${1:?}" port
  port=$(worker_port "${id}")
  curl --noproxy '*' -fsS --max-time 1 \
    -H "Authorization: Bearer ${BACKEND_API_KEY}" \
    "http://127.0.0.1:${port}/health" >/dev/null 2>&1
}

keepwarm_one_worker() {
  local id="${1:?}" result_file="${2:?}" port model thinking_toggle payload response metrics="" curl_status=0
  local http_code=000 ttft=0 total=0 status=failed error="" started_at finished_at
  port=$(worker_port "${id}")
  model=$(worker_served_model "${id}")
  thinking_toggle=$(worker_supports_thinking_toggle "${id}")
  response=$(mktemp "${KEEPWARM_STATE_DIR}/response-${id}.XXXXXX")
  started_at=$(date -u +%FT%TZ)
  if (( thinking_toggle == 1 )); then
    payload=$(jq -cn --arg model "${model}" '{model:$model,stream:false,max_tokens:1,temperature:0,chat_template_kwargs:{enable_thinking:false},messages:[{role:"user",content:"Reply OK."}]}')
  else
    payload=$(jq -cn --arg model "${model}" '{model:$model,stream:false,max_tokens:1,temperature:0,messages:[{role:"user",content:"Reply OK."}]}')
  fi
  if metrics=$(curl --noproxy '*' -sS -o "${response}" \
      -w $'%{http_code}\t%{time_starttransfer}\t%{time_total}' \
      --connect-timeout 3 --max-time "${KEEPWARM_TIMEOUT_SECONDS}" \
      -H "Authorization: Bearer ${BACKEND_API_KEY}" \
      -H 'Content-Type: application/json' -H 'Accept: application/json' \
      --data-binary "${payload}" "http://127.0.0.1:${port}/v1/chat/completions"); then
    IFS=$'\t' read -r http_code ttft total <<<"${metrics}"
    if [[ "${http_code}" =~ ^2[0-9][0-9]$ ]] && jq -e '.choices | type == "array" and length > 0' "${response}" >/dev/null 2>&1; then
      status=ok
    else
      error=$(jq -r '.error.message // .detail // .message // empty' "${response}" 2>/dev/null | head -c 300 || true)
      [[ -n "${error}" ]] || error="HTTP ${http_code} or invalid Chat Completions response"
    fi
  else
    curl_status=$?
    error="curl exit ${curl_status}"
  fi
  finished_at=$(date -u +%FT%TZ)
  jq -cn \
    --argjson worker "${id}" --arg gpus "$(worker_devices "${id}")" --argjson port "${port}" \
    --arg status "${status}" --arg http_code "${http_code}" --arg ttft "${ttft}" --arg total "${total}" \
    --arg error "${error}" --arg started_at "${started_at}" --arg finished_at "${finished_at}" \
    '{worker:$worker,gpus:$gpus,port:$port,status:$status,http_code:$http_code,
      ttft_seconds:($ttft|tonumber? // 0),total_seconds:($total|tonumber? // 0),
      error:(if $error=="" then null else $error end),started_at:$started_at,finished_at:$finished_at}' >"${result_file}"
  rm -f -- "${response}"
  [[ "${status}" == ok ]]
}

keepwarm_run_ids() {
  local ids="${1:?}" reason="${2:-manual}" id result_file failures=0 started_at finished_at started_epoch finished_epoch
  local lock_fd results_dir summary_file ok_count failed_count total_count
  install -d -m 0700 "${KEEPWARM_STATE_DIR}"
  exec {lock_fd}>"${KEEPWARM_LOCK_FILE}"
  if ! flock -w 5 "${lock_fd}"; then
    warn "$(ctl_l10n '已有 Worker 保活任务在运行，本次跳过。' 'A Worker keep-warm run is already active; this run was skipped.')"
    return 0
  fi
  results_dir=$(mktemp -d "${KEEPWARM_STATE_DIR}/run.XXXXXX")
  summary_file="${KEEPWARM_STATE_FILE}.new.$$"
  started_at=$(date -u +%FT%TZ)
  started_epoch=$(date +%s)
  log "$(ctl_l10n "并发预热 Worker [${ids}]：直接请求各 vLLM 实例，不经过接入层、用户额度或计费。" "Warming Workers [${ids}] concurrently with direct vLLM requests that bypass the gateway, user quotas, and billing.")"
  IFS=',' read -r -a keepwarm_id_list <<<"${ids}"
  for id in "${keepwarm_id_list[@]}"; do
    result_file="${results_dir}/${id}.json"
    keepwarm_one_worker "${id}" "${result_file}" &
  done
  wait || failures=1
  finished_at=$(date -u +%FT%TZ)
  finished_epoch=$(date +%s)
  total_count=${#keepwarm_id_list[@]}
  # Shell 被终止、文件系统写满或辅助程序异常时，聚合结果不能消失；为每个请求的
  # Worker 保留明确结果，让状态输出能定位具体丢失的探针。
  for id in "${keepwarm_id_list[@]}"; do
    result_file="${results_dir}/${id}.json"
    if [[ ! -s "${result_file}" ]] || ! jq -e 'type == "object" and has("status")' "${result_file}" >/dev/null 2>&1; then
      failures=1
      jq -cn \
        --argjson worker "${id}" --arg gpus "$(worker_devices "${id}")" \
        --argjson port "$(worker_port "${id}")" --arg finished_at "${finished_at}" \
        '{worker:$worker,gpus:$gpus,port:$port,status:"failed",http_code:"000",
          ttft_seconds:0,total_seconds:0,error:"probe exited without a valid result",
          started_at:null,finished_at:$finished_at}' >"${result_file}"
    fi
  done
  ok_count=$(jq -s '[.[] | select(.status=="ok")] | length' "${results_dir}"/*.json 2>/dev/null || printf 0)
  failed_count=$((total_count - ok_count))
  jq -s \
    --arg reason "${reason}" --arg started_at "${started_at}" --arg finished_at "${finished_at}" \
    --argjson started_epoch "${started_epoch}" --argjson finished_epoch "${finished_epoch}" \
    --argjson requested "${total_count}" --argjson succeeded "${ok_count}" --argjson failed "${failed_count}" \
    '{reason:$reason,started_at:$started_at,finished_at:$finished_at,started_epoch:$started_epoch,
      finished_epoch:$finished_epoch,summary:{requested:$requested,succeeded:$succeeded,failed:$failed},results:.}' \
    "${results_dir}"/*.json >"${summary_file}"
  chmod 0600 "${summary_file}"
  mv -f "${summary_file}" "${KEEPWARM_STATE_FILE}"
  while IFS= read -r result_file; do
    if [[ "$(jq -r '.status' "${result_file}")" == ok ]]; then
      log "$(ctl_l10n "Worker $(jq -r '.worker' "${result_file}") 保活成功：TTFT=$(jq -r '.ttft_seconds' "${result_file}")s，总耗时=$(jq -r '.total_seconds' "${result_file}")s。" "Worker $(jq -r '.worker' "${result_file}") keep-warm succeeded: TTFT=$(jq -r '.ttft_seconds' "${result_file}")s, total=$(jq -r '.total_seconds' "${result_file}")s.")"
    else
      warn "$(ctl_l10n "Worker $(jq -r '.worker' "${result_file}") 保活失败：$(jq -r '.error' "${result_file}")" "Worker $(jq -r '.worker' "${result_file}") keep-warm failed: $(jq -r '.error' "${result_file}")")"
    fi
  done < <(find "${results_dir}" -type f -name '*.json' -print | sort -V)
  rm -rf -- "${results_dir}"
  (( failed_count == 0 && failures == 0 ))
}

keepwarm_last_finished_epoch() {
  [[ -r "${KEEPWARM_STATE_FILE}" ]] || { printf '0\n'; return; }
  jq -r '.finished_epoch // 0' "${KEEPWARM_STATE_FILE}" 2>/dev/null || printf '0\n'
}

install_keepwarm_units() {
  local service_source="${KEEPWARM_UNIT_SOURCE_DIR}/llm-keepwarm.service"
  local timer_source="${KEEPWARM_UNIT_SOURCE_DIR}/llm-keepwarm.timer"
  if [[ -r "${service_source}" && -r "${timer_source}" ]]; then
    install -m 0644 "${service_source}" "${KEEPWARM_SERVICE_UNIT}"
    install -m 0644 "${timer_source}" "${KEEPWARM_TIMER_UNIT}"
  elif [[ ! -r "${KEEPWARM_SERVICE_UNIT}" || ! -r "${KEEPWARM_TIMER_UNIT}" ]]; then
    die "缺少 Worker 保活 systemd 单元；请重新执行 llmctl upgrade"
  fi
  systemctl daemon-reload
}

cmd_keepwarm_tick() {
  require_root; load_config
  (( KEEPWARM_ENABLED == 1 )) || return 0
  systemctl is-active --quiet llm-cluster.service || return 0
  local now last
  now=$(date +%s)
  last=$(keepwarm_last_finished_epoch)
  [[ "${last}" =~ ^[0-9]+$ ]] || last=0
  (( now - last >= KEEPWARM_INTERVAL_SECONDS )) || return 0
  keepwarm_run_ids "${ACTIVE_WORKERS}" timer || true
}

cmd_keepwarm() {
  require_root; load_config
  local action="${1:-status}" value="${2:-}" ids timer_active timer_enabled previous_enabled
  case "${action}" in
    status)
      timer_active=$(systemctl is-active llm-keepwarm.timer 2>/dev/null || true)
      timer_enabled=$(systemctl is-enabled llm-keepwarm.timer 2>/dev/null || true)
      printf 'enabled=%s\ninterval-seconds=%s\ntimeout-seconds=%s\ntimer-active=%s\ntimer-enabled=%s\nstate=%s\n' \
        "${KEEPWARM_ENABLED}" "${KEEPWARM_INTERVAL_SECONDS}" "${KEEPWARM_TIMEOUT_SECONDS}" \
        "${timer_active:-unknown}" "${timer_enabled:-unknown}" "${KEEPWARM_STATE_FILE}"
      if [[ -r "${KEEPWARM_STATE_FILE}" ]]; then jq . "${KEEPWARM_STATE_FILE}"; else printf 'last-run=never\n'; fi
      ;;
    run)
      ids=$(resolve_ids "${value:-all}")
      keepwarm_run_ids "${ids}" manual
      ;;
    enable)
      if [[ -n "${value}" ]]; then
        [[ "${value}" =~ ^[0-9]+$ ]] && (( value >= 60 && value <= 86400 )) || die "保活间隔范围 60-86400 秒"
        set_env_value "${CLUSTER_ENV}" KEEPWARM_INTERVAL_SECONDS "${value}"
      fi
      install -d -m 0700 "${KEEPWARM_STATE_DIR}"
      install_keepwarm_units
      previous_enabled="${KEEPWARM_ENABLED}"
      set_env_value "${CLUSTER_ENV}" KEEPWARM_ENABLED 1
      if ! systemctl enable --now llm-keepwarm.timer; then
        set_env_value "${CLUSTER_ENV}" KEEPWARM_ENABLED "${previous_enabled}"
        die "Worker 保活定时器启用失败；配置已恢复"
      fi
      load_config
      keepwarm_run_ids "${ACTIVE_WORKERS}" enable || warn "部分 Worker 首次保活失败；定时器仍已启用，请运行 llmctl keepwarm status 查看详情。"
      ;;
    disable)
      set_env_value "${CLUSTER_ENV}" KEEPWARM_ENABLED 0
      systemctl disable --now llm-keepwarm.timer 2>/dev/null || true
      systemctl stop llm-keepwarm.service 2>/dev/null || true
      log "$(ctl_l10n 'Worker 周期保活已关闭；模型进程和现有请求不受影响。' 'Periodic Worker keep-warm is disabled; model processes and existing requests are unaffected.')"
      ;;
    interval)
      [[ "${value}" =~ ^[0-9]+$ ]] && (( value >= 60 && value <= 86400 )) || die "保活间隔范围 60-86400 秒"
      set_env_value "${CLUSTER_ENV}" KEEPWARM_INTERVAL_SECONDS "${value}"
      log "$(ctl_l10n "保活间隔已改为 ${value} 秒；无需重启 Worker。" "Keep-warm interval changed to ${value} seconds; no Worker restart is required.")"
      ;;
    *) die "keepwarm 子命令必须是 status|run|enable|disable|interval" ;;
  esac
}

gpu_memory_snapshot() {
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    printf 'unavailable\n'
    return
  fi
  nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | \
    awk -F, '{gsub(/ /,""); out=out (out ? "," : "") $1 ":" $2 "/" $3 "MiB"} END{print out ? out : "unavailable"}' || \
    printf 'unavailable\n'
}

# 设置 PROGRESS_* 全局变量。健康探针并发运行，因此整批不可用时约耗时一秒，
# 不会按每张 GPU 依次等待超时。
collect_worker_progress() {
  local ids="${1:?}" inactive_is_pending="${2:-0}" id state healthy_csv="" label
  local -a id_list=()
  IFS=',' read -r -a id_list <<<"${ids}"
  healthy_csv=$(
    for id in "${id_list[@]}"; do
      (worker_health_fast "${id}" && printf '%s,' "${id}") &
    done
    wait || true
  )
  healthy_csv="${healthy_csv%,}"

  PROGRESS_TOTAL=${#id_list[@]}
  PROGRESS_HEALTHY=0
  PROGRESS_LOADING=0
  PROGRESS_PENDING=0
  PROGRESS_FAILED=0
  PROGRESS_STATES=""
  PROGRESS_FAILED_IDS=""
  for id in "${id_list[@]}"; do
    if [[ -n "${healthy_csv}" ]] && csv_has "${healthy_csv}" "${id}"; then
      label=healthy
      PROGRESS_HEALTHY=$((PROGRESS_HEALTHY + 1))
    else
      state=$(systemctl show "$(worker_unit "${id}")" -p ActiveState --value 2>/dev/null || printf unknown)
      case "${state}" in
        active|activating|reloading)
          label=loading
          PROGRESS_LOADING=$((PROGRESS_LOADING + 1))
          ;;
        inactive)
          if (( inactive_is_pending )); then
            label=pending
            PROGRESS_PENDING=$((PROGRESS_PENDING + 1))
          else
            label=failed
            PROGRESS_FAILED=$((PROGRESS_FAILED + 1))
            PROGRESS_FAILED_IDS+="${PROGRESS_FAILED_IDS:+,}${id}"
          fi
          ;;
        *)
          label="failed:${state}"
          PROGRESS_FAILED=$((PROGRESS_FAILED + 1))
          PROGRESS_FAILED_IDS+="${PROGRESS_FAILED_IDS:+,}${id}"
          ;;
      esac
    fi
    PROGRESS_STATES+="${PROGRESS_STATES:+,}${id}:${label}"
  done
}

log_worker_progress() {
  local elapsed="${1:?}" prefix="${2:-启动中}" dependency_status="${3:-}"
  log "${prefix} ${elapsed}s：healthy=${PROGRESS_HEALTHY}/${PROGRESS_TOTAL}，loading=${PROGRESS_LOADING}，pending=${PROGRESS_PENDING}，failed=${PROGRESS_FAILED}；Worker=[${PROGRESS_STATES}]；VRAM=[$(gpu_memory_snapshot)]${dependency_status}"
}

show_failed_worker_logs() {
  local ids="${1:-}" id
  [[ -n "${ids}" ]] || return 0
  IFS=',' read -r -a failed_id_list <<<"${ids}"
  for id in "${failed_id_list[@]}"; do
    warn "Worker ${id} 启动失败，最近日志如下。"
    journalctl -u "$(worker_unit "${id}")" -n 80 --no-pager >&2 || true
  done
}

wait_worker_batch() {
  local ids="${1:?}" timeout="${2:-${START_TIMEOUT}}" started now elapsed
  started=$(date +%s)
  while true; do
    now=$(date +%s)
    elapsed=$((now - started))
    # 给刚进入队列的 --no-block 启动几秒时间离开 inactive 状态。
    collect_worker_progress "${ids}" "$((elapsed < 10 ? 1 : 0))"
    log_worker_progress "${elapsed}" "批次加载中"
    if (( PROGRESS_HEALTHY == PROGRESS_TOTAL )); then
      log "Worker 批次 ${ids} 已全部就绪。"
      return 0
    fi
    if (( PROGRESS_FAILED > 0 )); then
      show_failed_worker_logs "${PROGRESS_FAILED_IDS}"
      return 1
    fi
    if (( elapsed >= timeout )); then
      warn "Worker 批次 ${ids} 启动超时（${timeout}s）。"
      IFS=',' read -r -a timeout_id_list <<<"${ids}"
      for id in "${timeout_id_list[@]}"; do
        worker_health_fast "${id}" || journalctl -u "$(worker_unit "${id}")" -n 40 --no-pager >&2 || true
      done
      return 1
    fi
    sleep 10
  done
}

healthy_worker_ids() {
  local id out=""
  for ((id = 0; id < INSTANCE_COUNT; id++)); do
    if worker_is_active "${id}" && worker_health "${id}"; then
      out+="${out:+,}${id}"
    fi
  done
  printf '%s\n' "${out}"
}

gateway_display_name() {
  case "${GATEWAY_KIND}" in
    newapi) printf 'New API\n' ;;
    litellm) printf 'LiteLLM\n' ;;
    bifrost) printf 'Bifrost\n' ;;
    omniroute) printf 'OmniRoute\n' ;;
  esac
}

gateway_config_path() {
  case "${GATEWAY_KIND}" in
    newapi) printf '%s\n' "${NEWAPI_PLAN}" ;;
    litellm) printf '%s\n' "${LITELLM_CONFIG}" ;;
    bifrost) printf '%s\n' "${BIFROST_CONFIG}" ;;
    omniroute) printf '%s\n' "${OMNIROUTE_PLAN}" ;;
  esac
}

gateway_ui_path() {
  printf '/ui/\n'
}

gateway_helper() {
  export SERVED_MODEL_NAME WORKER_BASE_PORT MAX_NUM_SEQS MAX_MODEL_LEN MAX_OUTPUT_TOKENS ROUTING_STRATEGY
  export GATEWAY_DB_PORT POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD BACKEND_API_KEY
  export GATEWAY_API_KEY BIFROST_ENCRYPTION_KEY UI_USERNAME UI_PASSWORD DATABASE_URL
  export SUPPORTS_IMAGE_INPUT SUPPORTS_OCR OMNIROUTE_JWT_SECRET OMNIROUTE_API_KEY_SECRET
  export OMNIROUTE_STORAGE_ENCRYPTION_KEY ACCOUNT_PORT ACCOUNT_BIND
  export DOCKER_NETWORK GATEWAY_INTERNAL_PORT
  "${GATEWAY_HELPER}" "$@"
}

account_helper() {
  export ACCOUNT_BIND ACCOUNT_PORT ACCOUNT_PUBLIC_URL ACCOUNT_API_PUBLIC_URL
  export ACCOUNT_ADMIN_USERNAME ACCOUNT_ADMIN_USERNAME_B64 ACCOUNT_ADMIN_PASSWORD ACCOUNT_DB_PATH
  export ACCOUNT_REGISTRATION_ENABLED ACCOUNT_ALLOWED_EMAIL_DOMAINS
  export ACCOUNT_DEFAULT_WELCOME_BALANCE ACCOUNT_DEFAULT_QUOTA_TOKENS ACCOUNT_QUOTA_RESET ACCOUNT_QUOTA_RESET_TIME
  export SMTP_HOST SMTP_PORT SMTP_SECURITY SMTP_USERNAME SMTP_PASSWORD SMTP_FROM
  export GATEWAY_API_KEY API_PORT GATEWAY_INTERNAL_PORT SUPPORTS_OCR
  # MySQL 能力激活后，CLI 与 systemd 门户必须使用同一套固定驱动环境；否则门户
  # 已能连接 MySQL，而 llmctl info 等维护命令仍会因系统 Python 缺少驱动失败。
  if [[ -r "${ACCOUNT_MYSQL_CAPABILITY}" ]] \
    && jq -e '.enabled == true' "${ACCOUNT_MYSQL_CAPABILITY}" >/dev/null 2>&1 \
    && [[ -x "${ACCOUNT_MYSQL_VENV}/bin/python" ]]; then
    "${ACCOUNT_MYSQL_VENV}/bin/python" "${ACCOUNT_HELPER}" "$@"
  else
    "${ACCOUNT_HELPER}" "$@"
  fi
}

persisted_published_origin() {
  [[ "${GATEWAY_KIND:-}" == omniroute && -f "${ACCOUNT_DB_PATH:-/nonexistent}" ]] || return 0
  account_helper dump-config 2>/dev/null | jq -r '.settings.published_origin // ""' 2>/dev/null || true
}

effective_account_origin() {
  local persisted
  persisted=$(persisted_published_origin)
  if [[ -n "${persisted}" ]]; then
    printf '%s\n' "${persisted%/}"
  elif [[ -n "${ACCOUNT_API_PUBLIC_URL:-}" ]]; then
    printf '%s\n' "${ACCOUNT_API_PUBLIC_URL%/}"
  elif [[ -n "${ACCOUNT_PUBLIC_URL:-}" ]]; then
    persisted="${ACCOUNT_PUBLIC_URL%/}"
    printf '%s\n' "${persisted%/ui}"
  else
    public_local_base_url
  fi
}

render_router_config() {
  local worker_ids="${1:-}" output
  [[ -n "${worker_ids}" ]] || die "没有健康 Worker，拒绝生成空路由。"
  output=$(gateway_config_path)
  [[ "${GATEWAY_KIND}" != bifrost ]] || install -d -m 750 "${BIFROST_DIR}"
  gateway_helper render --gateway "${GATEWAY_KIND}" --worker-ids "${worker_ids}" --output "${output}"
  chown root:root "${output}"
  chmod 640 "${output}"
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    log "OmniRoute 期望状态已生成，健康后端：${worker_ids}；策略：逐请求 round-robin（关闭会话粘性及粘性轮转批量）。"
  else
    log "$(gateway_display_name) 配置已生成，健康后端：${worker_ids}；策略：${ROUTING_STRATEGY}。"
  fi
}

gateway_process_health() {
  local base_url
  base_url=$(router_local_base_url)
  case "${GATEWAY_KIND}" in
    newapi) curl --noproxy '*' -fsS --max-time 3 "${base_url}/api/status" >/dev/null 2>&1 ;;
    litellm) curl --noproxy '*' -fsS --max-time 3 "${base_url}/health/liveliness" >/dev/null 2>&1 ;;
    bifrost) curl --noproxy '*' -fsS --max-time 3 "${base_url}/health" >/dev/null 2>&1 ;;
    omniroute) curl --noproxy '*' -fsS --max-time 3 "${base_url}/api/health/ping" >/dev/null 2>&1 ;;
  esac
}

wait_gateway_process() {
  local started now timeout=120
  started=$(date +%s)
  while true; do
    gateway_process_health && return 0
    if ! systemctl is-active --quiet llm-router.service; then
      journalctl -u llm-router.service -n 80 --no-pager >&2 || true
      return 1
    fi
    now=$(date +%s)
    (( now - started < timeout )) || die "$(gateway_display_name) 进程启动超时"
    sleep 2
  done
}

reconcile_gateway() {
  local worker_ids="${1:?}"
  export GATEWAY_LOCAL_URL
  GATEWAY_LOCAL_URL=$(router_local_base_url)
  case "${GATEWAY_KIND}" in
    newapi)
      log "自动初始化 New API，并同步 ${worker_ids} 个健康 Worker、管理员和调用密钥..."
      gateway_helper reconcile-newapi --worker-ids "${worker_ids}" --secrets-file "${SECRETS_ENV}"
      ;;
    omniroute)
      if [[ -s "${DEPLOYMENT_REGISTRY:-/etc/llm-cluster/deployments.json}" ]]; then
        log "自动初始化 OmniRoute，并根据多模型注册表同步全部模型、实例、Combo 和管理密钥..."
        gateway_helper reconcile-omniroute-registry \
          --registry "${DEPLOYMENT_REGISTRY:-/etc/llm-cluster/deployments.json}" \
          --secrets-file "${SECRETS_ENV}"
      else
        log "自动初始化 OmniRoute，并同步 ${worker_ids} 个健康 Worker、模型、Combo 和管理密钥..."
        gateway_helper reconcile-omniroute --worker-ids "${worker_ids}" --secrets-file "${SECRETS_ENV}"
      fi
      if (( SUPPORTS_IMAGE_INPUT == 1 )); then
        log "OmniRoute Vision Bridge 已关闭：当前模型原生支持图片，图片和 PDF 将直接转发给 vLLM。"
      fi
      ;;
    *) return 0 ;;
  esac
  # 同步过程可能原子替换受管网关 Key。
  # shellcheck disable=SC1090
  source "${SECRETS_ENV}"
  log "$(gateway_display_name) 自动配置完成。"
}

ensure_database_ready() {
  [[ "${GATEWAY_KIND}" != omniroute ]] || return 0
  local started now
  systemctl start llm-database.service
  started=$(date +%s)
  until database_health; do
    systemctl is-active --quiet llm-database.service || die "PostgreSQL 启动失败；请查看 llmctl logs database"
    now=$(date +%s)
    (( now - started < 120 )) || die "PostgreSQL 启动超时"
    sleep 2
  done
}

refresh_router() {
  local ids
  ids=$(healthy_worker_ids)
  if [[ -z "${ids}" ]]; then
    systemctl stop llm-router.service 2>/dev/null || true
    warn "没有健康 Worker，$(gateway_display_name) 已停止。"
    return 0
  fi
  ensure_database_ready
  render_router_config "${ids}"
  systemctl restart llm-router.service
  wait_gateway_process
  reconcile_gateway "${ids}"
  wait_router
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    systemctl restart llm-account.service
    wait_account_portal
  fi
}

reload_gateway_api_key() {
  local key value current="" legacy=""
  [[ -r "${SECRETS_ENV}" ]] || return 1
  while IFS='=' read -r key value; do
    case "${key}" in
      GATEWAY_API_KEY) current="${value}"; break ;;
      LITELLM_MASTER_KEY) legacy="${value}" ;;
    esac
  done <"${SECRETS_ENV}"
  current="${current:-${legacy}}"
  [[ -n "${current}" ]] || return 1
  GATEWAY_API_KEY="${current}"
  LITELLM_MASTER_KEY="${current}"
}

router_health() {
  local base_url
  # New API 会在启动观察器运行后才创建受管 Token。每次就绪探测都重新加载
  # 原子替换、仅 root 可读的 secrets 文件，使长时观察与并发 Key 轮换收敛。
  reload_gateway_api_key || return 1
  base_url=$(router_local_base_url)
  curl --noproxy '*' -fsS --max-time 3 \
    -H "Authorization: Bearer ${GATEWAY_API_KEY}" \
    "${base_url}/v1/models" >/dev/null 2>&1
}

router_local_base_url() {
  # API_PORT 回退保证 2.4 之前配置仍可做源码诊断；已安装的 2.4 配置总会定义
  # 隔离的内部端口。
  printf 'http://127.0.0.1:%s\n' "${GATEWAY_INTERNAL_PORT:-${API_PORT}}"
}

public_local_base_url() {
  case "${API_BIND}" in
    0.0.0.0) printf 'http://127.0.0.1:%s\n' "${API_PORT}" ;;
    ::) printf 'http://[::1]:%s\n' "${API_PORT}" ;;
    *:*) printf 'http://[%s]:%s\n' "${API_BIND}" "${API_PORT}" ;;
    *) printf 'http://%s:%s\n' "${API_BIND}" "${API_PORT}" ;;
  esac
}

public_router_health() {
  reload_gateway_api_key || return 1
  curl --noproxy '*' -fsS --max-time 5 \
    -H "Authorization: Bearer ${GATEWAY_API_KEY}" \
    "$(public_local_base_url)/v1/models" >/dev/null 2>&1
}

public_ui_health() {
  curl --noproxy '*' -fsS --max-time 5 "$(public_local_base_url)/ui/" >/dev/null 2>&1
}

render_nginx_config() {
  local listen_address ui_block server_names
  case "${API_BIND}" in
    ::) listen_address="[::]:${API_PORT}" ;;
    *) listen_address="${API_BIND}:${API_PORT}" ;;
  esac
  server_names="localhost 127.0.0.1 $(hostname 2>/dev/null || true) $(hostname -f 2>/dev/null || true) $(hostname -I 2>/dev/null || true) _"
  case "${GATEWAY_KIND}" in
    omniroute)
      ui_block="
  location ^~ /ui/ {
    proxy_pass http://127.0.0.1:${ACCOUNT_PORT};
    proxy_buffering off;
  }
  location ^~ /portal-api/ {
    proxy_pass http://127.0.0.1:${ACCOUNT_PORT};
    proxy_buffering off;
  }
  # 公开认证端点除门户身份锁定外，再增加一层按 IP 限流。精确 location 覆盖
  # 通用 portal-api 代理，同时不限制推理流量。
  location = /portal-api/auth/login {
    limit_req zone=llmctl_auth burst=10 nodelay;
    proxy_pass http://127.0.0.1:${ACCOUNT_PORT};
    proxy_buffering off;
  }
  location = /portal-api/auth/register {
    limit_req zone=llmctl_auth burst=10 nodelay;
    proxy_pass http://127.0.0.1:${ACCOUNT_PORT};
    proxy_buffering off;
  }
  location = /portal-api/auth/verify {
    limit_req zone=llmctl_auth burst=10 nodelay;
    proxy_pass http://127.0.0.1:${ACCOUNT_PORT};
    proxy_buffering off;
  }
  location = /base_ui { return 302 /base_ui/; }
  location ^~ /base_ui/ {
    rewrite ^/base_ui/(.*)$ /\$1 break;
    proxy_pass http://127.0.0.1:${GATEWAY_INTERNAL_PORT};
    proxy_buffering off;
  }"
      ;;
    litellm)
      ui_block="
  location ^~ /ui/ {
    proxy_pass http://127.0.0.1:${GATEWAY_INTERNAL_PORT};
    proxy_buffering off;
  }"
      ;;
    *)
      ui_block="
  location ^~ /ui/ {
    rewrite ^/ui/(.*)$ /\$1 break;
    proxy_pass http://127.0.0.1:${GATEWAY_INTERNAL_PORT};
    proxy_buffering off;
  }"
      ;;
  esac
  cat <<EOF
# 由 LLMCtl ${CTL_VERSION} 生成；请勿手工编辑，使用 llmctl tune/info。
map \$http_upgrade \$llmctl_connection_upgrade {
  default upgrade;
  '' close;
}

limit_req_zone \$binary_remote_addr zone=llmctl_auth:10m rate=30r/m;

server {
  listen ${listen_address};
  # 精确主机名/IP 让隔离 server 可与同一监听 socket 上的既有 Nginx 站点共存。
  server_name ${server_names};
  server_tokens off;
  client_max_body_size 128m;

  proxy_http_version 1.1;
  proxy_set_header Host \$host;
  proxy_set_header X-Real-IP \$remote_addr;
  # 不保留客户端提供的 X-Forwarded-For；门户仅信任本机 Nginx 注入的该 Header，
  # 用于审计和登录限流。
  proxy_set_header X-Forwarded-For \$remote_addr;
  proxy_set_header X-Forwarded-Proto \$scheme;
  proxy_set_header Upgrade \$http_upgrade;
  proxy_set_header Connection \$llmctl_connection_upgrade;
  proxy_connect_timeout 30s;
  proxy_send_timeout 7200s;
  proxy_read_timeout 7200s;
  proxy_request_buffering off;
  limit_req_status 429;

  add_header X-Content-Type-Options "nosniff" always;
  add_header Referrer-Policy "no-referrer" always;
  add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;

  location = / { return 302 /ui/; }
  location = /ui { return 302 /ui/; }
${ui_block}

  location ^~ /v1/ {
    proxy_pass http://127.0.0.1:${GATEWAY_INTERNAL_PORT};
    proxy_buffering off;
    add_header X-Accel-Buffering no always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;
  }
  location ^~ /v1beta/ {
    proxy_pass http://127.0.0.1:${GATEWAY_INTERNAL_PORT};
    proxy_buffering off;
    add_header X-Accel-Buffering no always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;
  }

  # 原生网关静态资源、管理 API、OAuth 回调与深层链接。
  location / {
    proxy_pass http://127.0.0.1:${GATEWAY_INTERNAL_PORT};
    proxy_buffering off;
  }
}
EOF
}

cmd_nginx_install() {
  require_root
  load_config
  command -v nginx >/dev/null 2>&1 || die "Nginx 未安装；请先安装 nginx 软件包"
  local temporary rollback="" backup="${NGINX_STATE_DIR}/previous.conf"
  local mode_file="${NGINX_STATE_DIR}/install-mode" install_mode=""
  install -d -m 700 "${NGINX_STATE_DIR}"
  temporary=$(mktemp /etc/nginx/conf.d/.llm-cluster.conf.XXXXXX)
  rollback=$(mktemp "${NGINX_STATE_DIR}/rollback.XXXXXX")
  render_nginx_config >"${temporary}"
  chmod 644 "${temporary}"
  if [[ -f "${NGINX_CONFIG}" ]]; then
    install -m 600 "${NGINX_CONFIG}" "${rollback}"
  fi
  if [[ ! -f "${mode_file}" ]]; then
    if [[ -f "${NGINX_CONFIG}" ]] && ! grep -q '^# Generated by LLMCtl ' "${NGINX_CONFIG}"; then
      install -m 600 "${NGINX_CONFIG}" "${backup}"
      install_mode=replaced
    elif [[ -f "${backup}" ]]; then
      # 兼容早期 2.4 开发版：该版本在 install-mode 标记出现前已捕获同名原站点。
      install_mode=replaced
    else
      rm -f "${backup}"
      install_mode=created
    fi
    printf '%s\n' "${install_mode}" >"${mode_file}"
    chmod 600 "${mode_file}"
  fi
  mv -f "${temporary}" "${NGINX_CONFIG}"
  if ! nginx -t; then
    if [[ -s "${rollback}" ]]; then
      install -m 644 "${rollback}" "${NGINX_CONFIG}"
    else
      rm -f "${NGINX_CONFIG}"
    fi
    rm -f "${rollback}"
    nginx -t >/dev/null 2>&1 || true
    die "Nginx 配置校验失败，已恢复修改前状态"
  fi
  rm -f "${rollback}"
  systemctl enable nginx.service >/dev/null
  if systemctl is-active --quiet nginx.service; then
    systemctl reload nginx.service
  else
    systemctl start nginx.service
  fi
  log "Nginx 本机统一入口已配置：$(public_local_base_url)/ui/ 与 $(public_local_base_url)/v1/；域名、80/443 和 TLS 仍由外部出口管理。"
}

cmd_nginx() {
  require_root
  load_config
  case "${1:-status}" in
    apply) cmd_nginx_install ;;
    test)
      command -v nginx >/dev/null 2>&1 || die "Nginx 未安装"
      nginx -t
      ;;
    status)
      printf 'CONFIG=%s\n' "${NGINX_CONFIG}"
      printf 'LISTEN=%s:%s\n' "${API_BIND}" "${API_PORT}"
      printf 'ACTIVE=%s\n' "$(systemctl is-active nginx.service 2>/dev/null || printf unknown)"
      printf 'ENABLED=%s\n' "$(systemctl is-enabled nginx.service 2>/dev/null || printf unknown)"
      ;;
    *) die "nginx 子命令必须是 apply|test|status" ;;
  esac
}

remove_nginx_config() {
  local backup="${NGINX_STATE_DIR}/previous.conf" mode_file="${NGINX_STATE_DIR}/install-mode"
  local install_mode=created action=deleted
  [[ -r "${mode_file}" ]] && install_mode=$(<"${mode_file}")
  if [[ "${install_mode}" == replaced && -f "${backup}" ]]; then
    install -m 644 "${backup}" "${NGINX_CONFIG}"
    action=restored
  else
    [[ -f "${NGINX_CONFIG}" ]] || return 0
    rm -f "${NGINX_CONFIG}"
  fi
  if command -v nginx >/dev/null 2>&1; then
    if nginx -t >/dev/null 2>&1; then
      if systemctl is-active --quiet nginx.service; then
        systemctl reload nginx.service || warn "Nginx 配置已更新，但 reload 失败；请手工检查 nginx.service"
      fi
    else
      warn "卸载后的 Nginx 全局配置校验失败；未 reload，请检查其他现有站点。"
    fi
  fi
  if [[ "${action}" == restored ]]; then
    log "已恢复安装前同名 Nginx 配置；现有 Nginx 软件包和其他站点均保留。"
  else
    log "已删除 LLMCtl 的 Nginx 配置；现有 Nginx 软件包和其他站点均保留。"
  fi
}

database_health() {
  [[ "${GATEWAY_KIND}" != omniroute ]] || return 0
  docker exec llm-database pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1
}

account_portal_health() {
  [[ "${GATEWAY_KIND}" == omniroute ]] || return 0
  curl --noproxy '*' -fsS --max-time 5 "$(account_local_base_url)/health" >/dev/null 2>&1
}

account_portal_ready() {
  [[ "${GATEWAY_KIND}" == omniroute ]] || return 0
  curl --noproxy '*' -fsS --max-time 5 "$(account_local_base_url)/ready" >/dev/null 2>&1
}

account_local_base_url() {
  case "${ACCOUNT_BIND}" in
    0.0.0.0) printf 'http://127.0.0.1:%s\n' "${ACCOUNT_PORT}" ;;
    ::) printf 'http://[::1]:%s\n' "${ACCOUNT_PORT}" ;;
    *:*) printf 'http://[%s]:%s\n' "${ACCOUNT_BIND}" "${ACCOUNT_PORT}" ;;
    *) printf 'http://%s:%s\n' "${ACCOUNT_BIND}" "${ACCOUNT_PORT}" ;;
  esac
}

wait_account_portal() {
  [[ "${GATEWAY_KIND}" == omniroute ]] || return 0
  local started now
  started=$(date +%s)
  until account_portal_ready; do
    systemctl is-active --quiet llm-account.service || {
      journalctl -u llm-account.service -n 100 --no-pager >&2 || true
      die "账户门户启动失败；请查看 llmctl logs account"
    }
    now=$(date +%s)
    (( now - started < 60 )) || die "账户门户启动超时"
    sleep 2
  done
  log "账户门户已就绪：$(account_local_base_url)"
}

wait_router() {
  local started now timeout=90
  started=$(date +%s)
  while true; do
    router_health && { log "$(gateway_display_name) 已就绪。"; return 0; }
    if ! systemctl is-active --quiet llm-router.service; then
      journalctl -u llm-router.service -n 80 --no-pager >&2 || true
      return 1
    fi
    now=$(date +%s)
    (( now - started < timeout )) || die "$(gateway_display_name) API 验证超时"
    sleep 2
  done
}

start_worker_batch() {
  local batch_csv="${1:?}" id request_failed=0 wait_failed=0
  local -a batch_ids=() units=()
  IFS=',' read -r -a batch_ids <<<"${batch_csv}"
  for id in "${batch_ids[@]}"; do units+=("$(worker_unit "${id}")"); done
  log "并行启动 Worker：${batch_csv}"
  systemctl start --no-block "${units[@]}" || {
    warn "批次 ${batch_csv} 的 systemd 启动请求失败。"
    request_failed=1
  }
  wait_worker_batch "${batch_csv}" || wait_failed=1
  (( request_failed == 0 && wait_failed == 0 ))
}

start_worker_ids_batched() {
  local ids="${1:?}" id batch="" batch_size=0 failed=0
  local -a id_list=()
  IFS=',' read -r -a id_list <<<"${ids}"
  for id in "${id_list[@]}"; do
    if worker_health "${id}"; then
      log "Worker ${id} 已在运行。"
      continue
    fi
    batch+="${batch:+,}${id}"
    batch_size=$((batch_size + 1))
    if (( batch_size >= STARTUP_PARALLELISM )); then
      start_worker_batch "${batch}" || failed=1
      batch=""
      batch_size=0
    fi
  done
  if [[ -n "${batch}" ]]; then start_worker_batch "${batch}" || failed=1; fi
  if (( KEEPWARM_ENABLED == 1 )); then
    keepwarm_run_ids "${ids}" startup || \
      warn "$(ctl_l10n '至少一个 Worker 启动预热失败；健康检查已完成，服务继续启动。运行 llmctl keepwarm status 查看详情。' 'At least one Worker startup warm-up failed. Health checks completed and startup will continue; run llmctl keepwarm status for details.')"
  fi
  return "${failed}"
}

start_ids() {
  local ids="${1:?}" failed=0
  start_worker_ids_batched "${ids}" || failed=1
  refresh_router
  return "${failed}"
}

stop_ids() {
  local ids="${1:?}" id
  local -a units=()
  IFS=',' read -r -a id_list <<<"${ids}"
  for id in "${id_list[@]}"; do
    units+=("$(worker_unit "${id}")")
  done
  log "并发停止 Worker：${ids}"
  systemctl stop --no-block "${units[@]}" 2>/dev/null || true
  wait_worker_units_stopped "${ids}" 150 || {
    warn "Worker ${ids} 未能在 150 秒内停止；未执行后续配置或重启操作。"
    return 1
  }
  refresh_router
}

wait_worker_units_stopped() {
  local ids="${1:?}" timeout="${2:-150}" started now elapsed id state container running="" states=""
  started=$(date +%s)
  while true; do
    running=""
    states=""
    IFS=',' read -r -a stopping_id_list <<<"${ids}"
    for id in "${stopping_id_list[@]}"; do
      state=$(systemctl show "$(worker_unit "${id}")" -p ActiveState --value 2>/dev/null || printf unknown)
      container="llm-worker-${id}"
      if [[ "${state}" =~ ^(active|activating|deactivating|reloading)$ ]] || \
         [[ "$(docker inspect -f '{{.State.Running}}' "${container}" 2>/dev/null || true)" == true ]]; then
        running+="${running:+,}${id}"
        states+="${states:+,}${id}:${state}"
      fi
    done
    now=$(date +%s)
    elapsed=$((now - started))
    log "Worker 停止中 ${elapsed}s：remaining=[${states:-none}]；VRAM=[$(gpu_memory_snapshot)]"
    [[ -z "${running}" ]] && return 0
    (( elapsed < timeout )) || return 1
    sleep 5
  done
}

restart_ids() {
  local ids="${1:?}" id failed=0
  # 停止前先排除处于重启过程的 Worker。
  stop_ids "${ids}" || return 1
  start_worker_ids_batched "${ids}" || failed=1
  refresh_router
  return "${failed}"
}

cmd_status() {
  load_config
  local spec="${1:-all}" ids id port unit_state health_state router_state database_state devices max_images active_worker_count
  local registry="${CONFIG_DIR}/deployments.json" registry_line="" registry_active_workers=""
  local status_hub="" status_model_id="" status_revision="" status_path="" status_served="" status_aliases=""
  local status_tp="" status_instances="" status_max_seqs="" status_slot_limit="" worker_model=""
  local -a active_id_list=()
  if [[ -r "${registry}" ]] && jq -e '.schema_version == 1 and (.deployments|type == "object")' "${registry}" >/dev/null 2>&1; then
    registry_line=$(jq -r '
      first(.deployments|to_entries[]|select(.value.enabled != false)) as $entry
      | $entry.value as $d | .artifacts[$d.artifact_id] as $a
      | [($a.hub // "unknown"), ($d.model_id // "unknown"), ($a.revision // "unknown"),
         ($a.path // "unknown"), ($d.served_model_name // "unknown"),
         (($d.served_model_aliases // [])|join(",")),
         (($d.runtime.tensor_parallel_size // 0)|tostring),
         ([ $d.instances[]? | select(.kind == "local" and .enabled != false) ]|length|tostring),
         (($d.runtime.max_num_seqs // 0)|tostring)] | join("\u001f")
    ' "${registry}" 2>/dev/null || true)
    if [[ -n "${registry_line}" ]]; then
      IFS=$'\x1f' read -r status_hub status_model_id status_revision status_path status_served status_aliases status_tp status_instances status_max_seqs <<<"${registry_line}"
      registry_active_workers=$(jq -r '[.deployments[] | select(.enabled != false) | .instances[]? | select(.kind == "local" and .enabled != false) | .worker_id] | unique | sort | join(",")' "${registry}")
      status_slot_limit=$(jq -r '[.deployments[] | select(.enabled != false) as $d | ([ $d.instances[]? | select(.kind == "local" and .enabled != false) ]|length) * ($d.runtime.max_num_seqs // 0)] | add // 0' "${registry}")
    fi
  fi
  if [[ "${spec}" == "all" ]]; then ids="${registry_active_workers:-$(all_instance_ids)}"; else ids=$(resolve_ids "${spec}"); fi
  printf 'LLM 集群管理器: %s\n' "${CTL_VERSION}"
  if [[ -n "${registry_line}" ]]; then
    printf '模型: %s:%s @ %s（部署注册表）\n' "${status_hub}" "${status_model_id}" "${status_revision}"
  else
    printf '模型: %s:%s @ %s（旧版全局配置）\n' "${MODEL_HUB}" "${MODEL_ID}" "${MODEL_REVISION}"
  fi
  printf '架构/精度/任务: %s / %s / %s\n' "${MODEL_ARCHITECTURE}" "${MODEL_PRECISION}" "${MODEL_TASK}"
  printf '本地模型: %s\n' "${status_path:-${MODEL_ROOT}/current}"
  IFS=',' read -r -a active_id_list <<<"${registry_active_workers:-${ACTIVE_WORKERS}}"
  active_worker_count=${#active_id_list[@]}
  if [[ -n "${registry_line}" ]]; then
    printf '拓扑: TP=%s，物理 GPU=%s，活动实例数=%s，每实例 max-num-seqs=%s（调度槽上限=%s）\n' \
      "${status_tp}" "${PHYSICAL_GPU_COUNT}" "${status_instances}" "${status_max_seqs}" "${status_slot_limit}"
    printf '内部模型名: %s；兼容别名: %s\n' "${status_served}" "${status_aliases:-无}"
    printf '活动部署:\n'
    jq -r '.artifacts as $artifacts | .deployments | to_entries[] | select(.value.enabled != false) | .value as $d | $artifacts[$d.artifact_id] as $a | "  - \(.key): \($a.hub):\($d.model_id) @ \($a.revision)；served=\($d.served_model_name)；aliases=\(($d.served_model_aliases // [])|join(","))；TP=\($d.runtime.tensor_parallel_size)；instances=\([$d.instances[]? | select(.kind == "local" and .enabled != false)]|length)"' "${registry}"
  else
    printf '拓扑: TP=%s，物理 GPU=%s，实例数=%s，每实例 max-num-seqs=%s（已激活调度槽上限=%s）\n' \
      "${TP_SIZE}" "${PHYSICAL_GPU_COUNT}" "${INSTANCE_COUNT}" "${MAX_NUM_SEQS}" "$((active_worker_count * MAX_NUM_SEQS))"
  fi
  printf '规划参考: 当前模型/显存估算每实例 32K 级请求最多约 %s 个；长请求会降低实际并发\n' "${ESTIMATED_MAX_NUM_SEQS}"
  printf '单请求最大输出: %s Token（vLLM 服务端硬上限）\n' "${MAX_OUTPUT_TOKENS}"
  printf '启动并行度: 每批最多 %s 个 Worker\n' "${STARTUP_PARALLELISM}"
  if (( SUPPORTS_IMAGE_INPUT == 1 )); then
    max_images=$(jq -r '.image // "unknown"' <<<"${MM_LIMIT}" 2>/dev/null || printf unknown)
    printf '多模态: 支持；每请求最多 %s 张图片；完整测试按配置验证多图输入\n' "${max_images}"
  else
    printf '多模态: 当前模型不支持图片输入\n'
  fi
  printf '工具/思考: %s(parser=%s) / %s(parser=%s，可按请求关闭=%s)\n' \
    "${SUPPORTS_TOOL_CALLING}" "${TOOL_CALL_PARSER:-none}" "${SUPPORTS_REASONING}" \
    "${REASONING_PARSER:-none}" "${SUPPORTS_THINKING_TOGGLE}"
  printf '入口: http://%s:%s/v1  模型名: %s\n' "${API_BIND}" "${API_PORT}" "${status_served:-${SERVED_MODEL_NAME}}"
  printf '开机激活 Worker: %s\n' "${registry_active_workers:-${ACTIVE_WORKERS}}"
  router_state=$(systemctl is-active llm-router.service 2>/dev/null || true)
  printf '%s: %s (%s)\n' "$(gateway_display_name)" "${router_state:-unknown}" "$([[ -n "${router_state}" ]] && router_health && printf healthy || printf unhealthy)"
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    printf 'SQLite: OmniRoute=%s；账户门户=%s（隔离文件，无数据库服务实例）\n' "${OMNIROUTE_SQLITE}" "${ACCOUNT_DB_PATH}"
    printf '账户门户: %s (%s)  http://%s:%s\n' "$(systemctl is-active llm-account.service 2>/dev/null || true)" "$(account_portal_health && printf healthy || printf unhealthy)" "${ACCOUNT_BIND}" "${ACCOUNT_PORT}"
  else
    database_state=$(systemctl is-active llm-database.service 2>/dev/null || true)
    printf 'PostgreSQL: %s (%s，仅监听 127.0.0.1:%s)\n' "${database_state:-unknown}" "$([[ -n "${database_state}" ]] && database_health && printf healthy || printf unhealthy)" "${GATEWAY_DB_PORT}"
  fi
  printf '\n%-8s %-10s %-7s %-9s %-9s %-12s %-18s %s\n' INSTANCE GPUS PORT BOOT SYSTEMD HEALTH VRAM MODEL
  IFS=',' read -r -a id_list <<<"${ids}"
  for id in "${id_list[@]}"; do
    port=$(worker_port "${id}")
    devices=$(worker_devices "${id}")
    unit_state=$(systemctl is-active "$(worker_unit "${id}")" 2>/dev/null || true)
    worker_model=$(worker_served_model "${id}")
    health_state=down
    worker_health "${id}" && health_state=healthy
    local boot=no vram='n/a' gpu_id gpu_vram
    csv_has "${registry_active_workers:-${ACTIVE_WORKERS}}" "${id}" && boot=yes
    if command -v nvidia-smi >/dev/null 2>&1; then
      vram=""
      IFS=',' read -r -a gpu_list <<<"${devices}"
      for gpu_id in "${gpu_list[@]}"; do
        gpu_vram=$(nvidia-smi --id="${gpu_id}" --query-gpu=memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | awk -F, '{gsub(/ /,""); print $1"/"$2"M"}' || printf n/a)
        vram+="${vram:+ }${gpu_id}:${gpu_vram}"
      done
    fi
    printf '%-8s %-10s %-7s %-9s %-9s %-12s %-18s %s\n' "${id}" "${devices}" "${port}" "${boot}" "${unit_state:-unknown}" "${health_state}" "${vram}" "${worker_model}"
  done
}

cmd_info() {
  require_root
  load_config
  local redact=0 value public_host public_origin effective_public_origin id state portal_inventory="" portal_inventory_status=unavailable
  local portal_users="n/a" portal_groups="n/a" portal_models="n/a" portal_free="n/a"
  local portal_usage="n/a" portal_transactions="n/a" portal_audits="n/a" portal_integrity="n/a"
  local portal_grant_conversion_status="not-required"
  local portal_db_backend="sqlite" mysql_capability="disabled" mysql_driver="<未激活>"
  local mysql_host="<未配置>" mysql_port="3306" mysql_database="<未配置>" mysql_username="<未配置>"
  local mysql_password="" mysql_tls="preferred" mysql_ca="<empty>"
  local migration_status="idle" migration_stage="未开始" migration_progress="0" migration_backup="<none>" migration_error="<none>"
  local active_model_dir="" effective_routing_strategy="" workflow_base_models="" container_numa="" container_cpus=""
  activate_default_published_deployment
  active_model_dir="${MODEL_LOCAL_DIR:-${MODEL_ROOT}/current}"
  effective_routing_strategy="${ROUTING_STRATEGY}"
  [[ "${GATEWAY_KIND}" != omniroute ]] || effective_routing_strategy="round-robin（逐请求，关闭会话粘性）"
  case "${1:-}" in
    "") ;;
    --redact) redact=1 ;;
    *) die "用法：llmctl info [--redact]" ;;
  esac
  if (( redact == 0 )); then
    warn "以下恢复清单包含明文密码、API Key、数据库和 SMTP 凭据；只应在可信 root 终端查看。"
  fi
  secret_value() {
    if (( redact )); then printf '<redacted>'; else printf '%s' "${1:-<empty>}"; fi
  }
  public_host=$(hostname -I 2>/dev/null | awk '{print $1}')
  public_host="${public_host:-<服务器IP>}"
  public_origin="http://${public_host}:${API_PORT}"
  if [[ "${GATEWAY_KIND}" == omniroute && -f "${ACCOUNT_DB_PATH}" ]]; then
    if (( redact )); then
      portal_inventory=$(account_helper dump-config 2>/dev/null || true)
    else
      portal_inventory=$(account_helper dump-config --show-secrets 2>/dev/null || true)
    fi
    if [[ -n "${portal_inventory}" ]] && printf '%s' "${portal_inventory}" | jq -e '.settings and .counts and .database' >/dev/null 2>&1; then
      portal_inventory_status=loaded
      ACCOUNT_REGISTRATION_ENABLED=$(printf '%s' "${portal_inventory}" | jq -r '.settings.registration_enabled // "0"')
      ACCOUNT_ALLOWED_EMAIL_DOMAINS=$(printf '%s' "${portal_inventory}" | jq -r '.settings.allowed_domains // ""')
      ACCOUNT_DEFAULT_WELCOME_BALANCE=$(printf '%s' "${portal_inventory}" | jq -r '.settings.default_welcome_balance // "0"')
      ACCOUNT_DEFAULT_QUOTA_TOKENS=$(printf '%s' "${portal_inventory}" | jq -r '.settings.default_quota_tokens // "0"')
      ACCOUNT_QUOTA_RESET=$(printf '%s' "${portal_inventory}" | jq -r '.settings.default_quota_reset // "monthly"')
      ACCOUNT_QUOTA_RESET_TIME=$(printf '%s' "${portal_inventory}" | jq -r '.settings.default_quota_reset_time // "00:00"')
      portal_grant_conversion_status=$(printf '%s' "${portal_inventory}" | jq -r '.settings.token_grant_conversion_status // "not-required"')
      ACCOUNT_PORTAL_TITLE=$(printf '%s' "${portal_inventory}" | jq -r '.settings.portal_title // "LLMCtl"')
      ACCOUNT_PUBLISHED_ORIGIN=$(printf '%s' "${portal_inventory}" | jq -r '.settings.published_origin // ""')
      ACCOUNT_PUBLIC_URL=$(printf '%s' "${portal_inventory}" | jq -r '.settings.public_url // ""')
      ACCOUNT_API_PUBLIC_URL=$(printf '%s' "${portal_inventory}" | jq -r '.settings.api_public_url // ""')
      SMTP_HOST=$(printf '%s' "${portal_inventory}" | jq -r '.settings.smtp_host // ""')
      SMTP_PORT=$(printf '%s' "${portal_inventory}" | jq -r '.settings.smtp_port // "587"')
      SMTP_SECURITY=$(printf '%s' "${portal_inventory}" | jq -r '.settings.smtp_security // "starttls"')
      SMTP_USERNAME=$(printf '%s' "${portal_inventory}" | jq -r '.settings.smtp_username // ""')
      SMTP_PASSWORD=$(printf '%s' "${portal_inventory}" | jq -r '.settings.smtp_password // ""')
      SMTP_FROM=$(printf '%s' "${portal_inventory}" | jq -r '.settings.smtp_from // ""')
      portal_users=$(printf '%s' "${portal_inventory}" | jq -r '.counts.users')
      portal_groups=$(printf '%s' "${portal_inventory}" | jq -r '.counts.user_groups')
      portal_models=$(printf '%s' "${portal_inventory}" | jq -r '.counts.published_models')
      portal_free=$(printf '%s' "${portal_inventory}" | jq -r '.counts.free_resources')
      portal_usage=$(printf '%s' "${portal_inventory}" | jq -r '.counts.usage_ledger')
      portal_transactions=$(printf '%s' "${portal_inventory}" | jq -r '.counts.balance_transactions')
      portal_audits=$(printf '%s' "${portal_inventory}" | jq -r '.counts.audit_events')
      portal_integrity=$(printf '%s' "${portal_inventory}" | jq -r '.database.quick_check')
      portal_db_backend=$(printf '%s' "${portal_inventory}" | jq -r '.database.backend // "sqlite"')
      mysql_host=$(printf '%s' "${portal_inventory}" | jq -r '.database.host // "<未配置>"')
      mysql_port=$(printf '%s' "${portal_inventory}" | jq -r '.database.port // 3306')
      mysql_database=$(printf '%s' "${portal_inventory}" | jq -r '.database.database // "<未配置>"')
      mysql_username=$(printf '%s' "${portal_inventory}" | jq -r '.database.username // "<未配置>"')
      mysql_password=$(printf '%s' "${portal_inventory}" | jq -r '.database.password // ""')
      mysql_tls=$(printf '%s' "${portal_inventory}" | jq -r '.database.tls_mode // "preferred"')
      mysql_ca=$(printf '%s' "${portal_inventory}" | jq -r '.database.ca_file // "<empty>"')
    fi
  fi
  if [[ -r "${ACCOUNT_MYSQL_CAPABILITY}" ]]; then
    mysql_capability=$(jq -r 'if .enabled == true then "enabled" else "disabled" end' "${ACCOUNT_MYSQL_CAPABILITY}" 2>/dev/null || printf invalid)
    mysql_driver=$(jq -r '.driver // "unknown"' "${ACCOUNT_MYSQL_CAPABILITY}" 2>/dev/null || printf invalid)
  fi
  # 即使当前仍使用 SQLite，也要从受保护配置中展示已经保存的 MySQL 目标，便于长期维护与灾难恢复。
  if [[ -r "${ACCOUNT_DATABASE_CONFIG}" ]]; then
    portal_db_backend=$(jq -r '.active_backend // "sqlite"' "${ACCOUNT_DATABASE_CONFIG}" 2>/dev/null || printf invalid)
    mysql_host=$(jq -r '.host // "<未配置>"' "${ACCOUNT_DATABASE_CONFIG}" 2>/dev/null || printf invalid)
    mysql_port=$(jq -r '.port // 3306' "${ACCOUNT_DATABASE_CONFIG}" 2>/dev/null || printf invalid)
    mysql_database=$(jq -r '.database // "<未配置>"' "${ACCOUNT_DATABASE_CONFIG}" 2>/dev/null || printf invalid)
    mysql_username=$(jq -r '.username // "<未配置>"' "${ACCOUNT_DATABASE_CONFIG}" 2>/dev/null || printf invalid)
    mysql_password=$(jq -r '.password // ""' "${ACCOUNT_DATABASE_CONFIG}" 2>/dev/null || printf invalid)
    mysql_tls=$(jq -r '.tls_mode // "preferred"' "${ACCOUNT_DATABASE_CONFIG}" 2>/dev/null || printf invalid)
    mysql_ca=$(jq -r '.ca_file // "<empty>"' "${ACCOUNT_DATABASE_CONFIG}" 2>/dev/null || printf invalid)
  fi
  if [[ -r "${ACCOUNT_DATABASE_MIGRATION}" ]]; then
    migration_status=$(jq -r '.status // "idle"' "${ACCOUNT_DATABASE_MIGRATION}" 2>/dev/null || printf invalid)
    migration_stage=$(jq -r '.stage // "unknown"' "${ACCOUNT_DATABASE_MIGRATION}" 2>/dev/null || printf invalid)
    migration_progress=$(jq -r '.progress // 0' "${ACCOUNT_DATABASE_MIGRATION}" 2>/dev/null || printf 0)
    migration_backup=$(jq -r '.backup_path // .backup // "<none>"' "${ACCOUNT_DATABASE_MIGRATION}" 2>/dev/null || printf invalid)
    migration_error=$(jq -r '.error // "<none>"' "${ACCOUNT_DATABASE_MIGRATION}" 2>/dev/null || printf invalid)
  fi
  effective_public_origin="${ACCOUNT_PUBLISHED_ORIGIN:-${public_origin}}"

  printf '\n========== LLMCtl 恢复清单 / Recovery inventory ==========\n'
  printf '生成时间 / Generated: %s\n' "$(date --iso-8601=seconds 2>/dev/null || date)"
  local control_plane_commit="unknown" control_plane_upgraded="unknown"
  if [[ -r "${CONTROL_PLANE_RELEASE}" ]]; then
    control_plane_commit=$(awk -F= '$1 == "LLMCTL_COMMIT" {sub(/^[^=]*=/, ""); print; exit}' "${CONTROL_PLANE_RELEASE}")
    control_plane_upgraded=$(awk -F= '$1 == "LLMCTL_UPGRADED_AT" {sub(/^[^=]*=/, ""); print; exit}' "${CONTROL_PLANE_RELEASE}")
  fi
  printf 'LLMCtl 版本: %s\n控制面提交: %s\n控制面升级时间: %s\n安装语言: %s\n主机名: %s\n时区: %s\n' \
    "${CTL_VERSION}" "${control_plane_commit:-unknown}" "${control_plane_upgraded:-unknown}" "${INTERFACE_LANGUAGE:-zh}" "$(hostname)" "${TZ:-$(timedatectl show -p Timezone --value 2>/dev/null || printf unknown)}"

  printf '\n[主机与运行时 / Host and runtimes]\n'
  printf '操作系统: %s\n内核/架构: %s / %s\nCPU: %s；逻辑核=%s\n内存: %s\n' \
    "$(. /etc/os-release 2>/dev/null; printf '%s' "${PRETTY_NAME:-unknown}")" "$(uname -r)" "$(uname -m)" \
    "$(awk -F: '/model name/{sub(/^[[:space:]]+/,"",$2); print $2; exit}' /proc/cpuinfo 2>/dev/null || printf unknown)" \
    "$(nproc 2>/dev/null || printf unknown)" "$(awk '/MemTotal/{printf "%.1f GiB",$2/1048576}' /proc/meminfo 2>/dev/null || printf unknown)"
  printf 'NVIDIA 驱动: %s；GPU: %s\nDocker: %s\nNginx: %s\n' \
    "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || printf unavailable)" \
    "$(nvidia-smi --query-gpu=index,name,memory.total,pci.bus_id --format=csv,noheader 2>/dev/null | paste -sd ';' - || printf unavailable)" \
    "$(docker version --format '{{.Server.Version}}' 2>/dev/null || printf unavailable)" \
    "$(nginx -v 2>&1 | sed 's#nginx version: ##' || printf unavailable)"

  printf '\n[统一公开入口 / Public front door]\n'
  printf 'Nginx 监听: %s:%s\n对外发布地址: %s\nAPI Base URL: %s/v1\nWeb UI: %s/ui/\n' \
    "${API_BIND}" "${API_PORT}" "${ACCOUNT_PUBLISHED_ORIGIN:-<自动使用当前访问地址>}" \
    "${effective_public_origin}" "${effective_public_origin}"
  printf 'Nginx 状态: %s；开机自启: %s；配置: %s\n' \
    "$(systemd_property_state is-active nginx.service)" \
    "$(systemd_property_state is-enabled nginx.service)" "${NGINX_CONFIG}"
  printf 'TLS: 未由 LLMCtl 自动配置；如由现有 Nginx 站点终止 TLS，请以站点配置为准\n'
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    printf '企业门户: %s/ui/\n门户管理 API: %s/portal-api/\nOmniRoute 原生 UI: %s/base_ui/\n' \
      "${effective_public_origin}" "${effective_public_origin}" "${effective_public_origin}"
  else
    printf '原生网关 UI: %s/ui/\n' "${public_origin}"
  fi

  printf '\n[内部网络 / Internal networking]\n'
  printf 'Docker 网络: %s (%s)\n网关回环地址: http://127.0.0.1:%s\n' \
    "${DOCKER_NETWORK}" "$(docker network inspect "${DOCKER_NETWORK}" >/dev/null 2>&1 && printf present || printf missing)" "${GATEWAY_INTERNAL_PORT}"
  printf 'Worker 回环端口: 127.0.0.1:%s-%s\n' "${WORKER_BASE_PORT}" "$((WORKER_BASE_PORT + INSTANCE_COUNT - 1))"
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    printf '门户回环地址: http://127.0.0.1:%s（bind=%s）\n' "${ACCOUNT_PORT}" "${ACCOUNT_BIND}"
  else
    printf 'PostgreSQL 回环地址: 127.0.0.1:%s；容器内地址: llm-database:5432\n' "${GATEWAY_DB_PORT}"
  fi

  printf '\n[接入层与管理员 / Gateway and administrators]\n'
  printf '网关: %s (%s)\n镜像: %s\n路由策略: %s\n' "$(gateway_display_name)" "${GATEWAY_KIND}" "${GATEWAY_IMAGE}" "${effective_routing_strategy}"
  printf '网关镜像 ID: %s\n' "$(docker image inspect --format '{{.Id}}' "${GATEWAY_IMAGE}" 2>/dev/null || printf unavailable)"
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    printf '原生 UI 管理员用户名: 不适用（OmniRoute 原生 UI 仅使用密码）\n原生 UI 管理员密码: %s\n' "$(secret_value "${UI_PASSWORD}")"
  else
    printf '原生 UI 管理员用户名: %s\n原生 UI 管理员密码: %s\n' "${UI_USERNAME}" "$(secret_value "${UI_PASSWORD}")"
  fi
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    printf '门户管理员登录名: %s\n门户管理员密码: %s\n' "${ACCOUNT_ADMIN_USERNAME}" "$(secret_value "${ACCOUNT_ADMIN_PASSWORD}")"
  fi

  printf '\n[API 与内部密钥 / API and internal secrets]\n'
  printf '公开调用/维护 API Key: %s\nWorker 后端 API Key: %s\nLiteLLM Salt Key: %s\n' \
    "$(secret_value "${GATEWAY_API_KEY}")" "$(secret_value "${BACKEND_API_KEY}")" "$(secret_value "${LITELLM_SALT_KEY}")"
  printf 'New API Session Secret: %s\nBifrost Encryption Key: %s\n' \
    "$(secret_value "${NEWAPI_SESSION_SECRET:-}")" "$(secret_value "${BIFROST_ENCRYPTION_KEY:-}")"
  printf 'OmniRoute JWT Secret: %s\nOmniRoute API-Key Secret: %s\nOmniRoute Storage Encryption Key: %s\n' \
    "$(secret_value "${OMNIROUTE_JWT_SECRET:-}")" "$(secret_value "${OMNIROUTE_API_KEY_SECRET:-}")" "$(secret_value "${OMNIROUTE_STORAGE_ENCRYPTION_KEY:-}")"

  printf '\n[数据库 / Databases]\n'
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    printf 'OmniRoute SQLite: %s (mode=%s, size=%s)\n门户活动后端: %s\n门户 SQLite/回滚副本: %s (mode=%s, size=%s)\nOmniRoute 与门户数据库隔离: yes\n' \
      "${OMNIROUTE_SQLITE}" "$(stat -c %a "${OMNIROUTE_SQLITE}" 2>/dev/null || printf missing)" "$(du -h "${OMNIROUTE_SQLITE}" 2>/dev/null | awk '{print $1}' || printf missing)" \
      "${portal_db_backend}" "${ACCOUNT_DB_PATH}" "$(stat -c %a "${ACCOUNT_DB_PATH}" 2>/dev/null || printf missing)" "$(du -h "${ACCOUNT_DB_PATH}" 2>/dev/null | awk '{print $1}' || printf missing)"
    printf 'MySQL 能力: %s；驱动: %s\nMySQL: %s:%s/%s；用户名=%s；密码=%s；TLS=%s；CA=%s\n' \
      "${mysql_capability}" "${mysql_driver}" "${mysql_host}" "${mysql_port}" "${mysql_database}" "${mysql_username}" \
      "$(secret_value "${mysql_password}")" "${mysql_tls}" "${mysql_ca}"
    printf '迁移: status=%s progress=%s%% stage=%s\n迁移备份: %s\n迁移错误: %s\n' \
      "${migration_status}" "${migration_progress}" "${migration_stage}" "${migration_backup}" "${migration_error}"
    printf '门户持久配置读取: %s；活动后端检查: %s\n门户对象: users=%s groups=%s models=%s free-resources=%s usage=%s transactions=%s audits=%s\n' \
      "${portal_inventory_status}" "${portal_integrity}" "${portal_users}" "${portal_groups}" "${portal_models}" "${portal_free}" "${portal_usage}" "${portal_transactions}" "${portal_audits}"
  else
    printf 'PostgreSQL 数据库: %s\nPostgreSQL 用户名: %s\nPostgreSQL 密码: %s\nDATABASE_URL: %s\n数据卷: llm-cluster-gateway-postgres\n' \
      "${POSTGRES_DB}" "${POSTGRES_USER}" "$(secret_value "${POSTGRES_PASSWORD}")" "$(secret_value "${DATABASE_URL}")"
  fi

  printf '\n[注册、余额与 SMTP / Registration, balance and SMTP]\n'
  printf '门户品牌名称: %s\n允许注册: %s\n允许邮箱后缀: %s\n新用户一次性赠款（USD）: %s\n旧版 Token 迁移状态: %s\n对外发布地址: %s\n门户公开 URL: %s\nAPI 公开 URL: %s\n' \
    "${ACCOUNT_PORTAL_TITLE}" "${ACCOUNT_REGISTRATION_ENABLED}" "${ACCOUNT_ALLOWED_EMAIL_DOMAINS:-<empty>}" "${ACCOUNT_DEFAULT_WELCOME_BALANCE}" \
    "${portal_grant_conversion_status}" "${ACCOUNT_PUBLISHED_ORIGIN:-<自动>}" \
    "${effective_public_origin}/ui/" "${effective_public_origin}"
  printf 'SMTP: %s:%s (%s)\nSMTP 用户名: %s\nSMTP 密码: %s\n发件人: %s\n' \
    "${SMTP_HOST:-<empty>}" "${SMTP_PORT}" "${SMTP_SECURITY}" "${SMTP_USERNAME:-<empty>}" "$(secret_value "${SMTP_PASSWORD}")" "${SMTP_FROM:-<empty>}"

  load_saved_proxy
  load_runtime_proxy
  printf '\n[维护网络 / Maintenance networking]\n'
  printf '国际网络预检: 安装启动和 Hugging Face 模型搜索前自动执行\n保存的维护代理: %s\n维护 NO_PROXY: %s\n维护代理配置文件: %s\n' \
    "$(secret_value "${MAINTENANCE_PROXY:-}")" "${MAINTENANCE_NO_PROXY:-127.0.0.1,localhost,::1}" "${PROXY_ENV}"
  printf '推理运行时代理: %s\n运行时 NO_PROXY: %s\n运行时代理配置文件: %s\n作用范围: Router + 可选 Workflow；GPU Worker=不注入\n' \
    "$(secret_value "${RUNTIME_HTTPS_PROXY:-}")" "${RUNTIME_NO_PROXY}" "${RUNTIME_PROXY_ENV}"

  printf '\n[模型与推理 / Model and inference]\n'
  printf '活动部署: %s\nHub/模型/revision: %s / %s @ %s\n本地目录: %s\n模型服务 ID: %s\n架构/精度/任务: %s / %s / %s\n' \
    "${ACTIVE_DEPLOYMENT_ID:-legacy}" "${MODEL_HUB}" "${MODEL_ID}" "${MODEL_REVISION}" "${active_model_dir}" \
    "${SERVED_MODEL_NAME}" "${MODEL_ARCHITECTURE}" "${MODEL_PRECISION}" "${MODEL_TASK}"
  printf 'GPU/TP/实例: %s / %s / %s；开机激活: %s；启动并行度: %s\n' \
    "${PHYSICAL_GPU_COUNT}" "${TP_SIZE}" "${INSTANCE_COUNT}" "${ACTIVE_WORKERS}" "${STARTUP_PARALLELISM}"
  printf 'Context/max-seqs/batched-tokens/GPU-memory: %s / %s / %s / %s\n' \
    "${MAX_MODEL_LEN}" "${MAX_NUM_SEQS}" "${MAX_NUM_BATCHED_TOKENS}" "${GPU_MEMORY_UTILIZATION}"
  printf 'PLE/EP/prefix-cache/FlashInfer-autotune/custom-AR-off: %s / %s / %s / %s / %s\nMTP/KV/YaRN: %s / %s / %s\n' \
    "${PLE_CPU_OFFLOAD}" "${ENABLE_EXPERT_PARALLEL}" "${ENABLE_PREFIX_CACHING}" "${ENABLE_FLASHINFER_AUTOTUNE}" "${DISABLE_CUSTOM_ALL_REDUCE}" \
    "${MTP_SPECULATIVE_TOKENS}" "${KV_CACHE_DTYPE}" "${YARN_FACTOR}"
  printf '单请求最大输出 Token: %s（vLLM 服务端硬上限）\n' "${MAX_OUTPUT_TOKENS}"
  printf '图片/OCR/工具/思考/关闭思考: %s / %s / %s / %s / %s\n' \
    "${SUPPORTS_IMAGE_INPUT}" "${SUPPORTS_OCR}" "${SUPPORTS_TOOL_CALLING}" "${SUPPORTS_REASONING}" "${SUPPORTS_THINKING_TOGGLE}"
  printf 'vLLM 镜像: %s (ID=%s)\n' \
    "${VLLM_IMAGE}" "$(docker image inspect --format '{{.Id}}' "${VLLM_IMAGE}" 2>/dev/null || printf unavailable)"
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    printf 'PostgreSQL: 当前 OmniRoute 模式不使用\n'
  else
    printf 'PostgreSQL 镜像: %s (ID=%s)\n' "${POSTGRES_IMAGE}" \
      "$(docker image inspect --format '{{.Id}}' "${POSTGRES_IMAGE}" 2>/dev/null || printf unavailable)"
  fi

  printf '\n[服务、自启与 Worker / Services and workers]\n'
  printf 'llm-cluster: %s；enabled=%s\nllm-router: %s\n' \
    "$(systemd_property_state is-active llm-cluster.service)" \
    "$(systemd_property_state is-enabled llm-cluster.service)" \
    "$(systemd_property_state is-active llm-router.service)"
  printf 'Worker 保活: enabled=%s；interval=%ss；timeout=%ss；timer=%s/%s\n最近保活: %s；状态文件: %s\n' \
    "${KEEPWARM_ENABLED}" "${KEEPWARM_INTERVAL_SECONDS}" "${KEEPWARM_TIMEOUT_SECONDS}" \
    "$(systemd_property_state is-active llm-keepwarm.timer)" \
    "$(systemd_property_state is-enabled llm-keepwarm.timer)" \
    "$([[ -r "${KEEPWARM_STATE_FILE}" ]] && jq -r '"\(.finished_at) requested=\(.summary.requested) succeeded=\(.summary.succeeded) failed=\(.summary.failed)"' "${KEEPWARM_STATE_FILE}" 2>/dev/null || printf never)" \
    "${KEEPWARM_STATE_FILE}"
  printf '可插拔工作流: configured=%s；enabled=%s；service=%s/%s\n配置: %s；密钥: %s；运行时: %s\n' \
    "$([[ -r "${WORKFLOW_CONFIG}" ]] && printf yes || printf no)" "${WORKFLOW_ENABLED}" \
    "$(systemd_property_state is-active llm-workflow.service)" \
    "$(systemd_property_state is-enabled llm-workflow.service)" \
    "${WORKFLOW_CONFIG}" "${WORKFLOW_ENV}" "${WORKFLOW_RUNTIME}"
  if [[ -r "${WORKFLOW_CONFIG}" ]]; then
    workflow_base_models=$(jq -r '[.models|to_entries[]|select(.value.enabled)|.value.base_model]|unique|join(",")' "${WORKFLOW_CONFIG}" 2>/dev/null || printf invalid)
    printf '工作流监听/路由→底层模型/资源池: %s / %s / %s\n' \
      "$(jq -r '.listen // "unknown"' "${WORKFLOW_CONFIG}" 2>/dev/null || printf invalid)" \
      "$(jq -r '[.models|to_entries[]|select(.value.enabled)|"\(.key)→\(.value.base_model)"]|join(",")' "${WORKFLOW_CONFIG}" 2>/dev/null || printf invalid)" \
      "$(jq -r '[.pools|to_entries[]|"\(.key)=\(.value.targets|length)"]|join(",")' "${WORKFLOW_CONFIG}" 2>/dev/null || printf invalid)"
    if [[ -n "${workflow_base_models}" && ",${workflow_base_models}," != *",${SERVED_MODEL_NAME},"* ]]; then
      printf '工作流提示: 独立工作流仍引用 %s，未随活动模型 %s 自动切换\n' "${workflow_base_models}" "${SERVED_MODEL_NAME}"
    fi
  fi
  printf '多模型控制器: registry=%s；service=%s/%s；socket=%s\n运行时: %s；任务目录: %s；回滚目录: %s\n' \
    "$([[ -r "${CONFIG_DIR}/deployments.json" ]] && printf configured || printf legacy)" \
    "$(systemd_property_state is-active llm-model-control.service)" \
    "$(systemd_property_state is-enabled llm-model-control.service)" \
    "$([[ -S "${MODEL_CONTROL_SOCKET}" ]] && printf ready || printf unavailable)" \
    "${MODEL_CONTROL_RUNTIME}" "${STATE_DIR}/model-control/jobs" "${STATE_DIR}/model-control/backups"
  if [[ -S "${MODEL_CONTROL_SOCKET}" && -x "${MODEL_CONTROL_RUNTIME}" ]]; then
    printf '部署/GPU 分配: %s\n' \
      "$("${MODEL_CONTROL_RUNTIME}" --socket "${MODEL_CONTROL_SOCKET}" snapshot 2>/dev/null | jq -c '{revision:.registry.revision,deployments:[.registry.deployments|to_entries[]|{id:.key,enabled:.value.enabled,status:.value.status,public_ids:.value.public_model_ids,instances:[.value.instances[]|{id,kind,worker_id,gpu_devices,base_url,enabled}]}],active_jobs:[.jobs[]|select(.state|IN("waiting","running"))|{id,kind,state,phase,progress}]}' 2>/dev/null || printf unavailable)"
  fi
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    printf 'OmniRoute 运维: 状态=%s；备份=%s；最近评估=%s\n' \
      "${OMNIROUTE_MAINTENANCE_STATE_DIR}" "${OMNIROUTE_MAINTENANCE_BACKUP_DIR}" \
      "${OMNIROUTE_MAINTENANCE_STATE_DIR}/last-assessment.json"
  fi
  [[ "${GATEWAY_KIND}" == omniroute ]] && printf 'llm-account: %s\n' "$(systemd_property_state is-active llm-account.service)"
  [[ "${GATEWAY_KIND}" != omniroute ]] && printf 'llm-database: %s\n' "$(systemd_property_state is-active llm-database.service)"
  for ((id = 0; id < INSTANCE_COUNT; id++)); do
    state=$(systemd_property_state is-active "$(worker_unit "${id}")")
    container_numa=$(docker inspect -f '{{.HostConfig.CpusetMems}}' "llm-worker-${id}" 2>/dev/null || true)
    container_cpus=$(docker inspect -f '{{.HostConfig.CpusetCpus}}' "llm-worker-${id}" 2>/dev/null || true)
    printf 'Worker %s: GPU=%s port=%s systemd=%s boot=%s NUMA=%s CPUs=%s\n' "${id}" "$(worker_devices "${id}")" \
      "$(worker_port "${id}")" "${state}" "$(csv_has "${ACTIVE_WORKERS}" "${id}" && printf yes || printf no)" \
      "${container_numa:-系统默认}" "${container_cpus:-系统默认}"
  done

  printf '\n[文件、日志与维护 / Files, logs and maintenance]\n'
  printf '主配置: %s\n密钥配置: %s (mode=%s)\n状态目录: %s\n缓存目录: %s\n网关计划: %s\n' \
    "${CLUSTER_ENV}" "${SECRETS_ENV}" "$(stat -c %a "${SECRETS_ENV}" 2>/dev/null || printf unknown)" "${STATE_DIR}" "${CACHE_DIR}" "$(gateway_config_path)"
  printf '活动模型目录: %s\n旧版 current 兼容链接: %s/current -> %s\n门户程序: %s\n门户静态资源: %s\nNginx 配置备份目录: %s\n' \
    "${active_model_dir}" "${MODEL_ROOT}" "$(readlink -f "${MODEL_ROOT}/current" 2>/dev/null || printf missing)" \
    "${ACCOUNT_HELPER}" "${ACCOUNT_STATIC_DIR:-/usr/local/lib/llm-cluster/account_portal_ui}" "${NGINX_STATE_DIR}"
  printf 'systemd 单元: %s\nDocker 网络: %s\nDocker 数据卷: %s\n' \
    "$(find /etc/systemd/system -maxdepth 1 -type f \( -name 'llm-*.service' -o -name 'llm-*.timer' \) -printf '%f ' 2>/dev/null || printf unavailable)" \
    "${DOCKER_NETWORK}" "$(docker volume ls --format '{{.Name}}' 2>/dev/null | awk '/^llm-cluster-/{printf "%s ",$0}' || printf unavailable)"
  printf 'systemd 日志: journalctl -u llm-cluster -u llm-router\n完整健康检查: llmctl health\n完整状态: llmctl status\n'
  printf '===========================================================\n'
}

cmd_health() {
  load_config
  local id failures=0 running_count=0
  if command -v nginx >/dev/null 2>&1 && nginx -t >/dev/null 2>&1 && systemctl is-active --quiet nginx.service; then
    log "Nginx 统一入口: healthy"
  else
    warn "Nginx 统一入口: unhealthy"
    failures=$((failures + 1))
  fi
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    log "SQLite 数据库：隔离文件，无独立数据库实例"
    if account_portal_health; then log "账户门户: healthy"; else warn "账户门户: unhealthy"; failures=$((failures + 1)); fi
  elif database_health; then log "PostgreSQL: healthy"; else warn "PostgreSQL: unhealthy"; failures=$((failures + 1)); fi
  if router_health; then log "$(gateway_display_name): healthy"; else warn "$(gateway_display_name): unhealthy"; failures=$((failures + 1)); fi
  if public_router_health; then
    log "统一公开 API /v1: healthy"
  else
    warn "统一公开 API /v1: unhealthy（内部网关可能正常，但 Nginx 路由不可用）"
    failures=$((failures + 1))
  fi
  if public_ui_health; then
    log "统一 Web UI /ui/: healthy"
  else
    warn "统一 Web UI /ui/: unhealthy"
    failures=$((failures + 1))
  fi
  if [[ "${WORKFLOW_ENABLED}" == 1 || -e "${WORKFLOW_SERVICE_UNIT}" ]]; then
    if [[ -r "${WORKFLOW_CONFIG}" ]] && systemctl is-active --quiet llm-workflow.service && \
       curl --noproxy '*' -fsS --max-time 3 "$(workflow_http_origin)/readyz" >/dev/null; then
      log "Go 工作流数据面: healthy"
    else
      warn "Go 工作流数据面: unhealthy"
      failures=$((failures + 1))
    fi
  fi
  for ((id = 0; id < INSTANCE_COUNT; id++)); do
    if worker_is_active "${id}"; then
      running_count=$((running_count + 1))
      if worker_health "${id}"; then log "Worker ${id}: healthy"; else warn "Worker ${id}: unhealthy"; failures=$((failures + 1)); fi
    elif csv_has "${ACTIVE_WORKERS}" "${id}"; then
      warn "Worker ${id}: 应为开机激活实例，但当前未运行"
      failures=$((failures + 1))
    fi
  done
  (( running_count > 0 )) || { warn "没有运行中的 Worker"; failures=$((failures + 1)); }
  (( failures == 0 )) || return 1
}

cmd_startup() {
  load_config
  local action="${1:-status}" timeout interval=10 started now elapsed cluster_state pending_mode
  local database_ready gateway_ready portal_ready database_state gateway_state portal_state dependency_text
  case "${action}" in
    status)
      cluster_state=$(systemctl show llm-cluster.service -p ActiveState --value 2>/dev/null || printf unknown)
      [[ "${cluster_state}" == activating ]] && pending_mode=1 || pending_mode=0
      collect_worker_progress "${ACTIVE_WORKERS}" "${pending_mode}"
      log_worker_progress 0 "启动快照（cluster=${cluster_state}）"
      if [[ "${GATEWAY_KIND}" == omniroute ]]; then
        printf 'sqlite=embedded router=%s account=%s\n' \
          "$(router_health && printf healthy || printf unavailable)" \
          "$(account_portal_ready && printf healthy || { account_portal_health && printf degraded || printf unavailable; })"
      else
        printf 'database=%s router=%s\n' \
          "$(database_health && printf healthy || printf unavailable)" \
          "$(router_health && printf healthy || printf unavailable)"
      fi
      ;;
    watch)
      timeout=$((START_TIMEOUT * ((INSTANCE_COUNT + STARTUP_PARALLELISM - 1) / STARTUP_PARALLELISM) + 300))
      shift || true
      while (($#)); do
        case "$1" in
          --timeout) timeout="${2:?--timeout 需要秒数}"; shift 2 ;;
          --interval) interval="${2:?--interval 需要秒数}"; shift 2 ;;
          *) die "未知 startup watch 参数：$1" ;;
        esac
      done
      [[ "${timeout}" =~ ^[0-9]+$ ]] && (( timeout >= 30 && timeout <= 86400 )) || die "startup timeout 范围 30-86400 秒"
      [[ "${interval}" =~ ^[0-9]+$ ]] && (( interval >= 2 && interval <= 60 )) || die "startup interval 范围 2-60 秒"
      started=$(date +%s)
      log "持续观察集群启动；SSH 断开不影响 systemd，重连后可再次执行本命令。"
      while true; do
        now=$(date +%s)
        elapsed=$((now - started))
        cluster_state=$(systemctl show llm-cluster.service -p ActiveState --value 2>/dev/null || printf unknown)
        [[ "${cluster_state}" == activating || ${elapsed} -lt 10 ]] && pending_mode=1 || pending_mode=0
        collect_worker_progress "${ACTIVE_WORKERS}" "${pending_mode}"
        database_ready=0
        gateway_ready=0
        portal_ready=1
        database_state=unavailable
        gateway_state=unavailable
        portal_state=not-applicable
        if database_health; then database_ready=1; database_state=healthy; fi
        if router_health; then gateway_ready=1; gateway_state=healthy; fi
        if [[ "${GATEWAY_KIND}" == omniroute ]]; then
          database_state=embedded
          portal_ready=0
          portal_state=unavailable
          if account_portal_ready; then
            portal_ready=1
            portal_state=healthy
          elif account_portal_health; then
            portal_state=degraded
          fi
          dependency_text="；Dependencies=[SQLite:${database_state},$(gateway_display_name):${gateway_state},AccountPortal:${portal_state}]"
        else
          dependency_text="；Dependencies=[PostgreSQL:${database_state},$(gateway_display_name):${gateway_state}]"
        fi
        log_worker_progress "${elapsed}" "集群启动中（cluster=${cluster_state}）" \
          "${dependency_text}"
        if (( PROGRESS_HEALTHY == PROGRESS_TOTAL && database_ready == 1 && gateway_ready == 1 && portal_ready == 1 )); then
          if [[ "${GATEWAY_KIND}" == omniroute ]]; then
            log "集群已就绪：${PROGRESS_TOTAL} 个 Worker、OmniRoute 和账户门户全部健康。"
          else
            log "集群已就绪：PostgreSQL、${PROGRESS_TOTAL} 个 Worker 和 $(gateway_display_name) 全部健康。"
          fi
          return 0
        fi
        if [[ "${cluster_state}" == failed ]] || (( PROGRESS_FAILED > 0 )); then
          show_failed_worker_logs "${PROGRESS_FAILED_IDS}"
          journalctl -u llm-cluster.service -n 120 --no-pager >&2 || true
          return 1
        fi
        (( elapsed < timeout )) || {
          warn "集群启动观察超时（${timeout}s）；systemd 作业未被取消。"
          journalctl -u llm-cluster.service -n 120 --no-pager >&2 || true
          return 1
        }
        sleep "${interval}"
      done
      ;;
    *) die "startup 子命令必须是 status|watch" ;;
  esac
}

cmd_start()   { require_root; load_config; start_ids "$(resolve_ids "${1:-all}")"; }
cmd_stop() {
  require_root; load_config
  local spec="${1:-all}" ids
  # `stop all` 表示所有可能实例，包括管理员手工启动且不在活动清单中的实例。
  # 启动但不加入持久开机列表。
  [[ "${spec}" == all ]] && ids=$(all_instance_ids) || ids=$(resolve_ids "${spec}")
  stop_ids "${ids}"
}
cmd_restart() { require_root; load_config; restart_ids "$(resolve_ids "${1:-all}")"; }

cmd_shutdown() {
  require_root; load_config
  local timeout=180
  while (($#)); do
    case "$1" in
      --timeout) timeout="${2:?--timeout 需要秒数}"; shift 2 ;;
      *) die "未知 shutdown 参数：$1" ;;
    esac
  done
  [[ "${timeout}" =~ ^[0-9]+$ ]] && (( timeout >= 30 && timeout <= 900 )) || die "shutdown timeout 范围 30-900 秒"
  stop_managed_services_with_progress "${timeout}"
}

cmd_enable() {
  require_root; load_config
  local ids spec="${1:?请指定实例 ID 或 all}"
  [[ "${spec}" == all ]] && ids=$(all_instance_ids) || ids=$(resolve_ids "${spec}")
  ACTIVE_WORKERS=$(csv_add "${ACTIVE_WORKERS}" "${ids}")
  set_env_value "${CLUSTER_ENV}" ACTIVE_WORKERS "${ACTIVE_WORKERS}"
  log "开机激活列表：${ACTIVE_WORKERS}"
}

cmd_disable() {
  require_root; load_config
  local ids spec="${1:?请指定实例 ID 或 all}"
  [[ "${spec}" == all ]] && ids=$(all_instance_ids) || ids=$(resolve_ids "${spec}")
  local new_active
  new_active=$(csv_remove "${ACTIVE_WORKERS}" "${ids}")
  [[ -n "${new_active}" ]] || die "至少保留一个开机激活 Worker；若只是临时全部停止，请用 stop all。"
  set_env_value "${CLUSTER_ENV}" ACTIVE_WORKERS "${new_active}"
  log "开机激活列表：${new_active}"
}

cmd_activate() {
  require_root; load_config
  local spec="${1:?请指定实例 ID 或 all}" ids
  [[ "${spec}" == all ]] && ids=$(all_instance_ids) || ids=$(resolve_ids "${spec}")
  ACTIVE_WORKERS=$(csv_add "${ACTIVE_WORKERS}" "${ids}")
  set_env_value "${CLUSTER_ENV}" ACTIVE_WORKERS "${ACTIVE_WORKERS}"
  start_ids "${ids}"
}

cmd_deactivate() {
  require_root; load_config
  local spec="${1:?请指定实例 ID 或 all}" ids new_active
  [[ "${spec}" == all ]] && ids=$(all_instance_ids) || ids=$(resolve_ids "${spec}")
  new_active=$(csv_remove "${ACTIVE_WORKERS}" "${ids}")
  [[ -n "${new_active}" ]] || die "不能持久停用全部 Worker；可使用 stop all 临时停止。"
  stop_ids "${ids}"
  set_env_value "${CLUSTER_ENV}" ACTIVE_WORKERS "${new_active}"
  log "开机激活列表：${new_active}"
}

cmd_scale() {
  require_root; load_config
  local count="${1:?请指定数量}" i target="" added removed
  [[ "${count}" =~ ^[0-9]+$ ]] || die "数量必须是整数"
  (( count >= 1 && count <= INSTANCE_COUNT )) || die "数量必须在 1-${INSTANCE_COUNT}"
  for ((i = 0; i < count; i++)); do target+="${target:+,}${i}"; done
  added=$(csv_remove "${target}" "${ACTIVE_WORKERS}")
  removed=$(csv_remove "${ACTIVE_WORKERS}" "${target}")
  set_env_value "${CLUSTER_ENV}" ACTIVE_WORKERS "${target}"
  ACTIVE_WORKERS="${target}"
  [[ -z "${removed}" ]] || stop_ids "${removed}"
  [[ -z "${added}" ]] || start_ids "${added}"
  refresh_router
  log "集群已调整为 ${count} 个 Worker：${target}"
}

cmd_autostart() {
  require_root; load_config
  case "${1:-status}" in
    enable) systemctl enable llm-cluster.service; log "集群开机自启已启用。" ;;
    disable) systemctl disable llm-cluster.service; log "集群开机自启已禁用；当前服务不受影响。" ;;
    status) systemctl is-enabled llm-cluster.service 2>/dev/null || true ;;
    *) die "autostart 子命令必须是 enable|disable|status" ;;
  esac
}

cmd_router() {
  require_root; load_config
  case "${1:-status}" in
    start)   refresh_router ;;
    restart) refresh_router ;;
    reconcile)
      local healthy
      healthy=$(healthy_worker_ids)
      [[ -n "${healthy}" ]] || die "没有健康 Worker，无法同步接入层。"
      gateway_process_health || die "$(gateway_display_name) 当前不可用，无法在线同步。"
      render_router_config "${healthy}"
      reconcile_gateway "${healthy}"
      wait_router
      log "接入层已在线同步 ${healthy} 个健康 Worker；Router 和 Worker 均未重启。"
      ;;
    stop)    systemctl stop llm-router.service ;;
    status)  systemctl status llm-router.service --no-pager || true ;;
    *) die "router 子命令必须是 start|stop|restart|reconcile|status" ;;
  esac
}

cmd_database_enable_mysql() {
  [[ "${GATEWAY_KIND}" == omniroute ]] || \
    die "MySQL 可选能力只用于 OmniRoute 模式下的 LLMCtl 账户门户。"
  [[ -f /etc/systemd/system/llm-account.service ]] || \
    die "未找到 llm-account.service；请先运行 llmctl upgrade，再重试。"
  command -v python3 >/dev/null 2>&1 || die "缺少 python3，无法创建独立 MySQL 驱动环境。"
  id llm-account >/dev/null 2>&1 || die "缺少 llm-account 系统用户；账户门户安装不完整。"

  local venv_python="${ACCOUNT_MYSQL_VENV}/bin/python"
  local site_packages driver_version crypto_version capability_tmp dropin_tmp was_active=0
  install -d -m 750 -o llm-account -g llm-account "$(dirname "${ACCOUNT_MYSQL_VENV}")"
  if [[ ! -x "${venv_python}" ]]; then
    log "正在创建账户门户专用的 MySQL 驱动环境；不会安装或修改 MySQL Server。"
    python3 -m venv "${ACCOUNT_MYSQL_VENV}" || \
      die "无法创建 Python venv；请确认系统已安装 python3-venv。"
  fi

  load_saved_proxy
  export_proxy_env
  log "正在安装并锁定 MySQL 驱动及 caching_sha2_password 支持；现有 Router 与 GPU Worker 不受影响。"
  if ! "${venv_python}" -m pip install --disable-pip-version-check --timeout 30 --retries 2 \
      "PyMySQL==1.1.2" "cryptography==46.0.7"; then
    warn "MySQL 驱动下载失败，将检查国际网络并按需询问维护代理。"
    unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy || true
    MAINTENANCE_PROXY=""
    prompt_proxy_if_needed
    export_proxy_env
    "${venv_python}" -m pip install --disable-pip-version-check --timeout 30 --retries 2 \
      "PyMySQL==1.1.2" "cryptography==46.0.7" || \
      die "MySQL 驱动安装失败；可先运行 llmctl proxy set <IP> <端口> 后重试。"
  fi

  site_packages=$("${venv_python}" -c 'import site; print(site.getsitepackages()[0])')
  driver_version=$("${venv_python}" -c 'from importlib.metadata import version; print(version("PyMySQL"))')
  crypto_version=$("${venv_python}" -c 'from importlib.metadata import version; print(version("cryptography"))')
  PYTHONPATH="${site_packages}" /usr/bin/python3 -c 'import pymysql, cryptography' || \
    die "MySQL 驱动已安装但账户门户 Python 无法加载；未修改 systemd。"

  install -d -m 700 -o llm-account -g llm-account "${ACCOUNT_MYSQL_CONFIG_DIR}"
  capability_tmp=$(mktemp "${ACCOUNT_MYSQL_CONFIG_DIR}/.mysql-capability.XXXXXX")
  "${venv_python}" - "${capability_tmp}" "${venv_python}" "${driver_version}" "${crypto_version}" <<'PY'
import json
import os
import sys
import time

target, runtime_python, driver_version, crypto_version = sys.argv[1:]
payload = {
    "enabled": True,
    "version": 1,
    "runtime_python": runtime_python,
    "driver": f"PyMySQL {driver_version}; cryptography {crypto_version}",
    "activated_at": int(time.time()),
}
with open(target, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
PY
  chown llm-account:llm-account "${capability_tmp}"
  chmod 600 "${capability_tmp}"
  mv -f "${capability_tmp}" "${ACCOUNT_MYSQL_CAPABILITY}"

  install -d -m 755 "${ACCOUNT_MYSQL_DROPIN_DIR}"
  dropin_tmp=$(mktemp "${ACCOUNT_MYSQL_DROPIN_DIR}/.50-llmctl-mysql.XXXXXX")
  printf '[Service]\nEnvironment="PYTHONPATH=%s"\n' "${site_packages}" >"${dropin_tmp}"
  chmod 644 "${dropin_tmp}"
  mv -f "${dropin_tmp}" "${ACCOUNT_MYSQL_DROPIN}"
  systemctl daemon-reload

  systemctl is-active --quiet llm-account.service && was_active=1 || true
  if (( was_active == 1 )); then
    log "仅重启账户门户以加载 MySQL 驱动；Router 与 GPU Worker 保持运行。"
    systemctl restart llm-account.service
    wait_account_portal
  else
    log "账户门户当前未运行；MySQL 驱动会在下次启动时加载。"
  fi

  log "MySQL 能力已激活：PyMySQL ${driver_version}，cryptography ${crypto_version}。"
  log "下一步请登录 /ui/ 管理端，进入“数据库”，在 WebUI 中填写连接、测试并迁移。"
  log "本命令没有安装 MySQL Server、没有迁移数据，也没有改变 OmniRoute 自身的 SQLite。"
}

cmd_database() {
  require_root; load_config
  if [[ "${1:-}" == enable-mysql ]]; then
    cmd_database_enable_mysql
    return 0
  fi
  [[ "${GATEWAY_KIND}" != omniroute ]] || die "OmniRoute 模式使用两个隔离的 SQLite 文件，没有 PostgreSQL 服务；请使用 llmctl account。"
  case "${1:-status}" in
    start)
      systemctl start llm-database.service
      refresh_router
      ;;
    restart)
      systemctl stop llm-router.service 2>/dev/null || true
      systemctl restart llm-database.service
      refresh_router
      ;;
    stop)
      systemctl stop llm-router.service 2>/dev/null || true
      systemctl stop llm-database.service
      warn "数据库停止时 $(gateway_display_name) 的 Web UI、密钥和入口路由均不可用。"
      ;;
    status) systemctl status llm-database.service --no-pager || true ;;
    *) die "database 子命令必须是 enable-mysql|start|stop|restart|status" ;;
  esac
}

cmd_account() {
  require_root; load_config
  [[ "${GATEWAY_KIND}" == omniroute ]] || die "账户门户只在 OmniRoute 模式启用。"
  case "${1:-status}" in
    start) systemctl start llm-account.service; wait_account_portal ;;
    restart) systemctl restart llm-account.service; wait_account_portal ;;
    stop) systemctl stop llm-account.service ;;
    status) systemctl status llm-account.service --no-pager || true ;;
    url)
      local public_account_origin
      public_account_origin=$(effective_account_origin)
      printf 'PORTAL_URL=%s/ui/\n' "${public_account_origin}"
      printf 'OMNIROUTE_URL=%s\n' "${public_account_origin}"
      ;;
    *) die "account 子命令必须是 start|stop|restart|status|url" ;;
  esac
}

cmd_logs() {
  load_config
  local target="${1:-all}" id follow=""
  local -a journal_args=()
  if [[ "${target}" == -f ]]; then target=all; follow=-f; fi
  case "${target}" in
    all)
      [[ "${2:-}" == "-f" ]] && follow="-f"
      journal_args=(journalctl -u llm-router.service)
      if [[ "${GATEWAY_KIND:-litellm}" == omniroute ]]; then journal_args+=(-u llm-account.service); else journal_args+=(-u llm-database.service); fi
      IFS=',' read -r -a log_worker_ids <<<"${ACTIVE_WORKERS}"
      for id in "${log_worker_ids[@]}"; do journal_args+=(-u "$(worker_unit "${id}")"); done
      [[ -z "${follow}" ]] || journal_args+=("${follow}")
      journal_args+=(-n 400 --no-pager)
      "${journal_args[@]}"
      ;;
    worker)
      id="${2:?请指定 Worker ID}"
      csv_normalize "${id}" >/dev/null
      [[ "${3:-}" == "-f" ]] && follow="-f"
      journalctl -u "$(worker_unit "${id}")" ${follow} -n 200 --no-pager
      ;;
    router)
      [[ "${2:-}" == "-f" ]] && follow="-f"
      journalctl -u llm-router.service ${follow} -n 200 --no-pager
      ;;
    database)
      [[ "${GATEWAY_KIND}" != omniroute ]] || die "OmniRoute 模式没有 PostgreSQL 服务"
      [[ "${2:-}" == "-f" ]] && follow="-f"
      journalctl -u llm-database.service ${follow} -n 200 --no-pager
      ;;
    account)
      [[ "${GATEWAY_KIND}" == omniroute ]] || die "账户门户只在 OmniRoute 模式启用"
      [[ "${2:-}" == "-f" ]] && follow="-f"
      journalctl -u llm-account.service ${follow} -n 200 --no-pager
      ;;
    workflow)
      [[ "${2:-}" == "-f" ]] && follow="-f"
      journalctl -u llm-workflow.service ${follow} -n 300 --no-pager
      ;;
    model)
      [[ "${2:-}" == "-f" ]] && follow="-f"
      journalctl -u llm-model-control.service ${follow} -n 300 --no-pager
      ;;
    *) die "用法：llmctl logs [all] [-f] | logs worker <ID> [-f] | logs router [-f] | logs database [-f] | logs account [-f] | logs workflow [-f] | logs model [-f]" ;;
  esac
}

api_post() {
  local url="${1:?}" key="${2:?}" payload="${3:?}"
  curl --noproxy '*' --fail-with-body -sS --max-time 600 \
    -H "Authorization: Bearer ${key}" \
    -H 'Accept: application/json' \
    -H 'Content-Type: application/json' \
    --data-binary "@${payload}" "${url}"
}

smoke_response_summary() {
  local response="${1:-}" detected_format=unknown
  [[ -n "${response}" ]] || response='{}'
  if jq -c '{finish_reason:(.choices[0].finish_reason // null),
          stop_reason:(.choices[0].stop_reason // null),
          content_chars:((.choices[0].message.content // "") | length),
          reasoning_chars:((.choices[0].message.reasoning_content // .choices[0].message.reasoning // "") | length),
          tool_calls:((.choices[0].message.tool_calls // []) | length),
          error:(.error.message // null)}' <<<"${response}" 2>/dev/null; then
    return 0
  fi
  if [[ "${response}" == data:* || "${response}" == *$'\ndata:'* ]]; then
    detected_format=sse
  elif [[ "${response}" == '<!DOCTYPE html'* || "${response}" == '<html'* ]]; then
    detected_format=html
  fi
  jq -cn --arg format "${detected_format}" --argjson body_chars "${#response}" \
    '{invalid_json:true,detected_format:$format,body_chars:$body_chars}'
}

smoke_save_response() {
  local stage="${1:?}" response="${2:-}" timestamp destination tmp
  [[ -n "${response}" ]] || response='{}'
  [[ "${stage}" =~ ^[a-z0-9-]+$ ]] || stage=unknown
  install -d -m 700 "${SMOKE_DIAGNOSTIC_DIR}"
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  destination="${SMOKE_DIAGNOSTIC_DIR}/${timestamp}-${stage}.json"
  tmp=$(mktemp "${SMOKE_DIAGNOSTIC_DIR}/.response.XXXXXX")
  if ! jq . <<<"${response}" >"${tmp}" 2>/dev/null; then
    jq -n --arg raw "${response}" '{invalid_json:true,raw:$raw}' >"${tmp}"
  fi
  chmod 600 "${tmp}"
  mv -f "${tmp}" "${destination}"
  ln -sfn "$(basename "${destination}")" "${SMOKE_DIAGNOSTIC_DIR}/latest-${stage}.json"
  printf '%s\n' "${destination}"
}

smoke_fail_response() {
  local stage="${1:?}" reason="${2:?}" response="${3:-}" diagnostic summary
  [[ -n "${response}" ]] || response='{}'
  diagnostic=$(smoke_save_response "${stage}" "${response}")
  summary=$(smoke_response_summary "${response}")
  die "${reason}；响应摘要=${summary}；完整响应=${diagnostic}"
}

# 创建 Docker 守护进程与当前调用进程都能看到的 OCR 夹具目录。
# systemd 的 PrivateTmp 会让服务进程和 Docker 对 /tmp、/var/tmp 看到不同目录，
# 因此必须使用 LLMCtl 的持久状态根；调用者负责在冒烟结束时删除返回目录。
smoke_fixture_directory() {
  install -d -m 700 "${SMOKE_DIAGNOSTIC_DIR}"
  mktemp -d "${SMOKE_DIAGNOSTIC_DIR}/.fixture.XXXXXX"
}

# 使用当前 vLLM 镜像中的 Pillow 生成确定性的合成 OCR 图片。
# 第一个参数是宿主机可见的输出目录；函数成功时保证 PNG 已实际写回宿主机。
make_ocr_fixture() {
  local out_dir="${1:?}"
  docker run --rm --network none \
    -v "${out_dir}:/out" \
    --user 0:0 \
    --entrypoint python3 "${VLLM_IMAGE}" -c '
from PIL import Image, ImageDraw, ImageFont
img = Image.new("RGB", (1400, 360), "white")
draw = ImageDraw.Draw(img)
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 110)
except Exception:
    small = Image.new("RGB", (350, 90), "white")
    d = ImageDraw.Draw(small)
    d.text((8, 20), "LLM OCR 7319", fill="black", font=ImageFont.load_default())
    img = small.resize((1400, 360))
else:
    draw.text((80, 105), "LLM OCR 7319", fill="black", font=font)
img.save("/out/llm-ocr-test.png")
' >/dev/null
  [[ -s "${out_dir}/llm-ocr-test.png" ]] || \
    die "OCR 冒烟测试图片未写回宿主机；请检查 Docker bind mount 和 ${out_dir}"
}

# 把本地图片编码成 OpenAI 兼容的 data URL 请求。
# 参数依次为可读图片路径、识别提示和需要写入的 JSON 请求文件。
ocr_request_file() {
  local image_file="${1:?}" prompt="${2:-请逐字识别图片中的全部文字，只输出识别结果。}" out_json="${3:?}"
  local mime b64_file
  mime=$(file --brief --mime-type "${image_file}" 2>/dev/null || printf image/png)
  [[ "${mime}" == image/* ]] || die "文件不是受支持的图片：${mime}"
  b64_file=$(mktemp)
  base64 -w 0 "${image_file}" >"${b64_file}"
  jq -n --rawfile b64 "${b64_file}" --arg mime "${mime}" --arg prompt "${prompt}" --arg model "${SERVED_MODEL_NAME}" '
    {model:$model, max_tokens:512, temperature:0, stream:false,
     reasoning_effort:"none",chat_template_kwargs:{enable_thinking:false},
     messages:[{role:"user",content:[
       {type:"image_url",image_url:{url:("data:"+$mime+";base64,"+$b64)}},
       {type:"text",text:$prompt}
     ]}]}' >"${out_json}"
  rm -f "${b64_file}"
}

smoke_endpoint() {
  local base_url="${1:?}" key="${2:?}" full="${3:-0}" tmp response content reasoning tool finish_reason
  local tmp_dir ocr_json multi_images_json reasoning_limit diagnostic max_images probe_images data_url
  tmp=$(mktemp)
  trap 'rm -f "${tmp:-}" "${ocr_json:-}" "${multi_images_json:-}"; [[ -z "${tmp_dir:-}" ]] || rm -rf "${tmp_dir}"' RETURN

  log "开始文本冒烟测试..."
  jq -n --arg model "${SERVED_MODEL_NAME}" --argjson toggle "${SUPPORTS_THINKING_TOGGLE}" '
    {model:$model,max_tokens:64,temperature:0,stream:false,messages:[{role:"user",content:"只输出 LLM_OK，不要输出其他内容。"}]} +
    (if $toggle == 1 then {reasoning_effort:"none",chat_template_kwargs:{enable_thinking:false}} else {} end)' >"${tmp}"
  if ! response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${tmp}"); then
    smoke_fail_response text "文本冒烟测试请求失败" "${response}"
  fi
  jq -e '.choices[0].message | type == "object"' <<<"${response}" >/dev/null 2>&1 || smoke_fail_response text "文本测试响应结构无效" "${response}"
  content=$(jq -r '.choices[0].message.content // ""' <<<"${response}")
  [[ "${content}" == *LLM_OK* ]] || smoke_fail_response text "文本语义测试失败，模型未返回 LLM_OK" "${response}"
  reasoning=$(jq -r '.choices[0].message.reasoning_content // .choices[0].message.reasoning // ""' <<<"${response}")
  (( SUPPORTS_THINKING_TOGGLE == 0 )) || [[ -z "${reasoning}" ]] || smoke_fail_response text-toggle "请求级思考开关未关闭 reasoning 输出" "${response}"
  log "文本生成$([[ ${SUPPORTS_THINKING_TOGGLE} -eq 1 ]] && printf '与请求级思考关闭' || true)：PASS"

  if (( SUPPORTS_REASONING == 1 )); then
    log "开始思考解析冒烟测试..."
    for reasoning_limit in 2048 4096; do
      jq -n --arg model "${SERVED_MODEL_NAME}" --argjson max_tokens "${reasoning_limit}" \
        '{model:$model,max_tokens:$max_tokens,temperature:0.6,top_p:0.95,top_k:20,stream:false,
          messages:[{role:"user",content:"请简短思考并计算 17×19，随后给出包含计算结果的最终答案。"}]}' >"${tmp}"
      if ! response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${tmp}"); then
        smoke_fail_response reasoning "思考测试请求失败" "${response}"
      fi
      jq -e '.choices[0].message | type == "object"' <<<"${response}" >/dev/null 2>&1 || smoke_fail_response reasoning "思考测试响应结构无效" "${response}"
      finish_reason=$(jq -r '.choices[0].finish_reason // ""' <<<"${response}")
      if [[ "${finish_reason}" != length ]]; then break; fi
      diagnostic=$(smoke_save_response "reasoning-truncated-${reasoning_limit}" "${response}")
      warn "思考测试在 ${reasoning_limit} token 达到长度上限；响应已保存至 ${diagnostic}。$([[ ${reasoning_limit} -eq 2048 ]] && printf ' 将以 4096 token 重试一次。' || true)"
    done
    content=$(jq -r '.choices[0].message.content // ""' <<<"${response}")
    reasoning=$(jq -r '.choices[0].message.reasoning_content // .choices[0].message.reasoning // ""' <<<"${response}")
    [[ "${finish_reason}" != length ]] || smoke_fail_response reasoning "思考生成在 4096 token 后仍因长度截断，不能证明完整推理能力" "${response}"
    [[ -n "${reasoning}" ]] || smoke_fail_response reasoning "未检测到独立 reasoning_content/reasoning 字段" "${response}"
    [[ -n "${content}" ]] || smoke_fail_response reasoning "思考已解析，但没有独立的最终答案 content" "${response}"
    [[ "${content}" == *323* ]] || smoke_fail_response reasoning "最终答案未包含正确结果 323" "${response}"
    log "默认思考并独立解析：PASS"
  fi

  if (( SUPPORTS_TOOL_CALLING == 1 )); then
    log "开始 OpenAI 工具调用冒烟测试..."
    jq -n --arg model "${SERVED_MODEL_NAME}" '{model:$model,max_tokens:512,temperature:0,stream:false,
      reasoning_effort:"none",chat_template_kwargs:{enable_thinking:false},
      messages:[{role:"user",content:"必须调用 get_weather 查询 Paris。"}],
      tools:[{type:"function",function:{name:"get_weather",description:"查询城市天气",parameters:{type:"object",properties:{city:{type:"string"}},required:["city"]}}}],tool_choice:"required"}' >"${tmp}"
    if ! response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${tmp}"); then
      smoke_fail_response tool-calling "工具调用测试请求失败" "${response}"
    fi
    tool=$(jq -r '.choices[0].message.tool_calls[0].function.name // ""' <<<"${response}")
    [[ "${tool}" == get_weather ]] || smoke_fail_response tool-calling "工具调用未解析为 get_weather" "${response}"
    log "OpenAI 工具调用：PASS"
  fi

  if [[ "${full}" == 1 && "${SUPPORTS_IMAGE_INPUT}" == 1 ]]; then
    max_images=$(jq -er '.image | select(type == "number" and . >= 1) | floor' <<<"${MM_LIMIT}") || \
      die "MM_LIMIT 未声明有效的图片数量上限"
    probe_images=$((max_images < 6 ? max_images : 6))
    log "开始图片/OCR 与单请求 ${probe_images} 图冒烟测试..."
    tmp_dir=$(smoke_fixture_directory)
    make_ocr_fixture "${tmp_dir}"
    ocr_json=$(mktemp)
    ocr_request_file "${tmp_dir}/llm-ocr-test.png" "识别图片文字，只输出文字。" "${ocr_json}"
    if ! response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${ocr_json}"); then
      smoke_fail_response image "图片输入测试请求失败" "${response}"
    fi
    content=$(jq -r '.choices[0].message.content // ""' <<<"${response}")
    if (( SUPPORTS_OCR == 1 )); then
      [[ "${content}" == *7319* ]] || smoke_fail_response ocr "OCR 语义测试未识别出 7319" "${response}"
      log "视觉/OCR：PASS"
    else
      [[ -n "${content}" ]] || smoke_fail_response image "图片输入测试返回空内容" "${response}"
      log "图片输入：PASS（模型未标记为 OCR 优化，不强制识别准确率）"
    fi
    data_url=$(jq -r '.messages[0].content[0].image_url.url' "${ocr_json}")
    multi_images_json=$(mktemp)
    jq -n --arg model "${SERVED_MODEL_NAME}" --arg url "${data_url}" \
      --argjson count "${probe_images}" --argjson toggle "${SUPPORTS_THINKING_TOGGLE}" '
      {model:$model,max_tokens:64,temperature:0,stream:false,
       messages:[{role:"user",content:
         ([range(0;$count)|{type:"image_url",image_url:{url:$url}}] +
          [{type:"text",text:"这些图片中的编号相同。请简短回答。"}])}]} +
       (if $toggle == 1 then {reasoning_effort:"none",chat_template_kwargs:{enable_thinking:false}} else {} end)' >"${multi_images_json}"
    if ! response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${multi_images_json}"); then
      smoke_fail_response multi-images "单请求 ${probe_images} 图测试失败" "${response}"
    fi
    content=$(jq -r '.choices[0].message.content // ""' <<<"${response}")
    [[ -n "${content}" ]] || smoke_fail_response multi-images "单请求 ${probe_images} 图返回空内容" "${response}"
    log "单请求 ${probe_images} 张图片：PASS（服务端上限 ${max_images} 张）"
  elif [[ "${full}" == 1 ]]; then
    log "当前模型不支持图片输入，跳过视觉测试。"
  fi
  trap - RETURN
  rm -f "${tmp}" "${ocr_json:-}" "${multi_images_json:-}"
  [[ -z "${tmp_dir:-}" ]] || rm -rf "${tmp_dir}"
}

cmd_smoke() {
  load_config
  local worker="" full=0
  while (($#)); do
    case "$1" in
      --worker) worker="${2:?--worker 需要 ID}"; shift 2 ;;
      --full) full=1; shift ;;
      *) die "未知 smoke 参数：$1" ;;
    esac
  done
  if [[ -n "${worker}" ]]; then
    csv_normalize "${worker}" >/dev/null
    worker_health "${worker}" || die "Worker ${worker} 未就绪"
    local worker_env="${CONFIG_DIR}/workers/${worker}.env"
    if [[ -r "${worker_env}" ]]; then
      # shellcheck disable=SC1090
      source "${worker_env}"
    fi
    smoke_endpoint "http://127.0.0.1:$(worker_port "${worker}")" "${BACKEND_API_KEY}" "${full}"
  else
    activate_default_published_deployment
    router_health || die "$(gateway_display_name) 内部 API 未就绪"
    public_router_health || die "Nginx 统一公开 API 未就绪"
    smoke_endpoint "$(public_local_base_url)" "${GATEWAY_API_KEY}" "${full}"
  fi
}

cmd_ocr() {
  load_config
  activate_default_published_deployment
  (( SUPPORTS_IMAGE_INPUT == 1 )) || die "当前模型 ${MODEL_ID} 不支持图片输入"
  local image_file="${1:?请提供图片文件}" prompt="${2:-请逐字识别图片中的全部文字，只输出识别结果。}" tmp response
  [[ -r "${image_file}" ]] || die "无法读取图片：${image_file}"
  router_health || die "$(gateway_display_name) 未就绪"
  tmp=$(mktemp)
  trap 'rm -f "${tmp}"' RETURN
  ocr_request_file "${image_file}" "${prompt}" "${tmp}"
  response=$(api_post "$(router_local_base_url)/v1/chat/completions" "${GATEWAY_API_KEY}" "${tmp}")
  jq -r '.choices[0].message.content // .error.message // empty' <<<"${response}"
  trap - RETURN
  rm -f "${tmp}"
}

cmd_bench() {
  load_config
  activate_default_published_deployment
  local concurrency=25 requests=50 max_tokens=512
  while (($#)); do
    case "$1" in
      --concurrency) concurrency="${2:?缺少并发数}"; shift 2 ;;
      --requests) requests="${2:?缺少请求数}"; shift 2 ;;
      --max-tokens) max_tokens="${2:?缺少输出 token 数}"; shift 2 ;;
      *) die "未知 bench 参数：$1" ;;
    esac
  done
  [[ "${concurrency}" =~ ^[0-9]+$ ]] && (( concurrency >= 1 && concurrency <= 256 )) || die "并发范围 1-256"
  [[ "${requests}" =~ ^[0-9]+$ ]] && (( requests >= concurrency && requests <= 10000 )) || die "requests 必须 >= concurrency 且 <=10000"
  [[ "${max_tokens}" =~ ^[0-9]+$ ]] && (( max_tokens >= 16 && max_tokens <= 8192 )) || die "max-tokens 范围 16-8192"
  router_health || die "$(gateway_display_name) 未就绪"
  warn "即将发起 ${requests} 个请求、并发 ${concurrency}；这是实际压力测试。"
  docker run --rm --network host \
    -e "BENCH_URL=$(router_local_base_url)/v1/chat/completions" \
    -e "BENCH_KEY=${GATEWAY_API_KEY}" \
    -e "BENCH_MODEL=${SERVED_MODEL_NAME}" \
    -e "BENCH_CONCURRENCY=${concurrency}" \
    -e "BENCH_REQUESTS=${requests}" \
    -e "BENCH_MAX_TOKENS=${max_tokens}" \
    --entrypoint python3 "${VLLM_IMAGE}" -c '
import concurrent.futures, json, math, os, statistics, time, urllib.request

url = os.environ["BENCH_URL"]
key = os.environ["BENCH_KEY"]
model = os.environ["BENCH_MODEL"]
concurrency = int(os.environ["BENCH_CONCURRENCY"])
requests = int(os.environ["BENCH_REQUESTS"])
max_tokens = int(os.environ["BENCH_MAX_TOKENS"])

def one(index):
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": False,
        "reasoning_effort": "none",
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "user", "content":
            "连续写一篇关于分布式系统的技术短文，内容尽量充实，在达到输出上限前不要提前结束。"}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=7200) as response:
            body = json.load(response)
        elapsed = time.monotonic() - started
        tokens = int(body.get("usage", {}).get("completion_tokens", 0))
        if not body.get("choices") or tokens <= 0:
            return {"ok": False, "error": str(body)[:300], "elapsed": elapsed, "tokens": 0}
        return {"ok": True, "elapsed": elapsed, "tokens": tokens, "tps": tokens / elapsed}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "elapsed": time.monotonic() - started, "tokens": 0}

wall_started = time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
    results = list(pool.map(one, range(requests)))
wall = time.monotonic() - wall_started
good = [r for r in results if r["ok"]]
bad = [r for r in results if not r["ok"]]
if not good:
    print("全部请求失败：", bad[0].get("error", "unknown"))
    raise SystemExit(1)

def percentile(values, p):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * p) - 1)]

tokens = sum(r["tokens"] for r in good)
latencies = [r["elapsed"] for r in good]
tps = [r["tps"] for r in good]
print(f"成功/失败: {len(good)}/{len(bad)}")
print(f"总输出 token: {tokens}")
print(f"墙钟时间: {wall:.2f}s")
print(f"集群聚合输出吞吐: {tokens / wall:.2f} token/s")
print(f"单请求有效吞吐 p50/p95: {percentile(tps,.50):.2f}/{percentile(tps,.95):.2f} token/s")
print(f"请求耗时 p50/p95: {percentile(latencies,.50):.2f}/{percentile(latencies,.95):.2f}s")
if bad:
    print("首个错误:", bad[0].get("error", "unknown"))
    raise SystemExit(2)
'
}

cmd_key() {
  require_root; load_config
  activate_default_published_deployment
  case "${1:-show}" in
    show)
      local api_host="${API_BIND}" api_origin=""
      if [[ "${GATEWAY_KIND}" == omniroute ]]; then
        api_origin=$(effective_account_origin)
      else
        [[ "${api_host}" == 0.0.0.0 || "${api_host}" == :: ]] && api_host='<服务器IP>'
        api_origin="http://${api_host}:${API_PORT}"
      fi
      printf 'OPENAI_BASE_URL=%s/v1\n' "${api_origin%/}"
      printf 'OPENAI_MODEL=%s\n' "${SERVED_MODEL_NAME}"
      printf 'OPENAI_API_KEY=%s\n' "${GATEWAY_API_KEY}"
      ;;
    rotate)
      local new_key="${2:-}"
      if [[ "${GATEWAY_KIND}" == newapi ]]; then
        [[ -z "${new_key}" ]] || die "New API 密钥由其数据库生成；请使用不带自定义 KEY 的 llmctl key rotate"
        export GATEWAY_LOCAL_URL
        GATEWAY_LOCAL_URL=$(router_local_base_url)
        gateway_helper rotate-newapi-token --secrets-file "${SECRETS_ENV}"
        log "New API 入口密钥已轮换。用 llmctl key show 查看。"
        return 0
      fi
      if [[ "${GATEWAY_KIND}" == omniroute ]]; then
        [[ -z "${new_key}" ]] || die "OmniRoute 管理密钥由其数据库生成；请使用不带自定义 KEY 的 llmctl key rotate"
        export GATEWAY_LOCAL_URL
        GATEWAY_LOCAL_URL=$(router_local_base_url)
        gateway_helper rotate-omniroute-key --secrets-file "${SECRETS_ENV}"
        # 重启依赖门户前重新加载新管理 Key。
        # shellcheck disable=SC1090
        source "${SECRETS_ENV}"
        systemctl restart llm-account.service
        wait_account_portal
        log "OmniRoute 管理密钥已轮换；用户个人 Key 不受影响。"
        return 0
      fi
      if [[ -z "${new_key}" ]]; then
        [[ "${GATEWAY_KIND}" == bifrost ]] && new_key="sk-bf-$(openssl rand -hex 32)" || new_key="sk-$(openssl rand -hex 32)"
      fi
      [[ "${new_key}" =~ ^[A-Za-z0-9._-]{16,}$ ]] || die "KEY 至少 16 位，只允许字母、数字、点、下划线和连字符"
      [[ "${GATEWAY_KIND}" != bifrost || "${new_key}" == sk-bf-* ]] || die "Bifrost 虚拟密钥必须以 sk-bf- 开头"
      set_env_value "${SECRETS_ENV}" GATEWAY_API_KEY "${new_key}"
      set_env_value "${SECRETS_ENV}" LITELLM_MASTER_KEY "${new_key}"
      GATEWAY_API_KEY="${new_key}"
      LITELLM_MASTER_KEY="${new_key}"
      refresh_router
      log "入口 API key 已轮换。用 llmctl key show 查看。"
      ;;
    *) die "key 子命令必须是 show|rotate" ;;
  esac
}

validate_admin_password_input() {
  local password="${1-}"
  [[ -n "${password}" ]] || die "管理员密码不能为空"
  [[ "${password}" != *$'\n'* && "${password}" != *$'\r'* ]] || die "管理员密码不能包含换行"
  python3 -c 'import sys; raise SystemExit(1 if sys.argv[1].isdecimal() else 0)' "${password}" || \
    die "管理员密码不能全部由数字组成"
}

read_admin_password() {
  local supplied="${1-}" password confirm=""
  if [[ -n "${supplied}" ]]; then
    password="${supplied}"
  else
    [[ -t 0 ]] || die "非交互模式请将新密码作为参数传入"
    read -r -s -p '输入新的管理员密码: ' password
    printf '\n' >&2
    read -r -s -p '再次输入新密码: ' confirm
    printf '\n' >&2
    [[ "${password}" == "${confirm}" ]] || die "两次输入的密码不一致"
  fi
  validate_admin_password_input "${password}"
  printf '%s' "${password}"
}

set_portal_username() {
  [[ "${GATEWAY_KIND}" == omniroute ]] || die "当前部署没有 LLMCtl 账户门户"
  local username="${1-}" encoded
  username=$(python3 -c 'import sys; value=sys.argv[1].strip(); raise SystemExit(1 if not value or any(ord(c) < 32 or ord(c) == 127 for c in value) else 0); print(value, end="")' "${username}") || \
    die "门户管理员登录名不能为空或包含控制字符"
  encoded=$(python3 -c 'import base64,sys; print(base64.b64encode(sys.argv[1].encode()).decode(), end="")' "${username}")
  export ACCOUNT_ADMIN_USERNAME="${username}" ACCOUNT_ADMIN_USERNAME_B64="${encoded}"
  account_helper set-admin-username || die "门户管理员登录名修改失败"
  set_env_value "${CLUSTER_ENV}" ACCOUNT_ADMIN_USERNAME_B64 "${encoded}"
  log "账户门户管理员登录名已修改为：${username}"
}

set_portal_password() {
  [[ "${GATEWAY_KIND}" == omniroute ]] || die "当前部署没有 LLMCtl 账户门户"
  local password="${1:?}"
  export ACCOUNT_ADMIN_PASSWORD="${password}"
  account_helper reset-admin-password || die "账户门户管理员密码更新失败"
  set_env_value "${SECRETS_ENV}" ACCOUNT_ADMIN_PASSWORD "${password}"
}

set_gateway_username() {
  local username="${1-}"
  [[ "${GATEWAY_KIND}" != omniroute ]] || die "OmniRoute 原生管理界面只有密码，没有用户名；请使用 set-portal-username 修改门户登录名。"
  [[ "${username}" =~ ^[A-Za-z0-9._@-]{1,64}$ ]] || die "原生网关用户名只允许字母、数字、点、下划线、@ 和连字符"
  if [[ "${GATEWAY_KIND}" == newapi ]]; then
    (( ${#username} <= 12 )) || die "New API 管理员用户名最多 12 位"
    export GATEWAY_LOCAL_URL LLMCTL_NEW_USERNAME="${username}"
    GATEWAY_LOCAL_URL=$(router_local_base_url)
    gateway_helper newapi-admin set-username --secrets-file "${SECRETS_ENV}"
  else
    set_env_value "${SECRETS_ENV}" UI_USERNAME "${username}"
    refresh_router
  fi
  log "原生网关管理员用户名已修改为：${username}"
}

set_gateway_password() {
  local password="${1:?}"
  if [[ "${GATEWAY_KIND}" == newapi ]]; then
    export GATEWAY_LOCAL_URL LLMCTL_NEW_PASSWORD="${password}"
    GATEWAY_LOCAL_URL=$(router_local_base_url)
    gateway_helper newapi-admin set-password --secrets-file "${SECRETS_ENV}"
  elif [[ "${GATEWAY_KIND}" == omniroute ]]; then
    export GATEWAY_LOCAL_URL LLMCTL_NEW_PASSWORD="${password}"
    GATEWAY_LOCAL_URL=$(router_local_base_url)
    gateway_helper omniroute-admin set-password --secrets-file "${SECRETS_ENV}"
  else
    set_env_value "${SECRETS_ENV}" UI_PASSWORD "${password}"
    refresh_router
  fi
}

cmd_admin() {
  require_root; load_config
  local action="${1:-show}"
  case "${action}" in
    show)
      local ui_host="${API_BIND}"
      [[ "${ui_host}" == 0.0.0.0 || "${ui_host}" == :: ]] && ui_host='<服务器IP>'
      printf 'GATEWAY=%s\n' "$(gateway_display_name)"
      printf 'GATEWAY_UI_URL=http://%s:%s%s\n' "${ui_host}" "${API_PORT}" "$(gateway_ui_path)"
      if [[ "${GATEWAY_KIND}" == omniroute ]]; then
        printf 'GATEWAY_UI_USERNAME=(不适用：OmniRoute 原生 UI 仅使用密码)\n'
      else
        printf 'GATEWAY_UI_USERNAME=%s\n' "${UI_USERNAME}"
      fi
      printf 'GATEWAY_UI_PASSWORD=%s\n' "${UI_PASSWORD}"
      if [[ "${GATEWAY_KIND}" == omniroute ]]; then
        local public_account_origin
        public_account_origin=$(effective_account_origin)
        printf 'ACCOUNT_PORTAL_URL=%s/ui/\n' "${public_account_origin}"
        printf 'OMNIROUTE_BASE_UI_URL=%s/base_ui/\n' "${public_account_origin}"
        printf 'ACCOUNT_PORTAL_ADMIN=%s\n' "${ACCOUNT_ADMIN_USERNAME}"
        printf 'ACCOUNT_PORTAL_PASSWORD=%s\n' "${ACCOUNT_ADMIN_PASSWORD}"
      fi
      warn "这是管理员凭据；请勿复制到日志、工单或代码仓库。"
      ;;
    set-username)
      if [[ "${GATEWAY_KIND}" == omniroute ]]; then
        set_portal_username "${2-}"
        log "OmniRoute 原生管理界面没有用户名，因此无需同步用户名。"
      else
        set_gateway_username "${2-}"
      fi
      ;;
    set-portal-username) set_portal_username "${2-}" ;;
    set-gateway-username) set_gateway_username "${2-}" ;;
    set-password|set-portal-password|set-gateway-password)
      local password
      password=$(read_admin_password "${2-}")
      if [[ "${action}" == set-portal-password ]]; then
        set_portal_password "${password}"
        log "账户门户管理员密码已修改。"
      elif [[ "${action}" == set-gateway-password ]]; then
        set_gateway_password "${password}"
        log "原生网关管理员密码已修改。"
      elif [[ "${GATEWAY_KIND}" == omniroute ]]; then
        local old_portal_password="${ACCOUNT_ADMIN_PASSWORD}"
        export ACCOUNT_ADMIN_PASSWORD="${password}"
        account_helper reset-admin-password || die "账户门户管理员密码更新失败；OmniRoute 密码未修改"
        if ! set_gateway_password "${password}"; then
          export ACCOUNT_ADMIN_PASSWORD="${old_portal_password}"
          account_helper reset-admin-password || warn "账户门户管理员密码回滚失败，请立即检查"
          die "OmniRoute 管理员密码更新失败；账户门户密码已回滚"
        fi
        set_env_value "${SECRETS_ENV}" ACCOUNT_ADMIN_PASSWORD "${password}"
        log "账户门户与 OmniRoute 原生管理密码已一起修改。"
      else
        set_gateway_password "${password}"
        log "原生网关管理员密码已修改。"
      fi
      ;;
    *) die "admin 子命令必须是 show|set-username|set-password|set-portal-username|set-portal-password|set-gateway-username|set-gateway-password" ;;
  esac
}


# 按稳定命令域加载实现。开发目录和安装目录使用同一入口，缺少任一模块时
# 立即失败，避免升级包不完整却继续执行部分管理动作。
load_llmctl_command_modules() {
  local script_dir module_dir module
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  if [[ -d "${script_dir}/lib/llmctl" ]]; then
    module_dir="${script_dir}/lib/llmctl"
  else
    module_dir="${LLMCTL_MODULE_DIR:-/usr/local/lib/llm-cluster/llmctl}"
  fi
  for module in topology registry_runtime workflow_model optimizer maintenance offline_lifecycle omniroute; do
    [[ -r "${module_dir}/${module}.sh" ]] || die "llmctl 命令模块缺失：${module_dir}/${module}.sh"
    # shellcheck disable=SC1090
    source "${module_dir}/${module}.sh"
  done
}

load_llmctl_command_modules

main() {
  local command="${1:-help}" release_commit=""
  (($# == 0)) || shift
  case "${command}" in
    help|-h|--help) usage ;;
    version|--version)
      printf 'llmctl %s' "${CTL_VERSION}"
      if [[ -r "${CONTROL_PLANE_RELEASE}" ]]; then
        release_commit=$(awk -F= '$1 == "LLMCTL_COMMIT" {sub(/^[^=]*=/, ""); print; exit}' "${CONTROL_PLANE_RELEASE}")
        [[ -z "${release_commit}" ]] || printf ' (%s)' "${release_commit:0:12}"
      fi
      printf '\n'
      ;;
    info) cmd_info "$@" ;;
    status) cmd_status "$@" ;;
    health) cmd_health "$@" ;;
    startup) cmd_startup "$@" ;;
    start) cmd_start "$@" ;;
    stop) cmd_stop "$@" ;;
    restart) cmd_restart "$@" ;;
    shutdown) cmd_shutdown "$@" ;;
    enable) cmd_enable "$@" ;;
    disable) cmd_disable "$@" ;;
    activate) cmd_activate "$@" ;;
    deactivate) cmd_deactivate "$@" ;;
    scale) cmd_scale "$@" ;;
    autostart) cmd_autostart "$@" ;;
    keepwarm) cmd_keepwarm "$@" ;;
    workflow) cmd_workflow "$@" ;;
    model) cmd_model "$@" ;;
    responses) cmd_responses "$@" ;;
    router) cmd_router "$@" ;;
    omniroute) cmd_omniroute "$@" ;;
    database) cmd_database "$@" ;;
    account) cmd_account "$@" ;;
    nginx) cmd_nginx "$@" ;;
    logs) cmd_logs "$@" ;;
    smoke) cmd_smoke "$@" ;;
    ocr) cmd_ocr "$@" ;;
    bench) cmd_bench "$@" ;;
    optimize) cmd_optimize "$@" ;;
    tune) cmd_tune "$@" ;;
    key) cmd_key "$@" ;;
    admin) cmd_admin "$@" ;;
    proxy) cmd_proxy "$@" ;;
    runtime-proxy) cmd_runtime_proxy "$@" ;;
    models) cmd_models "$@" ;;
    timezone) cmd_timezone "$@" ;;
    download) cmd_download "$@" ;;
    update) cmd_update "$@" ;;
    upgrade) cmd_upgrade "$@" ;;
    rollback) cmd_rollback "$@" ;;
    offline) cmd_offline "$@" ;;
    uninstall) cmd_uninstall "$@" ;;
    _worker-start) cmd_worker_start "$@" ;;
    _gateway-start) cmd_gateway_start "$@" ;;
    _nginx-install) cmd_nginx_install "$@" ;;
    _keepwarm-tick) cmd_keepwarm_tick "$@" ;;
    _boot-start) cmd_boot_start "$@" ;;
    _boot-stop) cmd_boot_stop "$@" ;;
    *) die "未知命令：${command}。运行 llmctl help 查看帮助。" ;;
  esac
}

if [[ "${LLMCTL_SOURCE_ONLY:-0}" != 1 ]]; then
  main "$@"
fi

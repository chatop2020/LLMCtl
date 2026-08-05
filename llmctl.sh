#!/usr/bin/env bash
# Hardware-aware vLLM cluster manager for Ubuntu 24.04.
# Installed by install-llm-cluster.sh as /usr/local/sbin/llmctl.

set -Eeuo pipefail
IFS=$'\n\t'

readonly CTL_VERSION="3.3.3"
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
readonly ACCOUNT_SQLITE="${STATE_DIR}/omniroute/portal/account-portal.db"
readonly ACCOUNT_HELPER="${LLM_ACCOUNT_HELPER:-/usr/local/lib/llm-cluster/account_portal.py}"
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
  # These files are root-owned and generated/validated by the installer/manager.
  # shellcheck disable=SC1090
  source "${CLUSTER_ENV}"
  # shellcheck disable=SC1090
  source "${SECRETS_ENV}"

  # Backward compatibility for the previous Ornith-only release.
  STARTUP_PARALLELISM="${STARTUP_PARALLELISM:-1}"
  # Upgraded deployments opt in explicitly. Fresh 3.2+ installations write 1.
  KEEPWARM_ENABLED="${KEEPWARM_ENABLED:-0}"
  KEEPWARM_INTERVAL_SECONDS="${KEEPWARM_INTERVAL_SECONDS:-300}"
  KEEPWARM_TIMEOUT_SECONDS="${KEEPWARM_TIMEOUT_SECONDS:-90}"
  # Workflow routing is an optional, separately managed data plane. Existing
  # 3.2.x deployments intentionally remain disabled after a control-plane
  # upgrade until an administrator runs `llmctl workflow init/enable`.
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
  ESTIMATED_MAX_NUM_SEQS="${ESTIMATED_MAX_NUM_SEQS:-${MAX_NUM_SEQS:-7}}"
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
  OMNIROUTE_IMAGE="${OMNIROUTE_IMAGE:-diegosouzapw/omniroute:3.8.48}"
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
        -v "${STATE_DIR}/omniroute/gateway:/app/data" \
        "${GATEWAY_IMAGE}"
      ;;
  esac
}

cmd_worker_start() {
  require_root
  load_config
  local id="${1:?缺少 Worker ID}" worker_env="${CONFIG_DIR}/workers/${1}.env"
  [[ "${id}" =~ ^[0-9]+$ ]] && (( id >= 0 && id < INSTANCE_COUNT )) || die "Worker ID 超出范围"
  [[ -r "${worker_env}" ]] || die "缺少 ${worker_env}"
  # shellcheck disable=SC1090
  source "${worker_env}"
  : "${GPU_DEVICES:?GPU_DEVICES missing}"
  : "${WORKER_PORT:?WORKER_PORT missing}"
  ensure_docker_network

  local -a docker_args=(
    /usr/bin/docker run --rm --name "llm-worker-${id}"
    --network "${DOCKER_NETWORK}" --ipc host --runtime=nvidia
    -p "127.0.0.1:${WORKER_PORT}:${WORKER_PORT}"
    -e "NVIDIA_VISIBLE_DEVICES=${GPU_DEVICES}"
    -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1
    -e VLLM_NO_USAGE_STATS=1 -e VLLM_MEDIA_URL_ALLOW_REDIRECTS=0
    -v "${MODEL_ROOT}:/models:ro"
    -v "${CACHE_DIR}/shared:/root/.cache"
    "${VLLM_IMAGE}" /models/current
    --served-model-name "${SERVED_MODEL_NAME}"
    --host 0.0.0.0 --port "${WORKER_PORT}"
    --api-key "${BACKEND_API_KEY}"
    --tensor-parallel-size "${TP_SIZE}"
    --max-model-len "${MAX_MODEL_LEN}"
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
    --max-num-seqs "${MAX_NUM_SEQS}"
    --max-num-batched-tokens "${MAX_NUM_BATCHED_TOKENS}"
    --enable-chunked-prefill --enable-prefix-caching
  )
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
  log "Worker ${id} 启动：GPU=${GPU_DEVICES}，TP=${TP_SIZE}，ctx=${MAX_MODEL_LEN}，seq=${MAX_NUM_SEQS}，模型=${MODEL_ID}"
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
  llmctl responses status                      检查公开模型 ID 是否可被 Responses API 原生解析
  llmctl responses repair                      备份数据并修复 Responses API 原生 Combo 与用户权限
  llmctl router <start|stop|restart|reconcile|status> 管理或在线同步所选接入层
  llmctl database <start|stop|restart|status>  管理接入层 PostgreSQL
  llmctl account <start|stop|restart|status|url> 管理 OmniRoute 账户门户
  llmctl nginx <apply|test|status>              应用、校验或查看 LLMCtl Nginx 公开入口
  llmctl timezone show|set [时区]              查看或设置系统时区（默认 Asia/Shanghai）

  llmctl logs [all] [-f]                      全部组件日志（默认）
  llmctl logs worker <ID> [-f]                Worker 日志
  llmctl logs router [-f]                     所选接入层日志
  llmctl logs database [-f]                   PostgreSQL 日志
  llmctl logs account [-f]                    OmniRoute 账户门户日志
  llmctl logs workflow [-f]                   可插拔 Go 工作流日志
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

ensure_workflow_identity() {
  getent group llm-workflow >/dev/null 2>&1 || groupadd --system llm-workflow
  if ! getent passwd llm-workflow >/dev/null 2>&1; then
    useradd --system --gid llm-workflow --home-dir /nonexistent --shell /usr/sbin/nologin llm-workflow
  fi
  install -d -m 0750 -o llm-workflow -g llm-workflow "${WORKFLOW_STATE_DIR}"
}

ensure_workflow_env() {
  ensure_workflow_identity
  if [[ ! -e "${WORKFLOW_ENV}" ]]; then
    install -m 0640 -o root -g llm-workflow /dev/null "${WORKFLOW_ENV}"
  fi
  chmod 0640 "${WORKFLOW_ENV}"
  chown root:llm-workflow "${WORKFLOW_ENV}"
  local shared_secret
  shared_secret=$(
    set +u
    set -a
    # shellcheck disable=SC1090
    source "${SECRETS_ENV}"
    # shellcheck disable=SC1090
    source "${WORKFLOW_ENV}"
    set +a
    printf '%s' "${LLM_WORKFLOW_SHARED_SECRET:-}"
  )
  [[ -n "${shared_secret}" ]] || shared_secret=$(openssl rand -hex 32)
  set_env_value "${SECRETS_ENV}" LLM_WORKFLOW_SHARED_SECRET "${shared_secret}"
  set_env_value "${WORKFLOW_ENV}" LLM_WORKFLOW_SHARED_SECRET "${shared_secret}"
  if ! awk -F= '$1 == "BACKEND_API_KEY" {found=1} END {exit !found}' "${WORKFLOW_ENV}"; then
    set_env_value "${WORKFLOW_ENV}" BACKEND_API_KEY "${BACKEND_API_KEY}"
  fi
}

refresh_account_workflow_credential() {
  if systemctl is-active --quiet llm-account.service; then
    systemctl restart llm-account.service
    log "账户门户已重新载入本机工作流管理凭据；Router 与 Worker 未重启。"
  fi
}

workflow_with_env() {
  [[ -r "${WORKFLOW_ENV}" ]] || die "缺少 ${WORKFLOW_ENV}；请先运行 llmctl workflow init"
  (
    set -a
    # shellcheck disable=SC1090
    source "${SECRETS_ENV}"
    # shellcheck disable=SC1090
    source "${WORKFLOW_ENV}"
    set +a
    exec "$@"
  )
}

workflow_helper() {
  [[ -x "${WORKFLOW_HELPER}" ]] || die "工作流配置助手不可执行：${WORKFLOW_HELPER}"
  workflow_with_env python3 "${WORKFLOW_HELPER}" --config "${WORKFLOW_CONFIG}" "$@"
}

workflow_require_runtime() {
  local runtime_dir
  runtime_dir=$(dirname "${WORKFLOW_RUNTIME}")
  if [[ ! -x "${WORKFLOW_RUNTIME}" || \
        ! -x "${runtime_dir}/llm-workflowd-linux-amd64" || \
        ! -x "${runtime_dir}/llm-workflowd-linux-arm64" ]]; then
    die "Go 工作流运行时未安装完整：${runtime_dir}。它随 LLMCtl 控制面提供，不需要 apt 安装；请先运行 llmctl upgrade --force。现有 OmniRoute、Worker 和普通 API 不受影响。"
  fi
}

workflow_check_config() {
  workflow_require_runtime
  [[ -r "${WORKFLOW_CONFIG}" ]] || die "缺少 ${WORKFLOW_CONFIG}；请先运行 llmctl workflow init"
  workflow_with_env "${WORKFLOW_RUNTIME}" --config "${WORKFLOW_CONFIG}" --check-config
}

workflow_mutate() {
  local backup=""
  if [[ -e "${WORKFLOW_CONFIG}" ]]; then
    backup=$(mktemp "${WORKFLOW_CONFIG}.backup.XXXXXX")
    cp -a "${WORKFLOW_CONFIG}" "${backup}"
  fi
  if ! workflow_helper "$@" || ! workflow_check_config >/dev/null; then
    if [[ -n "${backup}" ]]; then mv -f "${backup}" "${WORKFLOW_CONFIG}"; fi
    die "工作流配置未通过校验，已恢复修改前版本"
  fi
  [[ -z "${backup}" ]] || rm -f "${backup}"
  chown root:llm-workflow "${WORKFLOW_CONFIG}"
  chmod 0640 "${WORKFLOW_CONFIG}"
  if systemctl is-active --quiet llm-workflow.service; then
    systemctl reload llm-workflow.service
  fi
}

workflow_http_origin() {
  python3 - "${WORKFLOW_CONFIG}" <<'PY'
import json,sys
listen=json.load(open(sys.argv[1], encoding="utf-8"))["listen"]
if listen.startswith("["):
    host,port=listen[1:].rsplit("]:",1)
else:
    host,port=listen.rsplit(":",1)
if host in {"", "0.0.0.0", "::", "[::]"}:
    host="127.0.0.1"
if ":" in host:
    host=f"[{host}]"
print(f"http://{host}:{port}")
PY
}

wait_workflow_ready() {
  local origin elapsed=0
  origin=$(workflow_http_origin)
  while (( elapsed < 30 )); do
    if systemctl is-active --quiet llm-workflow.service && \
       curl --noproxy '*' -fsS --max-time 2 "${origin}/readyz" >/dev/null; then
      log "Go 工作流数据面已就绪：${origin}"
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
    log "等待工作流就绪：${elapsed}s/30s"
  done
  journalctl -u llm-workflow.service -n 80 --no-pager >&2 || true
  return 1
}

cmd_workflow_init() {
  local listen="${WORKFLOW_LISTEN}" route_model="${WORKFLOW_ROUTE_MODEL}" base_model="${SERVED_MODEL_NAME}"
  local pool="text-generation" api_key_env="BACKEND_API_KEY" local_ids="" gateway_base_url="" force=0 arg
  local -a targets=()
  while (( $# )); do
    arg="$1"
    case "${arg}" in
      --listen|--gateway-base-url|--route-model|--base-model|--pool|--target|--api-key-env|--local-worker-ids)
        (( $# >= 2 )) || die "${arg} 缺少值"
        case "${arg}" in
          --listen) listen="$2" ;;
          --gateway-base-url) gateway_base_url="$2" ;;
          --route-model) route_model="$2" ;;
          --base-model) base_model="$2" ;;
          --pool) pool="$2" ;;
          --target) targets+=(--target "$2") ;;
          --api-key-env) api_key_env="$2" ;;
          --local-worker-ids) local_ids="$2" ;;
        esac
        shift 2
        ;;
      --force) force=1; shift ;;
      *) die "未知 workflow init 参数：${arg}" ;;
    esac
  done
  # Fail before creating workflow.json when an older installed upgrader did
  # not yet copy the bundled Go runtime into the control-plane directory.
  workflow_require_runtime
  ensure_workflow_env
  if [[ -e "${WORKFLOW_CONFIG}" ]] && (( force == 0 )); then
    workflow_check_config >/dev/null
    local configured_models enabled_models first_model service_state
    configured_models=$(jq -r '.models | keys | join(", ")' "${WORKFLOW_CONFIG}")
    enabled_models=$(jq -r '[.models | to_entries[] | select(.value.enabled == true) | .key] | join(", ")' "${WORKFLOW_CONFIG}")
    first_model=$(jq -r '.models | keys[0] // empty' "${WORKFLOW_CONFIG}")
    service_state=$(systemctl is-active llm-workflow.service 2>/dev/null || true)
    log "已有工作流配置已保留并通过校验：${WORKFLOW_CONFIG}"
    log "已配置路由：${configured_models:-无}；已启用路由：${enabled_models:-无}；服务：${service_state:-inactive}。"
    if [[ -z "${first_model}" ]]; then
      die "现有工作流配置没有模型路由；请使用 llmctl workflow model set 添加路由，或确认后使用 workflow init --force 重建"
    elif [[ -z "${enabled_models}" ]]; then
      log "下一步：llmctl workflow model enable ${first_model}"
      log "然后运行：llmctl workflow check && llmctl workflow enable"
    elif [[ "${service_state}" != active ]]; then
      log "下一步：llmctl workflow check && llmctl workflow enable"
    else
      log "工作流数据面已在运行；无需重复初始化。"
    fi
    return 0
  fi
  [[ "${listen}" == *:* ]] || die "--listen 必须是 HOST:PORT"
  if ((${#targets[@]} == 0)) && [[ -z "${local_ids}" ]]; then local_ids="${ACTIVE_WORKERS}"; fi
  local -a helper_args=(init --listen "${listen}" --route-model "${route_model}" --base-model "${base_model}" --pool "${pool}" --api-key-env "${api_key_env}")
  [[ -z "${gateway_base_url}" ]] || helper_args+=(--gateway-base-url "${gateway_base_url}")
  [[ -z "${local_ids}" ]] || helper_args+=(--local-worker-ids "${local_ids}" --worker-base-port "${WORKER_BASE_PORT}")
  helper_args+=("${targets[@]}")
  (( force == 0 )) || helper_args+=(--force)
  workflow_helper "${helper_args[@]}"
  workflow_check_config >/dev/null
  chown root:llm-workflow "${WORKFLOW_CONFIG}"
  chmod 0640 "${WORKFLOW_CONFIG}"
  set_env_value "${CLUSTER_ENV}" WORKFLOW_LISTEN "${listen}"
  set_env_value "${CLUSTER_ENV}" WORKFLOW_ROUTE_MODEL "${route_model}"
  set_env_value "${CLUSTER_ENV}" WORKFLOW_ENABLED 0
  refresh_account_workflow_credential
  log "工作流配置已创建并保持关闭；现有 Worker、Router 和公开模型映射均未改变。"
  log "先运行 llmctl workflow model enable ${route_model}，再执行 llmctl workflow check 和 llmctl workflow enable。"
}

cmd_responses() {
  require_root
  load_config
  [[ "${GATEWAY_KIND}" == omniroute ]] || \
    die "responses status/repair 仅适用于 OmniRoute 接入层"
  local action="${1:-status}" output="" repair_status=0 was_active=0
  shift || true
  (( $# == 0 )) || die "用法：llmctl responses <status|repair>"
  case "${action}" in
    status)
      account_helper public-route-status | jq .
      ;;
    repair)
      systemctl is-active --quiet llm-account.service && was_active=1 || true
      if (( was_active )); then
        log "仅短暂停止账户门户以创建一致性快照并修复 Responses 路由；OmniRoute、Nginx、Docker 和 GPU Worker 保持运行。"
        systemctl stop llm-account.service
      fi
      set +e
      output=$(account_helper reconcile-public-routes)
      repair_status=$?
      set -e
      if (( was_active )); then
        systemctl start llm-account.service
        wait_account_portal
      fi
      if [[ -n "${output}" ]]; then
        if ! jq . <<<"${output}"; then printf '%s\n' "${output}"; fi
      fi
      (( repair_status == 0 )) || \
        die "Responses 路由修复未完成；在线 API 未停止。请运行 llmctl responses status 和 llmctl logs account 查看具体模型原因。"
      log "Responses API 原生 Combo 与用户权限已对账；Router 和 Worker 未重启。"
      ;;
    *) die "用法：llmctl responses <status|repair>" ;;
  esac
}

cmd_workflow_secret() {
  [[ "${1:-}" == set ]] || die "用法：llmctl workflow secret set <环境变量> [值]"
  local key="${2:-}" value="${3-}"
  [[ "${key}" =~ ^[A-Z][A-Z0-9_]{1,127}$ ]] || die "无效的密钥环境变量名：${key}"
  if [[ -z "${value}" ]]; then
    [[ -t 0 ]] || die "非交互模式必须直接提供密钥值"
    read -r -s -p "请输入 ${key}: " value
    printf '\n'
  fi
  [[ -n "${value}" ]] || die "密钥值不能为空"
  if [[ "${key}" == LLM_WORKFLOW_SHARED_SECRET && ${#value} -lt 24 ]]; then
    die "LLM_WORKFLOW_SHARED_SECRET 至少需要 24 个字符"
  fi
  ensure_workflow_env
  local backup
  backup=$(mktemp "${WORKFLOW_ENV}.backup.XXXXXX")
  cp -a "${WORKFLOW_ENV}" "${backup}"
  set_env_value "${WORKFLOW_ENV}" "${key}" "${value}"
  chmod 0640 "${WORKFLOW_ENV}"; chown root:llm-workflow "${WORKFLOW_ENV}"
  if [[ -r "${WORKFLOW_CONFIG}" ]] && ! workflow_check_config >/dev/null; then
    mv -f "${backup}" "${WORKFLOW_ENV}"
    die "新密钥配置未通过校验，已恢复修改前版本"
  fi
  rm -f "${backup}"
  if systemctl is-active --quiet llm-workflow.service; then systemctl restart llm-workflow.service; wait_workflow_ready; fi
  [[ "${key}" != LLM_WORKFLOW_SHARED_SECRET ]] || refresh_account_workflow_credential
  log "${key} 已写入独立工作流密钥文件；值未回显。"
}

cmd_workflow_target() {
  local action="${1:-}"; shift || true
  case "${action}" in
    add)
      (( $# >= 3 && $# <= 4 )) || die "用法：llmctl workflow target add <池> <ID> <URL> [密钥环境变量]"
      workflow_mutate target-add --pool "$1" --id "$2" --base-url "$3" --api-key-env "${4:-BACKEND_API_KEY}"
      ;;
    remove)
      (( $# == 2 )) || die "用法：llmctl workflow target remove <池> <ID>"
      workflow_mutate target-remove --pool "$1" --id "$2"
      ;;
    discover)
      (( $# >= 1 && $# <= 3 )) || die "用法：llmctl workflow target discover <URL> [密钥环境变量] [期望模型ID]"
      local -a args=(discover --base-url "$1" --api-key-env "${2:-BACKEND_API_KEY}")
      [[ -z "${3:-}" ]] || args+=(--expected-model "$3")
      workflow_helper "${args[@]}"
      ;;
    *) die "workflow target 子命令必须是 add|remove|discover" ;;
  esac
}

cmd_workflow_model() {
  local action="${1:-}"; shift || true
  case "${action}" in
    set)
      (( $# >= 3 && $# <= 5 )) || die "用法：llmctl workflow model set <公开ID> <底层ID> <池> [transparent|agent] [工具ID列表]"
      workflow_mutate model-set --route-model "$1" --base-model "$2" --pool "$3" --mode "${4:-transparent}" --tools "${5:-}" --enabled
      ;;
    enable|disable)
      (( $# == 1 )) || die "用法：llmctl workflow model ${action} <公开ID>"
      if [[ "${action}" == enable ]]; then
        workflow_mutate model-enable --route-model "$1" --enabled
      else
        workflow_mutate model-enable --route-model "$1" --no-enabled
      fi
      ;;
    *) die "workflow model 子命令必须是 set|enable|disable" ;;
  esac
}

cmd_workflow_adapter() {
  [[ "${1:-}" == set ]] || die "用法：llmctl workflow adapter set <ID> <URL> <工具定义JSON文件> [密钥环境变量]"
  shift
  (( $# >= 3 && $# <= 4 )) || die "用法：llmctl workflow adapter set <ID> <URL> <工具定义JSON文件> [密钥环境变量]"
  workflow_mutate adapter-set --id "$1" --endpoint "$2" --tool-definition "$3" --secret-env "${4:-}"
}

cmd_workflow() {
  require_root; load_config
  local action="${1:-status}"; shift || true
  case "${action}" in
    init) cmd_workflow_init "$@" ;;
    secret) cmd_workflow_secret "$@" ;;
    target) cmd_workflow_target "$@" ;;
    model) cmd_workflow_model "$@" ;;
    adapter) cmd_workflow_adapter "$@" ;;
    check)
      workflow_check_config
      workflow_helper check-targets | jq .
      workflow_helper show | jq '{version,listen,models,pools,adapters:(.adapters|keys)}'
      ;;
    enable)
      ensure_workflow_env
      workflow_check_config
      local first_disabled_model
      first_disabled_model=$(jq -r \
        '[.models | to_entries[] | select(.value.enabled != true) | .key][0] // empty' \
        "${WORKFLOW_CONFIG}")
      jq -e '[.models[]|select(.enabled == true)]|length > 0' "${WORKFLOW_CONFIG}" >/dev/null || \
        die "没有已启用的工作流路由；请先运行：llmctl workflow model enable ${first_disabled_model:-<公开ID>}"
      workflow_helper check-targets | jq .
      [[ -r "${WORKFLOW_UNIT_SOURCE}" ]] || die "缺少工作流 systemd 模板：${WORKFLOW_UNIT_SOURCE}"
      install -m 0644 "${WORKFLOW_UNIT_SOURCE}" "${WORKFLOW_SERVICE_UNIT}"
      systemctl daemon-reload
      if ! systemctl enable --now llm-workflow.service || ! wait_workflow_ready; then
        systemctl disable --now llm-workflow.service >/dev/null 2>&1 || true
        set_env_value "${CLUSTER_ENV}" WORKFLOW_ENABLED 0
        die "工作流未能在 30 秒内就绪；服务已停止并取消自启，原有 Router 与 Worker 未受影响"
      fi
      set_env_value "${CLUSTER_ENV}" WORKFLOW_ENABLED 1
      log "工作流已启用；没有自动替换现有 ${SERVED_MODEL_NAME} 或修改 OmniRoute 映射。"
      ;;
    disable)
      systemctl disable --now llm-workflow.service >/dev/null 2>&1 || true
      set_env_value "${CLUSTER_ENV}" WORKFLOW_ENABLED 0
      log "工作流已关闭；配置、资源池和密钥已保留。"
      ;;
    reload)
      workflow_check_config >/dev/null
      systemctl reload llm-workflow.service
      wait_workflow_ready
      ;;
    health)
      local origin; origin=$(workflow_http_origin)
      curl --noproxy '*' -fsS --max-time 3 "${origin}/healthz" | jq .
      curl --noproxy '*' -fsS --max-time 3 "${origin}/readyz" | jq .
      ;;
    status)
      local workflow_enabled_state workflow_active_state
      workflow_enabled_state=$(systemctl is-enabled llm-workflow.service 2>/dev/null || true)
      workflow_active_state=$(systemctl is-active llm-workflow.service 2>/dev/null || true)
      case "${workflow_enabled_state}" in
        enabled|enabled-runtime|static|indirect|disabled|masked) ;;
        not-found|"") workflow_enabled_state=not-installed ;;
      esac
      [[ -n "${workflow_active_state}" ]] || workflow_active_state=inactive
      printf 'LLMCtl workflow: configured=%s enabled=%s active=%s\n' \
        "$([[ -r "${WORKFLOW_CONFIG}" ]] && printf yes || printf no)" \
        "${workflow_enabled_state}" \
        "${workflow_active_state}"
      [[ ! -x "${WORKFLOW_RUNTIME}" ]] || "${WORKFLOW_RUNTIME}" --version
      if [[ -r "${WORKFLOW_CONFIG}" ]]; then
        workflow_helper show | jq '{listen,models,pools,adapters:(.adapters|keys)}'
      fi
      ;;
    show) workflow_helper show ;;
    logs)
      local follow=""; [[ "${1:-}" == -f ]] && follow=-f
      journalctl -u llm-workflow.service ${follow} -n 300 --no-pager
      ;;
    *) die "workflow 子命令必须是 init|secret|target|model|adapter|check|enable|disable|reload|health|status|show|logs" ;;
  esac
}

worker_port() { printf '%s\n' "$((WORKER_BASE_PORT + $1))"; }
worker_unit() { printf 'llm-worker@%s.service\n' "$1"; }

worker_devices() {
  local instance start i out=""
  instance="${1:?}"
  start=$((instance * TP_SIZE))
  for ((i = 0; i < TP_SIZE; i++)); do out+="${out:+,}$((start + i))"; done
  printf '%s\n' "${out}"
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
  local id="${1:?}" result_file="${2:?}" port payload response metrics="" curl_status=0
  local http_code=000 ttft=0 total=0 status=failed error="" started_at finished_at
  port=$(worker_port "${id}")
  response=$(mktemp "${KEEPWARM_STATE_DIR}/response-${id}.XXXXXX")
  started_at=$(date -u +%FT%TZ)
  if (( SUPPORTS_THINKING_TOGGLE == 1 )); then
    payload=$(jq -cn --arg model "${SERVED_MODEL_NAME}" '{model:$model,stream:false,max_tokens:1,temperature:0,chat_template_kwargs:{enable_thinking:false},messages:[{role:"user",content:"Reply OK."}]}')
  else
    payload=$(jq -cn --arg model "${SERVED_MODEL_NAME}" '{model:$model,stream:false,max_tokens:1,temperature:0,messages:[{role:"user",content:"Reply OK."}]}')
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
  # A killed shell, full filesystem, or unexpected helper failure must not
  # make the aggregate disappear. Preserve one explicit result per requested
  # Worker so status output can identify exactly which probe was lost.
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

# Sets PROGRESS_* globals. Health probes run concurrently so an unavailable
# batch costs about one second rather than one timeout per GPU.
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
    # Give a newly queued --no-block start a few seconds to leave inactive.
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
  export SERVED_MODEL_NAME WORKER_BASE_PORT MAX_NUM_SEQS MAX_MODEL_LEN ROUTING_STRATEGY
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
  "${ACCOUNT_HELPER}" "$@"
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
      log "自动初始化 OmniRoute，并同步 ${worker_ids} 个健康 Worker、模型、Combo 和管理密钥..."
      gateway_helper reconcile-omniroute --worker-ids "${worker_ids}" --secrets-file "${SECRETS_ENV}"
      if (( SUPPORTS_IMAGE_INPUT == 1 )); then
        log "OmniRoute Vision Bridge 已关闭：当前模型原生支持图片，图片和 PDF 将直接转发给 vLLM。"
      fi
      ;;
    *) return 0 ;;
  esac
  # Reconciliation can atomically replace the managed gateway key.
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
  # New API creates its managed token after the startup watcher has begun.
  # Reload the atomically replaced root-only secrets file for every readiness
  # probe so long-running observers and concurrent key rotations converge.
  reload_gateway_api_key || return 1
  base_url=$(router_local_base_url)
  curl --noproxy '*' -fsS --max-time 3 \
    -H "Authorization: Bearer ${GATEWAY_API_KEY}" \
    "${base_url}/v1/models" >/dev/null 2>&1
}

router_local_base_url() {
  # API_PORT fallback keeps source-only diagnostics for pre-2.4 configs usable;
  # installed 2.4 configurations always define the isolated internal port.
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
  # Public authentication endpoints receive a second, IP-wide throttle in
  # addition to the portal's identity lockout. Exact locations override the
  # generic portal-api proxy while keeping inference traffic unrestricted.
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
# Generated by LLMCtl ${CTL_VERSION}. Do not edit; use llmctl tune/info.
map \$http_upgrade \$llmctl_connection_upgrade {
  default upgrade;
  '' close;
}

limit_req_zone \$binary_remote_addr zone=llmctl_auth:10m rate=30r/m;

server {
  listen ${listen_address};
  # Exact host/IP names allow this isolated server to coexist with an existing
  # Nginx installation on the same listen socket without replacing its sites.
  server_name ${server_names};
  server_tokens off;
  client_max_body_size 128m;

  proxy_http_version 1.1;
  proxy_set_header Host \$host;
  proxy_set_header X-Real-IP \$remote_addr;
  # Never preserve a client-supplied X-Forwarded-For value. The portal trusts
  # this header only from the local Nginx hop for audit and login throttling.
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

  # Native gateway assets, management APIs, OAuth callbacks and deep links.
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
      # Upgrade from an earlier 2.4 development build that already captured an
      # original same-name site before the install-mode marker existed.
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
  # Exclude restarting workers before taking them down.
  stop_ids "${ids}" || return 1
  start_worker_ids_batched "${ids}" || failed=1
  refresh_router
  return "${failed}"
}

cmd_status() {
  load_config
  local spec="${1:-all}" ids id port unit_state health_state router_state database_state devices max_images active_worker_count
  local -a active_id_list=()
  if [[ "${spec}" == "all" ]]; then ids=$(all_instance_ids); else ids=$(resolve_ids "${spec}"); fi
  printf 'LLM 集群管理器: %s\n' "${CTL_VERSION}"
  printf '模型: %s:%s @ %s\n' "${MODEL_HUB}" "${MODEL_ID}" "${MODEL_REVISION}"
  printf '架构/精度/任务: %s / %s / %s\n' "${MODEL_ARCHITECTURE}" "${MODEL_PRECISION}" "${MODEL_TASK}"
  printf '本地模型: %s/current\n' "${MODEL_ROOT}"
  IFS=',' read -r -a active_id_list <<<"${ACTIVE_WORKERS}"
  active_worker_count=${#active_id_list[@]}
  printf '拓扑: TP=%s，物理 GPU=%s，实例数=%s，每实例 max-num-seqs=%s（已激活调度槽上限=%s）\n' \
    "${TP_SIZE}" "${PHYSICAL_GPU_COUNT}" "${INSTANCE_COUNT}" "${MAX_NUM_SEQS}" "$((active_worker_count * MAX_NUM_SEQS))"
  printf '规划参考: 当前模型/显存估算每实例 32K 级请求最多约 %s 个；长请求会降低实际并发\n' "${ESTIMATED_MAX_NUM_SEQS}"
  printf '启动并行度: 每批最多 %s 个 Worker\n' "${STARTUP_PARALLELISM}"
  if (( SUPPORTS_IMAGE_INPUT == 1 )); then
    max_images=$(jq -r '.image // "unknown"' <<<"${MM_LIMIT}" 2>/dev/null || printf unknown)
    printf '多模态: 支持；每请求最多 %s 张图片；完整测试覆盖单请求 6 图\n' "${max_images}"
  else
    printf '多模态: 当前模型不支持图片输入\n'
  fi
  printf '工具/思考: %s(parser=%s) / %s(parser=%s，可按请求关闭=%s)\n' \
    "${SUPPORTS_TOOL_CALLING}" "${TOOL_CALL_PARSER:-none}" "${SUPPORTS_REASONING}" \
    "${REASONING_PARSER:-none}" "${SUPPORTS_THINKING_TOGGLE}"
  printf '入口: http://%s:%s/v1  模型名: %s\n' "${API_BIND}" "${API_PORT}" "${SERVED_MODEL_NAME}"
  printf '开机激活 Worker: %s\n' "${ACTIVE_WORKERS}"
  router_state=$(systemctl is-active llm-router.service 2>/dev/null || true)
  printf '%s: %s (%s)\n' "$(gateway_display_name)" "${router_state:-unknown}" "$([[ -n "${router_state}" ]] && router_health && printf healthy || printf unhealthy)"
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    printf 'SQLite: OmniRoute=%s；账户门户=%s（隔离文件，无数据库服务实例）\n' "${OMNIROUTE_SQLITE}" "${ACCOUNT_DB_PATH}"
    printf '账户门户: %s (%s)  http://%s:%s\n' "$(systemctl is-active llm-account.service 2>/dev/null || true)" "$(account_portal_health && printf healthy || printf unhealthy)" "${ACCOUNT_BIND}" "${ACCOUNT_PORT}"
  else
    database_state=$(systemctl is-active llm-database.service 2>/dev/null || true)
    printf 'PostgreSQL: %s (%s，仅监听 127.0.0.1:%s)\n' "${database_state:-unknown}" "$([[ -n "${database_state}" ]] && database_health && printf healthy || printf unhealthy)" "${GATEWAY_DB_PORT}"
  fi
  printf '\n%-8s %-10s %-7s %-9s %-9s %-12s %-18s\n' INSTANCE GPUS PORT BOOT SYSTEMD HEALTH VRAM
  IFS=',' read -r -a id_list <<<"${ids}"
  for id in "${id_list[@]}"; do
    port=$(worker_port "${id}")
    devices=$(worker_devices "${id}")
    unit_state=$(systemctl is-active "$(worker_unit "${id}")" 2>/dev/null || true)
    health_state=down
    worker_health "${id}" && health_state=healthy
    local boot=no vram='n/a' gpu_id gpu_vram
    csv_has "${ACTIVE_WORKERS}" "${id}" && boot=yes
    if command -v nvidia-smi >/dev/null 2>&1; then
      vram=""
      IFS=',' read -r -a gpu_list <<<"${devices}"
      for gpu_id in "${gpu_list[@]}"; do
        gpu_vram=$(nvidia-smi --id="${gpu_id}" --query-gpu=memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | awk -F, '{gsub(/ /,""); print $1"/"$2"M"}' || printf n/a)
        vram+="${vram:+ }${gpu_id}:${gpu_vram}"
      done
    fi
    printf '%-8s %-10s %-7s %-9s %-9s %-12s %-18s\n' "${id}" "${devices}" "${port}" "${boot}" "${unit_state:-unknown}" "${health_state}" "${vram}"
  done
}

cmd_info() {
  require_root
  load_config
  local redact=0 value public_host public_origin effective_public_origin id state portal_inventory="" portal_inventory_status=unavailable
  local portal_users="n/a" portal_groups="n/a" portal_models="n/a" portal_free="n/a"
  local portal_usage="n/a" portal_transactions="n/a" portal_audits="n/a" portal_integrity="n/a"
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
    fi
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
    "$(systemctl is-active nginx.service 2>/dev/null || printf unknown)" \
    "$(systemctl is-enabled nginx.service 2>/dev/null || printf unknown)" "${NGINX_CONFIG}"
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
  printf '网关: %s (%s)\n镜像: %s\n路由策略: %s\n' "$(gateway_display_name)" "${GATEWAY_KIND}" "${GATEWAY_IMAGE}" "${ROUTING_STRATEGY}"
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
    printf 'OmniRoute SQLite: %s (mode=%s, size=%s)\n门户 SQLite: %s (mode=%s, size=%s)\n两者隔离: yes\n' \
      "${OMNIROUTE_SQLITE}" "$(stat -c %a "${OMNIROUTE_SQLITE}" 2>/dev/null || printf missing)" "$(du -h "${OMNIROUTE_SQLITE}" 2>/dev/null | awk '{print $1}' || printf missing)" \
      "${ACCOUNT_DB_PATH}" "$(stat -c %a "${ACCOUNT_DB_PATH}" 2>/dev/null || printf missing)" "$(du -h "${ACCOUNT_DB_PATH}" 2>/dev/null | awk '{print $1}' || printf missing)"
    printf '门户持久配置读取: %s；SQLite quick_check: %s\n门户对象: users=%s groups=%s models=%s free-resources=%s usage=%s transactions=%s audits=%s\n' \
      "${portal_inventory_status}" "${portal_integrity}" "${portal_users}" "${portal_groups}" "${portal_models}" "${portal_free}" "${portal_usage}" "${portal_transactions}" "${portal_audits}"
  else
    printf 'PostgreSQL 数据库: %s\nPostgreSQL 用户名: %s\nPostgreSQL 密码: %s\nDATABASE_URL: %s\n数据卷: llm-cluster-gateway-postgres\n' \
      "${POSTGRES_DB}" "${POSTGRES_USER}" "$(secret_value "${POSTGRES_PASSWORD}")" "$(secret_value "${DATABASE_URL}")"
  fi

  printf '\n[注册、余额与 SMTP / Registration, balance and SMTP]\n'
  printf '门户品牌名称: %s\n允许注册: %s\n允许邮箱后缀: %s\n新用户一次性赠款（USD）: %s\n旧版 Token 迁移状态: %s\n对外发布地址: %s\n门户公开 URL: %s\nAPI 公开 URL: %s\n' \
    "${ACCOUNT_PORTAL_TITLE}" "${ACCOUNT_REGISTRATION_ENABLED}" "${ACCOUNT_ALLOWED_EMAIL_DOMAINS:-<empty>}" "${ACCOUNT_DEFAULT_WELCOME_BALANCE}" \
    "$(printf '%s' "${portal_inventory:-{}}" | jq -r '.settings.token_grant_conversion_status // "not-required"' 2>/dev/null || printf unknown)" "${ACCOUNT_PUBLISHED_ORIGIN:-<自动>}" \
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
  printf 'Hub/模型/revision: %s / %s @ %s\n本地目录: %s/current\n模型服务 ID: %s\n架构/精度/任务: %s / %s / %s\n' \
    "${MODEL_HUB}" "${MODEL_ID}" "${MODEL_REVISION}" "${MODEL_ROOT}" "${SERVED_MODEL_NAME}" "${MODEL_ARCHITECTURE}" "${MODEL_PRECISION}" "${MODEL_TASK}"
  printf 'GPU/TP/实例: %s / %s / %s；开机激活: %s；启动并行度: %s\n' \
    "${PHYSICAL_GPU_COUNT}" "${TP_SIZE}" "${INSTANCE_COUNT}" "${ACTIVE_WORKERS}" "${STARTUP_PARALLELISM}"
  printf 'Context/max-seqs/batched-tokens/GPU-memory: %s / %s / %s / %s\n' \
    "${MAX_MODEL_LEN}" "${MAX_NUM_SEQS}" "${MAX_NUM_BATCHED_TOKENS}" "${GPU_MEMORY_UTILIZATION}"
  printf '图片/OCR/工具/思考/关闭思考: %s / %s / %s / %s / %s\n' \
    "${SUPPORTS_IMAGE_INPUT}" "${SUPPORTS_OCR}" "${SUPPORTS_TOOL_CALLING}" "${SUPPORTS_REASONING}" "${SUPPORTS_THINKING_TOGGLE}"
  printf 'vLLM 镜像: %s (ID=%s)\nPostgreSQL 镜像: %s (ID=%s)\n' \
    "${VLLM_IMAGE}" "$(docker image inspect --format '{{.Id}}' "${VLLM_IMAGE}" 2>/dev/null || printf unavailable)" \
    "${POSTGRES_IMAGE}" "$(docker image inspect --format '{{.Id}}' "${POSTGRES_IMAGE}" 2>/dev/null || printf unavailable)"

  printf '\n[服务、自启与 Worker / Services and workers]\n'
  printf 'llm-cluster: %s；enabled=%s\nllm-router: %s\n' \
    "$(systemctl is-active llm-cluster.service 2>/dev/null || printf unknown)" \
    "$(systemctl is-enabled llm-cluster.service 2>/dev/null || printf unknown)" \
    "$(systemctl is-active llm-router.service 2>/dev/null || printf unknown)"
  printf 'Worker 保活: enabled=%s；interval=%ss；timeout=%ss；timer=%s/%s\n最近保活: %s；状态文件: %s\n' \
    "${KEEPWARM_ENABLED}" "${KEEPWARM_INTERVAL_SECONDS}" "${KEEPWARM_TIMEOUT_SECONDS}" \
    "$(systemctl is-active llm-keepwarm.timer 2>/dev/null || printf unknown)" \
    "$(systemctl is-enabled llm-keepwarm.timer 2>/dev/null || printf unknown)" \
    "$([[ -r "${KEEPWARM_STATE_FILE}" ]] && jq -r '"\(.finished_at) requested=\(.summary.requested) succeeded=\(.summary.succeeded) failed=\(.summary.failed)"' "${KEEPWARM_STATE_FILE}" 2>/dev/null || printf never)" \
    "${KEEPWARM_STATE_FILE}"
  printf '可插拔工作流: configured=%s；enabled=%s；service=%s/%s\n配置: %s；密钥: %s；运行时: %s\n' \
    "$([[ -r "${WORKFLOW_CONFIG}" ]] && printf yes || printf no)" "${WORKFLOW_ENABLED}" \
    "$(systemctl is-active llm-workflow.service 2>/dev/null || printf inactive)" \
    "$(systemctl is-enabled llm-workflow.service 2>/dev/null || printf disabled)" \
    "${WORKFLOW_CONFIG}" "${WORKFLOW_ENV}" "${WORKFLOW_RUNTIME}"
  if [[ -r "${WORKFLOW_CONFIG}" ]]; then
    printf '工作流监听/路由/资源池: %s / %s / %s\n' \
      "$(jq -r '.listen // "unknown"' "${WORKFLOW_CONFIG}" 2>/dev/null || printf invalid)" \
      "$(jq -r '[.models|to_entries[]|select(.value.enabled)|.key]|join(",")' "${WORKFLOW_CONFIG}" 2>/dev/null || printf invalid)" \
      "$(jq -r '[.pools|to_entries[]|"\(.key)=\(.value.targets|length)"]|join(",")' "${WORKFLOW_CONFIG}" 2>/dev/null || printf invalid)"
  fi
  [[ "${GATEWAY_KIND}" == omniroute ]] && printf 'llm-account: %s\n' "$(systemctl is-active llm-account.service 2>/dev/null || printf unknown)"
  [[ "${GATEWAY_KIND}" != omniroute ]] && printf 'llm-database: %s\n' "$(systemctl is-active llm-database.service 2>/dev/null || printf unknown)"
  for ((id = 0; id < INSTANCE_COUNT; id++)); do
    state=$(systemctl is-active "$(worker_unit "${id}")" 2>/dev/null || printf unknown)
    printf 'Worker %s: GPU=%s port=%s systemd=%s boot=%s\n' "${id}" "$(worker_devices "${id}")" "$(worker_port "${id}")" "${state}" "$(csv_has "${ACTIVE_WORKERS}" "${id}" && printf yes || printf no)"
  done

  printf '\n[文件、日志与维护 / Files, logs and maintenance]\n'
  printf '主配置: %s\n密钥配置: %s (mode=%s)\n状态目录: %s\n缓存目录: %s\n网关计划: %s\n' \
    "${CLUSTER_ENV}" "${SECRETS_ENV}" "$(stat -c %a "${SECRETS_ENV}" 2>/dev/null || printf unknown)" "${STATE_DIR}" "${CACHE_DIR}" "$(gateway_config_path)"
  printf '模型当前链接: %s/current -> %s\n门户程序: %s\n门户静态资源: %s\nNginx 配置备份目录: %s\n' \
    "${MODEL_ROOT}" "$(readlink -f "${MODEL_ROOT}/current" 2>/dev/null || printf missing)" "${ACCOUNT_HELPER}" "${ACCOUNT_STATIC_DIR:-/usr/local/lib/llm-cluster/account_portal_ui}" "${NGINX_STATE_DIR}"
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
  # "stop all" means every possible instance, including one started manually
  # without adding it to the persistent boot list.
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

cmd_database() {
  require_root; load_config
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
    *) die "database 子命令必须是 start|stop|restart|status" ;;
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
    *) die "用法：llmctl logs [all] [-f] | logs worker <ID> [-f] | logs router [-f] | logs database [-f] | logs account [-f] | logs workflow [-f]" ;;
  esac
}

api_post() {
  local url="${1:?}" key="${2:?}" payload="${3:?}"
  curl --noproxy '*' -fsS --max-time 600 \
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

make_ocr_fixture() {
  local out_dir="${1:?}"
  docker run --rm --network none \
    -v "${out_dir}:/out" \
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
}

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
  local tmp_dir ocr_json six_images_json reasoning_limit diagnostic
  tmp=$(mktemp)
  trap 'rm -f "${tmp:-}" "${ocr_json:-}" "${six_images_json:-}"; [[ -z "${tmp_dir:-}" ]] || rm -rf "${tmp_dir}"' RETURN

  log "开始文本冒烟测试..."
  jq -n --arg model "${SERVED_MODEL_NAME}" --argjson toggle "${SUPPORTS_THINKING_TOGGLE}" '
    {model:$model,max_tokens:64,temperature:0,stream:false,messages:[{role:"user",content:"只输出 LLM_OK，不要输出其他内容。"}]} +
    (if $toggle == 1 then {reasoning_effort:"none",chat_template_kwargs:{enable_thinking:false}} else {} end)' >"${tmp}"
  response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${tmp}") || die "文本冒烟测试请求失败"
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
      response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${tmp}") || die "思考测试请求失败"
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
    response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${tmp}") || die "工具调用测试请求失败"
    tool=$(jq -r '.choices[0].message.tool_calls[0].function.name // ""' <<<"${response}")
    [[ "${tool}" == get_weather ]] || smoke_fail_response tool-calling "工具调用未解析为 get_weather" "${response}"
    log "OpenAI 工具调用：PASS"
  fi

  if [[ "${full}" == 1 && "${SUPPORTS_IMAGE_INPUT}" == 1 ]]; then
    log "开始图片/OCR 与单请求 6 图冒烟测试..."
    tmp_dir=$(mktemp -d)
    make_ocr_fixture "${tmp_dir}"
    ocr_json=$(mktemp)
    ocr_request_file "${tmp_dir}/llm-ocr-test.png" "识别图片文字，只输出文字。" "${ocr_json}"
    response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${ocr_json}") || die "图片输入测试请求失败"
    content=$(jq -r '.choices[0].message.content // ""' <<<"${response}")
    if (( SUPPORTS_OCR == 1 )); then
      [[ "${content}" == *7319* ]] || smoke_fail_response ocr "OCR 语义测试未识别出 7319" "${response}"
      log "视觉/OCR：PASS"
    else
      [[ -n "${content}" ]] || smoke_fail_response image "图片输入测试返回空内容" "${response}"
      log "图片输入：PASS（模型未标记为 OCR 优化，不强制识别准确率）"
    fi
    local data_url
    data_url=$(jq -r '.messages[0].content[0].image_url.url' "${ocr_json}")
    six_images_json=$(mktemp)
    jq -n --arg model "${SERVED_MODEL_NAME}" --arg url "${data_url}" --argjson toggle "${SUPPORTS_THINKING_TOGGLE}" '
      {model:$model,max_tokens:64,temperature:0,stream:false,
       messages:[{role:"user",content:
         ([range(0;6)|{type:"image_url",image_url:{url:$url}}] +
          [{type:"text",text:"这些图片中的编号相同。请简短回答。"}])}]} +
       (if $toggle == 1 then {reasoning_effort:"none",chat_template_kwargs:{enable_thinking:false}} else {} end)' >"${six_images_json}"
    response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${six_images_json}") || die "单请求 6 图测试失败"
    content=$(jq -r '.choices[0].message.content // ""' <<<"${response}")
    [[ -n "${content}" ]] || smoke_fail_response six-images "单请求 6 图返回空内容" "${response}"
    log "单请求 6 张图片：PASS"
  elif [[ "${full}" == 1 ]]; then
    log "当前模型不支持图片输入，跳过 OCR/6 图测试。"
  fi
  trap - RETURN
  rm -f "${tmp}" "${ocr_json:-}" "${six_images_json:-}"
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
    smoke_endpoint "http://127.0.0.1:$(worker_port "${worker}")" "${BACKEND_API_KEY}" "${full}"
  else
    router_health || die "$(gateway_display_name) 内部 API 未就绪"
    public_router_health || die "Nginx 统一公开 API 未就绪"
    smoke_endpoint "$(public_local_base_url)" "${GATEWAY_API_KEY}" "${full}"
  fi
}

cmd_ocr() {
  load_config
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

optimizer_metrics_urls() {
  local id urls=""
  IFS=',' read -r -a optimizer_worker_ids <<<"${ACTIVE_WORKERS}"
  for id in "${optimizer_worker_ids[@]}"; do
    worker_health "${id}" || continue
    urls+="${urls:+,}http://127.0.0.1:$(worker_port "${id}")/metrics"
  done
  printf '%s\n' "${urls}"
}

optimizer_pcie_only() {
  (( TP_SIZE > 1 )) || return 1
  command -v nvidia-smi >/dev/null 2>&1 || return 0
  ! nvidia-smi topo -m 2>/dev/null | grep -Eq 'NV[0-9]+'
}

optimizer_choose_workload() {
  local profile="${1:?}" quick="${2:?}" active_count slots cap
  local -a active_ids=()
  IFS=',' read -r -a active_ids <<<"${ACTIVE_WORKERS}"
  active_count=${#active_ids[@]}
  slots=$((active_count * MAX_NUM_SEQS))
  case "${profile}" in
    latency)
      cap=8; OPT_BENCH_MAX_TOKENS=64; OPT_BENCH_PROMPT_TOKENS=256 ;;
    balanced)
      cap=16; OPT_BENCH_MAX_TOKENS=128; OPT_BENCH_PROMPT_TOKENS=512 ;;
    throughput)
      cap=32; OPT_BENCH_MAX_TOKENS=256; OPT_BENCH_PROMPT_TOKENS=512 ;;
    *) die "$(ctl_l10n 'profile 必须是 latency、balanced 或 throughput' 'profile must be latency, balanced, or throughput')" ;;
  esac
  (( slots < cap )) && cap=${slots}
  (( cap < 1 )) && cap=1
  if (( quick )); then
    cap=$(((cap + 1) / 2))
    (( OPT_BENCH_MAX_TOKENS > 64 )) && OPT_BENCH_MAX_TOKENS=$((OPT_BENCH_MAX_TOKENS / 2))
    OPT_BENCH_REQUESTS=${cap}
  else
    OPT_BENCH_REQUESTS=$((cap * 2))
  fi
  OPT_BENCH_CONCURRENCY=${cap}
}

optimizer_preflight() {
  [[ -r "${OPTIMIZER_HELPER}" ]] || die "$(ctl_l10n "缺少 ${OPTIMIZER_HELPER}；请使用完整且同版本的安装包重新安装 llmctl" "Missing ${OPTIMIZER_HELPER}; reinstall llmctl from a complete matching-version package")"
  command -v python3 >/dev/null 2>&1 || die "python3 missing"
  command -v jq >/dev/null 2>&1 || die "jq missing"
  command -v flock >/dev/null 2>&1 || die "flock missing"
  router_health || die "$(ctl_l10n '统一入口未就绪，不能开始调优测试' 'The gateway is not ready; optimization cannot start')"
  local id
  IFS=',' read -r -a optimizer_preflight_ids <<<"${ACTIVE_WORKERS}"
  for id in "${optimizer_preflight_ids[@]}"; do
    worker_health "${id}" || die "$(ctl_l10n "开机激活的 Worker ${id} 未就绪；先恢复集群健康" "Boot-enabled Worker ${id} is not ready; restore cluster health first")"
  done
}

optimizer_run_benchmark() {
  local label="${1:?}" output="${2:?}" metrics thinking=()
  metrics=$(optimizer_metrics_urls)
  (( SUPPORTS_THINKING_TOGGLE == 0 )) || thinking+=(--thinking-toggle)
  log "$(ctl_l10n "运行 ${label} 合成负载：并发=${OPT_BENCH_CONCURRENCY}，请求=${OPT_BENCH_REQUESTS}，输出上限=${OPT_BENCH_MAX_TOKENS}..." "Running ${label} synthetic workload: concurrency=${OPT_BENCH_CONCURRENCY}, requests=${OPT_BENCH_REQUESTS}, output limit=${OPT_BENCH_MAX_TOKENS}...")"
  LLMCTL_BENCH_KEY="${GATEWAY_API_KEY}" LLMCTL_METRICS_KEY="${BACKEND_API_KEY}" \
    python3 "${OPTIMIZER_HELPER}" benchmark \
      --url "$(router_local_base_url)/v1/chat/completions" \
      --model "${SERVED_MODEL_NAME}" \
      --metrics-urls "${metrics}" \
      --concurrency "${OPT_BENCH_CONCURRENCY}" \
      --requests "${OPT_BENCH_REQUESTS}" \
      --max-tokens "${OPT_BENCH_MAX_TOKENS}" \
      --prompt-tokens "${OPT_BENCH_PROMPT_TOKENS}" \
      --label "${label}" "${thinking[@]}" >"${output}"
  jq -e '.schema_version == 1 and .outcome and .performance and .gpu and .host and .vllm' "${output}" >/dev/null || \
    die "$(ctl_l10n '调优基准工具返回了无效报告' 'The optimization benchmark helper returned an invalid report')"
}

optimizer_print_result() {
  local file="${1:?}" source="${1:?}" extracted="" success failed tps ttft itl e2e gpu_avg gpu_peak vram temp kv preempt waiting cpu_avg cpu_peak mem_available swap_used
  if jq -e '.baseline and .baseline.outcome' "${file}" >/dev/null 2>&1; then
    extracted=$(mktemp)
    jq '. as $report | if .selected == "baseline" then .baseline else ($report.trials[] | select(.name == $report.selected) | .result) end' "${file}" >"${extracted}"
    jq -e '.outcome and .performance' "${extracted}" >/dev/null || { rm -f "${extracted}"; die "$(ctl_l10n '调优报告缺少所选候选的测试结果' 'The optimization report lacks the selected candidate result')"; }
    source="${extracted}"
  fi
  success=$(jq -r '.outcome.successful_requests' "${source}")
  failed=$(jq -r '.outcome.failed_requests' "${source}")
  tps=$(jq -r '.performance.aggregate_output_tokens_per_second' "${source}")
  ttft=$(jq -r '.performance.ttft_p95_seconds' "${source}")
  itl=$(jq -r '.performance.itl_p95_seconds' "${source}")
  e2e=$(jq -r '.performance.e2e_p95_seconds' "${source}")
  gpu_avg=$(jq -r '.gpu.utilization_average_pct' "${source}")
  gpu_peak=$(jq -r '.gpu.utilization_peak_pct' "${source}")
  vram=$(jq -r '.gpu.vram_used_peak_pct' "${source}")
  temp=$(jq -r '.gpu.temperature_peak_c' "${source}")
  kv=$(jq -r '.vllm.kv_cache_usage_peak_pct' "${source}")
  preempt=$(jq -r '.vllm.preemptions_delta' "${source}")
  waiting=$(jq -r '.vllm.waiting_requests_peak' "${source}")
  cpu_avg=$(jq -r '.host.cpu_utilization_average_pct' "${source}")
  cpu_peak=$(jq -r '.host.cpu_utilization_peak_pct' "${source}")
  mem_available=$(jq -r '.host.memory_available_min_gib' "${source}")
  swap_used=$(jq -r '.host.swap_used_peak_gib' "${source}")
  if [[ "${INTERFACE_LANGUAGE:-zh}" == en ]]; then
    printf '  requests success/failure: %s/%s\n' "${success}" "${failed}"
    printf '  aggregate output: %s token/s; p95 TTFT/ITL/E2E: %ss / %ss / %ss\n' "${tps}" "${ttft}" "${itl}" "${e2e}"
    printf '  GPU avg/peak: %s%%/%s%%; peak VRAM: %s%%; peak temperature: %s°C\n' "${gpu_avg}" "${gpu_peak}" "${vram}" "${temp}"
    printf '  CPU avg/peak: %s%%/%s%%; minimum available RAM: %s GiB; peak swap used: %s GiB\n' "${cpu_avg}" "${cpu_peak}" "${mem_available}" "${swap_used}"
    printf '  peak KV cache: %s%%; preemptions: %s; peak waiting requests: %s\n' "${kv}" "${preempt}" "${waiting}"
  else
    printf '  请求成功/失败：%s/%s\n' "${success}" "${failed}"
    printf '  聚合输出：%s token/s；p95 TTFT/ITL/E2E：%ss / %ss / %ss\n' "${tps}" "${ttft}" "${itl}" "${e2e}"
    printf '  GPU 平均/峰值：%s%%/%s%%；显存峰值：%s%%；温度峰值：%s°C\n' "${gpu_avg}" "${gpu_peak}" "${vram}" "${temp}"
    printf '  CPU 平均/峰值：%s%%/%s%%；最低可用内存：%s GiB；Swap 使用峰值：%s GiB\n' "${cpu_avg}" "${cpu_peak}" "${mem_available}" "${swap_used}"
    printf '  KV Cache 峰值：%s%%；抢占：%s；排队请求峰值：%s\n' "${kv}" "${preempt}" "${waiting}"
  fi
  [[ -z "${extracted}" ]] || rm -f "${extracted}"
}

optimizer_generate_advice() {
  local baseline="${1:?}" profile="${2:?}" quick="${3:?}" output="${4:?}"
  local -a args=(recommend --result "${baseline}" --profile "${profile}"
    --max-num-seqs "${MAX_NUM_SEQS}"
    --estimated-max-num-seqs "${ESTIMATED_MAX_NUM_SEQS}"
    --max-num-batched-tokens "${MAX_NUM_BATCHED_TOKENS}"
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
    --max-model-len "${MAX_MODEL_LEN}"
    --instance-count "${INSTANCE_COUNT}"
    --tp-size "${TP_SIZE}"
    --lang "${INTERFACE_LANGUAGE:-zh}")
  (( quick == 0 )) || args+=(--quick)
  (( SUPPORTS_IMAGE_INPUT == 0 )) || args+=(--supports-image)
  optimizer_pcie_only && args+=(--pcie-only)
  python3 "${OPTIMIZER_HELPER}" "${args[@]}" >"${output}"
  jq -e '.schema_version == 1 and (.candidates | type == "array") and (.attentions | type == "array")' "${output}" >/dev/null || \
    die "$(ctl_l10n '调优建议工具返回了无效结果' 'The optimization adviser returned an invalid result')"
}

optimizer_print_advice() {
  local advice="${1:?}" count index key old new reason caution text
  count=$(jq -r '.candidates | length' "${advice}")
  printf '\n%s\n' "$(ctl_l10n '根据本次测试产生的候选优化：' 'Candidate optimizations derived from this test:')"
  if (( count == 0 )); then
    printf '  %s\n' "$(ctl_l10n '没有可安全自动试验的参数，建议保留当前配置。' 'No parameter change is safe to test automatically; keep the current configuration.')"
  fi
  for ((index = 0; index < count; index++)); do
    printf '  [%s] %s\n' "$((index + 1))" "$(jq -r ".candidates[${index}].name" "${advice}")"
    while IFS=$'\t' read -r key old new reason caution; do
      printf '    %s: %s -> %s\n' "${key}" "${old}" "${new}"
      printf '      %s %s\n' "$(ctl_l10n '原因：' 'Reason:')" "${reason}"
      printf '      %s %s\n' "$(ctl_l10n '代价：' 'Tradeoff:')" "${caution}"
    done < <(jq -r ".candidates[${index}].changes[] | [.key, .from, .to, .reason, .caution] | @tsv" "${advice}")
  done
  printf '\n%s\n' "$(ctl_l10n '注意事项与验证边界：' 'Cautions and validation boundaries:')"
  while IFS= read -r text; do printf '  - %s\n' "${text}"; done < <(jq -r '.attentions[].text' "${advice}")
}

optimizer_apply_snapshot() {
  local baseline="${1:?}" seqs="${2:?}" batched_tokens="${3:?}" memory="${4:?}" tmp
  tmp=$(mktemp "${CLUSTER_ENV}.optimize.XXXXXX")
  cp -p "${baseline}" "${tmp}"
  set_env_value "${tmp}" MAX_NUM_SEQS "${seqs}"
  set_env_value "${tmp}" MAX_NUM_BATCHED_TOKENS "${batched_tokens}"
  set_env_value "${tmp}" GPU_MEMORY_UTILIZATION "${memory}"
  mv -f "${tmp}" "${CLUSTER_ENV}"
}

optimizer_restore_snapshot() {
  local source="${1:?}" tmp
  [[ -r "${source}" ]] || return 1
  tmp=$(mktemp "${CLUSTER_ENV}.restore.XXXXXX")
  cp -p "${source}" "${tmp}"
  mv -f "${tmp}" "${CLUSTER_ENV}"
}

optimizer_recover_baseline() {
  local baseline="${1:?}" workers="${2:?}"
  warn "$(ctl_l10n '恢复调优前配置并重新加载 Worker。' 'Restoring the pre-optimization configuration and reloading workers.')"
  optimizer_restore_snapshot "${baseline}" || return 1
  load_config
  systemctl reset-failed >/dev/null 2>&1 || true
  restart_ids "${workers}" || return 1
  (cmd_smoke) || return 1
}

optimizer_emergency_rollback() {
  local code="${1:-130}"
  trap - ERR INT TERM HUP
  set +e
  if (( OPTIMIZER_ROLLBACK_ACTIVE )) && [[ -r "${OPTIMIZER_ROLLBACK_FILE}" ]]; then
    warn "$(ctl_l10n '调优流程被中断；正在自动恢复原配置。' 'Optimization was interrupted; automatically restoring the original configuration.')"
    optimizer_recover_baseline "${OPTIMIZER_ROLLBACK_FILE}" "${OPTIMIZER_ROLLBACK_WORKERS}" || \
      warn "$(ctl_l10n '自动回滚也未通过健康检查；请立即查看 Worker 日志和备份路径。' 'Automatic rollback also failed health checks; inspect worker logs and the backup path immediately.')"
  fi
  exit "${code}"
}

optimizer_failed_result() {
  local baseline="${1:?}" output="${2:?}" reason="${3:?}"
  jq --arg reason "${reason}" '
    .outcome.successful_requests = 0 |
    .outcome.failed_requests = 1 |
    .outcome.success_rate = 0 |
    .vllm.preemptions_delta = 0 |
    .errors = [$reason]
  ' "${baseline}" >"${output}"
}

optimizer_append_trial() {
  local trials="${1:?}" name="${2:?}" result="${3:?}" tmp
  tmp=$(mktemp "${trials}.XXXXXX")
  jq --arg name "${name}" --slurpfile result "${result}" '. + [{name:$name,result:$result[0]}]' "${trials}" >"${tmp}"
  mv -f "${tmp}" "${trials}"
}

optimizer_write_report() {
  local report="${1:?}" run_id="${2:?}" profile="${3:?}" status="${4:?}" selected="${5:?}"
  local baseline="${6:?}" advice="${7:?}" selection="${8:?}" trials="${9:?}" backup="${10:-}"
  local baseline_config="${backup:-${CLUSTER_ENV}}" before_seqs before_batched before_memory
  before_seqs=$(awk -F= '$1=="MAX_NUM_SEQS"{print substr($0,index($0,"=")+1); exit}' "${baseline_config}")
  before_batched=$(awk -F= '$1=="MAX_NUM_BATCHED_TOKENS"{print substr($0,index($0,"=")+1); exit}' "${baseline_config}")
  before_memory=$(awk -F= '$1=="GPU_MEMORY_UTILIZATION"{print substr($0,index($0,"=")+1); exit}' "${baseline_config}")
  [[ -n "${before_seqs}" && -n "${before_batched}" && -n "${before_memory}" ]] || die "$(ctl_l10n '无法从调优前配置生成审计报告' 'Could not derive the pre-optimization configuration for the audit report')"
  jq -n \
    --arg run_id "${run_id}" --arg generated_at "$(date -u +%FT%TZ)" \
    --arg tool_version "${CTL_VERSION}" --arg profile "${profile}" \
    --arg status "${status}" --arg selected "${selected}" --arg backup "${backup}" \
    --arg model_id "${MODEL_ID}" --arg model_revision "${MODEL_REVISION}" \
    --argjson before_seqs "${before_seqs}" --argjson before_batched "${before_batched}" \
    --argjson before_memory "${before_memory}" --argjson effective_seqs "${MAX_NUM_SEQS}" \
    --argjson effective_batched "${MAX_NUM_BATCHED_TOKENS}" --argjson effective_memory "${GPU_MEMORY_UTILIZATION}" \
    --argjson tp_size "${TP_SIZE}" --argjson instance_count "${INSTANCE_COUNT}" \
    --argjson max_model_len "${MAX_MODEL_LEN}" \
    --slurpfile baseline "${baseline}" --slurpfile advice "${advice}" \
    --slurpfile selection "${selection}" --slurpfile trials "${trials}" '
      {schema_version:1,run_id:$run_id,generated_at:$generated_at,tool_version:$tool_version,
       profile:$profile,status:$status,selected:$selected,
       backup:(if $backup == "" then null else $backup end),
       model:{id:$model_id,revision:$model_revision,tp_size:$tp_size,
              instance_count:$instance_count,max_model_len:$max_model_len},
       configuration:{before:{max_num_seqs:$before_seqs,
                              max_num_batched_tokens:$before_batched,
                              gpu_memory_utilization:$before_memory},
                      effective:{max_num_seqs:$effective_seqs,
                                 max_num_batched_tokens:$effective_batched,
                                 gpu_memory_utilization:$effective_memory}},
       baseline:$baseline[0],advice:$advice[0],selection:$selection[0],trials:$trials[0]}
    ' >"${report}"
  chmod 600 "${report}"
  ln -sfn "reports/${run_id}/report.json" "${OPTIMIZATION_DIR}/last-report.json"
}

optimizer_show_report() {
  local json=0 report="${OPTIMIZATION_DIR}/last-report.json"
  [[ "${1:-}" == --json ]] && json=1
  [[ -r "${report}" ]] || die "$(ctl_l10n '还没有调优报告' 'No optimization report exists yet')"
  if (( json )); then cat "${report}"; return 0; fi
  printf '%s %s\n' "$(ctl_l10n '运行：' 'Run:')" "$(jq -r '.run_id' "${report}")"
  printf '%s %s\n' "$(ctl_l10n '目标：' 'Profile:')" "$(jq -r '.profile' "${report}")"
  printf '%s %s\n' "$(ctl_l10n '结果：' 'Status:')" "$(jq -r '.status' "${report}")"
  printf '%s %s\n' "$(ctl_l10n '保留配置：' 'Selected configuration:')" "$(jq -r '.selected' "${report}")"
  optimizer_print_result "${report}"
  printf '%s %s\n' "$(ctl_l10n '完整 JSON：' 'Full JSON:')" "$(readlink -f "${report}" 2>/dev/null || printf '%s' "${report}")"
}

optimizer_restore_run() {
  require_root; load_config
  local target=latest assume_yes=0 backup current workers answer timestamp
  if (($#)) && [[ "$1" != --* ]]; then target="$1"; shift; fi
  while (($#)); do
    case "$1" in
      --yes) assume_yes=1; shift ;;
      *) die "$(ctl_l10n "未知 optimize restore 参数：$1" "Unknown optimize restore option: $1")" ;;
    esac
  done
  install -d -m 700 "${OPTIMIZATION_DIR}/backups"
  if [[ "${target}" == latest ]]; then
    backup=$(find "${OPTIMIZATION_DIR}/backups" -maxdepth 1 -type f -name '????????T??????Z.cluster.env' -print 2>/dev/null | sort -r | head -n1)
  else
    [[ "${target}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || die "$(ctl_l10n 'RUN_ID 格式无效' 'Invalid RUN_ID format')"
    backup="${OPTIMIZATION_DIR}/backups/${target}.cluster.env"
  fi
  [[ -n "${backup}" && -r "${backup}" ]] || die "$(ctl_l10n '未找到可恢复的调优前配置' 'No restorable pre-optimization configuration was found')"
  if (( ! assume_yes )); then
    [[ -t 0 ]] || die "$(ctl_l10n '非交互恢复需要 --yes' 'Non-interactive restore requires --yes')"
    read -r -p "$(ctl_l10n "将恢复 ${backup}、重启所有激活 Worker 并完整验收；继续？[y/N] " "Restore ${backup}, restart all active workers, and run full acceptance? [y/N] ")" answer
    [[ "${answer}" =~ ^[Yy]$ ]] || { log "$(ctl_l10n '已取消。' 'Cancelled.')"; return 0; }
  fi
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  current="${OPTIMIZATION_DIR}/backups/${timestamp}-before-restore.cluster.env"
  cp -p "${CLUSTER_ENV}" "${current}"
  chmod 600 "${current}"
  workers="${ACTIVE_WORKERS}"
  OPTIMIZER_ROLLBACK_ACTIVE=1
  OPTIMIZER_ROLLBACK_FILE="${current}"
  OPTIMIZER_ROLLBACK_WORKERS="${workers}"
  trap 'optimizer_emergency_rollback 1' ERR
  trap 'optimizer_emergency_rollback 130' INT TERM HUP
  optimizer_restore_snapshot "${backup}"
  load_config
  if ! restart_ids "${workers}" || ! (cmd_smoke --full); then
    local rollback_ok=1
    optimizer_recover_baseline "${current}" "${workers}" || rollback_ok=0
    OPTIMIZER_ROLLBACK_ACTIVE=0
    trap - ERR INT TERM HUP
    (( rollback_ok == 1 )) || die "$(ctl_l10n "恢复目标和恢复操作前配置都未通过验收；保留快照 ${current}，请立即检查日志" "Both the restore target and pre-restore configuration failed acceptance; snapshot ${current} is retained for immediate diagnosis")"
    die "$(ctl_l10n '恢复目标未通过验收，已回到恢复操作前的配置' 'The restore target failed acceptance; the pre-restore configuration was reinstated')"
  fi
  OPTIMIZER_ROLLBACK_ACTIVE=0
  trap - ERR INT TERM HUP
  log "$(ctl_l10n "已恢复 ${backup} 并通过完整冒烟测试。" "Restored ${backup} and passed full smoke testing.")"
}

optimizer_analyze_or_run() {
  require_root; load_config
  local action="${1:?}"; shift
  local profile=balanced quick=0 assume_yes=0 answer run_id run_dir baseline advice selection trials report
  local candidate_count index name seqs batched memory trial trial_ok current_name selected status backup=""
  local base_seqs="${MAX_NUM_SEQS}" base_batched="${MAX_NUM_BATCHED_TOKENS}" base_memory="${GPU_MEMORY_UTILIZATION}"
  local workers="${ACTIVE_WORKERS}" lock_fd
  local -a trial_args=()
  while (($#)); do
    case "$1" in
      --profile) profile="${2:?--profile missing value}"; shift 2 ;;
      --quick) quick=1; shift ;;
      --yes) assume_yes=1; shift ;;
      *) die "$(ctl_l10n "未知 optimize 参数：$1" "Unknown optimize option: $1")" ;;
    esac
  done
  case "${profile}" in latency|balanced|throughput) ;; *) die "$(ctl_l10n 'profile 必须是 latency、balanced 或 throughput' 'profile must be latency, balanced, or throughput')" ;; esac
  optimizer_preflight
  install -d -m 700 "${OPTIMIZATION_DIR}" "${OPTIMIZATION_DIR}/reports" "${OPTIMIZATION_DIR}/backups"
  exec {lock_fd}>"${OPTIMIZATION_DIR}/optimize.lock"
  flock -n "${lock_fd}" || die "$(ctl_l10n '另一个调优流程正在运行' 'Another optimization run is already active')"
  run_id=$(date -u +%Y%m%dT%H%M%SZ)
  while [[ -e "${OPTIMIZATION_DIR}/reports/${run_id}" ]]; do
    sleep 1
    run_id=$(date -u +%Y%m%dT%H%M%SZ)
  done
  run_dir="${OPTIMIZATION_DIR}/reports/${run_id}"
  install -d -m 700 "${run_dir}"
  baseline="${run_dir}/baseline.json"
  advice="${run_dir}/advice.json"
  selection="${run_dir}/selection.json"
  trials="${run_dir}/trials.json"
  report="${run_dir}/report.json"
  printf '[]\n' >"${trials}"
  optimizer_choose_workload "${profile}" "${quick}"
  warn "$(ctl_l10n '这是实际推理压力测试；当前阶段只读取指标，不修改配置。' 'This generates real inference load; the current stage reads metrics without changing configuration.')"
  (cmd_smoke) || die "$(ctl_l10n '基线冒烟失败，未进入压力测试' 'Baseline smoke testing failed; load testing was not started')"
  optimizer_run_benchmark baseline "${baseline}"
  printf '\n%s\n' "$(ctl_l10n '当前配置基线：' 'Current-configuration baseline:')"
  optimizer_print_result "${baseline}"
  optimizer_generate_advice "${baseline}" "${profile}" "${quick}" "${advice}"
  optimizer_print_advice "${advice}"
  candidate_count=$(jq -r '.candidates | length' "${advice}")
  if [[ "${action}" == analyze || ${candidate_count} -eq 0 ]]; then
    jq -n --arg profile "${profile}" '{schema_version:1,profile:$profile,selected:"baseline",scores:[{name:"baseline",score:1,eligible:true}]}' >"${selection}"
    optimizer_write_report "${report}" "${run_id}" "${profile}" analyzed baseline "${baseline}" "${advice}" "${selection}" "${trials}" ""
    log "$(ctl_l10n "分析完成，未修改配置。报告：${report}" "Analysis complete; configuration was not changed. Report: ${report}")"
    return 0
  fi
  printf '\n%s\n' "$(ctl_l10n "下一阶段会备份配置，依次测试 ${candidate_count} 个候选；每个候选会重启激活 Worker，期间 API 会短暂不可用。候选只有在无请求失败、抢占不恶化且目标综合得分至少提高 5% 时才会保留；任何启动或完整冒烟失败都会恢复原配置。" "The next stage backs up the configuration and tests ${candidate_count} candidate(s). Each candidate restarts active workers, briefly interrupting the API. A candidate is kept only with no request failures, no worse preemption, and at least a 5% target score improvement. Startup or full-smoke failure restores the original configuration.")"
  if (( ! assume_yes )); then
    [[ -t 0 ]] || die "$(ctl_l10n '非交互自动调优需要 --yes' 'Non-interactive automatic optimization requires --yes')"
    read -r -p "$(ctl_l10n '同意执行候选试验并自动应用最优结果？[y/N] ' 'Run candidate trials and automatically apply the best result? [y/N] ')" answer
    if [[ ! "${answer}" =~ ^[Yy]$ ]]; then
      jq -n --arg profile "${profile}" '{schema_version:1,profile:$profile,selected:"baseline",scores:[{name:"baseline",score:1,eligible:true}]}' >"${selection}"
      optimizer_write_report "${report}" "${run_id}" "${profile}" declined baseline "${baseline}" "${advice}" "${selection}" "${trials}" ""
      log "$(ctl_l10n '已取消，配置未修改。' 'Cancelled; configuration was not changed.')"
      return 0
    fi
  fi
  backup="${OPTIMIZATION_DIR}/backups/${run_id}.cluster.env"
  cp -p "${CLUSTER_ENV}" "${backup}"
  chmod 600 "${backup}"
  OPTIMIZER_ROLLBACK_ACTIVE=1
  OPTIMIZER_ROLLBACK_FILE="${backup}"
  OPTIMIZER_ROLLBACK_WORKERS="${workers}"
  trap 'optimizer_emergency_rollback 1' ERR
  trap 'optimizer_emergency_rollback 130' INT TERM HUP
  current_name=baseline
  for ((index = 0; index < candidate_count; index++)); do
    name=$(jq -r ".candidates[${index}].name" "${advice}")
    [[ "${name}" =~ ^[a-z0-9-]+$ ]] || optimizer_emergency_rollback 1
    seqs=$(jq -r ".candidates[${index}].max_num_seqs" "${advice}")
    batched=$(jq -r ".candidates[${index}].max_num_batched_tokens" "${advice}")
    memory=$(jq -r ".candidates[${index}].gpu_memory_utilization" "${advice}")
    trial="${run_dir}/trial-${name}.json"
    log "$(ctl_l10n "试验 $((index + 1))/${candidate_count}：${name}（seq=${seqs}, batched=${batched}, memory=${memory}）" "Trial $((index + 1))/${candidate_count}: ${name} (seq=${seqs}, batched=${batched}, memory=${memory})")"
    optimizer_apply_snapshot "${backup}" "${seqs}" "${batched}" "${memory}"
    load_config
    trial_ok=1
    restart_ids "${workers}" || trial_ok=0
    if (( trial_ok )) && ! (cmd_smoke); then trial_ok=0; fi
    if (( trial_ok )); then
      optimizer_run_benchmark "${name}" "${trial}"
      current_name="${name}"
    else
      warn "$(ctl_l10n "候选 ${name} 启动或冒烟失败，标记为不可用。" "Candidate ${name} failed startup or smoke testing and is ineligible.")"
      optimizer_failed_result "${baseline}" "${trial}" "startup or smoke failure"
      optimizer_recover_baseline "${backup}" "${workers}" || optimizer_emergency_rollback 1
      current_name=baseline
    fi
    optimizer_print_result "${trial}"
    optimizer_append_trial "${trials}" "${name}" "${trial}"
    trial_args+=(--trial "${name}=${trial}")
  done
  python3 "${OPTIMIZER_HELPER}" choose --baseline "${baseline}" --profile "${profile}" "${trial_args[@]}" >"${selection}"
  selected=$(jq -r '.selected' "${selection}")
  if [[ "${selected}" == baseline ]]; then
    seqs="${base_seqs}"; batched="${base_batched}"; memory="${base_memory}"
  else
    seqs=$(jq -r --arg name "${selected}" '.candidates[] | select(.name==$name) | .max_num_seqs' "${advice}")
    batched=$(jq -r --arg name "${selected}" '.candidates[] | select(.name==$name) | .max_num_batched_tokens' "${advice}")
    memory=$(jq -r --arg name "${selected}" '.candidates[] | select(.name==$name) | .gpu_memory_utilization' "${advice}")
    [[ -n "${seqs}" && -n "${batched}" && -n "${memory}" ]] || optimizer_emergency_rollback 1
  fi
  if [[ "${current_name}" != "${selected}" ]]; then
    optimizer_apply_snapshot "${backup}" "${seqs}" "${batched}" "${memory}"
    load_config
    restart_ids "${workers}" || optimizer_emergency_rollback 1
  fi
  if ! (cmd_smoke --full); then
    local rollback_ok=1
    optimizer_recover_baseline "${backup}" "${workers}" || rollback_ok=0
    OPTIMIZER_ROLLBACK_ACTIVE=0
    trap - ERR INT TERM HUP
    jq -n --arg profile "${profile}" '{schema_version:1,profile:$profile,selected:"baseline",scores:[]}' >"${selection}"
    if (( rollback_ok )); then
      optimizer_write_report "${report}" "${run_id}" "${profile}" rolled-back baseline "${baseline}" "${advice}" "${selection}" "${trials}" "${backup}"
    else
      optimizer_write_report "${report}" "${run_id}" "${profile}" rollback-failed "${selected}" "${baseline}" "${advice}" "${selection}" "${trials}" "${backup}" || true
      die "$(ctl_l10n "最优候选和自动回滚都未通过验收；原配置仍保存在 ${backup}，请立即检查日志" "Both the selected candidate and automatic rollback failed acceptance; the original configuration remains at ${backup}; inspect logs immediately")"
    fi
    die "$(ctl_l10n '最优候选未通过完整能力验收，已自动恢复原配置' 'The selected candidate failed full capability acceptance; the original configuration was restored')"
  fi
  [[ "${selected}" == baseline ]] && status=retained || status=applied
  optimizer_write_report "${report}" "${run_id}" "${profile}" "${status}" "${selected}" "${baseline}" "${advice}" "${selection}" "${trials}" "${backup}"
  OPTIMIZER_ROLLBACK_ACTIVE=0
  trap - ERR INT TERM HUP
  log "$(ctl_l10n "调优完成：保留 ${selected}；完整报告 ${report}；可用 llmctl optimize restore ${run_id} 回滚。" "Optimization complete: selected ${selected}; full report ${report}; roll back with llmctl optimize restore ${run_id}.")"
}

cmd_optimize() {
  local action="${1:-run}"
  (($# == 0)) || shift
  case "${action}" in
    analyze|run) optimizer_analyze_or_run "${action}" "$@" ;;
    report)
      require_root; load_config
      [[ $# -le 1 ]] || die "$(ctl_l10n '用法：llmctl optimize report [--json]' 'Usage: llmctl optimize report [--json]')"
      [[ $# -eq 0 || "${1:-}" == --json ]] || die "$(ctl_l10n "未知 report 参数：${1}" "Unknown report option: ${1}")"
      optimizer_show_report "${1:-}" ;;
    restore) optimizer_restore_run "$@" ;;
    *) die "$(ctl_l10n 'optimize 子命令必须是 analyze|run|report|restore' 'optimize subcommand must be analyze|run|report|restore')" ;;
  esac
}

cmd_tune() {
  require_root; load_config
  case "${1:-show}" in
    show)
      printf 'max-model-len=%s\n' "${MAX_MODEL_LEN}"
      printf 'gpu-memory-utilization=%s\n' "${GPU_MEMORY_UTILIZATION}"
      printf 'max-num-seqs=%s\n' "${MAX_NUM_SEQS}"
      printf 'max-num-batched-tokens=%s\n' "${MAX_NUM_BATCHED_TOKENS}"
      if [[ "${GATEWAY_KIND}" == omniroute ]]; then printf 'routing-strategy=round-robin (OmniRoute managed Combo)\n'; else printf 'routing-strategy=%s\n' "${ROUTING_STRATEGY}"; fi
      printf 'startup-parallelism=%s\n' "${STARTUP_PARALLELISM}"
      printf 'keepwarm-enabled=%s\nkeepwarm-interval-seconds=%s\nkeepwarm-timeout-seconds=%s\n' \
        "${KEEPWARM_ENABLED}" "${KEEPWARM_INTERVAL_SECONDS}" "${KEEPWARM_TIMEOUT_SECONDS}"
      printf 'api-bind=%s\napi-port=%s\n' "${API_BIND}" "${API_PORT}"
      printf 'mm-limit=%s\n' "${MM_LIMIT}"
      ;;
    set)
      local key="${2:?缺少键}" value="${3:?缺少值}" env_key restart_workers=1 apply_router=0 apply_nginx=0
      case "${key}" in
        max-model-len)
          [[ "${value}" =~ ^[0-9]+$ ]] && (( value >= 8192 && value <= MODEL_NATIVE_CONTEXT && value <= 262144 )) || die "范围 8192-${MODEL_NATIVE_CONTEXT}（且不超过 262144）"
          env_key=MAX_MODEL_LEN ;;
        gpu-memory-utilization)
          awk -v v="${value}" 'BEGIN{exit !(v>=0.70 && v<=0.96)}' || die "范围 0.70-0.96"
          env_key=GPU_MEMORY_UTILIZATION ;;
        max-num-seqs)
          [[ "${value}" =~ ^[0-9]+$ ]] && (( value >= 1 && value <= ESTIMATED_MAX_NUM_SEQS )) || die "范围 1-${ESTIMATED_MAX_NUM_SEQS}（当前模型/显存估算）"
          env_key=MAX_NUM_SEQS ;;
        max-num-batched-tokens)
          [[ "${value}" =~ ^[0-9]+$ ]] && (( value >= 1024 && value <= 65536 )) || die "范围 1024-65536"
          env_key=MAX_NUM_BATCHED_TOKENS ;;
        max-images)
          (( SUPPORTS_IMAGE_INPUT == 1 )) || die "当前模型不支持图片输入"
          [[ "${value}" =~ ^[0-9]+$ ]] && (( value >= 1 && value <= 32 )) || die "范围 1-32"
          value="'$(jq -cn --argjson n "${value}" '{image:$n,video:0}')'"
          env_key=MM_LIMIT ;;
        routing-strategy)
          [[ "${GATEWAY_KIND}" != omniroute ]] || die "OmniRoute 模式固定使用低开销 round-robin；本命令不修改 Combo 策略"
          case "${value}" in least-busy|simple-shuffle|latency-based-routing|usage-based-routing-v2) ;; *) die "不支持的路由策略" ;; esac
          env_key=ROUTING_STRATEGY; restart_workers=0; apply_router=1 ;;
        api-bind)
          [[ "${value}" =~ ^[0-9a-fA-F:.]+$ ]] || die "无效监听地址"
          env_key=API_BIND; restart_workers=0; apply_nginx=1 ;;
        api-port)
          [[ "${value}" =~ ^[0-9]+$ ]] && (( value >= 1024 && value <= 65535 )) || die "端口范围 1024-65535"
          [[ "${value}" != "${GATEWAY_INTERNAL_PORT}" ]] || die "公开端口不能等于内部网关端口"
          env_key=API_PORT; restart_workers=0; apply_nginx=1 ;;
        startup-parallelism)
          [[ "${value}" =~ ^[0-9]+$ ]] && (( value >= 1 && value <= INSTANCE_COUNT )) || die "范围 1-${INSTANCE_COUNT}"
          env_key=STARTUP_PARALLELISM; restart_workers=0 ;;
        keepwarm-interval-seconds)
          [[ "${value}" =~ ^[0-9]+$ ]] && (( value >= 60 && value <= 86400 )) || die "范围 60-86400"
          env_key=KEEPWARM_INTERVAL_SECONDS; restart_workers=0 ;;
        *) die "不可修改的键：${key}" ;;
      esac
      set_env_value "${CLUSTER_ENV}" "${env_key}" "${value}"
      log "已写入 ${key}=${value}"
      if (( apply_nginx )); then
        load_config
        cmd_nginx_install
      elif (( restart_workers )); then
        warn "该参数需重启 Worker 才生效：llmctl restart all"
      elif (( apply_router )); then
        # Reload values and apply router-only changes now.
        load_config
        refresh_router
      else
        log "启动并行度将在下次 start、restart 或开机启动时生效，无需重启当前 Worker。"
      fi
      ;;
    *) die "tune 子命令必须是 show|set" ;;
  esac
}

cmd_key() {
  require_root; load_config
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
        # Reload the new management key before restarting the dependent portal.
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

proxy_url_from_args() {
  local ip="${1:?缺少代理 IP}" port="${2:?缺少代理端口}" scheme="${3:-http}"
  [[ "${ip}" =~ ^[A-Za-z0-9._:-]+$ ]] || die "$(ctl_l10n '代理 IP/主机名格式无效' 'Invalid proxy IP/hostname')"
  [[ "${port}" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) || die "$(ctl_l10n '代理端口无效' 'Invalid proxy port')"
  [[ "${scheme}" == http || "${scheme}" == https ]] || die "$(ctl_l10n '代理协议只能是 http 或 https' 'Proxy scheme must be http or https')"
  printf '%s://%s:%s\n' "${scheme}" "${ip}" "${port}"
}

load_runtime_proxy() {
  RUNTIME_HTTP_PROXY=""
  RUNTIME_HTTPS_PROXY=""
  RUNTIME_NO_PROXY="127.0.0.1,localhost,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
  if [[ -r "${RUNTIME_PROXY_ENV}" ]]; then
    # This file is root-owned and generated by `llmctl runtime-proxy set`.
    # shellcheck disable=SC1090
    source "${RUNTIME_PROXY_ENV}"
  fi
  RUNTIME_HTTP_PROXY="${RUNTIME_HTTP_PROXY:-${RUNTIME_HTTPS_PROXY:-}}"
  RUNTIME_HTTPS_PROXY="${RUNTIME_HTTPS_PROXY:-${RUNTIME_HTTP_PROXY:-}}"
  RUNTIME_NO_PROXY="${RUNTIME_NO_PROXY:-127.0.0.1,localhost,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
}

runtime_proxy_docker_args() {
  local -n output_args="${1:?缺少输出数组}"
  load_runtime_proxy
  [[ -n "${RUNTIME_HTTPS_PROXY}" ]] || return 0
  output_args+=(
    -e "HTTP_PROXY=${RUNTIME_HTTP_PROXY}" -e "HTTPS_PROXY=${RUNTIME_HTTPS_PROXY}"
    -e "NO_PROXY=${RUNTIME_NO_PROXY}"
    -e "http_proxy=${RUNTIME_HTTP_PROXY}" -e "https_proxy=${RUNTIME_HTTPS_PROXY}"
    -e "no_proxy=${RUNTIME_NO_PROXY}"
  )
}

apply_runtime_proxy() {
  load_config
  local router_active=0 workflow_active=0
  systemctl is-active --quiet llm-router.service && router_active=1 || true
  systemctl is-active --quiet llm-workflow.service && workflow_active=1 || true

  if (( router_active )); then
    log "正在重新载入 $(gateway_display_name) 运行时代理；GPU Worker 保持运行。"
    systemctl restart llm-router.service
    wait_gateway_process || die "$(gateway_display_name) 重新载入运行时代理失败；请运行 llmctl logs router"
  fi
  if (( workflow_active )); then
    if [[ -r "${WORKFLOW_UNIT_SOURCE}" && -e "${WORKFLOW_SERVICE_UNIT}" ]]; then
      install -m 0644 "${WORKFLOW_UNIT_SOURCE}" "${WORKFLOW_SERVICE_UNIT}"
      systemctl daemon-reload
    fi
    systemctl restart llm-workflow.service
    wait_workflow_ready || die "工作流重新载入运行时代理失败；请运行 llmctl logs workflow"
  fi
  log "运行时代理已应用：Router=$([[ ${router_active} == 1 ]] && printf restarted || printf inactive)，Workflow=$([[ ${workflow_active} == 1 ]] && printf restarted || printf inactive)，GPU Worker=未重启。"
}

cmd_runtime_proxy() {
  require_root
  local action="${1:-show}" url no_proxy
  case "${action}" in
    set)
      url=$(proxy_url_from_args "${2:?缺少 IP}" "${3:?缺少端口}" "${4:-http}")
      no_proxy="${5:-127.0.0.1,localhost,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
      [[ -n "${no_proxy}" && ${#no_proxy} -le 2048 && "${no_proxy}" != *$'\n'* && "${no_proxy}" != *$'\r'* && "${no_proxy}" != *[[:space:]]* ]] || \
        die "NO_PROXY 必须是无空格、无换行的逗号分隔主机/IP/CIDR，且不超过 2048 字符"
      install -d -m 750 "${CONFIG_DIR}"
      umask 077
      printf 'RUNTIME_HTTP_PROXY=%q\nRUNTIME_HTTPS_PROXY=%q\nRUNTIME_NO_PROXY=%q\n' \
        "${url}" "${url}" "${no_proxy}" >"${RUNTIME_PROXY_ENV}"
      chmod 600 "${RUNTIME_PROXY_ENV}"
      log "运行时代理已保存；将应用到 Router 与可选 Workflow，本地/内网和 GPU Worker 流量不会使用该代理。"
      apply_runtime_proxy
      ;;
    show)
      load_runtime_proxy
      if [[ -n "${RUNTIME_HTTPS_PROXY}" ]]; then
        printf 'proxy=%s\nno_proxy=%s\nfile=%s\n' "${RUNTIME_HTTPS_PROXY}" "${RUNTIME_NO_PROXY}" "${RUNTIME_PROXY_ENV}"
      else
        printf '未设置\n'
      fi
      ;;
    clear)
      rm -f "${RUNTIME_PROXY_ENV}"
      log "运行时代理配置已清除，正在让相关服务恢复直连。"
      apply_runtime_proxy
      ;;
    test)
      load_runtime_proxy
      [[ -n "${RUNTIME_HTTPS_PROXY}" ]] || die "尚未设置运行时代理"
      curl --proxy "${RUNTIME_HTTPS_PROXY}" --noproxy '' -fsS --connect-timeout 10 --max-time 20 \
        -o /dev/null https://huggingface.co || die "运行时代理国际出口测试失败"
      log "运行时代理国际出口测试通过。"
      ;;
    apply) apply_runtime_proxy ;;
    *) die "runtime-proxy 子命令必须是 set|show|clear|test|apply" ;;
  esac
}

load_saved_proxy() {
  if [[ -r "${PROXY_ENV}" ]]; then
    # shellcheck disable=SC1090
    source "${PROXY_ENV}"
  fi
  MAINTENANCE_PROXY="${MAINTENANCE_PROXY:-}"
  MAINTENANCE_NO_PROXY="${MAINTENANCE_NO_PROXY:-127.0.0.1,localhost,::1}"
}

hf_network_probe() {
  local mode="${1:?}" http_code
  if [[ "${mode}" == direct ]]; then
    http_code=$(curl --noproxy '*' -sS --connect-timeout 5 --max-time 10 \
      -o /dev/null -w '%{http_code}' 'https://huggingface.co/api/models?limit=1' 2>/dev/null) || return 1
  else
    http_code=$(curl -sS --connect-timeout 8 --max-time 15 \
      -o /dev/null -w '%{http_code}' 'https://huggingface.co/api/models?limit=1' 2>/dev/null) || return 1
  fi
  [[ "${http_code}" =~ ^[0-9]{3}$ ]] && (( 10#${http_code} >= 100 && 10#${http_code} < 500 ))
}

prompt_proxy_if_needed() {
  load_saved_proxy
  log "$(ctl_l10n 'Hugging Face 搜索前网络测试：正在检查国际直连...' 'Pre-search network test: checking direct Hugging Face access...')"
  if hf_network_probe direct; then
    # A saved proxy is only a fallback. Prefer direct access when available.
    MAINTENANCE_PROXY=""
    log "$(ctl_l10n '国际网络测试通过：Hugging Face 可直连。' 'International connectivity passed: Hugging Face is directly reachable.')"
    return 0
  fi
  if [[ -n "${MAINTENANCE_PROXY}" ]]; then
    export_proxy_env
    if hf_network_probe proxy; then
      log "$(ctl_l10n '已保存代理的 Hugging Face 网络测试通过。' 'Hugging Face connectivity through the saved proxy passed.')"
      return 0
    fi
    warn "$(ctl_l10n '已保存的维护代理不可用，将重新询问。' 'The saved maintenance proxy failed; asking again.')"
    MAINTENANCE_PROXY=""
  fi
  (( MAINTENANCE_PROXY_DECLINED == 0 )) || return 0
  [[ -t 0 ]] || die "$(ctl_l10n '需要国际出口；请先执行 llmctl proxy set <IP> <端口>' 'International access is required; first run llmctl proxy set <IP> <PORT>')"
  local ip port scheme answer save_answer
  printf '%s\n' "$(ctl_l10n '国际网络检测失败；Hugging Face 搜索结果会缺失。' 'International connectivity failed; Hugging Face results will be missing.')" >&2
  while true; do
    read -r -p "$(ctl_l10n '是否现在配置代理？[Y/n] ' 'Configure a proxy now? [Y/n] ')" answer
    case "${answer}" in
      ""|y|Y|yes|YES) break ;;
      n|N|no|NO)
        MAINTENANCE_PROXY_DECLINED=1
        warn "$(ctl_l10n '已跳过代理；本次搜索可能只有 ModelScope 等当前可达来源。' 'Proxy setup was skipped; this search may contain only currently reachable sources such as ModelScope.')"
        return 0
        ;;
      *) warn "$(ctl_l10n '请输入 y 或 n。' 'Enter y or n.')" ;;
    esac
  done
  read -r -p "$(ctl_l10n '代理 IP/主机名: ' 'Proxy IP/hostname: ')" ip
  read -r -p "$(ctl_l10n '代理端口: ' 'Proxy port: ')" port
  read -r -p "$(ctl_l10n '协议 [http]: ' 'Scheme [http]: ')" scheme
  scheme="${scheme:-http}"
  MAINTENANCE_PROXY=$(proxy_url_from_args "${ip}" "${port}" "${scheme}")
  MAINTENANCE_NO_PROXY="127.0.0.1,localhost,::1"
  export_proxy_env
  hf_network_probe proxy || \
    die "$(ctl_l10n '代理后的 Hugging Face 网络复测失败，请检查代理配置' 'The Hugging Face retest through the proxy failed; check the proxy configuration')"
  while true; do
    read -r -p "$(ctl_l10n '是否保存为以后 llmctl 维护使用？[y/N] ' 'Save this proxy for future llmctl maintenance? [y/N] ')" save_answer
    case "${save_answer}" in
      y|Y|yes|YES) save_answer=y; break ;;
      ""|n|N|no|NO) save_answer=n; break ;;
      *) warn "$(ctl_l10n '请输入 y 或 n。' 'Enter y or n.')" ;;
    esac
  done
  if [[ "${save_answer}" == y ]]; then
    install -d -m 750 "${CONFIG_DIR}"
    umask 077
    printf 'MAINTENANCE_PROXY=%s\nMAINTENANCE_NO_PROXY=%s\n' "${MAINTENANCE_PROXY}" "${MAINTENANCE_NO_PROXY}" >"${PROXY_ENV}"
    chmod 600 "${PROXY_ENV}"
    log "$(ctl_l10n '维护代理已保存；不会注入推理服务。' 'The maintenance proxy was saved and will not be injected into inference services.')"
  fi
}

export_proxy_env() {
  [[ -n "${MAINTENANCE_PROXY:-}" ]] || return 0
  export HTTP_PROXY="${MAINTENANCE_PROXY}" HTTPS_PROXY="${MAINTENANCE_PROXY}"
  export http_proxy="${MAINTENANCE_PROXY}" https_proxy="${MAINTENANCE_PROXY}"
  export NO_PROXY="${MAINTENANCE_NO_PROXY}" no_proxy="${MAINTENANCE_NO_PROXY}"
}

setup_docker_proxy() {
  [[ -n "${MAINTENANCE_PROXY:-}" ]] || return 0
  install -d -m 755 "$(dirname "${DOCKER_PROXY_DROPIN}")"
  cat >"${DOCKER_PROXY_DROPIN}" <<EOF
[Service]
Environment="HTTP_PROXY=${MAINTENANCE_PROXY}"
Environment="HTTPS_PROXY=${MAINTENANCE_PROXY}"
Environment="NO_PROXY=${MAINTENANCE_NO_PROXY}"
EOF
  systemctl daemon-reload
  systemctl restart docker
}

clear_temporary_proxy() {
  unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy || true
  if [[ -e "${DOCKER_PROXY_DROPIN}" ]]; then
    rm -f "${DOCKER_PROXY_DROPIN}"
    systemctl daemon-reload
    systemctl restart docker || true
  fi
}

cmd_proxy() {
  require_root
  case "${1:-show}" in
    set)
      local url no_proxy
      url=$(proxy_url_from_args "${2:?缺少 IP}" "${3:?缺少端口}" "${4:-http}")
      no_proxy="127.0.0.1,localhost,::1"
      install -d -m 750 "${CONFIG_DIR}"
      umask 077
      cat >"${PROXY_ENV}" <<EOF
MAINTENANCE_PROXY=${url}
MAINTENANCE_NO_PROXY=${no_proxy}
EOF
      log "维护代理已保存；不会注入推理服务。"
      ;;
    show)
      load_saved_proxy
      if [[ -n "${MAINTENANCE_PROXY}" ]]; then printf '%s\n' "${MAINTENANCE_PROXY}"; else printf '未设置\n'; fi
      ;;
    clear)
      rm -f "${PROXY_ENV}"
      clear_temporary_proxy
      log "维护代理及 Docker 临时代理已清除。"
      ;;
    test)
      prompt_proxy_if_needed
      export_proxy_env
      curl -fsS --connect-timeout 10 --max-time 20 -o /dev/null https://huggingface.co || die "Hugging Face 代理测试失败"
      log "国际出口测试通过。"
      ;;
    *) die "proxy 子命令必须是 set|show|clear|test" ;;
  esac
}

run_catalog_maintenance() {
  [[ -x "${CATALOG_HELPER}" ]] || die "缺少模型目录助手 ${CATALOG_HELPER}"
  local language="${LLMCTL_LANG:-}"
  if [[ -z "${language}" && -r "${CLUSTER_ENV}" ]]; then
    language=$(awk -F= '$1=="INTERFACE_LANGUAGE"{print $2; exit}' "${CLUSTER_ENV}")
  fi
  [[ "${language}" == en || "${language}" == zh ]] || language=zh
  local -a command=(--lang "${language}" "$@")
  if python3 "${CATALOG_HELPER}" "${command[@]}"; then return 0; fi
  warn "目录查询未成功；尝试维护代理后重试。"
  prompt_proxy_if_needed
  export_proxy_env
  python3 "${CATALOG_HELPER}" "${command[@]}"
}

cmd_models() {
  require_root
  local action="${1:-hardware}"
  (($# == 0)) || shift
  case "${action}" in
    hardware)
      run_catalog_maintenance hardware "$@"
      ;;
    search)
      load_config
      local query="" source=all task=auto limit=10 show_rejected=0
      if (($#)) && [[ "$1" != --* ]]; then query="$1"; shift; fi
      while (($#)); do
        case "$1" in
          --source) source="${2:?缺少来源}"; shift 2 ;;
          --task) task="${2:?缺少用途}"; shift 2 ;;
          --limit) limit="${2:?缺少数量}"; shift 2 ;;
          --show-rejected) show_rejected=1; shift ;;
          *) die "未知 models search 参数：$1" ;;
        esac
      done
      if [[ "${source}" == all || "${source}" == huggingface ]]; then
        prompt_proxy_if_needed
        export_proxy_env
      fi
      local -a args=(search "${query}" --source "${source}" --task "${task}" --limit "${limit}" \
        --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}")
      (( show_rejected == 0 )) || args+=(--show-rejected)
      run_catalog_maintenance "${args[@]}"
      ;;
    inspect)
      load_config
      local source="${1:?缺少来源}" model_id="${2:?缺少 MODEL_ID}" revision="${3:-}"
      local -a args=(inspect "${source}" "${model_id}")
      [[ -z "${revision}" ]] || args+=("${revision}")
      args+=(--gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}")
      run_catalog_maintenance "${args[@]}"
      ;;
    current)
      load_config
      printf '%s:%s@%s\n' "${MODEL_HUB}" "${MODEL_ID}" "${MODEL_REVISION}"
      ;;
    *) die "models 子命令必须是 hardware|search|inspect|current" ;;
  esac
}

cmd_timezone() {
  require_root
  case "${1:-show}" in
    show) timedatectl status ;;
    set)
      local zone="${2:-Asia/Shanghai}"
      [[ "${zone}" =~ ^[A-Za-z0-9_+-]+/[A-Za-z0-9_+./-]+$ ]] || die "时区格式无效"
      [[ -e "/usr/share/zoneinfo/${zone}" ]] || die "系统不存在时区 ${zone}"
      timedatectl set-timezone "${zone}"
      log "系统时区已设置为 ${zone}。"
      timedatectl status
      ;;
    *) die "timezone 子命令必须是 show|set" ;;
  esac
}

download_model() {
  local model_id="${1:?}" revision="${2:?}" safe_name target partial current_link
  safe_name="${model_id//\//--}-${revision:0:12}"
  target="${MODEL_ROOT}/${safe_name}"
  partial="${target}.partial"
  current_link="${MODEL_ROOT}/current"
  install -d -m 755 "${MODEL_ROOT}"
  if [[ ! -r "${target}/config.json" ]]; then
    load_saved_proxy
    if [[ "${MODEL_HUB}" == huggingface && -z "${MAINTENANCE_PROXY}" ]]; then prompt_proxy_if_needed; fi
    export_proxy_env
    log "从 ${MODEL_HUB} 下载 ${model_id}@${revision} 到 ${target}（可断点续传）..."
    install -d -m 755 "${partial}"
    if [[ "${MODEL_HUB}" == huggingface ]]; then
      local -a env_args=() token_args=()
      [[ -n "${MAINTENANCE_PROXY:-}" ]] && env_args+=(
        -e "HTTP_PROXY=${MAINTENANCE_PROXY}" -e "HTTPS_PROXY=${MAINTENANCE_PROXY}"
        -e "NO_PROXY=${MAINTENANCE_NO_PROXY}"
      )
      [[ -z "${HF_TOKEN:-}" ]] || token_args+=(-e "HF_TOKEN=${HF_TOKEN}")
      docker run --rm --network host \
        -v "${MODEL_ROOT}:/models" \
        -e "LLM_MODEL_ID=${model_id}" -e "LLM_MODEL_REVISION=${revision}" \
        -e "LLM_MODEL_LOCAL=/models/$(basename "${partial}")" \
        "${env_args[@]}" "${token_args[@]}" \
        --entrypoint python3 "${VLLM_IMAGE}" -c '
import os
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id=os.environ["LLM_MODEL_ID"],
    revision=os.environ["LLM_MODEL_REVISION"],
    local_dir=os.environ["LLM_MODEL_LOCAL"],
)
' || die "Hugging Face 下载失败；保留 .partial 供下次续传"
    else
      [[ -x /opt/llm-cluster/hub-venv/bin/ms ]] || die "缺少 ModelScope 下载器；请重新运行安装脚本修复"
      /opt/llm-cluster/hub-venv/bin/ms download --help >/dev/null || die "ModelScope 下载器不支持 download 子命令；请重新运行安装脚本修复"
      /opt/llm-cluster/hub-venv/bin/ms download "${model_id}" --revision "${revision}" \
        --local-dir "${partial}" --max-workers 8 || die "ModelScope 下载失败；保留 .partial 供下次续传"
    fi
    [[ -r "${partial}/config.json" ]] || die "模型缺少 config.json"
    local weights bytes expected_min
    weights=$(find "${partial}" -maxdepth 2 -type f \( -name '*.safetensors' -o -name 'pytorch_model*.bin' -o -name 'model*.bin' \) | wc -l)
    (( weights >= 1 )) || die "模型目录中没有权重文件"
    bytes=$(du -sb "${partial}" | awk '{print $1}')
    expected_min=$((MODEL_WEIGHT_BYTES * 7 / 10))
    (( MODEL_WEIGHT_BYTES == 0 || bytes >= expected_min )) || die "模型体积异常：${bytes} < ${expected_min}"
    mv "${partial}" "${target}"
  fi
  [[ -r "${target}/config.json" ]] || die "模型缺少 config.json"
  local final_weights
  final_weights=$(find "${target}" -maxdepth 2 -type f \( -name '*.safetensors' -o -name 'pytorch_model*.bin' -o -name 'model*.bin' \) | wc -l)
  (( final_weights >= 1 )) || die "模型目录中没有权重文件"
  ln -sfn "$(basename "${target}")" "${current_link}"
  cat >"${MODEL_ROOT}/current.manifest" <<EOF
MODEL_ID=${model_id}
MODEL_REVISION=${revision}
MODEL_HUB=${MODEL_HUB}
MODEL_ARCHITECTURE=${MODEL_ARCHITECTURE}
LOCAL_DIR=${target}
INSTALLED_AT=$(date -u +%FT%TZ)
CONFIG_SHA256=$(sha256sum "${target}/config.json" | awk '{print $1}')
EOF
  log "本地模型 current -> $(basename "${target}")"
}

cmd_download() {
  require_root; load_config
  local model_id="${1:-${MODEL_ID}}" revision="${2:-${MODEL_REVISION}}"
  [[ "${model_id}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "MODEL_ID 格式无效"
  [[ "${revision}" =~ ^[A-Za-z0-9._/-]+$ ]] || die "revision 格式无效"
  [[ "${model_id}" == "${MODEL_ID}" && "${revision}" == "${MODEL_REVISION}" ]] || \
    die "直接切换模型可能改变 TP、上下文和能力参数；请重新运行安装器 --force-reconfigure 完成硬件规划与验收"
  download_model "${model_id}" "${revision}"
  warn "当前模型文件已核验；如服务此前因缺文件停止，请运行 llmctl restart all。"
}

cmd_upgrade() {
  require_root
  require_installed
  [[ -x "${CONTROL_PLANE_UPDATER}" ]] || \
    die "未找到控制面升级器 ${CONTROL_PLANE_UPDATER}；请先从新版发布包运行一次 upgrade-llmctl.sh --from-zip FILE"
  local language="zh"
  if [[ -r "${CLUSTER_ENV}" ]] && grep -Eq '^INTERFACE_LANGUAGE=en$' "${CLUSTER_ENV}"; then language="en"; fi
  exec "${CONTROL_PLANE_UPDATER}" --lang "${language}" "$@"
}

cmd_rollback() {
  require_root
  require_installed
  [[ $# == 1 ]] || die "用法：llmctl rollback /var/backups/llmctl/control-plane-TIMESTAMP"
  [[ -x "${CONTROL_PLANE_UPDATER}" ]] || \
    die "未找到控制面升级器 ${CONTROL_PLANE_UPDATER}"
  local language="zh"
  if [[ -r "${CLUSTER_ENV}" ]] && grep -Eq '^INTERFACE_LANGUAGE=en$' "${CLUSTER_ENV}"; then language="en"; fi
  exec "${CONTROL_PLANE_UPDATER}" --lang "${language}" --rollback "$1"
}

cmd_update() {
  require_root; load_config
  local new_vllm="${VLLM_IMAGE}" new_gateway="${GATEWAY_IMAGE}" new_postgres="${POSTGRES_IMAGE}" was_cluster_active=0 stopped_for_proxy=0
  while (($#)); do
    case "$1" in
      --vllm-image) new_vllm="${2:?缺少镜像}"; shift 2 ;;
      --gateway-image) new_gateway="${2:?缺少镜像}"; shift 2 ;;
      --litellm-image)
        [[ "${GATEWAY_KIND}" == litellm ]] || die "当前网关不是 LiteLLM；请使用 --gateway-image"
        new_gateway="${2:?缺少镜像}"; shift 2 ;;
      --newapi-image)
        [[ "${GATEWAY_KIND}" == newapi ]] || die "当前网关不是 New API；请使用 --gateway-image"
        new_gateway="${2:?缺少镜像}"; shift 2 ;;
      --bifrost-image)
        [[ "${GATEWAY_KIND}" == bifrost ]] || die "当前网关不是 Bifrost；请使用 --gateway-image"
        new_gateway="${2:?缺少镜像}"; shift 2 ;;
      --omniroute-image)
        [[ "${GATEWAY_KIND}" == omniroute ]] || die "当前网关不是 OmniRoute；请使用 --gateway-image"
        new_gateway="${2:?缺少镜像}"; shift 2 ;;
      --postgres-image) new_postgres="${2:?缺少镜像}"; shift 2 ;;
      *) die "未知 update 参数：$1" ;;
    esac
  done
  [[ "${new_vllm}" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die "vLLM 镜像名格式无效"
  [[ "${new_gateway}" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die "网关镜像名格式无效"
  [[ "${new_postgres}" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die "PostgreSQL 镜像名格式无效"
  prompt_proxy_if_needed
  export_proxy_env
  if [[ -n "${MAINTENANCE_PROXY:-}" ]] && systemctl is-active --quiet llm-cluster.service; then
    was_cluster_active=1
    stopped_for_proxy=1
    warn "Docker daemon 设置临时代理需要重启；先受控停止集群，更新后自动恢复。"
    systemctl stop llm-cluster.service
  fi
  trap 'clear_temporary_proxy; (( was_cluster_active == 0 )) || systemctl start llm-cluster.service 2>/dev/null || true' EXIT
  setup_docker_proxy
  local image_validation_failed=0
  docker pull "${new_vllm}" || image_validation_failed=1
  docker pull "${new_gateway}" || image_validation_failed=1
  if [[ "${GATEWAY_KIND}" != omniroute ]]; then
    docker pull "${new_postgres}" || image_validation_failed=1
  fi
  image_supports_architecture "${new_vllm}" "${MODEL_ARCHITECTURE}" || image_validation_failed=1
  if (( image_validation_failed )); then
    clear_temporary_proxy
    trap - EXIT
    (( was_cluster_active == 0 )) || systemctl start llm-cluster.service 2>/dev/null || true
    die "镜像拉取或 vLLM 架构核验失败；配置未修改"
  fi
  clear_temporary_proxy
  trap - EXIT
  set_env_value "${CLUSTER_ENV}" VLLM_IMAGE "${new_vllm}"
  set_env_value "${CLUSTER_ENV}" GATEWAY_IMAGE "${new_gateway}"
  case "${GATEWAY_KIND}" in
    newapi) set_env_value "${CLUSTER_ENV}" NEWAPI_IMAGE "${new_gateway}" ;;
    litellm) set_env_value "${CLUSTER_ENV}" LITELLM_IMAGE "${new_gateway}" ;;
    bifrost) set_env_value "${CLUSTER_ENV}" BIFROST_IMAGE "${new_gateway}" ;;
    omniroute) set_env_value "${CLUSTER_ENV}" OMNIROUTE_IMAGE "${new_gateway}" ;;
  esac
  set_env_value "${CLUSTER_ENV}" POSTGRES_IMAGE "${new_postgres}"
  if (( stopped_for_proxy )); then
    if ! systemctl start llm-cluster.service || ! cmd_smoke --full; then
      warn "新镜像验收失败，回滚到原镜像。"
      systemctl stop llm-cluster.service 2>/dev/null || true
      set_env_value "${CLUSTER_ENV}" VLLM_IMAGE "${VLLM_IMAGE}"
      set_env_value "${CLUSTER_ENV}" GATEWAY_IMAGE "${GATEWAY_IMAGE}"
      case "${GATEWAY_KIND}" in
        newapi) set_env_value "${CLUSTER_ENV}" NEWAPI_IMAGE "${NEWAPI_IMAGE}" ;;
        litellm) set_env_value "${CLUSTER_ENV}" LITELLM_IMAGE "${LITELLM_IMAGE}" ;;
        bifrost) set_env_value "${CLUSTER_ENV}" BIFROST_IMAGE "${BIFROST_IMAGE}" ;;
        omniroute) set_env_value "${CLUSTER_ENV}" OMNIROUTE_IMAGE "${OMNIROUTE_IMAGE}" ;;
      esac
      set_env_value "${CLUSTER_ENV}" POSTGRES_IMAGE "${POSTGRES_IMAGE}"
      systemctl reset-failed >/dev/null 2>&1 || true
      systemctl start llm-cluster.service 2>/dev/null || true
      die "更新失败并已回滚配置；请检查日志"
    fi
    log "镜像已更新，集群已恢复并通过完整冒烟测试。"
  else
    log "镜像已拉取并锁定；当前运行实例不受影响。维护窗口执行 systemctl restart llm-cluster，再运行 llmctl smoke --full。"
  fi
}

safe_tar_listing() {
  local archive="${1:?}"
  tar -tf "${archive}" | awk 'BEGIN{bad=0} /^\// || /(^|\/)\.\.($|\/)/ {bad=1} END{exit bad}' || die "离线包含不安全路径：${archive}"
  tar -tvf "${archive}" | awk 'substr($1,1,1)=="l" || substr($1,1,1)=="h" {bad=1} END{exit bad}' || die "离线模型包不允许符号链接或硬链接"
}

read_bundle_env() {
  local file="${1:?}" key value
  IMPORT_VLLM_IMAGE="" IMPORT_GATEWAY_KIND="" IMPORT_GATEWAY_IMAGE="" IMPORT_POSTGRES_IMAGE="" IMPORT_MODEL_HUB="" IMPORT_MODEL_ID="" IMPORT_MODEL_REVISION="" IMPORT_MODEL_ARCHITECTURE="" IMPORT_MODEL_DIR_NAME=""
  while IFS='=' read -r key value; do
    [[ -n "${key}" && "${key}" != \#* ]] || continue
    case "${key}" in
      VLLM_IMAGE) IMPORT_VLLM_IMAGE="${value}" ;;
      GATEWAY_KIND) IMPORT_GATEWAY_KIND="${value}" ;;
      GATEWAY_IMAGE) IMPORT_GATEWAY_IMAGE="${value}" ;;
      POSTGRES_IMAGE) IMPORT_POSTGRES_IMAGE="${value}" ;;
      MODEL_HUB) IMPORT_MODEL_HUB="${value}" ;;
      MODEL_ID) IMPORT_MODEL_ID="${value}" ;;
      MODEL_REVISION) IMPORT_MODEL_REVISION="${value}" ;;
      MODEL_ARCHITECTURE) IMPORT_MODEL_ARCHITECTURE="${value}" ;;
      MODEL_DIR_NAME) IMPORT_MODEL_DIR_NAME="${value}" ;;
      EXPORTED_AT) ;;
      *) die "离线 bundle.env 含未知键：${key}" ;;
    esac
  done <"${file}"
  [[ "${IMPORT_VLLM_IMAGE}" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die "离线 vLLM 镜像名无效"
  [[ "${IMPORT_GATEWAY_KIND}" =~ ^(newapi|litellm|bifrost|omniroute)$ ]] || die "离线 GATEWAY_KIND 无效"
  [[ "${IMPORT_GATEWAY_IMAGE}" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die "离线网关镜像名无效"
  [[ "${IMPORT_GATEWAY_KIND}" == "${GATEWAY_KIND}" ]] || die "离线包网关与当前安装规划不一致"
  [[ "${IMPORT_POSTGRES_IMAGE}" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die "离线 PostgreSQL 镜像名无效"
  [[ "${IMPORT_MODEL_HUB}" =~ ^(huggingface|modelscope)$ ]] || die "离线 MODEL_HUB 无效"
  [[ "${IMPORT_MODEL_ID}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "离线 MODEL_ID 无效"
  [[ "${IMPORT_MODEL_REVISION}" =~ ^[A-Za-z0-9._/-]+$ ]] || die "离线 revision 无效"
  [[ "${IMPORT_MODEL_ARCHITECTURE}" =~ ^[A-Za-z0-9_]+$ ]] || die "离线架构无效"
  [[ "${IMPORT_MODEL_DIR_NAME}" =~ ^[A-Za-z0-9._-]+$ ]] || die "离线模型目录名无效"
}

cmd_offline() {
  require_root; load_config
  local action="${1:-}" dir="${2:-}"
  [[ -n "${dir}" ]] || die "请指定离线包目录"
  case "${action}" in
    export)
      mkdir -p "${dir}"
      [[ -w "${dir}" ]] || die "目录不可写：${dir}"
      local current_name
      current_name=$(readlink "${MODEL_ROOT}/current")
      [[ -n "${current_name}" && -d "${MODEL_ROOT}/${current_name}" ]] || die "本地模型不存在"
      log "导出 vLLM 镜像（文件可能很大）..."
      docker save -o "${dir}/vllm-image.tar" "${VLLM_IMAGE}"
      log "导出 $(gateway_display_name) 镜像..."
      docker save -o "${dir}/gateway-image.tar" "${GATEWAY_IMAGE}"
      if [[ "${GATEWAY_KIND}" != omniroute ]]; then
        log "导出 PostgreSQL 镜像..."
        docker save -o "${dir}/postgres-image.tar" "${POSTGRES_IMAGE}"
      fi
      log "导出当前模型（未压缩以加快恢复）..."
      tar -C "${MODEL_ROOT}" -cf "${dir}/model.tar" "${current_name}" current.manifest
      cat >"${dir}/bundle.env" <<EOF
VLLM_IMAGE=${VLLM_IMAGE}
GATEWAY_KIND=${GATEWAY_KIND}
GATEWAY_IMAGE=${GATEWAY_IMAGE}
POSTGRES_IMAGE=${POSTGRES_IMAGE}
MODEL_HUB=${MODEL_HUB}
MODEL_ID=${MODEL_ID}
MODEL_REVISION=${MODEL_REVISION}
MODEL_ARCHITECTURE=${MODEL_ARCHITECTURE}
MODEL_DIR_NAME=${current_name}
EXPORTED_AT=$(date -u +%FT%TZ)
EOF
      if [[ "${GATEWAY_KIND}" == omniroute ]]; then
        (cd "${dir}" && sha256sum vllm-image.tar gateway-image.tar model.tar >SHA256SUMS)
      else
        (cd "${dir}" && sha256sum vllm-image.tar gateway-image.tar postgres-image.tar model.tar >SHA256SUMS)
      fi
      log "离线包已导出到 ${dir}"
      ;;
    import)
      [[ -r "${dir}/bundle.env" && -r "${dir}/SHA256SUMS" ]] || die "离线包清单不完整"
      read_bundle_env "${dir}/bundle.env"
      if [[ "${IMPORT_GATEWAY_KIND}" == omniroute ]]; then
        awk 'NF==2 && length($1)==64 && $1 !~ /[^0-9a-f]/ && ($2=="vllm-image.tar" || $2=="gateway-image.tar" || $2=="model.tar") && !seen[$2]++ {ok++} END{exit !(ok==3)}' "${dir}/SHA256SUMS" || die "SHA256SUMS 格式或文件名不安全"
      else
        awk 'NF==2 && length($1)==64 && $1 !~ /[^0-9a-f]/ && ($2=="vllm-image.tar" || $2=="gateway-image.tar" || $2=="postgres-image.tar" || $2=="model.tar") && !seen[$2]++ {ok++} END{exit !(ok==4)}' "${dir}/SHA256SUMS" || die "SHA256SUMS 格式或文件名不安全"
      fi
      (cd "${dir}" && sha256sum -c SHA256SUMS) || die "离线包校验失败"
      safe_tar_listing "${dir}/model.tar"
      [[ "${IMPORT_MODEL_HUB}:${IMPORT_MODEL_ID}@${IMPORT_MODEL_REVISION}:${IMPORT_MODEL_ARCHITECTURE}" == \
         "${MODEL_HUB}:${MODEL_ID}@${MODEL_REVISION}:${MODEL_ARCHITECTURE}" ]] || \
        die "离线包模型与当前硬件规划不一致；请用安装器选择该模型并生成匹配配置后再导入"
      tar -tf "${dir}/model.tar" | awk -v root="${IMPORT_MODEL_DIR_NAME}/" '$0 != "current.manifest" && index($0,root) != 1 {bad=1} END{exit bad}' || die "离线模型包路径与清单不一致"
      docker load -i "${dir}/vllm-image.tar"
      docker load -i "${dir}/gateway-image.tar"
      [[ "${IMPORT_GATEWAY_KIND}" == omniroute ]] || docker load -i "${dir}/postgres-image.tar"
      image_supports_architecture "${IMPORT_VLLM_IMAGE}" "${MODEL_ARCHITECTURE}" || die "离线 vLLM 镜像不支持 ${MODEL_ARCHITECTURE}"
      install -d -m 755 "${MODEL_ROOT}"
      tar -C "${MODEL_ROOT}" -xf "${dir}/model.tar"
      ln -sfn "${IMPORT_MODEL_DIR_NAME}" "${MODEL_ROOT}/current"
      set_env_value "${CLUSTER_ENV}" VLLM_IMAGE "${IMPORT_VLLM_IMAGE}"
      set_env_value "${CLUSTER_ENV}" GATEWAY_IMAGE "${IMPORT_GATEWAY_IMAGE}"
      case "${GATEWAY_KIND}" in
        newapi) set_env_value "${CLUSTER_ENV}" NEWAPI_IMAGE "${IMPORT_GATEWAY_IMAGE}" ;;
        litellm) set_env_value "${CLUSTER_ENV}" LITELLM_IMAGE "${IMPORT_GATEWAY_IMAGE}" ;;
        bifrost) set_env_value "${CLUSTER_ENV}" BIFROST_IMAGE "${IMPORT_GATEWAY_IMAGE}" ;;
        omniroute) set_env_value "${CLUSTER_ENV}" OMNIROUTE_IMAGE "${IMPORT_GATEWAY_IMAGE}" ;;
      esac
      set_env_value "${CLUSTER_ENV}" POSTGRES_IMAGE "${IMPORT_POSTGRES_IMAGE}"
      log "离线包导入完成；运行 llmctl restart all 生效。"
      ;;
    *) die "offline 子命令必须是 export|import" ;;
  esac
}

cmd_uninstall() {
  require_root; load_config
  local purge_model=0 purge_images=0 purge_database=0 assume_yes=0 arg
  for arg in "$@"; do
    case "${arg}" in
      --purge-model) purge_model=1 ;;
      --purge-images) purge_images=1 ;;
      --purge-database) purge_database=1 ;;
      --yes) assume_yes=1 ;;
      *) die "未知 uninstall 参数：${arg}" ;;
    esac
  done
  if (( ! assume_yes )); then
    [[ -t 0 ]] || die "非交互卸载需要 --yes"
    local answer
    read -r -p "将停止并删除 LLM systemd 服务、配置和可再生成的编译缓存；继续？[y/N] " answer
    [[ "${answer}" =~ ^[Yy]$ ]] || { log "已取消。"; return 0; }
  fi
  log "卸载 1/4：禁用开机自启。"
  # Only remove boot activation here. Stopping is deliberately delegated to
  # the bounded, visible, concurrent lifecycle below so uninstall cannot
  # become silent while systemd waits for a probe or container.
  systemctl disable llm-keepwarm.timer 2>/dev/null || true
  systemctl disable llm-cluster.service 2>/dev/null || true
  systemctl disable llm-workflow.service 2>/dev/null || true
  log "卸载 2/4：并发停止 Router、数据库和 ${INSTANCE_COUNT} 个 Worker。"
  stop_managed_services_with_progress 180 || \
    die "LLM 服务未能在限定时间内安全停止；配置尚未删除，请根据上方单位/容器状态检查"
  log "卸载 3/4：删除 systemd 单元和可再生成数据；配置保留到最后一步。"
  remove_nginx_config
  remove_tree_with_progress "${NGINX_STATE_DIR}" "可再生成的 Nginx 回滚缓存" 2
  rm -f /etc/systemd/system/llm-cluster.service /etc/systemd/system/llm-router.service /etc/systemd/system/llm-database.service /etc/systemd/system/llm-account.service /etc/systemd/system/llm-worker@.service /etc/systemd/system/llm-keepwarm.service /etc/systemd/system/llm-keepwarm.timer /etc/systemd/system/llm-workflow.service
  systemctl daemon-reload
  systemctl reset-failed >/dev/null 2>&1 || true
  clear_temporary_proxy
  if docker network inspect "${DOCKER_NETWORK}" >/dev/null 2>&1; then
    if [[ "$(docker network inspect --format '{{len .Containers}}' "${DOCKER_NETWORK}" 2>/dev/null || printf 1)" == 0 ]]; then
      docker network rm "${DOCKER_NETWORK}" >/dev/null
      log "Docker 内部网络 ${DOCKER_NETWORK} 已清理。"
    else
      warn "Docker 网络 ${DOCKER_NETWORK} 仍有非 LLMCtl 端点，已保留。"
    fi
  fi
  if (( ! purge_database )); then
    install -d -m 700 "${STATE_DIR}"
    install -m 600 "${SECRETS_ENV}" "${RETAINED_SECRETS}"
    chown root:root "${RETAINED_SECRETS}"
  fi
  [[ "${CACHE_DIR}" == /var/lib/llm-cluster/cache ]] || die "缓存路径安全检查失败"
  remove_tree_with_progress "${CACHE_DIR}" "可再生成编译缓存" 5
  [[ "${KEEPWARM_STATE_DIR}" == /var/lib/llm-cluster/keepwarm ]] || die "保活状态路径安全检查失败"
  remove_tree_with_progress "${KEEPWARM_STATE_DIR}" "可再生成保活状态" 2
  if (( purge_images )); then
    log "删除锁定的 LLM 容器镜像。"
    if [[ "${GATEWAY_KIND}" == omniroute ]]; then
      docker image rm "${VLLM_IMAGE}" "${GATEWAY_IMAGE}" 2>/dev/null || true
    else
      docker image rm "${VLLM_IMAGE}" "${GATEWAY_IMAGE}" "${POSTGRES_IMAGE}" 2>/dev/null || true
    fi
  fi
  if (( purge_database )); then
    if docker volume inspect llm-cluster-gateway-postgres >/dev/null 2>&1; then
      docker volume rm llm-cluster-gateway-postgres >/dev/null
      log "接入层 PostgreSQL 数据卷 llm-cluster-gateway-postgres 已永久删除。"
    else
      log "未发现接入层 PostgreSQL 数据卷，无需删除。"
    fi
    docker volume rm llm-cluster-gateway-data >/dev/null 2>&1 || true
    log "接入层本地状态卷 llm-cluster-gateway-data 已清理（如存在）。"
    if [[ -d "${STATE_DIR}/omniroute" ]]; then
      remove_tree_with_progress "${STATE_DIR}/omniroute" "OmniRoute 与账户门户 SQLite 数据" 5
    fi
    if getent passwd llm-account 2>/dev/null | awk -F: '$6=="/nonexistent" && $7=="/usr/sbin/nologin" {found=1} END{exit !found}'; then
      userdel llm-account 2>/dev/null || true
      groupdel llm-account 2>/dev/null || true
      log "账户门户专用系统用户 llm-account 已清理。"
    fi
    rm -f "${RETAINED_SECRETS}"
  else
    log "接入层 PostgreSQL/SQLite 状态和 root-only 恢复凭据 ${RETAINED_SECRETS} 已保留；重装相同接入层时可继续使用。"
  fi
  if getent passwd llm-workflow 2>/dev/null | awk -F: '$6=="/nonexistent" && $7=="/usr/sbin/nologin" {found=1} END{exit !found}'; then
    [[ "${WORKFLOW_STATE_DIR}" == /var/lib/llm-cluster/workflow ]] || die "工作流状态路径安全检查失败"
    remove_tree_with_progress "${WORKFLOW_STATE_DIR}" "工作流路由配置" 2
    userdel llm-workflow 2>/dev/null || true
    groupdel llm-workflow 2>/dev/null || true
    log "工作流专用系统用户 llm-workflow 已清理。"
  fi
  if (( purge_model )); then
    local normalized_root="${MODEL_ROOT%/}"
    case "${normalized_root}" in
      ""|/|/data|/mnt|/media|/srv|/opt|/var|/home|/root|/usr)
        die "拒绝删除过于宽泛的模型路径：${normalized_root:-/}"
        ;;
    esac
    [[ -f "${normalized_root}/.llm-cluster-model-root" ]] || die "缺少 .llm-cluster-model-root 标记，拒绝删除：${normalized_root}"
    remove_tree_with_progress "${MODEL_ROOT}" "模型目录（永久删除）" 10
  else
    log "模型已保留在 ${MODEL_ROOT}；加 --purge-model 才会删除。"
  fi
  rm -rf "${CONFIG_DIR}"
  rm -f /usr/local/sbin/llmctl
  rm -rf /usr/local/lib/llm-cluster /opt/llm-cluster/hub-venv
  (( purge_database == 0 )) || rmdir "${STATE_DIR}" 2>/dev/null || true
  log "卸载 4/4：LLM 集群服务已卸载。"
}

remove_tree_with_progress() {
  local target="${1:?}" label="${2:?}" interval="${3:-5}" started now elapsed pid result=0 last_report=0
  if [[ ! -e "${target}" && ! -L "${target}" ]]; then
    log "${label} ${target} 不存在，无需删除。"
    return 0
  fi
  log "开始删除${label}：${target}"
  started=$(date +%s)
  rm -rf --one-file-system -- "${target}" &
  pid=$!
  while kill -0 "${pid}" 2>/dev/null; do
    sleep 1
    now=$(date +%s)
    elapsed=$((now - started))
    if (( elapsed - last_report >= interval )); then
      log "删除${label}中 ${elapsed}s：进程仍在工作，请勿重复执行卸载。"
      last_report=${elapsed}
    fi
  done
  wait "${pid}" || result=$?
  (( result == 0 )) || die "删除${label}失败：${target}（rm exit=${result}）"
  now=$(date +%s)
  log "${label}已删除：${target}（耗时 $((now - started))s）。"
}

managed_container_names() {
  local id out="llm-router"
  [[ "${GATEWAY_KIND}" == omniroute ]] || out+=",llm-database"
  for ((id = 0; id < INSTANCE_COUNT; id++)); do out+=",llm-worker-${id}"; done
  printf '%s\n' "${out}"
}

running_managed_containers() {
  local names name running out=""
  names=$(managed_container_names)
  IFS=',' read -r -a container_name_list <<<"${names}"
  for name in "${container_name_list[@]}"; do
    running=$(docker inspect -f '{{.State.Running}}' "${name}" 2>/dev/null || true)
    [[ "${running}" == true ]] && out+="${out:+,}${name}"
  done
  printf '%s\n' "${out}"
}

active_managed_units() {
  local unit state out=""
  local -a units=(llm-cluster.service llm-router.service llm-keepwarm.service llm-keepwarm.timer llm-workflow.service)
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then units+=(llm-account.service); else units+=(llm-database.service); fi
  local id
  for ((id = 0; id < INSTANCE_COUNT; id++)); do units+=("$(worker_unit "${id}")"); done
  for unit in "${units[@]}"; do
    state=$(systemctl show "${unit}" -p ActiveState --value 2>/dev/null || printf unknown)
    case "${state}" in
      active|activating|deactivating|reloading) out+="${out:+,}${unit}:${state}" ;;
    esac
  done
  printf '%s\n' "${out}"
}

wait_managed_services_stopped() {
  local timeout="${1:?}" started now elapsed units containers
  started=$(date +%s)
  while true; do
    units=$(active_managed_units)
    containers=$(running_managed_containers)
    now=$(date +%s)
    elapsed=$((now - started))
    log "停止中 ${elapsed}s：活动单位=[${units:-none}]；运行容器=[${containers:-none}]；VRAM=[$(gpu_memory_snapshot)]"
    [[ -z "${units}" && -z "${containers}" ]] && return 0
    (( elapsed < timeout )) || return 1
    sleep 5
  done
}

force_stop_managed_services() {
  local names name
  local -a units=(llm-cluster.service llm-router.service llm-keepwarm.service llm-workflow.service)
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then units+=(llm-account.service); else units+=(llm-database.service); fi
  local id
  for ((id = 0; id < INSTANCE_COUNT; id++)); do units+=("$(worker_unit "${id}")"); done
  systemctl kill --kill-whom=all --signal=SIGKILL "${units[@]}" 2>/dev/null || true
  names=$(managed_container_names)
  IFS=',' read -r -a container_name_list <<<"${names}"
  for name in "${container_name_list[@]}"; do
    timeout 20 docker rm -f "${name}" >/dev/null 2>&1 &
  done
  wait || true
}

stop_managed_services_with_progress() {
  local timeout="${1:-180}" id
  local -a units=(llm-cluster.service llm-router.service llm-keepwarm.service llm-keepwarm.timer llm-workflow.service)
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then units+=(llm-account.service); else units+=(llm-database.service); fi
  for ((id = 0; id < INSTANCE_COUNT; id++)); do units+=("$(worker_unit "${id}")"); done
  systemctl stop --no-block "${units[@]}" 2>/dev/null || true
  if wait_managed_services_stopped "${timeout}"; then
    log "所有 LLM systemd 单元和容器均已停止。"
    return 0
  fi
  warn "正常停止超过 ${timeout}s；对本集群命名空间执行一次有界强制停止。"
  force_stop_managed_services
  wait_managed_services_stopped 30
}

cmd_boot_start() {
  require_root; load_config
  local startup_failed=0 healthy
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    log "OmniRoute 与账户门户使用两个隔离的 SQLite 数据库；无需启动数据库实例。"
  else
    log "启动 PostgreSQL 管理数据库..."
    ensure_database_ready
  fi
  log "按每批 ${STARTUP_PARALLELISM} 个启动 Worker：${ACTIVE_WORKERS}。"
  start_worker_ids_batched "${ACTIVE_WORKERS}" || startup_failed=1
  healthy=$(healthy_worker_ids)
  [[ -n "${healthy}" ]] || die "没有 Worker 成功启动"
  render_router_config "${healthy}"
  log "启动 $(gateway_display_name) 接入层..."
  systemctl start llm-router.service
  wait_gateway_process
  reconcile_gateway "${healthy}"
  wait_router
  if [[ "${GATEWAY_KIND}" == omniroute ]]; then
    log "启动公司账户门户..."
    systemctl start llm-account.service
    wait_account_portal
  fi
  (( startup_failed == 0 )) || warn "至少一个 Worker 启动失败；健康 Worker 已继续提供服务。"
}

cmd_boot_stop() {
  require_root; load_config
  # Every child unit declares PartOf=llm-cluster.service. systemd already adds
  # their stop jobs to the same transaction and runs them concurrently. Calling
  # `systemctl stop` recursively from this ExecStop can wait on its own parent
  # transaction and was the cause of the legacy uninstall appearing hung.
  log "systemd 已并发停止 Router、可选数据库/账户门户和 ${INSTANCE_COUNT} 个 Worker。"
}

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
    responses) cmd_responses "$@" ;;
    router) cmd_router "$@" ;;
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

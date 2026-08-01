#!/usr/bin/env bash
# Hardware-aware vLLM cluster manager for Ubuntu 24.04.
# Installed by install-llm-cluster.sh as /usr/local/sbin/llmctl.

set -Eeuo pipefail
IFS=$'\n\t'

readonly CTL_VERSION="2.0.1"
readonly CONFIG_DIR="${LLM_CLUSTER_CONFIG_DIR:-/etc/llm-cluster}"
readonly STATE_DIR="/var/lib/llm-cluster"
readonly CACHE_DIR="${STATE_DIR}/cache"
readonly RETAINED_SECRETS="${STATE_DIR}/retained-secrets.env"
readonly CLUSTER_ENV="${CONFIG_DIR}/cluster.env"
readonly SECRETS_ENV="${CONFIG_DIR}/secrets.env"
readonly PROXY_ENV="${CONFIG_DIR}/proxy.env"
readonly ROUTER_CONFIG="${CONFIG_DIR}/litellm.yaml"
readonly DOCKER_PROXY_DROPIN="/etc/systemd/system/docker.service.d/90-llm-cluster-temporary-proxy.conf"
readonly CATALOG_HELPER="${LLM_CATALOG_HELPER:-/usr/local/lib/llm-cluster/model_catalog.py}"

log()  { printf '[llmctl] %s\n' "$*"; }
warn() { printf '[llmctl] WARNING: %s\n' "$*" >&2; }
die()  { printf '[llmctl] ERROR: %s\n' "$*" >&2; exit 1; }

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

  : "${PHYSICAL_GPU_COUNT:?PHYSICAL_GPU_COUNT missing}"
  : "${TP_SIZE:?TP_SIZE missing}"
  : "${INSTANCE_COUNT:?INSTANCE_COUNT missing}"
  : "${ACTIVE_WORKERS:?ACTIVE_WORKERS missing}"
  : "${WORKER_BASE_PORT:?WORKER_BASE_PORT missing}"
  : "${API_BIND:?API_BIND missing}"
  : "${API_PORT:?API_PORT missing}"
  : "${MODEL_ROOT:?MODEL_ROOT missing}"
  : "${SERVED_MODEL_NAME:?SERVED_MODEL_NAME missing}"
  : "${VLLM_IMAGE:?VLLM_IMAGE missing}"
  : "${LITELLM_IMAGE:?LITELLM_IMAGE missing}"
  : "${POSTGRES_IMAGE:?POSTGRES_IMAGE missing}"
  : "${LITELLM_DB_PORT:?LITELLM_DB_PORT missing}"
  : "${MAX_MODEL_LEN:?MAX_MODEL_LEN missing}"
  : "${MAX_NUM_SEQS:?MAX_NUM_SEQS missing}"
  : "${MAX_NUM_BATCHED_TOKENS:?MAX_NUM_BATCHED_TOKENS missing}"
  : "${GPU_MEMORY_UTILIZATION:?GPU_MEMORY_UTILIZATION missing}"
  : "${MM_LIMIT:?MM_LIMIT missing}"
  : "${ROUTING_STRATEGY:?ROUTING_STRATEGY missing}"
  : "${START_TIMEOUT:?START_TIMEOUT missing}"
  : "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY missing}"
  : "${BACKEND_API_KEY:?BACKEND_API_KEY missing}"
  : "${LITELLM_SALT_KEY:?LITELLM_SALT_KEY missing}"
  : "${UI_USERNAME:?UI_USERNAME missing}"
  : "${UI_PASSWORD:?UI_PASSWORD missing}"
  : "${POSTGRES_USER:?POSTGRES_USER missing}"
  : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing}"
  : "${POSTGRES_DB:?POSTGRES_DB missing}"
  : "${DATABASE_URL:?DATABASE_URL missing}"
  [[ "${STARTUP_PARALLELISM}" =~ ^[0-9]+$ ]] && (( STARTUP_PARALLELISM >= 1 && STARTUP_PARALLELISM <= INSTANCE_COUNT )) || die "STARTUP_PARALLELISM 必须在 1-${INSTANCE_COUNT}"
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

  local -a docker_args=(
    /usr/bin/docker run --rm --name "llm-worker-${id}"
    --network host --ipc host --runtime=nvidia
    -e "NVIDIA_VISIBLE_DEVICES=${GPU_DEVICES}"
    -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1
    -e VLLM_NO_USAGE_STATS=1 -e VLLM_MEDIA_URL_ALLOW_REDIRECTS=0
    -v "${MODEL_ROOT}:/models:ro"
    -v "${CACHE_DIR}/shared:/root/.cache"
    "${VLLM_IMAGE}" /models/current
    --served-model-name "${SERVED_MODEL_NAME}"
    --host 127.0.0.1 --port "${WORKER_PORT}"
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
  llmctl router <start|stop|restart|status>    管理 LiteLLM
  llmctl database <start|stop|restart|status>  管理 LiteLLM PostgreSQL
  llmctl timezone show|set [时区]              查看或设置系统时区（默认 Asia/Shanghai）

  llmctl logs worker <ID> [-f]                Worker 日志
  llmctl logs router [-f]                     LiteLLM 日志
  llmctl logs database [-f]                   PostgreSQL 日志
  llmctl smoke [--worker ID] [--full]         文本/思考/工具；--full 另测 OCR/单请求6图
  llmctl ocr <图片文件> [提示词]               通过集群入口执行 OCR
  llmctl bench [--concurrency N] [--requests N] [--max-tokens N]
                                                并发吞吐验收（默认 25/50/512）

  llmctl tune show                            显示推理与路由参数
  llmctl tune set <键> <值>                   修改参数（修改后需重启 Worker）
    可修改键：max-model-len, gpu-memory-utilization, max-num-seqs,
              max-num-batched-tokens, max-images, routing-strategy,
              api-bind, api-port, startup-parallelism

  llmctl key show                             显示调用地址、模型名和 API key
  llmctl key rotate [新KEY]                   轮换 LiteLLM 入口 key
  llmctl admin show                           显示 Web UI 地址和管理员凭据
  llmctl admin set-username USER              修改 Web UI 管理员用户名
  llmctl admin set-password [PASSWORD]        修改密码；省略时安全交互输入
  llmctl proxy set <IP> <端口> [http|https]    保存“仅维护使用”的代理
  llmctl proxy show|clear|test                 查看/清除/测试维护代理

  llmctl models hardware                       显示 GPU 与显存
  llmctl models search [QUERY] [--source all|huggingface|modelscope]
                                                搜索并仅列出本机可部署模型
  llmctl models inspect <SOURCE> <MODEL_ID> [REVISION]
                                                检查能力、兼容性和推荐拓扑
  llmctl download [MODEL_ID] [REVISION]        重新核验或补齐当前模型文件
  llmctl update [--vllm-image IMG] [--litellm-image IMG] [--postgres-image IMG]
                                                显式拉取镜像；绝不自动更新
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
  local file="${1:?}" key="${2:?}" value="${3-}" tmp
  tmp=$(mktemp "${file}.XXXXXX")
  awk -F= -v key="${key}" '$1 != key {print}' "${file}" >"${tmp}"
  printf '%s=%s\n' "${key}" "${value}" >>"${tmp}"
  chmod --reference="${file}" "${tmp}" 2>/dev/null || chmod 600 "${tmp}"
  chown --reference="${file}" "${tmp}" 2>/dev/null || true
  mv -f "${tmp}" "${file}"
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
  local elapsed="${1:?}" prefix="${2:-启动中}"
  log "${prefix} ${elapsed}s：healthy=${PROGRESS_HEALTHY}/${PROGRESS_TOTAL}，loading=${PROGRESS_LOADING}，pending=${PROGRESS_PENDING}，failed=${PROGRESS_FAILED}；Worker=[${PROGRESS_STATES}]；VRAM=[$(gpu_memory_snapshot)]"
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

render_router_config() {
  local worker_ids="${1:-}" id port tmp
  [[ -n "${worker_ids}" ]] || die "没有健康 Worker，拒绝生成空路由。"
  tmp=$(mktemp "${ROUTER_CONFIG}.XXXXXX")
  {
    printf 'model_list:\n'
    IFS=',' read -r -a id_list <<<"${worker_ids}"
    for id in "${id_list[@]}"; do
      port=$(worker_port "${id}")
      cat <<EOF
  - model_name: "${SERVED_MODEL_NAME}"
    litellm_params:
      model: "hosted_vllm/${SERVED_MODEL_NAME}"
      api_base: "http://127.0.0.1:${port}/v1"
      api_key: "os.environ/BACKEND_API_KEY"
      max_parallel_requests: ${MAX_NUM_SEQS}
      timeout: 7200
    model_info:
      id: "llm-instance-${id}"
      mode: "chat"
      max_input_tokens: ${MAX_MODEL_LEN}
      max_output_tokens: ${MAX_MODEL_LEN}
EOF
    done
    cat <<EOF
router_settings:
  routing_strategy: "${ROUTING_STRATEGY}"
  num_retries: 1
  timeout: 7200
  allowed_fails: 1
  cooldown_time: 30
  retry_after: 1
litellm_settings:
  drop_params: false
  turn_off_message_logging: true
  request_timeout: 7200
general_settings:
  master_key: "os.environ/LITELLM_MASTER_KEY"
  database_url: "os.environ/DATABASE_URL"
  store_model_in_db: false
  disable_spend_logs: false
EOF
  } >"${tmp}"
  chmod 640 "${tmp}"
  chown root:root "${tmp}"
  mv -f "${tmp}" "${ROUTER_CONFIG}"
  log "LiteLLM 路由已生成，后端：${worker_ids}；策略：${ROUTING_STRATEGY}。"
}

refresh_router() {
  local ids
  ids=$(healthy_worker_ids)
  if [[ -z "${ids}" ]]; then
    systemctl stop llm-router.service 2>/dev/null || true
    warn "没有健康 Worker，LiteLLM 已停止。"
    return 0
  fi
  render_router_config "${ids}"
  systemctl restart llm-router.service
  wait_router
}

router_health() {
  local base_url
  base_url=$(router_local_base_url)
  curl --noproxy '*' -fsS --max-time 3 \
    "${base_url}/health/liveliness" >/dev/null 2>&1 || \
  curl --noproxy '*' -fsS --max-time 3 \
    -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
    "${base_url}/v1/models" >/dev/null 2>&1
}

router_local_base_url() {
  case "${API_BIND}" in
    0.0.0.0) printf 'http://127.0.0.1:%s\n' "${API_PORT}" ;;
    ::) printf 'http://[::1]:%s\n' "${API_PORT}" ;;
    *:*) printf 'http://[%s]:%s\n' "${API_BIND}" "${API_PORT}" ;;
    *) printf 'http://%s:%s\n' "${API_BIND}" "${API_PORT}" ;;
  esac
}

database_health() {
  docker exec llm-database pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1
}

wait_router() {
  local started now timeout=90
  started=$(date +%s)
  while true; do
    router_health && { log "LiteLLM 已就绪。"; return 0; }
    if ! systemctl is-active --quiet llm-router.service; then
      journalctl -u llm-router.service -n 80 --no-pager >&2 || true
      return 1
    fi
    now=$(date +%s)
    (( now - started < timeout )) || die "LiteLLM 启动超时"
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
  printf 'LiteLLM: %s (%s)\n' "${router_state:-unknown}" "$([[ -n "${router_state}" ]] && router_health && printf healthy || printf unhealthy)"
  database_state=$(systemctl is-active llm-database.service 2>/dev/null || true)
  printf 'PostgreSQL: %s (%s，仅监听 127.0.0.1:%s)\n' "${database_state:-unknown}" "$([[ -n "${database_state}" ]] && database_health && printf healthy || printf unhealthy)" "${LITELLM_DB_PORT}"
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

cmd_health() {
  load_config
  local id failures=0 running_count=0
  if database_health; then log "PostgreSQL: healthy"; else warn "PostgreSQL: unhealthy"; failures=$((failures + 1)); fi
  if router_health; then log "LiteLLM: healthy"; else warn "LiteLLM: unhealthy"; failures=$((failures + 1)); fi
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
  case "${action}" in
    status)
      cluster_state=$(systemctl show llm-cluster.service -p ActiveState --value 2>/dev/null || printf unknown)
      [[ "${cluster_state}" == activating ]] && pending_mode=1 || pending_mode=0
      collect_worker_progress "${ACTIVE_WORKERS}" "${pending_mode}"
      log_worker_progress 0 "启动快照（cluster=${cluster_state}）"
      printf 'database=%s router=%s\n' \
        "$(database_health && printf healthy || printf unavailable)" \
        "$(router_health && printf healthy || printf unavailable)"
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
        log_worker_progress "${elapsed}" "集群启动中（cluster=${cluster_state}）"
        if (( PROGRESS_HEALTHY == PROGRESS_TOTAL )) && database_health && router_health; then
          log "集群已就绪：PostgreSQL、${PROGRESS_TOTAL} 个 Worker 和 LiteLLM 全部健康。"
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
    stop)    systemctl stop llm-router.service ;;
    status)  systemctl status llm-router.service --no-pager || true ;;
    *) die "router 子命令必须是 start|stop|restart|status" ;;
  esac
}

cmd_database() {
  require_root; load_config
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
      warn "数据库停止时 LiteLLM Web UI、虚拟密钥和入口路由均不可用。"
      ;;
    status) systemctl status llm-database.service --no-pager || true ;;
    *) die "database 子命令必须是 start|stop|restart|status" ;;
  esac
}

cmd_logs() {
  load_config
  local target="${1:-}" id follow=""
  case "${target}" in
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
      [[ "${2:-}" == "-f" ]] && follow="-f"
      journalctl -u llm-database.service ${follow} -n 200 --no-pager
      ;;
    *) die "用法：llmctl logs worker <ID> [-f] | logs router [-f] | logs database [-f]" ;;
  esac
}

api_post() {
  local url="${1:?}" key="${2:?}" payload="${3:?}"
  curl --noproxy '*' -fsS --max-time 600 \
    -H "Authorization: Bearer ${key}" \
    -H 'Content-Type: application/json' \
    --data-binary "@${payload}" "${url}"
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
    {model:$model, max_tokens:512, temperature:0,
     reasoning_effort:"none",
     messages:[{role:"user",content:[
       {type:"image_url",image_url:{url:("data:"+$mime+";base64,"+$b64)}},
       {type:"text",text:$prompt}
     ]}]}' >"${out_json}"
  rm -f "${b64_file}"
}

smoke_endpoint() {
  local base_url="${1:?}" key="${2:?}" full="${3:-0}" tmp response content reasoning tool tmp_dir ocr_json six_images_json
  tmp=$(mktemp)
  trap 'rm -f "${tmp:-}" "${ocr_json:-}" "${six_images_json:-}"; [[ -z "${tmp_dir:-}" ]] || rm -rf "${tmp_dir}"' RETURN

  log "开始文本冒烟测试..."
  jq -n --arg model "${SERVED_MODEL_NAME}" --argjson toggle "${SUPPORTS_THINKING_TOGGLE}" '
    {model:$model,max_tokens:64,temperature:0,messages:[{role:"user",content:"只输出 LLM_OK，不要输出其他内容。"}]} +
    (if $toggle == 1 then {reasoning_effort:"none",chat_template_kwargs:{enable_thinking:false}} else {} end)' >"${tmp}"
  response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${tmp}") || die "文本冒烟测试请求失败"
  content=$(jq -r '.choices[0].message.content // ""' <<<"${response}")
  [[ "${content}" == *LLM_OK* ]] || die "文本语义测试失败，模型可能输出异常：${content:0:200}"
  reasoning=$(jq -r '.choices[0].message.reasoning_content // .choices[0].message.reasoning // ""' <<<"${response}")
  (( SUPPORTS_THINKING_TOGGLE == 0 )) || [[ -z "${reasoning}" ]] || die "请求级思考开关未关闭 reasoning 输出"
  log "文本生成$([[ ${SUPPORTS_THINKING_TOGGLE} -eq 1 ]] && printf '与请求级思考关闭' || true)：PASS"

  if (( SUPPORTS_REASONING == 1 )); then
    log "开始思考解析冒烟测试..."
    jq -n --arg model "${SERVED_MODEL_NAME}" '{model:$model,max_tokens:256,temperature:0,messages:[{role:"user",content:"请认真计算 17×19，并给出答案。"}]}' >"${tmp}"
    response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${tmp}") || die "思考测试请求失败"
    content=$(jq -r '.choices[0].message.content // ""' <<<"${response}")
    reasoning=$(jq -r '.choices[0].message.reasoning_content // .choices[0].message.reasoning // ""' <<<"${response}")
    [[ "${content}" == *323* ]] || die "思考测试答案异常：${content:0:200}"
    [[ -n "${reasoning}" ]] || die "未检测到独立 reasoning_content/reasoning 字段"
    log "默认思考并独立解析：PASS"
  fi

  if (( SUPPORTS_TOOL_CALLING == 1 )); then
    log "开始 OpenAI 工具调用冒烟测试..."
    jq -n --arg model "${SERVED_MODEL_NAME}" '{model:$model,max_tokens:256,temperature:0,messages:[{role:"user",content:"必须调用 get_weather 查询 Paris。"}],tools:[{type:"function",function:{name:"get_weather",description:"查询城市天气",parameters:{type:"object",properties:{city:{type:"string"}},required:["city"]}}}],tool_choice:"required"}' >"${tmp}"
    response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${tmp}") || die "工具调用测试请求失败"
    tool=$(jq -r '.choices[0].message.tool_calls[0].function.name // ""' <<<"${response}")
    [[ "${tool}" == get_weather ]] || die "工具调用解析失败：$(jq -c '.choices[0].message' <<<"${response}" | head -c 300)"
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
      [[ "${content}" == *7319* ]] || die "OCR 语义测试失败：${content:0:200}"
      log "视觉/OCR：PASS"
    else
      [[ -n "${content}" ]] || die "图片输入测试返回空内容"
      log "图片输入：PASS（模型未标记为 OCR 优化，不强制识别准确率）"
    fi
    local data_url
    data_url=$(jq -r '.messages[0].content[0].image_url.url' "${ocr_json}")
    six_images_json=$(mktemp)
    jq -n --arg model "${SERVED_MODEL_NAME}" --arg url "${data_url}" --argjson toggle "${SUPPORTS_THINKING_TOGGLE}" '
      {model:$model,max_tokens:64,temperature:0,
       messages:[{role:"user",content:
         ([range(0;6)|{type:"image_url",image_url:{url:$url}}] +
          [{type:"text",text:"这些图片中的编号相同。请简短回答。"}])}]} +
       (if $toggle == 1 then {reasoning_effort:"none",chat_template_kwargs:{enable_thinking:false}} else {} end)' >"${six_images_json}"
    response=$(api_post "${base_url}/v1/chat/completions" "${key}" "${six_images_json}") || die "单请求 6 图测试失败"
    content=$(jq -r '.choices[0].message.content // ""' <<<"${response}")
    [[ -n "${content}" ]] || die "单请求 6 图返回空内容"
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
    router_health || die "LiteLLM 未就绪"
    smoke_endpoint "$(router_local_base_url)" "${LITELLM_MASTER_KEY}" "${full}"
  fi
}

cmd_ocr() {
  load_config
  (( SUPPORTS_IMAGE_INPUT == 1 )) || die "当前模型 ${MODEL_ID} 不支持图片输入"
  local image_file="${1:?请提供图片文件}" prompt="${2:-请逐字识别图片中的全部文字，只输出识别结果。}" tmp response
  [[ -r "${image_file}" ]] || die "无法读取图片：${image_file}"
  router_health || die "LiteLLM 未就绪"
  tmp=$(mktemp)
  trap 'rm -f "${tmp}"' RETURN
  ocr_request_file "${image_file}" "${prompt}" "${tmp}"
  response=$(api_post "$(router_local_base_url)/v1/chat/completions" "${LITELLM_MASTER_KEY}" "${tmp}")
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
  router_health || die "LiteLLM 未就绪"
  warn "即将发起 ${requests} 个请求、并发 ${concurrency}；这是实际压力测试。"
  docker run --rm --network host \
    -e "BENCH_URL=$(router_local_base_url)/v1/chat/completions" \
    -e "BENCH_KEY=${LITELLM_MASTER_KEY}" \
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
        "reasoning_effort": "none",
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

cmd_tune() {
  require_root; load_config
  case "${1:-show}" in
    show)
      printf 'max-model-len=%s\n' "${MAX_MODEL_LEN}"
      printf 'gpu-memory-utilization=%s\n' "${GPU_MEMORY_UTILIZATION}"
      printf 'max-num-seqs=%s\n' "${MAX_NUM_SEQS}"
      printf 'max-num-batched-tokens=%s\n' "${MAX_NUM_BATCHED_TOKENS}"
      printf 'routing-strategy=%s\n' "${ROUTING_STRATEGY}"
      printf 'startup-parallelism=%s\n' "${STARTUP_PARALLELISM}"
      printf 'api-bind=%s\napi-port=%s\n' "${API_BIND}" "${API_PORT}"
      printf 'mm-limit=%s\n' "${MM_LIMIT}"
      ;;
    set)
      local key="${2:?缺少键}" value="${3:?缺少值}" env_key restart_workers=1 apply_router=0
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
          case "${value}" in least-busy|simple-shuffle|latency-based-routing|usage-based-routing-v2) ;; *) die "不支持的路由策略" ;; esac
          env_key=ROUTING_STRATEGY; restart_workers=0; apply_router=1 ;;
        api-bind)
          [[ "${value}" =~ ^[0-9a-fA-F:.]+$ ]] || die "无效监听地址"
          env_key=API_BIND; restart_workers=0; apply_router=1 ;;
        api-port)
          [[ "${value}" =~ ^[0-9]+$ ]] && (( value >= 1024 && value <= 65535 )) || die "端口范围 1024-65535"
          env_key=API_PORT; restart_workers=0; apply_router=1 ;;
        startup-parallelism)
          [[ "${value}" =~ ^[0-9]+$ ]] && (( value >= 1 && value <= INSTANCE_COUNT )) || die "范围 1-${INSTANCE_COUNT}"
          env_key=STARTUP_PARALLELISM; restart_workers=0 ;;
        *) die "不可修改的键：${key}" ;;
      esac
      set_env_value "${CLUSTER_ENV}" "${env_key}" "${value}"
      log "已写入 ${key}=${value}"
      if (( restart_workers )); then
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
      local api_host="${API_BIND}"
      [[ "${api_host}" == 0.0.0.0 || "${api_host}" == :: ]] && api_host='<服务器IP>'
      printf 'OPENAI_BASE_URL=http://%s:%s/v1\n' "${api_host}" "${API_PORT}"
      printf 'OPENAI_MODEL=%s\n' "${SERVED_MODEL_NAME}"
      printf 'OPENAI_API_KEY=%s\n' "${LITELLM_MASTER_KEY}"
      ;;
    rotate)
      local new_key="${2:-sk-$(openssl rand -hex 32)}"
      [[ "${new_key}" =~ ^[A-Za-z0-9._-]{16,}$ ]] || die "KEY 至少 16 位，只允许字母、数字、点、下划线和连字符"
      set_env_value "${SECRETS_ENV}" LITELLM_MASTER_KEY "${new_key}"
      systemctl restart llm-router.service
      log "入口 API key 已轮换。用 llmctl key show 查看。"
      ;;
    *) die "key 子命令必须是 show|rotate" ;;
  esac
}

cmd_admin() {
  require_root; load_config
  case "${1:-show}" in
    show)
      local ui_host="${API_BIND}"
      [[ "${ui_host}" == 0.0.0.0 || "${ui_host}" == :: ]] && ui_host='<服务器IP>'
      printf 'LITELLM_UI_URL=http://%s:%s/ui\n' "${ui_host}" "${API_PORT}"
      printf 'LITELLM_UI_USERNAME=%s\n' "${UI_USERNAME}"
      printf 'LITELLM_UI_PASSWORD=%s\n' "${UI_PASSWORD}"
      warn "这是管理员凭据；请勿复制到日志、工单或代码仓库。"
      ;;
    set-username)
      local new_username="${2:?请指定新用户名}"
      [[ "${new_username}" =~ ^[A-Za-z0-9._@-]{1,64}$ ]] || die "用户名只允许字母、数字、点、下划线、@ 和连字符"
      set_env_value "${SECRETS_ENV}" UI_USERNAME "${new_username}"
      systemctl restart llm-router.service
      log "Web UI 管理员用户名已修改为 ${new_username}。"
      ;;
    set-password)
      local new_password="${2:-}" confirm_password=""
      if [[ -z "${new_password}" ]]; then
        [[ -t 0 ]] || die "非交互模式请将新密码作为参数传入"
        read -r -s -p '输入新的 Web UI 管理员密码: ' new_password
        printf '\n' >&2
        read -r -s -p '再次输入新密码: ' confirm_password
        printf '\n' >&2
        [[ "${new_password}" == "${confirm_password}" ]] || die "两次输入的密码不一致"
      fi
      [[ "${new_password}" =~ ^[A-Za-z0-9._@-]{8,128}$ ]] || die "密码需 8-128 位，且只允许字母、数字、点、下划线、@ 和连字符"
      set_env_value "${SECRETS_ENV}" UI_PASSWORD "${new_password}"
      systemctl restart llm-router.service
      log "Web UI 管理员密码已修改。"
      ;;
    *) die "admin 子命令必须是 show|set-username|set-password" ;;
  esac
}

proxy_url_from_args() {
  local ip="${1:?缺少代理 IP}" port="${2:?缺少代理端口}" scheme="${3:-http}"
  [[ "${ip}" =~ ^[A-Za-z0-9._:-]+$ ]] || die "代理 IP/主机名格式无效"
  [[ "${port}" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) || die "代理端口无效"
  [[ "${scheme}" == http || "${scheme}" == https ]] || die "代理协议只能是 http 或 https"
  printf '%s://%s:%s\n' "${scheme}" "${ip}" "${port}"
}

load_saved_proxy() {
  if [[ -r "${PROXY_ENV}" ]]; then
    # shellcheck disable=SC1090
    source "${PROXY_ENV}"
  fi
  MAINTENANCE_PROXY="${MAINTENANCE_PROXY:-}"
  MAINTENANCE_NO_PROXY="${MAINTENANCE_NO_PROXY:-127.0.0.1,localhost,::1}"
}

prompt_proxy_if_needed() {
  load_saved_proxy
  if curl --noproxy '*' -sS --connect-timeout 5 --max-time 8 -o /dev/null https://huggingface.co 2>/dev/null; then
    # A saved proxy is only a fallback. Prefer direct access when available.
    MAINTENANCE_PROXY=""
    return 0
  fi
  [[ -n "${MAINTENANCE_PROXY}" ]] && return 0
  [[ -t 0 ]] || die "需要国际出口；请先执行 llmctl proxy set <IP> <端口>"
  local ip port scheme
  printf '当前操作需要临时国际出口。\n' >&2
  read -r -p '代理 IP/主机名: ' ip
  read -r -p '代理端口: ' port
  read -r -p '协议 [http]: ' scheme
  scheme="${scheme:-http}"
  MAINTENANCE_PROXY=$(proxy_url_from_args "${ip}" "${port}" "${scheme}")
  MAINTENANCE_NO_PROXY="127.0.0.1,localhost,::1"
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
  local -a command=("$@")
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
      [[ -x /opt/llm-cluster/hub-venv/bin/ms-hub ]] || die "缺少 ModelScope 下载器；请重新运行安装脚本修复"
      /opt/llm-cluster/hub-venv/bin/ms-hub download "${model_id}" --revision "${revision}" \
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

cmd_update() {
  require_root; load_config
  local new_vllm="${VLLM_IMAGE}" new_litellm="${LITELLM_IMAGE}" new_postgres="${POSTGRES_IMAGE}" was_cluster_active=0 stopped_for_proxy=0
  while (($#)); do
    case "$1" in
      --vllm-image) new_vllm="${2:?缺少镜像}"; shift 2 ;;
      --litellm-image) new_litellm="${2:?缺少镜像}"; shift 2 ;;
      --postgres-image) new_postgres="${2:?缺少镜像}"; shift 2 ;;
      *) die "未知 update 参数：$1" ;;
    esac
  done
  [[ "${new_vllm}" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die "vLLM 镜像名格式无效"
  [[ "${new_litellm}" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die "LiteLLM 镜像名格式无效"
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
  if ! docker pull "${new_vllm}" || ! docker pull "${new_litellm}" || ! docker pull "${new_postgres}" || \
     ! image_supports_architecture "${new_vllm}" "${MODEL_ARCHITECTURE}"; then
    clear_temporary_proxy
    trap - EXIT
    (( was_cluster_active == 0 )) || systemctl start llm-cluster.service 2>/dev/null || true
    die "镜像拉取或 vLLM 架构核验失败；配置未修改"
  fi
  clear_temporary_proxy
  trap - EXIT
  set_env_value "${CLUSTER_ENV}" VLLM_IMAGE "${new_vllm}"
  set_env_value "${CLUSTER_ENV}" LITELLM_IMAGE "${new_litellm}"
  set_env_value "${CLUSTER_ENV}" POSTGRES_IMAGE "${new_postgres}"
  if (( stopped_for_proxy )); then
    if ! systemctl start llm-cluster.service || ! cmd_smoke --full; then
      warn "新镜像验收失败，回滚到原镜像。"
      systemctl stop llm-cluster.service 2>/dev/null || true
      set_env_value "${CLUSTER_ENV}" VLLM_IMAGE "${VLLM_IMAGE}"
      set_env_value "${CLUSTER_ENV}" LITELLM_IMAGE "${LITELLM_IMAGE}"
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
  IMPORT_VLLM_IMAGE="" IMPORT_LITELLM_IMAGE="" IMPORT_POSTGRES_IMAGE="" IMPORT_MODEL_HUB="" IMPORT_MODEL_ID="" IMPORT_MODEL_REVISION="" IMPORT_MODEL_ARCHITECTURE="" IMPORT_MODEL_DIR_NAME=""
  while IFS='=' read -r key value; do
    [[ -n "${key}" && "${key}" != \#* ]] || continue
    case "${key}" in
      VLLM_IMAGE) IMPORT_VLLM_IMAGE="${value}" ;;
      LITELLM_IMAGE) IMPORT_LITELLM_IMAGE="${value}" ;;
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
  [[ "${IMPORT_LITELLM_IMAGE}" =~ ^[A-Za-z0-9./:_@-]+$ ]] || die "离线 LiteLLM 镜像名无效"
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
      log "导出 LiteLLM 镜像..."
      docker save -o "${dir}/litellm-image.tar" "${LITELLM_IMAGE}"
      log "导出 PostgreSQL 镜像..."
      docker save -o "${dir}/postgres-image.tar" "${POSTGRES_IMAGE}"
      log "导出当前模型（未压缩以加快恢复）..."
      tar -C "${MODEL_ROOT}" -cf "${dir}/model.tar" "${current_name}" current.manifest
      cat >"${dir}/bundle.env" <<EOF
VLLM_IMAGE=${VLLM_IMAGE}
LITELLM_IMAGE=${LITELLM_IMAGE}
POSTGRES_IMAGE=${POSTGRES_IMAGE}
MODEL_HUB=${MODEL_HUB}
MODEL_ID=${MODEL_ID}
MODEL_REVISION=${MODEL_REVISION}
MODEL_ARCHITECTURE=${MODEL_ARCHITECTURE}
MODEL_DIR_NAME=${current_name}
EXPORTED_AT=$(date -u +%FT%TZ)
EOF
      (cd "${dir}" && sha256sum vllm-image.tar litellm-image.tar postgres-image.tar model.tar >SHA256SUMS)
      log "离线包已导出到 ${dir}"
      ;;
    import)
      [[ -r "${dir}/bundle.env" && -r "${dir}/SHA256SUMS" ]] || die "离线包清单不完整"
      awk 'NF==2 && length($1)==64 && $1 !~ /[^0-9a-f]/ && ($2=="vllm-image.tar" || $2=="litellm-image.tar" || $2=="postgres-image.tar" || $2=="model.tar") && !seen[$2]++ {ok++} END{exit !(ok==4)}' "${dir}/SHA256SUMS" || die "SHA256SUMS 格式或文件名不安全"
      (cd "${dir}" && sha256sum -c SHA256SUMS) || die "离线包校验失败"
      safe_tar_listing "${dir}/model.tar"
      read_bundle_env "${dir}/bundle.env"
      [[ "${IMPORT_MODEL_HUB}:${IMPORT_MODEL_ID}@${IMPORT_MODEL_REVISION}:${IMPORT_MODEL_ARCHITECTURE}" == \
         "${MODEL_HUB}:${MODEL_ID}@${MODEL_REVISION}:${MODEL_ARCHITECTURE}" ]] || \
        die "离线包模型与当前硬件规划不一致；请用安装器选择该模型并生成匹配配置后再导入"
      tar -tf "${dir}/model.tar" | awk -v root="${IMPORT_MODEL_DIR_NAME}/" '$0 != "current.manifest" && index($0,root) != 1 {bad=1} END{exit bad}' || die "离线模型包路径与清单不一致"
      docker load -i "${dir}/vllm-image.tar"
      docker load -i "${dir}/litellm-image.tar"
      docker load -i "${dir}/postgres-image.tar"
      image_supports_architecture "${IMPORT_VLLM_IMAGE}" "${MODEL_ARCHITECTURE}" || die "离线 vLLM 镜像不支持 ${MODEL_ARCHITECTURE}"
      install -d -m 755 "${MODEL_ROOT}"
      tar -C "${MODEL_ROOT}" -xf "${dir}/model.tar"
      ln -sfn "${IMPORT_MODEL_DIR_NAME}" "${MODEL_ROOT}/current"
      set_env_value "${CLUSTER_ENV}" VLLM_IMAGE "${IMPORT_VLLM_IMAGE}"
      set_env_value "${CLUSTER_ENV}" LITELLM_IMAGE "${IMPORT_LITELLM_IMAGE}"
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
  systemctl disable llm-cluster.service 2>/dev/null || true
  log "卸载 2/4：并发停止 Router、数据库和 ${INSTANCE_COUNT} 个 Worker。"
  stop_managed_services_with_progress 180 || \
    die "LLM 服务未能在限定时间内安全停止；配置尚未删除，请根据上方单位/容器状态检查"
  log "卸载 3/4：删除 systemd 单元和可再生成数据；配置保留到最后一步。"
  rm -f /etc/systemd/system/llm-cluster.service /etc/systemd/system/llm-router.service /etc/systemd/system/llm-database.service /etc/systemd/system/llm-worker@.service
  systemctl daemon-reload
  systemctl reset-failed >/dev/null 2>&1 || true
  clear_temporary_proxy
  if (( ! purge_database )); then
    install -d -m 700 "${STATE_DIR}"
    install -m 600 "${SECRETS_ENV}" "${RETAINED_SECRETS}"
    chown root:root "${RETAINED_SECRETS}"
  fi
  [[ "${CACHE_DIR}" == /var/lib/llm-cluster/cache ]] || die "缓存路径安全检查失败"
  remove_tree_with_progress "${CACHE_DIR}" "可再生成编译缓存" 5
  if (( purge_images )); then
    log "删除锁定的 LLM 容器镜像。"
    docker image rm "${VLLM_IMAGE}" "${LITELLM_IMAGE}" "${POSTGRES_IMAGE}" 2>/dev/null || true
  fi
  if (( purge_database )); then
    if docker volume inspect llm-cluster-litellm-postgres >/dev/null 2>&1; then
      docker volume rm llm-cluster-litellm-postgres >/dev/null
      log "LiteLLM PostgreSQL 数据卷 llm-cluster-litellm-postgres 已永久删除。"
    else
      log "未发现 LiteLLM PostgreSQL 数据卷，无需删除。"
    fi
    rm -f "${RETAINED_SECRETS}"
  else
    log "LiteLLM PostgreSQL 数据卷和 root-only 恢复凭据 ${RETAINED_SECRETS} 已保留；重装时会自动接管。"
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
  local id out="llm-router,llm-database"
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
  local -a units=(llm-cluster.service llm-router.service llm-database.service)
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
  local -a units=(llm-cluster.service llm-router.service llm-database.service)
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
  local -a units=(llm-cluster.service llm-router.service llm-database.service)
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
  log "启动 PostgreSQL 管理数据库..."
  systemctl start llm-database.service
  local db_started db_now
  db_started=$(date +%s)
  until database_health; do
    systemctl is-active --quiet llm-database.service || die "PostgreSQL 启动失败；请查看 llmctl logs database"
    db_now=$(date +%s)
    (( db_now - db_started < 120 )) || die "PostgreSQL 启动超时"
    sleep 2
  done
  log "数据库已就绪；按每批 ${STARTUP_PARALLELISM} 个启动 Worker：${ACTIVE_WORKERS}。"
  start_worker_ids_batched "${ACTIVE_WORKERS}" || startup_failed=1
  healthy=$(healthy_worker_ids)
  [[ -n "${healthy}" ]] || die "没有 Worker 成功启动"
  render_router_config "${healthy}"
  log "启动 LiteLLM 路由器..."
  systemctl start llm-router.service
  wait_router
  (( startup_failed == 0 )) || warn "至少一个 Worker 启动失败；健康 Worker 已继续提供服务。"
}

cmd_boot_stop() {
  require_root; load_config
  # Every child unit declares PartOf=llm-cluster.service. systemd already adds
  # their stop jobs to the same transaction and runs them concurrently. Calling
  # `systemctl stop` recursively from this ExecStop can wait on its own parent
  # transaction and was the cause of the legacy uninstall appearing hung.
  log "systemd 已并发停止 Router、数据库和 ${INSTANCE_COUNT} 个 Worker。"
}

main() {
  local command="${1:-help}"
  (($# == 0)) || shift
  case "${command}" in
    help|-h|--help) usage ;;
    version|--version) printf 'llmctl %s\n' "${CTL_VERSION}" ;;
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
    router) cmd_router "$@" ;;
    database) cmd_database "$@" ;;
    logs) cmd_logs "$@" ;;
    smoke) cmd_smoke "$@" ;;
    ocr) cmd_ocr "$@" ;;
    bench) cmd_bench "$@" ;;
    tune) cmd_tune "$@" ;;
    key) cmd_key "$@" ;;
    admin) cmd_admin "$@" ;;
    proxy) cmd_proxy "$@" ;;
    models) cmd_models "$@" ;;
    timezone) cmd_timezone "$@" ;;
    download) cmd_download "$@" ;;
    update) cmd_update "$@" ;;
    offline) cmd_offline "$@" ;;
    uninstall) cmd_uninstall "$@" ;;
    _worker-start) cmd_worker_start "$@" ;;
    _boot-start) cmd_boot_start "$@" ;;
    _boot-stop) cmd_boot_stop "$@" ;;
    *) die "未知命令：${command}。运行 llmctl help 查看帮助。" ;;
  esac
}

if [[ "${LLMCTL_SOURCE_ONLY:-0}" != 1 ]]; then
  main "$@"
fi

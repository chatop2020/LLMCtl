# 工作流、Responses API 与模型部署控制命令。
# 本文件由 llmctl 主入口加载；共享配置和基础函数仍由主入口唯一持有。

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
  # 旧升级器尚未复制 Go 运行时到控制面目录时，在创建 workflow.json 前失败。
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

model_control_require_runtime() {
  [[ -x "${MODEL_CONTROL_RUNTIME}" ]] || \
    die "多模型部署控制器不可执行：${MODEL_CONTROL_RUNTIME}。请运行 llmctl upgrade --force 补齐控制面文件。"
}

wait_model_control_ready() {
  local elapsed=0
  while (( elapsed < 30 )); do
    if systemctl is-active --quiet llm-model-control.service && [[ -S "${MODEL_CONTROL_SOCKET}" ]]; then
      if "${MODEL_CONTROL_RUNTIME}" --socket "${MODEL_CONTROL_SOCKET}" snapshot >/dev/null 2>&1; then
        return 0
      fi
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  journalctl -u llm-model-control.service -n 80 --no-pager >&2 || true
  return 1
}

model_control_request() {
  local operation="${1:?}" input="${2:?}" temporary=""
  model_control_require_runtime
  [[ -S "${MODEL_CONTROL_SOCKET}" ]] || \
    die "模型部署控制服务未就绪；请先运行 llmctl model init"
  if [[ "${input}" == - ]]; then
    temporary=$(mktemp "${STATE_DIR}/model-request.XXXXXX.json")
    chmod 0600 "${temporary}"
    dd status=none bs=1M count=2 of="${temporary}"
    input="${temporary}"
  fi
  [[ -r "${input}" ]] || {
    [[ -z "${temporary}" ]] || rm -f "${temporary}"
    die "无法读取部署请求：${input}"
  }
  set +e
  "${MODEL_CONTROL_RUNTIME}" --socket "${MODEL_CONTROL_SOCKET}" request "${operation}" "${input}"
  local status=$?
  set -e
  [[ -z "${temporary}" ]] || rm -f "${temporary}"
  return "${status}"
}

# 从命令行构造与管理页面相同的 Ornith 升级契约。目标 revision 在计划阶段由
# 控制服务解析成固定 SHA，apply 必须携带计划返回的注册表版本以拒绝陈旧确认。
cmd_model_upgrade() {
  local operation="${1:-plan}" source_id="" target_hub="" target_model="" target_revision=""
  local max_model_len=32768 assume_yes=0 payload plan_file submit_file answer=""
  shift || true
  if [[ "${operation}" == rollback ]]; then
    (($# == 1)) || die "用法：llmctl model upgrade rollback <升级任务ID>"
    cmd_model rollback "$1"
    return
  fi
  [[ "${operation}" == plan || "${operation}" == apply ]] || \
    die "model upgrade 子命令必须是 plan|apply|rollback"
  (($# >= 1)) || die "用法：llmctl model upgrade ${operation} <部署ID> [--hub modelscope|huggingface] [--model MODEL_ID] [--revision SHA] [--max-model-len N] [--yes]"
  source_id="$1"
  shift
  [[ "${source_id}" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "来源部署 ID 非法"
  while (($#)); do
    case "$1" in
      --hub) target_hub="${2:?缺少目标模型来源}"; shift 2 ;;
      --model) target_model="${2:?缺少目标模型 ID}"; shift 2 ;;
      --revision) target_revision="${2:?缺少目标 revision}"; shift 2 ;;
      --max-model-len) max_model_len="${2:?缺少目标上下文}"; shift 2 ;;
      --yes) assume_yes=1; shift ;;
      *) die "未知 model upgrade 参数：$1" ;;
    esac
  done
  [[ -z "${target_hub}" || "${target_hub}" == modelscope || "${target_hub}" == huggingface ]] || \
    die "目标模型来源只能是 modelscope 或 huggingface"
  [[ "${max_model_len}" =~ ^[0-9]+$ ]] && \
    ((max_model_len >= 8192 && max_model_len <= 262144)) || \
    die "目标最大上下文必须位于 8192-262144"
  payload=$(mktemp "${STATE_DIR}/model-upgrade.XXXXXX.json")
  plan_file=$(mktemp "${STATE_DIR}/model-upgrade-plan.XXXXXX.json")
  submit_file=$(mktemp "${STATE_DIR}/model-upgrade-submit.XXXXXX.json")
  chmod 0600 "${payload}" "${plan_file}" "${submit_file}"
  jq -n \
    --arg source_deployment_id "${source_id}" \
    --arg target_hub "${target_hub}" \
    --arg target_model_id "${target_model}" \
    --arg target_revision "${target_revision}" \
    --argjson max_model_len "${max_model_len}" \
    '{source_deployment_id:$source_deployment_id,target_hub:$target_hub,target_model_id:$target_model_id,
      target_revision:$target_revision,max_model_len:$max_model_len}' >"${payload}"
  model_control_request upgrade-plan "${payload}" >"${plan_file}" || {
    rm -f "${payload}" "${plan_file}" "${submit_file}"
    return 1
  }
  jq . "${plan_file}"
  if [[ "${operation}" == plan ]]; then
    rm -f "${payload}" "${plan_file}" "${submit_file}"
    return
  fi
  if ((assume_yes == 0)); then
    read -r -p "确认下载固定 revision、重载计划中的 Worker，并保留升级前回退点？[y/N] " answer
    [[ "${answer}" =~ ^[Yy]$ ]] || {
      rm -f "${payload}" "${plan_file}" "${submit_file}"
      log "已取消，未修改模型、Worker 或接入层。"
      return
    }
  fi
  jq \
    --arg target_revision "$(jq -r '.upgrade.target_revision' "${plan_file}")" \
    --argjson expected_registry_revision "$(jq -r '.source_registry_revision' "${plan_file}")" \
    '. + {target_revision:$target_revision,
          expected_registry_revision:$expected_registry_revision}' \
    "${payload}" >"${submit_file}"
  model_control_request upgrade-submit "${submit_file}" | jq .
  rm -f "${payload}" "${plan_file}" "${submit_file}"
}

cmd_model() {
  require_root; load_config
  local action="${1:-status}" identifier="" payload="" enabled_state="" active_state=""
  shift || true
  case "${action}" in
    init)
      (( $# == 0 )) || die "用法：llmctl model init"
      model_control_require_runtime
      [[ -r "${MODEL_CONTROL_UNIT_SOURCE}" ]] || \
        die "缺少模型部署 systemd 单元：${MODEL_CONTROL_UNIT_SOURCE}"
      getent group llm-account >/dev/null 2>&1 || groupadd --system llm-account
      install -d -o root -g llm-account -m 0750 "${STATE_DIR}/model-control"
      install -m 0644 "${MODEL_CONTROL_UNIT_SOURCE}" "${MODEL_CONTROL_SERVICE_UNIT}"
      systemctl daemon-reload
      systemctl enable llm-model-control.service
      systemctl restart llm-model-control.service
      wait_model_control_ready || die "模型部署控制服务未能在 30 秒内就绪"
      "${MODEL_CONTROL_RUNTIME}" --socket "${MODEL_CONTROL_SOCKET}" migrate | jq .
      log "多模型注册表已就绪；现有 Router 和 Worker 未重启。"
      ;;
    status)
      (( $# == 0 )) || die "用法：llmctl model status"
      enabled_state=$(systemctl is-enabled llm-model-control.service 2>/dev/null || true)
      active_state=$(systemctl is-active llm-model-control.service 2>/dev/null || true)
      [[ -n "${enabled_state}" && "${enabled_state}" != not-found ]] || enabled_state=not-installed
      [[ -n "${active_state}" && "${active_state}" != unknown ]] || active_state=inactive
      printf 'LLMCtl model-control: configured=%s enabled=%s active=%s socket=%s\n' \
        "$([[ -r "${CONFIG_DIR}/deployments.json" ]] && printf yes || printf legacy)" \
        "${enabled_state}" "${active_state}" \
        "$([[ -S "${MODEL_CONTROL_SOCKET}" ]] && printf ready || printf unavailable)"
      if [[ -S "${MODEL_CONTROL_SOCKET}" ]]; then
        model_control_require_runtime
        "${MODEL_CONTROL_RUNTIME}" --socket "${MODEL_CONTROL_SOCKET}" snapshot | jq .
      elif [[ "${enabled_state}" == not-installed ]]; then
        if [[ -x "${MODEL_CONTROL_RUNTIME}" && -r "${MODEL_CONTROL_UNIT_SOURCE}" ]]; then
          log "检测到旧版控制面升级后的首次初始化状态；不需要安装额外软件。"
          log "请运行 llmctl model init。该命令只注册模型控制服务并迁移注册表，不重启 Router 或 Worker。"
        else
          warn "模型控制运行时或 systemd 单元模板不完整。"
          log "请依次运行：llmctl upgrade --force；llmctl model init。"
        fi
      elif [[ "${active_state}" != active ]]; then
        warn "模型控制服务已经注册但没有运行。"
        log "请运行 llmctl model init；如仍失败，再运行 llmctl logs model。"
      else
        warn "模型控制服务正在运行，但 Unix Socket 尚不可用。"
        log "请运行 llmctl logs model 检查启动日志。"
      fi
      ;;
    plan|deploy)
      (( $# == 1 )) || die "用法：llmctl model ${action} <JSON文件|->"
      payload="$1"
      if [[ "${action}" == plan ]]; then
        model_control_request plan "${payload}" | jq .
      else
        model_control_request submit "${payload}" | jq .
      fi
      ;;
    upgrade)
      cmd_model_upgrade "$@"
      ;;
    job|cancel|rollback)
      (( $# == 1 )) || die "用法：llmctl model ${action} <任务ID>"
      identifier="$1"
      [[ "${identifier}" =~ ^[a-f0-9-]{16,64}$ ]] || die "任务 ID 格式非法"
      payload=$(mktemp "${STATE_DIR}/model-job.XXXXXX.json")
      chmod 0600 "${payload}"
      printf '{"id":"%s"}\n' "${identifier}" >"${payload}"
      if [[ "${action}" == job ]]; then
        model_control_request job "${payload}" | jq .
      elif [[ "${action}" == rollback ]]; then
        model_control_request rollback "${payload}" | jq .
      else
        model_control_request cancel "${payload}" | jq .
      fi
      rm -f "${payload}"
      ;;
    *) die "model 子命令必须是 init|status|plan|deploy|upgrade|job|cancel|rollback" ;;
  esac
}

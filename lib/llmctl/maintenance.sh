# 管理员凭据之后的代理、模型目录、升级和镜像维护命令。
# 本文件由 llmctl 主入口加载；共享配置和基础函数仍由主入口唯一持有。

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
    # 此文件由 root 所有，通过 `llmctl runtime-proxy set` 生成。
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
    # 已保存代理仅作后备；直连可用时优先直连。
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

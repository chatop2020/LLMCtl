# 离线包、卸载和开关机生命周期命令。
# 本文件由 llmctl 主入口加载；共享配置和基础函数仍由主入口唯一持有。

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
  # 此处只移除开机启动；停止服务交给下方有超时、有进度的并发生命周期处理，
  # 避免 systemd 等待探针或容器时让卸载过程长时间没有输出。
  systemctl disable llm-keepwarm.timer 2>/dev/null || true
  systemctl disable llm-cluster.service 2>/dev/null || true
  systemctl disable llm-workflow.service 2>/dev/null || true
  systemctl disable llm-model-control.service 2>/dev/null || true
  log "卸载 2/4：并发停止 Router、数据库和 ${INSTANCE_COUNT} 个 Worker。"
  stop_managed_services_with_progress 180 || \
    die "LLM 服务未能在限定时间内安全停止；配置尚未删除，请根据上方单位/容器状态检查"
  log "卸载 3/4：删除 systemd 单元和可再生成数据；配置保留到最后一步。"
  remove_nginx_config
  remove_tree_with_progress "${NGINX_STATE_DIR}" "可再生成的 Nginx 回滚缓存" 2
  rm -f /etc/systemd/system/llm-cluster.service /etc/systemd/system/llm-router.service /etc/systemd/system/llm-database.service /etc/systemd/system/llm-account.service /etc/systemd/system/llm-worker@.service /etc/systemd/system/llm-keepwarm.service /etc/systemd/system/llm-keepwarm.timer /etc/systemd/system/llm-workflow.service /etc/systemd/system/llm-model-control.service
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
  [[ "${MODEL_CONTROL_STATE_DIR}" == /var/lib/llm-cluster/model-control ]] || die "模型部署状态路径安全检查失败"
  remove_tree_with_progress "${MODEL_CONTROL_STATE_DIR}" "模型部署任务状态" 2
  [[ "${MODEL_CONTROL_BACKUP_DIR}" == /var/backups/llmctl/model-deployments ]] || die "模型部署回滚路径安全检查失败"
  remove_tree_with_progress "${MODEL_CONTROL_BACKUP_DIR}" "模型部署回滚快照" 2
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
  local -a units=(llm-cluster.service llm-router.service llm-keepwarm.service llm-keepwarm.timer llm-workflow.service llm-model-control.service)
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
  local -a units=(llm-cluster.service llm-router.service llm-keepwarm.service llm-workflow.service llm-model-control.service)
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
  local -a units=(llm-cluster.service llm-router.service llm-keepwarm.service llm-keepwarm.timer llm-workflow.service llm-model-control.service)
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
  # 每个子单元都声明 PartOf=llm-cluster.service，systemd 会把停止任务加入同一
  # 事务并并发执行。从此 ExecStop 递归调用 `systemctl stop` 可能等待自己的父事务，
  # 这正是旧版卸载看似卡死的原因。
  log "systemd 已并发停止 Router、可选数据库/账户门户和 ${INSTANCE_COUNT} 个 Worker。"
}

# 运行时调优、报告恢复与参数修改命令。
# 本文件由 llmctl 主入口加载；共享配置和基础函数仍由主入口唯一持有。

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
        # 重新加载数值并立即应用仅影响 Router 的变更。
        load_config
        refresh_router
      else
        log "启动并行度将在下次 start、restart 或开机启动时生效，无需重启当前 Worker。"
      fi
      ;;
    *) die "tune 子命令必须是 show|set" ;;
  esac
}

#!/usr/bin/env bash
# 把多模型部署注册表投影为 CLI 诊断命令需要的当前运行配置。

# 让面向统一入口的命令使用首个已启用且请求发布的部署及其 Worker 能力。
# 成功时覆盖当前 Shell 的模型、制品、拓扑和能力变量；没有有效注册表时
# 保留旧版全局配置，保证控制面升级不会改变旧安装的行为。
activate_default_published_deployment() {
  local registry="${CONFIG_DIR}/deployments.json" selected="" public_model="" worker_id=""
  local hub="" revision="" artifact_path="" instance_count="" active_workers="" deployment_id="" registry_model_id=""
  local worker_env="" architecture="" model_identity=""
  [[ -r "${registry}" ]] || return 0
  selected=$(jq -r '
    .artifacts as $artifacts
    | [.deployments | to_entries[]
       | select(.value.enabled != false and .value.publish_requested != false)
       | .key as $id | .value as $d | $artifacts[$d.artifact_id] as $a
       | {deployment:$id,
          model:(($d.public_model_ids // [])[0] // $d.served_model_name // ""),
          worker:([$d.instances[]? | select(.kind == "local" and .enabled != false) | .worker_id][0] // ""),
          workers:([$d.instances[]? | select(.kind == "local" and .enabled != false) | .worker_id] | sort | join(",")),
          count:([$d.instances[]? | select(.kind == "local" and .enabled != false)] | length),
          hub:($a.hub // "unknown"), revision:($a.revision // "unknown"), path:($a.path // ""),
          model_id:($d.model_id // "")}
       | select(.model != "")][0] // {}
    | [(.model // ""), ((.worker // "") | tostring), (.hub // ""),
       (.revision // ""), (.path // ""), ((.count // "") | tostring),
       (.workers // ""), (.deployment // ""), (.model_id // "")] | join("\u001f")
  ' "${registry}" 2>/dev/null || true)
  IFS=$'\x1f' read -r public_model worker_id hub revision artifact_path \
    instance_count active_workers deployment_id registry_model_id <<<"${selected}"
  [[ "${public_model}" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$ ]] || return 0
  worker_env="${CONFIG_DIR}/workers/${worker_id}.env"
  if [[ "${worker_id}" =~ ^[0-9]+$ && -r "${worker_env}" ]]; then
    # shellcheck disable=SC1090
    source "${worker_env}"
  fi
  SERVED_MODEL_NAME="${public_model}"
  MODEL_ID="${registry_model_id:-${MODEL_ID:-unknown}}"
  MODEL_HUB="${hub:-${MODEL_HUB:-unknown}}"
  MODEL_REVISION="${revision:-${MODEL_REVISION:-unknown}}"
  MODEL_LOCAL_DIR="${artifact_path:-${MODEL_LOCAL_DIR:-${MODEL_ROOT:-/data/llm-cluster/models}/current}}"
  INSTANCE_COUNT="${instance_count:-${INSTANCE_COUNT:-0}}"
  ACTIVE_WORKERS="${active_workers:-${ACTIVE_WORKERS:-}}"
  ACTIVE_DEPLOYMENT_ID="${deployment_id:-legacy}"
  if [[ -r "${MODEL_LOCAL_DIR}/config.json" ]]; then
    architecture=$(jq -r '.architectures[0] // empty' "${MODEL_LOCAL_DIR}/config.json" 2>/dev/null || true)
    MODEL_ARCHITECTURE="${architecture:-${MODEL_ARCHITECTURE:-unknown}}"
  fi
  model_identity=$(printf '%s' "${MODEL_ID}" | tr '[:lower:]' '[:upper:]')
  case "${model_identity}" in
    *NVFP4*) MODEL_PRECISION=nvfp4-mixed ;;
    *FP8*) MODEL_PRECISION=fp8 ;;
  esac
  if (( SUPPORTS_IMAGE_INPUT == 1 )); then MODEL_TASK=vision; else MODEL_TASK=text; fi
}

# systemctl 对 inactive/disabled 会输出有效状态但返回非零；该助手保留输出，
# 只在命令完全没有返回文本时使用 unknown，避免出现“inactive unknown”双值。
systemd_property_state() {
  local operation="${1:?缺少 systemctl 操作}" unit="${2:?缺少 systemd 单元}" value=""
  value=$(systemctl "${operation}" "${unit}" 2>/dev/null) || true
  printf '%s\n' "${value:-unknown}"
}

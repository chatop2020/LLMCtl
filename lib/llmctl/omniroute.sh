#!/usr/bin/env bash
# OmniRoute 镜像生命周期与 SQLite 运维命令。所有写操作由 root 控制服务执行；
# 本模块只负责参数校验、确认、任务提交和进度展示。

omniroute_require_control() {
  model_control_require_runtime
  [[ -S "${MODEL_CONTROL_SOCKET}" ]] || \
    die "OmniRoute 运维控制服务未就绪；请先运行 llmctl model init"
}

# 把一个 OmniRoute 白名单操作及其单个 JSON 对象发送给 root 控制服务。
# 第二个参数可省略，此时使用空对象；写入前会规范化并拒绝多段或畸形 JSON，
# 避免把 Shell 参数展开错误泄漏成 Python traceback。
omniroute_request() {
  local operation="${1:?}" payload="${2:-}" temporary status
  [[ -n "${payload}" ]] || payload='{}'
  omniroute_require_control
  temporary=$(mktemp "${STATE_DIR}/omniroute-request.XXXXXX.json")
  if ! printf '%s' "${payload}" | \
      jq -s -ce 'if length == 1 and (.[0] | type == "object") then .[0] else error("expected one object") end' \
      >"${temporary}"; then
    rm -f "${temporary}"
    die "内部 OmniRoute 请求不是单个有效 JSON 对象"
  fi
  chmod 0600 "${temporary}"
  set +e
  model_control_request "${operation}" "${temporary}"
  status=$?
  set -e
  rm -f "${temporary}"
  return "${status}"
}

omniroute_wait_job() {
  local job_id="${1:?}" payload result state phase progress message last=""
  payload=$(jq -cn --arg id "${job_id}" '{id:$id}')
  while true; do
    result=$(omniroute_request omniroute-job "${payload}") || return 1
    state=$(jq -r '.state // "unknown"' <<<"${result}")
    phase=$(jq -r '.phase // "unknown"' <<<"${result}")
    progress=$(jq -r '.progress // 0' <<<"${result}")
    message=$(jq -r '.message // ""' <<<"${result}")
    if [[ "${state}|${phase}|${progress}|${message}" != "${last}" ]]; then
      printf '[omniroute] %s%% %-18s %s\n' "${progress}" "${phase}" "${message}"
      last="${state}|${phase}|${progress}|${message}"
    fi
    case "${state}" in
      succeeded)
        jq . <<<"${result}"
        return 0
        ;;
      failed|cancelled|rolled_back)
        jq . <<<"${result}"
        return 1
        ;;
    esac
    sleep 2
  done
}

omniroute_submit() {
  local payload="${1:?}" detach="${2:-0}" result job_id
  result=$(omniroute_request omniroute-submit "${payload}") || return 1
  job_id=$(jq -r '.id // empty' <<<"${result}")
  [[ "${job_id}" =~ ^[a-f0-9-]{36}$ ]] || die "控制服务未返回有效 OmniRoute 任务 ID"
  if (( detach == 1 )); then
    jq . <<<"${result}"
    log "任务已进入后台：${job_id}；使用 llmctl omniroute job ${job_id} 查看。"
    return
  fi
  omniroute_wait_job "${job_id}"
}

omniroute_confirm() {
  local prompt="${1:?}" assume_yes="${2:-0}" answer=""
  (( assume_yes == 1 )) && return 0
  read -r -p "${prompt} [y/N] " answer
  [[ "${answer}" =~ ^[Yy]$ ]]
}

cmd_omniroute() {
  require_root
  load_config
  [[ "${GATEWAY_KIND}" == omniroute ]] || die "当前 AI 接入层不是 OmniRoute"
  local action="${1:-status}" subaction="" image="" backup_id="" job_id=""
  local assume_yes=0 detach=0 deep=0 local_image=0 payload="" confirmation=""
  shift || true
  case "${action}" in
    status)
      (($# == 0)) || die "用法：llmctl omniroute status"
      omniroute_request omniroute-status '{}' | jq .
      ;;
    backups)
      (($# == 0)) || die "用法：llmctl omniroute backups"
      omniroute_request omniroute-status '{}' | jq '.backups'
      ;;
    backup)
      while (($#)); do
        case "$1" in
          --detach) detach=1; shift ;;
          *) die "未知 omniroute backup 参数：$1" ;;
        esac
      done
      omniroute_submit '{"action":"backup"}' "${detach}"
      ;;
    update)
      image="${OMNIROUTE_RECOMMENDED_IMAGE:-diegosouzapw/omniroute:3.8.49}"
      if (($#)) && [[ "$1" != --* ]]; then image="$1"; shift; fi
      while (($#)); do
        case "$1" in
          --yes) assume_yes=1; shift ;;
          --detach) detach=1; shift ;;
          --local-image) local_image=1; shift ;;
          *) die "未知 omniroute update 参数：$1" ;;
        esac
      done
      local source_hint="从 Docker Hub 拉取"
      (( local_image == 0 )) || source_hint="仅使用已离线导入的本地镜像"
      omniroute_confirm \
        "确认先评估并备份 SQLite，再${source_hint}升级到 ${image}；失败时自动恢复原镜像和数据库？" \
        "${assume_yes}" || { log "已取消；未修改镜像、Router 或数据库。"; return; }
      payload=$(jq -cn --arg image "${image}" --argjson local_image "${local_image}" \
        '{action:"update",image:$image,local_image:($local_image == 1),confirmation:"UPDATE OMNIROUTE"}')
      omniroute_submit "${payload}" "${detach}"
      ;;
    rollback)
      (($# >= 1)) || die "用法：llmctl omniroute rollback <备份ID> [--yes] [--detach]"
      backup_id="$1"; shift
      while (($#)); do
        case "$1" in
          --yes) assume_yes=1; shift ;;
          --detach) detach=1; shift ;;
          *) die "未知 omniroute rollback 参数：$1" ;;
        esac
      done
      [[ "${backup_id}" =~ ^[a-z0-9][a-z0-9._-]{0,119}$ ]] || die "备份 ID 格式无效"
      omniroute_confirm \
        "确认先备份当前状态，再恢复 ${backup_id} 的镜像和 SQLite？" \
        "${assume_yes}" || { log "已取消回滚。"; return; }
      confirmation="ROLLBACK ${backup_id}"
      payload=$(jq -cn --arg backup_id "${backup_id}" --arg confirmation "${confirmation}" \
        '{action:"rollback",backup_id:$backup_id,confirmation:$confirmation}')
      omniroute_submit "${payload}" "${detach}"
      ;;
    sqlite)
      subaction="${1:-assess}"; shift || true
      case "${subaction}" in
        assess)
          while (($#)); do
            case "$1" in
              --deep) deep=1; shift ;;
              *) die "未知 omniroute sqlite assess 参数：$1" ;;
            esac
          done
          payload=$(jq -cn --argjson deep "${deep}" '{deep:($deep == 1)}')
          omniroute_request omniroute-assess "${payload}" | jq .
          ;;
        maintain)
          subaction="${1:-online}"
          [[ "${subaction}" == online || "${subaction}" == compact ]] || \
            die "维护模式必须是 online 或 compact"
          (($# == 0)) || shift
          while (($#)); do
            case "$1" in
              --yes) assume_yes=1; shift ;;
              --detach) detach=1; shift ;;
              *) die "未知 omniroute sqlite maintain 参数：$1" ;;
            esac
          done
          if [[ "${subaction}" == online ]]; then
            omniroute_confirm \
              "确认先备份，再在线执行 PRAGMA optimize 与 PASSIVE checkpoint？" \
              "${assume_yes}" || { log "已取消在线维护。"; return; }
            confirmation="MAINTAIN ONLINE"
          else
            omniroute_confirm \
              "确认先备份，短暂停止 Router 执行 VACUUM，失败时自动恢复？" \
              "${assume_yes}" || { log "已取消维护窗压缩。"; return; }
            confirmation="COMPACT SQLITE"
          fi
          payload=$(jq -cn --arg action "${subaction}" --arg confirmation "${confirmation}" \
            '{action:$action,confirmation:$confirmation}')
          omniroute_submit "${payload}" "${detach}"
          ;;
        *) die "omniroute sqlite 子命令必须是 assess|maintain" ;;
      esac
      ;;
    job|cancel)
      (($# == 1)) || die "用法：llmctl omniroute ${action} <任务ID>"
      job_id="$1"
      [[ "${job_id}" =~ ^[a-f0-9-]{36}$ ]] || die "任务 ID 格式无效"
      payload=$(jq -cn --arg id "${job_id}" '{id:$id}')
      if [[ "${action}" == job ]]; then
        omniroute_request omniroute-job "${payload}" | jq .
      else
        omniroute_request omniroute-cancel "${payload}" | jq .
      fi
      ;;
    *)
      die "omniroute 子命令必须是 status|backup|backups|update|rollback|sqlite|job|cancel"
      ;;
  esac
}

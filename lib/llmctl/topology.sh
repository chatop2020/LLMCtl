#!/usr/bin/env bash
# 为需要大量主存访问的 GPU Worker 解析可验证、可降级的 NUMA 亲和参数。

# 根据一组物理 GPU ID 返回共同 NUMA 节点及该节点的 CPU 列表。
# 成功时输出“NUMA节点<TAB>CPU列表”；拓扑缺失、GPU 跨节点或 sysfs
# 数据无效时返回非零并保持 Worker 未绑定，调用者不得硬编码现场 CPU 编号。
worker_numa_binding() {
  local devices="${1:?缺少 GPU 列表}" matrix="" gpu="" node="" current="" cpulist=""
  local node_root="${LLM_SYS_NODE_ROOT:-/sys/devices/system/node}"
  local -a gpu_ids=()
  command -v nvidia-smi >/dev/null 2>&1 || {
    warn "无法检测 GPU NUMA 拓扑；PLE Worker 保持系统默认调度。"
    return 1
  }
  matrix=$(nvidia-smi topo -m 2>/dev/null) || {
    warn "nvidia-smi 无法读取 NUMA 拓扑；PLE Worker 保持系统默认调度。"
    return 1
  }
  IFS=',' read -r -a gpu_ids <<<"${devices}"
  for gpu in "${gpu_ids[@]}"; do
    [[ "${gpu}" =~ ^[0-9]+$ ]] || {
      warn "GPU 标识 ${gpu} 不是物理序号；PLE Worker 保持系统默认调度。"
      return 1
    }
    current=$(awk -v row="GPU${gpu}" '$1 == row {print $(NF-1); exit}' <<<"${matrix}")
    [[ "${current}" =~ ^[0-9]+$ ]] || {
      warn "GPU ${gpu} 没有可用 NUMA Affinity；PLE Worker 保持系统默认调度。"
      return 1
    }
    if [[ -z "${node}" ]]; then
      node="${current}"
    elif [[ "${node}" != "${current}" ]]; then
      warn "GPU 组 ${devices} 跨 NUMA ${node}/${current}；为避免错误限内存，本次不绑定。"
      return 1
    fi
  done
  [[ -r "${node_root}/node${node}/cpulist" ]] || {
    warn "NUMA ${node} 的 CPU 列表不可读；PLE Worker 保持系统默认调度。"
    return 1
  }
  cpulist=$(tr -d '[:space:]' <"${node_root}/node${node}/cpulist")
  [[ "${cpulist}" =~ ^[0-9,-]+$ ]] || {
    warn "NUMA ${node} 的 CPU 列表无效；PLE Worker 保持系统默认调度。"
    return 1
  }
  printf '%s\t%s\n' "${node}" "${cpulist}"
}

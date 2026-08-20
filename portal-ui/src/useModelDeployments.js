import { computed, reactive, ref, watch } from "vue";

/**
 * 建立模型部署页面的状态、计划指纹和受限操作。
 *
 * @param {object} dependencies 由门户组合根注入的稳定依赖。
 * @param {Function} dependencies.api 调用同源门户管理 API 的函数。
 * @param {import("vue").ComputedRef<boolean>} dependencies.isAdmin 当前会话的管理员状态。
 * @param {import("vue").Ref<object|null>} dependencies.session 当前登录会话。
 * @param {Function} dependencies.notify 向管理员展示结果和恢复建议的函数。
 * @returns {Record<string, unknown>} 页面使用的响应式状态和操作函数。
 */
export function useModelDeployments({ api, isAdmin, session, notify }) {
  const modelDeployments = ref(null);
  const modelDeploymentsLoading = ref(false);
  const modelDeploymentPlanning = ref(false);
  const modelDeploymentSubmitting = ref(false);
  const modelDeploymentPlan = ref(null);
  const modelDeploymentConfirmed = ref(false);
  const modelUpgradePlanning = ref(false);
  const modelUpgradeSubmitting = ref(false);
  const modelUpgradePlan = ref(null);
  const modelUpgradeConfirmed = ref(false);
  const modelUpgradeForm = reactive({
    source_deployment_id: "",
    target_model_id: "",
    target_revision: "",
    max_model_len: 32768,
  });

  const modelDeploymentMode = ref("local");
  const modelDeploymentForm = reactive({
    deployment_id: "",
    hub: "huggingface",
    model_id: "",
    revision: "main",
    artifact_path: "/data/llm-cluster/models/current",
    public_model_id: "",
    served_model_name: "",
    display_name: "",
    additional_public_ids: "",
    worker_start_id: 0,
    worker_base_port: 8100,
    selected_gpu_ids: [],
    image: "vllm/vllm-openai:v0.22.1",
    tensor_parallel_size: 1,
    max_model_len: 32768,
    gpu_memory_utilization: 0.9,
    max_num_seqs: 4,
    max_num_batched_tokens: 8192,
    trust_remote_code: false,
    supports_image_input: false,
    supports_ocr: false,
    supports_tool_calling: false,
    supports_reasoning: false,
    supports_thinking_toggle: false,
    tool_call_parser: "",
    reasoning_parser: "",
    mm_limit: '{"image":4}',
    publish_requested: true,
    preserve_legacy_alias: false,
  });
  const modelRemoteTargets = ref([
    {
      id: "remote-0",
      base_url: "",
      api_key_env: "BACKEND_API_KEY",
      enabled: true,
    },
  ]);

  const modelDeploymentRegistry = computed(
    () => modelDeployments.value?.registry || { deployments: {}, artifacts: {} },
  );
  const modelDeploymentRows = computed(() =>
    Object.values(modelDeploymentRegistry.value.deployments || {}).sort((left, right) =>
      String(left.display_name || left.id).localeCompare(
        String(right.display_name || right.id),
      ),
    ),
  );
  const modelDeploymentJobs = computed(() => modelDeployments.value?.jobs || []);
  const modelUpgradeProfiles = computed(
    () => modelDeployments.value?.upgrade_profiles || [],
  );
  const ornithUpgradeSources = computed(() =>
    modelDeploymentRows.value.filter(
      (deployment) =>
        deployment.enabled !== false &&
        String(deployment.model_id || "").toLowerCase().includes("ornith") &&
        (deployment.instances || []).some(
          (instance) => instance.kind === "local" && instance.enabled !== false,
        ),
    ),
  );
  const activeModelDeploymentJob = computed(() =>
    modelDeploymentJobs.value.find(
      (job) => !["succeeded", "failed", "cancelled", "rolled_back"].includes(job.state),
    ),
  );
  const selectedDeploymentGpus = computed(() =>
    new Set(modelDeploymentForm.selected_gpu_ids.map((value) => Number(value))),
  );
  const assignedDeploymentGpus = computed(() => {
    const assigned = {};
    for (const deployment of modelDeploymentRows.value) {
      if (!deployment.enabled) continue;
      for (const instance of deployment.instances || []) {
        if (instance.kind !== "local" || !instance.enabled) continue;
        for (const gpu of instance.gpu_devices || []) assigned[String(gpu)] = deployment.id;
      }
    }
    return assigned;
  });

  watch(
    [modelDeploymentMode, modelDeploymentForm, modelRemoteTargets],
    () => {
      modelDeploymentPlan.value = null;
      modelDeploymentConfirmed.value = false;
    },
    { deep: true },
  );
  watch(
    modelUpgradeForm,
    () => {
      modelUpgradePlan.value = null;
      modelUpgradeConfirmed.value = false;
    },
    { deep: true },
  );

  async function loadModelDeployments(options = {}) {
    if (!isAdmin.value || !session.value?.authenticated || modelDeploymentsLoading.value)
      return;
    modelDeploymentsLoading.value = true;
    try {
      const result = await api("admin/model-deployments");
      modelDeployments.value = result;
      const gpuIds = (result.gpus || []).map((gpu) => Number(gpu.id));
      if (!modelDeploymentForm.selected_gpu_ids.length && gpuIds.length) {
        const midpoint = Math.max(1, Math.ceil(gpuIds.length / 2));
        modelDeploymentForm.selected_gpu_ids = gpuIds.slice(midpoint);
        modelDeploymentForm.worker_start_id =
          modelDeploymentForm.selected_gpu_ids[0] ?? 0;
      }
      if (!result.gateway?.registry_publish)
        modelDeploymentForm.publish_requested = false;
      const profile = (result.upgrade_profiles || [])[0];
      if (!modelUpgradeForm.target_model_id && profile) {
        modelUpgradeForm.target_model_id = profile.model_id || "";
        modelUpgradeForm.target_revision = profile.revision || "";
        modelUpgradeForm.max_model_len = Number(
          profile.recommended_max_model_len || 32768,
        );
      }
      const sources = Object.values(result.registry?.deployments || {}).filter(
        (deployment) =>
          deployment.enabled !== false &&
          String(deployment.model_id || "").toLowerCase().includes("ornith"),
      );
      if (!modelUpgradeForm.source_deployment_id && sources.length === 1)
        modelUpgradeForm.source_deployment_id = sources[0].id;
    } catch (error) {
      modelDeployments.value = { available: false, error: error.message };
      if (!options.silent) notify(`模型部署状态读取失败：${error.message}`, "bad");
    } finally {
      modelDeploymentsLoading.value = false;
    }
  }

  function prepareExistingDeployment(deployment) {
    const artifact = modelDeploymentRegistry.value.artifacts?.[deployment.artifact_id] || {};
    const instances = (deployment.instances || []).filter((item) => item.enabled !== false);
    const localInstances = instances
      .filter((item) => item.kind === "local")
      .sort((left, right) => Number(left.worker_id) - Number(right.worker_id));
    const remoteInstances = instances.filter((item) => item.kind === "remote");
    const runtime = deployment.runtime || {};
    const isLegacy = deployment.id === "legacy";
    const publicIds = [...(deployment.public_model_ids || [])];

    modelDeploymentMode.value = remoteInstances.length && !localInstances.length ? "remote" : "local";
    Object.assign(modelDeploymentForm, {
      deployment_id: deployment.id,
      hub: artifact.hub || "local",
      model_id: deployment.model_id || artifact.model_id || "",
      revision: artifact.revision || "main",
      artifact_path: artifact.path || "",
      public_model_id: isLegacy ? "gdn-inside-ornith" : (publicIds[0] || deployment.id),
      served_model_name: deployment.served_model_name || deployment.model_id || "",
      display_name: isLegacy ? "Ornith 内部模型" : (deployment.display_name || deployment.id),
      additional_public_ids: isLegacy ? "" : publicIds.slice(1).join(", "),
      worker_start_id: localInstances[0]?.worker_id ?? 0,
      worker_base_port: localInstances.length
        ? Number(localInstances[0].port) - Number(localInstances[0].worker_id)
        : 8100,
      selected_gpu_ids: localInstances.flatMap((item) => item.gpu_devices || []).map(Number),
      image: runtime.image || "vllm/vllm-openai:v0.22.1",
      tensor_parallel_size: Number(runtime.tensor_parallel_size || 1),
      max_model_len: Number(runtime.max_model_len || 32768),
      gpu_memory_utilization: Number(runtime.gpu_memory_utilization || 0.9),
      max_num_seqs: Number(runtime.max_num_seqs || 4),
      max_num_batched_tokens: Number(runtime.max_num_batched_tokens || 8192),
      trust_remote_code: Boolean(runtime.trust_remote_code),
      supports_image_input: Boolean(runtime.supports_image_input),
      supports_ocr: Boolean(runtime.supports_ocr),
      supports_tool_calling: Boolean(runtime.supports_tool_calling),
      supports_reasoning: Boolean(runtime.supports_reasoning),
      supports_thinking_toggle: Boolean(runtime.supports_thinking_toggle),
      tool_call_parser: runtime.tool_call_parser || "",
      reasoning_parser: runtime.reasoning_parser || "",
      mm_limit: runtime.mm_limit || '{"image":4}',
      publish_requested: Boolean(
        deployment.publish_requested && modelDeployments.value?.gateway?.registry_publish,
      ),
      preserve_legacy_alias: isLegacy,
    });
    modelRemoteTargets.value = remoteInstances.length
      ? remoteInstances.map((item) => ({
          id: item.id,
          base_url: item.base_url,
          api_key_env: item.api_key_env || "BACKEND_API_KEY",
          enabled: item.enabled !== false,
        }))
      : [{ id: "remote-0", base_url: "", api_key_env: "BACKEND_API_KEY", enabled: true }];
    modelDeploymentPlan.value = null;
    modelDeploymentConfirmed.value = false;
    window.scrollTo({ top: 0, behavior: "smooth" });
    notify(
      isLegacy
        ? "已载入旧版 Ornith 部署；建议先完成 Qwen 对后半组 GPU 的接管，再把这里改为前半组"
        : `已载入 ${deployment.display_name || deployment.id}，修改后请重新生成部署计划`,
    );
  }

  function setDeploymentGpuSelection(mode) {
    const ids = (modelDeployments.value?.gpus || []).map((gpu) => Number(gpu.id));
    const midpoint = Math.max(1, Math.ceil(ids.length / 2));
    if (mode === "first") modelDeploymentForm.selected_gpu_ids = ids.slice(0, midpoint);
    else if (mode === "second") modelDeploymentForm.selected_gpu_ids = ids.slice(midpoint);
    else modelDeploymentForm.selected_gpu_ids = ids;
    modelDeploymentForm.worker_start_id =
      modelDeploymentForm.selected_gpu_ids[0] ?? 0;
  }

  function toggleDeploymentGpu(gpuId) {
    const id = Number(gpuId);
    const selected = new Set(modelDeploymentForm.selected_gpu_ids.map(Number));
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
    modelDeploymentForm.selected_gpu_ids = [...selected].sort((left, right) => left - right);
    if (modelDeploymentForm.selected_gpu_ids.length)
      modelDeploymentForm.worker_start_id = modelDeploymentForm.selected_gpu_ids[0];
  }

  function addModelRemoteTarget() {
    modelRemoteTargets.value.push({
      id: `remote-${modelRemoteTargets.value.length}`,
      base_url: "",
      api_key_env: "BACKEND_API_KEY",
      enabled: true,
    });
  }

  function removeModelRemoteTarget(index) {
    if (modelRemoteTargets.value.length === 1) {
      notify("至少保留一个远程实例输入行", "bad");
      return;
    }
    modelRemoteTargets.value.splice(index, 1);
  }

  function deploymentPublicIds(value) {
    return [...new Set(String(value || "").split(/[\s,，]+/).map((item) => item.trim()).filter(Boolean))];
  }

  function modelDeploymentPayload() {
    const deploymentId = String(modelDeploymentForm.deployment_id || "").trim();
    const publicModelId = String(modelDeploymentForm.public_model_id || "").trim();
    const servedModelName = String(modelDeploymentForm.served_model_name || "").trim();
    if (!deploymentId || !publicModelId || !servedModelName)
      throw new Error("部署 ID、公开模型 ID 和 vLLM 服务模型名均为必填项");

    const tensorParallelSize = Number(modelDeploymentForm.tensor_parallel_size);
    let instances = [];
    if (modelDeploymentMode.value === "local") {
      const gpuIds = [...selectedDeploymentGpus.value].sort((left, right) => left - right);
      if (!gpuIds.length) throw new Error("请至少选择一张 GPU");
      if (!Number.isInteger(tensorParallelSize) || tensorParallelSize < 1)
        throw new Error("张量并行数必须为正整数");
      if (gpuIds.length % tensorParallelSize !== 0)
        throw new Error("所选 GPU 数必须能被张量并行数整除");
      const workerStart = Number(modelDeploymentForm.worker_start_id);
      for (let offset = 0; offset < gpuIds.length; offset += tensorParallelSize) {
        const workerId = workerStart + offset / tensorParallelSize;
        instances.push({
          id: `local-worker-${workerId}`,
          kind: "local",
          worker_id: workerId,
          gpu_devices: gpuIds.slice(offset, offset + tensorParallelSize),
          port: Number(modelDeploymentForm.worker_base_port) + workerId,
          enabled: true,
        });
      }
    } else {
      instances = modelRemoteTargets.value
        .filter((item) => item.enabled && String(item.base_url || "").trim())
        .map((item, index) => ({
          id: String(item.id || `remote-${index}`).trim(),
          kind: "remote",
          base_url: String(item.base_url).trim().replace(/\/$/, ""),
          api_key_env: String(item.api_key_env || "BACKEND_API_KEY").trim(),
          enabled: true,
        }));
      if (!instances.length) throw new Error("请至少配置一个已启用的远程实例");
    }

    const payload = {
      deployment_id: deploymentId,
      hub: modelDeploymentForm.hub,
      model_id: String(modelDeploymentForm.model_id || "").trim(),
      revision: String(modelDeploymentForm.revision || "main").trim(),
      public_model_id: publicModelId,
      served_model_name: servedModelName,
      artifact_path: String(modelDeploymentForm.artifact_path || "").trim(),
      instances,
      image: String(modelDeploymentForm.image || "").trim(),
      tensor_parallel_size: tensorParallelSize,
      max_model_len: Number(modelDeploymentForm.max_model_len),
      gpu_memory_utilization: Number(modelDeploymentForm.gpu_memory_utilization),
      max_num_seqs: Number(modelDeploymentForm.max_num_seqs),
      max_num_batched_tokens: Number(modelDeploymentForm.max_num_batched_tokens),
      trust_remote_code: Boolean(modelDeploymentForm.trust_remote_code),
      supports_image_input: Boolean(modelDeploymentForm.supports_image_input),
      supports_ocr: Boolean(modelDeploymentForm.supports_ocr),
      supports_tool_calling: Boolean(modelDeploymentForm.supports_tool_calling),
      supports_reasoning: Boolean(modelDeploymentForm.supports_reasoning),
      supports_thinking_toggle: Boolean(modelDeploymentForm.supports_thinking_toggle),
      tool_call_parser: String(modelDeploymentForm.tool_call_parser || "").trim(),
      reasoning_parser: String(modelDeploymentForm.reasoning_parser || "").trim(),
      mm_limit: String(modelDeploymentForm.mm_limit || "").trim(),
      display_name: String(modelDeploymentForm.display_name || publicModelId).trim(),
      additional_public_ids: deploymentPublicIds(modelDeploymentForm.additional_public_ids),
      publish_requested: Boolean(modelDeploymentForm.publish_requested),
      preserve_legacy_alias: Boolean(modelDeploymentForm.preserve_legacy_alias),
    };
    if (!payload.model_id)
      throw new Error("必须填写模型 ID；本机目录也需要填写其实际服务模型 ID");
    return payload;
  }

  async function planModelDeployment() {
    if (modelDeploymentPlanning.value || activeModelDeploymentJob.value) return;
    modelDeploymentPlanning.value = true;
    try {
      const payload = modelDeploymentPayload();
      const plan = await api("admin/model-deployments/plan", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      modelDeploymentPlan.value = {
        ...plan,
        request_fingerprint: JSON.stringify(payload),
      };
      modelDeploymentConfirmed.value = false;
      notify("部署计划已生成，请核对受影响 Worker、GPU 和公开模型 ID");
    } catch (error) {
      notify(`部署计划生成失败：${error.message}`, "bad");
    } finally {
      modelDeploymentPlanning.value = false;
    }
  }

  async function submitModelDeployment() {
    if (modelDeploymentSubmitting.value || !modelDeploymentPlan.value) return;
    if (!modelDeploymentConfirmed.value) {
      notify("请先确认部署影响和自动回滚说明", "bad");
      return;
    }
    modelDeploymentSubmitting.value = true;
    try {
      const payload = modelDeploymentPayload();
      if (JSON.stringify(payload) !== modelDeploymentPlan.value.request_fingerprint)
        throw new Error("配置已变化，请重新生成部署计划");
      await api("admin/model-deployments/submit", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      notify("模型部署任务已进入后台执行", "working");
      await loadModelDeployments({ silent: true });
    } catch (error) {
      notify(`模型部署提交失败：${error.message}`, "bad");
    } finally {
      modelDeploymentSubmitting.value = false;
    }
  }

  async function cancelModelDeployment(job) {
    if (!job?.id) return;
    try {
      await api("admin/model-deployments/cancel", {
        method: "POST",
        body: JSON.stringify({ id: job.id }),
      });
      notify("已请求取消；控制服务将在安全检查点停止或回滚", "working");
      await loadModelDeployments({ silent: true });
    } catch (error) {
      notify(`取消部署失败：${error.message}`, "bad");
    }
  }

  async function rollbackModelDeployment(job) {
    if (!job?.id || !job?.backup || job.state !== "succeeded") return;
    if (
      !window.confirm(
        "确认恢复到该任务执行前的运行配置？只会重启该任务涉及的 Worker；当前配置会先创建新的保护快照。",
      )
    )
      return;
    try {
      await api("admin/model-deployments/rollback", {
        method: "POST",
        body: JSON.stringify({ id: job.id }),
      });
      notify("回滚任务已进入后台执行，可在本页持续查看进度", "working");
      await loadModelDeployments({ silent: true });
    } catch (error) {
      notify(`回滚提交失败：${error.message}`, "bad");
    }
  }

  function deploymentJobStateLabel(value) {
    return {
      waiting: "等待执行",
      running: "执行中",
      succeeded: "已完成",
      failed: "失败",
      cancelled: "已取消",
      rolled_back: "已回滚",
    }[value] || value || "未知";
  }

  function modelUpgradePayload() {
    /** 返回两端共用的最小升级契约，拓扑和能力由控制服务权威计算。 */
    const payload = {
      source_deployment_id: String(
        modelUpgradeForm.source_deployment_id || "",
      ).trim(),
      target_model_id: String(modelUpgradeForm.target_model_id || "").trim(),
      target_revision: String(modelUpgradeForm.target_revision || "").trim(),
      max_model_len: Number(modelUpgradeForm.max_model_len),
    };
    if (!payload.source_deployment_id) throw new Error("请选择要升级的 Ornith 部署");
    if (!payload.target_model_id) throw new Error("请选择 Ornith 升级目标");
    return payload;
  }

  async function planModelUpgrade() {
    /** 只读解析目标 SHA、目标拓扑、受影响 Worker 和回退条件。 */
    if (modelUpgradePlanning.value || activeModelDeploymentJob.value) return;
    modelUpgradePlanning.value = true;
    try {
      const payload = modelUpgradePayload();
      const plan = await api("admin/model-upgrades/plan", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      modelUpgradePlan.value = {
        ...plan,
        request_fingerprint: JSON.stringify(payload),
      };
      modelUpgradeConfirmed.value = false;
      notify("升级计划已生成；请核对固定 SHA、Worker 重载范围和回退说明");
    } catch (error) {
      notify(`升级计划生成失败：${error.message}`, "bad");
    } finally {
      modelUpgradePlanning.value = false;
    }
  }

  async function submitModelUpgrade() {
    /** 提交仍与已确认计划完全一致且注册表未变化的升级任务。 */
    if (modelUpgradeSubmitting.value || !modelUpgradePlan.value) return;
    if (!modelUpgradeConfirmed.value) {
      notify("请先确认维护窗口、固定 revision 和回退条件", "bad");
      return;
    }
    modelUpgradeSubmitting.value = true;
    try {
      const payload = modelUpgradePayload();
      if (JSON.stringify(payload) !== modelUpgradePlan.value.request_fingerprint)
        throw new Error("升级参数已变化，请重新生成计划");
      await api("admin/model-upgrades/submit", {
        method: "POST",
        body: JSON.stringify({
          ...payload,
          // 即使表单计划时留空，执行也只使用计划展示过的不可变 SHA。
          target_revision: modelUpgradePlan.value.upgrade.target_revision,
          expected_registry_revision:
            modelUpgradePlan.value.source_registry_revision,
        }),
      });
      notify("Ornith 升级任务已进入后台；公开切换前会先执行真实生成", "working");
      await loadModelDeployments({ silent: true });
    } catch (error) {
      notify(`升级提交失败：${error.message}`, "bad");
    } finally {
      modelUpgradeSubmitting.value = false;
    }
  }

  return {
    modelDeployments,
    modelDeploymentsLoading,
    modelDeploymentPlanning,
    modelDeploymentSubmitting,
    modelDeploymentPlan,
    modelDeploymentConfirmed,
    modelUpgradePlanning,
    modelUpgradeSubmitting,
    modelUpgradePlan,
    modelUpgradeConfirmed,
    modelUpgradeForm,
    modelDeploymentMode,
    modelDeploymentForm,
    modelRemoteTargets,
    modelDeploymentRegistry,
    modelDeploymentRows,
    modelDeploymentJobs,
    modelUpgradeProfiles,
    ornithUpgradeSources,
    activeModelDeploymentJob,
    selectedDeploymentGpus,
    assignedDeploymentGpus,
    loadModelDeployments,
    prepareExistingDeployment,
    setDeploymentGpuSelection,
    toggleDeploymentGpu,
    addModelRemoteTarget,
    removeModelRemoteTarget,
    deploymentPublicIds,
    modelDeploymentPayload,
    planModelDeployment,
    submitModelDeployment,
    cancelModelDeployment,
    rollbackModelDeployment,
    deploymentJobStateLabel,
    modelUpgradePayload,
    planModelUpgrade,
    submitModelUpgrade,
  };
}

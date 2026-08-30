import { computed, nextTick, reactive, ref, watch } from "vue";

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
  const qwen38QuickBusy = ref(false);
  const qwen38QuickPlan = ref(null);
  const qwen38QuickForm = reactive({
    max_model_len: 262144,
    gpu_memory_utilization: 0.92,
    max_num_seqs: 8,
    max_num_batched_tokens: 8192,
    mtp_speculative_tokens: 0,
    kv_cache_dtype: "auto",
    enable_prefix_caching: false,
    max_images_per_request: 4,
  });
  const modelUpgradePlanning = ref(false);
  const modelUpgradeSubmitting = ref(false);
  const modelUpgradePlan = ref(null);
  const modelUpgradeConfirmed = ref(false);
  const modelUpgradeSubmitError = ref("");
  const modelDownloadProxyBusy = ref(false);
  const modelDownloadProxyMessage = ref("");
  const modelDownloadProxyForm = reactive({
    proxy_url: "",
    no_proxy: "127.0.0.1,localhost,::1",
    hub: "huggingface",
  });
  const modelUpgradeForm = reactive({
    source_deployment_id: "",
    target_profile_id: "",
    target_hub: "",
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
    ple_cpu_offload: false,
    enable_expert_parallel: false,
    enable_prefix_caching: true,
    enable_flashinfer_autotune: true,
    disable_custom_all_reduce: false,
    mtp_speculative_tokens: 0,
    kv_cache_dtype: "auto",
    yarn_factor: 1,
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
  const qwen38QuickProfile = computed(
    () => modelDeployments.value?.qwen38_quick || null,
  );
  const qwen38QuickJob = computed(
    () =>
      modelDeploymentJobs.value.find((job) => job.kind === "qwen38") || null,
  );
  const qwen38QuickRollbackJob = computed(() => {
    const id = qwen38QuickProfile.value?.rollback_job_id;
    return id ? modelDeploymentJobs.value.find((job) => job.id === id) || null : null;
  });
  const modelUpgradeProfiles = computed(
    () => modelDeployments.value?.upgrade_profiles || [],
  );
  const modelUpgradeProfileGroups = computed(() => {
    const source = modelDeploymentRows.value.find(
      (deployment) => deployment.id === modelUpgradeForm.source_deployment_id,
    );
    const artifact = modelDeploymentRegistry.value.artifacts?.[source?.artifact_id] || {};
    const preferredHub = ["modelscope", "huggingface"].includes(artifact.hub)
      ? artifact.hub
      : "modelscope";
    return [preferredHub, ...["modelscope", "huggingface"].filter((hub) => hub !== preferredHub)]
      .map((hub) => ({
        hub,
        label: hub === "modelscope" ? "ModelScope" : "Hugging Face",
        profiles: modelUpgradeProfiles.value.filter((profile) => profile.hub === hub),
      }))
      .filter((group) => group.profiles.length);
  });
  const modelUpgradeUnavailableReason = computed(() => {
    if (!modelDeployments.value || modelDeployments.value.available === false)
      return "";
    if (modelUpgradeProfiles.value.length) return "";
    return "模型部署控制服务仍在运行旧版本。请执行 sudo llmctl model init 重新加载控制服务，然后刷新状态。";
  });
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
  const displayedModelUpgradeJob = computed(
    () =>
      activeModelDeploymentJob.value ||
      modelDeploymentJobs.value.find((job) => ["upgrade", "publish"].includes(job.kind)) ||
      null,
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

  let upgradeSourceDefaultsId = "";
  let downloadProxyDefaultsLoaded = false;
  let qwen38DefaultsLoaded = false;

  function applyUpgradeProfile(profileId) {
    const profile = modelUpgradeProfiles.value.find((item) => item.id === profileId);
    if (!profile) return false;
    modelUpgradeForm.target_profile_id = profile.id;
    modelUpgradeForm.target_hub = profile.hub || "modelscope";
    modelUpgradeForm.target_model_id = profile.model_id || "";
    modelUpgradeForm.target_revision = profile.revision || "";
    return true;
  }

  /**
   * 从来源部署读取升级前真实生效的上下文。
   * 管理员切换来源时更新一次，后续自动刷新不覆盖其手工调整。
   */
  function applyUpgradeSourceRuntime(deploymentId) {
    const source = modelDeploymentRows.value.find(
      (deployment) => deployment.id === deploymentId,
    );
    if (!source) return;
    const maxModelLen = Number(source.runtime?.max_model_len);
    modelUpgradeForm.max_model_len =
      Number.isInteger(maxModelLen) && maxModelLen >= 8192 && maxModelLen <= 262144
        ? maxModelLen
        : 32768;
    const artifact = modelDeploymentRegistry.value.artifacts?.[source.artifact_id] || {};
    const preferredHub = ["modelscope", "huggingface"].includes(artifact.hub)
      ? artifact.hub
      : "modelscope";
    const preferredProfile =
      modelUpgradeProfiles.value.find(
        (profile) =>
          profile.hub === preferredHub &&
          profile.model_id === "ornith-ai/Ornith-1.5-35B-A3B-FP8",
      ) ||
      modelUpgradeProfiles.value.find(
        (profile) => profile.hub === preferredHub && profile.recommended,
      ) ||
      modelUpgradeProfiles.value.find((profile) => profile.hub === preferredHub) ||
      modelUpgradeProfiles.value[0];
    if (preferredProfile) applyUpgradeProfile(preferredProfile.id);
    upgradeSourceDefaultsId = deploymentId;
  }

  watch(
    () => modelUpgradeForm.source_deployment_id,
    (deploymentId) => {
      if (deploymentId && deploymentId !== upgradeSourceDefaultsId)
        applyUpgradeSourceRuntime(deploymentId);
    },
  );
  watch(
    () => modelUpgradeForm.target_profile_id,
    (profileId) => applyUpgradeProfile(profileId),
  );

  async function loadModelDeployments(options = {}) {
    if (!isAdmin.value || !session.value?.authenticated || modelDeploymentsLoading.value)
      return;
    modelDeploymentsLoading.value = true;
    try {
      const result = await api("admin/model-deployments");
      modelDeployments.value = result;
      const proxy = result.download_environment?.maintenance_proxy;
      if (!downloadProxyDefaultsLoaded && proxy) {
        modelDownloadProxyForm.proxy_url = proxy.proxy_url || "";
        modelDownloadProxyForm.no_proxy =
          proxy.no_proxy || "127.0.0.1,localhost,::1";
        downloadProxyDefaultsLoaded = true;
      }
      const gpuIds = (result.gpus || []).map((gpu) => Number(gpu.id));
      if (!modelDeploymentForm.selected_gpu_ids.length && gpuIds.length) {
        const midpoint = Math.max(1, Math.ceil(gpuIds.length / 2));
        modelDeploymentForm.selected_gpu_ids = gpuIds.slice(midpoint);
        modelDeploymentForm.worker_start_id =
          modelDeploymentForm.selected_gpu_ids[0] ?? 0;
      }
      if (!result.gateway?.registry_publish)
        modelDeploymentForm.publish_requested = false;
      if (!qwen38DefaultsLoaded && result.qwen38_quick?.runtime) {
        const runtime = result.qwen38_quick.runtime;
        for (const key of Object.keys(qwen38QuickForm)) {
          if (Object.hasOwn(runtime, key)) qwen38QuickForm[key] = runtime[key];
        }
        qwen38QuickForm.max_images_per_request = Number(
          result.qwen38_quick.max_images_per_request || 4,
        );
        qwen38DefaultsLoaded = true;
      }
      const sources = Object.values(result.registry?.deployments || {}).filter(
        (deployment) =>
          deployment.enabled !== false &&
          String(deployment.model_id || "").toLowerCase().includes("ornith"),
      );
      if (!modelUpgradeForm.source_deployment_id && sources.length === 1) {
        modelUpgradeForm.source_deployment_id = sources[0].id;
        applyUpgradeSourceRuntime(sources[0].id);
      } else if (
        modelUpgradeForm.source_deployment_id &&
        !upgradeSourceDefaultsId
      ) {
        applyUpgradeSourceRuntime(modelUpgradeForm.source_deployment_id);
      }
      if (!modelUpgradeForm.target_profile_id) {
        const profile = (result.upgrade_profiles || []).find(
          (item) => item.hub === "modelscope" && item.recommended,
        ) || (result.upgrade_profiles || [])[0];
        if (profile) applyUpgradeProfile(profile.id);
      }
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
      ple_cpu_offload: Boolean(runtime.ple_cpu_offload),
      enable_expert_parallel: Boolean(runtime.enable_expert_parallel),
      enable_prefix_caching: runtime.enable_prefix_caching !== false,
      enable_flashinfer_autotune: runtime.enable_flashinfer_autotune !== false,
      disable_custom_all_reduce: Boolean(runtime.disable_custom_all_reduce),
      mtp_speculative_tokens: Number(runtime.mtp_speculative_tokens || 0),
      kv_cache_dtype: runtime.kv_cache_dtype || "auto",
      yarn_factor: Number(runtime.yarn_factor || 1),
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

  /**
   * 应用已核对 revision 的 Qwen3.8 Flash Next NVFP4 原生 262K 预设。
   * GPU 选择保持不变，管理员仍需按本机拓扑确认四个 TP2 分组。
   */
  function applyQwen38FlashNextPreset() {
    const profile = qwen38QuickProfile.value;
    if (!profile) {
      notify("Qwen3.8 一键预设尚未加载，请先刷新模型部署状态", "bad");
      return;
    }
    const runtime = profile.runtime || {};
    Object.assign(modelDeploymentForm, {
      deployment_id: "qwen38-flash-next",
      hub: profile.hub,
      model_id: profile.model_id,
      revision: profile.revision,
      public_model_id: profile.public_model_id,
      served_model_name: profile.public_model_id,
      display_name: profile.display_name,
      selected_gpu_ids: (profile.gpu_groups?.groups || []).flat(),
      worker_start_id: 0,
      ...runtime,
    });
    notify("已载入后端核验的 Qwen3.8 全八卡预设");
  }

  /** 返回专用页面允许调整的少量高级参数。 */
  function qwen38QuickPayload() {
    return {
      max_model_len: Number(qwen38QuickForm.max_model_len),
      gpu_memory_utilization: Number(qwen38QuickForm.gpu_memory_utilization),
      max_num_seqs: Number(qwen38QuickForm.max_num_seqs),
      max_num_batched_tokens: Number(qwen38QuickForm.max_num_batched_tokens),
      mtp_speculative_tokens: Number(qwen38QuickForm.mtp_speculative_tokens),
      kv_cache_dtype: String(qwen38QuickForm.kv_cache_dtype || "auto"),
      enable_prefix_caching: Boolean(qwen38QuickForm.enable_prefix_caching),
      max_images_per_request: Number(qwen38QuickForm.max_images_per_request),
    };
  }

  /** 一次点击完成后端预检，并在同一注册表版本上提交自动部署。 */
  async function deployQwen38Quick() {
    if (qwen38QuickBusy.value || activeModelDeploymentJob.value) return;
    qwen38QuickBusy.value = true;
    qwen38QuickPlan.value = null;
    try {
      const payload = qwen38QuickPayload();
      const plan = await api("admin/qwen38/plan", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      qwen38QuickPlan.value = plan;
      const job = await api("admin/qwen38/deploy", {
        method: "POST",
        body: JSON.stringify({
          ...payload,
          expected_registry_revision: plan.source_registry_revision,
        }),
      });
      recordModelDeploymentJob(job);
      notify("Qwen3.8 自动部署已开始；失败会自动恢复当前状态", "working");
      await loadModelDeployments({ silent: true });
      await focusActiveModelDeploymentJob();
    } catch (error) {
      notify(`Qwen3.8 自动部署未启动：${error.message}`, "bad");
    } finally {
      qwen38QuickBusy.value = false;
    }
  }

  /** 把成功的一键部署恢复到它执行前保存的完整运行快照。 */
  async function rollbackQwen38Quick() {
    const job = qwen38QuickRollbackJob.value;
    if (!job || activeModelDeploymentJob.value) return;
    if (!window.confirm("确认恢复到部署 Qwen3.8 之前的模型、Worker 和路由状态？")) return;
    try {
      const rollback = await api("admin/model-deployments/rollback", {
        method: "POST",
        body: JSON.stringify({ id: job.id }),
      });
      recordModelDeploymentJob(rollback);
      notify("恢复任务已开始；完成前不会提前切换公开路由", "working");
      await loadModelDeployments({ silent: true });
      await focusActiveModelDeploymentJob();
    } catch (error) {
      notify(`恢复任务未启动：${error.message}`, "bad");
    }
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
      ple_cpu_offload: Boolean(modelDeploymentForm.ple_cpu_offload),
      enable_expert_parallel: Boolean(modelDeploymentForm.enable_expert_parallel),
      enable_prefix_caching: Boolean(modelDeploymentForm.enable_prefix_caching),
      enable_flashinfer_autotune: Boolean(modelDeploymentForm.enable_flashinfer_autotune),
      disable_custom_all_reduce: Boolean(modelDeploymentForm.disable_custom_all_reduce),
      mtp_speculative_tokens: Number(modelDeploymentForm.mtp_speculative_tokens),
      kv_cache_dtype: String(modelDeploymentForm.kv_cache_dtype || "auto"),
      yarn_factor: Number(modelDeploymentForm.yarn_factor),
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

  /**
   * 仅把当前部署注册表重新发布到 AI 接入层。
   * 该恢复动作不下载权重、不停止 Worker，适用于模型已验收但 publishing 失败。
   */
  async function retryModelDeploymentPublish() {
    if (activeModelDeploymentJob.value) return;
    try {
      const job = await api("admin/model-deployments/publish", {
        method: "POST",
        body: "{}",
      });
      await loadModelDeployments({ silent: true });
      if (!(modelDeployments.value?.jobs || []).some((item) => item?.id === job?.id)) {
        recordModelDeploymentJob(job);
      }
      notify("已创建仅路由发布任务；模型和 Worker 保持运行", "working");
      await focusActiveModelDeploymentJob();
    } catch (error) {
      notify(`路由发布重试失败：${error.message}`, "bad");
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

  /** 返回 Web UI 代理测试与保存接口共享的最小配置。 */
  function modelDownloadProxyPayload() {
    return {
      proxy_url: String(modelDownloadProxyForm.proxy_url || "").trim(),
      no_proxy: String(modelDownloadProxyForm.no_proxy || "").trim(),
      hub: String(modelDownloadProxyForm.hub || "huggingface").trim(),
    };
  }

  /** 通过候选代理执行真实 Hub 元数据请求，但不修改服务器配置。 */
  async function testModelDownloadProxy() {
    if (modelDownloadProxyBusy.value) return;
    modelDownloadProxyBusy.value = true;
    modelDownloadProxyMessage.value = "正在通过代理访问目标 Hub…";
    try {
      const payload = modelDownloadProxyPayload();
      const result = await api("admin/model-download/proxy/test", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      modelDownloadProxyMessage.value = `${result.hub} 代理测试通过；尚未保存。`;
    } catch (error) {
      modelDownloadProxyMessage.value = `代理测试失败：${error.message}`;
    } finally {
      modelDownloadProxyBusy.value = false;
    }
  }

  /** 测试成功后保存维护代理，并立即更新页面显示的权威状态。 */
  async function saveModelDownloadProxy() {
    if (modelDownloadProxyBusy.value) return;
    modelDownloadProxyBusy.value = true;
    modelDownloadProxyMessage.value = "正在测试并保存维护代理…";
    try {
      const environment = await api("admin/model-download/proxy/save", {
        method: "POST",
        body: JSON.stringify(modelDownloadProxyPayload()),
      });
      if (modelDeployments.value) {
        modelDeployments.value = {
          ...modelDeployments.value,
          download_environment: environment,
        };
      }
      modelDownloadProxyMessage.value = "维护代理已测试并保存，后续目录检查和权重下载会自动使用。";
    } catch (error) {
      modelDownloadProxyMessage.value = `代理保存失败：${error.message}`;
    } finally {
      modelDownloadProxyBusy.value = false;
    }
  }

  /** 清除维护代理；该操作不会修改 Router、Worker 或推理运行时代理。 */
  async function clearModelDownloadProxy() {
    if (modelDownloadProxyBusy.value) return;
    modelDownloadProxyBusy.value = true;
    try {
      const environment = await api("admin/model-download/proxy/clear", {
        method: "POST",
        body: "{}",
      });
      modelDownloadProxyForm.proxy_url = "";
      if (modelDeployments.value) {
        modelDeployments.value = {
          ...modelDeployments.value,
          download_environment: environment,
        };
      }
      modelDownloadProxyMessage.value = "维护代理已清除；后续下载恢复为直连。";
    } catch (error) {
      modelDownloadProxyMessage.value = `代理清除失败：${error.message}`;
    } finally {
      modelDownloadProxyBusy.value = false;
    }
  }

  /**
   * 把提交接口返回的任务立即写入当前快照，避免等待下一次轮询才显示反馈。
   *
   * @param {object|null} job 模型控制服务刚创建的完整任务对象。
   * @returns {boolean} 任务包含有效 ID 且已经写入页面状态时返回真。
   */
  function recordModelDeploymentJob(job) {
    if (!job?.id || !modelDeployments.value) return false;
    const jobs = (modelDeployments.value.jobs || []).filter(
      (item) => item?.id !== job.id,
    );
    modelDeployments.value = {
      ...modelDeployments.value,
      jobs: [job, ...jobs],
    };
    return true;
  }

  /**
   * 在任务卡完成渲染后把它移入视口，让提交结果出现在原操作附近。
   * 浏览器文档对象不存在的测试和服务端环境会安全跳过滚动。
   */
  async function focusActiveModelDeploymentJob() {
    await nextTick();
    globalThis.document
      ?.querySelector("#model-deployment-active-job")
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function modelUpgradePayload() {
    /** 返回两端共用的最小升级契约，拓扑和能力由控制服务权威计算。 */
    const payload = {
      source_deployment_id: String(
        modelUpgradeForm.source_deployment_id || "",
      ).trim(),
      target_hub: String(modelUpgradeForm.target_hub || "").trim(),
      target_model_id: String(modelUpgradeForm.target_model_id || "").trim(),
      target_revision: String(modelUpgradeForm.target_revision || "").trim(),
      max_model_len: Number(modelUpgradeForm.max_model_len),
    };
    if (!payload.source_deployment_id) throw new Error("请选择要升级的 Ornith 部署");
    if (!payload.target_hub) throw new Error("请选择 Ornith 升级目标来源");
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
    modelUpgradeSubmitError.value = "";
    try {
      const payload = modelUpgradePayload();
      if (JSON.stringify(payload) !== modelUpgradePlan.value.request_fingerprint)
        throw new Error("升级参数已变化，请重新生成计划");
      const job = await api("admin/model-upgrades/submit", {
        method: "POST",
        body: JSON.stringify({
          ...payload,
          // 即使表单计划时留空，执行也只使用计划展示过的不可变 SHA。
          target_revision: modelUpgradePlan.value.upgrade.target_revision,
          expected_registry_revision:
            modelUpgradePlan.value.source_registry_revision,
        }),
      });
      modelUpgradePlan.value = null;
      modelUpgradeConfirmed.value = false;
      notify("Ornith 升级任务已进入后台；公开切换前会先执行真实生成", "working");
      await loadModelDeployments({ silent: true });
      if (!(modelDeployments.value?.jobs || []).some((item) => item?.id === job?.id)) {
        // 控制服务已持久化任务，但紧邻提交的快照可能仍由旧请求返回。
        // 仅在权威快照缺少该 ID 时使用提交响应兜底，避免状态卡闪退。
        recordModelDeploymentJob(job);
      }
      await focusActiveModelDeploymentJob();
    } catch (error) {
      modelUpgradeSubmitError.value = `升级提交失败：${error.message}`;
      notify(modelUpgradeSubmitError.value, "bad");
    } finally {
      modelUpgradeSubmitting.value = false;
    }
  }

  const qwen38Quick = reactive({
    busy: qwen38QuickBusy,
    plan: qwen38QuickPlan,
    form: qwen38QuickForm,
    profile: qwen38QuickProfile,
    job: qwen38QuickJob,
    rollbackJob: qwen38QuickRollbackJob,
    deploy: deployQwen38Quick,
    rollback: rollbackQwen38Quick,
  });

  return {
    modelDeployments,
    modelDeploymentsLoading,
    modelDeploymentPlanning,
    modelDeploymentSubmitting,
    modelDeploymentPlan,
    modelDeploymentConfirmed,
    qwen38Quick,
    modelUpgradePlanning,
    modelUpgradeSubmitting,
    modelUpgradePlan,
    modelUpgradeConfirmed,
    modelUpgradeSubmitError,
    modelDownloadProxyBusy,
    modelDownloadProxyMessage,
    modelDownloadProxyForm,
    modelUpgradeForm,
    modelDeploymentMode,
    modelDeploymentForm,
    modelRemoteTargets,
    modelDeploymentRegistry,
    modelDeploymentRows,
    modelDeploymentJobs,
    modelUpgradeProfiles,
    modelUpgradeProfileGroups,
    modelUpgradeUnavailableReason,
    ornithUpgradeSources,
    activeModelDeploymentJob,
    displayedModelUpgradeJob,
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
    applyQwen38FlashNextPreset,
    retryModelDeploymentPublish,
    deploymentJobStateLabel,
    modelDownloadProxyPayload,
    testModelDownloadProxy,
    saveModelDownloadProxy,
    clearModelDownloadProxy,
    modelUpgradePayload,
    planModelUpgrade,
    submitModelUpgrade,
  };
}

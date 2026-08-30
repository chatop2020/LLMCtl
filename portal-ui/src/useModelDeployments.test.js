import { ref } from "vue";
import { describe, expect, it, vi } from "vitest";

import { useModelDeployments } from "./useModelDeployments.js";


describe("模型版本升级组合逻辑", () => {
  it("复用后端目标目录，并把计划版本带回升级提交", async () => {
    const calls = [];
    const snapshot = {
      available: true,
      gateway: { registry_publish: true },
      gpus: [{ id: 0 }, { id: 1 }],
      jobs: [],
      download_environment: {
        maintenance_proxy: {
          configured: false,
          proxy_url: "",
          no_proxy: "127.0.0.1,localhost,::1",
        },
        modelscope: { downloader_ready: false, auto_prepare: true },
      },
      registry: {
        deployments: {
          legacy: {
            id: "legacy",
            enabled: true,
            model_id: "protoLabsAI/Ornith-1.0-35B-FP8",
            instances: [{ kind: "local", enabled: true, worker_id: 0 }],
            runtime: { max_model_len: 262144 },
          },
        },
        artifacts: {},
      },
      upgrade_profiles: [
        {
          id: "modelscope-ornith-1.5",
          hub: "modelscope",
          label: "Ornith 1.5",
          model_id: "ornith-ai/Ornith-1.5-35B-A3B-FP8",
          revision: "",
          recommended: true,
          recommended_max_model_len: 32768,
        },
        {
          id: "huggingface-ornith-1.5-9b",
          hub: "huggingface",
          label: "Ornith 1.5 9B",
          model_id: "ornith-ai/Ornith-1.5-9B",
          revision: "",
        },
      ],
    };
    const api = vi.fn(async (path, options = {}) => {
      calls.push({ path, body: options.body ? JSON.parse(options.body) : null });
      if (path === "admin/model-deployments") return snapshot;
      if (path === "admin/model-upgrades/plan") {
        return {
          source_registry_revision: 7,
          affected_worker_ids: [0, 1],
          warnings: [],
          upgrade: {
            target_revision: "0".repeat(40),
            target_model_id: "ornith-ai/Ornith-1.5-35B-A3B-FP8",
          },
        };
      }
      if (path === "admin/model-upgrades/submit") {
        return { id: "upgrade-job", kind: "upgrade", state: "waiting" };
      }
      throw new Error(`未处理的测试路径：${path}`);
    });
    const notify = vi.fn();
    const state = useModelDeployments({
      api,
      isAdmin: ref(true),
      session: ref({ authenticated: true }),
      notify,
    });

    await state.loadModelDeployments();
    expect(state.modelUpgradeForm.source_deployment_id).toBe("legacy");
    expect(state.modelUpgradeForm.target_model_id).toBe(
      "ornith-ai/Ornith-1.5-35B-A3B-FP8",
    );
    expect(state.modelUpgradeForm.target_hub).toBe("modelscope");
    expect(state.modelUpgradeProfileGroups.value.map((group) => group.label)).toEqual([
      "ModelScope",
      "Hugging Face",
    ]);
    expect(state.modelUpgradeForm.max_model_len).toBe(262144);

    await state.planModelUpgrade();
    state.modelUpgradeConfirmed.value = true;
    await state.submitModelUpgrade();
    const submitted = calls.find((call) => call.path === "admin/model-upgrades/submit");
    expect(submitted.body).toMatchObject({
      source_deployment_id: "legacy",
      target_hub: "modelscope",
      target_model_id: "ornith-ai/Ornith-1.5-35B-A3B-FP8",
      target_revision: "0".repeat(40),
      expected_registry_revision: 7,
    });
    expect(state.modelUpgradePlan.value).toBeNull();
    expect(state.activeModelDeploymentJob.value?.id).toBe("upgrade-job");
    expect(notify).toHaveBeenCalledWith(
      expect.stringContaining("公开切换前会先执行真实生成"),
      "working",
    );
  });

  it("在页面真实测试、保存并清除模型下载代理", async () => {
    const calls = [];
    const api = vi.fn(async (path, options = {}) => {
      calls.push({ path, body: options.body ? JSON.parse(options.body) : null });
      if (path === "admin/model-deployments") {
        return {
          available: true,
          gateway: { registry_publish: true },
          gpus: [],
          jobs: [],
          registry: { deployments: {}, artifacts: {} },
          upgrade_profiles: [],
          download_environment: {
            maintenance_proxy: {
              configured: false,
              proxy_url: "",
              no_proxy: "127.0.0.1,localhost,::1",
            },
            modelscope: { downloader_ready: false, auto_prepare: true },
          },
        };
      }
      if (path.endsWith("/proxy/test")) {
        return { ok: true, hub: "huggingface", proxy_url: "http://proxy:7890" };
      }
      if (path.endsWith("/proxy/save")) {
        return {
          maintenance_proxy: {
            configured: true,
            proxy_url: "http://proxy:7890",
            no_proxy: "127.0.0.1,localhost,::1",
          },
          modelscope: { downloader_ready: false, auto_prepare: true },
        };
      }
      if (path.endsWith("/proxy/clear")) {
        return {
          maintenance_proxy: {
            configured: false,
            proxy_url: "",
            no_proxy: "127.0.0.1,localhost,::1",
          },
          modelscope: { downloader_ready: false, auto_prepare: true },
        };
      }
      throw new Error(`未处理的测试路径：${path}`);
    });
    const state = useModelDeployments({
      api,
      isAdmin: ref(true),
      session: ref({ authenticated: true }),
      notify: vi.fn(),
    });
    await state.loadModelDeployments();
    Object.assign(state.modelDownloadProxyForm, {
      proxy_url: "http://proxy:7890",
      hub: "huggingface",
    });

    await state.testModelDownloadProxy();
    expect(state.modelDownloadProxyMessage.value).toContain("测试通过");
    await state.saveModelDownloadProxy();
    expect(state.modelDeployments.value.download_environment.maintenance_proxy.configured).toBe(true);
    await state.clearModelDownloadProxy();
    expect(state.modelDownloadProxyForm.proxy_url).toBe("");
    expect(calls.map((call) => call.path)).toEqual([
      "admin/model-deployments",
      "admin/model-download/proxy/test",
      "admin/model-download/proxy/save",
      "admin/model-download/proxy/clear",
    ]);
  });

  it("旧模型控制进程缺少升级目录时显示明确恢复命令", async () => {
    const api = vi.fn(async () => ({
      available: true,
      gateway: { registry_publish: true },
      gpus: [{ id: 0 }],
      jobs: [],
      registry: {
        deployments: {
          legacy: {
            id: "legacy",
            enabled: true,
            model_id: "protoLabsAI/Ornith-1.0-35B-FP8",
            runtime: { max_model_len: 262144 },
            instances: [{ kind: "local", enabled: true, worker_id: 0 }],
          },
        },
        artifacts: {},
      },
    }));
    const state = useModelDeployments({
      api,
      isAdmin: ref(true),
      session: ref({ authenticated: true }),
      notify: vi.fn(),
    });

    await state.loadModelDeployments();

    expect(state.modelUpgradeProfiles.value).toEqual([]);
    expect(state.modelUpgradeUnavailableReason.value).toContain(
      "sudo llmctl model init",
    );
    expect(state.modelUpgradeForm.max_model_len).toBe(262144);
  });

  it("发布阶段失败后只重试路由并立即显示新任务", async () => {
    const failed = {
      id: "failed-upgrade",
      kind: "upgrade",
      state: "rolled_back",
      phase: "failed",
      progress: 92,
      message: "OmniRoute 发布失败",
    };
    const snapshot = {
      available: true,
      gateway: { registry_publish: true },
      gpus: [],
      jobs: [failed],
      registry: { deployments: {}, artifacts: {} },
      upgrade_profiles: [],
    };
    const api = vi.fn(async (path) => {
      if (path === "admin/model-deployments") return snapshot;
      if (path === "admin/model-deployments/publish") {
        return {
          id: "publish-retry",
          kind: "publish",
          state: "waiting",
          phase: "waiting",
          progress: 0,
          message: "等待执行",
        };
      }
      throw new Error(`未处理的测试路径：${path}`);
    });
    const notify = vi.fn();
    const state = useModelDeployments({
      api,
      isAdmin: ref(true),
      session: ref({ authenticated: true }),
      notify,
    });
    await state.loadModelDeployments();
    await state.retryModelDeploymentPublish();

    expect(state.activeModelDeploymentJob.value?.id).toBe("publish-retry");
    expect(state.displayedModelUpgradeJob.value?.kind).toBe("publish");
    expect(notify).toHaveBeenCalledWith(
      expect.stringContaining("模型和 Worker 保持运行"),
      "working",
    );
  });

  it("Qwen3.8 预设生成四个同构 TP2 原生 262K 实例", async () => {
    const quick = {
      available: true,
      blockers: [],
      warnings: [],
      model_id: "Inferact/Qwen3.8-Flash-Next-NVFP4",
      revision: "103a7608316173ca6edd49929544244de7ffda70",
      public_model_id: "gdn-inside",
      display_name: "Qwen3.8 Flash Next NVFP4",
      gpu_groups: { groups: [[0, 1], [2, 3], [4, 5], [6, 7]], source: "nvidia-smi" },
      runtime: {
        image: "vllm/vllm-openai:qwen38-flash-next",
        tensor_parallel_size: 2,
        max_model_len: 262144,
        gpu_memory_utilization: 0.92,
        max_num_seqs: 8,
        max_num_batched_tokens: 8192,
        ple_cpu_offload: true,
        enable_expert_parallel: true,
        enable_prefix_caching: false,
        enable_flashinfer_autotune: false,
        disable_custom_all_reduce: false,
        mtp_speculative_tokens: 0,
        kv_cache_dtype: "auto",
        yarn_factor: 1,
        trust_remote_code: false,
        supports_image_input: true,
        supports_ocr: false,
        supports_tool_calling: true,
        supports_reasoning: true,
        supports_thinking_toggle: true,
        tool_call_parser: "qwen3_xml",
        reasoning_parser: "qwen3",
        mm_limit: '{"image":4,"video":0}',
      },
    };
    const notify = vi.fn();
    const calls = [];
    const api = vi.fn(async (path, options = {}) => {
      calls.push({ path, body: options.body ? JSON.parse(options.body) : null });
      if (path === "admin/model-deployments") {
        return {
          available: true,
          gateway: { registry_publish: true },
          gpus: Array.from({ length: 8 }, (_, id) => ({ id })),
          jobs: [],
          registry: { deployments: {}, artifacts: {} },
          upgrade_profiles: [],
          qwen38_quick: quick,
        };
      }
      if (path === "admin/qwen38/plan") return { source_registry_revision: 9 };
      if (path === "admin/qwen38/deploy") {
        return { id: "qwen-job", kind: "qwen38", state: "waiting" };
      }
      throw new Error(`未处理的测试路径：${path}`);
    });
    const state = useModelDeployments({
      api,
      isAdmin: ref(true),
      session: ref({ authenticated: true }),
      notify,
    });
    await state.loadModelDeployments();
    state.applyQwen38FlashNextPreset();
    const payload = state.modelDeploymentPayload();

    expect(payload.model_id).toBe("Inferact/Qwen3.8-Flash-Next-NVFP4");
    expect(payload.revision).toBe("103a7608316173ca6edd49929544244de7ffda70");
    expect(payload.image).toBe("vllm/vllm-openai:qwen38-flash-next");
    expect(payload.tensor_parallel_size).toBe(2);
    expect(payload.instances).toHaveLength(4);
    expect(payload.public_model_id).toBe("gdn-inside");
    expect(payload.instances.map((item) => item.gpu_devices)).toEqual([
      [0, 1],
      [2, 3],
      [4, 5],
      [6, 7],
    ]);
    expect(payload.max_model_len).toBe(262144);
    expect(payload.max_num_seqs).toBe(8);
    expect(payload.ple_cpu_offload).toBe(true);
    expect(payload.enable_expert_parallel).toBe(true);
    expect(payload.enable_prefix_caching).toBe(false);
    expect(payload.mtp_speculative_tokens).toBe(0);
    expect(payload.kv_cache_dtype).toBe("auto");
    expect(payload.yarn_factor).toBe(1);
    expect(notify).toHaveBeenCalledWith(expect.stringContaining("后端核验"));

    await state.qwen38Quick.deploy();
    expect(calls.at(-1)).toMatchObject({
      path: "admin/model-deployments",
    });
    const submitted = calls.find((item) => item.path === "admin/qwen38/deploy");
    expect(submitted.body).toMatchObject({
      expected_registry_revision: 9,
      max_model_len: 262144,
      max_num_seqs: 8,
      mtp_speculative_tokens: 0,
    });
    expect(notify).toHaveBeenCalledWith(
      expect.stringContaining("失败会自动恢复"),
      "working",
    );
  });

  it("Qwen3.8 成功后可从专用入口恢复部署前状态", async () => {
    const sourceJob = {
      id: "qwen-source-job",
      kind: "qwen38",
      state: "succeeded",
      backup: "/backup/qwen-source-job",
    };
    const calls = [];
    const api = vi.fn(async (path, options = {}) => {
      calls.push({ path, body: options.body ? JSON.parse(options.body) : null });
      if (path === "admin/model-deployments") {
        return {
          available: true,
          gateway: { registry_publish: true },
          gpus: [],
          jobs: [sourceJob],
          registry: { deployments: {}, artifacts: {} },
          upgrade_profiles: [],
          qwen38_quick: {
            active: true,
            available: true,
            blockers: [],
            warnings: [],
            rollback_job_id: sourceJob.id,
            runtime: {},
            gpu_groups: { groups: [] },
          },
        };
      }
      if (path === "admin/model-deployments/rollback") {
        return { id: "rollback-job", kind: "rollback", state: "waiting" };
      }
      throw new Error(`未处理的测试路径：${path}`);
    });
    const originalWindow = globalThis.window;
    globalThis.window = { confirm: vi.fn(() => true) };
    try {
      const state = useModelDeployments({
        api,
        isAdmin: ref(true),
        session: ref({ authenticated: true }),
        notify: vi.fn(),
      });
      await state.loadModelDeployments();
      expect(state.qwen38Quick.rollbackJob?.id).toBe(sourceJob.id);
      await state.qwen38Quick.rollback();
      expect(
        calls.find((item) => item.path === "admin/model-deployments/rollback")?.body,
      ).toEqual({ id: sourceJob.id });
    } finally {
      globalThis.window = originalWindow;
    }
  });
});

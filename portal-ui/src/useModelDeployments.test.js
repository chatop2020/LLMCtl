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
});

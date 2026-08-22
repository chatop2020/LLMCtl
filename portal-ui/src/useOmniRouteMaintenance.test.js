import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  OMNIROUTE_MAX_JOB_POLL_FAILURES,
  useOmniRouteMaintenance,
} from "./useOmniRouteMaintenance.js";

describe("OmniRoute 生命周期维护", () => {
  beforeEach(() => {
    globalThis.document = { hidden: false };
    globalThis.window = {
      setTimeout: vi.fn(() => 1),
      clearTimeout: vi.fn(),
    };
  });

  it("读取状态并使用服务端推荐的固定镜像", async () => {
    const api = vi.fn().mockResolvedValue({
      configured_image: "diegosouzapw/omniroute:3.8.48",
      recommended_image: "diegosouzapw/omniroute:3.8.49",
      backups: [],
      jobs: [],
    });
    const state = useOmniRouteMaintenance({ api, notify: vi.fn() });

    await state.loadOmniRouteMaintenance();

    expect(api).toHaveBeenCalledWith("admin/omniroute");
    expect(state.omnirouteForm.update_image).toBe(
      "diegosouzapw/omniroute:3.8.49",
    );
  });

  it("把深度评估作为只读管理员请求提交", async () => {
    const assessment = {
      health: "healthy",
      integrity: { ok: true },
      foreign_keys: { ok: true },
    };
    const api = vi.fn().mockResolvedValueOnce(assessment).mockResolvedValueOnce({
      latest_assessment: assessment,
      backups: [],
      jobs: [],
    });
    const state = useOmniRouteMaintenance({ api, notify: vi.fn() });

    await state.assessOmniRouteSqlite(true);

    expect(api).toHaveBeenNthCalledWith(1, "admin/omniroute/assess", {
      method: "POST",
      body: JSON.stringify({ deep: true }),
    });
    expect(state.omnirouteAssessment.value).toEqual(assessment);
  });

  it("升级、维护和回滚都提交精确确认与受管备份 ID", async () => {
    const api = vi.fn().mockResolvedValue({
      id: "11111111-1111-4111-8111-111111111111",
      state: "waiting",
    });
    const state = useOmniRouteMaintenance({ api, notify: vi.fn() });
    state.omnirouteForm.update_image = "diegosouzapw/omniroute:3.8.49";
    state.omnirouteForm.update_confirmation = "UPDATE OMNIROUTE";

    await state.updateOmniRoute();

    expect(api).toHaveBeenCalledWith("admin/omniroute/submit", {
      method: "POST",
      body: JSON.stringify({
        action: "update",
        image: "diegosouzapw/omniroute:3.8.49",
        confirmation: "UPDATE OMNIROUTE",
      }),
    });

    state.omnirouteJob.value = null;
    state.selectOmniRouteBackup("upgrade-20260822t000000z-12345678");
    state.omnirouteForm.rollback_confirmation =
      "ROLLBACK upgrade-20260822t000000z-12345678";
    await state.rollbackOmniRoute();
    expect(api).toHaveBeenLastCalledWith("admin/omniroute/submit", {
      method: "POST",
      body: JSON.stringify({
        action: "rollback",
        backup_id: "upgrade-20260822t000000z-12345678",
        confirmation: "ROLLBACK upgrade-20260822t000000z-12345678",
      }),
    });
  });

  it("任务接口短暂失败时从总快照恢复，不误报任务失败", async () => {
    const activeJob = {
      id: "22222222-2222-4222-8222-222222222222",
      state: "running",
      phase: "starting",
      progress: 80,
      message: "恢复 Router 并执行完整模型冒烟",
    };
    const api = vi.fn(async (path) => {
      if (path === "admin/omniroute/job") throw new Error("HTTP 502");
      return { active_job: activeJob, jobs: [activeJob], backups: [] };
    });
    const notify = vi.fn();
    const state = useOmniRouteMaintenance({ api, notify });
    state.omnirouteJob.value = activeJob;

    const result = await state.pollOmniRouteJob(activeJob.id);

    expect(result).toEqual(activeJob);
    expect(state.omnirouteJobActive.value).toBe(true);
    expect(state.omniroutePollFailures.value).toBe(0);
    expect(state.omniroutePollingPaused.value).toBe(false);
    expect(notify).not.toHaveBeenCalledWith(expect.stringContaining("读取失败"), "bad");
  });

  it("账户门户维护中持续 502 时保留后台任务并自动重试", async () => {
    const activeJob = {
      id: "33333333-3333-4333-8333-333333333333",
      state: "running",
      phase: "starting",
      progress: 80,
    };
    const api = vi.fn().mockRejectedValue(new Error("HTTP 502"));
    const notify = vi.fn();
    const state = useOmniRouteMaintenance({ api, notify });
    state.omnirouteJob.value = activeJob;

    await state.pollOmniRouteJob(activeJob.id);

    expect(state.omnirouteJob.value).toEqual(activeJob);
    expect(state.omnirouteJobActive.value).toBe(true);
    expect(state.omniroutePollFailures.value).toBe(1);
    expect(state.omniroutePollError.value).toBe("HTTP 502");
    expect(state.omniroutePollingPaused.value).toBe(false);
    expect(notify).not.toHaveBeenCalled();
  });

  it("活动任务期间手动刷新失败时说明任务未被取消", async () => {
    const activeJob = {
      id: "66666666-6666-4666-8666-666666666666",
      state: "running",
      phase: "starting",
      progress: 80,
    };
    const api = vi.fn().mockRejectedValue(new Error("HTTP 502"));
    const notify = vi.fn();
    const state = useOmniRouteMaintenance({ api, notify });
    state.omnirouteJob.value = activeJob;

    await state.loadOmniRouteMaintenance();

    expect(notify).toHaveBeenCalledWith(
      expect.stringContaining("后台任务未被取消"),
      "working",
    );
    expect(state.omnirouteJob.value).toEqual(activeJob);
  });

  it("长时间不可读后提供手动恢复，并在恢复后读取真实终态", async () => {
    const activeJob = {
      id: "44444444-4444-4444-8444-444444444444",
      state: "running",
      phase: "starting",
      progress: 80,
    };
    const succeededJob = {
      ...activeJob,
      state: "succeeded",
      phase: "succeeded",
      progress: 100,
      message: "OmniRoute 升级完成",
    };
    const api = vi.fn().mockRejectedValue(new Error("HTTP 502"));
    const notify = vi.fn();
    const state = useOmniRouteMaintenance({ api, notify });
    state.omnirouteJob.value = activeJob;

    for (let attempt = 0; attempt < OMNIROUTE_MAX_JOB_POLL_FAILURES; attempt += 1) {
      await state.pollOmniRouteJob(activeJob.id);
    }

    expect(state.omniroutePollingPaused.value).toBe(true);
    expect(state.omnirouteJobActive.value).toBe(true);
    expect(notify).toHaveBeenCalledWith(
      expect.stringContaining("后台任务未被取消"),
      "bad",
    );

    expect(state.resumeOmniRoutePolling()).toBe(true);
    expect(state.omniroutePollingPaused.value).toBe(false);
    expect(state.omniroutePollFailures.value).toBe(0);
    api.mockReset().mockResolvedValueOnce(succeededJob).mockResolvedValueOnce({
      active_job: null,
      jobs: [succeededJob],
      backups: [],
    });

    await state.pollOmniRouteJob(activeJob.id);

    expect(state.omnirouteJob.value).toEqual(succeededJob);
    expect(state.omnirouteJobActive.value).toBe(false);
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";

import { useOmniRouteMaintenance } from "./useOmniRouteMaintenance.js";

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
});

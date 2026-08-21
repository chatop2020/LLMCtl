import { beforeEach, describe, expect, it, vi } from "vitest";

import { usePendingUserActions } from "./usePendingUserActions.js";

describe("未验证用户管理操作", () => {
  beforeEach(() => {
    globalThis.document = {
      querySelector: vi.fn(() => ({ showModal: vi.fn(), close: vi.fn() })),
    };
  });

  it("补发验证邮件时使用管理员对象级接口", async () => {
    const api = vi.fn().mockResolvedValue({ ok: true });
    const action = vi.fn(async (operation) => operation());
    const state = usePendingUserActions({ api, action, notify: vi.fn() });

    await state.resendUserVerification({
      id: "pending-1",
      email: "pending@example.com",
      status: "pending",
    });
    expect(api).toHaveBeenCalledWith("admin/users/verification/resend", {
      method: "POST",
      body: JSON.stringify({ user_id: "pending-1" }),
    });
  });

  it("只有完整邮箱确认匹配后才提交手动通过", async () => {
    const api = vi.fn().mockResolvedValue({ ok: true });
    const action = vi.fn(async (operation) => operation());
    const notify = vi.fn();
    const state = usePendingUserActions({ api, action, notify });
    state.openPendingUserApproval({
      id: "pending-approve",
      email: "approve@example.com",
    });

    expect(await state.approvePendingUser()).toBeNull();
    expect(api).not.toHaveBeenCalled();
    state.pendingUserApproval.confirmation = "approve@example.com";
    await state.approvePendingUser();
    expect(api).toHaveBeenCalledWith("admin/users/verification/approve", {
      method: "POST",
      body: JSON.stringify({
        user_id: "pending-approve",
        confirmation_email: "approve@example.com",
      }),
    });
  });

  it("只有完整邮箱确认匹配后才提交删除", async () => {
    const api = vi.fn().mockResolvedValue({ ok: true });
    const action = vi.fn(async (operation) => operation());
    const notify = vi.fn();
    const state = usePendingUserActions({ api, action, notify });
    state.openPendingUserDelete({ id: "pending-2", email: "two@example.com" });

    expect(await state.deletePendingUser()).toBeNull();
    expect(api).not.toHaveBeenCalled();
    state.pendingUserDelete.confirmation = "two@example.com";
    await state.deletePendingUser();
    expect(api).toHaveBeenCalledWith("admin/users/pending/delete", {
      method: "POST",
      body: JSON.stringify({ user_id: "pending-2" }),
    });
  });
});

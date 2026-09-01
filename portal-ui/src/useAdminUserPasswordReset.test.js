import { afterEach, describe, expect, it, vi } from "vitest";

import { useAdminUserPasswordReset } from "./useAdminUserPasswordReset.js";


afterEach(() => {
  vi.unstubAllGlobals();
});


describe("管理员用户密码重置", () => {
  it("成功后清空明文，并只向受审计接口发送目标和两次输入", async () => {
    const close = vi.fn();
    const showModal = vi.fn();
    vi.stubGlobal("document", {
      querySelector: vi.fn(() => ({ close, showModal })),
    });
    const api = vi.fn(async () => ({
      ok: true,
      user_id: "user-1",
      sessions_revoked: 2,
    }));
    const action = vi.fn(async (operation) => operation());
    const notify = vi.fn();
    const reset = useAdminUserPasswordReset({ api, action, notify });

    reset.openUserPasswordReset({ id: "user-1", email: "user@example.com" });
    reset.userPasswordReset.password = "ReplacementPass456";
    reset.userPasswordReset.confirm = "ReplacementPass456";
    const result = await reset.resetUserPassword();

    expect(result.sessions_revoked).toBe(2);
    expect(api).toHaveBeenCalledWith("admin/users/password/reset", {
      method: "POST",
      body: JSON.stringify({
        user_id: "user-1",
        password: "ReplacementPass456",
        confirm: "ReplacementPass456",
      }),
    });
    expect(reset.userPasswordReset).toEqual({
      user_id: "",
      email: "",
      password: "",
      confirm: "",
    });
    expect(showModal).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
  });

  it("确认不一致时不调用接口并保留输入供管理员修正", async () => {
    const api = vi.fn();
    const action = vi.fn();
    const notify = vi.fn();
    const reset = useAdminUserPasswordReset({ api, action, notify });
    Object.assign(reset.userPasswordReset, {
      user_id: "user-1",
      email: "user@example.com",
      password: "ReplacementPass456",
      confirm: "DifferentPass789",
    });

    expect(await reset.resetUserPassword()).toBeNull();
    expect(api).not.toHaveBeenCalled();
    expect(action).not.toHaveBeenCalled();
    expect(notify).toHaveBeenCalledWith("两次输入的密码不一致", "bad");
    expect(reset.userPasswordReset.password).toBe("ReplacementPass456");
  });
});

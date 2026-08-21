import { describe, expect, it, vi } from "vitest";

import { useAdminApiKeys } from "./useAdminApiKeys.js";

describe("管理员按需查看用户 API Key", () => {
  it("只在内存展示一个用户的当前 Key，并支持复制和隐藏", async () => {
    const api = vi
      .fn()
      .mockResolvedValueOnce({ api_key: "sk-current-user-one-abcdefghijklmnopqrstuvwxyz" })
      .mockResolvedValueOnce({ api_key: "sk-current-user-two-abcdefghijklmnopqrstuvwxyz" });
    const copy = vi.fn().mockResolvedValue(true);
    const notify = vi.fn();
    const state = useAdminApiKeys({ api, copy, notify });
    const first = { id: "user-1", email: "one@example.com", api_key_id: "key-1" };
    const second = { id: "user-2", email: "two@example.com", api_key_id: "key-2" };

    await state.revealAdminUserApiKey(first);
    expect(state.adminUserApiKeys["user-1"]).toContain("sk-current-user-one");
    await state.copyAdminUserApiKey(first);
    expect(copy).toHaveBeenCalledWith(
      "sk-current-user-one-abcdefghijklmnopqrstuvwxyz",
    );

    await state.revealAdminUserApiKey(second);
    expect(state.adminUserApiKeys["user-1"]).toBeUndefined();
    expect(state.adminUserApiKeys["user-2"]).toContain("sk-current-user-two");
    state.hideAdminUserApiKey("user-2");
    expect(state.adminUserApiKeys["user-2"]).toBeUndefined();
  });

  it("用户没有 Key 时不调用管理员揭示接口", async () => {
    const api = vi.fn();
    const notify = vi.fn();
    const state = useAdminApiKeys({ api, copy: vi.fn(), notify });

    expect(await state.revealAdminUserApiKey({ id: "pending-user" })).toBe("");
    expect(api).not.toHaveBeenCalled();
    expect(notify).toHaveBeenCalledWith("该用户尚未配置 API Key", "bad");
  });
});

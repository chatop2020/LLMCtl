import { describe, expect, it } from "vitest";

import { auditActionLabel, formatAuditDetail } from "./auditDisplay.js";

describe("审计日志展示", () => {
  it("把常见内部动作翻译为管理员可理解的说明", () => {
    expect(auditActionLabel("admin.usage.detail.view")).toBe(
      "管理员查看请求详情",
    );
    expect(auditActionLabel("admin/users/key/reveal")).toBe(
      "管理员查看用户 API Key",
    );
    expect(auditActionLabel("custom.action")).toBe("custom.action");
  });

  it("完整格式化 JSON、普通文本和空详情", () => {
    const longValue = "x".repeat(600);
    const formatted = formatAuditDetail(
      JSON.stringify({ ok: true, nested: { longValue } }),
    );
    expect(formatted).toContain('\n  "nested"');
    expect(formatted).toContain(longValue);
    expect(formatAuditDetail("plain detail")).toBe("plain detail");
    expect(formatAuditDetail("")).toBe("无额外详情");
  });
});

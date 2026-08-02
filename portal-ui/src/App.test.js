import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = readFileSync(new URL("./App.vue", import.meta.url), "utf8");
const style = readFileSync(new URL("./style.css", import.meta.url), "utf8");

describe("LLMCtl portal contracts", () => {
  it("sends inference directly to the public v1 endpoint", () => {
    expect(source).toContain("`${dashboard.value.api_base}/chat/completions`");
    expect(source).not.toContain('api("chat');
  });

  it("supports native model mapping, metadata sync, access rules and prices", () => {
    for (const marker of [
      "public_model_id",
      "admin?.combos",
      "addModelAccess",
      "input_price",
      "reasoning_price",
      "context_window_tokens",
      "max_output_tokens",
      "sync_context_window",
      "admin/models/inspect",
      "模型原生参数",
    ])
      expect(source).toContain(marker);
    expect(source).toContain('model.description || "未填写模型描述"');
  });

  it("keeps registration, SMTP, free resources and audit in the admin UI", () => {
    for (const marker of ["注册与 SMTP", "免费资源", "用户管理", "审计日志"]) {
      expect(source).toContain(marker);
    }
    expect(source).toMatch(/scope:\s*["']registration["']/);
    expect(source).toMatch(/scope:\s*["']smtp["']/);
    expect(source).toContain("测试邮件使用当前表单内容，不必先保存");
  });

  it("gives scoped progress feedback and a non-blocking dismissible toast", () => {
    expect(source).toContain("正在执行真实模型请求，最长约 60 秒");
    expect(source).toContain("正在测试模型、更新映射并同步权限");
    expect(source).toContain("dismissToast");
    expect(style).toMatch(/\.toast\s*\{[\s\S]*bottom:\s*22px/);
    expect(style).not.toMatch(/\.toast\s*\{[\s\S]{0,120}top:\s*92px/);
  });

  it("copies keys through a tested browser fallback and never claims success early", () => {
    expect(source).toContain("writeClipboardText");
    expect(source).toContain("result.copied");
    expect(source).toContain("revealOnFailure");
    expect(source).not.toContain("navigator.clipboard.writeText(value)");
  });

  it("filters and paginates every growing catalog and ledger", () => {
    for (const key of [
      "user-models",
      "user-grants",
      "user-billing",
      "admin-models",
      "admin-free",
      "admin-users",
      "admin-groups",
      "admin-billing",
      "admin-audit",
    ]) {
      expect(source).toMatch(new RegExp(`pageRows\\(\\s*["']${key}["']`));
      expect(source).toContain(`listFilters['${key}']`);
    }
    expect(source).toContain("usage-page?");
    expect(source).toContain("usage_pagination?.total");
    expect(source).toContain("applyUsageFilters");
    expect(source).toContain(
      "第 ${props.page} / ${props.pages} 页 · 共 ${props.total} 条",
    );
  });

  it("refreshes role data on navigation and humanizes account editing", () => {
    expect(source).toContain('@click="selectSection(item[0])"');
    expect(source).toContain("workspaceLoadVersion");
    expect(source).toContain("workspaceRefreshing");
    expect(source).toContain('class="choice-group"');
    expect(source).toContain('<option value="active">正常</option>');
    expect(source).toContain('<option value="none">仅本次</option>');
    expect(source).toContain('api(isAdmin.value ? "admin/billing/reconcile" : "usage/reconcile"');
    expect(source).toContain("usageRefreshTimer = window.setInterval");
    expect(source).toContain("reconcile: !isAdmin.value");
    expect(source).toContain("clearAuthenticatedClientState()");
    expect(source).toContain(
      "for (const key of Object.keys(requestDetails)) delete requestDetails[key]",
    );
  });

  it("marks disabled models and keeps compact status badges readable", () => {
    expect(source).toContain("'model-row-disabled': model.status === 'disabled'");
    expect(source).toContain('v-if="model.status === \'disabled\'"');
    expect(source).toContain("最近测试：{{ statusLabel(model.health_status) }}");
    expect(style).toMatch(/\.status\s*\{[\s\S]*?white-space:\s*nowrap/);
    expect(style).toContain(".resource-head > div");
    expect(style).toContain("text-overflow: ellipsis");
  });

  it("shows user and administrator request contents and honest empty ledgers", () => {
    expect(source).toContain("toggleRequestDetail(row)");
    expect(source).toContain('`${isAdmin.value ? "admin/" : ""}usage/');
    expect(source).toContain("暂无金额流水；现有请求可能全部由赠送 Token 抵扣");
    expect(source).toContain("尚无请求用量；点击“同步用量”");
    expect(source).toContain("模型输出 <small>仅管理员可见</small>");
    expect(source).toContain("response_messages");
    expect(source).toContain("该请求没有保留可显示的文本内容");
  });

  it("keeps local administration visible when the AI gateway is degraded", () => {
    expect(source).toContain("admin?.gateway_error");
    expect(source).toContain("用户、SMTP、账本和审计仍可查看");
  });

  it("uses public-project LLMCtl language and a light operations-console visual system", () => {
    for (const marker of [
      "OmniRoute",
      "独立 SQLite",
      "企业 AI 门户",
      "公司模型",
    ]) {
      expect(source).not.toContain(marker);
    }
    expect(source).toContain("LLMCtl 模型服务门户");
    expect(style).toContain("--bg: #f4f9fc");
    expect(style).toContain("background: #fffffff2");
  });
});

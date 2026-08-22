import { readFileSync, readdirSync } from "node:fs";
import { compileTemplate, parse } from "@vue/compiler-sfc";
import { describe, expect, it } from "vitest";

// 门户已经按页面和组合逻辑拆分；契约检查必须覆盖真实生产源码，同时排除
// 测试文件本身，避免断言文本让检查产生假阳性。
const componentSources = readdirSync(new URL("./components/", import.meta.url))
  .filter((name) => name.endsWith(".vue"))
  .map((name) => readFileSync(new URL(`./components/${name}`, import.meta.url), "utf8"));
const appSource = readFileSync(new URL("./App.vue", import.meta.url), "utf8");
const adminCoreSource = readFileSync(
  new URL("./components/PortalAdminCorePages.vue", import.meta.url),
  "utf8",
);
const source = [
  appSource,
  readFileSync(new URL("./auditDisplay.js", import.meta.url), "utf8"),
  readFileSync(new URL("./useAdminApiKeys.js", import.meta.url), "utf8"),
  readFileSync(new URL("./useModelDeployments.js", import.meta.url), "utf8"),
  readFileSync(new URL("./useOmniRouteMaintenance.js", import.meta.url), "utf8"),
  readFileSync(new URL("./usePendingUserActions.js", import.meta.url), "utf8"),
  readFileSync(new URL("./portalWorkspaceContext.js", import.meta.url), "utf8"),
  ...componentSources,
].join("\n");
const style = ["style.css", "operations-theme.css"]
  .map((name) => readFileSync(new URL(`./${name}`, import.meta.url), "utf8"))
  .join("\n");

describe("LLMCtl portal contracts", () => {
  it("provides every root field referenced by the split admin page", () => {
    // 编译后的 `_ctx` 访问就是拆分页面向组合根索取的真实字段。把它与
    // provide 对象逐项核对，可在构建前阻止只在任务、空态等稀有分支触发的白屏。
    const descriptor = parse(adminCoreSource).descriptor;
    const compiled = compileTemplate({
      source: descriptor.template.content,
      filename: "PortalAdminCorePages.vue",
      id: "portal-admin-context-contract",
    }).code;
    const referenced = new Set(
      [...compiled.matchAll(/_ctx\.([A-Za-z_$][\w$]*)/g)].map((match) => match[1]),
    );
    const providerBody = appSource.match(
      /provide\(PORTAL_WORKSPACE_KEY,\s*\{([\s\S]*?)\n\}\);/,
    )?.[1];
    expect(providerBody).toBeTruthy();
    const provided = new Set(
      [...providerBody.matchAll(/^\s*([A-Za-z_$][\w$]*),\s*$/gm)].map(
        (match) => match[1],
      ),
    );

    expect([...referenced].filter((name) => !provided.has(name)).sort()).toEqual([]);
  });

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
    expect(source).toContain("MAX_OUTPUT_TOKENS_LIMIT = 32768");
    expect(source).toContain(':max="MAX_OUTPUT_TOKENS_LIMIT"');
  });

  it("keeps registration, SMTP, free resources and audit in the admin UI", () => {
    for (const marker of ["注册与 SMTP", "免费资源", "用户管理", "审计日志"]) {
      expect(source).toContain(marker);
    }
    expect(source).toMatch(/scope:\s*["']registration["']/);
    expect(source).toMatch(/scope:\s*["']smtp["']/);
    expect(source).toMatch(/scope:\s*["']publishing["']/);
    expect(source).toContain("portal_title");
    expect(source).toContain("published_origin");
    expect(source).toContain("https://llm.zjguardian.com");
    expect(source).toContain("测试邮件使用当前表单内容，不必先保存");
    expect(source).toContain("default_max_sessions");
    expect(source).toContain("userEdit.max_sessions");
    expect(source).toContain("原生 maxSessions");
    expect(source).toContain("default_requests_per_minute");
    expect(source).toContain("userEdit.requests_per_minute");
    expect(source).toContain("允许注册邮箱");
    expect(source).toContain("allowedRegistrationEmails");
  });

  it("reveals a managed user's current API Key only after an admin action", () => {
    expect(source).toContain('api("admin/users/key/reveal"');
    expect(source).toContain("adminUserApiKeys[user.id]");
    expect(source).toContain("hideAdminUserApiKey");
    expect(source).toContain("copyAdminUserApiKey");
    expect(source).toContain("••••••••••••••••");
    expect(source).not.toContain('sessionStorage.setItem("admin_user_api_key"');
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
      "admin-stress",
      "admin-audit",
    ]) {
      expect(source).toMatch(new RegExp(`pageRows\\(\\s*["']${key}["']`));
      expect(source).toContain(`listFilters['${key}']`);
    }
    expect(source).toContain("usage-page?");
    expect(source).toContain("usage_pagination?.total");
    expect(source).toContain("applyUsageFilters");
    expect(source).toContain(
      "第 {{ page }} / {{ pages }} 页 · 共 {{ total }} 条",
    );
  });

  it("refreshes role data on navigation and humanizes account editing", () => {
    expect(source).toContain('@click="selectSection(item[0])"');
    expect(source).toContain("workspaceLoadVersion");
    expect(source).toContain("workspaceRefreshing");
    expect(source).toContain('class="choice-group"');
    expect(source).toContain('<option value="active">正常</option>');
    expect(source).toContain("新用户一次性赠送金额（USD）");
    expect(source).toContain("历史 Token 赠额已一次性折现");
    expect(source).toContain('api(isAdmin.value ? "admin/billing/reconcile" : "usage/reconcile"');
    expect(source).toContain("usageRefreshTimer = window.setInterval");
    expect(source).toContain("reconcile: !isAdmin.value");
    expect(source).toContain("clearAuthenticatedClientState()");
    expect(source).toContain(
      "for (const key of Object.keys(requestDetails)) delete requestDetails[key]",
    );
  });

  it("distinguishes email verification from API Key permission synchronization", () => {
    expect(source).toContain("邮箱未验证");
    expect(source).toContain("验证邮箱后自动创建");
    expect(source).toContain("等待邮箱验证");
    expect(source).toContain("验证后自动创建 Key 并同步权限");
    expect(source).toContain("等待权限同步");
  });

  it("keeps desktop navigation fixed and removes the redundant routing note", () => {
    expect(source).not.toContain("请求直达推理 API");
    expect(style).toContain("height: calc(100vh - 58px)");
    expect(style).toContain("overflow-y: auto");
    expect(style).toContain("overscroll-behavior: contain");
    expect(style).toMatch(
      /@media \(max-width: 700px\)[\s\S]*?\.sidebar\s*\{[\s\S]*?height:\s*auto;[\s\S]*?overflow-x:\s*auto;[\s\S]*?overflow-y:\s*hidden;/,
    );
  });

  it("expands complete audit details with human-readable action names", () => {
    expect(source).toContain("auditActionLabel(row.action)");
    expect(source).toContain("formatAuditDetail(row.detail)");
    expect(source).toContain("查看完整详情");
    expect(source).toContain("管理员查看请求详情");
    expect(style).toContain(".audit-detail pre");
    expect(style).toContain("max-height: 60vh");
  });

  it("moves historical stress-test selection to the result that changed", () => {
    expect(source).toContain('id="stress-run-detail"');
    expect(source).toContain("selectStressRun(run)");
    expect(source).toContain('scrollIntoView({');
    expect(source).toContain("当前查看");
    expect(source).toContain("查看详情");
  });

  it("exposes OmniRoute upgrade and SQLite maintenance through one operations page", () => {
    expect(source).toContain('["omniroute", "OmniRoute 维护"]');
    expect(source).toContain("admin/omniroute/submit");
    expect(source).toContain("UPDATE OMNIROUTE");
    expect(source).toContain("MAINTAIN ONLINE");
    expect(source).toContain("COMPACT SQLITE");
    expect(source).toContain("备份、升级并自动验收");
    expect(source).toContain("备份当前状态并执行回滚");
    expect(source).toContain("账户门户正在恢复，系统会自动重试");
    expect(source).toContain("重新读取任务");
    expect(source).toContain("账户门户恢复连接后才能提交取消请求");
    expect(source).toContain("后台任务未被取消，系统会继续重试");
  });

  it("为待验证用户提供手动通过、补发和安全删除操作", () => {
    expect(source).toContain("admin/users/verification/approve");
    expect(source).toContain("admin/users/verification/resend");
    expect(source).toContain("admin/users/pending/delete");
    expect(source).toContain("手动通过验证");
    expect(source).toContain("输入完整邮箱确认手动通过");
    expect(source).toContain("补发验证邮件");
    expect(source).toContain("删除未验证用户");
    expect(source).toContain("输入完整邮箱确认删除");
  });

  it("确保用户操作按钮位于标准表格单元格内部", () => {
    expect(source).toContain('<td class="user-row-actions-cell">');
    expect(source).toContain('<div class="user-row-actions">');
    expect(style).toMatch(/\.user-row-actions-cell\s*\{[\s\S]*?min-width:/);
    expect(style).toMatch(/\.user-row-actions\s*\{[\s\S]*?display:\s*flex/);
    expect(style).not.toMatch(/td\.user-row-actions\s*\{[\s\S]*?display:\s*flex/);
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
    expect(source).toContain("注册赠款、管理员调整、旧 Token 折现和每次模型调用扣费");
    expect(source).toContain("尚无请求用量；点击“同步用量”");
    expect(source).toContain("模型输出 <small>仅管理员可见</small>");
    expect(source).toContain("response_messages");
    expect(source).toContain("该请求产生时未开启详细日志");
    expect(source).toContain("历史输入无法补录");
    expect(source).toContain("1,000,000 个字符");
    expect(source).toContain("现金余额");
    expect(source).toContain("历史 Token 赠额折现");
    expect(source).toContain("cashTokenCapacity");
  });

  it("keeps local administration visible when the AI gateway is degraded", () => {
    expect(source).toContain("admin?.gateway_error");
    expect(source).toContain("用户、SMTP、账本和审计仍可查看");
  });

  it("labels token usage, supports attachments, and delegates stress to the backend", () => {
    for (const marker of ["输入 Token", "输出 Token", "合计 Token"]) {
      expect(source).toContain(marker);
    }
    expect(source).toContain("prepareAttachment");
    expect(source).toContain("buildUserContent");
    expect(source).toContain('api("admin/stress/start"');
    expect(source).toContain('api("admin/stress/cancel"');
    expect(source).toContain("后台执行真实流式请求");
    expect(source).toContain("路由分布");
    expect(source).toContain("GPU 并行负载");
    expect(source).toContain("peak_concurrent_active_gpu_count");
    expect(source).toContain("item.route_target");
    expect(source).toContain("的新计划尚未启动");
    expect(source).toContain("selectedStressRun.request_multiplier");
    expect(source).not.toContain("每个并发 Worker");
    expect(source).not.toContain("Promise.all(Array(stressPlan.concurrency)");
  });

  it("keeps the registered API key stable and embeds it in curl examples", () => {
    expect(source).toContain('api("key/reveal"');
    expect(source).toContain("只有手工轮换才会更换");
    expect(source).toContain("登录不会创建或更换 Key");
    expect(source).toContain("Authorization: Bearer ${apiKey}");
    expect(source).not.toContain("尚未保存，请输入或前往 API Key 页面轮换");
  });

  it("builds source-backed operations analytics without double-counting token subsets", () => {
    for (const marker of [
      "admin/analytics?",
      "adminAnalytics.summary.total_tokens",
      "adminAnalytics.timeseries",
      "adminAnalytics.top_users",
      "adminAnalytics.active_users",
      "adminAnalytics.selected_user",
      "settlement_lag_seconds",
      "输入 + 输出，不重复计算缓存与思考",
      "今日（按小时）",
      "近 7 天（按天）",
      "近 30 天（按天）",
      "近 12 个月（按月）",
      "analyticsRefreshTimer",
    ])
      expect(source).toContain(marker);
    expect(style).toContain(".usage-chart");
    expect(style).toContain(".ranking-list");
  });

  it("supports explicit audited bulk updates for mutable user call policies", () => {
    for (const marker of [
      "admin/users/bulk-policy",
      "selectedUserIds",
      "filteredAdminUsers",
      "bulkTargetUsers",
      "change_max_sessions",
      "change_requests_per_minute",
      "change_requests_per_day",
      "批量修改调用策略",
      "现金余额、账户状态、用户组和模型权限不会被修改",
    ])
      expect(source).toContain(marker);
    expect(style).toContain(".bulk-policy-dialog");
    expect(style).toContain(".selection-column");
  });

  it("offers an admin-only read-only system monitor without continuous background polling", () => {
    for (const marker of [
      '["monitoring", "系统监控"]',
      'api("admin/system-monitor")',
      'section.value !== "monitoring"',
      "monitorPaused.value",
      "最多保留 60 次",
      "命令参数中的 Key、Token 和密码会自动脱敏",
      ':pages="monitorProcessPages"',
      "GPU 状态",
      "网络接口",
      "本地持久文件系统使用情况",
    ])
      expect(source).toContain(marker);
    expect(source).toContain("monitorRefreshTimer = window.setInterval");
    expect(source).toContain("document.hidden");
  });

  it("separates CPU and memory from per-GPU small-multiple trends", () => {
    expect(source).toContain("latestMonitorTrendValue");
    expect(source).toContain("const verticalPadding = 2");
    expect(source).toContain("monitorHistory.gpus");
    expect(source).toContain("monitorGpuTrends");
    expect(source).toContain("GPU 并行趋势");
    expect(source).toContain('v-for="gpu in monitorGpuTrends"');
    expect(source).not.toContain('<polyline class="gpu"');
    expect(style).toMatch(/polyline\.cpu\s*\{[\s\S]*stroke-dasharray:\s*6 3/);
    expect(style).toContain(".gpu-trend-grid");
    expect(style).toContain(".gpu-mini-chart");
  });

  it("keeps pluggable orchestration explicit, remote-capable and off the Python data path", () => {
    for (const marker of [
      "admin/workflow",
      "admin/workflow/config",
      "admin/workflow/publish",
      "gateway_base_url",
      "同步到 AI 网关",
      "llmctl-workflow-*",
      "另一台服务器或独立 GPU 集群",
      "allowed_purposes",
      "工具参数 JSON Schema",
      "request_body_limit_bytes",
      "upstream_timeout_ms",
      "密钥环境变量",
      "推理和流式响应由 Go",
      "不会自动覆盖当前生产映射",
    ])
      expect(source).toContain(marker);
    expect(source).toContain("await Promise.all([refreshWorkspace(), loadWorkflow()])");
  });

  it("offers the same pinned Ornith upgrade and rollback contract as llmctl", () => {
    for (const marker of [
      "Ornith 版本升级",
      "admin/model-upgrades/plan",
      "admin/model-upgrades/submit",
      "source_registry_revision",
      "固定 SHA",
      "公开切换前执行真实生成",
      "确认升级并保留回退点",
      "回退到升级前",
      "模型部署控制服务仍在运行旧版本",
      "source.runtime?.max_model_len",
      "modelUpgradeProfileGroups",
      "profile.model_id",
      "target_hub",
      "managed_runtime_corrected_count",
      "接入层旧值",
    ])
      expect(source).toContain(marker);
  });

  it("uses readable analytics tables and consistent accessible choice controls", () => {
    expect(source).toContain('class="table-wrap active-users-table"');
    expect(source).not.toContain('class="table-wrap compact-table">\n                    <table>\n                      <thead>\n                        <tr>\n                          <th>用户</th>');
    expect(source.match(/class="choice-control"/g)?.length).toBeGreaterThanOrEqual(10);
    expect(style).toContain(".active-users-table td");
    expect(style).toContain(".choice-control:checked");
    expect(style).toContain('.choice-control[type="radio"]');
    expect(style).toContain(".bulk-scope > label:has(.choice-control:checked)");
  });

  it("uses public-project LLMCtl language and a light operations-console visual system", () => {
    const userFacingSource = readFileSync(
      new URL("./components/PortalUserPages.vue", import.meta.url),
      "utf8",
    );
    for (const marker of [
      "OmniRoute",
      "独立 SQLite",
      "企业 AI 门户",
      "公司模型",
    ]) {
      expect(userFacingSource).not.toContain(marker);
    }
    expect(source).toContain("{{ portalTitle }} 模型服务门户");
    expect(source).toContain("document.title = `${title} 模型服务门户`");
    expect(style).toContain("--bg: #f4f9fc");
    expect(style).toContain("background: #fffffff2");
  });
});

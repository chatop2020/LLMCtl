<script setup>
import {
  computed,
  h,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from "vue";
import {
  chunkParts,
  consumeChatResponse,
  splitThinkingMarkup,
} from "./chatStream.js";
import {
  ACCEPTED_ATTACHMENTS,
  MAX_TOTAL_ATTACHMENT_BYTES,
  MAX_VISUAL_INPUTS,
  attachmentKind,
  buildUserContent,
  prepareAttachment,
  visualInputCount,
} from "./attachments.js";
import { writeClipboardText } from "./clipboard.js";

const session = ref(null);
const publicConfig = ref({
  registration_enabled: false,
  allowed_domains: [],
  portal_title: "LLMCtl",
});
const dashboard = ref(null);
const admin = ref(null);
const adminAnalytics = ref(null);
const workflow = ref(null);
const workflowLoading = ref(false);
const workflowSaving = ref(false);
const workflowPublishing = ref(false);
const analyticsLoading = ref(false);
const busy = ref(false);
const operation = ref("");
const workspaceRefreshing = ref(false);
const usageRefreshing = ref(false);
const toast = reactive({ text: "", kind: "ok" });
let toastTimer = null;
let workspaceLoadVersion = 0;
let usageRefreshTimer = null;
let stressRefreshTimer = null;
let analyticsRefreshTimer = null;
let analyticsLoadVersion = 0;
const authMode = ref(
  location.hash.startsWith("#/register")
    ? "register"
    : location.hash.startsWith("#/verify")
      ? "verify"
      : "login",
);
const section = ref("overview");
const auth = reactive({
  identity: "",
  password: "",
  confirm: "",
  token:
    new URLSearchParams(location.hash.split("?")[1] || "").get("token") || "",
});
const chat = reactive({
  model: "",
  prompt: "请简要介绍一下你自己。",
  result: "",
  reasoning: "",
  sending: false,
  thinking: "auto",
  maxTokens: 1024,
  temperature: 0.7,
  status: "idle",
  ttftMs: null,
  elapsedMs: null,
  generationMs: null,
  inputTokens: null,
  outputTokens: null,
  totalTokens: null,
  reasoningTokens: null,
  tokensPerSecond: null,
  requestId: "",
  responseModel: "",
  provider: "",
  cost: "",
  cacheHit: "",
  attachments: [],
  preparingAttachments: false,
});
let chatController = null;
let chatTimer = null;
const attachmentInput = ref(null);
const keyOnce = ref(sessionStorage.getItem("llmctl_api_key") || "");
const showApiKey = ref(false);
const keyLoading = ref(false);
const apiKeyField = ref(null);
const userEdit = reactive({
  user_id: "",
  status: "active",
  max_sessions: 0,
  requests_per_minute: 0,
  requests_per_day: 0,
  balance_delta: "0",
  group_ids: [],
  note: "",
});
const selectedUserIds = ref([]);
const analyticsFilters = reactive({
  range: "today",
  model: "",
  user: "",
  active_page: 1,
});
const bulkPolicy = reactive({
  scope: "filtered",
  change_max_sessions: true,
  max_sessions: 5,
  change_requests_per_minute: true,
  requests_per_minute: 100,
  change_requests_per_day: true,
  requests_per_day: 8000,
});
const groupEdit = reactive({
  id: "",
  name: "",
  description: "",
  status: "active",
});
const workflowNewPool = reactive({ id: "", strategy: "p2c-least-inflight" });
const workflowNewRoute = reactive({
  id: "",
  base_model: "",
  pool: "",
  mode: "transparent",
});
const workflowNewAdapter = reactive({
  id: "",
  endpoint: "",
  secret_env: "",
  function_name: "",
  description: "",
  allowed_purposes: "",
});
const modelEdit = reactive({
  id: "",
  public_model_id: "",
  display_name: "",
  description: "",
  source_kind: "combo",
  source_ref: "",
  source_provider: "",
  source_model: "",
  capabilities: ["chat"],
  context_window_tokens: "",
  max_output_tokens: "",
  sync_context_window: false,
  sync_max_output_tokens: false,
  metadata: null,
  metadata_sync_status: "unknown",
  metadata_sync_error: "",
  inspecting: false,
  input_price: "0",
  output_price: "0",
  cached_price: "0",
  reasoning_price: "0",
  status: "published",
  access: [{ type: "all", id: "" }],
});
const settings = reactive({});
const portalTitle = computed(
  () => settings.portal_title || publicConfig.value.portal_title || "LLMCtl",
);
const portalInitial = computed(
  () => Array.from(portalTitle.value.trim())[0]?.toUpperCase() || "L",
);
const allowedRegistrationEmails = computed(() =>
  (publicConfig.value.allowed_domains || [])
    .map((domain) => `@${String(domain).replace(/^@+/, "")}`)
    .join("、"),
);
const smtpTestRecipient = ref("");
const showHiddenFreeResources = ref(false);
const stressPlan = reactive({
  model: "",
  concurrency: 1,
  input_tokens: 50,
  output_tokens: 128,
  request_multiplier: 2,
  risk_confirmed: false,
});
const selectedStressRunId = ref("");
const PAGE_SIZE = 20;

const stressRuns = computed(() => admin.value?.stress_runs || []);
const activeStressRun = computed(() =>
  stressRuns.value.find((run) =>
    ["starting", "running", "canceling"].includes(run.status),
  ),
);
const selectedStressRun = computed(() =>
  stressRuns.value.find((run) => run.id === selectedStressRunId.value) ||
  activeStressRun.value ||
  stressRuns.value[0] ||
  null,
);
const stressRouteTargets = computed(() =>
  Object.entries(selectedStressRun.value?.metrics?.routing?.targets || {}).sort(
    (left, right) => right[1] - left[1],
  ),
);
const stressGpuImbalanced = computed(() => {
  const run = selectedStressRun.value;
  const gpu = run?.gpu;
  const total = gpu?.gpus?.length || 0;
  return (
    total > 1 &&
    gpu.sample_count >= 3 &&
    run.concurrency >= total &&
    gpu.peak_concurrent_active_gpu_count < total
  );
});
const stressPlanDiffersFromSelected = computed(() => {
  const run = selectedStressRun.value;
  if (!run || activeStressRun.value) return false;
  return (
    stressPlan.model !== run.public_model_id ||
    stressPlan.concurrency !== run.concurrency ||
    stressPlan.input_tokens !== run.target_input_tokens ||
    stressPlan.output_tokens !== run.max_output_tokens ||
    stressPlan.request_multiplier !== run.request_multiplier
  );
});
const stressIsHighRisk = computed(
  () => stressPlan.concurrency >= 20 || stressPlan.input_tokens >= 8000,
);

const selectedChatModel = computed(() =>
  dashboard.value?.models?.find(
    (model) => model.public_model_id === chat.model,
  ),
);
const selectedChatCapabilities = computed(() =>
  Array.isArray(selectedChatModel.value?.capabilities)
    ? selectedChatModel.value.capabilities.map((item) => String(item).toLowerCase())
    : [],
);
const selectedChatSupportsVision = computed(() =>
  selectedChatCapabilities.value.some((item) =>
    ["vision", "ocr", "image", "multimodal"].includes(item),
  ),
);
const pages = reactive({});
const requestDetails = reactive({});
const listFilters = reactive(
  Object.fromEntries(
    [
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
    ].map((key) => [key, { query: "", status: "all", category: "all" }]),
  ),
);
const usageFilters = reactive({ user: "", model: "" });
const statusOptions = [
  { value: "active", label: "正常" },
  { value: "published", label: "已发布" },
  { value: "disabled", label: "已停用" },
  { value: "draft", label: "草稿" },
  { value: "healthy", label: "测试通过" },
  { value: "failed", label: "测试失败" },
  { value: "untested", label: "未测试" },
  { value: "unconfigured", label: "未配置" },
  { value: "success", label: "成功" },
];
const kindOptions = [
  { value: "credit", label: "入账" },
  { value: "debit", label: "扣费" },
  { value: "adjustment", label: "调整" },
  { value: "refund", label: "退款" },
];
const resetOptions = [
  { value: "none", label: "仅本次" },
  { value: "daily", label: "每日" },
  { value: "weekly", label: "每周" },
  { value: "monthly", label: "每月" },
];
const filterFields = {
  "user-models": ["public_model_id", "display_name", "description"],
  "user-grants": ["label", "model_id", "reset_interval"],
  "user-billing": ["kind", "note"],
  "admin-models": [
    "public_model_id",
    "display_name",
    "description",
    "source_kind",
    "source_provider",
    "source_model",
  ],
  "admin-free": [
    "display_name",
    "provider",
    "model_id",
    "free_type",
    "test_error",
  ],
  "admin-users": [
    "email",
    "status",
    "permission_status",
    "balance",
  ],
  "admin-groups": ["name", "description", "status"],
  "admin-billing": ["user_email", "user_id", "kind", "note"],
  "admin-stress": ["public_model_id", "status", "created_by", "error"],
  "admin-audit": ["actor", "action", "target", "status", "detail"],
};
const filteredAdminUsers = computed(() =>
  filteredRows(
    "admin-users",
    (admin.value?.users || []).filter((user) => user.role === "user"),
  ),
);
const currentAdminUserPage = computed(() =>
  pageRows("admin-users", filteredAdminUsers.value),
);
const bulkTargetUsers = computed(() => {
  if (bulkPolicy.scope === "selected") {
    const selected = new Set(selectedUserIds.value);
    return (admin.value?.users || []).filter(
      (user) => user.role === "user" && selected.has(user.id),
    );
  }
  return filteredAdminUsers.value;
});
const allCurrentUserPageSelected = computed(
  () =>
    currentAdminUserPage.value.length > 0 &&
    currentAdminUserPage.value.every((user) =>
      selectedUserIds.value.includes(user.id),
    ),
);
const analyticsMaxTokens = computed(() =>
  Math.max(
    1,
    ...(adminAnalytics.value?.timeseries || []).map((row) =>
      Number(row.total_tokens || 0),
    ),
  ),
);
const selectedUserAnalyticsMaxTokens = computed(() =>
  Math.max(
    1,
    ...(adminAnalytics.value?.selected_user?.timeseries || []).map((row) =>
      Number(row.total_tokens || 0),
    ),
  ),
);
const PaginationBar = (props, { emit }) =>
  props.pages <= 1
    ? null
    : h(
        "nav",
        {
          "aria-label": "分页",
          style: {
            display: "flex",
            justifyContent: "flex-end",
            alignItems: "center",
            gap: "10px",
            marginTop: "16px",
          },
        },
        [
          h(
            "span",
            {
              style: {
                color: "var(--muted)",
                fontSize: "12px",
                marginRight: "4px",
              },
            },
            `第 ${props.page} / ${props.pages} 页 · 共 ${props.total} 条`,
          ),
          h(
            "button",
            {
              type: "button",
              class: "ghost",
              disabled: props.page <= 1,
              onClick: () => emit("previous"),
            },
            "上一页",
          ),
          h(
            "button",
            {
              type: "button",
              class: "ghost",
              disabled: props.page >= props.pages,
              onClick: () => emit("next"),
            },
            "下一页",
          ),
        ],
      );
PaginationBar.props = { page: Number, pages: Number, total: Number };
PaginationBar.emits = ["previous", "next"];

const ListFilterBar = (props, { emit }) =>
  h("div", { class: "list-filter", role: "search" }, [
    h("input", {
      type: "search",
      value: props.modelValue,
      placeholder: props.placeholder || "搜索当前列表",
      "aria-label": props.placeholder || "搜索当前列表",
      onInput: (event) => emit("update:modelValue", event.target.value),
    }),
    props.statusOptions?.length
      ? h(
          "select",
          {
            value: props.status,
            "aria-label": props.statusLabel || "状态筛选",
            onChange: (event) => emit("update:status", event.target.value),
          },
          [
            h("option", { value: "all" }, props.statusLabel || "全部状态"),
            ...props.statusOptions.map((option) =>
              h("option", { value: option.value }, option.label),
            ),
          ],
        )
      : null,
    props.categoryOptions?.length
      ? h(
          "select",
          {
            value: props.category,
            "aria-label": props.categoryLabel || "分类筛选",
            onChange: (event) => emit("update:category", event.target.value),
          },
          [
            h("option", { value: "all" }, props.categoryLabel || "全部分类"),
            ...props.categoryOptions.map((option) =>
              h("option", { value: option.value }, option.label),
            ),
          ],
        )
      : null,
    h("span", { class: "filter-count" }, `${props.count || 0} 条`),
  ]);
ListFilterBar.props = {
  modelValue: String,
  status: String,
  statusLabel: String,
  statusOptions: Array,
  category: String,
  categoryLabel: String,
  categoryOptions: Array,
  placeholder: String,
  count: Number,
};
ListFilterBar.emits = ["update:modelValue", "update:status", "update:category"];

const isAdmin = computed(() => session.value?.user?.role === "admin");
const freeProviderOptions = computed(() =>
  [...new Set((admin.value?.free_resources || []).map((row) => row.provider))]
    .filter(Boolean)
    .sort((a, b) => a.localeCompare(b))
    .map((value) => ({ value, label: value })),
);
const nav = computed(() =>
  isAdmin.value
    ? [
        ["overview", "总览"],
        ["models", "模型与定价"],
        ["free", "免费资源"],
        ["users", "用户"],
        ["groups", "用户组"],
        ["billing", "账单"],
        ["workflow", "能力编排"],
        ["stress", "性能压测"],
        ["settings", "发布、注册与 SMTP"],
        ["audit", "审计"],
      ]
    : [
        ["overview", "工作台"],
        ["models", "模型广场"],
        ["playground", "在线测试"],
        ["billing", "用量与账单"],
        ["keys", "API Key"],
      ],
);

function cookie(name) {
  return (
    document.cookie
      .split("; ")
      .find((v) => v.startsWith(`${name}=`))
      ?.split("=")
      .slice(1)
      .join("=") || ""
  );
}

async function api(path, options = {}) {
  const response = await fetch(`/portal-api/${path}`, {
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": decodeURIComponent(cookie("llm_account_csrf")),
      ...(options.headers || {}),
    },
    ...options,
  });
  const body = await response
    .json()
    .catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function notify(text, kind = "ok") {
  toast.text = text;
  toast.kind = kind;
  if (toastTimer) window.clearTimeout(toastTimer);
  if (kind !== "working")
    toastTimer = window.setTimeout(
      () => {
        if (toast.text === text) toast.text = "";
      },
      kind === "bad" ? 9000 : 3600,
    );
}

function dismissToast() {
  if (toastTimer) window.clearTimeout(toastTimer);
  toast.text = "";
}

async function load() {
  publicConfig.value = await api("public");
  session.value = await api("session");
  if (!session.value.authenticated) {
    clearAuthenticatedClientState();
    return;
  }
  await refreshWorkspace();
}

function clearAuthenticatedClientState() {
  sessionStorage.removeItem("llmctl_api_key");
  keyOnce.value = "";
  showApiKey.value = false;
  dashboard.value = null;
  admin.value = null;
  adminAnalytics.value = null;
  workflow.value = null;
  selectedUserIds.value = [];
  section.value = "overview";
  usageFilters.user = "";
  usageFilters.model = "";
  for (const key of Object.keys(requestDetails)) delete requestDetails[key];
  resetChatResult();
  chat.status = "idle";
}

async function loadAdminAnalytics(activePage = analyticsFilters.active_page, options = {}) {
  if (!isAdmin.value || !session.value?.authenticated) return;
  const version = ++analyticsLoadVersion;
  analyticsLoading.value = true;
  const params = new URLSearchParams({
    range: analyticsFilters.range,
    model: analyticsFilters.model,
    user: analyticsFilters.user,
    active_page: String(activePage || 1),
    active_page_size: "10",
  });
  try {
    const result = await api(`admin/analytics?${params}`);
    if (version !== analyticsLoadVersion) return;
    adminAnalytics.value = result;
    analyticsFilters.active_page = result.active_pagination?.page || 1;
  } catch (error) {
    if (!options.silent) notify(`运行统计读取失败：${error.message}`, "bad");
  } finally {
    if (version === analyticsLoadVersion) analyticsLoading.value = false;
  }
}

async function changeAnalyticsFilters() {
  analyticsFilters.active_page = 1;
  await loadAdminAnalytics(1);
}

function applyAdminSnapshot(snapshot) {
  snapshot.stress_runs ||= [];
  admin.value = snapshot;
  if (
    !stressPlan.model ||
    !snapshot.models?.some(
      (model) =>
        model.public_model_id === stressPlan.model &&
        model.status === "published",
    )
  )
    stressPlan.model =
      snapshot.models?.find((model) => model.status === "published")
        ?.public_model_id || "";
  if (!selectedStressRunId.value && snapshot.stress_runs?.length)
    selectedStressRunId.value = snapshot.stress_runs[0].id;
  Object.assign(settings, snapshot.settings);
  settings.portal_title ||= publicConfig.value.portal_title || "LLMCtl";
  settings.published_origin ||= "";
  if (!smtpTestRecipient.value)
    smtpTestRecipient.value = settings.smtp_from || "";
  const currentOrigin = location.origin;
  const currentIsRemote =
    !/^https?:\/\/(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$/i.test(
      currentOrigin,
    );
  const savedPortalIsLocal =
    /^https?:\/\/(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?(?:\/|$)/i.test(
      settings.public_url || "",
    );
  const savedApiIsLocal =
    /^https?:\/\/(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?(?:\/|$)/i.test(
      settings.api_public_url || "",
    );
  if (!settings.public_url || (currentIsRemote && savedPortalIsLocal))
    settings.public_url = `${currentOrigin}/ui`;
  if (!settings.api_public_url || (currentIsRemote && savedApiIsLocal))
    settings.api_public_url = currentOrigin;
}

async function refreshWorkspace() {
  if (!session.value?.authenticated) return;
  const version = ++workspaceLoadVersion;
  workspaceRefreshing.value = true;
  try {
    const snapshot = await api(isAdmin.value ? "admin" : "dashboard");
    if (version !== workspaceLoadVersion) return;
    if (isAdmin.value) applyAdminSnapshot(snapshot);
    else {
      dashboard.value = snapshot;
      if (
        !chat.model ||
        !snapshot.models.some((model) => model.public_model_id === chat.model)
      )
        chat.model = snapshot.models[0]?.public_model_id || "";
      if (snapshot.has_api_key && !keyOnce.value) await loadExistingKey();
    }
    if (
      section.value === "billing" &&
      (usageFilters.user || usageFilters.model)
    )
      await loadUsagePage(1);
    if (isAdmin.value && section.value === "overview")
      await loadAdminAnalytics(analyticsFilters.active_page);
  } finally {
    if (version === workspaceLoadVersion) workspaceRefreshing.value = false;
  }
}

async function loadExistingKey() {
  if (isAdmin.value || keyOnce.value || keyLoading.value) return;
  keyLoading.value = true;
  try {
    const result = await api("key/reveal", { method: "POST", body: "{}" });
    if (!result?.api_key) throw new Error("接入层未返回现有 Key");
    keyOnce.value = result.api_key;
    sessionStorage.setItem("llmctl_api_key", result.api_key);
  } catch (error) {
    notify(`现有 API Key 读取失败：${error.message}`, "bad");
  } finally {
    keyLoading.value = false;
  }
}

async function syncUsageAndRefresh(options = {}) {
  const {
    reconcile = true,
    announce = false,
    preservePage = true,
    silent = false,
  } = options;
  if (!session.value?.authenticated || usageRefreshing.value) return;
  const target = isAdmin.value ? admin.value : dashboard.value;
  const previousPage = preservePage
    ? target?.usage_pagination?.page || 1
    : 1;
  usageRefreshing.value = true;
  try {
    if (reconcile) {
      await api(isAdmin.value ? "admin/billing/reconcile" : "usage/reconcile", {
        method: "POST",
        body: "{}",
      });
    }
    await refreshWorkspace();
    if (section.value === "billing") await loadUsagePage(previousPage);
    if (announce) notify("用量数据已更新");
  } catch (error) {
    if (!silent) notify(`用量更新失败：${error.message}`, "bad");
  } finally {
    usageRefreshing.value = false;
  }
}

async function selectSection(nextSection) {
  section.value = nextSection;
  try {
    if (nextSection === "workflow" && isAdmin.value) {
      await Promise.all([refreshWorkspace(), loadWorkflow()]);
    } else if (nextSection === "billing") {
      await syncUsageAndRefresh({ preservePage: false });
    } else {
      await refreshWorkspace();
    }
  } catch (error) {
    notify(`页面数据更新失败：${error.message}`, "bad");
  }
}

async function loadWorkflow(options = {}) {
  if (!isAdmin.value || !session.value?.authenticated) return;
  workflowLoading.value = true;
  try {
    const result = await api("admin/workflow");
    workflow.value = {
      available: true,
      revision: result.revision,
      config: structuredClone(result.config),
    };
    if (!workflowNewRoute.pool) {
      workflowNewRoute.pool = Object.keys(workflow.value.config.pools || {})[0] || "";
    }
  } catch (error) {
    workflow.value = { available: false, error: error.message };
    if (!options.silent) notify(`工作流读取失败：${error.message}`, "bad");
  } finally {
    workflowLoading.value = false;
  }
}

async function saveWorkflow() {
  if (!workflow.value?.available || workflowSaving.value) return;
  workflowSaving.value = true;
  try {
    const result = await api("admin/workflow/config", {
      method: "POST",
      body: JSON.stringify({
        revision: workflow.value.revision,
        config: workflow.value.config,
      }),
    });
    workflow.value = {
      available: true,
      revision: result.revision,
      config: structuredClone(result.config),
    };
    notify("能力编排配置已校验、保存并热加载");
  } catch (error) {
    notify(`能力编排保存失败：${error.message}`, "bad");
    if (/changed|冲突|版本/i.test(error.message)) await loadWorkflow({ silent: true });
  } finally {
    workflowSaving.value = false;
  }
}

async function publishWorkflow() {
  if (!workflow.value?.available || workflowPublishing.value) return;
  workflowPublishing.value = true;
  try {
    const result = await api("admin/workflow/publish", {
      method: "POST",
      body: "{}",
    });
    const routes = (result.published || []).map((item) => item.combo).join("、");
    notify(`已同步到 AI 网关${routes ? `：${routes}` : ""}`);
    await loadAdmin({ silent: true });
  } catch (error) {
    notify(`同步 AI 网关失败：${error.message}`, "bad");
  } finally {
    workflowPublishing.value = false;
  }
}

function workflowIdentifier(value) {
  return String(value || "").trim();
}

function addWorkflowPool() {
  const id = workflowIdentifier(workflowNewPool.id);
  if (!id || workflow.value.config.pools[id]) {
    notify("请输入未使用的资源池 ID", "bad");
    return;
  }
  workflow.value.config.pools[id] = {
    strategy: workflowNewPool.strategy,
    targets: [
      {
        id: "target-1",
        base_url: "http://127.0.0.1:8000/v1",
        api_key_env: "BACKEND_API_KEY",
      },
    ],
  };
  if (!workflowNewRoute.pool) workflowNewRoute.pool = id;
  workflowNewPool.id = "";
}

function removeWorkflowPool(id) {
  const used = Object.values(workflow.value.config.models).some(
    (route) => route.pool === id,
  );
  if (used) {
    notify("该资源池仍被模型路由使用，不能删除", "bad");
    return;
  }
  delete workflow.value.config.pools[id];
}

function addWorkflowTarget(pool) {
  pool.targets.push({
    id: `target-${pool.targets.length + 1}`,
    base_url: "http://127.0.0.1:8000/v1",
    api_key_env: "BACKEND_API_KEY",
  });
}

function removeWorkflowTarget(pool, index) {
  if (pool.targets.length <= 1) {
    notify("每个资源池至少需要保留一个 Worker / 资源实例", "bad");
    return;
  }
  pool.targets.splice(index, 1);
}

function addWorkflowRoute() {
  const id = workflowIdentifier(workflowNewRoute.id);
  if (!id || workflow.value.config.models[id]) {
    notify("请输入未使用的公开模型 ID", "bad");
    return;
  }
  if (!workflowNewRoute.pool) {
    notify("请先选择资源池", "bad");
    return;
  }
  if (!workflowIdentifier(workflowNewRoute.base_model)) {
    notify("请输入底层模型 ID", "bad");
    return;
  }
  workflow.value.config.models[id] = {
    enabled: false,
    mode: workflowNewRoute.mode,
    base_model: workflowIdentifier(workflowNewRoute.base_model),
    pool: workflowNewRoute.pool,
    tools: [],
    max_tool_rounds: 4,
    system_prompt: "",
  };
  workflowNewRoute.id = "";
  workflowNewRoute.base_model = "";
}

function removeWorkflowRoute(id) {
  if (Object.keys(workflow.value.config.models).length <= 1) {
    notify("至少需要保留一个模型路由", "bad");
    return;
  }
  delete workflow.value.config.models[id];
}

function toggleWorkflowTool(route, adapterId, enabled) {
  const values = new Set(Array.isArray(route.tools) ? route.tools : []);
  if (enabled) values.add(adapterId);
  else values.delete(adapterId);
  route.tools = [...values];
}

function workflowToolParameters(adapter) {
  return JSON.stringify(
    adapter?.tool_definition?.function?.parameters || {
      type: "object",
      additionalProperties: true,
    },
    null,
    2,
  );
}

function updateWorkflowToolParameters(adapter, raw) {
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      throw new Error("参数 Schema 必须是 JSON 对象");
    }
    adapter.tool_definition ||= { type: "function", function: {} };
    adapter.tool_definition.function ||= {};
    adapter.tool_definition.function.parameters = parsed;
  } catch (error) {
    notify(`工具参数 Schema 无效：${error.message}`, "bad");
  }
}

function addWorkflowAdapter() {
  const id = workflowIdentifier(workflowNewAdapter.id);
  const functionName = workflowIdentifier(workflowNewAdapter.function_name);
  if (!id || workflow.value.config.adapters[id]) {
    notify("请输入未使用的适配器 ID", "bad");
    return;
  }
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(functionName)) {
    notify("工具函数名只能包含字母、数字、下划线和连字符", "bad");
    return;
  }
  if (!/^https?:\/\/[^\s]+$/i.test(workflowIdentifier(workflowNewAdapter.endpoint))) {
    notify("请输入有效的 HTTP(S) 适配器地址", "bad");
    return;
  }
  workflow.value.config.adapters[id] = {
    kind: "http-json",
    endpoint: workflowIdentifier(workflowNewAdapter.endpoint),
    secret_env: workflowIdentifier(workflowNewAdapter.secret_env),
    timeout_ms: 60000,
    result_max_bytes: 4194304,
    allowed_purposes: workflowNewAdapter.allowed_purposes
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean),
    tool_definition: {
      type: "function",
      function: {
        name: functionName,
        description: workflowIdentifier(workflowNewAdapter.description),
        parameters: { type: "object", additionalProperties: true },
      },
    },
  };
  Object.assign(workflowNewAdapter, {
    id: "",
    endpoint: "",
    secret_env: "",
    function_name: "",
    description: "",
    allowed_purposes: "",
  });
}

function removeWorkflowAdapter(id) {
  for (const route of Object.values(workflow.value.config.models))
    route.tools = (route.tools || []).filter((tool) => tool !== id);
  delete workflow.value.config.adapters[id];
}

function replaceStressRun(run) {
  if (!admin.value || !run?.id) return;
  const index = admin.value.stress_runs.findIndex((item) => item.id === run.id);
  if (index >= 0) admin.value.stress_runs.splice(index, 1, run);
  else admin.value.stress_runs.unshift(run);
  selectedStressRunId.value = run.id;
}

async function pollStressRun() {
  if (!isAdmin.value || document.hidden || section.value !== "stress") return;
  const target = activeStressRun.value || selectedStressRun.value;
  if (!target?.id) return;
  try {
    replaceStressRun(
      await api(`admin/stress?id=${encodeURIComponent(target.id)}`),
    );
  } catch (error) {
    notify(`读取压测进度失败：${error.message}`, "bad");
  }
}

async function startStressRun() {
  if (!stressPlan.model) return notify("请选择已发布模型", "bad");
  if (stressIsHighRisk.value && !stressPlan.risk_confirmed)
    return notify("请先确认高负载风险", "bad");
  const result = await action(
    () =>
      api("admin/stress/start", {
        method: "POST",
        body: JSON.stringify(stressPlan),
      }),
    "后台压测已启动",
    {
      key: "stress-start",
      pending: "正在启动后台压测执行器…",
      refresh: false,
    },
  );
  if (result) replaceStressRun(result);
}

async function cancelStressRun() {
  const target = activeStressRun.value;
  if (!target) return;
  const result = await action(
    () =>
      api("admin/stress/cancel", {
        method: "POST",
        body: JSON.stringify({ id: target.id }),
      }),
    "已要求后台停止压测",
    { key: "stress-cancel", pending: "正在停止压测…", refresh: false },
  );
  if (result) replaceStressRun(result);
}

function metric(run, group, key) {
  return run?.metrics?.[group]?.[key] ?? null;
}

function metricNumber(value, digits = 1) {
  return value === null || value === undefined || !Number.isFinite(Number(value))
    ? "—"
    : Number(value).toFixed(digits);
}

function compactTokens(value) {
  const number = Number(value || 0);
  if (number >= 1000) return `${number / 1000}k`;
  return String(number);
}

function formatTokens(value) {
  return Number(value || 0).toLocaleString();
}

function analyticsSegmentHeight(value, maximum) {
  return `${Math.max(0, (Number(value || 0) / Math.max(1, maximum)) * 100)}%`;
}

function analyticsLabelVisible(index, total) {
  if (total <= 12) return true;
  const step = Math.ceil(total / 8);
  return index === 0 || index === total - 1 || index % step === 0;
}

async function analyzeUser(userId) {
  analyticsFilters.user = userId || "";
  await loadAdminAnalytics(1);
  if (userId) await nextTick();
  document.querySelector("#user-usage-analysis")?.scrollIntoView({
    behavior: "smooth",
    block: "start",
  });
}

async function goToSection(nextSection) {
  await selectSection(nextSection);
}

async function action(fn, success = "操作成功", options = {}) {
  if (busy.value) {
    notify("已有操作正在执行，请等待当前操作完成。", "working");
    return null;
  }
  const {
    key = "general",
    pending = "正在处理，请稍候…",
    refresh = true,
  } = options;
  busy.value = true;
  operation.value = key;
  notify(pending, "working");
  try {
    const result = await fn();
    if (refresh) await load();
    notify(success);
    return result;
  } catch (error) {
    notify(error.message, "bad");
    return null;
  } finally {
    busy.value = false;
    operation.value = "";
  }
}

async function login() {
  await action(async () => {
    await api("auth/login", {
      method: "POST",
      body: JSON.stringify({
        identity: auth.identity,
        password: auth.password,
      }),
    });
    auth.password = "";
    auth.confirm = "";
  }, "登录成功");
}

async function register() {
  if (auth.password.length < 8 || auth.password.length > 200)
    return notify("密码必须为 8-200 个字符", "bad");
  if (auth.password.trim() && /^\p{Nd}+$/u.test(auth.password.trim()))
    return notify("密码不能全部由数字组成", "bad");
  if (auth.password !== auth.confirm)
    return notify("两次输入的密码不一致", "bad");
  await action(
    () =>
      api("auth/register", {
        method: "POST",
        body: JSON.stringify({
          email: auth.identity,
          password: auth.password,
          confirm: auth.confirm,
        }),
      }),
    "验证邮件已发送，请点击邮件中的链接完成验证",
  );
}

async function verify() {
  const result = await action(
    () =>
      api("auth/verify", {
        method: "POST",
        body: JSON.stringify({ token: auth.token }),
      }),
    "账户已开通，请立即保存 API Key",
  );
  if (result?.api_key) {
    keyOnce.value = result.api_key;
    sessionStorage.setItem("llmctl_api_key", result.api_key);
  }
}

async function logout() {
  await api("auth/logout", { method: "POST", body: "{}" });
  clearAuthenticatedClientState();
  auth.password = "";
  auth.confirm = "";
  await load();
}

async function rotateKey() {
  const result = await action(
    () => api("key/rotate", { method: "POST", body: "{}" }),
    "API Key 已轮换，旧 Key 已失效",
  );
  if (result?.api_key) {
    keyOnce.value = result.api_key;
    sessionStorage.setItem("llmctl_api_key", result.api_key);
  }
}

async function copy(value, options = {}) {
  const result = await writeClipboardText(value);
  if (result.copied) {
    notify("已复制到剪贴板");
    return true;
  }
  if (options.revealOnFailure) {
    showApiKey.value = true;
    await nextTick();
    apiKeyField.value?.focus();
    apiKeyField.value?.select();
    apiKeyField.value?.setSelectionRange?.(0, String(value || "").length);
    notify("浏览器禁止自动复制；Key 已显示并选中，请按 Ctrl+C。", "bad");
  } else {
    notify("浏览器禁止自动复制，请手工选中文本后复制。", "bad");
  }
  return false;
}

function money(micros) {
  const fixed = (Number(micros || 0) / 1_000_000).toFixed(6);
  const [whole, fraction] = fixed.split(".");
  return `$${whole}.${fraction.replace(/0+$/, "").padEnd(4, "0")}`;
}
function cashTokenCapacity(pricePerMillion) {
  const balance = Number(dashboard.value?.balance || 0);
  const price = Number(pricePerMillion || 0);
  if (price <= 0) return "不扣现金余额";
  if (!Number.isFinite(balance) || balance <= 0) return "0 Token";
  return `${Math.floor((balance * 1_000_000) / price).toLocaleString()} Token`;
}
function statusLabel(value) {
  return (
    {
      active: "正常",
      published: "已发布",
      disabled: "已停用",
      draft: "草稿",
      healthy: "测试通过",
      failed: "失败",
      untested: "未测试",
      pending: "待同步",
      synced: "已同步",
      success: "成功",
      starting: "正在启动",
      running: "运行中",
      canceling: "正在停止",
      completed: "已完成",
      canceled: "已停止",
      expired: "已过期",
    }[value] ||
    value ||
    "未知"
  );
}
function kindLabel(value) {
  return (
    {
      credit: "入账",
      debit: "扣费",
      adjustment: "调整",
      refund: "退款",
    }[value] ||
    value ||
    "—"
  );
}
function resetLabel(value) {
  return (
    { none: "仅本次", daily: "每日", weekly: "每周", monthly: "每月" }[value] ||
    value
  );
}
function rowStatus(key, row) {
  if (key === "admin-free")
    return row.configured ? row.test_status : "unconfigured";
  if (key === "admin-models") return row.status;
  return row.status || row.permission_status || "";
}
function rowCategory(key, row) {
  if (key === "admin-free") return row.provider;
  if (key.endsWith("billing")) return row.kind;
  if (key === "user-grants") return row.reset_interval;
  if (key === "admin-models") return row.source_kind;
  return "";
}
function filteredRows(key, rows) {
  const filter = listFilters[key];
  if (!filter) return rows || [];
  const query = filter.query.trim().toLocaleLowerCase();
  return (rows || []).filter((row) => {
    if (
      key === "admin-free" &&
      !showHiddenFreeResources.value &&
      Number(row.native_visible) === 0
    )
      return false;
    if (
      query &&
      !(filterFields[key] || []).some((field) =>
        String(row[field] ?? "")
          .toLocaleLowerCase()
          .includes(query),
      )
    )
      return false;
    if (filter.status !== "all" && rowStatus(key, row) !== filter.status)
      return false;
    if (filter.category !== "all" && rowCategory(key, row) !== filter.category)
      return false;
    return true;
  });
}

watch(
  listFilters,
  () => {
    for (const key of Object.keys(listFilters)) pages[key] = 1;
  },
  { deep: true },
);

watch(
  portalTitle,
  (title) => {
    document.title = `${title} 模型服务门户`;
  },
  { immediate: true },
);

async function loadUsagePage(nextPage = 1) {
  const parameters = new URLSearchParams({
    page: String(nextPage),
    page_size: String(PAGE_SIZE),
  });
  if (usageFilters.model) parameters.set("model", usageFilters.model);
  if (isAdmin.value && usageFilters.user)
    parameters.set("user", usageFilters.user);
  const result = await api(
    `${isAdmin.value ? "admin/" : ""}usage-page?${parameters}`,
  );
  const target = isAdmin.value ? admin.value : dashboard.value;
  target.usage = result.items;
  target.usage_pagination = {
    page: result.page,
    page_size: result.page_size,
    pages: result.pages,
    total: result.total,
  };
}

async function applyUsageFilters() {
  try {
    await loadUsagePage(1);
  } catch (error) {
    notify(`筛选用量失败：${error.message}`, "bad");
  }
}

async function changeUsagePage(delta) {
  const target = isAdmin.value ? admin.value : dashboard.value;
  const pagination = target?.usage_pagination || {
    page: 1,
    pages: 1,
  };
  const next = Math.min(pagination.pages, Math.max(1, pagination.page + delta));
  if (next === pagination.page) return;
  try {
    await loadUsagePage(next);
  } catch (error) {
    notify(`读取用量分页失败：${error.message}`, "bad");
  }
}
function date(value) {
  return value
    ? new Date(Number.isFinite(+value) ? +value * 1000 : value).toLocaleString()
    : "—";
}
function pageCount(rows) {
  return Math.max(1, Math.ceil((rows?.length || 0) / PAGE_SIZE));
}
function pageNumber(key, rows) {
  return Math.min(Math.max(1, pages[key] || 1), pageCount(rows));
}
function pageRows(key, rows) {
  const current = pageNumber(key, rows);
  return (rows || []).slice((current - 1) * PAGE_SIZE, current * PAGE_SIZE);
}
function changePage(key, rows, delta) {
  pages[key] = Math.min(
    pageCount(rows),
    Math.max(1, pageNumber(key, rows) + delta),
  );
}

async function toggleRequestDetail(row) {
  const key = row.request_id;
  const current = requestDetails[key];
  if (current?.loaded) {
    current.expanded = !current.expanded;
    return;
  }
  requestDetails[key] = {
    loading: true,
    loaded: false,
    expanded: true,
    messages: [],
    error: "",
  };
  try {
    const detail = await api(
      `${isAdmin.value ? "admin/" : ""}usage/${encodeURIComponent(key)}`,
    );
    Object.assign(requestDetails[key], detail, {
      loading: false,
      loaded: true,
    });
  } catch (error) {
    Object.assign(requestDetails[key], {
      loading: false,
      loaded: true,
      error: error.message,
    });
  }
}

function curlFor(model) {
  const base =
    dashboard.value?.api_base || `${publicConfig.value.api_public_url}/v1`;
  const apiKey = keyOnce.value || "YOUR_API_KEY";
  return [
    `curl ${base}/chat/completions \\`,
    `  -H 'Authorization: Bearer ${apiKey}' \\`,
    "  -H 'Content-Type: application/json' \\",
    `  -d '${JSON.stringify({ model: model.public_model_id, stream: false, messages: [{ role: "user", content: "你好" }] })}'`,
  ].join("\n");
}

function resetChatResult() {
  Object.assign(chat, {
    result: "",
    reasoning: "",
    status: "connecting",
    ttftMs: null,
    elapsedMs: 0,
    generationMs: null,
    inputTokens: null,
    outputTokens: null,
    totalTokens: null,
    reasoningTokens: null,
    tokensPerSecond: null,
    requestId: "",
    responseModel: "",
    provider: "",
    cost: "",
    cacheHit: "",
  });
}

function stopChat() {
  if (chatController) chatController.abort();
}

function attachmentSize(value) {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

function removeAttachment(id) {
  if (chat.sending || chat.preparingAttachments) return;
  const index = chat.attachments.findIndex((item) => item.id === id);
  if (index >= 0) chat.attachments.splice(index, 1);
}

async function addAttachmentFiles(event) {
  const files = Array.from(event?.target?.files || event?.dataTransfer?.files || []);
  if (event?.target) event.target.value = "";
  if (!files.length || chat.preparingAttachments || chat.sending) return;
  chat.preparingAttachments = true;
  let added = 0;
  try {
    for (const file of files) {
      const kind = attachmentKind(file);
      if (
        ["image", "pdf"].includes(kind) &&
        !selectedChatSupportsVision.value
      )
        throw new Error(
          `当前模型未声明图片/OCR 能力，不能添加 ${file.name}；请选择支持 vision 或 ocr 的模型。`,
        );
      const totalBytes = chat.attachments.reduce(
        (total, item) => total + Number(item.size || 0),
        file.size,
      );
      if (totalBytes > MAX_TOTAL_ATTACHMENT_BYTES)
        throw new Error("本次请求的附件原文件合计不能超过 24 MiB");
      const remainingVisuals =
        MAX_VISUAL_INPUTS - visualInputCount(chat.attachments);
      const attachment = await prepareAttachment(file, remainingVisuals);
      chat.attachments.push(attachment);
      added += 1;
      if (attachment.truncated)
        notify(
          attachment.kind === "pdf"
            ? `${attachment.name} 共 ${attachment.pageCount} 页，本次按视觉输入上限读取前 ${attachment.pages.length} 页。`
            : `${attachment.name} 内容过长，本次只读取前 100 万字符。`,
          "warn",
        );
    }
    if (added) notify(`已添加 ${added} 个附件，仅随本次推理请求发送。`);
  } catch (error) {
    notify(error.message, "bad");
  } finally {
    chat.preparingAttachments = false;
  }
}

async function responseError(response) {
  const raw = await response.text();
  try {
    const body = JSON.parse(raw);
    return (
      body.error?.message ||
      body.error ||
      body.message ||
      `HTTP ${response.status}`
    );
  } catch {
    return raw.slice(0, 500) || `HTTP ${response.status}`;
  }
}

async function sendChat() {
  if (!keyOnce.value) return notify("请先输入或轮换个人 API Key", "bad");
  if (!chat.model) return notify("请选择模型", "bad");
  if (!chat.prompt.trim() && !chat.attachments.length)
    return notify("请输入消息或添加附件", "bad");
  if (
    chat.attachments.some((item) => ["image", "pdf"].includes(item.kind)) &&
    !selectedChatSupportsVision.value
  )
    return notify("当前模型不支持图片或 PDF 视觉输入，请更换模型或移除附件", "bad");
  resetChatResult();
  chat.sending = true;
  chatController = new AbortController();
  const startedAt = performance.now();
  let firstTokenAt = null;
  let lastTokenAt = null;
  chatTimer = window.setInterval(() => {
    chat.elapsedMs = Math.round(performance.now() - startedAt);
  }, 100);
  const payload = {
    model: chat.model,
    stream: true,
    stream_options: { include_usage: true },
    max_tokens: Math.max(1, Math.min(32768, Number(chat.maxTokens) || 1024)),
    temperature: Math.max(0, Math.min(2, Number(chat.temperature) || 0)),
    messages: [
      {
        role: "user",
        content: buildUserContent(chat.prompt, chat.attachments),
      },
    ],
  };
  if (chat.thinking === "enabled")
    Object.assign(payload, {
      reasoning_effort: "medium",
      chat_template_kwargs: { enable_thinking: true },
    });
  if (chat.thinking === "disabled")
    Object.assign(payload, {
      reasoning_effort: "none",
      chat_template_kwargs: { enable_thinking: false },
    });
  try {
    const response = await fetch(
      `${dashboard.value.api_base}/chat/completions`,
      {
        method: "POST",
        signal: chatController.signal,
        headers: {
          Authorization: `Bearer ${keyOnce.value}`,
          "Content-Type": "application/json",
          Accept: "text/event-stream",
        },
        body: JSON.stringify(payload),
      },
    );
    if (!response.ok) throw new Error(await responseError(response));
    chat.status = "generating";
    await consumeChatResponse(
      response,
      (chunk) => {
        const parts = chunkParts(chunk);
        const stamp = performance.now();
        if ((parts.content || parts.reasoning) && firstTokenAt === null) {
          firstTokenAt = stamp;
          chat.ttftMs = Math.round(stamp - startedAt);
        }
        if (parts.content || parts.reasoning) lastTokenAt = stamp;
        chat.result += parts.content;
        chat.reasoning += parts.reasoning;
        chat.requestId ||= parts.id;
        chat.responseModel ||= parts.model;
        if (parts.usage) {
          chat.inputTokens = Number(
            parts.usage.prompt_tokens ?? parts.usage.input_tokens ?? 0,
          );
          chat.outputTokens = Number(
            parts.usage.completion_tokens ?? parts.usage.output_tokens ?? 0,
          );
          chat.totalTokens = Number(
            parts.usage.total_tokens ?? chat.inputTokens + chat.outputTokens,
          );
          chat.reasoningTokens =
            parts.usage.completion_tokens_details?.reasoning_tokens ??
            parts.usage.reasoning_tokens ??
            null;
        }
      },
      (metadata) => {
        if (metadata["x-omniroute-tokens-in"] !== undefined)
          chat.inputTokens = Number(metadata["x-omniroute-tokens-in"]);
        if (metadata["x-omniroute-tokens-out"] !== undefined)
          chat.outputTokens = Number(metadata["x-omniroute-tokens-out"]);
        chat.provider ||= metadata["x-omniroute-provider"] || "";
        chat.responseModel ||= metadata["x-omniroute-model"] || "";
        chat.cost ||= metadata["x-omniroute-response-cost"] || "";
        chat.cacheHit ||= metadata["x-omniroute-cache-hit"] || "";
      },
    );
    const finishedAt = lastTokenAt || performance.now();
    chat.elapsedMs = Math.round(performance.now() - startedAt);
    if (firstTokenAt !== null) {
      chat.generationMs = Math.max(1, Math.round(finishedAt - firstTokenAt));
      if (Number.isFinite(chat.outputTokens))
        chat.tokensPerSecond = chat.outputTokens / (chat.generationMs / 1000);
    }
    if (
      chat.totalTokens === null &&
      Number.isFinite(chat.inputTokens) &&
      Number.isFinite(chat.outputTokens)
    )
      chat.totalTokens = chat.inputTokens + chat.outputTokens;
    const tagged = splitThinkingMarkup(chat.result);
    if (tagged.reasoning) {
      chat.reasoning = [chat.reasoning, tagged.reasoning]
        .filter(Boolean)
        .join("\n\n");
      chat.result = tagged.content;
    }
    chat.status = "completed";
  } catch (error) {
    if (error.name === "AbortError") {
      chat.status = "stopped";
      notify("已停止生成");
    } else {
      chat.status = "failed";
      notify(error.message, "bad");
    }
  } finally {
    if (chatTimer) window.clearInterval(chatTimer);
    chatTimer = null;
    chatController = null;
    chat.sending = false;
    chat.elapsedMs = Math.round(performance.now() - startedAt);
  }
}

function editUser(user) {
  Object.assign(userEdit, {
    user_id: user.id,
    status: user.status,
    max_sessions: Number(user.max_sessions ?? 0),
    requests_per_minute: Number(user.requests_per_minute ?? 0),
    requests_per_day: Number(user.requests_per_day ?? 0),
    balance_delta: "0",
    group_ids: admin.value.memberships
      .filter((m) => m.user_id === user.id)
      .map((m) => m.group_id),
    note: "",
  });
  document.querySelector("#user-editor")?.showModal();
}
function editGroup(group = {}) {
  Object.assign(groupEdit, {
    id: group.id || "",
    name: group.name || "",
    description: group.description || "",
    status: group.status || "active",
  });
  document.querySelector("#group-editor")?.showModal();
}
function editModel(model = {}) {
  Object.assign(modelEdit, {
    id: model.id || "",
    public_model_id: model.public_model_id || "",
    display_name: model.display_name || "",
    description: model.description || "",
    source_kind: model.source_kind || "combo",
    source_ref: model.source_ref || "",
    source_provider: model.source_provider || "",
    source_model: model.source_model || "",
    capabilities: model.capabilities ? [...model.capabilities] : ["chat"],
    context_window_tokens: model.context_window_tokens || "",
    max_output_tokens: model.max_output_tokens || "",
    sync_context_window: false,
    sync_max_output_tokens: false,
    metadata: model.metadata || null,
    metadata_sync_status: model.metadata_sync_status || "unknown",
    metadata_sync_error: model.metadata_sync_error || "",
    inspecting: false,
    input_price: model.input_price || "0",
    output_price: model.output_price || "0",
    cached_price: model.cached_price || "0",
    reasoning_price: model.reasoning_price || "0",
    status: model.status || "published",
    access: model.access?.map((a) => ({
      type: a.subject_type,
      id: a.subject_id,
    })) || [{ type: "all", id: "" }],
  });
  document.querySelector("#model-editor")?.showModal();
  if (model.source_model) inspectModel(false);
}
function publishFree(resource) {
  if (!resource.configured)
    return notify(
      `尚未检测到供应商 ${resource.provider} 的可用配置。请先在 AI 接入层配置凭据，再重新发现资源。`,
      "bad",
    );
  if (!resource.available || resource.test_status !== "healthy")
    return notify(
      "请先完成实时测试；只有测试通过的资源才能开放给用户。",
      "bad",
    );
  editModel({
    source_kind: "free",
    source_ref: resource.resource_key,
    source_provider: resource.provider,
    source_model: resource.model_id,
    public_model_id: resource.model_id,
    display_name: resource.display_name,
    capabilities: ["chat"],
    status: "published",
  });
}
async function testFreeResource(resource) {
  if (!resource.configured)
    return notify(
      `尚未检测到供应商 ${resource.provider} 的可用配置。请先在 AI 接入层配置凭据，再重新发现资源。`,
      "bad",
    );
  await action(
    () =>
      api("admin/free/test", {
        method: "POST",
        body: JSON.stringify({ resource_key: resource.resource_key }),
      }),
    "实时测试通过",
    {
      key: `free-test:${resource.resource_key}`,
      pending: "正在使用供应商限定的模型 ID 执行真实请求…",
    },
  );
}
function addModelAccess() {
  modelEdit.access.push({ type: "group", id: "" });
}
function removeModelAccess(index) {
  if (modelEdit.access.length === 1)
    Object.assign(modelEdit.access[0], { type: "all", id: "" });
  else modelEdit.access.splice(index, 1);
}
function selectCombo(event) {
  const combo = admin.value?.combos.find(
    (item) => String(item.id) === event.target.value,
  );
  if (combo) modelEdit.source_model = combo.name || combo.id;
  inspectModel(true);
}

async function inspectModel(overwrite = true) {
  if (!modelEdit.source_model || modelEdit.inspecting) return;
  modelEdit.inspecting = true;
  try {
    const metadata = await api("admin/models/inspect", {
      method: "POST",
      body: JSON.stringify(modelEdit),
    });
    modelEdit.metadata = metadata;
    if (overwrite || !modelEdit.context_window_tokens)
      modelEdit.context_window_tokens = metadata.context_window_tokens || "";
    if (overwrite || !modelEdit.max_output_tokens)
      modelEdit.max_output_tokens = metadata.max_output_tokens || "";
    if (
      (!modelEdit.capabilities?.length ||
        modelEdit.capabilities.length === 1) &&
      metadata.capabilities?.length
    )
      modelEdit.capabilities = [...metadata.capabilities];
  } catch (error) {
    notify(error.message, "bad");
  } finally {
    modelEdit.inspecting = false;
  }
}

async function saveUser() {
  const result = await action(
    () =>
      api("admin/users/update", {
        method: "POST",
        body: JSON.stringify(userEdit),
      }),
    "用户已更新",
    { key: "user-save", pending: "正在更新用户并同步模型权限…" },
  );
  if (result) document.querySelector("#user-editor")?.close();
}

function toggleUserSelection(userId, checked) {
  const selected = new Set(selectedUserIds.value);
  if (checked) selected.add(userId);
  else selected.delete(userId);
  selectedUserIds.value = [...selected];
}

function toggleCurrentUserPage(checked) {
  const selected = new Set(selectedUserIds.value);
  for (const user of currentAdminUserPage.value) {
    if (checked) selected.add(user.id);
    else selected.delete(user.id);
  }
  selectedUserIds.value = [...selected];
}

function openBulkPolicy() {
  bulkPolicy.scope = selectedUserIds.value.length ? "selected" : "filtered";
  document.querySelector("#bulk-policy-editor")?.showModal();
}

async function saveBulkPolicy() {
  if (!bulkTargetUsers.value.length)
    return notify("当前批量范围没有用户", "bad");
  const payload = {
    user_ids: bulkTargetUsers.value.map((user) => user.id),
  };
  if (bulkPolicy.change_max_sessions)
    payload.max_sessions = bulkPolicy.max_sessions;
  if (bulkPolicy.change_requests_per_minute)
    payload.requests_per_minute = bulkPolicy.requests_per_minute;
  if (bulkPolicy.change_requests_per_day)
    payload.requests_per_day = bulkPolicy.requests_per_day;
  if (Object.keys(payload).length === 1)
    return notify("请至少勾选一个要修改的字段", "bad");
  const result = await action(
    () =>
      api("admin/users/bulk-policy", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    `已更新 ${bulkTargetUsers.value.length} 个用户的调用策略`,
    {
      key: "bulk-user-policy",
      pending: `正在停用、更新并重新同步 ${bulkTargetUsers.value.length} 个用户 Key…`,
    },
  );
  if (!result) return;
  if (result.failed?.length) {
    notify(
      `${result.synced}/${result.updated} 个用户同步成功；${result.failed.length} 个 Key 已保持停用，请查看权限异常。`,
      "bad",
    );
    return;
  }
  selectedUserIds.value = [];
  document.querySelector("#bulk-policy-editor")?.close();
}

async function saveGroup() {
  const result = await action(
    () =>
      api("admin/groups/save", {
        method: "POST",
        body: JSON.stringify(groupEdit),
      }),
    "用户组已保存",
    { key: "group-save", pending: "正在保存用户组并同步模型权限…" },
  );
  if (result) document.querySelector("#group-editor")?.close();
}

async function testModel(model) {
  const result = await action(
    () =>
      api("admin/models/test", {
        method: "POST",
        body: JSON.stringify({ model_id: model.id }),
      }),
    "模型测试通过",
    {
      key: `model-test:${model.id}`,
      pending: "正在执行真实模型请求，最长约 60 秒…",
      refresh: false,
    },
  );
  if (result) {
    model.health_status = result.status;
    model.health_latency_ms = result.latency_ms;
  }
}

async function saveModel() {
  const result = await action(
    () =>
      api("admin/models/save", {
        method: "POST",
        body: JSON.stringify(modelEdit),
      }),
    "模型已测试、保存并同步权限",
    {
      key: "model-save",
      pending: "正在测试模型、更新映射并同步权限，最长约 60 秒…",
    },
  );
  if (result) {
    const sync = result.metadata_sync;
    if (sync?.status === "failed" || sync?.status === "partial")
      notify(
        `模型已保存，但原生参数同步${sync.status === "partial" ? "仅部分成功" : "失败"}：${sync.error || "请重试"}`,
        "bad",
      );
    document.querySelector("#model-editor")?.close();
  }
}

function registrationPayload() {
  return {
    scope: "registration",
    registration_enabled:
      settings.registration_enabled === "1" ||
      settings.registration_enabled === true,
    allowed_domains: settings.allowed_domains,
    default_welcome_balance: settings.default_welcome_balance,
    default_max_sessions: settings.default_max_sessions,
    default_requests_per_minute: settings.default_requests_per_minute,
    default_requests_per_day: settings.default_requests_per_day,
    public_url: settings.public_url,
    api_public_url: settings.api_public_url,
  };
}

function publishingPayload() {
  return {
    scope: "publishing",
    portal_title: settings.portal_title || "LLMCtl",
    published_origin: settings.published_origin || "",
  };
}

function smtpPayload() {
  return {
    scope: "smtp",
    smtp_host: settings.smtp_host,
    smtp_port: settings.smtp_port,
    smtp_security: settings.smtp_security,
    smtp_username: settings.smtp_username,
    smtp_password: settings.smtp_password,
    smtp_from: settings.smtp_from,
  };
}

async function saveRegistration() {
  await action(
    () =>
      api("admin/settings", {
        method: "POST",
        body: JSON.stringify(registrationPayload()),
      }),
    "注册设置已保存",
    { key: "registration-save", pending: "正在验证并保存注册设置…" },
  );
}

async function savePublishing() {
  await action(
    () =>
      api("admin/settings", {
        method: "POST",
        body: JSON.stringify(publishingPayload()),
      }),
    settings.published_origin
      ? "公开基准地址已保存"
      : "已恢复自动使用当前访问地址",
    { key: "publishing-save", pending: "正在验证并保存公开基准地址…" },
  );
}

async function saveSmtp() {
  await action(
    () =>
      api("admin/settings", {
        method: "POST",
        body: JSON.stringify(smtpPayload()),
      }),
    "SMTP 设置已保存",
    { key: "smtp-save", pending: "正在验证并保存 SMTP 设置…" },
  );
}

async function testSmtp() {
  await action(
    () =>
      api("admin/smtp/test", {
        method: "POST",
        body: JSON.stringify({
          ...smtpPayload(),
          recipient: smtpTestRecipient.value,
        }),
      }),
    "测试邮件已发送",
    {
      key: "smtp-test",
      pending: "正在使用当前表单配置连接 SMTP 并发送测试邮件…",
      refresh: false,
    },
  );
}

onMounted(async () => {
  try {
    await load();
    usageRefreshTimer = window.setInterval(() => {
      if (
        section.value !== "billing" ||
        document.hidden ||
        busy.value ||
        usageRefreshing.value
      )
        return;
      syncUsageAndRefresh({
        reconcile: !isAdmin.value,
        preservePage: true,
        silent: true,
      });
    }, 15_000);
    stressRefreshTimer = window.setInterval(pollStressRun, 2_000);
    analyticsRefreshTimer = window.setInterval(() => {
      if (
        !isAdmin.value ||
        section.value !== "overview" ||
        document.hidden ||
        busy.value ||
        analyticsLoading.value
      )
        return;
      loadAdminAnalytics(analyticsFilters.active_page, { silent: true });
    }, 30_000);
  } catch (error) {
    notify(error.message, "bad");
  }
});

onBeforeUnmount(() => {
  if (usageRefreshTimer) window.clearInterval(usageRefreshTimer);
  if (stressRefreshTimer) window.clearInterval(stressRefreshTimer);
  if (analyticsRefreshTimer) window.clearInterval(analyticsRefreshTimer);
  if (chatTimer) window.clearInterval(chatTimer);
  if (toastTimer) window.clearTimeout(toastTimer);
});
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark">{{ portalInitial }}</span>
        <div>
          <strong>{{ portalTitle }} 模型服务门户</strong
          ><small>模型访问 · API Key · 额度与用量</small>
        </div>
      </div>
      <div class="top-actions" v-if="session?.authenticated">
        <span class="identity">{{
          session.user.login_name || session.user.email
        }}</span
        ><button class="ghost" @click="logout">退出</button>
      </div>
    </header>

    <div v-if="toast.text" class="toast" :class="toast.kind" role="status">
      <span>{{ toast.text }}</span
      ><button type="button" aria-label="关闭提示" @click="dismissToast">
        ×
      </button>
    </div>

    <main v-if="!session?.authenticated" class="auth-page">
      <section class="auth-copy">
        <h1>{{ portalTitle }}<br /><em>模型服务门户</em></h1>
        <p>查看已授权模型、管理调用凭据，并核对额度、价格与请求用量。</p>
        <div class="trust-row">
          <span>✓ OpenAI 兼容 API</span><span>✓ 细粒度模型授权</span
          ><span>✓ 用量与审计</span>
        </div>
      </section>
      <section class="auth-card">
        <div class="segmented">
          <button
            :class="{ active: authMode === 'login' }"
            @click="authMode = 'login'"
          >
            登录</button
          ><button
            :disabled="!publicConfig.registration_enabled"
            :class="{ active: authMode === 'register' }"
            @click="authMode = 'register'"
          >
            注册
          </button>
        </div>
        <template v-if="authMode === 'verify'"
          ><h2>验证邮箱</h2>
          <p class="muted">确认后会创建个人 API Key，明文仅显示一次。</p>
          <label>验证令牌<input v-model="auth.token" /></label
          ><button class="primary wide-button" :disabled="busy" @click="verify">
            确认并开通
          </button></template
        >
        <form
          v-else
          @submit.prevent="authMode === 'login' ? login() : register()"
        >
          <h2>{{ authMode === "login" ? "欢迎回来" : `创建 ${portalTitle} 账户` }}</h2>
          <p class="muted" v-if="authMode === 'register'">
            允许注册邮箱：{{
              allowedRegistrationEmails || "管理员尚未配置"
            }}
          </p>
          <label
            >{{ authMode === "login" ? "登录名或邮箱" : "邮箱" }}<input
              v-model="auth.identity"
              :type="authMode === 'login' ? 'text' : 'email'"
              autocomplete="username"
              required
          /></label>
          <label
            >密码<input
              v-model="auth.password"
              type="password"
              :minlength="authMode === 'register' ? 8 : undefined"
              maxlength="200"
              :autocomplete="
                authMode === 'register' ? 'new-password' : 'current-password'
              "
              required
          /></label>
          <label v-if="authMode === 'register'"
            >确认密码<input
              v-model="auth.confirm"
              type="password"
              minlength="8"
              maxlength="200"
              autocomplete="new-password"
              required
          /></label>
          <p class="muted" v-if="authMode === 'register'">
            8-200 个字符，不能为纯数字；邮件发送后请点击其中的验证链接。
          </p>
          <button class="primary wide-button" :disabled="busy">
            {{ authMode === "login" ? "登录" : "发送验证邮件" }}
          </button>
        </form>
        <p class="footnote">注册范围、邮箱域名和初始额度由服务管理员配置。</p>
      </section>
    </main>

    <div v-else class="workspace">
      <aside class="sidebar">
        <div class="role-pill">
          {{ isAdmin ? `${portalTitle} 管理台` : "用户工作台" }}
        </div>
        <nav>
          <button
            v-for="item in nav"
            :key="item[0]"
            :class="{ active: section === item[0] }"
            @click="selectSection(item[0])"
          >
            <span class="nav-dot"></span>{{ item[1] }}
          </button>
        </nav>
        <div class="side-note">
          <strong>请求直达推理 API</strong>
          <p>/v1 请求不经过账户管理服务。</p>
        </div>
      </aside>
      <main class="content">
        <div v-if="workspaceRefreshing" class="refresh-indicator" role="status">
          正在更新数据…
        </div>
        <div v-if="isAdmin && admin?.gateway_error" class="warning">
          <strong>AI 接入层当前不可用。</strong
          >用户、SMTP、账本和审计仍可查看；涉及模型、Key
          或实时对账的操作请在服务恢复后执行。技术详情请查看审计日志或运行
          <code>llmctl logs account</code>。
        </div>
        <template v-if="!isAdmin && dashboard">
          <section v-if="section === 'overview'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">YOUR ACCOUNT</span>
                <h1>工作台</h1>
                <p>现金余额和近期使用情况。</p>
              </div>
              <span class="status ok">服务可用</span>
            </div>
            <div class="metrics">
              <article>
                <span>金额余额</span><strong>${{ dashboard.balance }}</strong
                ><small>付费模型消费</small>
              </article>
              <article>
                <span>可用模型</span
                ><strong>{{ dashboard.models.length }}</strong
                ><small>按个人与用户组授权</small>
              </article>
              <article>
                <span>累计扣款</span
                ><strong>{{ money(dashboard.total_spent_micros || 0) }}</strong
                ><small>按模型实际 Token 用量结算</small>
              </article>
              <article>
                <span>已记录请求</span
                ><strong>{{ dashboard.usage_pagination?.total || 0 }}</strong
                ><small>LLMCtl 用量账本</small>
              </article>
            </div>
            <section class="panel">
              <div class="panel-head"><h2>最近请求</h2></div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>模型</th>
                      <th>输入 / 输出</th>
                      <th>标价 / 余额扣款</th>
                      <th>请求内容</th>
                    </tr>
                  </thead>
                  <tbody>
                    <template
                      v-for="row in dashboard.usage.slice(0, 8)"
                      :key="row.id"
                      ><tr>
                        <td>{{ date(row.occurred_at) }}</td>
                        <td>
                          <code>{{ row.public_model_id }}</code>
                        </td>
                        <td>
                          {{ row.input_tokens }} / {{ row.output_tokens }}
                        </td>
                        <td>
                          {{ money(row.gross_amount_micros) }} /
                          {{ money(row.amount_micros) }}
                        </td>
                        <td>
                          <button
                            class="ghost"
                            @click="toggleRequestDetail(row)"
                          >
                            {{
                              requestDetails[row.request_id]?.loading
                                ? "读取中…"
                                : requestDetails[row.request_id]?.expanded
                                  ? "收起"
                                  : "查看"
                            }}
                          </button>
                        </td>
                      </tr>
                      <tr
                        v-if="requestDetails[row.request_id]?.expanded"
                        class="request-detail-row"
                      >
                        <td colspan="5">
                          <div
                            v-if="requestDetails[row.request_id].error"
                            class="error-text"
                          >
                            {{ requestDetails[row.request_id].error }}
                          </div>
                          <div
                            v-else-if="requestDetails[row.request_id].loading"
                            class="muted"
                          >
                            正在从用量日志读取请求内容…
                          </div>
                          <div
                            v-else-if="
                              !requestDetails[row.request_id].available
                            "
                            class="muted"
                          >
                            该请求没有保留可显示的文本内容，可能启用了 noLog
                            或日志详情已经清理。
                          </div>
                          <div v-else class="request-messages">
                            <article
                              v-for="(message, index) in requestDetails[
                                row.request_id
                              ].messages"
                              :key="index"
                            >
                              <strong>{{ message.role }}</strong>
                              <pre>{{ message.content }}</pre>
                            </article>
                            <small
                              v-if="requestDetails[row.request_id].truncated"
                              >内容过长，当前仅显示前 20,000 个字符。</small
                            >
                          </div>
                        </td>
                      </tr></template
                    >
                    <tr v-if="!dashboard.usage.length">
                      <td colspan="6" class="empty">尚无用量</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </section>
          </section>

          <section v-if="section === 'models'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">MODEL CATALOG</span>
                <h1>模型广场</h1>
                <p>这里只展示你真正有权限调用的模型。</p>
              </div>
            </div>
            <section class="panel catalog-key">
              <div>
                <strong>个人 API Key</strong
                ><small
                  >登录后自动读取注册时创建的同一把 Key，并内置到 curl
                  示例；只有手工轮换才会更换。</small
                >
              </div>
              <div class="catalog-key-input">
                <input
                  ref="apiKeyField"
                  v-model="keyOnce"
                  :type="showApiKey ? 'text' : 'password'"
                  :placeholder="keyLoading ? '正在读取现有 Key…' : '现有 Key 暂不可用'"
                  autocomplete="off"
                  readonly
                /><button
                  type="button"
                  class="ghost"
                  :disabled="!keyOnce"
                  @click="showApiKey = !showApiKey"
                >
                  {{ showApiKey ? "隐藏" : "显示" }}</button
                ><button
                  type="button"
                  class="ghost"
                  :disabled="!keyOnce"
                  @click="copy(keyOnce, { revealOnFailure: true })"
                >
                  复制 Key</button
                ><button
                  type="button"
                  class="ghost"
                  v-if="!keyOnce"
                  :disabled="keyLoading"
                  @click="loadExistingKey"
                >
                  {{ keyLoading ? "读取中…" : "重新读取" }}
                </button>
              </div>
            </section>
            <ListFilterBar
              v-model="listFilters['user-models'].query"
              :count="filteredRows('user-models', dashboard.models).length"
              placeholder="搜索模型 ID、名称或描述"
            />
            <div class="model-grid">
              <article
                class="model-card"
                v-for="model in pageRows(
                  'user-models',
                  filteredRows('user-models', dashboard.models),
                )"
                :key="model.id"
              >
                <div class="model-title">
                  <span class="model-icon">AI</span>
                  <div>
                    <h3>{{ model.display_name }}</h3>
                    <code>{{ model.public_model_id }}</code>
                  </div>
                  <button
                    type="button"
                    class="icon-button"
                    @click="copy(model.public_model_id)"
                  >
                    复制 ID
                  </button>
                </div>
                <p>{{ model.description || "暂无描述" }}</p>
                <div class="chips">
                  <span v-for="cap in model.capabilities" :key="cap">{{
                    cap
                  }}</span>
                </div>
                <div class="price-grid">
                  <span
                    >输入<strong>${{ model.input_price }}/1M</strong></span
                  ><span
                    >输出<strong>${{ model.output_price }}/1M</strong></span
                  ><span
                    >缓存<strong>${{ model.cached_price }}/1M</strong></span
                  ><span
                    >上下文<strong>{{
                      model.context_window_tokens
                        ? Number(model.context_window_tokens).toLocaleString()
                        : "未知"
                    }}</strong></span
                  ><span
                    >最大输出<strong>{{
                      model.max_output_tokens
                        ? Number(model.max_output_tokens).toLocaleString()
                        : "未知"
                    }}</strong></span
                  >
                </div>
                <p class="muted">
                  当前现金余额折算：纯输入
                  {{ cashTokenCapacity(model.input_price) }}；纯输出
                  {{ cashTokenCapacity(model.output_price) }}。实际扣款按本次请求的输入、输出、缓存和思考 Token 分项计算。
                </p>
                <details>
                  <summary>查看 curl 示例</summary>
                  <pre>{{ curlFor(model) }}</pre>
                  <button
                    type="button"
                    class="ghost"
                    @click="copy(curlFor(model))"
                  >
                    复制完整示例
                  </button>
                </details>
              </article>
            </div>
            <PaginationBar
              :page="
                pageNumber(
                  'user-models',
                  filteredRows('user-models', dashboard.models),
                )
              "
              :pages="pageCount(filteredRows('user-models', dashboard.models))"
              :total="filteredRows('user-models', dashboard.models).length"
              @previous="
                changePage(
                  'user-models',
                  filteredRows('user-models', dashboard.models),
                  -1,
                )
              "
              @next="
                changePage(
                  'user-models',
                  filteredRows('user-models', dashboard.models),
                  1,
                )
              "
            />
          </section>

          <section v-if="section === 'playground'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">PLAYGROUND</span>
                <h1>在线测试</h1>
                <p>浏览器流式调用 /v1；使用账户固定 Key，手工轮换前持续有效。</p>
              </div>
              <span
                class="status"
                :class="
                  chat.status === 'completed'
                    ? 'ok'
                    : chat.status === 'failed'
                      ? 'bad'
                      : 'warn'
                "
                >{{
                  {
                    idle: "待请求",
                    connecting: "连接中",
                    generating: "生成中",
                    completed: "已完成",
                    stopped: "已停止",
                    failed: "失败",
                  }[chat.status]
                }}</span
              >
            </div>
            <div class="chat-layout">
              <section class="panel form-stack playground-controls">
                <label
                  >模型<select v-model="chat.model" :disabled="chat.sending">
                    <option
                      v-for="model in dashboard.models"
                      :value="model.public_model_id"
                    >
                      {{ model.public_model_id }}
                    </option>
                  </select></label
                ><label
                  >API Key<input
                    v-model="keyOnce"
                    type="password"
                    :placeholder="keyLoading ? '正在读取现有 Key…' : '现有 Key 暂不可用'"
                    autocomplete="off"
                    readonly
                /></label>
                <div class="playground-options">
                  <label
                    >思考模式<select
                      v-model="chat.thinking"
                      :disabled="chat.sending"
                    >
                      <option value="auto">自动（推荐）</option>
                      <option value="enabled">开启</option>
                      <option value="disabled">关闭</option>
                    </select></label
                  ><label
                    >最大输出 Token<input
                      v-model.number="chat.maxTokens"
                      type="number"
                      min="1"
                      max="32768"
                      :disabled="chat.sending" /></label
                  ><label
                    >温度<input
                      v-model.number="chat.temperature"
                      type="number"
                      min="0"
                      max="2"
                      step="0.1"
                      :disabled="chat.sending"
                  /></label>
                </div>
                <label
                  >消息<textarea
                    v-model="chat.prompt"
                    rows="7"
                    :disabled="chat.sending"
                    @keydown.ctrl.enter.prevent="sendChat"
                  ></textarea>
                </label>
                <div
                  class="attachment-dropzone"
                  :class="{ disabled: chat.sending || chat.preparingAttachments }"
                  @dragover.prevent
                  @drop.prevent="addAttachmentFiles"
                >
                  <input
                    ref="attachmentInput"
                    class="visually-hidden"
                    type="file"
                    multiple
                    :accept="ACCEPTED_ATTACHMENTS"
                    :disabled="chat.sending || chat.preparingAttachments"
                    @change="addAttachmentFiles"
                  />
                  <div>
                    <strong>附件</strong>
                    <small>图片、PDF、TXT、Markdown、CSV、JSON；单个 12 MiB</small>
                  </div>
                  <button
                    type="button"
                    class="ghost compact"
                    :disabled="chat.sending || chat.preparingAttachments"
                    @click="attachmentInput?.click()"
                  >
                    {{ chat.preparingAttachments ? "处理中…" : "选择文件" }}
                  </button>
                </div>
                <p class="field-hint" v-if="!selectedChatSupportsVision">
                  当前模型未声明图片/OCR 能力；仍可上传文本类附件。
                </p>
                <div class="attachment-list" v-if="chat.attachments.length">
                  <article
                    v-for="attachment in chat.attachments"
                    :key="attachment.id"
                  >
                    <img
                      v-if="attachment.kind === 'image'"
                      :src="attachment.dataUrl"
                      alt=""
                    />
                    <span v-else class="attachment-icon">{{
                      attachment.kind === "pdf" ? "PDF" : "TXT"
                    }}</span>
                    <div>
                      <strong>{{ attachment.name }}</strong>
                      <small>
                        {{ attachmentSize(attachment.size) }}
                        <template v-if="attachment.kind === 'pdf'">
                          · {{ attachment.pages.length }}/{{ attachment.pageCount }} 页
                        </template>
                      </small>
                    </div>
                    <button
                      type="button"
                      class="icon-button"
                      :disabled="chat.sending || chat.preparingAttachments"
                      :aria-label="`移除 ${attachment.name}`"
                      @click="removeAttachment(attachment.id)"
                    >
                      ×
                    </button>
                  </article>
                </div>
                <p class="field-hint">
                  PDF 会在浏览器内转成页面图片后直传 /v1；附件不会上传到门户服务器或写入门户数据库。
                </p>
                <div class="button-row">
                  <button
                    class="primary playground-send"
                    :disabled="chat.sending || chat.preparingAttachments"
                    @click="sendChat"
                  >
                    发送请求 <small>Ctrl + Enter</small></button
                  ><button v-if="chat.sending" class="danger" @click="stopChat">
                    停止生成
                  </button>
                </div>
              </section>
              <section class="playground-output">
                <div class="stream-metrics">
                  <article>
                    <span>首字延迟</span
                    ><strong>{{
                      chat.ttftMs === null
                        ? "—"
                        : `${(chat.ttftMs / 1000).toFixed(2)}s`
                    }}</strong>
                  </article>
                  <article>
                    <span>总耗时</span
                    ><strong>{{
                      chat.elapsedMs === null
                        ? "—"
                        : `${(chat.elapsedMs / 1000).toFixed(2)}s`
                    }}</strong>
                  </article>
                  <article>
                    <span>输出速度</span
                    ><strong>{{
                      chat.tokensPerSecond === null
                        ? "—"
                        : `${chat.tokensPerSecond.toFixed(1)} tok/s`
                    }}</strong>
                  </article>
                  <article class="token-metric-card">
                    <span>Token 用量</span>
                    <div class="token-metric-values">
                      <b
                        ><small>输入 Token</small
                        >{{ chat.inputTokens === null ? "—" : chat.inputTokens }}</b
                      ><b
                        ><small>输出 Token</small
                        >{{ chat.outputTokens === null ? "—" : chat.outputTokens }}</b
                      ><b
                        ><small>合计 Token</small
                        >{{ chat.totalTokens === null ? "—" : chat.totalTokens }}</b
                      >
                    </div>
                  </article>
                </div>
                <section
                  class="panel reasoning-panel"
                  :class="{ streaming: chat.sending }"
                  v-if="
                    chat.reasoning ||
                    chat.sending ||
                    chat.status === 'completed'
                  "
                >
                  <div class="answer-head">
                    <span class="eyebrow">思考过程</span
                    ><span v-if="chat.reasoningTokens !== null"
                      >{{ chat.reasoningTokens }} tokens</span
                    >
                  </div>
                  <pre>{{
                    chat.reasoning ||
                    (chat.sending
                      ? "等待模型返回思考内容…"
                      : "当前模型或接入层没有单独返回思考内容。")
                  }}</pre>
                </section>
                <section class="panel answer">
                  <div class="answer-head">
                    <span class="eyebrow">最终回答</span
                    ><span v-if="chat.responseModel">{{
                      chat.responseModel
                    }}</span>
                  </div>
                  <pre>{{
                    chat.result ||
                    (chat.sending
                      ? "正在等待模型输出…"
                      : "模型回复会显示在这里。")
                  }}</pre>
                  <div
                    class="response-meta"
                    v-if="chat.requestId || chat.provider || chat.cost"
                  >
                    <span v-if="chat.provider">供应商 {{ chat.provider }}</span
                    ><span v-if="chat.cost">请求成本 {{ chat.cost }}</span
                    ><span v-if="chat.cacheHit"
                      >缓存
                      {{ chat.cacheHit === "true" ? "命中" : "未命中" }}</span
                    ><code v-if="chat.requestId">{{ chat.requestId }}</code>
                  </div>
                </section>
              </section>
            </div>
          </section>

          <section v-if="section === 'billing'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">USAGE & BILLING</span>
                <h1>用量与账单</h1>
                <p>按模型输入、输出、缓存与思考 Token 的价格，从现金余额实时结算；余额耗尽后 API Key 停止授权。</p>
              </div>
              <button
                class="ghost"
                type="button"
                :disabled="usageRefreshing"
                @click="
                  syncUsageAndRefresh({
                    announce: true,
                    preservePage: false,
                  })
                "
              >
                {{ usageRefreshing ? "更新中…" : "刷新用量" }}
              </button>
            </div>
            <section v-if="dashboard.grants.length" class="panel">
              <h2>历史 Token 赠额折现</h2>
              <p class="muted">
                旧版剩余 Token 已按升级时的模型最高分类单价一次性折算为现金；原记录仅用于审计，不再参与后续扣费。
              </p>
              <ListFilterBar
                v-model="listFilters['user-grants'].query"
                v-model:status="listFilters['user-grants'].status"
                v-model:category="listFilters['user-grants'].category"
                :status-options="[
                  { value: 'active', label: '有效' },
                  { value: 'disabled', label: '已停用' },
                  { value: 'expired', label: '已过期' },
                ]"
                status-label="全部状态"
                :category-options="resetOptions"
                category-label="全部重置周期"
                :count="filteredRows('user-grants', dashboard.grants).length"
                placeholder="搜索历史赠额名称或模型"
              />
              <div class="grant-list">
                <div
                  v-for="grant in pageRows(
                    'user-grants',
                    filteredRows('user-grants', dashboard.grants),
                  )"
                  :key="grant.id"
                >
                  <div>
                    <strong>{{ grant.label }}</strong
                    ><small
                      >{{ grant.model_id ? "指定模型" : "所有模型" }} ·
                      {{ resetLabel(grant.reset_interval) }}</small
                    >
                  </div>
                  <div class="grant-number">
                    <template v-if="grant.converted_at">
                      {{ grant.tokens_initial.toLocaleString() }} Token →
                      {{ money(grant.converted_amount_micros || 0) }}
                    </template>
                    <template v-else>
                      待处理 {{ grant.tokens_remaining.toLocaleString() }} Token
                    </template>
                  </div>
                </div>
              </div>
              <PaginationBar
                :page="
                  pageNumber(
                    'user-grants',
                    filteredRows('user-grants', dashboard.grants),
                  )
                "
                :pages="
                  pageCount(filteredRows('user-grants', dashboard.grants))
                "
                :total="filteredRows('user-grants', dashboard.grants).length"
                @previous="
                  changePage(
                    'user-grants',
                    filteredRows('user-grants', dashboard.grants),
                    -1,
                  )
                "
                @next="
                  changePage(
                    'user-grants',
                    filteredRows('user-grants', dashboard.grants),
                    1,
                  )
                "
              />
            </section>
            <section class="panel">
              <h2>请求用量</h2>
              <div class="list-filter" role="search">
                <select
                  v-model="usageFilters.model"
                  aria-label="按模型筛选请求用量"
                  @change="applyUsageFilters"
                >
                  <option value="">全部模型</option>
                  <option
                    v-for="model in dashboard.models"
                    :key="model.id"
                    :value="model.public_model_id"
                  >
                    {{ model.public_model_id }}
                  </option>
                </select>
                <span class="filter-count"
                  >{{ dashboard.usage_pagination?.total || 0 }} 条</span
                >
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>模型</th>
                      <th>输入 / 输出</th>
                      <th>历史赠额 Token</th>
                      <th>标价</th>
                      <th>赠额抵扣</th>
                      <th>余额扣款</th>
                      <th>请求内容</th>
                    </tr>
                  </thead>
                  <tbody>
                    <template v-for="row in dashboard.usage" :key="row.id"
                      ><tr>
                        <td>{{ date(row.occurred_at) }}</td>
                        <td>
                          <code>{{ row.public_model_id }}</code>
                        </td>
                        <td>
                          {{ row.input_tokens }} / {{ row.output_tokens }}
                        </td>
                        <td>{{ row.granted_tokens }}</td>
                        <td>{{ money(row.gross_amount_micros) }}</td>
                        <td>{{ money(row.grant_amount_micros) }}</td>
                        <td>{{ money(row.amount_micros) }}</td>
                        <td>
                          <button
                            class="ghost"
                            @click="toggleRequestDetail(row)"
                          >
                            {{
                              requestDetails[row.request_id]?.loading
                                ? "读取中…"
                                : requestDetails[row.request_id]?.expanded
                                  ? "收起"
                                  : "查看"
                            }}
                          </button>
                        </td>
                      </tr>
                      <tr
                        v-if="requestDetails[row.request_id]?.expanded"
                        class="request-detail-row"
                      >
                        <td colspan="8">
                          <div
                            v-if="requestDetails[row.request_id].error"
                            class="error-text"
                          >
                            {{ requestDetails[row.request_id].error }}
                          </div>
                          <div
                            v-else-if="requestDetails[row.request_id].loading"
                            class="muted"
                          >
                            正在从用量日志读取请求内容…
                          </div>
                          <div
                            v-else-if="
                              !requestDetails[row.request_id].available
                            "
                            class="muted"
                          >
                            该请求没有保留可显示的文本内容，可能启用了 noLog
                            或日志详情已经清理。
                          </div>
                          <div v-else class="request-messages">
                            <article
                              v-for="(message, index) in requestDetails[
                                row.request_id
                              ].messages"
                              :key="index"
                            >
                              <strong>{{ message.role }}</strong>
                              <pre>{{ message.content }}</pre>
                            </article>
                            <small
                              v-if="requestDetails[row.request_id].truncated"
                              >内容过长，当前仅显示前 20,000 个字符。</small
                            >
                          </div>
                        </td>
                      </tr></template
                    >
                    <tr v-if="!dashboard.usage.length">
                      <td colspan="8" class="empty">尚无请求用量</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <PaginationBar
                :page="dashboard.usage_pagination?.page || 1"
                :pages="dashboard.usage_pagination?.pages || 1"
                :total="dashboard.usage_pagination?.total || 0"
                @previous="changeUsagePage(-1)"
                @next="changeUsagePage(1)"
              />
            </section>
            <section class="panel">
              <h2>金额流水</h2>
              <p class="muted">
                注册赠款、管理员调整、旧 Token 折现和每次模型调用扣费都会形成可审计流水。
              </p>
              <ListFilterBar
                v-model="listFilters['user-billing'].query"
                v-model:category="listFilters['user-billing'].category"
                :category-options="kindOptions"
                category-label="全部流水类型"
                :count="
                  filteredRows('user-billing', dashboard.transactions).length
                "
                placeholder="搜索类型或备注"
              />
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>类型</th>
                      <th>变动</th>
                      <th>余额</th>
                      <th>备注</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="row in pageRows(
                        'user-billing',
                        filteredRows('user-billing', dashboard.transactions),
                      )"
                      :key="row.id"
                    >
                      <td>{{ date(row.created_at) }}</td>
                      <td>{{ kindLabel(row.kind) }}</td>
                      <td>{{ money(row.amount_micros) }}</td>
                      <td>{{ money(row.balance_after_micros) }}</td>
                      <td>{{ row.note }}</td>
                    </tr>
                    <tr v-if="!dashboard.transactions.length">
                      <td colspan="5" class="empty">
                        暂无金额流水
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <PaginationBar
                :page="
                  pageNumber(
                    'user-billing',
                    filteredRows('user-billing', dashboard.transactions),
                  )
                "
                :pages="
                  pageCount(
                    filteredRows('user-billing', dashboard.transactions),
                  )
                "
                :total="
                  filteredRows('user-billing', dashboard.transactions).length
                "
                @previous="
                  changePage(
                    'user-billing',
                    filteredRows('user-billing', dashboard.transactions),
                    -1,
                  )
                "
                @next="
                  changePage(
                    'user-billing',
                    filteredRows('user-billing', dashboard.transactions),
                    1,
                  )
                "
              />
            </section>
          </section>

          <section v-if="section === 'keys'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">API SECURITY</span>
                <h1>API Key</h1>
                <p>注册时创建并保持不变；只有你手工轮换后旧 Key 才会失效。</p>
              </div>
            </div>
            <section class="panel key-panel">
              <label
                >个人 API Key<input
                  ref="apiKeyField"
                  v-model="keyOnce"
                  :type="showApiKey ? 'text' : 'password'"
                  readonly
                  :placeholder="keyLoading ? '正在读取现有 Key…' : '现有 Key 暂不可用'"
              /></label>
              <div class="button-row">
                <button
                  class="ghost"
                  :disabled="!keyOnce"
                  type="button"
                  @click="copy(keyOnce, { revealOnFailure: true })"
                >
                  复制</button
                ><button
                  class="ghost"
                  type="button"
                  :disabled="!keyOnce"
                  @click="showApiKey = !showApiKey"
                >
                  {{ showApiKey ? "隐藏" : "显示" }}</button
                ><button
                  class="ghost"
                  type="button"
                  v-if="!keyOnce"
                  :disabled="keyLoading"
                  @click="loadExistingKey"
                >
                  {{ keyLoading ? "读取中…" : "重新读取现有 Key" }}</button
                ><button
                  class="danger"
                  type="button"
                  :disabled="busy"
                  @click="rotateKey"
                >
                  轮换并撤销旧 Key
                </button>
              </div>
              <div class="warning">
                登录不会创建或更换 Key。curl 示例会自动填入这把 Key；请不要把它
                写入前端源码、工单或聊天记录，生产应用应从密钥管理系统读取。
              </div>
            </section>
          </section>
        </template>

        <template v-if="isAdmin && admin">
          <section v-if="section === 'overview'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">LLM OPERATIONS</span>
                <h1>运行总览</h1>
                <p>集中查看 LLMCtl 用户、模型、额度和运行状态。</p>
              </div>
              <div class="button-row">
                <button
                  class="ghost"
                  @click="
                    action(
                      () =>
                        api('admin/permissions/reconcile', {
                          method: 'POST',
                          body: '{}',
                        }),
                      '权限已对账',
                    )
                  "
                >
                  权限对账</button
                ><button
                  class="primary"
                  @click="
                    action(
                      () =>
                        api('admin/billing/reconcile', {
                          method: 'POST',
                          body: '{}',
                        }),
                      '用量已对账',
                    )
                  "
                >
                  立即结算
                </button>
              </div>
            </div>
            <section class="panel analytics-toolbar">
              <div>
                <strong>用量观察范围</strong>
                <small>按已结算请求统计；切换模型会同步更新全部指标。</small>
              </div>
              <select
                v-model="analyticsFilters.range"
                aria-label="统计时间范围"
                @change="changeAnalyticsFilters"
              >
                <option value="today">今日（按小时）</option>
                <option value="7d">近 7 天（按天）</option>
                <option value="30d">近 30 天（按天）</option>
                <option value="12m">近 12 个月（按月）</option>
              </select>
              <select
                v-model="analyticsFilters.model"
                aria-label="按模型筛选运行统计"
                @change="changeAnalyticsFilters"
              >
                <option value="">全部模型</option>
                <option
                  v-for="model in admin.models"
                  :key="model.id"
                  :value="model.public_model_id"
                >
                  {{ model.public_model_id }}
                </option>
              </select>
              <button
                type="button"
                class="ghost"
                :disabled="analyticsLoading"
                @click="loadAdminAnalytics(analyticsFilters.active_page)"
              >
                {{ analyticsLoading ? "刷新中…" : "刷新统计" }}
              </button>
            </section>

            <template v-if="adminAnalytics">
              <div class="metrics operations-kpis">
                <article>
                  <span>{{ adminAnalytics.range.label }}总 Token</span>
                  <strong>{{ formatTokens(adminAnalytics.summary.total_tokens) }}</strong>
                  <small>输入 + 输出，不重复计算缓存与思考</small>
                </article>
                <article>
                  <span>{{ adminAnalytics.range.label }}请求</span>
                  <strong>{{ formatTokens(adminAnalytics.summary.requests) }}</strong>
                  <small
                    >平均
                    {{ formatTokens(adminAnalytics.summary.average_tokens_per_request) }}
                    Token / 请求</small
                  >
                </article>
                <article>
                  <span>{{ adminAnalytics.range.label }}活跃用户</span>
                  <strong>{{ formatTokens(adminAnalytics.summary.active_users) }}</strong>
                  <small>至少产生 1 条已结算请求</small>
                </article>
                <article>
                  <span>{{ adminAnalytics.range.label }}余额扣款</span>
                  <strong>{{ money(adminAnalytics.summary.amount_micros) }}</strong>
                  <small>按请求发生时的模型价格快照结算</small>
                </article>
              </div>

              <section class="panel usage-trend-panel">
                <div class="panel-head analytics-panel-head">
                  <div>
                    <h2>Token 用量趋势</h2>
                    <p>
                      {{ adminAnalytics.range.label }} ·
                      {{ analyticsFilters.model || "全部模型" }}
                    </p>
                  </div>
                  <div class="chart-legend" aria-label="图例">
                    <span><i class="input"></i>输入</span>
                    <span><i class="output"></i>输出</span>
                  </div>
                </div>
                <div class="usage-composition" aria-label="Token 构成">
                  <span
                    ><b>{{ formatTokens(adminAnalytics.summary.input_tokens) }}</b
                    >输入 Token</span
                  ><span
                    ><b>{{ formatTokens(adminAnalytics.summary.output_tokens) }}</b
                    >输出 Token</span
                  ><span
                    ><b>{{ formatTokens(adminAnalytics.summary.cached_tokens) }}</b
                    >缓存命中 Token（输入子集）</span
                  ><span
                    ><b>{{ formatTokens(adminAnalytics.summary.reasoning_tokens) }}</b
                    >思考 Token（输出子集）</span
                  >
                </div>
                <div
                  class="usage-chart"
                  role="img"
                  :aria-label="`${adminAnalytics.range.label} Token 用量柱状图`"
                >
                  <div
                    v-for="(point, index) in adminAnalytics.timeseries"
                    :key="point.start_at"
                    class="chart-slot"
                    :title="`${point.label}：输入 ${formatTokens(point.input_tokens)}，输出 ${formatTokens(point.output_tokens)}，请求 ${formatTokens(point.requests)}`"
                  >
                    <div class="chart-value">
                      {{ point.total_tokens ? compactTokens(point.total_tokens) : "" }}
                    </div>
                    <div class="chart-column">
                      <i
                        class="chart-segment output"
                        :style="{ height: analyticsSegmentHeight(point.output_tokens, analyticsMaxTokens) }"
                      ></i>
                      <i
                        class="chart-segment input"
                        :style="{ height: analyticsSegmentHeight(point.input_tokens, analyticsMaxTokens) }"
                      ></i>
                    </div>
                    <small v-if="analyticsLabelVisible(index, adminAnalytics.timeseries.length)">
                      {{ point.label }}
                    </small>
                  </div>
                </div>
                <p class="analytics-source">
                  数据源：LLMCtl 已结算用量账本 · 时区 {{ adminAnalytics.timezone }} ·
                  通常延迟约 {{ adminAnalytics.settlement_lag_seconds }} 秒 ·
                  更新于 {{ date(adminAnalytics.generated_at) }}
                </p>
              </section>

              <div class="analytics-grid">
                <section class="panel ranking-panel">
                  <div class="panel-head">
                    <div>
                      <h2>Token 用量 Top 10</h2>
                      <p>按所选时间和模型范围排序。</p>
                    </div>
                  </div>
                  <div v-if="adminAnalytics.top_users.length" class="ranking-list">
                    <button
                      v-for="(user, index) in adminAnalytics.top_users"
                      :key="user.user_id"
                      type="button"
                      class="ranking-row"
                      @click="analyzeUser(user.user_id)"
                    >
                      <span class="rank">{{ index + 1 }}</span>
                      <span class="ranking-identity"
                        ><strong>{{ user.email }}</strong
                        ><small>{{ formatTokens(user.requests) }} 次请求</small></span
                      >
                      <span class="ranking-value"
                        ><strong>{{ formatTokens(user.total_tokens) }}</strong
                        ><small>{{ user.share_percent }}%</small></span
                      >
                      <i><b :style="{ width: `${user.share_percent}%` }"></b></i>
                    </button>
                  </div>
                  <p v-else class="empty-state">当前范围尚无已结算请求。</p>
                </section>

                <section class="panel active-users-panel">
                  <div class="panel-head">
                    <div>
                      <h2>最近活跃用户</h2>
                      <p>按最后一次已结算请求倒序。</p>
                    </div>
                    <span>{{ adminAnalytics.active_pagination.total }} 人</span>
                  </div>
                  <div class="table-wrap active-users-table">
                    <table>
                      <thead>
                        <tr>
                          <th>用户</th>
                          <th>最后活跃</th>
                          <th>请求</th>
                          <th>Token</th>
                          <th></th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr
                          v-for="user in adminAnalytics.active_users"
                          :key="user.user_id"
                        >
                          <td>{{ user.email }}</td>
                          <td>{{ date(user.last_activity_at) }}</td>
                          <td>{{ formatTokens(user.requests) }}</td>
                          <td>{{ formatTokens(user.total_tokens) }}</td>
                          <td>
                            <button class="ghost" @click="analyzeUser(user.user_id)">
                              分析
                            </button>
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                  <PaginationBar
                    :page="adminAnalytics.active_pagination.page"
                    :pages="adminAnalytics.active_pagination.pages"
                    :total="adminAnalytics.active_pagination.total"
                    @previous="loadAdminAnalytics(adminAnalytics.active_pagination.page - 1)"
                    @next="loadAdminAnalytics(adminAnalytics.active_pagination.page + 1)"
                  />
                </section>
              </div>

              <section id="user-usage-analysis" class="panel user-analysis">
                <div class="panel-head analytics-panel-head">
                  <div>
                    <h2>单用户用量分析</h2>
                    <p>沿用上方时间和模型范围，可按小时、天或月观察。</p>
                  </div>
                  <select
                    v-model="analyticsFilters.user"
                    aria-label="选择要分析的用户"
                    @change="analyzeUser(analyticsFilters.user)"
                  >
                    <option value="">选择用户</option>
                    <option
                      v-for="user in admin.users.filter((item) => item.role === 'user')"
                      :key="user.id"
                      :value="user.id"
                    >
                      {{ user.email }}
                    </option>
                  </select>
                </div>
                <template v-if="adminAnalytics.selected_user">
                  <div class="user-analysis-summary">
                    <span
                      ><b>{{ adminAnalytics.selected_user.email }}</b
                      >当前分析对象</span
                    ><span
                      ><b>{{ formatTokens(adminAnalytics.selected_user.summary.total_tokens) }}</b
                      >总 Token</span
                    ><span
                      ><b>{{ formatTokens(adminAnalytics.selected_user.summary.requests) }}</b
                      >请求数</span
                    ><span
                      ><b>{{ money(adminAnalytics.selected_user.summary.amount_micros) }}</b
                      >余额扣款</span
                    >
                  </div>
                  <div class="usage-chart user-usage-chart" role="img" aria-label="单用户 Token 用量趋势">
                    <div
                      v-for="(point, index) in adminAnalytics.selected_user.timeseries"
                      :key="point.start_at"
                      class="chart-slot"
                      :title="`${point.label}：输入 ${formatTokens(point.input_tokens)}，输出 ${formatTokens(point.output_tokens)}`"
                    >
                      <div class="chart-value">
                        {{ point.total_tokens ? compactTokens(point.total_tokens) : "" }}
                      </div>
                      <div class="chart-column">
                        <i
                          class="chart-segment output"
                          :style="{ height: analyticsSegmentHeight(point.output_tokens, selectedUserAnalyticsMaxTokens) }"
                        ></i>
                        <i
                          class="chart-segment input"
                          :style="{ height: analyticsSegmentHeight(point.input_tokens, selectedUserAnalyticsMaxTokens) }"
                        ></i>
                      </div>
                      <small v-if="analyticsLabelVisible(index, adminAnalytics.selected_user.timeseries.length)">
                        {{ point.label }}
                      </small>
                    </div>
                  </div>
                </template>
                <p v-else class="empty-state">选择一个活跃用户查看其用量趋势。</p>
              </section>
            </template>
            <section v-else class="panel empty-state">
              {{ analyticsLoading ? "正在读取运行统计…" : "运行统计暂不可用。" }}
            </section>

            <details class="panel service-inventory">
              <summary>控制面状态与服务入口</summary>
              <div class="service-facts">
                <span
                  ><b>{{ admin.users.filter((u) => u.role === "user").length }}</b
                  >用户</span
                ><span
                  ><b>{{ admin.models.filter((m) => m.status === "published").length }}</b
                  >开放模型</span
                ><span
                  ><b>{{ admin.free_resources.filter((r) => r.available).length }}</b
                  >可用免费资源</span
                ><span
                  ><b>{{ admin.users.filter((u) => u.permission_status === "failed").length }}</b
                  >权限异常</span
                >
              </div>
              <div class="architecture">
                <div><strong>/ui/</strong><span>LLMCtl 门户</span></div>
                <i>→</i>
                <div><strong>/portal-api/</strong><span>账户与管理 API</span></div>
                <i class="split">↘</i>
                <div><strong>/v1/</strong><span>推理 API</span></div>
              </div>
            </details>
          </section>

          <section v-if="section === 'models'" class="page">
            <div class="page-head">
              <div>
                <h1>模型、映射与定价</h1>
                <p>公开模型 ID、原生参数、价格与访问规则集中管理。</p>
              </div>
              <button
                type="button"
                class="primary"
                :disabled="busy"
                @click="editModel()"
              >
                发布模型
              </button>
            </div>
            <ListFilterBar
              v-model="listFilters['admin-models'].query"
              v-model:status="listFilters['admin-models'].status"
              :status-options="[
                { value: 'published', label: '已发布' },
                { value: 'draft', label: '草稿' },
                { value: 'disabled', label: '已停用' },
              ]"
              status-label="全部发布状态"
              :count="filteredRows('admin-models', admin.models).length"
              placeholder="搜索模型、来源或描述"
            />
            <div class="table-wrap panel">
              <table>
                <thead>
                  <tr>
                    <th>模型</th>
                    <th>来源</th>
                    <th>原生参数</th>
                    <th>价格（入/出）</th>
                    <th>能力</th>
                    <th>状态</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="model in pageRows(
                      'admin-models',
                      filteredRows('admin-models', admin.models),
                    )"
                    :key="model.id"
                    :class="{ 'model-row-disabled': model.status === 'disabled' }"
                  >
                    <td class="model-summary">
                      <div class="model-name-line">
                        <strong>{{ model.display_name }}</strong>
                        <span
                          v-if="model.status === 'disabled'"
                          class="status bad"
                          >已停用</span
                        >
                      </div>
                      <code>{{ model.public_model_id }}</code
                      ><small>{{
                        model.description || "未填写模型描述"
                      }}</small>
                    </td>
                    <td>
                      {{ model.source_kind
                      }}<small>{{ model.source_model }}</small>
                    </td>
                    <td>
                      <span>{{
                        model.context_window_tokens
                          ? `${Number(model.context_window_tokens).toLocaleString()} 上下文`
                          : "上下文未知"
                      }}</span
                      ><small>{{
                        model.max_output_tokens
                          ? `${Number(model.max_output_tokens).toLocaleString()} 最大输出`
                          : "最大输出未知"
                      }}</small>
                    </td>
                    <td>
                      ${{ model.input_price }} / ${{ model.output_price }}
                    </td>
                    <td>
                      <div class="chips">
                        <span v-for="cap in model.capabilities" :key="cap">{{
                          cap
                        }}</span>
                      </div>
                    </td>
                    <td>
                      <span
                        class="status"
                        :class="
                          model.status === 'published'
                            ? 'ok'
                            : model.status === 'disabled' ||
                                model.status === 'error'
                              ? 'bad'
                              : 'warn'
                        "
                        >{{ statusLabel(model.status) }}</span
                      ><small>最近测试：{{ statusLabel(model.health_status) }}</small
                      ><small>参数：{{ statusLabel(model.metadata_sync_status) }}</small
                      >
                    </td>
                    <td class="row-actions">
                      <button
                        type="button"
                        class="ghost"
                        :disabled="busy"
                        @click="editModel(model)"
                      >
                        编辑</button
                      ><button
                        type="button"
                        class="ghost"
                        :disabled="busy"
                        @click="testModel(model)"
                      >
                        {{
                          operation === `model-test:${model.id}`
                            ? "测试中…"
                            : "测试"
                        }}
                      </button>
                    </td>
                  </tr>
                  <tr v-if="!admin.models.length">
                    <td colspan="7" class="empty">尚未发布模型</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <PaginationBar
              :page="
                pageNumber(
                  'admin-models',
                  filteredRows('admin-models', admin.models),
                )
              "
              :pages="pageCount(filteredRows('admin-models', admin.models))"
              :total="filteredRows('admin-models', admin.models).length"
              @previous="
                changePage(
                  'admin-models',
                  filteredRows('admin-models', admin.models),
                  -1,
                )
              "
              @next="
                changePage(
                  'admin-models',
                  filteredRows('admin-models', admin.models),
                  1,
                )
              "
            />
          </section>

          <section v-if="section === 'free'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">FREE-TIER DISCOVERY</span>
                <h1>免费资源</h1>
                <p>
                  发现不等于可用：必须配置、在线实测并明确发布后用户才能访问。
                </p>
              </div>
              <button
                class="primary"
                @click="
                  action(
                    () =>
                      api('admin/free/discover', {
                        method: 'POST',
                        body: '{}',
                      }),
                    '免费目录已刷新',
                  )
                "
              >
                重新发现
              </button>
            </div>
            <ListFilterBar
              v-model="listFilters['admin-free'].query"
              v-model:status="listFilters['admin-free'].status"
              v-model:category="listFilters['admin-free'].category"
              :status-options="statusOptions.slice(4, 8)"
              status-label="全部可用状态"
              :category-options="freeProviderOptions"
              category-label="全部供应商"
              :count="filteredRows('admin-free', admin.free_resources).length"
              placeholder="搜索模型或供应商"
            />
            <label class="visibility-filter">
              <input
                v-model="showHiddenFreeResources"
                class="choice-control"
                type="checkbox"
              />
              显示已在原生接入层隐藏的资源
            </label>
            <div class="resource-grid">
              <article
                class="resource"
                v-for="item in pageRows(
                  'admin-free',
                  filteredRows('admin-free', admin.free_resources),
                )"
                :key="item.resource_key"
              >
                <div class="resource-head">
                  <div>
                    <strong>{{ item.display_name }}</strong
                    ><code>{{ item.provider }} · {{ item.model_id }}</code>
                  </div>
                  <span
                    class="status"
                    :class="
                      item.test_status === 'healthy'
                        ? 'ok'
                        : item.test_status === 'failed'
                          ? 'bad'
                          : 'warn'
                    "
                    >{{
                      !item.native_visible
                        ? "原生已隐藏"
                        : item.configured
                          ? statusLabel(item.test_status)
                          : "未配置"
                    }}</span
                  >
                </div>
                <div class="resource-meta">
                  <span>类型 {{ item.free_type }}</span
                  ><span>月额 {{ item.monthly_tokens || "—" }}</span
                  ><span>已配置 {{ item.configured ? "是" : "否" }}</span
                  ><span>当前可用 {{ item.available ? "是" : "否" }}</span>
                  <span v-if="!item.native_visible">原生可见性 隐藏</span>
                </div>
                <p class="error-text" v-if="item.test_error">
                  {{ item.test_error }}
                </p>
                <p class="muted" v-if="!item.configured">
                  需要先配置并启用供应商 {{ item.provider }}，才能执行真实测试。
                </p>
                <div class="button-row">
                  <button
                    type="button"
                    class="ghost"
                    :disabled="busy || !item.native_visible"
                    :title="
                      item.configured
                        ? '使用供应商限定的模型 ID 测试'
                        : `请先配置供应商 ${item.provider}`
                    "
                    @click="testFreeResource(item)"
                  >
                    {{
                      operation === `free-test:${item.resource_key}`
                        ? "测试中…"
                        : "实时测试"
                    }}</button
                  ><button
                    type="button"
                    class="primary"
                    :disabled="busy || !item.native_visible"
                    @click="publishFree(item)"
                  >
                    开放给用户
                  </button>
                </div>
              </article>
            </div>
            <PaginationBar
              :page="
                pageNumber(
                  'admin-free',
                  filteredRows('admin-free', admin.free_resources),
                )
              "
              :pages="
                pageCount(filteredRows('admin-free', admin.free_resources))
              "
              :total="filteredRows('admin-free', admin.free_resources).length"
              @previous="
                changePage(
                  'admin-free',
                  filteredRows('admin-free', admin.free_resources),
                  -1,
                )
              "
              @next="
                changePage(
                  'admin-free',
                  filteredRows('admin-free', admin.free_resources),
                  1,
                )
              "
            />
          </section>

          <section v-if="section === 'users'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">IDENTITY & QUOTA</span>
                <h1>用户管理</h1>
                <p>禁用、分组、现金余额调整与调用限制集中完成。</p>
              </div>
              <div class="button-row">
                <span v-if="selectedUserIds.length" class="selection-count"
                  >已选 {{ selectedUserIds.length }} 人</span
                >
                <button
                  type="button"
                  class="primary"
                  :disabled="!filteredAdminUsers.length || busy"
                  @click="openBulkPolicy"
                >
                  批量修改调用策略
                </button>
              </div>
            </div>
            <ListFilterBar
              v-model="listFilters['admin-users'].query"
              v-model:status="listFilters['admin-users'].status"
              :status-options="
                statusOptions.slice(0, 1).concat(statusOptions.slice(2, 3))
              "
              status-label="全部账户状态"
              :count="filteredAdminUsers.length"
              placeholder="搜索邮箱或同步状态"
            />
            <div class="table-wrap panel">
              <table>
                <thead>
                  <tr>
                    <th class="selection-column">
                      <input
                        class="choice-control"
                        type="checkbox"
                        aria-label="选择当前页全部用户"
                        :checked="allCurrentUserPageSelected"
                        @change="toggleCurrentUserPage($event.target.checked)"
                      />
                    </th>
                    <th>用户</th>
                    <th>状态</th>
                    <th>现金余额</th>
                    <th>调用策略</th>
                    <th>权限同步</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="user in currentAdminUserPage"
                    :key="user.id"
                  >
                    <td class="selection-column">
                      <input
                        class="choice-control"
                        type="checkbox"
                        :aria-label="`选择用户 ${user.email}`"
                        :checked="selectedUserIds.includes(user.id)"
                        @change="toggleUserSelection(user.id, $event.target.checked)"
                      />
                    </td>
                    <td>
                      <strong>{{ user.email }}</strong
                      ><small>{{ date(user.created_at) }}</small>
                    </td>
                    <td>
                      <span
                        class="status"
                        :class="user.status === 'active' ? 'ok' : 'bad'"
                        >{{ statusLabel(user.status) }}</span
                      >
                    </td>
                    <td>${{ user.balance }}</td>
                    <td>
                      <small>会话 {{ Number(user.max_sessions) === 0 ? "不限" : user.max_sessions }}</small>
                      <small>RPM {{ Number(user.requests_per_minute) === 0 ? "不限" : user.requests_per_minute }}</small>
                      <small>每日 {{ Number(user.requests_per_day) === 0 ? "不限" : user.requests_per_day }}</small>
                    </td>
                    <td>
                      <span
                        class="status"
                        :class="
                          user.permission_status === 'synced' ? 'ok' : 'warn'
                        "
                        >{{
                          statusLabel(user.permission_status || "pending")
                        }}</span
                      ><small v-if="user.permission_error">{{
                        user.permission_error
                      }}</small>
                    </td>
                    <td>
                      <button class="ghost" @click="editUser(user)">
                        管理
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <PaginationBar
              :page="pageNumber('admin-users', filteredAdminUsers)"
              :pages="pageCount(filteredAdminUsers)"
              :total="filteredAdminUsers.length"
              @previous="
                changePage('admin-users', filteredAdminUsers, -1)
              "
              @next="
                changePage('admin-users', filteredAdminUsers, 1)
              "
            />
          </section>

          <section v-if="section === 'groups'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">ACCESS GROUPS</span>
                <h1>用户组</h1>
                <p>
                  LLMCtl 汇总用户与用户组的有效模型权限，并同步到对应 API Key。
                </p>
              </div>
              <button class="primary" @click="editGroup()">新建用户组</button>
            </div>
            <ListFilterBar
              v-model="listFilters['admin-groups'].query"
              v-model:status="listFilters['admin-groups'].status"
              :status-options="
                statusOptions.slice(0, 1).concat(statusOptions.slice(2, 3))
              "
              status-label="全部状态"
              :count="filteredRows('admin-groups', admin.groups).length"
              placeholder="搜索组名或描述"
            />
            <div class="group-grid">
              <article
                class="panel"
                v-for="group in pageRows(
                  'admin-groups',
                  filteredRows('admin-groups', admin.groups),
                )"
                :key="group.id"
              >
                <div class="panel-head">
                  <h2>{{ group.name }}</h2>
                  <span
                    class="status"
                    :class="group.status === 'active' ? 'ok' : 'bad'"
                    >{{ statusLabel(group.status) }}</span
                  >
                </div>
                <p>{{ group.description || "暂无描述" }}</p>
                <strong>{{ group.member_count }} 位成员</strong
                ><button class="ghost" @click="editGroup(group)">编辑</button>
              </article>
            </div>
            <PaginationBar
              :page="
                pageNumber(
                  'admin-groups',
                  filteredRows('admin-groups', admin.groups),
                )
              "
              :pages="pageCount(filteredRows('admin-groups', admin.groups))"
              :total="filteredRows('admin-groups', admin.groups).length"
              @previous="
                changePage(
                  'admin-groups',
                  filteredRows('admin-groups', admin.groups),
                  -1,
                )
              "
              @next="
                changePage(
                  'admin-groups',
                  filteredRows('admin-groups', admin.groups),
                  1,
                )
              "
            />
          </section>

          <section v-if="section === 'billing'" class="page">
            <div class="page-head">
              <div>
                <h1>计费账本</h1>
                <p>请求用量与金额流水分开呈现；同步操作具有幂等性。</p>
              </div>
              <button
                class="primary"
                :disabled="usageRefreshing"
                @click="
                  syncUsageAndRefresh({
                    announce: true,
                    preservePage: false,
                  })
                "
              >
                {{ usageRefreshing ? "同步中…" : "同步用量" }}
              </button>
            </div>
            <section class="panel">
              <div class="panel-head">
                <div>
                  <h2>请求用量</h2>
                  <p>新请求按模型价格从现金余额扣款；旧赠额字段仅为历史审计保留。</p>
                </div>
              </div>
              <div class="list-filter" role="search">
                <select
                  v-model="usageFilters.user"
                  aria-label="按用户筛选请求用量"
                  @change="applyUsageFilters"
                >
                  <option value="">全部用户</option>
                  <option
                    v-for="user in admin.users.filter(
                      (item) => item.role === 'user',
                    )"
                    :key="user.id"
                    :value="user.id"
                  >
                    {{ user.email }}
                  </option>
                </select>
                <select
                  v-model="usageFilters.model"
                  aria-label="按模型筛选请求用量"
                  @change="applyUsageFilters"
                >
                  <option value="">全部模型</option>
                  <option
                    v-for="model in admin.models"
                    :key="model.id"
                    :value="model.public_model_id"
                  >
                    {{ model.public_model_id }}
                  </option>
                </select>
                <span class="filter-count"
                  >{{ admin.usage_pagination?.total || 0 }} 条</span
                >
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>用户</th>
                      <th>模型</th>
                      <th>输入 / 输出</th>
                      <th>历史赠额 Token</th>
                      <th>标价</th>
                      <th>赠额抵扣</th>
                      <th>余额扣款</th>
                      <th>内容</th>
                    </tr>
                  </thead>
                  <tbody>
                    <template v-for="row in admin.usage" :key="row.id"
                      ><tr>
                        <td>{{ date(row.occurred_at) }}</td>
                        <td>{{ row.user_email }}</td>
                        <td>
                          <code>{{ row.public_model_id }}</code>
                        </td>
                        <td>
                          {{ row.input_tokens }} / {{ row.output_tokens }}
                        </td>
                        <td>{{ row.granted_tokens }}</td>
                        <td>{{ money(row.gross_amount_micros) }}</td>
                        <td>{{ money(row.grant_amount_micros) }}</td>
                        <td>{{ money(row.amount_micros) }}</td>
                        <td>
                          <button
                            class="ghost"
                            @click="toggleRequestDetail(row)"
                          >
                            {{
                              requestDetails[row.request_id]?.loading
                                ? "读取中…"
                                : requestDetails[row.request_id]?.expanded
                                  ? "收起"
                                  : "查看"
                            }}
                          </button>
                        </td>
                      </tr>
                      <tr
                        v-if="requestDetails[row.request_id]?.expanded"
                        class="request-detail-row"
                      >
                        <td colspan="9">
                          <div
                            v-if="requestDetails[row.request_id].error"
                            class="error-text"
                          >
                            {{ requestDetails[row.request_id].error }}
                          </div>
                          <div
                            v-else-if="requestDetails[row.request_id].loading"
                            class="muted"
                          >
                            正在读取请求内容…
                          </div>
                          <div v-else class="request-detail-sections">
                            <section>
                              <h3>请求输入</h3>
                              <div
                                v-if="requestDetails[row.request_id].available"
                                class="request-messages"
                              >
                                <article
                                  v-for="(message, index) in requestDetails[
                                    row.request_id
                                  ].messages"
                                  :key="`request-${index}`"
                                >
                                  <strong>{{ message.role }}</strong>
                                  <pre>{{ message.content }}</pre>
                                </article>
                              </div>
                              <p v-else class="muted">
                                该请求未保留可显示的输入内容。
                              </p>
                            </section>
                            <section class="admin-response-detail">
                              <h3>模型输出 <small>仅管理员可见</small></h3>
                              <div
                                v-if="
                                  requestDetails[row.request_id]
                                    .response_available
                                "
                                class="request-messages"
                              >
                                <article
                                  v-for="(message, index) in requestDetails[
                                    row.request_id
                                  ].response_messages"
                                  :key="`response-${index}`"
                                >
                                  <strong>{{ message.role }}</strong>
                                  <pre>{{ message.content }}</pre>
                                </article>
                              </div>
                              <p v-else class="muted">
                                当前接入层没有保留可显示的最终响应；请检查请求日志保留设置。
                              </p>
                            </section>
                          </div>
                        </td>
                      </tr></template
                    >
                    <tr v-if="!admin.usage.length">
                      <td colspan="9" class="empty">
                        尚无请求用量；点击“同步用量”从当前接入层读取记录。
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <PaginationBar
                :page="admin.usage_pagination?.page || 1"
                :pages="admin.usage_pagination?.pages || 1"
                :total="admin.usage_pagination?.total || 0"
                @previous="changeUsagePage(-1)"
                @next="changeUsagePage(1)"
              />
            </section>
            <section class="panel">
              <div class="panel-head">
                <div>
                  <h2>金额流水</h2>
                  <p>注册赠款、充值、余额调整和模型调用扣款均逐笔记录。</p>
                </div>
              </div>
              <ListFilterBar
                v-model="listFilters['admin-billing'].query"
                v-model:category="listFilters['admin-billing'].category"
                :category-options="kindOptions"
                category-label="全部流水类型"
                :count="
                  filteredRows('admin-billing', admin.transactions).length
                "
                placeholder="搜索用户、类型或备注"
              />
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>用户</th>
                      <th>类型</th>
                      <th>变动</th>
                      <th>余额</th>
                      <th>备注</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="row in pageRows(
                        'admin-billing',
                        filteredRows('admin-billing', admin.transactions),
                      )"
                      :key="row.id"
                    >
                      <td>{{ date(row.created_at) }}</td>
                      <td>
                        {{ row.user_email || row.user_id.slice(0, 8) }}
                      </td>
                      <td>{{ kindLabel(row.kind) }}</td>
                      <td>{{ money(row.amount_micros) }}</td>
                      <td>{{ money(row.balance_after_micros) }}</td>
                      <td>{{ row.note }}</td>
                    </tr>
                    <tr v-if="!admin.transactions.length">
                      <td colspan="6" class="empty">
                        暂无金额流水
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <PaginationBar
                :page="
                  pageNumber(
                    'admin-billing',
                    filteredRows('admin-billing', admin.transactions),
                  )
                "
                :pages="
                  pageCount(filteredRows('admin-billing', admin.transactions))
                "
                :total="
                  filteredRows('admin-billing', admin.transactions).length
                "
                @previous="
                  changePage(
                    'admin-billing',
                    filteredRows('admin-billing', admin.transactions),
                    -1,
                  )
                "
                @next="
                  changePage(
                    'admin-billing',
                    filteredRows('admin-billing', admin.transactions),
                    1,
                  )
                "
              />
            </section>
          </section>

          <section v-if="section === 'workflow'" class="page workflow-page">
            <div class="page-head">
              <div>
                <span class="eyebrow">PLUGGABLE ORCHESTRATION</span>
                <h1>能力编排</h1>
                <p>
                  配置公开模型路由、远程资源池与工具适配器。推理和流式响应由 Go
                  数据面处理，不经过门户 Python 进程。
                </p>
              </div>
              <div class="button-row">
                <button class="ghost" :disabled="workflowLoading" @click="loadWorkflow()">
                  {{ workflowLoading ? "读取中…" : "重新读取" }}
                </button>
                <button
                  class="primary"
                  :disabled="!workflow?.available || workflowSaving"
                  @click="saveWorkflow"
                >
                  {{ workflowSaving ? "校验并保存中…" : "校验、保存并热加载" }}
                </button>
                <button
                  class="primary"
                  :disabled="!workflow?.available || workflowPublishing || workflowSaving"
                  @click="publishWorkflow"
                >
                  {{ workflowPublishing ? "同步中…" : "同步到 AI 网关" }}
                </button>
              </div>
            </div>

            <section v-if="workflowLoading && !workflow" class="panel empty-state">
              正在读取本机 Go 工作流数据面…
            </section>
            <section v-else-if="!workflow?.available" class="panel workflow-unavailable">
              <h2>工作流数据面尚未启用</h2>
              <p>{{ workflow?.error || "没有可用的工作流管理接口。" }}</p>
              <p>
                先由维护人员执行 <code>llmctl workflow status</code>。若
                <code>configured=yes</code> 但服务为 <code>inactive</code>，不要使用
                <code>--force</code> 覆盖配置；启用所需路由后依次执行
                <code>llmctl workflow check</code> 与 <code>llmctl workflow enable</code>。
                仅在 <code>configured=no</code> 时执行 <code>llmctl workflow init</code>。
                这些操作不会重启现有 Router 或 GPU Worker。
              </p>
            </section>

            <template v-else>
              <section class="panel workflow-summary">
                <div>
                  <small>内部监听</small><strong>{{ workflow.config.listen }}</strong>
                </div>
                <div>
                  <small>模型路由</small><strong>{{ Object.keys(workflow.config.models).length }}</strong>
                </div>
                <div>
                  <small>资源池</small><strong>{{ Object.keys(workflow.config.pools).length }}</strong>
                </div>
                <div>
                  <small>适配器</small><strong>{{ Object.keys(workflow.config.adapters).length }}</strong>
                </div>
              </section>

              <section class="panel workflow-gateway-publish">
                <div>
                  <h2>AI 网关接入地址</h2>
                  <p class="muted">
                    这是当前 AI 接入层访问 Go 数据面的内部或跨主机地址，不是用户公网 API 地址。保存配置不会改变现有网关；只有点击“同步到 AI 网关”才会创建独立的
                    <code>llmctl-workflow-*</code> 路由组合。
                  </p>
                </div>
                <label>
                  OpenAI 兼容 Base URL
                  <input
                    v-model.trim="workflow.config.gateway_base_url"
                    placeholder="http://127.0.0.1:18100/v1"
                  />
                </label>
                <p class="muted">
                  同步后，仍需在“模型、映射与定价”中显式将所需公开 ID（例如
                  <code>gdn-inside</code>）指向生成的路由组合。LLMCtl 不会自动覆盖当前生产映射。
                </p>
                <details class="workflow-advanced-settings">
                  <summary>运行时高级设置</summary>
                  <div class="form-grid">
                    <label>
                      单次请求体上限（字节）
                      <input
                        v-model.number="workflow.config.request_body_limit_bytes"
                        type="number"
                        min="1048576"
                        max="268435456"
                      />
                    </label>
                    <label>
                      上游响应头超时（毫秒）
                      <input
                        v-model.number="workflow.config.upstream_timeout_ms"
                        type="number"
                        min="1000"
                        max="86400000"
                      />
                    </label>
                    <label>
                      共享密钥环境变量
                      <input v-model.trim="workflow.config.shared_secret_env" />
                    </label>
                  </div>
                  <p class="muted">
                    内部监听地址 <code>{{ workflow.config.listen }}</code> 的变更需要维护人员修改配置并重启
                    <code>llm-workflow.service</code>；密钥值只保存在受限环境文件中。
                  </p>
                </details>
              </section>

              <section class="panel workflow-section">
                <div class="panel-head">
                  <div>
                    <h2>公开模型路由</h2>
                    <p class="muted">路由 ID、底层模型、资源池及可调用工具均可独立调整。</p>
                  </div>
                </div>
                <article
                  v-for="[routeId, route] in Object.entries(workflow.config.models)"
                  :key="routeId"
                  class="workflow-editor-card"
                >
                  <div class="workflow-card-title">
                    <div><strong>{{ routeId }}</strong><small>公开模型 ID</small></div>
                    <label class="switch compact-switch">
                      <input v-model="route.enabled" class="switch-control" type="checkbox" />
                      <span aria-hidden="true"></span>{{ route.enabled ? "已启用" : "已停用" }}
                    </label>
                    <button class="danger subtle" type="button" @click="removeWorkflowRoute(routeId)">
                      删除
                    </button>
                  </div>
                  <div class="form-grid workflow-route-fields">
                    <label>底层模型 ID<input v-model.trim="route.base_model" /></label>
                    <label>资源池<select v-model="route.pool">
                      <option v-for="poolId in Object.keys(workflow.config.pools)" :key="poolId" :value="poolId">
                        {{ poolId }}
                      </option>
                    </select></label>
                    <label>执行模式<select v-model="route.mode">
                      <option value="transparent">透明转发</option>
                      <option value="agent">工具编排</option>
                    </select></label>
                    <label>最大工具轮数<input v-model.number="route.max_tool_rounds" type="number" min="1" max="12" /></label>
                  </div>
                  <label>系统提示词<textarea v-model="route.system_prompt" rows="3" placeholder="可留空"></textarea></label>
                  <div v-if="Object.keys(workflow.config.adapters).length" class="workflow-tool-list">
                    <span>允许调用的适配器</span>
                    <label v-for="adapterId in Object.keys(workflow.config.adapters)" :key="adapterId" class="check-chip">
                      <input
                        type="checkbox"
                        :checked="(route.tools || []).includes(adapterId)"
                        @change="toggleWorkflowTool(route, adapterId, $event.target.checked)"
                      />{{ adapterId }}
                    </label>
                  </div>
                </article>
                <div class="workflow-add-row">
                  <input v-model.trim="workflowNewRoute.id" placeholder="新公开模型 ID" />
                  <input v-model.trim="workflowNewRoute.base_model" placeholder="底层模型 ID" />
                  <select v-model="workflowNewRoute.pool">
                    <option value="">选择资源池</option>
                    <option v-for="poolId in Object.keys(workflow.config.pools)" :key="poolId" :value="poolId">{{ poolId }}</option>
                  </select>
                  <select v-model="workflowNewRoute.mode">
                    <option value="transparent">透明转发</option>
                    <option value="agent">工具编排</option>
                  </select>
                  <button class="ghost" type="button" @click="addWorkflowRoute">增加路由</button>
                </div>
              </section>

              <section class="panel workflow-section">
                <div class="panel-head">
                  <div>
                    <h2>资源池与 Worker</h2>
                    <p class="muted">
                      Worker 是显式 HTTP(S) 地址，可以位于本机、另一台服务器或独立 GPU 集群；不依赖 Docker 自动发现。
                    </p>
                  </div>
                </div>
                <article
                  v-for="[poolId, pool] in Object.entries(workflow.config.pools)"
                  :key="poolId"
                  class="workflow-editor-card"
                >
                  <div class="workflow-card-title">
                    <div><strong>{{ poolId }}</strong><small>资源池</small></div>
                    <label>调度策略<select v-model="pool.strategy">
                      <option value="p2c-least-inflight">P2C 最少在途</option>
                      <option value="round-robin">轮询</option>
                    </select></label>
                    <button class="danger subtle" type="button" @click="removeWorkflowPool(poolId)">删除</button>
                  </div>
                  <div v-for="(target, index) in pool.targets" :key="`${poolId}-${index}`" class="workflow-target-row">
                    <label>目标 ID<input v-model.trim="target.id" /></label>
                    <label class="target-url">OpenAI 兼容 Base URL<input v-model.trim="target.base_url" placeholder="http://10.0.0.20:8000/v1" /></label>
                    <label>密钥环境变量<input v-model.trim="target.api_key_env" placeholder="BACKEND_API_KEY" /></label>
                    <button class="danger subtle" type="button" @click="removeWorkflowTarget(pool, index)">移除</button>
                  </div>
                  <button class="ghost" type="button" @click="addWorkflowTarget(pool)">增加 Worker / 资源实例</button>
                </article>
                <div class="workflow-add-row compact-add-row">
                  <input v-model.trim="workflowNewPool.id" placeholder="新资源池 ID" />
                  <select v-model="workflowNewPool.strategy">
                    <option value="p2c-least-inflight">P2C 最少在途</option>
                    <option value="round-robin">轮询</option>
                  </select>
                  <button class="ghost" type="button" @click="addWorkflowPool">增加资源池</button>
                </div>
              </section>

              <section class="panel workflow-section">
                <div class="panel-head">
                  <div>
                    <h2>工具与多模态适配器</h2>
                    <p class="muted">
                      搜索、生图、图片编辑、音频和视频能力都以 HTTP JSON 适配器接入；密钥值只能由
                      <code>llmctl workflow secret set</code> 写入，不在页面显示。
                    </p>
                  </div>
                </div>
                <article
                  v-for="[adapterId, adapter] in Object.entries(workflow.config.adapters)"
                  :key="adapterId"
                  class="workflow-editor-card"
                >
                  <div class="workflow-card-title">
                    <div><strong>{{ adapterId }}</strong><small>{{ adapter.tool_definition?.function?.name || "工具" }}</small></div>
                    <button class="danger subtle" type="button" @click="removeWorkflowAdapter(adapterId)">删除</button>
                  </div>
                  <div class="form-grid">
                    <label>适配器地址<input v-model.trim="adapter.endpoint" /></label>
                    <label>密钥环境变量<input v-model.trim="adapter.secret_env" placeholder="可留空" /></label>
                    <label>超时（毫秒）<input v-model.number="adapter.timeout_ms" type="number" min="100" max="7200000" /></label>
                    <label>最大结果字节<input v-model.number="adapter.result_max_bytes" type="number" min="1024" max="67108864" /></label>
                    <label>工具函数名<input v-model.trim="adapter.tool_definition.function.name" /></label>
                  </div>
                  <label>工具说明<input v-model.trim="adapter.tool_definition.function.description" /></label>
                  <label>允许用途（逗号分隔）<input
                    :value="(adapter.allowed_purposes || []).join(',')"
                    @change="adapter.allowed_purposes = $event.target.value.split(',').map((v) => v.trim()).filter(Boolean)"
                    placeholder="text-to-image,image-edit"
                  /></label>
                  <label>
                    工具参数 JSON Schema
                    <textarea
                      :value="workflowToolParameters(adapter)"
                      rows="8"
                      spellcheck="false"
                      @change="updateWorkflowToolParameters(adapter, $event.target.value)"
                    ></textarea>
                  </label>
                </article>
                <div class="workflow-adapter-new">
                  <input v-model.trim="workflowNewAdapter.id" placeholder="适配器 ID" />
                  <input v-model.trim="workflowNewAdapter.endpoint" placeholder="HTTP(S) 接口地址" />
                  <input v-model.trim="workflowNewAdapter.secret_env" placeholder="密钥环境变量（可空）" />
                  <input v-model.trim="workflowNewAdapter.function_name" placeholder="工具函数名" />
                  <input v-model.trim="workflowNewAdapter.description" placeholder="给模型看的工具说明" />
                  <input v-model.trim="workflowNewAdapter.allowed_purposes" placeholder="允许用途（逗号分隔，可空）" />
                  <button class="ghost" type="button" @click="addWorkflowAdapter">增加适配器</button>
                </div>
              </section>

              <section class="panel workflow-save-footer">
                <div>
                  <strong>保存前由 Go 运行时执行完整校验</strong>
                  <p class="muted">版本冲突、空资源池、无效 URL、缺失密钥和错误工具定义都会被拒绝，原配置继续运行。</p>
                </div>
                <button class="primary" :disabled="workflowSaving" @click="saveWorkflow">
                  {{ workflowSaving ? "保存中…" : "保存并热加载" }}
                </button>
              </section>
            </template>
          </section>

          <section v-if="section === 'stress'" class="page stress-page">
            <div class="page-head">
              <div>
                <span class="eyebrow">PERFORMANCE LAB</span>
                <h1>性能压测</h1>
                <p>后台执行真实流式请求；页面只负责配置、观察与停止任务。</p>
              </div>
              <span
                v-if="activeStressRun"
                class="status warn"
              >{{ statusLabel(activeStressRun.status) }}</span>
            </div>

            <div class="stress-plan-layout">
              <section class="panel form-stack stress-plan">
                <div class="panel-head">
                  <div>
                    <h2>测试计划</h2>
                    <p>每个并发槽位顺序执行所选轮次；请求由 AI 接入层分配给后端 Worker。</p>
                  </div>
                </div>
                <label>公开模型 ID<select v-model="stressPlan.model" :disabled="!!activeStressRun">
                  <option
                    v-for="model in admin.models.filter((item) => item.status === 'published')"
                    :key="model.id"
                    :value="model.public_model_id"
                  >{{ model.public_model_id }}</option>
                </select></label>
                <div class="form-grid stress-fields">
                  <label>并发数<select v-model.number="stressPlan.concurrency" :disabled="!!activeStressRun">
                    <option v-for="value in [1,2,4,6,8,10,15,20,25,30,40,50,60,70,80,100]" :key="value" :value="value">{{ value }}</option>
                  </select></label>
                  <label>目标输入 Token<select v-model.number="stressPlan.input_tokens" :disabled="!!activeStressRun">
                    <option v-for="value in [50,100,300,800,1500,3000,6000,8000,15000,30000]" :key="value" :value="value">{{ compactTokens(value) }}</option>
                  </select></label>
                  <label>最大输出 Token<select v-model.number="stressPlan.output_tokens" :disabled="!!activeStressRun">
                    <option v-for="value in [64,128,256,512,1024]" :key="value" :value="value">{{ value }}</option>
                  </select></label>
                  <label>每槽位请求数<select v-model.number="stressPlan.request_multiplier" :disabled="!!activeStressRun">
                    <option v-for="value in [1,2,3,4]" :key="value" :value="value">{{ value }} 次</option>
                  </select></label>
                </div>
                <div class="stress-request-total">
                  <span>计划请求</span>
                  <strong>{{ stressPlan.concurrency * stressPlan.request_multiplier }}</strong>
                  <small>{{ stressPlan.concurrency }} 个并发槽位 × 每槽 {{ stressPlan.request_multiplier }} 次</small>
                </div>
                <label v-if="stressIsHighRisk" class="risk-confirmation">
                  <input
                    v-model="stressPlan.risk_confirmed"
                    class="choice-control"
                    type="checkbox"
                  />
                  <span><strong>我确认这是高负载测试。</strong>它可能占满 GPU、拉高延迟并影响正在使用 API 的用户。</span>
                </label>
                <div class="warning compact-warning">
                  输入档位是生成提示词的目标值；最终以模型 tokenizer 返回的实际输入 Token 为准。LLMCtl 同一时间只运行一个压测任务。
                </div>
                <div class="button-row">
                  <button
                    class="primary"
                    type="button"
                    :disabled="busy || !!activeStressRun || (stressIsHighRisk && !stressPlan.risk_confirmed)"
                    @click="startStressRun"
                  >启动后台压测</button>
                  <button
                    class="danger"
                    type="button"
                    :disabled="busy || !activeStressRun"
                    @click="cancelStressRun"
                  >停止当前任务</button>
                </div>
              </section>

              <section class="panel stress-live" v-if="selectedStressRun">
                <div class="panel-head">
                  <div>
                    <h2>实时结果</h2>
                    <p>
                      <code>{{ selectedStressRun.public_model_id }}</code>
                      · 并发 {{ selectedStressRun.concurrency }} × {{ selectedStressRun.request_multiplier }} 轮
                      = {{ selectedStressRun.request_count }} 请求
                      · 目标输入 {{ compactTokens(selectedStressRun.target_input_tokens) }}
                    </p>
                  </div>
                  <span
                    class="status"
                    :class="selectedStressRun.status === 'completed' ? 'ok' : selectedStressRun.status === 'failed' ? 'bad' : 'warn'"
                  >{{ statusLabel(selectedStressRun.status) }}</span>
                </div>
                <div v-if="stressPlanDiffersFromSelected" class="inline-alert info">
                  右侧是上一轮任务结果（{{ selectedStressRun.concurrency }} × {{ selectedStressRun.request_multiplier }} = {{ selectedStressRun.request_count }} 请求）；左侧 {{ stressPlan.concurrency }} × {{ stressPlan.request_multiplier }} = {{ stressPlan.concurrency * stressPlan.request_multiplier }} 请求的新计划尚未启动。
                </div>
                <div class="stress-progress" role="progressbar" :aria-valuenow="selectedStressRun.progress || 0" aria-valuemin="0" aria-valuemax="100">
                  <i :style="{ width: `${selectedStressRun.progress || 0}%` }"></i>
                </div>
                <p class="stress-progress-label">
                  {{ selectedStressRun.metrics?.completed || 0 }} / {{ selectedStressRun.request_count }} 请求
                  · {{ metricNumber(selectedStressRun.elapsed_seconds, 1) }} 秒
                  · {{ metricNumber(selectedStressRun.progress, 1) }}%
                </p>
                <div class="metrics stress-metrics">
                  <article><span>成功率</span><strong>{{ metricNumber(selectedStressRun.metrics?.success_rate, 2) }}%</strong><small>{{ selectedStressRun.metrics?.successful || 0 }} 成功 / {{ selectedStressRun.metrics?.failed || 0 }} 失败</small></article>
                  <article><span>请求吞吐</span><strong>{{ metricNumber(selectedStressRun.metrics?.request_rps, 2) }} RPS</strong><small>成功 {{ metricNumber(selectedStressRun.metrics?.successful_rps, 2) }} RPS</small></article>
                  <article><span>Token 吞吐</span><strong>{{ metricNumber(selectedStressRun.metrics?.output_tokens_per_second, 1) }} tok/s</strong><small>全任务总输出速率</small></article>
                  <article><span>单请求输出速度</span><strong>{{ metricNumber(metric(selectedStressRun, 'request_tokens_per_second', 'p50'), 1) }} tok/s</strong><small>P50 · P95 {{ metricNumber(metric(selectedStressRun, 'request_tokens_per_second', 'p95'), 1) }}</small></article>
                  <article><span>实际输入 Token</span><strong>{{ (selectedStressRun.metrics?.input_tokens || 0).toLocaleString() }}</strong><small>由网关 usage 汇总</small></article>
                  <article><span>实际输出 Token</span><strong>{{ (selectedStressRun.metrics?.output_tokens || 0).toLocaleString() }}</strong><small>由网关 usage 汇总</small></article>
                </div>
                <div class="latency-grid">
                  <div><strong>首 Token 延迟（TTFT）</strong><span>平均 {{ metricNumber(metric(selectedStressRun, 'ttft_ms', 'average'), 0) }} ms</span><span>P50 {{ metricNumber(metric(selectedStressRun, 'ttft_ms', 'p50'), 0) }} ms</span><span>P95 {{ metricNumber(metric(selectedStressRun, 'ttft_ms', 'p95'), 0) }} ms</span><span>P99 {{ metricNumber(metric(selectedStressRun, 'ttft_ms', 'p99'), 0) }} ms</span></div>
                  <div><strong>端到端延迟</strong><span>平均 {{ metricNumber(metric(selectedStressRun, 'latency_ms', 'average'), 0) }} ms</span><span>P50 {{ metricNumber(metric(selectedStressRun, 'latency_ms', 'p50'), 0) }} ms</span><span>P95 {{ metricNumber(metric(selectedStressRun, 'latency_ms', 'p95'), 0) }} ms</span><span>P99 {{ metricNumber(metric(selectedStressRun, 'latency_ms', 'p99'), 0) }} ms</span></div>
                </div>
                <div class="stress-evidence-grid">
                  <section class="stress-evidence-card">
                    <div class="stress-evidence-head">
                      <div><h3>路由分布</h3><p>当前 AI 接入层为已完成请求返回的实际命中目标。</p></div>
                      <strong>{{ selectedStressRun.metrics?.routing?.observed_target_count || 0 }} 个目标</strong>
                    </div>
                    <div v-if="stressRouteTargets.length" class="route-distribution">
                      <div v-for="([target, count]) in stressRouteTargets" :key="target">
                        <span><code>{{ target }}</code><b>{{ count }} 请求</b></span>
                        <i><em :style="{ width: `${Math.max(2, count * 100 / Math.max(1, selectedStressRun.metrics?.successful || 0))}%` }"></em></i>
                      </div>
                    </div>
                    <p v-else class="empty compact-empty">
                      暂未收到路由元数据；已完成 {{ selectedStressRun.metrics?.routing?.unknown_requests || 0 }} 个无法归属目标的请求。
                    </p>
                  </section>
                  <section class="stress-evidence-card gpu-evidence">
                    <div class="stress-evidence-head">
                      <div><h3>GPU 并行负载</h3><p>后台每秒采样利用率、显存和功耗。</p></div>
                      <strong v-if="selectedStressRun.gpu?.available">
                        峰值并行 {{ selectedStressRun.gpu.peak_concurrent_active_gpu_count || 0 }}/{{ selectedStressRun.gpu.gpus.length }}
                      </strong>
                    </div>
                    <div v-if="stressGpuImbalanced" class="inline-alert bad">
                      负载偏斜：采样期间最多只有 {{ selectedStressRun.gpu.peak_concurrent_active_gpu_count }} / {{ selectedStressRun.gpu.gpus.length }} 张 GPU 同时计算。
                    </div>
                    <div v-if="selectedStressRun.gpu?.available" class="table-wrap compact-table">
                      <table>
                        <thead><tr><th>GPU</th><th>平均 / 峰值利用率</th><th>活跃采样</th><th>峰值显存</th><th>平均 / 峰值功耗</th></tr></thead>
                        <tbody><tr v-for="gpu in selectedStressRun.gpu.gpus" :key="gpu.index">
                          <td><strong>GPU {{ gpu.index }}</strong></td>
                          <td>{{ metricNumber(gpu.utilization_average, 1) }}% / {{ metricNumber(gpu.utilization_peak, 1) }}%</td>
                          <td>{{ metricNumber(gpu.active_sample_percent, 1) }}%</td>
                          <td>{{ Math.round(gpu.memory_used_peak_mib || 0).toLocaleString() }} MiB</td>
                          <td>{{ metricNumber(gpu.power_average_watts, 0) }} / {{ metricNumber(gpu.power_peak_watts, 0) }} W</td>
                        </tr></tbody>
                      </table>
                    </div>
                    <p v-else class="empty compact-empty">
                      GPU 采样不可用：{{ selectedStressRun.gpu?.error || '尚未取得 nvidia-smi 数据' }}
                    </p>
                  </section>
                </div>
                <p class="error-text" v-if="selectedStressRun.error">{{ selectedStressRun.error }}</p>
                <details v-if="Object.keys(selectedStressRun.metrics?.errors || {}).length" class="stress-errors">
                  <summary>错误分类</summary>
                  <code v-for="(count, kind) in selectedStressRun.metrics.errors" :key="kind">{{ kind }}: {{ count }}</code>
                </details>
              </section>
            </div>

            <section class="panel" v-if="selectedStressRun?.recent_requests?.length">
              <div class="panel-head"><div><h2>最近完成的请求</h2><p>后台 JSONL 事件的最近 20 条，不包含提示词或模型输出正文。</p></div></div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>#</th><th>结果</th><th>命中 Worker</th><th>TTFT</th><th>总延迟</th><th>输入 / 输出</th><th>tok/s</th><th>错误</th></tr></thead>
                  <tbody><tr v-for="item in [...selectedStressRun.recent_requests].reverse()" :key="item.index">
                    <td>{{ item.index + 1 }}</td><td><span class="status" :class="item.ok ? 'ok' : 'bad'">{{ item.ok ? '成功' : '失败' }}</span></td><td><code>{{ item.route_target || item.route_provider || '—' }}</code></td><td>{{ metricNumber(item.ttft_ms, 0) }} ms</td><td>{{ metricNumber(item.latency_ms, 0) }} ms</td><td>{{ item.input_tokens || 0 }} / {{ item.output_tokens || 0 }}</td><td>{{ metricNumber(item.tokens_per_second, 1) }}</td><td class="detail">{{ item.error_kind || '—' }}</td>
                  </tr></tbody>
                </table>
              </div>
            </section>

            <section class="panel">
              <div class="panel-head"><div><h2>历史任务</h2><p>保存测试计划和聚合指标，便于对比不同并发与上下文档位。</p></div></div>
              <ListFilterBar
                v-model="listFilters['admin-stress'].query"
                v-model:status="listFilters['admin-stress'].status"
                :status-options="[
                  { value: 'running', label: '运行中' },
                  { value: 'completed', label: '已完成' },
                  { value: 'failed', label: '失败' },
                  { value: 'canceled', label: '已停止' },
                ]"
                status-label="全部状态"
                :count="filteredRows('admin-stress', stressRuns).length"
                placeholder="搜索模型、操作者或错误"
              />
              <div class="table-wrap">
                <table><thead><tr><th>开始时间</th><th>模型</th><th>计划</th><th>结果</th><th>吞吐</th><th></th></tr></thead>
                  <tbody><tr v-for="run in pageRows('admin-stress', filteredRows('admin-stress', stressRuns))" :key="run.id" :class="{ selected: selectedStressRunId === run.id }">
                    <td>{{ date(run.created_at) }}</td><td><code>{{ run.public_model_id }}</code></td><td>并发 {{ run.concurrency }} · 输入 {{ compactTokens(run.target_input_tokens) }} · {{ run.request_count }} 请求</td><td><span class="status" :class="run.status === 'completed' ? 'ok' : run.status === 'failed' ? 'bad' : 'warn'">{{ statusLabel(run.status) }}</span><small>{{ metricNumber(run.metrics?.success_rate, 1) }}% 成功</small></td><td>{{ metricNumber(run.metrics?.request_rps, 2) }} RPS<br><small>{{ metricNumber(run.metrics?.output_tokens_per_second, 1) }} tok/s</small></td><td><button class="ghost compact" type="button" @click="selectedStressRunId = run.id; pollStressRun()">查看</button></td>
                  </tr></tbody>
                </table>
              </div>
              <PaginationBar
                :page="pageNumber('admin-stress', filteredRows('admin-stress', stressRuns))"
                :pages="pageCount(filteredRows('admin-stress', stressRuns))"
                :total="filteredRows('admin-stress', stressRuns).length"
                @previous="changePage('admin-stress', filteredRows('admin-stress', stressRuns), -1)"
                @next="changePage('admin-stress', filteredRows('admin-stress', stressRuns), 1)"
              />
            </section>
          </section>

          <section v-if="section === 'settings'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">PUBLISHING &amp; ONBOARDING</span>
                <h1>发布、注册与 SMTP</h1>
                <p>
                  对外地址、注册策略和邮件服务分别保存，修改其中一项不会被另一项阻塞。
                </p>
              </div>
            </div>
            <div class="settings-grid">
              <section class="panel form-stack settings-publish">
                <div class="panel-head">
                  <div>
                    <h2>公开基准地址</h2>
                    <p class="muted setting-note">
                      仅用于生成门户链接、邮箱验证链接、API 地址和 curl
                      示例；不会绑定域名、配置端口或启用 TLS。留空则继续使用当前访问地址或安装时地址。
                    </p>
                  </div>
                  <span
                    class="status"
                    :class="
                      settings.published_origin?.startsWith('https://')
                        ? 'ok'
                        : settings.published_origin
                          ? 'warn'
                          : ''
                    "
                    >{{
                      settings.published_origin
                        ? settings.published_origin.startsWith("https://")
                          ? "HTTPS 地址"
                          : "HTTP 地址"
                        : "自动"
                    }}</span
                  >
                </div>
                <label
                  >门户品牌名称<input
                    v-model.trim="settings.portal_title"
                    type="text"
                    maxlength="40"
                    autocomplete="organization"
                    placeholder="LLMCtl"
                /></label>
                <p class="muted setting-note">
                  用于登录页、页眉和浏览器标题；默认值为 LLMCtl。
                </p>
                <label
                  >公开基准地址（可选，仅生成链接）<input
                    v-model.trim="settings.published_origin"
                    type="url"
                    inputmode="url"
                    autocomplete="url"
                    placeholder="https://llm.zjguardian.com"
                /></label>
                <div class="publish-preview">
                  <span
                    ><small>门户</small><code>{{
                        settings.published_origin
                          ? `${settings.published_origin.replace(/\/$/, '')}/ui/`
                          : settings.effective_public_url || `${location.origin}/ui`
                      }}</code></span
                  >
                  <span
                    ><small>API Base</small><code>{{
                        settings.published_origin
                          ? `${settings.published_origin.replace(/\/$/, '')}/v1`
                          : `${settings.effective_api_public_url || location.origin}/v1`
                      }}</code></span
                  >
                </div>
                <p class="muted setting-note">
                  此设置只是链接与显示元数据，不修改 Nginx、TLS、Cookie、登录跳转、
                  当前访问地址、端口映射或 Worker。
                </p>
                <details class="advanced-settings">
                  <summary>查看自动回退地址</summary>
                  <div class="form-grid compact">
                    <label
                      >门户回退 URL<input
                        v-model="settings.public_url"
                        placeholder="http://server:8000/ui"
                    /></label>
                    <label
                      >API 回退 URL<input
                        v-model="settings.api_public_url"
                        placeholder="http://server:8000"
                    /></label>
                  </div>
                </details>
                <button
                  type="button"
                  class="primary"
                  :disabled="busy"
                  @click="savePublishing"
                >
                  {{
                    operation === "publishing-save"
                      ? "保存中…"
                      : "保存基准地址"
                  }}
                </button>
              </section>
              <section class="panel form-stack">
                <h2>注册策略</h2>
                <label class="switch"
                  ><input
                    class="switch-control"
                    type="checkbox"
                    :checked="settings.registration_enabled === '1'"
                    @change="
                      settings.registration_enabled = $event.target.checked
                        ? '1'
                        : '0'
                    "
                  /><span aria-hidden="true"></span>允许新用户注册</label
                ><label
                  >允许邮箱后缀<input
                    v-model="settings.allowed_domains"
                    placeholder="example.com,corp.example.com" /></label
                ><label
                  >默认 API Key 活跃会话上限<input
                    v-model.number="settings.default_max_sessions"
                    type="number"
                    min="0"
                    max="10000" /></label
                ><p class="muted">
                  原生 maxSessions 是防止 Key 长期共享的会话指纹限制，不是 HTTP 并发数；0 表示不限制。
                </p><div class="form-grid"><label
                  >默认每分钟请求数（RPM）<input
                    v-model.number="settings.default_requests_per_minute"
                    type="number"
                    min="0"
                    max="10000000" /></label
                ><label
                  >默认每日请求数<input
                    v-model.number="settings.default_requests_per_day"
                    type="number"
                    min="0"
                    max="10000000" /></label
                ></div><p class="muted">
                  由 AI 接入层按 API Key 原生执行；0 表示不限制。RPM 控制突发调用，每日上限控制持续滥用。
                </p><label
                  >新用户一次性赠送金额（USD）<input
                    v-model="settings.default_welcome_balance"
                    inputmode="decimal"
                    step="0.000001"
                    min="0"
                    max="1000000000" /></label
                ><p class="muted">
                  注册验证完成后一次性入账。每次请求按实际 Token 和模型单价扣款；余额耗尽后停止模型权限。
                </p
                ><button
                  type="button"
                  class="primary"
                  :disabled="busy"
                  @click="saveRegistration"
                >
                  {{
                    operation === "registration-save"
                      ? "保存中…"
                      : "保存注册设置"
                  }}
                </button>
              </section>
              <section class="panel form-stack">
                <h2>SMTP</h2>
                <label>主机<input v-model="settings.smtp_host" /></label
                ><label
                  >端口<input
                    v-model="settings.smtp_port"
                    type="number" /></label
                ><label
                  >安全<select v-model="settings.smtp_security">
                    <option>starttls</option>
                    <option>ssl</option>
                    <option>plain</option>
                  </select></label
                ><label>用户名<input v-model="settings.smtp_username" /></label
                ><label
                  >密码<input
                    v-model="settings.smtp_password"
                    type="password"
                    :placeholder="
                      settings.smtp_password_configured === '1'
                        ? '已配置；留空保持不变'
                        : ''
                    " /></label
                ><label
                  >发件人<input v-model="settings.smtp_from" type="email"
                /></label
                ><label
                  >测试收件人<input
                    v-model="smtpTestRecipient"
                    type="email"
                    placeholder="测试邮件将发送到这里"
                /></label>
                <p class="muted setting-note">
                  测试邮件使用当前表单内容，不必先保存。
                </p>
                <div class="button-row">
                  <button
                    type="button"
                    class="ghost"
                    :disabled="busy"
                    @click="testSmtp"
                  >
                    {{
                      operation === "smtp-test" ? "发送中…" : "发送测试邮件"
                    }}</button
                  ><button
                    type="button"
                    class="primary"
                    :disabled="busy"
                    @click="saveSmtp"
                  >
                    {{ operation === "smtp-save" ? "保存中…" : "保存 SMTP" }}
                  </button>
                </div>
              </section>
            </div>
          </section>

          <section v-if="section === 'audit'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">AUDIT TRAIL</span>
                <h1>审计日志</h1>
                <p>LLMCtl 持久记录管理操作、失败结果与操作者。</p>
              </div>
            </div>
            <ListFilterBar
              v-model="listFilters['admin-audit'].query"
              v-model:status="listFilters['admin-audit'].status"
              :status-options="[
                { value: 'success', label: '成功' },
                { value: 'failed', label: '失败' },
              ]"
              status-label="全部结果"
              :count="filteredRows('admin-audit', admin.audit).length"
              placeholder="搜索操作者、动作、目标或详情"
            />
            <div class="table-wrap panel">
              <table>
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>操作者</th>
                    <th>动作</th>
                    <th>目标</th>
                    <th>结果</th>
                    <th>详情</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="row in pageRows(
                      'admin-audit',
                      filteredRows('admin-audit', admin.audit),
                    )"
                    :key="row.id"
                  >
                    <td>{{ date(row.created_at) }}</td>
                    <td>{{ row.actor }}</td>
                    <td>
                      <code>{{ row.action }}</code>
                    </td>
                    <td>{{ row.target }}</td>
                    <td>
                      <span
                        class="status"
                        :class="row.status === 'success' ? 'ok' : 'bad'"
                        >{{ statusLabel(row.status) }}</span
                      >
                    </td>
                    <td class="detail">{{ row.detail }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <PaginationBar
              :page="
                pageNumber(
                  'admin-audit',
                  filteredRows('admin-audit', admin.audit),
                )
              "
              :pages="pageCount(filteredRows('admin-audit', admin.audit))"
              :total="filteredRows('admin-audit', admin.audit).length"
              @previous="
                changePage(
                  'admin-audit',
                  filteredRows('admin-audit', admin.audit),
                  -1,
                )
              "
              @next="
                changePage(
                  'admin-audit',
                  filteredRows('admin-audit', admin.audit),
                  1,
                )
              "
            />
          </section>
        </template>
      </main>
    </div>

    <dialog id="user-editor">
      <form method="dialog" class="dialog-head">
        <h2>管理用户</h2>
        <button class="icon-button">×</button>
      </form>
      <div class="form-stack">
        <label
          >状态<select v-model="userEdit.status">
            <option value="active">正常</option>
            <option value="disabled">已禁用</option>
          </select></label
        >
        <label
          >API Key 活跃会话上限<input
            v-model.number="userEdit.max_sessions"
            type="number"
            min="0"
            max="10000"
        /></label>
        <p class="muted">
          用于识别长期共享 Key；0 表示不限制。它不是 HTTP 并发请求数，不能代替请求频率限制。
        </p>
        <div class="form-grid">
          <label
            >每分钟请求数（RPM）<input
              v-model.number="userEdit.requests_per_minute"
              type="number"
              min="0"
              max="10000000"
          /></label>
          <label
            >每日请求数<input
              v-model.number="userEdit.requests_per_day"
              type="number"
              min="0"
              max="10000000"
          /></label>
        </div>
        <p class="muted">
          这两项按 API Key 在 AI 接入层执行，超限直接返回 429；0 表示不限制。
        </p>
        <fieldset class="choice-group">
          <legend>所属用户组</legend>
          <label
            v-for="group in admin?.groups.filter(
              (item) => item.status === 'active',
            )"
            :key="group.id"
            class="choice-option"
          >
            <input
              v-model="userEdit.group_ids"
              class="choice-control"
              type="checkbox"
              :value="group.id"
            />
            <span
              ><strong>{{ group.name }}</strong
              ><small>{{ group.description || "未填写说明" }}</small></span
            >
          </label>
          <p
            v-if="!admin?.groups.some((item) => item.status === 'active')"
            class="muted"
          >
            尚无可用用户组
          </p>
        </fieldset>
        <label
          >金额调整（可为负数）<input v-model="userEdit.balance_delta" /></label
        ><label>调整说明<input v-model="userEdit.note" /></label
        ><p class="muted">
          正数为充值，负数为扣减。账户只使用现金余额结算；历史 Token 赠额已一次性折现并保留审计记录。
        </p
        ><button
          type="button"
          class="primary"
          :disabled="busy"
          @click="saveUser"
        >
          {{ operation === "user-save" ? "保存中…" : "保存并同步权限" }}
        </button>
      </div>
    </dialog>
    <dialog id="bulk-policy-editor" class="bulk-policy-dialog">
      <form method="dialog" class="dialog-head">
        <div>
          <h2>批量修改调用策略</h2>
          <p>只更新明确勾选的字段，其他用户资料保持不变。</p>
        </div>
        <button class="icon-button" aria-label="关闭">×</button>
      </form>
      <div class="form-stack">
        <fieldset class="bulk-scope">
          <legend>修改范围</legend>
          <label>
            <input
              v-model="bulkPolicy.scope"
              class="choice-control"
              type="radio"
              value="filtered"
            />
            <span
              ><strong>当前筛选结果</strong
              ><small>{{ filteredAdminUsers.length }} 位用户；包含所有分页</small></span
            >
          </label>
          <label :class="{ disabled: !selectedUserIds.length }">
            <input
              v-model="bulkPolicy.scope"
              class="choice-control"
              type="radio"
              value="selected"
              :disabled="!selectedUserIds.length"
            />
            <span
              ><strong>已勾选用户</strong
              ><small>{{ selectedUserIds.length }} 位用户</small></span
            >
          </label>
        </fieldset>

        <section class="bulk-policy-fields">
          <label class="bulk-field-toggle">
            <input
              v-model="bulkPolicy.change_max_sessions"
              class="choice-control"
              type="checkbox"
            />
            <span>修改 API Key 活跃会话上限</span>
          </label>
          <label
            >活动会话数
            <input
              v-model.number="bulkPolicy.max_sessions"
              type="number"
              min="0"
              max="10000"
              :disabled="!bulkPolicy.change_max_sessions"
            />
            <small>原生 maxSessions；0 表示不限制，不等于 HTTP 并发数。</small>
          </label>

          <label class="bulk-field-toggle">
            <input
              v-model="bulkPolicy.change_requests_per_minute"
              class="choice-control"
              type="checkbox"
            />
            <span>修改每分钟请求数</span>
          </label>
          <label
            >RPM
            <input
              v-model.number="bulkPolicy.requests_per_minute"
              type="number"
              min="0"
              max="10000000"
              :disabled="!bulkPolicy.change_requests_per_minute"
            />
            <small>按 API Key 限制突发请求；0 表示不限制。</small>
          </label>

          <label class="bulk-field-toggle">
            <input
              v-model="bulkPolicy.change_requests_per_day"
              class="choice-control"
              type="checkbox"
            />
            <span>修改每日请求数</span>
          </label>
          <label
            >每日请求上限
            <input
              v-model.number="bulkPolicy.requests_per_day"
              type="number"
              min="0"
              max="10000000"
              :disabled="!bulkPolicy.change_requests_per_day"
            />
            <small>控制持续滥用；0 表示不限制。</small>
          </label>
        </section>

        <div class="warning bulk-policy-warning">
          将修改 <strong>{{ bulkTargetUsers.length }}</strong> 位用户。保存时会短暂停用这些
          Key，写入本地策略后逐一同步到当前 AI 接入层；同步失败的 Key 会保持停用，避免旧策略继续生效。
          现金余额、账户状态、用户组和模型权限不会被修改。
        </div>
        <button
          type="button"
          class="primary"
          :disabled="busy || !bulkTargetUsers.length"
          @click="saveBulkPolicy"
        >
          {{ operation === "bulk-user-policy" ? "正在批量同步…" : `确认修改 ${bulkTargetUsers.length} 位用户` }}
        </button>
      </div>
    </dialog>
    <dialog id="group-editor">
      <form method="dialog" class="dialog-head">
        <h2>用户组</h2>
        <button class="icon-button">×</button>
      </form>
      <div class="form-stack">
        <label
          >名称<input v-model="groupEdit.name" maxlength="80" required
        /></label>
        <p class="muted">支持中文，名称为 1-80 个可见字符。</p>
        <label>描述<textarea v-model="groupEdit.description"></textarea></label
        ><label
          >状态<select v-model="groupEdit.status">
            <option value="active">正常</option>
            <option value="disabled">已停用</option>
          </select></label
        ><button
          type="button"
          class="primary"
          :disabled="busy"
          @click="saveGroup"
        >
          {{ operation === "group-save" ? "保存中…" : "保存" }}
        </button>
      </div>
    </dialog>
    <dialog id="model-editor" class="model-dialog">
      <form method="dialog" class="dialog-head">
        <div>
          <h2>{{ modelEdit.id ? "编辑模型" : "发布模型" }}</h2>
          <p>映射、原生参数、定价与访问范围</p>
        </div>
        <button class="icon-button">×</button>
      </form>
      <div class="form-grid">
        <label
          >公开模型 ID<input
            v-model="modelEdit.public_model_id"
            placeholder="gdn-inside" /></label
        ><label>显示名称<input v-model="modelEdit.display_name" /></label
        ><label
          >来源类型<select v-model="modelEdit.source_kind">
            <option value="combo">路由组合</option>
            <option value="model">单一模型</option>
            <option value="free">免费模型</option>
          </select></label
        ><label
          >来源模型 ID<input
            v-model="modelEdit.source_model"
            list="gateway-models"
            @change="inspectModel(true)" /><datalist id="gateway-models">
            <option
              v-for="model in admin?.gateway_models"
              :value="model.id"
            /></datalist></label
        ><label v-if="modelEdit.source_kind === 'combo'"
          >路由组合<select v-model="modelEdit.source_ref" @change="selectCombo">
            <option value="">请选择</option>
            <option v-for="combo in admin?.combos" :value="combo.id">
              {{ combo.name || combo.id }}
            </option>
          </select></label
        ><label v-else
          >来源引用<input
            v-model="modelEdit.source_ref"
            :readonly="modelEdit.source_kind === 'free'" /></label
        ><label
          >供应商<input
            v-model="modelEdit.source_provider"
            @change="inspectModel(true)" /></label
        ><label
          >状态<select v-model="modelEdit.status">
            <option value="draft">草稿</option>
            <option value="published">已发布</option>
            <option value="disabled">已停用</option>
          </select></label
        >
        <section class="span-2 native-metadata">
          <div class="panel-head">
            <div>
              <strong>模型原生参数</strong
              ><small
                >从当前 AI
                接入层读取；路由组合采用所有底层目标中的保守值。</small
              >
            </div>
            <button
              type="button"
              class="ghost"
              :disabled="modelEdit.inspecting"
              @click="inspectModel(true)"
            >
              {{ modelEdit.inspecting ? "读取中…" : "重新读取" }}
            </button>
          </div>
          <div class="form-grid compact">
            <label
              >最大上下文 Token<input
                v-model="modelEdit.context_window_tokens"
                type="number"
                min="1"
                max="10000000"
                @input="modelEdit.sync_context_window = true"
              /><span class="field-hint">修改后同步到每个底层模型</span></label
            ><label
              >最大输出 Token<input
                v-model="modelEdit.max_output_tokens"
                type="number"
                min="1"
                max="10000000"
                @input="modelEdit.sync_max_output_tokens = true"
              /><span class="field-hint"
                >写入接入层 max_token 覆盖值</span
              ></label
            >
          </div>
          <div v-if="modelEdit.metadata" class="metadata-summary">
            <span>已解析 {{ modelEdit.metadata.target_count }} 个底层目标</span
            ><span
              >上下文已知 {{ modelEdit.metadata.context_known_count }}/{{
                modelEdit.metadata.target_count
              }}</span
            ><span
              >最大输出已知 {{ modelEdit.metadata.output_known_count }}/{{
                modelEdit.metadata.target_count
              }}</span
            ><span
              :class="
                modelEdit.metadata.native_sync_supported
                  ? 'ok-text'
                  : 'warn-text'
              "
              >{{
                modelEdit.metadata.native_sync_supported
                  ? "支持原生同步"
                  : "部分目标无法原生同步"
              }}</span
            >
            <details>
              <summary>查看底层目标</summary>
              <ul>
                <li
                  v-for="target in modelEdit.metadata.targets"
                  :key="target.qualified_id"
                >
                  <code>{{ target.qualified_id }}</code
                  ><span
                    >{{
                      target.context_window_tokens?.toLocaleString() ||
                      "上下文未知"
                    }}
                    /
                    {{
                      target.max_output_tokens?.toLocaleString() || "输出未知"
                    }}</span
                  >
                </li>
              </ul>
            </details>
          </div>
          <p v-if="modelEdit.metadata_sync_error" class="error-text">
            上次同步：{{ modelEdit.metadata_sync_error }}
          </p>
        </section>
        <fieldset class="span-2 capability-editor">
          <legend>模型能力</legend>
          <label
            v-for="cap in [
              'chat',
              'vision',
              'ocr',
              'tools',
              'reasoning',
              'embedding',
            ]"
            :key="cap"
            class="capability-option"
            ><input
              v-model="modelEdit.capabilities"
              class="choice-control"
              type="checkbox"
              :value="cap"
            /><span>{{ cap }}</span></label
          >
          <p>
            能力与描述属于 LLMCtl 发布元数据；OCR
            等门户标签不会伪装成接入层原生参数。
          </p>
        </fieldset>
        <label
          >输入价 $/1M<input
            v-model="modelEdit.input_price"
            inputmode="decimal" /></label
        ><label
          >输出价 $/1M<input
            v-model="modelEdit.output_price"
            inputmode="decimal" /></label
        ><label
          >缓存价 $/1M<input
            v-model="modelEdit.cached_price"
            inputmode="decimal" /></label
        ><label
          >思考价 $/1M<input
            v-model="modelEdit.reasoning_price"
            inputmode="decimal" /></label
        ><label class="span-2"
          >模型描述<textarea
            v-model="modelEdit.description"
            rows="3"
            placeholder="面向用户说明模型用途、能力与注意事项"
          ></textarea>
        </label>
        <section class="span-2 access-editor">
          <div class="panel-head">
            <strong>开放范围</strong
            ><button
              type="button"
              class="ghost"
              :disabled="busy"
              @click="addModelAccess"
            >
              增加规则
            </button>
          </div>
          <div
            class="access-row"
            v-for="(rule, index) in modelEdit.access"
            :key="index"
          >
            <select v-model="rule.type">
              <option value="all">所有用户</option>
              <option value="group">指定用户组</option>
              <option value="user">指定用户</option></select
            ><select v-if="rule.type === 'group'" v-model="rule.id">
              <option value="">请选择用户组</option>
              <option
                v-for="group in admin?.groups.filter(
                  (g) => g.status === 'active',
                )"
                :value="group.id"
              >
                {{ group.name }}
              </option></select
            ><select v-else-if="rule.type === 'user'" v-model="rule.id">
              <option value="">请选择用户</option>
              <option
                v-for="user in admin?.users.filter((u) => u.role === 'user')"
                :value="user.id"
              >
                {{ user.email }}
              </option></select
            ><span v-else class="access-all">所有有效用户</span
            ><button
              type="button"
              class="danger"
              :disabled="busy"
              @click="removeModelAccess(index)"
            >
              移除
            </button>
          </div>
        </section>
      </div>
      <div class="dialog-footer">
        <p>保存前会执行真实请求。模型 API 调用仍直接进入高性能推理入口。</p>
        <button
          type="button"
          class="primary"
          :disabled="busy"
          @click="saveModel"
        >
          {{ operation === "model-save" ? "正在测试并保存…" : "测试并保存" }}
        </button>
      </div>
    </dialog>
  </div>
</template>

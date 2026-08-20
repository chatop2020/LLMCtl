<script setup>
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  provide,
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
import PortalAdminCorePages from "./components/PortalAdminCorePages.vue";
import PortalAdminOperationsPages from "./components/PortalAdminOperationsPages.vue";
import PortalDialogs from "./components/PortalDialogs.vue";
import PortalUserPages from "./components/PortalUserPages.vue";
import { PORTAL_WORKSPACE_KEY } from "./portalWorkspaceContext.js";
import { useModelDeployments } from "./useModelDeployments.js";

const session = ref(null);
const publicConfig = ref({
  registration_enabled: false,
  allowed_domains: [],
  portal_title: "LLMCtl",
});
const dashboard = ref(null);
const admin = ref(null);
const adminAnalytics = ref(null);
const adminUsageReport = ref(null);
const systemMonitor = ref(null);
const workflow = ref(null);
const workflowLoading = ref(false);
const workflowSaving = ref(false);
const workflowPublishing = ref(false);
const databaseRuntime = ref(null);
const databaseLoading = ref(false);
const databaseConnectionTest = ref(null);
const databaseMigrationToken = ref("");
const databaseMigrationConfirmation = ref("");
const databaseRollbackConfirmation = ref("");
const analyticsLoading = ref(false);
const usageReportLoading = ref(false);
const usageReportExporting = ref(false);
const monitorLoading = ref(false);
const monitorPaused = ref(false);
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
let monitorRefreshTimer = null;
let modelDeploymentRefreshTimer = null;
let databaseMigrationRefreshTimer = null;
let analyticsLoadVersion = 0;
let usageReportLoadVersion = 0;
let monitorLoadVersion = 0;
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
function localDateValue(value = new Date()) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}
const reportToday = new Date();
const usageReportFilters = reactive({
  period: "day",
  anchor_day: localDateValue(reportToday),
  anchor_month: localDateValue(reportToday).slice(0, 7),
  anchor_year: String(reportToday.getFullYear()),
  start_date: `${localDateValue(reportToday).slice(0, 7)}-01`,
  end_date: localDateValue(reportToday),
  model: "",
  user_query: "",
  status: "",
  page: 1,
  page_size: 20,
});
const monitorHistory = reactive({
  sampledAt: [],
  cpu: [],
  memory: [],
  gpus: {},
  receive: [],
  transmit: [],
});
const monitorProcess = reactive({
  query: "",
  sort: "cpu",
  page: 1,
  pageSize: 20,
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
const databaseConfig = reactive({
  host: "",
  port: 3306,
  database: "",
  username: "",
  password: "",
  tls_mode: "preferred",
  ca_file: "",
});
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
const usageReportMaxTokens = computed(() =>
  Math.max(
    1,
    ...(adminUsageReport.value?.timeseries || []).map((row) =>
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
const monitoredGpuAverage = computed(() => {
  const rows = systemMonitor.value?.gpu?.gpus || [];
  if (!rows.length) return null;
  return rows.reduce((sum, row) => sum + Number(row.utilization_percent || 0), 0) / rows.length;
});
const monitoredGpuMemory = computed(() => {
  const rows = systemMonitor.value?.gpu?.gpus || [];
  const used = rows.reduce((sum, row) => sum + Number(row.memory_used_bytes || 0), 0);
  const total = rows.reduce((sum, row) => sum + Number(row.memory_total_bytes || 0), 0);
  return { used, total, percent: total ? (used * 100) / total : null };
});
const monitorGpuTrends = computed(() =>
  (systemMonitor.value?.gpu?.gpus || []).map((gpu) => {
    const values = monitorHistory.gpus[String(gpu.index)] || [];
    const valid = values
      .filter((value) => value !== null && value !== undefined && Number.isFinite(Number(value)))
      .map(Number);
    return {
      ...gpu,
      values,
      current: valid.length ? valid.at(-1) : null,
      peak: valid.length ? Math.max(...valid) : null,
    };
  }),
);
const filteredMonitorProcesses = computed(() => {
  const query = monitorProcess.query.trim().toLocaleLowerCase();
  const rows = (systemMonitor.value?.processes || []).filter((row) =>
    !query || [row.pid, row.user, row.command, row.command_line]
      .some((value) => String(value ?? "").toLocaleLowerCase().includes(query)),
  );
  const key = monitorProcess.sort;
  return [...rows].sort((left, right) => {
    if (key === "memory") return Number(right.rss_bytes || 0) - Number(left.rss_bytes || 0);
    if (key === "pid") return Number(left.pid || 0) - Number(right.pid || 0);
    return Number(right.cpu_percent || 0) - Number(left.cpu_percent || 0);
  });
});
const monitorProcessPages = computed(() =>
  Math.max(1, Math.ceil(filteredMonitorProcesses.value.length / monitorProcess.pageSize)),
);
const currentMonitorProcesses = computed(() => {
  const page = Math.min(monitorProcess.page, monitorProcessPages.value);
  const offset = (page - 1) * monitorProcess.pageSize;
  return filteredMonitorProcesses.value.slice(offset, offset + monitorProcess.pageSize);
});

const isAdmin = computed(() => session.value?.user?.role === "admin");
const freeProviderOptions = computed(() =>
  [...new Set((admin.value?.free_resources || []).map((row) => row.provider))]
    .filter(Boolean)
    .sort((a, b) => a.localeCompare(b))
    .map((value) => ({ value, label: value })),
);
const {
  modelDeployments,
  modelDeploymentsLoading,
  modelDeploymentPlanning,
  modelDeploymentSubmitting,
  modelDeploymentPlan,
  modelDeploymentConfirmed,
  modelUpgradePlanning,
  modelUpgradeSubmitting,
  modelUpgradePlan,
  modelUpgradeConfirmed,
  modelUpgradeSubmitError,
  modelDownloadProxyBusy,
  modelDownloadProxyMessage,
  modelDownloadProxyForm,
  modelUpgradeForm,
  modelDeploymentMode,
  modelDeploymentForm,
  modelRemoteTargets,
  modelDeploymentRegistry,
  modelDeploymentRows,
  modelDeploymentJobs,
  modelUpgradeProfiles,
  modelUpgradeProfileGroups,
  modelUpgradeUnavailableReason,
  ornithUpgradeSources,
  activeModelDeploymentJob,
  displayedModelUpgradeJob,
  selectedDeploymentGpus,
  assignedDeploymentGpus,
  loadModelDeployments,
  prepareExistingDeployment,
  setDeploymentGpuSelection,
  toggleDeploymentGpu,
  addModelRemoteTarget,
  removeModelRemoteTarget,
  deploymentPublicIds,
  modelDeploymentPayload,
  planModelDeployment,
  submitModelDeployment,
  cancelModelDeployment,
  rollbackModelDeployment,
  retryModelDeploymentPublish,
  deploymentJobStateLabel,
  modelDownloadProxyPayload,
  testModelDownloadProxy,
  saveModelDownloadProxy,
  clearModelDownloadProxy,
  modelUpgradePayload,
  planModelUpgrade,
  submitModelUpgrade,
} = useModelDeployments({ api, isAdmin, session, notify });
const nav = computed(() =>
  isAdmin.value
    ? [
        ["overview", "总览"],
        ["models", "模型与定价"],
        ["deployments", "模型部署"],
        ["free", "免费资源"],
        ["users", "用户"],
        ["groups", "用户组"],
        ["billing", "账单"],
        ["reports", "用量报表"],
        ["database", "数据库"],
        ["monitoring", "系统监控"],
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
  adminUsageReport.value = null;
  systemMonitor.value = null;
  for (const key of ["sampledAt", "cpu", "memory", "receive", "transmit"])
    monitorHistory[key].splice(0);
  for (const key of Object.keys(monitorHistory.gpus)) delete monitorHistory.gpus[key];
  workflow.value = null;
  modelDeployments.value = null;
  modelDeploymentPlan.value = null;
  modelDeploymentConfirmed.value = false;
  databaseRuntime.value = null;
  databaseConnectionTest.value = null;
  databaseMigrationToken.value = "";
  databaseMigrationConfirmation.value = "";
  databaseRollbackConfirmation.value = "";
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

function usageReportParams(page = usageReportFilters.page) {
  let anchor = usageReportFilters.anchor_day;
  if (usageReportFilters.period === "month") anchor = usageReportFilters.anchor_month;
  if (usageReportFilters.period === "year") anchor = usageReportFilters.anchor_year;
  return new URLSearchParams({
    period: usageReportFilters.period,
    anchor,
    start_date: usageReportFilters.start_date,
    end_date: usageReportFilters.end_date,
    model: usageReportFilters.model,
    user_query: usageReportFilters.user_query,
    status: usageReportFilters.status,
    page: String(page || 1),
    page_size: String(usageReportFilters.page_size || 20),
  });
}

async function loadAdminUsageReport(page = usageReportFilters.page, options = {}) {
  if (!isAdmin.value || !session.value?.authenticated) return;
  const version = ++usageReportLoadVersion;
  usageReportLoading.value = true;
  try {
    const result = await api(`admin/usage-report?${usageReportParams(page)}`);
    if (version !== usageReportLoadVersion) return;
    adminUsageReport.value = result;
    usageReportFilters.page = result.pagination?.page || 1;
  } catch (error) {
    if (!options.silent) notify(`全员用量报表读取失败：${error.message}`, "bad");
  } finally {
    if (version === usageReportLoadVersion) usageReportLoading.value = false;
  }
}

async function changeUsageReportFilters() {
  usageReportFilters.page = 1;
  await loadAdminUsageReport(1);
}

async function exportAdminUsageReport() {
  if (usageReportExporting.value) return;
  usageReportExporting.value = true;
  notify("正在生成 Excel 报表…", "working");
  try {
    const response = await fetch(
      `/portal-api/admin/usage-report/export?${usageReportParams(1)}`,
      { credentials: "same-origin" },
    );
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || `HTTP ${response.status}`);
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    const fallbackName = `LLMCtl-全员用量-${localDateValue()}.xlsx`;
    const filename = encodedName ? decodeURIComponent(encodedName) : fallbackName;
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    notify("Excel 报表已导出");
  } catch (error) {
    notify(`Excel 导出失败：${error.message}`, "bad");
  } finally {
    usageReportExporting.value = false;
  }
}

function appendMonitorHistory(snapshot) {
  if (!snapshot?.sampled_at) return;
  const last = monitorHistory.sampledAt.at(-1);
  if (last === snapshot.sampled_at) return;
  const gpuRows = snapshot.gpu?.gpus || [];
  const priorSampleCount = monitorHistory.sampledAt.length;
  const points = {
    sampledAt: snapshot.sampled_at,
    cpu: snapshot.cpu?.usage_percent,
    memory: snapshot.memory?.used_percent,
    receive: snapshot.network?.rx_bytes_per_second,
    transmit: snapshot.network?.tx_bytes_per_second,
  };
  for (const [key, value] of Object.entries(points)) {
    monitorHistory[key].push(value);
    if (monitorHistory[key].length > 60) monitorHistory[key].shift();
  }
  const sampledGpuIds = new Set();
  for (const row of gpuRows) {
    const key = String(row.index);
    sampledGpuIds.add(key);
    if (!monitorHistory.gpus[key])
      monitorHistory.gpus[key] = Array(priorSampleCount).fill(null);
    monitorHistory.gpus[key].push(row.utilization_percent);
    if (monitorHistory.gpus[key].length > 60) monitorHistory.gpus[key].shift();
  }
  for (const [key, values] of Object.entries(monitorHistory.gpus)) {
    if (sampledGpuIds.has(key)) continue;
    values.push(null);
    if (values.length > 60) values.shift();
  }
}

async function loadSystemMonitor(options = {}) {
  if (!isAdmin.value || !session.value?.authenticated || monitorLoading.value) return;
  const version = ++monitorLoadVersion;
  monitorLoading.value = true;
  try {
    const result = await api("admin/system-monitor");
    if (version !== monitorLoadVersion) return;
    systemMonitor.value = result;
    appendMonitorHistory(result);
    if (monitorProcess.page > monitorProcessPages.value)
      monitorProcess.page = monitorProcessPages.value;
  } catch (error) {
    if (!options.silent) notify(`系统监控读取失败：${error.message}`, "bad");
  } finally {
    if (version === monitorLoadVersion) monitorLoading.value = false;
  }
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
    if (nextSection === "monitoring" && isAdmin.value) {
      await Promise.all([refreshWorkspace(), loadSystemMonitor()]);
    } else if (nextSection === "workflow" && isAdmin.value) {
      await Promise.all([refreshWorkspace(), loadWorkflow()]);
    } else if (nextSection === "deployments" && isAdmin.value) {
      await Promise.all([refreshWorkspace(), loadModelDeployments()]);
    } else if (nextSection === "database" && isAdmin.value) {
      await loadDatabaseRuntime();
    } else if (nextSection === "reports" && isAdmin.value) {
      await Promise.all([refreshWorkspace(), loadAdminUsageReport(1)]);
    } else if (nextSection === "billing") {
      await syncUsageAndRefresh({ preservePage: false });
    } else {
      await refreshWorkspace();
    }
  } catch (error) {
    notify(`页面数据更新失败：${error.message}`, "bad");
  }
}

function applyDatabaseRuntime(snapshot) {
  databaseRuntime.value = snapshot;
  const config = snapshot?.config || {};
  databaseConfig.host = config.host || "";
  databaseConfig.port = Number(config.port || 3306);
  databaseConfig.database = config.database || "";
  databaseConfig.username = config.username || "";
  databaseConfig.password = "";
  databaseConfig.tls_mode = config.tls_mode || "preferred";
  databaseConfig.ca_file = config.ca_file || "";
}

function databasePayload() {
  return {
    host: databaseConfig.host,
    port: Number(databaseConfig.port || 3306),
    database: databaseConfig.database,
    username: databaseConfig.username,
    password: databaseConfig.password,
    tls_mode: databaseConfig.tls_mode,
    ca_file: databaseConfig.ca_file,
  };
}

async function loadDatabaseRuntime(options = {}) {
  if (!isAdmin.value || !session.value?.authenticated || databaseLoading.value)
    return;
  databaseLoading.value = true;
  try {
    applyDatabaseRuntime(await api("admin/database"));
  } catch (error) {
    if (!options.silent) notify(`数据库状态读取失败：${error.message}`, "bad");
  } finally {
    databaseLoading.value = false;
  }
}

async function saveDatabaseConfig() {
  const result = await action(
    () =>
      api("admin/database/config", {
        method: "POST",
        body: JSON.stringify(databasePayload()),
      }),
    "MySQL 连接配置已保存",
    { key: "database-save", pending: "正在保存 MySQL 连接配置…", refresh: false },
  );
  if (result) await loadDatabaseRuntime();
}

async function testDatabaseConnection() {
  const result = await action(
    () =>
      api("admin/database/test", {
        method: "POST",
        body: JSON.stringify(databasePayload()),
      }),
    "MySQL 连接与版本检查通过",
    { key: "database-test", pending: "正在连接并检查 MySQL…", refresh: false },
  );
  if (result) {
    databaseConnectionTest.value = result;
    databaseConfig.password = "";
    await loadDatabaseRuntime();
  }
}

async function pollDatabaseMigration() {
  if (!databaseMigrationToken.value) return;
  try {
    const result = await api("database-migration-progress", {
      headers: {
        "X-LLMCtl-Migration-Token": databaseMigrationToken.value,
      },
    });
    databaseRuntime.value = {
      ...(databaseRuntime.value || {}),
      migration: result.migration,
      busy: result.busy,
    };
    if (result.migration?.status === "running" || result.busy) return;
    databaseMigrationToken.value = "";
    busy.value = false;
    operation.value = "";
    if (result.migration?.status === "completed") {
      notify("数据校验通过，门户已切换到 MySQL");
      await Promise.all([loadDatabaseRuntime(), refreshWorkspace()]);
    } else {
      notify(`迁移失败，门户仍使用 SQLite：${result.migration?.error || "未知错误"}`, "bad");
      await loadDatabaseRuntime();
    }
  } catch (error) {
    databaseMigrationToken.value = "";
    busy.value = false;
    operation.value = "";
    notify(`迁移进度读取失败：${error.message}。请重新打开数据库页面确认实际状态。`, "bad");
  }
}

async function startDatabaseMigration() {
  if (busy.value) return;
  if (databaseMigrationConfirmation.value !== "MIGRATE_TO_MYSQL") {
    notify("请输入 MIGRATE_TO_MYSQL 确认迁移", "bad");
    return;
  }
  busy.value = true;
  operation.value = "database-migrate";
  notify("正在备份 SQLite 并启动迁移…", "working");
  try {
    const result = await api("admin/database/migrate", {
      method: "POST",
      body: JSON.stringify({ confirmation: databaseMigrationConfirmation.value }),
    });
    applyDatabaseRuntime(result);
    databaseMigrationToken.value = result.progress_token || "";
    databaseMigrationConfirmation.value = "";
    if (!databaseMigrationToken.value)
      throw new Error("服务未返回迁移进度令牌");
    await pollDatabaseMigration();
  } catch (error) {
    busy.value = false;
    operation.value = "";
    notify(`迁移未启动：${error.message}`, "bad");
    await loadDatabaseRuntime({ silent: true });
  }
}

async function rollbackDatabaseToSqlite() {
  const result = await action(
    () =>
      api("admin/database/rollback", {
        method: "POST",
        body: JSON.stringify({ confirmation: databaseRollbackConfirmation.value }),
      }),
    "门户已回滚到迁移时保留的 SQLite",
    { key: "database-rollback", pending: "正在检查 SQLite 并切换…", refresh: false },
  );
  if (result) {
    databaseRollbackConfirmation.value = "";
    applyDatabaseRuntime(result);
    await refreshWorkspace();
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

function formatBytes(value, digits = 1) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number === 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  const index = Math.min(
    units.length - 1,
    Math.max(0, Math.floor(Math.log(Math.abs(number)) / Math.log(1024))),
  );
  const scaled = number / 1024 ** index;
  return `${scaled.toFixed(index === 0 ? 0 : digits)} ${units[index]}`;
}

function formatRate(value) {
  return value === null || value === undefined ? "采样中" : `${formatBytes(value)}/s`;
}

function formatMonitorPercent(value, digits = 1) {
  return value === null || value === undefined || !Number.isFinite(Number(value))
    ? "采样中"
    : `${Number(value).toFixed(digits)}%`;
}

function formatUptime(value) {
  let seconds = Math.max(0, Math.floor(Number(value || 0)));
  const days = Math.floor(seconds / 86400);
  seconds %= 86400;
  const hours = Math.floor(seconds / 3600);
  seconds %= 3600;
  const minutes = Math.floor(seconds / 60);
  return [days ? `${days} 天` : "", hours ? `${hours} 小时` : "", `${minutes} 分钟`]
    .filter(Boolean)
    .join(" ");
}

function monitorTrendPoints(values, maximum = 100) {
  if (!values.length) return "";
  const width = 300;
  const height = 72;
  const verticalPadding = 2;
  const drawableHeight = height - verticalPadding * 2;
  const valid = values.filter(
    (value) => value !== null && value !== undefined && Number.isFinite(Number(value)),
  );
  const ceiling = Math.max(maximum || 0, ...valid.map(Number), 1);
  return values
    .map((value, index) => {
      const x = values.length === 1 ? width : (index / (values.length - 1)) * width;
      const number = Number.isFinite(Number(value)) ? Number(value) : 0;
      const y = height - verticalPadding -
        Math.max(0, Math.min(drawableHeight, (number / ceiling) * drawableHeight));
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

function latestMonitorTrendValue(values) {
  for (let index = values.length - 1; index >= 0; index -= 1) {
    if (
      values[index] !== null &&
      values[index] !== undefined &&
      Number.isFinite(Number(values[index]))
    )
      return formatMonitorPercent(values[index]);
  }
  return "采样中";
}

function setMonitorProcessPage(page) {
  monitorProcess.page = Math.max(1, Math.min(monitorProcessPages.value, page));
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
  () => [monitorProcess.query, monitorProcess.sort],
  () => {
    monitorProcess.page = 1;
  },
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
    const managedRuntime = Number(metadata.managed_runtime_count || 0) > 0;
    if (managedRuntime || overwrite || !modelEdit.context_window_tokens)
      modelEdit.context_window_tokens = metadata.context_window_tokens || "";
    if (overwrite || !modelEdit.max_output_tokens)
      modelEdit.max_output_tokens = metadata.max_output_tokens || "";
    if (
      (!modelEdit.capabilities?.length ||
        modelEdit.capabilities.length === 1) &&
      metadata.capabilities?.length
    )
      modelEdit.capabilities = [...metadata.capabilities];
    if (Number(metadata.managed_runtime_corrected_count || 0) > 0)
      modelEdit.sync_context_window = true;
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

// 页面组件只消费这个组合根提供的响应式状态和操作；业务规则继续位于
// 对应 composable 或控制函数中，避免组件之间复制状态机。
provide(PORTAL_WORKSPACE_KEY, {
  chunkParts,
  consumeChatResponse,
  splitThinkingMarkup,
  ACCEPTED_ATTACHMENTS,
  MAX_TOTAL_ATTACHMENT_BYTES,
  MAX_VISUAL_INPUTS,
  attachmentKind,
  buildUserContent,
  prepareAttachment,
  visualInputCount,
  writeClipboardText,
  session,
  publicConfig,
  dashboard,
  admin,
  adminAnalytics,
  adminUsageReport,
  systemMonitor,
  workflow,
  workflowLoading,
  workflowSaving,
  workflowPublishing,
  databaseRuntime,
  databaseLoading,
  databaseConnectionTest,
  databaseMigrationToken,
  databaseMigrationConfirmation,
  databaseRollbackConfirmation,
  analyticsLoading,
  usageReportLoading,
  usageReportExporting,
  monitorLoading,
  monitorPaused,
  busy,
  operation,
  workspaceRefreshing,
  usageRefreshing,
  toast,
  authMode,
  section,
  auth,
  chat,
  attachmentInput,
  keyOnce,
  showApiKey,
  keyLoading,
  apiKeyField,
  userEdit,
  selectedUserIds,
  analyticsFilters,
  usageReportFilters,
  monitorHistory,
  monitorProcess,
  bulkPolicy,
  groupEdit,
  workflowNewPool,
  workflowNewRoute,
  workflowNewAdapter,
  modelEdit,
  settings,
  databaseConfig,
  portalTitle,
  portalInitial,
  allowedRegistrationEmails,
  smtpTestRecipient,
  showHiddenFreeResources,
  stressPlan,
  selectedStressRunId,
  stressRuns,
  activeStressRun,
  selectedStressRun,
  stressRouteTargets,
  stressGpuImbalanced,
  stressPlanDiffersFromSelected,
  stressIsHighRisk,
  selectedChatModel,
  selectedChatCapabilities,
  selectedChatSupportsVision,
  pages,
  requestDetails,
  listFilters,
  usageFilters,
  statusOptions,
  kindOptions,
  resetOptions,
  filterFields,
  filteredAdminUsers,
  currentAdminUserPage,
  bulkTargetUsers,
  allCurrentUserPageSelected,
  analyticsMaxTokens,
  usageReportMaxTokens,
  selectedUserAnalyticsMaxTokens,
  monitoredGpuAverage,
  monitoredGpuMemory,
  monitorGpuTrends,
  filteredMonitorProcesses,
  monitorProcessPages,
  currentMonitorProcesses,
  isAdmin,
  freeProviderOptions,
  nav,
  localDateValue,
  cookie,
  api,
  notify,
  dismissToast,
  load,
  clearAuthenticatedClientState,
  loadAdminAnalytics,
  changeAnalyticsFilters,
  usageReportParams,
  loadAdminUsageReport,
  changeUsageReportFilters,
  exportAdminUsageReport,
  appendMonitorHistory,
  loadSystemMonitor,
  applyAdminSnapshot,
  refreshWorkspace,
  loadExistingKey,
  syncUsageAndRefresh,
  selectSection,
  applyDatabaseRuntime,
  databasePayload,
  loadDatabaseRuntime,
  saveDatabaseConfig,
  testDatabaseConnection,
  pollDatabaseMigration,
  startDatabaseMigration,
  rollbackDatabaseToSqlite,
  loadWorkflow,
  saveWorkflow,
  publishWorkflow,
  workflowIdentifier,
  addWorkflowPool,
  removeWorkflowPool,
  addWorkflowTarget,
  removeWorkflowTarget,
  addWorkflowRoute,
  removeWorkflowRoute,
  toggleWorkflowTool,
  workflowToolParameters,
  updateWorkflowToolParameters,
  addWorkflowAdapter,
  removeWorkflowAdapter,
  replaceStressRun,
  pollStressRun,
  startStressRun,
  cancelStressRun,
  metric,
  metricNumber,
  compactTokens,
  formatTokens,
  formatBytes,
  formatRate,
  formatMonitorPercent,
  formatUptime,
  monitorTrendPoints,
  latestMonitorTrendValue,
  setMonitorProcessPage,
  analyticsSegmentHeight,
  analyticsLabelVisible,
  analyzeUser,
  goToSection,
  action,
  login,
  register,
  verify,
  logout,
  rotateKey,
  copy,
  money,
  cashTokenCapacity,
  statusLabel,
  kindLabel,
  resetLabel,
  rowStatus,
  rowCategory,
  filteredRows,
  loadUsagePage,
  applyUsageFilters,
  changeUsagePage,
  date,
  pageCount,
  pageNumber,
  pageRows,
  changePage,
  toggleRequestDetail,
  curlFor,
  resetChatResult,
  stopChat,
  attachmentSize,
  removeAttachment,
  addAttachmentFiles,
  responseError,
  sendChat,
  editUser,
  editGroup,
  editModel,
  publishFree,
  testFreeResource,
  addModelAccess,
  removeModelAccess,
  selectCombo,
  inspectModel,
  saveUser,
  toggleUserSelection,
  toggleCurrentUserPage,
  openBulkPolicy,
  saveBulkPolicy,
  saveGroup,
  testModel,
  saveModel,
  registrationPayload,
  publishingPayload,
  smtpPayload,
  saveRegistration,
  savePublishing,
  saveSmtp,
  testSmtp,
  modelDeployments,
  modelDeploymentsLoading,
  modelDeploymentPlanning,
  modelDeploymentSubmitting,
  modelDeploymentPlan,
  modelDeploymentConfirmed,
  modelUpgradePlanning,
  modelUpgradeSubmitting,
  modelUpgradePlan,
  modelUpgradeConfirmed,
  modelUpgradeSubmitError,
  modelDownloadProxyBusy,
  modelDownloadProxyMessage,
  modelDownloadProxyForm,
  modelUpgradeForm,
  modelDeploymentMode,
  modelDeploymentForm,
  modelRemoteTargets,
  modelDeploymentRegistry,
  modelDeploymentRows,
  modelDeploymentJobs,
  modelUpgradeProfiles,
  modelUpgradeProfileGroups,
  modelUpgradeUnavailableReason,
  ornithUpgradeSources,
  activeModelDeploymentJob,
  displayedModelUpgradeJob,
  selectedDeploymentGpus,
  assignedDeploymentGpus,
  loadModelDeployments,
  prepareExistingDeployment,
  setDeploymentGpuSelection,
  toggleDeploymentGpu,
  addModelRemoteTarget,
  removeModelRemoteTarget,
  deploymentPublicIds,
  modelDeploymentPayload,
  planModelDeployment,
  submitModelDeployment,
  cancelModelDeployment,
  rollbackModelDeployment,
  retryModelDeploymentPublish,
  deploymentJobStateLabel,
  modelDownloadProxyPayload,
  testModelDownloadProxy,
  saveModelDownloadProxy,
  clearModelDownloadProxy,
  modelUpgradePayload,
  planModelUpgrade,
  submitModelUpgrade,
});

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
    monitorRefreshTimer = window.setInterval(() => {
      if (
        !isAdmin.value ||
        section.value !== "monitoring" ||
        document.hidden ||
        monitorPaused.value ||
        monitorLoading.value
      )
        return;
      loadSystemMonitor({ silent: true });
    }, 2_000);
    modelDeploymentRefreshTimer = window.setInterval(() => {
      if (
        !isAdmin.value ||
        document.hidden ||
        modelDeploymentsLoading.value ||
        (section.value !== "deployments" && !activeModelDeploymentJob.value)
      )
        return;
      loadModelDeployments({ silent: true });
    }, 2_000);
    databaseMigrationRefreshTimer = window.setInterval(() => {
      if (!databaseMigrationToken.value || document.hidden) return;
      pollDatabaseMigration();
    }, 1_000);
  } catch (error) {
    notify(error.message, "bad");
  }
});

onBeforeUnmount(() => {
  if (usageRefreshTimer) window.clearInterval(usageRefreshTimer);
  if (stressRefreshTimer) window.clearInterval(stressRefreshTimer);
  if (analyticsRefreshTimer) window.clearInterval(analyticsRefreshTimer);
  if (monitorRefreshTimer) window.clearInterval(monitorRefreshTimer);
  if (modelDeploymentRefreshTimer)
    window.clearInterval(modelDeploymentRefreshTimer);
  if (databaseMigrationRefreshTimer)
    window.clearInterval(databaseMigrationRefreshTimer);
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
        <PortalUserPages v-if="!isAdmin && dashboard" />
        <PortalAdminCorePages v-if="isAdmin && admin" />
        <PortalAdminOperationsPages v-if="isAdmin && admin" />
      </main>
    </div>

    <PortalDialogs />
  </div>
</template>

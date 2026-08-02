<script setup>
import { computed, h, onMounted, reactive, ref } from "vue";
import {
  chunkParts,
  consumeChatResponse,
  splitThinkingMarkup,
} from "./chatStream.js";

const session = ref(null);
const publicConfig = ref({ registration_enabled: false, allowed_domains: [] });
const dashboard = ref(null);
const admin = ref(null);
const busy = ref(false);
const operation = ref("");
const toast = reactive({ text: "", kind: "ok" });
let toastTimer = null;
const authMode = ref(
  location.hash.startsWith("#/register")
    ? "register"
    : location.hash.startsWith("#/verify")
      ? "verify"
      : "login",
);
const section = ref("overview");
const auth = reactive({
  email: "",
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
});
let chatController = null;
let chatTimer = null;
const keyOnce = ref(sessionStorage.getItem("llmctl_api_key") || "");
const showApiKey = ref(false);
const userEdit = reactive({
  user_id: "",
  status: "active",
  balance_delta: "0",
  group_ids: [],
  grant_tokens: 0,
  grant_reset: "none",
  grant_reset_time: "00:00",
  grant_model_id: "",
  grant_label: "",
});
const groupEdit = reactive({
  id: "",
  name: "",
  description: "",
  status: "active",
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
const PAGE_SIZE = 20;
const pages = reactive({});
const requestDetails = reactive({});
const PaginationBar = (props, { emit }) =>
  props.total <= PAGE_SIZE
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

const isAdmin = computed(() => session.value?.user?.role === "admin");
const nav = computed(() =>
  isAdmin.value
    ? [
        ["overview", "总览"],
        ["models", "模型与定价"],
        ["free", "免费资源"],
        ["users", "用户"],
        ["groups", "用户组"],
        ["billing", "账单"],
        ["settings", "注册与 SMTP"],
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
  if (!session.value.authenticated) return;
  if (isAdmin.value) {
    admin.value = await api("admin");
    Object.assign(settings, admin.value.settings);
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
  } else {
    dashboard.value = await api("dashboard");
    if (!chat.model)
      chat.model = dashboard.value.models[0]?.public_model_id || "";
  }
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
    await api("auth/login", { method: "POST", body: JSON.stringify(auth) });
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
    () => api("auth/register", { method: "POST", body: JSON.stringify(auth) }),
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
  sessionStorage.removeItem("llmctl_api_key");
  keyOnce.value = "";
  dashboard.value = null;
  admin.value = null;
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

function copy(value) {
  navigator.clipboard.writeText(value);
  notify("已复制到剪贴板");
}

function money(micros) {
  return `$${(Number(micros || 0) / 1_000_000).toFixed(4)}`;
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
  if (!chat.prompt.trim()) return notify("请输入消息", "bad");
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
    messages: [{ role: "user", content: chat.prompt }],
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
    balance_delta: "0",
    group_ids: admin.value.memberships
      .filter((m) => m.user_id === user.id)
      .map((m) => m.group_id),
    grant_tokens: 0,
    grant_reset: "none",
    grant_reset_time: admin.value.settings.default_quota_reset_time || "00:00",
    grant_model_id: "",
    grant_label: "",
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
    return notify(`请先配置并启用供应商：${resource.provider}`, "bad");
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
    default_quota_tokens: settings.default_quota_tokens,
    default_quota_reset: settings.default_quota_reset,
    default_quota_reset_time: settings.default_quota_reset_time,
    public_url: settings.public_url,
    api_public_url: settings.api_public_url,
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
          recipient: session.value.user.email,
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
  } catch (error) {
    notify(error.message, "bad");
  }
});
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark">L</span>
        <div>
          <strong>LLMCtl 模型服务门户</strong
          ><small>模型访问、API Key、额度与用量</small>
        </div>
      </div>
      <div class="top-actions" v-if="session?.authenticated">
        <span class="identity">{{ session.user.email }}</span
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
        <h1>LLMCtl<br /><em>模型服务门户</em></h1>
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
          <h2>{{ authMode === "login" ? "欢迎回来" : "创建 LLMCtl 账户" }}</h2>
          <p class="muted" v-if="authMode === 'register'">
            允许域名：{{
              publicConfig.allowed_domains?.join(", ") || "管理员尚未配置"
            }}
          </p>
          <label
            >邮箱<input
              v-model="auth.email"
              type="email"
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
          {{ isAdmin ? "LLMCtl 管理台" : "用户工作台" }}
        </div>
        <nav>
          <button
            v-for="item in nav"
            :key="item[0]"
            :class="{ active: section === item[0] }"
            @click="section = item[0]"
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
                <p>余额、赠送 Token 和近期使用情况。</p>
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
                <span>赠送 Token</span
                ><strong>{{
                  dashboard.grants
                    .reduce((n, g) => n + g.tokens_remaining, 0)
                    .toLocaleString()
                }}</strong
                ><small>优先于余额消耗</small>
              </article>
              <article>
                <span>已记录请求</span
                ><strong>{{ dashboard.usage.length }}</strong
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
                      <th>赠额</th>
                      <th>金额</th>
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
                        <td>{{ row.granted_tokens }}</td>
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
                        <td colspan="6">
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
                <strong>当前浏览器会话 API Key</strong
                ><small
                  >curl 示例会自动使用此
                  Key；仅保存在当前浏览器会话，不会传给门户后端。</small
                >
              </div>
              <div class="catalog-key-input">
                <input
                  v-model="keyOnce"
                  :type="showApiKey ? 'text' : 'password'"
                  placeholder="尚未保存，请输入或前往 API Key 页面轮换"
                  autocomplete="off"
                  @input="sessionStorage.setItem('llmctl_api_key', keyOnce)"
                /><button
                  class="ghost"
                  :disabled="!keyOnce"
                  @click="showApiKey = !showApiKey"
                >
                  {{ showApiKey ? "隐藏" : "显示" }}</button
                ><button
                  class="ghost"
                  :disabled="!keyOnce"
                  @click="copy(keyOnce)"
                >
                  复制 Key</button
                ><button
                  class="ghost"
                  v-if="!keyOnce"
                  @click="section = 'keys'"
                >
                  前往 API Key
                </button>
              </div>
            </section>
            <div class="model-grid">
              <article
                class="model-card"
                v-for="model in pageRows('user-models', dashboard.models)"
                :key="model.id"
              >
                <div class="model-title">
                  <span class="model-icon">AI</span>
                  <div>
                    <h3>{{ model.display_name }}</h3>
                    <code>{{ model.public_model_id }}</code>
                  </div>
                  <button
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
                <details>
                  <summary>查看 curl 示例</summary>
                  <pre>{{ curlFor(model) }}</pre>
                  <button class="ghost" @click="copy(curlFor(model))">
                    复制完整示例
                  </button>
                </details>
              </article>
            </div>
            <PaginationBar
              :page="pageNumber('user-models', dashboard.models)"
              :pages="pageCount(dashboard.models)"
              :total="dashboard.models.length"
              @previous="changePage('user-models', dashboard.models, -1)"
              @next="changePage('user-models', dashboard.models, 1)"
            />
          </section>

          <section v-if="section === 'playground'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">PLAYGROUND</span>
                <h1>在线测试</h1>
                <p>
                  浏览器以流式方式直接调用 /v1；个人 Key
                  仅保存在当前浏览器会话。
                </p>
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
                    placeholder="sk-..."
                    autocomplete="off"
                    @change="sessionStorage.setItem('llmctl_api_key', keyOnce)"
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
                <div class="button-row">
                  <button
                    class="primary playground-send"
                    :disabled="chat.sending"
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
                    <span>首 Token</span
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
                  <article>
                    <span>Token（入 / 出 / 总）</span
                    ><strong
                      >{{ chat.inputTokens === null ? "—" : chat.inputTokens }}
                      /
                      {{ chat.outputTokens === null ? "—" : chat.outputTokens }}
                      /
                      {{
                        chat.totalTokens === null ? "—" : chat.totalTokens
                      }}</strong
                    >
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
                    <span class="eyebrow">REASONING · 思考过程</span
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
                    <span class="eyebrow">RESPONSE · 最终回答</span
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
                <p>赠送 Token 先消耗，超出部分按模型价格扣减金额余额。</p>
              </div>
            </div>
            <section class="panel">
              <h2>Token 赠额</h2>
              <div class="grant-list">
                <div
                  v-for="grant in pageRows('user-grants', dashboard.grants)"
                  :key="grant.id"
                >
                  <div>
                    <strong>{{ grant.label }}</strong
                    ><small
                      >{{ grant.model_id ? "指定模型" : "所有模型" }} ·
                      {{ grant.reset_interval }}</small
                    >
                  </div>
                  <div class="grant-number">
                    {{ grant.tokens_remaining.toLocaleString() }} /
                    {{ grant.tokens_initial.toLocaleString() }}
                  </div>
                </div>
              </div>
              <PaginationBar
                :page="pageNumber('user-grants', dashboard.grants)"
                :pages="pageCount(dashboard.grants)"
                :total="dashboard.grants.length"
                @previous="changePage('user-grants', dashboard.grants, -1)"
                @next="changePage('user-grants', dashboard.grants, 1)"
              />
            </section>
            <section class="panel">
              <h2>请求用量</h2>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>模型</th>
                      <th>输入 / 输出</th>
                      <th>赠额抵扣</th>
                      <th>金额</th>
                      <th>请求内容</th>
                    </tr>
                  </thead>
                  <tbody>
                    <template
                      v-for="row in pageRows('user-usage', dashboard.usage)"
                      :key="row.id"
                      ><tr>
                        <td>{{ date(row.occurred_at) }}</td>
                        <td>
                          <code>{{ row.public_model_id }}</code>
                        </td>
                        <td>
                          {{ row.input_tokens }} / {{ row.output_tokens }}
                        </td>
                        <td>{{ row.granted_tokens }}</td>
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
                        <td colspan="6">
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
                      <td colspan="6" class="empty">尚无请求用量</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <PaginationBar
                :page="pageNumber('user-usage', dashboard.usage)"
                :pages="pageCount(dashboard.usage)"
                :total="dashboard.usage.length"
                @previous="changePage('user-usage', dashboard.usage, -1)"
                @next="changePage('user-usage', dashboard.usage, 1)"
              />
            </section>
            <section class="panel">
              <h2>金额流水</h2>
              <p class="muted">
                只有充值、余额调整或赠送 Token
                不足而实际扣减金额时，才会产生金额流水。
              </p>
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
                        dashboard.transactions,
                      )"
                      :key="row.id"
                    >
                      <td>{{ date(row.created_at) }}</td>
                      <td>{{ row.kind }}</td>
                      <td>{{ money(row.amount_micros) }}</td>
                      <td>{{ money(row.balance_after_micros) }}</td>
                      <td>{{ row.note }}</td>
                    </tr>
                    <tr v-if="!dashboard.transactions.length">
                      <td colspan="5" class="empty">
                        暂无金额流水；当前请求由赠送 Token
                        抵扣，没有发生金额余额变动。
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <PaginationBar
                :page="pageNumber('user-billing', dashboard.transactions)"
                :pages="pageCount(dashboard.transactions)"
                :total="dashboard.transactions.length"
                @previous="
                  changePage('user-billing', dashboard.transactions, -1)
                "
                @next="changePage('user-billing', dashboard.transactions, 1)"
              />
            </section>
          </section>

          <section v-if="section === 'keys'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">API SECURITY</span>
                <h1>API Key</h1>
                <p>LLMCtl 不保存明文；轮换后旧 Key 立即失效。</p>
              </div>
            </div>
            <section class="panel key-panel">
              <label
                >当前浏览器会话中的 Key<input
                  v-model="keyOnce"
                  type="password"
                  @change="sessionStorage.setItem('llmctl_api_key', keyOnce)"
                  placeholder="未保存；可手工输入或轮换"
              /></label>
              <div class="button-row">
                <button
                  class="ghost"
                  :disabled="!keyOnce"
                  @click="copy(keyOnce)"
                >
                  复制</button
                ><button class="danger" :disabled="busy" @click="rotateKey">
                  轮换并撤销旧 Key
                </button>
              </div>
              <div class="warning">
                请不要把 Key
                写入前端源码、工单或聊天记录。生产应用应从密钥管理系统读取。
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
            <div class="metrics">
              <article>
                <span>用户</span
                ><strong>{{
                  admin.users.filter((u) => u.role === "user").length
                }}</strong
                ><small
                  >{{
                    admin.users.filter(
                      (u) => u.role === "user" && u.status === "active",
                    ).length
                  }}
                  active</small
                >
              </article>
              <article>
                <span>开放模型</span
                ><strong>{{
                  admin.models.filter((m) => m.status === "published").length
                }}</strong
                ><small
                  >{{
                    admin.models.filter((m) => m.health_status === "healthy")
                      .length
                  }}
                  healthy</small
                >
              </article>
              <article>
                <span>免费资源</span
                ><strong>{{
                  admin.free_resources.filter((r) => r.available).length
                }}</strong
                ><small
                  >{{
                    admin.free_resources.filter(
                      (r) => r.test_status === "healthy",
                    ).length
                  }}
                  tested</small
                >
              </article>
              <article>
                <span>权限异常</span
                ><strong>{{
                  admin.users.filter((u) => u.permission_status === "failed")
                    .length
                }}</strong
                ><small>可一键重新对账</small>
              </article>
            </div>
            <section class="panel">
              <h2>服务入口</h2>
              <div class="architecture">
                <div><strong>/ui/</strong><span>LLMCtl 门户</span></div>
                <i>→</i>
                <div>
                  <strong>/portal-api/</strong><span>账户与管理 API</span>
                </div>
                <i class="split">↘</i>
                <div><strong>/v1/</strong><span>推理 API</span></div>
              </div>
            </section>
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
                    v-for="model in pageRows('admin-models', admin.models)"
                    :key="model.id"
                  >
                    <td class="model-summary">
                      <strong>{{ model.display_name }}</strong
                      ><code>{{ model.public_model_id }}</code
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
                          model.health_status === 'healthy' ? 'ok' : 'warn'
                        "
                        >{{ model.health_status }}</span
                      ><small
                        >参数
                        {{ model.metadata_sync_status || "unknown" }}</small
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
              :page="pageNumber('admin-models', admin.models)"
              :pages="pageCount(admin.models)"
              :total="admin.models.length"
              @previous="changePage('admin-models', admin.models, -1)"
              @next="changePage('admin-models', admin.models, 1)"
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
                发现资源
              </button>
            </div>
            <div class="resource-grid">
              <article
                class="resource"
                v-for="item in pageRows('admin-free', admin.free_resources)"
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
                    >{{ item.test_status }}</span
                  >
                </div>
                <div class="resource-meta">
                  <span>类型 {{ item.free_type }}</span
                  ><span>月额 {{ item.monthly_tokens || "—" }}</span
                  ><span>已配置 {{ item.configured ? "是" : "否" }}</span
                  ><span>当前可用 {{ item.available ? "是" : "否" }}</span>
                </div>
                <p class="error-text" v-if="item.test_error">
                  {{ item.test_error }}
                </p>
                <p class="muted" v-if="!item.configured">
                  需要先配置并启用供应商 {{ item.provider }}，才能执行真实测试。
                </p>
                <div class="button-row">
                  <button
                    class="ghost"
                    :disabled="busy || !item.configured"
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
                    class="primary"
                    :disabled="
                      !item.configured ||
                      !item.available ||
                      item.test_status !== 'healthy'
                    "
                    @click="publishFree(item)"
                  >
                    开放给用户
                  </button>
                </div>
              </article>
            </div>
            <PaginationBar
              :page="pageNumber('admin-free', admin.free_resources)"
              :pages="pageCount(admin.free_resources)"
              :total="admin.free_resources.length"
              @previous="changePage('admin-free', admin.free_resources, -1)"
              @next="changePage('admin-free', admin.free_resources, 1)"
            />
          </section>

          <section v-if="section === 'users'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">IDENTITY & QUOTA</span>
                <h1>用户管理</h1>
                <p>禁用、分组、金额调整与额外 Token 赠送集中完成。</p>
              </div>
            </div>
            <div class="table-wrap panel">
              <table>
                <thead>
                  <tr>
                    <th>用户</th>
                    <th>状态</th>
                    <th>余额</th>
                    <th>权限同步</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="user in pageRows(
                      'admin-users',
                      admin.users.filter((u) => u.role === 'user'),
                    )"
                    :key="user.id"
                  >
                    <td>
                      <strong>{{ user.email }}</strong
                      ><small>{{ date(user.created_at) }}</small>
                    </td>
                    <td>
                      <span
                        class="status"
                        :class="user.status === 'active' ? 'ok' : 'bad'"
                        >{{ user.status }}</span
                      >
                    </td>
                    <td>${{ user.balance }}</td>
                    <td>
                      <span
                        class="status"
                        :class="
                          user.permission_status === 'synced' ? 'ok' : 'warn'
                        "
                        >{{ user.permission_status || "pending" }}</span
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
              :page="
                pageNumber(
                  'admin-users',
                  admin.users.filter((u) => u.role === 'user'),
                )
              "
              :pages="pageCount(admin.users.filter((u) => u.role === 'user'))"
              :total="admin.users.filter((u) => u.role === 'user').length"
              @previous="
                changePage(
                  'admin-users',
                  admin.users.filter((u) => u.role === 'user'),
                  -1,
                )
              "
              @next="
                changePage(
                  'admin-users',
                  admin.users.filter((u) => u.role === 'user'),
                  1,
                )
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
            <div class="group-grid">
              <article
                class="panel"
                v-for="group in pageRows('admin-groups', admin.groups)"
                :key="group.id"
              >
                <div class="panel-head">
                  <h2>{{ group.name }}</h2>
                  <span
                    class="status"
                    :class="group.status === 'active' ? 'ok' : 'bad'"
                    >{{ group.status }}</span
                  >
                </div>
                <p>{{ group.description || "暂无描述" }}</p>
                <strong>{{ group.member_count }} 位成员</strong
                ><button class="ghost" @click="editGroup(group)">编辑</button>
              </article>
            </div>
            <PaginationBar
              :page="pageNumber('admin-groups', admin.groups)"
              :pages="pageCount(admin.groups)"
              :total="admin.groups.length"
              @previous="changePage('admin-groups', admin.groups, -1)"
              @next="changePage('admin-groups', admin.groups, 1)"
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
                :disabled="busy"
                @click="
                  action(
                    () =>
                      api('admin/billing/reconcile', {
                        method: 'POST',
                        body: '{}',
                      }),
                    '用量已结算',
                  )
                "
              >
                {{ operation === "general" ? "同步中…" : "同步用量" }}
              </button>
            </div>
            <section class="panel">
              <div class="panel-head">
                <div>
                  <h2>请求用量</h2>
                  <p>包含赠额抵扣与实际金额扣费。</p>
                </div>
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>时间</th>
                      <th>用户</th>
                      <th>模型</th>
                      <th>输入 / 输出</th>
                      <th>赠额</th>
                      <th>金额</th>
                      <th>内容</th>
                    </tr>
                  </thead>
                  <tbody>
                    <template
                      v-for="row in pageRows('admin-usage', admin.usage)"
                      :key="row.id"
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
                        <td colspan="7">
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
                          <div
                            v-else-if="
                              !requestDetails[row.request_id].available
                            "
                            class="empty"
                          >
                            该请求未保留可显示的文本内容。
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
                          </div>
                        </td>
                      </tr></template
                    >
                    <tr v-if="!admin.usage.length">
                      <td colspan="7" class="empty">
                        尚无请求用量；点击“同步用量”从当前接入层读取记录。
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <PaginationBar
                :page="pageNumber('admin-usage', admin.usage)"
                :pages="pageCount(admin.usage)"
                :total="admin.usage.length"
                @previous="changePage('admin-usage', admin.usage, -1)"
                @next="changePage('admin-usage', admin.usage, 1)"
              />
            </section>
            <section class="panel">
              <div class="panel-head">
                <div>
                  <h2>金额流水</h2>
                  <p>仅在充值、余额调整或赠额不足产生实际扣费时出现。</p>
                </div>
              </div>
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
                        admin.transactions,
                      )"
                      :key="row.id"
                    >
                      <td>{{ date(row.created_at) }}</td>
                      <td>
                        <code>{{ row.user_id.slice(0, 8) }}</code>
                      </td>
                      <td>{{ row.kind }}</td>
                      <td>{{ money(row.amount_micros) }}</td>
                      <td>{{ money(row.balance_after_micros) }}</td>
                      <td>{{ row.note }}</td>
                    </tr>
                    <tr v-if="!admin.transactions.length">
                      <td colspan="6" class="empty">
                        暂无金额流水；现有请求可能全部由赠送 Token 抵扣。
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <PaginationBar
                :page="pageNumber('admin-billing', admin.transactions)"
                :pages="pageCount(admin.transactions)"
                :total="admin.transactions.length"
                @previous="changePage('admin-billing', admin.transactions, -1)"
                @next="changePage('admin-billing', admin.transactions, 1)"
              />
            </section>
          </section>

          <section v-if="section === 'settings'" class="page">
            <div class="page-head">
              <div>
                <span class="eyebrow">ONBOARDING</span>
                <h1>注册与 SMTP</h1>
                <p>
                  注册策略和邮件服务分别保存，修改其中一项不会被另一项阻塞。
                </p>
              </div>
            </div>
            <div class="settings-grid">
              <section class="panel form-stack">
                <h2>注册策略</h2>
                <label class="switch"
                  ><input
                    type="checkbox"
                    :checked="settings.registration_enabled === '1'"
                    @change="
                      settings.registration_enabled = $event.target.checked
                        ? '1'
                        : '0'
                    "
                  /><span></span>允许新用户注册</label
                ><label
                  >允许邮箱后缀<input
                    v-model="settings.allowed_domains"
                    placeholder="example.com,corp.example.com" /></label
                ><label
                  >默认赠送 Token<input
                    v-model="settings.default_quota_tokens"
                    type="number" /></label
                ><label
                  >重置周期<select v-model="settings.default_quota_reset">
                    <option>daily</option>
                    <option>weekly</option>
                    <option>monthly</option>
                  </select></label
                ><label
                  >重置时间<input
                    v-model="settings.default_quota_reset_time"
                    type="time" /></label
                ><label
                  >门户公开 URL<input
                    v-model="settings.public_url"
                    placeholder="http://server:8000/ui" /></label
                ><label
                  >API 公开 URL<input
                    v-model="settings.api_public_url"
                    placeholder="http://server:8000" /></label
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
                    v-for="row in pageRows('admin-audit', admin.audit)"
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
                        >{{ row.status }}</span
                      >
                    </td>
                    <td class="detail">{{ row.detail }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <PaginationBar
              :page="pageNumber('admin-audit', admin.audit)"
              :pages="pageCount(admin.audit)"
              :total="admin.audit.length"
              @previous="changePage('admin-audit', admin.audit, -1)"
              @next="changePage('admin-audit', admin.audit, 1)"
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
            <option>active</option>
            <option>disabled</option>
          </select></label
        ><label
          >用户组<select v-model="userEdit.group_ids" multiple>
            <option v-for="group in admin?.groups" :value="group.id">
              {{ group.name }}
            </option>
          </select></label
        ><label
          >金额调整（可为负数）<input v-model="userEdit.balance_delta" /></label
        ><label
          >赠送 Token<input
            v-model.number="userEdit.grant_tokens"
            type="number" /></label
        ><label
          >赠额模型<select v-model="userEdit.grant_model_id">
            <option value="">所有模型</option>
            <option v-for="model in admin?.models" :value="model.id">
              {{ model.public_model_id }}
            </option>
          </select></label
        ><label
          >赠额重置<select v-model="userEdit.grant_reset">
            <option>none</option>
            <option>daily</option>
            <option>weekly</option>
            <option>monthly</option>
          </select></label
        ><label v-if="userEdit.grant_reset !== 'none'"
          >重置时间<input
            v-model="userEdit.grant_reset_time"
            type="time" /></label
        ><label>说明<input v-model="userEdit.grant_label" /></label
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
            <option>active</option>
            <option>disabled</option>
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

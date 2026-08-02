<script setup>
import { computed, onMounted, reactive, ref } from 'vue'

const session = ref(null)
const publicConfig = ref({ registration_enabled: false, allowed_domains: [] })
const dashboard = ref(null)
const admin = ref(null)
const busy = ref(false)
const toast = reactive({ text: '', kind: 'ok' })
const authMode = ref(location.hash.startsWith('#/register') ? 'register' : location.hash.startsWith('#/verify') ? 'verify' : 'login')
const section = ref('overview')
const auth = reactive({ email: '', password: '', confirm: '', token: new URLSearchParams(location.hash.split('?')[1] || '').get('token') || '' })
const chat = reactive({ model: '', prompt: '请简要介绍一下你自己。', result: '', sending: false })
const keyOnce = ref(sessionStorage.getItem('llmctl_api_key') || '')
const userEdit = reactive({ user_id: '', status: 'active', balance_delta: '0', group_ids: [], grant_tokens: 0, grant_reset: 'none', grant_reset_time: '00:00', grant_model_id: '', grant_label: '' })
const groupEdit = reactive({ id: '', name: '', description: '', status: 'active' })
const modelEdit = reactive({ id: '', public_model_id: '', display_name: '', description: '', source_kind: 'combo', source_ref: '', source_provider: '', source_model: '', capabilities: ['chat'], input_price: '0', output_price: '0', cached_price: '0', reasoning_price: '0', status: 'published', access: [{ type: 'all', id: '' }] })
const settings = reactive({})

const isAdmin = computed(() => session.value?.user?.role === 'admin')
const nav = computed(() => isAdmin.value ? [
  ['overview', '总览'], ['models', '模型与定价'], ['free', '免费资源'], ['users', '用户'], ['groups', '用户组'], ['billing', '账单'], ['settings', '注册与 SMTP'], ['audit', '审计']
] : [['overview', '工作台'], ['models', '模型广场'], ['playground', '在线测试'], ['billing', '用量与账单'], ['keys', 'API Key']])

function cookie(name) {
  return document.cookie.split('; ').find(v => v.startsWith(`${name}=`))?.split('=').slice(1).join('=') || ''
}

async function api(path, options = {}) {
  const response = await fetch(`/portal-api/${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': decodeURIComponent(cookie('llm_account_csrf')), ...(options.headers || {}) },
    ...options,
  })
  const body = await response.json().catch(() => ({ error: `HTTP ${response.status}` }))
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`)
  return body
}

function notify(text, kind = 'ok') {
  toast.text = text; toast.kind = kind
  window.setTimeout(() => { if (toast.text === text) toast.text = '' }, 5000)
}

async function load() {
  publicConfig.value = await api('public')
  session.value = await api('session')
  if (!session.value.authenticated) return
  if (isAdmin.value) {
    admin.value = await api('admin')
    Object.assign(settings, admin.value.settings)
  } else {
    dashboard.value = await api('dashboard')
    if (!chat.model) chat.model = dashboard.value.models[0]?.public_model_id || ''
  }
}

async function action(fn, success = '操作成功') {
  busy.value = true
  try { const result = await fn(); notify(success); await load(); return result }
  catch (error) { notify(error.message, 'bad'); throw error }
  finally { busy.value = false }
}

async function login() {
  await action(async () => {
    await api('auth/login', { method: 'POST', body: JSON.stringify(auth) })
  }, '登录成功')
}

async function register() {
  await action(() => api('auth/register', { method: 'POST', body: JSON.stringify(auth) }), '验证邮件已发送')
}

async function verify() {
  const result = await action(() => api('auth/verify', { method: 'POST', body: JSON.stringify({ token: auth.token }) }), '账户已开通，请立即保存 API Key')
  if (result?.api_key) { keyOnce.value = result.api_key; sessionStorage.setItem('llmctl_api_key', result.api_key) }
}

async function logout() {
  await api('auth/logout', { method: 'POST', body: '{}' })
  sessionStorage.removeItem('llmctl_api_key'); keyOnce.value = ''; dashboard.value = null; admin.value = null
  await load()
}

async function rotateKey() {
  const result = await action(() => api('key/rotate', { method: 'POST', body: '{}' }), 'API Key 已轮换，旧 Key 已失效')
  keyOnce.value = result.api_key; sessionStorage.setItem('llmctl_api_key', result.api_key)
}

function copy(value) {
  navigator.clipboard.writeText(value); notify('已复制到剪贴板')
}

function money(micros) { return `$${(Number(micros || 0) / 1_000_000).toFixed(4)}` }
function date(value) { return value ? new Date(Number.isFinite(+value) ? +value * 1000 : value).toLocaleString() : '—' }

function curlFor(model) {
  const base = dashboard.value?.api_base || `${publicConfig.value.api_public_url}/v1`
  return `curl ${base}/chat/completions \\\n+  -H 'Authorization: Bearer YOUR_API_KEY' \\\n+  -H 'Content-Type: application/json' \\\n+  -d '${JSON.stringify({ model: model.public_model_id, stream: false, messages: [{ role: 'user', content: '你好' }] })}'`
}

async function sendChat() {
  if (!keyOnce.value) return notify('请先输入或轮换个人 API Key', 'bad')
  chat.sending = true; chat.result = ''
  try {
    const response = await fetch(`${dashboard.value.api_base}/chat/completions`, {
      method: 'POST', headers: { Authorization: `Bearer ${keyOnce.value}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: chat.model, stream: false, messages: [{ role: 'user', content: chat.prompt }] }),
    })
    const body = await response.json()
    if (!response.ok) throw new Error(body.error?.message || body.error || `HTTP ${response.status}`)
    chat.result = body.choices?.[0]?.message?.content || JSON.stringify(body, null, 2)
  } catch (error) { notify(error.message, 'bad') }
  finally { chat.sending = false }
}

function editUser(user) {
  Object.assign(userEdit, { user_id: user.id, status: user.status, balance_delta: '0', group_ids: admin.value.memberships.filter(m => m.user_id === user.id).map(m => m.group_id), grant_tokens: 0, grant_reset: 'none', grant_reset_time: admin.value.settings.default_quota_reset_time || '00:00', grant_model_id: '', grant_label: '' })
  document.querySelector('#user-editor')?.showModal()
}
function editGroup(group = {}) { Object.assign(groupEdit, { id: group.id || '', name: group.name || '', description: group.description || '', status: group.status || 'active' }); document.querySelector('#group-editor')?.showModal() }
function editModel(model = {}) {
  Object.assign(modelEdit, { id: model.id || '', public_model_id: model.public_model_id || '', display_name: model.display_name || '', description: model.description || '', source_kind: model.source_kind || 'combo', source_ref: model.source_ref || '', source_provider: model.source_provider || '', source_model: model.source_model || '', capabilities: model.capabilities ? [...model.capabilities] : ['chat'], input_price: model.input_price || '0', output_price: model.output_price || '0', cached_price: model.cached_price || '0', reasoning_price: model.reasoning_price || '0', status: model.status || 'published', access: model.access?.map(a => ({ type: a.subject_type, id: a.subject_id })) || [{ type: 'all', id: '' }] })
  document.querySelector('#model-editor')?.showModal()
}
function publishFree(resource) {
  editModel({ source_kind: 'free', source_ref: resource.resource_key, source_provider: resource.provider, source_model: resource.model_id, public_model_id: resource.model_id, display_name: resource.display_name, capabilities: ['chat'], status: 'published' })
}
function addModelAccess() { modelEdit.access.push({ type: 'group', id: '' }) }
function removeModelAccess(index) {
  if (modelEdit.access.length === 1) Object.assign(modelEdit.access[0], { type: 'all', id: '' })
  else modelEdit.access.splice(index, 1)
}
function selectCombo(event) {
  const combo = admin.value?.combos.find(item => String(item.id) === event.target.value)
  if (combo) modelEdit.source_model = combo.name || combo.id
}

async function saveSettings() {
  const body = { ...settings, registration_enabled: settings.registration_enabled === '1' || settings.registration_enabled === true }
  await action(() => api('admin/settings', { method: 'POST', body: JSON.stringify(body) }), '注册与 SMTP 设置已保存')
}

onMounted(async () => { try { await load() } catch (error) { notify(error.message, 'bad') } })
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <div class="brand"><span class="brand-mark">L</span><div><strong>LLMCtl 企业 AI 门户</strong><small>轻量控制平面 · OmniRoute 数据路径直连</small></div></div>
      <div class="top-actions" v-if="session?.authenticated"><span class="identity">{{ session.user.email }}</span><button class="ghost" @click="logout">退出</button></div>
    </header>

    <div v-if="toast.text" class="toast" :class="toast.kind">{{ toast.text }}</div>

    <main v-if="!session?.authenticated" class="auth-page">
      <section class="auth-copy"><span class="eyebrow">PRIVATE AI, SIMPLIFIED</span><h1>公司模型，一个入口。<br><em>权限、额度与审计都清楚。</em></h1><p>用户只看到被授权的模型与价格；管理员无需进入 OmniRoute 完成日常工作。模型调用直接进入高性能网关，不经过门户后端。</p><div class="trust-row"><span>✓ 企业邮箱验证</span><span>✓ 独立 SQLite</span><span>✓ API Key 原生鉴权</span></div></section>
      <section class="auth-card">
        <div class="segmented"><button :class="{active: authMode==='login'}" @click="authMode='login'">登录</button><button :disabled="!publicConfig.registration_enabled" :class="{active: authMode==='register'}" @click="authMode='register'">注册</button></div>
        <template v-if="authMode==='verify'"><h2>验证公司邮箱</h2><p class="muted">确认后会创建个人 API Key，明文仅显示一次。</p><label>验证令牌<input v-model="auth.token" /></label><button class="primary wide-button" :disabled="busy" @click="verify">确认并开通</button></template>
        <form v-else @submit.prevent="authMode==='login' ? login() : register()">
          <h2>{{ authMode==='login' ? '欢迎回来' : '创建企业账户' }}</h2>
          <p class="muted" v-if="authMode==='register'">允许域名：{{ publicConfig.allowed_domains?.join(', ') || '管理员尚未配置' }}</p>
          <label>公司邮箱<input v-model="auth.email" type="email" autocomplete="username" required /></label>
          <label>密码<input v-model="auth.password" type="password" minlength="12" autocomplete="current-password" required /></label>
          <label v-if="authMode==='register'">确认密码<input v-model="auth.confirm" type="password" minlength="12" required /></label>
          <button class="primary wide-button" :disabled="busy">{{ authMode==='login' ? '登录门户' : '发送验证邮件' }}</button>
        </form>
        <p class="footnote">注册策略、邮箱后缀和 SMTP 均由公司管理员控制。</p>
      </section>
    </main>

    <div v-else class="workspace">
      <aside class="sidebar"><div class="role-pill">{{ isAdmin ? '管理员控制台' : '用户工作台' }}</div><nav><button v-for="item in nav" :key="item[0]" :class="{active:section===item[0]}" @click="section=item[0]"><span class="nav-dot"></span>{{ item[1] }}</button></nav><div class="side-note"><strong>推理不经门户</strong><p>/v1 请求由 Nginx 直接送往 OmniRoute。</p></div></aside>
      <main class="content">
        <div v-if="isAdmin && admin?.gateway_error" class="warning"><strong>OmniRoute 当前不可用：</strong>{{ admin.gateway_error }}。本地用户、SMTP、账本和审计仍可查看；涉及模型、Key 或实时对账的操作请在网关恢复后执行。</div>
        <template v-if="!isAdmin && dashboard">
          <section v-if="section==='overview'" class="page"><div class="page-head"><div><span class="eyebrow">YOUR ACCOUNT</span><h1>工作台</h1><p>余额、赠送 Token 和近期使用情况。</p></div><span class="status ok">服务可用</span></div><div class="metrics"><article><span>金额余额</span><strong>${{ dashboard.balance }}</strong><small>付费模型消费</small></article><article><span>可用模型</span><strong>{{ dashboard.models.length }}</strong><small>按个人与用户组授权</small></article><article><span>赠送 Token</span><strong>{{ dashboard.grants.reduce((n,g)=>n+g.tokens_remaining,0).toLocaleString() }}</strong><small>优先于余额消耗</small></article><article><span>已记录请求</span><strong>{{ dashboard.usage.length }}</strong><small>门户计费账本</small></article></div><section class="panel"><div class="panel-head"><h2>最近请求</h2></div><div class="table-wrap"><table><thead><tr><th>时间</th><th>模型</th><th>输入 / 输出</th><th>赠额</th><th>金额</th></tr></thead><tbody><tr v-for="row in dashboard.usage.slice(0,8)" :key="row.id"><td>{{ date(row.occurred_at) }}</td><td><code>{{ row.public_model_id }}</code></td><td>{{ row.input_tokens }} / {{ row.output_tokens }}</td><td>{{ row.granted_tokens }}</td><td>{{ money(row.amount_micros) }}</td></tr><tr v-if="!dashboard.usage.length"><td colspan="5" class="empty">尚无用量</td></tr></tbody></table></div></section></section>

          <section v-if="section==='models'" class="page"><div class="page-head"><div><span class="eyebrow">MODEL CATALOG</span><h1>模型广场</h1><p>这里只展示你真正有权限调用的模型。</p></div></div><div class="model-grid"><article class="model-card" v-for="model in dashboard.models" :key="model.id"><div class="model-title"><span class="model-icon">AI</span><div><h3>{{ model.display_name }}</h3><code>{{ model.public_model_id }}</code></div><button class="icon-button" @click="copy(model.public_model_id)">复制 ID</button></div><p>{{ model.description || '暂无描述' }}</p><div class="chips"><span v-for="cap in model.capabilities" :key="cap">{{ cap }}</span></div><div class="price-grid"><span>输入<strong>${{ model.input_price }}/1M</strong></span><span>输出<strong>${{ model.output_price }}/1M</strong></span><span>缓存<strong>${{ model.cached_price }}/1M</strong></span></div><details><summary>查看 curl 示例</summary><pre>{{ curlFor(model) }}</pre><button class="ghost" @click="copy(curlFor(model))">复制示例</button></details></article></div></section>

          <section v-if="section==='playground'" class="page"><div class="page-head"><div><span class="eyebrow">PLAYGROUND</span><h1>在线测试</h1><p>浏览器直接调用 /v1；个人 Key 仅保存在当前浏览器会话。</p></div></div><div class="chat-layout"><section class="panel form-stack"><label>模型<select v-model="chat.model"><option v-for="model in dashboard.models" :value="model.public_model_id">{{ model.public_model_id }}</option></select></label><label>API Key<input v-model="keyOnce" type="password" placeholder="sk-..." @change="sessionStorage.setItem('llmctl_api_key', keyOnce)" /></label><label>消息<textarea v-model="chat.prompt" rows="7"></textarea></label><button class="primary" :disabled="chat.sending" @click="sendChat">{{ chat.sending ? '生成中…' : '发送请求' }}</button></section><section class="panel answer"><span class="eyebrow">RESPONSE</span><pre>{{ chat.result || '模型回复会显示在这里。' }}</pre></section></div></section>

          <section v-if="section==='billing'" class="page"><div class="page-head"><div><span class="eyebrow">USAGE & BILLING</span><h1>用量与账单</h1><p>赠送 Token 先消耗，超出部分按模型价格扣减金额余额。</p></div></div><section class="panel"><h2>Token 赠额</h2><div class="grant-list"><div v-for="grant in dashboard.grants" :key="grant.id"><div><strong>{{ grant.label }}</strong><small>{{ grant.model_id ? '指定模型' : '所有模型' }} · {{ grant.reset_interval }}</small></div><div class="grant-number">{{ grant.tokens_remaining.toLocaleString() }} / {{ grant.tokens_initial.toLocaleString() }}</div></div></div></section><section class="panel"><h2>金额流水</h2><div class="table-wrap"><table><thead><tr><th>时间</th><th>类型</th><th>变动</th><th>余额</th><th>备注</th></tr></thead><tbody><tr v-for="row in dashboard.transactions" :key="row.id"><td>{{ date(row.created_at) }}</td><td>{{ row.kind }}</td><td>{{ money(row.amount_micros) }}</td><td>{{ money(row.balance_after_micros) }}</td><td>{{ row.note }}</td></tr></tbody></table></div></section></section>

          <section v-if="section==='keys'" class="page"><div class="page-head"><div><span class="eyebrow">API SECURITY</span><h1>API Key</h1><p>门户不保存明文；轮换后旧 Key 立即失效。</p></div></div><section class="panel key-panel"><label>当前浏览器会话中的 Key<input v-model="keyOnce" type="password" @change="sessionStorage.setItem('llmctl_api_key', keyOnce)" placeholder="未保存；可手工输入或轮换" /></label><div class="button-row"><button class="ghost" :disabled="!keyOnce" @click="copy(keyOnce)">复制</button><button class="danger" :disabled="busy" @click="rotateKey">轮换并撤销旧 Key</button></div><div class="warning">请不要把 Key 写入前端源码、工单或聊天记录。生产应用应从密钥管理系统读取。</div></section></section>
        </template>

        <template v-if="isAdmin && admin">
          <section v-if="section==='overview'" class="page"><div class="page-head"><div><span class="eyebrow">CONTROL PLANE</span><h1>运行总览</h1><p>日常业务管理集中在这里，OmniRoute 对最终用户不可见。</p></div><div class="button-row"><button class="ghost" @click="action(()=>api('admin/permissions/reconcile',{method:'POST',body:'{}'}),'权限已对账')">权限对账</button><button class="primary" @click="action(()=>api('admin/billing/reconcile',{method:'POST',body:'{}'}),'用量已对账')">立即结算</button></div></div><div class="metrics"><article><span>用户</span><strong>{{ admin.users.filter(u=>u.role==='user').length }}</strong><small>{{ admin.users.filter(u=>u.role==='user' && u.status==='active').length }} active</small></article><article><span>开放模型</span><strong>{{ admin.models.filter(m=>m.status==='published').length }}</strong><small>{{ admin.models.filter(m=>m.health_status==='healthy').length }} healthy</small></article><article><span>免费资源</span><strong>{{ admin.free_resources.filter(r=>r.available).length }}</strong><small>{{ admin.free_resources.filter(r=>r.test_status==='healthy').length }} tested</small></article><article><span>权限异常</span><strong>{{ admin.users.filter(u=>u.permission_status==='failed').length }}</strong><small>可一键重新对账</small></article></div><section class="panel"><h2>架构边界</h2><div class="architecture"><div><strong>/ui/</strong><span>Vue 企业门户</span></div><i>→</i><div><strong>/portal-api/</strong><span>轻量控制平面</span></div><i class="split">↘</i><div><strong>/v1/</strong><span>直达 OmniRoute</span></div></div></section></section>

          <section v-if="section==='models'" class="page"><div class="page-head"><div><span class="eyebrow">MODEL POLICY</span><h1>模型、映射与定价</h1><p>模型 ID 映射写入 OmniRoute；价格和授权策略由门户维护。</p></div><button class="primary" @click="editModel()">发布模型</button></div><div class="table-wrap panel"><table><thead><tr><th>公开 ID</th><th>来源</th><th>价格（入/出）</th><th>能力</th><th>健康</th><th></th></tr></thead><tbody><tr v-for="model in admin.models" :key="model.id"><td><strong>{{ model.display_name }}</strong><code>{{ model.public_model_id }}</code></td><td>{{ model.source_kind }}<small>{{ model.source_model }}</small></td><td>${{ model.input_price }} / ${{ model.output_price }}</td><td><div class="chips"><span v-for="cap in model.capabilities">{{ cap }}</span></div></td><td><span class="status" :class="model.health_status==='healthy'?'ok':'warn'">{{ model.health_status }}</span></td><td><button class="ghost" @click="editModel(model)">编辑</button><button class="ghost" @click="action(()=>api('admin/models/test',{method:'POST',body:JSON.stringify({model_id:model.id})}),'模型测试通过')">测试</button></td></tr></tbody></table></div></section>

          <section v-if="section==='free'" class="page"><div class="page-head"><div><span class="eyebrow">FREE-TIER DISCOVERY</span><h1>免费资源</h1><p>发现不等于可用：必须配置、在线实测并明确发布后用户才能访问。</p></div><button class="primary" @click="action(()=>api('admin/free/discover',{method:'POST',body:'{}'}),'免费目录已刷新')">发现资源</button></div><div class="resource-grid"><article class="resource" v-for="item in admin.free_resources" :key="item.resource_key"><div class="resource-head"><div><strong>{{ item.display_name }}</strong><code>{{ item.provider }} · {{ item.model_id }}</code></div><span class="status" :class="item.test_status==='healthy'?'ok':item.test_status==='failed'?'bad':'warn'">{{ item.test_status }}</span></div><div class="resource-meta"><span>类型 {{ item.free_type }}</span><span>月额 {{ item.monthly_tokens || '—' }}</span><span>已配置 {{ item.configured ? '是' : '否' }}</span><span>当前可用 {{ item.available ? '是' : '否' }}</span></div><p class="error-text" v-if="item.test_error">{{ item.test_error }}</p><div class="button-row"><button class="ghost" @click="action(()=>api('admin/free/test',{method:'POST',body:JSON.stringify({resource_key:item.resource_key})}),'实时测试通过')">实时测试</button><button class="primary" :disabled="!item.configured || !item.available || item.test_status!=='healthy'" @click="publishFree(item)">开放给用户</button></div></article></div></section>

          <section v-if="section==='users'" class="page"><div class="page-head"><div><span class="eyebrow">IDENTITY & QUOTA</span><h1>用户管理</h1><p>禁用、分组、金额调整与额外 Token 赠送集中完成。</p></div></div><div class="table-wrap panel"><table><thead><tr><th>用户</th><th>状态</th><th>余额</th><th>权限同步</th><th></th></tr></thead><tbody><tr v-for="user in admin.users.filter(u=>u.role==='user')" :key="user.id"><td><strong>{{ user.email }}</strong><small>{{ date(user.created_at) }}</small></td><td><span class="status" :class="user.status==='active'?'ok':'bad'">{{ user.status }}</span></td><td>${{ user.balance }}</td><td><span class="status" :class="user.permission_status==='synced'?'ok':'warn'">{{ user.permission_status || 'pending' }}</span><small v-if="user.permission_error">{{ user.permission_error }}</small></td><td><button class="ghost" @click="editUser(user)">管理</button></td></tr></tbody></table></div></section>

          <section v-if="section==='groups'" class="page"><div class="page-head"><div><span class="eyebrow">ACCESS GROUPS</span><h1>用户组</h1><p>门户计算用户与组的有效模型集合，再同步到个人 OmniRoute Key。</p></div><button class="primary" @click="editGroup()">新建用户组</button></div><div class="group-grid"><article class="panel" v-for="group in admin.groups"><div class="panel-head"><h2>{{ group.name }}</h2><span class="status" :class="group.status==='active'?'ok':'bad'">{{ group.status }}</span></div><p>{{ group.description || '暂无描述' }}</p><strong>{{ group.member_count }} 位成员</strong><button class="ghost" @click="editGroup(group)">编辑</button></article></div></section>

          <section v-if="section==='billing'" class="page"><div class="page-head"><div><span class="eyebrow">LEDGER</span><h1>计费账本</h1><p>模型价格按版本快照；请求 ID 唯一，重复拉取不会重复扣费。</p></div><button class="primary" @click="action(()=>api('admin/billing/reconcile',{method:'POST',body:'{}'}),'用量已结算')">从 OmniRoute 对账</button></div><section class="panel"><div class="table-wrap"><table><thead><tr><th>时间</th><th>用户</th><th>类型</th><th>变动</th><th>余额</th><th>备注</th></tr></thead><tbody><tr v-for="row in admin.transactions" :key="row.id"><td>{{ date(row.created_at) }}</td><td><code>{{ row.user_id.slice(0,8) }}</code></td><td>{{ row.kind }}</td><td>{{ money(row.amount_micros) }}</td><td>{{ money(row.balance_after_micros) }}</td><td>{{ row.note }}</td></tr></tbody></table></div></section></section>

          <section v-if="section==='settings'" class="page"><div class="page-head"><div><span class="eyebrow">ONBOARDING</span><h1>注册与 SMTP</h1><p>可关闭注册，并严格限制公司邮箱后缀。</p></div></div><div class="settings-grid"><section class="panel form-stack"><h2>注册策略</h2><label class="switch"><input type="checkbox" :checked="settings.registration_enabled==='1'" @change="settings.registration_enabled=$event.target.checked?'1':'0'" /><span></span>允许新用户注册</label><label>允许邮箱后缀<input v-model="settings.allowed_domains" placeholder="example.com,corp.example.com" /></label><label>默认赠送 Token<input v-model="settings.default_quota_tokens" type="number" /></label><label>重置周期<select v-model="settings.default_quota_reset"><option>daily</option><option>weekly</option><option>monthly</option></select></label><label>重置时间<input v-model="settings.default_quota_reset_time" type="time" /></label><label>门户公开 URL<input v-model="settings.public_url" placeholder="http://server:8000/ui" /></label><label>API 公开 URL<input v-model="settings.api_public_url" placeholder="http://server:8000" /></label></section><section class="panel form-stack"><h2>SMTP</h2><label>主机<input v-model="settings.smtp_host" /></label><label>端口<input v-model="settings.smtp_port" type="number" /></label><label>安全<select v-model="settings.smtp_security"><option>starttls</option><option>ssl</option><option>plain</option></select></label><label>用户名<input v-model="settings.smtp_username" /></label><label>密码<input v-model="settings.smtp_password" type="password" :placeholder="settings.smtp_password_configured==='1'?'已配置；留空保持不变':''" /></label><label>发件人<input v-model="settings.smtp_from" type="email" /></label><div class="button-row"><button class="ghost" @click="action(()=>api('admin/smtp/test',{method:'POST',body:JSON.stringify({recipient:session.user.email})}),'测试邮件已发送')">发送测试邮件</button><button class="primary" @click="saveSettings">保存全部设置</button></div></section></div></section>

          <section v-if="section==='audit'" class="page"><div class="page-head"><div><span class="eyebrow">AUDIT TRAIL</span><h1>审计日志</h1><p>门户管理动作、失败与操作者均持久记录。</p></div></div><div class="table-wrap panel"><table><thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>目标</th><th>结果</th><th>详情</th></tr></thead><tbody><tr v-for="row in admin.audit" :key="row.id"><td>{{ date(row.created_at) }}</td><td>{{ row.actor }}</td><td><code>{{ row.action }}</code></td><td>{{ row.target }}</td><td><span class="status" :class="row.status==='success'?'ok':'bad'">{{ row.status }}</span></td><td class="detail">{{ row.detail }}</td></tr></tbody></table></div></section>
        </template>
      </main>
    </div>

    <dialog id="user-editor"><form method="dialog" class="dialog-head"><h2>管理用户</h2><button class="icon-button">×</button></form><div class="form-stack"><label>状态<select v-model="userEdit.status"><option>active</option><option>disabled</option></select></label><label>用户组<select v-model="userEdit.group_ids" multiple><option v-for="group in admin?.groups" :value="group.id">{{ group.name }}</option></select></label><label>金额调整（可为负数）<input v-model="userEdit.balance_delta" /></label><label>赠送 Token<input v-model.number="userEdit.grant_tokens" type="number" /></label><label>赠额模型<select v-model="userEdit.grant_model_id"><option value="">所有模型</option><option v-for="model in admin?.models" :value="model.id">{{ model.public_model_id }}</option></select></label><label>赠额重置<select v-model="userEdit.grant_reset"><option>none</option><option>daily</option><option>weekly</option><option>monthly</option></select></label><label v-if="userEdit.grant_reset!=='none'">重置时间<input v-model="userEdit.grant_reset_time" type="time" /></label><label>说明<input v-model="userEdit.grant_label" /></label><button class="primary" @click="action(()=>api('admin/users/update',{method:'POST',body:JSON.stringify(userEdit)}),'用户已更新').then(()=>document.querySelector('#user-editor').close())">保存并同步权限</button></div></dialog>
    <dialog id="group-editor"><form method="dialog" class="dialog-head"><h2>用户组</h2><button class="icon-button">×</button></form><div class="form-stack"><label>名称<input v-model="groupEdit.name" /></label><label>描述<textarea v-model="groupEdit.description"></textarea></label><label>状态<select v-model="groupEdit.status"><option>active</option><option>disabled</option></select></label><button class="primary" @click="action(()=>api('admin/groups/save',{method:'POST',body:JSON.stringify(groupEdit)}),'用户组已保存').then(()=>document.querySelector('#group-editor').close())">保存</button></div></dialog>
    <dialog id="model-editor"><form method="dialog" class="dialog-head"><h2>发布模型</h2><button class="icon-button">×</button></form><div class="form-grid"><label>公开模型 ID<input v-model="modelEdit.public_model_id" placeholder="gdn-inside" /></label><label>显示名称<input v-model="modelEdit.display_name" /></label><label>来源类型<select v-model="modelEdit.source_kind"><option value="combo">OmniRoute Combo</option><option value="model">普通模型</option><option value="free">免费层资源</option></select></label><label>来源模型 ID<input v-model="modelEdit.source_model" list="gateway-models" /><datalist id="gateway-models"><option v-for="model in admin?.gateway_models" :value="model.id" /></datalist></label><label v-if="modelEdit.source_kind==='combo'">OmniRoute Combo<select v-model="modelEdit.source_ref" @change="selectCombo"><option value="">请选择</option><option v-for="combo in admin?.combos" :value="combo.id">{{ combo.name || combo.id }}</option></select></label><label v-else>来源引用<input v-model="modelEdit.source_ref" :readonly="modelEdit.source_kind==='free'" /></label><label>供应商<input v-model="modelEdit.source_provider" /></label><label>状态<select v-model="modelEdit.status"><option>draft</option><option>published</option><option>disabled</option></select></label><label>能力（多选）<select v-model="modelEdit.capabilities" multiple><option>chat</option><option>vision</option><option>ocr</option><option>tools</option><option>reasoning</option><option>embedding</option></select></label><label>输入价 $/1M<input v-model="modelEdit.input_price" /></label><label>输出价 $/1M<input v-model="modelEdit.output_price" /></label><label>缓存价 $/1M<input v-model="modelEdit.cached_price" /></label><label>思考价 $/1M<input v-model="modelEdit.reasoning_price" /></label><label class="span-2">描述<textarea v-model="modelEdit.description"></textarea></label><section class="span-2 access-editor"><div class="panel-head"><strong>开放范围</strong><button class="ghost" @click="addModelAccess">增加规则</button></div><div class="access-row" v-for="(rule,index) in modelEdit.access" :key="index"><select v-model="rule.type"><option value="all">所有用户</option><option value="group">指定用户组</option><option value="user">指定用户</option></select><select v-if="rule.type==='group'" v-model="rule.id"><option value="">请选择用户组</option><option v-for="group in admin?.groups.filter(g=>g.status==='active')" :value="group.id">{{ group.name }}</option></select><select v-else-if="rule.type==='user'" v-model="rule.id"><option value="">请选择用户</option><option v-for="user in admin?.users.filter(u=>u.role==='user')" :value="user.id">{{ user.email }}</option></select><span v-else class="access-all">门户内所有有效用户</span><button class="danger" @click="removeModelAccess(index)">移除</button></div></section></div><div class="warning">发布前会执行真实模型请求；Combo 映射直接写入 OmniRoute，不增加推理转发层。用户只获得公开 ID 权限，不能绕过映射调用底层 ID。</div><button class="primary wide-button" @click="action(()=>api('admin/models/save',{method:'POST',body:JSON.stringify(modelEdit)}),'模型已测试、发布并同步权限').then(()=>document.querySelector('#model-editor').close())">测试并保存</button></dialog>
  </div>
</template>

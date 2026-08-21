<script>
import { usePortalWorkspaceContext } from "../portalWorkspaceContext.js";
import ListFilterBar from "./ListFilterBar.vue";
import PaginationBar from "./PaginationBar.vue";

export default {
  name: "PortalUserPages",
  components: { ListFilterBar, PaginationBar },
  setup() {
    return usePortalWorkspaceContext();
  },
};
</script>

<template>
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
                            该请求产生时未开启详细日志、启用了 noLog，或内容已过保留期；历史输入无法补录。
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
                              >内容超过单次安全显示上限，当前已显示前 1,000,000 个字符。</small
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
                            该请求产生时未开启详细日志、启用了 noLog，或内容已过保留期；历史输入无法补录。
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
                              >内容超过单次安全显示上限，当前已显示前 1,000,000 个字符。</small
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

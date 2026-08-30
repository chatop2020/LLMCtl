<script>
import { usePortalWorkspaceContext } from "../portalWorkspaceContext.js";
import ListFilterBar from "./ListFilterBar.vue";
import PaginationBar from "./PaginationBar.vue";

export default {
  name: "PortalAdminCorePages",
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

          <section v-if="section === 'deployments'" class="page model-deployment-page">
            <div class="page-head">
              <div>
                <h1>模型部署</h1>
                <p>下载模型、分配 GPU、生成 Worker 实例，并同步到当前 AI 接入层。</p>
              </div>
              <button
                type="button"
                class="ghost"
                :disabled="modelDeploymentsLoading"
                @click="loadModelDeployments()"
              >
                {{ modelDeploymentsLoading ? "读取中…" : "刷新状态" }}
              </button>
            </div>

            <section v-if="modelDeployments?.available === false" class="panel deployment-unavailable">
              <h2>模型部署控制服务不可用</h2>
              <p>{{ modelDeployments.error }}</p>
              <p>
                从旧版 LLMCtl 首次升级时，运行时文件可能已经就绪，但 systemd 服务尚未注册。
                这不需要安装额外软件，也不会影响当前 Router 或 Worker。
              </p>
              <strong>先执行</strong>
              <code>llmctl model init</code>
              <strong>然后确认</strong>
              <code>llmctl model status</code>
              <code>llmctl logs model</code>
            </section>

            <template v-else>
              <section class="panel deployment-safety-note">
                <strong>变更边界</strong>
                <p>
                  部署前会生成计划并备份注册表与受影响 Worker 配置；任务只重启发生 GPU 或 Worker
                  归属变化的实例，失败时自动恢复备份。现有 Router、门户和未受影响 Worker 不会重启。
                </p>
              </section>

              <section class="panel qwen38-quick-card">
                <div class="qwen38-quick-head">
                  <div>
                    <span class="eyebrow">RECOMMENDED FOR THIS SERVER</span>
                    <h2>Qwen3.8 Flash Next 一键部署</h2>
                    <p>点击一次，从 ModelScope 下载固定模型，连同视觉编码器部署四个 TP2 实例；逐实例完成真实图文测试后上线为 <code>gdn-inside</code>。</p>
                  </div>
                  <span class="status" :class="qwen38Quick.profile?.active ? 'ok' : qwen38Quick.profile?.available ? 'warn' : 'bad'">
                    {{ qwen38Quick.profile?.active ? "已上线" : qwen38Quick.profile?.available ? "可以部署" : "需要处理" }}
                  </span>
                </div>

                <div class="qwen38-transition">
                  <article>
                    <small>当前 gdn-inside</small>
                    <strong>{{ qwen38Quick.profile?.current_model?.display_name || "未识别" }}</strong>
                    <span>{{ qwen38Quick.profile?.current_model?.model_id || "尚无公开模型" }}</span>
                  </article>
                  <b aria-hidden="true">→</b>
                  <article class="target">
                    <small>自动部署目标</small>
                    <strong>Qwen3.8 Flash Next NVFP4</strong>
                    <span>ModelScope 固定版本；公开调用仍使用 <code>gdn-inside</code></span>
                  </article>
                </div>

                <div class="qwen38-preset-grid">
                  <article><strong>8 张 GPU</strong><span>自动分为 4 个 TP2 实例</span></article>
                  <article><strong>262K 上下文</strong><span>原生窗口，不启用 YaRN</span></article>
                  <article><strong>NVFP4 + BF16 KV</strong><span>PLE 放入主内存，开启 EP</span></article>
                  <article><strong>图文多模态</strong><span>包含视觉编码器，默认每请求最多 4 张图</span></article>
                  <article><strong>自动保护</strong><span>失败自动恢复，成功可一键回滚</span></article>
                </div>

                <div v-if="qwen38Quick.profile?.gpu_groups?.groups?.length" class="qwen38-groups">
                  <span
                    v-for="(group, index) in qwen38Quick.profile.gpu_groups.groups"
                    :key="group.join('-')"
                  >实例 {{ index + 1 }}：GPU {{ group.join(' + ') }}</span>
                  <small>配对来源：{{ qwen38Quick.profile.gpu_groups.source === 'nvidia-smi' ? 'GPU 实际拓扑' : '按编号回退分组' }}</small>
                </div>

                <ul v-if="qwen38Quick.profile?.blockers?.length" class="deployment-warnings qwen38-blockers">
                  <li v-for="item in qwen38Quick.profile.blockers" :key="item">{{ item }}</li>
                </ul>
                <ul v-else-if="qwen38Quick.profile?.warnings?.length" class="deployment-warnings">
                  <li v-for="item in qwen38Quick.profile.warnings" :key="item">{{ item }}</li>
                </ul>

                <details class="qwen38-advanced">
                  <summary>高级设置（推荐保持默认）</summary>
                  <div class="form-grid qwen38-advanced-grid">
                    <label>最大上下文
                      <input v-model.number="qwen38Quick.form.max_model_len" type="number" min="262144" max="1000000" />
                      <small>推荐 262144；更大窗口会降低余量并启用 Static YaRN。</small>
                    </label>
                    <label>每实例并发序列
                      <input v-model.number="qwen38Quick.form.max_num_seqs" type="number" min="1" max="16" />
                      <small>推荐 8；不是 8 路请求都能同时占满 262K。</small>
                    </label>
                    <label>显存利用率
                      <input v-model.number="qwen38Quick.form.gpu_memory_utilization" type="number" min="0.7" max="0.96" step="0.01" />
                      <small>推荐 0.90，为图、状态和多模态工作区留余量。</small>
                    </label>
                    <label>每请求最大图片数
                      <input v-model.number="qwen38Quick.form.max_images_per_request" type="number" min="1" max="16" />
                      <small>推荐 4；图片越多、分辨率越高，占用的视觉 Token 和处理时间越多。</small>
                    </label>
                    <label>MTP 草稿 Token
                      <select v-model.number="qwen38Quick.form.mtp_speculative_tokens">
                        <option :value="0">0（推荐，先稳定运行）</option>
                        <option :value="1">1</option>
                        <option :value="2">2（验收后可尝试）</option>
                        <option :value="3">3</option>
                      </select>
                    </label>
                    <label>KV Cache 精度
                      <select v-model="qwen38Quick.form.kv_cache_dtype">
                        <option value="auto">auto / BF16（推荐）</option>
                        <option value="bfloat16">bfloat16</option>
                        <option value="fp8">fp8（需镜像支持）</option>
                        <option value="fp8_e4m3">fp8_e4m3（需镜像支持）</option>
                        <option value="nvfp4">nvfp4（实验，需镜像支持）</option>
                      </select>
                    </label>
                    <label class="qwen38-toggle">
                      <input v-model="qwen38Quick.form.enable_prefix_caching" type="checkbox" />
                      启用前缀缓存
                      <small>当前预览版默认关闭，避免已知混合缓存风险。</small>
                    </label>
                  </div>
                </details>

                <div v-if="qwen38Quick.job" class="qwen38-last-result">
                  <span class="status" :class="['failed', 'rolled_back'].includes(qwen38Quick.job.state) ? 'bad' : qwen38Quick.job.state === 'succeeded' ? 'ok' : 'warn'">
                    {{ deploymentJobStateLabel(qwen38Quick.job.state) }}
                  </span>
                  <strong>{{ qwen38Quick.job.message }}</strong>
                </div>

                <div class="qwen38-actions">
                  <button
                    type="button"
                    class="primary qwen38-deploy-button"
                    :disabled="qwen38Quick.busy || Boolean(activeModelDeploymentJob) || !qwen38Quick.profile?.available"
                    @click="qwen38Quick.deploy"
                  >{{ qwen38Quick.busy ? "正在检查并提交…" : qwen38Quick.profile?.active ? "应用新参数并重新部署" : "开始自动部署并上线" }}</button>
                  <button
                    v-if="qwen38Quick.rollbackJob"
                    type="button"
                    class="danger qwen38-rollback-button"
                    :disabled="Boolean(activeModelDeploymentJob)"
                    @click="qwen38Quick.rollback"
                  >恢复到部署前状态</button>
                  <small>{{ qwen38Quick.profile?.active ? "重新部署会复用现有权重和镜像，重新加载四个 Worker；失败自动恢复当前运行状态。" : "开始后无需停留在本页。下载和部署在服务器后台继续，失败会自动恢复。" }}</small>
                </div>
              </section>

              <section
                v-if="displayedModelUpgradeJob"
                id="model-deployment-active-job"
                class="panel deployment-active-job"
              >
                <div class="deployment-job-head">
                  <div>
                    <span
                      class="status"
                      :class="['failed', 'rolled_back'].includes(displayedModelUpgradeJob.state) ? 'bad' : displayedModelUpgradeJob.state === 'succeeded' ? 'ok' : 'warn'"
                    >{{ deploymentJobStateLabel(displayedModelUpgradeJob.state) }}</span>
                    <strong>{{ displayedModelUpgradeJob.message }}</strong>
                  </div>
                  <button
                    v-if="activeModelDeploymentJob && activeModelDeploymentJob.kind !== 'publish'"
                    type="button"
                    class="danger ghost"
                    @click="cancelModelDeployment(activeModelDeploymentJob)"
                  >安全取消</button>
                </div>
                <progress :value="displayedModelUpgradeJob.progress || 0" max="100"></progress>
                <small>
                  阶段 {{ displayedModelUpgradeJob.phase }} · {{ displayedModelUpgradeJob.progress || 0 }}%
                  · 任务 {{ displayedModelUpgradeJob.id }}
                </small>
                <ol v-if="displayedModelUpgradeJob.logs?.length" class="deployment-log">
                  <li v-for="entry in displayedModelUpgradeJob.logs.slice(-6)" :key="`${entry.time}-${entry.message}`">
                    <time>{{ date(entry.time) }}</time><span>{{ entry.message }}</span>
                  </li>
                </ol>
                <button
                  v-if="displayedModelUpgradeJob.kind === 'upgrade' && ['failed', 'rolled_back'].includes(displayedModelUpgradeJob.state) && Number(displayedModelUpgradeJob.progress || 0) >= 92"
                  type="button"
                  class="primary"
                  :disabled="Boolean(activeModelDeploymentJob)"
                  @click="retryModelDeploymentPublish"
                >仅重试 AI 接入层发布（不重启 Worker）</button>
              </section>

              <details class="panel qwen38-other-tools">
                <summary>其他模型与高级部署工具</summary>
                <div class="qwen38-other-tools-body">
              <section class="panel deployment-form-section">
                <div class="section-title-row">
                  <div>
                    <h2>Ornith 版本升级</h2>
                    <p>
                      保留现有公开模型 ID 和旧权重，先解析固定 SHA 与目标拓扑，再在公开切换前执行真实生成。
                    </p>
                  </div>
                </div>
                <details class="panel deployment-advanced">
                  <summary>下载环境与维护代理</summary>
                  <p class="deployment-gateway-note">
                    ModelScope 下载器：
                    {{ modelDeployments?.download_environment?.modelscope?.downloader_ready ? "已就绪" : "缺失时将在任务中自动准备" }}；
                    维护代理只用于模型目录、依赖和权重下载，不会注入 Router 或 Worker。
                  </p>
                  <div class="form-grid deployment-source-grid">
                    <label>
                      代理地址
                      <input
                        v-model.trim="modelDownloadProxyForm.proxy_url"
                        placeholder="例如 http://127.0.0.1:7890"
                      />
                    </label>
                    <label>
                      测试目标
                      <select v-model="modelDownloadProxyForm.hub">
                        <option value="huggingface">Hugging Face</option>
                        <option value="modelscope">ModelScope</option>
                      </select>
                    </label>
                    <label>
                      NO_PROXY
                      <input v-model.trim="modelDownloadProxyForm.no_proxy" />
                    </label>
                  </div>
                  <div class="button-row">
                    <button type="button" class="ghost" :disabled="modelDownloadProxyBusy" @click="testModelDownloadProxy">
                      {{ modelDownloadProxyBusy ? "正在测试…" : "仅测试" }}
                    </button>
                    <button type="button" class="primary" :disabled="modelDownloadProxyBusy" @click="saveModelDownloadProxy">
                      测试并保存
                    </button>
                    <button type="button" class="danger ghost" :disabled="modelDownloadProxyBusy" @click="clearModelDownloadProxy">
                      清除代理
                    </button>
                    <small>{{ modelDownloadProxyMessage || (modelDeployments?.download_environment?.maintenance_proxy?.configured ? "已保存维护代理" : "当前未保存维护代理") }}</small>
                  </div>
                </details>
                <p v-if="!modelDeployments?.gateway?.registry_publish" class="warning">
                  当前接入层不能原子同步版本切换。升级入口保持关闭，避免 Worker 已变化但公开路由仍指向旧配置。
                </p>
                <p v-else-if="modelUpgradeUnavailableReason" class="warning">
                  {{ modelUpgradeUnavailableReason }}
                </p>
                <p v-else-if="!ornithUpgradeSources.length" class="empty-state">
                  当前没有已启用且包含本机 Worker 的 Ornith 部署。
                </p>
                <template v-else>
                  <div class="form-grid deployment-source-grid">
                    <label>
                      当前 Ornith 部署
                      <select v-model="modelUpgradeForm.source_deployment_id">
                        <option value="">请选择部署</option>
                        <option
                          v-for="deployment in ornithUpgradeSources"
                          :key="deployment.id"
                          :value="deployment.id"
                        >
                          {{ deployment.display_name || deployment.id }} · {{ deployment.model_id }}
                        </option>
                      </select>
                    </label>
                    <label>
                      升级目标
                      <select v-model="modelUpgradeForm.target_profile_id">
                        <optgroup
                          v-for="group in modelUpgradeProfileGroups"
                          :key="group.hub"
                          :label="group.label"
                        >
                          <option
                            v-for="profile in group.profiles"
                            :key="profile.id"
                            :value="profile.id"
                          >
                            {{ profile.model_id }}
                          </option>
                        </optgroup>
                      </select>
                      <small>
                        {{ modelUpgradeForm.target_hub === "modelscope" ? "ModelScope" : "Hugging Face" }}
                        · {{ modelUpgradeForm.target_model_id }}
                      </small>
                    </label>
                    <label>
                      固定 revision（可留空）
                      <input
                        v-model.trim="modelUpgradeForm.target_revision"
                        placeholder="留空时计划阶段解析当前完整 SHA"
                      />
                    </label>
                    <label>
                      目标最大上下文
                      <input
                        v-model.number="modelUpgradeForm.max_model_len"
                        type="number"
                        min="8192"
                        max="262144"
                      />
                    </label>
                  </div>
                  <div class="button-row">
                    <button
                      type="button"
                      class="primary"
                      :disabled="modelUpgradePlanning || Boolean(activeModelDeploymentJob)"
                      @click="planModelUpgrade"
                    >
                      {{ modelUpgradePlanning ? "正在解析模型与硬件…" : "生成版本升级计划" }}
                    </button>
                    <small>此步骤只读，不下载权重、不停止 Worker。</small>
                  </div>
                  <section v-if="modelUpgradePlan" class="panel deployment-plan-card">
                    <h3>升级与回退计划</h3>
                    <dl class="deployment-plan-facts">
                      <div>
                        <dt>当前版本</dt>
                        <dd>{{ modelUpgradePlan.upgrade.current_model_id }}@{{ modelUpgradePlan.upgrade.current_revision }}</dd>
                      </div>
                      <div>
                        <dt>目标版本</dt>
                        <dd>
                          {{ modelUpgradePlan.upgrade.target_hub === "modelscope" ? "ModelScope" : "Hugging Face" }}
                          · {{ modelUpgradePlan.upgrade.target_model_id }}
                        </dd>
                      </div>
                      <div>
                        <dt>兼容内部名</dt>
                        <dd>
                          {{ modelUpgradePlan.upgrade.compatible_served_model_aliases?.join(", ") || "无" }}
                        </dd>
                      </div>
                      <div>
                        <dt>固定 SHA</dt>
                        <dd><code>{{ modelUpgradePlan.upgrade.target_revision }}</code></dd>
                      </div>
                      <div>
                        <dt>目标拓扑</dt>
                        <dd>
                          TP{{ modelUpgradePlan.upgrade.target_tp_size }} ·
                          {{ modelUpgradePlan.upgrade.target_instance_count }} 个实例
                        </dd>
                      </div>
                      <div>
                        <dt>受影响 Worker</dt>
                        <dd>{{ modelUpgradePlan.affected_worker_ids?.join(", ") }}</dd>
                      </div>
                      <div>
                        <dt>旧权重</dt>
                        <dd>保留于 {{ modelUpgradePlan.upgrade.current_artifact_path }}</dd>
                      </div>
                    </dl>
                    <ul class="deployment-warnings">
                      <li v-for="warning in modelUpgradePlan.warnings" :key="warning">
                        {{ warning }}
                      </li>
                    </ul>
                    <label class="deployment-confirm">
                      <input v-model="modelUpgradeConfirmed" type="checkbox" />
                      我已安排维护窗口，并确认回退需要重新加载旧模型权重。
                    </label>
                    <button
                      type="button"
                      class="primary"
                      :disabled="!modelUpgradeConfirmed || modelUpgradeSubmitting || Boolean(activeModelDeploymentJob)"
                      @click="submitModelUpgrade"
                    >
                      {{ modelUpgradeSubmitting ? "正在提交…" : "确认升级并保留回退点" }}
                    </button>
                    <p v-if="modelUpgradeSubmitError" class="error-text" role="alert">
                      {{ modelUpgradeSubmitError }}
                    </p>
                  </section>
                </template>
              </section>

              <section v-if="modelDeploymentRows.some((item) => item.id === 'legacy')" class="panel deployment-migration-note">
                <strong>8 卡单模型拆分建议</strong>
                <ol>
                  <li>先新建 Qwen 部署并选择 GPU 4–7，发布为 <code>gdn-inside-qwen</code>。</li>
                  <li>待 Qwen 验收成功后，再编辑旧 Ornith 部署，保留 GPU 0–3，发布为 <code>gdn-inside-ornith</code>。</li>
                  <li>勾选“保留兼容别名”后，旧客户端仍可使用 <code>gdn-inside</code>。</li>
                </ol>
              </section>

              <div class="deployment-workspace">
                <div class="deployment-editor">
                  <section class="panel deployment-form-section">
                    <div class="section-title-row">
                      <div><h2>1. 模型来源</h2><p>可以下载新权重、复用本机目录或接入远程推理实例。</p></div>
                      <div class="segmented-control">
                        <button type="button" @click="applyQwen38FlashNextPreset">Qwen3.8 NVFP4 预设</button>
                        <button type="button" :class="{ active: modelDeploymentMode === 'local' }" @click="modelDeploymentMode = 'local'">本机 GPU</button>
                        <button type="button" :class="{ active: modelDeploymentMode === 'remote' }" @click="modelDeploymentMode = 'remote'">远程实例</button>
                      </div>
                    </div>
                    <div class="form-grid deployment-source-grid">
                      <label>部署 ID<input v-model.trim="modelDeploymentForm.deployment_id" placeholder="例如 qwen-35b" /></label>
                      <label>来源
                        <select v-model="modelDeploymentForm.hub">
                          <option value="huggingface">Hugging Face</option>
                          <option value="modelscope">ModelScope</option>
                          <option value="local">本机已有目录</option>
                        </select>
                      </label>
                      <label>模型 ID<input v-model.trim="modelDeploymentForm.model_id" :placeholder="modelDeploymentForm.hub === 'local' ? '模型实际 ID' : '组织名/模型名'" /></label>
                      <label v-if="modelDeploymentForm.hub !== 'local'">Revision<input v-model.trim="modelDeploymentForm.revision" placeholder="main" /></label>
                      <label v-else>模型目录<input v-model.trim="modelDeploymentForm.artifact_path" placeholder="/data/llm-cluster/models/..." /></label>
                    </div>
                  </section>

                  <section class="panel deployment-form-section">
                    <div class="section-title-row"><div><h2>2. 公开模型</h2><p>外部调用只看到公开 ID，vLLM 服务名用于内部路由。</p></div></div>
                    <p class="deployment-gateway-note">
                      {{ modelDeployments?.gateway?.message || '正在读取 AI 接入层能力…' }}
                    </p>
                    <div class="form-grid">
                      <label>公开模型 ID<input v-model.trim="modelDeploymentForm.public_model_id" placeholder="gdn-inside-qwen" /></label>
                      <label>vLLM 服务模型名<input v-model.trim="modelDeploymentForm.served_model_name" placeholder="qwen-..." /></label>
                      <label>显示名称<input v-model.trim="modelDeploymentForm.display_name" placeholder="Qwen 内部模型" /></label>
                      <label>附加公开 ID<input v-model.trim="modelDeploymentForm.additional_public_ids" placeholder="多个 ID 用逗号分隔" /></label>
                    </div>
                    <div class="check-row">
                      <label><input v-model="modelDeploymentForm.publish_requested" type="checkbox" :disabled="!modelDeployments?.gateway?.registry_publish" />部署成功后同步到 AI 接入层</label>
                      <label><input v-model="modelDeploymentForm.preserve_legacy_alias" type="checkbox" />保留兼容别名 gdn-inside</label>
                    </div>
                  </section>

                  <section v-if="modelDeploymentMode === 'local'" class="panel deployment-form-section">
                    <div class="section-title-row">
                      <div><h2>3. GPU 与 Worker</h2><p>单卡实例使用 TP1；跨卡实例按张量并行数将 GPU 连续分组。</p></div>
                      <div class="compact-actions">
                        <button type="button" class="ghost" @click="setDeploymentGpuSelection('first')">前半组</button>
                        <button type="button" class="ghost" @click="setDeploymentGpuSelection('second')">后半组</button>
                        <button type="button" class="ghost" @click="setDeploymentGpuSelection('all')">全部</button>
                      </div>
                    </div>
                    <div class="deployment-gpu-grid">
                      <button
                        v-for="gpu in modelDeployments?.gpus || []"
                        :key="gpu.id"
                        type="button"
                        class="deployment-gpu-card"
                        :class="{
                          selected: selectedDeploymentGpus.has(Number(gpu.id)),
                          assigned: assignedDeploymentGpus[String(gpu.id)],
                        }"
                        @click="toggleDeploymentGpu(gpu.id)"
                      >
                        <span>GPU {{ gpu.id }}</span><strong>{{ gpu.name }}</strong>
                        <small>{{ Number(gpu.memory_mib).toLocaleString() }} MiB</small>
                        <em v-if="assignedDeploymentGpus[String(gpu.id)]">当前：{{ assignedDeploymentGpus[String(gpu.id)] }}</em>
                        <em v-else>当前：未登记</em>
                      </button>
                    </div>
                    <div class="form-grid deployment-runtime-grid">
                      <label>首个 Worker ID<input v-model.number="modelDeploymentForm.worker_start_id" type="number" min="0" max="255" /></label>
                      <label>Worker 基础端口<input v-model.number="modelDeploymentForm.worker_base_port" type="number" min="1024" max="65000" /></label>
                      <label>张量并行数
                        <select v-model.number="modelDeploymentForm.tensor_parallel_size">
                          <option :value="1">TP1</option><option :value="2">TP2</option><option :value="4">TP4</option><option :value="8">TP8</option>
                        </select>
                      </label>
                      <label>vLLM 镜像<input v-model.trim="modelDeploymentForm.image" /></label>
                    </div>
                  </section>

                  <section v-else class="panel deployment-form-section">
                    <div class="section-title-row">
                      <div><h2>3. 远程 Worker</h2><p>控制面只登记和健康检查远端，不在本机执行 Docker 或 systemd。</p></div>
                      <button type="button" class="ghost" @click="addModelRemoteTarget">增加实例</button>
                    </div>
                    <div v-for="(target, index) in modelRemoteTargets" :key="index" class="remote-target-row">
                      <input v-model.trim="target.id" aria-label="远程实例 ID" placeholder="remote-qwen-0" />
                      <input v-model.trim="target.base_url" aria-label="远程实例地址" placeholder="http://10.0.0.20:8100/v1" />
                      <input v-model.trim="target.api_key_env" aria-label="密钥环境变量" placeholder="BACKEND_API_KEY" />
                      <label><input v-model="target.enabled" type="checkbox" />启用</label>
                      <button type="button" class="danger ghost" @click="removeModelRemoteTarget(index)">移除</button>
                    </div>
                  </section>

                  <details class="panel deployment-form-section deployment-advanced" open>
                    <summary>4. vLLM 参数与模型能力</summary>
                    <div class="form-grid deployment-runtime-grid">
                      <label>最大上下文<input v-model.number="modelDeploymentForm.max_model_len" type="number" min="1024" /></label>
                      <label>显存利用率<input v-model.number="modelDeploymentForm.gpu_memory_utilization" type="number" min="0.1" max="0.99" step="0.01" /></label>
                      <label>最大并发序列<input v-model.number="modelDeploymentForm.max_num_seqs" type="number" min="1" /></label>
                      <label>批处理 Token 上限<input v-model.number="modelDeploymentForm.max_num_batched_tokens" type="number" min="256" /></label>
                      <label>MTP 草稿 Token<input v-model.number="modelDeploymentForm.mtp_speculative_tokens" type="number" min="0" max="8" /></label>
                      <label>KV Cache 精度
                        <select v-model="modelDeploymentForm.kv_cache_dtype">
                          <option value="auto">auto / BF16</option>
                          <option value="bfloat16">bfloat16</option>
                          <option value="fp8">fp8</option>
                          <option value="fp8_e4m3">fp8_e4m3</option>
                          <option value="nvfp4">nvfp4（需镜像能力）</option>
                        </select>
                      </label>
                      <label>Static YaRN
                        <select v-model.number="modelDeploymentForm.yarn_factor">
                          <option :value="1">关闭（原生上下文）</option>
                          <option :value="2">2×</option>
                          <option :value="4">4×</option>
                        </select>
                      </label>
                      <label>工具解析器<input v-model.trim="modelDeploymentForm.tool_call_parser" placeholder="可留空" /></label>
                      <label>思考解析器<input v-model.trim="modelDeploymentForm.reasoning_parser" placeholder="可留空" /></label>
                      <label>多模态限制<input v-model.trim="modelDeploymentForm.mm_limit" placeholder='{"image":4}' /></label>
                    </div>
                    <div class="check-row deployment-capabilities">
                      <label><input v-model="modelDeploymentForm.trust_remote_code" type="checkbox" />信任模型仓库代码</label>
                      <label><input v-model="modelDeploymentForm.supports_image_input" type="checkbox" />图片输入</label>
                      <label><input v-model="modelDeploymentForm.supports_ocr" type="checkbox" />OCR</label>
                      <label><input v-model="modelDeploymentForm.supports_tool_calling" type="checkbox" />工具调用</label>
                      <label><input v-model="modelDeploymentForm.supports_reasoning" type="checkbox" />思考</label>
                      <label><input v-model="modelDeploymentForm.supports_thinking_toggle" type="checkbox" />可关闭思考</label>
                      <label><input v-model="modelDeploymentForm.ple_cpu_offload" type="checkbox" />PLE 放入主内存</label>
                      <label><input v-model="modelDeploymentForm.enable_expert_parallel" type="checkbox" />专家并行（EP）</label>
                      <label><input v-model="modelDeploymentForm.enable_prefix_caching" type="checkbox" />前缀缓存</label>
                      <label><input v-model="modelDeploymentForm.enable_flashinfer_autotune" type="checkbox" />FlashInfer 自动调优</label>
                      <label><input v-model="modelDeploymentForm.disable_custom_all_reduce" type="checkbox" />关闭 vLLM 自定义 AllReduce</label>
                    </div>
                    <p class="deployment-gateway-note">Qwen3.8 当前建议：PLE+EP 开启，KV=auto/BF16，YaRN=1，MTP=0，前缀缓存关闭。MTP2 只在无 MTP 基线通过后 A/B；NVFP4 QSA KV 会先检查镜像能力。</p>
                  </details>

                  <section class="panel deployment-submit-panel">
                    <button type="button" class="primary" :disabled="modelDeploymentPlanning || Boolean(activeModelDeploymentJob)" @click="planModelDeployment">
                      {{ modelDeploymentPlanning ? "正在校验…" : "生成部署计划" }}
                    </button>
                    <p>生成计划不会停止服务、下载模型或修改配置。</p>
                  </section>
                </div>

                <aside class="deployment-side">
                  <section class="panel deployment-plan-card">
                    <h2>部署计划</h2>
                    <p v-if="!modelDeploymentPlan" class="empty-state">填写配置后先生成计划。执行按钮在计划确认前始终不可用。</p>
                    <template v-else>
                      <dl class="deployment-plan-facts">
                        <div><dt>受影响 Worker</dt><dd>{{ modelDeploymentPlan.affected_worker_ids?.join(', ') || '无' }}</dd></div>
                        <div><dt>目标 GPU</dt><dd>{{ modelDeploymentPlan.requested_gpu_ids?.join(', ') || '远程' }}</dd></div>
                        <div><dt>公开 ID</dt><dd>{{ modelDeploymentPlan.public_model_ids?.join(', ') }}</dd></div>
                        <div><dt>权重下载</dt><dd>{{ modelDeploymentPlan.download_required ? '需要' : '跳过，复用已有目录' }}</dd></div>
                      </dl>
                      <ul v-if="modelDeploymentPlan.warnings?.length" class="deployment-warnings">
                        <li v-for="warning in modelDeploymentPlan.warnings" :key="warning">{{ warning }}</li>
                      </ul>
                      <label class="deployment-confirm">
                        <input v-model="modelDeploymentConfirmed" type="checkbox" />
                        我已核对 GPU、Worker、公开 ID；理解受影响实例将短暂停止，失败会自动回滚。
                      </label>
                      <button type="button" class="primary" :disabled="!modelDeploymentConfirmed || modelDeploymentSubmitting || Boolean(activeModelDeploymentJob)" @click="submitModelDeployment">
                        {{ modelDeploymentSubmitting ? "正在提交…" : "确认并后台部署" }}
                      </button>
                    </template>
                  </section>

                  <section class="panel deployment-current-card">
                    <h2>当前部署</h2>
                    <article v-for="deployment in modelDeploymentRows" :key="deployment.id" class="deployment-current-row">
                      <div><strong>{{ deployment.display_name || deployment.id }}</strong><code>{{ deployment.id }}</code></div>
                      <span class="status" :class="deployment.enabled ? 'ok' : 'bad'">{{ deployment.enabled ? '已启用' : '已停用' }}</span>
                      <small>公开 ID：{{ deployment.public_model_ids?.join(', ') || '未发布' }}</small>
                      <small>实例：{{ (deployment.instances || []).filter((item) => item.enabled).length }} 个</small>
                      <button type="button" class="ghost compact" :disabled="Boolean(activeModelDeploymentJob)" @click="prepareExistingDeployment(deployment)">
                        {{ deployment.id === 'legacy' ? '拆分并改名' : '编辑部署' }}
                      </button>
                    </article>
                    <p v-if="!modelDeploymentRows.length" class="empty-state">尚无注册部署；升级旧环境后会自动生成兼容记录。</p>
                  </section>

                  <section class="panel deployment-history-card">
                    <h2>最近任务</h2>
                    <article v-for="job in modelDeploymentJobs.slice(0, 8)" :key="job.id" class="deployment-history-row">
                      <span class="status" :class="job.state === 'succeeded' ? 'ok' : ['failed', 'rolled_back'].includes(job.state) ? 'bad' : 'warn'">{{ deploymentJobStateLabel(job.state) }}</span>
                      <strong>{{ job.kind === 'rollback' ? '配置回滚' : job.kind === 'upgrade' ? 'Ornith 版本升级' : job.kind === 'publish' ? 'AI 接入层路由发布' : (job.request?.deployment?.display_name || job.request?.deployment?.id || job.id) }}</strong>
                      <small>{{ job.message }}</small><time>{{ date(job.updated_at || job.created_at) }}</time>
                      <button
                        v-if="job.kind !== 'rollback' && job.state === 'succeeded' && job.backup"
                        type="button"
                        class="danger ghost compact"
                        :disabled="Boolean(activeModelDeploymentJob)"
                        @click="rollbackModelDeployment(job)"
                      >{{ job.kind === 'upgrade' ? '回退到升级前' : '回滚到部署前' }}</button>
                    </article>
                    <p v-if="!modelDeploymentJobs.length" class="empty-state">暂无部署任务。</p>
                  </section>
                </aside>
              </div>
                </div>
              </details>
            </template>
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
                <p>
                  注册用户需先完成邮箱验证；系统随后自动创建 API Key
                  并同步模型权限。也可在这里管理分组、余额和调用限制。
                </p>
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
              :status-options="[
                { value: 'pending', label: '邮箱未验证' },
                { value: 'active', label: '正常' },
                { value: 'disabled', label: '已停用' },
              ]"
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
                      <div class="admin-user-key">
                        <span>API Key</span>
                        <em v-if="!user.api_key_id">{{
                          user.status === "pending"
                            ? "验证邮箱后自动创建"
                            : "尚未创建"
                        }}</em>
                        <template v-else>
                          <code>{{
                            adminUserApiKeys[user.id] || "••••••••••••••••"
                          }}</code>
                          <button
                            type="button"
                            class="ghost admin-key-action"
                            :disabled="adminUserApiKeyLoading[user.id]"
                            :aria-label="`${adminUserApiKeys[user.id] ? '隐藏' : '显示'} ${user.email} 的 API Key`"
                            @click="
                              adminUserApiKeys[user.id]
                                ? hideAdminUserApiKey(user.id)
                                : revealAdminUserApiKey(user)
                            "
                          >
                            {{
                              adminUserApiKeyLoading[user.id]
                                ? "读取中…"
                                : adminUserApiKeys[user.id]
                                  ? "隐藏"
                                  : "显示"
                            }}
                          </button>
                          <button
                            v-if="adminUserApiKeys[user.id]"
                            type="button"
                            class="ghost admin-key-action"
                            :aria-label="`复制 ${user.email} 的 API Key`"
                            @click="copyAdminUserApiKey(user)"
                          >
                            复制
                          </button>
                        </template>
                      </div>
                    </td>
                    <td>
                      <span
                        class="status"
                        :class="
                          user.status === 'active'
                            ? 'ok'
                            : user.status === 'pending'
                              ? 'warn'
                              : 'bad'
                        "
                        >{{
                          user.status === "pending"
                            ? "邮箱未验证"
                            : statusLabel(user.status)
                        }}</span
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
                          !user.api_key_id && user.status === "pending"
                            ? "等待邮箱验证"
                            : user.permission_status === "pending" ||
                                !user.permission_status
                              ? "等待权限同步"
                              : statusLabel(user.permission_status)
                        }}</span
                      ><small v-if="!user.api_key_id && user.status === 'pending'">
                        验证后自动创建 Key 并同步权限
                      </small>
                      <small v-else-if="user.permission_error">{{
                        user.permission_error
                      }}</small>
                    </td>
                    <td class="user-row-actions-cell">
                      <div class="user-row-actions">
                        <template v-if="user.status === 'pending'">
                          <button
                            type="button"
                            class="primary"
                            :disabled="busy"
                            @click="openPendingUserApproval(user)"
                          >
                            手动通过验证
                          </button>
                          <button
                            type="button"
                            class="ghost"
                            :disabled="busy"
                            @click="resendUserVerification(user)"
                          >
                            {{
                              operation ===
                              `user-verification-resend:${user.id}`
                                ? "发送中…"
                                : "补发验证邮件"
                            }}
                          </button>
                          <button
                            type="button"
                            class="danger"
                            :disabled="busy"
                            @click="openPendingUserDelete(user)"
                          >
                            删除未验证用户
                          </button>
                        </template>
                        <button v-else class="ghost" @click="editUser(user)">
                          管理
                        </button>
                      </div>
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
                                <small
                                  v-if="requestDetails[row.request_id].truncated"
                                >输入超过单次安全显示上限，当前已显示前 1,000,000 个字符。</small>
                              </div>
                              <p v-else class="muted">
                                该请求产生时未开启详细日志，或内容已过保留期。
                                详细日志启用后仅新请求可查看，历史内容无法补录。
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
                                <small
                                  v-if="requestDetails[row.request_id].response_truncated"
                                >输出超过单次安全显示上限，当前已显示前 1,000,000 个字符。</small>
                              </div>
                              <p v-else class="muted">
                                该请求产生时未保留最终响应，或内容已过保留期。
                                详细日志启用后仅新请求可查看，历史输出无法补录。
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
</template>

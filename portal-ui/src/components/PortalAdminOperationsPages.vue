<script>
import { onBeforeUnmount, watch } from "vue";
import { usePortalWorkspaceContext } from "../portalWorkspaceContext.js";
import { useOmniRouteMaintenance } from "../useOmniRouteMaintenance.js";
import ListFilterBar from "./ListFilterBar.vue";
import PaginationBar from "./PaginationBar.vue";

export default {
  name: "PortalAdminOperationsPages",
  components: { ListFilterBar, PaginationBar },
  setup() {
    const workspace = usePortalWorkspaceContext();
    const maintenance = useOmniRouteMaintenance({
      api: workspace.api,
      notify: workspace.notify,
    });
    const stopSectionWatch = watch(
      () => workspace.section.value,
      (section) => {
        if (section === "omniroute") maintenance.loadOmniRouteMaintenance();
        else maintenance.stopOmniRoutePolling();
      },
      { immediate: true },
    );
    onBeforeUnmount(() => {
      stopSectionWatch();
      maintenance.stopOmniRoutePolling();
    });
    return { ...workspace, ...maintenance };
  },
};
</script>

<template>
          <section v-if="section === 'omniroute'" class="page omniroute-maintenance-page">
            <div class="page-head">
              <div>
                <span class="eyebrow">ROUTER LIFECYCLE & SQLITE</span>
                <h1>OmniRoute 维护</h1>
                <p>
                  评估并维护 OmniRoute 自身的 SQLite，或在自动备份和失败回滚保护下升级 Router。
                </p>
              </div>
              <div class="button-row">
                <button
                  class="ghost"
                  type="button"
                  :disabled="omnirouteLoading || omnirouteActionLoading"
                  @click="loadOmniRouteMaintenance"
                >
                  {{ omnirouteLoading ? "读取中…" : "刷新状态" }}
                </button>
                <button
                  class="primary"
                  type="button"
                  :disabled="omnirouteActionLoading || omnirouteJobActive"
                  @click="assessOmniRouteSqlite(false)"
                >
                  立即评估
                </button>
              </div>
            </div>

            <div v-if="!omnirouteMaintenance && omnirouteLoading" class="panel">
              正在读取 OmniRoute 镜像、SQLite、备份和任务状态…
            </div>

            <template v-if="omnirouteMaintenance">
              <div class="metrics omniroute-summary">
                <article>
                  <span>配置镜像</span>
                  <strong>{{ omnirouteMaintenance.configured_image || "未知" }}</strong>
                  <small>推荐：{{ omnirouteMaintenance.recommended_image }}</small>
                </article>
                <article>
                  <span>实际运行镜像</span>
                  <strong>{{ omnirouteMaintenance.running_image || "未运行" }}</strong>
                  <small class="mono">{{ omnirouteMaintenance.running_image_id || "未读取镜像 ID" }}</small>
                </article>
                <article>
                  <span>SQLite 主库</span>
                  <strong>{{ formatBytes(omnirouteMaintenance.database?.size || 0) }}</strong>
                  <small>
                    WAL {{ formatBytes(omnirouteMaintenance.database?.wal_size || 0) }} ·
                    SHM {{ formatBytes(omnirouteMaintenance.database?.shm_size || 0) }}
                  </small>
                </article>
                <article>
                  <span>可校验备份</span>
                  <strong>{{ omnirouteMaintenance.backups?.length || 0 }}</strong>
                  <small>升级、维护和回滚前均自动新增，不自动删除</small>
                </article>
              </div>
              <div v-if="omnirouteMaintenance.image_drift" class="warning">
                配置镜像与实际运行镜像不一致。为保证回退点可复现，升级会被拒绝；
                请先在维护窗口执行 <code>llmctl router restart</code>，再重新评估。
              </div>

              <section v-if="omnirouteJob" class="panel omniroute-job-panel">
                <div class="omniroute-job-head">
                  <div>
                    <span
                      class="status"
                      :class="
                        omnirouteJob.state === 'succeeded'
                          ? 'ok'
                          : ['failed', 'rolled_back'].includes(omnirouteJob.state)
                            ? 'bad'
                            : 'warn'
                      "
                    >{{ deploymentJobStateLabel(omnirouteJob.state) }}</span>
                    <strong>{{ omnirouteJob.message }}</strong>
                  </div>
                  <button
                    v-if="omnirouteJobActive"
                    type="button"
                    class="danger"
                    @click="cancelOmniRouteJob"
                  >
                    安全取消
                  </button>
                </div>
                <progress :value="Number(omnirouteJob.progress || 0)" max="100"></progress>
                <p>
                  阶段 {{ omnirouteJob.phase }} · {{ omnirouteJob.progress || 0 }}% ·
                  任务 <code>{{ omnirouteJob.id }}</code>
                </p>
                <div v-if="omnirouteJob.logs?.length" class="deployment-log">
                  <p v-for="(entry, index) in omnirouteJob.logs.slice(-8)" :key="index">
                    <time>{{ entry.time }}</time>{{ entry.message }}
                  </p>
                </div>
              </section>

              <section class="panel omniroute-assessment-panel">
                <div class="panel-head">
                  <div>
                    <h2>SQLite 可靠性评估</h2>
                    <p>
                      日常评估使用 quick_check；深度评估读取全库，适合升级或维护窗口前。
                    </p>
                  </div>
                  <button
                    type="button"
                    class="ghost"
                    :disabled="omnirouteActionLoading || omnirouteJobActive"
                    @click="assessOmniRouteSqlite(true)"
                  >
                    深度完整性检查
                  </button>
                </div>
                <div v-if="omnirouteAssessment" class="omniroute-assessment-grid">
                  <dl>
                    <dt>总体状态</dt>
                    <dd>
                      <span
                        class="status"
                        :class="
                          omnirouteAssessment.health === 'healthy'
                            ? 'ok'
                            : omnirouteAssessment.health === 'critical'
                              ? 'bad'
                              : 'warn'
                        "
                      >{{
                        omnirouteAssessment.health === "healthy"
                          ? "健康"
                          : omnirouteAssessment.health === "critical"
                            ? "严重异常"
                            : "需要维护"
                      }}</span>
                    </dd>
                  </dl>
                  <dl>
                    <dt>完整性 / 外键</dt>
                    <dd>
                      {{ omnirouteAssessment.integrity?.ok ? "通过" : "失败" }} /
                      {{ omnirouteAssessment.foreign_keys?.ok ? "通过" : "失败" }}
                    </dd>
                  </dl>
                  <dl>
                    <dt>Journal / 空闲页</dt>
                    <dd>
                      {{ String(omnirouteAssessment.sqlite?.journal_mode || "").toUpperCase() }} /
                      {{ (Number(omnirouteAssessment.sqlite?.free_ratio || 0) * 100).toFixed(1) }}%
                    </dd>
                  </dl>
                  <dl>
                    <dt>磁盘可用</dt>
                    <dd>{{ formatBytes(omnirouteAssessment.storage?.disk_free || 0) }}</dd>
                  </dl>
                </div>
                <div v-else class="empty">
                  尚未评估。点击“立即评估”读取完整性、WAL、空间和备份准备度。
                </div>
                <div
                  v-if="omnirouteAssessment?.recommendations?.length"
                  class="omniroute-recommendations"
                >
                  <article
                    v-for="item in omnirouteAssessment.recommendations"
                    :key="item.code"
                    :class="item.severity"
                  >
                    <strong>{{ item.severity === "critical" ? "必须处理" : "维护建议" }}</strong>
                    <p>{{ item.message }}</p>
                  </article>
                </div>
              </section>

              <div class="omniroute-action-grid">
                <section class="panel">
                  <h2>备份与在线维护</h2>
                  <p>
                    在线维护先备份，再执行 <code>PRAGMA optimize</code> 与
                    <code>PASSIVE checkpoint</code>；不会停止 Router 或 GPU Worker。
                  </p>
                  <button
                    type="button"
                    class="ghost wide-button"
                    :disabled="omnirouteActionLoading || omnirouteJobActive"
                    @click="backupOmniRouteSqlite"
                  >
                    仅创建可校验备份
                  </button>
                  <label>
                    输入 <code>MAINTAIN ONLINE</code> 确认在线写入维护
                    <input
                      v-model="omnirouteForm.online_confirmation"
                      autocomplete="off"
                      placeholder="MAINTAIN ONLINE"
                    />
                  </label>
                  <button
                    type="button"
                    class="primary wide-button"
                    :disabled="
                      omnirouteActionLoading ||
                      omnirouteJobActive ||
                      omnirouteForm.online_confirmation.trim() !== 'MAINTAIN ONLINE'
                    "
                    @click="maintainOmniRouteOnline"
                  >
                    备份并在线维护
                  </button>
                </section>

                <section class="panel">
                  <h2>维护窗口压缩</h2>
                  <p>
                    备份后短暂停止 Router，执行 WAL 截断、VACUUM 和完整性检查；
                    GPU Worker 保持运行，失败时自动恢复数据库。
                  </p>
                  <label>
                    输入 <code>COMPACT SQLITE</code> 确认短暂中断 /v1
                    <input
                      v-model="omnirouteForm.compact_confirmation"
                      autocomplete="off"
                      placeholder="COMPACT SQLITE"
                    />
                  </label>
                  <button
                    type="button"
                    class="danger wide-button"
                    :disabled="
                      omnirouteActionLoading ||
                      omnirouteJobActive ||
                      omnirouteForm.compact_confirmation.trim() !== 'COMPACT SQLITE'
                    "
                    @click="compactOmniRouteSqlite"
                  >
                    备份并压缩 SQLite
                  </button>
                </section>

                <section class="panel">
                  <h2>升级 OmniRoute</h2>
                  <p>
                    拉取固定版本，深度评估并备份后切换；新版本或完整冒烟失败时，
                    自动恢复原镜像和升级前 SQLite。
                  </p>
                  <label>
                    固定版本镜像
                    <input v-model.trim="omnirouteForm.update_image" autocomplete="off" />
                  </label>
                  <label>
                    输入 <code>UPDATE OMNIROUTE</code> 确认短暂中断 /v1
                    <input
                      v-model="omnirouteForm.update_confirmation"
                      autocomplete="off"
                      placeholder="UPDATE OMNIROUTE"
                    />
                  </label>
                  <button
                    type="button"
                    class="primary wide-button"
                    :disabled="
                      omnirouteActionLoading ||
                      omnirouteJobActive ||
                      !omnirouteForm.update_image.trim() ||
                      omnirouteForm.update_confirmation.trim() !== 'UPDATE OMNIROUTE'
                    "
                    @click="updateOmniRoute"
                  >
                    备份、升级并自动验收
                  </button>
                </section>
              </div>

              <section class="panel omniroute-backups-panel">
                <div class="panel-head">
                  <div>
                    <h2>可恢复备份</h2>
                    <p>每个备份都包含 SHA256、SQLite quick_check、原镜像和文件权限。</p>
                  </div>
                </div>
                <div class="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>时间</th>
                        <th>用途</th>
                        <th>数据库</th>
                        <th>来源镜像</th>
                        <th>备份 ID</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="backup in omnirouteMaintenance.backups" :key="backup.id">
                        <td>{{ date(backup.created_at) }}</td>
                        <td>{{ backup.purpose }}</td>
                        <td>{{ formatBytes(backup.database_size) }}</td>
                        <td><code>{{ backup.source_image }}</code></td>
                        <td>
                          <code>{{ backup.id }}</code>
                          <small>SHA256 {{ backup.database_sha256.slice(0, 16) }}…</small>
                        </td>
                        <td>
                          <button
                            type="button"
                            class="ghost"
                            :disabled="omnirouteJobActive"
                            @click="selectOmniRouteBackup(backup.id)"
                          >
                            选择回滚
                          </button>
                        </td>
                      </tr>
                      <tr v-if="!omnirouteMaintenance.backups?.length">
                        <td colspan="6" class="empty">尚无受管备份。</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <div v-if="omnirouteForm.rollback_id" class="omniroute-rollback-confirm">
                  <div class="warning">
                    回滚会短暂停止 Router，并恢复备份时的 OmniRoute 镜像与 SQLite；
                    操作前会再次备份当前状态。
                  </div>
                  <p>目标备份：<code>{{ omnirouteForm.rollback_id }}</code></p>
                  <label>
                    输入 <code>ROLLBACK {{ omnirouteForm.rollback_id }}</code> 确认
                    <input
                      v-model="omnirouteForm.rollback_confirmation"
                      autocomplete="off"
                      :placeholder="`ROLLBACK ${omnirouteForm.rollback_id}`"
                    />
                  </label>
                  <button
                    type="button"
                    class="danger"
                    :disabled="
                      omnirouteActionLoading ||
                      omnirouteJobActive ||
                      omnirouteForm.rollback_confirmation.trim() !==
                        `ROLLBACK ${omnirouteForm.rollback_id}`
                    "
                    @click="rollbackOmniRoute"
                  >
                    备份当前状态并执行回滚
                  </button>
                </div>
              </section>
            </template>
          </section>

          <section v-if="section === 'reports'" class="page usage-report-page">
            <div class="page-head">
              <div>
                <span class="eyebrow">USAGE REPORTS</span>
                <h1>全员用量报表</h1>
                <p>按日、月、年或自定义日期统计全部用户；零用量用户也会列入报表。</p>
              </div>
              <div class="report-head-actions">
                <button
                  class="ghost"
                  type="button"
                  :disabled="usageReportLoading"
                  @click="loadAdminUsageReport(usageReportFilters.page)"
                >
                  {{ usageReportLoading ? "统计中…" : "刷新统计" }}
                </button>
                <button
                  class="primary"
                  type="button"
                  :disabled="usageReportExporting || usageReportLoading"
                  @click="exportAdminUsageReport"
                >
                  {{ usageReportExporting ? "生成 Excel…" : "导出 Excel" }}
                </button>
              </div>
            </div>

            <section class="panel report-filter-panel">
              <div class="report-period-tabs" role="group" aria-label="统计周期">
                <button
                  v-for="option in [
                    ['day', '按天'],
                    ['month', '按月'],
                    ['year', '按年'],
                    ['custom', '自定义时间'],
                  ]"
                  :key="option[0]"
                  type="button"
                  :class="{ active: usageReportFilters.period === option[0] }"
                  @click="
                    usageReportFilters.period = option[0];
                    changeUsageReportFilters();
                  "
                >
                  {{ option[1] }}
                </button>
              </div>
              <div class="usage-report-filters">
                <label v-if="usageReportFilters.period === 'day'">
                  <span>统计日期</span>
                  <input v-model="usageReportFilters.anchor_day" type="date" />
                </label>
                <label v-else-if="usageReportFilters.period === 'month'">
                  <span>统计月份</span>
                  <input v-model="usageReportFilters.anchor_month" type="month" />
                </label>
                <label v-else-if="usageReportFilters.period === 'year'">
                  <span>统计年份</span>
                  <input
                    v-model="usageReportFilters.anchor_year"
                    type="number"
                    min="1970"
                    max="9998"
                    inputmode="numeric"
                  />
                </label>
                <template v-else>
                  <label>
                    <span>开始日期</span>
                    <input v-model="usageReportFilters.start_date" type="date" />
                  </label>
                  <label>
                    <span>结束日期</span>
                    <input v-model="usageReportFilters.end_date" type="date" />
                  </label>
                </template>
                <label>
                  <span>模型</span>
                  <select v-model="usageReportFilters.model">
                    <option value="">全部模型</option>
                    <option
                      v-for="model in admin.models"
                      :key="model.id"
                      :value="model.public_model_id"
                    >
                      {{ model.public_model_id }}
                    </option>
                  </select>
                </label>
                <label>
                  <span>用户</span>
                  <input
                    v-model.trim="usageReportFilters.user_query"
                    type="search"
                    placeholder="邮箱或登录名"
                    @keyup.enter="changeUsageReportFilters"
                  />
                </label>
                <label>
                  <span>账户状态</span>
                  <select v-model="usageReportFilters.status">
                    <option value="">全部状态</option>
                    <option value="active">正常</option>
                    <option value="pending">待验证</option>
                    <option value="disabled">已禁用</option>
                  </select>
                </label>
                <label>
                  <span>每页行数</span>
                  <select v-model.number="usageReportFilters.page_size">
                    <option :value="20">20</option>
                    <option :value="50">50</option>
                    <option :value="100">100</option>
                  </select>
                </label>
                <button
                  class="primary report-query-button"
                  type="button"
                  :disabled="usageReportLoading"
                  @click="changeUsageReportFilters"
                >
                  查询
                </button>
              </div>
              <p class="report-filter-note">
                页面分页与 Excel 导出使用同一组筛选条件；导出文件包含筛选范围内的全部用户，而不是仅导出当前页。
              </p>
            </section>

            <section v-if="usageReportLoading && !adminUsageReport" class="panel report-loading">
              正在汇总全员用量…
            </section>

            <template v-if="adminUsageReport">
              <div class="metrics usage-report-summary">
                <article class="panel">
                  <span>统计范围</span>
                  <strong>{{ adminUsageReport.range.label }}</strong>
                  <small>{{ adminUsageReport.timezone }}</small>
                </article>
                <article class="panel">
                  <span>全员 / 活跃</span>
                  <strong>
                    {{ formatTokens(adminUsageReport.summary.total_users) }} /
                    {{ formatTokens(adminUsageReport.summary.active_users) }}
                  </strong>
                  <small>{{ formatTokens(adminUsageReport.summary.inactive_users) }} 人零用量</small>
                </article>
                <article class="panel">
                  <span>请求数</span>
                  <strong>{{ formatTokens(adminUsageReport.summary.requests) }}</strong>
                  <small>平均 {{ formatTokens(adminUsageReport.summary.average_tokens_per_request) }} Token / 请求</small>
                </article>
                <article class="panel">
                  <span>Token 总量</span>
                  <strong>{{ formatTokens(adminUsageReport.summary.total_tokens) }}</strong>
                  <small>
                    输入 {{ formatTokens(adminUsageReport.summary.input_tokens) }} ·
                    输出 {{ formatTokens(adminUsageReport.summary.output_tokens) }}
                  </small>
                </article>
                <article class="panel">
                  <span>余额扣款</span>
                  <strong>{{ money(adminUsageReport.summary.amount_micros) }}</strong>
                  <small>按请求结算记录汇总</small>
                </article>
              </div>

              <section class="panel usage-trend-panel">
                <div class="panel-head analytics-panel-head">
                  <div>
                    <h2>用量趋势</h2>
                    <p>蓝色为输入 Token，绿色为输出 Token。</p>
                  </div>
                  <div class="chart-legend" aria-hidden="true">
                    <span><i class="input"></i>输入</span>
                    <span><i class="output"></i>输出</span>
                  </div>
                </div>
                <div class="usage-composition">
                  <span><b>{{ formatTokens(adminUsageReport.summary.input_tokens) }}</b>输入 Token</span>
                  <span><b>{{ formatTokens(adminUsageReport.summary.output_tokens) }}</b>输出 Token</span>
                  <span><b>{{ formatTokens(adminUsageReport.summary.cached_tokens) }}</b>缓存命中 Token</span>
                  <span><b>{{ formatTokens(adminUsageReport.summary.reasoning_tokens) }}</b>思考 Token</span>
                </div>
                <div class="usage-chart" role="img" :aria-label="`${adminUsageReport.range.label} Token 用量趋势`">
                  <div
                    v-for="(point, index) in adminUsageReport.timeseries"
                    :key="point.start_at"
                    class="chart-slot"
                    :title="`${point.label}：输入 ${formatTokens(point.input_tokens)}，输出 ${formatTokens(point.output_tokens)}，请求 ${formatTokens(point.requests)}`"
                  >
                    <span class="chart-value">{{ formatTokens(point.total_tokens) }}</span>
                    <span class="chart-column">
                      <i
                        class="chart-segment output"
                        :style="{ height: analyticsSegmentHeight(point.output_tokens, usageReportMaxTokens) }"
                      ></i>
                      <i
                        class="chart-segment input"
                        :style="{ height: analyticsSegmentHeight(point.input_tokens, usageReportMaxTokens) }"
                      ></i>
                    </span>
                    <small v-if="analyticsLabelVisible(index, adminUsageReport.timeseries.length)">{{ point.label }}</small>
                  </div>
                </div>
              </section>

              <section class="panel">
                <div class="panel-head">
                  <div>
                    <h2>模型用量</h2>
                    <p>仅显示当前时间和模型筛选范围内产生过请求的模型。</p>
                  </div>
                  <span class="filter-count">{{ adminUsageReport.models.length }} 个模型</span>
                </div>
                <div class="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>模型 ID</th><th>请求</th><th>活跃用户</th><th>输入 Token</th>
                        <th>输出 Token</th><th>Token 总量</th><th>余额扣款</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="row in adminUsageReport.models" :key="row.public_model_id">
                        <td><code>{{ row.public_model_id }}</code></td>
                        <td>{{ formatTokens(row.requests) }}</td>
                        <td>{{ formatTokens(row.active_users) }}</td>
                        <td>{{ formatTokens(row.input_tokens) }}</td>
                        <td>{{ formatTokens(row.output_tokens) }}</td>
                        <td><strong>{{ formatTokens(row.total_tokens) }}</strong></td>
                        <td>{{ money(row.amount_micros) }}</td>
                      </tr>
                      <tr v-if="!adminUsageReport.models.length">
                        <td colspan="7" class="empty">当前范围没有模型调用记录。</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </section>

              <section class="panel">
                <div class="panel-head">
                  <div>
                    <h2>全员使用情况</h2>
                    <p>按 Token 总量降序排列；未产生调用的用户仍会显示为零用量。</p>
                  </div>
                  <span class="filter-count">{{ adminUsageReport.pagination.total }} 位用户</span>
                </div>
                <div class="table-wrap usage-report-table">
                  <table>
                    <thead>
                      <tr>
                        <th>用户</th><th>状态</th><th>请求</th><th>输入 Token</th><th>输出 Token</th>
                        <th>Token 总量</th><th>余额扣款</th><th>当前余额</th><th>最后活跃</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr
                        v-for="row in adminUsageReport.users"
                        :key="row.user_id"
                        :class="{ 'zero-usage-row': !Number(row.requests || 0) }"
                      >
                        <td><strong>{{ row.email }}</strong><small v-if="row.login_name">{{ row.login_name }}</small></td>
                        <td><span class="status" :class="row.status === 'active' ? 'ok' : row.status === 'disabled' ? 'bad' : 'warn'">{{ statusLabel(row.status) }}</span></td>
                        <td>{{ formatTokens(row.requests) }}</td>
                        <td>{{ formatTokens(row.input_tokens) }}</td>
                        <td>{{ formatTokens(row.output_tokens) }}</td>
                        <td><strong>{{ formatTokens(row.total_tokens) }}</strong></td>
                        <td>{{ money(row.amount_micros) }}</td>
                        <td>{{ money(row.balance_micros) }}</td>
                        <td>{{ date(row.last_activity_at) }}</td>
                      </tr>
                      <tr v-if="!adminUsageReport.users.length">
                        <td colspan="9" class="empty">当前筛选条件下没有用户。</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <PaginationBar
                  :page="adminUsageReport.pagination.page"
                  :pages="adminUsageReport.pagination.pages"
                  :total="adminUsageReport.pagination.total"
                  @previous="loadAdminUsageReport(Math.max(1, adminUsageReport.pagination.page - 1))"
                  @next="loadAdminUsageReport(Math.min(adminUsageReport.pagination.pages, adminUsageReport.pagination.page + 1))"
                />
              </section>
            </template>
          </section>

          <section v-if="section === 'database'" class="page database-page">
            <div class="page-head">
              <div>
                <span class="eyebrow">PORTAL DATABASE</span>
                <h1>门户数据库</h1>
                <p>配置并迁移 LLMCtl 账户门户数据；AI 接入层、推理 Router 和 GPU Worker 不受影响。</p>
              </div>
              <button class="ghost" type="button" :disabled="databaseLoading" @click="loadDatabaseRuntime()">
                {{ databaseLoading ? "读取中…" : "刷新状态" }}
              </button>
            </div>

            <section v-if="!databaseRuntime" class="panel database-loading">
              {{ databaseLoading ? "正在读取数据库状态…" : "暂无数据库状态。" }}
            </section>

            <template v-else>
              <section class="database-summary">
                <article class="panel">
                  <span>当前后端</span>
                  <strong>{{ databaseRuntime.config.active_backend === "mysql" ? "MySQL" : "SQLite" }}</strong>
                  <small>{{ databaseRuntime.config.active_backend === "mysql" ? "门户业务读写已切换" : "默认本地数据库" }}</small>
                </article>
                <article class="panel">
                  <span>MySQL 能力</span>
                  <strong>{{ databaseRuntime.capability.enabled ? "已激活" : "未激活" }}</strong>
                  <small>{{ databaseRuntime.capability.driver || "尚未安装 PyMySQL 驱动" }}</small>
                </article>
                <article class="panel">
                  <span>SQLite 源库</span>
                  <strong>{{ formatBytes(databaseRuntime.sqlite.bytes || 0) }}</strong>
                  <small>{{ databaseRuntime.sqlite.exists ? "迁移后保留，可回滚" : "文件不存在" }}</small>
                </article>
                <article class="panel">
                  <span>迁移状态</span>
                  <strong>{{ databaseRuntime.migration.status === "idle" ? "未迁移" : databaseRuntime.migration.status }}</strong>
                  <small>{{ databaseRuntime.migration.stage || "未开始" }}</small>
                </article>
              </section>

              <section v-if="!databaseRuntime.capability.enabled" class="panel database-capability-note">
                <div>
                  <h2>先激活 MySQL 能力</h2>
                  <p>服务器只需执行一次下列命令。它仅安装门户所需的 Python 驱动并短暂重启账户门户，不安装 MySQL Server，也不重启 AI 接入层、Router 或任何 Worker。</p>
                </div>
                <code>llmctl database enable-mysql</code>
              </section>

              <template v-else>
                <div class="database-workspace">
                  <section class="panel database-config-card">
                    <div class="panel-head">
                      <div>
                        <h2>MySQL 连接</h2>
                        <p>请先自行创建 MySQL 8.0+ database 和专用用户，再在此保存和测试。</p>
                      </div>
                      <span class="status" :class="databaseRuntime.config.active_backend === 'mysql' ? 'ok' : 'warn'">
                        {{ databaseRuntime.config.active_backend === "mysql" ? "使用中" : "待迁移" }}
                      </span>
                    </div>
                    <div class="form-grid database-form">
                      <label>主机
                        <input v-model.trim="databaseConfig.host" autocomplete="off" placeholder="127.0.0.1" :disabled="databaseRuntime.busy || databaseRuntime.config.active_backend === 'mysql'" />
                      </label>
                      <label>端口
                        <input v-model.number="databaseConfig.port" type="number" min="1" max="65535" :disabled="databaseRuntime.busy || databaseRuntime.config.active_backend === 'mysql'" />
                      </label>
                      <label>Database
                        <input v-model.trim="databaseConfig.database" autocomplete="off" placeholder="llmctl_portal" :disabled="databaseRuntime.busy || databaseRuntime.config.active_backend === 'mysql'" />
                      </label>
                      <label>用户名
                        <input v-model.trim="databaseConfig.username" autocomplete="username" placeholder="llmctl" :disabled="databaseRuntime.busy || databaseRuntime.config.active_backend === 'mysql'" />
                      </label>
                      <label class="span-2">密码
                        <input v-model="databaseConfig.password" type="password" autocomplete="new-password" :placeholder="databaseRuntime.config.password_configured ? '已保存；留空保持不变' : '输入 MySQL 用户密码'" :disabled="databaseRuntime.busy || databaseRuntime.config.active_backend === 'mysql'" />
                      </label>
                      <label>TLS 模式
                        <select v-model="databaseConfig.tls_mode" :disabled="databaseRuntime.busy || databaseRuntime.config.active_backend === 'mysql'">
                          <option value="disabled">关闭（仅可信内网）</option>
                          <option value="preferred">优先 TLS</option>
                          <option value="required">必须 TLS</option>
                          <option value="verify_ca">TLS 并验证 CA</option>
                        </select>
                      </label>
                      <label>CA 文件（服务器路径）
                        <input v-model.trim="databaseConfig.ca_file" autocomplete="off" placeholder="/etc/ssl/certs/mysql-ca.pem" :disabled="databaseRuntime.busy || databaseRuntime.config.active_backend === 'mysql'" />
                      </label>
                    </div>
                    <div class="button-row database-actions">
                      <button class="primary" type="button" :disabled="busy || databaseRuntime.config.active_backend === 'mysql'" @click="saveDatabaseConfig">保存配置</button>
                      <button class="ghost" type="button" :disabled="busy || databaseRuntime.config.active_backend === 'mysql'" @click="testDatabaseConnection">测试连接</button>
                    </div>
                    <div v-if="databaseConnectionTest" class="database-test-result">
                      <strong>连接通过</strong>
                      <span>MySQL {{ databaseConnectionTest.version }}</span>
                      <span>{{ databaseConnectionTest.latency_ms }} ms</span>
                      <span>{{ databaseConnectionTest.empty ? "目标库为空，可迁移" : `目标库已有 ${databaseConnectionTest.tables} 张表` }}</span>
                    </div>
                  </section>

                  <section class="panel database-migration-card">
                    <div class="panel-head">
                      <div>
                        <h2>数据迁移与切换</h2>
                        <p>迁移期间账户门户暂停业务数据库请求；普通 <code>/v1</code> 推理仍直达 AI 接入层。</p>
                      </div>
                    </div>
                    <div v-if="databaseRuntime.migration.status !== 'idle'" class="database-migration-state">
                      <div><strong>{{ databaseRuntime.migration.stage }}</strong><span>{{ Number(databaseRuntime.migration.progress || 0) }}%</span></div>
                      <progress :value="Number(databaseRuntime.migration.progress || 0)" max="100"></progress>
                      <p v-if="databaseRuntime.migration.error" class="error-text">{{ databaseRuntime.migration.error }}</p>
                      <small v-if="databaseRuntime.migration.backup_path">SQLite 备份：<code>{{ databaseRuntime.migration.backup_path }}</code></small>
                    </div>
                    <ol class="database-safety-list">
                      <li>目标 MySQL database 必须为空，LLMCtl 不会覆盖已有表。</li>
                      <li>切换前会创建 SQLite 备份，并逐表核对行数和 SHA-256。</li>
                      <li>只有全部校验通过才写入切换标记；失败时继续使用 SQLite。</li>
                    </ol>
                    <template v-if="databaseRuntime.config.active_backend === 'sqlite'">
                      <label>输入 <code>MIGRATE_TO_MYSQL</code> 确认
                        <input v-model.trim="databaseMigrationConfirmation" autocomplete="off" placeholder="MIGRATE_TO_MYSQL" :disabled="databaseRuntime.busy || busy" />
                      </label>
                      <button class="primary wide-button" type="button" :disabled="databaseRuntime.busy || busy || databaseMigrationConfirmation !== 'MIGRATE_TO_MYSQL'" @click="startDatabaseMigration">
                        {{ databaseRuntime.busy || operation === "database-migrate" ? "迁移中…" : "备份、迁移、校验并切换" }}
                      </button>
                    </template>
                    <template v-else>
                      <div class="warning">回滚会切回迁移时保留的 SQLite；切换 MySQL 后产生的新用户、用量和设置不会自动写回旧 SQLite。</div>
                      <label>输入 <code>ROLLBACK_TO_SQLITE</code> 确认
                        <input v-model.trim="databaseRollbackConfirmation" autocomplete="off" placeholder="ROLLBACK_TO_SQLITE" :disabled="busy" />
                      </label>
                      <button class="danger wide-button" type="button" :disabled="busy || databaseRollbackConfirmation !== 'ROLLBACK_TO_SQLITE'" @click="rollbackDatabaseToSqlite">回滚到保留的 SQLite</button>
                    </template>
                  </section>
                </div>
              </template>
            </template>
          </section>

          <section v-if="section === 'monitoring'" class="page monitor-page">
            <div class="page-head">
              <div>
                <span class="eyebrow">HOST TELEMETRY</span>
                <h1>系统监控</h1>
                <p>CPU、内存、GPU、网络、磁盘和进程的实时运行状态。</p>
              </div>
              <div class="button-row">
                <span class="monitor-live" :class="{ paused: monitorPaused }">
                  <i></i>{{ monitorPaused ? "已暂停" : "2 秒刷新" }}
                </span>
                <button class="ghost" type="button" @click="monitorPaused = !monitorPaused">
                  {{ monitorPaused ? "继续采样" : "暂停采样" }}
                </button>
                <button class="primary" type="button" :disabled="monitorLoading" @click="loadSystemMonitor()">
                  {{ monitorLoading ? "读取中…" : "立即刷新" }}
                </button>
              </div>
            </div>

            <div v-if="!systemMonitor" class="panel monitor-loading">
              {{ monitorLoading ? "正在采集系统状态…" : "暂无系统监控数据。" }}
            </div>
            <template v-else>
              <div class="monitor-context panel">
                <div>
                  <strong>{{ systemMonitor.host.hostname }}</strong>
                  <span>{{ systemMonitor.host.cpu_model }}</span>
                </div>
                <dl>
                  <div><dt>内核</dt><dd>{{ systemMonitor.host.kernel }}</dd></div>
                  <div><dt>逻辑 CPU</dt><dd>{{ systemMonitor.host.logical_cpus }}</dd></div>
                  <div><dt>运行时间</dt><dd>{{ formatUptime(systemMonitor.host.uptime_seconds) }}</dd></div>
                  <div><dt>最后采样</dt><dd>{{ date(systemMonitor.sampled_at) }}</dd></div>
                </dl>
              </div>

              <div class="monitor-metrics">
                <article class="panel">
                  <span>CPU 使用率</span>
                  <strong>{{ formatMonitorPercent(systemMonitor.cpu.usage_percent) }}</strong>
                  <small>负载 {{ systemMonitor.host.load_average.map((value) => Number(value).toFixed(2)).join(" / ") }}</small>
                  <div class="meter"><i :style="{ width: `${systemMonitor.cpu.usage_percent || 0}%` }"></i></div>
                </article>
                <article class="panel">
                  <span>内存</span>
                  <strong>{{ formatMonitorPercent(systemMonitor.memory.used_percent) }}</strong>
                  <small>{{ formatBytes(systemMonitor.memory.used_bytes) }} / {{ formatBytes(systemMonitor.memory.total_bytes) }}</small>
                  <div class="meter"><i :style="{ width: `${systemMonitor.memory.used_percent || 0}%` }"></i></div>
                </article>
                <article class="panel">
                  <span>GPU 平均负载</span>
                  <strong>{{ systemMonitor.gpu.available ? formatMonitorPercent(monitoredGpuAverage) : "不可用" }}</strong>
                  <small v-if="systemMonitor.gpu.available">{{ systemMonitor.gpu.gpus.length }} 张 GPU · 显存 {{ formatMonitorPercent(monitoredGpuMemory.percent) }}</small>
                  <small v-else>{{ systemMonitor.gpu.error }}</small>
                  <div class="meter gpu"><i :style="{ width: `${monitoredGpuAverage || 0}%` }"></i></div>
                </article>
                <article class="panel">
                  <span>网络吞吐</span>
                  <strong>↓ {{ formatRate(systemMonitor.network.rx_bytes_per_second) }}</strong>
                  <small>↑ {{ formatRate(systemMonitor.network.tx_bytes_per_second) }}</small>
                </article>
                <article class="panel">
                  <span>进程</span>
                  <strong>{{ systemMonitor.process_summary.total }}</strong>
                  <small>运行 {{ systemMonitor.process_summary.running }} · 不可中断 {{ systemMonitor.process_summary.uninterruptible }} · 僵尸 {{ systemMonitor.process_summary.zombie }}</small>
                </article>
              </div>

              <section class="panel monitor-trends">
                <div class="panel-head">
                  <div><h2>资源趋势</h2><p>当前 {{ monitorHistory.sampledAt.length }} 次采样，最多保留 60 次；离开本页即停止请求数据。</p></div>
                  <div class="trend-legend">
                    <span class="cpu"><i></i>CPU <small>{{ latestMonitorTrendValue(monitorHistory.cpu) }}</small></span>
                    <span class="memory"><i></i>内存 <small>{{ latestMonitorTrendValue(monitorHistory.memory) }}</small></span>
                  </div>
                </div>
                <div class="trend-chart" aria-label="CPU 和内存使用率趋势">
                  <span class="trend-ceiling">100%</span><span class="trend-floor">0%</span>
                  <svg viewBox="0 0 300 72" preserveAspectRatio="none" role="img">
                    <polyline class="memory" :points="monitorTrendPoints(monitorHistory.memory)" />
                    <polyline class="cpu" :points="monitorTrendPoints(monitorHistory.cpu)" />
                  </svg>
                </div>
                <div v-if="monitorGpuTrends.length" class="gpu-trends">
                  <div class="gpu-trends-head">
                    <strong>GPU 并行趋势</strong>
                    <span>每张 GPU 独立显示，便于识别负载偏斜</span>
                  </div>
                  <div class="gpu-trend-grid">
                    <article v-for="gpu in monitorGpuTrends" :key="gpu.index" class="gpu-trend-card">
                      <header>
                        <span>GPU {{ gpu.index }}</span>
                        <strong>{{ formatMonitorPercent(gpu.current) }}</strong>
                      </header>
                      <div class="gpu-mini-chart">
                        <svg viewBox="0 0 300 72" preserveAspectRatio="none" role="img" :aria-label="`GPU ${gpu.index} 使用率趋势`">
                          <polyline :points="monitorTrendPoints(gpu.values)" />
                        </svg>
                      </div>
                      <footer><span>{{ gpu.name }}</span><span>峰值 {{ formatMonitorPercent(gpu.peak) }}</span></footer>
                    </article>
                  </div>
                </div>
                <div class="network-rate-summary">
                  <span>当前下行 <strong>{{ formatRate(systemMonitor.network.rx_bytes_per_second) }}</strong></span>
                  <span>当前上行 <strong>{{ formatRate(systemMonitor.network.tx_bytes_per_second) }}</strong></span>
                  <span>Swap <strong>{{ formatBytes(systemMonitor.memory.swap_used_bytes) }} / {{ formatBytes(systemMonitor.memory.swap_total_bytes) }}</strong></span>
                </div>
              </section>

              <section class="panel" v-if="systemMonitor.gpu.available">
                <div class="panel-head"><div><h2>GPU 状态</h2><p>利用率、显存、温度、功耗与计算进程。</p></div><span class="filter-count">{{ systemMonitor.gpu.gpus.length }} 张</span></div>
                <div class="table-wrap">
                  <table class="monitor-table gpu-table">
                    <thead><tr><th>GPU</th><th>利用率</th><th>显存</th><th>显存控制器</th><th>温度</th><th>功耗</th><th>性能状态</th><th>计算进程</th></tr></thead>
                    <tbody><tr v-for="gpu in systemMonitor.gpu.gpus" :key="gpu.index">
                      <td><strong>GPU {{ gpu.index }}</strong><small>{{ gpu.name }}</small><small>驱动 {{ gpu.driver_version }} · PCI {{ gpu.pci_bus_id }}</small></td>
                      <td><strong>{{ formatMonitorPercent(gpu.utilization_percent) }}</strong><div class="mini-meter"><i :style="{ width: `${gpu.utilization_percent || 0}%` }"></i></div></td>
                      <td>{{ formatBytes(gpu.memory_used_bytes) }} / {{ formatBytes(gpu.memory_total_bytes) }}</td>
                      <td>{{ formatMonitorPercent(gpu.memory_utilization_percent) }}</td>
                      <td>{{ gpu.temperature_c == null ? "—" : `${gpu.temperature_c}°C` }}</td>
                      <td>{{ gpu.power_watts == null ? "—" : `${gpu.power_watts.toFixed(0)} / ${gpu.power_limit_watts?.toFixed(0) || "—"} W` }}</td>
                      <td><code>{{ gpu.pstate }}</code></td>
                      <td>{{ gpu.process_count }} 个 · {{ formatBytes(gpu.process_memory_bytes) }}</td>
                    </tr></tbody>
                  </table>
                </div>
              </section>

              <div class="monitor-detail-grid">
                <section class="panel">
                  <div class="panel-head"><div><h2>磁盘</h2><p>本地持久文件系统使用情况。</p></div></div>
                  <div class="monitor-resource-list">
                    <article v-for="disk in systemMonitor.disks" :key="`${disk.source}:${disk.mount_point}`">
                      <div><strong>{{ disk.mount_point }}</strong><small>{{ disk.source }} · {{ disk.filesystem }}</small></div>
                      <span>{{ formatBytes(disk.used_bytes) }} / {{ formatBytes(disk.total_bytes) }}</span>
                      <div class="meter"><i :class="{ warning: disk.used_percent >= 85 }" :style="{ width: `${disk.used_percent || 0}%` }"></i></div>
                    </article>
                    <p v-if="!systemMonitor.disks.length" class="empty">未读取到可见的本地文件系统。</p>
                  </div>
                </section>
                <section class="panel">
                  <div class="panel-head"><div><h2>网络接口</h2><p>速率由相邻两次系统计数器差值计算。</p></div></div>
                  <div class="table-wrap compact-monitor-table"><table class="monitor-table"><thead><tr><th>接口</th><th>下行</th><th>上行</th><th>累计接收</th><th>累计发送</th></tr></thead>
                    <tbody><tr v-for="item in systemMonitor.network.interfaces" :key="item.name"><td><code>{{ item.name }}</code><small v-if="item.loopback">本机回环</small></td><td>{{ formatRate(item.rx_bytes_per_second) }}</td><td>{{ formatRate(item.tx_bytes_per_second) }}</td><td>{{ formatBytes(item.rx_bytes) }}</td><td>{{ formatBytes(item.tx_bytes) }}</td></tr></tbody>
                  </table></div>
                </section>
              </div>

              <section class="panel monitor-process-panel">
                <div class="panel-head"><div><h2>进程</h2><p>类似 top 的只读视图；命令参数中的 Key、Token 和密码会自动脱敏。</p></div><span class="filter-count">显示候选 {{ systemMonitor.process_summary.returned }} / {{ systemMonitor.process_summary.total }}</span></div>
                <div class="monitor-process-filter">
                  <input v-model="monitorProcess.query" type="search" placeholder="搜索 PID、用户、进程或命令" aria-label="搜索系统进程" />
                  <select v-model="monitorProcess.sort" aria-label="进程排序"><option value="cpu">CPU 从高到低</option><option value="memory">内存从高到低</option><option value="pid">PID 从小到大</option></select>
                  <span class="filter-count">{{ filteredMonitorProcesses.length }} 条</span>
                </div>
                <div class="table-wrap"><table class="monitor-table process-table"><thead><tr><th>PID</th><th>用户</th><th>状态</th><th>CPU</th><th>内存</th><th>RSS</th><th>命令</th></tr></thead>
                  <tbody><tr v-for="row in currentMonitorProcesses" :key="row.pid"><td><code>{{ row.pid }}</code></td><td>{{ row.user }}</td><td><code>{{ row.state }}</code></td><td><strong>{{ row.cpu_percent.toFixed(1) }}%</strong></td><td>{{ row.memory_percent.toFixed(2) }}%</td><td>{{ formatBytes(row.rss_bytes) }}</td><td><strong>{{ row.command }}</strong><small class="command-line" :title="row.command_line">{{ row.command_line }}</small></td></tr>
                    <tr v-if="!currentMonitorProcesses.length"><td colspan="7" class="empty">没有符合条件的进程。</td></tr>
                  </tbody></table></div>
                <PaginationBar :page="monitorProcess.page" :pages="monitorProcessPages" :total="filteredMonitorProcesses.length" @previous="setMonitorProcessPage(monitorProcess.page - 1)" @next="setMonitorProcessPage(monitorProcess.page + 1)" />
                <p class="monitor-footnote">进程 CPU 是相邻采样间的占用率，多线程进程可超过 100%；首次打开页面时需等待下一次采样才会出现准确的 CPU 和网络速率。</p>
              </section>

              <div v-if="Object.keys(systemMonitor.errors || {}).length" class="warning monitor-warning">
                <strong>部分指标不可用。</strong>
                <span v-for="(message, name) in systemMonitor.errors" :key="name"><code>{{ name }}</code> {{ message }}</span>
              </div>
            </template>
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
                <code>--force</code> 覆盖配置。若状态中的模型为
                <code>enabled: false</code>，先复制该模型 ID 并执行
                <code>llmctl workflow model enable &lt;模型ID&gt;</code>，再依次执行
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

              <section
                id="stress-run-detail"
                class="panel stress-live"
                v-if="selectedStressRun"
              >
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
                    <td>{{ date(run.created_at) }}</td><td><code>{{ run.public_model_id }}</code></td><td>并发 {{ run.concurrency }} · 输入 {{ compactTokens(run.target_input_tokens) }} · {{ run.request_count }} 请求</td><td><span class="status" :class="run.status === 'completed' ? 'ok' : run.status === 'failed' ? 'bad' : 'warn'">{{ statusLabel(run.status) }}</span><small>{{ metricNumber(run.metrics?.success_rate, 1) }}% 成功</small></td><td>{{ metricNumber(run.metrics?.request_rps, 2) }} RPS<br><small>{{ metricNumber(run.metrics?.output_tokens_per_second, 1) }} tok/s</small></td><td><button :class="selectedStressRunId === run.id ? 'primary' : 'ghost'" type="button" :disabled="selectedStressRunId === run.id" @click="selectStressRun(run)">{{ selectedStressRunId === run.id ? '当前查看' : '查看详情' }}</button></td>
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
                <p>
                  记录谁在什么时间执行了什么操作、作用于哪个对象以及结果。
                  展开“完整详情”可以查看未截断的审计数据。
                </p>
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
                    <td class="audit-action">
                      <strong>{{ auditActionLabel(row.action) }}</strong>
                      <code>{{ row.action }}</code>
                    </td>
                    <td><code class="audit-target">{{ row.target || "—" }}</code></td>
                    <td>
                      <span
                        class="status"
                        :class="row.status === 'success' ? 'ok' : 'bad'"
                        >{{ statusLabel(row.status) }}</span
                      >
                    </td>
                    <td class="audit-detail-cell">
                      <details class="audit-detail">
                        <summary>查看完整详情</summary>
                        <pre>{{ formatAuditDetail(row.detail) }}</pre>
                      </details>
                    </td>
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

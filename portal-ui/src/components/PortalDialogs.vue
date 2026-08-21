<script>
import { usePortalWorkspaceContext } from "../portalWorkspaceContext.js";
import ListFilterBar from "./ListFilterBar.vue";
import PaginationBar from "./PaginationBar.vue";

export default {
  name: "PortalDialogs",
  components: { ListFilterBar, PaginationBar },
  setup() {
    return usePortalWorkspaceContext();
  },
};
</script>

<template>
    <dialog id="pending-user-delete">
      <form method="dialog" class="dialog-head">
        <div>
          <h2>删除未验证用户</h2>
          <p>仅删除尚未完成邮箱验证、没有 API Key 和调用记录的注册占位。</p>
        </div>
        <button class="icon-button" aria-label="关闭">×</button>
      </form>
      <div class="form-stack">
        <div class="warning">
          删除后，现有验证链接立即失效；用户需要重新注册才能继续。
        </div>
        <p>
          目标邮箱：<strong>{{ pendingUserDelete.email }}</strong>
        </p>
        <label>
          输入完整邮箱确认删除
          <input
            v-model="pendingUserDelete.confirmation"
            autocomplete="off"
            :placeholder="pendingUserDelete.email"
          />
        </label>
        <button
          type="button"
          class="danger"
          :disabled="
            busy ||
            pendingUserDelete.confirmation.trim() !== pendingUserDelete.email
          "
          @click="deletePendingUser"
        >
          {{ operation === "pending-user-delete" ? "删除中…" : "确认删除注册记录" }}
        </button>
      </div>
    </dialog>
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
                >LLMCtl 受管模型优先读取当前 Worker 有效配置；其他模型读取 AI
                接入层，路由组合采用所有底层目标中的保守值。</small
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
                :max="MAX_OUTPUT_TOKENS_LIMIT"
                @input="modelEdit.sync_max_output_tokens = true"
              /><span class="field-hint"
                >写入接入层覆盖值；vLLM 单次生成硬上限为 32,768</span
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
            <span
              v-if="modelEdit.metadata.managed_runtime_corrected_count"
              class="warn-text"
            >
              已按运行配置修正
              {{ modelEdit.metadata.managed_runtime_corrected_count }} 个接入层旧值；保存时同步
            </span>
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
                    <template
                      v-if="
                        target.managed_runtime_context &&
                        target.gateway_context_window_tokens &&
                        target.gateway_context_window_tokens !==
                          target.context_window_tokens
                      "
                    >
                      （接入层旧值
                      {{ target.gateway_context_window_tokens.toLocaleString() }}）
                    </template>
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
</template>

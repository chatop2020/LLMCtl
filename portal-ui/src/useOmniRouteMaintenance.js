import { computed, reactive, ref } from "vue";

const TERMINAL_STATES = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "rolled_back",
]);

// Router 维护会按设计短暂停止账户门户。30 分钟重试窗口覆盖镜像切换、
// 完整冒烟和一次自动回滚，同时避免永久显示无法确认的运行中状态。
export const OMNIROUTE_MAX_JOB_POLL_FAILURES = 180;

/**
 * 管理 OmniRoute 只读评估和 root 后台维护任务的页面状态。
 *
 * @param {object} dependencies 页面组合根提供的受控能力。
 * @param {Function} dependencies.api 调用同源门户管理员 API。
 * @param {Function} dependencies.notify 显示自然语言结果和失败原因。
 * @returns {object} 状态、确认表单、加载/评估/提交/取消和轮询方法。
 */
export function useOmniRouteMaintenance({ api, notify }) {
  const omnirouteMaintenance = ref(null);
  const omnirouteAssessment = ref(null);
  const omnirouteJob = ref(null);
  const omnirouteLoading = ref(false);
  const omnirouteActionLoading = ref(false);
  const omniroutePollFailures = ref(0);
  const omniroutePollingPaused = ref(false);
  const omniroutePollError = ref("");
  const omnirouteForm = reactive({
    update_image: "",
    update_confirmation: "",
    online_confirmation: "",
    compact_confirmation: "",
    audit_cleanup_confirmation: "",
    rollback_id: "",
    rollback_confirmation: "",
  });
  let pollTimer = null;

  const omnirouteJobActive = computed(
    () =>
      Boolean(omnirouteJob.value?.id) &&
      !TERMINAL_STATES.has(String(omnirouteJob.value?.state || "")),
  );

  /** 清除当前轮询定时器，不改变服务端任务。 */
  function stopOmniRoutePolling() {
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = null;
  }

  /** 清空连接失败状态；不改变当前任务或服务端执行状态。 */
  function resetOmniRoutePollHealth() {
    omniroutePollFailures.value = 0;
    omniroutePollingPaused.value = false;
    omniroutePollError.value = "";
  }

  /**
   * 返回下一次状态读取的退避时间，单位为毫秒。
   *
   * @returns {number} 正常时为两秒；连续失败后逐步放缓，最多十秒。
   */
  function omniroutePollDelay() {
    if (!omniroutePollFailures.value) return 2000;
    return Math.min(10000, 2000 * 2 ** Math.min(3, omniroutePollFailures.value));
  }

  /**
   * 合并控制服务快照，并用推荐镜像初始化尚未编辑的升级目标。
   *
   * 快照是当前任务是否仍在执行的权威来源。若快照已经没有对应活动任务，
   * 本地遗留的 waiting/running 状态会被终态替换或清除，避免永久锁住按钮。
   *
   * @param {object} snapshot 门户 API 返回的非敏感 OmniRoute 状态。
   * @returns {object|null} 快照中解析到的当前或最近任务。
   */
  function applyOmniRouteSnapshot(snapshot) {
    omnirouteMaintenance.value = snapshot;
    omnirouteAssessment.value =
      snapshot?.latest_assessment || omnirouteAssessment.value;
    if (!omnirouteForm.update_image) {
      omnirouteForm.update_image =
        snapshot?.recommended_image || snapshot?.configured_image || "";
    }
    const currentJob = omnirouteJob.value;
    const currentId = String(currentJob?.id || "");
    const matchingJob = currentId
      ? (snapshot?.jobs || []).find((job) => String(job?.id || "") === currentId)
      : null;
    const nextJob =
      matchingJob ||
      snapshot?.active_job ||
      (TERMINAL_STATES.has(String(currentJob?.state || "")) ? currentJob : null);
    omnirouteJob.value = nextJob || null;
    return omnirouteJob.value;
  }

  /**
   * 读取镜像、数据库、备份和后台任务摘要。
   *
   * @param {object} options 加载选项；silent 为真时不弹出轮询错误。
   * @returns {Promise<object|null>} 成功快照；失败返回 null。
   */
  async function loadOmniRouteMaintenance(options = {}) {
    if (omnirouteLoading.value) return omnirouteMaintenance.value;
    omnirouteLoading.value = true;
    try {
      const snapshot = await api("admin/omniroute");
      applyOmniRouteSnapshot(snapshot);
      resetOmniRoutePollHealth();
      if (snapshot?.active_job) startOmniRoutePolling(snapshot.active_job.id);
      return snapshot;
    } catch (error) {
      if (!options.silent) {
        notify(
          omnirouteJobActive.value
            ? "OmniRoute 任务状态暂时无法读取；Router 维护期间账户门户可能短暂停止，后台任务未被取消，系统会继续重试"
            : `OmniRoute 状态读取失败：${error.message}`,
          omnirouteJobActive.value ? "working" : "bad",
        );
      }
      return null;
    } finally {
      omnirouteLoading.value = false;
    }
  }

  /**
   * 执行只读 SQLite 评估；深度模式运行完整 integrity_check。
   *
   * @param {boolean} deep 是否执行深度完整性检查。
   * @returns {Promise<object|null>} 评估结果；失败返回 null。
   */
  async function assessOmniRouteSqlite(deep = false) {
    if (omnirouteActionLoading.value || omnirouteJobActive.value) return null;
    omnirouteActionLoading.value = true;
    notify(deep ? "正在执行深度 SQLite 完整性评估…" : "正在评估 SQLite…", "working");
    try {
      const result = await api("admin/omniroute/assess", {
        method: "POST",
        body: JSON.stringify({ deep }),
      });
      omnirouteAssessment.value = result;
      notify(
        result.health === "healthy"
          ? "OmniRoute SQLite 评估通过"
          : `OmniRoute SQLite 评估完成：${result.health}`,
        result.health === "critical" ? "bad" : result.health === "warning" ? "warn" : "ok",
      );
      await loadOmniRouteMaintenance({ silent: true });
      return result;
    } catch (error) {
      notify(`SQLite 评估失败：${error.message}`, "bad");
      return null;
    } finally {
      omnirouteActionLoading.value = false;
    }
  }

  /**
   * 查询一个后台任务并在终态后刷新数据库与备份摘要。
   *
   * @param {string} jobId 控制服务返回的任务 UUID。
   * @returns {Promise<object|null>} 最新任务；读取失败返回 null。
   */
  async function pollOmniRouteJob(jobId) {
    if (!jobId || document.hidden) return omnirouteJob.value;
    try {
      const job = await api("admin/omniroute/job", {
        method: "POST",
        body: JSON.stringify({ id: jobId }),
      });
      const recovered = omniroutePollFailures.value > 0;
      omnirouteJob.value = job;
      resetOmniRoutePollHealth();
      if (TERMINAL_STATES.has(String(job.state || ""))) {
        stopOmniRoutePolling();
        await loadOmniRouteMaintenance({ silent: true });
        notify(
          job.message || "OmniRoute 任务已结束",
          job.state === "succeeded" ? "ok" : "bad",
        );
      } else if (recovered) {
        notify("OmniRoute 任务状态连接已恢复，后台任务仍在执行", "working");
      }
      return job;
    } catch (error) {
      omniroutePollFailures.value += 1;
      omniroutePollError.value = String(error?.message || "连接失败");

      // 单个任务接口失败时尝试读取总快照。快照可以在浏览器漏掉终态更新后
      // 恢复真实状态；账户门户整体暂停时，两条路径都会失败并进入有界重试。
      try {
        const snapshot = await api("admin/omniroute");
        const recoveredJob = applyOmniRouteSnapshot(snapshot);
        resetOmniRoutePollHealth();
        if (TERMINAL_STATES.has(String(recoveredJob?.state || ""))) {
          stopOmniRoutePolling();
          notify(
            recoveredJob.message || "OmniRoute 任务已结束",
            recoveredJob.state === "succeeded" ? "ok" : "bad",
          );
        }
        return recoveredJob;
      } catch {
        if (
          omniroutePollFailures.value >= OMNIROUTE_MAX_JOB_POLL_FAILURES
        ) {
          stopOmniRoutePolling();
          omniroutePollingPaused.value = true;
          notify(
            "OmniRoute 任务状态已连续 30 分钟无法读取；后台任务未被取消，请点击“重新读取任务”恢复监控",
            "bad",
          );
        }
        return null;
      }
    }
  }

  /**
   * 正常时每两秒轮询后台任务；连接失败时退避到十秒。重复调用只保留一个定时器。
   *
   * @param {string} jobId 后台任务 UUID。
   * @param {object} options 轮询选项；resetHealth 为假时保留失败计数。
   */
  function startOmniRoutePolling(jobId, options = {}) {
    stopOmniRoutePolling();
    if (options.resetHealth !== false) resetOmniRoutePollHealth();
    const tick = async () => {
      pollTimer = null;
      await pollOmniRouteJob(String(omnirouteJob.value?.id || jobId));
      if (omnirouteJobActive.value && !omniroutePollingPaused.value)
        pollTimer = window.setTimeout(tick, omniroutePollDelay());
    };
    pollTimer = window.setTimeout(tick, 300);
  }

  /**
   * 在自动重试暂停后重新读取当前任务。
   *
   * @returns {boolean} 存在可继续监控的活动任务时返回 true。
   */
  function resumeOmniRoutePolling() {
    if (!omnirouteJobActive.value) return false;
    startOmniRoutePolling(omnirouteJob.value.id);
    notify("正在重新连接账户门户并读取 OmniRoute 任务状态…", "working");
    return true;
  }

  /**
   * 提交经过服务端再次校验的维护任务。
   *
   * @param {string} action backup、online、compact、audit-cleanup、update 或 rollback。
   * @param {object} fields 镜像、备份 ID 和精确确认短语。
   * @returns {Promise<object|null>} 已提交任务；失败返回 null。
   */
  async function submitOmniRouteTask(action, fields = {}) {
    if (omnirouteActionLoading.value || omnirouteJobActive.value) return null;
    omnirouteActionLoading.value = true;
    notify("正在校验并提交 OmniRoute 运维任务…", "working");
    try {
      const job = await api("admin/omniroute/submit", {
        method: "POST",
        body: JSON.stringify({ action, ...fields }),
      });
      omnirouteJob.value = job;
      startOmniRoutePolling(job.id);
      notify("OmniRoute 运维任务已受理，可在本页查看实时阶段", "working");
      return job;
    } catch (error) {
      notify(`OmniRoute 任务未提交：${error.message}`, "bad");
      return null;
    } finally {
      omnirouteActionLoading.value = false;
    }
  }

  /** 创建不改变 Router 的一致性 SQLite 备份。 */
  async function backupOmniRouteSqlite() {
    return submitOmniRouteTask("backup");
  }

  /** 提交在线 optimize 与 PASSIVE checkpoint。 */
  async function maintainOmniRouteOnline() {
    const result = await submitOmniRouteTask("online", {
      confirmation: omnirouteForm.online_confirmation.trim(),
    });
    if (result) omnirouteForm.online_confirmation = "";
    return result;
  }

  /** 提交需要短暂停止 Router 的 VACUUM 压缩。 */
  async function compactOmniRouteSqlite() {
    const result = await submitOmniRouteTask("compact", {
      confirmation: omnirouteForm.compact_confirmation.trim(),
    });
    if (result) omnirouteForm.compact_confirmation = "";
    return result;
  }

  /** 提交带完整备份、回滚和冒烟保护的重复 Key 激活审计清理。 */
  async function cleanOmniRouteAudit() {
    const result = await submitOmniRouteTask("audit-cleanup", {
      confirmation: omnirouteForm.audit_cleanup_confirmation.trim(),
    });
    if (result) omnirouteForm.audit_cleanup_confirmation = "";
    return result;
  }

  /** 提交固定镜像升级任务。 */
  async function updateOmniRoute() {
    const result = await submitOmniRouteTask("update", {
      image: omnirouteForm.update_image.trim(),
      confirmation: omnirouteForm.update_confirmation.trim(),
    });
    if (result) omnirouteForm.update_confirmation = "";
    return result;
  }

  /**
   * 选择一个受管备份，并清空旧的回滚确认短语。
   *
   * @param {string} backupId 备份列表中的稳定 ID。
   */
  function selectOmniRouteBackup(backupId) {
    omnirouteForm.rollback_id = String(backupId || "");
    omnirouteForm.rollback_confirmation = "";
  }

  /** 提交回滚任务；服务端会在覆盖前再次备份当前状态。 */
  async function rollbackOmniRoute() {
    const result = await submitOmniRouteTask("rollback", {
      backup_id: omnirouteForm.rollback_id,
      confirmation: omnirouteForm.rollback_confirmation.trim(),
    });
    if (result) omnirouteForm.rollback_confirmation = "";
    return result;
  }

  /** 请求任务在下一安全检查点取消。 */
  async function cancelOmniRouteJob() {
    if (!omnirouteJobActive.value) return null;
    try {
      const job = await api("admin/omniroute/cancel", {
        method: "POST",
        body: JSON.stringify({ id: omnirouteJob.value.id }),
      });
      omnirouteJob.value = job;
      notify("已请求在下一安全检查点取消任务", "working");
      return job;
    } catch (error) {
      notify(`取消请求失败：${error.message}`, "bad");
      return null;
    }
  }

  return {
    omnirouteMaintenance,
    omnirouteAssessment,
    omnirouteJob,
    omnirouteLoading,
    omnirouteActionLoading,
    omniroutePollFailures,
    omniroutePollingPaused,
    omniroutePollError,
    omnirouteForm,
    omnirouteJobActive,
    stopOmniRoutePolling,
    resumeOmniRoutePolling,
    loadOmniRouteMaintenance,
    pollOmniRouteJob,
    assessOmniRouteSqlite,
    backupOmniRouteSqlite,
    maintainOmniRouteOnline,
    compactOmniRouteSqlite,
    cleanOmniRouteAudit,
    updateOmniRoute,
    selectOmniRouteBackup,
    rollbackOmniRoute,
    cancelOmniRouteJob,
  };
}

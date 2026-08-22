import { computed, reactive, ref } from "vue";

const TERMINAL_STATES = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "rolled_back",
]);

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
  const omnirouteForm = reactive({
    update_image: "",
    update_confirmation: "",
    online_confirmation: "",
    compact_confirmation: "",
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

  /**
   * 合并控制服务快照，并用推荐镜像初始化尚未编辑的升级目标。
   *
   * @param {object} snapshot 门户 API 返回的非敏感 OmniRoute 状态。
   */
  function applyOmniRouteSnapshot(snapshot) {
    omnirouteMaintenance.value = snapshot;
    omnirouteAssessment.value =
      snapshot?.latest_assessment || omnirouteAssessment.value;
    if (!omnirouteForm.update_image) {
      omnirouteForm.update_image =
        snapshot?.recommended_image || snapshot?.configured_image || "";
    }
    if (snapshot?.active_job) omnirouteJob.value = snapshot.active_job;
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
      if (snapshot?.active_job) startOmniRoutePolling(snapshot.active_job.id);
      return snapshot;
    } catch (error) {
      if (!options.silent)
        notify(`OmniRoute 状态读取失败：${error.message}`, "bad");
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
      omnirouteJob.value = job;
      if (TERMINAL_STATES.has(String(job.state || ""))) {
        stopOmniRoutePolling();
        await loadOmniRouteMaintenance({ silent: true });
        notify(
          job.message || "OmniRoute 任务已结束",
          job.state === "succeeded" ? "ok" : "bad",
        );
      }
      return job;
    } catch (error) {
      stopOmniRoutePolling();
      notify(`OmniRoute 任务读取失败：${error.message}`, "bad");
      return null;
    }
  }

  /**
   * 每两秒轮询后台任务；重复调用只保留一个定时器。
   *
   * @param {string} jobId 后台任务 UUID。
   */
  function startOmniRoutePolling(jobId) {
    stopOmniRoutePolling();
    const tick = async () => {
      await pollOmniRouteJob(jobId);
      if (omnirouteJobActive.value)
        pollTimer = window.setTimeout(tick, 2000);
    };
    pollTimer = window.setTimeout(tick, 300);
  }

  /**
   * 提交经过服务端再次校验的维护任务。
   *
   * @param {string} action backup、online、compact、update 或 rollback。
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
    omnirouteForm,
    omnirouteJobActive,
    stopOmniRoutePolling,
    loadOmniRouteMaintenance,
    assessOmniRouteSqlite,
    backupOmniRouteSqlite,
    maintainOmniRouteOnline,
    compactOmniRouteSqlite,
    updateOmniRoute,
    selectOmniRouteBackup,
    rollbackOmniRoute,
    cancelOmniRouteJob,
  };
}

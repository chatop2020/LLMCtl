import { reactive } from "vue";

/**
 * 管理未验证用户的手动通过、验证邮件补发和受限删除交互。
 *
 * @param {object} dependencies 组合根提供的 API、统一操作状态和消息能力。
 * @param {Function} dependencies.api 调用管理员 API。
 * @param {Function} dependencies.action 执行带刷新和反馈的管理操作。
 * @param {Function} dependencies.notify 显示确认错误。
 * @returns {object} 手动通过与删除确认状态，以及对应的管理操作方法。
 */
export function usePendingUserActions({ api, action, notify }) {
  const pendingUserApproval = reactive({
    user_id: "",
    email: "",
    confirmation: "",
  });
  const pendingUserDelete = reactive({ user_id: "", email: "", confirmation: "" });

  /**
   * 打开管理员手动通过确认框；这里只准备目标状态，不会立即激活账户。
   *
   * @param {object} user 管理员已通过其他可信方式核实邮箱归属的待验证用户。
   */
  function openPendingUserApproval(user) {
    Object.assign(pendingUserApproval, {
      user_id: String(user?.id || ""),
      email: String(user?.email || ""),
      confirmation: "",
    });
    document.querySelector("#pending-user-approval")?.showModal();
  }

  /**
   * 在管理员准确输入邮箱后，请求服务端完整开通待验证账户。
   *
   * @returns {Promise<object|null>} 成功响应；确认不匹配或开户失败时返回 null。
   */
  async function approvePendingUser() {
    if (
      !pendingUserApproval.email ||
      pendingUserApproval.confirmation.trim() !== pendingUserApproval.email
    ) {
      notify("请输入完整邮箱以确认手动通过", "bad");
      return null;
    }
    const result = await action(
      () =>
        api("admin/users/verification/approve", {
          method: "POST",
          body: JSON.stringify({
            user_id: pendingUserApproval.user_id,
            confirmation_email: pendingUserApproval.confirmation.trim(),
          }),
        }),
      "用户已手动通过验证，API Key 和模型权限已创建",
      { key: "pending-user-approval", pending: "正在创建 API Key 并同步模型权限…" },
    );
    if (result) {
      document.querySelector("#pending-user-approval")?.close();
      Object.assign(pendingUserApproval, {
        user_id: "",
        email: "",
        confirmation: "",
      });
    }
    return result;
  }

  /**
   * 为一个未验证用户补发验证邮件。
   *
   * @param {object} user 管理列表中的待验证普通用户。
   * @returns {Promise<object|null>} 成功响应；失败时由统一操作入口反馈并返回 null。
   */
  async function resendUserVerification(user) {
    if (user?.status !== "pending") {
      notify("只有邮箱未验证的用户可以补发验证邮件", "bad");
      return null;
    }
    return action(
      () =>
        api("admin/users/verification/resend", {
          method: "POST",
          body: JSON.stringify({ user_id: user.id }),
        }),
      `验证邮件已重新发送给 ${user.email}`,
      { key: `user-verification-resend:${user.id}`, pending: "正在生成并发送新的验证链接…" },
    );
  }

  /**
   * 打开未验证用户删除确认框；这里只准备状态，不立即删除数据。
   *
   * @param {object} user 管理员准备删除的待验证普通用户。
   */
  function openPendingUserDelete(user) {
    Object.assign(pendingUserDelete, {
      user_id: String(user?.id || ""),
      email: String(user?.email || ""),
      confirmation: "",
    });
    document.querySelector("#pending-user-delete")?.showModal();
  }

  /**
   * 在管理员准确输入邮箱后删除未验证注册占位。
   *
   * @returns {Promise<object|null>} 成功响应；确认不匹配或后端拒绝时返回 null。
   */
  async function deletePendingUser() {
    if (
      !pendingUserDelete.email ||
      pendingUserDelete.confirmation.trim() !== pendingUserDelete.email
    ) {
      notify("请输入完整邮箱以确认删除", "bad");
      return null;
    }
    const result = await action(
      () =>
        api("admin/users/pending/delete", {
          method: "POST",
          body: JSON.stringify({ user_id: pendingUserDelete.user_id }),
        }),
      "未验证注册记录已删除",
      { key: "pending-user-delete", pending: "正在检查关联数据并删除注册记录…" },
    );
    if (result) {
      document.querySelector("#pending-user-delete")?.close();
      Object.assign(pendingUserDelete, { user_id: "", email: "", confirmation: "" });
    }
    return result;
  }

  return {
    pendingUserApproval,
    pendingUserDelete,
    openPendingUserApproval,
    approvePendingUser,
    resendUserVerification,
    openPendingUserDelete,
    deletePendingUser,
  };
}

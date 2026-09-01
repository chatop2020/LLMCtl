import { reactive } from "vue";

/**
 * 管理员重置普通用户门户密码的确认状态与受审计操作。
 *
 * @param {object} dependencies 组合根提供的网络、统一操作和反馈能力。
 * @param {Function} dependencies.api 调用带管理员会话与 CSRF 的门户 API。
 * @param {Function} dependencies.action 执行带刷新、成功和失败反馈的管理操作。
 * @param {Function} dependencies.notify 显示提交前密码校验错误。
 * @returns {object} 临时密码表单及打开、提交密码重置的方法。
 *
 * 明文只存在当前对话框的响应式内存中，成功后立即清空；服务端响应、审计和
 * 管理快照均不会返回密码或密码哈希。
 */
export function useAdminUserPasswordReset({ api, action, notify }) {
  const userPasswordReset = reactive({
    user_id: "",
    email: "",
    password: "",
    confirm: "",
  });

  /** 清空当前目标和两次密码输入，避免在用户之间遗留明文。 */
  function clearUserPasswordReset() {
    Object.assign(userPasswordReset, {
      user_id: "",
      email: "",
      password: "",
      confirm: "",
    });
  }

  /**
   * 为管理列表中的正式普通用户打开密码重置对话框。
   *
   * @param {object} user 当前选中的普通用户快照。
   */
  function openUserPasswordReset(user) {
    clearUserPasswordReset();
    Object.assign(userPasswordReset, {
      user_id: String(user?.id || ""),
      email: String(user?.email || ""),
    });
    document.querySelector("#user-password-reset")?.showModal();
  }

  /**
   * 校验两次输入并提交管理员密码重置。
   *
   * @returns {Promise<object|null>} 成功时返回会话撤销结果；校验或请求失败返回
   *     ``null``，失败原因由页面反馈且不会记录密码。
   */
  async function resetUserPassword() {
    const password = userPasswordReset.password;
    if (password.length < 8 || password.length > 200) {
      notify("密码必须为 8-200 个字符", "bad");
      return null;
    }
    if (password.trim() && /^\p{Nd}+$/u.test(password.trim())) {
      notify("密码不能全部由数字组成", "bad");
      return null;
    }
    if (password !== userPasswordReset.confirm) {
      notify("两次输入的密码不一致", "bad");
      return null;
    }
    const result = await action(
      () =>
        api("admin/users/password/reset", {
          method: "POST",
          body: JSON.stringify({
            user_id: userPasswordReset.user_id,
            password,
            confirm: userPasswordReset.confirm,
          }),
        }),
      "用户密码已重置，现有门户会话已退出",
      { key: "user-password-reset", pending: "正在更新密码并撤销现有门户会话…" },
    );
    if (result) {
      document.querySelector("#user-password-reset")?.close();
      clearUserPasswordReset();
    }
    return result;
  }

  return {
    userPasswordReset,
    openUserPasswordReset,
    resetUserPassword,
  };
}

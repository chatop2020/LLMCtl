import { reactive } from "vue";

/**
 * 管理用户管理页中按需展示的 API Key，明文只保留在当前页面内存。
 *
 * @param {object} dependencies 组合根提供的网络与反馈能力。
 * @param {Function} dependencies.api 调用带管理员会话与 CSRF 的门户 API。
 * @param {Function} dependencies.copy 复制明文并向用户反馈结果。
 * @param {Function} dependencies.notify 显示加载失败或无 Key 等提示。
 * @returns {object} 每个用户的临时明文、加载状态和显示/隐藏/复制操作。
 */
export function useAdminApiKeys({ api, copy, notify }) {
  const adminUserApiKeys = reactive({});
  const adminUserApiKeyLoading = reactive({});

  /** 清除页面内全部管理员已查看的 Key，退出或切页时调用。 */
  function clearAdminUserApiKeys() {
    for (const userId of Object.keys(adminUserApiKeys))
      delete adminUserApiKeys[userId];
  }

  /**
   * 隐藏指定用户的 Key，并立即从浏览器内存中的响应式映射删除。
   *
   * @param {string} userId 要隐藏凭据的门户用户 ID。
   */
  function hideAdminUserApiKey(userId) {
    delete adminUserApiKeys[String(userId || "")];
  }

  /**
   * 从管理员专用接口读取一个用户的当前 Key；不会创建或轮换凭据。
   *
   * @param {object} user 管理列表中的普通用户快照。
   * @returns {Promise<string>} 成功时返回 Key 明文，失败或无 Key 时返回空字符串。
   */
  async function revealAdminUserApiKey(user) {
    const userId = String(user?.id || "");
    if (!userId || !user?.api_key_id) {
      notify("该用户尚未配置 API Key", "bad");
      return "";
    }
    adminUserApiKeyLoading[userId] = true;
    try {
      const result = await api("admin/users/key/reveal", {
        method: "POST",
        body: JSON.stringify({ user_id: userId }),
      });
      const rawKey = String(result?.api_key || "");
      if (rawKey.length < 16) throw new Error("接入层未返回有效 API Key");
      // 每次只展示一个用户的凭据，避免管理员滚动页面时累积多份明文。
      clearAdminUserApiKeys();
      adminUserApiKeys[userId] = rawKey;
      notify(`已显示 ${user.email || "该用户"} 的当前 API Key`);
      return rawKey;
    } catch (error) {
      notify(`API Key 读取失败：${error.message}`, "bad");
      return "";
    } finally {
      delete adminUserApiKeyLoading[userId];
    }
  }

  /**
   * 复制指定用户的当前 Key；尚未展示时先执行一次受审计的读取。
   *
   * @param {object} user 管理列表中的普通用户快照。
   * @returns {Promise<boolean>} 成功复制返回 true，读取或复制失败返回 false。
   */
  async function copyAdminUserApiKey(user) {
    const userId = String(user?.id || "");
    const rawKey =
      adminUserApiKeys[userId] || (await revealAdminUserApiKey(user));
    return rawKey ? copy(rawKey) : false;
  }

  return {
    adminUserApiKeys,
    adminUserApiKeyLoading,
    clearAdminUserApiKeys,
    hideAdminUserApiKey,
    revealAdminUserApiKey,
    copyAdminUserApiKey,
  };
}

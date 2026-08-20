import { inject } from "vue";

/** 管理门户页面共享的组合状态键，避免把数十个稳定字段逐层转发。 */
export const PORTAL_WORKSPACE_KEY = Symbol("llmctl-portal-workspace");

/**
 * 返回组合根提供的门户状态和操作。
 *
 * @returns {Record<string, unknown>} 供页面模板直接使用的响应式状态与函数。
 * @throws {Error} 页面脱离门户组合根独立挂载时明确失败。
 */
export function usePortalWorkspaceContext() {
  const workspace = inject(PORTAL_WORKSPACE_KEY, null);
  if (!workspace) throw new Error("门户页面缺少共享工作区上下文");
  return workspace;
}

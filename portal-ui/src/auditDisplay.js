const ACTION_LABELS = Object.freeze({
  "admin.usage.detail.view": "管理员查看请求详情",
  "usage.detail.view": "用户查看请求详情",
  "admin/billing/reconcile": "结算并同步用量",
  "admin/users/key/reveal": "管理员查看用户 API Key",
  "admin/users/verification/approve": "管理员手动通过邮箱验证",
  "admin/users/verification/resend": "管理员补发验证邮件",
  "admin/users/pending/delete": "管理员删除未验证用户",
  "admin/users/update": "更新用户资料与权限",
  "admin/users/bulk-policy": "批量修改用户调用策略",
  "admin/permissions/reconcile": "同步全部用户权限",
  "admin/models/save": "保存公开模型配置",
  "admin/models/test": "测试公开模型",
  "admin/settings": "更新系统设置",
  "admin/smtp/test": "发送 SMTP 测试邮件",
  "admin/omniroute/assess": "评估 OmniRoute SQLite",
  "admin/omniroute/submit": "提交 OmniRoute 运维任务",
  "admin/omniroute/cancel": "请求取消 OmniRoute 运维任务",
  "key/reveal": "用户查看个人 API Key",
  "key/rotate": "用户轮换个人 API Key",
  "login.success": "用户登录成功",
  "login.failed": "用户登录失败",
  "register.email": "发送注册验证邮件",
  "verify.provision": "验证邮箱并开通账户",
});

/**
 * 把审计动作的内部标识翻译成管理员可理解的操作名称。
 *
 * @param {string} action 审计表中保存的稳定动作标识。
 * @returns {string} 已知动作的中文说明；未知动作保留原标识以便排错。
 */
export function auditActionLabel(action) {
  const normalized = String(action || "").trim();
  return ACTION_LABELS[normalized] || normalized || "未知操作";
}

/**
 * 把审计详情格式化为可完整阅读的文本，不截断未知字段。
 *
 * @param {unknown} detail 数据库返回的 JSON 字符串、普通文本或结构化值。
 * @returns {string} 缩进后的完整 JSON 或原始文本；空值返回明确说明。
 */
export function formatAuditDetail(detail) {
  if (detail === null || detail === undefined || detail === "") return "无额外详情";
  if (typeof detail !== "string") return JSON.stringify(detail, null, 2);
  try {
    return JSON.stringify(JSON.parse(detail), null, 2);
  } catch {
    return detail;
  }
}

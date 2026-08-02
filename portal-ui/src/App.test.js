import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const source = readFileSync(new URL('./App.vue', import.meta.url), 'utf8')

describe('LLMCtl portal contracts', () => {
  it('sends inference directly to the public v1 endpoint', () => {
    expect(source).toContain('`${dashboard.value.api_base}/chat/completions`')
    expect(source).not.toContain("api('chat")
  })

  it('supports native model mapping, multiple access rules and prices', () => {
    expect(source).toContain('public_model_id')
    expect(source).toContain('admin?.combos')
    expect(source).toContain('addModelAccess')
    expect(source).toContain('input_price')
    expect(source).toContain('reasoning_price')
  })

  it('keeps registration, SMTP, free resources and audit in the admin UI', () => {
    for (const marker of ['注册与 SMTP', '免费资源', '用户管理', '审计日志']) {
      expect(source).toContain(marker)
    }
    expect(source).toContain("scope: 'registration'")
    expect(source).toContain("scope: 'smtp'")
    expect(source).toContain('测试邮件使用当前表单内容，不必先保存')
  })

  it('gives immediate progress feedback for slow model operations', () => {
    expect(source).toContain('正在执行真实模型请求，最长约 60 秒')
    expect(source).toContain('正在测试模型、更新映射并同步权限')
    expect(source).toContain("operation===`model-test:${model.id}`")
    expect(source).toContain("kind !== 'working'")
  })

  it('paginates every growing catalog and ledger', () => {
    for (const key of ['user-models', 'user-grants', 'user-billing', 'admin-models', 'admin-free', 'admin-users', 'admin-groups', 'admin-billing', 'admin-audit']) {
      expect(source).toContain(`pageRows('${key}'`)
    }
    expect(source).toContain('第 ${props.page} / ${props.pages} 页 · 共 ${props.total} 条')
    expect(source).toContain("dashboard.usage.slice(0,8)")
  })

  it('keeps local administration visible when the AI gateway is degraded', () => {
    expect(source).toContain('admin?.gateway_error')
    expect(source).toContain("u.role==='user' && u.status==='active'")
    expect(source).toContain('用户、SMTP、账本和审计仍可查看')
  })

  it('uses LLMCtl product language instead of exposing implementation brands', () => {
    for (const marker of ['OmniRoute', '独立 SQLite', '企业 AI 门户', '公司模型']) {
      expect(source).not.toContain(marker)
    }
    expect(source).toContain('LLMCtl 模型服务门户')
    expect(source).toContain('模型服务，一个入口')
  })
})

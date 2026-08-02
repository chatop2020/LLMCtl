import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const source = readFileSync(new URL('./App.vue', import.meta.url), 'utf8')

describe('enterprise portal contracts', () => {
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
  })

  it('keeps local administration visible when OmniRoute is degraded', () => {
    expect(source).toContain('admin?.gateway_error')
    expect(source).toContain("u.role==='user' && u.status==='active'")
    expect(source).toContain('本地用户、SMTP、账本和审计仍可查看')
  })
})

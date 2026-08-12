import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'
import type { RunRecord } from '../src/types'

const password = process.env.DEMO_PASSWORD ?? 'harbor-demo-2026'
const critical = '@critical'

async function login(page: Page, username = 'admin@harbor.local') {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: /让 Agent 的每一步/ })).toBeVisible()
  await page.getByLabel('账号').fill(username)
  await page.getByLabel('演示密码').fill(password)
  await page.getByRole('button', { name: '进入控制塔' }).click()
  await expect(page.getByRole('button', { name: '新建事件' })).toBeVisible()
}

async function logout(page: Page) {
  await page.getByRole('button', { name: '退出登录' }).click()
  await expect(page.getByRole('button', { name: '进入控制塔' })).toBeVisible()
}

async function createLabRun(page: Page, faultKind: string): Promise<RunRecord> {
  const created = page.waitForResponse((response) => (
    response.url().endsWith('/api/runs')
    && response.request().method() === 'POST'
  ))
  await page.getByRole('button', { name: '新建事件' }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('tab', { name: '受控故障实验' }).click()
  await page.getByLabel('故障类型').selectOption(faultKind)
  await page.getByRole('button', { name: '注入故障并入队' }).click()
  const response = await created
  expect(response.ok()).toBe(true)
  return response.json() as Promise<RunRecord>
}

async function openRun(page: Page, run: RunRecord) {
  const selectedId = page.locator('.run-subline .mono')
  if (await selectedId.textContent().catch(() => null) === run.id) return
  await page.getByRole('button', { name: '总览' }).click()
  const recent = page.locator('.run-list-item').filter({ hasText: run.incident.title }).first()
  await expect(recent).toBeVisible()
  await recent.click()
  await expect(selectedId).toHaveText(run.id)
}

async function expectRunStatus(page: Page, label: string) {
  await expect(page.locator('.run-subline .status-pill')).toContainText(label, { timeout: 45_000 })
}

async function expectNoSeriousA11yViolations(page: Page) {
  const result = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze()
  if (result.violations.length > 0) {
    const summary = result.violations.map((violation) => ({
      id: violation.id,
      nodes: violation.nodes.map((node) => ({
        target: node.target.join(' '),
        message: node.any[0]?.message ?? node.failureSummary,
      })),
    }))
    throw new Error(`Accessibility violations:\n${JSON.stringify(summary, null, 2)}`)
  }
}

async function browserApi<T>(
  page: Page,
  path: string,
  init: { method?: string; body?: unknown } = {},
): Promise<{ status: number; body: T }> {
  return page.evaluate(async ({ requestPath, requestInit }) => {
    const token = window.sessionStorage.getItem('harbor.access_token')
    const response = await fetch(requestPath, {
      method: requestInit.method ?? 'GET',
      headers: {
        Authorization: `Bearer ${token}`,
        ...(requestInit.body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: requestInit.body === undefined ? undefined : JSON.stringify(requestInit.body),
    })
    return { status: response.status, body: await response.json() as T }
  }, { requestPath: path, requestInit: init })
}

test.describe.configure({ mode: 'serial' })

test(`${critical} 01 Login：身份登录、键盘边界和基础可访问性`, async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText(/API v3\./)).toBeVisible()
  await expectNoSeriousA11yViolations(page)
  await login(page)
  await expect(page.locator('.identity-chip').getByText('平台管理员', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: '新建事件' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(page.getByRole('button', { name: '关闭' })).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(page.getByRole('button', { name: '提交自由事件' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expectNoSeriousA11yViolations(page)
})

test(`${critical} 02 创建 Incident：自由输入被写入持久运行`, async ({ page }) => {
  await login(page)
  const title = `E2E 自由事件 ${Date.now()}`
  const created = page.waitForResponse((response) => response.url().endsWith('/api/runs') && response.request().method() === 'POST')
  await page.getByRole('button', { name: '新建事件' }).click()
  await page.getByLabel('事件标题').fill(title)
  await page.getByRole('button', { name: '提交自由事件' }).click()
  const response = await created
  const run = await response.json() as RunRecord

  expect(response.status()).toBe(202)
  expect(run.status).toBe('queued')
  await expect(page.getByRole('heading', { name: title })).toBeVisible()
  await expect(page.locator('.run-subline .mono')).toHaveText(run.id)
})

test(`${critical} 03 Run 状态变化：queued 最终进入 completed`, async ({ page }) => {
  await login(page)
  const run = await createLabRun(page, 'stale_cache')
  expect(run.status).toBe('queued')
  await expectRunStatus(page, '已完成')
  await expect(page.getByText('refresh_cache', { exact: true }).first()).toBeVisible()
})

test(`${critical} 04 Medium Risk Approval：独立负责人一票后恢复执行`, async ({ page }) => {
  await login(page)
  const run = await createLabRun(page, 'queue_backlog')
  await expectRunStatus(page, '等待人工审批')
  await expect(page.locator('.risk-line')).toContainText('中风险')
  await expect(page.getByText('0 / 1 票')).toBeVisible()

  await logout(page)
  await login(page, 'lead@harbor.local')
  await openRun(page, run)
  await page.getByRole('button', { name: /登记审批票/ }).click()
  await expectRunStatus(page, '已完成')
  await expect(page.getByText('scale_workers', { exact: true }).first()).toBeVisible()
})

test(`${critical} 05 High Risk Four-eyes Approval：两个独立主体形成 quorum`, async ({ page }) => {
  await login(page)
  const run = await createLabRun(page, 'expired_credential')
  await expectRunStatus(page, '等待人工审批')
  await expect(page.locator('.risk-line')).toContainText('高风险')
  await expect(page.getByText('0 / 2 票')).toBeVisible()

  await logout(page)
  await login(page, 'security@harbor.local')
  await openRun(page, run)
  await page.getByRole('button', { name: /登记审批票/ }).click()
  await expect(page.getByText('1 / 2 票')).toBeVisible()

  await logout(page)
  await login(page, 'approver@harbor.local')
  await openRun(page, run)
  await page.getByRole('button', { name: /登记审批票/ }).click()
  await expectRunStatus(page, '已完成')
  await expect(page.getByText('rotate_credential', { exact: true }).first()).toBeVisible()
})

test(`${critical} 06 Requester Cannot Approve：申请人自批被界面和服务端边界阻止`, async ({ page }) => {
  await login(page)
  await createLabRun(page, 'connection_pool_exhaustion')
  await expectRunStatus(page, '等待人工审批')
  await expect(page.getByText(/申请人不能审批/)).toBeVisible()
  await expect(page.getByRole('button', { name: /登记审批票/ })).toBeDisabled()

  await page.getByRole('button', { name: '安全取消' }).click()
  await expectRunStatus(page, '已取消')
})

test(`${critical} 07 Stale Request：旧 expected_version 返回 409 且不能覆盖当前运行`, async ({ page }) => {
  await login(page)
  const run = await createLabRun(page, 'queue_backlog')
  await expectRunStatus(page, '等待人工审批')
  const versionText = await page.locator('.version-chip').textContent()
  const staleVersion = Number(versionText?.match(/\d+/)?.[0])
  expect(Number.isInteger(staleVersion)).toBe(true)

  await logout(page)
  await login(page, 'lead@harbor.local')
  await openRun(page, run)
  const firstDecision = page.waitForResponse((response) => response.url().includes(`/api/runs/${run.id}/decision`))
  await page.getByRole('button', { name: /登记审批票/ }).click()
  expect((await firstDecision).status()).toBe(200)

  const stale = await browserApi<{ detail: string }>(page, `/api/runs/${run.id}/decision`, {
    method: 'POST',
    body: { decision: 'approve', note: 'stale browser request', expected_version: staleVersion },
  })
  expect(stale.status).toBe(409)
  expect(stale.body.detail).toContain('版本冲突')
  const current = await browserApi<RunRecord>(page, `/api/runs/${run.id}`)
  expect(current.body.version).toBeGreaterThan(staleVersion)
})

test(`${critical} 08 Cancel：审批等待态可在安全检查点取消`, async ({ page }) => {
  await login(page)
  await createLabRun(page, 'queue_backlog')
  await expectRunStatus(page, '等待人工审批')
  await page.getByRole('button', { name: '安全取消' }).click()
  await expectRunStatus(page, '已取消')
  await expect(page.getByRole('button', { name: '安全取消' })).toHaveCount(0)
})

test(`${critical} 09 Retry：失败运行携带当前 CAS 版本重新入队`, async ({ page }) => {
  await login(page, 'lead@harbor.local')
  const runs = await browserApi<RunRecord[]>(page, '/api/runs')
  expect(runs.body.length).toBeGreaterThan(0)
  let mockedRun = JSON.parse(JSON.stringify(runs.body[0])) as RunRecord
  mockedRun.status = 'failed'
  mockedRun.current_node = 'diagnose'
  mockedRun.error_code = 'E2E_INJECTED_FAILED_VIEW'
  mockedRun.error_detail = 'Browser contract fixture; backend retry semantics are covered by an integration test.'
  mockedRun.version += 100
  mockedRun.attempt = 1
  mockedRun.max_attempts = Math.max(3, mockedRun.max_attempts)
  const expectedVersion = mockedRun.version
  let retryCalled = false

  await page.route('**/api/runs', async (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([mockedRun]) })
  })
  await page.route(`**/api/runs/${mockedRun.id}/jobs`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  })
  await page.route(`**/api/runs/${mockedRun.id}/retry?*`, async (route) => {
    const url = new URL(route.request().url())
    expect(url.searchParams.get('expected_version')).toBe(String(expectedVersion))
    expect(route.request().method()).toBe('POST')
    retryCalled = true
    mockedRun = { ...mockedRun, status: 'queued', version: mockedRun.version + 1, error_code: null, error_detail: null }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(mockedRun) })
  })

  await page.reload()
  await expect(page.getByRole('button', { name: '失败重试' })).toBeVisible()
  await page.getByRole('button', { name: '失败重试' }).click()
  await expectRunStatus(page, '已入队')
  expect(retryCalled).toBe(true)
})

test(`${critical} 10 Evidence：检索、现场观测、工具结果与独立复验同时可见`, async ({ page }) => {
  await login(page)
  await createLabRun(page, 'stale_cache')
  await expectRunStatus(page, '已完成')
  await expect(page.locator('.observation-list article')).not.toHaveCount(0)
  await expect(page.locator('.source-list article')).not.toHaveCount(0)
  await expect(page.locator('.tool-result-list').getByText('refresh_cache', { exact: true }).first()).toBeVisible()
  await expect(page.locator('.verification-list').getByText(/验证通过/).first()).toBeVisible()
  await expectNoSeriousA11yViolations(page)
})

test('full-matrix: ordinary production incident records Prometheus evidence and never writes', async ({ page }) => {
  await login(page)
  await page.getByRole('button', { name: '新建事件' }).click()
  await page.getByLabel('事件标题').fill(`payment-api 生产只读观测 ${Date.now()}`)
  await page.getByRole('button', { name: '提交自由事件' }).click()

  await expectRunStatus(page, '已转人工')
  await expect(page.getByText('query_prometheus_slo', { exact: true }).first()).toBeVisible()
  await expect(page.getByText(/未配置生产写连接器|没有返回可归因指标/).first()).toBeVisible()
  await expect(page.locator('.tool-result-list').getByText(/refresh_cache|restart_service|scale_workers|rotate_credential/)).toHaveCount(0)
})

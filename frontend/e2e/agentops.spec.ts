import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'


const password = process.env.DEMO_PASSWORD ?? 'harbor-demo-2026'

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

async function createLabRun(page: Page, faultKind: string) {
  await page.getByRole('button', { name: '新建事件' }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('tab', { name: '受控故障实验' }).click()
  await page.getByLabel('故障类型').selectOption(faultKind)
  await page.getByRole('button', { name: '注入故障并入队' }).click()
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

test.describe.configure({ mode: 'serial' })

test('login, responsive shell and modal keyboard boundary are accessible', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText(/API v3\.3\.0/)).toBeVisible()
  await expectNoSeriousA11yViolations(page)
  await login(page)
  await expectNoSeriousA11yViolations(page)

  await page.getByRole('button', { name: '新建事件' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await expect(page.getByRole('button', { name: '关闭' })).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(page.getByRole('button', { name: '提交自由事件' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  expect(overflow).toBeLessThanOrEqual(1)
})

test('ordinary production incident records Prometheus evidence and never writes', async ({ page }) => {
  await login(page)
  await page.getByRole('button', { name: '新建事件' }).click()
  await page.getByLabel('事件标题').fill('payment-api 生产只读观测验证')
  await page.getByRole('button', { name: '提交自由事件' }).click()

  await expect(page.getByRole('heading', { name: 'payment-api 生产只读观测验证' })).toBeVisible()
  await expect(page.locator('.status-pill')).toContainText('已转人工', { timeout: 30_000 })
  await expect(page.getByText('query_prometheus_slo', { exact: true }).first()).toBeVisible()
  await expect(page.getByText(/未配置生产写连接器|没有返回可归因指标/).first()).toBeVisible()
  await expect(page.getByText(/prometheus:9090\/api\/v1\/query/).first()).toBeVisible()
  await expect(page.locator('.tool-result-list').getByText(/refresh_cache|restart_service|scale_workers|rotate_credential/)).toHaveCount(0)
  await expectNoSeriousA11yViolations(page)
})

test('browser proves low-risk automation and two-subject high-risk quorum', async ({ page }) => {
  await login(page)
  await createLabRun(page, 'stale_cache')
  await expect(page.locator('.status-pill')).toContainText('已完成', { timeout: 30_000 })
  await expect(page.getByText('refresh_cache', { exact: true }).first()).toBeVisible()

  await createLabRun(page, 'expired_credential')
  await expect(page.locator('.status-pill')).toContainText('等待人工审批', { timeout: 30_000 })
  await expect(page.getByText(/申请人不能审批/)).toBeVisible()
  await expect(page.getByRole('button', { name: /登记审批票/ })).toBeDisabled()
  await expectNoSeriousA11yViolations(page)

  await logout(page)
  await login(page, 'security@harbor.local')
  await expect(page.getByRole('heading', { name: /partner-gateway/ })).toBeVisible()
  await page.getByRole('button', { name: /登记审批票/ }).click()
  await expect(page.getByText('1 / 2 票')).toBeVisible()

  await logout(page)
  await login(page, 'approver@harbor.local')
  await page.getByRole('button', { name: /登记审批票/ }).click()
  await expect(page.locator('.status-pill')).toContainText('已完成', { timeout: 30_000 })
  await expect(page.getByText('rotate_credential', { exact: true }).first()).toBeVisible()
})

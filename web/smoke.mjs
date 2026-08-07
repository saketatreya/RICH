import { chromium } from 'playwright'

const baseUrl = process.argv[2] || 'http://127.0.0.1:8765'
const screenshotPath = process.argv[3] || '/tmp/rich-canvas-smoke.png'
const projectId = `project.browser-smoke-${Date.now()}`
const errors = []

const browser = await chromium.launch({ args: ['--no-sandbox'] })
try {
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
  })
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`)
  })
  page.on('pageerror', (error) => errors.push(`page: ${error.message}`))
  page.on('requestfailed', (request) => {
    errors.push(`request: ${request.method()} ${request.url()} ${request.failure()?.errorText}`)
  })

  const response = await page.goto(baseUrl, { waitUntil: 'networkidle' })
  if (!response?.ok()) {
    throw new Error(`Canvas returned HTTP ${response?.status() ?? 'no response'}`)
  }
  await page.getByText('software development compiler', { exact: true }).waitFor()
  await page.getByText('Control plane online', { exact: true }).waitFor()

  const health = await page.evaluate(async () => {
    const result = await fetch('/v2/health')
    return { status: result.status, body: await result.json() }
  })
  if (health.status !== 200 || health.body.api_version !== 'v2') {
    throw new Error(`Unexpected health response: ${JSON.stringify(health)}`)
  }

  await page.getByLabel('Stable project id').fill(projectId)
  await page.getByLabel('Project name').fill('Browser Smoke Project')
  await page.getByRole('button', { name: 'Create', exact: true }).click()
  await page.getByText(`Created ${projectId}. Intent compilation is ready.`, {
    exact: true,
  }).waitFor()

  const durableProject = await page.evaluate(async (id) => {
    const result = await fetch(`/v2/projects/${encodeURIComponent(id)}`)
    return { status: result.status, body: await result.json() }
  }, projectId)
  if (
    durableProject.status !== 200
    || durableProject.body.project?.id !== projectId
    || durableProject.body.project?.current_revision !== 0
  ) {
    throw new Error(`Project was not durable: ${JSON.stringify(durableProject)}`)
  }

  const rejectedResponse = await page.request.post(
    new URL('/v2/projects', baseUrl).href,
    {
      data: {
        project_id: 'project.must-not-exist',
        name: 'Rejected mutation',
      },
    },
  )
  const missingIdempotency = {
    status: rejectedResponse.status(),
    body: await rejectedResponse.json(),
  }
  if (
    missingIdempotency.status !== 428
    || missingIdempotency.body.error !== 'IdempotencyKeyRequired'
  ) {
    throw new Error(
      `Mutation guard did not fail closed: ${JSON.stringify(missingIdempotency)}`,
    )
  }

  await page.screenshot({ path: screenshotPath, fullPage: true })
  if (errors.length) {
    throw new Error(`Browser errors:\n${errors.join('\n')}`)
  }
  console.log(
    JSON.stringify({
      ok: true,
      baseUrl,
      projectId,
      health: health.body,
      screenshotPath,
    }),
  )
} finally {
  await browser.close()
}

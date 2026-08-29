// M6 drive: the software comes out — the ZIP downloads, the repository appears.
//
// A real browser against a running `rich serve`, over a project whose run
// has already succeeded (web/drive/seed-release.py writes it, and the seed's
// facts — ids, the release digest, a bare repository to push to — into
// .rich/drive-m6/seed.json). The preview step needs Neon and Vercel
// credentials and reports itself skipped without them; the card says so.
//
//   python web/drive/seed-release.py .rich/drive-m6
//   rich serve --state-dir .rich/drive-m6/state --port 8793 --route none &
//   RICH_URL=http://127.0.0.1:8793 RICH_SEED=.rich/drive-m6/seed.json node web/drive/m6-release.mjs

import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { chromium } from 'playwright'

const base = process.env.RICH_URL || 'http://127.0.0.1:8793'
const seed = JSON.parse(readFileSync(process.env.RICH_SEED || '.rich/drive-m6/seed.json', 'utf8'))
const shots = process.env.RICH_SHOTS || '.rich/drive-m6'

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
const page = await context.newPage()
page.setDefaultTimeout(15_000)

let failures = 0
let skipped = 0
const step = async (name, run) => {
  process.stdout.write(`- ${name} … `)
  try {
    const outcome = await run()
    if (outcome === 'skipped') {
      skipped += 1
      console.log('skipped')
    } else {
      console.log('ok')
    }
  } catch (error) {
    failures += 1
    console.log('FAILED')
    console.log(`  ${String(error.message || error).split('\n')[0]}`)
    await page.screenshot({ path: `${shots}/failed-${name.replace(/\W+/g, '-')}.png` })
  }
}

await step('open the canvas and load the project', async () => {
  await page.goto(base)
  await page.getByText('Control plane online').waitFor()
  await page.locator('.plane-project-chip', { hasText: seed.project_id }).click()
  await page.getByText(`Loaded ${seed.project_id}`).waitFor()
  await page.getByText('Compiled build plan').waitFor()
})

await step('download the release ZIP: the exact verified bytes, digest in the header', async () => {
  const link = page.getByRole('link', { name: 'Download release ZIP' })
  await link.waitFor()
  const href = await link.getAttribute('href')
  const response = await page.request.get(new URL(href, base).toString())
  assert.equal(response.status(), 200)
  assert.equal(response.headers()['x-rich-release-digest'], seed.release_digest)
  const body = await response.body()
  assert.equal(createHash('sha256').update(body).digest('hex'), seed.release_digest)
  assert.match(response.headers()['content-disposition'] || '', /attachment; filename="rich-release-/)
})

await step('push to a repository: one commit, the snapshot digest in its message', async () => {
  await page.getByLabel('GitHub repository').fill(seed.remote)
  const create = page.getByLabel('Create it if missing')
  assert.equal(await create.isDisabled(), true, 'create is offered only for github.com')
  await page.getByRole('button', { name: 'Push', exact: true }).click()
  await page.locator('.plane-push-list li').first().waitFor({ timeout: 60_000 })
  const pushes = await (await page.request.get(`${base}/v1/runs/${encodeURIComponent(seed.run_id)}/repository-pushes`)).json()
  assert.equal(pushes.pushes.length, 1)
  const push = pushes.pushes[0]
  assert.equal(push.snapshot_digest, seed.release_digest)
  const shown = await page.locator('.plane-push-list li').first().innerText()
  assert.ok(shown.includes(push.commit_sha.slice(0, 12)), 'the receipt names the commit')
  const head = execFileSync('git', ['-C', seed.bare, 'rev-parse', 'main'], { encoding: 'utf8' }).trim()
  assert.equal(head, push.commit_sha)
  const message = execFileSync('git', ['-C', seed.bare, 'log', '-1', '--format=%B', 'main'], { encoding: 'utf8' })
  assert.ok(message.includes(`sha256:${seed.release_digest}`))
  const files = execFileSync('git', ['-C', seed.bare, 'ls-tree', '-r', '--name-only', 'main'], { encoding: 'utf8' })
  assert.ok(files.includes('package.json'), 'the pushed tree is the scaffolded application')
})

await step('push again: the same commit, reported as already current', async () => {
  await page.getByRole('button', { name: 'Push', exact: true }).click()
  await page.locator('.plane-push-list li').nth(1).waitFor({ timeout: 60_000 })
  const second = await page.locator('.plane-push-list li').nth(1).innerText()
  assert.ok(second.includes('already current'))
  const count = execFileSync('git', ['-C', seed.bare, 'rev-list', '--count', 'main'], { encoding: 'utf8' }).trim()
  assert.equal(count, '1')
})

await step('preview: a URL opens', async () => {
  if (!process.env.NEON_API_TOKEN || !process.env.VERCEL_TOKEN) return 'skipped'
  throw new Error('preview drive not written yet: credentials present but no steps')
})

await page.screenshot({ path: `${shots}/m6-final.png`, fullPage: true })
await browser.close()
if (failures) {
  console.log(`\nM6 drive: ${failures} step(s) failed`)
  process.exit(1)
}
console.log(`\nM6 drive: every step held${skipped ? ` (${skipped} skipped without credentials)` : ''}`)

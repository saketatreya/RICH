// M4 drive: amend a requirement, see what it costs, rebuild only that.
//
// A real browser against `rich serve` with a model route: the example is
// built once, one quality constraint is amended, the spec and the
// architecture are approved again, the canvas shows what the amendment costs
// before any money moves, and "Apply and build" rebuilds only the components
// that serve the amended requirement -- the untouched one replays its
// generation, which the durable events show as the absence of a model
// attempt. Two live builds: minutes and a little quota.
//
//   rich serve --state-dir .rich/drive-m3/state --port 8792 --route claude-code &
//   RICH_URL=http://127.0.0.1:8792 RICH_DRIVE_MINUTES=30 node web/drive/m4-amend.mjs

import assert from 'node:assert/strict'
import { chromium } from 'playwright'

const base = process.env.RICH_URL || 'http://127.0.0.1:8792'
const minutes = Number(process.env.RICH_DRIVE_MINUTES || 30)
const shots = process.env.RICH_SHOTS || '.rich/drive-m4'
const stamp = Date.now().toString(36)
let projectId = `project.drive-m4-${stamp}`
const projectName = `Drive M4 ${stamp}`
const projectIdByName = async (name) => {
  const response = await fetch(`${base}/v1/projects`)
  const found = (await response.json()).projects.find((item) => item.name === name)
  if (!found) throw new Error(`no project named ${name}`)
  return found.id
}
const amended = 'All actions are reachable with the keyboard alone, and focus order follows reading order.'

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } })
const page = await context.newPage()
page.setDefaultTimeout(20_000)

let failures = 0
let stopped = false
const step = async (name, run) => {
  if (stopped) {
    console.log(`- ${name} … skipped (an earlier step failed)`)
    return
  }
  process.stdout.write(`- ${name} … `)
  try {
    await run()
    console.log('ok')
  } catch (error) {
    failures += 1
    stopped = true
    console.log('FAILED')
    console.log(`  ${String(error.message || error).split('\n')[0]}`)
    await page.screenshot({ path: `${shots}/failed-${name.replace(/\W+/g, '-')}.png`, fullPage: true })
  }
}

const projectForm = () => page.locator('.plane-project-form')
const runs = async () => {
  const response = await fetch(`${base}/v1/projects/${encodeURIComponent(projectId)}/runs`)
  return (await response.json()).runs
}
const latestRun = async () => {
  const all = await runs()
  return all[all.length - 1] ?? null
}
const executionError = async () => {
  const run = await latestRun()
  if (!run) return null
  const response = await fetch(`${base}/v1/runs/${encodeURIComponent(run.id)}/timeline`)
  const lines = (await response.json()).lines
  const error = [...lines].reverse().find((line) => line.text.includes('run.execution_error'))
  return error ? error.text.split('\n').pop().trim() : null
}
const settle = async () => {
  const deadline = Date.now() + minutes * 60_000
  let status = (await latestRun())?.status ?? null
  while (!['succeeded', 'failed', 'canceled'].includes(status ?? '') && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10_000))
    status = (await latestRun())?.status ?? null
    const died = await executionError()
    assert.equal(died, null, `the build could not start: ${died}`)
    process.stdout.write('.')
  }
  console.log(` ${status}`)
  return status
}
const allEvents = async (runId) => {
  const collected = []
  let after = 0
  for (;;) {
    const response = await fetch(`${base}/v1/runs/${encodeURIComponent(runId)}/events?after=${after}`)
    const batch = (await response.json()).events
    if (!batch.length) break
    collected.push(...batch)
    after = batch[batch.length - 1].sequence
  }
  return collected
}
const usage = async (runId) => (await (await fetch(`${base}/v1/runs/${encodeURIComponent(runId)}/usage`)).json())
const modelAttemptsFor = (events, node) =>
  events.filter(
    (event) => event.event_type === 'model.attempt.started' && String(event.task_id ?? '').endsWith(`:implement:${node}`),
  ).length

const approveSpec = async () => {
  await page.getByRole('button', { name: /Write the specification/ }).click()
  await page.getByText('The specification is ready for your approval').waitFor()
  await page.getByRole('button', { name: /^Approve/ }).first().click()
  await page.getByText('Product specification approved.').waitFor()
}
// The architect, not the deterministic fallback. The fallback allocates every
// requirement to every layer -- that is what a layered decomposition means --
// so under it no component can ever be untouched by an amendment and the cost
// this drive exists to check is always "all of them". Driving the fallback and
// asserting locality was asserting something the shape cannot provide.
const approveArchitectedDesign = async () => {
  await page.getByRole('button', { name: 'Draft with the architect →' }).click()
  await page
    .getByRole('button', { name: 'Apply as a new revision' })
    .waitFor({ timeout: 300_000 })
  await page.getByRole('button', { name: 'Apply as a new revision' }).click()
  await page.getByText('The architecture is ready for your approval').waitFor({
    timeout: 60_000,
  })
  await page.getByRole('button', { name: /^Approve/ }).first().click()
  await page.getByText('Architecture approved.').waitFor()
}

let firstRun = null
let secondRun = null

await step('create a project and approve the example specification', async () => {
  await page.goto(base)
  await page.getByText('Control plane online').waitFor()
    await projectForm().getByLabel('Project name').fill(projectName)
  await page.getByRole('button', { name: 'Create', exact: true }).click()
  await page.getByText(`Created ${projectName}`).waitFor()
  projectId = await projectIdByName(projectName)
  await page.getByRole('button', { name: 'Start from an example' }).click()
  await approveSpec()
})

await step('the architect designs the decomposition and it is approved', async () => {
  await approveArchitectedDesign()
})

await step('build the example with a $10 ceiling', async () => {
  await page.getByLabel('Spending ceiling · USD').fill('10.00')
  await page.getByRole('button', { name: 'Build →' }).click()
  await page.getByText(/Build started with a 10.00 USD ceiling/).waitFor({ timeout: 60_000 })
})

await step(`the first build succeeds within ${minutes} minutes`, async () => {
  const status = await settle()
  firstRun = await latestRun()
  assert.equal(status, 'succeeded', `the example did not build: run is ${status}`)
})

await step('amend one quality constraint and approve the new specification', async () => {
  await page.getByLabel('Quality constraints 1 statement').fill(amended)
  await approveSpec()
})

await step('the architecture is drafted again and approved', async () => {
  await approveArchitectedDesign()
})

await step('the cost is shown before any money moves: domain is untouched', async () => {
  await page.getByText('What this amendment costs').waitFor({ timeout: 60_000 })
  const chip = await page.locator('.plane-panel', { hasText: 'What this amendment costs' }).locator('.chip').first().innerText()
  assert.match(chip, /^\d+ of \d+\s+stale/, `chip says ${JSON.stringify(chip)}`)
  const untouched = page.locator('.plane-panel', { hasText: 'What this amendment costs' }).getByText('Untouched')
  await untouched.waitFor()
  const panel = await page.locator('.plane-panel', { hasText: 'What this amendment costs' }).innerText()
  assert.ok(/Untouched[\s\S]*domain/.test(panel), 'domain is not listed as untouched')
  assert.ok(/Rebuilt[\s\S]*web/.test(panel), 'web is not listed as rebuilt')
  await page.screenshot({ path: `${shots}/m4-cost.png`, fullPage: true })
})

await step('Apply and build: the stale components are forgotten and a second build starts', async () => {
  await page.getByRole('button', { name: 'Apply and build →' }).click()
  await page.getByText(/Build started with a 10.00 USD ceiling/).waitFor({ timeout: 60_000 })
  const all = await runs()
  assert.equal(all.length, 2, `expected two runs, found ${all.length}`)
})

await step(`the second build succeeds within ${minutes} minutes`, async () => {
  const status = await settle()
  secondRun = await latestRun()
  assert.equal(status, 'succeeded', `the amended build did not succeed: run is ${status}`)
})

await step('the untouched component replayed; the rebuilt one was generated again; every gate ran', async () => {
  const before = await allEvents(firstRun.id)
  const after = await allEvents(secondRun.id)
  assert.ok(modelAttemptsFor(before, 'domain') >= 1, 'the first build generated domain')
  assert.equal(modelAttemptsFor(after, 'domain'), 0, 'domain was generated again instead of replayed')
  assert.ok(modelAttemptsFor(after, 'web') >= 1, 'web was not generated again')
  const gates = after.filter(
    (event) => event.event_type === 'evidence.recorded' && String(event.task_id ?? '').endsWith(':implement:domain'),
  )
  assert.ok(gates.length >= 2, `domain's gates did not run again (${gates.length} evidence events)`)
  const first = await usage(firstRun.id)
  const second = await usage(secondRun.id)
  assert.ok(
    Number(second.used.model_attempts) < Number(first.used.model_attempts),
    `second build used ${second.used.model_attempts} model attempts, first ${first.used.model_attempts}`,
  )
  console.log(
    `\n  first build: ${first.used.model_attempts} attempts, $${first.used.cost_usd}; second: ${second.used.model_attempts} attempts, $${second.used.cost_usd}`,
  )
  await page.getByRole('link', { name: 'Download release ZIP' }).waitFor()
})

await page.screenshot({ path: `${shots}/m4-final.png`, fullPage: true })
await browser.close()
if (failures) {
  console.log(`\nM4 drive: ${failures} step(s) failed`)
  process.exit(1)
}
console.log('\nM4 drive: every step held')

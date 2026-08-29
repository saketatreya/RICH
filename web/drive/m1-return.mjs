// M1 drive: close the tab, reopen, everything is there.
//
// Runs a real browser against a running `rich serve`. Every step is something
// a person would do; every assertion is something they would see. This is the
// acceptance test for milestone M1 in docs/program.md, not a unit test: it
// fails when the product fails, whichever layer is to blame.
//
//   rich serve --state-dir .rich/drive-m1/state --port 8790 &
//   RICH_URL=http://127.0.0.1:8790 node web/drive/m1-return.mjs

import assert from 'node:assert/strict'
import { chromium } from 'playwright'

const base = process.env.RICH_URL || 'http://127.0.0.1:8790'
const stamp = Date.now().toString(36)
const projectId = `project.drive-${stamp}`
const otherProjectId = `${projectId}-b`
// Kept free of the words the adaptive interview reacts to, so the demo draft
// still compiles without a policy answer.
const goal = `Drive ${stamp}: an interview whose words survive a reload.`

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
const page = await context.newPage()
page.setDefaultTimeout(15_000)

let failures = 0
const step = async (name, run) => {
  process.stdout.write(`- ${name} … `)
  try {
    await run()
    console.log('ok')
  } catch (error) {
    failures += 1
    console.log('FAILED')
    console.log(`  ${String(error.message || error).split('\n')[0]}`)
    await page.screenshot({ path: `.rich/drive-m1/failed-${name.replace(/\W+/g, '-')}.png` })
  }
}

const eventually = async (check, what, timeoutMs = 10_000) => {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    if (await check()) return
    await new Promise((resolve) => setTimeout(resolve, 300))
  }
  throw new Error(`timed out waiting for ${what}`)
}

// The project form's fields, scoped: once a spec exists, the compiled scenario list
// renders its oracle's "Project name" locator as text, and a bare label match is
// a substring match.
const projectForm = () => page.locator('.plane-project-form')

const serverDraft = async () => {
  const response = await fetch(`${base}/v1/projects/${encodeURIComponent(projectId)}/interview`)
  return (await response.json()).draft
}

await step('open the canvas', async () => {
  await page.goto(base)
  await page.getByText('Control plane online').waitFor()
})

await step('create a project', async () => {
  await projectForm().getByLabel('Stable project id').fill(projectId)
  await projectForm().getByLabel('Project name').fill('Drive M1')
  await page.getByRole('button', { name: 'Create', exact: true }).click()
  await page.getByText(`Created ${projectId}`).waitFor()
})

await step('type into the interview; the server holds it within a second', async () => {
  await page.getByLabel('Outcome and problem').fill(goal)
  await eventually(
    async () => (await serverDraft())?.document?.form?.goal === goal,
    'the draft to reach the server',
  )
})

await step('reload: the interview is still there', async () => {
  await page.reload()
  await page.getByText(`Loaded ${projectId}`).waitFor()
  assert.equal(await page.getByLabel('Outcome and problem').inputValue(), goal)
})

await step('compile the specification and approve it', async () => {
  await page.getByRole('button', { name: /Compile product specification/ }).click()
  await page.getByText('Intent compiled into a versioned spec').waitFor()
  await page.getByRole('button', { name: /^Approve/ }).first().click()
  await page.getByText('Product specification approved.').waitFor()
})

await step('reload: the approved specification is still there', async () => {
  await page.reload()
  await page.getByText(`Loaded ${projectId}`).waitFor()
  await page.getByText('Specification approved').waitFor()
  await page.getByText('product specification', { exact: false }).first().waitFor()
  assert.equal(await page.getByLabel('Outcome and problem').inputValue(), goal)
})

await step('switch to another project, then back: each restores intact', async () => {
  await projectForm().getByLabel('Stable project id').fill(otherProjectId)
  await projectForm().getByLabel('Project name').fill('Drive M1 B')
  await page.getByRole('button', { name: 'Create', exact: true }).click()
  await page.getByText(`Created ${otherProjectId}`).waitFor()
  assert.equal(await page.getByText('Specification approved').count(), 0)
  await page.locator('.plane-project-chip', { hasText: projectId }).click()
  await page.getByText(`Loaded ${projectId}`).waitFor()
  await page.getByText('Specification approved').waitFor()
  assert.equal(await page.getByLabel('Outcome and problem').inputValue(), goal)
})

await step('a fresh tab after closing this one lands on the same project', async () => {
  const fresh = await context.newPage()
  await fresh.goto(base)
  await fresh.getByText(`Loaded ${projectId}`).waitFor()
  await fresh.getByText('Specification approved').waitFor()
  await fresh.close()
})

await browser.close()
if (failures) {
  console.log(`M1 drive: ${failures} step(s) failed`)
  process.exit(1)
}
console.log('M1 drive: every step held')

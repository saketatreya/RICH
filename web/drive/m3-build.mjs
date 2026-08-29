// M3 drive: Build with a dollar ceiling, watch the cost climb, watch the run
// settle -- and whichever way it settles, read what happened as a person.
//
// This is the first build driven from the canvas. It needs a server with a
// model route (the coding worker is a real model) and Bubblewrap; the cold
// bootstrap downloads ~2 GiB, so the run can take twenty minutes.
//
//   PYTHONPATH=src python -m richbuild.cli serve --state-dir .rich/drive-m3/state --port 8792 &
//   RICH_URL=http://127.0.0.1:8792 RICH_DRIVE_MINUTES=30 node web/drive/m3-build.mjs

import assert from 'node:assert/strict'
import { chromium } from 'playwright'

const base = process.env.RICH_URL || 'http://127.0.0.1:8792'
const minutes = Number(process.env.RICH_DRIVE_MINUTES || 30)
const stamp = Date.now().toString(36)
const projectId = `project.drive-m3-${stamp}`

const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } })
const page = await context.newPage()
page.setDefaultTimeout(20_000)

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
    await page.screenshot({ path: `.rich/drive-m3/failed-${name.replace(/\W+/g, '-')}.png`, fullPage: true })
  }
}

const projectForm = () => page.locator('.plane-project-form')
const runStatus = async () => {
  const response = await fetch(`${base}/v1/projects/${encodeURIComponent(projectId)}/runs`)
  const runs = (await response.json()).runs
  return runs[runs.length - 1]?.status ?? null
}

await step('create a project and approve the example specification', async () => {
  await page.goto(base)
  await page.getByText('Control plane online').waitFor()
  await projectForm().getByLabel('Stable project id').fill(projectId)
  await projectForm().getByLabel('Project name').fill('Drive M3')
  await page.getByRole('button', { name: 'Create', exact: true }).click()
  await page.getByText(`Created ${projectId}`).waitFor()
  await page.getByRole('button', { name: 'Start from an example' }).click()
  await page.getByRole('button', { name: /Compile product specification/ }).click()
  await page.getByText('Intent compiled into a versioned spec').waitFor()
  await page.getByRole('button', { name: /^Approve/ }).first().click()
  await page.getByText('Product specification approved.').waitFor()
})

await step('plan the architecture deterministically and approve it', async () => {
  // The deterministic plan, not the architect: this drive is about the build.
  await page.getByRole('button', { name: 'Use the deterministic plan' }).click()
  await page.getByText('Architecture compiled').waitFor()
  await page.getByRole('button', { name: /^Approve/ }).first().click()
  await page.getByText('Architecture approved.').waitFor()
})

await step('build with a $10 ceiling: one button, three durable steps', async () => {
  await page.getByLabel('Spending ceiling · USD').fill('10.00')
  await page.getByText('Up to 20 model attempts').waitFor()
  await page.getByRole('button', { name: 'Build →' }).click()
  await page.getByText(/Build started with a 10.00 USD ceiling/).waitFor({ timeout: 60_000 })
  await page.getByText('Built in').waitFor()
})

await step('the meter and the timeline are live while it builds', async () => {
  await page.getByLabel('Spending against the ceiling').waitFor()
  await page.getByText('$10.00').waitFor()
  await page.locator('.plane-timeline pre').first().waitFor({ timeout: 60_000 })
  const lines = await page.locator('.plane-timeline pre').allTextContents()
  assert.ok(lines.some((line) => line.includes('run.execution_requested') || line.includes('run.prepared')), lines.join('\n'))
})

await step(`the run settles within ${minutes} minutes`, async () => {
  const deadline = Date.now() + minutes * 60_000
  let status = await runStatus()
  while (!['succeeded', 'failed', 'canceled'].includes(status ?? '') && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10_000))
    status = await runStatus()
    process.stdout.write('.')
  }
  console.log(` ${status}`)
  assert.ok(['succeeded', 'failed'].includes(status ?? ''), `run is ${status}`)
})

await step('what happened is readable either way', async () => {
  const status = await runStatus()
  await page.reload()
  await page.getByText(`Loaded ${projectId}`).waitFor()
  await page.locator('.plane-meter-figures b').first().waitFor()
  const spent = await page.locator('.plane-meter-figures b').first().textContent()
  assert.notEqual(spent?.trim(), '$0.00', 'a build that ran must have spent something')
  if (status === 'succeeded') {
    await page.getByRole('link', { name: 'Download release ZIP' }).waitFor()
    await page.getByText('Release verified ✓').waitFor()
  } else {
    await page.getByText('A gate the model cannot touch said no.').waitFor()
    await page.getByRole('button', { name: 'Build again →' }).waitFor()
  }
})

await browser.close()
if (failures) {
  console.log(`M3 drive: ${failures} step(s) failed`)
  process.exit(1)
}
console.log('M3 drive: every step held')

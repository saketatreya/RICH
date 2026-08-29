// M2 drive, the no-model path: describe it in prose, get questions back, start
// from the example, edit a step through dropdowns, read it back as a sentence,
// compile, approve -- and after a reload the conversation is still there.
//
// The model-backed path is proven by tests/test_interviewer_live.py; this
// drive runs against a server with no model route (form-fallback), which is
// what a customer without a login sees, and it must still be usable.
//
//   PYTHONPATH=src python -m richbuild.cli serve --state-dir .rich/drive-m2/state --port 8791 &
//   RICH_URL=http://127.0.0.1:8791 node web/drive/m2-interview.mjs

import assert from 'node:assert/strict'
import { chromium } from 'playwright'

const base = process.env.RICH_URL || 'http://127.0.0.1:8791'
const stamp = Date.now().toString(36)
let projectId = `project.drive-m2-${stamp}`
const projectName = `Drive M2 ${stamp}`
const projectIdByName = async (name) => {
  const response = await fetch(`${base}/v1/projects`)
  const found = (await response.json()).projects.find((item) => item.name === name)
  if (!found) throw new Error(`no project named ${name}`)
  return found.id
}
const prose = `Drive ${stamp}: a reading list where I add books and mark the ones I finished.`

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
    await page.screenshot({ path: `.rich/drive-m2/failed-${name.replace(/\W+/g, '-')}.png` })
  }
}

const projectForm = () => page.locator('.plane-project-form')

await step('open the canvas and create a project', async () => {
  await page.goto(base)
  await page.getByText('Control plane online').waitFor()
    await projectForm().getByLabel('Project name').fill(projectName)
  await page.getByRole('button', { name: 'Create', exact: true }).click()
  await page.getByText(`Created ${projectName}`).waitFor()
  projectId = await projectIdByName(projectName)
})

await step('say what you want in prose; the interviewer answers', async () => {
  await page.getByLabel('Your message to the interviewer').fill(prose)
  await page.getByRole('button', { name: 'Send', exact: true }).click()
  await page.locator('.plane-chat-line.user').getByText(prose).waitFor()
  await page.locator('.plane-chat-line.interviewer').first().waitFor()
  // No model route on this server: the fixed questions, and it says so.
  await page.getByText('Before drafting, the interviewer asks').waitFor()
  await page.locator('.plane-chat-note').waitFor()
})

await step('start from the example and read a step as a sentence', async () => {
  await page.getByRole('button', { name: 'Start from an example' }).click()
  await page.getByText("Type ‘Example project’ into the field labelled ‘Project name’").waitFor()
  await page.getByText("Click the button named ‘Create project’").waitFor()
})

await step('add a step through the dropdown and see the sentence', async () => {
  const scenario = page.locator('.plane-scenario-card').first()
  await scenario.getByLabel('Scenario 1 steps · add a step').selectOption('reload')
  await scenario.locator('.plane-step-sentence', { hasText: 'Reload the page' }).waitFor()
  const count = await scenario.locator('.plane-step').count()
  assert.equal(count, 5)
})

await step('change a step\'s target through the controls', async () => {
  const scenario = page.locator('.plane-scenario-card').first()
  await scenario.getByLabel('Step 2 target').fill('Project title')
  await scenario.getByText("Type ‘Example project’ into the field labelled ‘Project title’").waitFor()
})

await step('compile the specification and approve it', async () => {
  await page.getByRole('button', { name: /Write the specification/ }).click()
  await page.getByText('The specification is ready for your approval').waitFor()
  // The compiled spec renders its steps as the same sentences.
  await page.getByText("Type ‘Example project’ into the field labelled ‘Project title’").first().waitFor()
  await page.getByRole('button', { name: /^Approve/ }).first().click()
  await page.getByText('Product specification approved.').waitFor()
})

await step('reload: the conversation and the draft are still there', async () => {
  await page.reload()
  await page.getByText(`Loaded ${projectName}`).waitFor()
  await page.locator('.plane-chat-line.user').getByText(prose).waitFor()
  await page.getByText('Specification approved').waitFor()
})

await browser.close()
if (failures) {
  console.log(`M2 drive: ${failures} step(s) failed`)
  process.exit(1)
}
console.log('M2 drive: every step held')

import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright')
const root = path.resolve(import.meta.dirname, '..')
await mkdir(path.join(root, 'test-results'), { recursive: true })
await mkdir(path.join(root, '.runtime'), { recursive: true })
const work = await mkdtemp(path.join(root, '.runtime', 'e2e-'))
const port = Number(process.env.KEJI_E2E_PORT || 8766)
const base = `http://127.0.0.1:${port}`
const python = process.env.KEJI_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')
const server = spawn(python, ['server/app.py', '--port', String(port), '--db', path.join(work, 'test.sqlite3')], { cwd: root, windowsHide: true, stdio: 'pipe' })
let serverLog = ''
server.stdout.on('data', data => { serverLog += data })
server.stderr.on('data', data => { serverLog += data })
server.on('error', error => { serverLog += error.message })
let browser
const checks = [], errors = []
const check = async (name, fn) => { await fn(); checks.push(name); console.log(`PASS ${name}`) }
try {
  for (let i = 0; i < 60; i++) {
    if (server.exitCode !== null) throw new Error(serverLog)
    try { if ((await fetch(`${base}/api/health`)).ok) break } catch { }
    if (i === 59) throw new Error('Test server did not start: ' + serverLog)
    await delay(100)
  }
  browser = await chromium.launch({ headless: true, ...(process.env.KEJI_BROWSER_CHANNEL ? { channel: process.env.KEJI_BROWSER_CHANNEL } : {}) })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${base}/replay`)
  await check('empty state and explicit demo import', async () => {
    await page.getByRole('button', { name: '填入合成示例' }).click()
    await page.getByRole('button', { name: '校验并预览字幕' }).click()
    await page.getByText('已识别 16 段字幕', { exact: false }).waitFor()
    assert.equal((await (await fetch(`${base}/api/catalog`)).json()).replays.length, 0)
    await page.getByRole('button', { name: '确认导入字幕', exact: true }).click()
    await page.getByRole('button', { name: '生成摘要', exact: true }).waitFor()
  })
  await check('asynchronous summary and real timestamp citation', async () => {
    await page.getByRole('button', { name: '生成摘要', exact: true }).click()
    await page.getByRole('heading', { name: '章节与知识点' }).waitFor()
    await page.locator('.citation-buttons button').first().click()
    await page.locator('.line.active').waitFor()
    const active = await page.locator('.line.active').textContent()
    assert.ok(active.length > 20)
    assert.equal((await (await fetch(`${base}/api/catalog`)).json()).tasks.length, 0)
  })
  await check('editable versions and retained original', async () => {
    await page.getByRole('button', { name: '编辑摘要', exact: true }).click()
    await page.locator('.edit-chapter textarea').first().fill('人工核对：正常路径包括上传、预览、确认保存。')
    await page.getByRole('button', { name: '保存为新版本' }).click()
    await page.getByText('编辑已保存为新版本', { exact: false }).waitFor()
    await page.route('**/api/replays/*/versions/1', route => route.abort())
    await page.getByLabel('摘要版本').selectOption('1')
    await page.getByRole('alert').filter({ hasText: '无法连接' }).waitFor()
    assert.equal(await page.getByLabel('摘要版本').inputValue(), '2')
    await page.unroute('**/api/replays/*/versions/1')
    await page.getByLabel('摘要版本').selectOption('1')
    await page.waitForFunction(() => document.querySelector('section[aria-label="回放摘要"] > p.meta')?.textContent.trim().startsWith('v1 ·'))
    await page.waitForFunction(() => [...document.querySelectorAll('button')].find(b => b.textContent === '编辑摘要')?.disabled)
    assert.ok(!(await page.locator('.summary-chapter').first().textContent()).includes('人工核对：'))
    await page.getByLabel('摘要版本').selectOption('latest')
    await page.getByText('人工核对：正常路径包括上传、预览、确认保存。', { exact: true }).waitFor()
  })
  await check('download includes actual source excerpts', async () => {
    const downloadPromise = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出摘要与引用' }).click()
    const download = await downloadPromise
    const target = path.join(work, download.suggestedFilename())
    await download.saveAs(target)
    const data = JSON.parse(await readFile(target, 'utf8'))
    assert.equal(data.version, 2)
    assert.ok(data.chapters[0].citations[0].text)
  })
  await check('confirmation creates one task and survives reload', async () => {
    await page.getByRole('button', { name: /^确认加入学习任务/ }).click()
    await page.getByRole('button', { name: '已加入学习任务', exact: true }).waitFor()
    await page.reload()
    await page.getByRole('button', { name: '已加入学习任务', exact: true }).waitFor()
    assert.equal((await (await fetch(`${base}/api/catalog`)).json()).tasks.length, 1)
  })
  await page.screenshot({ path: path.join(root, 'test-results/replay-desktop.png'), fullPage: true })
  await check('dashboard completion and version backlink', async () => {
    await page.getByRole('button', { name: '回到看板', exact: true }).click()
    await page.getByRole('checkbox', { name: /^完成学习任务：/ }).check()
    await page.getByLabel('筛选学习任务').selectOption('done')
    await page.getByRole('checkbox', { name: /^完成学习任务：/ }).waitFor()
    await page.reload()
    await page.getByLabel('筛选学习任务').selectOption('done')
    assert.ok(await page.getByRole('checkbox', { name: /^完成学习任务：/ }).isChecked())
    await page.getByRole('button', { name: '返回确认时的回放摘要 →' }).click()
    await page.getByRole('heading', { name: '章节与知识点' }).waitFor()
    assert.equal(await page.getByLabel('摘要版本').inputValue(), '2')
  })
  await check('materials import, deduplication and course filtering', async () => {
    await page.getByRole('button', { name: '资料收集', exact: false }).click()
    await page.getByText('批量导入资料清单', { exact: true }).click()
    const source = { title: '合成演示资料', url: 'https://example.org/course/se', locator: '第三阶段' }
    const bundle = { course: { id: 'stage3-demo', name: '软件工程 · 合成演示课程' }, materials: [{ title: '迭代验收说明（合成示例）', category: '课程说明', content: '演示资料导入、字幕整理、确认学习任务。', source }], deadlines: [{ title: '第三阶段演示准备（示例）', due: '2026-11-19T09:00:00+08:00', source }], schedules: [{ title: '软件工程课程（示例）', weekday: 4, startTime: '09:00', endTime: '10:50', location: '示例教室', rawText: '周四 09:00-10:50 示例教室', source }] }
    await page.getByRole('textbox', { name: '资料清单JSON' }).fill(JSON.stringify(bundle))
    await page.getByRole('button', { name: '确认导入清单' }).click()
    await page.getByText('导入完成：新增 3 条', { exact: false }).waitFor()
    await page.getByRole('button', { name: '确认导入清单' }).click()
    await page.getByText('未变化 3 条', { exact: false }).waitFor()
    await page.getByLabel('搜索资料').fill('迭代验收')
    await page.getByRole('heading', { name: '迭代验收说明（合成示例）' }).waitFor()
    await page.getByRole('button', { name: '查看历史版本' }).click()
    await page.locator('.version-list details').waitFor()
  })
  await check('dashboard timetable and deadlines', async () => {
    await page.getByRole('button', { name: '课程看板', exact: false }).click()
    await page.getByRole('heading', { name: /周四 09:00/ }).waitFor()
    await page.getByRole('heading', { name: '第三阶段演示准备（示例）' }).waitFor()
    await page.getByLabel('筛选学习任务').selectOption('all')
    await page.screenshot({ path: path.join(root, 'test-results/dashboard-desktop.png'), fullPage: true })
  })
  await check('768px layout and deep-link refresh', async () => {
    await page.setViewportSize({ width: 768, height: 1024 })
    await page.getByRole('button', { name: '整理课程回放', exact: true }).click()
    await page.getByRole('heading', { name: '章节与知识点' }).waitFor()
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    await page.screenshot({ path: path.join(root, 'test-results/replay-768.png'), fullPage: true })
    await page.reload()
    await page.getByRole('heading', { name: '章节与知识点' }).waitFor()
    await page.goto(`${base}/test/hello`)
    await page.getByRole('heading', { name: 'hello world', exact: true }).waitFor()
  })
  await check('no browser exceptions', async () => assert.deepEqual(errors, []))
  await writeFile(path.join(root, 'test-results/e2e-report.json'), JSON.stringify({ browser: await browser.version(), checks, passed: checks.length, errors }, null, 2))
} catch (error) {
  await writeFile(path.join(root, 'test-results/e2e-failure.txt'), String(error.stack) + '\n' + serverLog)
  console.error(error)
  process.exitCode = 1
} finally {
  await browser?.close()
  server.kill()
}

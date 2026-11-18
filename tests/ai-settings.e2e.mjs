import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'
import { createServer } from 'node:http'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright')
const root = path.resolve(import.meta.dirname, '..')
await mkdir(path.join(root, 'test-results'), { recursive: true })
await mkdir(path.join(root, '.runtime'), { recursive: true })
const work = await mkdtemp(path.join(root, '.runtime', 'ai-e2e-'))
const port = Number(process.env.KEJI_AI_E2E_PORT || 8767)
const base = `http://127.0.0.1:${port}`
const calls = []
const modelServer = createServer(async (req, res) => {
  const chunks = []
  for await (const chunk of req) chunks.push(chunk)
  const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : null
  calls.push({ method: req.method, path: req.url, body })
  res.setHeader('Content-Type', 'application/json')
  if (req.headers.authorization !== 'Bearer e2e-fake-key') { res.writeHead(401).end('{}'); return }
  if (req.url === '/v1/models') { res.end(JSON.stringify({ data: [{ id: 'summary-model' }] })); return }
  if (body?.model === 'simulate-error') { res.writeHead(429).end(JSON.stringify({ error: 'never show provider internals' })); return }
  if (req.url !== '/v1/chat/completions') { res.writeHead(404).end('{}'); return }
  const cues = JSON.parse(body.messages[1].content)
  const value = { chapters: [{ title: '需求与验收', summary: `模型归纳：${cues[0].text}`, cueIds: [body.model === 'bad-citation' ? 'invented' : cues[0].id] }] }
  res.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify(value) } }] }))
})
await new Promise(resolve => modelServer.listen(0, '127.0.0.1', resolve))
const modelBase = `http://127.0.0.1:${modelServer.address().port}/v1`
const python = process.env.KEJI_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')
const server = spawn(python, ['server/app.py', '--port', String(port), '--db', path.join(work, 'test.sqlite3')], {
  cwd: root, windowsHide: true, stdio: 'pipe',
  env: { ...process.env, KEJI_AI_BASE_URL: '', KEJI_AI_API_KEY: '', KEJI_AI_MODEL: '' },
})
let serverLog = '', browser
server.stdout.on('data', data => { serverLog += data })
server.stderr.on('data', data => { serverLog += data })
server.on('error', error => { serverLog += error.message })
const checks = [], errors = []
const check = async (name, fn) => { await fn(); checks.push(name); console.log(`PASS ${name}`) }
const get = async endpoint => (await fetch(`${base}${endpoint}`)).json()
try {
  for (let i = 0; i < 60; i++) {
    if (server.exitCode !== null) throw new Error(serverLog)
    try { if ((await fetch(`${base}/api/health`)).ok) break } catch { }
    if (i === 59) throw new Error('Test server did not start: ' + serverLog)
    await delay(100)
  }
  browser = await chromium.launch({ headless: true, ...(process.env.KEJI_BROWSER_CHANNEL ? { channel: process.env.KEJI_BROWSER_CHANNEL } : {}) })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, locale: 'zh-CN' })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${base}/settings/ai`)
  await check('settings load and optional model discovery with full endpoint', async () => {
    await page.getByLabel('API 地址', { exact: true }).fill(`${modelBase}/chat/completions`)
    await page.getByLabel('API Key', { exact: true }).fill('e2e-fake-key')
    await page.getByRole('button', { name: '读取模型列表（可选）' }).click()
    await page.getByText('已读取 1 个模型 ID', { exact: false }).waitFor()
    assert.equal(await page.locator('#ai-model-list option').first().getAttribute('value'), 'summary-model')
    assert.equal(calls[0].path, '/v1/models')
  })
  await check('probe validates summary but does not save or create tasks', async () => {
    await page.getByLabel('模型 ID', { exact: true }).fill('summary-model')
    await page.getByRole('button', { name: '测试连接与摘要', exact: true }).click()
    await page.getByRole('heading', { name: '连接测试通过', exact: true }).waitFor()
    assert.equal((await get('/api/settings/ai')).enabled, false)
    assert.equal((await get('/api/catalog')).tasks.length, 0)
    assert.equal((await get('/api/catalog')).replays.length, 0)
    assert.equal(JSON.parse(calls[1].body.messages[1].content).length, 2)
  })
  await check('saved settings activate immediately and key never returns', async () => {
    await page.getByRole('button', { name: '保存并启用', exact: true }).click()
    await page.getByText('设置已保存并立即生效', { exact: false }).waitFor()
    assert.ok((await get('/api/capabilities')).aiConfigured)
    assert.equal(await page.getByLabel('API Key', { exact: true }).inputValue(), '')
    assert.equal((await get('/api/settings/ai')).baseUrl, modelBase)
    assert.ok(!JSON.stringify(await get('/api/settings/ai')).includes('e2e-fake-key'))
    await page.reload()
    await page.getByText('AI 已配置', { exact: true }).waitFor()
    assert.equal(await page.getByLabel('API Key', { exact: true }).inputValue(), '')
    assert.ok(!(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).includes('e2e-fake-key'))
    await page.screenshot({ path: path.join(root, 'test-results/ai-settings-desktop.png'), fullPage: true })
  })
  await check('configured model generates replay summary with source and no implicit confirmation', async () => {
    await page.getByRole('button', { name: '前往回放整理 →' }).click()
    await page.getByRole('button', { name: '填入合成示例' }).click()
    await page.getByRole('button', { name: '校验并预览字幕' }).click()
    await page.getByText('已识别 16 段字幕', { exact: false }).waitFor()
    await page.getByRole('button', { name: '确认导入字幕', exact: true }).click()
    await page.getByLabel('整理方式').selectOption('ai')
    await page.getByRole('button', { name: '生成摘要', exact: true }).click()
    await page.getByRole('heading', { name: '章节与知识点' }).waitFor()
    await page.getByText('需求与验收', { exact: true }).waitFor()
    await page.locator('.citation-buttons button').first().click()
    await page.locator('.line.active').waitFor()
    assert.equal((await get('/api/catalog')).tasks.length, 0)
  })
  await check('failed model request retains last summary and reports actual failure', async () => {
    await page.getByRole('button', { name: '管理 AI 服务 →' }).click()
    await page.getByLabel('模型 ID', { exact: true }).fill('simulate-error')
    await page.getByRole('button', { name: '测试连接与摘要', exact: true }).click()
    await page.getByRole('alert').filter({ hasText: 'HTTP 429' }).waitFor()
    assert.equal((await get('/api/settings/ai')).model, 'summary-model')
    await page.getByRole('button', { name: '保存并启用', exact: true }).click()
    await page.getByText('设置已保存并立即生效', { exact: false }).waitFor()
    await page.getByRole('button', { name: '前往回放整理 →' }).click()
    await page.getByLabel('整理方式').selectOption('ai')
    await page.getByRole('button', { name: '重新生成新版本', exact: true }).click()
    await page.getByRole('status').filter({ hasText: 'HTTP 429' }).waitFor()
    await page.getByText('需求与验收', { exact: true }).waitFor()
    const replay = (await get('/api/replays')).replays[0]
    assert.equal((await get(`/api/replays/${replay.id}`)).versions.length, 1)
  })
  await check('invalid source IDs rejected during connection test', async () => {
    await page.getByRole('button', { name: '管理 AI 服务 →' }).click()
    await page.getByLabel('模型 ID', { exact: true }).fill('bad-citation')
    await page.getByRole('button', { name: '测试连接与摘要', exact: true }).click()
    await page.getByRole('alert').filter({ hasText: '实际存在的字幕' }).waitFor()
    assert.equal((await get('/api/settings/ai')).model, 'simulate-error')
  })
  await check('disable clears saved key and restores offline-only choice', async () => {
    await page.getByRole('button', { name: '停用 AI 并清除本机配置', exact: true }).click()
    await page.getByText('AI 已停用，本机保存的密钥已清除', { exact: false }).waitFor()
    assert.equal((await get('/api/capabilities')).aiConfigured, false)
    assert.ok(!(await readFile(path.join(work, 'ai-config.json'), 'utf8')).includes('e2e-fake-key'))
    await page.getByRole('button', { name: '前往回放整理 →' }).click()
    assert.equal(await page.getByLabel('整理方式').locator('option').count(), 1)
    await page.getByText('需求与验收', { exact: true }).waitFor()
  })
  await check('settings tutorial on narrow screen and no browser exceptions', async () => {
    await page.getByRole('button', { name: '配置 AI 服务 →' }).click()
    await page.setViewportSize({ width: 768, height: 1024 })
    await page.getByText('连接失败如何处理？', { exact: true }).click()
    await page.getByText('401/403：', { exact: false }).waitFor()
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({ path: path.join(root, 'test-results/ai-settings-768.png'), fullPage: true })
    assert.deepEqual(errors, [])
  })
  await writeFile(path.join(root, 'test-results/ai-e2e-report.json'), JSON.stringify({ browser: await browser.version(), passed: checks.length, checks, errors, provider: 'local HTTP protocol fixture; no external AI' }, null, 2))
} catch (error) {
  await writeFile(path.join(root, 'test-results/ai-e2e-failure.txt'), String(error.stack) + '\n' + serverLog)
  console.error(error)
  process.exitCode = 1
} finally {
  await browser?.close()
  server.kill()
  await new Promise(resolve => modelServer.close(resolve))
}

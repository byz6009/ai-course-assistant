import { reactive } from 'vue'

const data = reactive({ courses: [], materials: [], deadlines: [], schedules: [], collections: [], replays: [], tasks: [], loading: false, loaded: false, error: '' })

export async function request(path, options = {}) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 30000)
  try {
    const response = await fetch(path, { ...options, signal: controller.signal, headers: { 'Content-Type': 'application/json', ...options.headers } })
    const body = await response.json().catch(() => ({ error: '服务没有返回有效数据，请检查本地课程服务。' }))
    if (!response.ok) throw new Error(body.error || '请求失败，请重试。')
    return body
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求超时，请检查连接后重试；已保存的数据会保留。')
    if (error instanceof TypeError) throw new Error('无法连接本地课程服务，请检查服务是否正在运行。')
    throw error
  } finally {
    clearTimeout(timer)
  }
}

async function load() {
  data.loading = true
  data.error = ''
  try {
    const catalog = await request('/api/catalog')
    Object.assign(data, catalog, { loaded: true })
  } catch (error) {
    data.error = error.message
  } finally {
    data.loading = false
  }
}

async function importBundle(bundle) {
  const result = await request('/api/imports', { method: 'POST', body: JSON.stringify(bundle) })
  await load()
  if (data.error) throw new Error(`已保存，但刷新列表失败：${data.error}`)
  return result
}

async function setDone(id, done) {
  const result = await request(`/api/deadlines/${id}`, { method: 'PATCH', body: JSON.stringify({ done }) })
  const record = data.deadlines.find((item) => item.id === id)
  if (record) record.done = result.done
}

async function setTaskDone(id, done) {
  const result = await request(`/api/tasks/${id}`, { method: 'PATCH', body: JSON.stringify({ done }) })
  const record = data.tasks.find((item) => item.id === id)
  if (record) record.done = result.done
}

export function useCourseStore() {
  return { data, load, importBundle, setDone, setTaskDone, connector: () => request('/api/connector'), history: (id) => request(`/api/records/${id}/history`) }
}

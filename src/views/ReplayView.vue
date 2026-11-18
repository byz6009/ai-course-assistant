<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import ReplayImport from '../components/ReplayImport.vue'
import { request, useCourseStore } from '../composables/useCourseStore'
import { usePlatform } from '../composables/usePlatform'
import { useRouter } from '../router'

const { data, load } = useCourseStore()
const { state } = usePlatform()
const { query, push } = useRouter()
const selectedId = ref(query.value.id || '')
const selected = ref(null)
const result = ref(null)
const capabilities = ref({ providers: [], aiConfigured: false })
const provider = ref('local')
const error = ref('')
const message = ref('')
const busy = ref(false)
const detailLoading = ref(false)
const chosen = ref([])
const viewingVersion = ref('latest')
const versionLoading = ref(false)
const editing = ref(false)
const draft = ref([])
const activeCue = ref('')
const search = ref('')
const cuePage = ref(0)
let pollTimer, generation = 0, versionRequest = 0, disposed = false
const visible = computed(() => data.replays.filter(r => state.courseId === 'all' || r.courseId === state.courseId))
const inProgress = computed(() => ['queued', 'running'].includes(selected.value?.job?.status))
const filteredCues = computed(() => (selected.value?.cues || []).filter(c => c.text.toLowerCase().includes(search.value.toLowerCase())))
const shownCues = computed(() => filteredCues.value.slice(cuePage.value * 50, (cuePage.value + 1) * 50))
const courseName = id => data.courses.find(c => c.id === id)?.name || id
const modeLabel = value => ({ local: '本地原文摘录', ai: 'AI 生成 · 待核对', manual: '人工编辑' }[value] || value)
const statusLabel = value => ({ queued: '排队中', running: '正在整理', succeeded: '整理完成', failed: '整理失败' }[value] || '待整理')
const time = value => {
  const secs = Math.floor(value)
  return [Math.floor(secs / 3600), Math.floor(secs / 60) % 60, secs % 60].map(x => String(x).padStart(2, '0')).join(':')
}
watch(search, () => { cuePage.value = 0 })
watch(visible, list => {
  if (list.length && !list.some(r => r.id === selectedId.value)) select(list[0].id)
  else if (!list.length && data.loaded) { selectedId.value = ''; selected.value = null; result.value = null; generation++; clearTimeout(pollTimer) }
}, { immediate: true })
watch(() => query.value.id, id => { if (id && id !== selectedId.value) select(id) })

function select(id) {
  if (editing.value) { message.value = '请先保存或取消当前编辑，再切换回放。'; return }
  selectedId.value = id
  viewingVersion.value = 'latest'
  push(`/replay?id=${id}`)
  refreshDetail(true)
}
async function refreshDetail(reset = false) {
  if (!selectedId.value) return
  const ticket = ++generation
  const id = selectedId.value
  clearTimeout(pollTimer)
  if (reset) { selected.value = null; result.value = null; detailLoading.value = true; error.value = ''; search.value = ''; activeCue.value = '' }
  try {
    const detail = await request(`/api/replays/${id}`)
    if (ticket !== generation || disposed) return
    const wasProcessing = ['queued', 'running'].includes(selected.value?.job?.status)
    selected.value = detail
    error.value = ''
    if (!editing.value && viewingVersion.value === 'latest') {
      result.value = detail.result
      chosen.value = detail.result?.chapters.map(c => c.id) || []
    }
    if (['queued', 'running'].includes(detail.job?.status)) pollTimer = setTimeout(() => refreshDetail(), 1000)
    else if (wasProcessing) await load()
  } catch (e) { if (ticket === generation) error.value = `${e.message} 可点击“刷新结果”重新连接。` }
  finally { if (ticket === generation) detailLoading.value = false }
}
async function loadVersion() {
  const ticket = ++versionRequest
  const id = selectedId.value
  error.value = ''
  versionLoading.value = true
  try {
    const value = viewingVersion.value === 'latest' ? selected.value.result : await request(`/api/replays/${id}/versions/${viewingVersion.value}`)
    if (ticket !== versionRequest || id !== selectedId.value || disposed) return
    result.value = value
    chosen.value = value?.chapters.map(c => c.id) || []
  } catch (e) {
    if (ticket === versionRequest && id === selectedId.value) {
      error.value = e.message
      viewingVersion.value = result.value ? String(result.value.version) : 'latest'
    }
  }
  finally { if (ticket === versionRequest) versionLoading.value = false }
}
async function generate() {
  busy.value = true
  error.value = message.value = ''
  try {
    const job = await request(`/api/replays/${selectedId.value}/jobs`, { method: 'POST', body: JSON.stringify({ provider: provider.value }) })
    selected.value.job = job
    viewingVersion.value = 'latest'
    message.value = '处理任务已创建。你可以离开页面，稍后回来查看结果。'
    await refreshDetail()
    await load()
  } catch (e) { error.value = e.message }
  finally { busy.value = false }
}
async function confirmTask() {
  busy.value = true
  error.value = message.value = ''
  try {
    const response = await request(`/api/replays/${selectedId.value}/tasks`, { method: 'POST', body: JSON.stringify({ confirmed: true, version: result.value.version, chapterIds: chosen.value }) })
    selected.value.taskId = response.id
    message.value = response.duplicate ? '这讲的学习任务已经在看板中，未重复创建。' : '学习任务已加入看板，引用保留在当前摘要版本。'
    await load()
    if (data.error) throw new Error(`任务已保存，但看板刷新失败：${data.error}`)
  } catch (e) { error.value = e.message }
  finally { busy.value = false }
}
function edit() {
  draft.value = result.value.chapters.map(c => ({ title: c.title, summary: c.summary, cueIds: [...c.cueIds] }))
  editing.value = true
}
async function saveEdit() {
  busy.value = true
  error.value = ''
  try {
    await request(`/api/replays/${selectedId.value}/summary`, { method: 'PATCH', body: JSON.stringify({ baseVersion: result.value.version, chapters: draft.value }) })
    editing.value = false
    viewingVersion.value = 'latest'
    message.value = '编辑已保存为新版本，原摘要仍可查看。'
    await refreshDetail()
    await load()
  } catch (e) { error.value = e.message }
  finally { busy.value = false }
}
async function focusCue(cue) {
  search.value = ''
  activeCue.value = cue.id
  await nextTick()
  cuePage.value = Math.floor(selected.value.cues.findIndex(c => c.id === cue.id) / 50)
  await nextTick()
  document.getElementById(`cue-${cue.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}
function download() {
  const output = { title: selected.value.title, filename: selected.value.filename, sourceUrl: selected.value.sourceUrl, courseId: selected.value.courseId, ...result.value }
  const url = URL.createObjectURL(new Blob([JSON.stringify(output, null, 2)], { type: 'application/json;charset=utf-8' }))
  const a = document.createElement('a')
  a.href = url
  a.download = `keji-summary-v${result.value.version}.json`
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
onMounted(async () => {
  try { capabilities.value = await request('/api/capabilities') }
  catch (e) { error.value = e.message }
  if (!data.loaded) await load()
  if (selectedId.value && !selected.value) await refreshDetail(true)
  if (query.value.version && selected.value) { viewingVersion.value = query.value.version; await loadVersion() }
})
onUnmounted(() => { disposed = true; generation++; versionRequest++; clearTimeout(pollTimer) })
</script>

<template>
  <main class="page replay-page">
    <p class="kicker">回放整理 · 第三阶段</p>
    <h1>从一讲字幕，走到一次复习</h1>
    <p class="lead">导入字幕，核对知识点的原始时间，再把选定内容加入学习任务。每次整理与编辑都保留版本。</p>
    <ReplayImport @imported="select" />
    <p v-if="error" class="note block" role="alert">{{ error }}</p>
    <button v-if="error && !selected && selectedId" class="ghost" :disabled="detailLoading" @click="refreshDetail(true)">重试读取回放</button>
    <p v-if="message" class="note ok" role="status">{{ message }}</p>
    <div class="replay-layout">
      <section class="replay-list" aria-label="回放列表">
        <div class="section-title"><h2>已保存回放</h2><span>{{ visible.length }} 讲</span></div>
        <button v-for="item in visible" :key="item.id" class="lecture" :aria-pressed="selectedId === item.id" :disabled="editing || busy" @click="select(item.id)">
          <span class="badge pine">{{ courseName(item.courseId) }}</span><h3>{{ item.title }}</h3>
          <p class="meta">{{ item.cueCount }} 段字幕 · {{ time(item.duration) }}</p>
          <p class="meta">{{ item.latestVersion ? `已保存 ${item.latestVersion} 个摘要版本` : '等待首次整理' }}</p>
        </button>
        <p v-if="!visible.length" class="empty">还没有回放。导入自己的字幕，或填入合成示例体验流程。</p>
      </section>
      <article v-if="selected" class="detail replay-detail">
        <span class="badge">{{ courseName(selected.courseId) }}</span><h2>{{ selected.title }}</h2>
        <p class="meta">{{ selected.filename }} · {{ selected.cues.length }} 段 · {{ selected.durationFromSubtitles ? '字幕覆盖到' : '回放时长' }} {{ time(selected.duration) }}</p>
        <a v-if="selected.sourceUrl" :href="selected.sourceUrl" target="_blank" rel="noopener noreferrer" class="cite">打开原始回放 ↗</a>
        <div class="processing-controls">
          <label>整理方式<select v-model="provider" :disabled="busy || inProgress || editing" aria-label="整理方式"><option value="local">本地原文摘录</option><option v-if="capabilities.aiConfigured" value="ai">AI 摘要</option></select></label>
          <button class="primary" :disabled="busy || inProgress || editing" @click="generate">{{ inProgress ? statusLabel(selected.job.status) + '…' : selected.job?.status === 'failed' ? '重试整理' : result ? '重新生成新版本' : '生成摘要' }}</button>
          <button class="ghost" :disabled="busy || editing" @click="refreshDetail()">刷新结果</button>
        </div>
        <p v-if="provider === 'local'" class="meta">本地方法按章节选择实际字幕原句，不调用大模型；适合离线浏览和核对。</p>
        <p v-else class="meta">所选字幕将发送到小组配置的模型服务。模型摘要仍需逐条核对原文。</p>
        <p class="note">{{ capabilities.aiConfigured ? 'AI 服务已配置，可在整理方式中选择 AI 摘要。' : 'AI 服务尚未配置，当前可使用本地原文摘录。' }} <button class="cite" @click="push('/settings/ai')">{{ capabilities.aiConfigured ? '管理 AI 服务 →' : '配置 AI 服务 →' }}</button></p>
        <p v-if="selected.job" class="note" :class="selected.job.status === 'failed' ? 'block' : ''" role="status">{{ statusLabel(selected.job.status) }}{{ selected.job.error ? '：' + selected.job.error : '' }}<span v-if="inProgress"> · 结果会自动更新，已有摘要继续保留。</span></p>
        <section v-if="result" aria-label="回放摘要">
          <div class="section-title"><h2>章节与知识点</h2><label>摘要版本<select v-model="viewingVersion" :disabled="editing || busy" @change="loadVersion"><option value="latest">最新版本</option><option v-for="v in selected.versions" :key="v.version" :value="String(v.version)">v{{ v.version }} · {{ modeLabel(v.provider) }}</option></select></label></div>
          <p class="meta">v{{ result.version }} · {{ modeLabel(result.provider) }} · {{ new Date(result.createdAt).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) }}（北京时间）</p>
          <form v-if="editing" @submit.prevent="saveEdit">
            <div v-for="(chapter, index) in draft" :key="index" class="edit-chapter"><label>章节 {{ index + 1 }} 标题<input v-model="chapter.title" required maxlength="160" /></label><label>知识点摘要<textarea v-model="chapter.summary" rows="5" required maxlength="5000" /></label><p class="meta">保留此章节原有字幕引用，请确保修改内容仍有原文依据。</p></div>
            <div class="actions"><button class="primary" :disabled="busy">保存为新版本</button><button type="button" class="ghost" :disabled="busy" @click="editing = false">取消编辑</button></div>
          </form>
          <template v-else>
            <article v-for="chapter in result.chapters" :key="chapter.id" class="summary-chapter">
              <label class="chapter-heading"><input v-model="chosen" type="checkbox" :value="chapter.id" :aria-label="`选择知识点：${chapter.title}`" /><strong>{{ chapter.title }}</strong></label>
              <p class="source-content">{{ chapter.summary }}</p>
              <div class="citation-buttons"><button v-for="cue in chapter.citations" :key="cue.id" class="cite" @click="focusCue(cue)">{{ time(cue.start) }}–{{ time(cue.end) }} · 查看原文</button></div>
            </article>
            <div class="actions"><button class="primary" :disabled="busy || versionLoading || !chosen.length || !!selected.taskId" @click="confirmTask">{{ selected.taskId ? '已加入学习任务' : `确认加入学习任务（${chosen.length} 个知识点）` }}</button><button class="ghost" :disabled="busy || versionLoading || inProgress || result.version !== selected.versions[0]?.version" @click="edit">编辑摘要</button><button class="ghost" @click="download">导出摘要与引用</button><button class="ghost" @click="push('/')">回到看板</button></div>
            <p v-if="selected.taskId" class="meta">已创建的任务保留确认时的摘要和来源，重新生成不会修改任务内容或完成状态。</p>
          </template>
        </section>
        <p v-else class="empty">字幕已保存。点击“生成摘要”开始整理。</p>
        <section aria-label="字幕原文">
          <div class="section-title"><h2>字幕原文</h2><span>{{ filteredCues.length }} 段</span></div>
          <input v-model="search" type="search" class="transcript-search" placeholder="搜索字幕中的词句" aria-label="搜索字幕" />
          <div class="transcript"><p v-for="cue in shownCues" :id="`cue-${cue.id}`" :key="cue.id" class="line" :class="{ active: activeCue === cue.id }"><time>{{ time(cue.start) }}<br />{{ time(cue.end) }}</time><span>{{ cue.text }}</span></p><p v-if="!shownCues.length" class="empty">没有匹配的字幕。</p></div>
          <div v-if="filteredCues.length > 50" class="actions"><button class="ghost" :disabled="cuePage === 0" @click="cuePage--">上一页</button><span>第 {{ cuePage + 1 }} / {{ Math.ceil(filteredCues.length / 50) }} 页</span><button class="ghost" :disabled="(cuePage + 1) * 50 >= filteredCues.length" @click="cuePage++">下一页</button></div>
        </section>
      </article>
      <p v-else class="empty">{{ detailLoading ? '正在读取回放…' : '选择一讲回放，查看字幕和摘要。' }}</p>
    </div>
  </main>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { request, useCourseStore } from '../composables/useCourseStore'
import { usePlatform } from '../composables/usePlatform'
import sample from '../../examples/replay-demo.srt?raw'

const emit = defineEmits(['imported'])
const { data, load } = useCourseStore()
const { state } = usePlatform()
const form = ref({ courseId: '', newId: '', newName: '', title: '', filename: '课程字幕.srt', content: '', sourceUrl: '', duration: '' })
const preview = ref(null)
const busy = ref(false)
const error = ref('')
const note = ref('')
const isNew = computed(() => form.value.courseId === '__new__' || !data.courses.length)
const payload = computed(() => ({ courseId: isNew.value ? form.value.newId : form.value.courseId,
  title: form.value.title, filename: form.value.filename, content: form.value.content,
  sourceUrl: form.value.sourceUrl, durationSeconds: form.value.duration ? Number(form.value.duration) : null }))
watch(form, () => { preview.value = null }, { deep: true })
watch(() => [state.courseId, data.courses.length], () => {
  if (state.courseId !== 'all') form.value.courseId = state.courseId
  else if (!form.value.courseId) form.value.courseId = data.courses[0]?.id || '__new__'
}, { immediate: true })

function example() {
  form.value = { courseId: '__new__', newId: 'stage3-demo', newName: '软件工程 · 合成演示课程',
    title: '需求、设计与测试的可追溯关系（合成示例）', filename: 'replay-demo.srt', content: sample, sourceUrl: '', duration: '' }
  note.value = '已填入公开合成字幕。预览并确认后才会保存，不是实际课堂内容。'
}
async function readFile(event) {
  const file = event.target.files?.[0]
  if (!file) return
  error.value = ''
  if (!/\.(srt|vtt)$/i.test(file.name) || file.size > 5 * 1024 * 1024) {
    error.value = '请选择不超过 5 MiB 的 SRT 或 VTT 字幕。音视频文件请先转写。'
    event.target.value = ''
    return
  }
  try {
    form.value.content = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer())
    form.value.filename = file.name
    if (!form.value.title) form.value.title = file.name.replace(/\.(srt|vtt)$/i, '')
  } catch { error.value = '无法读取 UTF-8 字幕，请转换编码后重试。' }
}
async function check() {
  busy.value = true
  error.value = note.value = ''
  try {
    if (isNew.value && !form.value.newName.trim()) throw new Error('请填写课程名称。')
    preview.value = await request('/api/replays/preview', { method: 'POST', body: JSON.stringify(payload.value) })
  } catch (e) { error.value = e.message }
  finally { busy.value = false }
}
async function save() {
  if (!preview.value) return
  busy.value = true
  error.value = ''
  const body = { ...payload.value }
  try {
    if (isNew.value) await request('/api/courses', { method: 'POST', body: JSON.stringify({ id: form.value.newId, name: form.value.newName }) })
    const result = await request('/api/replays', { method: 'POST', body: JSON.stringify(body) })
    state.courseId = body.courseId
    await load()
    if (data.error) throw new Error(`字幕已保存，但刷新失败：${data.error}`)
    note.value = result.duplicate ? '相同字幕已经保存，已打开原记录。' : '字幕已保存。可以在下方生成摘要。'
    preview.value = null
    emit('imported', result.id)
  } catch (e) { error.value = e.message }
  finally { busy.value = false }
}
</script>

<template>
  <details class="detail replay-import" :open="!data.replays.length">
    <summary>导入一讲字幕 <span class="meta">SRT / VTT · 最多 5 MiB</span></summary>
    <p>保留字幕原文与时间。上传前请确认你有权使用这些资料；自动音视频转写将在后续接入。</p>
    <form @submit.prevent="check">
      <fieldset :disabled="busy">
        <div class="form-grid">
          <label>所属课程<select v-model="form.courseId" required><option v-for="course in data.courses" :key="course.id" :value="course.id">{{ course.name }}</option><option value="__new__">新建课程</option></select></label>
          <label>回放标题<input v-model="form.title" required maxlength="200" placeholder="例如：第 6 讲 面向对象设计" /></label>
          <label v-if="isNew">课程编号<input v-model="form.newId" required maxlength="80" pattern="[A-Za-z0-9_-]+" placeholder="例如 software-engineering" /></label>
          <label v-if="isNew">课程名称<input v-model="form.newName" required maxlength="200" placeholder="例如 软件工程" /></label>
          <label>原始回放链接（可选）<input v-model="form.sourceUrl" type="url" maxlength="2000" placeholder="https://…" /></label>
          <label>回放总时长（秒，可选）<input v-model="form.duration" type="number" min="1" max="86400" step="0.001" placeholder="留空时仅显示字幕覆盖时长" /></label>
          <label>选择字幕文件<input type="file" accept=".srt,.vtt" @change="readFile" /></label>
          <label>字幕文件名<input v-model="form.filename" required maxlength="200" /></label>
        </div>
        <label class="full-field">字幕内容（也可粘贴）<textarea v-model="form.content" rows="5" required placeholder="1&#10;00:00:01,000 --> 00:00:05,000&#10;课堂内容…" /></label>
        <div class="actions"><button type="submit" class="primary">{{ busy ? '正在处理…' : '校验并预览字幕' }}</button><button type="button" class="ghost" @click="example">填入合成示例</button></div>
      </fieldset>
    </form>
    <div v-if="preview" class="preview-box">
      <h3>已识别 {{ preview.cueCount }} 段字幕 · 覆盖到 {{ preview.duration }} 秒</h3>
      <p v-for="cue in preview.sample" :key="cue.id" class="meta">{{ cue.start }}–{{ cue.end }} 秒 · {{ cue.text }}</p>
      <button class="primary" :disabled="busy" @click="save">确认导入字幕</button>
    </div>
    <p v-if="error" class="note block" role="alert">{{ error }}</p>
    <p v-if="note" class="note ok" role="status">{{ note }}</p>
  </details>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { request } from '../composables/useCourseStore'
import { useRouter } from '../router'

const { push } = useRouter()
const settings = ref(null)
const form = reactive({ baseUrl: '', model: '', apiKey: '', timeout: 90, jsonMode: true })
const busy = ref('')
const error = ref('')
const message = ref('')
const models = ref([])
const test = ref(null)
const sourceLabel = computed(() => ({ local: '本机设置', environment: '后端环境变量', disabled: '已停用', none: '未配置', invalid: '需要重新配置' }[settings.value?.source] || '读取中'))
const changed = computed(() => settings.value && (form.apiKey || ['baseUrl', 'model', 'timeout', 'jsonMode'].some(key => form[key] !== settings.value[key])))
const endpoint = computed(() => {
  const value = form.baseUrl.trim().replace(/\/+$/, '')
  return value ? value.endsWith('/chat/completions') ? value : `${value}/chat/completions` : ''
})

function apply(value) {
  settings.value = value
  Object.assign(form, { baseUrl: value.baseUrl, model: value.model, timeout: value.timeout, jsonMode: value.jsonMode, apiKey: '' })
  models.value = []
  test.value = null
}
function edited() { message.value = ''; error.value = ''; test.value = null }
async function reload() {
  busy.value = 'load'; error.value = ''; message.value = ''
  try { apply(await request('/api/settings/ai')); error.value = settings.value.error }
  catch (e) { error.value = e.message }
  finally { busy.value = '' }
}
async function run(action) {
  if (busy.value || !settings.value) return
  busy.value = action; error.value = ''; message.value = ''; test.value = null
  const body = { ...form, revision: settings.value.revision, action }
  try {
    if (action === 'test') {
      test.value = await request('/api/settings/ai/test', { method: 'POST', body: JSON.stringify(body) })
    } else if (action === 'models') {
      const result = await request('/api/settings/ai/models', { method: 'POST', body: JSON.stringify(body) })
      models.value = result.models
      message.value = `已读取 ${models.value.length} 个模型 ID。请选择支持文本对话的模型，再测试摘要能力。`
    } else {
      apply(await request('/api/settings/ai', { method: 'POST', body: JSON.stringify(body) }))
      message.value = action === 'disable' ? 'AI 已停用，本机保存的密钥已清除。本地原文摘录继续可用。' : action === 'environment' ? '已恢复后端环境配置。' : '设置已保存并立即生效。可前往回放整理选择 AI 摘要。'
    }
  } catch (e) { error.value = e.message }
  finally { busy.value = '' }
}
onMounted(reload)
</script>

<template>
  <main class="page ai-settings-page">
    <p class="kicker">课迹 · 模型连接</p>
    <h1>AI 服务设置</h1>
    <p class="lead">连接你选择的模型服务，把课堂字幕整理成可核对的章节与知识点。</p>
    <div class="ai-status note" role="status">
      <strong>{{ settings?.enabled ? 'AI 已配置' : '本地原文摘录可用' }}</strong>
      <span>配置来源：{{ sourceLabel }}</span>
      <span v-if="settings?.enabled">{{ settings.model }} · 保存配置不代表已通过连接测试</span>
    </div>
    <p v-if="error" class="note block" role="alert">{{ error }}</p>
    <p v-if="message" class="note" role="status">{{ message }}</p>

    <div class="ai-settings-layout">
      <section class="card ai-connection" aria-label="模型连接配置">
        <div class="section-title"><h2>连接模型</h2><span class="meta">OpenAI 兼容 Chat Completions</span></div>
        <form @submit.prevent="run('save')" autocomplete="off">
          <fieldset :disabled="!!busy || !settings" @input="edited" @change="edited">
            <label>API 地址
              <input v-model="form.baseUrl" type="url" required maxlength="2000" placeholder="https://api.example.com/v1" spellcheck="false" aria-describedby="api-url-help" />
            </label>
            <p id="api-url-help" class="meta">填写服务商提供的 Base URL，也接受以 /chat/completions 结尾的完整地址。这里不是聊天网站的首页。</p>
            <p v-if="endpoint" class="meta api-endpoint">请求地址：{{ endpoint }}</p>
            <label>API Key
              <input v-model="form.apiKey" type="password" maxlength="4096" autocomplete="new-password" :placeholder="settings?.hasApiKey ? '已保存密钥；同一地址留空则继续使用' : '粘贴服务商签发的 API Key'" aria-describedby="api-key-help" />
            </label>
            <p id="api-key-help" class="meta">{{ settings?.hasApiKey ? '后端已有密钥，页面不会回显。更换 API 地址后请重新输入。' : '云服务需要密钥；无认证的本机模型服务可留空。' }}</p>
            <label>模型 ID
              <input v-model="form.model" list="ai-model-list" required maxlength="200" placeholder="从服务商控制台复制模型 ID" spellcheck="false" />
              <datalist id="ai-model-list"><option v-for="model in models" :key="model" :value="model" /></datalist>
            </label>
            <button class="ghost models-button" type="button" @click="run('models')">{{ busy === 'models' ? '正在读取…' : '读取模型列表（可选）' }}</button>
            <p class="meta">服务不支持列表接口时可直接输入。列表中出现某个模型，不代表它支持本应用的摘要格式。</p>
            <div class="ai-options">
              <label>生成超时（秒）<input v-model.number="form.timeout" type="number" required min="5" max="90" step="1" /></label>
              <label class="checkbox-label"><input v-model="form.jsonMode" type="checkbox" />请求 JSON 模式</label>
            </div>
            <p class="meta">默认开启 JSON 模式；服务明确不支持 response_format 时可关闭。关闭后仍会校验输出结构与引用。</p>
            <div class="actions">
              <button type="button" class="ghost" @click="run('test')">{{ busy === 'test' ? '正在验证摘要…' : '测试连接与摘要' }}</button>
              <button type="submit" class="primary">{{ busy === 'save' ? '正在保存…' : '保存并启用' }}</button>
            </div>
          </fieldset>
        </form>
        <p class="meta">测试只发送两句合成字幕，最长等待 20 秒，可能消耗少量服务额度；测试通过后仍需点击保存。</p>
        <p v-if="changed" class="note">表单有未保存内容。离开页面不会应用这些修改。</p>
        <div v-if="test" class="ai-test-result" role="status">
          <h3>连接测试通过</h3>
          <p>{{ test.message }}</p>
          <p class="meta">{{ test.model }} · {{ test.latencyMs }} ms · {{ test.chapterCount }} 个章节</p>
          <p><strong>{{ test.sample.title }}</strong></p>
          <p class="source-content">{{ test.sample.summary }}</p>
          <p v-for="citation in test.sample.citations" :key="citation.id" class="meta">测试原文：{{ citation.text }}</p>
        </div>
        <div class="ai-storage">
          <p class="meta">保存后，地址、模型和密钥保存在本机服务的 .local-data/ai-config.json（未加密），不会写入浏览器存储、摘要导出或发布包。共享电脑建议使用后端环境变量。</p>
          <div class="actions">
            <button class="ghost" :disabled="!!busy" @click="reload">重新载入设置</button>
            <button v-if="settings?.enabled || settings?.source === 'invalid'" class="ghost" :disabled="!!busy" @click="run('disable')">停用 AI 并清除本机配置</button>
            <button v-if="settings?.hasEnvironment && settings.source !== 'environment'" class="ghost" :disabled="!!busy" @click="run('environment')">改用后端环境变量</button>
          </div>
          <p class="meta">设置修改只影响新任务；已经提交的生成任务会用提交时的配置完成。</p>
        </div>
      </section>

      <aside class="card ai-help" aria-label="AI 使用教程">
        <h2>从连接到学习任务</h2>
        <ol>
          <li><strong>准备连接信息</strong><p>在模型服务控制台获取 API 地址、API Key 和文本对话模型 ID。网页会员与 API 额度通常分别管理，以服务商说明为准。</p></li>
          <li><strong>填写、测试、保存</strong><p>左侧输入信息，点击“测试连接与摘要”。看到摘要与测试原文后，点击“保存并启用”。</p></li>
          <li><strong>导入课程字幕</strong><p>回放整理中选择课程，导入 UTF-8 的 SRT 或 VTT，校验预览后保存。没有字幕时先在外部完成转写。</p></li>
          <li><strong>选择 AI 摘要</strong><p>整理方式切换为“AI 摘要”后生成。当前字幕文本会发送到配置的服务；生成失败可查看原因并手动重试。</p></li>
          <li><strong>核对并确认</strong><p>点击时间引用核对原文，必要时编辑为新版本。选择知识点并确认加入学习任务，之后可在看板勾选。</p></li>
        </ol>
        <button type="button" class="primary" @click="push('/replay')">前往回放整理 →</button>
        <details>
          <summary>地址应该怎么填？</summary>
          <p>例如 DeepSeek 的兼容 Base URL 为 <code>https://api.deepseek.com</code>。其他服务按各自控制台提供的地址填写；地域与密钥应匹配，模型 ID 以当前账号可用列表为准。</p>
          <p>本机兼容服务可填 <code>http://127.0.0.1:端口/v1</code>。仅接受 Chat Completions 协议；原生 Anthropic、Gemini 或仅 Responses 接口不能直接使用。</p>
          <p><a class="cite" href="https://api-docs.deepseek.com/" target="_blank" rel="noopener noreferrer">DeepSeek 官方文档 ↗</a> · <a class="cite" href="https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope" target="_blank" rel="noopener noreferrer">百炼兼容接口文档 ↗</a></p>
        </details>
        <details>
          <summary>连接失败如何处理？</summary>
          <p>401/403：检查密钥、权限和地域。404：检查 API 路径与模型 ID。429：检查余额或限流。400：检查 JSON 模式支持。超时：确认后端网络，或选择响应更快的文本模型。</p>
          <p>“引用无效”表示模型没有按要求引用实际字幕 ID；本次结果不会保存，也不会改用本地结果冒充 AI。</p>
        </details>
        <details>
          <summary>本阶段支持哪些 AI 功能？</summary>
          <p>支持基于字幕的章节与知识点摘要、引用定位、人工编辑和学习任务确认。单次输入最多 60000 个 JSON 字符；过长字幕请拆分课次。</p>
          <p>自动音视频转写、作业辅导生成、备考生成仍属后续迭代。本地原文摘录随时可用，不需要 API Key。</p>
        </details>
      </aside>
    </div>
  </main>
</template>

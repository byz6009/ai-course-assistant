<script setup>
import { useRouter } from '../router'

const { push } = useRouter()
</script>

<template>
  <main class="hello">
    <section class="hello-card">
      <p class="kicker">/test/hello</p>
      <h1>hello world</h1>
      <p class="lead">
        课迹是面向大学课程的 AI 辅助学习平台。第三阶段已接通课程看板、资料收集与字幕回放整理；作业辅导和备考仍为原型。
      </p>
      <div class="actions">
        <button class="primary" type="button" @click="push('/')">进入课程看板</button>
      </div>
    </section>
  </main>
</template>

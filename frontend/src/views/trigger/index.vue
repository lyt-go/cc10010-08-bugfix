<template>
  <section class="page" data-module="trigger">
    <header class="page-head">
      <div>
        <h2>下游触发与执行</h2>
        <p class="page-desc">
          上游完成事件校验去重后生成唯一的下游触发数据记录，一条记录只对应一次上游事件并保留关系版本；
          执行模块只按唯一触发记录执行。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openEvent">登记上游完成事件</button>
        <button class="btn" type="button" @click="reload">刷新</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form v-if="eventOpen" class="create-panel" @submit.prevent="submitEvent">
      <label v-for="field in eventFields" :key="field.name" class="filter-item">
        <span>{{ field.label }}</span>
        <input v-model="eventValues[field.name]" :placeholder="field.placeholder" />
      </label>
      <div class="create-actions">
        <button class="btn primary" type="submit">提交事件</button>
        <button class="btn ghost" type="button" @click="eventOpen = false">取消</button>
      </div>
    </form>

    <p v-if="message" class="ok-text">{{ message }}</p>
    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <h3 class="section-title">唯一的下游触发数据记录</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>触发编号</th><th>关系编号</th><th>关系版本</th><th>事件编号</th>
          <th>上游作业</th><th>下游作业</th><th>业务日期</th><th>执行状态</th><th>执行项</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in triggers" :key="String(row.id)">
          <td>{{ row.触发编号 }}</td>
          <td>{{ row.关系编号 }}</td>
          <td>v{{ row.关系版本 }}</td>
          <td>{{ row.事件编号 }}</td>
          <td>{{ row.上游作业 }}</td>
          <td>{{ row.下游作业 }}</td>
          <td>{{ row.业务日期 }}</td>
          <td>{{ row.执行状态 }}</td>
          <td>{{ row.执行项编号 ?? '—' }}</td>
        </tr>
        <tr v-if="!triggers.length">
          <td colspan="9" class="empty-state">暂无下游触发记录</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">执行模块清单</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>执行项编号</th><th>触发编号</th><th>下游作业</th><th>关系编号</th><th>关系版本</th>
          <th>状态</th><th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in execution" :key="String(row.id)">
          <td>{{ row.执行项编号 }}</td>
          <td>{{ row.触发编号 }}</td>
          <td>{{ row.下游作业 }}</td>
          <td>{{ row.关系编号 }}</td>
          <td>v{{ row.关系版本 }}</td>
          <td>{{ row.状态 }}</td>
          <td class="row-actions">
            <button
              v-for="action in executionActions"
              :key="action"
              class="link"
              type="button"
              @click="runExecution(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!execution.length">
          <td colspan="7" class="empty-state">执行清单为空，合法上游事件会自动回写</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">业务面执行状态</h3>
    <table class="data-table">
      <thead>
        <tr><th>下游作业</th><th>关系编号</th><th>关系版本</th><th>业务面状态</th><th>更新时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in businessStatus" :key="String(row.id)">
          <td>{{ row.下游作业 }}</td>
          <td>{{ row.关系编号 }}</td>
          <td>v{{ row.关系版本 }}</td>
          <td>{{ row.业务面状态 }}</td>
          <td>{{ row.updated_at }}</td>
        </tr>
        <tr v-if="!businessStatus.length">
          <td colspan="5" class="empty-state">暂无业务面执行状态</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">上游完成事件处理日志</h3>
    <table class="data-table">
      <thead>
        <tr><th>事件编号</th><th>上下游</th><th>关系版本</th><th>完成时间</th><th>处理结果</th><th>说明</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in events" :key="String(row.id)">
          <td>{{ row.事件编号 }}</td>
          <td>{{ row.上游作业 }} → {{ row.下游作业 }}</td>
          <td>{{ row.关系版本 ?? '—' }}</td>
          <td>{{ row.完成时间 }}</td>
          <td>{{ row.result }}</td>
          <td class="reason">{{ row.reason || '—' }}</td>
        </tr>
        <tr v-if="!events.length">
          <td colspan="6" class="empty-state">暂无上游完成事件</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/trigger'
const executionActions = ["开始执行", "标记完成", "标记失败"]
const eventFields = [
  { name: '事件编号', label: '事件编号', placeholder: 'EVT-XXXX' },
  { name: '上游作业', label: '上游作业', placeholder: 'ETLJ-XXXX' },
  { name: '下游作业', label: '下游作业', placeholder: 'ETLJ-XXXX' },
  { name: '关系编号', label: '关系编号', placeholder: 'DEPE-XXXX' },
  { name: '关系版本', label: '关系版本', placeholder: '留空则取当前活动版本' },
  { name: '业务日期', label: '业务日期', placeholder: '2026-09-30' },
  { name: '完成时间', label: '完成时间', placeholder: '2026-09-30T01:00:00+00:00' },
]

const triggers = ref<Row[]>([])
const execution = ref<Row[]>([])
const results = ref<Row[]>([])
const businessStatus = ref<Row[]>([])
const events = ref<Row[]>([])
const eventOpen = ref(false)
const eventValues = ref<Record<string, string>>({})
const message = ref('')
const errorMessage = ref('')

const statCards = computed(() => [
  { label: '下游触发记录', value: triggers.value.length },
  { label: '执行清单条目', value: execution.value.length },
  { label: '执行结果', value: results.value.length },
  { label: '已拒绝/重复事件', value: events.value.filter((row) => row.result !== '已生成下游触发记录').length },
])

function openEvent() {
  eventOpen.value = true
  eventValues.value = {}
  message.value = ''
  errorMessage.value = ''
}

async function submitEvent() {
  message.value = ''
  errorMessage.value = ''
  const response = await request(`${ENDPOINT}/events`, {
    method: 'POST',
    body: JSON.stringify({ values: eventValues.value }),
  })
  const payload = await response.json()
  if (!payload.ok) {
    errorMessage.value = payload.message || '上游完成事件被拒绝'
  } else {
    message.value = payload.message
    eventOpen.value = false
  }
  await reload()
}

async function runExecution(action: string, row: Row) {
  message.value = ''
  errorMessage.value = ''
  const response = await request(`${ENDPOINT}/execution/${row.id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ values: { action } }),
  })
  const payload = await response.json()
  if (!payload.ok) {
    errorMessage.value = payload.message || '执行动作未生效'
  } else {
    message.value = payload.message
  }
  await reload()
}

async function reload() {
  try {
    const [triggerResponse, executionResponse, eventResponse] = await Promise.all([
      request(`${ENDPOINT}?size=200`),
      request(`${ENDPOINT}/execution`),
      request(`${ENDPOINT}/events`),
    ])
    if (!triggerResponse.ok || !executionResponse.ok || !eventResponse.ok) {
      throw new Error('下游触发数据读取失败')
    }
    const triggerPayload = await triggerResponse.json()
    const executionPayload = await executionResponse.json()
    const eventPayload = await eventResponse.json()
    triggers.value = triggerPayload.items ?? []
    execution.value = executionPayload.items ?? []
    results.value = executionPayload.results ?? []
    businessStatus.value = executionPayload.business_status ?? []
    events.value = eventPayload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下游触发数据读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.create-panel {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: flex-end;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.create-actions {
  display: flex;
  gap: 8px;
}
.section-title {
  font-size: 14px;
  margin: 16px 0 8px;
}
.reason {
  color: var(--muted);
  font-size: 12px;
}
.ok-text {
  color: #067647;
}
</style>

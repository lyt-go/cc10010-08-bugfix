<template>
  <section class="page" data-module="dependency">
    <header class="page-head">
      <div>
        <h2>依赖解析管理</h2>
        <p class="page-desc">
          管理ETL作业间的依赖关系，定义时间偏移、触发条件和等待策略；同一上下游只保留一条活动边，
          重复登记不会新增记录。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记依赖关系</button>
        <button class="btn" type="button" @click="exportRows">导出依赖解析清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form v-if="creating" class="create-panel" @submit.prevent="submitCreate">
      <label v-for="field in createFields" :key="field.name" class="filter-item">
        <span>{{ field.label }}</span>
        <input v-model="createValues[field.name]" :placeholder="field.placeholder" />
      </label>
      <div class="create-actions">
        <button class="btn primary" type="submit">提交登记</button>
        <button class="btn ghost" type="button" @click="creating = false">取消</button>
      </div>
    </form>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>关系版本</th>
          <th>活动状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>v{{ row.relation_version }}</td>
          <td>{{ row.active ? '活动边' : '历史边' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :disabled="!actionEnabled(action, row) || !canOperate"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="showDetail(row)">查看详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无依赖解析数据，可先登记依赖关系</td>
        </tr>
      </tbody>
    </table>

    <section v-if="detail" class="detail-panel">
      <header class="detail-head">
        <strong>依赖关系详情 · {{ detail.关系编号 }}</strong>
        <button class="btn ghost" type="button" @click="detail = null">关闭</button>
      </header>
      <dl class="detail-grid">
        <div><dt>上游作业</dt><dd>{{ detail.上游作业 }}</dd></div>
        <div><dt>下游作业</dt><dd>{{ detail.下游作业 }}</dd></div>
        <div><dt>方向</dt><dd>{{ detail.direction }}</dd></div>
        <div><dt>关系状态</dt><dd>{{ detail.status }}</dd></div>
        <div><dt>关系版本</dt><dd>v{{ detail.relation_version }}</dd></div>
        <div><dt>所属项目</dt><dd>{{ detail.所属项目 ?? '—' }}</dd></div>
        <div><dt>版本起始</dt><dd>{{ detail.version_started_at ?? '—' }}</dd></div>
        <div><dt>解除时间</dt><dd>{{ detail.deactivated_at ?? '—' }}</dd></div>
      </dl>
      <p class="detail-note">
        关系身份由关系编号、上下游作业、方向与状态共同确定；重复登记只保留一条活动边。
      </p>
    </section>

    <section class="assignment-panel">
      <header class="detail-head">
        <strong>关系共享与维护权限</strong>
        <span class="hint">观察者只读共享关系；维护权限按项目归属判定</span>
      </header>
      <table class="data-table">
        <thead>
          <tr><th>关系编号</th><th>项目</th><th>维护者</th><th>观察者</th><th>访问级别</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in assignments" :key="String(item.id)">
            <td>{{ item.关系编号 }}</td>
            <td>{{ item.项目 }}</td>
            <td>{{ item.维护者 ?? '—' }}</td>
            <td>{{ item.观察者 ?? '—' }}</td>
            <td>{{ item.access_level }}</td>
          </tr>
          <tr v-if="!assignments.length">
            <td colspan="5" class="empty-state">暂无共享关系</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条依赖解析记录</span>
      <span v-if="message" class="ok-text">{{ message }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type Assignment = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/dependency'
const columns = ["关系编号", "上游作业", "下游作业", "依赖类型", "偏移量分钟", "触发条件", "等待超时", "跳过标志"]
const actions = ["提交关系评审", "生效关系", "解除关系"]
const statuses = ["草稿", "关系评审中", "已生效", "已解除"]
const actionSources: Record<string, string[]> = {
  提交关系评审: ['草稿', '已解除'],
  生效关系: ['草稿', '关系评审中', '已解除'],
  解除关系: ['草稿', '关系评审中', '已生效'],
}
const createFields = [
  { name: '关系编号', label: '关系编号', placeholder: 'DEPE-XXXX' },
  { name: '上游作业', label: '上游作业', placeholder: 'ETLJ-XXXX' },
  { name: '下游作业', label: '下游作业', placeholder: 'ETLJ-XXXX' },
  { name: '依赖类型', label: '依赖类型', placeholder: '完成触发' },
  { name: '偏移量分钟', label: '偏移量分钟', placeholder: '0' },
  { name: '触发条件', label: '触发条件', placeholder: '上游成功' },
  { name: '等待超时', label: '等待超时', placeholder: '30' },
  { name: '跳过标志', label: '跳过标志', placeholder: '否' },
  { name: '所属项目', label: '所属项目', placeholder: '数仓公共' },
]

const rows = ref<Row[]>([])
const total = ref(0)
const assignments = ref<Assignment[]>([])
const detail = ref<Row | null>(null)
const errorMessage = ref('')
const message = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const statusFilter = ref('')
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const canOperate = true

const statCards = computed(() => [
  { label: '依赖关系总数', value: total.value },
  { label: '已生效关系', value: rows.value.filter((row) => row.status === '已生效').length },
  { label: '评审中关系', value: rows.value.filter((row) => row.status === '关系评审中').length },
  { label: '活动边数量', value: rows.value.filter((row) => row.active).length },
])

function actionEnabled(action: string, row: Row): boolean {
  return (actionSources[action] ?? []).includes(String(row.status))
}

function resetFilters() {
  filters.value = {}
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  creating.value = true
  createValues.value = {}
  errorMessage.value = ''
}

function showDetail(row: Row) {
  detail.value = row
}

async function submitCreate() {
  errorMessage.value = ''
  message.value = ''
  const response = await request(ENDPOINT, {
    method: 'POST',
    body: JSON.stringify({ values: createValues.value }),
  })
  const payload = await response.json()
  if (!payload.ok) {
    errorMessage.value = payload.message || '依赖关系登记失败'
    return
  }
  message.value = payload.message
  creating.value = false
  await reload()
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  message.value = ''
  const response = await request(`${ENDPOINT}/${row.id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ values: { action } }),
  })
  const payload = await response.json()
  if (!payload.ok) {
    errorMessage.value = payload.message || '依赖解析动作未生效'
    return
  }
  message.value = payload.message
  await reload()
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams({ ...filters.value, status: statusFilter.value }).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('依赖关系列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await loadAssignments()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '依赖解析列表读取失败'
  }
}

async function loadAssignments() {
  const response = await request(`${ENDPOINT}/assignments`)
  if (!response.ok) return
  const payload = await response.json()
  assignments.value = payload.items ?? []
}

onMounted(reload)
</script>

<style scoped>
.create-panel,
.detail-panel,
.assignment-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.create-panel {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: flex-end;
}
.create-actions {
  display: flex;
  gap: 8px;
}
.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.detail-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px 16px;
  margin: 0;
}
.detail-grid dt {
  color: var(--muted);
  font-size: 12px;
}
.detail-grid dd {
  margin: 2px 0 0;
  font-size: 13px;
}
.detail-note,
.hint {
  color: var(--muted);
  font-size: 12px;
}
.ok-text {
  color: #067647;
}
.link:disabled {
  color: var(--muted);
  cursor: not-allowed;
}
</style>

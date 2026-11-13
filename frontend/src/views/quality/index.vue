<template>
  <section class="page" data-module="quality">
    <header class="page-head">
      <div>
        <h2>质量监控管理</h2>
        <p class="page-desc">
          维护质量指标阈值与判定口径：每次调整生成新版本，活跃指标按新口径重判，
          历史结果保留当时阈值与告警级别，聚合视图与治理规则同步读取新版本。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记质量指标</button>
        <button class="btn" type="button" @click="exportRows">导出质量监控清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reloadAll">
      <label class="filter-item">
        <span>指标编号</span>
        <input v-model="filters.keyword" placeholder="按指标编号检索" />
      </label>
      <label class="filter-item">
        <span>监控对象</span>
        <input v-model="filters.objectName" placeholder="按监控对象精确过滤" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
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
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link primary-link" type="button" @click="openThreshold(row)">调整阈值</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无质量监控数据，可先登记质量指标</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条质量监控记录</span>
      <span v-if="message" class="notice-text">{{ message }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="editing" class="panel">
      <h3>调整阈值 · {{ editing.row['指标编号'] }}（{{ editing.row['监控对象'] }}）</h3>
      <p class="panel-hint">
        当前阈值 {{ editing.row['生效阈值'] }}，生效版本 {{ editing.row['阈值版本'] }}。
        超出 0~1 的阈值不会保存；重复提交同一修改沿用已有版本号。
      </p>
      <form class="filter-bar" @submit.prevent="submitThreshold">
        <label class="filter-item">
          <span>阈值范围</span>
          <input v-model="editing.threshold" placeholder="如 0.995 或 0.98~0.995" />
        </label>
        <label class="filter-item">
          <span>告警级别</span>
          <select v-model="editing.alertLevel">
            <option v-for="level in alertLevels" :key="level" :value="level">{{ level }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>业务优先级</span>
          <select v-model="editing.priority">
            <option v-for="level in priorityLevels" :key="level" :value="level">{{ level }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>备注</span>
          <input v-model="editing.remark" placeholder="变更原因" />
        </label>
        <button class="btn primary" type="submit">提交调整</button>
        <button class="btn ghost" type="button" @click="editing = null">取消</button>
      </form>
    </section>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <table v-if="activeTab === 'versions'" class="data-table">
      <thead>
        <tr>
          <th>版本号</th>
          <th>指标编号</th>
          <th>阈值范围</th>
          <th>原阈值</th>
          <th>原版本</th>
          <th>告警级别</th>
          <th>业务优先级</th>
          <th>变更类型</th>
          <th>状态</th>
          <th>变更时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in versions" :key="String(item['版本号'])">
          <td>{{ item['版本号'] }}</td>
          <td>{{ item['指标编号'] }}</td>
          <td>{{ item['阈值范围'] }}</td>
          <td>{{ item['原阈值'] || '—' }}</td>
          <td>{{ item['原版本'] || '—' }}</td>
          <td>{{ item['告警级别'] }}</td>
          <td>{{ item['业务优先级'] }}</td>
          <td>{{ item['变更类型'] }}</td>
          <td>{{ item['状态'] }}</td>
          <td>{{ item['变更时间'] }}</td>
        </tr>
        <tr v-if="!versions.length">
          <td colspan="10" class="empty-state">暂无阈值版本</td>
        </tr>
      </tbody>
    </table>

    <table v-else-if="activeTab === 'results'" class="data-table">
      <thead>
        <tr>
          <th>批次号</th>
          <th>指标编号</th>
          <th>检测日期</th>
          <th>实测值</th>
          <th>判定阈值</th>
          <th>阈值版本</th>
          <th>判定</th>
          <th>告警级别</th>
          <th>判定口径</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in results" :key="String(item['批次号'])">
          <td>{{ item['批次号'] }}</td>
          <td>{{ item['指标编号'] }}</td>
          <td>{{ item['检测日期'] }}</td>
          <td>{{ item['实测值'] }}</td>
          <td>{{ item['阈值'] }}</td>
          <td>{{ item['阈值版本'] }}</td>
          <td>{{ item['判定'] }}</td>
          <td>{{ item['告警级别'] }}</td>
          <td>
            {{ item['判定口径'] }}
            <span v-if="item['历史口径']" class="tag">历史口径</span>
            <span v-else class="tag tag-current">当前口径</span>
          </td>
        </tr>
        <tr v-if="!results.length">
          <td colspan="9" class="empty-state">最近 {{ historyDays }} 天暂无检测结果</td>
        </tr>
      </tbody>
    </table>

    <table v-else-if="activeTab === 'alerts'" class="data-table">
      <thead>
        <tr>
          <th>告警编号</th>
          <th>指标编号</th>
          <th>批次号</th>
          <th>发生时间</th>
          <th>实测值</th>
          <th>触发阈值</th>
          <th>触发阈值版本</th>
          <th>告警级别</th>
          <th>当前告警级别</th>
          <th>恢复阈值</th>
          <th>恢复阈值版本</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in alerts" :key="String(item['告警编号'])">
          <td>{{ item['告警编号'] }}</td>
          <td>{{ item['指标编号'] }}</td>
          <td>{{ item['批次号'] }}</td>
          <td>{{ item['发生时间'] }}</td>
          <td>{{ item['实测值'] }}</td>
          <td>{{ item['触发阈值'] }}</td>
          <td>{{ item['阈值版本'] }}</td>
          <td>{{ item['告警级别'] }}</td>
          <td>{{ item['当前告警级别'] }}</td>
          <td>{{ item['恢复阈值'] }}</td>
          <td>{{ item['恢复阈值版本'] }}</td>
          <td>{{ item['状态'] }}</td>
        </tr>
        <tr v-if="!alerts.length">
          <td colspan="12" class="empty-state">暂无告警通知</td>
        </tr>
      </tbody>
    </table>

    <table v-else-if="activeTab === 'conflicts'" class="data-table">
      <thead>
        <tr>
          <th>监控对象</th>
          <th>冲突指标</th>
          <th>裁决指标</th>
          <th>裁决告警级别</th>
          <th>裁决依据</th>
          <th>生效阈值</th>
          <th>恢复阈值</th>
          <th>阈值版本</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in conflicts" :key="item.监控对象">
          <td>{{ item.监控对象 }}</td>
          <td>
            <span v-for="metric in item.冲突指标" :key="String(metric['指标编号'])" class="conflict-item">
              {{ metric['指标编号'] }} · {{ metric['告警级别'] }} · {{ metric['业务优先级'] }} · 实测 {{ metric['实测值'] }}
            </span>
          </td>
          <td>{{ item.裁决指标 }}（{{ item.裁决指标类型 }}）</td>
          <td>{{ item.裁决告警级别 }}</td>
          <td>{{ item.裁决依据 }}</td>
          <td>{{ item.生效阈值 }}</td>
          <td>{{ item.恢复阈值 }}</td>
          <td>{{ item.阈值版本 }}</td>
        </tr>
        <tr v-if="!conflicts.length">
          <td colspan="8" class="empty-state">暂无监控对象内多指标冲突</td>
        </tr>
      </tbody>
    </table>

    <table v-else class="data-table">
      <thead>
        <tr>
          <th>指标编号</th>
          <th>监控对象</th>
          <th>告警级别</th>
          <th>业务优先级</th>
          <th>恢复策略</th>
          <th>生效阈值</th>
          <th>恢复阈值</th>
          <th>阈值版本</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in recovery" :key="String(item['指标编号'])">
          <td>{{ item['指标编号'] }}</td>
          <td>{{ item['监控对象'] }}</td>
          <td>{{ item['告警级别'] }}</td>
          <td>{{ item['业务优先级'] }}</td>
          <td>{{ item['恢复策略'] }}</td>
          <td>{{ item['生效阈值'] }}</td>
          <td>{{ item['恢复阈值'] }}</td>
          <td>{{ item['阈值版本'] }}</td>
        </tr>
        <tr v-if="!recovery.length">
          <td colspan="8" class="empty-state">暂无恢复策略</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type ConflictMetric = Record<string, string | number | null>

interface ConflictGroup {
  监控对象: string
  冲突指标: ConflictMetric[]
  裁决指标: string
  裁决指标类型: string
  裁决告警级别: string
  裁决依据: string
  生效阈值: string
  恢复阈值: string
  阈值版本: string
}

interface EditingState {
  row: Row
  threshold: string
  alertLevel: string
  priority: string
  remark: string
}

const ENDPOINT = '/api/quality'
const HISTORY_DAYS = 7
const columns = [
  '指标编号', '监控对象', '指标类型', '阈值范围', '生效阈值', '阈值版本', '检测周期',
  '告警级别', '通知渠道', '恢复策略', '业务优先级',
]
const actions = ['提交校验', '启用监控', '停用监控']
const statuses = ['草稿', '待校验', '已启用', '已停用']
const alertLevels = ['提示', '一般', '严重']
const priorityLevels = ['P0', 'P1', 'P2', 'P3']
const tabs = [
  { key: 'versions', label: '阈值版本' },
  { key: 'results', label: `检测结果（近 ${HISTORY_DAYS} 天）` },
  { key: 'alerts', label: '告警通知' },
  { key: 'conflicts', label: '冲突裁决' },
  { key: 'recovery', label: '恢复策略' },
]

const rows = ref<Row[]>([])
const total = ref(0)
const versions = ref<Row[]>([])
const results = ref<Row[]>([])
const alerts = ref<Row[]>([])
const conflicts = ref<ConflictGroup[]>([])
const recovery = ref<Row[]>([])
const message = ref('')
const errorMessage = ref('')
const activeTab = ref('versions')
const editing = ref<EditingState | null>(null)
const filters = reactive({ keyword: '', objectName: '', status: '' })
const historyDays = HISTORY_DAYS

const stats = computed(() => [
  { label: '质量指标总数', value: rows.value.length },
  { label: '已启用指标', value: rows.value.filter((row) => row.status === '已启用').length },
  { label: '生效版本数', value: versions.value.filter((row) => row['状态'] === '生效').length },
])

function resetFilters() {
  filters.keyword = ''
  filters.objectName = ''
  filters.status = ''
  void reloadAll()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '质量指标登记入口尚未接入审批流，可直接调用 POST /api/quality'
}

function openThreshold(row: Row) {
  editing.value = {
    row,
    threshold: String(row['生效阈值'] ?? row['阈值范围'] ?? ''),
    alertLevel: String(row['当前告警级别'] ?? row['告警级别'] ?? '一般'),
    priority: String(row['业务优先级'] ?? 'P2'),
    remark: '',
  }
  message.value = ''
  errorMessage.value = ''
}

async function submitThreshold() {
  if (!editing.value) return
  const { row, threshold, alertLevel, priority, remark } = editing.value
  errorMessage.value = ''
  message.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/threshold`, {
      method: 'POST',
      body: JSON.stringify({
        values: { 阈值范围: threshold, 告警级别: alertLevel, 业务优先级: priority, 备注: remark },
      }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '阈值调整未生效')
    }
    message.value = payload.message
    editing.value = null
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '阈值调整失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  message.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '质量监控动作未生效，请稍后重试')
    }
    message.value = payload.message
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量监控操作失败'
  }
}

async function fetchJson(path: string): Promise<Record<string, unknown>> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as Record<string, unknown>
}

async function reloadAll() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (filters.keyword) query.set('keyword', filters.keyword)
  if (filters.status) query.set('status', filters.status)
  if (filters.objectName) query.set('监控对象', filters.objectName)
  const objectQuery = filters.objectName
    ? `监控对象=${encodeURIComponent(filters.objectName)}`
    : ''
  try {
    const list = await fetchJson(`${ENDPOINT}?${query.toString()}`)
    rows.value = (list.items as Row[]) ?? []
    total.value = Number(list.total ?? rows.value.length)
    const [versionPayload, resultPayload, alertPayload, conflictPayload, recoveryPayload] = await Promise.all([
      fetchJson(`${ENDPOINT}/versions${objectQuery ? `?${objectQuery}` : ''}`),
      fetchJson(`${ENDPOINT}/results?days=${HISTORY_DAYS}${objectQuery ? `&${objectQuery}` : ''}`),
      fetchJson(`${ENDPOINT}/alerts${objectQuery ? `?${objectQuery}` : ''}`),
      fetchJson(`${ENDPOINT}/conflicts${objectQuery ? `?${objectQuery}` : ''}`),
      fetchJson(`${ENDPOINT}/recovery`),
    ])
    versions.value = (versionPayload.items as Row[]) ?? []
    results.value = (resultPayload.items as Row[]) ?? []
    alerts.value = (alertPayload.items as Row[]) ?? []
    conflicts.value = (conflictPayload.items as ConflictGroup[]) ?? []
    recovery.value = (recoveryPayload.items as Row[]) ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质量监控列表读取失败'
  }
}

onMounted(reloadAll)
</script>

<style scoped>
select { border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; background: #fff; }
.primary-link { font-weight: 600; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-top: 12px; }
.panel h3 { margin: 0 0 4px; font-size: 15px; }
.panel-hint { color: var(--muted); font-size: 12px; margin: 0 0 8px; }
.tab-bar { display: flex; gap: 8px; margin: 16px 0 8px; }
.tab { border: 1px solid var(--border); background: #fff; border-radius: 6px; padding: 6px 12px; cursor: pointer; }
.tab.active { background: var(--brand); border-color: var(--brand); color: #fff; }
.tag { margin-left: 6px; padding: 1px 6px; border-radius: 10px; background: #f1f5f9; color: var(--muted); font-size: 12px; }
.tag-current { background: #e0f2fe; color: #0369a1; }
.conflict-item { display: block; font-size: 12px; }
.notice-text { color: #047857; }
</style>

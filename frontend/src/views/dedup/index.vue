<template>
  <section class="page" data-module="dedup">
    <header class="page-head">
      <div>
        <h2>采集去重管理</h2>
        <p class="page-desc">维护去重策略，围绕策略编号、输入来源、去重键、排序字段做登记、筛选与状态流转；并按去重键和时间窗口生成可回查的保留记录台账。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记去重策略</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出采集去重清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集去重数据，可先登记去重策略</td>
        </tr>
      </tbody>
    </table>

    <!-- ===================== 保留记录台账工作区（去重 → 血缘 跨模块链路） ===================== -->
    <section class="retention-panel" data-testid="retention-panel">
      <header class="page-head">
        <div>
          <h3>保留记录台账与重复项引用</h3>
          <p class="page-desc">按去重键和时间窗口计算唯一主保留项，台账由血缘服务持久化；刷新页面后仍与处理依据一致。同一窗口仅一个主保留项，被合并记录保留数据快照可回查。</p>
        </div>
      </header>

      <div class="retention-toolbar">
        <label class="filter-item">
          <span>选择去重策略</span>
          <select v-model="selectedStrategyId" data-testid="retention-strategy-select" @change="onStrategyChange">
            <option value="">全部策略</option>
            <option v-for="row in rows" :key="String(row.id)" :value="Number(row.id)">
              {{ row['策略编号'] }}（{{ row['保留策略'] }} / {{ row['时间窗口'] }}）
            </option>
          </select>
        </label>
        <button class="btn primary" data-testid="retention-load" type="button" :disabled="loading" @click="loadRetention">
          {{ loading ? '装载中…' : '并发装载保留台账' }}
        </button>
        <button class="btn" data-testid="retention-recompute" type="button" :disabled="loading" @click="recomputeRetention">按冲突口径重算</button>
        <button class="btn ghost" data-testid="retention-refresh" type="button" @click="refreshRetention">刷新台账</button>
      </div>

      <div class="stat-row">
        <article class="stat-card"><span class="stat-label">主保留记录数</span><strong class="stat-value">{{ summary['主保留记录数'] ?? 0 }}</strong></article>
        <article class="stat-card"><span class="stat-label">重复项引用总数</span><strong class="stat-value">{{ summary['重复项引用总数'] ?? 0 }}</strong></article>
        <article class="stat-card"><span class="stat-label">待修复条目数</span><strong class="stat-value" :class="{ 'error-text': (summary['待修复条目数'] ?? 0) > 0 }">{{ summary['待修复条目数'] ?? 0 }}</strong></article>
        <article class="stat-card"><span class="stat-label">下游血缘绑定数</span><strong class="stat-value">{{ summary['下游绑定数'] ?? 0 }}</strong></article>
      </div>

      <div class="retention-tabs" role="tablist">
        <button v-for="tab in tabs" :key="tab.key" type="button" class="btn"
          :class="{ primary: activeTab === tab.key }" :data-testid="`retention-tab-${tab.key}`"
          @click="activeTab = tab.key">{{ tab.label }}</button>
      </div>

      <!-- 保留记录台账 -->
      <table v-if="activeTab === 'ledger'" class="data-table" data-testid="retention-ledger-table">
        <thead>
          <tr>
            <th>台账编号</th><th>版本</th><th>去重键值</th><th>窗口依据</th><th>保留策略</th>
            <th>主保留记录</th><th>重复项</th><th>状态</th><th>处理依据</th><th>回查</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in ledgerRows" :key="String(row['台账编号'])">
            <tr :data-testid="`ledger-row-${row['台账编号']}`" :class="{ 'row-conflict': row['是否冲突'] }">
              <td>{{ row['台账编号'] }}</td>
              <td>V{{ row['版本'] }}</td>
              <td>{{ row['去重键值'] }}</td>
              <td>{{ row['窗口依据'] }}</td>
              <td>{{ row['保留策略'] }}</td>
              <td>{{ row['主保留记录编号'] }}</td>
              <td>{{ row['重复项数量'] }}</td>
              <td>{{ row['台账状态'] }}</td>
              <td class="basis-cell">{{ row['处理依据'] }}</td>
              <td><button class="link" type="button" :data-testid="`toggle-${row['台账编号']}`" @click="toggleDetail(row['台账编号'])">{{ expandedNo === row['台账编号'] ? '收起' : '查看快照' }}</button></td>
            </tr>
            <tr v-if="expandedNo === row['台账编号']" :data-testid="`ledger-detail-${row['台账编号']}`" class="ledger-detail-row">
              <td :colspan="10">
                <div class="snapshot-grid">
                  <div>
                    <h4>主保留记录快照（{{ row['主保留记录编号'] }}）</h4>
                    <pre class="snapshot-box">{{ JSON.stringify(row['主保留快照'], null, 2) }}</pre>
                  </div>
                  <div>
                    <h4>重复项引用与被合并数据快照（{{ row['重复项数量'] }} 条）</h4>
                    <p v-if="!row['重复项引用']?.length" class="page-desc">该窗口仅一条记录，无重复项。</p>
                    <article v-for="ref in row['重复项引用']" :key="String(ref['来源记录编号'])" class="ref-card" :data-testid="`ref-${ref['来源记录编号']}`">
                      <div class="ref-head">
                        <strong>{{ ref['来源记录编号'] }}</strong>
                        <span class="page-desc">{{ ref['记录时间'] }} · 排序值 {{ ref['排序值'] }} · 完整度 {{ ref['完整度'] }}</span>
                      </div>
                      <p class="ref-reason">{{ ref['合并原因'] }}</p>
                      <pre class="snapshot-box">{{ JSON.stringify(ref['数据快照'], null, 2) }}</pre>
                    </article>
                  </div>
                </div>
              </td>
            </tr>
          </template>
          <tr v-if="!ledgerRows.length"><td colspan="10" class="empty-state">尚未生成保留台账，请选择策略后点击「并发装载保留台账」</td></tr>
        </tbody>
      </table>

      <!-- 待修复清单 -->
      <table v-else-if="activeTab === 'fix'" class="data-table" data-testid="retention-fix-table">
        <thead>
          <tr><th>清单编号</th><th>版本</th><th>策略</th><th>去重键值</th><th>窗口依据</th><th>冲突类型</th><th>排序口径主记录</th><th>末位口径主记录</th><th>最终采用</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in fixRows" :key="String(item['清单编号'])" :data-testid="`fix-row-${item['清单编号']}`">
            <td>{{ item['清单编号'] }}</td><td>V{{ item['版本'] }}</td><td>{{ item['策略编号'] }}</td>
            <td>{{ item['去重键值'] }}</td><td>{{ item['窗口依据'] }}</td><td>{{ item['冲突类型'] }}</td>
            <td>{{ item['排序字段口径主记录'] }}</td><td>{{ item['窗口末位口径主记录'] }}</td>
            <td>{{ item['最终采用口径'] }} → {{ item['最终主保留记录'] }}</td><td>{{ item['处理状态'] }}</td>
          </tr>
          <tr v-if="!fixRows.length"><td colspan="10" class="empty-state">暂无窗口边界冲突，待修复清单为空</td></tr>
        </tbody>
      </table>

      <!-- 输入来源记录 -->
      <table v-else class="data-table" data-testid="retention-source-table">
        <thead>
          <tr><th>来源记录编号</th><th>策略编号</th><th>去重键值</th><th>排序值</th><th>优先级</th><th>记录时间</th><th>完整度</th><th>数据快照</th></tr>
        </thead>
        <tbody>
          <tr v-for="rec in sourceRows" :key="String(rec.id)" :data-testid="`source-row-${rec.id}`">
            <td>{{ rec['来源记录编号'] }}</td><td>{{ rec['策略编号'] }}</td><td>{{ rec['去重键值'] }}</td>
            <td>{{ rec['排序值'] }}</td><td>{{ rec['优先级'] }}</td><td>{{ rec['记录时间'] }}</td>
            <td>{{ rec['完整度'] }}</td><td class="basis-cell">{{ JSON.stringify(rec['数据快照']) }}</td>
          </tr>
          <tr v-if="!sourceRows.length"><td colspan="8" class="empty-state">该策略暂无输入来源记录</td></tr>
        </tbody>
      </table>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记去重策略</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集去重记录</span>
      <span v-if="errorMessage" class="error-text" data-testid="retention-message">{{ errorMessage }}</span>
      <span v-else-if="okMessage" class="ok-text" data-testid="retention-message">{{ okMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type AnyRow = Record<string, any>

const ENDPOINT = '/api/dedup'
const columns = ["策略编号", "输入来源", "去重键", "排序字段", "保留策略", "时间窗口", "重复阈值", "策略状态"]
const actions = ["提交配置", "启用策略", "停用策略"]
const statuses = ["草稿", "已配置", "生效中", "已停用"]
const metricNames = ["策略总数", "生效中策略", "待配置策略"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const okMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

// ---- 保留记录台账状态 ----
const selectedStrategyId = ref<number | ''>('')
const loading = ref(false)
const activeTab = ref<'ledger' | 'fix' | 'source'>('ledger')
const tabs = [
  { key: 'ledger' as const, label: '保留记录台账' },
  { key: 'fix' as const, label: '待修复清单' },
  { key: 'source' as const, label: '输入来源记录' },
]
const ledgerRows = ref<AnyRow[]>([])
const fixRows = ref<AnyRow[]>([])
const sourceRows = ref<AnyRow[]>([])
const summary = ref<AnyRow>({})
const expandedNo = ref<string | null>(null)

function setMessage(ok: boolean, text: string) {
  if (ok) { okMessage.value = text; errorMessage.value = '' }
  else { errorMessage.value = text; okMessage.value = '' }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'dedup.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('采集去重动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集去重操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('去重策略列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集去重列表读取失败'
  }
}

// ---- 保留台账：装载 / 重算 / 查询 ----
function strategyQuery() {
  return selectedStrategyId.value === '' ? '' : `?strategy_id=${selectedStrategyId.value}`
}

async function refreshRetention() {
  try {
    const [ledgerRes, fixRes, sourceRes, summaryRes] = await Promise.all([
      request(`${ENDPOINT}/retention/ledger${strategyQuery()}`),
      request(`${ENDPOINT}/retention/fix-list${strategyQuery()}`),
      request(`${ENDPOINT}/source-records${strategyQuery()}`),
      request(`${ENDPOINT}/retention/summary`),
    ])
    ledgerRows.value = (await ledgerRes.json()).items ?? []
    fixRows.value = (await fixRes.json()).items ?? []
    sourceRows.value = (await sourceRes.json()).items ?? []
    summary.value = await summaryRes.json()
  } catch (error) {
    setMessage(false, error instanceof Error ? error.message : '保留台账读取失败')
  }
}

function onStrategyChange() {
  expandedNo.value = null
  void refreshRetention()
}

async function postRetention(pathSuffix: string, successText: (m: string) => string) {
  if (selectedStrategyId.value === '') {
    setMessage(false, '请先选择一条去重策略再装载或重算')
    return
  }
  loading.value = true
  try {
    const response = await request(`${ENDPOINT}/${selectedStrategyId.value}/retention/${pathSuffix}`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok) {
      // 并发装载仅首个请求完成，其余请求被拒绝时把后端口径直接展示给用户。
      throw new Error(payload.detail || payload.message || '请求被拒绝')
    }
    setMessage(true, successText(payload.message))
    await refreshRetention()
  } catch (error) {
    setMessage(false, error instanceof Error ? error.message : '保留台账操作失败')
  } finally {
    loading.value = false
  }
}

function loadRetention() {
  void postRetention('load', (m) => m)
}

function recomputeRetention() {
  void postRetention('recompute', (m) => m)
}

function toggleDetail(ledgerNo: string) {
  expandedNo.value = expandedNo.value === ledgerNo ? null : ledgerNo
}

onMounted(async () => {
  await reload()
  await refreshRetention()
})
</script>

<style scoped>
.retention-panel {
  margin-top: 24px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
}
.retention-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: flex-end;
  margin-bottom: 12px;
}
.retention-toolbar select {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
  min-width: 260px;
}
.retention-tabs {
  display: flex;
  gap: 8px;
  margin: 8px 0 12px;
}
.row-conflict {
  background: #fff7ed;
}
.basis-cell {
  max-width: 280px;
  font-size: 12px;
  color: var(--muted);
}
.ledger-detail-row td {
  background: #f8fafc;
}
.snapshot-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.snapshot-box {
  background: #1e293b;
  color: #e2e8f0;
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
  max-height: 240px;
  overflow: auto;
  margin: 6px 0 0;
}
.ref-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  margin-bottom: 10px;
  background: #fff;
}
.ref-head {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  align-items: baseline;
}
.ref-reason {
  font-size: 12px;
  color: #b45309;
  margin: 4px 0;
}
.ok-text {
  color: #15803d;
}
@media (max-width: 960px) {
  .snapshot-grid {
    grid-template-columns: 1fr;
  }
}
</style>

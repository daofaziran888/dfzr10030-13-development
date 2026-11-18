<template>
  <section class="page" data-module="lineage">
    <header class="page-head">
      <div><h2>数据血缘追踪</h2><p class="page-desc">维护血缘关系，围绕关系编号、上游节点、下游节点、来源字段做登记、筛选与状态流转；并把去重保留记录台账作为只读输入持久化，支持按保留项回查。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记血缘关系</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出关系清单</button>
      </div>
    </header>
    <div class="dag-wrap" ref="dagRef">
      <svg class="dag-svg" :viewBox="`0 0 ${svgW} ${svgH}`">
        <defs><marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6" fill="#94a3b8"/></marker></defs>
        <g v-for="edge in graph.edges" :key="edge.from+edge.to">
          <line class="dag-edge" :x1="nodePos[edge.from]?.x||0" :y1="nodePos[edge.from]?.y||0" :x2="nodePos[edge.to]?.x||0" :y2="nodePos[edge.to]?.y||0"/>
          <text :x="(nodePos[edge.from]?.x+nodePos[edge.to]?.x)/2" :y="(nodePos[edge.from]?.y+nodePos[edge.to]?.y)/2 - 6" font-size="10" fill="#64748b" text-anchor="middle">{{ edge.label }}</text>
        </g>
        <g v-for="node in graph.nodes" :key="node.id" :class="['dag-node', {selected: selectedNode?.id===node.id}]" @click="selectNode(node)" @mousedown="startDrag($event, node)">
          <rect :x="nodePos[node.id]?.x - 60" :y="nodePos[node.id]?.y - 20" width="120" height="40" rx="6" :fill="nodeColor(node.status)"/>
          <text :x="nodePos[node.id]?.x" :y="nodePos[node.id]?.y + 4" text-anchor="middle">{{ node.name || node.label }}</text>
        </g>
      </svg>
    </div>
    <div v-if="selectedNode" class="dag-detail" data-testid="dag-detail">
      <dl><dt>编号</dt><dd>{{ selectedNode.id }}</dd></dl>
      <dl><dt>名称</dt><dd>{{ selectedNode.name || selectedNode.label }}</dd></dl>
      <dl v-for="f in detailFields" :key="f"><dt>{{ f }}</dt><dd>{{ selectedNode[f] ?? '—' }}</dd></dl>
      <div style="grid-column:1/-1; margin-top:8px; display:flex; gap:6px;">
        <button v-for="action in actions" :key="action" class="btn" :data-testid="`action-${selectedNode.id}-${action}`" @click="runAction(action, selectedNode)">{{ action }}</button>
      </div>
    </div>

    <!-- ============== 保留记录台账血缘回查（血缘服务持久化，只读输入） ============== -->
    <section class="retention-panel" data-testid="lineage-retention-panel">
      <header class="page-head">
        <div>
          <h3>保留记录台账血缘回查</h3>
          <p class="page-desc">血缘服务把去重保留记录台账及重复项引用作为只读输入持久化，并把每个保留项绑定到下游字段关系；可按主保留记录编号或台账编号回查被合并的数据快照。</p>
        </div>
        <button class="btn ghost" type="button" data-testid="lineage-retention-refresh" @click="loadAll">刷新</button>
      </header>

      <div class="stat-row">
        <article class="stat-card"><span class="stat-label">台账条目数</span><strong class="stat-value">{{ summary['台账条目数'] ?? 0 }}</strong></article>
        <article class="stat-card"><span class="stat-label">重复项引用总数</span><strong class="stat-value">{{ summary['重复项引用总数'] ?? 0 }}</strong></article>
        <article class="stat-card"><span class="stat-label">待修复条目数</span><strong class="stat-value" :class="{ 'error-text': (summary['待修复条目数'] ?? 0) > 0 }">{{ summary['待修复条目数'] ?? 0 }}</strong></article>
        <article class="stat-card"><span class="stat-label">下游绑定数</span><strong class="stat-value">{{ summary['下游绑定数'] ?? 0 }}</strong></article>
      </div>

      <div class="lookup-bar">
        <input v-model="lookupNo" data-testid="lineage-lookup-input" placeholder="输入主保留记录编号（如 SRC-0001）或台账编号" />
        <button class="btn primary" type="button" data-testid="lineage-lookup-btn" @click="doLookup">按保留项回查</button>
      </div>
      <span v-if="lookupMessage" class="error-text" data-testid="lineage-lookup-message">{{ lookupMessage }}</span>

      <div v-if="lookupResult" class="lookup-result" data-testid="lineage-lookup-result">
        <h4>保留项 {{ lookupResult.retention['主保留记录编号'] }}（{{ lookupResult.retention['台账编号'] }} · V{{ lookupResult.retention['版本'] }}）</h4>
        <p class="page-desc">{{ lookupResult.retention['处理依据'] }}</p>
        <div class="snapshot-grid">
          <div>
            <h4>主保留快照</h4>
            <pre class="snapshot-box">{{ JSON.stringify(lookupResult.retention['主保留快照'], null, 2) }}</pre>
          </div>
          <div>
            <h4>被合并重复项数据快照（{{ lookupResult.retention['重复项数量'] }} 条）</h4>
            <p v-if="!lookupResult.retention['重复项引用']?.length" class="page-desc">无重复项。</p>
            <article v-for="ref in lookupResult.retention['重复项引用']" :key="String(ref['来源记录编号'])" class="ref-card">
              <strong>{{ ref['来源记录编号'] }}</strong>
              <p class="ref-reason">{{ ref['合并原因'] }}</p>
              <pre class="snapshot-box">{{ JSON.stringify(ref['数据快照'], null, 2) }}</pre>
            </article>
          </div>
        </div>
        <h4>下游字段关系绑定（{{ lookupResult.bindings.length }}）</h4>
        <table class="data-table">
          <thead><tr><th>绑定编号</th><th>下游节点</th><th>目标字段</th><th>绑定时间</th></tr></thead>
          <tbody>
            <tr v-for="b in lookupResult.bindings" :key="String(b['绑定编号'])">
              <td>{{ b['绑定编号'] }}</td><td>{{ b['下游节点'] }}</td><td>{{ b['目标字段'] }}</td><td>{{ b['绑定时间'] }}</td>
            </tr>
            <tr v-if="!lookupResult.bindings.length"><td colspan="4" class="empty-state">该保留项暂无下游绑定</td></tr>
          </tbody>
        </table>
      </div>

      <div class="retention-tabs">
        <button v-for="tab in tabs" :key="tab.key" type="button" class="btn"
          :class="{ primary: activeTab === tab.key }" :data-testid="`lineage-tab-${tab.key}`"
          @click="activeTab = tab.key">{{ tab.label }}</button>
      </div>

      <table v-if="activeTab === 'ledger'" class="data-table" data-testid="lineage-ledger-table">
        <thead><tr><th>台账编号</th><th>版本</th><th>策略</th><th>去重键值</th><th>窗口依据</th><th>保留策略</th><th>主保留记录</th><th>重复项</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="row in ledgerRows" :key="String(row['台账编号'])" :class="{ 'row-conflict': row['是否冲突'] }">
            <td>{{ row['台账编号'] }}</td><td>V{{ row['版本'] }}</td><td>{{ row['策略编号'] }}</td>
            <td>{{ row['去重键值'] }}</td><td>{{ row['窗口依据'] }}</td><td>{{ row['保留策略'] }}</td>
            <td>{{ row['主保留记录编号'] }}</td><td>{{ row['重复项数量'] }}</td><td>{{ row['台账状态'] }}</td>
            <td><button class="link" type="button" :data-testid="`lineage-row-lookup-${row['主保留记录编号']}`" @click="quickLookup(row['主保留记录编号'])">回查</button></td>
          </tr>
          <tr v-if="!ledgerRows.length"><td colspan="10" class="empty-state">血缘服务尚未持久化保留台账，请先在采集去重页装载</td></tr>
        </tbody>
      </table>

      <table v-else-if="activeTab === 'binding'" class="data-table" data-testid="lineage-binding-table">
        <thead><tr><th>绑定编号</th><th>批次</th><th>版本</th><th>台账编号</th><th>主保留记录</th><th>去重键值</th><th>下游节点</th><th>目标字段</th></tr></thead>
        <tbody>
          <tr v-for="b in bindingRows" :key="String(b['绑定编号'])">
            <td>{{ b['绑定编号'] }}</td><td>{{ b['批次号'] }}</td><td>V{{ b['版本'] }}</td><td>{{ b['台账编号'] }}</td>
            <td>{{ b['主保留记录编号'] }}</td><td>{{ b['去重键值'] }}</td><td>{{ b['下游节点'] }}</td><td>{{ b['目标字段'] }}</td>
          </tr>
          <tr v-if="!bindingRows.length"><td colspan="8" class="empty-state">暂无下游字段绑定</td></tr>
        </tbody>
      </table>

      <table v-else class="data-table" data-testid="lineage-fix-table">
        <thead><tr><th>清单编号</th><th>策略</th><th>去重键值</th><th>窗口依据</th><th>排序口径主记录</th><th>末位口径主记录</th><th>最终采用</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="f in fixRows" :key="String(f['清单编号'])">
            <td>{{ f['清单编号'] }}</td><td>{{ f['策略编号'] }}</td><td>{{ f['去重键值'] }}</td><td>{{ f['窗口依据'] }}</td>
            <td>{{ f['排序字段口径主记录'] }}</td><td>{{ f['窗口末位口径主记录'] }}</td>
            <td>{{ f['最终采用口径'] }} → {{ f['最终主保留记录'] }}</td><td>{{ f['处理状态'] }}</td>
          </tr>
          <tr v-if="!fixRows.length"><td colspan="8" class="empty-state">暂无窗口边界冲突</td></tr>
        </tbody>
      </table>

      <div class="verify-bar">
        <span class="page-desc">只读核对（同一版本内同窗口唯一主记录、重复项快照可回查）：</span>
        <input v-model.number="verifyStrategyId" data-testid="lineage-verify-input" placeholder="去重策略 id" style="width:120px" />
        <button class="btn" type="button" data-testid="lineage-verify-btn" @click="doVerify">核对</button>
        <span v-if="verifyResult" :data-testid="'lineage-verify-result'" :class="verifyResult.ok ? 'ok-text' : 'error-text'">
          {{ verifyResult['核对结果'] }}（条目 {{ verifyResult['台账条目数'] }}，冲突 {{ verifyResult['冲突条目数'] }}）
          <template v-if="verifyResult['问题明细']?.length">：{{ verifyResult['问题明细'].join('；') }}</template>
        </span>
      </div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记血缘关系</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
    <footer class="page-foot"><span>{{ graph.nodes.length }} 个节点</span></footer>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/lineage'
const columns = ["关系编号", "上游节点", "下游节点", "来源字段", "目标字段", "转换说明", "采集批次", "关系版本"]
const actions = ["记录关系", "验证关系", "归档关系"]
const statuses = ["草稿", "已记录", "已验证", "已归档"]
const STATUS_COLORS: Record<string,string> = { '草稿':'#e2e8f0','待校验':'#fef3c7','已启用':'#d1fae5','已停用':'#fee2e2' }
const nodeColor = (s: string) => STATUS_COLORS[s] || '#e2e8f0'
const graph = reactive<{nodes: any[], edges: any[]}>({ nodes: [], edges: [] })
const nodePos = ref<Record<number,{x:number;y:number}>>({})
const selectedNode = ref<any>(null)
const detailFields = computed(() => columns.filter((f: string) => !['id','name','label','status'].includes(f)))
const svgW = ref(800); const svgH = ref(400)
const errorMessage = ref('')
const creating = ref(false); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
let dragNode: any = null; const dagRef = ref<HTMLElement|null>(null)
function layout(nodes: any[]) {
  const w = 800; const h = Math.max(400, nodes.length * 80)
  svgW.value = w; svgH.value = h
  const pos: Record<number,{x:number;y:number}> = {}
  nodes.forEach((n,i) => { pos[n.id] = { x: 120 + (i % 5) * 150, y: 50 + Math.floor(i / 5) * 80 } })
  nodePos.value = pos
}
function selectNode(node: any) { selectedNode.value = selectedNode.value?.id === node.id ? null : node }
function startDrag(e: MouseEvent, node: any) { dragNode = node; const onMove = (ev: MouseEvent) => { if(!dragNode) return; nodePos.value[node.id] = { x: ev.offsetX, y: ev.offsetY } }; const onUp = () => { dragNode = null; document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp) }; document.addEventListener('mousemove', onMove); document.addEventListener('mouseup', onUp) }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); graph.nodes = data.nodes || []; graph.edges = data.edges || []; layout(graph.nodes) } catch(e) { errorMessage.value = '加载失败' }
}
async function runAction(action: string, node: any) {
  try { await request(`${ENDPOINT}/${node.id}/actions`, { method: 'POST', body: JSON.stringify({ action }) }); await reload() } catch(e) { errorMessage.value = '操作失败' }
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }

// ---- 保留记录台账血缘回查（只读） ----
const activeTab = ref<'ledger' | 'binding' | 'fix'>('ledger')
const tabs = [
  { key: 'ledger' as const, label: '保留记录台账' },
  { key: 'binding' as const, label: '下游字段绑定' },
  { key: 'fix' as const, label: '待修复清单' },
]
const ledgerRows = ref<any[]>([])
const bindingRows = ref<any[]>([])
const fixRows = ref<any[]>([])
const summary = ref<Record<string, any>>({})
const lookupNo = ref('')
const lookupResult = ref<any>(null)
const lookupMessage = ref('')
const verifyStrategyId = ref<number | null>(null)
const verifyResult = ref<any>(null)

async function loadAll() {
  try {
    const [ledgerRes, bindingRes, fixRes, summaryRes] = await Promise.all([
      request(`${ENDPOINT}/retention/ledger`),
      request(`${ENDPOINT}/retention/bindings`),
      request(`${ENDPOINT}/retention/fix-list`),
      request(`${ENDPOINT}/retention/summary`),
    ])
    ledgerRows.value = (await ledgerRes.json()).items ?? []
    bindingRows.value = (await bindingRes.json()).items ?? []
    fixRows.value = (await fixRes.json()).items ?? []
    summary.value = await summaryRes.json()
  } catch (e) {
    errorMessage.value = '保留台账读取失败'
  }
}

async function doLookup() {
  lookupMessage.value = ''
  lookupResult.value = null
  if (!lookupNo.value.trim()) { lookupMessage.value = '请输入主保留记录编号或台账编号'; return }
  try {
    const res = await request(`${ENDPOINT}/retention/lookup?retention_no=${encodeURIComponent(lookupNo.value.trim())}`)
    if (!res.ok) {
      const payload = await res.json().catch(() => ({}))
      throw new Error(payload.detail || '未找到保留项')
    }
    lookupResult.value = await res.json()
  } catch (e) {
    lookupMessage.value = e instanceof Error ? e.message : '回查失败'
  }
}

function quickLookup(no: string) {
  lookupNo.value = no
  void doLookup()
}

async function doVerify() {
  verifyResult.value = null
  if (!verifyStrategyId.value) { errorMessage.value = '请输入去重策略 id'; return }
  try {
    const res = await request(`${ENDPOINT}/retention/verify?strategy_id=${verifyStrategyId.value}`)
    verifyResult.value = await res.json()
  } catch (e) {
    errorMessage.value = '核对失败'
  }
}

onMounted(async () => {
  await reload()
  await loadAll()
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
.lookup-bar, .verify-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin: 10px 0;
}
.lookup-bar input, .verify-bar input {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
  min-width: 320px;
}
.retention-tabs { display: flex; gap: 8px; margin: 12px 0; }
.row-conflict { background: #fff7ed; }
.lookup-result {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin: 10px 0;
  background: #f8fafc;
}
.snapshot-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
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
.ref-reason { font-size: 12px; color: #b45309; margin: 4px 0; }
.ok-text { color: #15803d; }
@media (max-width: 960px) {
  .snapshot-grid { grid-template-columns: 1fr; }
}
</style>

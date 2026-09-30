<template>
  <section class="page" data-module="section">
    <header class="page-head">
      <div>
        <h2>剖面编录管理 · 批次打包台</h2>
        <p class="page-desc">
          出包前先在剖面上确认剖面分层与剖面长度（累计长度不相符时以审定边界为准）；
          数据包、图纸清单、核验摘要三件齐全才整批出包，任一缺失整批不出包并说明原因。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="exportSelected" :disabled="!selectedIds.size">
          打包选中剖面（{{ selectedIds.size }}）
        </button>
        <button class="btn" type="button" @click="exportByFilter">按当前筛选整批打包</button>
        <button class="btn ghost" type="button" @click="toggleBatchPanel">
          {{ batchPanelOpen ? '收起批次台' : '打开批次打包台' }}
        </button>
      </div>
    </header>

    <div v-if="migrationNote" class="banner info">
      <span>历史剖面缺图幅号补数：{{ migrationNote.total }} 条已在启动时补入图幅号，原编号保留。</span>
      <button class="link" type="button" @click="runBackfill">重新执行补数</button>
    </div>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>剖面编号</span>
        <input v-model="keyword" placeholder="按剖面编号检索" />
      </label>
      <label class="filter-item">
        <span>剖面状态</span>
        <select v-model="statusFilter">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th style="width: 36px"><input type="checkbox" :checked="allSelected" @change="toggleSelectAll" /></th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>分层确认</th>
          <th>出包预检</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td><input type="checkbox" :value="Number(row.id)" v-model="selectedChecks" /></td>
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span :class="['tag', row['分层确认'] ? 'ok' : 'warn']">
              {{ row['分层确认'] ? `已确认${row['审定长度'] != null ? ' ' + row['审定长度'] + 'm' : ''}` : '未确认' }}
            </span>
          </td>
          <td>
            <button class="link" type="button" @click="preflightOne(Number(row.id))">预检</button>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(Number(row.id))">详情/确认分层</button>
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
          <td :colspan="columns.length + 4" class="empty-state">暂无剖面编录数据</td>
        </tr>
      </tbody>
    </table>

    <div class="pager">
      <button class="btn" type="button" :disabled="page <= 1" @click="changePage(-1)">上一页</button>
      <span>第 {{ page }} 页 / 共 {{ totalPages }} 页 · 共 {{ total }} 条（按编号稳定排序）</span>
      <button class="btn" type="button" :disabled="page >= totalPages" @click="changePage(1)">下一页</button>
    </div>

    <footer class="page-foot">
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="infoMessage" class="info-text">{{ infoMessage }}</span>
    </footer>

    <!-- 剖面详情抽屉：确认分层、外部核对、来源同步、历史版本 -->
    <div v-if="detail" class="drawer-mask" @click.self="detail = null">
      <aside class="drawer">
        <div class="drawer-head">
          <h3>{{ detail['剖面编号'] }} · {{ detail['剖面名称'] }}</h3>
          <button class="btn ghost" type="button" @click="detail = null">关闭</button>
        </div>

        <section class="drawer-section">
          <h4>剖面分层与长度确认</h4>
          <p class="muted">
            申报剖面长度 {{ detail['剖面长度'] }}m ｜ 分层累计长度 {{ detail['分层累计长度'] ?? '—' }}m ｜
            审定长度 {{ detail['审定长度'] ?? '—' }}m
          </p>
          <table class="data-table inner">
            <thead><tr><th>序号</th><th>层号</th><th>地层名称</th><th>层底里程</th><th>审定边界</th></tr></thead>
            <tbody>
              <tr v-for="layer in detail.layers" :key="String(layer.id)">
                <td>{{ layer['序号'] }}</td><td>{{ layer['层号'] }}</td><td>{{ layer['地层名称'] }}</td>
                <td>{{ layer['层底里程'] }}</td><td>{{ layer['审定边界'] }}</td>
              </tr>
            </tbody>
          </table>
          <div class="inline-form">
            <label>审定末层边界（可选，米）<input v-model="auditedBoundary" placeholder="留空按现有审定边界" /></label>
            <button class="btn primary" type="button" @click="confirmLayers">确认剖面分层与长度</button>
          </div>
        </section>

        <section class="drawer-section">
          <h4>外部核对清单（图属相符性）</h4>
          <table class="data-table inner">
            <thead><tr><th>核对项</th><th>外部来源</th><th>状态</th><th>说明</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="item in detail.checklist" :key="String(item.id)">
                <td>{{ item['核对项'] }}</td>
                <td>{{ item['外部来源'] }}</td>
                <td><span :class="['tag', item['核对状态'] === '已核' ? 'ok' : 'warn']">{{ item['核对状态'] }}</span></td>
                <td>{{ item['说明'] }}</td>
                <td>
                  <button v-if="item['核对状态'] !== '已核'" class="link" type="button" @click="reconcile(Number(item.id))">
                    登记已核
                  </button>
                  <span v-else class="muted">—</span>
                </td>
              </tr>
            </tbody>
          </table>
        </section>

        <section class="drawer-section">
          <h4>三处来源同步</h4>
          <pre class="json-box">{{ JSON.stringify(detail.source_refs, null, 2) }}</pre>
        </section>

        <section class="drawer-section">
          <h4>历史导出版本（快照保留）</h4>
          <p class="muted">当前内容与最新导出：{{ detail['当前与最新导出同步'] ? '同步' : '有更新，再导出将产生新版本' }}</p>
          <ul class="version-list">
            <li v-for="a in detail.archives" :key="a.version">
              v{{ a.version }} · {{ a['来源'] }} · {{ a.created_at }} · 三件齐全
            </li>
            <li v-if="!detail.archives.length" class="muted">尚无历史导出</li>
          </ul>
        </section>
      </aside>
    </div>

    <!-- 批次打包台面板 -->
    <section v-if="batchPanelOpen" class="batch-panel">
      <header class="batch-head">
        <h3>批次打包台</h3>
        <button class="btn" type="button" @click="loadBatches">刷新批次</button>
      </header>
      <p class="muted">超过 {{ asyncThreshold }} 条走异步队列；断线后可「接续导出」接着原批次继续，重复导出只保留一套版本。</p>
      <table class="data-table inner">
        <thead>
          <tr><th>批次号</th><th>剖面数</th><th>状态</th><th>已出包</th><th>创建时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="b in batches" :key="String(b.id)">
            <td>{{ b.batch_no }}</td>
            <td>{{ b.total }}</td>
            <td><span :class="['tag', batchTagClass(b.status)]">{{ b.status }}</span></td>
            <td>{{ b.packed }}/{{ b.total }}</td>
            <td>{{ b.created_at }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="viewBatch(Number(b.id))">明细</button>
              <button
                v-if="b.status === '排队中' || b.status === '导出中'"
                class="link"
                type="button"
                @click="resumeBatch(Number(b.id))"
              >接续导出</button>
              <button
                v-if="b.status === '已出包'"
                class="link"
                type="button"
                @click="downloadBatch(Number(b.id), b.package_name)"
              >整包下载</button>
            </td>
          </tr>
          <tr v-if="!batches.length"><td colspan="6" class="empty-state">尚无导出批次</td></tr>
        </tbody>
      </table>

      <div v-if="currentBatch" class="batch-detail">
        <h4>批次 {{ currentBatch.batch_no }} 明细</h4>
        <div v-if="currentBatch.status === '已驳回'" class="banner error">
          <strong>整批未出包，原因：</strong>
          <ul><li v-for="(r, i) in currentBatch['失败原因']" :key="i">{{ r }}</li></ul>
        </div>
        <table class="data-table inner">
          <thead><tr><th>#</th><th>剖面编号</th><th>状态</th><th>版本</th><th>三件产物</th><th>缺失原因</th></tr></thead>
          <tbody>
            <tr v-for="it in currentBatch.items" :key="String(it.section_id)">
              <td>{{ it.seq }}</td>
              <td>{{ it['剖面编号'] }}</td>
              <td>{{ it.status }}</td>
              <td>v{{ it.version ?? '—' }}<span v-if="it['复用旧版本']" class="muted">（复用）</span></td>
              <td>{{ (it.artifacts || []).length }}/3</td>
              <td class="error-text">{{ (it['缺失原因'] || []).join('；') || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Layer = Record<string, string | number | null>
type ChecklistItem = Record<string, string | number | null>
type BatchItem = {
  seq: number
  section_id: number
  剖面编号: string
  剖面名称: string
  status: string
  version: number | null
  复用旧版本: boolean
  artifacts: string[]
  缺失原因: string[]
}
type Batch = {
  id: number
  batch_no: string
  status: string
  total: number
  packed: number
  created_at: string
  package_name: string | null
  frozen_section_ids: number[]
  失败原因: string[]
  items: BatchItem[]
}
type Detail = {
  id: number
  [key: string]: unknown
  layers: Layer[]
  checklist: ChecklistItem[]
  source_refs: Record<string, unknown>
  archives: { version: number; 来源: string; created_at: string; artifacts: string[] }[]
  当前与最新导出同步: boolean
  分层累计长度: number | null
}

const ENDPOINT = '/api/section'
const columns = ['剖面编号', '剖面名称', '剖面长度', '起点坐标', '终点坐标', '编录日期', '编录人员', '剖面状态']
const actions = ['完成实测', '提交制图', '申请验收']
const statuses = ['实测中', '已编录', '已制图', '已验收']
const asyncThreshold = 2
const PAGE_SIZE = 20

const rows = ref<Row[]>([])
const total = ref(0)
const page = ref(1)
const keyword = ref('')
const statusFilter = ref('')
const errorMessage = ref('')
const infoMessage = ref('')
const selectedChecks = ref<number[]>([])
const batchPanelOpen = ref(false)
const batches = ref<Batch[]>([])
const currentBatch = ref<Batch | null>(null)
const detail = ref<Detail | null>(null)
const auditedBoundary = ref('')
const migrationNote = ref<{ total: number } | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const selectedIds = computed(() => new Set(selectedChecks.value))
const allSelected = computed(() => rows.value.length > 0 && rows.value.every((r) => selectedIds.value.has(Number(r.id))))

const stats = computed(() => [
  { label: '剖面总数', value: total.value },
  { label: '已确认分层', value: rows.value.filter((r) => r['分层确认']).length },
  { label: '缺图幅号', value: rows.value.filter((r) => !String(r['图幅号'] ?? '').trim()).length },
  { label: '本页勾选', value: selectedIds.value.size },
])

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  page.value = 1
  void reload()
}

function changePage(delta: number) {
  page.value = Math.min(Math.max(1, page.value + delta), totalPages.value)
  void reload()
}

function toggleSelectAll() {
  if (allSelected.value) {
    const pageIds = new Set(rows.value.map((r) => Number(r.id)))
    selectedChecks.value = selectedChecks.value.filter((id) => !pageIds.has(id))
  } else {
    const merged = new Set([...selectedChecks.value, ...rows.value.map((r) => Number(r.id))])
    selectedChecks.value = [...merged]
  }
}

function setInfo(message: string) {
  infoMessage.value = message
  errorMessage.value = ''
}
function setError(message: string) {
  errorMessage.value = message
  infoMessage.value = ''
}

async function reload() {
  setInfo('')
  const query = new URLSearchParams({ page: String(page.value), size: String(PAGE_SIZE) })
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) throw new Error('实测剖面列表读取失败')
    const payload = await response.json()
    rows.value = (payload.items ?? []) as Row[]
    total.value = Number(payload.total ?? 0)
  } catch (error) {
    setError(error instanceof Error ? error.message : '实测剖面列表读取失败')
  }
}

async function runAction(action: string, row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!payload.ok) throw new Error(payload.message || '动作未生效')
    setInfo(payload.message)
    await reload()
  } catch (error) {
    setError(error instanceof Error ? error.message : '剖面操作失败')
  }
}

async function preflightOne(id: number) {
  try {
    // 通过创建一个仅含该剖面、不写库的预检路径：直接打开详情查看分层/清单与同步状态。
    await openDetail(id)
    setInfo(`已打开 ${id} 号剖面详情，可核对分层、外部清单与来源同步后再打包`)
  } catch (error) {
    setError(error instanceof Error ? error.message : '预检失败')
  }
}

async function openDetail(id: number) {
  const response = await request(`${ENDPOINT}/${id}/detail`)
  if (!response.ok) {
    setError(`剖面 ${id} 详情读取失败`)
    return
  }
  detail.value = (await response.json()) as Detail
  auditedBoundary.value = ''
}

async function confirmLayers() {
  if (!detail.value) return
  const values: Record<string, string> = {}
  if (auditedBoundary.value.trim()) values['审定边界'] = auditedBoundary.value.trim()
  const response = await request(`${ENDPOINT}/${detail.value.id}/layers/confirm`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  const payload = await response.json()
  if (!payload.ok) {
    setError(payload.message || '分层确认失败')
    return
  }
  setInfo(payload.message)
  await openDetail(Number(detail.value.id))
  await reload()
}

async function reconcile(itemId: number) {
  if (!detail.value) return
  const response = await request(`${ENDPOINT}/checklist/${itemId}/reconcile`, {
    method: 'POST',
    body: JSON.stringify({
      values: { 核对状态: '已核', 说明: '图属相符性核对通过', 核对日期: new Date().toISOString().slice(0, 10) },
    }),
  })
  const payload = await response.json()
  if (!payload.ok) {
    setError(payload.message || '外部核对登记失败')
    return
  }
  setInfo(payload.message)
  await openDetail(Number(detail.value.id))
}

async function createBatch(body: Record<string, unknown>): Promise<Batch | null> {
  const response = await request(`${ENDPOINT}/batches`, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json()
  if (!response.ok || !payload.ok) {
    setError(payload.detail || payload.message || '批次创建失败')
    return null
  }
  setInfo(payload.message)
  batchPanelOpen.value = true
  await loadBatches()
  return payload.batch as Batch
}

async function exportSelected() {
  if (!selectedIds.value.size) return
  const batch = await createBatch({ section_ids: [...selectedIds.value], operator: '前端值班员' })
  if (batch) await inspectAfterCreate(batch)
}

async function exportByFilter() {
  const body: Record<string, unknown> = { operator: '前端值班员' }
  if (keyword.value) body.keyword = keyword.value
  if (statusFilter.value) body.status = statusFilter.value
  const batch = await createBatch(body)
  if (batch) await inspectAfterCreate(batch)
}

async function inspectAfterCreate(batch: Batch) {
  currentBatch.value = batch
  if (batch.status === '已驳回') {
    setError(`批次 ${batch.batch_no} 整批未出包，请在批次明细查看原因`)
    return
  }
  if (batch.total > asyncThreshold) {
    setInfo(`批次 ${batch.batch_no} 已入异步队列，正在后台导出，可在批次台刷新或等待自动更新`)
    return
  }
  setInfo(`批次 ${batch.batch_no} 已出包，可整包下载`)
}

function toggleBatchPanel() {
  batchPanelOpen.value = !batchPanelOpen.value
  if (batchPanelOpen.value) void loadBatches()
}

async function loadBatches() {
  const response = await request(`${ENDPOINT}/batches?page=1&size=50`)
  if (!response.ok) {
    setError('批次列表读取失败')
    return
  }
  const payload = await response.json()
  batches.value = (payload.items ?? []) as Batch[]
  if (currentBatch.value) {
    const fresh = batches.value.find((b) => b.id === currentBatch.value?.id)
    if (fresh) currentBatch.value = fresh
  }
  startPolling()
}

async function viewBatch(id: number) {
  const response = await request(`${ENDPOINT}/batches/${id}`)
  if (!response.ok) {
    setError(`批次 ${id} 读取失败`)
    return
  }
  currentBatch.value = (await response.json()) as Batch
}

async function resumeBatch(id: number) {
  const response = await request(`${ENDPOINT}/batches/${id}/resume`, { method: 'POST', body: '{}' })
  const payload = await response.json()
  if (!response.ok || !payload.ok) {
    setError(payload.detail || '接续导出失败')
    return
  }
  setInfo(payload.message)
  currentBatch.value = payload.batch as Batch
  await loadBatches()
}

function downloadBatch(id: number, filename: string | null) {
  // 直接走浏览器下载；驳回/未出包时后端返回 409，这里用 fetch 捕获原因。
  void request(`${ENDPOINT}/batches/${id}/download`)
    .then(async (response) => {
      if (response.status === 409) {
        const data = await response.json().catch(() => ({ detail: '整批未出包' }))
        setError(data.detail || '整批未出包')
        return
      }
      if (!response.ok) {
        setError('整包下载失败')
        return
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename || `PACK-${id}.zip`
      a.click()
      URL.revokeObjectURL(url)
      setInfo('整包已下载：数据包、图纸清单、核验摘要与批次清单/总摘要一起下载')
    })
    .catch(() => setError('整包下载请求失败'))
}

async function loadMigrationNote() {
  try {
    const response = await request(`${ENDPOINT}/migration`)
    if (!response.ok) return
    const payload = await response.json()
    if (payload.total > 0) migrationNote.value = { total: payload.total }
  } catch {
    /* 迁移信息仅作提示，失败不阻塞页面 */
  }
}

async function runBackfill() {
  const response = await request(`${ENDPOINT}/migration/backfill`, { method: 'POST', body: '{}' })
  const payload = await response.json()
  if (response.ok && payload.ok) {
    setInfo(payload.message)
    await loadMigrationNote()
    await reload()
  } else {
    setError(payload.detail || '补数失败')
  }
}

function batchTagClass(status: string) {
  if (status === '已出包') return 'ok'
  if (status === '已驳回') return 'err'
  return 'warn'
}

function startPolling() {
  if (pollTimer) return
  const active = () => batches.value.some((b) => b.status === '排队中' || b.status === '导出中')
  pollTimer = setInterval(() => {
    if (active()) void loadBatches()
  }, 1500)
}

onMounted(() => {
  void reload()
  void loadMigrationNote()
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<style scoped>
.pager { display: flex; align-items: center; gap: 12px; margin-top: 8px; font-size: 12px; color: var(--muted); }
.banner { border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; margin-bottom: 12px; font-size: 13px; display: flex; justify-content: space-between; align-items: center; }
.banner.info { background: #eef5ff; border-color: #bcd3fb; }
.banner.error { background: #fef3f2; border-color: #f0b8b0; color: #b42318; display: block; }
.banner.error ul { margin: 6px 0 0; padding-left: 18px; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; border: 1px solid var(--border); }
.tag.ok { background: #e8f7ee; border-color: #9bd8b2; color: #1a7f43; }
.tag.warn { background: #fff6e0; border-color: #f0cf86; color: #9a6700; }
.tag.err { background: #fdeceb; border-color: #e9a39a; color: #b42318; }
.drawer-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); z-index: 50; display: flex; justify-content: flex-end; }
.drawer { width: 720px; max-width: 92vw; background: #fff; height: 100%; overflow-y: auto; padding: 18px 20px; }
.drawer-head { display: flex; justify-content: space-between; align-items: center; }
.drawer-section { margin-top: 18px; border-top: 1px solid var(--border); padding-top: 12px; }
.drawer-section h4 { margin: 0 0 8px; }
.data-table.inner { font-size: 12px; }
.inline-form { display: flex; gap: 10px; align-items: flex-end; margin-top: 10px; }
.inline-form label { font-size: 12px; color: var(--muted); display: flex; flex-direction: column; gap: 4px; }
.inline-form input { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.json-box { background: #0b1526; color: #d7e3f7; padding: 10px; border-radius: 8px; font-size: 12px; overflow-x: auto; }
.version-list { margin: 0; padding-left: 18px; font-size: 13px; }
.muted { color: var(--muted); font-size: 12px; }
.info-text { color: #1a7f43; }
.batch-panel { margin-top: 20px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.batch-head { display: flex; justify-content: space-between; align-items: center; }
.batch-detail { margin-top: 14px; border-top: 1px solid var(--border); padding-top: 12px; }
select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
</style>

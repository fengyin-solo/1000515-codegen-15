<template>
  <section class="page packing-page">
    <header class="page-head">
      <div>
        <h2>实测剖面批次打包台</h2>
        <p class="page-desc">
          导出前先在剖面上确认分层与长度（累计不符以审定边界为准），数据包、图纸清单、核验摘要齐套才整批出包；
          大批量走异步队列，断连可按原批次续传，重复导出只保留一套版本。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">待选剖面</span>
        <strong class="stat-value">{{ preflight.total }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">具备出包条件</span>
        <strong class="stat-value ok-text">{{ preflight.ready }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">材料不齐</span>
        <strong class="stat-value error-text">{{ preflight.blocked }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已勾选</span>
        <strong class="stat-value">{{ selected.length }}</strong>
      </article>
    </div>

    <p v-if="notice" :class="noticeOk ? 'ok-text' : 'error-text'" class="notice-bar">{{ notice }}</p>

    <!-- 第一步：逐剖面核验与确认 -->
    <div class="panel">
      <div class="panel-head">
        <h3>① 出包前核验：剖面分层 / 剖面长度 / 图纸 / 外部核对清单</h3>
        <button class="btn ghost" type="button" @click="loadPreflight">刷新核验</button>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>选择</th>
            <th>剖面编号</th>
            <th>剖面名称</th>
            <th>图幅编号</th>
            <th>申报长度(m)</th>
            <th>原始累计(m)</th>
            <th>审定累计(m)</th>
            <th>分层</th>
            <th>图纸(已到/总)</th>
            <th>外部清单</th>
            <th>确认</th>
            <th>核验结论</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in preflight.items" :key="item.剖面编号" :class="{ 'row-blocked': !item.ready }">
            <td>
              <input
                type="checkbox"
                :checked="selected.includes(item.剖面编号)"
                :disabled="!item.ready"
                @change="toggleSelect(item.剖面编号)"
              />
            </td>
            <td>{{ item.剖面编号 }}</td>
            <td>{{ item.剖面名称 }}</td>
            <td>
              {{ item.图幅编号 || '—' }}
              <em v-if="item.图幅号来源 === '历史补数迁移'" class="tag tag-amber" title="历史剖面缺图幅号，迁移补挂，原编号不变">补</em>
            </td>
            <td>{{ fmtLen(item.length.declared_length) }}</td>
            <td :class="{ 'len-diff': item.length.adjusted }">{{ fmtLen(item.length.raw_cumulative) }}</td>
            <td>{{ fmtLen(item.length.approved_cumulative) }}</td>
            <td>{{ item.layers_count }}</td>
            <td>{{ item.drawings_present }}/{{ item.drawings_total }}</td>
            <td>{{ item.checklists_total }}</td>
            <td>
              <span v-if="item.confirmed" class="ok-text">已确认{{ item.confirmed_at ? ' ' + item.confirmed_at.slice(0, 10) : '' }}</span>
              <span v-else class="error-text">未确认</span>
            </td>
            <td class="conclusion-cell">
              <span v-if="item.ready" class="ok-text">可出包</span>
              <ul v-else class="reason-list">
                <li v-for="reason in item.blocking" :key="reason">{{ reason }}</li>
              </ul>
              <ul v-if="item.warnings.length" class="warn-list">
                <li v-for="w in item.warnings" :key="w">{{ w }}</li>
              </ul>
            </td>
            <td>
              <button class="link" type="button" @click="confirmSection(item.剖面编号)">
                {{ item.confirmed ? '重新确认分层与长度' : '确认分层与长度' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 第二步：建批导出 -->
    <div class="panel action-bar">
      <div>
        <button class="btn primary" type="button" :disabled="!selected.length" @click="createBatch">
          打包导出（{{ selected.length }} 条）
        </button>
        <span class="hint">超过 {{ asyncThreshold }} 条自动进入异步队列；任一材料缺失整批不出包</span>
      </div>
      <div v-if="lastFailures.length" class="failure-box">
        <strong>整批未出包，原因：</strong>
        <ul>
          <li v-for="(f, i) in lastFailures" :key="i"><b>{{ f.剖面编号 }}</b>：{{ f.原因 }}</li>
        </ul>
      </div>
    </div>

    <!-- 第三步：批次进度（含中断续传） -->
    <div class="panel">
      <div class="panel-head">
        <h3>② 导出批次（键集分页，翻页顺序稳定）</h3>
        <div class="pager">
          <select v-model="batchStatus" @change="reloadBatches(true)">
            <option value="">全部状态</option>
            <option v-for="s in batchStatuses" :key="s" :value="s">{{ s }}</option>
          </select>
          <button class="btn" type="button" @click="reloadBatches(true)">第一页</button>
          <button class="btn" type="button" :disabled="!cursor" @click="reloadBatches(false)">下一页</button>
        </div>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>批次</th>
            <th>版本号</th>
            <th>状态</th>
            <th>方式</th>
            <th>剖面数</th>
            <th>进度</th>
            <th>创建时间</th>
            <th>完成时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in batches" :key="b.id">
            <td>#{{ b.id }} {{ b.package_name }}</td>
            <td>{{ b.version_code || '—' }}</td>
            <td>
              <span :class="statusClass(b.status)">{{ b.status }}</span>
              <p v-if="b.reason" class="error-text reason-line">{{ b.reason }}</p>
            </td>
            <td>{{ b.async_mode ? '异步队列' : '同步' }}</td>
            <td>{{ b.total }}</td>
            <td>
              <div class="progress"><div class="progress-bar" :style="{ width: progressPct(b) + '%' }"></div></div>
              <span class="hint">{{ b.packed_count }}/{{ b.total }}</span>
            </td>
            <td>{{ b.created_at }}</td>
            <td>{{ b.completed_at || '—' }}</td>
            <td class="row-actions">
              <button
                v-if="b.status === '已中断' || b.status === '排队中' || b.status === '打包中'"
                class="link"
                type="button"
                @click="resumeBatch(b.id)"
              >
                接着原批次续传
              </button>
              <button
                v-if="b.status === '已出包' && b.archive_id"
                class="link"
                type="button"
                @click="downloadArchive(b.archive_id)"
              >
                下载整包
              </button>
            </td>
          </tr>
          <tr v-if="!batches.length"><td colspan="9" class="empty-state">暂无导出批次</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 第四步：历史存档 -->
    <div class="panel">
      <div class="panel-head">
        <h3>③ 历史导出（按出包时快照保留，不随后续台账变动）</h3>
        <button class="btn ghost" type="button" @click="loadArchives">刷新</button>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>版本号</th>
            <th>包名</th>
            <th>包含剖面</th>
            <th>大小</th>
            <th>导出时间</th>
            <th>操作人</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in archives" :key="a.id">
            <td>{{ a.version_code }}</td>
            <td>{{ a.package_name }}</td>
            <td>{{ (a.section_codes || []).join('、') }}</td>
            <td>{{ (a.package_size / 1024).toFixed(1) }} KB</td>
            <td>{{ a.created_at }}</td>
            <td>{{ a.created_by }}</td>
            <td><button class="link" type="button" @click="downloadArchive(a.id)">下载整套（数据+图纸+核验）</button></td>
          </tr>
          <tr v-if="!archives.length"><td colspan="7" class="empty-state">暂无历史导出</td></tr>
        </tbody>
      </table>
    </div>

    <!-- 迁移留痕 -->
    <div class="panel">
      <div class="panel-head">
        <h3>④ 历史数据补数迁移（缺图幅号补数，保留原编号）</h3>
        <button class="btn ghost" type="button" @click="loadMigrations">查看迁移日志</button>
      </div>
      <table v-if="migrations.length" class="data-table">
        <thead><tr><th>业务编号(原编号)</th><th>补数字段</th><th>补数值</th><th>执行时间</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="m in migrations" :key="m.id">
            <td>{{ m.业务编号 }}（{{ m.原编号 }}）</td>
            <td>{{ m.补数字段 }}</td>
            <td>{{ m.补数值 }}</td>
            <td>{{ m.执行时间 }}</td>
            <td>{{ m.说明 }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="hint">点击「查看迁移日志」加载。</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

interface Failure { 剖面编号: string; 原因: string }
interface PreflightItem {
  entry_id: number
  剖面编号: string
  剖面名称: string
  图幅编号: string
  图幅号来源: string
  layers_count: number
  drawings_total: number
  drawings_present: number
  checklists_total: number
  confirmed: boolean
  confirmed_at?: string
  ready: boolean
  blocking: string[]
  warnings: string[]
  length: { declared_length: number | null; raw_cumulative: number; approved_cumulative: number; adjusted: boolean }
}
interface Batch {
  id: number
  package_name: string
  version_code: string
  status: string
  async_mode: boolean
  total: number
  packed_count: number
  reason: string
  created_at: string
  completed_at: string
  archive_id: number | null
}
interface Archive {
  id: number
  version_code: string
  package_name: string
  section_codes: string[]
  package_size: number
  created_at: string
  created_by: string
}

const asyncThreshold = 3
const batchStatuses = ['排队中', '打包中', '已出包', '已中断']

const preflight = ref<{ total: number; ready: number; blocked: number; items: PreflightItem[] }>({
  total: 0, ready: 0, blocked: 0, items: [],
})
const selected = ref<string[]>([])
const notice = ref('')
const noticeOk = ref(false)
const lastFailures = ref<Failure[]>([])
const batches = ref<Batch[]>([])
const archives = ref<Archive[]>([])
const migrations = ref<Record<string, string | number>[]>([])
const batchStatus = ref('')
const cursor = ref(0)

function fmtLen(v: number | null): string {
  return v === null || v === undefined ? '—' : v.toFixed(2)
}
function progressPct(b: Batch): number {
  return b.total ? Math.round((b.packed_count / b.total) * 100) : 0
}
function statusClass(status: string): string {
  if (status === '已出包') return 'ok-text'
  if (status === '已中断') return 'error-text'
  return 'muted-text'
}

async function loadPreflight() {
  const res = await request('/api/section-packing/preflight')
  if (res.ok) {
    preflight.value = await res.json()
    selected.value = selected.value.filter(code => preflight.value.items.some(i => i.剖面编号 === code && i.ready))
  }
}

function toggleSelect(code: string) {
  if (selected.value.includes(code)) {
    selected.value = selected.value.filter(c => c !== code)
  } else {
    selected.value = [...selected.value, code]
  }
}

async function confirmSection(code: string) {
  notice.value = ''
  const res = await request('/api/section-packing/confirm', {
    method: 'POST',
    body: JSON.stringify({ section_code: code }),
  })
  const payload = await res.json()
  if (!res.ok || !payload.ok) {
    notice.value = payload.detail || payload.message || '确认失败'
    noticeOk.value = false
  } else {
    notice.value = `${code}：${payload.message}`
    noticeOk.value = true
  }
  await loadPreflight()
}

async function createBatch() {
  notice.value = ''
  lastFailures.value = []
  const res = await request('/api/section-packing/batches', {
    method: 'POST',
    body: JSON.stringify({ section_codes: selected.value }),
  })
  const payload = await res.json()
  if (!payload.ok) {
    notice.value = payload.message
    noticeOk.value = false
    lastFailures.value = payload.failures || []
  } else {
    notice.value = payload.message || `批次 #${payload.batch.id} 已创建：${payload.batch.status}`
    noticeOk.value = true
  }
  await Promise.all([loadPreflight(), reloadBatches(true), loadArchives()])
}

async function reloadBatches(firstPage: boolean) {
  const after = firstPage ? 0 : cursor.value
  const query = new URLSearchParams()
  query.set('after_id', String(after))
  query.set('size', '10')
  if (batchStatus.value) query.set('status', batchStatus.value)
  const res = await request(`/api/section-packing/batches?${query.toString()}`)
  if (res.ok) {
    const data = await res.json()
    batches.value = data.items
    cursor.value = data.next_after_id ?? 0
  }
}

async function resumeBatch(id: number) {
  notice.value = ''
  const res = await request(`/api/section-packing/batches/${id}/resume`, { method: 'POST' })
  const payload = await res.json()
  notice.value = payload.ok ? `${payload.message}（批次 #${id}）` : '续传失败'
  noticeOk.value = !!payload.ok
  // 异步批次重新入队后稍等再刷新，便于看到最终状态
  if (payload.ok && payload.batch?.async_mode && payload.batch.status !== '已出包') {
    await new Promise(resolve => setTimeout(resolve, 800))
  }
  await reloadBatches(true)
  await loadArchives()
}

async function loadArchives() {
  const res = await request('/api/section-packing/archives')
  if (res.ok) {
    const data = await res.json()
    archives.value = data.items
  }
}

async function loadMigrations() {
  const res = await request('/api/section-packing/migrations')
  if (res.ok) migrations.value = (await res.json()).items
}

function downloadArchive(id: number) {
  window.open(`/api/section-packing/archives/${id}/download`, '_blank')
}

onMounted(async () => {
  await Promise.all([loadPreflight(), reloadBatches(true), loadArchives()])
})
</script>

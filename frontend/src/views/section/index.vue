<template>
  <section class="page" data-module="section">
    <header class="page-head">
      <div>
        <h2>剖面编录管理</h2>
        <p class="page-desc">维护实测剖面，围绕剖面编号、剖面名称、剖面长度、起点坐标做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记实测剖面</button>
        <RouterLink class="btn primary-link" to="/section-packing">进入批次打包台</RouterLink>
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
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
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
          <td v-for="column in columns" :key="column">
            <template v-if="column === '图幅编号'">
              <span>{{ row[column] || '—' }}</span>
              <em v-if="row['图幅号来源'] === '历史补数迁移'" class="tag tag-amber" title="历史剖面缺图幅号，迁移时按填图台账补挂，原编号不变">补</em>
            </template>
            <template v-else>{{ row[column] ?? '—' }}</template>
          </td>
          <td class="row-actions">
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
          <td :colspan="columns.length + 1" class="empty-state">暂无剖面编录数据，可先登记实测剖面</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条剖面编录记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/section'
const columns = ["剖面编号", "剖面名称", "剖面长度", "图幅编号", "起点坐标", "终点坐标", "编录日期", "编录人员", "剖面状态"]
const actions = ["完成实测", "提交制图", "申请验收"]
const statuses = ["实测中", "已编录", "已制图", "已验收"]
const stats = [{"label": "实测中剖面", "value": 0}, {"label": "已制图剖面", "value": 0}, {"label": "已验收剖面", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = ["剖面编号", "剖面名称", "剖面长度"]

function resetFilters() {
  filters.value = {}
  void reload()
}

function openCreate() {
  errorMessage.value = '实测剖面登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('剖面编录动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '剖面编录操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('实测剖面列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '剖面编录列表读取失败'
  }
}

onMounted(reload)
</script>

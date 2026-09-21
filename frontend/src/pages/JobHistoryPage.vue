<template>
  <q-page class="page-pad">
    <div class="row items-center q-mb-md">
      <div class="text-h5">作业历史</div>
      <q-space />
      <q-btn flat icon="refresh" label="刷新" @click="load" :loading="loading" />
      <q-btn
        v-if="auth.role === 'bioops'"
        color="primary"
        class="q-ml-sm"
        label="新建作业"
        to="/jobs/new"
      />
    </div>

    <q-table
      flat
      bordered
      row-key="id"
      :rows="rows"
      :columns="columns"
      :loading="loading"
      hide-pagination
      :pagination="{ rowsPerPage: 0 }"
    >
      <template #body-cell-status="props">
        <q-td :props="props">
          <q-badge :color="statusColor(props.row.status)">
            {{ statusLabel(props.row.status) }}
          </q-badge>
        </q-td>
      </template>
      <template #body-cell-metrics="props">
        <q-td :props="props">
          <span v-if="props.row.metrics">
            Q={{ props.row.metrics.mean_quality ?? '—' }}
            · N={{ props.row.metrics.n_rate ?? '—' }}
            · reads={{ props.row.metrics.reads ?? '—' }}
          </span>
          <span v-else class="text-grey-6">—</span>
        </q-td>
      </template>
      <template #body-cell-actions="props">
        <q-td :props="props">
          <q-btn dense flat color="primary" label="详情" :to="`/jobs/${props.row.id}`" />
          <q-btn
            v-if="auth.role === 'bioops' && ['pending', 'running'].includes(props.row.status)"
            dense
            flat
            color="negative"
            label="终止"
            @click="confirmCancel(props.row)"
          />
        </q-td>
      </template>
    </q-table>
  </q-page>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useQuasar } from 'quasar'
import { cancelJob, listJobs } from '../api/client'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const $q = useQuasar()
const loading = ref(false)
const rows = ref([])

const columns = [
  { name: 'id', label: 'ID', field: 'id', align: 'left' },
  { name: 'sample_name', label: '样例', field: 'sample_name', align: 'left' },
  { name: 'status', label: '状态', field: 'status', align: 'left' },
  { name: 'created_by', label: '提交人', field: 'created_by', align: 'left' },
  { name: 'metrics', label: '指标摘要', field: 'metrics', align: 'left' },
  {
    name: 'created_at',
    label: '创建时间',
    field: 'created_at',
    align: 'left',
    format: (v) => (v ? new Date(v).toLocaleString() : ''),
  },
  { name: 'actions', label: '操作', field: 'actions', align: 'left' },
]

function statusLabel(s) {
  return (
    { pending: '排队中', running: '运行中', success: '成功', failed: '失败', cancelled: '已取消' }[
      s
    ] || s
  )
}

function statusColor(s) {
  return (
    {
      pending: 'grey',
      running: 'info',
      success: 'positive',
      failed: 'negative',
      cancelled: 'warning',
    }[s] || 'grey'
  )
}

function confirmCancel(row) {
  $q.dialog({
    title: '终止作业',
    message: `确定要终止作业 #${row.id}（${row.sample_name}）吗？终止后状态为已取消，后续阶段将停止或跳过。`,
    cancel: { label: '再想想', flat: true },
    ok: { label: '确认终止', color: 'negative' },
    persistent: true,
  }).onOk(async () => {
    try {
      await cancelJob(row.id)
      $q.notify({ type: 'positive', message: `作业 #${row.id} 已终止` })
    } catch (e) {
      $q.notify({ type: 'negative', message: e.message || '终止失败' })
    } finally {
      await load()
    }
  })
}

async function load() {
  loading.value = true
  try {
    rows.value = await listJobs()
  } catch (e) {
    $q.notify({ type: 'negative', message: e.message || '加载失败' })
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <q-page class="page-pad">
    <div class="row items-center q-mb-md">
      <div class="text-h5">作业详情 #{{ job?.id || '…' }}</div>
      <q-space />
      <q-btn
        v-if="auth.role === 'bioops' && canCancel"
        color="negative"
        icon="cancel"
        label="终止作业"
        :disable="cancelling"
        class="q-mr-sm"
        @click="confirmCancel"
      />
      <q-btn flat icon="refresh" label="刷新" @click="load" :loading="loading" />
      <q-btn flat label="返回历史" to="/jobs" />
    </div>

    <q-banner v-if="job" rounded class="q-mb-md" :class="statusBannerClass">
      状态：{{ statusLabel(job.status) }}
      · 样例：{{ job.sample_name }}
      · 提交人：{{ job.created_by }}
      <div v-if="job.error_message" class="q-mt-sm">失败原因：{{ job.error_message }}</div>
    </q-banner>

    <div class="text-subtitle1 q-mb-sm">Actor 阶段时间线</div>
    <q-timeline color="primary" class="q-mb-lg">
      <q-timeline-entry
        v-for="s in stages"
        :key="s.id"
        :title="s.actor_name"
        :subtitle="stageSubtitle(s)"
        :color="stageColor(s.status)"
        :icon="stageIcon(s.status)"
      >
        <div>{{ s.message || '—' }}</div>
      </q-timeline-entry>
    </q-timeline>

    <div class="text-subtitle1 q-mb-sm">质控指标</div>
    <div class="row q-col-gutter-md" v-if="metrics">
      <div class="col-12 col-sm-4" v-for="m in metricCards" :key="m.label">
        <q-card flat bordered class="metric-card">
          <q-card-section>
            <div class="text-caption text-grey-7">{{ m.label }}</div>
            <div class="text-h5">{{ m.value }}</div>
          </q-card-section>
        </q-card>
      </div>
      <div class="col-12" v-if="perPosPreview.length">
        <q-card flat bordered>
          <q-card-section>
            <div class="text-subtitle2 q-mb-sm">per_position 摘要（前 8 位）</div>
            <q-markup-table flat dense>
              <thead>
                <tr>
                  <th class="text-left">位点</th>
                  <th class="text-left">平均质量</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="p in perPosPreview" :key="p.position">
                  <td>{{ p.position }}</td>
                  <td>{{ p.mean_quality }}</td>
                </tr>
              </tbody>
            </q-markup-table>
          </q-card-section>
        </q-card>
      </div>
    </div>
    <div v-else class="text-grey-6">尚无指标（作业未成功完成或仍在运行）</div>
  </q-page>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useQuasar } from 'quasar'
import { cancelJob, getJob, getJobStages } from '../api/client'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const $q = useQuasar()
const auth = useAuthStore()
const loading = ref(false)
const cancelling = ref(false)
const job = ref(null)
const stages = ref([])
let timer = null

const metrics = computed(() => job.value?.metrics || null)

const canCancel = computed(() => ['pending', 'running'].includes(job.value?.status))

const metricCards = computed(() => {
  const m = metrics.value
  if (!m) return []
  return [
    { label: 'reads', value: m.reads ?? m.summary?.reads ?? '—' },
    { label: 'mean_quality', value: m.mean_quality ?? m.summary?.mean_quality ?? '—' },
    { label: 'n_rate', value: m.n_rate ?? m.summary?.n_rate ?? '—' },
  ]
})

const perPosPreview = computed(() => {
  const list = metrics.value?.per_position || []
  return list.slice(0, 8)
})

const statusBannerClass = computed(() => {
  const s = job.value?.status
  if (s === 'success') return 'bg-positive text-white'
  if (s === 'failed') return 'bg-negative text-white'
  if (s === 'cancelled') return 'bg-orange-7 text-white'
  if (s === 'running') return 'bg-info text-dark'
  return 'bg-grey-3'
})

function statusLabel(s) {
  return (
    {
      pending: '排队中',
      running: '运行中',
      success: '成功',
      failed: '失败',
      cancelled: '已取消',
    }[s] || s
  )
}

function stageColor(status) {
  return (
    {
      pending: 'grey',
      running: 'info',
      success: 'positive',
      failed: 'negative',
      skipped: 'warning',
      cancelled: 'orange',
    }[status] || 'grey'
  )
}

function stageIcon(status) {
  return (
    {
      pending: 'hourglass_empty',
      running: 'play_circle',
      success: 'check_circle',
      failed: 'error',
      skipped: 'skip_next',
      cancelled: 'cancel',
    }[status] || 'circle'
  )
}

function stageSubtitle(s) {
  const parts = [statusLabel(s.status) || s.status]
  if (s.started_at) parts.push(`开始 ${formatTime(s.started_at)}`)
  if (s.finished_at) parts.push(`结束 ${formatTime(s.finished_at)}`)
  return parts.join(' · ')
}

function formatTime(iso) {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

async function load() {
  loading.value = true
  try {
    const id = route.params.id
    job.value = await getJob(id)
    stages.value = await getJobStages(id)
  } catch (e) {
    $q.notify({ type: 'negative', message: e.message || '加载失败' })
  } finally {
    loading.value = false
  }
}

function confirmCancel() {
  $q.dialog({
    title: '终止作业',
    message: `确定终止作业 #${job.value.id} 吗？终止后状态变为「已取消」，后续阶段将停止或跳过，且不可恢复。`,
    ok: { label: '终止', color: 'negative' },
    cancel: { label: '再想想', flat: true },
  }).onOk(() => doCancel())
}

async function doCancel() {
  cancelling.value = true
  try {
    job.value = await cancelJob(job.value.id)
    stages.value = await getJobStages(job.value.id)
    $q.notify({ type: 'positive', message: `作业 #${job.value.id} 已终止` })
  } catch (e) {
    $q.notify({ type: 'negative', message: e.message || '终止失败' })
    await load()
  } finally {
    cancelling.value = false
  }
}

onMounted(async () => {
  await load()
  timer = setInterval(async () => {
    if (job.value && (job.value.status === 'pending' || job.value.status === 'running')) {
      await load()
    }
  }, 1500)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

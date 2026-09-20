<template>
  <q-page class="page-pad">
    <div class="row items-center q-mb-md">
      <div class="text-h5">样例库</div>
      <q-space />
      <q-btn flat icon="refresh" label="刷新" @click="load" :loading="loading" />
      <q-btn
        v-if="auth.role === 'bioops'"
        color="primary"
        class="q-ml-sm"
        label="提交质控作业"
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
      <template #body-cell-is_broken="props">
        <q-td :props="props">
          <q-badge :color="props.row.is_broken ? 'negative' : 'positive'">
            {{ props.row.is_broken ? '损坏' : '合格' }}
          </q-badge>
        </q-td>
      </template>
      <template #body-cell-actions="props">
        <q-td :props="props">
          <q-btn
            v-if="auth.role === 'bioops'"
            dense
            flat
            color="primary"
            label="用此样例跑质控"
            :to="{ path: '/jobs/new', query: { sampleId: props.row.id } }"
          />
          <span v-else class="text-grey-6">只读</span>
        </q-td>
      </template>
    </q-table>
  </q-page>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useQuasar } from 'quasar'
import { listSamples } from '../api/client'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const $q = useQuasar()
const loading = ref(false)
const rows = ref([])

const columns = [
  { name: 'id', label: 'ID', field: 'id', align: 'left' },
  { name: 'name', label: '名称', field: 'name', align: 'left' },
  { name: 'description', label: '说明', field: 'description', align: 'left' },
  { name: 'is_broken', label: '状态', field: 'is_broken', align: 'left' },
  { name: 'actions', label: '操作', field: 'actions', align: 'left' },
]

async function load() {
  loading.value = true
  try {
    rows.value = await listSamples()
  } catch (e) {
    $q.notify({ type: 'negative', message: e.message || '加载失败' })
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

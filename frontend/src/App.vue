<template>
  <q-layout view="hHh lpR fFf">
    <q-header elevated class="bg-primary text-white" v-if="auth.token">
      <q-toolbar>
        <q-toolbar-title>FASTQ 质控流水线台</q-toolbar-title>
        <q-btn flat dense label="样例库" to="/samples" />
        <q-btn flat dense label="提交作业" to="/jobs/new" v-if="auth.role === 'bioops'" />
        <q-btn flat dense label="历史" to="/jobs" />
        <q-space />
        <div class="q-mr-md text-caption">
          {{ auth.username }}（{{ roleLabel }}）
        </div>
        <q-btn flat dense icon="logout" label="退出" @click="logout" />
      </q-toolbar>
    </q-header>
    <q-page-container>
      <router-view />
    </q-page-container>
  </q-layout>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'

const auth = useAuthStore()
const router = useRouter()

const roleLabel = computed(() => (auth.role === 'bioops' ? '生物运维' : '审计员'))

function logout() {
  auth.logout()
  router.push('/login')
}
</script>

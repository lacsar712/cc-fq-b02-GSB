<template>
  <q-page class="flex flex-center">
    <q-card style="width: 420px; max-width: 92vw" flat bordered>
      <q-card-section>
        <div class="text-h5">登录</div>
        <div class="text-caption text-grey-7 q-mt-sm">
          FASTQ 质控流水线台 — 选择样例 / 粘贴序列 → Actor 链质控 → 查看阶段与指标
        </div>
      </q-card-section>
      <q-card-section>
        <q-input v-model="username" label="用户名" outlined dense class="q-mb-md" />
        <q-input
          v-model="password"
          label="密码"
          type="password"
          outlined
          dense
          class="q-mb-md"
          @keyup.enter="onSubmit"
        />
        <q-btn color="primary" class="full-width" :loading="loading" label="登录" @click="onSubmit" />
        <div class="text-caption text-grey-7 q-mt-md">
          演示账号：bioops / fastq123456（可提交）；auditor / audit123456（只读）
        </div>
      </q-card-section>
    </q-card>
  </q-page>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuasar } from 'quasar'
import { login } from '../api/client'
import { useAuthStore } from '../stores/auth'

const username = ref('bioops')
const password = ref('fastq123456')
const loading = ref(false)
const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const $q = useQuasar()

async function onSubmit() {
  loading.value = true
  try {
    const data = await login(username.value.trim(), password.value)
    auth.setSession(data)
    $q.notify({ type: 'positive', message: '登录成功' })
    router.replace(route.query.redirect || '/samples')
  } catch (e) {
    $q.notify({ type: 'negative', message: e.message || '登录失败' })
  } finally {
    loading.value = false
  }
}
</script>

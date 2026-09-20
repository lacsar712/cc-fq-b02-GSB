import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import LoginPage from '../pages/LoginPage.vue'
import SamplesPage from '../pages/SamplesPage.vue'
import JobSubmitPage from '../pages/JobSubmitPage.vue'
import JobDetailPage from '../pages/JobDetailPage.vue'
import JobHistoryPage from '../pages/JobHistoryPage.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginPage, meta: { public: true } },
    { path: '/', redirect: '/samples' },
    { path: '/samples', name: 'samples', component: SamplesPage },
    { path: '/jobs/new', name: 'job-submit', component: JobSubmitPage, meta: { bioops: true } },
    { path: '/jobs', name: 'jobs', component: JobHistoryPage },
    { path: '/jobs/:id', name: 'job-detail', component: JobDetailPage },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.token) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.meta.bioops && auth.role !== 'bioops') {
    return { name: 'jobs' }
  }
  if (to.name === 'login' && auth.token) {
    return { name: 'samples' }
  }
  return true
})

export default router

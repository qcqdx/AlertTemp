import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import { auth, loadMe } from './api.js'
import './style.css'

import LoginView from './views/LoginView.vue'
import DashboardView from './views/DashboardView.vue'
import ControllerView from './views/ControllerView.vue'
import IncidentsView from './views/IncidentsView.vue'
import AdminView from './views/AdminView.vue'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', component: LoginView },
    { path: '/', component: DashboardView },
    { path: '/controllers/:id', component: ControllerView, props: true },
    { path: '/incidents', component: IncidentsView },
    { path: '/admin', component: AdminView, meta: { admin: true } },
  ],
})

let meLoaded = false
router.beforeEach(async (to) => {
  if (!meLoaded) {
    await loadMe()
    meLoaded = true
  }
  if (to.path !== '/login' && !auth.user) return '/login'
  if (to.path === '/login' && auth.user) return '/'
  if (to.meta.admin && auth.user?.role !== 'admin') return '/'
  return true
})

createApp(App).use(router).mount('#app')

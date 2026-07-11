<script setup>
import { useRouter } from 'vue-router'
import { api, auth } from './api.js'

const router = useRouter()

async function logout() {
  await api.post('/api/v1/auth/logout')
  auth.user = null
  router.push('/login')
}
</script>

<template>
  <header v-if="auth.user" class="topbar">
    <div class="brand">Cold<span>Watch</span></div>
    <nav>
      <router-link to="/">Дашборд</router-link>
      <router-link to="/incidents">Инциденты</router-link>
      <router-link v-if="auth.isAdmin" to="/admin">Администрирование</router-link>
    </nav>
    <div class="spacer"></div>
    <div class="user">{{ auth.user.full_name }} · {{ auth.user.role }}</div>
    <button @click="logout">Выйти</button>
  </header>
  <router-view />
</template>

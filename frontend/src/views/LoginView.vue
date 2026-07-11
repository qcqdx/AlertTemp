<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, auth } from '../api.js'

const router = useRouter()
const username = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)

async function submit() {
  error.value = ''
  busy.value = true
  try {
    auth.user = await api.post('/api/v1/auth/login', {
      username: username.value,
      password: password.value,
    })
    router.push('/')
  } catch (e) {
    error.value = e.status === 401 ? 'Неверный логин или пароль' : e.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <form class="login-box card" @submit.prevent="submit">
      <h1>Cold<span style="color: var(--accent)">Watch</span></h1>
      <p class="hint" style="text-align: center">Контроль холодовой цепи</p>
      <input v-model="username" placeholder="Логин" autocomplete="username" required />
      <input
        v-model="password"
        type="password"
        placeholder="Пароль"
        autocomplete="current-password"
        required
      />
      <p v-if="error" class="error">{{ error }}</p>
      <button class="primary" :disabled="busy">Войти</button>
    </form>
  </div>
</template>

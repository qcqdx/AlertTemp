<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, auth } from '../api.js'

const incidents = ref([])
const controllers = ref([])
const filterStatus = ref('')
const filterType = ref('')
const filterController = ref('')
let timer = null

const TYPE_LABEL = { overheat: '🔥 Перегрев', overcool: '❄️ Переохлаждение', offline: '📡 Нет данных' }
const STATUS_LABEL = { open: 'Открыт', acknowledged: 'Подтверждён', resolved: 'Закрыт' }

function controllerName(id) {
  return controllers.value.find((c) => c.id === id)?.name || `#${id}`
}

function filterParams() {
  const params = new URLSearchParams()
  if (filterStatus.value) params.set('status', filterStatus.value)
  if (filterType.value) params.set('type', filterType.value)
  if (filterController.value) params.set('controller_id', filterController.value)
  return params
}

async function refresh() {
  const params = filterParams()
  params.set('limit', '200')
  incidents.value = await api.get(`/api/v1/incidents?${params}`)
}

// выгрузка журнала с теми же фильтрами, что на экране
const csvUrl = computed(() => {
  const qs = filterParams().toString()
  return `/api/v1/incidents/export.csv${qs ? `?${qs}` : ''}`
})

async function ack(incident) {
  // кто подтвердил — сервер берёт из сессии
  await api.post(`/api/v1/incidents/${incident.id}/ack`)
  await refresh()
}

function fmt(ts) {
  return ts ? new Date(ts).toLocaleString('ru-RU') : '—'
}

function duration(incident) {
  if (!incident.closed_at) return '…'
  const s = Math.round((new Date(incident.closed_at) - new Date(incident.opened_at)) / 1000)
  if (s >= 3600) return `${Math.floor(s / 3600)} ч ${Math.floor((s % 3600) / 60)} мин`
  if (s >= 60) return `${Math.floor(s / 60)} мин ${s % 60} с`
  return `${s} с`
}

onMounted(async () => {
  controllers.value = await api.get('/api/v1/controllers?include_archived=true')
  await refresh()
  timer = setInterval(refresh, 10000)
})
onUnmounted(() => clearInterval(timer))
watch([filterStatus, filterType, filterController], refresh)
</script>

<template>
  <div class="page">
    <h1>Журнал инцидентов</h1>
    <div class="form-row">
      <select v-model="filterStatus">
        <option value="">Все статусы</option>
        <option value="open">Открытые</option>
        <option value="acknowledged">Подтверждённые</option>
        <option value="resolved">Закрытые</option>
      </select>
      <select v-model="filterType">
        <option value="">Все типы</option>
        <option value="overheat">Перегрев</option>
        <option value="overcool">Переохлаждение</option>
        <option value="offline">Нет данных</option>
      </select>
      <select v-model="filterController">
        <option value="">Все холодильники</option>
        <option v-for="c in controllers" :key="c.id" :value="c.id">{{ c.name }}</option>
      </select>
      <a :href="csvUrl">⬇ CSV</a>
    </div>
    <div class="card" style="padding: 0; overflow-x: auto">
      <table>
        <thead>
          <tr>
            <th>Холодильник</th><th>Тип</th><th>Статус</th><th>Начало</th>
            <th>Длительность</th><th>Пик, °C</th><th>Подтвердил</th><th>Комментарий</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="incident in incidents" :key="incident.id">
            <td>
              <router-link :to="`/controllers/${incident.controller_id}`">
                {{ controllerName(incident.controller_id) }}
              </router-link>
            </td>
            <td>
              {{ TYPE_LABEL[incident.type] }}
              <span class="badge" :class="incident.severity === 'critical' ? 'crit' : 'warn'">
                {{ incident.severity === 'critical' ? 'крит' : 'предупр' }}
              </span>
            </td>
            <td>
              <span class="badge" :class="incident.status === 'resolved' ? 'ok' : incident.status === 'open' ? 'crit' : 'warn'">
                {{ STATUS_LABEL[incident.status] }}
              </span>
            </td>
            <td>{{ fmt(incident.opened_at) }}</td>
            <td>{{ duration(incident) }}</td>
            <td>{{ incident.peak_value ?? '—' }}</td>
            <td>{{ incident.acknowledged_by ?? '—' }}</td>
            <td>{{ incident.resolution_note ?? '' }}</td>
            <td style="white-space: nowrap">
              <router-link :to="`/incidents/${incident.id}/report`">Отчёт</router-link>
              <button v-if="auth.canOperate && incident.status === 'open'" @click="ack(incident)">
                Подтвердить
              </button>
            </td>
          </tr>
          <tr v-if="!incidents.length"><td colspan="9" class="empty">Ничего не найдено</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

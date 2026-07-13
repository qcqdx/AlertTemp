<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api.js'
import { connectLive } from '../live.js'

const controllers = ref([])
const latest = ref({}) // controller_id -> [SensorLatest]
const openIncidents = ref([])
const loaded = ref(false)
const live = ref(false)
let timer = null
let closeLive = null

function incidentFor(sensorId) {
  return openIncidents.value.find((i) => i.sensor_id === sensorId)
}

function sensorClass(sensor) {
  const incident = incidentFor(sensor.sensor_id)
  if (incident?.type === 'offline' || sensor.online === false) return 'offline'
  if (incident?.severity === 'critical') return 'crit'
  if (incident) return 'warn'
  return 'ok'
}

function controllerBadge(controller) {
  const sensors = latest.value[controller.id] || []
  const classes = sensors.map(sensorClass)
  if (classes.includes('crit')) return { text: 'АВАРИЯ', cls: 'crit' }
  if (classes.includes('offline')) return { text: 'НЕТ ДАННЫХ', cls: 'offline' }
  if (classes.includes('warn')) return { text: 'Внимание', cls: 'warn' }
  if (!sensors.length) return { text: 'Пусто', cls: 'muted' }
  return { text: 'Норма', cls: 'ok' }
}

async function refresh() {
  const [ctrls, incidents] = await Promise.all([
    api.get('/api/v1/controllers'),
    api.get('/api/v1/incidents/open'),
  ])
  controllers.value = ctrls
  openIncidents.value = incidents
  const updates = await Promise.all(
    ctrls.map((c) => api.get(`/api/v1/controllers/${c.id}/latest`))
  )
  const map = {}
  ctrls.forEach((c, i) => (map[c.id] = updates[i]))
  latest.value = map
  loaded.value = true
}

// приходящее измерение обновляет значение датчика без похода в API
function applyMeasurement(msg) {
  for (const sensors of Object.values(latest.value)) {
    const sensor = sensors.find((s) => s.sensor_id === msg.sensor_id)
    if (sensor) {
      sensor.value = msg.value
      sensor.time = msg.at
      sensor.online = true
      return
    }
  }
}

onMounted(() => {
  refresh()
  // WebSocket ускоряет обновление; поллинг оставлен как страховка и как
  // источник структурных изменений (новые контроллеры/датчики) — но реже
  timer = setInterval(refresh, 30000)
  closeLive = connectLive({
    onMeasurement: applyMeasurement,
    // переход инцидента меняет раскраску — перечитываем открытые аварии
    onIncident: () => api.get('/api/v1/incidents/open').then((i) => (openIncidents.value = i)),
    onStatus: (s) => (live.value = s === 'online'),
  })
})
onUnmounted(() => {
  clearInterval(timer)
  closeLive?.()
})
</script>

<template>
  <div class="page">
    <h1>
      Холодильники
      <span v-if="live" class="badge ok" style="font-size: 0.7rem; vertical-align: middle">
        ● живое обновление
      </span>
    </h1>
    <div v-if="loaded && !controllers.length" class="empty">
      Нет контроллеров. Добавьте их в разделе «Администрирование».
    </div>
    <div class="grid">
      <router-link
        v-for="controller in controllers"
        :key="controller.id"
        :to="`/controllers/${controller.id}`"
        class="card"
        style="color: inherit"
      >
        <div class="card-head">
          <div>
            <div class="name">{{ controller.name }}</div>
            <div class="loc">{{ controller.location || '&nbsp;' }}</div>
          </div>
          <span class="badge" :class="controllerBadge(controller).cls">
            {{ controllerBadge(controller).text }}
          </span>
        </div>
        <div
          v-for="sensor in latest[controller.id] || []"
          :key="sensor.sensor_id"
          class="sensor-row"
          :class="sensorClass(sensor)"
        >
          <span class="alias">{{ sensor.alias }}</span>
          <span class="value">
            <template v-if="sensor.value !== null">{{ sensor.value.toFixed(1) }}<small> °C</small></template>
            <template v-else>—</template>
          </span>
        </div>
      </router-link>
    </div>
  </div>
</template>

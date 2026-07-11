<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api.js'

const controllers = ref([])
const latest = ref({}) // controller_id -> [SensorLatest]
const openIncidents = ref([])
const loaded = ref(false)
let timer = null

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

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 5000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="page">
    <h1>Холодильники</h1>
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

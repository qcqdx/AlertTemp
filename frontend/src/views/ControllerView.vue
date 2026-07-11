<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, auth } from '../api.js'
import TempChart from '../components/TempChart.vue'

const props = defineProps({ id: { type: String, required: true } })

const controller = ref(null)
const latest = ref([])
const series = ref([])
const thresholds = ref(null) // активный профиль первого датчика — линии на графике
const incidents = ref([])
const error = ref('')
const range = ref('24h')
let timer = null

const RANGES = {
  '6h': { hours: 6, bucket: '1m' },
  '24h': { hours: 24, bucket: '1m' },
  '7d': { hours: 24 * 7, bucket: '10m' },
  '30d': { hours: 24 * 30, bucket: '1h' },
}

const activeSensors = computed(() =>
  (controller.value?.sensors || []).filter((s) => s.status !== 'archived')
)

async function loadChart() {
  const { hours, bucket } = RANGES[range.value]
  const start = new Date(Date.now() - hours * 3600 * 1000).toISOString()
  const data = await Promise.all(
    activeSensors.value.map((s) =>
      api.get(
        `/api/v1/sensors/${s.id}/measurements?start=${encodeURIComponent(start)}&bucket=${bucket}`
      )
    )
  )
  series.value = activeSensors.value.map((s, i) => ({ name: s.alias, points: data[i] }))
}

async function loadThresholds() {
  thresholds.value = null
  for (const sensor of activeSensors.value) {
    try {
      thresholds.value = await api.get(`/api/v1/sensors/${sensor.id}/thresholds`)
      break
    } catch {
      /* порогов нет */
    }
  }
}

async function refresh() {
  controller.value = await api.get(`/api/v1/controllers/${props.id}`)
  latest.value = await api.get(`/api/v1/controllers/${props.id}/latest`)
  incidents.value = await api.get(`/api/v1/incidents?controller_id=${props.id}&limit=50`)
}

async function loadAll() {
  error.value = ''
  try {
    await refresh()
    await Promise.all([loadChart(), loadThresholds()])
  } catch (e) {
    error.value = e.message
  }
}

// ---------- пороги (админ) ----------
const thrForm = ref(null)
const thrBusy = ref(false)

function editThresholds() {
  thrForm.value = thresholds.value
    ? { ...thresholds.value }
    : { warn_low: 2, warn_high: 8, crit_low: -0.5, crit_high: 15,
        hysteresis: 0.3, warn_delay_s: 300, crit_delay_s: 0 }
}

async function saveThresholds() {
  thrBusy.value = true
  error.value = ''
  try {
    const body = {
      warn_low: Number(thrForm.value.warn_low),
      warn_high: Number(thrForm.value.warn_high),
      crit_low: thrForm.value.crit_low === '' ? null : Number(thrForm.value.crit_low),
      crit_high: thrForm.value.crit_high === '' ? null : Number(thrForm.value.crit_high),
      hysteresis: Number(thrForm.value.hysteresis),
      warn_delay_s: Number(thrForm.value.warn_delay_s),
      crit_delay_s: Number(thrForm.value.crit_delay_s),
      created_by: auth.user.full_name,
    }
    // единый профиль на контроллер: применяем ко всем трём датчикам
    for (const sensor of activeSensors.value) {
      await api.put(`/api/v1/sensors/${sensor.id}/thresholds`, body)
    }
    thrForm.value = null
    await loadThresholds()
  } catch (e) {
    error.value = e.message
  } finally {
    thrBusy.value = false
  }
}

// ---------- инциденты ----------
async function ack(incident) {
  await api.post(`/api/v1/incidents/${incident.id}/ack`, {
    acknowledged_by: auth.user.full_name,
  })
  await refresh()
}

const TYPE_LABEL = { overheat: '🔥 Перегрев', overcool: '❄️ Переохлаждение', offline: '📡 Нет данных' }
const STATUS_LABEL = { open: 'Открыт', acknowledged: 'Подтверждён', resolved: 'Закрыт' }

function fmt(ts) {
  return ts ? new Date(ts).toLocaleString('ru-RU') : '—'
}

onMounted(() => {
  loadAll()
  timer = setInterval(refresh, 5000)
})
onUnmounted(() => clearInterval(timer))
watch(range, loadChart)
</script>

<template>
  <div class="page" v-if="controller">
    <h1>
      {{ controller.name }}
      <span class="hint" v-if="controller.location">· {{ controller.location }}</span>
    </h1>
    <p v-if="error" class="error">{{ error }}</p>

    <div class="grid" style="grid-template-columns: repeat(auto-fill, minmax(220px, 1fr))">
      <div v-for="sensor in latest" :key="sensor.sensor_id" class="sensor-row"
           :class="{ offline: sensor.online === false }" style="margin-top: 0">
        <span class="alias">{{ sensor.alias }}</span>
        <span class="value">
          <template v-if="sensor.value !== null">{{ sensor.value.toFixed(1) }}<small> °C</small></template>
          <template v-else>—</template>
        </span>
      </div>
    </div>

    <h2>График</h2>
    <div class="form-row">
      <button v-for="(cfg, key) in RANGES" :key="key"
              :class="{ primary: range === key }" @click="range = key">
        {{ key }}
      </button>
    </div>
    <div class="card">
      <TempChart :series="series" :thresholds="thresholds" />
    </div>

    <h2>
      Пороги
      <button v-if="auth.isAdmin && !thrForm" style="margin-left: 0.6rem" @click="editThresholds">
        Изменить
      </button>
    </h2>
    <div class="card" v-if="thrForm">
      <div class="form-row">
        <label>Норма от</label><input v-model="thrForm.warn_low" size="5" />
        <label>до</label><input v-model="thrForm.warn_high" size="5" />
        <label>Критично ниже</label><input v-model="thrForm.crit_low" size="5" />
        <label>выше</label><input v-model="thrForm.crit_high" size="5" />
      </div>
      <div class="form-row">
        <label>Гистерезис, °C</label><input v-model="thrForm.hysteresis" size="5" />
        <label>Задержка предупреждения, с</label><input v-model="thrForm.warn_delay_s" size="6" />
        <label>критического, с</label><input v-model="thrForm.crit_delay_s" size="6" />
      </div>
      <p class="hint">Профиль применяется ко всем датчикам контроллера. Изменение создаёт новую версию (история сохраняется).</p>
      <div class="form-row">
        <button class="primary" :disabled="thrBusy" @click="saveThresholds">Сохранить</button>
        <button @click="thrForm = null">Отмена</button>
      </div>
    </div>
    <p v-else-if="thresholds" class="hint">
      Норма {{ thresholds.warn_low }}…{{ thresholds.warn_high }} °C, критично
      &lt; {{ thresholds.crit_low ?? '—' }} / &gt; {{ thresholds.crit_high ?? '—' }} °C,
      гистерезис {{ thresholds.hysteresis }} °C, задержка {{ thresholds.warn_delay_s }} с
      (версия {{ thresholds.version }})
    </p>
    <p v-else class="hint">Пороги не заданы — детекция аварий для этого холодильника не работает!</p>

    <h2>Инциденты</h2>
    <div class="card" style="padding: 0; overflow-x: auto">
      <table>
        <thead>
          <tr><th>Тип</th><th>Статус</th><th>Начало</th><th>Конец</th><th>Пик</th><th>Подтвердил</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="incident in incidents" :key="incident.id">
            <td>
              {{ TYPE_LABEL[incident.type] }}
              <span class="badge" :class="incident.severity === 'critical' ? 'crit' : 'warn'">
                {{ incident.severity === 'critical' ? 'крит' : 'предупр' }}
              </span>
            </td>
            <td>{{ STATUS_LABEL[incident.status] }}</td>
            <td>{{ fmt(incident.opened_at) }}</td>
            <td>{{ fmt(incident.closed_at) }}</td>
            <td>{{ incident.peak_value ?? '—' }}</td>
            <td>{{ incident.acknowledged_by ?? '—' }}</td>
            <td>
              <button v-if="auth.canOperate && incident.status === 'open'" @click="ack(incident)">
                Подтвердить
              </button>
            </td>
          </tr>
          <tr v-if="!incidents.length"><td colspan="7" class="empty">Инцидентов нет</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

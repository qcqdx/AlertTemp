<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const props = defineProps({ id: { type: String, required: true } })

const report = ref(null)
const error = ref('')
// по умолчанию — последние сутки
const endLocal = ref(toLocalInput(new Date()))
const startLocal = ref(toLocalInput(new Date(Date.now() - 24 * 3600 * 1000)))

function toLocalInput(date) {
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

async function load() {
  error.value = ''
  try {
    const start = new Date(startLocal.value).toISOString()
    const end = new Date(endLocal.value).toISOString()
    report.value = await api.get(
      `/api/v1/controllers/${props.id}/report?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`
    )
  } catch (e) {
    error.value = e.message
  }
}

function fmt(ts) {
  return ts ? new Date(ts).toLocaleString('ru-RU') : '—'
}

function hours(seconds) {
  if (seconds === null || seconds === undefined) return '—'
  const h = Math.floor(seconds / 3600)
  const m = Math.round((seconds % 3600) / 60)
  return h ? `${h} ч ${m} мин` : `${m} мин`
}

const TYPE_LABEL = { overheat: 'Перегрев', overcool: 'Переохлаждение', offline: 'Нет данных' }

onMounted(load)
</script>

<template>
  <div class="page report-page">
    <div class="form-row no-print">
      <label>Период с</label>
      <input v-model="startLocal" type="datetime-local" />
      <label>по</label>
      <input v-model="endLocal" type="datetime-local" />
      <button class="primary" @click="load">Сформировать</button>
      <button @click="window.print ? window.print() : print()">🖨 Печать</button>
      <router-link :to="`/controllers/${props.id}`">← к холодильнику</router-link>
    </div>
    <p v-if="error" class="error">{{ error }}</p>

    <div v-if="report" class="report">
      <h1>Температурный журнал</h1>
      <table class="report-head">
        <tbody>
          <tr><td>Объект</td><td><b>{{ report.controller_name }}</b>
            <template v-if="report.location"> · {{ report.location }}</template></td></tr>
          <tr><td>Период</td><td>{{ fmt(report.period_start) }} — {{ fmt(report.period_end) }}</td></tr>
          <tr><td>Сформирован</td><td>{{ fmt(report.generated_at) }}, {{ report.generated_by }}</td></tr>
          <tr><td>Методика</td><td class="hint">{{ report.metric_note }}</td></tr>
        </tbody>
      </table>

      <div v-for="sensor in report.sensors" :key="sensor.sensor_id" class="report-sensor">
        <h2>{{ sensor.alias }} <span class="hint">({{ sensor.mqtt_topic }})</span></h2>

        <h3>Действовавшие пороги</h3>
        <table>
          <thead>
            <tr><th>Версия</th><th>Норма, °C</th><th>Критично, °C</th>
                <th>Задержка, с</th><th>Действовал</th></tr>
          </thead>
          <tbody>
            <tr v-for="profile in sensor.profiles" :key="profile.version">
              <td>v{{ profile.version }}</td>
              <td>{{ profile.warn_low }}…{{ profile.warn_high }}</td>
              <td>&lt; {{ profile.crit_low ?? '—' }} / &gt; {{ profile.crit_high ?? '—' }}</td>
              <td>{{ profile.warn_delay_s }}</td>
              <td>{{ fmt(profile.valid_from) }} — {{ profile.valid_to ? fmt(profile.valid_to) : 'конец периода' }}</td>
            </tr>
            <tr v-if="!sensor.profiles.length">
              <td colspan="5" class="hint">Пороги в периоде не действовали</td>
            </tr>
          </tbody>
        </table>

        <h3>Итоги периода</h3>
        <table>
          <tbody>
            <tr><td>Измерений</td><td>{{ sensor.samples }}</td>
                <td>Покрытие данными</td><td>{{ Math.round(sensor.coverage * 100) }}%</td></tr>
            <tr><td>Мин / Средняя / Макс, °C</td>
                <td>{{ sensor.min ?? '—' }} / {{ sensor.avg ?? '—' }} / {{ sensor.max ?? '—' }}</td>
                <td>MKT, °C</td><td><b>{{ sensor.mkt ?? '—' }}</b></td></tr>
            <tr><td>Вне диапазона (выше / ниже)</td>
                <td>{{ hours(sensor.out_above_s) }} / {{ hours(sensor.out_below_s) }}</td>
                <td>Всего вне диапазона</td><td><b>{{ hours(sensor.out_total_s) }}</b></td></tr>
          </tbody>
        </table>

        <h3>Слепые зоны (нет данных)</h3>
        <table v-if="sensor.gaps.length">
          <thead><tr><th>С</th><th>По</th><th>Длительность</th></tr></thead>
          <tbody>
            <tr v-for="(gap, i) in sensor.gaps" :key="i">
              <td>{{ fmt(gap.start) }}</td><td>{{ fmt(gap.end) }}</td>
              <td>{{ hours(gap.duration_s) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="hint">Разрывов данных длиннее 5 минут нет.</p>

        <h3>Инциденты периода</h3>
        <table v-if="sensor.incidents.length">
          <thead>
            <tr><th>№</th><th>Тип</th><th>Начало</th><th>Конец</th>
                <th>Пик, °C</th><th>Подтвердил</th><th>Комментарий</th></tr>
          </thead>
          <tbody>
            <tr v-for="incident in sensor.incidents" :key="incident.id">
              <td>{{ incident.id }}</td>
              <td>{{ TYPE_LABEL[incident.type] }}
                  {{ incident.severity === 'critical' ? '(крит)' : '' }}</td>
              <td>{{ fmt(incident.opened_at) }}</td>
              <td>{{ fmt(incident.closed_at) }}</td>
              <td>{{ incident.peak_value ?? '—' }}</td>
              <td>{{ incident.acknowledged_by ?? '—' }}</td>
              <td>{{ incident.resolution_note ?? '' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="hint">Инцидентов не зафиксировано.</p>
      </div>

      <p class="report-sign">Подпись ответственного: _____________________ Дата: ____________</p>
    </div>
  </div>
</template>

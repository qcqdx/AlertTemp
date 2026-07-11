<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api.js'

const discovered = ref([])
const controllers = ref([])
const recipients = ref([])
const users = ref([])
const error = ref('')
const notice = ref('')
let timer = null

async function refresh() {
  ;[discovered.value, controllers.value, recipients.value, users.value] = await Promise.all([
    api.get('/api/v1/discovery'),
    api.get('/api/v1/controllers?include_archived=false'),
    api.get('/api/v1/notify/recipients'),
    api.get('/api/v1/users'),
  ])
}

function run(action) {
  error.value = ''
  notice.value = ''
  return action().then(refresh).catch((e) => (error.value = e.message))
}

// ---------- контроллеры ----------
const newController = ref({ name: '', location: '' })

function addController() {
  return run(async () => {
    await api.post('/api/v1/controllers', { ...newController.value })
    newController.value = { name: '', location: '' }
  })
}

// ---------- обнаруженные датчики ----------
const bindForm = ref(null) // { topic, controller_id, alias, position }

function startBind(topic) {
  bindForm.value = { topic: topic.topic, controller_id: '', alias: '', position: 1 }
}

function freePositions(controllerId) {
  const controller = controllers.value.find((c) => c.id === Number(controllerId))
  const taken = (controller?.sensors || [])
    .filter((s) => s.status !== 'archived')
    .map((s) => s.position)
  return [1, 2, 3].filter((p) => !taken.includes(p))
}

function bindSensor() {
  return run(async () => {
    await api.post('/api/v1/sensors', {
      controller_id: Number(bindForm.value.controller_id),
      mqtt_topic: bindForm.value.topic,
      alias: bindForm.value.alias,
      position: Number(bindForm.value.position),
    })
    bindForm.value = null
  })
}

// ---------- датчики ----------
const sensorEdit = ref(null) // { id, alias, mqtt_topic, heartbeat_timeout_s }

function startSensorEdit(sensor) {
  sensorEdit.value = { ...sensor }
}

function saveSensor() {
  return run(async () => {
    const { id, alias, mqtt_topic, heartbeat_timeout_s } = sensorEdit.value
    await api.patch(`/api/v1/sensors/${id}`, {
      alias,
      mqtt_topic,
      heartbeat_timeout_s: Number(heartbeat_timeout_s),
    })
    sensorEdit.value = null
  })
}

function archiveSensor(sensor) {
  if (!confirm(`Архивировать датчик «${sensor.alias}»? История сохранится.`)) return
  return run(() => api.post(`/api/v1/sensors/${sensor.id}/archive`))
}

function archiveController(controller) {
  if (!confirm(`Архивировать «${controller.name}» со всеми датчиками?`)) return
  return run(() => api.post(`/api/v1/controllers/${controller.id}/archive`))
}

// ---------- получатели ----------
const newRecipient = ref({ name: '', chat_id: '' })

function addRecipient() {
  return run(async () => {
    await api.post('/api/v1/notify/recipients', { ...newRecipient.value })
    newRecipient.value = { name: '', chat_id: '' }
  })
}

async function sendTest() {
  error.value = ''
  try {
    const result = await api.post('/api/v1/notify/test')
    notice.value = 'Тест: ' + Object.entries(result).map(([k, v]) => `${k} → ${v}`).join(', ')
  } catch (e) {
    error.value = e.status === 503 ? 'Telegram не настроен (нет токена бота в .env)' : e.message
  }
}

// ---------- пользователи ----------
const newUser = ref({ username: '', password: '', full_name: '', role: 'viewer' })

function addUser() {
  return run(async () => {
    await api.post('/api/v1/users', { ...newUser.value })
    newUser.value = { username: '', password: '', full_name: '', role: 'viewer' }
  })
}

function fmt(ts) {
  return ts ? new Date(ts).toLocaleString('ru-RU') : '—'
}

onMounted(() => {
  refresh().catch((e) => (error.value = e.message))
  timer = setInterval(() => api.get('/api/v1/discovery').then((d) => (discovered.value = d)), 5000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="page">
    <h1>Администрирование</h1>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="notice" class="hint">{{ notice }}</p>

    <h2>Обнаруженные датчики ({{ discovered.length }})</h2>
    <p class="hint">
      Id датчиков, которые видны в эфире, но никуда не привязаны. По текущему значению легко
      опознать, какой это физически датчик.
    </p>
    <div class="card" style="padding: 0; overflow-x: auto">
      <table>
        <thead>
          <tr><th>Топик (id датчика)</th><th>Значение</th><th>Сообщений</th><th>Последнее</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="topic in discovered" :key="topic.id">
            <td><code>{{ topic.topic }}</code></td>
            <td><b>{{ topic.last_value ?? '—' }}</b></td>
            <td>{{ topic.message_count }}</td>
            <td>{{ fmt(topic.last_seen) }}</td>
            <td class="form-row" style="margin: 0">
              <button class="primary" @click="startBind(topic)">Привязать</button>
              <button @click="run(() => api.post(`/api/v1/discovery/${topic.id}/ignore`))">
                Игнорировать
              </button>
            </td>
          </tr>
          <tr v-if="!discovered.length"><td colspan="5" class="empty">Новых датчиков в эфире нет</td></tr>
        </tbody>
      </table>
    </div>

    <div v-if="bindForm" class="card" style="margin-top: 0.8rem">
      <b>Привязка: <code>{{ bindForm.topic }}</code></b>
      <div class="form-row">
        <select v-model="bindForm.controller_id">
          <option value="" disabled>Выберите контроллер…</option>
          <option v-for="c in controllers" :key="c.id" :value="c.id">{{ c.name }}</option>
        </select>
        <input v-model="bindForm.alias" placeholder="Псевдоним (например: Верхняя полка)" size="30" />
        <select v-model="bindForm.position">
          <option v-for="p in freePositions(bindForm.controller_id)" :key="p" :value="p">
            Позиция {{ p }}
          </option>
        </select>
        <button class="primary" :disabled="!bindForm.controller_id || !bindForm.alias" @click="bindSensor">
          Привязать
        </button>
        <button @click="bindForm = null">Отмена</button>
      </div>
    </div>

    <h2>Контроллеры и датчики</h2>
    <div class="form-row">
      <input v-model="newController.name" placeholder="Название (Холодильник аптеки №1)" size="34" />
      <input v-model="newController.location" placeholder="Расположение" size="24" />
      <button class="primary" :disabled="!newController.name" @click="addController">
        Добавить контроллер
      </button>
    </div>
    <div v-for="controller in controllers" :key="controller.id" class="card" style="margin-top: 0.8rem">
      <div class="card-head">
        <div>
          <span class="name">{{ controller.name }}</span>
          <span class="loc" v-if="controller.location"> · {{ controller.location }}</span>
        </div>
        <button class="danger" @click="archiveController(controller)">Архивировать</button>
      </div>
      <table style="margin-top: 0.6rem">
        <tbody>
          <tr v-for="sensor in controller.sensors.filter((s) => s.status !== 'archived')" :key="sensor.id">
            <template v-if="sensorEdit?.id === sensor.id">
              <td colspan="5">
                <div class="form-row" style="margin: 0">
                  <input v-model="sensorEdit.alias" size="24" />
                  <input v-model="sensorEdit.mqtt_topic" size="28" />
                  <label>таймаут, с</label>
                  <input v-model="sensorEdit.heartbeat_timeout_s" size="6" />
                  <button class="primary" @click="saveSensor">Сохранить</button>
                  <button @click="sensorEdit = null">Отмена</button>
                </div>
              </td>
            </template>
            <template v-else>
              <td style="width: 4rem">№{{ sensor.position }}</td>
              <td>{{ sensor.alias }}</td>
              <td><code>{{ sensor.mqtt_topic }}</code></td>
              <td class="hint">таймаут {{ sensor.heartbeat_timeout_s }} с</td>
              <td class="form-row" style="margin: 0">
                <button @click="startSensorEdit(sensor)">Изменить</button>
                <button class="danger" @click="archiveSensor(sensor)">Архив</button>
              </td>
            </template>
          </tr>
          <tr v-if="!controller.sensors.filter((s) => s.status !== 'archived').length">
            <td class="empty">Датчики не привязаны</td>
          </tr>
        </tbody>
      </table>
    </div>

    <h2>Получатели оповещений (Telegram)</h2>
    <div class="form-row">
      <input v-model="newRecipient.name" placeholder="Имя (Дежурная смена)" size="26" />
      <input v-model="newRecipient.chat_id" placeholder="chat_id" size="16" />
      <button class="primary" :disabled="!newRecipient.name || !newRecipient.chat_id" @click="addRecipient">
        Добавить
      </button>
      <button @click="sendTest">Отправить тестовое</button>
    </div>
    <div class="card" style="padding: 0; overflow-x: auto">
      <table>
        <tbody>
          <tr v-for="recipient in recipients" :key="recipient.id">
            <td>{{ recipient.name }}</td>
            <td><code>{{ recipient.chat_id }}</code></td>
            <td>
              <span class="badge" :class="recipient.enabled ? 'ok' : 'muted'">
                {{ recipient.enabled ? 'включён' : 'выключен' }}
              </span>
            </td>
            <td class="form-row" style="margin: 0">
              <button @click="run(() => api.patch(`/api/v1/notify/recipients/${recipient.id}`, { enabled: !recipient.enabled }))">
                {{ recipient.enabled ? 'Выключить' : 'Включить' }}
              </button>
              <button class="danger" @click="run(() => api.delete(`/api/v1/notify/recipients/${recipient.id}`))">
                Удалить
              </button>
            </td>
          </tr>
          <tr v-if="!recipients.length"><td class="empty">Получатели не настроены — оповещения никому не уходят!</td></tr>
        </tbody>
      </table>
    </div>

    <h2>Пользователи</h2>
    <div class="form-row">
      <input v-model="newUser.username" placeholder="Логин" size="14" />
      <input v-model="newUser.full_name" placeholder="ФИО" size="24" />
      <input v-model="newUser.password" type="password" placeholder="Пароль (мин. 8)" size="16" />
      <select v-model="newUser.role">
        <option value="viewer">Наблюдатель</option>
        <option value="operator">Оператор</option>
        <option value="admin">Администратор</option>
      </select>
      <button class="primary"
              :disabled="!newUser.username || newUser.password.length < 8 || !newUser.full_name"
              @click="addUser">
        Создать
      </button>
    </div>
    <div class="card" style="padding: 0; overflow-x: auto">
      <table>
        <tbody>
          <tr v-for="user in users" :key="user.id">
            <td><b>{{ user.username }}</b></td>
            <td>{{ user.full_name }}</td>
            <td>{{ user.role }}</td>
            <td>
              <span class="badge" :class="user.enabled ? 'ok' : 'muted'">
                {{ user.enabled ? 'активен' : 'заблокирован' }}
              </span>
            </td>
            <td>
              <button @click="run(() => api.patch(`/api/v1/users/${user.id}`, { enabled: !user.enabled }))">
                {{ user.enabled ? 'Заблокировать' : 'Разблокировать' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

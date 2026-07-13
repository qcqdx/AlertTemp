// Живая раздача событий по WebSocket (этап B.3).
//
// Заменяет 5-секундный поллинг там, где нужна свежесть (значения датчиков,
// появление/закрытие инцидентов). Деградация мягкая: если WebSocket не
// поднялся или оборвался, вызывающий продолжает жить на поллинге —
// раздача только ускоряет обновление, не является единственным источником.
//
// Контракт сообщений см. backend/app/api/routes/ws.py.

export function connectLive({ onMeasurement, onIncident, onStatus } = {}) {
  let ws = null
  let closed = false
  let retry = 0
  let retryTimer = null

  function url() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${proto}//${window.location.host}/api/v1/ws`
  }

  function open() {
    if (closed) return
    ws = new WebSocket(url())

    ws.onopen = () => {
      retry = 0
      onStatus?.('online')
    }

    ws.onmessage = (event) => {
      let msg
      try {
        msg = JSON.parse(event.data)
      } catch {
        return
      }
      if (msg.type === 'measurement') onMeasurement?.(msg)
      else if (msg.type === 'incident') onIncident?.(msg)
    }

    ws.onclose = () => {
      onStatus?.('offline')
      if (closed) return
      // экспоненциальный бэкофф до 30 с; поллинг вызывающего закрывает
      // разрыв, пока сокет переподключается
      retry = Math.min(retry + 1, 6)
      retryTimer = setTimeout(open, Math.min(1000 * 2 ** retry, 30000))
    }

    ws.onerror = () => ws && ws.close()
  }

  open()

  return function close() {
    closed = true
    if (retryTimer) clearTimeout(retryTimer)
    if (ws) ws.close()
  }
}

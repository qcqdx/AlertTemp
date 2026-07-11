import { reactive } from 'vue'

export const auth = reactive({
  user: null, // { id, username, full_name, role }
  get isAdmin() {
    return this.user?.role === 'admin'
  },
  get canOperate() {
    return this.user?.role === 'admin' || this.user?.role === 'operator'
  },
})

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`)
    this.status = status
  }
}

async function request(method, path, body) {
  const options = { method, credentials: 'same-origin' }
  if (body !== undefined) {
    options.headers = { 'Content-Type': 'application/json' }
    options.body = JSON.stringify(body)
  }
  const response = await fetch(path, options)
  if (response.status === 401 && !path.startsWith('/api/v1/auth/')) {
    auth.user = null
    window.location.hash = '#/login'
    throw new ApiError(401, 'Требуется вход')
  }
  if (!response.ok) {
    let detail
    try {
      const data = await response.json()
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {
      detail = response.statusText
    }
    throw new ApiError(response.status, detail)
  }
  if (response.status === 204) return null
  return response.json()
}

export const api = {
  get: (path) => request('GET', path),
  post: (path, body) => request('POST', path, body),
  put: (path, body) => request('PUT', path, body),
  patch: (path, body) => request('PATCH', path, body),
  delete: (path) => request('DELETE', path),
}

export async function loadMe() {
  try {
    auth.user = await api.get('/api/v1/auth/me')
  } catch {
    auth.user = null
  }
  return auth.user
}

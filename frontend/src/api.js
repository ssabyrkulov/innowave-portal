const API_BASE = import.meta.env.VITE_API_BASE || '/api'

const TOKEN_KEY = 'pc_token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

async function request(path, { method = 'GET', body, form } = {}) {
  const headers = {}
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  let payload
  if (form) {
    payload = new URLSearchParams(form).toString()
    headers['Content-Type'] = 'application/x-www-form-urlencoded'
  } else if (body !== undefined) {
    payload = JSON.stringify(body)
    headers['Content-Type'] = 'application/json'
  }

  const res = await fetch(`${API_BASE}${path}`, { method, headers, body: payload })

  if (res.status === 401) {
    setToken(null)
    throw new Error('Сессия истекла. Войдите снова.')
  }
  if (res.status === 204) return null
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    const detail = data?.detail
    const message = Array.isArray(detail)
      ? detail.map((d) => d.msg).join(', ')
      : detail || 'Ошибка запроса'
    throw new Error(message)
  }
  return data
}

export const api = {
  login: (email, password) =>
    request('/auth/login', { method: 'POST', form: { username: email, password } }),
  me: () => request('/auth/me'),

  listUsers: () => request('/users'),
  createUser: (body) => request('/users', { method: 'POST', body }),
  updateUser: (id, body) => request(`/users/${id}`, { method: 'PATCH', body }),
  deleteUser: (id) => request(`/users/${id}`, { method: 'DELETE' }),

  listPayments: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null && v !== '')
    ).toString()
    return request(`/payments${qs ? `?${qs}` : ''}`)
  },
  summary: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null && v !== '')
    ).toString()
    return request(`/payments/summary${qs ? `?${qs}` : ''}`)
  },
  createPayment: (body) => request('/payments', { method: 'POST', body }),
  updatePayment: (id, body) => request(`/payments/${id}`, { method: 'PATCH', body }),
  deletePayment: (id) => request(`/payments/${id}`, { method: 'DELETE' }),
}

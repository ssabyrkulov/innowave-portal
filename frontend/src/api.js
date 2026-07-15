// Empty string = same origin (single-service deploy where the backend also
// serves the frontend). Undefined (local dev) falls back to the /api proxy.
const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

const TOKEN_KEY = 'pc_token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

async function request(path, { method = 'GET', body, form, formData } = {}) {
  const headers = {}
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  let payload
  if (formData) {
    payload = formData // Content-Type с boundary браузер выставит сам
  } else if (form) {
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

  importSales: (file, replacePeriod = false) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('replace_period', replacePeriod ? 'true' : 'false')
    return request('/sales/import', { method: 'POST', formData: fd })
  },
  importLog: () => request('/sales/imports'),

  importReceipts: (file, replacePeriod = false) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('replace_period', replacePeriod ? 'true' : 'false')
    return request('/receipts/import', { method: 'POST', formData: fd })
  },
  receivables: () => request('/receipts/receivables'),
  listReceipts: () => request('/receipts'),
  setReceiptRate: (id, rate) =>
    request(`/receipts/${id}/rate`, { method: 'PATCH', body: { rate } }),
  createAlias: (payer, client) =>
    request('/receipts/alias', { method: 'POST', body: { payer, client } }),

  dashboard: () => request('/dashboard'),

  agentsSummary: () => request('/agents/summary'),
  resetImportedData: () => request('/integrations/reset', { method: 'POST' }),
  setAgentTarget: (agent, month, amount) =>
    request('/agents/target', { method: 'POST', body: { agent, month, amount } }),

  checks: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null && v !== '' && v !== false)
    ).toString()
    return request(`/checks${qs ? `?${qs}` : ''}`)
  },
  checksCount: () => request('/checks/count'),
  ackViolation: (vhash) => request(`/checks/${vhash}/ack`, { method: 'POST' }),
  unackViolation: (vhash) => request(`/checks/${vhash}/ack`, { method: 'DELETE' }),
  salesSummary: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null && v !== '')
    ).toString()
    return request(`/sales/summary${qs ? `?${qs}` : ''}`)
  },

  salesProducts: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null && v !== '')
    ).toString()
    return request(`/sales/products${qs ? `?${qs}` : ''}`)
  },

  budgetPlanFact: (period) => request(`/budget/plan-fact?period=${period}`),
  budgetPlan: (period) => request(`/budget?period=${period}`),
  budgetUpsert: (body) => request('/budget', { method: 'PUT', body }),
  budgetDelete: (id) => request(`/budget/${id}`, { method: 'DELETE' }),
  budgetFactArticles: (period) => request(`/budget/fact-articles?period=${period}`),

  stockBalances: () => request('/balances/stock'),

  agentWork: (agent) => request(`/agents/work${agent ? `?agent=${encodeURIComponent(agent)}` : ''}`),
  addActivity: (body) => request('/agents/activity', { method: 'POST', body }),
  listActivity: (client, agent) => {
    const qs = new URLSearchParams({ client, ...(agent ? { agent } : {}) }).toString()
    return request(`/agents/activity?${qs}`)
  },
}

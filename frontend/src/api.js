// Empty string = same origin (single-service deploy where the backend also
// serves the frontend). Undefined (local dev) falls back to the /api proxy.
const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

const TOKEN_KEY = 'pc_token'
const ORG_KEY = 'pc_org'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

// Выбранная организация ('all' | 'hygiene' | 'innowave') — добавляется ко всем
// запросам как ?org=; эндпоинты, которым она не нужна, её игнорируют.
export function getOrg() {
  return localStorage.getItem(ORG_KEY) || 'all'
}

export function setOrg(org) {
  localStorage.setItem(ORG_KEY, org)
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

  const sep = path.includes('?') ? '&' : '?'
  const url = `${API_BASE}${path}${sep}org=${encodeURIComponent(getOrg())}`
  const res = await fetch(url, { method, headers, body: payload })

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

  // Какая версия портала развёрнута на сервере: короткий SHA коммита и имя
  // собранного JS-бандла. По второму видно, ту ли сборку показывает браузер.
  health: () => request('/healthz'),

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

  importLog: () => request('/sales/imports'),

  receivables: () => request('/receipts/receivables'),
  listReceipts: () => request('/receipts'),
  setReceiptRate: (id, rate) =>
    request(`/receipts/${id}/rate`, { method: 'PATCH', body: { rate } }),
  createAlias: (payer, client) =>
    request('/receipts/alias', { method: 'POST', body: { payer, client } }),
  clientDetail: (client) =>
    request(`/receipts/client-detail?client=${encodeURIComponent(client)}`),
  listFlags: () => request('/receipts/flags'),
  setFlag: (client, kind, note) =>
    request('/receipts/flags', { method: 'POST', body: { client, kind, note } }),
  removeFlag: (client) =>
    request(`/receipts/flags/${encodeURIComponent(client)}`, { method: 'DELETE' }),

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

  salesdocStatus: () => request('/salesdoc/status'),
  // Причина расхождения считается всегда (из зеркала — бесплатно), поэтому
  // отдельный флаг не нужен: сервер включает её по умолчанию.
  salesdocDebt: (onlyDiff = false, refresh = false) => {
    const qs = new URLSearchParams()
    if (onlyDiff) qs.set('only_diff', 'true')
    if (refresh) qs.set('refresh', 'true')
    const s = qs.toString()
    return request(`/salesdoc/debt${s ? `?${s}` : ''}`)
  },
  salesdocPeriod: (dateFrom, dateTo) =>
    request(`/salesdoc/period?date_from=${dateFrom}&date_to=${dateTo}`),
  salesdocAnalyze: (dateFrom, dateTo) =>
    request(`/salesdoc/analyze?date_from=${dateFrom}&date_to=${dateTo}`),
  salesdocWarehouseReport: (dateFrom, dateTo) =>
    request(`/salesdoc/warehouse-report?date_from=${dateFrom}&date_to=${dateTo}`),
  salesdocPaymentsDebug: (dateFrom, dateTo) =>
    request(`/salesdoc/payments-debug?date_from=${dateFrom}&date_to=${dateTo}`),
  salesdocReturnsDebug: (dateFrom, dateTo) =>
    request(`/salesdoc/returns-debug?date_from=${dateFrom}&date_to=${dateTo}`),
  salesdocSpeedProbe: () => request('/salesdoc/speed-probe'),
  salesdocStock: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null && v !== '' && v !== false)
    ).toString()
    return request(`/salesdoc/stock${qs ? `?${qs}` : ''}`)
  },
  salesdocClientDebug: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null && v !== '')
    ).toString()
    return request(`/salesdoc/client-debug${qs ? `?${qs}` : ''}`)
  },
  purchasesSummary: () => request('/purchases/summary'),
  writeoffsSummary: () => request('/writeoffs/summary'),
  writeoffsLines: () => request('/writeoffs/lines'),
  purchasesLines: () => request('/purchases/lines'),
  stockCalc: () => request('/purchases/stock-calc'),
  stockCompare: () => request('/purchases/stock-compare'),
  taxSummary: () => request('/tax/summary'),
  taxCompare: () => request('/tax/compare'),
  taxDocs: (kind) => request(`/tax/docs?kind=${encodeURIComponent(kind)}`),
  taxLinks: () => request('/tax/links'),
  taxGroups: () => request('/tax/groups'),
  taxLinkSave: (taxName, uprNames) =>
    request('/tax/links', { method: 'POST', body: { tax_name: taxName, upr_names: uprNames || [] } }),
  taxImport: (file, org) => {
    const fd = new FormData()
    fd.append('file', file, file.name)
    fd.append('org', org || 'hygiene')
    return request('/tax/import', { method: 'POST', formData: fd })
  },
  salesdocCashboxes: () => request('/salesdoc/cashboxes'),
  salesdocOrderChanges: () => request('/salesdoc/order-changes'),
  salesdocPaymentTypes: () => request('/salesdoc/payment-types'),
  salesdocPaymentsByType: ({ date_from, date_to, type_id }) => {
    const qs = new URLSearchParams({ date_from, date_to })
    if (type_id) qs.set('type_id', type_id)
    return request(`/salesdoc/payments-by-type?${qs.toString()}`)
  },
  salesdocPaymentsDay: (day, live) =>
    request(`/salesdoc/payments-day?day=${day}${live ? '&live=true' : ''}`),
  salesdocSetClientFirm: (sd_id, org) =>
    request(`/salesdoc/client-firm?sd_id=${encodeURIComponent(sd_id)}&org=${encodeURIComponent(org)}`,
      { method: 'POST' }),
  salesdocWhy: (query) => request(`/salesdoc/why?query=${encodeURIComponent(query)}`),
  salesdocMethodProbe: () => request('/salesdoc/method-probe'),
  salesdocJournalAnatomy: () => request('/salesdoc/journal-anatomy'),
  salesdocAgentModel: () => request('/salesdoc/agent-model'),
  salesdocTxnTypes: () => request('/salesdoc/txn-types'),
  salesdocByGuid: () => request('/salesdoc/by-guid'),
  salesdocStoreLog: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null && v !== '')
    ).toString()
    return request(`/salesdoc/store-log${qs ? `?${qs}` : ''}`)
  },
  salesdocStoreLogDebug: () => request('/salesdoc/store-log-debug'),
  salesdocMovementsProbe: (method) =>
    request(`/salesdoc/movements-probe${method ? `?method=${encodeURIComponent(method)}` : ''}`),
  salesdocHiddenOrdersProbe: ({ sd_id, code_1c, number }) => {
    const qs = new URLSearchParams()
    if (sd_id) qs.set('sd_id', sd_id)
    if (code_1c) qs.set('code_1c', code_1c)
    if (number) qs.set('number', number)
    return request(`/salesdoc/hidden-orders-probe?${qs.toString()}`)
  },
  salesdocIdMatch: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null && v !== '')
    ).toString()
    return request(`/salesdoc/id-match${qs ? `?${qs}` : ''}`)
  },
  salesdocVisitsSample: () => request('/salesdoc/visits-sample'),
  salesdocVisitDebt: () => request('/salesdoc/visit-debt'),
  salesdocAgentsToday: (day) =>
    request(`/salesdoc/agents-today${day ? `?day=${day}` : ''}`),
  salesdocApiProbe: (params) =>
    request('/salesdoc/api-probe?' + new URLSearchParams(params).toString()),
  // Любое сочетание условий: сумма, точка, период. Пустые не отправляем —
  // сервер отличает «не задано» от «задано пустым».
  salesdocFindDoc: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null && v !== '')
    ).toString()
    return request(`/salesdoc/find-doc${qs ? `?${qs}` : ''}`)
  },
  salesdocPaymentRaw: (sdId) =>
    request(`/salesdoc/payment-raw?sd_id=${encodeURIComponent(sdId)}`),
  salesdocOrderRaw: (sdId) =>
    request(`/salesdoc/order-raw?sd_id=${encodeURIComponent(sdId)}`),
  salesdocStoreClients: () => request('/salesdoc/store-clients'),
  salesdocStoreOrders: (storeId) =>
    request(`/salesdoc/store-orders?store_id=${encodeURIComponent(storeId)}`),
  salesdocShipmentsCompare: (params) =>
    request('/salesdoc/shipments-compare?' + new URLSearchParams(params).toString()),
  salesdocMirror: () => request('/salesdoc/mirror'),
  salesdocMirrorSync: (full = false, docsOnly = false) => {
    const qs = new URLSearchParams()
    if (full) qs.set('full', 'true')
    if (docsOnly) qs.set('docs_only', 'true')
    const tail = qs.toString()
    return request(`/salesdoc/mirror/sync${tail ? '?' + tail : ''}`, { method: 'POST' })
  },
  salesdocMatching: () => request('/salesdoc/matching'),
  salesdocLink: (client_1c, sd_id) =>
    request('/salesdoc/link', { method: 'POST', body: { client_1c, sd_id } }),
  salesdocUnlink: (client_1c) =>
    request(`/salesdoc/link/${encodeURIComponent(client_1c)}`, { method: 'DELETE' }),
  salesdocWarehouses: () => request('/salesdoc/warehouses'),
  salesdocSaveWarehouses: (list) =>
    request('/salesdoc/warehouses', { method: 'POST', body: list }),
  salesdocClientDetail: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null && v !== '')
    ).toString()
    return request(`/salesdoc/client-detail${qs ? `?${qs}` : ''}`)
  },

  operationTypes: () => request('/operations/types'),
  operationsFreshness: () => request('/operations/freshness'),
  operations: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v != null && v !== '')
    ).toString()
    return request(`/operations${qs ? `?${qs}` : ''}`)
  },

  agentWork: (agent) => request(`/agents/work${agent ? `?agent=${encodeURIComponent(agent)}` : ''}`),
  addActivity: (body) => request('/agents/activity', { method: 'POST', body }),
  listActivity: (client, agent) => {
    const qs = new URLSearchParams({ client, ...(agent ? { agent } : {}) }).toString()
    return request(`/agents/activity?${qs}`)
  },
}

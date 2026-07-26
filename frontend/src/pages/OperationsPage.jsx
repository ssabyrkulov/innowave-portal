import { useEffect, useRef, useState } from 'react'
import { api } from '../api'

const money = (v) =>
  Number(v || 0).toLocaleString('ru-RU', { maximumFractionDigits: 2 })
const num = (v) =>
  Number(v || 0).toLocaleString('ru-RU', { maximumFractionDigits: 3 })
const fmtDate = (iso) => (iso ? iso.split('-').reverse().join('.') : '—')

const PAGE_SIZES = [25, 50, 100, 200]

// Полоса свежести: до какого числа доехали данные по каждому виду операций.
// Отстающие подсвечены — сразу видно, какая выгрузка из 1С не обновляется.
function FreshnessBar({ fresh, current, onPick }) {
  const LAG_WARN = 2 // дня отставания, после которых подсвечиваем
  return (
    <div className="fresh-bar">
      <span className="muted fresh-title">Данные загружены до:</span>
      {fresh.types.map((t) => {
        const lag = t.days_behind
        const stale = lag != null && lag >= LAG_WARN
        return (
          <button
            key={t.type}
            className={`fresh-chip ${stale ? 'fresh-stale' : ''} ${t.type === current ? 'fresh-current' : ''}`}
            onClick={() => onPick(t.type)}
            title={
              t.last_date
                ? `${t.label}: ${t.count.toLocaleString('ru-RU')} записей` +
                  (stale ? `, отстаёт на ${lag} дн.` : '')
                : `${t.label}: данных нет`
            }
          >
            {t.label}: <b>{fmtDate(t.last_date)}</b>
            {stale && ` · −${lag} дн.`}
          </button>
        )
      })}
    </div>
  )
}

function renderCell(col, value) {
  if (col.type === 'date') return fmtDate(value)
  if (value == null || value === '') return '—'
  if (col.type === 'money') return money(value)
  if (col.type === 'num') return num(value)
  return value
}

export default function OperationsPage() {
  const [types, setTypes] = useState([])
  const [type, setType] = useState('sales')
  const [filters, setFilters] = useState({ date_from: '', date_to: '', q: '' })
  const [qInput, setQInput] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [fresh, setFresh] = useState(null)
  const qTimer = useRef(null)

  // Список вкладок один раз
  useEffect(() => {
    api.operationTypes().then(setTypes).catch((e) => setError(e.message))
    api.operationsFreshness().then(setFresh).catch(() => {})
  }, [])

  // Дебаунс текстового поиска: печатаем в qInput, в фильтры уходит с паузой
  useEffect(() => {
    if (qTimer.current) clearTimeout(qTimer.current)
    qTimer.current = setTimeout(() => {
      setFilters((f) => (f.q === qInput ? f : { ...f, q: qInput }))
      setPage(1)
    }, 400)
    return () => qTimer.current && clearTimeout(qTimer.current)
  }, [qInput])

  // Загрузка данных при смене вкладки/фильтров/страницы
  useEffect(() => {
    let alive = true
    setLoading(true)
    setError(null)
    api
      .operations({ type, ...filters, page, page_size: pageSize })
      .then((d) => {
        if (alive) setData(d)
      })
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [type, filters, page, pageSize])

  function switchTab(t) {
    if (t === type) return
    setType(t)
    setPage(1)
  }

  function setDate(field, value) {
    setFilters((f) => ({ ...f, [field]: value }))
    setPage(1)
  }

  const columns = data?.columns || types.find((t) => t.type === type)?.columns || []
  const rows = data?.rows || []
  const hasFilters = filters.date_from || filters.date_to || filters.q

  return (
    <div className="ops">
      <div className="page-header">
        <h1>Операции</h1>
        {data && (
          <div className="ops-count muted">
            {data.total.toLocaleString('ru-RU')}{' '}
            {data.total === 1 ? 'запись' : 'записей'}
          </div>
        )}
      </div>

      {/* Вкладки типов операций */}
      <div className="ops-tabs">
        {types.map((t) => (
          <button
            key={t.type}
            className={`ops-tab ${t.type === type ? 'active' : ''}`}
            onClick={() => switchTab(t.type)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Свежесть данных: до какого числа загружено по каждому виду. Если
          какая-то выгрузка из 1С отстала, это видно сразу. */}
      {fresh && <FreshnessBar fresh={fresh} current={type} onPick={switchTab} />}

      {/* Фильтры */}
      <div className="filters ops-filters">
        <input
          className="product-search-input"
          placeholder="Поиск: клиент, контрагент, товар…"
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
        />
        <label className="ops-date">
          <span>с</span>
          <input
            type="date"
            className="filter-select"
            value={filters.date_from}
            onChange={(e) => setDate('date_from', e.target.value)}
          />
        </label>
        <label className="ops-date">
          <span>по</span>
          <input
            type="date"
            className="filter-select"
            value={filters.date_to}
            onChange={(e) => setDate('date_to', e.target.value)}
          />
        </label>
        <select
          className="filter-select"
          value={pageSize}
          onChange={(e) => {
            setPageSize(Number(e.target.value))
            setPage(1)
          }}
        >
          {PAGE_SIZES.map((n) => (
            <option key={n} value={n}>
              {n} на странице
            </option>
          ))}
        </select>
        {hasFilters && (
          <button
            className="btn btn-sm btn-ghost"
            onClick={() => {
              setFilters({ date_from: '', date_to: '', q: '' })
              setQInput('')
              setPage(1)
            }}
          >
            Сбросить
          </button>
        )}
      </div>

      {/* Итог по выборке */}
      {data && (
        <div className="summary-bar ops-summary">
          <div className="summary-card">
            <span className="summary-label">Итого по выборке</span>
            <span className="summary-value">{money(data.total_amount)} сом</span>
          </div>
          <div className="summary-card">
            <span className="summary-label">Строк</span>
            <span className="summary-value">
              {data.total.toLocaleString('ru-RU')}
            </span>
          </div>
        </div>
      )}

      {error && <div className="error">{error}</div>}

      <div className="table-wrap cards ops-table">
        <table>
          <thead>
            <tr>
              {columns.map((c) => (
                <th
                  key={c.key}
                  className={c.type === 'money' || c.type === 'num' ? 'num' : ''}
                >
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && !loading && (
              <tr>
                <td colSpan={columns.length} className="muted ops-empty">
                  Ничего не найдено
                </td>
              </tr>
            )}
            {rows.map((r, i) => (
              <tr key={i}>
                {columns.map((c) => (
                  <td
                    key={c.key}
                    data-label={c.label}
                    className={c.type === 'money' || c.type === 'num' ? 'num' : ''}
                  >
                    {renderCell(c, r[c.key])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {loading && <div className="ops-loading muted">Загрузка…</div>}
      </div>

      {/* Пагинация */}
      {data && data.pages > 1 && (
        <div className="ops-pager">
          <button
            className="btn btn-sm"
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            ‹ Назад
          </button>
          <span className="ops-pager-info">
            Стр. {data.page} из {data.pages}
          </span>
          <button
            className="btn btn-sm"
            disabled={page >= data.pages}
            onClick={() => setPage((p) => p + 1)}
          >
            Вперёд ›
          </button>
        </div>
      )}
    </div>
  )
}

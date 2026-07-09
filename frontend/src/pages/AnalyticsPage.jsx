import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'
import { formatMoney } from '../utils'

const MONTH_SHORT = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']

function shortMoney(v) {
  const n = Number(v || 0)
  if (Math.abs(n) >= 1e6) return (n / 1e6).toLocaleString('ru-RU', { maximumFractionDigits: 1 }) + ' млн'
  if (Math.abs(n) >= 1e3) return (n / 1e3).toLocaleString('ru-RU', { maximumFractionDigits: 0 }) + ' тыс'
  return n.toLocaleString('ru-RU', { maximumFractionDigits: 0 })
}

function monthLabel(key) {
  const [y, m] = key.split('-')
  return `${MONTH_SHORT[Number(m) - 1]} ${y.slice(2)}`
}

export default function AnalyticsPage() {
  const { can } = useAuth()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [importing, setImporting] = useState(false)
  const [importResult, setImportResult] = useState(null)
  const fileRef = useRef(null)

  async function load() {
    setError(null)
    try {
      setData(await api.salesSummary({ date_from: dateFrom, date_to: dateTo }))
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dateFrom, dateTo])

  async function onFile(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setImporting(true)
    setImportResult(null)
    setError(null)
    try {
      const res = await api.importSales(file)
      setImportResult(res)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setImporting(false)
    }
  }

  const empty = data && data.lines === 0 && !dateFrom && !dateTo

  return (
    <div>
      <div className="page-header">
        <h1>Аналитика продаж</h1>
        {can.editPayments && (
          <>
            <input
              ref={fileRef}
              type="file"
              accept=".xlsx,.xlsm"
              style={{ display: 'none' }}
              onChange={onFile}
            />
            <button
              className="btn btn-primary"
              disabled={importing}
              onClick={() => fileRef.current?.click()}
            >
              {importing ? 'Загрузка…' : '⬆ Загрузить Excel из 1С'}
            </button>
          </>
        )}
      </div>

      {importResult && (
        <div className="import-result">
          Импорт завершён: добавлено <b>{importResult.added}</b>
          {importResult.skipped_duplicates > 0 && (
            <>, пропущено дублей: <b>{importResult.skipped_duplicates}</b></>
          )}
          {importResult.errors.length > 0 && (
            <details>
              <summary>Строк с ошибками: {importResult.errors.length}</summary>
              <ul>
                {importResult.errors.map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}

      {error && <div className="error">{error}</div>}

      {empty ? (
        <div className="table-wrap">
          <div className="center muted">
            Данных о продажах пока нет.
            {can.editPayments
              ? ' Нажмите «Загрузить Excel из 1С» и выберите файл выгрузки (РеализацияТоваровУслуг).'
              : ' Попросите бухгалтера загрузить выгрузку из 1С.'}
          </div>
        </div>
      ) : data ? (
        <>
          <div className="filters">
            <label className="filter-inline">
              С
              <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
            </label>
            <label className="filter-inline">
              По
              <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
            </label>
            {(dateFrom || dateTo) && (
              <button className="btn btn-ghost" onClick={() => { setDateFrom(''); setDateTo('') }}>
                Сбросить
              </button>
            )}
          </div>

          <div className="summary-bar">
            <div className="summary-card">
              <span className="summary-label">Выручка</span>
              <span className="summary-value">{formatMoney(data.revenue)}</span>
            </div>
            <div className="summary-card">
              <span className="summary-label">Накладных</span>
              <span className="summary-value">{data.docs.toLocaleString('ru-RU')}</span>
            </div>
            <div className="summary-card">
              <span className="summary-label">Клиентов</span>
              <span className="summary-value">{data.clients.toLocaleString('ru-RU')}</span>
            </div>
            <div className="summary-card">
              <span className="summary-label">Средняя накладная</span>
              <span className="summary-value">{formatMoney(data.avg_doc)}</span>
            </div>
          </div>

          {data.monthly.length > 0 && (
            <div className="chart-card">
              <h2 className="chart-title">Выручка по месяцам</h2>
              <ColumnChart points={data.monthly} />
            </div>
          )}

          <div className="tops-grid">
            <TopTable
              title="Топ клиентов"
              rows={data.top_clients}
              cols={[['revenue', 'Выручка'], ['docs', 'Накладных']]}
            />
            <TopTable
              title="Топ товаров"
              rows={data.top_products}
              cols={[['revenue', 'Выручка'], ['qty', 'Кол-во']]}
            />
            <TopTable
              title="Топ агентов"
              rows={data.top_agents}
              cols={[['revenue', 'Выручка'], ['docs', 'Накладных']]}
            />
          </div>
        </>
      ) : (
        <div className="center muted">Загрузка…</div>
      )}
    </div>
  )
}

function ColumnChart({ points }) {
  const W = 900
  const H = 240
  const PAD_L = 8
  const PAD_B = 26
  const PAD_T = 20
  const max = Math.max(...points.map((p) => p.revenue), 1)
  const innerH = H - PAD_B - PAD_T
  const n = points.length
  const gap = 2
  const barW = Math.max((W - PAD_L * 2) / n - gap, 3)
  const maxIdx = points.findIndex((p) => p.revenue === max)

  // подписи значений — выборочно: максимум и последний месяц
  const labeled = new Set([maxIdx, n - 1])

  const gridYs = [0.25, 0.5, 0.75].map((f) => PAD_T + innerH * (1 - f))

  return (
    <div className="chart-scroll">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="column-chart"
        role="img"
        aria-label="Выручка по месяцам"
      >
        {gridYs.map((y, i) => (
          <line key={i} x1={PAD_L} x2={W - PAD_L} y1={y} y2={y} className="grid-line" />
        ))}
        <line
          x1={PAD_L} x2={W - PAD_L}
          y1={PAD_T + innerH} y2={PAD_T + innerH}
          className="axis-line"
        />
        {points.map((p, i) => {
          const h = Math.max((p.revenue / max) * innerH, 1.5)
          const x = PAD_L + i * ((W - PAD_L * 2) / n) + gap / 2
          const y = PAD_T + innerH - h
          const showTick = n <= 12 || i % Math.ceil(n / 12) === 0
          return (
            <g key={p.month} className="bar-group">
              <rect x={x} y={y} width={barW} height={h} rx="3" className="bar">
                <title>{`${monthLabel(p.month)}: ${formatMoney(p.revenue)} · накладных: ${p.docs}`}</title>
              </rect>
              {labeled.has(i) && (
                <text x={x + barW / 2} y={y - 6} className="bar-label" textAnchor="middle">
                  {shortMoney(p.revenue)}
                </text>
              )}
              {showTick && (
                <text
                  x={x + barW / 2}
                  y={PAD_T + innerH + 16}
                  className="tick-label"
                  textAnchor="middle"
                >
                  {monthLabel(p.month)}
                </text>
              )}
            </g>
          )
        })}
      </svg>
    </div>
  )
}

function TopTable({ title, rows, cols }) {
  const max = Math.max(...rows.map((r) => r.revenue), 1)
  return (
    <div className="chart-card">
      <h2 className="chart-title">{title}</h2>
      <div className="top-list">
        {rows.length === 0 && <div className="muted">Нет данных</div>}
        {rows.map((r) => (
          <div key={r.name} className="top-row" title={r.name}>
            <div className="top-name">{r.name}</div>
            <div className="top-bar-track">
              <div
                className="top-bar-fill"
                style={{ width: `${Math.max((r.revenue / max) * 100, 1)}%` }}
              />
            </div>
            <div className="top-vals">
              <span className="top-money">{shortMoney(r.revenue)}</span>
              <span className="top-extra">
                {cols[1][0] === 'qty'
                  ? `${Number(r.qty).toLocaleString('ru-RU', { maximumFractionDigits: 0 })} шт`
                  : `${r.docs} нак.`}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

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
  const [replacePeriod, setReplacePeriod] = useState(false)
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
      const res = await api.importSales(file, replacePeriod)
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
          <div className="import-controls">
            <label
              className="filter-inline replace-toggle"
              title="Удалить из базы период, который покрывает файл, и загрузить его заново — правки 1С задним числом попадут корректно"
            >
              <input
                type="checkbox"
                checked={replacePeriod}
                onChange={(e) => setReplacePeriod(e.target.checked)}
              />
              заменить период
            </label>
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
          </div>
        )}
      </div>

      {importResult && (
        <div className="import-result">
          Импорт завершён: добавлено <b>{importResult.added}</b>
          {importResult.replaced_rows > 0 && (
            <>, заменено строк периода: <b>{importResult.replaced_rows}</b></>
          )}
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

          <ProductSearch dateFrom={dateFrom} dateTo={dateTo} />

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

function fmtDate(iso) {
  if (!iso) return ''
  const [y, m, d] = iso.split('-')
  return `${d}.${m}.${y}`
}

function fmtQty(v) {
  return Number(v || 0).toLocaleString('ru-RU', { maximumFractionDigits: 2 })
}

// Поиск по номенклатуре: «сколько штук и на какую сумму продали товара X».
// Даёт быстрый ответ на вопросы про конкретный товар без обращения ко мне.
function ProductSearch({ dateFrom, dateTo }) {
  const [q, setQ] = useState('')
  const [res, setRes] = useState(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    // пустой запрос без дат не грузим — иначе тянем всю номенклатуру зря
    if (!q.trim() && !dateFrom && !dateTo) {
      setRes(null)
      return
    }
    const t = setTimeout(async () => {
      setLoading(true)
      setErr(null)
      try {
        setRes(await api.salesProducts({ q: q.trim(), date_from: dateFrom, date_to: dateTo }))
      } catch (e) {
        setErr(e.message)
      } finally {
        setLoading(false)
      }
    }, 350)
    return () => clearTimeout(t)
  }, [q, dateFrom, dateTo])

  return (
    <div className="chart-card">
      <h2 className="chart-title">Поиск по номенклатуре</h2>
      <div className="product-search-bar">
        <input
          className="product-search-input"
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Название товара, напр. «туалетная бумага», «салфетки»…"
        />
        {q && (
          <button className="btn btn-ghost" onClick={() => setQ('')}>
            Очистить
          </button>
        )}
      </div>

      {err && <div className="error">{err}</div>}

      {!res && !loading && (
        <div className="muted product-search-hint">
          Введите часть названия товара — покажу, сколько штук и на какую сумму
          продано{dateFrom || dateTo ? ' за выбранный период' : ''}.
        </div>
      )}

      {loading && <div className="muted product-search-hint">Ищу…</div>}

      {res && !loading && (
        <>
          <div className="product-search-total">
            {res.count === 0 ? (
              <span className="muted">Ничего не найдено по запросу «{res.query}».</span>
            ) : (
              <>
                Найдено позиций: <b>{res.count}</b> · продано{' '}
                <b>{fmtQty(res.total_qty)} шт</b> на{' '}
                <b>{formatMoney(res.total_amount)}</b>
                {res.total_docs > 0 && <> · накладных: <b>{res.total_docs}</b></>}
              </>
            )}
          </div>

          {res.count > 0 && (
            <div className="table-wrap product-search-table">
              <table>
                <thead>
                  <tr>
                    <th>Товар</th>
                    <th className="num-col">Кол-во</th>
                    <th className="num-col">Сумма</th>
                    <th className="num-col">Накл.</th>
                    <th className="num-col">Период продаж</th>
                  </tr>
                </thead>
                <tbody>
                  {res.products.map((p) => (
                    <tr key={p.product}>
                      <td>{p.product}</td>
                      <td className="num num-col">
                        {fmtQty(p.qty)} {p.unit}
                      </td>
                      <td className="num num-col">{formatMoney(p.amount)}</td>
                      <td className="num num-col">{p.docs}</td>
                      <td className="num num-col product-search-period">
                        {p.first_date === p.last_date
                          ? fmtDate(p.first_date)
                          : `${fmtDate(p.first_date)}—${fmtDate(p.last_date)}`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
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

import { useEffect, useState } from 'react'
import { api } from '../api'
import { formatMoney } from '../utils'

function monthRange() {
  const now = new Date()
  const y = now.getFullYear()
  const m = String(now.getMonth() + 1).padStart(2, '0')
  const last = new Date(y, now.getMonth() + 1, 0).getDate()
  return { from: `${y}-${m}-01`, to: `${y}-${m}-${String(last).padStart(2, '0')}` }
}

const money = (v) => formatMoney(v)
const cls = (diff) => (Math.abs(Number(diff || 0)) >= 0.5 ? 'sd-diff-bad' : 'sd-diff-ok')

export default function SalesDocPage() {
  const [configured, setConfigured] = useState(null)
  const [range, setRange] = useState(monthRange())
  const [period, setPeriod] = useState(null)
  const [debt, setDebt] = useState(null)
  const [onlyDiff, setOnlyDiff] = useState(true)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.salesdocStatus()
      .then((s) => {
        setConfigured(s.configured)
        if (s.configured) loadAll(range, onlyDiff)
      })
      .catch((e) => setError(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function loadAll(r, od) {
    setLoading(true)
    setError(null)
    try {
      const [p, d] = await Promise.all([
        api.salesdocPeriod(r.from, r.to),
        api.salesdocDebt(od),
      ])
      setPeriod(p)
      setDebt(d)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  if (configured === null && !error) {
    return <div className="center muted">Загрузка…</div>
  }

  if (configured === false) {
    return (
      <div>
        <div className="page-header"><h1>Сверка с SalesDoc</h1></div>
        <div className="chart-card">
          <p>Интеграция SalesDoc ещё не подключена. Чтобы включить, задайте на
            сервере (Render → Environment) переменные:</p>
          <ul className="sd-env">
            <li><code>SALESDOC_URL</code> — например <code>https://innowave.salesdoc.io</code></li>
            <li><code>SALESDOC_LOGIN</code></li>
            <li><code>SALESDOC_PASSWORD</code></li>
            <li><code>SALESDOC_FILIAL</code> — необязательно (ID филиала)</li>
          </ul>
          <p className="muted">После сохранения переменных сервис перезапустится и
            раздел заработает.</p>
        </div>
      </div>
    )
  }

  return (
    <div className="salesdoc">
      <div className="page-header">
        <h1>Сверка с SalesDoc</h1>
        <button className="btn btn-primary" disabled={loading} onClick={() => loadAll(range, onlyDiff)}>
          {loading ? 'Обновление…' : '↻ Обновить'}
        </button>
      </div>

      <p className="note-readonly">
        Данные из SalesDoc сравниваются с нашей 1С-картиной. Клиенты
        сопоставляются по названию, поэтому небольшие расхождения могут быть
        из-за разного написания имён.
      </p>

      {error && <div className="error">{error}</div>}

      {/* --- Итоги за период --- */}
      <div className="filters">
        <label className="ops-date">
          <span>с</span>
          <input type="date" className="filter-select" value={range.from}
            onChange={(e) => setRange((r) => ({ ...r, from: e.target.value }))} />
        </label>
        <label className="ops-date">
          <span>по</span>
          <input type="date" className="filter-select" value={range.to}
            onChange={(e) => setRange((r) => ({ ...r, to: e.target.value }))} />
        </label>
        <button className="btn btn-sm" disabled={loading} onClick={() => loadAll(range, onlyDiff)}>
          Применить период
        </button>
      </div>

      {period && (
        <div className="sd-compare">
          <CompareCard title="Реализации за период" our={period.sales.our}
            sd={period.sales.sd} diff={period.sales.diff} note={`${period.sales.sd_count} заказов в SD`} />
          <CompareCard title="Оплаты за период" our={period.payments.our}
            sd={period.payments.sd} diff={period.payments.diff} note={`${period.payments.sd_count} оплат в SD`} />
        </div>
      )}

      {/* --- Дебиторка по клиентам --- */}
      {debt && (
        <>
          <div className="summary-bar sd-debt-summary">
            <div className="summary-card">
              <span className="summary-label">Долг · 1С</span>
              <span className="summary-value">{money(debt.our_total)}</span>
            </div>
            <div className="summary-card">
              <span className="summary-label">Долг · SalesDoc</span>
              <span className="summary-value">{money(debt.sd_total)}</span>
            </div>
            <div className={`summary-card ${cls(debt.diff)}`}>
              <span className="summary-label">Расхождение</span>
              <span className="summary-value">{money(debt.diff)}</span>
            </div>
            <div className="summary-card">
              <span className="summary-label">Совпало · только 1С · только SD</span>
              <span className="summary-value sd-counts">
                {debt.matched} · {debt.only_1c} · {debt.only_sd}
              </span>
            </div>
          </div>

          <div className="sd-toolbar">
            <label className="sd-check">
              <input type="checkbox" checked={onlyDiff}
                onChange={(e) => { setOnlyDiff(e.target.checked); loadAll(range, e.target.checked) }} />
              {' '}Только расхождения
            </label>
            <span className="muted">{debt.rows.length} строк</span>
          </div>

          <div className="table-wrap cards">
            <table>
              <thead>
                <tr>
                  <th>Клиент</th>
                  <th className="num">Долг 1С</th>
                  <th className="num">Долг SD</th>
                  <th className="num">Разница</th>
                  <th>Где есть</th>
                </tr>
              </thead>
              <tbody>
                {debt.rows.length === 0 && (
                  <tr><td colSpan={5} className="muted center">Расхождений нет 🎉</td></tr>
                )}
                {debt.rows.map((r, i) => (
                  <tr key={i}>
                    <td data-label="Клиент">{r.name}</td>
                    <td className="num" data-label="Долг 1С">{money(r.our_debt)}</td>
                    <td className="num" data-label="Долг SD">{money(r.sd_debt)}</td>
                    <td className={`num ${cls(r.diff)}`} data-label="Разница">{money(r.diff)}</td>
                    <td data-label="Где есть">
                      {r.in_1c && r.in_sd ? (
                        <span className="badge badge-paid">обе</span>
                      ) : r.in_1c ? (
                        <span className="badge badge-overdue">только 1С</span>
                      ) : (
                        <span className="badge badge-overdue">только SD</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}

function CompareCard({ title, our, sd, diff, note }) {
  return (
    <div className="chart-card sd-card">
      <div className="sd-card-title">{title}</div>
      <div className="sd-card-rows">
        <div><span className="muted">1С</span><b>{money(our)}</b></div>
        <div><span className="muted">SalesDoc</span><b>{money(sd)}</b></div>
        <div className={cls(diff)}><span>Разница</span><b>{money(diff)}</b></div>
      </div>
      {note && <div className="muted sd-card-note">{note}</div>}
    </div>
  )
}

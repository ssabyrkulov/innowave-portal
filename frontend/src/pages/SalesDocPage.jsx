import { useEffect, useState } from 'react'
import { api } from '../api'
import { formatMoney, toISODate } from '../utils'

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
  const [detail, setDetail] = useState(null)

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
      // Последовательно, а не Promise.all: параллельные запросы к SalesDoc
      // провоцируют повторный логин, который гасит токен предыдущего.
      const p = await api.salesdocPeriod(r.from, r.to)
      setPeriod(p)
      const d = await api.salesdocDebt(od)
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
        Клиенты связываются по ИД SalesDoc (из имени контрагента), затем по коду
        1С и имени. «обе» — точка есть в обеих системах; долг SD = 0 означает,
        что в SalesDoc она оплачена (частая причина разницы — у нас не загружены
        наличные оплаты). «только 1С» / «только SD» — точку не удалось связать
        (разные имена без ИД).
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
                    <td data-label="Клиент">
                      <button className="client-link" onClick={() => setDetail(r)}>
                        {r.name}
                      </button>
                    </td>
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

      {detail && (
        <ReconcileDetailModal row={detail} onClose={() => setDetail(null)} />
      )}
    </div>
  )
}

function inRange(iso, r) {
  return iso && iso >= r.from && iso <= r.to
}

function ReconcileDetailModal({ row, onClose }) {
  // По умолчанию — вся история (долг накопительный): с начала данных до сегодня.
  const today = toISODate(new Date())
  const [dr, setDr] = useState({ from: `${new Date().getFullYear() - 3}-01-01`, to: today })
  const [oneC, setOneC] = useState(null)
  const [sd, setSd] = useState(null)
  const [err, setErr] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    setLoading(true)
    setErr(null)
    setSd(null)
    const jobs = [
      row.in_1c && !oneC
        ? api.clientDetail(row.name).then((d) => alive && setOneC(d)).catch(() => {})
        : Promise.resolve(),
      (row.sd_id || row.code_1C)
        ? api.salesdocClientDetail({
            sd_id: row.sd_id, code_1c: row.code_1C,
            date_from: dr.from, date_to: dr.to,
          }).then((d) => alive && setSd(d)).catch((e) => alive && setErr(e.message))
        : Promise.resolve(),
    ]
    Promise.all(jobs).finally(() => alive && setLoading(false))
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row, dr])

  // 1С за тот же период, что и SD — для сопоставимости.
  const cShip = (oneC?.shipments || []).filter((s) => inRange(s.date, dr))
  const cPay = (oneC?.payments || []).filter((p) => inRange(p.date, dr))
  const cRet = (oneC?.returns || []).filter((r) => inRange(r.date, dr))
  const sum = (arr, k) => arr.reduce((s, x) => s + Number(x[k] || 0), 0)

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="cd-head">
          <h2>{row.name}</h2>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>✕</button>
        </div>
        <div className="rc-period">
          <span className="muted">Период:</span>
          <input type="date" className="filter-select" value={dr.from}
            onChange={(e) => setDr((d) => ({ ...d, from: e.target.value }))} />
          <span className="muted">—</span>
          <input type="date" className="filter-select" value={dr.to}
            onChange={(e) => setDr((d) => ({ ...d, to: e.target.value }))} />
          <span className="muted rc-period-hint">по умолчанию — вся история</span>
        </div>
        {loading && <div className="center muted">Загрузка…</div>}
        {err && <div className="error">SalesDoc: {err}</div>}
        {sd?.errors?.length > 0 && <div className="error">SalesDoc: {sd.errors.join('; ')}</div>}

        <div className="rc-cols">
          {/* ---- 1С ---- */}
          <div className="rc-col">
            <div className="rc-col-title">1С {row.in_1c ? '' : '· нет'}</div>
            <RcSection title="Реализации" total={sum(cShip, 'amount')} count={cShip.length}
              rows={cShip.map((s) => [s.date, s.doc_number || '—', money(s.amount)])}
              head={['Дата', 'Документ', 'Сумма']} />
            <RcSection title="Оплаты" total={sum(cPay, 'amount_kgs')} count={cPay.length}
              rows={cPay.map((p) => [p.date, p.kind === 'cash' ? 'касса' : 'банк', money(p.amount_kgs)])}
              head={['Дата', 'Тип', 'Сумма']} />
            <RcSection title="Возвраты" total={sum(cRet, 'amount')} count={cRet.length}
              rows={cRet.map((r) => [r.date, money(r.amount)])} head={['Дата', 'Сумма']} />
          </div>

          {/* ---- SalesDoc ---- */}
          <div className="rc-col">
            <div className="rc-col-title">SalesDoc {row.in_sd ? '' : '· нет'}</div>
            <RcSection title="Реализации" total={sd?.orders?.total} count={sd?.orders?.count}
              rows={(sd?.orders?.items || []).map((o) => [o.date, o.status_label, money(o.amount)])}
              head={['Дата', 'Статус', 'Сумма']} />
            <RcSection title="Оплаты" total={sd?.payments?.total} count={sd?.payments?.count}
              rows={(sd?.payments?.items || []).map((p) => [p.date, money(p.amount)])}
              head={['Дата', 'Сумма']} />
            <RcSection title="Возвраты" total={sd?.returns?.total} count={sd?.returns?.count}
              rows={(sd?.returns?.items || []).map((r) => [r.date, money(r.amount)])}
              head={['Дата', 'Сумма']} />
          </div>
        </div>
      </div>
    </div>
  )
}

function RcSection({ title, total, count, rows, head }) {
  const fdate = (v) => (v && /^\d{4}-\d{2}-\d{2}$/.test(v) ? v.split('-').reverse().join('.') : v)
  return (
    <div className="rc-section">
      <div className="rc-section-head">
        <span>{title}</span>
        <span className="muted">{total != null ? money(total) : '—'}{count ? ` · ${count}` : ''}</span>
      </div>
      {(!rows || rows.length === 0) ? (
        <div className="muted rc-empty">Нет записей</div>
      ) : (
        <div className="table-wrap rc-table">
          <table>
            <thead>
              <tr>{head.map((h, i) => <th key={i} className={i > 0 && i === head.length - 1 ? 'num' : ''}>{h}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  {r.map((c, j) => (
                    <td key={j} className={j === r.length - 1 ? 'num' : ''}>{j === 0 ? fdate(c) : c}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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

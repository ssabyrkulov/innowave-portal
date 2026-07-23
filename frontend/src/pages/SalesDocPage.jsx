import { useEffect, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'
import { formatMoney, toISODate } from '../utils'

const ORG_LABELS = { hygiene: 'Innowave Hygiene', innowave: 'Innowave' }

// Подсказка фирмы по названию склада (Хайджин→hygiene, Инновейв→innowave).
function guessOrg(name) {
  const n = (name || '').toLowerCase()
  if (n.includes('хайджин') || n.includes('hygiene') || n.includes('хайдж')) return 'hygiene'
  if (n.includes('инновейв') || n.includes('innowave')) return 'innowave'
  return ''
}

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
  const { can } = useAuth()
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
            <li><code>SALESDOC_LOGIN</code> и <code>SALESDOC_PASSWORD</code></li>
            <li><code>SALESDOC_FILIAL</code> — необязательно (ID филиала)</li>
          </ul>
          <p className="muted">Чтобы портал не конфликтовал с интеграцией 1С за
            токен (у SalesDoc один токен на аккаунт), лучше вместо логина/пароля
            задать <b>тот же токен, что в 1С</b>: <code>SALESDOC_TOKEN</code> и
            <code>SALESDOC_USER_ID</code> (из карточки учётной записи SalesDoc в
            1С). Тогда портал не делает вход и токен 1С не гаснет.</p>
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
        <div className="import-controls">
          {can.editPayments && <ReconnectButton onDone={() => loadAll(range, onlyDiff)} />}
          <button className="btn btn-primary" disabled={loading} onClick={() => loadAll(range, onlyDiff)}>
            {loading ? 'Обновление…' : '↻ Обновить'}
          </button>
        </div>
      </div>

      <p className="note-readonly">
        Клиенты связываются по ИД SalesDoc (из имени контрагента), затем по коду
        1С и имени. «обе» — точка есть в обеих системах; долг SD = 0 означает,
        что в SalesDoc она оплачена (частая причина разницы — у нас не загружены
        наличные оплаты). «только 1С» / «только SD» — точку не удалось связать
        (разные имена без ИД).
      </p>

      <StoreMapping />

      <AnalyzePanel />

      <WarehouseReportPanel />

      <MatchingPanel onLinked={() => loadAll(range, onlyDiff)} />

      {debt?.sd_account_wide && (
        <p className="note-readonly sd-warn">
          Выбрана одна фирма. Реализации SalesDoc делятся по складу, а <b>баланс
          и оплаты в SalesDoc — общие по клиенту на обе фирмы</b> (так устроен их
          API). Поэтому «Долг SalesDoc» здесь может быть выше долга выбранной фирмы.
        </p>
      )}

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
                  <th>Фирма</th>
                  <th className="num">Долг 1С</th>
                  <th className="num">Долг SD</th>
                  <th className="num">Разница</th>
                  <th>Где есть</th>
                </tr>
              </thead>
              <tbody>
                {debt.rows.length === 0 && (
                  <tr><td colSpan={6} className="muted center">Расхождений нет 🎉</td></tr>
                )}
                {debt.rows.map((r, i) => (
                  <tr key={i}>
                    <td data-label="Клиент">
                      <button className="client-link" onClick={() => setDetail(r)}>
                        {r.name}
                      </button>
                    </td>
                    <td data-label="Фирма" className="muted">
                      {ORG_LABELS[r.organization] || '—'}
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
              rows={(sd?.orders?.items || []).map((o) => ({
                cells: [o.date, o.status_label, money(o.amount)],
                muted: !o.counted,
              }))}
              head={['Дата', 'Статус', 'Сумма']} />
            <RcSection title="Оплаты" total={sd?.payments?.total} count={sd?.payments?.count}
              rows={(sd?.payments?.items || []).map((p) => ({
                cells: [p.date, p.type_name ? `${p.txn_label} · ${p.type_name}` : p.txn_label, money(p.amount)],
                muted: !p.counted,
              }))}
              head={['Дата', 'Вид', 'Сумма']} />
            {sd?.payments && sd.payments.matched === 0 && (
              <div className="muted sd-pay-diag">
                {sd.payments.scanned > 0
                  ? `Проверено ${sd.payments.scanned} оплат SalesDoc за период — ни одна не привязана к этому клиенту (оплата записана на другого клиента/кассу).`
                  : 'SalesDoc не вернул ни одной оплаты за период.'}
              </div>
            )}
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
              {rows.map((r, i) => {
                const cells = Array.isArray(r) ? r : r.cells
                const muted = !Array.isArray(r) && r.muted
                return (
                  <tr key={i} className={muted ? 'rc-row-muted' : ''}>
                    {cells.map((c, j) => (
                      <td key={j} className={j === cells.length - 1 ? 'num' : ''}>{j === 0 ? fdate(c) : c}</td>
                    ))}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function ReconnectButton({ onDone }) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)
  async function go() {
    setBusy(true)
    setMsg(null)
    try {
      const r = await api.salesdocReconnect()
      setMsg(r.mode === 'static' ? 'Токен проверен ✓' : 'Переподключено ✓')
      onDone?.()
    } catch (e) {
      setMsg(e.message)
    } finally {
      setBusy(false)
      setTimeout(() => setMsg(null), 6000)
    }
  }
  return (
    <>
      <button className="btn" disabled={busy} onClick={go} title="Заново взять токен SalesDoc">
        {busy ? '…' : '🔌 Переподключить'}
      </button>
      {msg && <span className="muted sd-reconnect-msg">{msg}</span>}
    </>
  )
}

function WarehouseReportPanel() {
  const [open, setOpen] = useState(false)
  const [dr, setDr] = useState({
    from: toISODate(new Date(Date.now() - 90 * 86400000)),
    to: toISODate(new Date()),
  })
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function load(r) {
    setLoading(true)
    setError(null)
    api.salesdocWarehouseReport(r.from, r.to)
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }

  function toggle() {
    const n = !open
    setOpen(n)
    if (n && data === null) load(dr)
  }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 📦 Отчёт по складам SalesDoc
      </button>
      {open && (
        <div className="store-map-body">
          <div className="filters">
            <label className="ops-date"><span>с</span>
              <input type="date" className="filter-select" value={dr.from}
                onChange={(e) => setDr((d) => ({ ...d, from: e.target.value }))} />
            </label>
            <label className="ops-date"><span>по</span>
              <input type="date" className="filter-select" value={dr.to}
                onChange={(e) => setDr((d) => ({ ...d, to: e.target.value }))} />
            </label>
            <button className="btn btn-sm" disabled={loading} onClick={() => load(dr)}>Обновить</button>
          </div>
          <p className="muted">Приход/закладка и списания через API SalesDoc не
            читаются (это методы записи) — показываю реализации, возвраты и
            текущий остаток (количество).</p>
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Считаю…</div>}
          {data && (
            <div className="table-wrap cd-table">
              <table>
                <thead>
                  <tr>
                    <th>Склад</th>
                    <th>Фирма</th>
                    <th className="num">Реализации</th>
                    <th className="num">Возвраты</th>
                    <th className="num">Заказов</th>
                    <th className="num">Остаток, шт</th>
                  </tr>
                </thead>
                <tbody>
                  {data.warehouses.map((w) => (
                    <tr key={w.sd_id}>
                      <td>{w.name}</td>
                      <td className="muted">{ORG_LABELS[w.org] || '—'}</td>
                      <td className="num">{money(w.sales)}</td>
                      <td className="num">{w.returns ? money(w.returns) : '—'}</td>
                      <td className="num">{w.orders}</td>
                      <td className="num">{w.stock_qty ? w.stock_qty.toLocaleString('ru-RU') : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function AnalyzePanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  const today = toISODate(new Date())
  const from = toISODate(new Date(Date.now() - 90 * 86400000))

  function load() {
    setLoading(true)
    setError(null)
    api.salesdocAnalyze(from, today)
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }

  function toggle() {
    const n = !open
    setOpen(n)
    if (n && data === null) load()
  }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 🔬 Диагностика структуры SalesDoc
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">За последние 90 дней. Показывает, как реально
            устроены склады/филиалы и можно ли честно делить по фирме.</p>
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Считаю по вашей базе…</div>}
          {data && (
            <div className="an-grid">
              <AnRow label="Заказов за период" value={data.orders_total} />
              <AnRow label="Из них со складом"
                value={`${data.orders_with_store} (${pct(data.orders_with_store, data.orders_total)}%)`} />
              <AnRow label="Без склада"
                value={data.orders_without_store}
                warn={data.orders_without_store > 0} />
              <AnRow label="Филиалы (по CS_id)"
                value={data.filials.map((f) => `${f.prefix}: ${f.orders}`).join(' · ')}
                warn={data.filials.length > 1} />
              <AnRow label="Точек заказывало" value={data.clients_ordered} />
              <AnRow label="Точек с >1 складом"
                value={data.clients_multi_store}
                warn={data.clients_multi_store > 0} />
              <AnRow label="Точек, пересекающих ФИРМЫ"
                value={data.clients_cross_firm}
                warn={data.clients_cross_firm > 0}
                hint="если 0 — деление по складу честное; если >0 — у этих точек баланс общий на обе фирмы" />
              {data.unmapped_stores.length > 0 && (
                <AnRow label="Склады без привязки"
                  value={data.unmapped_stores.length} warn />
              )}
              <div className="an-sub">Склады (заказы · сумма · фирма)</div>
              {data.stores.map((s) => (
                <div key={s.sd_id} className="an-store">
                  <span>{s.name} <span className="muted">· {s.sd_id}</span></span>
                  <span className="num">{s.orders} · {money(s.sum)} · {ORG_LABELS[s.org] || '—'}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function pct(a, b) {
  return b ? Math.round((a / b) * 100) : 0
}

function AnRow({ label, value, warn, hint }) {
  return (
    <div className="an-row">
      <span className="an-label">{label}{hint && <span className="muted an-hint"> — {hint}</span>}</span>
      <span className={`an-val ${warn ? 'an-warn' : ''}`}>{value}</span>
    </div>
  )
}

function MatchingPanel({ onLinked }) {
  const { can } = useAuth()
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [pick, setPick] = useState({}) // client_1c -> sd_id
  const [busy, setBusy] = useState(null)

  function load() {
    setError(null)
    setData(null)
    api.salesdocMatching()
      .then(setData)
      .catch((e) => setError(e.message))
  }

  function toggle() {
    const n = !open
    setOpen(n)
    if (n && data === null) load()
  }

  async function link(client_1c) {
    const sd_id = pick[client_1c]
    if (!sd_id) return
    setBusy(client_1c)
    try {
      await api.salesdocLink(client_1c, sd_id)
      load()
      onLinked?.()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 🔗 Сопоставление точек 1С ↔ SalesDoc
        {data && (data.only_1c_count || data.only_sd_count) ? (
          <span className="match-badge">{data.only_1c_count + data.only_sd_count} не совпало</span>
        ) : null}
      </button>
      {open && (
        <div className="store-map-body">
          {error && <div className="error">{error}</div>}
          {data === null && !error && <div className="muted">Загрузка…</div>}
          {data && (
            <>
              <div className="match-summary">
                ✅ совпало: <b>{data.matched}</b> · только в 1С:{' '}
                <b>{data.only_1c_count}</b> · только в SalesDoc:{' '}
                <b>{data.only_sd_count}</b>
              </div>
              <div className="match-cols">
                <div>
                  <div className="rc-col-title">Только в 1С ({data.only_1c_count})</div>
                  {data.only_1c.length === 0 && <div className="muted rc-empty">Нет</div>}
                  {data.only_1c.map((c) => (
                    <div key={c.name} className="match-row">
                      <div className="match-name">
                        {c.name}
                        <span className="muted"> · долг {money(c.our_debt)}</span>
                      </div>
                      {can.editPayments && (
                        <div className="match-link">
                          <select
                            className="filter-select"
                            value={pick[c.name] || ''}
                            onChange={(e) => setPick((p) => ({ ...p, [c.name]: e.target.value }))}
                          >
                            <option value="">— выбрать точку SalesDoc —</option>
                            {c.suggestions.length > 0 && (
                              <optgroup label="Похожие">
                                {c.suggestions.map((s) => (
                                  <option key={s.sd_id} value={s.sd_id}>
                                    {s.name} ({Math.round(s.score * 100)}%)
                                  </option>
                                ))}
                              </optgroup>
                            )}
                            <optgroup label="Все несопоставленные SalesDoc">
                              {data.only_sd.map((s) => (
                                <option key={s.sd_id} value={s.sd_id}>{s.name}</option>
                              ))}
                            </optgroup>
                          </select>
                          <button
                            className="btn btn-sm btn-primary"
                            disabled={!pick[c.name] || busy === c.name}
                            onClick={() => link(c.name)}
                          >
                            {busy === c.name ? '…' : 'Связать'}
                          </button>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
                <div>
                  <div className="rc-col-title">Только в SalesDoc ({data.only_sd_count})</div>
                  {data.only_sd.length === 0 && <div className="muted rc-empty">Нет</div>}
                  {data.only_sd.map((s) => (
                    <div key={s.sd_id} className="match-row">
                      <div className="match-name">
                        {s.name}
                        <span className="muted"> · долг {money(s.sd_debt)}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function StoreMapping() {
  const { can } = useAuth()
  const [open, setOpen] = useState(false)
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)
  const [savedMsg, setSavedMsg] = useState(null)

  function load() {
    setError(null)
    api.salesdocWarehouses()
      // Непривязанным складам подставляем подсказку по названию — юзер
      // проверяет и сохраняет.
      .then((d) => setRows(d.warehouses.map((r) => ({ ...r, org: r.org || guessOrg(r.name) }))))
      .catch((e) => setError(e.message))
  }

  function toggle() {
    const n = !open
    setOpen(n)
    if (n && rows === null) load()
  }

  function setOrg(i, org) {
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, org: org || null } : r)))
    setSavedMsg(null)
  }

  async function save() {
    setSaving(true)
    setError(null)
    try {
      await api.salesdocSaveWarehouses(
        rows.map((r) => ({ store_id: r.store_id, name: r.name, org: r.org }))
      )
      setSavedMsg('Сохранено. Обновите страницу, чтобы применить.')
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 🏬 Склады SalesDoc → фирмы
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">
            В SalesDoc одна база на обе фирмы. Укажите, какой склад к какой
            фирме относится — по этому реализации будут делиться. Фирма
            предзаполнена по названию склада (Хайджин→Hygiene, Инновейв→
            Innowave) — проверьте и сохраните.
          </p>
          {error && <div className="error">{error}</div>}
          {rows === null && !error && <div className="muted">Загрузка…</div>}
          {rows && rows.length === 0 && <div className="muted">Складов не найдено.</div>}
          {rows && rows.map((r, i) => (
            <div key={r.store_id} className="store-row">
              <span className="store-name">
                {r.name || r.store_id}
                <span className="muted"> · {r.store_id}</span>
              </span>
              <select
                className="filter-select"
                value={r.org || ''}
                disabled={!can.editPayments}
                onChange={(e) => setOrg(i, e.target.value)}
              >
                <option value="">— не задано —</option>
                <option value="hygiene">{ORG_LABELS.hygiene}</option>
                <option value="innowave">{ORG_LABELS.innowave}</option>
              </select>
            </div>
          ))}
          {rows && rows.length > 0 && can.editPayments && (
            <div className="store-map-actions">
              <button className="btn btn-primary btn-sm" disabled={saving} onClick={save}>
                {saving ? 'Сохранение…' : 'Сохранить привязку'}
              </button>
              {savedMsg && <span className="muted">{savedMsg}</span>}
            </div>
          )}
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

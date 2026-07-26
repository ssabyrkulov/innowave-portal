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

// epoch (сек) → ЧЧ:ММ — когда кэш SalesDoc последний раз обновлялся.
function fmtClock(epoch) {
  return new Date(epoch * 1000).toLocaleTimeString('ru-RU', {
    hour: '2-digit', minute: '2-digit',
  })
}

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

  // Обновление зеркала идёт на сервере в фоне: список отдаётся сразу, а
  // свежие данные подтягиваем повторным чтением через несколько секунд.
  function reloadSoon(r, od) {
    setTimeout(() => loadAll(r, od).catch(() => {}), 4000)
  }

  async function loadAll(r, od, refresh = false) {
    setLoading(true)
    setError(null)
    try {
      // Последовательно, а не Promise.all: параллельные запросы к SalesDoc
      // провоцируют повторный логин, который гасит токен предыдущего.
      // Долг — первым: при refresh он сбрасывает кэш, дальше период тянется
      // уже свежим. Обычно оба читаются из кэша — мгновенно.
      const d = await api.salesdocDebt(od, refresh)
      setDebt(d)
      const p = await api.salesdocPeriod(r.from, r.to)
      setPeriod(p)
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
            задать <b>тот же токен, что в 1С</b>: <code>SALESDOC_TOKEN</code>
            (обязательно) и <code>SALESDOC_USER_ID</code> (необязательно —
            достаточно токена). Тогда портал не делает вход и токен 1С не гаснет.</p>
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
          {debt?.synced_at && (
            <span className="muted sd-synced" title="Данные SalesDoc кэшируются и обновляются в фоне">
              данные на {fmtClock(debt.synced_at)}
            </span>
          )}
          <button className="btn btn-primary" disabled={loading}
            title="Перечитать из SalesDoc. Список остаётся на месте — свежие данные подтянутся через несколько секунд."
            onClick={() => { loadAll(range, onlyDiff, true); reloadSoon(range, onlyDiff) }}>
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

      <PaymentsDebugPanel />

      <ReturnsDebugPanel />

      <StockPanel />

      <SpeedProbePanel />

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
                  <th>Причина</th>
                </tr>
              </thead>
              <tbody>
                {debt.rows.length === 0 && (
                  <tr><td colSpan={7} className="muted center">Расхождений нет 🎉</td></tr>
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
                    <td data-label="Причина">
                      {r.reason ? (
                        <span className={`sd-reason sd-reason-${r.reason_level}`}>{r.reason}</span>
                      ) : (
                        <span className="muted">—</span>
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

  const [refreshing, setRefreshing] = useState(false)

  function loadSd() {
    return api.salesdocClientDetail({
      sd_id: row.sd_id, code_1c: row.code_1C,
      date_from: dr.from, date_to: dr.to,
    })
  }

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
        ? loadSd().then((d) => {
            if (!alive) return
            setSd(d)
            // Данные показаны мгновенно (из зеркала). Просим сервер догрузить
            // изменения — он делает это в фоне и отвечает сразу, — а через
            // пару секунд перечитываем и обновляем цифры на месте.
            if (d.source === 'mirror') {
              setRefreshing(true)
              api.salesdocMirrorSync(false).catch(() => {})
              setTimeout(() => {
                if (!alive) return
                loadSd().then((fresh) => alive && setSd(fresh)).catch(() => {})
                  .finally(() => alive && setRefreshing(false))
              }, 2500)
            }
          }).catch((e) => alive && setErr(e.message))
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

  // Суммы по компонентам с обеих сторон — по ним ставим «диагноз» расхождения.
  const s1 = sum(cShip, 'amount')
  const p1 = sum(cPay, 'amount_kgs')
  const r1 = sum(cRet, 'amount')
  const sSd = Number(sd?.orders?.total || 0)
  const pSd = Number(sd?.payments?.total || 0)
  const rSd = Number(sd?.returns?.total || 0)
  // Показываем вердикт, когда данные готовы (или точка есть лишь в одной системе).
  const bothLoaded = row.in_1c && row.in_sd ? sd != null : true
  const diag = !loading && !err && bothLoaded
    ? diagnoseReconcile(row, { s1, p1, r1, sSd, pSd, rSd })
    : null

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
          {refreshing && <span className="muted rc-refreshing">проверяю обновления…</span>}
          {!refreshing && sd?.synced_at && (
            <span className="muted rc-refreshing">данные на {fmtClock(Date.parse(sd.synced_at) / 1000)}</span>
          )}
        </div>
        {loading && <div className="center muted">Загрузка…</div>}
        {err && <div className="error">SalesDoc: {err}</div>}
        {sd?.errors?.length > 0 && <div className="error">SalesDoc: {sd.errors.join('; ')}</div>}

        {diag && <ReconcileVerdict diag={diag} />}

        <div className="rc-cols">
          {/* ---- 1С ---- */}
          <div className="rc-col">
            <div className="rc-col-title">1С {row.in_1c ? '' : '· нет'}</div>
            <RcSection title="Реализации" total={s1} count={cShip.length}
              rows={cShip.map((s) => [s.date, s.doc_number || '—', money(s.amount)])}
              head={['Дата', 'Документ', 'Сумма']} />
            <RcSection title="Оплаты" total={p1} count={cPay.length}
              rows={cPay.map((p) => [p.date, p.kind === 'cash' ? 'касса' : 'банк', money(p.amount_kgs)])}
              head={['Дата', 'Тип', 'Сумма']} />
            <RcSection title="Возвраты" total={r1} count={cRet.length}
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

// Ставит «диагноз» расхождения долга 1С ↔ SalesDoc: раскладывает разницу по
// компонентам (реализации / возвраты / оплаты) и называет вероятную причину
// человеческим языком — «в SalesDoc не проведены возвраты» и т. п.
function diagnoseReconcile(row, t) {
  // Точка есть лишь в одной системе — причина очевидна, компоненты не нужны.
  if (!row.in_sd) {
    return {
      level: 'bad', head: 'Точки нет в SalesDoc',
      lines: ['Клиент есть только в 1С — сверять не с чем. Свяжите точку во ' +
        'вкладке «Сопоставление точек 1С ↔ SalesDoc» или это новая/закрытая точка.'],
    }
  }
  if (!row.in_1c) {
    return {
      level: 'bad', head: 'Точки нет в 1С',
      lines: ['Клиент есть только в SalesDoc — в 1С отгрузок и оплат по нему нет.'],
    }
  }

  const delta = Number(row.sd_debt || 0) - Number(row.our_debt || 0)
  if (Math.abs(delta) < 1) {
    return { level: 'ok', head: 'Долг сходится — расхождения нет', lines: [] }
  }

  // Вклад каждого компонента в разницу долга (насколько SalesDoc показывает больше):
  //  реализации: больше реализаций в SD → выше долг в SD
  //  возвраты:   меньше возвратов в SD → выше долг в SD
  //  оплаты:     меньше оплат в SD    → выше долг в SD
  const factors = [
    { key: 'sales', c: t.sSd - t.s1 },
    { key: 'returns', c: t.r1 - t.rSd },
    { key: 'pay', c: t.p1 - t.pSd },
  ]
  const phrase = (f) => {
    const a = money(Math.abs(f.c))
    if (f.key === 'sales') {
      return f.c > 0
        ? `В SalesDoc реализаций больше на ${a} — вероятно, в 1С не проведены отгрузки.`
        : `В 1С реализаций больше на ${a} — вероятно, в SalesDoc не проведены отгрузки.`
    }
    if (f.key === 'returns') {
      return f.c > 0
        ? `В SalesDoc не проведены возвраты на ${a} (в 1С они есть).`
        : `В SalesDoc возвратов больше на ${a} (в 1С их нет или меньше).`
    }
    return f.c > 0
      ? `В SalesDoc не проведены оплаты на ${a} (в 1С они есть).`
      : `В 1С не загружены оплаты на ${a} — вероятно, наличные (в SalesDoc они есть).`
  }

  // Значимые факторы (> 500 сом), по убыванию влияния.
  const sig = factors
    .filter((f) => Math.abs(f.c) >= 500)
    .sort((a, b) => Math.abs(b.c) - Math.abs(a.c))

  if (sig.length === 0) {
    // Компоненты совпадают. Если баланс SalesDoc расходится с суммой его же
    // операций — деньги «застряли» в балансе SD (оплата в журнале, но не
    // применена к балансу; либо входящий остаток). Это, а не «курс/период».
    const sdTxnNet = t.sSd - t.rSd - t.pSd
    const gap = Number(row.sd_debt || 0) - sdTxnNet
    if (Math.abs(gap) >= 500) {
      return {
        level: 'warn',
        head: `Операции 1С и SalesDoc совпадают, но баланс SalesDoc это не отражает (на ${money(Math.abs(gap))})`,
        lines: [
          `По реализациям, оплатам и возвратам долг должен быть ${money(sdTxnNet)}, а баланс SalesDoc = ${money(Number(row.sd_debt || 0))}.`,
          gap > 0
            ? 'В SalesDoc есть сумма сверх проведённых операций — чаще всего оплата, попавшая в журнал, но не применённая к балансу, либо входящий остаток. Это правится в SalesDoc — новая загрузка из 1С на это не влияет.'
            : 'Баланс SalesDoc меньше, чем следует из операций — возможно, в балансе учтена лишняя оплата или возврат.',
        ],
      }
    }
    return {
      level: 'warn',
      head: `Долг расходится на ${money(delta)}, но по компонентам не раскладывается`,
      lines: ['Реализации, возвраты и оплаты в обеих системах близки. Возможна ' +
        'разница из-за курса валют, входящего остатка или периода — попробуйте ' +
        'расширить даты выше.'],
    }
  }

  return { level: 'warn', head: phrase(sig[0]), lines: sig.slice(1).map(phrase) }
}

function ReconcileVerdict({ diag }) {
  const icon = diag.level === 'ok' ? '✅' : diag.level === 'bad' ? '⛔' : '⚠️'
  return (
    <div className={`sd-diag sd-diag-${diag.level}`}>
      <div className="sd-diag-head">
        <span className="sd-diag-icon">{icon}</span>
        <span>{diag.head}</span>
      </div>
      {diag.lines.length > 0 && (
        <ul className="sd-diag-list">
          {diag.lines.map((l, i) => <li key={i}>{l}</li>)}
        </ul>
      )}
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

function PaymentsDebugPanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const today = toISODate(new Date())
  const from = toISODate(new Date(Date.now() - 180 * 86400000))

  function load() {
    setLoading(true); setError(null)
    api.salesdocPaymentsDebug(from, today)
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 🔍 Диагностика оплат SalesDoc
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">За 180 дней. Показывает, у скольких оплат вообще
            указан клиент — если много «без клиента», такие оплаты (обычно касса)
            привязать к точке нельзя.</p>
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Считаю…</div>}
          {data && (
            <div className="an-grid">
              <AnRow label="Всего оплат" value={data.scanned} />
              <AnRow label="С клиентом (по ИД)" value={data.with_client_sdid} />
              <AnRow label="С клиентом (по коду 1С)" value={data.with_client_code} />
              <AnRow label="БЕЗ клиента" value={data.without_client}
                warn={data.without_client > 0}
                hint="эти оплаты нельзя привязать к точке" />
              <div className="an-sub">По типу оплаты (с клиентом / без)</div>
              {data.by_type.map((t) => (
                <div key={t.type} className="an-store">
                  <span>{t.type}</span>
                  <span className="num">{t.with_client} / <span className="an-warn">{t.without_client}</span></span>
                </div>
              ))}
              <div className="an-sub">По виду операции</div>
              {Object.entries(data.txn).map(([k, v]) => (
                <div key={k} className="an-store"><span>{k}</span><span className="num">{v}</span></div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function ReturnsDebugPanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const today = toISODate(new Date())
  const from = `${new Date().getFullYear() - 3}-01-01`

  function load() {
    setLoading(true); setError(null)
    api.salesdocReturnsDebug(from, today)
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  // Насколько источник SD близок к 1С (для подсказки, какой считать «возвратом»).
  const near = data
    ? Math.abs(data.shelf.total - data.one_c.total) <= Math.abs(data.defects.total - data.one_c.total)
      ? 'shelf' : 'defects'
    : null
  const dup = data && data.overlap.amount >= 1

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 🔁 Диагностика возвратов SD (за 3 года)
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Сравниваю два источника возвратов SalesDoc с
            возвратами 1С: <b>getOrderDefect</b> (документы брака) и <b>«Возврат
            с полки»</b> (тип 9 в журнале оплат). Так видно, какой из них — это
            ваши обычные возвраты и нет ли задвоения.</p>
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Считаю по вашей базе…</div>}
          {data && (
            <div className="an-grid">
              <AnRow label="Возвраты 1С (эталон)"
                value={`${money(data.one_c.total)} · ${data.one_c.count}`} />
              <AnRow label="«Возврат с полки» (тип 9)"
                value={`${money(data.shelf.total)} · ${data.shelf.count} · клиентов ${data.shelf.clients}`}
                hint={near === 'shelf' ? 'ближе к 1С' : undefined} />
              <AnRow label="getOrderDefect (брак)"
                value={`${money(data.defects.total)} · ${data.defects.count} · клиентов ${data.defects.clients}`}
                hint={near === 'defects' ? 'ближе к 1С' : undefined} />
              <AnRow label="Клиентов в обоих источниках"
                value={data.clients_with_both} warn={data.clients_with_both > 0} />
              <AnRow label="Совпадающих возвратов (дата+сумма)"
                value={`${data.overlap.count} на ${money(data.overlap.amount)}`}
                warn={dup}
                hint={dup ? 'возможное задвоение' : 'пересечений нет'} />
              <div className={`sd-diag sd-diag-${dup ? 'warn' : 'ok'}`}>
                <div className="sd-diag-head">
                  <span className="sd-diag-icon">{dup ? '⚠️' : '✅'}</span>
                  <span>
                    {dup
                      ? `Есть совпадающие возвраты в обоих источниках на ${money(data.overlap.amount)} — вероятно задвоение. Стоит считать возвратом только «Возврат с полки» и убрать getOrderDefect.`
                      : 'Источники не пересекаются (одинаковых возвратов нет) — суммировать безопасно.'}
                  </span>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// Замер перед ускорением карточки: соблюдает ли SalesDoc фильтр по клиенту и
// работает ли инкремент. От результата зависит, как строить быструю выгрузку.
const qty = (v) => Number(v || 0).toLocaleString('ru-RU', { maximumFractionDigits: 3 })

// Остатки SalesDoc по складам и позициям — в штуках (сумм SalesDoc не отдаёт).
function StockPanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [q, setQ] = useState('')
  const [allItems, setAllItems] = useState(false)
  const [openStore, setOpenStore] = useState(null)

  function load(search = q, all = allItems) {
    setLoading(true); setError(null)
    api.salesdocStock({ q: search, all_items: all })
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  // Поиск с паузой, чтобы не дёргать сервер на каждую букву.
  useEffect(() => {
    if (!open) return
    const t = setTimeout(() => load(q, allItems), 350)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, allItems])

  const totalQty = (data?.warehouses || []).reduce((s, w) => s + w.total_qty, 0)
  const totalPos = (data?.warehouses || []).reduce((s, w) => s + w.positions, 0)

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 📦 Остатки на складах SalesDoc (шт)
      </button>
      {open && (
        <div className="store-map-body">
          <div className="filters">
            <input className="product-search-input" type="search" value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Поиск по названию товара…" />
            <label className="sd-check">
              <input type="checkbox" checked={allItems}
                onChange={(e) => setAllItems(e.target.checked)} />
              {' '}показывать нулевые
            </label>
          </div>
          <p className="muted">SalesDoc отдаёт по остаткам только количество —
            цен и сумм в них нет. Данные из зеркала, обновляются в фоне.</p>
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Загрузка…</div>}
          {data && data.warehouses.length === 0 && (
            <div className="muted">
              {data.ready ? 'Ничего не найдено.' : 'Остатки ещё загружаются — обновите через минуту.'}
            </div>
          )}
          {data && data.warehouses.length > 0 && (
            <>
              <div className="an-row">
                <span className="an-label">Всего</span>
                <span className="an-val">{qty(totalQty)} шт · {totalPos} позиций</span>
              </div>
              {data.warehouses.map((w) => (
                <div key={w.store_id} className="stock-store">
                  <button className="btn btn-ghost stock-store-head"
                    onClick={() => setOpenStore(openStore === w.store_id ? null : w.store_id)}>
                    {openStore === w.store_id ? '▾' : '▸'} {w.store}
                    <span className="muted">
                      {' '}· {ORG_LABELS[w.org] || 'фирма не задана'}
                      {' '}· {qty(w.total_qty)} шт · {w.positions} поз.
                    </span>
                  </button>
                  {openStore === w.store_id && (
                    <div className="table-wrap rc-table">
                      <table>
                        <thead>
                          <tr><th>Товар</th><th className="num">Кол-во, шт</th></tr>
                        </thead>
                        <tbody>
                          {w.items.map((it, i) => (
                            <tr key={i}>
                              <td>{it.name}</td>
                              <td className="num">{qty(it.qty)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function SpeedProbePanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function load() {
    setLoading(true); setError(null)
    api.salesdocSpeedProbe()
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  const yes = (v) => (v ? '✅ да' : '❌ нет')

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} ⚡ Замер скорости SalesDoc
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Проверяю на живых данных, можно ли тянуть данные
            клиента точечно (а не весь журнал за 3 года) и работает ли догрузка
            «только изменённого». От этого зависит, как ускорить карточку.
            Занимает до минуты — тянет журнал целиком для сравнения.</p>
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Замеряю…</div>}
          {data && (
            <div className="an-grid">
              <AnRow label="Клиент для теста" value={data.client_name} />

              <div className="an-sub">Реализации (getOrder)</div>
              <AnRow label="Фильтр по клиенту работает"
                value={yes(data.orders.server_filter_works)}
                warn={!data.orders.server_filter_works} />
              <AnRow label="С фильтром"
                value={`${data.orders.with_client_filter.returned} зап. · ${data.orders.with_client_filter.ms} мс`} />
              <AnRow label="Без фильтра (весь журнал)"
                value={`${data.orders.without_filter.returned} зап. · ${data.orders.without_filter.ms} мс`} />
              {data.orders.speedup && (
                <AnRow label="Выигрыш" value={`в ${data.orders.speedup}× быстрее`} />
              )}

              <div className="an-sub">Возвраты (getOrderDefect)</div>
              <AnRow label="Фильтр по клиенту работает"
                value={yes(data.defects.server_filter_works)}
                warn={!data.defects.server_filter_works} />
              <AnRow label="С фильтром / без"
                value={`${data.defects.with_client_filter.returned} / ${data.defects.without_filter.returned} зап.`} />

              <div className="an-sub">Оплаты (getPayment)</div>
              <AnRow label="Фильтр по типу операции работает"
                value={yes(data.payments.type_filter_works)}
                warn={!data.payments.type_filter_works} />
              <AnRow label="Только оплаты / весь журнал"
                value={`${data.payments.with_type_filter.returned} / ${data.payments.without_filter.returned} зап.`} />

              <div className="an-sub">Догрузка изменений (dateUpdate)</div>
              <AnRow label="Инкремент пригоден"
                value={yes(data.incremental.usable)}
                warn={!data.incremental.usable}
                hint="если да — зеркало можно обновлять дельтой, а не перекачкой" />
              <AnRow label={`Изменено с ${data.incremental.since}`}
                value={`${data.incremental.by_dateUpdate.returned} зап. · ${data.incremental.by_dateUpdate.ms} мс`} />
              <AnRow label="Появилось за тот же срок"
                value={`${data.incremental.by_date.returned} зап.`} />
            </div>
          )}
        </div>
      )}
    </div>
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

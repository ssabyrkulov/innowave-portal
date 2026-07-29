import { useEffect, useMemo, useState } from 'react'
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

// Идентификаторы SalesDoc бывают длинными (UUID) — показываем начало, полное
// значение остаётся в подсказке при наведении.
function shortId(id) {
  if (!id) return null
  const s = String(id)
  return 'ИД ' + (s.length > 14 ? s.slice(0, 12) + '…' : s)
}

// Дата в будущем — признак опечатки в годе при ручном вводе. Такие записи
// двигают баланс, но в интерфейсе SalesDoc их обычно не видно: там стоит
// фильтр по периоду, и запись «проваливается» за его край.
function isFuture(iso) {
  return Boolean(iso) && iso > toISODate(new Date())
}

// Подпись под датой у реализации SalesDoc. В поле «код 1С» там лежит либо
// номер накладной (обмен с 1С отработал), либо служебный GUID — длинный и
// нечитаемый, его сокращаем.
function orderNote(o) {
  const code = o.code_1C ? String(o.code_1C) : ''
  if (code && code.length <= 20) return `док. ${code}`
  if (code) return `док. ${code.slice(0, 12)}…`
  return shortId(o.sd_id)
}

// Построчное сопоставление двух списков (1С и SalesDoc) по сумме и дате.
// Раньше колонки жили независимо и сравнивались только итоги: видно было «в
// SalesDoc не проведены оплаты на 186 575», но не видно КАКИЕ. Пара считается
// по совпадению суммы (до копеек) и близкой дате — в SalesDoc документ часто
// датирован на день позже, чем в 1С.
const PAIR_TOL_DAYS = 3

function daysBetween(a, b) {
  if (!a || !b) return 1e9
  return Math.abs(Date.parse(a) - Date.parse(b)) / 86400000
}

function pairLists(left, right, amountOf) {
  const usedR = new Set()
  const pairedL = new Set()
  // Сначала точные совпадения по дате, потом «в пределах нескольких дней»:
  // иначе близкий по дате чужой документ мог перехватить пару.
  for (const exact of [true, false]) {
    left.forEach((l, li) => {
      if (pairedL.has(li)) return
      const la = amountOf(l)
      const ri = right.findIndex((r, i) => {
        if (usedR.has(i)) return false
        if (Math.abs(amountOf(r) - la) >= 0.5) return false
        const d = daysBetween(l.date, r.date)
        return exact ? d === 0 : d <= PAIR_TOL_DAYS
      })
      if (ri >= 0) { usedR.add(ri); pairedL.add(li) }
    })
  }
  return {
    leftUnpaired: (i) => !pairedL.has(i),
    rightUnpaired: (i) => !usedR.has(i),
  }
}

// 2025-11-12 → 12.11.2025
function fdateShort(iso) {
  return iso && /^\d{4}-\d{2}-\d{2}$/.test(iso) ? iso.split('-').reverse().join('.') : (iso || '—')
}

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
  const [reasonFilter, setReasonFilter] = useState('')

  // Причина у строки может быть составной («оплата · возврат»), поэтому
  // разбираем её на отдельные ярлыки: так одну точку видно во всех своих
  // категориях, а фильтр по «оплата» ловит и составные случаи.
  const reasonTokens = (r) =>
    (r.reason || '').split('·').map((s) => s.trim()).filter(Boolean)

  const reasonStats = useMemo(() => {
    const counts = new Map()
    for (const r of debt?.rows || []) {
      for (const t of reasonTokens(r)) counts.set(t, (counts.get(t) || 0) + 1)
    }
    return [...counts.entries()]
      .map(([key, count]) => ({ key, count }))
      .sort((a, b) => b.count - a.count)
  }, [debt])

  const visibleRows = useMemo(() => {
    const rows = debt?.rows || []
    if (!reasonFilter) return rows
    return rows.filter((r) => reasonTokens(r).includes(reasonFilter))
  }, [debt, reasonFilter])

  // Сколько всего расходится по выбранной причине — сразу видно масштаб.
  const filteredDiff = useMemo(
    () => visibleRows.reduce((s, r) => s + Number(r.diff || 0), 0),
    [visibleRows]
  )

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

      <ShipmentsComparePanel />

      <OrderChangesPanel />

      <AnalyzePanel />

      <PaymentsDebugPanel />

      <ReturnsDebugPanel />

      <StockPanel />

      <CashboxPanel />

      <SpeedProbePanel />

      <WarehouseReportPanel />

      <MatchingPanel onLinked={() => loadAll(range, onlyDiff)} />

      {debt?.unmapped_stores?.length > 0 && (
        <p className="note-readonly sd-warn">
          Складам не задана фирма: <b>{debt.unmapped_stores.join(', ')}</b>. Их
          реализации показываются в обеих фирмах — иначе они бы исчезали при
          переключении. Задайте фирму в панели «Склады SalesDoc → фирмы», чтобы
          деление стало точным.
        </p>
      )}

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
            {reasonStats.length > 0 && (
              <select className="filter-select" value={reasonFilter}
                onChange={(e) => setReasonFilter(e.target.value)}
                title="Показать только точки с этой причиной расхождения">
                <option value="">Все причины</option>
                {reasonStats.map((r) => (
                  <option key={r.key} value={r.key}>
                    {r.key} — {r.count}
                  </option>
                ))}
              </select>
            )}
            <span className="muted">
              {visibleRows.length} строк
              {reasonFilter && ` · на ${money(filteredDiff)}`}
            </span>
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
                {visibleRows.length === 0 && (
                  <tr>
                    <td colSpan={7} className="muted center">
                      {reasonFilter ? 'По этой причине точек нет' : 'Расхождений нет 🎉'}
                    </td>
                  </tr>
                )}
                {visibleRows.map((r, i) => (
                  <tr key={i}>
                    <td data-label="Клиент">
                      <button className="client-link" onClick={() => setDetail(r)}>
                        {r.name}
                      </button>
                      {r.org_note && (
                        <div className={`rc-org-note${r.org_note_warn ? ' rc-org-note-warn' : ''}`}>
                          {r.org_note}
                        </div>
                      )}
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
  // По умолчанию — вся история (долг накопительный). Конец периода — конец
  // следующего года, а не «сегодня»: в SalesDoc попадаются операции с датой в
  // будущем (опечатка в годе при ручном вводе), и они тоже двигают баланс —
  // если их не показать, получается «операции сходятся, а баланс нет».
  const [dr, setDr] = useState({
    from: `${new Date().getFullYear() - 3}-01-01`,
    to: `${new Date().getFullYear() + 1}-12-31`,
  })
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

  // Какая оплата раскрыта «сырым ответом» — чтобы посмотреть, есть ли в ней
  // вообще хоть какой-то признак фирмы.
  const [rawPay, setRawPay] = useState(null)

  // 1С за тот же период, что и SD — для сопоставимости.
  const cShip = (oneC?.shipments || []).filter((s) => inRange(s.date, dr))
  const cPay = (oneC?.payments || []).filter((p) => inRange(p.date, dr))
  const cRet = (oneC?.returns || []).filter((r) => inRange(r.date, dr))
  const sum = (arr, k) => arr.reduce((s, x) => s + Number(x[k] || 0), 0)

  // Построчные пары: какие именно документы остались без пары. Итог «не
  // проведены оплаты на 186 575» без этого не отвечал на вопрос «какие».
  const sdShip = (sd?.orders?.items || []).filter((o) => o.status !== 5)
  const sdPay = sd?.payments?.items || []
  const shipPairs = useMemo(
    () => pairLists(cShip, sdShip, (x) => Number(x.amount || 0)),
    [cShip, sdShip]
  )
  const payPairs = useMemo(
    () => pairLists(cPay, sdPay, (x) => Number(x.amount_kgs ?? x.amount ?? 0)),
    [cPay, sdPay]
  )

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

        <ClientDebug row={row} />

        <div className="rc-cols">
          {/* ---- 1С ---- */}
          <div className="rc-col">
            <div className="rc-col-title">1С {row.in_1c ? '' : '· нет'}</div>
            <RcSection title="Реализации" total={s1} count={cShip.length}
              rows={cShip.map((s, i) => ({
                cells: [s.date, money(s.amount)],
                // Номер документа и склад отгрузки: по складу видно, чьей
                // фирме документ, и почему он мог не сойтись с SalesDoc.
                // Если пары в SalesDoc нет — это и есть ответ «какие именно».
                note: shipPairs.leftUnpaired(i)
                  ? 'нет пары в SalesDoc'
                  : ([s.doc_number && `док. ${s.doc_number}`, s.warehouse]
                      .filter(Boolean).join(' · ') || null),
                warn: shipPairs.leftUnpaired(i),
              }))}
              head={['Дата', 'Сумма']} />
            <RcSection title="Оплаты" total={p1} count={cPay.length}
              // Названия кассы/счёта в выгрузке 1С нет — только вид оплаты
              // (касса или банк), он и стоит в колонке «Тип».
              rows={cPay.map((p, i) => ({
                cells: [p.date, p.kind === 'cash' ? 'касса' : 'банк', money(p.amount_kgs)],
                note: payPairs.leftUnpaired(i) ? 'нет пары в SalesDoc' : null,
                warn: payPairs.leftUnpaired(i),
              }))}
              head={['Дата', 'Тип', 'Сумма']} />
            <RcSection title="Возвраты" total={r1} count={cRet.length}
              rows={cRet.map((r) => [r.date, money(r.amount)])} head={['Дата', 'Сумма']} />
          </div>

          {/* ---- SalesDoc ---- */}
          <div className="rc-col">
            <div className="rc-col-title">SalesDoc {row.in_sd ? '' : '· нет'}</div>
            <RcSection title="Реализации" total={sd?.orders?.total} count={sd?.orders?.count}
              rows={sdShip
                .map((o, i) => ({
                  // Номер обязателен: в один день бывает несколько отгрузок на
                  // равные суммы, и без него не понять, какая из них какой
                  // накладной 1С соответствует. code_1C — номер, присвоенный
                  // при обмене с 1С; если его нет, показываем ИД SalesDoc.
                  cells: [o.date, o.status_label, money(o.amount)],
                  // Номер + склад одной строкой, как в колонке 1С: по складу
                  // сразу видно, чьей фирме документ и почему пара не сошлась.
                  note: isFuture(o.date)
                    ? 'дата в будущем!'
                    : shipPairs.rightUnpaired(i)
                      ? 'нет пары в 1С'
                      : [orderNote(o), o.store].filter(Boolean).join(' · '),
                  warn: isFuture(o.date) || shipPairs.rightUnpaired(i),
                  muted: !o.counted,
                }))}
              head={['Дата', 'Статус', 'Сумма']} />
            {sd?.orders?.hidden_by_store > 0 && (
              <div className="muted sd-pay-diag">
                Ещё {sd.orders.hidden_by_store} реализаций скрыто отбором по
                складам выбранной фирмы
                {sd.orders.hidden_stores?.length > 0 && (
                  <> — они лежат на складах: <b>{sd.orders.hidden_stores.join(', ')}</b></>
                )}.
                {' '}Переключитесь на «Обе фирмы» или задайте фирму складам,
                чтобы увидеть их.
              </div>
            )}
            <RcSection title="Оплаты" total={sd?.payments?.total} count={sd?.payments?.count}
              rows={sdPay.map((p, i) => ({
                // У обычной оплаты пишем только способ («Наличный»): слово
                // «Оплата» и так следует из названия блока, а длинная подпись
                // переносилась на вторую строку и ломала выравнивание с 1С.
                cells: [
                  p.date,
                  p.counted ? (p.type_name || 'Оплата') : p.txn_label,
                  money(p.amount),
                ],
                // Дата в будущем — почти всегда опечатка в годе при ручном
                // вводе. В интерфейсе SalesDoc такие записи часто не видны
                // (там стоит фильтр по периоду), а баланс они двигают.
                // Склада у оплаты нет, но есть заказы, которые она гасит —
                // по их складам и видно, чьей фирме эта оплата.
                note: isFuture(p.date)
                  ? 'дата в будущем!'
                  : payPairs.rightUnpaired(i)
                    ? 'нет пары в 1С'
                    : (p.stores?.length ? p.stores.join(', ') : shortId(p.sd_id)),
                warn: isFuture(p.date) || payPairs.rightUnpaired(i),
                muted: !p.counted,
                action: (
                  <button className="store-stat store-stat-link"
                    onClick={() => setRawPay(rawPay === p.sd_id ? null : p.sd_id)}>
                    {rawPay === p.sd_id ? 'скрыть ответ SD' : 'сырой ответ SD'}
                  </button>
                ),
              }))}
              head={['Дата', 'Вид', 'Сумма']} />
            {/* Оплата без связи с заказами к фирме не относится никак —
                показываем её при любой фирме и говорим об этом прямо, иначе
                она читается как «заплатили именно этой фирме». */}
            {sd?.payments?.hidden_by_store > 0 && (
              <div className="muted sd-pay-diag">
                Ещё {sd.payments.hidden_by_store} оплат скрыто: они гасят заказы
                другой фирмы
                {sd.payments.hidden_stores?.length > 0 && (
                  <> (<b>{sd.payments.hidden_stores.join(', ')}</b>)</>
                )}.
              </div>
            )}
            {sd?.payments?.items?.some((p) => !p.stores?.length) && (
              <div className="muted sd-pay-diag">
                Оплаты без склада под датой не привязаны ни к одному заказу
                (аванс, начальный остаток) — поделить их по фирмам нечем, они
                показываются в обеих.
              </div>
            )}
            {rawPay && <PaymentRaw sdId={rawPay} />}
            {sd?.payments && sd.payments.matched === 0 && (
              <div className="muted sd-pay-diag">
                {sd.payments.scanned > 0
                  ? `Проверено ${sd.payments.scanned} операций SalesDoc за период — ни одна не привязана к этому клиенту (оплата записана на другого клиента/кассу).`
                  : 'Оплат этого клиента в SalesDoc не найдено. Если в SalesDoc они есть — данные ещё не догрузились, нажмите «↻ Обновить».'}
              </div>
            )}
            <RcSection title="Возвраты" total={sd?.returns?.total} count={sd?.returns?.count}
              rows={(sd?.returns?.items || []).map((r) => ({
                cells: [r.date, money(r.amount)],
                note: shortId(r.sd_id),
              }))}
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
            : 'Баланс SalesDoc меньше, чем следует из операций: значит его уменьшила операция, которой нет среди реализаций, оплат и возвратов — списание долга, начальный остаток или конверсия. Нажмите «Почему не видно?» — там весь журнал по клиенту с разбивкой по видам.',
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

// «Почему не видно?» — сверяет, что лежит в зеркале и что отдаёт SalesDoc
// напрямую по этому клиенту. Отвечает на вопрос «в SD операция есть, а в
// портале нет»: сразу видно, потерялась запись при выгрузке или SalesDoc
// записал её на другой идентификатор клиента.
function ClientDebug({ row }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  function run() {
    setLoading(true); setError(null)
    api.salesdocClientDebug({ sd_id: row.sd_id, code_1c: row.code_1C })
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }

  return (
    <div className="cd-debug">
      <button className="btn btn-sm btn-ghost" disabled={loading} onClick={run}>
        {loading ? 'Проверяю…' : '🔎 Почему не видно?'}
      </button>
      {error && <div className="error">{error}</div>}
      {data && (
        <div className="an-grid">
          <AnRow label="В зеркале по этому клиенту"
            value={`реализаций ${data.mirror.orders} · оплат ${data.mirror.payments}`} />
          <AnRow label="SalesDoc отдаёт по этому клиенту"
            value={`реализаций ${data.live.orders.by_sd_id + data.live.orders.by_cs_id}`
              + ` · оплат ${data.live.payments.by_sd_id + data.live.payments.by_cs_id}`
              + ` · возвратов ${data.live.defects.by_sd_id + data.live.defects.by_cs_id}`}
            warn={data.live.payments.by_sd_id + data.live.payments.by_cs_id
                  > data.mirror.payments} />
          <AnRow label="Найдено по коду 1С"
            value={`реализаций ${data.live.orders.by_code_1c}`
              + ` · оплат ${data.live.payments.by_code_1c}`} />
          <AnRow label="Всего просмотрено в SalesDoc"
            value={`${data.live.orders.scanned} заказов · ${data.live.payments.scanned} операций`} />
          <AnRow label="Всего строк в зеркале"
            value={`${data.mirror.orders_total_rows} заказов · ${data.mirror.payments_total_rows} операций`} />
          {data.live.orders.by_store?.length > 0 && (
            <>
              <div className="an-sub">Заказы клиента по складам</div>
              {data.live.orders.by_store.map((s) => (
                <div key={s.store} className="an-store">
                  <span>{s.store} <span className="muted">· {s.statuses}</span></span>
                  <span className="num">{money(s.sum)} · {s.count}</span>
                </div>
              ))}
            </>
          )}
          {data.live.payments.by_txn?.length > 0 && (
            <>
              <div className="an-sub">Операции журнала SalesDoc по этому клиенту</div>
              {data.live.payments.by_txn.map((t) => (
                <div key={t.txn} className="an-store">
                  <span>{t.txn}</span>
                  <span className="num">{money(t.sum)} · {t.count}</span>
                </div>
              ))}
              <div className="muted sd-pay-diag">
                Баланс SalesDoc двигают не только оплаты: списание долга,
                начальный остаток и конверсия тоже. Если операции сходятся, а
                баланс — нет, причина обычно в этом списке.
              </div>
            </>
          )}
          {data.live.payments.client_refs.length > 0 && (
            <>
              <div className="an-sub">Как SalesDoc записал клиента в оплатах</div>
              {data.live.payments.client_refs.map((c, i) => (
                <div key={i} className="an-store">
                  <span className="muted">
                    SD_id: {c.SD_id || '—'} · CS_id: {c.CS_id || '—'} · код 1С: {c.code_1C || '—'}
                  </span>
                </div>
              ))}
            </>
          )}
        </div>
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
                // Номер документа — мелкой строкой под датой, а не колонкой:
                // идентификаторы SalesDoc длинные и выдавливали сумму за край.
                const note = !Array.isArray(r) && r.note
                const warn = !Array.isArray(r) && r.warn
                const action = !Array.isArray(r) && r.action
                return (
                  <tr key={i} className={muted ? 'rc-row-muted' : ''}>
                    {cells.map((c, j) => (
                      <td key={j} className={j === cells.length - 1 ? 'num' : ''}>
                        {j === 0 ? fdate(c) : c}
                        {j === 0 && note && (
                          <div className={`rc-note ${warn ? 'rc-note-warn' : ''}`}
                            title={note === 'дата в будущем!'
                              ? 'Скорее всего опечатка в годе — в SalesDoc такая запись не видна из-за фильтра по периоду, но баланс двигает'
                              : note}>
                            {note}
                          </div>
                        )}
                        {j === 0 && action}
                      </td>
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
                    title={`ИД склада в SalesDoc: ${w.store_id}`}
                    onClick={() => setOpenStore(openStore === w.store_id ? null : w.store_id)}>
                    {openStore === w.store_id ? '▾' : '▸'} {w.store}
                    <span className="muted">
                      {' '}· {ORG_LABELS[w.org] || 'фирма не задана'}
                      {' '}· {w.positions
                        ? `${qty(w.total_qty)} шт · ${w.positions} поз.`
                        : 'пусто'}
                    </span>
                  </button>
                  {openStore === w.store_id && (
                    w.items.length === 0 ? (
                      <div className="muted rc-empty">Остатков нет</div>
                    ) : (
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
                    )
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

// Реализации 1С ↔ SalesDoc: сколько документов с каждой стороны, разбивка по
// складам и поимённые списки тех, что не нашли пару. Сопоставление идёт по
// номеру накладной — в SalesDoc он приходит в code_1C после обмена с 1С.
function ShipmentsComparePanel() {
  const [open, setOpen] = useState(false)
  const [dr, setDr] = useState(() => ({
    from: `${new Date().getFullYear()}-01-01`,
    to: toISODate(new Date()),
  }))
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [filter, setFilter] = useState('')

  function load(r = dr) {
    setLoading(true); setError(null)
    api.salesdocShipmentsCompare({ date_from: r.from, date_to: r.to })
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  // Один список на всё: фильтр только сужает его, а не уводит в другую вкладку.
  const FILTERS = [
    ['', 'Все'],
    ['diff', 'Суммы разные'],
    ['only_1c', 'Нет в SalesDoc'],
    ['only_sd', 'Нет в 1С'],
    ['ok', 'Сходится'],
  ]
  const list = (data?.rows || []).filter((r) => !filter || r.verdict === filter)
  const VERDICT = {
    ok: ['сходится', 'sc-ok'],
    diff: ['суммы разные', 'sc-diff'],
    only_1c: ['нет в SalesDoc', 'sc-bad'],
    only_sd: ['нет в 1С', 'sc-bad'],
  }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 📦 Реализации: 1С ↔ SalesDoc
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Каждая реализация одной строкой: склад и сумма с
            обеих сторон, статус в SalesDoc и вердикт. Сопоставление — по номеру
            накладной (в SalesDoc он в поле «код 1С»), а если номера нет — по
            клиенту, дате и сумме. Учитываются только отгруженные документы
            SalesDoc: «Новый» ещё не отгружен, «Отменён» — не продажа.</p>
          <div className="rc-period">
            <span className="muted">Период:</span>
            <input type="date" className="filter-select" value={dr.from}
              onChange={(e) => setDr((d) => ({ ...d, from: e.target.value }))} />
            <span className="muted">—</span>
            <input type="date" className="filter-select" value={dr.to}
              onChange={(e) => setDr((d) => ({ ...d, to: e.target.value }))} />
            <button className="btn btn-sm" onClick={() => load()} disabled={loading}>
              {loading ? 'Считаю…' : 'Показать'}
            </button>
          </div>
          {error && <div className="error">{error}</div>}
          {data && (
            <>
              <div className="sc-totals">
                <div><span className="muted">1С</span><b>{data.our.count} док.</b><span>{money(data.our.amount)}</span></div>
                <div><span className="muted">SalesDoc</span><b>{data.sd.count} док.</b><span>{money(data.sd.amount)}</span></div>
                <div><span className="muted">Сходится</span><b>{data.counts.ok}</b><span /></div>
                <div><span className="muted">Расходится</span>
                  <b>{data.counts.diff + data.counts.only_1c + data.counts.only_sd}</b><span /></div>
              </div>

              {/* Выгрузка продаж 1С не всегда содержит номер и склад. Пока это
                  так, сверять по номеру нечем — честно об этом говорим, иначе
                  «совпало 0» читается как поломка сверки. */}
              {data.our.no_number > 0 && (
                <p className="note-readonly sd-warn">
                  У {data.our.no_number} из {data.our.count} документов 1С нет
                  номера{data.our.no_warehouse > 0 && `, у ${data.our.no_warehouse} — склада`}.
                  {' '}По номеру сопоставлено {data.matched_by_number}, остальное — по
                  клиенту, дате и сумме. Чтобы сверка стала точной, в выгрузку
                  продаж из 1С нужно добавить колонки «Номер документа» и «Склад».
                </p>
              )}

              <div className="rc-cols sc-stores">
                <div className="rc-col">
                  <div className="rc-col-title">Склады 1С</div>
                  <StoreStats rows={data.our.by_store} />
                </div>
                <div className="rc-col">
                  <div className="rc-col-title">Склады SalesDoc</div>
                  <StoreStats rows={data.sd.by_store} />
                </div>
              </div>

              <div className="sc-tabs">
                {FILTERS.map(([key, label]) => (
                  <button key={key}
                    className={`btn btn-sm ${filter === key ? 'btn-primary' : 'btn-ghost'}`}
                    onClick={() => setFilter(key)}>
                    {label}{key && ` · ${data.counts[key]}`}
                  </button>
                ))}
              </div>
              {list.length === 0 ? (
                <div className="muted rc-empty">Таких документов нет 🎉</div>
              ) : (
                <div className="table-wrap rc-table sc-table">
                  <table>
                    <thead>
                      <tr>
                        <th>Дата</th><th>Клиент</th>
                        <th>Склад 1С</th><th className="num">Сумма 1С</th>
                        <th>Склад SalesDoc</th><th>Статус</th><th className="num">Сумма SD</th>
                        <th className="num">Разница</th><th>Итог</th>
                      </tr>
                    </thead>
                    <tbody>
                      {list.map((r, i) => {
                        const [label, cls2] = VERDICT[r.verdict]
                        return (
                          <tr key={i}>
                            <td>{fdateShort(r.date)}</td>
                            <td>{r.client}
                              {/* Номера документов с обеих сторон — мелко под
                                  клиентом, чтобы не раздувать таблицу. */}
                              <div className="rc-note" title={`1С: ${r.doc_number || '—'} · SD: ${r.sd_doc || '—'}`}>
                                {r.doc_number || '—'} · {r.sd_doc || '—'}
                              </div>
                            </td>
                            <td>{r.our_warehouse || <span className="muted">—</span>}</td>
                            <td className="num">{r.our_amount == null ? '—' : money(r.our_amount)}</td>
                            <td>{r.sd_store || <span className="muted">—</span>}</td>
                            <td>{r.sd_status || <span className="muted">—</span>}</td>
                            <td className="num">{r.sd_amount == null ? '—' : money(r.sd_amount)}</td>
                            <td className={`num ${r.diff ? cls(r.diff) : ''}`}>
                              {r.diff == null ? '—' : money(r.diff)}
                            </td>
                            <td><span className={`sd-reason ${cls2}`}>{label}</span></td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
              {data.total_rows > data.cap && (
                <div className="muted sd-pay-diag">
                  Всего строк {data.total_rows}, показаны первые {data.cap} —
                  сузьте период, чтобы увидеть остальные.
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function StoreStats({ rows }) {
  if (!rows || rows.length === 0) return <div className="muted rc-empty">Нет записей</div>
  return (
    <div className="table-wrap rc-table">
      <table>
        <thead><tr><th>Склад</th><th className="num">Док.</th><th className="num">Сумма</th></tr></thead>
        <tbody>
          {rows.map((s, i) => (
            <tr key={i}>
              <td>{s.name}</td>
              <td className="num">{s.count}</td>
              <td className="num">{money(s.amount)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// История смен склада и статуса — та, которой в SalesDoc нет. Зеркало раз в
// час перечитывает журнал целиком, поэтому правку оно видит, даже если сам
// SalesDoc её нигде не сохранил.
function OrderChangesPanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function load() {
    setLoading(true); setError(null)
    api.salesdocOrderChanges()
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  const rows = data?.changes || []
  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 🕓 Изменения документов SalesDoc
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">В SalesDoc правки склада в истории документа не
            сохраняются: там остаётся первый склад, а когда и на что его
            поменяли — узнать негде. Портал перечитывает журнал целиком раз в
            час, поэтому такие правки замечает и записывает здесь. История
            копится с момента этой доработки — задним числом её взять неоткуда.</p>
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Загрузка…</div>}
          {data && rows.length === 0 && (
            <div className="muted">Изменений пока не замечено.</div>
          )}
          {rows.length > 0 && (
            <div className="table-wrap rc-table">
              <table>
                <thead>
                  <tr>
                    <th>Замечено</th><th>Документ</th><th>Точка</th>
                    <th>Что</th><th>Было → стало</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((c, i) => (
                    <tr key={i}>
                      <td>{(c.noticed_at || '').replace('T', ' ').slice(0, 16)}</td>
                      <td>{shortId(c.order_sd_id)}<div className="rc-note">{fdateShort(c.doc_date)}</div></td>
                      <td>{c.client}</td>
                      <td>{c.field_label}</td>
                      <td>{c.old} → <b>{c.new}</b></td>
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

// Кассы журнала оплат. У оплаты в SalesDoc нет склада, поэтому для точки, у
// которой в SalesDoc одни оплаты, фирму по складам не вычислить. Касса —
// единственный признак «куда посадили деньги»; сначала смотрим, разведены ли
// кассы по фирмам, и только потом делаем из них привязку.
function CashboxPanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function load() {
    setLoading(true); setError(null)
    api.salesdocCashboxes()
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  const list = data?.cashboxes || []
  const sp = data?.split
  const pct = (n, d) => (d ? Math.round((n / d) * 100) : 0)

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 💰 Оплаты: чем делить по фирмам
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Склада у оплаты в SalesDoc нет. Признаков фирмы в
            операции ровно три кандидата: касса, поле trade и связь с заказами
            (orders). Здесь видно, сколько оплат каждый из них реально
            покрывает — решение принимаем по фактам, а не по догадкам.</p>
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Считаю…</div>}
          {sp && (
            <>
              <div className="sc-totals">
                <div><span className="muted">Оплат всего</span><b>{sp.total}</b><span /></div>
                <div><span className="muted">Привязаны к заказам</span>
                  <b>{sp.linked}</b><span>{pct(sp.linked, sp.total)}%</span></div>
                <div><span className="muted">Без привязки</span>
                  <b>{sp.unlinked}</b><span>{pct(sp.unlinked, sp.total)}%</span></div>
              </div>
              {sp.linked === 0 && (
                <div className="note-readonly sd-warn">
                  Ни одна оплата не привязана к заказам. Значит поле orders в
                  SalesDoc не заполняется, и делить оплаты по фирмам через него
                  нельзя — нужен другой признак или ручное решение.
                </div>
              )}
              <div className="rc-cols sc-stores">
                <div className="rc-col">
                  <div className="rc-col-title">Кассы</div>
                  <ValueStats rows={sp.cashboxes} />
                  <div className="rc-col-title">Поле trade</div>
                  <ValueStats rows={sp.trades} />
                </div>
                <div className="rc-col">
                  <div className="rc-col-title">Привязка по годам</div>
                  <div className="table-wrap rc-table">
                    <table>
                      <thead><tr><th>Год</th><th className="num">Оплат</th><th className="num">С заказами</th></tr></thead>
                      <tbody>
                        {sp.by_year.map((y, i) => (
                          <tr key={i}>
                            <td>{y.year}</td>
                            <td className="num">{y.total}</td>
                            <td className="num">{y.linked} ({pct(y.linked, y.total)}%)</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="rc-col-title">Способы оплаты</div>
                  <ValueStats rows={sp.types} />
                </div>
              </div>
            </>
          )}
          {list.length > 0 && (
            <>
              <div className="rc-col-title">Кассы: обороты</div>
              <div className="table-wrap rc-table">
                <table>
                  <thead>
                    <tr><th>Касса</th><th className="num">Операций</th><th className="num">Сумма</th></tr>
                  </thead>
                  <tbody>
                    {list.map((c, i) => (
                      <tr key={i}>
                        <td>{c.name}</td>
                        <td className="num">{c.count}</td>
                        <td className="num">{money(c.amount)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function ValueStats({ rows }) {
  if (!rows || rows.length === 0) return <div className="muted rc-empty">Нет данных</div>
  return (
    <div className="table-wrap rc-table">
      <table>
        <thead><tr><th>Значение</th><th className="num">Оплат</th></tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}><td>{r.value}</td><td className="num">{r.count}</td></tr>
          ))}
        </tbody>
      </table>
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
  const [openStore, setOpenStore] = useState(null)

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
            <div key={r.store_id}>
              <div className="store-row">
                <span className="store-name">
                  {r.name || r.store_id}
                  <span className="muted"> · {r.store_id}</span>
                  {/* Сколько отгрузок реально прошло по складу — привязку
                      делаешь по факту, а не по названию. Клик разворачивает
                      сами документы: «что за реализации тут лежат» — первый
                      вопрос, который возникает к непонятному складу. */}
                  {r.stats ? (
                    <button className="store-stat store-stat-link"
                      onClick={() => setOpenStore(openStore === r.store_id ? null : r.store_id)}>
                      {`${r.stats.shipped_count} отгружено · ${money(r.stats.amount)}`}
                      {r.stats.count > r.stats.shipped_count
                        && ` (+${r.stats.count - r.stats.shipped_count} новых/отменённых)`}
                      {` · ${fdateShort(r.stats.first)} — ${fdateShort(r.stats.last)}`}
                      {openStore === r.store_id ? ' ▾' : ' ▸'}
                    </button>
                  ) : (
                    <span className="store-stat">реализаций нет</span>
                  )}
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
              {openStore === r.store_id && <StoreOrders storeId={r.store_id} />}
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

// Документы, лежащие на конкретном складе SalesDoc.
function StoreOrders({ storeId }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [raw, setRaw] = useState(null)

  useEffect(() => {
    let alive = true
    setData(null); setError(null)
    api.salesdocStoreOrders(storeId)
      .then((d) => alive && setData(d)).catch((e) => alive && setError(e.message))
    return () => { alive = false }
  }, [storeId])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="muted store-orders">Загрузка…</div>
  if (data.items.length === 0) {
    return <div className="muted store-orders">Реализаций на этом складе нет.</div>
  }
  return (
    <div className="store-orders">
      <div className="table-wrap rc-table">
        <table>
          <thead>
            <tr>
              <th>Дата</th><th>Документ</th><th>Точка</th><th>Статус</th>
              <th className="num">Сумма</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((r, i) => (
              // Отменённые и новые показываем приглушённо: они на складе есть,
              // но в сумму отгрузок не идут.
              <tr key={i} className={r.counted ? '' : 'rc-row-muted'}>
                <td>{fdateShort(r.date)}</td>
                <td>
                  {r.doc_number || <span className="muted">{shortId(r.sd_id)}</span>}
                  {/* Когда портал и интерфейс SalesDoc показывают разный склад,
                      спор решает только сырой ответ метода getOrder. */}
                  <button className="store-stat store-stat-link"
                    onClick={() => setRaw(raw === r.sd_id ? null : r.sd_id)}>
                    {raw === r.sd_id ? 'скрыть ответ SD' : 'сырой ответ SD'}
                  </button>
                </td>
                <td>{r.client}</td>
                <td>{r.status_label}</td>
                <td className="num">{money(r.amount)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {raw && <OrderRaw sdId={raw} />}
      <div className="muted store-orders-total">
        Отгружено: <b>{money(data.amount)}</b> · всего документов {data.count}
      </div>
    </div>
  )
}

// Сырой ответ SalesDoc по оплате: список полей операции целиком. Ответ на
// вопрос «есть ли в оплате хоть какой-то признак фирмы» — только тут.
function PaymentRaw({ sdId }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    setData(null); setError(null)
    api.salesdocPaymentRaw(sdId)
      .then((d) => alive && setData(d)).catch((e) => alive && setError(e.message))
    return () => { alive = false }
  }, [sdId])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="muted store-orders">Спрашиваю SalesDoc…</div>
  return (
    <div className="order-raw">
      <div>
        <b>В зеркале:</b> {money(data.mirror.amount)}, вид {data.mirror.txn},
        способ {data.mirror.type_name || '—'}, касса{' '}
        <code>{data.mirror.cashbox_name || data.mirror.cashbox_sd_id || 'не задана'}</code>
      </div>
      {/* Главное в ответе — какие заказы гасит оплата: склада у неё нет, а у
          заказа есть, и это единственный признак фирмы. */}
      <div>
        <b>Гасит заказы:</b>{' '}
        {data.linked.length === 0
          ? <span className="muted">ни одного — оплата ни к чему не привязана,
              поделить её по фирмам нечем</span>
          : null}
      </div>
      {data.linked.length > 0 && (
        <ul className="order-raw-sib">
          {data.linked.map((o, i) => (
            <li key={i}>
              {o.found
                ? <>{fdateShort(o.date)} · склад <b>{o.store || '—'}</b> · {o.status} · {money(o.amount)}</>
                : <span className="muted">{o.sd_id} — заказа нет в зеркале</span>}
            </li>
          ))}
        </ul>
      )}
      <div>
        <b>Поля операции в SalesDoc:</b>{' '}
        {data.fields.length > 0
          ? <code>{data.fields.join(', ')}</code>
          : <span className="muted">операция в ответе не найдена</span>}
      </div>
      <details>
        <summary className="muted">Показать ответ целиком (JSON)</summary>
        <pre className="order-raw-json">{JSON.stringify(data.raw, null, 2)}</pre>
      </details>
    </div>
  )
}

// Сырой ответ SalesDoc по документу: слева то, что лежит у нас в зеркале,
// ниже — то, что прямо сейчас отдаёт метод getOrder. Если склады разные,
// видно сразу, чья это правда.
function OrderRaw({ sdId }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    setData(null); setError(null)
    api.salesdocOrderRaw(sdId)
      .then((d) => alive && setData(d)).catch((e) => alive && setError(e.message))
    return () => { alive = false }
  }, [sdId])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="muted store-orders">Спрашиваю SalesDoc…</div>

  const store = (o) => (o?.store || {}).name || (o?.store || {}).SD_id || '—'
  return (
    <div className="order-raw">
      <div>
        <b>В зеркале:</b> склад <code>{data.mirror.store_sd_id || '—'}</code>,
        статус {data.mirror.status}, {money(data.mirror.amount)},
        обновлено {data.mirror.synced_at || '—'}
      </div>
      <div>
        <b>Сейчас в SalesDoc:</b>{' '}
        {data.raw ? <>склад <code>{store(data.raw)}</code>, статус {data.raw.status}</>
          : <span className="muted">документ в ответе не найден</span>}
      </div>
      {data.siblings?.length > 0 && (
        <div className="order-raw-sib">
          Документы этой точки за соседние дни:
          <ul>
            {data.siblings.map((o, i) => (
              <li key={i}>
                {o.dateDocument || o.dateCreate} · {o.SD_id || o.CS_id} · склад{' '}
                <b>{store(o)}</b> · статус {o.status} ·{' '}
                {money(Number(o.totalSummaAfterDiscount || o.totalSumma || 0))}
              </li>
            ))}
          </ul>
        </div>
      )}
      <details>
        <summary className="muted">Показать ответ целиком (JSON)</summary>
        <pre className="order-raw-json">{JSON.stringify(data.raw, null, 2)}</pre>
      </details>
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

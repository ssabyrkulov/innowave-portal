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
  // 'diff' — по величине расхождения (как раньше), 'fresh' — по свежести:
  // недавно возникшие расхождения сверху, застарелые внизу.
  const [sortMode, setSortMode] = useState('diff')

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
    let rows = debt?.rows || []
    if (reasonFilter) {
      rows = rows.filter((r) => reasonTokens(r).includes(reasonFilter))
    }
    if (sortMode === 'fresh') {
      // Сервер отдаёт строки по величине расхождения — здесь пересортируем по
      // моменту появления в списке. Строки без отметки (сошедшиеся) — вниз.
      rows = [...rows].sort((a, b) => {
        const ta = a.appeared_at ? Date.parse(a.appeared_at) : -1
        const tb = b.appeared_at ? Date.parse(b.appeared_at) : -1
        return tb - ta || Math.abs(b.diff) - Math.abs(a.diff)
      })
    }
    return rows
  }, [debt, reasonFilter, sortMode])

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
  // После «Обновить» зеркало наполняется в фоне. Раньше страница
  // перечитывалась ровно один раз через 4 секунды — к этому моменту полная
  // выгрузка обычно ещё идёт, ничего не менялось, и человек жал кнопку снова.
  // Теперь опрашиваем, пока синхронизация не закончится (но не бесконечно).
  function reloadSoon(r, od, tries = 12) {
    setTimeout(async () => {
      const d = await loadAll(r, od).catch(() => null)
      const busy = d?.sync?.running || d?.sync?.full_pending
      if (busy && tries > 1) reloadSoon(r, od, tries - 1)
    }, 5000)
  }

  // Ручная привязка точки к фирме: перекрывает догадку по складам там, где
  // SalesDoc не отдаёт реализации и вычислить фирму не из чего.
  // Разорвать ручную связку контрагента 1С с точкой SalesDoc. Связку легко
  // задать не ту (в выпадающем списке рядом стоят похожие имена), а найти и
  // отменить её до сих пор было негде — эндпоинт был, кнопки не было.
  async function unlink(row) {
    if (!row.name) return
    try {
      await api.salesdocUnlink(row.name)
      await loadAll(range, onlyDiff)
    } catch (e) {
      setError(e.message)
    }
  }

  async function setFirm(row, value) {
    if (!row.sd_id || !value) return
    try {
      await api.salesdocSetClientFirm(row.sd_id, value)
      await loadAll(range, onlyDiff)
    } catch (e) {
      setError(e.message)
    }
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
      return d
    } catch (e) {
      setError(e.message)
      return null
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
          {/* Что происходит с зеркалом прямо сейчас. Без этого «Обновить»
              выглядит как кнопка, которая ничего не делает: полная выгрузка
              идёт минутами, а на экране всё это время ничего не меняется. */}
          <SyncState sync={debt?.sync} />
          <button className="btn btn-primary" disabled={loading}
            title="Перечитать заказы и оплаты из SalesDoc. Список остаётся на месте — данные подтянутся сами."
            onClick={() => { loadAll(range, onlyDiff, true); reloadSoon(range, onlyDiff) }}>
            {loading ? 'Обновление…' : '↻ Обновить'}
          </button>
          {/* Тяжёлое обновление отдельной ссылкой: справочники, товары,
              остатки и визиты (~100 тыс. строк) нужны редко, а ждать их
              каждый раз — минуты. */}
          <button className="btn btn-ghost btn-sm" disabled={loading}
            title="Перевыгрузить и справочники: товары, склады, агенты, остатки, визиты. Идёт минутами."
            onClick={async () => {
              await api.salesdocMirrorSync(true).catch(() => {})
              reloadSoon(range, onlyDiff)
            }}>обновить всё</button>
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

      <VisitDebtPanel />

      <WhyPanel />

      <FindDocPanel />

      <ApiProbePanel />

      <MethodProbePanel />

      <JournalAnatomyPanel />

      <AgentModelPanel />

      <ByGuidPanel />

      <StoreLogPanel />

      <MovementsProbePanel />
      <HiddenOrdersProbePanel />
      <PaymentsDayPanel />
      <PaymentsByTypePanel />

      <TxnTypesPanel />

      <VisitsSamplePanel />

      <OrderChangesPanel />

      <AnalyzePanel />

      <PaymentsDebugPanel />

      <ReturnsDebugPanel />

      <StockPanel />

      <CashboxPanel />

      <SpeedProbePanel />

      <WarehouseReportPanel />

      <MatchingPanel onLinked={() => loadAll(range, onlyDiff)} />

      {/* Документы, которые SalesDoc держит в балансе, но не отдаёт в
          выгрузке. Считаются по всем точкам: у таких клиентов долги сходятся,
          поэтому в отфильтрованный список расхождений они не попадают, и без
          этой сводки масштаб проблемы не виден вовсе. */}
      {debt?.hidden?.clients > 0 && (
        <HiddenDocsBanner hidden={debt.hidden} onOpen={setDetail} />
      )}

      {debt?.unmapped_stores?.length > 0 && (
        <UnmappedStoresBanner stores={debt.unmapped_stores} />
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
            <select className="filter-select" value={sortMode}
              onChange={(e) => setSortMode(e.target.value)}
              title="Свежесть — когда расхождение впервые появилось в этом списке">
              <option value="diff">Сначала крупные</option>
              <option value="fresh">Сначала новые</option>
            </select>
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
                      {/* Имя точки в SalesDoc показываем, только когда оно
                          расходится с именем в 1С: связка идёт по ИД из
                          названия контрагента, и устаревший или неверно
                          набранный ИД молча сводит 1С с чужой точкой. */}
                      {r.sd_name_mismatch && (
                        <div className="rc-org-note rc-org-note-warn">
                          в SalesDoc это <b>{r.sd_name}</b> — имена не совпадают,
                          проверьте ИД в названии контрагента 1С
                        </div>
                      )}
                      {r.linked_by_hand && (
                        <div className="rc-note">
                          связка с точкой SalesDoc задана вручную{' '}
                          <button className="btn btn-ghost btn-sm"
                            onClick={() => unlink(r)}>разорвать</button>
                        </div>
                      )}
                      {r.sd_active === false && (
                        <div className="rc-org-note rc-org-note-warn">
                          точка неактивна в SalesDoc — в списке клиентов её не видно
                        </div>
                      )}
                      {sortMode === 'fresh' && r.appeared_at && (
                        <div className="rc-note">
                          в списке с {fdateShort(r.appeared_at.slice(0, 10))}
                        </div>
                      )}
                      {r.org_note && (
                        <div className={`rc-org-note${r.org_note_warn ? ' rc-org-note-warn' : ''}`}>
                          {r.org_note}
                        </div>
                      )}
                    </td>
                    <td data-label="Фирма" className="muted">
                      {ORG_LABELS[r.organization] || '—'}
                      {/* Точку без определённой фирмы можно привязать руками:
                          SalesDoc отдаёт не все реализации, и автоматике
                          иногда просто не на чём её вычислить. */}
                      {!r.in_1c && (r.org_note_warn || r.firm_manual) && (
                        <select className="filter-select rc-firm-pick"
                          value={r.firm_manual || ''}
                          onChange={(e) => setFirm(r, e.target.value)}>
                          <option value="">задать фирму…</option>
                          <option value="hygiene">Innowave Hygiene</option>
                          <option value="innowave">Innowave</option>
                          {r.firm_manual && <option value="clear">снять привязку</option>}
                        </select>
                      )}
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
                      {/* Сумма, которую баланс SalesDoc знает, а его журнал
                          не показывает: документы есть, но не выгружаются. */}
                      {r.reason?.includes('скрыто в SD') && (
                        <div className="rc-note" title="Баланс SalesDoc учитывает эти документы, но в выгрузке их нет">
                          {money(r.sd_hidden)}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {debt?.offset?.clients > 0 && (
        <OffsetPanel offset={debt.offset} onOpen={setDetail} />
      )}

      {detail && (
        <ReconcileDetailModal row={detail} onClose={() => setDetail(null)} />
      )}
    </div>
  )
}

/* Точки, у которых ДОЛГ сошёлся, а операции — нет. Самый неприятный случай:
   две ошибки гасят друг друга (не проведена реализация и не проведена оплата
   на ту же сумму), сальдо совпадает, и в обычном списке строка выглядит
   здоровой. Найти её можно только сравнив компоненты по отдельности. */
function OffsetPanel({ offset, onOpen }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="note-readonly sd-warn sd-offset">
      <b>Баланс сходится, а операции — нет: {offset.clients} точек.</b>{' '}
      Долг совпал с точностью до сома, но реализации, возвраты или оплаты
      расходятся — значит ошибки в двух местах взаимно погасились. Сальдо
      верное, обороты нет: такие точки не видны ни в «только расхождения», ни
      по колонке «Причина». Наибольшее расхождение — {money(offset.worst)}.{' '}
      <button className="btn btn-ghost btn-sm" onClick={() => setOpen(!open)}>
        {open ? 'Свернуть' : 'Показать точки'}
      </button>
      {open && (
        <div className="table-wrap rc-table">
          <table>
            <thead>
              <tr>
                <th>Точка</th><th>Что разошлось</th>
                <th className="num">1С</th><th className="num">SalesDoc</th>
                <th className="num">Разница</th>
              </tr>
            </thead>
            <tbody>
              {offset.top.map((o) => (
                o.gaps.map((g, j) => (
                  <tr key={o.sd_id + g.name}>
                    {j === 0 && (
                      <td rowSpan={o.gaps.length}>
                        <button className="client-link" onClick={() => onOpen(o)}>
                          {o.name}
                        </button>
                        {o.sd_name && o.sd_name !== o.name && (
                          <div className="rc-note">в SalesDoc: {o.sd_name}</div>
                        )}
                      </td>
                    )}
                    <td>{g.name}</td>
                    <td className="num">{money(g.one_c)}</td>
                    <td className="num">{money(g.sd)}</td>
                    <td className={`num ${cls(g.diff)}`}>{money(g.diff)}</td>
                  </tr>
                ))
              ))}
            </tbody>
          </table>
          <p className="muted">
            Знак везде «1С минус SalesDoc». Компоненты складываются в разницу
            долга: реализации − возвраты − оплаты. Здесь эта сумма равна нулю —
            потому строки и не попали в список расхождений.
          </p>
        </div>
      )}
    </div>
  )
}

// Сводка по документам, которые SalesDoc учитывает в балансе, но не отдаёт в
// выгрузке — типичный след деактивированного агента.
const KIND_RU = {
  orders: 'заказы', payments: 'оплаты', clients: 'точки', warehouses: 'склады',
  agents: 'агенты', products: 'товары', movements: 'перемещения',
  store_log: 'журнал склада', stock: 'остатки', visits: 'визиты',
}

/* Состояние зеркала рядом с кнопкой «Обновить»: идёт ли выгрузка, стоит ли
   полная в очереди, когда была последняя полная и не отвалился ли какой-то
   вид данных. Дельта раз в минуту берёт только изменившиеся документы, а
   старый документ с неизменным dateUpdate приезжает лишь полной выгрузкой —
   поэтому «когда была полная» важнее, чем «когда была любая». */
function SyncState({ sync }) {
  if (!sync) return null
  const errs = Object.entries(sync.errors || {})
  const full = sync.full_at
    ? new Date(sync.full_at + 'Z').toLocaleString('ru-RU',
        { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
    : null
  return (
    <span className="sd-sync-state">
      {sync.running && (
        <span className="muted" title="Кнопка обновляет только заказы и оплаты; остальное — фоновая часовая выгрузка">
          обновление{sync.current?.kind ? `: ${KIND_RU[sync.current.kind] || sync.current.kind}` : '…'}
          {sync.current?.seconds > 5 && ` · ${sync.current.seconds} с`}
        </span>
      )}
      {!sync.running && sync.full_pending && (
        <span className="muted">полное обновление в очереди</span>
      )}
      {!sync.running && !sync.full_pending && full && (
        <span className="muted" title="Полная выгрузка забирает и те документы, которые в SalesDoc не менялись">
          полностью: {full}
        </span>
      )}
      {errs.length > 0 && (
        <span className="sd-sync-err" title={errs.map(([k, v]) => `${k}: ${v}`).join('\n')}>
          · ошибка: {errs.map(([k]) => k).join(', ')}
        </span>
      )}
      {/* Прямой ответ на «новые документы не загрузились»: сколько записей
          видит SalesDoc и сколько лежит у нас. Расхождение портал лечит сам —
          при нём ближайшая синхронизация идёт полной, — но видеть его надо. */}
      {Object.entries(sync.counts || {})
        .filter(([, c]) => c && c.salesdoc !== c.ours)
        .map(([k, c]) => (
          <span key={k} className="sd-sync-err"
            title={`решение синхронизации: ${c.why || '—'}. Портал догрузит недостающее ближайшей выгрузкой.`}>
            {' '}· {KIND_RU[k] || k}: {c.salesdoc === null
              ? `SalesDoc не отдаёт счётчик, у нас ${c.ours}`
              : `в SalesDoc ${c.salesdoc}, у нас ${c.ours}`}
          </span>
        ))}
    </span>
  )
}

function HiddenDocsBanner({ hidden, onOpen }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="note-readonly sd-warn">
      <b>Скрыто от выгрузки: {hidden.clients} точек на {money(hidden.amount)}.</b>{' '}
      Столько SalesDoc держит в балансе сверх того, что складывается из его же
      журналов. Считается по всем фирмам сразу: баланс SalesDoc на фирмы не
      делится, поэтому сравнивать его можно только с полными суммами. Сюда
      попадают только точки, у которых долг с 1С разошёлся и разница не
      объясняется ни доставкой в пути, ни списанием долга — они есть и в
      основном списке, колонка «Причина» показывает, что именно разошлось.
      Прежде чем считать это виной SalesDoc, стоит открыть карточку точки:
      балансовая разница бывает и от неполной выгрузки 1С.{' '}
      <button className="btn btn-ghost btn-sm" onClick={() => setOpen(!open)}>
        {open ? 'Свернуть' : 'Показать точки'}
      </button>
      {open && (
        <div className="table-wrap rc-table">
          <table>
            <thead>
              <tr><th>Точка</th><th>Причина</th><th className="num">Скрыто</th></tr>
            </thead>
            <tbody>
              {hidden.top.map((h, i) => (
                <tr key={i}>
                  <td>
                    <button className="client-link" onClick={() => onOpen(h)}>
                      {h.name}
                    </button>
                    {h.sd_name_mismatch && (
                      <div className="rc-org-note rc-org-note-warn">
                        в SalesDoc: {h.sd_name} ({h.sd_id})
                      </div>
                    )}
                    {h.sd_active === false && (
                      <div className="rc-note">точка неактивна в SalesDoc</div>
                    )}
                  </td>
                  <td>{h.reason}</td>
                  <td className="num">{money(h.amount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// Склады без фирмы: по кнопке показываем, чьи именно точки с них отгружались —
// без этого решение «чей это склад» приходится принимать вслепую.
function UnmappedStoresBanner({ stores }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [open, setOpen] = useState(false)

  function toggle() {
    const n = !open
    setOpen(n)
    if (n && data === null) {
      api.salesdocStoreClients().then(setData).catch((e) => setError(e.message))
    }
  }

  return (
    <div className="note-readonly sd-warn">
      Складам не задана фирма: <b>{stores.join(', ')}</b>. Их реализации
      показываются в обеих фирмах — иначе они бы исчезали при переключении.
      Задайте фирму в панели «Склады SalesDoc → фирмы», чтобы деление стало
      точным.{' '}
      <button className="btn btn-ghost btn-sm" onClick={toggle}>
        {open ? 'Свернуть' : 'Показать точки'}
      </button>
      {error && <div className="error">{error}</div>}
      {open && data === null && !error && <div className="muted">Загрузка…</div>}
      {open && data?.stores.map((s) => (
        <div key={s.store_id}>
          <div className="rc-col-title">{s.name} · {s.clients.length} точек</div>
          {s.clients.length === 0 ? (
            <div className="muted">Отгрузок с этого склада нет — фирму можно
              задать любую, на цифры это не влияет.</div>
          ) : (
            <div className="table-wrap rc-table">
              <table>
                <thead>
                  <tr>
                    <th>Точка</th><th className="num">Отгрузок</th>
                    <th>Период</th><th className="num">Сумма</th>
                  </tr>
                </thead>
                <tbody>
                  {s.clients.map((c, i) => (
                    <tr key={i}>
                      <td>{c.name}</td>
                      <td className="num">{c.count}</td>
                      <td>{fdateShort(c.first)} — {fdateShort(c.last)}</td>
                      <td className="num">{money(c.amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}
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
      // Данные читаются из зеркала — они уже свежие (фоновая синхронизация
      // идёт сама). Раньше открытие карточки дополнительно запускало
      // синхронизацию и ждало зашитые 2,5 секунды, чтобы перечитать всё
      // заново: карточка «думала» лишние пару секунд на каждом открытии и
      // нагружала базу. Обновить вручную можно кнопкой ниже.
      (row.sd_id || row.code_1C)
        ? loadSd().then((d) => alive && setSd(d))
            .catch((e) => alive && setErr(e.message))
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
  // Отменённые не участвуют в сравнении, но прятать их молча нельзя: человек
  // видит документ в журнале SalesDoc и спрашивает, куда он делся у нас.
  const sdCancelled = (sd?.orders?.items || []).filter((o) => o.status === 5)
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
          {refreshing && <span className="muted rc-refreshing">обновляю…</span>}
          {!refreshing && sd?.synced_at && (
            <span className="muted rc-refreshing">данные на {fmtClock(Date.parse(sd.synced_at) / 1000)}</span>
          )}
          {/* Обновление — по кнопке, а не само при каждом открытии карточки. */}
          <button className="btn btn-ghost btn-sm" disabled={refreshing}
            onClick={() => {
              setRefreshing(true)
              api.salesdocMirrorSync(false).catch(() => {})
              setTimeout(() => {
                loadSd().then(setSd).catch(() => {}).finally(() => setRefreshing(false))
              }, 2500)
            }}>Обновить</button>
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
            {sdCancelled.length > 0 && (
              <div className="muted sd-pay-diag">
                Ещё {sdCancelled.length} отменённых реализаций (в суммы не идут):{' '}
                {sdCancelled.map((o) =>
                  `${fdateShort(o.date)} · ${money(o.amount)}`).join(', ')}.
              </div>
            )}
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
                // Подпись под датой: склад заказов, которые гасит оплата.
                // Если привязки к заказам нет, склада не существует — пишем
                // это словами, а не голым ИД: иначе разнобой «где-то склад,
                // где-то код» выглядит как ошибка, хотя это свойство данных.
                note: isFuture(p.date)
                  ? 'дата в будущем!'
                  : payPairs.rightUnpaired(i)
                    ? 'нет пары в 1С'
                    : (p.stores?.length
                        ? p.stores.join(', ')
                        : `без привязки к заказу · ${shortId(p.sd_id)}`),
                warn: isFuture(p.date) || payPairs.rightUnpaired(i),
                muted: !p.counted,
                action: (
                  <button className="store-stat store-stat-link sd-raw-btn"
                    title="Показать сырой ответ SalesDoc"
                    onClick={() => setRawPay(rawPay === p.sd_id ? null : p.sd_id)}>
                    {rawPay === p.sd_id ? '×' : '{ }'}
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
            {/* Баланс самого SalesDoc против баланса по его же журналам.
                Расхождение означает, что SalesDoc знает документы, которых
                не отдаёт в выгрузках, — и показывает их сумму. */}
            {sd?.balance?.sd != null && Math.abs(sd.balance.diff) >= 0.5 && (
              <div className="note-readonly sd-warn">
                {sd.balance.explained_by_transit ? (
                  <>
                    Баланс SalesDoc <b>{money(sd.balance.sd)}</b>, а по операциям
                    выходит <b>{money(sd.balance.by_ops)}</b>. Разница ровно
                    равна сумме заказов в статусе «Отправлен» —{' '}
                    <b>{money(sd.balance.in_transit)}</b>: SalesDoc считает
                    долгом только доставленное. Документы на месте, дело в
                    статусе.
                  </>
                ) : sd.balance.in_balance ? (
                  <>
                    Баланс SalesDoc <b>{money(sd.balance.sd)}</b>, а по его
                    операциям выходит <b>{money(sd.balance.by_ops)}</b> — разница{' '}
                    <b>{money(sd.balance.diff)}</b>. Столько SalesDoc учитывает
                    документами, которых нет в его выгрузках.
                  </>
                ) : (
                  <>
                    Этой точки нет в ответе SalesDoc о балансах — ноль в колонке
                    «Долг SD» подставлен нами, а не сообщён SalesDoc. По его же
                    операциям выходит <b>{money(sd.balance.by_ops)}</b>.
                  </>
                )}
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
      // «Нет в 1С» значит «нет операций в 1С этой фирмы»: карточка контрагента
      // может быть заведена, но пока по ней нет ни продаж, ни оплат, в
      // дебиторку она не попадает. Формулировка важна — иначе читается как
      // «контрагента вообще нет», и человек идёт искать его в справочнике.
      level: 'bad', head: 'В 1С этой фирмы операций по точке нет',
      lines: ['В 1С выбранной фирмы по этой точке нет ни отгрузок, ни оплат. ' +
              'Сам контрагент в 1С может быть заведён — в дебиторку попадают ' +
              'только точки с операциями. Проверьте в панели «Почему точка ' +
              'здесь / не здесь»: там видно, есть ли операции у другой фирмы ' +
              'и нет ли похожего имени, с которым не сработала склейка.'],
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
        // Два разных случая с одинаковым видом: отгрузку не провели в SD —
        // или провели, но выгрузка её не отдаёт (у документа деактивированный
        // агент). Не выдаём догадку за факт, а называем оба и способ проверки.
        : `В 1С реализаций больше на ${a}. Либо отгрузки не проведены в SalesDoc, `
          + `либо они там есть, но не приходят в выгрузку — так бывает у документов `
          + `деактивированных агентов. Проверьте номер документа в SalesDoc или `
          + `сверьте баланс ниже.`
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
          {/* matched — записи, совпавшие хоть по одному ключу. Раньше здесь
              складывались совпадения по SD_id и CS_id, а SalesDoc пишет туда
              одно и то же значение — цифра выходила вдвое больше реальной. */}
          <AnRow label="SalesDoc отдаёт по этому клиенту"
            value={`реализаций ${data.live.orders.matched}`
              + ` · оплат ${data.live.payments.matched}`
              + ` · возвратов ${data.live.defects.matched}`}
            warn={data.live.payments.matched > data.mirror.payments} />
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
                        {/* Заметка и кнопка «сырой ответ SD» — одной строкой.
                            Кнопка своей строкой добавляла третий этаж каждой
                            строке таблицы, и два столбца сверки переставали
                            совпадать по высоте: сравнивать их приходилось
                            прокруткой, а не глазом. */}
                        {j === 0 && (note || action) && (
                          <div className={`rc-note ${warn ? 'rc-note-warn' : ''}`}
                            title={note === 'дата в будущем!'
                              ? 'Скорее всего опечатка в годе — в SalesDoc такая запись не видна из-за фильтра по периоду, но баланс двигает'
                              : note || undefined}>
                            {note}
                            {note && action ? ' · ' : null}
                            {action}
                          </div>
                        )}
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
    ['new_sd', 'Не отгружен в SD'],
    ['only_1c', 'Нет в SalesDoc'],
    ['only_sd', 'Нет в 1С'],
    ['ok', 'Сходится'],
  ]
  const list = (data?.rows || []).filter((r) => !filter || r.verdict === filter)
  const VERDICT = {
    ok: ['сходится', 'sc-ok'],
    diff: ['суммы разные', 'sc-diff'],
    // Заявка в SalesDoc есть, но висит в «Новых»: не «завести», а «провести».
    new_sd: ['в SD не отгружен', 'sc-diff'],
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
                  <b>{data.counts.diff + data.counts.only_1c + data.counts.only_sd
                    + (data.counts.new_sd || 0)}</b><span /></div>
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
                            <td>
                              {fdateShort(r.date)}
                              {/* Дата в SalesDoc разъехалась с 1С — обычное
                                  дело после правки документа админом. */}
                              {r.sd_date && r.sd_date !== r.date && (
                                <div className="rc-note rc-note-warn">
                                  в SD: {fdateShort(r.sd_date)}
                                </div>
                              )}
                            </td>
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

// Анатомия журнала getOrder: по каким срезам выдача полна, а где дыры.
// Агент или склад, у которого в журнале ноль заказов при живом справочнике, —
// главный подозреваемый в пропаже документов.
// Модель агента: к чему он привязан — к точке или к документу. От ответа
// зависит, что делать при увольнении, поэтому зонд отвечает сырыми полями
// справочников, а не пересказом документации.
// Виды операций журнала SalesDoc. В журнале не только оплаты: там же возврат
// с полки, списание долга, выплата клиенту. Первые два портал считает, третий
// и четвёртый баланс SalesDoc меняют, а в 1С пары не имеют — и пока это не
// показано, точка со списанным долгом выглядит необъяснимым расхождением.
// Сверка по GUID документа — единственная связка, которая не врёт: поиск по
// сумме и дате с допуском рвётся от любой правки документа. Но GUID нужен с
// обеих сторон, поэтому панель сначала честно говорит, где сверка возможна.
const GUID_STATUS = {
  ready: { icon: '✅', text: 'сверка работает' },
  no_guid_1c: { icon: '⛔', text: 'нет идентификатора в 1С' },
  no_guid_sd: { icon: '⛔', text: 'нет идентификатора в SalesDoc' },
  no_guid_both: { icon: '⛔', text: 'нет идентификатора с обеих сторон' },
  shape_mismatch: { icon: '⚠️', text: 'ключи разного вида' },
  no_counterpart: { icon: '➖', text: 'пары в SalesDoc нет' },
}

// Что реально лежит в поле идентификатора. Без этого «сверка не работает»
// читается как отговорка: видно, есть ли ключ и одного ли он вида.
function IdSample({ side }) {
  if (!side.sample || side.sample.length === 0) {
    return <div className="rc-note">значений нет</div>
  }
  return (
    <div className="rc-note" title={side.sample.join('\n')}>
      {side.shape}: <span className="sd-doc-id">{side.sample[0]}</span>
    </div>
  )
}

function GuidKind({ k }) {
  const [open, setOpen] = useState(false)
  const st = GUID_STATUS[k.status] || GUID_STATUS.no_guid_both
  const clickable = k.status === 'ready' || k.status === 'shape_mismatch'
  return (
    <>
      <tr className={clickable ? 'doc-row' : ''}
        onClick={() => clickable && setOpen((v) => !v)}>
        <td>
          {clickable && <span className="muted">{open ? '▾ ' : '▸ '}</span>}
          <b>{k.label}</b>
          <div className="rc-note">
            1С: {k.ours.source} · SalesDoc: {k.theirs.source}
          </div>
        </td>
        <td>
          {st.icon} {st.text}
          {k.hint && <div className="rc-note">{k.hint}</div>}
        </td>
        <td className="num">
          {k.ours.docs_with_guid} / {k.ours.docs}
          <div className="rc-note">
            {k.ours.docs} док. из {k.ours.rows} строк
          </div>
          <IdSample side={k.ours} />
        </td>
        <td className="num">
          {k.theirs.docs_with_guid} / {k.theirs.docs}
          {k.theirs.without_guid > 0 && (
            <div className="rc-note sc-diff" title="Документы SalesDoc без code_1C — в 1С они не проведены">
              {k.theirs.without_guid} без связи с 1С
            </div>
          )}
          <IdSample side={k.theirs} />
        </td>
        <td className="num">{clickable ? k.matched : '—'}</td>
        <td className={`num ${k.only_1c_count ? 'sc-diff' : ''}`}>
          {clickable ? k.only_1c_count : '—'}
        </td>
        <td className={`num ${k.only_sd_count ? 'sc-diff' : ''}`}>
          {clickable ? k.only_sd_count : '—'}
        </td>
      </tr>
      {open && (
        <tr>
          <td className="doc-lines" colSpan={7}>
            <GuidList title="Есть в 1С, нет в SalesDoc" rows={k.only_1c} />
            <GuidList title="Есть в SalesDoc, нет в 1С" rows={k.only_sd} />
            <GuidList title="Совпали по идентификатору, но суммы разные"
              rows={k.diffs} withDelta />
          </td>
        </tr>
      )}
    </>
  )
}

function GuidList({ title, rows, withDelta }) {
  if (!rows || rows.length === 0) return null
  return (
    <>
      <div className="rc-col-title">{title} · {rows.length}</div>
      <table>
        <thead>
          <tr>
            <th>Дата</th><th>Контрагент</th>
            <th className="num">Сумма</th>
            {withDelta && <><th className="num">В SalesDoc</th><th className="num">Δ</th></>}
            <th>Идентификатор</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.guid}>
              <td>{fdateShort(r.date)}</td>
              <td>{r.label}</td>
              <td className="num">{money(r.amount)}</td>
              {withDelta && (
                <>
                  <td className="num">{money(r.sd_amount)}</td>
                  <td className="num sc-diff">{money(r.delta)}</td>
                </>
              )}
              <td><span className="sd-doc-id">{r.guid}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

// Что именно нужно сделать, чтобы сверка заработала. Список считается из
// данных, а не написан руками: виды, где SalesDoc уже отдаёт идентификатор,
// ждут только колонку в выгрузке 1С; где не отдаёт — колонка не поможет.
function GuidTodo({ kinds }) {
  const waiting = kinds.filter(
    (k) => k.status === 'no_guid_1c' && k.theirs.docs_with_guid > 0)
  const blocked = kinds.filter((k) => k.status === 'no_guid_both')
  return (
    <div className="note-readonly sd-warn">
      Сверка по идентификатору пока не работает ни по одному виду операций.
      {waiting.length > 0 && (
        <>
          <div className="rc-col-title">Ждут колонку в выгрузке 1С</div>
          <ul className="debt-other-ops">
            {waiting.map((k) => (
              <li key={k.kind}>
                <b>{k.label}</b> — SalesDoc идентификатор уже отдаёт
                ({k.theirs.docs_with_guid} из {k.theirs.docs} документов).
                Нужна колонка <code>ДокументGUID</code> в выгрузке
                «{k.ours.source}» — портал прочтёт её сам, доработок не нужно.
              </li>
            ))}
          </ul>
        </>
      )}
      {blocked.length > 0 && (
        <>
          <div className="rc-col-title">Колонка не поможет</div>
          <ul className="debt-other-ops">
            {blocked.map((k) => (
              <li key={k.kind}>
                <b>{k.label}</b> — идентификатора нет и на стороне SalesDoc
                ({k.theirs.source}), поэтому сверять будет не с чем даже после
                правки выгрузки. Здесь остаётся сопоставление по клиенту,
                сумме и дате.
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

function ByGuidPanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  function load() {
    api.salesdocByGuid().then(setData).catch((e) => setError(e.message))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  const ready = (data?.kinds || []).filter((k) => k.status === 'ready')
  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 🔗 Сверка операций по идентификатору 1С ↔ SalesDoc
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">GUID документа — единственная связка, которая не
            врёт: поиск по сумме и дате с допуском рвётся от любой правки
            документа, а идентификатор переживает и правку, и переоформление.
            1С отдаёт его в колонке <code>ДокументGUID</code>, SalesDoc — в поле{' '}
            <code>code_1C</code>. Сверка возможна там, где он есть с обеих
            сторон; где нет — написано, чего не хватает.</p>
          {error && <div className="error">{error}</div>}
          {data && (
            <>
              {ready.length === 0 && <GuidTodo kinds={data.kinds} />}
              <div className="table-wrap rc-table">
                <table>
                  <thead>
                    <tr>
                      <th>Вид операции</th>
                      <th>Состояние</th>
                      <th className="num">1С: док. с ИД / док.</th>
                      <th className="num">SD: док. с ИД / док.</th>
                      <th className="num">Совпало</th>
                      <th className="num">Только 1С</th>
                      <th className="num">Только SD</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.kinds.map((k) => <GuidKind key={k.kind} k={k} />)}
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

// getMovement нашёлся зондом (91 запись), но по имени не понять, перемещения
// это или списания. Отвечают поля: два склада — перемещение, один склад со
// статьёй затрат — списание. Смотрим сырые записи, а не гадаем.
// Журнал движений склада. Отдельного getExcretion в API нет — списание можно
// записать (setExcretion), но не прочитать. Зато в журнале склада тип
// документа Excretion есть наравне с приходами и перемещениями.
// Пустой журнал по всем складам — это либо «журнал пуст», либо «метод ждёт
// параметр, которого мы не шлём». Перебираем формы запроса разом и показываем
// сырой ответ: гадать по одному предположению за круг слишком дорого.
function StoreLogDebug() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function run() {
    setLoading(true); setError(null)
    api.salesdocStoreLogDebug()
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }

  return (
    <div className="cd-section">
      <p className="muted">Журнал пуст по всем складам. Прежде чем считать это
        фактом, стоит проверить, не ждёт ли метод другого набора параметров:
        формат даты, обязательный <code>documents</code>, иное имя поля склада.
        Перебор идёт по одному складу, чтобы не упереться в лимит запросов —
        занимает около 15 секунд.</p>
      <button className="btn btn-primary" onClick={run} disabled={loading}>
        {loading ? '⏳ Перебираю формы запроса…' : '🔎 Разобраться, почему пусто'}
      </button>
      {error && <div className="error">{error}</div>}
      {data && (
        <>
          <div className="why-verdict">{data.verdict}</div>
          <p className="muted">Склад: <code>{data.store_id}</code></p>
          <div className="table-wrap rc-table">
            <table>
              <thead>
                <tr><th>Форма запроса</th><th className="num">Строк</th><th>Массивы в ответе</th></tr>
              </thead>
              <tbody>
                {data.attempts.map((a, i) => (
                  <tr key={i} className={a.rows ? '' : 'rc-row-warn'}>
                    <td>{a.shape}</td>
                    <td className="num">{a.error ? '—' : a.rows}</td>
                    <td className="muted">
                      {a.error
                        ? a.error
                        : (Object.keys(a.arrays || {}).length
                          ? JSON.stringify(a.arrays)
                          : 'массивов нет')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <details>
            <summary className="muted">сырые ответы целиком</summary>
            <pre className="order-raw-json">{JSON.stringify(data.attempts, null, 2)}</pre>
          </details>
        </>
      )}
    </div>
  )
}

// Что означает каждый тип документа в журнале склада. Названия в ответе
// английские и без пояснений, а половина из них к нашим выгрузкам 1С
// отношения не имеет — без расшифровки таблица читается как шифр.
const LOG_DOCS = {
  Order: {
    what: 'Отгрузка по заказу клиента',
    src: '1С: Реализация товаров и услуг',
    sign: 'расход',
  },
  Purchase: {
    what: 'Поступление от поставщика',
    src: '1С: Поступление товаров и услуг',
    sign: 'приход',
  },
  Excretion: {
    what: 'Списание со склада',
    src: '1С: Списание товаров',
    sign: 'расход',
  },
  OrderDefect: {
    what: 'Возврат от клиента (брак, недовоз)',
    src: '1С: Возврат товаров от покупателя',
    sign: 'приход',
  },
  PurchaseRefund: {
    what: 'Возврат поставщику',
    src: '1С: Возврат товаров поставщику — портал не ведёт',
    sign: 'расход',
  },
  StoreCorrector: {
    what: 'Ручная корректировка остатков',
    src: 'Пары в 1С нет: правка делается прямо в SalesDoc',
    sign: 'в обе стороны',
  },
  Exchange: {
    what: 'Обмен при выездной торговле: выдача агенту в машину и возврат',
    src: 'Пары в 1С нет',
    sign: 'в обе стороны',
  },
}

function StoreLogPanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [doc, setDoc] = useState('')

  function load(kind = doc) {
    setLoading(true); setError(null)
    api.salesdocStoreLog({ document: kind })
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load('') }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 📚 Журнал движений склада (здесь списания)
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Отдельного метода для чтения списаний в API нет:
            <code>setExcretion</code> умеет записать, а <code>getExcretion</code>
            не существует. Зато <code>getStoreLog</code> отдаёт все движения
            склада, и <code>Excretion</code> — один из типов документа наравне
            с приходом и перемещением. Журнал запрашивается по каждому складу
            отдельно: этот метод требует склад обязательным параметром.</p>
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Читаю журнал по складам…</div>}
          {data && (
            <>
              <p className="muted">
                Период {fdateShort(data.period.from)} — {fdateShort(data.period.to)} ·
                складов опрошено {data.stores_asked} · строк {data.rows_total}
              </p>
              {data.errors?.length > 0 && (
                <div className="note-readonly sd-warn">
                  Складов не ответило: {data.errors.length}. HTTP 429 — это
                  лимит частоты запросов; портал ждёт и повторяет до четырёх
                  раз, но если склады всё равно отваливаются, откройте панель
                  ещё раз через минуту.
                  {data.errors.map((e, i) => (
                    <div key={i}>{e.store}: {e.error}</div>
                  ))}
                </div>
              )}
              {data.rows_total === 0 && data.errors.length < data.stores_asked && (
                <div className="note-readonly sd-warn">
                  {data.result_keys?.length > 0 ? (
                    <>Ответ пришёл, массивы в нём:{' '}
                      <b>{data.result_keys.join(', ')}</b> — но строк в них нет.</>
                  ) : (
                    <>Складов ответило {data.stores_asked - data.errors.length}, и
                    ни один не вернул ни одного массива. Значит журнал за период
                    действительно пуст либо метод требует ещё какой-то параметр —
                    но не «мы прочитали не тот ключ»: портал берёт из ответа
                    любой список, как бы он ни назывался.</>
                  )}
                  <StoreLogDebug />
                </div>
              )}
              {data.result_keys?.length > 0 && (
                <p className="muted">Массив в ответе: <code>{data.result_keys.join(', ')}</code></p>
              )}
              <div className="rc-col-title">Типы документов в журнале</div>
              <div className="table-wrap rc-table">
                <table>
                  <thead>
                    <tr><th>Тип</th><th>Что это</th>
                      <th className="num">Строк</th>
                      <th className="num">Документов</th>
                      <th className="num">Приход</th><th className="num">Расход</th>
                      <th></th></tr>
                  </thead>
                  <tbody>
                    {data.by_document.map((d) => (
                      <tr key={d.document}>
                        <td><b>{d.document}</b></td>
                        <td>
                          {LOG_DOCS[d.document]?.what || '—'}
                          {LOG_DOCS[d.document] && (
                            <div className="rc-note">{LOG_DOCS[d.document].src}</div>
                          )}
                        </td>
                        <td className="num">{d.rows}</td>
                        <td className="num">{d.docs}</td>
                        <td className="num">{d.qty_in ? `+${d.qty_in}` : '—'}</td>
                        <td className="num">{d.qty_out || '—'}</td>
                        <td>
                          <button className="btn btn-sm"
                            onClick={() => { setDoc(d.document); load(d.document) }}>
                            показать
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {data.by_document.length > 0
                && !data.by_document.some((d) => /movement/i.test(d.document)) && (
                <div className="note-readonly sd-warn">
                  Перемещений между складами (getMovement — 91 документ) в
                  журнале нет ни одной строкой. Значит остаток склада, собранный
                  только по журналу, на них не сойдётся: товар уезжает с одного
                  склада на другой мимо этого учёта. Для общего остатка это
                  безразлично, для склада — нет.
                </div>
              )}
              {data.rows.length > 0 && (
                <>
                  <div className="rc-col-title">
                    Строки{doc ? ` · ${doc}` : ''} · показано {data.rows.length}
                    {data.rows_total > data.rows.length && ` из ${data.rows_total}`}
                  </div>
                  <div className="table-wrap rc-table">
                    <table>
                      <thead>
                        <tr><th>Дата</th><th>Тип</th><th>Документ</th>
                          <th>Склад</th><th>Номенклатура</th>
                          <th className="num">Кол-во</th></tr>
                      </thead>
                      <tbody>
                        {data.rows.map((r, i) => (
                          <tr key={i}>
                            <td>{r.date}</td>
                            <td>{r.document}</td>
                            <td className="sd-doc-id">{r.document_id || '—'}</td>
                            <td>{r.store}</td>
                            <td>{r.product}</td>
                            <td className={`num ${r.quantity < 0 ? 'sc-diff' : ''}`}>
                              {r.quantity}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function PaymentsByTypePanel() {
  const [open, setOpen] = useState(false)
  const [types, setTypes] = useState([])
  const [typeId, setTypeId] = useState('')
  const [from, setFrom] = useState(`${new Date().getFullYear()}-01-01`)
  const [to, setTo] = useState(toISODate(new Date()))
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)

  function toggle() {
    const n = !open; setOpen(n)
    if (n && !types.length) {
      api.salesdocPaymentTypes().then((d) => setTypes(d.rows || [])).catch(() => {})
    }
  }
  function load() {
    setBusy(true); setErr(null)
    api.salesdocPaymentsByType({ date_from: from, date_to: to, type_id: typeId })
      .then(setData).catch((e) => setErr(e.message)).finally(() => setBusy(false))
  }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 🏦 Оплаты по способу оплаты (фирма в способе)
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Когда фирма зашита в сам способ оплаты
            («Bank Innowave (KGS)»), отбор по способу — это и есть отбор по фирме
            и месту денег. Отбор идёт по <b>идентификатору</b>, а не по названию:
            названия переименовывают, идентификатор остаётся.</p>
          <div className="rc-period">
            <input type="date" className="filter-select" value={from}
              onChange={(e) => setFrom(e.target.value)} />
            <span className="muted">—</span>
            <input type="date" className="filter-select" value={to}
              onChange={(e) => setTo(e.target.value)} />
            <select className="filter-select" value={typeId}
              onChange={(e) => setTypeId(e.target.value)}>
              <option value="">все способы</option>
              {types.map((t, i) => (
                <option key={i} value={(t.SD_id || t.CS_id || '').toLowerCase()}>
                  {t.name} · {t.SD_id || t.CS_id}
                </option>
              ))}
            </select>
            <button className="btn btn-sm" onClick={load} disabled={busy}>
              {busy ? 'Считаю…' : 'Показать'}
            </button>
          </div>
          {err && <div className="error">{err}</div>}
          {data && (
            <>
              <p className="muted">
                {data.count} операций на {formatMoney(data.total)}
                {data.type_resolved && (
                  <span> · тип «{data.type_resolved.name}», ключи: {data.type_resolved.ids.join(', ')}</span>
                )}
                {data.count === 0 && (
                  <span className="error"> · по этому способу операций нет.
                    Проверьте: обмен из 1С мог проставить другой тип — посмотрите
                    в панели «Оплаты за день» с галочкой «спросить SalesDoc напрямую»,
                    что реально лежит в поле paymentType.</span>
                )}
                {data.without_type_id > 0 && (
                  <span className="error"> · без идентификатора способа: {data.without_type_id}
                    {' '}(старые записи — заполнятся после полной синхронизации)</span>
                )}
              </p>
              <h4>По способам оплаты</h4>
              <table className="table">
                <thead><tr><th>Способ</th><th>ИД</th><th className="num">Операций</th><th className="num">Сумма</th></tr></thead>
                <tbody>
                  {(data.by_type || []).map((b, i) => (
                    <tr key={i}>
                      <td>{b.name || '—'}</td>
                      <td><code>{b.type_id}</code></td>
                      <td className="num">{b.count}</td>
                      <td className="num">{formatMoney(b.sum)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {data.sides && (
                data.sides.guid_ready ? (
                  <p className="muted">
                    Пара в 1С найдена: {data.sides.in_1c_yes} · нет в 1С: {data.sides.in_1c_no} ·
                    сопоставить нечем (без GUID): {data.sides.in_1c_unknown}
                    {data.sides.amount_mismatch > 0 && (
                      <span className="error"> · сумма расходится: {data.sides.amount_mismatch}</span>
                    )}
                  </p>
                ) : (
                  <p className="error">
                    Сверка с 1С недоступна: среди {data.sides.receipts_total} загруженных
                    оплат 1С нет ни одной с ДокументGUID. Это старый формат выгрузок —
                    сверять по идентификатору не с чем. Заработает, когда приедет пакет
                    выгрузок с колонкой ДокументGUID.
                  </p>
                )
              )}
              <h4>Операции {data.rows.length < data.count ? `(первые ${data.rows.length})` : ''}</h4>
              <table className="table">
                <thead>
                  <tr><th>Дата</th><th>Клиент</th><th>Вид</th><th>Способ</th>
                    <th className="num">Сумма</th><th>SD</th><th>1С</th></tr>
                </thead>
                <tbody>
                  {(data.rows || []).map((r, i) => (
                    <tr key={i}>
                      <td>{r.date}</td>
                      <td>{r.client || '—'}</td>
                      <td>{r.txn_name}</td>
                      <td>{r.type_name || '—'}</td>
                      <td className="num">{formatMoney(r.amount)}</td>
                      {/* Операция взята из зеркала SalesDoc — в SD она есть всегда. */}
                      <td><span className="badge badge-paid" title={r.sd_id}>есть</span></td>
                      <td>
                        {r.in_1c === 'yes' && (
                          <span className="badge badge-paid"
                            title={`${r.one_c?.payer || ''} · ${r.one_c?.date || ''} · ${r.one_c?.kind || ''}`}>
                            есть{r.amount_diff ? ` (Δ ${formatMoney(r.amount_diff)})` : ''}
                          </span>
                        )}
                        {r.in_1c === 'no' && (
                          <span className="badge badge-overdue" title={r.code_1c}>нет</span>
                        )}
                        {r.in_1c === 'unknown' && (
                          <span className="muted" title="у операции не заполнен code_1C — сопоставлять нечем">—</span>
                        )}
                        {r.in_1c === 'unavailable' && (
                          <span className="muted" title="в наших выгрузках 1С нет ДокументGUID — сверять не с чем">н/д</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function PaymentTypesBlock() {
  const [types, setTypes] = useState(null)
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)

  function load() {
    setBusy(true); setErr(null)
    api.salesdocPaymentTypes()
      .then(setTypes).catch((e) => setErr(e.message)).finally(() => setBusy(false))
  }

  return (
    <div className="rc-subblock">
      <button className="btn btn-ghost btn-sm" onClick={load} disabled={busy}>
        {busy ? 'Читаю…' : 'Показать справочник способов оплаты'}
      </button>
      {err && <div className="error">{err}</div>}
      {types && (
        <>
          <p className="muted">
            Всего {types.count}; без <code>code_1C</code>: {types.without_code_1c}.
            Обмен из 1С выбирает тип именно по <code>code_1C</code> — у типов
            без кода обмен их выбрать не сможет.
          </p>
          <table className="table">
            <thead>
              <tr><th>Название</th><th>CS_id</th><th>SD_id</th><th>code_1C</th><th>Активен</th></tr>
            </thead>
            <tbody>
              {types.rows.map((t, i) => (
                <tr key={i}>
                  <td>{t.name}{t.short ? ` (${t.short})` : ''}</td>
                  <td><code>{t.CS_id || '—'}</code></td>
                  <td><code>{t.SD_id || '—'}</code></td>
                  <td>{t.code_1C
                    ? <code>{t.code_1C}</code>
                    : <span className="error">нет</span>}</td>
                  <td>{String(t.active ?? '')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}

function PaymentsDayPanel() {
  const [open, setOpen] = useState(false)
  const [day, setDay] = useState(toISODate(new Date()))
  const [live, setLive] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function load() {
    setLoading(true); setError(null)
    api.salesdocPaymentsDay(day, live)
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 💳 Оплаты SalesDoc за день: касса и способ оплаты
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Что за операция прошла, на какую кассу села и каким
            способом. Операции <b>из 1С</b> помечены отдельно: у них заполнен
            <code>code_1C</code> — GUID документа 1С, который проставляет обмен.
            Без него операция заведена в самом SalesDoc (агентом или оператором).</p>
          <div className="rc-period">
            <input type="date" className="filter-select" value={day}
              onChange={(e) => setDay(e.target.value)} />
            <label className="muted">
              <input type="checkbox" checked={live}
                onChange={(e) => setLive(e.target.checked)} /> спросить SalesDoc напрямую
            </label>
            <button className="btn btn-sm" onClick={load} disabled={loading}>
              {loading ? 'Читаю…' : 'Показать'}
            </button>
          </div>
          {error && <div className="error">{error}</div>}
          <PaymentTypesBlock />
          {data && (
            <>
              <p className="muted">
                {data.count} операций на {formatMoney(data.total)} · из 1С: {data.from_1c} ·
                источник: {data.source === 'live' ? 'живой запрос' : 'зеркало'}
              </p>
              <h4>По кассам</h4>
              <table className="table">
                <thead><tr><th>Касса</th><th className="num">Операций</th><th className="num">Сумма</th></tr></thead>
                <tbody>
                  {(data.by_cashbox || []).map((b, i) => (
                    <tr key={i}>
                      <td>{b.cashbox}</td>
                      <td className="num">{b.count}</td>
                      <td className="num">{formatMoney(b.sum)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h4>Операции</h4>
              <table className="table">
                <thead>
                  <tr><th>ИД</th><th>Клиент</th><th>Вид</th><th>Способ</th>
                    <th>Касса</th><th className="num">Сумма</th><th>Источник</th></tr>
                </thead>
                <tbody>
                  {(data.rows || []).map((r, i) => (
                    <tr key={i}>
                      <td><code>{r.sd_id}</code></td>
                      <td>{r.client || '—'}</td>
                      <td>{r.txn_name}</td>
                      <td>{r.type_name || '—'}</td>
                      <td>{r.cashbox || '—'}</td>
                      <td className="num">{formatMoney(r.amount)}</td>
                      <td>
                        {r.from_1c
                          ? <span className="badge badge-paid" title={r.code_1c}>из 1С</span>
                          : <span className="muted">заведена в SD</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function HiddenOrdersProbePanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [sdId, setSdId] = useState('y8_96')
  const [number, setNumber] = useState('161')
  // Перебор агентов — самая долгая часть: это отдельная выгрузка журнала на
  // каждого. По умолчанию выключен, чтобы быстрые проверки (статусы, филиал,
  // номер) не приходилось ждать минутами.
  const [withAgents, setWithAgents] = useState(false)

  function load() {
    setLoading(true); setError(null)
    api.salesdocHiddenOrdersProbe({ sd_id: sdId.trim(), number: number.trim(), withAgents })
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }

  const sweep = data?.status_sweep || []
  // Строка со «своим» статусом, дающая больше строк клиента, чем наш обычный
  // набор [1..5], и есть ответ: значит документы прячет фильтр статусов.
  const ours = sweep.find((r) => r.status === 'наш обычный [1..5]')
  const best = sweep.reduce((a, r) => (r.client_rows > (a?.client_rows ?? -1) ? r : a), null)
  const verdict = (() => {
    if (!data) return null
    // Перебор агентов — решающий опыт, поэтому его вывод идёт первым.
    const ag0 = data.by_agent || {}
    if (ag0['фильтр по агенту работает'] === false)
      return 'Ключ agent сервер игнорирует: запрос с фильтром по агенту вернул ровно ту же выдачу, что и без него. Проверить версию про деактивированных агентов через API невозможно.'
    if ((ag0['фильтр по агенту дал новых документов'] ?? 0) > 0)
      return `Нашлось: запрос по конкретному агенту вернул ${ag0['фильтр по агенту дал новых документов']} документов, которых нет в общей выдаче. Версия про агентов подтверждена — чиним у себя, синхронизацией поагентно.`
    if (best && ours && best.client_rows > ours.client_rows)
      return `Нашлось: статус «${best.status}» отдаёт ${best.client_rows} реализаций вместо ${ours.client_rows}. Причина — фильтр статусов.`
    const f = data.filial || {}
    if ((f['без филиала']?.client_rows ?? 0) > (f['с филиалом']?.client_rows ?? 0))
      return 'Нашлось: без подстановки филиала документов больше — причина в филиале.'
    // Ненулевой ответ на поиск по номеру ещё ничего не значит: если сервер
    // ключ `number` не понимает, он вернёт свою выдачу по умолчанию. Узнаём
    // это по совпадению с запросом БЕЗ ключа status — у него та же природа.
    const noStatus = sweep.find((r) => r.status === 'без ключа status')
    const nums = data.by_number?.варианты || []
    const ignoredNumber = noStatus != null
      && nums.length > 0
      && nums.every((v) => v.count === noStatus.scanned)
    if (ignoredNumber)
      return `Ключ number сервер игнорирует: все четыре формы вернули по ${noStatus.scanned} записей — ровно столько же, сколько запрос вообще без фильтров. Документ по номеру не ищется, версия про фильтры запроса отпадает.`
    const found = nums.find((v) => v.count > 0)
    if (found) return `Документ нашёлся поиском по номеру (${found.shape}) — значит дело в фильтрах запроса, а не в видимости.`
    const ag = data.by_agent || {}
    if (ag['фильтр по агенту работает'] === false)
      return 'Ключ agent сервер игнорирует: выдача с фильтром по агенту и без него одинаковая. Проверить версию про деактивированных агентов через API нельзя — вопрос в поддержку SalesDoc.'
    if ((ag['фильтр по агенту дал новых документов'] ?? 0) > 0)
      return `Нашлось: запрос по конкретному агенту вернул ${ag['фильтр по агенту дал новых документов']} документов, которых нет в общей выдаче. Версия про агентов подтверждена — синхронизацию можно чинить у нас, поагентно.`
    return 'Ни один вариант не вернул скрытые документы: остаётся версия про видимость (права токена / деактивированный агент) — вопрос в поддержку SalesDoc.'
  })()

  function toggle() { const n = !open; setOpen(n) }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 🕵 Почему SalesDoc не отдаёт часть реализаций
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">По клиенту <code>y8_96</code> баланс SalesDoc сходится
            с 1С до сома — значит документы у него <b>есть</b>, но <code>getOrder</code>
            их не возвращает (984 160 KGS, 6 реализаций). Зонд перебирает подозреваемых:
            статусы по одному, филиал, поиск по номеру, сырые возвраты со ссылкой
            на родительский заказ. Только чтение. Окно — год: зонд гоняет журнал
            заказов больше десятка раз, и на трёх годах это минуты ожидания.</p>
          <div className="rc-period">
            <input className="filter-select" value={sdId}
              placeholder="SD_id клиента" onChange={(e) => setSdId(e.target.value)} />
            <input className="filter-select" value={number}
              placeholder="номер скрытого документа" onChange={(e) => setNumber(e.target.value)} />
            <label className="muted" title="Отдельная выгрузка журнала на каждого агента — самая долгая часть зонда">
              <input type="checkbox" checked={withAgents}
                onChange={(e) => setWithAgents(e.target.checked)} />{' '}
              перебрать агентов (долго)
            </label>
            <button className="btn btn-sm" onClick={load} disabled={loading || !sdId.trim()}>
              {loading ? 'Опрашиваю…' : 'Запустить'}
            </button>
          </div>
          {data?.seconds != null && (
            <p className="muted">
              Окно {data.date_from} … {data.date_to} · запросов к SalesDoc:{' '}
              {data.requests} · {data.seconds} с
            </p>
          )}
          {error && <div className="error">{error}</div>}
          {verdict && <div className="rc-note"><b>Вывод:</b> {verdict}</div>}
          {data && (
            <>
              <h4>Перебор статусов</h4>
              <table className="table">
                <thead><tr><th>Статус</th><th>Всего в выдаче</th><th>Строк клиента</th><th>Сумма клиента</th></tr></thead>
                <tbody>
                  {sweep.map((r, i) => (
                    <tr key={i} className={r.client_rows > (ours?.client_rows ?? 0) ? 'row-alert' : ''}>
                      <td>{String(r.status)}</td>
                      <td>{r.error ? <span className="error">{r.error}</span> : r.scanned}</td>
                      <td>{r.client_rows}</td>
                      <td>{r.client_sum ? formatMoney(r.client_sum) : ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h4>Филиал (настроен: {data.filial?.['настроен'] || 'нет'})</h4>
              <table className="table">
                <tbody>
                  {['с филиалом', 'без филиала'].map((k) => (
                    <tr key={k}>
                      <td>{k}</td>
                      <td>{data.filial?.[k]?.client_rows ?? '—'} строк клиента</td>
                      <td>{data.filial?.[k]?.scanned ?? '—'} всего</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {data.by_number && (
                <>
                  <h4>Поиск документа № {data.by_number['искали']}</h4>
                  <table className="table">
                    <tbody>
                      {(data.by_number['варианты'] || []).map((v, i) => (
                        <tr key={i}>
                          <td><code>{v.shape}</code></td>
                          <td>{v.error ? <span className="error">{v.error}</span> : `${v.count} записей`}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}
              {data.by_agent && !data.by_agent.skipped && (
            <>
              <h4>Перебор по агентам</h4>
              <div className="an-grid">
                <AnRow label="Фильтр по агенту работает"
                  value={data.by_agent['фильтр по агенту работает'] ? 'да' : 'нет — сервер его игнорирует'}
                  warn={!data.by_agent['фильтр по агенту работает']} />
                <AnRow label="Заказов в выдаче без фильтра"
                  value={String(data.by_agent['заказов в выдаче без фильтра'] ?? '—')} />
                <AnRow label="Документов клиента без фильтра"
                  value={String(data.by_agent['без фильтра по агенту'] ?? '—')} />
                <AnRow label="Агентов в справочнике"
                  value={`${data.by_agent['агентов в справочнике']} · уволенных ${data.by_agent['из них уволенных']}`} />
                <AnRow label="Фильтр по агенту дал новых документов"
                  value={String(data.by_agent['фильтр по агенту дал новых документов'] ?? 0)}
                  warn={(data.by_agent['фильтр по агенту дал новых документов'] ?? 0) > 0} />
              </div>
              <table className="table">
                <thead>
                  <tr><th>Агент</th><th>Работает</th><th className="num">Всего в выдаче</th>
                    <th className="num">Строк клиента</th><th className="num">Новых</th><th>Примечание</th></tr>
                </thead>
                <tbody>
                  {(data.by_agent['по агентам'] || []).map((a) => (
                    <tr key={a.sd_id}>
                      <td>{a.agent} <span className="muted">{a.sd_id}</span></td>
                      <td>{a.active ? 'да' : 'уволен'}</td>
                      <td className="num">{a.scanned ?? '—'}</td>
                      <td className="num">{a.client_rows ?? '—'}</td>
                      <td className="num">{a['новых сверх общего запроса'] ?? '—'}</td>
                      <td className="muted">{a.note || a.error || ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          <h4>Сырые возвраты клиента</h4>
              <p className="muted">Ищем в них ссылку на родительскую реализацию —
                это и будет идентификатор скрытого документа.</p>
              <pre className="order-raw-json">{JSON.stringify(data.defects_raw, null, 2)}</pre>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function MovementsProbePanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [custom, setCustom] = useState('')

  function load() {
    setLoading(true); setError(null)
    api.salesdocMovementsProbe(custom.trim())
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 📦 Складские методы SalesDoc: что в них лежит
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Списания в интерфейсе SalesDoc живут по адресу
            <code>/stock/excretion</code>, а имена методов в этом API повторяют
            разделы (movement, inventory, consumption) — значит метод должен
            называться <code>getExcretion</code>. Зонд проверяет его вместе с
            остальными складскими и показывает поля с сырыми записями: два
            склада «откуда/куда» — перемещение, один склад — списание. Можно
            опросить и любой другой метод по имени.</p>
          <div className="rc-period">
            <input className="filter-select" value={custom}
              placeholder="свой метод, напр. getExcretion"
              onChange={(e) => setCustom(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && load()} />
            <button className="btn btn-sm" onClick={load} disabled={loading}>
              {loading ? 'Опрашиваю…' : 'Опросить'}
            </button>
          </div>
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Опрашиваю SalesDoc…</div>}
          {data && Object.values(data).map((m) => (
            <div key={m.method}>
              <div className="rc-col-title">
                {m.method} · {m.error ? 'ошибка' : `${m.count} записей`}
              </div>
              {m.error && <div className="error">{m.error}</div>}
              {m.verdict && <div className="why-verdict">{m.verdict}</div>}
              {m.attempts?.length > 0 && (
                <p className="muted">
                  Формы запроса:{' '}
                  {m.attempts.map((a, i) => (
                    <span key={i}>
                      {i > 0 && ' · '}
                      {a.shape} → {a.error ? `ошибка (${a.error})` : `${a.count}`}
                    </span>
                  ))}
                  {m.worked && <> · сработала: <b>{m.worked}</b></>}
                </p>
              )}
              {m.fields?.length > 0 && (
                <div className="table-wrap rc-table">
                  <table>
                    <thead>
                      <tr><th>Поле</th><th className="num">Заполнено</th><th>Пример</th></tr>
                    </thead>
                    <tbody>
                      {m.fields.map((f) => (
                        <tr key={f.field}>
                          <td>{f.field}</td>
                          <td className="num">{f.filled}</td>
                          <td className="muted">
                            {typeof f.example === 'object'
                              ? JSON.stringify(f.example)
                              : String(f.example ?? '—')}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {m.line_field && (
                <p className="muted">Товарные строки в поле
                  <code>{m.line_field}</code>: {m.line_fields.join(', ')}</p>
              )}
              {m.sample?.length > 0 && (
                <details>
                  <summary className="muted">сырые записи ({m.sample.length})</summary>
                  <pre className="order-raw-json">
                    {JSON.stringify(m.sample, null, 2)}
                  </pre>
                </details>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function TxnTypesPanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  function load() {
    api.salesdocTxnTypes().then(setData).catch((e) => setError(e.message))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 📒 Виды операций журнала SalesDoc
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Что реально лежит в журнале операций и как портал
            это учитывает. Долг гасят оплата и возврат с полки; списание долга и
            выплата клиенту меняют баланс SalesDoc, но в 1С пары не имеют —
            поэтому в сверке они выделены отдельной причиной, а не свалены в
            «баланс SD».</p>
          {error && <div className="error">{error}</div>}
          {data && (
            <>
              <div className="table-wrap rc-table">
                <table>
                  <thead>
                    <tr><th>Вид операции</th><th>Как учитывается</th>
                      <th className="num">Записей</th><th className="num">Сумма</th>
                      <th>Период</th></tr>
                  </thead>
                  <tbody>
                    {data.types.map((t) => (
                      <tr key={t.txn}>
                        <td>{t.label} <span className="muted">#{t.txn}</span></td>
                        <td className="muted">{t.role}</td>
                        <td className="num">{t.count}</td>
                        <td className="num">{money(t.amount)}</td>
                        <td className="muted">
                          {fdateShort(t.first)} — {fdateShort(t.last)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {data.balance_only.length > 0 && (
                <>
                  <div className="rc-col-title">
                    Списания долга и выплаты клиентам · {data.balance_only.length} ·{' '}
                    {money(data.balance_only_total)}
                  </div>
                  <div className="table-wrap rc-table">
                    <table>
                      <thead>
                        <tr><th>Дата</th><th>Точка</th><th>Вид</th>
                          <th className="num">Сумма</th></tr>
                      </thead>
                      <tbody>
                        {data.balance_only.map((o) => (
                          <tr key={o.sd_id}>
                            <td>{fdateShort(o.date)}</td>
                            <td>{o.client}</td>
                            <td>{o.type_name || o.label}</td>
                            <td className="num">{money(o.amount)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function AgentModelPanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function load() {
    setLoading(true); setError(null)
    api.salesdocAgentModel()
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 🧭 Модель агента: точка или документ?
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Агент в SalesDoc может быть реквизитом карточки
            точки (тогда при увольнении точку надо переназначить) либо
            реквизитом заказа (тогда «закрепления» нет вообще — есть только след
            «кто последний продал»). Зонд смотрит сырые поля справочников и
            историю заказов и отвечает фактами.</p>
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Опрашиваю справочники SalesDoc…</div>}
          {data && (
            <>
              {(data.verdicts || []).map((v, i) => (
                <div key={i} className="why-verdict">{v}</div>
              ))}

              <div className="rc-col-title">Поля карточки точки (getClient)</div>
              <p>
                {data.client_agent_fields?.length > 0
                  ? <>Поля агента/территории: <b>{data.client_agent_fields.join(', ')}</b></>
                  : <>Полей агента в карточке точки нет.</>}
              </p>
              <pre className="order-raw-json">{JSON.stringify(data.client_sample, null, 1)}</pre>

              <div className="rc-col-title">Справочники</div>
              <p className="muted">
                Агентов: {data.agents_total} (неактивных {data.agents_inactive})
                {data.getTerritory_total != null && <> · территорий: {data.getTerritory_total}</>}
              </p>
              <pre className="order-raw-json">{JSON.stringify(
                { getAgent: data.getAgent_sample, getTerritory: data.getTerritory_sample },
                null, 1)}</pre>

              <div className="rc-col-title">Закрепление точек</div>
              <p>
                Точек всего: <b>{data.clients_total}</b> · закреплено за
                агентом: <b>{data.clients_assigned}</b> · без агента:{' '}
                <b>{data.clients_unassigned}</b>
              </p>
              <p className="muted">
                Агент в заказах (кто выписал документ): точек с заказами{' '}
                {data.clients_with_orders} · из них с разными агентами{' '}
                {data.clients_multi_agent} · заказов с агентом{' '}
                {data.orders_with_agent} из {data.orders_total}
              </p>

              {data.orphan_count > 0 && (
                <>
                  <div className="note-readonly sd-warn">
                    Точек закреплено только за уволенными:{' '}
                    <b>{data.orphan_count}</b> на {formatMoney(data.orphan_debt)}.
                    Их заказы API больше не отдаёт, вести точку некому —
                    переназначьте агента в карточке точки SalesDoc.
                  </div>
                  <div className="table-wrap rc-table">
                    <table>
                      <thead>
                        <tr><th>Точка</th><th>Уволенный агент</th><th>Маршрут</th><th className="num">Долг</th></tr>
                      </thead>
                      <tbody>
                        {data.orphan_clients.map((o) => (
                          <tr key={o.client_sd_id}>
                            <td>{o.client}</td>
                            <td>{o.agents.map((a) => a.agent).join(', ')}</td>
                            <td className="muted">{o.agents.map((a) => a.days).filter(Boolean).join(' / ') || '—'}</td>
                            <td className="num">{formatMoney(o.debt)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function JournalAnatomyPanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function load() {
    setLoading(true); setError(null)
    api.salesdocJournalAnatomy()
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 🧬 Анатомия журнала SalesDoc
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Распределение выдачи getOrder по месяцам,
            агентам, складам, направлениям торговли и типам цен, сверенное со
            справочниками. Если у агента или склада ноль заказов в журнале, а в
            справочнике он есть — скорее всего именно его документы и не
            приходят.</p>
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Разбираю журнал…</div>}
          {data && (
            <>
              {data.verdict && <div className="why-verdict">{data.verdict}</div>}
              <p>Всего в выдаче: <b>{data.total}</b> заказов ·
                {' '}без номера накладной: {data.no_invoice_number}
                {data.no_invoice_number === data.total && (
                  <span className="muted"> (поле invoiceNumber SalesDoc не
                    заполняет вовсе — искать документ можно только по ИД)</span>
                )}</p>

              {data.agents_without_orders.length > 0 && (
                <div className="note-readonly sd-warn">
                  Агенты без единого заказа в журнале:{' '}
                  <b>{data.agents_without_orders
                    .map((a) => `${a.name}${a.active === 'N' ? ' (неактивен)' : ''}`)
                    .join(', ')}</b>. Если у них есть заказы в интерфейсе
                  SalesDoc — причина пропажи найдена.
                </div>
              )}

              <div className="rc-cols">
                <div className="rc-col">
                  <div className="rc-col-title">Заказы по месяцам</div>
                  <div className="table-wrap rc-table">
                    <table>
                      <tbody>
                        {data.months.map((m, i) => (
                          <tr key={i}>
                            <td>{m.month.split('-').reverse().join('.')}</td>
                            <td className="num">{m.count}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
                <div className="rc-col">
                  <div className="rc-col-title">Агенты</div>
                  <div className="table-wrap rc-table">
                    <table>
                      <thead>
                        <tr><th>Агент</th><th>Акт.</th><th className="num">Заказов</th></tr>
                      </thead>
                      <tbody>
                        {data.agents.map((a, i) => (
                          <tr key={i} className={a.orders === 0 ? 'rc-row-warn' : ''}>
                            <td>{a.name}</td>
                            <td>{a.active === 'N' ? 'нет' : 'да'}</td>
                            <td className="num">{a.orders}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="rc-col-title">Направления торговли</div>
                  <ValueStats rows={data.by_trade.map((t) => ({ value: t.value, count: t.count }))} />
                </div>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// Опрос API SalesDoc по списку имён методов: что вообще существует, кроме
// задокументированного. Прежде всего интересуют визиты/маршруты/задачи
// агентов — на них можно построить работу с дебиторкой.
function MethodProbePanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function load() {
    setLoading(true); setError(null)
    api.salesdocMethodProbe()
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 🧪 Какие методы есть в API SalesDoc
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Зонд дёргает каждого кандидата (визиты, маршруты,
            задачи, агенты, долги…) и по ответу решает, знает ли сервер такой
            метод. Занимает ~полминуты.</p>
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Опрашиваю…</div>}
          {data && (
            <>
              <p>
                Найдено методов: <b>{data.found.length}</b>
                {data.found.length > 0 && <> — <code>{data.found.join(', ')}</code></>}
              </p>
              <ul className="order-raw-sib">
                {data.results.map((r, i) => (
                  <li key={i}>
                    {r.exists ? '✅' : '❌'} <code>{r.method}</code>
                    {r.exists && r.keys.length > 0 && (
                      <> · ключи: <code>{r.keys.join(', ')}</code>
                        {r.total != null && <> · записей {r.total}</>}</>
                    )}
                    {r.exists && r.sample && (
                      <details>
                        <summary className="muted">пример записи</summary>
                        <pre className="order-raw-json">
                          {JSON.stringify(r.sample, null, 2)}
                        </pre>
                      </details>
                    )}
                    {!r.exists && <span className="muted"> · {r.error}</span>}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// Дебиторка × визиты: у каждого должника — когда были, когда придут, когда
// платил; у агентов — долг портфеля и отдача визитов. Ответ на вопрос
// «куда ехать за деньгами».
function VisitDebtPanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function load() {
    setLoading(true); setError(null)
    api.salesdocVisitDebt()
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  const warn = (r) =>
    (r.days_since_visit == null || r.days_since_visit > 14) && !r.next_planned

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 👣 Дебиторка × визиты
      </button>
      {open && (
        <div className="store-map-body">
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Загрузка…</div>}
          {data && !data.visits_ready && (
            <div className="note-readonly sd-warn">
              Визиты ещё не доехали в зеркало — они подтягиваются при полной
              синхронизации (раз в час) или по кнопке «↻ Обновить».
            </div>
          )}
          {data && data.visits_ready && (
            <>
              <p className="muted">
                Должников: <b>{data.debtors_total}</b> на <b>{money(data.debt_sum)}</b>.
                Жёлтым — точки без визита больше двух недель и без плана:
                туда стоит ехать за деньгами в первую очередь.
              </p>
              <div className="table-wrap rc-table sc-table">
                <table>
                  <thead>
                    <tr>
                      <th>Точка</th><th className="num">Долг</th><th>Агент</th>
                      <th>Был визит</th><th>План визита</th><th>Платил</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.debtors.map((r, i) => (
                      <tr key={i} className={warn(r) ? 'rc-row-warn' : ''}>
                        <td>{r.name}</td>
                        <td className="num">{money(r.debt)}</td>
                        <td>{r.agent || <span className="muted">—</span>}</td>
                        <td>
                          {r.last_visit
                            ? <>{fdateShort(r.last_visit)} <span className="muted">({r.days_since_visit} дн.)</span></>
                            : <span className="sc-bad">не были</span>}
                        </td>
                        <td>
                          {r.next_planned
                            ? fdateShort(r.next_planned)
                            : <span className="sc-diff">нет плана</span>}
                        </td>
                        <td>
                          {r.last_payment
                            ? <>{fdateShort(r.last_payment)} <span className="muted">({r.days_since_payment} дн.)</span></>
                            : <span className="sc-bad">нет оплат</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="rc-col-title">Агенты за 30 дней</div>
              <div className="table-wrap rc-table">
                <table>
                  <thead>
                    <tr>
                      <th>Агент</th><th className="num">Долг портфеля</th>
                      <th className="num">Визитов</th><th className="num">С заказом</th>
                      <th className="num">Сумма заказов</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.agents.map((a, i) => (
                      <tr key={i}>
                        <td>{a.agent}</td>
                        <td className="num">{money(a.portfolio_debt)}</td>
                        <td className="num">{a.visits_30d}</td>
                        <td className="num">{a.with_order}</td>
                        <td className="num">{money(a.order_summa)}</td>
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

// Разбор полей визитов getVisit: сводка «поле → пример значения» и первые
// записи целиком, всё сразу развёрнуто — по этому проектируется зеркало
// визитов и «Дебиторка × визиты».
function VisitsSamplePanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function load() {
    setLoading(true); setError(null)
    api.salesdocVisitsSample()
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 👣 Визиты SalesDoc: поля
      </button>
      {open && (
        <div className="store-map-body">
          {error && <div className="error">{error}</div>}
          {loading && <div className="muted">Спрашиваю SalesDoc…</div>}
          {data && (
            <>
              <p>Всего визитов: <b>{data.total}</b> · фильтр периода:{' '}
                date → {String(data.period_filter.date)},{' '}
                dateUpdate → {String(data.period_filter.dateUpdate)}</p>
              <div className="rc-col-title">Поля визита (пример значения)</div>
              <pre className="order-raw-json">
                {JSON.stringify(data.fields, null, 2)}
              </pre>
              <div className="rc-col-title">Первые записи целиком</div>
              <pre className="order-raw-json">
                {JSON.stringify(data.rows, null, 2)}
              </pre>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// Глубокая проверка API SalesDoc: целостность пагинации, скрытые статусы,
// работоспособность фильтров периода и сырой разбор заказов точки. Нужна,
// когда интерфейс SalesDoc показывает документ, а выгрузка — нет.
function ApiProbePanel() {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [dr, setDr] = useState({ from: '', to: '' })
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  const [docNum, setDocNum] = useState('')

  function load() {
    setLoading(true); setError(null); setData(null)
    const params = { client: q.trim(), doc_number: docNum.trim() }
    if (dr.from && dr.to) { params.date_from = dr.from; params.date_to = dr.to }
    api.salesdocApiProbe(params)
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }

  const co = data?.client_orders
  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={() => setOpen(!open)}>
        {open ? '▾' : '▸'} 🔬 Глубокая проверка API SalesDoc
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Батарея замеров на живых данных: целая ли
            пагинация (сколько сервер заявляет и сколько отдаёт), нет ли
            дубликатов и статусов вне 1–5, какие ключи фильтра периода реально
            работают, и полный сырой список заказов точки со всеми полями дат и
            сумм. Занимает 10–20 секунд — журнал выгружается несколько раз.</p>
          <div className="rc-period">
            <input className="filter-select" value={q} placeholder="точка (имя или ИД)"
              onChange={(e) => setQ(e.target.value)} />
            <input className="filter-select" value={docNum} placeholder="№ заявки (1961)"
              onChange={(e) => setDocNum(e.target.value)} />
            <input type="date" className="filter-select" value={dr.from}
              onChange={(e) => setDr((d) => ({ ...d, from: e.target.value }))} />
            <span className="muted">—</span>
            <input type="date" className="filter-select" value={dr.to}
              onChange={(e) => setDr((d) => ({ ...d, to: e.target.value }))} />
            <button className="btn btn-sm" onClick={load} disabled={loading}>
              {loading ? 'Проверяю…' : 'Проверить'}
            </button>
          </div>
          {error && <div className="error">{error}</div>}
          {data && (
            <div className="order-raw">
              {data.verdicts.map((v, i) => (
                <div key={i} className="why-verdict">{v}</div>
              ))}
              {data.filial && (
                <div>
                  <b>Филиал в настройках:</b>{' '}
                  {data.filial.configured
                    ? <><code>{data.filial.configured}</code>
                        {data.filial.without_filial_total != null && (
                          <> · без филиала документов: {data.filial.without_filial_total}</>
                        )}</>
                    : <span className="muted">не задан — фильтра по филиалу нет</span>}
                </div>
              )}
              {data.no_period && (
                <div>
                  <b>Без фильтра периода:</b> {data.no_period.total} документов
                  {data.no_period.total > data.journal.declared_total && (
                    <span className="sc-bad"> · на {data.no_period.total - data.journal.declared_total} больше!</span>
                  )}
                  {data.no_period.extra?.length > 0 && (
                    <pre className="order-raw-json">
                      {JSON.stringify(data.no_period.extra, null, 2)}
                    </pre>
                  )}
                </div>
              )}
              <div>
                <b>Журнал getOrder:</b> заявлено {data.journal.declared_total} ·
                получено {data.journal.received} · уникальных {data.journal.unique}
                {data.big_page && <> · одной страницей {data.big_page.received}
                  {data.big_page.note && <span className="muted"> ({data.big_page.note})</span>}</>}
              </div>
              {data.by_number && (
                <div>
                  <b>Поиск по номеру «{data.by_number.query}»:</b>{' '}
                  {data.by_number.count === 0 ? 'не найдено' : `найдено ${data.by_number.count}`}
                  {data.by_number.orders.length > 0 && (
                    <pre className="order-raw-json">
                      {JSON.stringify(data.by_number.orders, null, 2)}
                    </pre>
                  )}
                </div>
              )}
              <div>
                <b>Статусы (1–5):</b>{' '}
                {Object.entries(data.status_histogram)
                  .map(([s, n]) => `${s}: ${n}`).join(' · ')}
              </div>
              {data.extended_statuses && !data.extended_statuses.error && (
                <div>
                  <b>Статусы (0–10):</b>{' '}
                  {Object.entries(data.extended_statuses.histogram)
                    .map(([s, n]) => `${s}: ${n}`).join(' · ')}
                </div>
              )}
              {data.period_keys && (
                <div>
                  <b>Ключи фильтра периода:</b>
                  <ul className="order-raw-sib">
                    {data.period_keys.map((p, i) => (
                      <li key={i}>
                        <code>{p.key}</code>:{' '}
                        {p.error ? `ошибка — ${p.error}` : `${p.total} записей`}
                        {p.note && <span className="why-similar"> · {p.note}</span>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {co && (
                <div>
                  <b>Заказы точки ({co.matched_clients.join(', ') || co.query}):
                    {' '}{co.count}</b>
                  <div className="muted">Поля документа: <code>{co.all_keys.join(', ')}</code></div>
                  <ul className="order-raw-sib">
                    {co.orders.map((o, i) => (
                      <li key={i}>
                        {o.sd_id} · статус {o.status} · склад {o.store || '—'} ·{' '}
                        {money(Number(o.totalSummaAfterDiscount ?? o.totalSumma ?? 0))}
                        {o.totalSumma != null && o.totalSummaAfterDiscount != null
                          && Number(o.totalSumma) !== Number(o.totalSummaAfterDiscount)
                          && <> (до скидки {money(Number(o.totalSumma))})</>}
                        {' · '}
                        {Object.entries(o.dates).map(([k, v]) => `${k}=${v}`).join(' ')}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// «Почему на портале не видно реализацию на N?» — поиск суммы сразу в трёх
// местах: 1С, зеркало, живой SalesDoc. Вердикт говорит, где документ потерялся.
function FindDocPanel() {
  const [open, setOpen] = useState(false)
  const [amount, setAmount] = useState('')
  const [q, setQ] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function today() {
    const d = toISODate(new Date())
    setFrom(d); setTo(d)
  }

  function load() {
    const a = parseFloat(String(amount).replace(',', '.').replace(/\s/g, ''))
    // Сумма не обязательна: искать можно и по точке с датой — тогда ответом
    // будут идентификаторы документов, а их-то обычно и надо назвать.
    if (!a && !q.trim() && !from && !to) return
    setLoading(true); setError(null); setData(null)
    api.salesdocFindDoc({ amount: a || undefined, query: q.trim() || undefined,
                          date_from: from || undefined, date_to: to || undefined })
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }

  const Section = ({ title, rows, render }) => (
    <div>
      <div className="rc-col-title">{title} · {rows.length}</div>
      {rows.length === 0
        ? <div className="muted rc-empty">Не найдено</div>
        : <ul className="order-raw-sib">{rows.map((r, i) => <li key={i}>{render(r)}</li>)}</ul>}
    </div>
  )

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={() => setOpen(!open)}>
        {open ? '▾' : '▸'} 🧭 Найти документ
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Любое сочетание условий: сумма, точка (имя или
            ИД вида <code>t5_388</code>), период. Портал ищет в 1С, в своём
            зеркале и напрямую в SalesDoc, показывает идентификаторы найденных
            документов и говорит, на каком шаге документ теряется: не проведён,
            не доехал до зеркала, скрыт фильтром по складу или не в том статусе.
            Прямой запрос в SalesDoc занимает несколько секунд.</p>
          <div className="rc-period">
            <input className="filter-select" value={amount} placeholder="сумма (не обяз.)"
              onChange={(e) => setAmount(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && load()} />
            <input className="filter-select" value={q} placeholder="точка или ИД"
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && load()} />
            <input className="filter-select" type="date" value={from}
              onChange={(e) => setFrom(e.target.value)} />
            <input className="filter-select" type="date" value={to}
              onChange={(e) => setTo(e.target.value)} />
            <button className="btn btn-sm" onClick={today}>сегодня</button>
            <button className="btn btn-sm" onClick={load} disabled={loading}>
              {loading ? 'Ищу…' : 'Найти'}
            </button>
          </div>
          {error && <div className="error">{error}</div>}
          {data && (
            <div className="order-raw">
              {data.verdicts.length > 0 && (
                <div>
                  {data.verdicts.map((v, i) => (
                    <div key={i} className="why-verdict">{v}</div>
                  ))}
                </div>
              )}
              {data.live_error && (
                <div className="error">SalesDoc не ответил: {data.live_error} —
                  сравниваю только 1С и зеркало.</div>
              )}
              <Section title="В 1С" rows={data.in_1c}
                render={(r) => <>{fdateShort(r.date)} · {r.client} ·{' '}
                  {r.doc_number || 'без номера'} · {r.warehouse || '—'} · {money(r.amount)}</>} />
              <Section title="В зеркале портала" rows={data.in_mirror}
                render={(r) => <>
                  <b className="sd-doc-id">{r.sd_id}</b> · {fdateShort(r.date)} ·{' '}
                  {r.client} · {r.store || '—'} · {r.status_label} ·{' '}
                  {money(r.amount)}{r.agent ? ` · ${r.agent}` : ''}
                </>} />
              <Section title="В SalesDoc (живой запрос)" rows={data.in_salesdoc}
                render={(r) => <>
                  <b className="sd-doc-id">{r.sd_id}</b>
                  {r.number ? ` · №${r.number}` : ''} · {fdateShort(r.date)} ·{' '}
                  {r.client} · {r.store || '—'} · {r.status_label} ·{' '}
                  {money(r.amount)}{r.agent ? ` · ${r.agent}` : ''}
                </>} />
              {data.nearby_client && (
                <Section
                  title={`Заказы «${data.nearby_client}» рядом с датой 1С`}
                  rows={data.nearby}
                  render={(r) => <>
                    {fdateShort(r.date)} · №{r.number || r.sd_id} · {r.store || '—'} ·{' '}
                    {r.status_label} · {money(r.total)}
                    {r.total_after !== r.total && <> (после скидки {money(r.total_after)})</>}
                    {r.returns > 0 && <> · возвраты {money(r.returns)}</>}
                  </>} />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// «Почему эта точка здесь, а соседняя нет» — вопрос, на который каждый раз
// приходилось отвечать расследованием. Панель разбирает решение по шагам.
function WhyPanel() {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  function load() {
    if (q.trim().length < 2) return
    setLoading(true); setError(null)
    api.salesdocWhy(q.trim())
      .then(setData).catch((e) => setError(e.message)).finally(() => setLoading(false))
  }

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={() => setOpen(!open)}>
        {open ? '▾' : '▸'} 🔎 Почему точка здесь / не здесь
      </button>
      {open && (
        <div className="store-map-body">
          <p className="muted">Введите часть названия точки — портал покажет по
            шагам, почему она попала в список выбранной фирмы или не попала:
            есть ли она в 1С каждой фирмы, какой долг в SalesDoc и на складах
            какой фирмы лежат её реализации.</p>
          <div className="rc-period">
            <input className="filter-select" value={q} placeholder="Глобус"
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && load()} />
            <button className="btn btn-sm" onClick={load} disabled={loading}>
              {loading ? 'Ищу…' : 'Разобрать'}
            </button>
          </div>
          {error && <div className="error">{error}</div>}
          {data && data.clients.length === 0 && (
            <div className="muted">В SalesDoc таких точек не нашлось.</div>
          )}
          {data?.clients.map((c, i) => (
            <div key={i} className="order-raw">
              <div><b>{c.name}</b> <span className="muted">· {c.sd_id}</span></div>
              <div>Долг в SalesDoc: <b>{money(c.sd_debt)}</b></div>
              <div>
                Операции в 1С: {Object.entries(c.in_1c).map(([firm, debt]) => (
                  <span key={firm} className="why-firm">
                    {ORG_LABELS[firm] || firm}:{' '}
                    {debt == null ? <span className="muted">нет</span> : <b>{money(debt)}</b>}
                  </span>
                ))}
              </div>
              {/* Разница между «операций у фирмы нет» и «операции есть, но имя
                  другое» решает, что делать: догружать или связать вручную. */}
              {Object.entries(c.similar || {}).map(([firm, list]) => (
                list.length > 0 && (
                  <div key={firm} className="why-similar">
                    Похожие имена в 1С ({ORG_LABELS[firm] || firm}):{' '}
                    {list.map((s, k) => (
                      <span key={k} className="why-firm">«{s.name}» — {money(s.debt)}</span>
                    ))}
                  </div>
                )
              ))}
              <div>
                Реализации на складах фирм:{' '}
                {c.store_orgs.length
                  ? <b>{c.store_orgs.map((f) => ORG_LABELS[f] || f).join(', ')}</b>
                  : <span className="muted">нет реализаций на складах с привязкой</span>}
                {c.unmapped_stores.length > 0 && (
                  <> · склады без фирмы: <b>{c.unmapped_stores.join(', ')}</b></>
                )}
              </div>
              <div className="why-verdict">{c.verdict}</div>
            </div>
          ))}
        </div>
      )}
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
                      спор решает только сырой ответ getOrder. Прячем его за
                      значком в той же строке: своей строкой он удваивал
                      высоту реестра. */}
                  <button className="store-stat store-stat-link sd-raw-btn"
                    title="Показать сырой ответ SalesDoc"
                    onClick={() => setRaw(raw === r.sd_id ? null : r.sd_id)}>
                    {raw === r.sd_id ? '×' : '{ }'}
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

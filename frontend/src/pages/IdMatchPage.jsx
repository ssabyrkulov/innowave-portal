import { useEffect, useState } from 'react'
import { api } from '../api'
import { formatMoney } from '../utils'

const money = (v) => formatMoney(v)
const qty = (v) => `${Number(v || 0).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} шт`
const fdate = (iso) => (iso ? iso.split('-').reverse().join('.') : '—')

const TABS = [
  ['sales', '📦 Реализации'],
  ['payments', '💵 Оплаты покупателей'],
  ['returns', '↩️ Возвраты'],
  ['purchases', '📥 Поступления'],
  ['writeoffs', '📤 Списания'],
  ['movements', '🔀 Перемещения'],
]

// Метка на каждой операции. Различать «связано по идентификатору» и «сошлось
// по сумме» обязательно: первое — факт, второе — догадка, и выдавать одно за
// другое нельзя, особенно когда по догадке принимают решения.
const VERDICTS = {
  id: { icon: '🔗', label: 'по идентификатору', cls: 'sc-ok',
        hint: 'Документы связаны по ДокументGUID — совпадение точное' },
  guess: { icon: '≈', label: 'по сумме и дате', cls: '',
           hint: 'Идентификатора нет; совпали контрагент, сумма и дата (±3 дня) — вероятно, один документ' },
  diff: { icon: '⚠️', label: 'суммы разные', cls: 'sc-diff',
          hint: 'Идентификатор совпал, а суммы разошлись — документ правили в одной системе' },
  only_1c: { icon: '1С', label: 'только в 1С', cls: 'sc-diff',
             hint: 'В SalesDoc пары не нашлось' },
  only_sd: { icon: 'SD', label: 'только в SalesDoc', cls: 'sc-diff',
             hint: 'В 1С пары не нашлось' },
  // Не «в SalesDoc документа нет», а «мы там не искали»: метода для складских
  // документов в API мы пока не нашли. Путать эти две вещи нельзя.
  no_sd_side: { icon: '❔', label: 'сторона SD не подключена', cls: 'muted',
                hint: 'Портал пока не знает, каким методом SalesDoc отдаёт документы этого вида — сверка не проводилась' },
  no_1c_side: { icon: '❔', label: 'сторона 1С не подключена', cls: 'muted',
                hint: 'Выгрузка этого вида из 1С в портал пока не грузится — сверка не проводилась' },
}
const ORDER = ['diff', 'only_1c', 'only_sd', 'guess', 'id', 'no_sd_side', 'no_1c_side']

export default function IdMatchPage() {
  const [kind, setKind] = useState('sales')
  const [verdict, setVerdict] = useState('')
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let alive = true
    setLoading(true); setError(null)
    api.salesdocIdMatch({ kind, verdict, q: q.trim(), page })
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [kind, verdict, q, page])

  // Смена вкладки или фильтра начинает список сначала — иначе можно остаться
  // на 7-й странице набора, в котором её больше нет.
  function pick(next) { setKind(next); setVerdict(''); setPage(1) }

  const counts = data?.counts || {}
  // У списаний 1С не выгружает сумму — только количество. Показывать там
  // «0 KGS» значит утверждать, что документ на ноль сомов.
  const byQty = data?.measure === 'qty'
  const val = (d) => (byQty ? qty(d.qty) : money(d.amount))
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1

  return (
    <div>
      <div className="page-header">
        <h1>Сверка по ID</h1>
      </div>

      <div className="note-readonly">
        Операции 1С и SalesDoc одним списком. Сначала документы связываются по
        идентификатору (<code>ДокументGUID</code> в 1С, <code>code_1C</code> в
        SalesDoc) — это точное совпадение. Что не связалось, портал пробует
        сопоставить по контрагенту, сумме и дате (±3 дня) и помечает отдельно:
        это догадка, а не факт.
      </div>

      {error && <div className="error">{error}</div>}

      <div className="ops-tabs debt-tabs">
        {TABS.map(([k, label]) => (
          <button key={k} className={`ops-tab ${kind === k ? 'active' : ''}`}
            onClick={() => pick(k)}>
            {label}
          </button>
        ))}
      </div>

      {data && !data.has_sd && (
        <div className="note-readonly sd-warn">
          Сторона SalesDoc здесь не подключена, поэтому строки помечены
          «сторона SD не подключена», а не «только в 1С» — сверка не
          проводилась. Отдельного метода для списаний в API нет:
          <code>getConsumption</code> существует, но отдаёт ноль записей, а
          <code>getMovement</code> оказался перемещениями между складами (см.
          вкладку «Перемещения»).
        </div>
      )}
      {data && data.has_1c === false && (
        <div className="note-readonly sd-warn">
          Сторона 1С здесь не подключена: перемещения выгружаются отдельным
          файлом «Перемещение товаров», а портал его пока не грузит. Строки
          ниже — то, что есть в SalesDoc; метка честно говорит, что сверки не
          было. Как только выгрузка появится, вид станет двусторонним.
        </div>
      )}

      <div className="chart-card debt-filters">
        <div className="debt-filter-row">
          <input className="product-search-input"
            placeholder="Поиск: контрагент, номер документа, идентификатор"
            value={q} onChange={(e) => { setQ(e.target.value); setPage(1) }} />
          <button className={`btn btn-sm ${verdict === '' ? 'btn-primary' : ''}`}
            onClick={() => { setVerdict(''); setPage(1) }}>
            Все {data ? `· ${Object.values(counts).reduce((a, b) => a + b, 0)}` : ''}
          </button>
          {ORDER.filter((v) => counts[v]).map((v) => (
            <button key={v} title={VERDICTS[v].hint}
              className={`btn btn-sm ${verdict === v ? 'btn-primary' : ''}`}
              onClick={() => { setVerdict(v); setPage(1) }}>
              {VERDICTS[v].icon} {VERDICTS[v].label} · {counts[v]}
            </button>
          ))}
        </div>
      </div>

      <div className="table-wrap rc-table sc-table">
        <table>
          <thead>
            <tr>
              <th>Метка</th>
              <th>Дата</th>
              <th>Контрагент</th>
              <th>1С</th>
              <th className="num">{byQty ? 'Кол-во 1С' : 'Сумма 1С'}</th>
              <th>SalesDoc</th>
              <th className="num">{byQty ? 'Кол-во SD' : 'Сумма SD'}</th>
              <th className="num">Δ</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr><td colSpan={8} className="muted center">Считаю…</td></tr>
            )}
            {!loading && data && data.rows.length === 0 && (
              <tr><td colSpan={8} className="muted center">Ничего не найдено</td></tr>
            )}
            {!loading && data && data.rows.map((r, i) => {
              const v = VERDICTS[r.verdict]
              const o = r.ours
              const t = r.theirs
              return (
                <tr key={i}>
                  <td title={v.hint}>
                    <span className={v.cls}>{v.icon} {v.label}</span>
                  </td>
                  <td>{fdate((o || t).date)}</td>
                  <td>{(o || t).client}</td>
                  <td>
                    {o ? (o.number || '—') : <span className="muted">нет</span>}
                    {o?.lines > 1 && <div className="rc-note">{o.lines} позиций</div>}
                    {o?.note && <div className="rc-note">{o.note}</div>}
                    {o?.guid && <div className="rc-note sd-doc-id">{o.guid}</div>}
                  </td>
                  <td className="num">{o ? val(o) : '—'}</td>
                  <td>
                    {t ? (t.number || '—') : <span className="muted">нет</span>}
                    {t?.status && <div className="rc-note">{t.status}</div>}
                    {t?.guid && <div className="rc-note sd-doc-id">{t.guid}</div>}
                  </td>
                  <td className="num">{t ? val(t) : '—'}</td>
                  <td className={`num ${Math.abs(r.delta) > 0.5 ? 'sc-diff' : ''}`}>
                    {r.delta ? money(r.delta) : '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {data && pages > 1 && (
        <div className="rc-period">
          <button className="btn btn-sm" disabled={page <= 1}
            onClick={() => setPage(page - 1)}>← назад</button>
          <span className="muted">
            Страница {page} из {pages} · всего {data.total}
          </span>
          <button className="btn btn-sm" disabled={page >= pages}
            onClick={() => setPage(page + 1)}>вперёд →</button>
        </div>
      )}
    </div>
  )
}

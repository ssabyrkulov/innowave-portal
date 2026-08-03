import { useEffect, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'
import { formatMoney } from '../utils'

const money = (v) => `${formatMoney(v)} KGS`

// Налоговый контур — черновик. Файлы налоговой базы (1С ред. 1.7) грузятся
// вручную и живут в отдельной таблице: с управленческими цифрами портала они
// не пересекаются нигде. Когда Эрмек доведёт выгрузку (метка НАЛ, банк,
// остатки), загрузка станет автоматической через те же папки Drive.
// Реестр операций: каждый документ строкой. Для реализаций позиции файла
// собраны в документы (номер + дата + контрагент), остальные виды — одна
// строка = одна операция.
function DocsRegistry() {
  const [kind, setKind] = useState('sale')
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    setData(null); setError(null)
    api.taxDocs(kind)
      .then((d) => alive && setData(d)).catch((e) => alive && setError(e.message))
    return () => { alive = false }
  }, [kind])

  const TABS = [
    ['sale', 'Реализации'], ['return', 'Возвраты'],
    ['cash_in', 'Касса · приход'], ['cash_out', 'Касса · расход'],
  ]
  return (
    <div className="chart-card">
      <div className="rc-col-title">Реестр операций</div>
      <div className="sc-tabs">
        {TABS.map(([k, label]) => (
          <button key={k}
            className={`btn btn-sm ${kind === k ? 'btn-primary' : 'btn-ghost'}`}
            onClick={() => setKind(k)}>
            {label}
          </button>
        ))}
      </div>
      {error && <div className="error">{error}</div>}
      {!data && !error && <div className="muted">Загрузка…</div>}
      {data && (
        <>
          <p className="muted">
            {data.label}: <b>{data.count}</b> операций на <b>{money(data.amount)}</b>
            {' '}· пара в управленке у <b>{data.matched}</b>
            {data.unmatched > 0 && (
              <> · <span className="sc-bad">без пары {data.unmatched} на {money(data.unmatched_amount)}</span></>
            )}
          </p>
          {data.items.length === 0 ? (
            <div className="muted">Операций нет.</div>
          ) : (
            <div className="table-wrap rc-table">
              <table>
                <thead>
                  <tr>
                    <th>Дата</th><th>№</th><th>Контрагент</th>
                    {kind === 'sale'
                      ? <><th>Склад</th><th className="num">Позиций</th></>
                      : <th>Вид операции</th>}
                    <th className="num">Сумма</th>
                    <th>Управленка</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((r, i) => (
                    <tr key={i}>
                      <td>{r.date.split('-').reverse().join('.')}</td>
                      <td>{r.doc_number || <span className="muted">—</span>}</td>
                      <td>{r.counterparty || <span className="muted">—</span>}</td>
                      {kind === 'sale'
                        ? <><td>{r.warehouse || '—'}</td>
                            <td className="num">{r.positions}</td></>
                        : <td>{r.operation || '—'}</td>}
                      <td className="num">
                        {formatMoney(r.amount)} {r.currency || 'KGS'}
                      </td>
                      {/* Пара найдена по сумме и близкой дате: имя контрагента
                          в контурах разное, показываем его как подсказку. */}
                      <td>
                        {r.upr ? (
                          <span className="sc-ok" title={r.upr.who || ''}>
                            ✓ {r.upr.date.split('-').reverse().join('.')}
                            {r.upr.days > 0 && ` (±${r.upr.days} дн.)`}
                            {r.upr.who && (
                              <span className="rc-note">{r.upr.who}</span>
                            )}
                          </span>
                        ) : (
                          <span className="sc-bad">нет пары</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {data.count > data.cap && (
            <p className="muted">Показаны первые {data.cap} из {data.count}.</p>
          )}
        </>
      )}
    </div>
  )
}

function CompareTable({ title, data, note }) {
  const [open, setOpen] = useState(true)
  const t = data.totals
  const cls = (v) => (v == null ? '' : v < 0 ? 'neg' : v > 0 ? 'pos' : '')
  return (
    <div className="chart-card">
      <button className="btn btn-ghost store-map-toggle" onClick={() => setOpen(!open)}>
        {open ? '▾' : '▸'} {title}
      </button>
      {open && (
        <div className="table-wrap rc-table sc-table">
          <table>
            <thead>
              <tr>
                <th>Месяц</th>
                <th className="num">Управленка</th>
                <th className="num">Налоговая</th>
                <th className="num">SalesDoc</th>
                <th className="num" title="Доля официально проведённого от управленческого оборота">НАЛ/УПР</th>
                <th className="num" title="SalesDoc минус управленка">Δ SD</th>
              </tr>
            </thead>
            <tbody>
              <tr className="sc-total-row">
                <td><b>Итого</b></td>
                <td className="num"><b>{money(t.upr)}</b></td>
                <td className="num"><b>{money(t.nal)}</b></td>
                <td className="num"><b>{money(t.sd)}</b></td>
                <td className="num"><b>{t.nal_share == null ? '—' : `${t.nal_share}%`}</b></td>
                <td className={`num ${cls(t.sd_diff)}`}><b>{money(t.sd_diff)}</b></td>
              </tr>
              {data.rows.map((r) => (
                <tr key={r.month}>
                  <td>{r.month.split('-').reverse().join('.')}</td>
                  <td className="num">{money(r.upr)}</td>
                  <td className="num">{money(r.nal)}</td>
                  <td className="num">{money(r.sd)}</td>
                  <td className="num">{r.nal_share == null ? '—' : `${r.nal_share}%`}</td>
                  <td className={`num ${cls(r.sd_diff)}`}>{money(r.sd_diff)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {note && <p className="muted">{note}</p>}
        </div>
      )}
    </div>
  )
}

export default function TaxPage() {
  const { can } = useAuth()
  const [data, setData] = useState(null)
  const [cmp, setCmp] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [log, setLog] = useState([])

  function load() {
    api.taxSummary().then(setData).catch((e) => setError(e.message))
    api.taxCompare().then(setCmp).catch(() => {})
  }
  useEffect(load, [])

  async function onFiles(e) {
    const files = [...(e.target.files || [])]
    e.target.value = ''
    if (!files.length) return
    setBusy(true)
    for (const f of files) {
      try {
        const r = await api.taxImport(f, 'hygiene')
        setLog((l) => [`✅ ${f.name}: ${r.kind}, строк ${r.added}`, ...l])
      } catch (err) {
        setLog((l) => [`❌ ${f.name}: ${err.message}`, ...l])
      }
    }
    setBusy(false)
    load()
  }

  const kinds = data?.kinds || []
  return (
    <div>
      <div className="page-header">
        <h1>Налоговая · черновик</h1>
      </div>
      <p className="muted">Данные налоговой базы (1С ред. 1.7). Хранятся
        отдельно и с управленческими цифрами портала не смешиваются. Загрузка
        каждого файла заменяет данные своего вида целиком — файлы выгружаются
        за всю историю, дублей не бывает.</p>

      {can.editPayments && (
        <div className="chart-card">
          <label className="btn btn-primary btn-sm">
            {busy ? 'Загружаю…' : '⬆️ Загрузить файлы налоговой базы'}
            <input type="file" multiple accept=".xlsx,.xlsm" hidden
              disabled={busy} onChange={onFiles} />
          </label>
          <span className="muted" style={{ marginLeft: 12 }}>
            Реализация, возвраты, ПКО, РКО — тип определяется по колонкам.
          </span>
          {log.length > 0 && (
            <ul className="order-raw-sib" style={{ marginTop: 8 }}>
              {log.slice(0, 6).map((l, i) => <li key={i}>{l}</li>)}
            </ul>
          )}
        </div>
      )}

      {error && <div className="error">{error}</div>}
      {data && kinds.length === 0 && (
        <div className="chart-card muted">Данных пока нет — загрузите файлы.</div>
      )}

      {/* Трёхсторонняя сверка: поклиентно контуры не сопоставить (в налоговой
          продажи проведены на юрлица), поэтому сверяем помесячные агрегаты.
          НАЛ/УПР — доля официально проведённого оборота, Δ SD — расхождение
          SalesDoc с управленкой. */}
      {data && kinds.length > 0 && <DocsRegistry />}

      {cmp && cmp.sales.rows.length > 0 && (
        <>
          <CompareTable title="Выручка по месяцам: Управленка · Налоговая · SalesDoc"
            data={cmp.sales} />
          <CompareTable title="Поступления от покупателей по месяцам"
            data={cmp.money}
            note="Оплаты SalesDoc показаны все: аванс без привязки к заказам по фирмам не делится." />
        </>
      )}

      {kinds.length > 0 && (
        <>
          <div className="summary-bar">
            {kinds.map((k) => (
              <div className="summary-card" key={k.kind}>
                <span className="summary-label">{k.label}</span>
                <span className="summary-value">{money(k.amount)}</span>
                <span className="muted">{k.count} строк · до {k.last_date}</span>
              </div>
            ))}
          </div>

          <div className="rc-cols">
            <div className="rc-col">
              <div className="chart-card">
                <div className="rc-col-title">Выручка по годам</div>
                <table className="table rc-table">
                  <tbody>
                    {data.sales_by_year.map((y) => (
                      <tr key={y.year}>
                        <td>{y.year}</td>
                        <td className="num">{money(y.amount)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="chart-card">
                <div className="rc-col-title">Топ клиентов</div>
                <table className="table rc-table">
                  <tbody>
                    {data.top_clients.map((c, i) => (
                      <tr key={i}>
                        <td>{c.client}</td>
                        <td className="num">{money(c.amount)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
            <div className="rc-col">
              <div className="chart-card">
                <div className="rc-col-title">Касса по видам операций</div>
                <table className="table rc-table">
                  <tbody>
                    {data.cash_by_operation.map((c, i) => (
                      <tr key={i}>
                        <td>{c.direction === 'cash_in' ? '↓' : '↑'} {c.operation}</td>
                        <td className="num">{money(c.amount)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {data.podotchet.length > 0 && (
                <div className="chart-card">
                  <div className="rc-col-title">Подотчёт по людям</div>
                  <table className="table rc-table">
                    <thead>
                      <tr><th>Сотрудник</th><th className="num">Выдано</th>
                        <th className="num">Вернул</th><th className="num">Висит</th></tr>
                    </thead>
                    <tbody>
                      {data.podotchet.map((p, i) => (
                        <tr key={i}>
                          <td>{p.person}</td>
                          <td className="num">{money(p.issued)}</td>
                          <td className="num">{money(p.returned)}</td>
                          <td className="num"><b>{money(p.hanging)}</b></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <p className="muted">«Висит» = выдано − возвращено. Авансовые
                    отчёты (на что потрачено) в выгрузке пока нет — когда Эрмек
                    добавит, колонка станет честным остатком долга.</p>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  )
}

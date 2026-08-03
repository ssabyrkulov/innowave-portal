import { useEffect, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'
import { formatMoney } from '../utils'

const money = (v) => `${formatMoney(v)} KGS`

// Налоговый контур — черновик. Файлы налоговой базы (1С ред. 1.7) грузятся
// вручную и живут в отдельной таблице: с управленческими цифрами портала они
// не пересекаются нигде. Когда Эрмек доведёт выгрузку (метка НАЛ, банк,
// остатки), загрузка станет автоматической через те же папки Drive.
export default function TaxPage() {
  const { can } = useAuth()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [log, setLog] = useState([])

  function load() {
    api.taxSummary().then(setData).catch((e) => setError(e.message))
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

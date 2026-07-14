import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { formatMoney } from '../utils'

const fmtQty = (v) => Number(v || 0).toLocaleString('ru-RU', { maximumFractionDigits: 0 })

export default function StockPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [q, setQ] = useState('')
  const [wh, setWh] = useState('')

  useEffect(() => {
    api.stockBalances().then(setData).catch((e) => setError(e.message))
  }, [])

  const warehouses = useMemo(
    () => [...new Set((data?.items || []).map((i) => i.warehouse).filter(Boolean))].sort(),
    [data]
  )

  const rows = useMemo(() => {
    let r = data?.items || []
    if (q.trim()) {
      const s = q.trim().toLowerCase()
      r = r.filter((i) => (i.product || '').toLowerCase().includes(s))
    }
    if (wh) r = r.filter((i) => i.warehouse === wh)
    return [...r].sort((a, b) => b.amount - a.amount)
  }, [data, q, wh])

  const shown = useMemo(
    () => rows.reduce((a, r) => ({ amount: a.amount + r.amount, qty: a.qty + r.qty }), { amount: 0, qty: 0 }),
    [rows]
  )

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="center muted">Загрузка…</div>

  const negatives = (data.items || []).filter((i) => i.qty < 0 || i.amount < 0).length
  const empty = data.items.length === 0

  return (
    <div>
      <div className="page-header">
        <h1>Остатки на складах</h1>
        <span className="muted">
          {data.updated_at
            ? `обновлено ${new Date(data.updated_at + 'Z').toLocaleString('ru-RU')} · из 1С`
            : 'из 1С'}
        </span>
      </div>

      {empty ? (
        <div className="table-wrap">
          <div className="center muted">
            Остатков пока нет — загрузите из 1С выгрузку остатков (ВыгрузкаОст).
          </div>
        </div>
      ) : (
        <>
          <div className="summary-bar">
            <div className="summary-card summary-in">
              <span className="summary-label">Стоимость остатков</span>
              <span className="summary-value">{formatMoney(data.total_amount)}</span>
            </div>
            <div className="summary-card">
              <span className="summary-label">Всего штук</span>
              <span className="summary-value">{fmtQty(data.total_qty)}</span>
            </div>
            <div className="summary-card">
              <span className="summary-label">Позиций</span>
              <span className="summary-value">{data.items.length}</span>
            </div>
            {negatives > 0 && (
              <div className="summary-card summary-out">
                <span className="summary-label">Отрицательных</span>
                <span className="summary-value">{negatives}</span>
                <span className="dash-delta muted">проверьте склад</span>
              </div>
            )}
          </div>

          <div className="filters">
            <input
              className="product-search-input"
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Поиск по товару…"
            />
            <select className="filter-select" value={wh} onChange={(e) => setWh(e.target.value)}>
              <option value="">Все склады</option>
              {warehouses.map((w) => (
                <option key={w} value={w}>{w}</option>
              ))}
            </select>
            {(q || wh) && (
              <button className="btn btn-ghost" onClick={() => { setQ(''); setWh('') }}>
                Сбросить
              </button>
            )}
          </div>

          <div className="table-wrap compact">
            <table>
              <thead>
                <tr>
                  <th>Товар</th>
                  <th className="hide-mobile">Склад</th>
                  <th className="num">Кол-во</th>
                  <th className="num">Сумма</th>
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 && (
                  <tr><td colSpan={4} className="muted center">Ничего не найдено</td></tr>
                )}
                {rows.map((r, i) => {
                  const neg = r.qty < 0 || r.amount < 0
                  return (
                    <tr key={`${r.product}|${r.warehouse}|${i}`} className={neg ? 'row-neg' : ''}>
                      <td>{r.product}</td>
                      <td className="hide-mobile muted">{r.warehouse || '—'}</td>
                      <td className={`num ${neg ? 'neg' : ''}`}>{fmtQty(r.qty)}</td>
                      <td className={`num ${neg ? 'neg' : ''}`}>{formatMoney(r.amount, false)}</td>
                    </tr>
                  )
                })}
              </tbody>
              <tfoot>
                <tr>
                  <td>Итого{q || wh ? ' (по фильтру)' : ''}</td>
                  <td className="hide-mobile"></td>
                  <td className="num">{fmtQty(shown.qty)}</td>
                  <td className="num">{formatMoney(shown.amount, false)}</td>
                </tr>
              </tfoot>
            </table>
          </div>
        </>
      )}
    </div>
  )
}

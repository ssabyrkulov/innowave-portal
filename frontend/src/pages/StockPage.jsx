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

      <PurchasesPanel />
    </div>
  )
}

// Закупки (поступления товаров, ВыгрузкаПост): по поставщикам и годам.
// Первый шаг товарного контура — дальше на этих данных строятся кредиторка
// и товарный баланс «приход − расход = остаток».
const SORTS = {
  date_desc: { label: 'Дата ↓ (новые)', fn: (a, b) => b.date.localeCompare(a.date) },
  date_asc: { label: 'Дата ↑ (старые)', fn: (a, b) => a.date.localeCompare(b.date) },
  amount_desc: { label: 'Сумма ↓', fn: (a, b) => b.amount_kgs - a.amount_kgs },
  qty_desc: { label: 'Количество ↓', fn: (a, b) => (b.qty || 0) - (a.qty || 0) },
  price_desc: { label: 'Цена ↓', fn: (a, b) => (b.price || 0) - (a.price || 0) },
  supplier: { label: 'Поставщик А-Я', fn: (a, b) => (a.supplier || '').localeCompare(b.supplier || '') },
  product: { label: 'Номенклатура А-Я', fn: (a, b) => (a.product || '').localeCompare(b.product || '') },
}

function PurchasesPanel() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [lines, setLines] = useState(null)
  const [error, setError] = useState(null)
  const [q, setQ] = useState('')
  const [sort, setSort] = useState('date_desc')

  function load() {
    api.purchasesSummary().then(setData).catch((e) => setError(e.message))
    api.purchasesLines().then(setLines).catch((e) => setError(e.message))
  }
  function toggle() { const n = !open; setOpen(n); if (n && data === null) load() }

  // Фильтр ищет по номенклатуре, поставщику, складу и номеру одновременно.
  const visible = useMemo(() => {
    let items = lines?.items || []
    const s = q.trim().toLowerCase()
    if (s) {
      items = items.filter((r) =>
        [r.product, r.supplier, r.warehouse, r.doc_number]
          .some((v) => (v || '').toLowerCase().includes(s)))
    }
    return [...items].sort(SORTS[sort].fn)
  }, [lines, q, sort])

  const visTotal = useMemo(
    () => visible.reduce((a, r) => ({ amount: a.amount + r.amount_kgs, qty: a.qty + (r.qty || 0) }),
      { amount: 0, qty: 0 }),
    [visible]
  )

  return (
    <div className="chart-card store-map">
      <button className="btn btn-ghost store-map-toggle" onClick={toggle}>
        {open ? '▾' : '▸'} 📥 Закупки (поступления товаров)
      </button>
      {open && (
        <div className="store-map-body">
          {error && <div className="error">{error}</div>}
          {data === null && !error && <div className="muted">Загрузка…</div>}
          {data && data.rows_total === 0 && (
            <div className="muted">Закупок пока нет — файл ВыгрузкаПост
              подтянется автосинком.</div>
          )}
          {data && data.rows_total > 0 && (
            <>
              <p>
                Документов: <b>{data.docs_total}</b> · строк {data.rows_total} ·
                на <b>{formatMoney(data.amount_kgs)} KGS</b>
                {' '}· по годам:{' '}
                {data.by_year.map((y) => `${y.year}: ${formatMoney(y.amount_kgs)}`).join(' · ')}
              </p>
              <div className="table-wrap rc-table">
                <table>
                  <thead>
                    <tr>
                      <th>Поставщик</th><th className="num">Док.</th>
                      <th>Валюта</th><th>Последняя</th>
                      <th className="num">Сумма, KGS</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.suppliers.map((s, i) => (
                      <tr key={i}>
                        <td>{s.supplier}</td>
                        <td className="num">{s.docs}</td>
                        <td>{s.currencies.join(', ')}</td>
                        <td>{(s.last_date || '').split('-').reverse().join('.')}</td>
                        <td className="num">{formatMoney(s.amount_kgs)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* --- Построчная детализация: как в файле, без группировок --- */}
              {lines && (
                <>
                  <div className="rc-col-title" style={{ marginTop: 14 }}>
                    Детализация по строкам
                  </div>
                  <div className="rc-period">
                    <input className="filter-select" value={q}
                      placeholder="поиск: товар / поставщик / склад / №"
                      onChange={(e) => setQ(e.target.value)} />
                    <select className="filter-select" value={sort}
                      onChange={(e) => setSort(e.target.value)}>
                      {Object.entries(SORTS).map(([k, v]) => (
                        <option key={k} value={k}>{v.label}</option>
                      ))}
                    </select>
                    <span className="muted">
                      {visible.length} строк · {fmtQty(visTotal.qty)} шт ·{' '}
                      {formatMoney(visTotal.amount)} KGS
                    </span>
                  </div>
                  <div className="table-wrap rc-table sc-table">
                    <table>
                      <thead>
                        <tr>
                          <th>Дата</th><th>№</th><th>Поставщик</th><th>Склад</th>
                          <th>Номенклатура</th>
                          <th className="num">Кол-во</th><th>Ед.</th>
                          <th className="num">Цена</th><th>Вал.</th>
                          <th className="num">Сумма, KGS</th>
                          <th className="num">Итог дока</th>
                        </tr>
                      </thead>
                      <tbody>
                        {visible.map((r, i) => (
                          <tr key={i}>
                            <td>{r.date.split('-').reverse().join('.')}</td>
                            <td>{r.doc_number || '—'}</td>
                            <td>{r.supplier}</td>
                            <td>{r.warehouse || '—'}</td>
                            <td>{r.product || '—'}</td>
                            <td className="num">{r.qty == null ? '—' : fmtQty(r.qty)}</td>
                            <td>{r.unit || '—'}</td>
                            <td className="num">{r.price == null ? '—' : r.price}</td>
                            <td>{r.currency}</td>
                            <td className="num">{formatMoney(r.amount_kgs)}</td>
                            <td className="num">
                              {r.doc_total == null ? '—' : `${formatMoney(r.doc_total)} ${r.currency}`}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {lines.total > lines.cap && (
                    <p className="muted">Показаны первые {lines.cap} из {lines.total} строк.</p>
                  )}
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

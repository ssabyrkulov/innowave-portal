import { useEffect, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'
import { formatMoney } from '../utils'

const STALE_DAYS = 30

function daysSince(iso) {
  if (!iso) return Infinity
  return Math.floor((Date.now() - new Date(iso).getTime()) / 86400000)
}

function fmtDate(iso) {
  return iso ? iso.split('-').reverse().join('.') : '—'
}

// Дней без оплат: от последней оплаты, а если её не было — от последней
// отгрузки (долг живёт с момента отгрузки). null — данных нет.
function daysNoPay(c) {
  const ref = c.last_payment || c.last_shipment
  return ref ? daysSince(ref) : null
}

// Метка в активной таблице: только число дней без оплат, без слов.
function noPayText(c) {
  const d = daysNoPay(c)
  return d != null ? `${d} дн.` : '—'
}

// Категории пометок контрагента. Помеченные уходят из активной дебиторки в
// свою вкладку. Легко добавить новую — достаточно дописать сюда.
const CATEGORIES = [
  {
    kind: 'bad_debt', label: 'Безнадёжные', icon: '⛔', verb: 'В безнадёжные',
    hint: 'Безнадёжные контрагенты убираются из активной дебиторки и не учитываются в долге к взысканию.',
  },
  {
    kind: 'disputed', label: 'Под вопросом', icon: '❓', verb: 'В «под вопросом»',
    hint: 'Спорные: была оплата или нет — неясно (старые агенты уже не работают). Убираются из активной дебиторки до выяснения.',
  },
]
const CAT_BY_KIND = Object.fromEntries(CATEGORIES.map((c) => [c.kind, c]))

export default function DebtPage() {
  const { can } = useAuth()
  const [data, setData] = useState(null)
  const [receipts, setReceipts] = useState([])
  const [showReceipts, setShowReceipts] = useState(false)
  const [error, setError] = useState(null)
  const [aliasDraft, setAliasDraft] = useState({})
  const [tab, setTab] = useState('active') // active | bad
  const [detailClient, setDetailClient] = useState(null)
  const [pickClient, setPickClient] = useState('')
  const [pickNote, setPickNote] = useState('')

  async function load() {
    setError(null)
    try {
      setData(await api.receivables())
      if (showReceipts) setReceipts(await api.listReceipts())
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showReceipts])

  async function linkAlias(payer) {
    const client = aliasDraft[payer]
    if (!client) return
    try {
      await api.createAlias(payer, client)
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function saveRate(r, value) {
    const rate = Number(value)
    if (!rate || rate <= 0 || rate === r.rate) return
    try {
      await api.setReceiptRate(r.id, rate)
      setReceipts(await api.listReceipts())
      setData(await api.receivables())
    } catch (err) {
      setError(err.message)
    }
  }

  async function markFlag(client, kind, note) {
    if (!client) return
    try {
      await api.setFlag(client, kind, note || null)
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function addToCategory(kind) {
    const client = pickClient.trim()
    if (!client) return
    await markFlag(client, kind, pickNote.trim())
    setPickClient('')
    setPickNote('')
  }

  async function markInline(c, kind) {
    const cat = CAT_BY_KIND[kind]
    const ok = confirm(
      `Пометить «${c.client}» — ${cat.label.toLowerCase()}? Клиент уйдёт из ` +
        `активной дебиторки (долг ${formatMoney(c.debt)}). Вернуть можно во ` +
        `вкладке «${cat.label}».`
    )
    if (ok) await markFlag(c.client, kind, null)
  }

  async function removeFlag(client) {
    try {
      await api.removeFlag(client)
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  const flags = data?.flags || {}
  const flaggedSet = new Set(Object.keys(flags))
  const allDebtors = data ? data.clients.filter((c) => c.debt > 0.01) : []
  const debtors = allDebtors.filter((c) => !flaggedSet.has(c.client))
  // Помеченные показываем даже с нулевым/погашенным долгом — это ручной список.
  const clientsOf = (kind) =>
    data ? data.clients.filter((c) => flags[c.client] === kind) : []
  const activeDebt = debtors.reduce((s, c) => s + c.debt, 0)

  return (
    <div>
      <div className="page-header">
        <h1>Дебиторка</h1>
      </div>

      <div className="note-readonly">
        Учитываются только безналичные оплаты (выгрузка «Поступление денежных
        средств»). Наличные платежи пока не загружаются — реальные долги могут
        быть ниже показанных.
      </div>

      {error && <div className="error">{error}</div>}

      {data && !data.has_receipts ? (
        <div className="table-wrap">
          <div className="center muted">
            Оплаты ещё не загружены. Они подгружаются автоматически из 1С
            (выгрузка «Поступление денежных средств» синхронизируется из Google Drive).
          </div>
        </div>
      ) : data ? (
        <>
          <div className="summary-bar">
            <div className="summary-card">
              <span className="summary-label">Отгружено</span>
              <span className="summary-value">{formatMoney(data.total_shipped)}</span>
            </div>
            {data.total_returned > 0 && (
              <div className="summary-card">
                <span className="summary-label">Возвраты</span>
                <span className="summary-value">−{formatMoney(data.total_returned)}</span>
              </div>
            )}
            <div className="summary-card summary-in">
              <span className="summary-label">Оплачено</span>
              <span className="summary-value">{formatMoney(data.total_paid)}</span>
            </div>
            <div className="summary-card summary-out">
              <span className="summary-label">Долг{flaggedSet.size > 0 ? ' (к взысканию)' : ''}</span>
              <span className="summary-value">{formatMoney(activeDebt)}</span>
            </div>
            <div className="summary-card">
              <span className="summary-label">Должников</span>
              <span className="summary-value">{debtors.length}</span>
            </div>
            {CATEGORIES.map((cat) => {
              const cl = clientsOf(cat.kind)
              if (cl.length === 0) return null
              const tot = cl.reduce((s, c) => s + c.debt, 0)
              return (
                <div key={cat.kind} className="summary-card summary-bad">
                  <span className="summary-label">{cat.label}</span>
                  <span className="summary-value">
                    {formatMoney(tot)}
                    <span className="muted"> · {cl.length}</span>
                  </span>
                </div>
              )
            })}
          </div>

          <div className="ops-tabs debt-tabs">
            <button
              className={`ops-tab ${tab === 'active' ? 'active' : ''}`}
              onClick={() => setTab('active')}
            >
              Активные ({debtors.length})
            </button>
            {CATEGORIES.map((cat) => (
              <button
                key={cat.kind}
                className={`ops-tab ${tab === cat.kind ? 'active' : ''}`}
                onClick={() => { setTab(cat.kind); setPickClient(''); setPickNote('') }}
              >
                {cat.icon} {cat.label} ({clientsOf(cat.kind).length})
              </button>
            ))}
          </div>

          {tab === 'active' && data.unmatched.length > 0 && can.editPayments && (
            <div className="chart-card">
              <h2 className="chart-title">
                ⚠ Плательщики, не найденные среди клиентов ({data.unmatched.length})
              </h2>
              <p className="muted unmatched-hint">
                Оплаты этих плательщиков не попадают в расчёт долгов, пока вы не
                укажете, какой это клиент (обычно — другое написание имени).
              </p>
              <div className="unmatched-list">
                {data.unmatched.map((u) => (
                  <div key={u.payer} className="unmatched-row">
                    <div className="unmatched-name">
                      {u.payer}
                      <span className="muted"> · {formatMoney(u.paid)} ({u.count} опл.)</span>
                    </div>
                    <input
                      list="sales-clients"
                      placeholder="Выберите клиента из продаж…"
                      value={aliasDraft[u.payer] || ''}
                      onChange={(e) =>
                        setAliasDraft((d) => ({ ...d, [u.payer]: e.target.value }))
                      }
                    />
                    <button
                      className="btn btn-sm btn-primary"
                      disabled={!data.sales_clients.includes(aliasDraft[u.payer])}
                      onClick={() => linkAlias(u.payer)}
                    >
                      Связать
                    </button>
                  </div>
                ))}
              </div>
              <datalist id="sales-clients">
                {data.sales_clients.map((c) => (
                  <option key={c} value={c} />
                ))}
              </datalist>
            </div>
          )}

          {tab === 'active' && (
          <div className="table-wrap compact">
            <table>
              <thead>
                <tr>
                  <th>Клиент</th>
                  <th className="num hide-mobile">Отгружено</th>
                  <th className="num hide-mobile">Возвраты</th>
                  <th className="num">Оплачено</th>
                  <th className="num">Долг</th>
                  <th className="hide-mobile">Посл. отгрузка</th>
                  <th className="hide-mobile">Посл. оплата</th>
                  <th className="hide-mobile"></th>
                  {can.editPayments && <th className="debt-act-col">Пометить</th>}
                </tr>
              </thead>
              <tbody>
                {debtors.length === 0 && (
                  <tr>
                    <td colSpan={can.editPayments ? 9 : 8} className="muted center">
                      Долгов нет 🎉
                    </td>
                  </tr>
                )}
                {debtors.map((c) => {
                  const stale = daysSince(c.last_payment) > STALE_DAYS
                  return (
                    <tr key={c.client}>
                      <td>
                        <button className="client-link" onClick={() => setDetailClient(c.client)}>
                          {c.client}
                        </button>
                        {/* на телефоне метку «давно не платил» показываем прямо под именем */}
                        {stale && (
                          <span className="badge badge-overdue debt-stale-inline" title={`Оплат не было больше ${STALE_DAYS} дней`}>
                            {noPayText(c)} без оплат
                          </span>
                        )}
                      </td>
                      <td className="num hide-mobile">{formatMoney(c.shipped)}</td>
                      <td className="num muted hide-mobile">
                        {c.returned > 0 ? `−${formatMoney(c.returned)}` : '—'}
                      </td>
                      <td className="num pos">{formatMoney(c.paid)}</td>
                      <td className="num neg">{formatMoney(c.debt)}</td>
                      <td className="hide-mobile">{fmtDate(c.last_shipment)}</td>
                      <td className="hide-mobile">{fmtDate(c.last_payment)}</td>
                      <td className="hide-mobile">
                        {stale && (
                          <span className="badge badge-overdue" title={`Оплат не было больше ${STALE_DAYS} дней`}>
                            {noPayText(c)}
                          </span>
                        )}
                      </td>
                      {can.editPayments && (
                        <td className="debt-act-col">
                          {CATEGORIES.map((cat) => (
                            <button
                              key={cat.kind}
                              className="btn btn-sm debt-mark-bad"
                              title={`${cat.label} — убрать из активной дебиторки`}
                              onClick={() => markInline(c, cat.kind)}
                            >
                              {cat.icon}
                            </button>
                          ))}
                        </td>
                      )}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          )}

          {CAT_BY_KIND[tab] && (
            <CategoryTab
              cat={CAT_BY_KIND[tab]}
              list={clientsOf(tab)}
              allDebtors={allDebtors}
              notes={data.flag_notes || {}}
              canEdit={can.editPayments}
              pickClient={pickClient}
              setPickClient={setPickClient}
              pickNote={pickNote}
              setPickNote={setPickNote}
              onAdd={() => addToCategory(tab)}
              onRemove={removeFlag}
              onOpen={setDetailClient}
            />
          )}

          <h2 className="section-title">
            <button
              className="btn btn-ghost"
              onClick={() => setShowReceipts((s) => !s)}
            >
              {showReceipts ? '▾' : '▸'} Все поступления
            </button>
          </h2>
          {showReceipts && (
            <div className="table-wrap cards">
              <table>
                <thead>
                  <tr>
                    <th>Дата</th>
                    <th>Плательщик</th>
                    <th>Операция</th>
                    <th className="num">Сумма</th>
                    <th>Валюта</th>
                    <th className="num">Курс</th>
                    <th className="num">В сомах</th>
                  </tr>
                </thead>
                <tbody>
                  {receipts.map((r) => (
                    <tr key={r.id}>
                      <td data-label="Дата">{fmtDate(r.date)}</td>
                      <td data-label="Плательщик">{r.payer}</td>
                      <td className="muted" data-label="Операция">{r.operation}</td>
                      <td className="num" data-label="Сумма">
                        {Number(r.amount).toLocaleString('ru-RU')}
                      </td>
                      <td data-label="Валюта">
                        {r.currency !== 'KGS' ? <b>{r.currency}</b> : r.currency}
                      </td>
                      <td className="num" data-label="Курс">
                        {r.currency !== 'KGS' && can.editPayments ? (
                          <input
                            className="rate-input"
                            type="number"
                            step="0.01"
                            defaultValue={r.rate}
                            onBlur={(e) => saveRate(r, e.target.value)}
                            title="Курс к сому — изменится после выхода из поля"
                          />
                        ) : (
                          r.rate
                        )}
                      </td>
                      <td className="num" data-label="В сомах">{formatMoney(r.amount_kgs)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      ) : (
        <div className="center muted">Загрузка…</div>
      )}

      {detailClient && (
        <ClientDetailModal client={detailClient} onClose={() => setDetailClient(null)} />
      )}
    </div>
  )
}

function CategoryTab({
  cat, list, allDebtors, notes, canEdit,
  pickClient, setPickClient, pickNote, setPickNote, onAdd, onRemove, onOpen,
}) {
  return (
    <div className="bad-debt">
      {canEdit && (
        <div className="chart-card bad-add">
          <div className="bad-add-row">
            <input
              list="cat-debtors"
              className="product-search-input"
              placeholder="Выберите контрагента…"
              value={pickClient}
              onChange={(e) => setPickClient(e.target.value)}
            />
            <input
              className="product-search-input"
              placeholder="Причина / комментарий (необязательно)"
              value={pickNote}
              onChange={(e) => setPickNote(e.target.value)}
            />
            <button className="btn btn-primary" disabled={!pickClient.trim()} onClick={onAdd}>
              {cat.verb}
            </button>
          </div>
          <datalist id="cat-debtors">
            {allDebtors.map((c) => (
              <option key={c.client} value={c.client}>{`долг ${formatMoney(c.debt)}`}</option>
            ))}
          </datalist>
          <p className="muted bad-hint">{cat.hint}</p>
        </div>
      )}

      <div className="table-wrap cards">
        <table>
          <thead>
            <tr>
              <th>Клиент</th>
              <th className="num">Долг</th>
              <th className="num">Дней без оплат</th>
              <th className="hide-mobile">Причина</th>
              <th className="hide-mobile">Посл. оплата</th>
              {canEdit && <th></th>}
            </tr>
          </thead>
          <tbody>
            {list.length === 0 && (
              <tr>
                <td colSpan={canEdit ? 6 : 5} className="muted center">
                  Список пуст. {canEdit ? 'Добавьте контрагента выше.' : ''}
                </td>
              </tr>
            )}
            {list.map((c) => (
              <tr key={c.client}>
                <td data-label="Клиент">
                  <button className="client-link" onClick={() => onOpen(c.client)}>
                    {c.client}
                  </button>
                </td>
                <td className="num neg" data-label="Долг">{formatMoney(c.debt)}</td>
                <td className="num" data-label="Дней без оплат">
                  {daysNoPay(c) != null ? `${daysNoPay(c)} дн.` : '—'}
                </td>
                <td className="muted hide-mobile" data-label="Причина">
                  {notes[c.client] || '—'}
                </td>
                <td className="hide-mobile" data-label="Посл. оплата">
                  {fmtDate(c.last_payment)}
                </td>
                {canEdit && (
                  <td className="card-action">
                    <button
                      className="btn btn-sm"
                      onClick={() => onRemove(c.client)}
                      title="Вернуть в активную дебиторку"
                    >
                      Вернуть
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function ClientDetailModal({ client, onClose }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    api
      .clientDetail(client)
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(e.message))
    return () => {
      alive = false
    }
  }, [client])

  const t = data?.totals

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="cd-head">
          <h2>{client}</h2>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>✕</button>
        </div>

        {error && <div className="error">{error}</div>}
        {!data && !error && <div className="center muted">Загрузка…</div>}

        {data && (
          <>
            <div className="summary-bar cd-summary">
              <div className="summary-card">
                <span className="summary-label">Отгружено</span>
                <span className="summary-value">{formatMoney(t.shipped)}</span>
              </div>
              {t.returned > 0 && (
                <div className="summary-card">
                  <span className="summary-label">Возвраты</span>
                  <span className="summary-value">−{formatMoney(t.returned)}</span>
                </div>
              )}
              <div className="summary-card summary-in">
                <span className="summary-label">Оплачено</span>
                <span className="summary-value">{formatMoney(t.paid)}</span>
              </div>
              <div className="summary-card summary-out">
                <span className="summary-label">Долг</span>
                <span className="summary-value">{formatMoney(t.debt)}</span>
              </div>
            </div>

            <CdSection
              title="Реализации"
              count={data.shipments.length}
              head={['Дата', 'Документ', 'Позиций', 'Сумма']}
              rows={data.shipments.map((s) => [
                fmtDate(s.date),
                s.doc_number || '—',
                s.positions,
                formatMoney(s.amount),
              ])}
              nums={[false, false, true, true]}
            />

            <CdSection
              title="Оплаты"
              count={data.payments.length}
              head={['Дата', 'Операция', 'Касса/Банк', 'Сумма']}
              rows={data.payments.map((p) => [
                fmtDate(p.date),
                p.operation,
                p.kind === 'cash' ? 'касса' : 'банк',
                formatMoney(p.amount_kgs),
              ])}
              nums={[false, false, false, true]}
            />

            {data.returns.length > 0 && (
              <CdSection
                title="Возвраты"
                count={data.returns.length}
                head={['Дата', 'Сумма']}
                rows={data.returns.map((r) => [fmtDate(r.date), formatMoney(r.amount)])}
                nums={[false, true]}
              />
            )}
          </>
        )}
      </div>
    </div>
  )
}

function CdSection({ title, count, head, rows, nums }) {
  return (
    <div className="cd-section">
      <div className="cd-section-head">
        <span>{title}</span>
        <span className="muted">{count}</span>
      </div>
      {rows.length === 0 ? (
        <div className="muted cd-empty">Нет записей</div>
      ) : (
        <div className="table-wrap cd-table">
          <table>
            <thead>
              <tr>
                {head.map((h, i) => (
                  <th key={i} className={nums[i] ? 'num' : ''}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  {r.map((cell, j) => (
                    <td key={j} className={nums[j] ? 'num' : ''}>{cell}</td>
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

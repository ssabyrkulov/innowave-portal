import { useEffect, useRef, useState } from 'react'
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

export default function DebtPage() {
  const { can } = useAuth()
  const [data, setData] = useState(null)
  const [receipts, setReceipts] = useState([])
  const [showReceipts, setShowReceipts] = useState(false)
  const [error, setError] = useState(null)
  const [importing, setImporting] = useState(false)
  const [importResult, setImportResult] = useState(null)
  const [replacePeriod, setReplacePeriod] = useState(false)
  const [aliasDraft, setAliasDraft] = useState({})
  const [tab, setTab] = useState('active') // active | bad
  const [pickClient, setPickClient] = useState('')
  const [pickNote, setPickNote] = useState('')
  const fileRef = useRef(null)

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

  async function onFile(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setImporting(true)
    setImportResult(null)
    setError(null)
    try {
      const res = await api.importReceipts(file, replacePeriod)
      setImportResult(res)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setImporting(false)
    }
  }

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

  async function addBad() {
    const client = pickClient.trim()
    if (!client) return
    try {
      await api.addBadDebt(client, pickNote.trim() || null)
      setPickClient('')
      setPickNote('')
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function removeBad(client) {
    try {
      await api.removeBadDebt(client)
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  const badSet = new Set(data?.bad_debt || [])
  const allDebtors = data ? data.clients.filter((c) => c.debt > 0.01) : []
  const debtors = allDebtors.filter((c) => !badSet.has(c.client))
  // Безнадёжные показываем даже с нулевым/погашенным долгом — это ручной список.
  const badClients = data ? data.clients.filter((c) => badSet.has(c.client)) : []
  const activeDebt = debtors.reduce((s, c) => s + c.debt, 0)
  const badDebtTotal = badClients.reduce((s, c) => s + c.debt, 0)

  return (
    <div>
      <div className="page-header">
        <h1>Дебиторка</h1>
        {can.editPayments && (
          <div className="import-controls">
            <label className="replace-toggle">
              <input
                type="checkbox"
                checked={replacePeriod}
                onChange={(e) => setReplacePeriod(e.target.checked)}
              />{' '}
              заменить период
            </label>
            <input
              ref={fileRef}
              type="file"
              accept=".xlsx,.xlsm"
              style={{ display: 'none' }}
              onChange={onFile}
            />
            <button
              className="btn btn-primary"
              disabled={importing}
              onClick={() => fileRef.current?.click()}
            >
              {importing ? 'Загрузка…' : '⬆ Загрузить оплаты из 1С'}
            </button>
          </div>
        )}
      </div>

      <div className="note-readonly">
        Учитываются только безналичные оплаты (выгрузка «Поступление денежных
        средств»). Наличные платежи пока не загружаются — реальные долги могут
        быть ниже показанных.
      </div>

      {importResult && (
        <div className="import-result">
          Импорт завершён: добавлено <b>{importResult.added}</b>
          {importResult.replaced_rows > 0 && (
            <>, заменено строк периода: <b>{importResult.replaced_rows}</b></>
          )}
          {importResult.skipped_duplicates > 0 && (
            <>, пропущено дублей: <b>{importResult.skipped_duplicates}</b></>
          )}
          {importResult.errors.length > 0 && (
            <details>
              <summary>Строк с ошибками: {importResult.errors.length}</summary>
              <ul>
                {importResult.errors.map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}

      {error && <div className="error">{error}</div>}

      {data && !data.has_receipts ? (
        <div className="table-wrap">
          <div className="center muted">
            Оплаты ещё не загружены.
            {can.editPayments
              ? ' Нажмите «Загрузить оплаты из 1С» и выберите выгрузку «Поступление денежных средств».'
              : ' Попросите бухгалтера загрузить выгрузку оплат.'}
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
              <span className="summary-label">Долг{badClients.length > 0 ? ' (к взысканию)' : ''}</span>
              <span className="summary-value">{formatMoney(activeDebt)}</span>
            </div>
            <div className="summary-card">
              <span className="summary-label">Должников</span>
              <span className="summary-value">{debtors.length}</span>
            </div>
            {badClients.length > 0 && (
              <div className="summary-card summary-bad">
                <span className="summary-label">Безнадёжные</span>
                <span className="summary-value">
                  {formatMoney(badDebtTotal)}
                  <span className="muted"> · {badClients.length}</span>
                </span>
              </div>
            )}
          </div>

          <div className="ops-tabs debt-tabs">
            <button
              className={`ops-tab ${tab === 'active' ? 'active' : ''}`}
              onClick={() => setTab('active')}
            >
              Активные ({debtors.length})
            </button>
            <button
              className={`ops-tab ${tab === 'bad' ? 'active' : ''}`}
              onClick={() => setTab('bad')}
            >
              Безнадёжные ({badClients.length})
            </button>
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
                </tr>
              </thead>
              <tbody>
                {debtors.length === 0 && (
                  <tr>
                    <td colSpan={8} className="muted center">
                      Долгов нет 🎉
                    </td>
                  </tr>
                )}
                {debtors.map((c) => {
                  const stale = daysSince(c.last_payment) > STALE_DAYS
                  return (
                    <tr key={c.client}>
                      <td>
                        {c.client}
                        {/* на телефоне метку «давно не платил» показываем прямо под именем */}
                        {stale && (
                          <span className="badge badge-overdue debt-stale-inline" title={`Оплат не было больше ${STALE_DAYS} дней`}>
                            {c.last_payment ? `>${daysSince(c.last_payment)} дн. без оплат` : 'не платил'}
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
                            {c.last_payment ? `>${daysSince(c.last_payment)} дн.` : 'не платил'}
                          </span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          )}

          {tab === 'bad' && (
            <div className="bad-debt">
              {can.editPayments && (
                <div className="chart-card bad-add">
                  <div className="bad-add-row">
                    <input
                      list="all-debtors"
                      className="product-search-input"
                      placeholder="Выберите контрагента…"
                      value={pickClient}
                      onChange={(e) => setPickClient(e.target.value)}
                    />
                    <input
                      className="product-search-input"
                      placeholder="Причина (необязательно)"
                      value={pickNote}
                      onChange={(e) => setPickNote(e.target.value)}
                    />
                    <button
                      className="btn btn-primary"
                      disabled={!pickClient.trim()}
                      onClick={addBad}
                    >
                      В безнадёжные
                    </button>
                  </div>
                  <datalist id="all-debtors">
                    {allDebtors.map((c) => (
                      <option key={c.client} value={c.client}>
                        {`долг ${formatMoney(c.debt)}`}
                      </option>
                    ))}
                  </datalist>
                  <p className="muted bad-hint">
                    Безнадёжные контрагенты убираются из активной дебиторки и не
                    учитываются в долге к взысканию.
                  </p>
                </div>
              )}

              <div className="table-wrap cards">
                <table>
                  <thead>
                    <tr>
                      <th>Клиент</th>
                      <th className="num">Долг</th>
                      <th className="hide-mobile">Причина</th>
                      <th className="hide-mobile">Посл. оплата</th>
                      {can.editPayments && <th></th>}
                    </tr>
                  </thead>
                  <tbody>
                    {badClients.length === 0 && (
                      <tr>
                        <td colSpan={5} className="muted center">
                          Список пуст. {can.editPayments ? 'Добавьте контрагента выше.' : ''}
                        </td>
                      </tr>
                    )}
                    {badClients.map((c) => (
                      <tr key={c.client}>
                        <td data-label="Клиент">{c.client}</td>
                        <td className="num neg" data-label="Долг">{formatMoney(c.debt)}</td>
                        <td className="muted hide-mobile" data-label="Причина">
                          {data.bad_debt_notes?.[c.client] || '—'}
                        </td>
                        <td className="hide-mobile" data-label="Посл. оплата">
                          {fmtDate(c.last_payment)}
                        </td>
                        {can.editPayments && (
                          <td className="card-action">
                            <button
                              className="btn btn-sm"
                              onClick={() => removeBad(c.client)}
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
    </div>
  )
}

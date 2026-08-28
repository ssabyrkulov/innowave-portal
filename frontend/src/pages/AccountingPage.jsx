import { Fragment, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { formatMoney } from '../utils'

const fmtMonth = (m) => {
  const [y, mo] = m.split('-')
  const NAMES = ['янв', 'фев', 'мар', 'апр', 'май', 'июн',
    'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
  return `${NAMES[Number(mo) - 1]} ${y.slice(2)}`
}

// ФОТ: начисления зарплаты по месяцам, подразделениям и людям.
// Персональные данные — бэкенд отдаёт этот отчёт только администратору.
function PayrollBlock() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [showPeople, setShowPeople] = useState(false)

  useEffect(() => {
    api.payroll().then(setData).catch((e) => setError(e.message))
  }, [])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="muted">Загрузка…</div>
  if (!data.months.length) {
    return (
      <p className="muted">
        Начислений пока нет — файл «Начисление зарплаты» подтянется
        автосинком.
      </p>
    )
  }

  const max = Math.max(...data.months.map((m) => m.amount), 1)
  return (
    <>
      <p>
        За {fmtMonth(data.last_month)} начислено{' '}
        <b>{formatMoney(data.last_month_amount)}</b> · всего за период{' '}
        <b>{formatMoney(data.total)}</b>
      </p>
      <div className="table-wrap compact">
        <table>
          <thead>
            <tr><th>Месяц</th><th className="num">Начислено</th><th></th></tr>
          </thead>
          <tbody>
            {[...data.months].reverse().map((m) => (
              <tr key={m.month}>
                <td>{fmtMonth(m.month)}</td>
                <td className="num">{formatMoney(m.amount, '')}</td>
                <td style={{ width: '40%' }}>
                  <div className="fot-bar"
                    style={{ width: `${(m.amount / max) * 100}%` }} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="table-wrap compact">
        <table>
          <thead>
            <tr>
              <th>Подразделение</th>
              <th className="num">Людей</th>
              <th className="num">Начислено всего</th>
            </tr>
          </thead>
          <tbody>
            {data.by_department.map((d) => (
              <tr key={d.department}>
                <td>{d.department}</td>
                <td className="num">{d.employees}</td>
                <td className="num">{formatMoney(d.amount, '')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <button className="btn btn-ghost" onClick={() => setShowPeople((v) => !v)}>
        {showPeople ? '▾' : '▸'} По сотрудникам ({data.employees.length})
      </button>
      {showPeople && (
        <div className="table-wrap cards">
          <table>
            <thead>
              <tr>
                <th>Сотрудник</th><th>Должность</th><th>Подразделение</th>
                <th className="num">Начислений</th>
                <th>Последний месяц</th>
                <th className="num">Последнее</th>
                <th className="num">Всего</th>
              </tr>
            </thead>
            <tbody>
              {data.employees.map((e, i) => (
                <tr key={`emp-${i}`}>
                  <td data-label="Сотрудник">
                    {e.employee}
                    <span className="muted"> · {e.organization}</span>
                  </td>
                  <td data-label="Должность">{e.position || '—'}</td>
                  <td data-label="Подразделение">{e.department}</td>
                  <td className="num" data-label="Начислений">{e.accruals}</td>
                  <td data-label="Последний">{e.last_month ? fmtMonth(e.last_month) : '—'}</td>
                  <td className="num" data-label="Последнее">
                    {formatMoney(e.last_amount, '')}
                  </td>
                  <td className="num" data-label="Всего">{formatMoney(e.total, '')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

// Оборотно-сальдовая ведомость из журнала проводок. Клик по счёту
// раскрывает его проводки.
function LedgerBlock() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [open, setOpen] = useState(null)
  const [entries, setEntries] = useState({})
  const [q, setQ] = useState('')

  useEffect(() => {
    api.ledgerAccounts().then(setData).catch((e) => setError(e.message))
  }, [])

  function toggle(account) {
    const next = open === account ? null : account
    setOpen(next)
    if (next && !entries[next]) {
      api.ledgerEntries({ account: next })
        .then((rows) => setEntries((e) => ({ ...e, [next]: rows })))
        .catch(() => setEntries((e) => ({ ...e, [next]: [] })))
    }
  }

  const rows = useMemo(() => {
    const items = data?.rows || []
    const s = q.trim().toLowerCase()
    if (!s) return items
    return items.filter((r) => (r.account || '').toLowerCase().includes(s)
      || (r.name || '').toLowerCase().includes(s))
  }, [data, q])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="muted">Загрузка…</div>
  if (!data.rows.length) {
    return (
      <p className="muted">
        Проводок пока нет — файл «Журнал проводок» подтянется автосинком.
      </p>
    )
  }

  return (
    <>
      <p>
        Проводок: <b>{data.entries.toLocaleString('ru-RU')}</b> · обороты{' '}
        Дт <b>{formatMoney(data.total_debit)}</b> / Кт{' '}
        <b>{formatMoney(data.total_credit)}</b>
        {data.balanced ? (
          <span className="sc-ok"> · сходятся</span>
        ) : (
          <span className="sc-diff"> · НЕ СХОДЯТСЯ — часть проводок потерялась при выгрузке</span>
        )}
      </p>
      <p className="muted">
        Сальдо — разница оборотов с начала журнала. Знак не навязывается:
        активный счёт покажет плюс, пассивный минус. Клик по счёту раскрывает
        его проводки.
      </p>
      <div className="rc-period">
        <input className="filter-select" value={q}
          placeholder="поиск: номер или название счёта"
          onChange={(e) => setQ(e.target.value)} />
        <span className="muted">{rows.length} счетов</span>
      </div>
      <div className="table-wrap compact">
        <table>
          <thead>
            <tr>
              <th>Счёт</th><th>Название</th>
              <th className="num">Оборот Дт</th>
              <th className="num">Оборот Кт</th>
              <th className="num">Сальдо</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => [
              <tr key={r.account} className="doc-row"
                onClick={() => toggle(r.account)}>
                <td>{open === r.account ? '▾' : '▸'} {r.account}</td>
                <td>{r.name || '—'}</td>
                <td className="num">{formatMoney(r.debit, '')}</td>
                <td className="num">{formatMoney(r.credit, '')}</td>
                <td className={`num ${r.balance < 0 ? 'neg' : ''}`}>
                  {formatMoney(r.balance, '')}
                </td>
              </tr>,
              open === r.account && (
                <tr key={r.account + ':e'}>
                  <td colSpan={5} className="doc-lines">
                    {!entries[r.account] ? (
                      <div className="muted">Загрузка…</div>
                    ) : (
                      <table>
                        <thead>
                          <tr>
                            <th>Дата</th><th>Документ</th>
                            <th>Дт</th><th>Кт</th>
                            <th className="num">Сумма</th>
                          </tr>
                        </thead>
                        <tbody>
                          {entries[r.account].map((e, i) => (
                            <tr key={i}>
                              <td>{e.date.split('-').reverse().join('.')}</td>
                              <td>
                                {e.doc || '—'}
                                {e.content && (
                                  <span className="muted skipped-cols">{e.content}</span>
                                )}
                              </td>
                              <td>
                                {e.debit_account}
                                {e.debit_sub && <span className="muted"> · {e.debit_sub}</span>}
                              </td>
                              <td>
                                {e.credit_account}
                                {e.credit_sub && <span className="muted"> · {e.credit_sub}</span>}
                              </td>
                              <td className="num">{formatMoney(e.amount, '')}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                    {entries[r.account]?.length === 200 && (
                      <div className="muted">Показаны последние 200 проводок.</div>
                    )}
                  </td>
                </tr>
              ),
            ])}
          </tbody>
        </table>
      </div>
    </>
  )
}

export default function AccountingPage() {
  return (
    <div>
      <div className="page-header">
        <h1>Учёт 1С</h1>
        <span className="muted">ФОТ и журнал проводок · из управленки</span>
      </div>

      <h2 className="section-title">ФОТ · начисление зарплаты</h2>
      <PayrollBlock />

      <h2 className="section-title">Оборотно-сальдовая ведомость</h2>
      <LedgerBlock />
    </div>
  )
}

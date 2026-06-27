import { useEffect, useState } from 'react'
import Calendar from '../components/Calendar'
import PaymentModal from '../components/PaymentModal'
import { api } from '../api'
import { useAuth } from '../auth'
import { formatMoney, toISODate } from '../utils'

export default function CalendarPage() {
  const { can } = useAuth()
  const now = new Date()
  const [year, setYear] = useState(now.getFullYear())
  const [month, setMonth] = useState(now.getMonth())
  const [payments, setPayments] = useState([])
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)
  const [modal, setModal] = useState(null) // {payment} or {date}

  const dateFrom = toISODate(new Date(year, month, 1))
  const dateTo = toISODate(new Date(year, month + 1, 0))

  async function load() {
    setError(null)
    try {
      const [p, s] = await Promise.all([
        api.listPayments({ date_from: dateFrom, date_to: dateTo }),
        api.summary({ date_from: dateFrom, date_to: dateTo }),
      ])
      setPayments(p)
      setSummary(s)
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [year, month])

  function prev() {
    if (month === 0) {
      setMonth(11)
      setYear((y) => y - 1)
    } else setMonth((m) => m - 1)
  }
  function next() {
    if (month === 11) {
      setMonth(0)
      setYear((y) => y + 1)
    } else setMonth((m) => m + 1)
  }
  function today() {
    setYear(now.getFullYear())
    setMonth(now.getMonth())
  }

  function onDayClick(iso) {
    if (!can.editPayments) return
    setModal({ date: iso })
  }

  async function save(data) {
    if (modal?.payment?.id) await api.updatePayment(modal.payment.id, data)
    else await api.createPayment(data)
    await load()
  }

  return (
    <div>
      {summary && (
        <div className="summary-bar">
          <div className="summary-card summary-in">
            <span className="summary-label">Поступления</span>
            <span className="summary-value">{formatMoney(summary.incoming_total)}</span>
          </div>
          <div className="summary-card summary-out">
            <span className="summary-label">Списания</span>
            <span className="summary-value">{formatMoney(summary.outgoing_total)}</span>
          </div>
          <div className="summary-card summary-balance">
            <span className="summary-label">Баланс месяца</span>
            <span className="summary-value">{formatMoney(summary.balance)}</span>
          </div>
          <div className="summary-card">
            <span className="summary-label">Платежей</span>
            <span className="summary-value">{summary.count}</span>
          </div>
          {can.editPayments && (
            <button
              className="btn btn-primary summary-add"
              onClick={() => setModal({ date: toISODate(new Date()) })}
            >
              + Платёж
            </button>
          )}
        </div>
      )}

      {error && <div className="error">{error}</div>}

      <Calendar
        year={year}
        month={month}
        payments={payments}
        onPrev={prev}
        onNext={next}
        onToday={today}
        onDayClick={onDayClick}
      />

      {modal && (
        <PaymentModal
          initial={modal.payment || { due_date: modal.date }}
          onClose={() => setModal(null)}
          onSave={save}
        />
      )}
    </div>
  )
}

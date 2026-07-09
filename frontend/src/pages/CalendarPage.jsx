import { useEffect, useState } from 'react'
import Calendar from '../components/Calendar'
import PaymentModal from '../components/PaymentModal'
import { api } from '../api'
import { useAuth } from '../auth'
import { DIRECTION_LABELS, STATUS_LABELS, formatMoney, toISODate } from '../utils'

export default function CalendarPage() {
  const { can } = useAuth()
  const now = new Date()
  const [year, setYear] = useState(now.getFullYear())
  const [month, setMonth] = useState(now.getMonth())
  const [payments, setPayments] = useState([])
  const [summary, setSummary] = useState(null)
  const [error, setError] = useState(null)
  // {type:'day', date} — список платежей дня; {type:'form', date, payment} — форма
  const [modal, setModal] = useState(null)

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
    setModal({ type: 'day', date: iso })
  }

  async function save(data) {
    if (modal?.payment?.id) await api.updatePayment(modal.payment.id, data)
    else await api.createPayment(data)
    await load()
  }

  async function removePayment(p) {
    if (!confirm(`Удалить платёж «${p.title}»?`)) return
    try {
      await api.deletePayment(p.id)
      await load()
    } catch (err) {
      setError(err.message)
    }
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
              onClick={() => setModal({ type: 'form', date: toISODate(new Date()) })}
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

      {modal?.type === 'day' && (
        <DayModal
          date={modal.date}
          payments={payments.filter((p) => p.due_date === modal.date)}
          canEdit={can.editPayments}
          onClose={() => setModal(null)}
          onAdd={() => setModal({ type: 'form', date: modal.date })}
          onEdit={(p) => setModal({ type: 'form', date: modal.date, payment: p })}
          onDelete={removePayment}
        />
      )}

      {modal?.type === 'form' && (
        <PaymentModal
          initial={modal.payment || { due_date: modal.date }}
          onClose={() => setModal(null)}
          onSave={save}
        />
      )}
    </div>
  )
}

function formatDayTitle(iso) {
  const [y, m, d] = iso.split('-')
  return `${d}.${m}.${y}`
}

function DayModal({ date, payments, canEdit, onClose, onAdd, onEdit, onDelete }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="day-modal-header">
          <h2>{formatDayTitle(date)}</h2>
          {canEdit && (
            <button className="btn btn-primary btn-sm" onClick={onAdd}>
              + Добавить платёж
            </button>
          )}
        </div>

        {payments.length === 0 ? (
          <div className="muted center">На этот день платежей нет</div>
        ) : (
          <div className="day-list">
            {payments.map((p) => (
              <div key={p.id} className="day-item">
                <div className="day-item-main">
                  <div className="day-item-title">{p.title}</div>
                  <div className="day-item-sub">
                    {DIRECTION_LABELS[p.direction]}
                    {p.counterparty ? ` · ${p.counterparty}` : ''}
                  </div>
                </div>
                <div className="day-item-right">
                  <div
                    className={`day-item-amount ${
                      p.direction === 'incoming' ? 'pos' : 'neg'
                    }`}
                  >
                    {p.direction === 'incoming' ? '+' : '−'}
                    {formatMoney(p.amount, p.currency)}
                  </div>
                  <span className={`badge badge-${p.status}`}>
                    {STATUS_LABELS[p.status]}
                  </span>
                </div>
                {canEdit && (
                  <div className="day-item-actions">
                    <button className="btn btn-sm" onClick={() => onEdit(p)}>
                      ✎
                    </button>
                    <button
                      className="btn btn-sm btn-danger"
                      onClick={() => onDelete(p)}
                    >
                      🗑
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onClose}>
            Закрыть
          </button>
        </div>
      </div>
    </div>
  )
}

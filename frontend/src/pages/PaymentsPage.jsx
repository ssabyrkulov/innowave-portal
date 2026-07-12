import { useEffect, useState } from 'react'
import PaymentModal from '../components/PaymentModal'
import { api } from '../api'
import { useAuth } from '../auth'
import { DIRECTION_LABELS, STATUS_LABELS, formatMoney } from '../utils'

export default function PaymentsPage() {
  const { can } = useAuth()
  const [payments, setPayments] = useState([])
  const [filters, setFilters] = useState({ status: '', direction: '' })
  const [error, setError] = useState(null)
  const [modal, setModal] = useState(null)

  async function load() {
    setError(null)
    try {
      setPayments(await api.listPayments(filters))
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters])

  async function save(data) {
    if (modal?.id) await api.updatePayment(modal.id, data)
    else await api.createPayment(data)
    await load()
  }

  async function remove(p) {
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
      <div className="page-header">
        <h1>Платежи</h1>
        {can.editPayments && (
          <button className="btn btn-primary" onClick={() => setModal({})}>
            + Новый платёж
          </button>
        )}
      </div>

      <div className="filters">
        <select
          value={filters.direction}
          onChange={(e) => setFilters((f) => ({ ...f, direction: e.target.value }))}
        >
          <option value="">Все направления</option>
          <option value="incoming">Входящие</option>
          <option value="outgoing">Исходящие</option>
        </select>
        <select
          value={filters.status}
          onChange={(e) => setFilters((f) => ({ ...f, status: e.target.value }))}
        >
          <option value="">Все статусы</option>
          <option value="planned">Запланирован</option>
          <option value="paid">Оплачен</option>
          <option value="overdue">Просрочен</option>
        </select>
      </div>

      {error && <div className="error">{error}</div>}

      <div className="table-wrap cards">
        <table>
          <thead>
            <tr>
              <th>Дата</th>
              <th>Название</th>
              <th>Контрагент</th>
              <th>Категория</th>
              <th className="num">Сумма</th>
              <th>Направление</th>
              <th>Статус</th>
              <th>Автор</th>
              {can.editPayments && <th></th>}
            </tr>
          </thead>
          <tbody>
            {payments.length === 0 && (
              <tr>
                <td colSpan={9} className="muted center">
                  Платежей пока нет
                </td>
              </tr>
            )}
            {payments.map((p) => (
              <tr key={p.id}>
                <td data-label="Дата">{p.due_date}</td>
                <td data-label="Название">{p.title}</td>
                <td data-label="Контрагент">{p.counterparty || '—'}</td>
                <td data-label="Категория">{p.category || '—'}</td>
                <td className={`num ${p.direction === 'incoming' ? 'pos' : 'neg'}`} data-label="Сумма">
                  {p.direction === 'incoming' ? '+' : '−'}
                  {formatMoney(p.amount, p.currency)}
                </td>
                <td data-label="Направление">{DIRECTION_LABELS[p.direction]}</td>
                <td data-label="Статус">
                  <span className={`badge badge-${p.status}`}>
                    {STATUS_LABELS[p.status]}
                  </span>
                </td>
                <td className="muted" data-label="Автор">{p.creator_name || '—'}</td>
                {can.editPayments && (
                  <td className="actions card-action">
                    <button className="btn btn-sm" onClick={() => setModal(p)}>
                      ✎
                    </button>
                    <button className="btn btn-sm btn-danger" onClick={() => remove(p)}>
                      🗑
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {modal && (
        <PaymentModal
          initial={modal.id ? modal : {}}
          onClose={() => setModal(null)}
          onSave={save}
        />
      )}
    </div>
  )
}

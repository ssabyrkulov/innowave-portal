import { useState } from 'react'

const EMPTY = {
  title: '',
  amount: '',
  currency: 'KGS',
  direction: 'outgoing',
  status: 'planned',
  due_date: '',
  counterparty: '',
  category: '',
  note: '',
}

export default function PaymentModal({ initial, onClose, onSave }) {
  const [form, setForm] = useState({ ...EMPTY, ...sanitize(initial) })
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)
  const isEdit = Boolean(initial?.id)

  function update(field, value) {
    setForm((f) => ({ ...f, [field]: value }))
  }

  async function submit(e) {
    e.preventDefault()
    setError(null)
    if (!form.title.trim()) return setError('Укажите название платежа')
    if (!form.amount || Number(form.amount) <= 0)
      return setError('Сумма должна быть больше нуля')
    if (!form.due_date) return setError('Укажите дату платежа')

    setSaving(true)
    try {
      await onSave({
        ...form,
        amount: String(form.amount),
        counterparty: form.counterparty || null,
        category: form.category || null,
        note: form.note || null,
      })
      onClose()
    } catch (err) {
      setError(err.message)
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>{isEdit ? 'Редактировать платёж' : 'Новый платёж'}</h2>
        <form onSubmit={submit}>
          <label>
            Название
            <input
              value={form.title}
              onChange={(e) => update('title', e.target.value)}
              placeholder="Например, аренда офиса"
              autoFocus
            />
          </label>

          <div className="row">
            <label>
              Сумма
              <input
                type="number"
                step="0.01"
                min="0"
                value={form.amount}
                onChange={(e) => update('amount', e.target.value)}
              />
            </label>
            <label>
              Валюта
              <select value={form.currency} onChange={(e) => update('currency', e.target.value)}>
                <option value="KGS">KGS</option>
                <option value="USD">USD</option>
                <option value="EUR">EUR</option>
                <option value="RUB">RUB</option>
                <option value="KZT">KZT</option>
              </select>
            </label>
          </div>

          <div className="row">
            <label>
              Направление
              <select value={form.direction} onChange={(e) => update('direction', e.target.value)}>
                <option value="outgoing">Исходящий</option>
                <option value="incoming">Входящий</option>
              </select>
            </label>
            <label>
              Статус
              <select value={form.status} onChange={(e) => update('status', e.target.value)}>
                <option value="planned">Запланирован</option>
                <option value="paid">Оплачен</option>
                <option value="overdue">Просрочен</option>
              </select>
            </label>
          </div>

          <div className="row">
            <label>
              Дата
              <input
                type="date"
                value={form.due_date}
                onChange={(e) => update('due_date', e.target.value)}
              />
            </label>
            <label>
              Контрагент
              <input
                value={form.counterparty}
                onChange={(e) => update('counterparty', e.target.value)}
                placeholder="Необязательно"
              />
            </label>
          </div>

          <label>
            Категория
            <input
              value={form.category}
              onChange={(e) => update('category', e.target.value)}
              placeholder="Аренда, зарплата, налоги…"
            />
          </label>

          <label>
            Комментарий
            <textarea
              value={form.note}
              onChange={(e) => update('note', e.target.value)}
              rows={2}
            />
          </label>

          {error && <div className="error">{error}</div>}

          <div className="modal-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>
              Отмена
            </button>
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? 'Сохранение…' : 'Сохранить'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function sanitize(p) {
  if (!p) return {}
  const out = {}
  for (const k of Object.keys(EMPTY)) {
    if (p[k] != null) out[k] = p[k]
  }
  return out
}

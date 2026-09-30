import { useEffect, useRef, useState } from 'react'
import { api, getOrg } from '../api'
import { ORG_LABELS } from '../utils'

const EMPTY = {
  organization: '',
  title: '',
  amount: '',
  currency: 'KGS',
  due_date: '',
  counterparty: '',
  counterparty_guid: '',
  article: '',
  method: 'bank',
  basis: '',
  purpose: '',
  note: '',
}

function todayISO() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/* Форма заявки на платёж: создание и правка черновика. Файлы, выбранные при
   создании, отправляются сразу после того, как заявка получила номер. */
export default function RequestForm({ initial, onClose, onSaved }) {
  const isEdit = Boolean(initial?.id)
  const org = getOrg()
  const [form, setForm] = useState({
    ...EMPTY,
    organization: org !== 'all' ? org : 'hygiene',
    due_date: todayISO(),
    ...sanitize(initial),
  })
  const [files, setFiles] = useState([])
  const [submitNow, setSubmitNow] = useState(!isEdit)
  const [articles, setArticles] = useState([])
  const [cpHints, setCpHints] = useState([])
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)
  const cpTimer = useRef(null)

  useEffect(() => {
    api.approvalsArticles().then(setArticles).catch(() => {})
  }, [])

  function update(field, value) {
    setForm((f) => ({ ...f, [field]: value }))
  }

  function onCounterparty(value) {
    update('counterparty', value)
    update('counterparty_guid', '')
    clearTimeout(cpTimer.current)
    if (value.trim().length < 2) return setCpHints([])
    cpTimer.current = setTimeout(() => {
      api.approvalsCounterparties(value).then(setCpHints).catch(() => setCpHints([]))
    }, 250)
  }

  function pickCounterparty(c) {
    setForm((f) => ({ ...f, counterparty: c.name, counterparty_guid: c.guid }))
    setCpHints([])
  }

  async function submit(e) {
    e.preventDefault()
    setError(null)
    if (!form.title.trim()) return setError('Напишите, за что платим')
    if (!form.amount || Number(form.amount) <= 0) return setError('Сумма должна быть больше нуля')
    if (!form.counterparty.trim()) return setError('Укажите контрагента')
    if (!form.due_date) return setError('Укажите срок оплаты')

    setSaving(true)
    try {
      const body = {
        ...form,
        amount: String(form.amount),
        counterparty_guid: form.counterparty_guid || null,
        article: form.article || null,
        basis: form.basis || null,
        purpose: form.purpose || null,
        note: form.note || null,
      }
      let saved
      if (isEdit) {
        saved = await api.approvalUpdate(initial.id, body)
      } else {
        saved = await api.approvalCreate({ ...body, submit: false })
      }
      for (const f of files) {
        saved = await api.approvalUpload(saved.id, f, 'basis')
      }
      if (submitNow && saved.status === 'draft') {
        saved = await api.approvalAction(saved.id, 'submit')
      }
      await onSaved(saved)
      onClose()
    } catch (err) {
      setError(err.message)
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>{isEdit ? `Заявка №${initial.id}` : 'Новая заявка на платёж'}</h2>
        <form onSubmit={submit}>
          <div className="row">
            <label>
              Фирма
              <select value={form.organization} onChange={(e) => update('organization', e.target.value)}>
                {Object.entries(ORG_LABELS).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
            </label>
            <label>
              Способ оплаты
              <select value={form.method} onChange={(e) => update('method', e.target.value)}>
                <option value="bank">Банк</option>
                <option value="cash">Касса</option>
                <option value="card">Карта</option>
              </select>
            </label>
          </div>

          <label>
            За что платим
            <input
              value={form.title}
              onChange={(e) => update('title', e.target.value)}
              placeholder="Например, аренда офиса за октябрь"
              autoFocus
            />
          </label>

          <label className="cp-field">
            Контрагент
            <input
              value={form.counterparty}
              onChange={(e) => onCounterparty(e.target.value)}
              placeholder="Начните вводить название из 1С"
              autoComplete="off"
            />
            {cpHints.length > 0 && (
              <div className="cp-hints">
                {cpHints.map((c) => (
                  <button type="button" key={c.guid} onClick={() => pickCounterparty(c)}>
                    {c.name}
                    {c.inn && <span className="muted"> · ИНН {c.inn}</span>}
                  </button>
                ))}
              </div>
            )}
            {form.counterparty_guid ? (
              <span className="hint-line">Из справочника 1С</span>
            ) : form.counterparty.trim().length > 1 ? (
              <span className="hint-line">Нового контрагента бухгалтер заведёт в 1С до оплаты</span>
            ) : null}
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
            <label>
              Оплатить до
              <input
                type="date"
                value={form.due_date}
                onChange={(e) => update('due_date', e.target.value)}
              />
            </label>
          </div>

          <label>
            Статья бюджета
            <input
              list="approval-articles"
              value={form.article}
              onChange={(e) => update('article', e.target.value)}
              placeholder="Аренда, зарплата, товар, логистика…"
            />
            <datalist id="approval-articles">
              {articles.map((a) => (
                <option key={a} value={a} />
              ))}
            </datalist>
          </label>

          <label>
            Основание
            <input
              value={form.basis}
              onChange={(e) => update('basis', e.target.value)}
              placeholder="Счёт №… от …, договор №…"
            />
          </label>

          <label>
            Назначение платежа
            <input
              value={form.purpose}
              onChange={(e) => update('purpose', e.target.value)}
              placeholder="Как напишем в платёжке"
            />
          </label>

          <label>
            Комментарий
            <textarea value={form.note} onChange={(e) => update('note', e.target.value)} rows={2} />
          </label>

          <label>
            Документы (счёт, договор, акт)
            <input
              type="file"
              multiple
              onChange={(e) => setFiles(Array.from(e.target.files || []))}
            />
            {files.length > 0 && (
              <span className="hint-line">{files.map((f) => f.name).join(', ')}</span>
            )}
          </label>

          {!isEdit && (
            <label className="check-line">
              <input
                type="checkbox"
                checked={submitNow}
                onChange={(e) => setSubmitNow(e.target.checked)}
              />
              Сразу отправить на проверку
            </label>
          )}

          {error && <div className="error">{error}</div>}

          <div className="modal-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>
              Отмена
            </button>
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? 'Сохранение…' : isEdit ? 'Сохранить' : submitNow ? 'Отправить' : 'Сохранить черновик'}
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

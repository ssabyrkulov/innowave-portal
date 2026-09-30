import { useEffect, useState } from 'react'
import { api } from '../api'
import {
  METHOD_LABELS,
  ORG_LABELS,
  REQUEST_STATUS_LABELS,
  formatDateRu,
  formatDateTimeRu,
  formatMoney,
} from '../utils'

const ACTION_LABELS = {
  submit: 'Отправить на проверку',
  check: 'Проверено, на согласование',
  return: 'Вернуть на доработку',
  approve: 'Утвердить',
  reject: 'Отклонить',
  pay: 'Оплачено',
  cancel: 'Отменить заявку',
}

const EVENT_LABELS = {
  create: 'Создана',
  update: 'Изменена',
  submit: 'Отправлена на проверку',
  check: 'Проверена',
  return: 'Возвращена на доработку',
  approve: 'Утверждена',
  reject: 'Отклонена',
  pay: 'Оплачена',
  cancel: 'Отменена',
  upload: 'Добавлен файл',
  file_delete: 'Удалён файл',
  reconcile: 'Найдена в учёте 1С',
}

function todayISO() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function fmtSize(n) {
  if (n < 1024) return `${n} Б`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} КБ`
  return `${(n / 1024 / 1024).toFixed(1)} МБ`
}

/* Карточка заявки: все поля, предупреждения, бюджет и деньги, файлы,
   кнопки действий по правам и история. Действия с комментарием (возврат,
   отклонение, оплата) раскрывают маленькую панель внутри карточки. */
export default function RequestDetail({ id, onClose, onChanged, onEdit }) {
  const [req, setReq] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [panel, setPanel] = useState(null) // { action }
  const [comment, setComment] = useState('')
  const [pay, setPay] = useState({ paid_date: todayISO(), paid_amount: '', paid_doc: '' })
  const [uploadKind, setUploadKind] = useState('basis')

  async function load() {
    try {
      const r = await api.approvalGet(id)
      setReq(r)
      setPay((p) => ({ ...p, paid_amount: p.paid_amount || String(r.amount) }))
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])

  function openPanel(action) {
    setPanel({ action })
    setComment('')
    setError(null)
  }

  async function run(action, body = {}) {
    setBusy(true)
    setError(null)
    try {
      const r = await api.approvalAction(id, action, body)
      setReq(r)
      setPanel(null)
      setComment('')
      await load()
      onChanged && onChanged(r)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  function confirmPanel() {
    const a = panel.action
    if ((a === 'return' || a === 'reject') && !comment.trim()) {
      return setError('Напишите причину — автору нужно понять, что исправить')
    }
    if (a === 'pay') {
      if (!pay.paid_date) return setError('Укажите дату оплаты')
      return run('pay', {
        paid_date: pay.paid_date,
        paid_amount: pay.paid_amount ? String(pay.paid_amount) : null,
        paid_doc: pay.paid_doc || null,
        comment: comment || null,
      })
    }
    run(a, { comment: comment || null })
  }

  async function upload(e) {
    const files = Array.from(e.target.files || [])
    if (!files.length) return
    setBusy(true)
    setError(null)
    try {
      for (const f of files) await api.approvalUpload(id, f, uploadKind)
      await load()
      onChanged && onChanged()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
      e.target.value = ''
    }
  }

  async function removeFile(f) {
    if (!confirm(`Удалить файл «${f.filename}»?`)) return
    setBusy(true)
    try {
      await api.approvalFileDelete(id, f.id)
      await load()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const can = req?.can || {}
  const actions = req
    ? ['submit', 'check', 'approve', 'pay', 'return', 'reject', 'cancel'].filter((a) => can[a])
    : []

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal-wide req-detail" onClick={(e) => e.stopPropagation()}>
        {!req && !error && <div className="muted center">Загрузка…</div>}
        {req && (
          <>
            <div className="req-head">
              <div>
                <h2>
                  Заявка №{req.id} · {req.title}
                </h2>
                <div className="muted">
                  {ORG_LABELS[req.organization] || req.organization} · {METHOD_LABELS[req.method]} ·
                  создал {req.creator_name || '—'} {formatDateTimeRu(req.created_at)}
                </div>
              </div>
              <span className={`badge badge-req-${req.status}`}>
                {REQUEST_STATUS_LABELS[req.status] || req.status}
              </span>
            </div>

            {req.warnings?.length > 0 && (
              <div className="req-warnings">
                {req.warnings.map((w, i) => (
                  <div key={i} className={`req-warning req-warning-${w.level}`}>
                    {w.level === 'warn' ? '⚠️ ' : 'ℹ️ '}
                    {w.text}
                  </div>
                ))}
              </div>
            )}

            <div className="req-grid">
              <div className="req-field">
                <span className="req-label">Сумма</span>
                <strong className="req-amount">{formatMoney(req.amount, req.currency)}</strong>
              </div>
              <div className="req-field">
                <span className="req-label">Оплатить до</span>
                <span>{formatDateRu(req.due_date)}</span>
              </div>
              <div className="req-field">
                <span className="req-label">Контрагент</span>
                <span>
                  {req.counterparty}
                  {!req.counterparty_guid && <span className="hint-line">Нет в справочнике 1С</span>}
                </span>
              </div>
              <div className="req-field">
                <span className="req-label">Статья бюджета</span>
                <span>{req.article || '—'}</span>
              </div>
              <div className="req-field">
                <span className="req-label">Основание</span>
                <span>{req.basis || '—'}</span>
              </div>
              <div className="req-field">
                <span className="req-label">Назначение платежа</span>
                <span>{req.purpose || '—'}</span>
              </div>
              {req.note && (
                <div className="req-field req-field-wide">
                  <span className="req-label">Комментарий автора</span>
                  <span>{req.note}</span>
                </div>
              )}
              {req.decision_comment && (
                <div className="req-field req-field-wide">
                  <span className="req-label">Решение</span>
                  <span>{req.decision_comment}</span>
                </div>
              )}
              {req.paid_date && (
                <div className="req-field req-field-wide">
                  <span className="req-label">Оплата</span>
                  <span>
                    {formatDateRu(req.paid_date)} · {formatMoney(req.paid_amount ?? req.amount, req.currency)}
                    {req.paid_doc ? ` · ${req.paid_doc}` : ''}
                    {req.payer_name ? ` · ${req.payer_name}` : ''}
                    {req.expense_id ? ' · найдена в учёте 1С' : ''}
                  </span>
                </div>
              )}
            </div>

            <div className="req-blocks">
              <div className="req-block">
                <div className="req-block-title">Бюджет по статье</div>
                {!req.budget ? (
                  <div className="muted">Статья не указана</div>
                ) : req.budget.no_plan ? (
                  <div className="muted">
                    План на {req.budget.period} по статье «{req.budget.article}» не задан
                  </div>
                ) : (
                  <table className="req-mini">
                    <tbody>
                      <tr><td>План на {req.budget.period}</td><td className="num">{formatMoney(req.budget.plan, false)}</td></tr>
                      <tr><td>Уже оплачено (1С)</td><td className="num">{formatMoney(req.budget.fact, false)}</td></tr>
                      <tr><td>Утверждено, не оплачено</td><td className="num">{formatMoney(req.budget.pending, false)}</td></tr>
                      <tr className={req.budget.over ? 'neg' : ''}>
                        <td>Остаток лимита</td>
                        <td className="num">{formatMoney(req.budget.left, false)}</td>
                      </tr>
                    </tbody>
                  </table>
                )}
              </div>
              <div className="req-block">
                <div className="req-block-title">Деньги фирмы</div>
                {!req.cash || req.cash.updated_at == null ? (
                  <div className="muted">Остатки из 1С ещё не загружены</div>
                ) : (
                  <table className="req-mini">
                    <tbody>
                      <tr><td>Остаток сейчас</td><td className="num">{formatMoney(req.cash.total, false)}</td></tr>
                      <tr><td>Утверждено, не оплачено</td><td className="num">{formatMoney(req.cash.committed, false)}</td></tr>
                      <tr className={req.cash.negative ? 'neg' : req.cash.below_cushion ? 'warn' : ''}>
                        <td>После этой оплаты</td>
                        <td className="num">{formatMoney(req.cash.after, false)}</td>
                      </tr>
                      {Number(req.cash.cushion) > 0 && (
                        <tr><td>Неснижаемый остаток</td><td className="num">{formatMoney(req.cash.cushion, false)}</td></tr>
                      )}
                    </tbody>
                  </table>
                )}
              </div>
            </div>

            <div className="req-files">
              <div className="req-block-title">
                Документы {req.files.length > 0 && <span className="muted">({req.files.length})</span>}
              </div>
              {req.files.length === 0 && <div className="muted">Файлов пока нет</div>}
              {req.files.map((f) => (
                <div key={f.id} className="req-file">
                  <button
                    type="button"
                    className="req-file-link"
                    onClick={() => api.approvalFileOpen(id, f.id, f.filename).catch((e) => setError(e.message))}
                  >
                    📎 {f.filename}
                  </button>
                  <span className="muted">
                    {f.kind === 'payment' ? 'платёжка' : 'основание'} · {fmtSize(f.size)} · {formatDateTimeRu(f.created_at)}
                  </span>
                  {can.upload && (
                    <button className="btn btn-sm btn-ghost" onClick={() => removeFile(f)} disabled={busy}>
                      ✕
                    </button>
                  )}
                </div>
              ))}
              {can.upload && (
                <div className="req-upload">
                  <select value={uploadKind} onChange={(e) => setUploadKind(e.target.value)}>
                    <option value="basis">Основание (счёт, договор, акт)</option>
                    <option value="payment">Платёжный документ</option>
                  </select>
                  <input type="file" multiple onChange={upload} disabled={busy} />
                </div>
              )}
            </div>

            {panel && (
              <div className="req-panel">
                <div className="req-block-title">{ACTION_LABELS[panel.action]}</div>
                {panel.action === 'pay' && (
                  <div className="row">
                    <label>
                      Дата оплаты
                      <input type="date" value={pay.paid_date}
                        onChange={(e) => setPay((p) => ({ ...p, paid_date: e.target.value }))} />
                    </label>
                    <label>
                      Сумма оплаты
                      <input type="number" step="0.01" min="0" value={pay.paid_amount}
                        onChange={(e) => setPay((p) => ({ ...p, paid_amount: e.target.value }))} />
                    </label>
                    <label>
                      Номер платёжки
                      <input value={pay.paid_doc} placeholder="ПП №…"
                        onChange={(e) => setPay((p) => ({ ...p, paid_doc: e.target.value }))} />
                    </label>
                  </div>
                )}
                <label>
                  {panel.action === 'return' || panel.action === 'reject' ? 'Причина' : 'Комментарий'}
                  <textarea rows={2} value={comment} onChange={(e) => setComment(e.target.value)} autoFocus />
                </label>
                <div className="modal-actions">
                  <button className="btn btn-ghost" onClick={() => setPanel(null)} disabled={busy}>
                    Назад
                  </button>
                  <button
                    className={`btn ${panel.action === 'reject' || panel.action === 'cancel' ? 'btn-danger' : 'btn-primary'}`}
                    onClick={confirmPanel}
                    disabled={busy}
                  >
                    {busy ? 'Сохранение…' : 'Подтвердить'}
                  </button>
                </div>
              </div>
            )}

            {error && <div className="error">{error}</div>}

            {!panel && (
              <div className="req-actions">
                {can.edit && (
                  <button className="btn" onClick={() => onEdit(req)} disabled={busy}>
                    ✎ Изменить
                  </button>
                )}
                {actions.map((a) => (
                  <button
                    key={a}
                    className={`btn ${
                      a === 'approve' || a === 'check' || a === 'submit' || a === 'pay'
                        ? 'btn-primary'
                        : a === 'reject' || a === 'cancel'
                          ? 'btn-danger'
                          : ''
                    }`}
                    disabled={busy}
                    onClick={() =>
                      a === 'submit' || a === 'check' || a === 'approve' ? run(a) : openPanel(a)
                    }
                  >
                    {ACTION_LABELS[a]}
                  </button>
                ))}
                <span className="req-actions-spacer" />
                <button className="btn btn-ghost" onClick={onClose}>
                  Закрыть
                </button>
              </div>
            )}

            <div className="req-history">
              <div className="req-block-title">История</div>
              {req.events.map((ev) => (
                <div key={ev.id} className="req-event">
                  <span className="muted">{formatDateTimeRu(ev.created_at)}</span>
                  <span>
                    <strong>{EVENT_LABELS[ev.action] || ev.action}</strong>
                    {ev.user_name ? ` — ${ev.user_name}` : ''}
                    {ev.comment ? `: ${ev.comment}` : ''}
                  </span>
                </div>
              ))}
            </div>
          </>
        )}
        {!req && error && (
          <>
            <div className="error">{error}</div>
            <div className="modal-actions">
              <button className="btn btn-ghost" onClick={onClose}>Закрыть</button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

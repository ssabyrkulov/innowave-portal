import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import RequestDetail from '../components/RequestDetail'
import RequestForm from '../components/RequestForm'
import { api } from '../api'
import { useAuth } from '../auth'
import {
  METHOD_LABELS,
  ORG_LABELS,
  REQUEST_STATUS_LABELS,
  formatDateRu,
  formatMoney,
} from '../utils'

const TABS = [
  { key: 'inbox', label: 'Ждут меня' },
  { key: 'active', label: 'В работе' },
  { key: 'mine', label: 'Мои' },
  { key: 'all', label: 'Все' },
]

function todayISO() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/* Согласование платежей. Вкладки: «Ждут меня» — заявки, по которым нужен
   именно мой шаг; «В работе» — всё, что ещё не закрыто; «Мои» — созданные
   мной; «Все» — с фильтром по статусу. Клик по строке открывает карточку
   (адрес ?id=… — карточку можно прислать ссылкой). */
export default function ApprovalsPage() {
  const { user } = useAuth()
  const [params, setParams] = useSearchParams()
  const [tab, setTab] = useState(params.get('tab') || 'inbox')
  const [status, setStatus] = useState('')
  const [rows, setRows] = useState([])
  const [summary, setSummary] = useState(null)
  const [settings, setSettings] = useState(null)
  const [error, setError] = useState(null)
  const [form, setForm] = useState(null) // {} новая, объект — правка
  const [settingsOpen, setSettingsOpen] = useState(false)
  const openId = params.get('id') ? Number(params.get('id')) : null

  async function load() {
    setError(null)
    try {
      const p = {}
      if (tab === 'inbox' || tab === 'mine') p.scope = tab
      if (tab === 'active') p.status = 'active'
      if (tab === 'all' && status) p.status = status
      const [list, sum] = await Promise.all([api.approvalsList(p), api.approvalsSummary()])
      setRows(list)
      setSummary(sum)
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, status])

  useEffect(() => {
    if (user?.role === 'admin') api.approvalsSettings().then(setSettings).catch(() => {})
  }, [user])

  function open(id) {
    const next = new URLSearchParams(params)
    if (id) next.set('id', String(id))
    else next.delete('id')
    setParams(next, { replace: true })
  }

  function changeTab(k) {
    setTab(k)
    const next = new URLSearchParams(params)
    next.set('tab', k)
    setParams(next, { replace: true })
  }

  const today = todayISO()
  const inboxCount = summary?.inbox || 0

  return (
    <div>
      <div className="page-header">
        <h1>Согласование платежей</h1>
        <div className="page-actions">
          {user?.role === 'admin' && settings && (
            <button className="btn btn-ghost" onClick={() => setSettingsOpen(true)}>
              ⚙ Правила
            </button>
          )}
          <button className="btn btn-primary" onClick={() => setForm({})}>
            + Новая заявка
          </button>
        </div>
      </div>

      {summary && (
        <div className="req-kpis">
          <div className="req-kpi">
            <div className="req-kpi-num">{summary.inbox}</div>
            <div className="req-kpi-label">ждут вашего решения · {formatMoney(summary.inbox_sum, false)}</div>
          </div>
          <div className={`req-kpi ${summary.overdue > 0 ? 'req-kpi-bad' : ''}`}>
            <div className="req-kpi-num">{summary.overdue}</div>
            <div className="req-kpi-label">утверждены, но срок прошёл · {formatMoney(summary.overdue_sum, false)}</div>
          </div>
          {['review', 'approval', 'approved'].map((s) => (
            <div key={s} className="req-kpi">
              <div className="req-kpi-num">{summary.by_status?.[s]?.count || 0}</div>
              <div className="req-kpi-label">
                {REQUEST_STATUS_LABELS[s].toLowerCase()} · {formatMoney(summary.by_status?.[s]?.sum || 0, false)}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="filters">
        <div className="req-tabs">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={`req-tab ${tab === t.key ? 'active' : ''}`}
              onClick={() => changeTab(t.key)}
            >
              {t.label}
              {t.key === 'inbox' && inboxCount > 0 && <span className="nav-badge">{inboxCount}</span>}
            </button>
          ))}
        </div>
        {tab === 'all' && (
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">Все статусы</option>
            {Object.entries(REQUEST_STATUS_LABELS).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
            <option value="done">Закрытые</option>
          </select>
        )}
      </div>

      {error && <div className="error">{error}</div>}

      <div className="table-wrap cards">
        <table>
          <thead>
            <tr>
              <th>№</th>
              <th>Срок</th>
              <th>За что</th>
              <th>Контрагент</th>
              <th>Фирма</th>
              <th className="num">Сумма</th>
              <th>Статус</th>
              <th>Автор</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={9} className="muted center">
                  {tab === 'inbox' ? 'Заявок, ждущих вашего решения, нет' : 'Заявок нет'}
                </td>
              </tr>
            )}
            {rows.map((r) => {
              const late = r.status === 'approved' && r.due_date < today
              const warn = (r.warnings || []).filter((w) => w.level === 'warn')
              return (
                <tr key={r.id} className={`row-click ${late ? 'row-alert' : ''}`} onClick={() => open(r.id)}>
                  <td data-label="№" className="muted">{r.id}</td>
                  <td data-label="Срок">{formatDateRu(r.due_date)}</td>
                  <td data-label="За что">
                    {r.title}
                    {r.article && <span className="hint-line">{r.article} · {METHOD_LABELS[r.method]}</span>}
                  </td>
                  <td data-label="Контрагент">{r.counterparty}</td>
                  <td data-label="Фирма" className="muted">{ORG_LABELS[r.organization] || r.organization}</td>
                  <td data-label="Сумма" className="num">{formatMoney(r.amount, r.currency)}</td>
                  <td data-label="Статус">
                    <span className={`badge badge-req-${r.status}`}>
                      {REQUEST_STATUS_LABELS[r.status] || r.status}
                    </span>
                  </td>
                  <td data-label="Автор" className="muted">{r.creator_name || '—'}</td>
                  <td className="req-flags">
                    {warn.length > 0 && (
                      <span title={warn.map((w) => w.text).join('\n')}>⚠️</span>
                    )}
                    {r.files_count > 0 && <span title={`Файлов: ${r.files_count}`}>📎</span>}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {openId && (
        <RequestDetail
          id={openId}
          onClose={() => open(null)}
          onChanged={load}
          onEdit={(r) => setForm(r)}
        />
      )}

      {form && (
        <RequestForm
          initial={form.id ? form : null}
          onClose={() => setForm(null)}
          onSaved={async (saved) => {
            await load()
            if (saved?.id) open(saved.id)
          }}
        />
      )}

      {settingsOpen && settings && (
        <SettingsModal
          initial={settings}
          onClose={() => setSettingsOpen(false)}
          onSaved={(s) => {
            setSettings(s)
            setSettingsOpen(false)
            load()
          }}
        />
      )}
    </div>
  )
}

/* Правила согласования для администратора: до какой суммы бухгалтер
   утверждает сам, какой остаток денег не трогаем, нужен ли файл-основание. */
function SettingsModal({ initial, onClose, onSaved }) {
  // initial — правила по фирмам: { all: {...}, hygiene: {...} }
  const [org, setOrgSel] = useState('all')
  const [form, setForm] = useState(pick(initial, 'all'))
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)

  function changeOrg(v) {
    setOrgSel(v)
    setForm(pick(initial, v))
  }

  async function submit(e) {
    e.preventDefault()
    setSaving(true)
    setError(null)
    try {
      const saved = await api.approvalsSettingsSave({
        organization: org,
        accountant_limit: String(form.accountant_limit || 0),
        cash_cushion: String(form.cash_cushion || 0),
        require_basis_file: form.require_basis_file,
      })
      onSaved(saved)
    } catch (err) {
      setError(err.message)
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Правила согласования</h2>
        <form onSubmit={submit}>
          <label>
            Для какой фирмы
            <select value={org} onChange={(e) => changeOrg(e.target.value)}>
              <option value="all">Все фирмы (по умолчанию)</option>
              {Object.entries(ORG_LABELS).map(([k, v]) => (
                <option key={k} value={k}>{v}{initial[k] ? ' · свои правила' : ''}</option>
              ))}
            </select>
          </label>
          <label>
            Бухгалтер утверждает сам до суммы (KGS)
            <input type="number" min="0" step="1" value={form.accountant_limit}
              onChange={(e) => setForm((f) => ({ ...f, accountant_limit: e.target.value }))} />
            <span className="hint-line">0 — бухгалтер только проверяет, утверждает руководитель</span>
          </label>
          <label>
            Неснижаемый остаток денег (KGS)
            <input type="number" min="0" step="1" value={form.cash_cushion}
              onChange={(e) => setForm((f) => ({ ...f, cash_cushion: e.target.value }))} />
            <span className="hint-line">Если после оплаты остаётся меньше — заявка подсветится</span>
          </label>
          <label className="check-line">
            <input type="checkbox" checked={form.require_basis_file}
              onChange={(e) => setForm((f) => ({ ...f, require_basis_file: e.target.checked }))} />
            Без файла-основания заявку отправить нельзя (кроме кассы, зарплаты и налогов)
          </label>
          {error && <div className="error">{error}</div>}
          <div className="modal-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>Отмена</button>
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? 'Сохранение…' : 'Сохранить'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function pick(all, org) {
  const base = all[org] || all.all || {}
  return {
    accountant_limit: base.accountant_limit ?? 0,
    cash_cushion: base.cash_cushion ?? 0,
    require_basis_file: base.require_basis_file ?? true,
  }
}

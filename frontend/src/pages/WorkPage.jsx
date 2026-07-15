import { useEffect, useState } from 'react'
import { api } from '../api'
import { formatMoney } from '../utils'

const shortMoney = (v) => {
  const n = Number(v || 0)
  if (Math.abs(n) >= 1e6) return (n / 1e6).toLocaleString('ru-RU', { maximumFractionDigits: 1 }) + ' млн'
  if (Math.abs(n) >= 1e3) return (n / 1e3).toLocaleString('ru-RU', { maximumFractionDigits: 0 }) + ' тыс'
  return n.toLocaleString('ru-RU')
}
const fmtDate = (iso) => (iso ? iso.split('-').reverse().join('.') : '—')

const KIND_LABEL = {
  visit: '🚶 Визит', call: '📞 Звонок', promise: '🤝 Обещал оплату',
  order: '🛒 Заказ', note: '📝 Заметка',
}

export default function WorkPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [agent, setAgent] = useState(null) // выбранный агент (для админа)
  const [modal, setModal] = useState(null) // { client }

  async function load(a) {
    setError(null)
    try {
      const res = await api.agentWork(a)
      setData(res)
      setAgent(res.agent)
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="center muted">Загрузка…</div>

  const plan = data.plan

  return (
    <div className="work">
      <div className="page-header">
        <h1>Мой день</h1>
        {!data.is_fixed && data.agents.length > 0 && (
          <select
            className="filter-select"
            value={agent || ''}
            onChange={(e) => load(e.target.value)}
          >
            {data.agents.map((a) => (
              <option key={a} value={a}>{a}</option>
            ))}
          </select>
        )}
      </div>

      {plan && (
        <div className="summary-bar">
          <div className="summary-card summary-in">
            <span className="summary-label">Продажи · месяц</span>
            <span className="summary-value">{formatMoney(plan.month_revenue)}</span>
            {plan.target != null && (
              <span className="dash-delta muted">
                план {shortMoney(plan.target)} · {plan.target_pct ?? 0}%
              </span>
            )}
          </div>
          {plan.forecast > 0 && (
            <div className="summary-card">
              <span className="summary-label">Прогноз к концу мес.</span>
              <span className="summary-value">{formatMoney(plan.forecast)}</span>
            </div>
          )}
        </div>
      )}

      {data.promises.length > 0 && (
        <div className="chart-card work-promises">
          <div className="work-head"><span>🤝 Обещания оплаты</span></div>
          {data.promises.map((p, i) => (
            <div key={i} className={`work-promise ${p.overdue ? 'is-overdue' : ''}`}>
              <span className="work-cli">{p.client}</span>
              <span className="work-date">{fmtDate(p.promise_date)}{p.overdue ? ' · просрочено' : ''}</span>
            </div>
          ))}
        </div>
      )}

      <WorkSection
        title="🔴 Собрать долг"
        empty="Долгов нет 🎉"
        rows={data.debtors}
        onAct={(c) => setModal({ client: c })}
        render={(d) => (
          <>
            <span className="num neg">{shortMoney(d.debt)}</span>
            <span className="work-sub">
              {d.days_no_payment != null ? `${d.days_no_payment} дн. без оплат` : 'не платил'}
            </span>
          </>
        )}
      />

      <WorkSection
        title={`📅 Пора посетить · не были ≥ ${data.cadence_days} дн.`}
        empty="Все клиенты посещены вовремя"
        rows={data.to_visit}
        onAct={(c) => setModal({ client: c })}
        render={(v) => (
          <>
            <span className="num">{v.days} дн.</span>
            <span className="work-sub">
              посл. заказ {fmtDate(v.last_order)}
              {v.debt > 0 ? ` · долг ${shortMoney(v.debt)}` : ''}
            </span>
          </>
        )}
      />

      <WorkSection
        title={`😴 Разбудить · молчат ≥ ${data.sleeping_days} дн.`}
        empty="Спящих нет"
        rows={data.sleeping}
        onAct={(c) => setModal({ client: c })}
        render={(s) => (
          <>
            <span className="num">{s.days} дн.</span>
            <span className="work-sub">приносил {shortMoney(s.revenue)}</span>
          </>
        )}
      />

      {modal && (
        <ActivityModal
          client={modal.client}
          agent={data.is_fixed ? null : agent}
          onClose={() => setModal(null)}
          onSaved={() => { setModal(null); load(agent) }}
        />
      )}
    </div>
  )
}

function WorkSection({ title, empty, rows, render, onAct }) {
  return (
    <div className="chart-card work-section">
      <div className="work-head"><span>{title}</span><span className="muted">{rows.length}</span></div>
      {rows.length === 0 && <div className="muted work-empty">{empty}</div>}
      {rows.map((r) => (
        <div key={r.name} className="work-row">
          <div className="work-row-main">
            <div className="work-cli">{r.name}</div>
            <div className="work-vals">{render(r)}</div>
          </div>
          {r.last_activity && (
            <div className="work-last">
              {KIND_LABEL[r.last_activity.kind] || r.last_activity.kind}
              {r.last_activity.promise_date ? ` до ${fmtDate(r.last_activity.promise_date)}` : ''}
              {r.last_activity.note ? ` · ${r.last_activity.note}` : ''}
            </div>
          )}
          <button className="btn btn-sm work-act" onClick={() => onAct(r.name)}>Отметить</button>
        </div>
      ))}
    </div>
  )
}

function ActivityModal({ client, agent, onClose, onSaved }) {
  const [kind, setKind] = useState('visit')
  const [note, setNote] = useState('')
  const [promiseDate, setPromiseDate] = useState('')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState(null)

  async function save() {
    setSaving(true)
    setErr(null)
    try {
      await api.addActivity({
        client,
        kind,
        note: note.trim() || null,
        promise_date: kind === 'promise' && promiseDate ? promiseDate : null,
        ...(agent ? { agent } : {}),
      })
      onSaved()
    } catch (e) {
      setErr(e.message)
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>{client}</h2>
        <div className="work-kinds">
          {Object.entries(KIND_LABEL).map(([k, label]) => (
            <button
              key={k}
              className={`work-kind ${kind === k ? 'active' : ''}`}
              onClick={() => setKind(k)}
            >
              {label}
            </button>
          ))}
        </div>
        {kind === 'promise' && (
          <label className="work-field">
            <span>Дата обещанной оплаты</span>
            <input type="date" value={promiseDate} onChange={(e) => setPromiseDate(e.target.value)} />
          </label>
        )}
        <label className="work-field">
          <span>Комментарий</span>
          <textarea
            rows={2}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Что сделали / договорились…"
          />
        </label>
        {err && <div className="error">{err}</div>}
        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onClose}>Отмена</button>
          <button className="btn btn-primary" disabled={saving} onClick={save}>
            {saving ? 'Сохранение…' : 'Записать'}
          </button>
        </div>
      </div>
    </div>
  )
}

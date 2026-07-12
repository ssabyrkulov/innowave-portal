import { useEffect, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'
import { formatMoney } from '../utils'

function shortMoney(v) {
  const n = Number(v || 0)
  if (Math.abs(n) >= 1e6) return (n / 1e6).toLocaleString('ru-RU', { maximumFractionDigits: 1 }) + ' млн'
  if (Math.abs(n) >= 1e3) return (n / 1e3).toLocaleString('ru-RU', { maximumFractionDigits: 0 }) + ' тыс'
  return n.toLocaleString('ru-RU', { maximumFractionDigits: 0 })
}

const MONTH_SHORT = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
const monthLabel = (key) => {
  const [y, m] = key.split('-')
  return `${MONTH_SHORT[Number(m) - 1]} ${y.slice(2)}`
}
const fmtDate = (iso) => (iso ? iso.split('-').reverse().join('.') : '—')

function Sparkline({ monthly }) {
  const W = 150
  const H = 36
  const max = Math.max(...monthly.map((p) => p.revenue), 1)
  const n = monthly.length || 1
  const barW = Math.max(W / n - 2, 2)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="sparkline" role="img" aria-label="Выручка по месяцам">
      {monthly.map((p, i) => {
        const h = Math.max((p.revenue / max) * (H - 4), 1)
        return (
          <rect key={p.month} x={i * (W / n) + 1} y={H - h} width={barW} height={h} rx="1.5" className="spark-bar">
            <title>{`${monthLabel(p.month)}: ${formatMoney(p.revenue)}`}</title>
          </rect>
        )
      })}
    </svg>
  )
}

export default function AgentsPage() {
  const { can } = useAuth()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [open, setOpen] = useState(null)

  async function load() {
    try {
      setData(await api.agentsSummary())
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => {
    load()
  }, [])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="center muted">Загрузка…</div>

  async function saveTarget(agent, value) {
    const amount = Number(String(value).replace(/\s/g, '')) || 0
    try {
      await api.setAgentTarget(agent, data.current_month, amount)
      await load()
    } catch (e) {
      setError(e.message)
    }
  }

  return (
    <div>
      <div className="page-header">
        <h1>Агенты</h1>
        <span className="muted">текущий месяц: {monthLabel(data.current_month)}</span>
      </div>

      <div className="summary-bar">
        <div className="summary-card">
          <span className="summary-label">Агентов</span>
          <span className="summary-value">{data.agents.length}</span>
        </div>
        <div className="summary-card summary-in">
          <span className="summary-label">Выручка агентов за {monthLabel(data.current_month)}</span>
          <span className="summary-value">{formatMoney(data.total_month_revenue)}</span>
        </div>
        <div className="summary-card">
          <span className="summary-label">Выручка агентов за всё время</span>
          <span className="summary-value">{formatMoney(data.total_revenue)}</span>
        </div>
        {data.no_agent && (
          <div className="summary-card">
            <span className="summary-label">Продажи без агента</span>
            <span className="summary-value">{formatMoney(data.no_agent.revenue)}</span>
          </div>
        )}
      </div>

      <div className="table-wrap compact">
        <table>
          <thead>
            <tr>
              <th>Агент</th>
              <th className="num">Тек. месяц</th>
              <th>План / выполнение</th>
              <th className="num hide-mobile" title="К прошлому месяцу">Δ к пр. мес</th>
              <th className="num hide-mobile">Долг клиентов</th>
              <th className="hide-mobile">Динамика (12 мес)</th>
            </tr>
          </thead>
          <tbody>
            {data.agents.map((a) => (
              <AgentRow
                key={a.name}
                agent={a}
                canEdit={can.editPayments}
                open={open === a.name}
                onToggle={() => setOpen(open === a.name ? null : a.name)}
                onSaveTarget={saveTarget}
              />
            ))}
            {data.agents.length === 0 && (
              <tr>
                <td colSpan={6} className="muted center">
                  В продажах нет данных об агентах
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="muted agents-hint">
        Клик по агенту — его должники, «уснувшие» клиенты и топ покупателей.
        План на месяц {can.editPayments ? 'вводится в колонке «План»' : 'задаёт администратор или бухгалтер'}.
      </p>
    </div>
  )
}

function delta(cur, prev) {
  if (!prev) return null
  return ((cur - prev) / prev) * 100
}

function AgentRow({ agent, canEdit, open, onToggle, onSaveTarget }) {
  const d = delta(agent.month_revenue, agent.prev_month_revenue)
  const pct = agent.target ? Math.min((agent.month_revenue / agent.target) * 100, 100) : 0
  const onTrack = agent.target ? agent.forecast >= agent.target : null

  return (
    <>
      <tr className="agent-row" onClick={onToggle}>
        <td>
          <span className="agent-toggle">{open ? '▾' : '▸'}</span> {agent.name}
          <div className="muted agent-sub">
            {agent.clients} клиентов · посл. отгрузка {fmtDate(agent.last_shipment)}
          </div>
        </td>
        <td className="num">
          {shortMoney(agent.month_revenue)}
          {agent.forecast > 0 && (
            <div className="muted agent-sub" title="Прогноз до конца месяца по текущему темпу">
              ≈{shortMoney(agent.forecast)} к концу мес.
            </div>
          )}
        </td>
        <td onClick={(e) => e.stopPropagation()}>
          {canEdit ? (
            <input
              className="target-input"
              type="text"
              inputMode="numeric"
              placeholder="план, сом"
              defaultValue={agent.target ?? ''}
              onBlur={(e) => {
                const v = Number(String(e.target.value).replace(/\s/g, '')) || 0
                if (v !== (agent.target ?? 0)) onSaveTarget(agent.name, v)
              }}
              onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
            />
          ) : (
            <span>{agent.target ? shortMoney(agent.target) : '—'}</span>
          )}
          {agent.target ? (
            <div className="target-track" title={`${agent.target_pct}% плана`}>
              <div
                className={`target-fill ${onTrack ? 'target-ok' : 'target-behind'}`}
                style={{ width: `${pct}%` }}
              />
              <span className="target-pct">{agent.target_pct}%</span>
            </div>
          ) : null}
        </td>
        <td className={`num hide-mobile ${d == null ? 'muted' : d >= 0 ? 'pos' : 'neg'}`}>
          {d == null ? '—' : `${d >= 0 ? '+' : ''}${d.toFixed(0)}%`}
        </td>
        <td className="num hide-mobile">
          {agent.debt > 0 ? (
            <span className="neg">
              {shortMoney(agent.debt)}
              <span className="muted"> ({agent.debtors_count})</span>
            </span>
          ) : (
            <span className="muted">—</span>
          )}
        </td>
        <td className="hide-mobile">
          <Sparkline monthly={agent.monthly} />
        </td>
      </tr>
      {open && (
        <tr className="agent-detail">
          <td colSpan={6}>
            <div className="agent-panels">
              <div className="agent-panel">
                <div className="agent-clients-title">
                  📞 Должники — кому напомнить ({agent.debtors_count})
                </div>
                {agent.debtors.length === 0 && <div className="muted">Долгов нет 🎉</div>}
                {agent.debtors.map((c) => (
                  <div key={c.name} className="agent-client-row">
                    <span className="agent-client-name">{c.name}</span>
                    <span className="num neg">{shortMoney(c.debt)}</span>
                    <span className="muted num">
                      {c.days_no_payment != null
                        ? `${c.days_no_payment} дн. без оплат`
                        : 'не платил'}
                    </span>
                  </div>
                ))}
              </div>
              <div className="agent-panel">
                <div className="agent-clients-title">
                  😴 Уснувшие клиенты — кого навестить ({agent.sleeping.length})
                </div>
                {agent.sleeping.length === 0 && <div className="muted">Таких нет</div>}
                {agent.sleeping.map((c) => (
                  <div key={c.name} className="agent-client-row">
                    <span className="agent-client-name">{c.name}</span>
                    <span className="num">{shortMoney(c.revenue)}</span>
                    <span className="muted num">{c.days} дн. без покупок</span>
                  </div>
                ))}
              </div>
              <div className="agent-panel">
                <div className="agent-clients-title">🏆 Топ клиентов</div>
                {agent.top_clients.map((c) => (
                  <div key={c.name} className="agent-client-row">
                    <span className="agent-client-name">{c.name}</span>
                    <span className="num">{formatMoney(c.revenue)}</span>
                    <span className={`num ${c.debt > 0 ? 'neg' : 'muted'}`}>
                      {c.debt > 0 ? `долг ${shortMoney(c.debt)}` : 'без долга'}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

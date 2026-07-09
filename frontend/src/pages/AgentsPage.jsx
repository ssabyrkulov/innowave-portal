import { useEffect, useState } from 'react'
import { api } from '../api'
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

function Sparkline({ monthly }) {
  const W = 180
  const H = 40
  const max = Math.max(...monthly.map((p) => p.revenue), 1)
  const n = monthly.length || 1
  const barW = Math.max(W / n - 2, 2)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="sparkline" role="img" aria-label="Выручка по месяцам">
      {monthly.map((p, i) => {
        const h = Math.max((p.revenue / max) * (H - 4), 1)
        return (
          <rect
            key={p.month}
            x={i * (W / n) + 1}
            y={H - h}
            width={barW}
            height={h}
            rx="1.5"
            className="spark-bar"
          >
            <title>{`${monthLabel(p.month)}: ${formatMoney(p.revenue)}`}</title>
          </rect>
        )
      })}
    </svg>
  )
}

export default function AgentsPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [open, setOpen] = useState(null)

  useEffect(() => {
    api.agentsSummary().then(setData).catch((e) => setError(e.message))
  }, [])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="center muted">Загрузка…</div>

  const monthsHint = data.months.length
    ? `${monthLabel(data.months[0])} — ${monthLabel(data.months[data.months.length - 1])}`
    : ''

  return (
    <div>
      <div className="page-header">
        <h1>Агенты</h1>
      </div>

      <div className="summary-bar">
        <div className="summary-card">
          <span className="summary-label">Агентов</span>
          <span className="summary-value">{data.agents.length}</span>
        </div>
        <div className="summary-card summary-in">
          <span className="summary-label">Выручка через агентов</span>
          <span className="summary-value">{formatMoney(data.total_revenue)}</span>
        </div>
        {data.no_agent && (
          <div className="summary-card">
            <span className="summary-label">Продажи без агента</span>
            <span className="summary-value">{formatMoney(data.no_agent.revenue)}</span>
          </div>
        )}
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Агент</th>
              <th className="num">Выручка</th>
              <th className="num">Накладных</th>
              <th className="num">Клиентов</th>
              <th className="num">Средняя накладная</th>
              <th className="num">Долг клиентов</th>
              <th title={monthsHint}>Динамика (12 мес)</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.agents.map((a) => (
              <AgentRow
                key={a.name}
                agent={a}
                open={open === a.name}
                onToggle={() => setOpen(open === a.name ? null : a.name)}
              />
            ))}
            {data.agents.length === 0 && (
              <tr>
                <td colSpan={8} className="muted center">
                  В продажах нет данных об агентах — загрузите выгрузку с
                  колонкой агента или «ОтветственныйФИО»
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function AgentRow({ agent, open, onToggle }) {
  return (
    <>
      <tr className="agent-row" onClick={onToggle}>
        <td>
          <span className="agent-toggle">{open ? '▾' : '▸'}</span> {agent.name}
        </td>
        <td className="num">{formatMoney(agent.revenue)}</td>
        <td className="num">{agent.docs.toLocaleString('ru-RU')}</td>
        <td className="num">{agent.clients.toLocaleString('ru-RU')}</td>
        <td className="num">{shortMoney(agent.avg_doc)}</td>
        <td className="num">
          {agent.debt > 0 ? (
            <span className="neg">
              {shortMoney(agent.debt)}
              <span className="muted"> ({agent.debtors})</span>
            </span>
          ) : (
            <span className="muted">—</span>
          )}
        </td>
        <td>
          <Sparkline monthly={agent.monthly} />
        </td>
        <td className="muted">
          {agent.last_shipment
            ? agent.last_shipment.split('-').reverse().join('.')
            : ''}
        </td>
      </tr>
      {open && (
        <tr className="agent-detail">
          <td colSpan={8}>
            <div className="agent-clients">
              <div className="agent-clients-title">Топ клиентов агента</div>
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
          </td>
        </tr>
      )}
    </>
  )
}

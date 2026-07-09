import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
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
const fmtDate = (iso) => {
  const [, m, d] = iso.split('-')
  return `${d}.${m}`
}

function deltaPct(cur, prev) {
  if (!prev) return null
  return ((cur - prev) / prev) * 100
}

function Delta({ cur, prev }) {
  const d = deltaPct(cur, prev)
  if (d == null) return null
  return (
    <span className={`dash-delta ${d >= 0 ? 'pos' : 'neg'}`}>
      {d >= 0 ? '↑' : '↓'} {Math.abs(d).toFixed(0)}% к пр. мес
    </span>
  )
}

function MiniChart({ monthly, height = 64, width = 400 }) {
  const W = width
  const H = height
  const max = Math.max(...monthly.map((p) => p.revenue), 1)
  const n = monthly.length || 1
  const barW = Math.max(W / n - 3, 3)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="dash-chart" role="img" aria-label="Продажи по месяцам">
      {monthly.map((p, i) => {
        const h = Math.max((p.revenue / max) * (H - 16), 1.5)
        const isCur = i === n - 1
        return (
          <g key={p.month}>
            <rect
              x={i * (W / n) + 1.5}
              y={H - 12 - h}
              width={barW}
              height={h}
              rx="2.5"
              className={isCur ? 'dash-bar dash-bar-cur' : 'dash-bar'}
            >
              <title>{`${monthLabel(p.month)}: ${formatMoney(p.revenue)}`}</title>
            </rect>
            {(i % 2 === 0 || isCur) && (
              <text x={i * (W / n) + 1.5 + barW / 2} y={H - 2} className="dash-tick" textAnchor="middle">
                {monthLabel(p.month).split(' ')[0]}
              </text>
            )}
          </g>
        )
      })}
    </svg>
  )
}

export default function DashboardPage() {
  const { user } = useAuth()
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [isMobile, setIsMobile] = useState(
    () => window.matchMedia('(max-width: 840px)').matches
  )

  useEffect(() => {
    const mq = window.matchMedia('(max-width: 840px)')
    const fn = (e) => setIsMobile(e.matches)
    mq.addEventListener('change', fn)
    return () => mq.removeEventListener('change', fn)
  }, [])

  useEffect(() => {
    api.dashboard().then(setData).catch((e) => setError(e.message))
  }, [])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="center muted">Загрузка…</div>

  return isMobile ? (
    <MobileDash data={data} user={user} />
  ) : (
    <DesktopDash data={data} user={user} />
  )
}

/* ============================== ДЕСКТОП ============================== */

function DesktopDash({ data }) {
  const hasAlerts = data.checks.critical + data.checks.warning > 0
  return (
    <div>
      <div className="page-header">
        <h1>Главная</h1>
        <span className="muted">
          {new Date(data.today + 'T00:00:00').toLocaleDateString('ru-RU', {
            weekday: 'long', day: 'numeric', month: 'long',
          })}
        </span>
      </div>

      <div className="summary-bar">
        <div className="summary-card summary-in">
          <span className="summary-label">Поступило от клиентов · {monthLabel(data.current_month)}</span>
          <span className="summary-value">{formatMoney(data.money.month_in)}</span>
          <Delta cur={data.money.month_in} prev={data.money.prev_month_in} />
        </div>
        <div className="summary-card">
          <span className="summary-label">Продажи · {monthLabel(data.current_month)}</span>
          <span className="summary-value">{formatMoney(data.sales.month)}</span>
          <Delta cur={data.sales.month} prev={data.sales.prev_month} />
        </div>
        <Link to="/debt" className="summary-card summary-link summary-out">
          <span className="summary-label">Долг клиентов</span>
          <span className="summary-value">{formatMoney(data.debt.total)}</span>
          <span className="dash-delta muted">{data.debt.debtors} должников →</span>
        </Link>
        <Link to="/checks" className={`summary-card summary-link ${data.checks.critical ? 'card-alert' : ''}`}>
          <span className="summary-label">Сигналы контроля</span>
          <span className="summary-value">
            {hasAlerts ? (
              <>
                {data.checks.critical > 0 && <span className="neg">{data.checks.critical} крит</span>}
                {data.checks.critical > 0 && data.checks.warning > 0 && ' · '}
                {data.checks.warning > 0 && <span className="dash-warn">{data.checks.warning}</span>}
              </>
            ) : (
              <span className="pos">чисто ✓</span>
            )}
          </span>
          <span className="dash-delta muted">открыть контроль →</span>
        </Link>
      </div>

      <div className="dash-grid">
        <div className="chart-card dash-span2">
          <div className="dash-card-head">
            <h2 className="chart-title">Продажи по месяцам</h2>
            <Link className="dash-more" to="/analytics">аналитика →</Link>
          </div>
          <MiniChart monthly={data.sales.monthly} height={150} width={1060} />
        </div>

        <div className="chart-card">
          <div className="dash-card-head">
            <h2 className="chart-title">Ближайшие платежи · 30 дней</h2>
            <Link className="dash-more" to="/calendar">календарь →</Link>
          </div>
          <div className="dash-inout">
            <span className="pos">↓ {shortMoney(data.payments.in_30)}</span>
            <span className="neg">↑ {shortMoney(data.payments.out_30)}</span>
            {data.payments.overdue_count > 0 && (
              <span className="badge badge-overdue">{data.payments.overdue_count} просрочено</span>
            )}
          </div>
          <div className="dash-list">
            {data.payments.upcoming.length === 0 && (
              <div className="muted">Запланированных платежей нет</div>
            )}
            {data.payments.upcoming.map((p, i) => (
              <div key={i} className="dash-row">
                <span className="dash-row-date">{fmtDate(p.due_date)}</span>
                <span className="dash-row-name">{p.title}</span>
                <span className={`num ${p.direction === 'incoming' ? 'pos' : 'neg'}`}>
                  {p.direction === 'incoming' ? '+' : '−'}{shortMoney(p.amount)}
                </span>
              </div>
            ))}
          </div>
        </div>

        <div className="chart-card">
          <div className="dash-card-head">
            <h2 className="chart-title">Топ должников</h2>
            <Link className="dash-more" to="/debt">дебиторка →</Link>
          </div>
          <div className="dash-list">
            {data.debt.top.map((d) => (
              <div key={d.name} className="dash-row">
                <span className="dash-row-name">{d.name}</span>
                <span className="num neg">{shortMoney(d.debt)}</span>
              </div>
            ))}
            {data.debt.top.length === 0 && <div className="muted">Долгов нет 🎉</div>}
          </div>
        </div>
      </div>
    </div>
  )
}

/* ============================== МОБИЛЬНЫЙ ============================== */

function greeting() {
  const h = new Date().getHours()
  if (h < 5) return 'Доброй ночи'
  if (h < 12) return 'Доброе утро'
  if (h < 18) return 'Добрый день'
  return 'Добрый вечер'
}

function MobileDash({ data, user }) {
  const firstName = user.full_name.split(/\s+/)[0]
  return (
    <div className="mdash">
      <div className="mdash-head">
        <div className="mdash-greet">{greeting()}, {firstName} 👋</div>
        <div className="mdash-date">
          {new Date(data.today + 'T00:00:00').toLocaleDateString('ru-RU', {
            weekday: 'long', day: 'numeric', month: 'long',
          })}
        </div>
      </div>

      {data.checks.critical > 0 && (
        <Link to="/checks" className="mdash-alert">
          ⚠ {data.checks.critical} критичных сигнала в контроле — посмотреть →
        </Link>
      )}

      <div className="mdash-carousel">
        <div className="mdash-kpi mdash-kpi-money">
          <div className="mdash-kpi-label">Поступило · {monthLabel(data.current_month)}</div>
          <div className="mdash-kpi-value">{shortMoney(data.money.month_in)}</div>
          <Delta cur={data.money.month_in} prev={data.money.prev_month_in} />
        </div>
        <div className="mdash-kpi">
          <div className="mdash-kpi-label">Продажи · {monthLabel(data.current_month)}</div>
          <div className="mdash-kpi-value">{shortMoney(data.sales.month)}</div>
          <Delta cur={data.sales.month} prev={data.sales.prev_month} />
        </div>
        <Link to="/debt" className="mdash-kpi mdash-kpi-debt">
          <div className="mdash-kpi-label">Долг клиентов</div>
          <div className="mdash-kpi-value">{shortMoney(data.debt.total)}</div>
          <div className="mdash-kpi-sub">{data.debt.debtors} должников</div>
        </Link>
        <Link to="/calendar" className="mdash-kpi">
          <div className="mdash-kpi-label">Платежи · 30 дн</div>
          <div className="mdash-kpi-value neg">↑ {shortMoney(data.payments.out_30)}</div>
          <div className="mdash-kpi-sub pos">↓ {shortMoney(data.payments.in_30)}</div>
        </Link>
      </div>

      <div className="mdash-section">
        <div className="mdash-section-head">
          <span>Ближайшие платежи</span>
          <Link to="/calendar">все →</Link>
        </div>
        {data.payments.upcoming.slice(0, 4).map((p, i) => (
          <div key={i} className="mdash-item">
            <div className="mdash-item-date">{fmtDate(p.due_date)}</div>
            <div className="mdash-item-name">{p.title}</div>
            <div className={`num ${p.direction === 'incoming' ? 'pos' : 'neg'}`}>
              {p.direction === 'incoming' ? '+' : '−'}{shortMoney(p.amount)}
            </div>
          </div>
        ))}
        {data.payments.upcoming.length === 0 && (
          <div className="muted mdash-empty">Запланированных платежей нет</div>
        )}
      </div>

      <div className="mdash-section">
        <div className="mdash-section-head">
          <span>Топ должников</span>
          <Link to="/debt">все →</Link>
        </div>
        {data.debt.top.slice(0, 4).map((d) => (
          <div key={d.name} className="mdash-item">
            <div className="mdash-item-name">{d.name}</div>
            <div className="num neg">{shortMoney(d.debt)}</div>
          </div>
        ))}
      </div>

      <div className="mdash-section">
        <div className="mdash-section-head">
          <span>Продажи · 12 мес</span>
          <Link to="/analytics">аналитика →</Link>
        </div>
        <MiniChart monthly={data.sales.monthly} height={80} />
      </div>
    </div>
  )
}

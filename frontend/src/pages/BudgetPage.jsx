import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'
import { formatMoney } from '../utils'

const MONTHS = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль',
  'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']

function currentPeriod() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

function shiftPeriod(period, delta) {
  let [y, m] = period.split('-').map(Number)
  m += delta
  if (m < 1) { m = 12; y-- }
  if (m > 12) { m = 1; y++ }
  return `${y}-${String(m).padStart(2, '0')}`
}

function periodLabel(period) {
  const [y, m] = period.split('-')
  return `${MONTHS[Number(m) - 1]} ${y}`
}

// Поле плана: не перерисовывается под курсором, коммитит по Enter/уходу.
function PlanInput({ value, onCommit, disabled }) {
  const ref = useRef(null)
  return (
    <input
      ref={ref}
      className="bdds-input"
      type="number"
      defaultValue={value || ''}
      placeholder="0"
      disabled={disabled}
      onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
      onBlur={(e) => {
        const v = Number(String(e.target.value).replace(/\s/g, '')) || 0
        if (v !== (value || 0)) onCommit(v)
      }}
    />
  )
}

export default function BudgetPage() {
  const { can } = useAuth()
  const [period, setPeriod] = useState(currentPeriod)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [adding, setAdding] = useState(null) // 'in' | 'out' | null
  const [newArticle, setNewArticle] = useState('')
  const [newAmount, setNewAmount] = useState('')

  async function load() {
    setError(null)
    try {
      setData(await api.budgetPlanFact(period))
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [period])

  async function savePlan(direction, article, amount) {
    try {
      await api.budgetUpsert({ period, direction, article, amount })
      await load()
    } catch (e) {
      setError(e.message)
    }
  }

  async function addRow(direction) {
    const a = newArticle.trim()
    if (!a) return
    await savePlan(direction, a, Number(newAmount) || 0)
    setAdding(null)
    setNewArticle('')
    setNewAmount('')
  }

  const t = data?.totals

  return (
    <div className="bdds">
      <div className="page-header">
        <h1>БДДС · план-факт</h1>
        <div className="bdds-monthnav">
          <button className="btn btn-ghost" onClick={() => setPeriod((p) => shiftPeriod(p, -1))}>←</button>
          <span className="bdds-month">{periodLabel(period)}</span>
          <button className="btn btn-ghost" onClick={() => setPeriod((p) => shiftPeriod(p, 1))}>→</button>
        </div>
      </div>

      <div className="note-readonly">
        План вводится по статьям вручную; факт подтягивается из 1С (поступления и
        расходы) и сводится по той же статье. Только для админа и бухгалтера.
      </div>

      {error && <div className="error">{error}</div>}
      {!data ? (
        <div className="center muted">Загрузка…</div>
      ) : (
        <>
          {t && (
            <div className="summary-bar">
              <div className="summary-card summary-in">
                <span className="summary-label">Поступления · факт</span>
                <span className="summary-value">{formatMoney(t.in_fact)}</span>
                <span className="dash-delta muted">план {formatMoney(t.in_plan)}</span>
              </div>
              <div className="summary-card summary-out">
                <span className="summary-label">Выплаты · факт</span>
                <span className="summary-value">{formatMoney(t.out_fact)}</span>
                <span className="dash-delta muted">план {formatMoney(t.out_plan)}</span>
              </div>
              <div className={`summary-card ${t.flow_fact >= 0 ? 'summary-in' : 'summary-out'}`}>
                <span className="summary-label">Чистый поток · факт</span>
                <span className="summary-value">{formatMoney(t.flow_fact)}</span>
                <span className="dash-delta muted">план {formatMoney(t.flow_plan)}</span>
              </div>
            </div>
          )}

          <BudgetSection
            title="Поступления"
            direction="in"
            rows={data.incoming}
            canEdit={can.editPayments}
            onSave={savePlan}
            adding={adding === 'in'}
            onAddClick={() => setAdding(adding === 'in' ? null : 'in')}
            newArticle={newArticle}
            newAmount={newAmount}
            setNewArticle={setNewArticle}
            setNewAmount={setNewAmount}
            onAdd={() => addRow('in')}
          />

          <BudgetSection
            title="Выплаты"
            direction="out"
            rows={data.outgoing}
            canEdit={can.editPayments}
            onSave={savePlan}
            adding={adding === 'out'}
            onAddClick={() => setAdding(adding === 'out' ? null : 'out')}
            newArticle={newArticle}
            newAmount={newAmount}
            setNewArticle={setNewArticle}
            setNewAmount={setNewAmount}
            onAdd={() => addRow('out')}
          />
        </>
      )}
    </div>
  )
}

function BudgetSection({
  title, direction, rows, canEdit, onSave, adding, onAddClick,
  newArticle, newAmount, setNewArticle, setNewAmount, onAdd,
}) {
  const sum = useMemo(
    () => rows.reduce((a, r) => ({ plan: a.plan + r.plan, fact: a.fact + r.fact }), { plan: 0, fact: 0 }),
    [rows]
  )
  // для выплат перерасход (факт>план) — плохо (красный); для поступлений — наоборот
  const varClass = (v) => {
    if (Math.abs(v) < 0.5) return 'muted'
    const good = direction === 'in' ? v > 0 : v < 0
    return good ? 'pos' : 'neg'
  }

  return (
    <div className="chart-card bdds-section">
      <div className="bdds-head">
        <h2 className="chart-title">{title}</h2>
        {canEdit && (
          <button className="btn btn-ghost btn-sm" onClick={onAddClick}>
            {adding ? 'Отмена' : '+ статья'}
          </button>
        )}
      </div>

      <div className="table-wrap cards">
        <table>
          <thead>
            <tr>
              <th>Статья</th>
              <th className="num">План</th>
              <th className="num">Факт</th>
              <th className="num">Отклонение</th>
              <th className="num">%</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && !adding && (
              <tr><td colSpan={5} className="muted center">Нет данных за месяц</td></tr>
            )}
            {rows.map((r) => (
              <tr key={r.article}>
                <td data-label="Статья">{r.article}</td>
                <td className="num" data-label="План">
                  {canEdit ? (
                    <PlanInput value={r.plan} onCommit={(v) => onSave(direction, r.article, v)} />
                  ) : (
                    formatMoney(r.plan, false)
                  )}
                </td>
                <td className="num" data-label="Факт">{formatMoney(r.fact, false)}</td>
                <td className={`num ${varClass(r.variance)}`} data-label="Отклонение">
                  {r.variance > 0 ? '+' : ''}{formatMoney(r.variance, false)}
                </td>
                <td className="num muted" data-label="%">{r.pct == null ? '—' : `${r.pct}%`}</td>
              </tr>
            ))}
            {adding && (
              <tr className="bdds-addrow">
                <td data-label="Статья">
                  <input
                    className="bdds-input bdds-input-text"
                    value={newArticle}
                    onChange={(e) => setNewArticle(e.target.value)}
                    placeholder="Название статьи"
                    autoFocus
                  />
                </td>
                <td className="num" data-label="План">
                  <input
                    className="bdds-input"
                    type="number"
                    value={newAmount}
                    onChange={(e) => setNewAmount(e.target.value)}
                    placeholder="0"
                    onKeyDown={(e) => e.key === 'Enter' && onAdd()}
                  />
                </td>
                <td colSpan={3}>
                  <button className="btn btn-primary btn-sm" onClick={onAdd}>Добавить</button>
                </td>
              </tr>
            )}
          </tbody>
          <tfoot>
            <tr>
              <td>Итого {title.toLowerCase()}</td>
              <td className="num">{formatMoney(sum.plan, false)}</td>
              <td className="num">{formatMoney(sum.fact, false)}</td>
              <td className={`num ${varClass(sum.fact - sum.plan)}`}>
                {sum.fact - sum.plan > 0 ? '+' : ''}{formatMoney(sum.fact - sum.plan, false)}
              </td>
              <td className="num muted">
                {sum.plan ? `${Math.round(sum.fact / sum.plan * 100)}%` : '—'}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  )
}

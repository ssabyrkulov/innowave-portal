import { useEffect, useMemo, useRef, useState } from 'react'
import {
  DEFAULT_CONFIG,
  computeCompany,
  buildPlan,
  fmtMoney,
  fmtPct,
  monthName,
} from '../lib/unitEconomics'

const STORE_KEY = 'ue_config_v1'
const clone = (o) => JSON.parse(JSON.stringify(o))

function loadConfig() {
  try {
    const raw = localStorage.getItem(STORE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      // при смене структуры модели (новая версия) берём свежие дефолты,
      // чтобы новые направления/поля точно подхватились
      if (parsed && parsed.version === DEFAULT_CONFIG.version) return parsed
    }
  } catch {
    /* ignore */
  }
  return clone(DEFAULT_CONFIG)
}

/* Поле с живым пересчётом: обновляет модель на каждый ввод, но показывает
   ровно то, что печатает пользователь (не переформатирует под курсором). */
function LiveInput({ value, onCommit, toText, parse, className = '', ...rest }) {
  const [txt, setTxt] = useState(() => toText(value))
  const focused = useRef(false)
  useEffect(() => {
    if (!focused.current) setTxt(toText(value))
  }, [value, toText])
  return (
    <input
      {...rest}
      className={`ue-input ${className}`}
      value={txt}
      onFocus={() => (focused.current = true)}
      onBlur={() => {
        focused.current = false
        setTxt(toText(value))
      }}
      onChange={(e) => {
        setTxt(e.target.value)
        const p = parse(e.target.value)
        if (p != null) onCommit(p)
      }}
    />
  )
}

const moneyText = (v) => String(v ?? 0)
const moneyParse = (s) => (s.trim() === '' ? 0 : Number.isNaN(Number(s)) ? null : Number(s))
const pctText = (v) => String(Number(((v ?? 0) * 100).toFixed(6)))
const pctParse = (s) => (s.trim() === '' ? 0 : Number.isNaN(Number(s)) ? null : Number(s) / 100)

function MoneyInput(props) {
  return <LiveInput type="number" toText={moneyText} parse={moneyParse} {...props} />
}
function PctInput(props) {
  return <LiveInput type="number" step="0.01" toText={pctText} parse={pctParse} {...props} />
}
function NumInput(props) {
  return <LiveInput type="number" toText={moneyText} parse={moneyParse} {...props} />
}

export default function UnitEconomicsPage() {
  const [config, setConfig] = useState(loadConfig)

  useEffect(() => {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(config))
    } catch {
      /* ignore */
    }
  }, [config])

  const company = useMemo(() => computeCompany(config), [config])
  const plan = useMemo(() => buildPlan(config, company), [config, company])

  // ——— мутаторы (иммутабельно) ———
  const patch = (fn) => setConfig((c) => { const n = clone(c); fn(n); return n })
  const setCommon = (i, v) => patch((n) => { n.common.items[i].amount = v })
  const setDir = (di, key, v) => patch((n) => { n.directions[di][key] = v })
  const setDirect = (di, ci, v) => patch((n) => { n.directions[di].directCosts[ci].amount = v })
  const setChan = (di, ci, key, v) => patch((n) => { n.directions[di].channels[ci][key] = v })
  const setPlan = (key, v) => patch((n) => { n.plan[key] = v })

  const commonSum = company.commonTotal
  const coeffSum = config.directions.reduce((s, d) => s + (Number(d.commonCoeff) || 0), 0)
  const coeffOk = Math.abs(coeffSum - 1) < 0.005

  return (
    <div className="ue">
      <div className="page-header">
        <h1>Юнит-экономика и ТБУ</h1>
        <button
          className="btn btn-ghost"
          onClick={() => {
            if (confirm('Сбросить все значения к дефолтным?')) setConfig(clone(DEFAULT_CONFIG))
          }}
        >
          Сбросить к дефолтам
        </button>
      </div>

      <div className="note-readonly">
        Внутренний финансовый инструмент. Считает вживую при изменении любого поля.
        Данные хранятся в этом браузере. <b>Поля с рамкой</b> — редактируемые,
        серые значения — вычисляемые.
      </div>

      {/* ——— ИТОГ ПО КОМПАНИИ ——— */}
      <div className="ue-hero">
        <div className="ue-hero-main">
          <div className="ue-hero-label">Точка безубыточности компании · выручка/мес</div>
          <div className="ue-hero-value">{fmtMoney(company.bepTotal)}</div>
        </div>
        <div className="ue-hero-side">
          {company.directions.map((d) => (
            <div key={d.id} className="ue-hero-item">
              <span>{d.name}</span>
              <b>{fmtMoney(d.bepRevenue)}</b>
            </div>
          ))}
        </div>
      </div>

      {/* ——— НАПРАВЛЕНИЯ ——— */}
      {config.directions.map((dir, di) => {
        const res = company.directions[di]
        const fixedCost = dir.costMode === 'fixedCost'
        const shareOk = Math.abs(res.sharesSum - 1) < 0.005
        return (
          <div key={dir.id} className="chart-card ue-dir">
            <div className="ue-dir-head">
              <h2 className="chart-title">{dir.name}</h2>
              <div className={`ue-bep ${res.valid ? '' : 'ue-bad'}`}>
                ТБУ {res.valid ? fmtMoney(res.bepRevenue) : 'проверь маржу'}
              </div>
            </div>

            {/* параметры направления */}
            <div className="ue-params">
              {fixedCost ? (
                <label className="ue-field">
                  <span>Себестоимость, сом/шт</span>
                  <MoneyInput value={dir.unitCost} onCommit={(v) => setDir(di, 'unitCost', v)} />
                </label>
              ) : (
                <>
                  <label className="ue-field">
                    <span>Доставка, %</span>
                    <PctInput value={dir.deliveryPct} onCommit={(v) => setDir(di, 'deliveryPct', v)} />
                  </label>
                  <label className="ue-field">
                    <span>Растаможка, %</span>
                    <PctInput value={dir.customsPct} onCommit={(v) => setDir(di, 'customsPct', v)} />
                  </label>
                </>
              )}
              <label className="ue-field">
                <span>Доля общих расходов, %</span>
                <PctInput value={dir.commonCoeff} onCommit={(v) => setDir(di, 'commonCoeff', v)} />
              </label>
            </div>

            {/* каналы */}
            <div className="table-wrap ue-table">
              <table>
                <thead>
                  <tr>
                    <th>Канал</th>
                    {fixedCost && <th className="num">Цена</th>}
                    <th className="num">Доля</th>
                    <th className="num">Поставщики</th>
                    <th className="num">Налог</th>
                    <th className="num">Маржа</th>
                    <th className="num">ТБУ канала</th>
                  </tr>
                </thead>
                <tbody>
                  {dir.channels.map((ch, ci) => {
                    const c = res.perChannel[ci]
                    return (
                      <tr key={ci}>
                        <td>
                          {ch.name}
                          {ch.provisional && <span className="ue-flag">уточнить</span>}
                        </td>
                        {fixedCost && (
                          <td className="num">
                            <MoneyInput value={ch.price} onCommit={(v) => setChan(di, ci, 'price', v)} />
                          </td>
                        )}
                        <td className="num">
                          <PctInput
                            className={ch.provisional ? 'ue-input-flag' : ''}
                            value={ch.share}
                            onCommit={(v) => setChan(di, ci, 'share', v)}
                          />
                        </td>
                        <td className="num">
                          {fixedCost ? (
                            <span className="ue-calc">{fmtPct(c.supplierPct)}</span>
                          ) : (
                            <PctInput value={ch.supplierPct} onCommit={(v) => setChan(di, ci, 'supplierPct', v)} />
                          )}
                        </td>
                        <td className="num">
                          <PctInput value={ch.taxPct} onCommit={(v) => setChan(di, ci, 'taxPct', v)} />
                        </td>
                        <td className={`num ue-calc ${c.margin < 0 ? 'neg' : ''}`}>{fmtPct(c.margin)}</td>
                        <td className="num ue-calc">{fmtMoney(c.bep, false)}</td>
                      </tr>
                    )
                  })}
                </tbody>
                <tfoot>
                  <tr>
                    <td>Итого</td>
                    {fixedCost && <td></td>}
                    <td className={`num ${shareOk ? '' : 'neg'}`}>{fmtPct(res.sharesSum)}</td>
                    <td className="num"></td>
                    <td className="num ue-calc">{fmtPct(res.weightedTax)}</td>
                    <td className="num ue-calc">{fmtPct(res.weightedMargin)}</td>
                    <td className="num ue-calc">{fmtMoney(res.bepRevenue, false)}</td>
                  </tr>
                </tfoot>
              </table>
            </div>
            {!shareOk && (
              <div className="ue-warn">⚠ Сумма долей каналов = {fmtPct(res.sharesSum)}, а должна быть 100%.</div>
            )}

            {/* прямые расходы направления */}
            <div className="ue-direct">
              <div className="ue-sub">Прямые постоянные расходы направления, сом/мес</div>
              {dir.directCosts.map((d, ci) => (
                <label key={ci} className="ue-field ue-field-row">
                  <span>{d.label}</span>
                  <MoneyInput value={d.amount} onCommit={(v) => setDirect(di, ci, v)} />
                </label>
              ))}
            </div>

            {/* сводка расчёта */}
            <div className="ue-summary">
              <div><span>Взвешенная маржа</span><b>{fmtPct(res.weightedMargin)}</b></div>
              <div><span>Взвешенный налог</span><b>{fmtPct(res.weightedTax)}</b></div>
              <div><span>Прямые расходы</span><b>{fmtMoney(res.direct)}</b></div>
              <div><span>Доля общих ({fmtPct(dir.commonCoeff)})</span><b>{fmtMoney(res.commonShare)}</b></div>
              <div><span>Фиксы к покрытию</span><b>{fmtMoney(res.fixed)}</b></div>
              <div className={`ue-check ${res.valid && Math.abs(res.balanceCheck) < 0.5 ? 'ok' : 'bad'}`}>
                <span>Проверка баланса (= 0)</span>
                <b>{res.valid ? (Math.abs(res.balanceCheck) < 0.5 ? '✓ 0' : fmtMoney(res.balanceCheck)) : '—'}</b>
              </div>
            </div>
          </div>
        )
      })}

      {/* ——— ОБЩИЙ КОТЁЛ ——— */}
      <div className="chart-card">
        <div className="ue-dir-head">
          <h2 className="chart-title">Общий котёл · постоянные расходы</h2>
          <div className="ue-bep">{fmtMoney(commonSum)} / мес</div>
        </div>
        {!coeffOk && (
          <div className="ue-warn">
            ⚠ Сумма «долей общих» по направлениям = {fmtPct(coeffSum)}, а должна быть 100%
            (иначе общие расходы делятся неверно). Поправь коэффициенты в направлениях.
          </div>
        )}
        <div className="ue-common-grid">
          {config.common.items.map((it, i) => (
            <label key={i} className="ue-field ue-field-row">
              <span>{it.label}</span>
              <MoneyInput value={it.amount} onCommit={(v) => setCommon(i, v)} />
            </label>
          ))}
        </div>
      </div>

      {/* ——— ПЛАН ПРОДАЖ ——— */}
      <div className="chart-card">
        <div className="ue-dir-head">
          <h2 className="chart-title">
            План продаж · {monthName(config.plan.month)} {config.plan.year}
          </h2>
          <div className="ue-bep">{fmtMoney(plan.daily)} / день</div>
        </div>

        <div className="ue-plan-controls">
          <div className="ue-goal">
            <span className="ue-sub">Цель плана</span>
            <div className="ue-seg">
              {[
                ['bep', 'ТБУ'],
                ['bepReserve', 'ТБУ + запас'],
                ['custom', 'Своя сумма'],
              ].map(([mode, label]) => (
                <button
                  key={mode}
                  className={`ue-seg-btn ${config.plan.goalMode === mode ? 'active' : ''}`}
                  onClick={() => setPlan('goalMode', mode)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {config.plan.goalMode === 'bepReserve' && (
            <label className="ue-field">
              <span>Запас, %</span>
              <PctInput value={config.plan.reservePct} onCommit={(v) => setPlan('reservePct', v)} />
            </label>
          )}
          {config.plan.goalMode === 'custom' && (
            <label className="ue-field">
              <span>Сумма, сом</span>
              <MoneyInput value={config.plan.customAmount} onCommit={(v) => setPlan('customAmount', v)} />
            </label>
          )}
          <label className="ue-field">
            <span>Дней в неделе</span>
            <NumInput value={config.plan.daysPerWeek} onCommit={(v) => setPlan('daysPerWeek', v)} />
          </label>
          <label className="ue-field ue-field-wide">
            <span>Праздники (через запятую, ГГГГ-ММ-ДД)</span>
            <LiveInput
              value={config.plan.holidays.join(', ')}
              toText={(v) => v}
              parse={(s) => s.split(',').map((x) => x.trim()).filter(Boolean)}
              onCommit={(v) => setPlan('holidays', v)}
            />
          </label>
        </div>

        <div className="ue-plan-kpis">
          <div className="ue-kpi"><span>Цель месяца</span><b>{fmtMoney(plan.target)}</b></div>
          <div className="ue-kpi"><span>Рабочих дней</span><b>{plan.workingDays}</b></div>
          <div className="ue-kpi"><span>Дневная норма</span><b>{fmtMoney(plan.daily)}</b></div>
          <div className="ue-kpi"><span>Недельная норма</span><b>{fmtMoney(plan.weekly)}</b></div>
        </div>

        <div className="table-wrap ue-table ue-plan-table">
          <table>
            <thead>
              <tr>
                <th>Дата</th>
                <th>День</th>
                <th className="num">Норма дня</th>
                <th className="num">Накопительно</th>
                <th className="num">% плана</th>
              </tr>
            </thead>
            <tbody>
              {plan.rows.map((r) => {
                const dt = new Date(r.date + 'T00:00:00')
                const wd = dt.toLocaleDateString('ru-RU', { weekday: 'short' })
                return (
                  <tr key={r.date}>
                    <td>{dt.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' })}</td>
                    <td className="muted">{wd}</td>
                    <td className="num">{fmtMoney(r.daily, false)}</td>
                    <td className="num">{fmtMoney(r.cumulative, false)}</td>
                    <td className="num muted">{fmtPct(r.pct, 0)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      <p className="muted ue-foot">
        На будущее: деление общих расходов по драйверу (доля выручки/площадь);
        ввод долей салфеток через объёмы/цены; сезонность по дням недели; экспорт плана.
      </p>
    </div>
  )
}

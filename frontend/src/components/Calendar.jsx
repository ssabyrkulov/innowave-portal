import { MONTHS, WEEKDAYS, formatMoney, monthMatrix, toISODate } from '../utils'

export default function Calendar({ year, month, payments, onPrev, onNext, onToday, onDayClick }) {
  const weeks = monthMatrix(year, month)
  const todayISO = toISODate(new Date())

  // Group payments by ISO date for quick lookup
  const byDate = {}
  for (const p of payments) {
    ;(byDate[p.due_date] ||= []).push(p)
  }

  return (
    <div className="calendar">
      <div className="calendar-header">
        <h1>
          {MONTHS[month]} {year}
        </h1>
        <div className="calendar-nav">
          <button className="btn btn-ghost" onClick={onPrev}>
            ‹
          </button>
          <button className="btn btn-ghost" onClick={onToday}>
            Сегодня
          </button>
          <button className="btn btn-ghost" onClick={onNext}>
            ›
          </button>
        </div>
      </div>

      <div className="calendar-grid">
        {WEEKDAYS.map((wd) => (
          <div key={wd} className="weekday">
            {wd}
          </div>
        ))}

        {weeks.flat().map((date) => {
          const iso = toISODate(date)
          const inMonth = date.getMonth() === month
          const items = byDate[iso] || []
          const isToday = iso === todayISO
          return (
            <div
              key={iso}
              className={`day ${inMonth ? '' : 'day-muted'} ${isToday ? 'day-today' : ''}`}
              onClick={() => onDayClick(iso)}
            >
              <div className="day-number">{date.getDate()}</div>
              <div className="day-items">
                {items.slice(0, 3).map((p) => (
                  <div
                    key={p.id}
                    className={`chip chip-${p.direction} chip-${p.status}`}
                    title={`${p.title} — ${formatMoney(p.amount, p.currency)}`}
                  >
                    <span className="chip-arrow">
                      {p.direction === 'incoming' ? '▲' : '▼'}
                    </span>
                    {formatMoney(p.amount, p.currency)}
                  </div>
                ))}
                {items.length > 3 && (
                  <div className="chip chip-more">+{items.length - 3} ещё</div>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

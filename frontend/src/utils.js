export const STATUS_LABELS = {
  planned: 'Запланирован',
  paid: 'Оплачен',
  overdue: 'Просрочен',
}

export const DIRECTION_LABELS = {
  incoming: 'Входящий',
  outgoing: 'Исходящий',
}

export const MONTHS = [
  'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь',
]

export const WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']

export function formatMoney(amount, currency = 'KGS') {
  const value = Number(amount || 0)
  const text = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(value)
  // Второй аргумент местами передают как false — «в этой колонке валюта не
  // нужна». Раньше он приклеивался как есть, и в остатках и бюджете суммы
  // печатались как «1 234 false».
  return currency ? `${text} ${currency}` : text
}

// Build an ISO date string (YYYY-MM-DD) without timezone drift.
export function toISODate(date) {
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const d = String(date.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

// Returns the matrix of dates (6 weeks) for a month grid starting on Monday.
export function monthMatrix(year, month) {
  const first = new Date(year, month, 1)
  // getDay(): 0=Sun..6=Sat → shift so Monday=0
  const offset = (first.getDay() + 6) % 7
  const start = new Date(year, month, 1 - offset)
  const weeks = []
  let cursor = new Date(start)
  for (let w = 0; w < 6; w++) {
    const days = []
    for (let d = 0; d < 7; d++) {
      days.push(new Date(cursor))
      cursor.setDate(cursor.getDate() + 1)
    }
    weeks.push(days)
  }
  return weeks
}

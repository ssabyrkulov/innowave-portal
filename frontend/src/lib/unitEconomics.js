// Юнит-экономика и точка безубыточности (ТБУ).
// Чистая математика без React — чтобы легко тестировать и переиспользовать.
// Модель: направления → каналы + общий котёл постоянных расходов, который
// делится между направлениями по коэффициенту. Архитектура на N направлений
// и N каналов (ничего не захардкожено под «два/три»).

// ————————————————————————————————————————————————————————————
// Дефолтная модель (все значения редактируемые в UI)
// ————————————————————————————————————————————————————————————
export const DEFAULT_CONFIG = {
  common: {
    items: [
      { label: 'Подписки / сертификаты', amount: 60000 },
      { label: 'Аренда офиса', amount: 60000 },
      { label: 'Аренда склада', amount: 45000 },
      { label: 'Кредит', amount: 0 },
      { label: 'ФОТ (общий)', amount: 600000 },
      { label: 'Учредитель', amount: 0 },
      { label: 'Резерв', amount: 20000 },
      { label: 'Жизнедеятельность офиса', amount: 10000 },
      { label: 'Комуслуги', amount: 2000 },
      { label: 'Основные средства', amount: 0 },
      { label: 'Коммуникации + банк', amount: 20000 },
      { label: 'Представительские', amount: 0 },
      { label: 'Сотрудник месяца', amount: 0 },
      { label: 'Благотворительность', amount: 3000 },
      { label: 'Непредвиденные', amount: 10000 },
      { label: 'Курьеры', amount: 2000 },
      { label: 'Подарки клиентам', amount: 0 },
      { label: 'Корпоративные расходы', amount: 0 },
    ],
  },
  directions: [
    {
      id: 'diapers',
      name: 'Подгузники ONE',
      costMode: 'pct', // поставщики% задаётся напрямую
      deliveryPct: 0.07785314066647006,
      customsPct: 0.10380418755529343,
      commonCoeff: 0.5,
      directCosts: [
        { label: 'Реклама (роддом/таргет)', amount: 50000 },
        { label: 'Кэшбэк / полки', amount: 50000 },
      ],
      channels: [
        { name: 'ОПТ', share: 0.8139191978767325, supplierPct: 0.5159420289855072, taxPct: 0.04 },
        { name: 'РОЗНИЦА', share: 0.157770569153642, supplierPct: 0.2616822429906542, taxPct: 0.04 },
        { name: 'ONLINE', share: 0.02831023296962548, supplierPct: 0.375, taxPct: 0.02 },
      ],
    },
    {
      id: 'napkins',
      name: 'Салфетки и туалетная бумага',
      costMode: 'fixedCost', // поставщики% = unitCost / цена канала
      unitCost: 110, // себестоимость «всё включено» (доставка/растаможка в цене)
      deliveryPct: 0,
      customsPct: 0,
      commonCoeff: 0.5,
      directCosts: [
        { label: 'Реклама', amount: 0 },
        { label: 'Кэшбэк / полки', amount: 0 },
      ],
      channels: [
        { name: 'ОПТ (сети)', price: 130, share: 1 / 3, taxPct: 0.04, provisional: true },
        { name: 'РОЗНИЦА', price: 145.8, share: 1 / 3, taxPct: 0.04, provisional: true },
        { name: 'БАЗАР', price: 135, share: 1 / 3, taxPct: 0.04, provisional: true },
      ],
    },
  ],
  plan: {
    year: 2026,
    month: 8, // Август
    daysPerWeek: 5,
    workdayMask: [1, 2, 3, 4, 5], // Пн–Пт (0 = вс)
    holidays: ['2026-08-31'], // День независимости КР
    goalMode: 'bep', // 'bep' | 'bepReserve' | 'custom'
    reservePct: 0.1,
    customAmount: 4366726,
  },
}

// ————————————————————————————————————————————————————————————
// Расчёты
// ————————————————————————————————————————————————————————————
export function channelSupplierPct(direction, ch) {
  if (direction.costMode === 'fixedCost') {
    const price = Number(ch.price) || 0
    return price > 0 ? (Number(direction.unitCost) || 0) / price : 0
  }
  return Number(ch.supplierPct) || 0
}

export function channelMargin(direction, ch) {
  const sup = channelSupplierPct(direction, ch)
  return 1 - sup - (Number(direction.deliveryPct) || 0) - (Number(direction.customsPct) || 0)
}

export function commonTotal(config) {
  return config.common.items.reduce((s, i) => s + (Number(i.amount) || 0), 0)
}

export function computeDirection(direction, config) {
  const channels = direction.channels.map((ch) => ({
    ...ch,
    supplierPctEff: channelSupplierPct(direction, ch),
    margin: channelMargin(direction, ch),
  }))

  const weightedMargin = channels.reduce((s, c) => s + (Number(c.share) || 0) * c.margin, 0)
  const weightedTax = channels.reduce((s, c) => s + (Number(c.share) || 0) * (Number(c.taxPct) || 0), 0)
  const sharesSum = channels.reduce((s, c) => s + (Number(c.share) || 0), 0)

  const direct = direction.directCosts.reduce((s, d) => s + (Number(d.amount) || 0), 0)
  const commonShare = commonTotal(config) * (Number(direction.commonCoeff) || 0)
  const fixed = direct + commonShare

  const denom = weightedMargin - weightedTax
  const bepRevenue = denom > 0 ? fixed / denom : null

  const marginAtBep = bepRevenue == null ? null : bepRevenue * weightedMargin
  const taxAtBep = bepRevenue == null ? null : bepRevenue * weightedTax
  // должно быть 0
  const balanceCheck = bepRevenue == null ? null : marginAtBep - taxAtBep - fixed

  const perChannel = channels.map((c) => ({
    name: c.name,
    share: Number(c.share) || 0,
    supplierPct: c.supplierPctEff,
    margin: c.margin,
    taxPct: Number(c.taxPct) || 0,
    provisional: !!c.provisional,
    bep: bepRevenue == null ? null : bepRevenue * (Number(c.share) || 0),
  }))

  return {
    id: direction.id,
    name: direction.name,
    weightedMargin,
    weightedTax,
    sharesSum,
    direct,
    commonShare,
    fixed,
    denom,
    bepRevenue,
    marginAtBep,
    taxAtBep,
    balanceCheck,
    perChannel,
    valid: bepRevenue != null,
  }
}

export function computeCompany(config) {
  const directions = config.directions.map((d) => computeDirection(d, config))
  const bepTotal = directions.every((d) => d.valid)
    ? directions.reduce((s, d) => s + d.bepRevenue, 0)
    : null
  return { directions, bepTotal, commonTotal: commonTotal(config) }
}

// Цель плана продаж (в сомах) на основе company ТБУ и режима цели
export function planTarget(config, company) {
  const p = config.plan
  if (p.goalMode === 'custom') return Number(p.customAmount) || 0
  if (company.bepTotal == null) return null
  if (p.goalMode === 'bepReserve') return company.bepTotal * (1 + (Number(p.reservePct) || 0))
  return company.bepTotal
}

// Список рабочих дат месяца по маске рабочих дней минус праздники
export function workingDates(plan) {
  const { year, month, workdayMask, holidays } = plan
  const mask = workdayMask && workdayMask.length ? workdayMask : [1, 2, 3, 4, 5]
  const hset = new Set(holidays || [])
  const days = new Date(year, month, 0).getDate() // число дней в месяце
  const out = []
  for (let d = 1; d <= days; d++) {
    const date = new Date(year, month - 1, d)
    const iso = `${year}-${String(month).padStart(2, '0')}-${String(d).padStart(2, '0')}`
    if (mask.includes(date.getDay()) && !hset.has(iso)) out.push(iso)
  }
  return out
}

export function buildPlan(config, company) {
  const p = config.plan
  const dates = workingDates(p)
  const workingDays = dates.length
  const target = planTarget(config, company)
  const daily = target == null || workingDays === 0 ? null : target / workingDays
  const weekly = daily == null ? null : daily * (Number(p.daysPerWeek) || 5)
  const rows = dates.map((iso, i) => ({
    date: iso,
    daily,
    cumulative: daily == null ? null : daily * (i + 1),
    pct: target ? ((i + 1) / workingDays) : null,
  }))
  return { dates, workingDays, target, daily, weekly, rows }
}

// ————————————————————————————————————————————————————————————
// Форматирование
// ————————————————————————————————————————————————————————————
export function fmtMoney(v, withSuffix = true) {
  if (v == null || Number.isNaN(v)) return '—'
  const n = Math.round(v)
  const grouped = Math.abs(n)
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, ' ')
  const body = n < 0 ? `(${grouped})` : grouped
  return withSuffix ? `${body} сом` : body
}

export function fmtPct(v, digits = 1) {
  if (v == null || Number.isNaN(v)) return '—'
  return (
    (v * 100).toLocaleString('ru-RU', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }) + '%'
  )
}

const MONTHS = ['', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль',
  'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']
export const monthName = (m) => MONTHS[m] || ''

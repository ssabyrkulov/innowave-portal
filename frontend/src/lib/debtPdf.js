// Выгрузка дебиторки в PDF. Отдельной библиотеки нет: страница собирается в
// новом окне и уходит в печать браузера, где «Сохранить как PDF» есть везде —
// и на компьютере, и на телефоне. Печатается ровно то, что видно на экране:
// после фильтров и в том же порядке сортировки.

const WEEKDAYS = ['', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']

function esc(v) {
  return String(v ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function money(v) {
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(Number(v || 0))
}

function fmtDate(iso) {
  return iso ? iso.split('-').reverse().join('.') : '—'
}

function agentHtml(c) {
  if (!c.agent) return '<span class="bad">без агента</span>'
  const route = c.agent_days
    ? String(c.agent_days).split(',').map((d) => WEEKDAYS[Number(d)] || d).join(', ')
    : ''
  const note = [c.agent_source, route && `маршрут ${route}`].filter(Boolean).join(' · ')
  return (
    esc(c.agent) +
    (c.agent_active === false ? ' <span class="bad">уволен</span>' : '') +
    (note ? `<div class="note">${esc(note)}</div>` : '')
  )
}

// rows — клиенты в порядке экрана; daysOf(c) — дней без оплат или null;
// filters и sortLabel — человеческое описание среза для шапки документа.
export function buildDebtHtml({ rows, daysOf, staleDays, filters, sortLabel }) {
  const total = rows.reduce((s, c) => s + c.debt, 0)
  const stale = rows.filter((c) => (daysOf(c) ?? 0) > staleDays)
  const staleSum = stale.reduce((s, c) => s + c.debt, 0)
  const now = new Date()
  const stamp = now.toLocaleDateString('ru-RU') + ' ' +
    now.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })

  const body = rows.map((c, i) => {
    const d = daysOf(c)
    const cls = d == null ? '' : d > 90 ? 'r' : d > staleDays ? 'w' : 'g'
    return `<tr>
      <td class="n">${i + 1}</td>
      <td class="cl">${esc(c.client)}${c.network ? `<div class="note">${esc(c.network)}</div>` : ''}</td>
      <td class="ag">${agentHtml(c)}</td>
      <td class="num">${money(c.shipped)}</td>
      <td class="num mu">${c.returned > 0 ? '−' + money(c.returned) : '—'}</td>
      <td class="num">${money(c.paid)}</td>
      <td class="num debt">${money(c.debt)}</td>
      <td class="dt">${fmtDate(c.last_shipment)}</td>
      <td class="dt">${fmtDate(c.last_payment)}</td>
      <td class="c">${d == null ? '—' : `<span class="b ${cls}">${d} дн.</span>`}</td>
    </tr>`
  }).join('')

  return `<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Дебиторка ${esc(now.toLocaleDateString('ru-RU'))}</title>
<style>
  @page { size: A4 landscape; margin: 10mm; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", Roboto, "DejaVu Sans", Arial, sans-serif;
         color: #202020; font-size: 9pt; margin: 0; padding: 12px; }
  h1 { font-size: 15pt; margin: 0 0 4px; }
  .sub { color: #4d4d4d; margin-bottom: 8px; }
  .tiles { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
  .tile { border: 1px solid #d7d7d7; border-radius: 6px; padding: 5px 10px; }
  .tile b { display: block; font-size: 11.5pt; }
  .tile span { color: #4d4d4d; font-size: 8pt; }
  table { width: 100%; border-collapse: collapse; }
  thead { display: table-header-group; }
  th { text-align: left; font-size: 7.5pt; text-transform: uppercase; letter-spacing: .04em;
       color: #4d4d4d; font-weight: 600; border-bottom: 1.2px solid #202020; padding: 5px 6px; }
  td { padding: 4px 6px; border-bottom: .5px solid #e2e2e2; vertical-align: top; }
  tr { page-break-inside: avoid; }
  th.num, td.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .n { color: #828282; width: 22px; }
  .cl { font-weight: 600; width: 22%; }
  .ag { width: 17%; }
  .note { color: #828282; font-size: 7.5pt; font-weight: 400; }
  .mu { color: #828282; }
  .debt { color: #c2410c; font-weight: 700; }
  .dt { white-space: nowrap; }
  .c { text-align: center; white-space: nowrap; }
  .bad { color: #c2410c; font-size: 7.5pt; }
  .b { display: inline-block; padding: 1px 6px; border-radius: 8px; font-size: 7.5pt; }
  .b.g { background: #e8f3ea; color: #2f6b3a; }
  .b.w { background: #fbefd9; color: #8a5a00; }
  .b.r { background: #ffe7dd; color: #c2410c; }
  .foot { margin-top: 8px; color: #828282; font-size: 7.5pt; }
  @media print { body { padding: 0; } }
</style></head><body>
  <h1>Дебиторка</h1>
  <div class="sub">Сформировано ${esc(stamp)} · ${esc(filters || 'без фильтров')} · сортировка: ${esc(sortLabel)}</div>
  <div class="tiles">
    <div class="tile"><b>${money(total)} KGS</b><span>долг в списке, клиентов: ${rows.length}</span></div>
    <div class="tile"><b>${money(staleSum)} KGS</b><span>без оплат больше ${staleDays} дн., клиентов: ${stale.length}</span></div>
  </div>
  <table>
    <thead><tr>
      <th>№</th><th>Клиент</th><th>Агент</th><th class="num">Отгружено</th>
      <th class="num">Возвраты</th><th class="num">Оплачено</th><th class="num">Долг</th>
      <th>Посл. отгрузка</th><th>Посл. оплата</th><th>Без оплат</th>
    </tr></thead>
    <tbody>${body}</tbody>
  </table>
  <div class="foot">Суммы в KGS. «Без оплат» — дней с последней оплаты, а если оплат не было — с последней отгрузки.</div>
</body></html>`
}

// Открывает документ и вызывает печать. Окно открывается прямо в обработчике
// клика, иначе браузер посчитает его всплывающим и заблокирует. Если окно
// всё же не открылось — печатаем через невидимый iframe на этой же странице.
export function printHtml(html) {
  const w = window.open('', '_blank')
  if (w) {
    w.document.open()
    w.document.write(html)
    w.document.close()
    w.focus()
    // Даём шрифтам и таблице разложиться, потом печать.
    setTimeout(() => w.print(), 300)
    return
  }
  const frame = document.createElement('iframe')
  frame.style.position = 'fixed'
  frame.style.width = '0'
  frame.style.height = '0'
  frame.style.border = '0'
  document.body.appendChild(frame)
  const doc = frame.contentWindow.document
  doc.open()
  doc.write(html)
  doc.close()
  setTimeout(() => {
    frame.contentWindow.focus()
    frame.contentWindow.print()
    setTimeout(() => frame.remove(), 1000)
  }, 300)
}

import { Fragment, useEffect, useState } from 'react'
import { api } from '../api'
import { formatMoney } from '../utils'

// Даты читают как в 1С — «27.08.2026», а не «2026-08-27».
const fdate = (iso) => (iso ? iso.split('-').reverse().join('.') : '—')

const pairs = (n) => {
  const t = n % 10
  const h = n % 100
  if (t === 1 && h !== 11) return 'пара'
  if (t >= 2 && t <= 4 && (h < 12 || h > 14)) return 'пары'
  return 'пар'
}

// Управленка и налоговая — две разные базы 1С. Документ попадает во вторую
// руками, когда бухгалтер до него дойдёт; пока не дошёл, товар в налоговой
// числится на складе, а выручки нет. Видно это было только косвенно —
// расхождением остатков, и причину искали раскопками в выгрузках. Страница
// называет причину прямо: что проведено здесь и не проведено там.
export default function ContoursPage() {
  const [unposted, setUnposted] = useState(null)
  const [openType, setOpenType] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    // Фирмы приходят внутри ответа — выбор в шапке эндпоинт учитывает сам.
    api.taxUnposted().then(setUnposted).catch((e) => setError(e.message))
  }, [])

  if (error) return <div className="error">{error}</div>

  return (
    <div>
      <div className="page-header">
        <h1>Управленка ↔ налоговая</h1>
        <span className="muted">
          {unposted
            ? `дыр: ${unposted.gaps} · в хвосте: ${unposted.tail}`
            : 'Загрузка…'}
        </span>
      </div>

    {unposted?.firms?.length > 0 && (
      <div className="fresh-block">
        <p className="muted">
          Пара ищется по количеству, а не по сумме: штуки в контурах
          одинаковы, а цены разные — в налоговой трансфертные. Дата тоже
          своя: документ проводят во второй базе позже, иногда через
          месяцы. «Хвост» — документы свежее последнего документа второго
          контура: обычное отставание. «Дыра» — пропуск внутри закрытого
          периода, вот её и надо разбирать. Где контур ведёт лишь часть
          документов (у Хайджина налоговая — это ЭСФ на юрлиц и сводные),
          непарные помечены нейтрально: это устройство учёта, а не потеря.
        </p>
        <div className="table-wrap compact">
          <table>
            <thead>
              <tr>
                <th>Фирма</th><th>Документы</th>
                <th className="num">Только в управленке</th>
                <th className="num">Только в налоговой</th>
                <th>Спарено</th>
                <th>Последний документ</th>
              </tr>
            </thead>
            <tbody>
              {unposted.firms.map((f) => f.types
                .filter((t) => t.only_upr_count || t.only_tax_count)
                .map((t) => {
                  const firm = f.org
                  const id = `${firm}:${t.key}`
                  const open = openType === id
                  return (
                    <Fragment key={id}>
                      <tr className="doc-row"
                        onClick={() => setOpenType(open ? null : id)}>
                        <td>{firm === 'hygiene' ? 'Innowave Hygiene' : 'Innowave'}</td>
                        <td>{open ? '▾' : '▸'} {t.label}</td>
                        <td className={`num ${t.gaps_upr ? 'sc-diff' : ''}`}>
                          {t.upr_absent ? <span className="muted">не ведётся</span>
                            : t.only_upr_count || '—'}
                          {t.gaps_upr > 0 && <> · дыр <b>{t.gaps_upr}</b></>}
                        </td>
                        <td className={`num ${t.gaps_tax ? 'sc-diff' : ''}`}>
                          {t.tax_absent ? <span className="muted">не ведётся</span>
                            : t.only_tax_count || '—'}
                          {t.gaps_tax > 0 && <> · дыр <b>{t.gaps_tax}</b></>}
                        </td>
                        <td className="muted">
                          {t.paired} {pairs(t.paired)}
                          {/* Доля пар осмысленна, только когда документы
                              есть с обеих сторон: у пустого контура «100%»
                              значили бы «сошлось», а сходиться нечему. */}
                          {!t.upr_absent && !t.tax_absent && (
                            <> · {t.cover_upr}% упр. / {t.cover_tax}% нал.</>
                          )}
                        </td>
                        <td className="muted">
                          упр. {t.upr_last ? fdate(t.upr_last) : '—'} · нал.{' '}
                          {t.tax_last ? fdate(t.tax_last) : '—'}
                        </td>
                      </tr>
                      {open && (
                        <tr>
                          <td colSpan={6} className="doc-lines">
                            {[['Только в управленке', t.only_upr],
                              ['Только в налоговой', t.only_tax]].map(
                              ([title, rows]) => rows.length > 0 && (
                                <div key={title}>
                                  <div className="muted">{title}</div>
                                  <table>
                                    <thead>
                                      <tr>
                                        <th>Дата</th><th>Документ</th>
                                        <th>Контрагент</th>
                                        <th className="num">Кол-во</th>
                                        <th className="num">Сумма</th>
                                        <th></th>
                                      </tr>
                                    </thead>
                                    <tbody>
                                      {rows.map((r, i) => (
                                        <tr key={i}>
                                          <td>{fdate(r.date)}</td>
                                          <td>{r.number || '—'}</td>
                                          <td>{r.party || '—'}</td>
                                          <td className="num">
                                            {r.qty ? r.qty.toLocaleString('ru-RU') : '—'}
                                          </td>
                                          <td className="num">
                                            {t.by_amount ? formatMoney(r.amount, '') : '—'}
                                          </td>
                                          <td>
                                            {r.tail
                                              ? <span className="muted">хвост</span>
                                              : r.gap
                                                ? <span className="sc-diff">дыра</span>
                                                : <span className="muted">нет пары</span>}
                                          </td>
                                        </tr>
                                      ))}
                                    </tbody>
                                  </table>
                                </div>
                              ))}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                }))}
            </tbody>
          </table>
        </div>
      </div>
    )}
    {unposted && unposted.firms.every(
      (f) => f.types.every((t) => !t.only_upr_count && !t.only_tax_count)) && (
      <p className="muted">
        Все документы обеих баз нашли пару — расхождений нет.
      </p>
    )}
    </div>
  )
}

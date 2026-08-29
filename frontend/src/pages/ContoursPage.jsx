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
const KINDS = {
  sale: 'Реализация', return: 'Возврат',
  writeoff: 'Списание', purchase: 'Поступление',
}
const FIRMS = { hygiene: 'Innowave Hygiene', innowave: 'Innowave' }

// Момент, когда расхождение заметили, знает только сам портал: 1С про него
// не помнит, а список «сейчас» ничего не хранит — документ провели во второй
// базе, и расхождение исчезло, будто его не было. Журнал держит его событием.
function Journal() {
  const [state, setState] = useState('open')
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  // Журнал пополняется, когда приходят файлы из 1С. Пока после запуска не
  // было ни одной выгрузки, он пуст — и это выглядит как поломка. Поэтому
  // первый раз считаем сами, а дальше есть кнопка.
  const [scanned, setScanned] = useState(false)

  function load(st = state) {
    return api.contourEvents(st).then(setData).catch(() => setData(null))
  }
  useEffect(() => { load(state) }, [state])

  useEffect(() => {
    if (!data || scanned || data.rows.length || state !== 'open') return
    setScanned(true)
    rescan()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, scanned, state])

  async function rescan() {
    setBusy(true)
    try {
      await api.contourEventsScan()
      await load()
    } catch (e) {
      alert('Ошибка: ' + e.message)
    } finally {
      setBusy(false)
    }
  }

  async function ack(id) {
    try {
      await api.contourEventAck(id)
      load()
    } catch (e) { alert('Ошибка: ' + e.message) }
  }

  return (
    <>
      <h2 className="section-title">Журнал расхождений</h2>
      <p className="muted">
        Каждое расхождение записано событием: когда замечено и чем
        закончилось. Ушло само — значит документ провели во второй базе.
        «Это норма» убирает событие из открытых, но не из журнала.
      </p>
      <div className="rc-period">
        {[['open', 'Открытые'], ['resolved', 'Закрылись сами'],
          ['acked', 'Признаны нормой'], ['all', 'Все']].map(([k, label]) => (
          <button key={k}
            className={`btn btn-sm ${state === k ? '' : 'btn-ghost'}`}
            onClick={() => setState(k)}>{label}</button>
        ))}
        <button className="btn btn-sm" disabled={busy} onClick={rescan}>
          {busy ? 'Считаю…' : 'Пересчитать'}
        </button>
        {data && (
          <span className="muted">
            открытых: {data.open_total} · из них просрочено: {data.open_gaps}
          </span>
        )}
      </div>
      {!data ? <div className="muted">Загрузка…</div>
        : data.rows.length === 0 ? (
          <p className="muted">
            {state === 'open'
              ? 'Открытых расхождений нет.'
              : 'Записей нет.'}
          </p>
        ) : (
        <div className="table-wrap compact">
          <table>
            <thead>
              <tr>
                <th>Замечено</th><th>Фирма</th><th>Документ</th>
                <th>Контрагент</th><th className="num">Кол-во</th>
                <th className="num">Сумма</th><th>Состояние</th><th></th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((e) => (
                <tr key={e.id}>
                  <td>{fdate(e.first_seen.slice(0, 10))}</td>
                  <td className="muted">{FIRMS[e.organization] || e.organization}</td>
                  <td>
                    {KINDS[e.kind] || e.kind} {e.number || '—'}
                    <span className="muted"> · {e.side === 'upr'
                      ? 'только в управленке' : 'только в налоговой'}</span>
                    <div className="muted skipped-cols">от {fdate(e.date)}</div>
                  </td>
                  <td>{e.party || '—'}</td>
                  <td className="num">{e.qty ? e.qty.toLocaleString('ru-RU') : '—'}</td>
                  <td className="num">{formatMoney(e.amount, '')}</td>
                  <td>
                    {e.resolved_at
                      ? <span className="sc-ok">закрылось {fdate(e.resolved_at.slice(0, 10))}</span>
                      : e.acked_at ? <span className="muted">норма</span>
                        : e.gap ? <span className="sc-diff"
                            title="Во второй базе уже есть документы более поздние, чем этот, — значит его пропустили, а не просто не успели">
                            просрочено
                          </span>
                          : <span className="muted">нет пары</span>}
                  </td>
                  <td>
                    {!e.resolved_at && !e.acked_at && (
                      <button className="btn btn-ghost btn-sm"
                        onClick={() => ack(e.id)}>это норма</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

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
            ? `просрочено: ${unposted.gaps} · ждут очереди: ${unposted.tail}`
            : 'Загрузка…'}
        </span>
      </div>

    {unposted?.firms?.length > 0 && (
      <div className="fresh-block">
        <p className="muted">
          Документ есть в одной базе 1С и не найден в другой. Две пометки
          различают спокойное и тревожное. <b>Ждёт очереди</b> — документ
          свежее последнего документа второй базы: бухгалтерия просто ещё не
          дошла, это норма. <b>Просрочено</b> — во второй базе уже есть
          документы более поздние, а этот так и не появился: его пропустили,
          и вот это надо разбирать.
          <br />
          Пара ищется по количеству, а не по сумме: штуки в базах одинаковы,
          а цены разные — в налоговой трансфертные. Дата тоже своя: документ
          проводят во второй базе позже, иногда через месяцы. Там, где вторая
          база ведёт лишь часть документов (у Хайджина налоговая — это ЭСФ на
          юрлиц и сводные), непарные помечены нейтрально «нет пары»: это
          устройство учёта, а не потеря.
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
                          {t.gaps_upr > 0 && <> · просрочено <b>{t.gaps_upr}</b></>}
                        </td>
                        <td className={`num ${t.gaps_tax ? 'sc-diff' : ''}`}>
                          {t.tax_absent ? <span className="muted">не ведётся</span>
                            : t.only_tax_count || '—'}
                          {t.gaps_tax > 0 && <> · просрочено <b>{t.gaps_tax}</b></>}
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
                                                ? <span className="sc-diff"
                                                    title="Во второй базе уже есть документы более поздние, чем этот, — значит его пропустили, а не просто не успели">
                                                    просрочено
                                                  </span>
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

    <Journal />
    </div>
  )
}

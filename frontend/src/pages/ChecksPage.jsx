import { Fragment, useEffect, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'
import { formatMoney } from '../utils'

// Даты в этом отчёте читают как в 1С — «27.08.2026», а не «2026-08-27».
const fdate = (iso) => (iso ? iso.split('-').reverse().join('.') : '—')

export default function ChecksPage() {
  const { can, user } = useAuth()
  const [resetting, setResetting] = useState(false)

  async function rebuildFromScratch() {
    if (
      !confirm(
        'Полностью очистить импортированные из 1С данные (продажи, оплаты, ' +
        'расходы, возвраты, остатки)?\n\nРучные данные — пользователи, платежи ' +
        'календаря, планы агентов, сопоставления имён — сохранятся.\n\n' +
        'После очистки запустите resendAll в Google Apps Script — данные ' +
        'зальются заново начисто.'
      )
    )
      return
    setResetting(true)
    try {
      const res = await api.resetImportedData()
      const n = Object.values(res.cleared).reduce((a, b) => a + b, 0)
      alert(
        `Готово: удалено ${n} записей.\n\nТеперь запустите resendAll в Apps ` +
        'Script — портал зальёт данные из 1С заново и без дублей.'
      )
      await load()
    } catch (e) {
      alert('Ошибка: ' + e.message)
    } finally {
      setResetting(false)
    }
  }
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [rule, setRule] = useState('')
  const [showAcked, setShowAcked] = useState(false)
  const [imports, setImports] = useState([])
  // Что 1С присылает, а портал не грузит: отчёт объясняет расхождение
  // остатков лучше любой догадки — документ был, импортёра не было.
  const [skipped, setSkipped] = useState(null)
  // На чём держится склейка номенклатуры. Пока строки без ключа есть,
  // «продано то, чего не закупали» будет появляться снова.
  const [coverage, setCoverage] = useState(null)
  // Что 1С сама считает недоделанным: непроведённое и помеченное на
  // удаление. Список её собственный — портал только сверяет его со своими
  // таблицами.
  const [problems, setProblems] = useState(null)
  // Проводки, введённые мимо документов, и сторно.
  const [manual, setManual] = useState(null)
  // Свежесть выгрузок: молчащий контур не выглядит поломкой — цифры просто
  // перестают меняться, и вчерашний остаток легко принять за сегодняшний.
  const [fresh, setFresh] = useState(null)
  // Документы, проведённые в одном контуре и не проведённые в другом. Пока
  // их не назвать поимённо, расхождение остатков и оборотов приходится
  // раскапывать вручную по выгрузкам.
  const [unposted, setUnposted] = useState(null)
  const [openType, setOpenType] = useState(null)

  async function load() {
    setError(null)
    try {
      const [d, logs, sk, cov, pr, me, fr, un] = await Promise.all([
        api.checks({ rule, include_acked: showAcked }),
        api.importLog(),
        api.skippedKinds().catch(() => null),
        api.guidCoverage().catch(() => null),
        api.problemDocs().catch(() => null),
        api.manualEntries().catch(() => null),
        api.freshness().catch(() => null),
        // Контуры сверяются по каждой фирме отдельно: базы 1С разные, и
        // «нет пары» у Хайджина ничего не говорит про Инновейв. Фирмы
        // приходят внутри ответа — выбор в шапке эндпоинт учитывает сам.
        api.taxUnposted().catch(() => null),
      ])
      setData(d)
      setImports(logs)
      setSkipped(sk)
      setCoverage(cov)
      setProblems(pr)
      setManual(me)
      setFresh(fr)
      setUnposted(un)
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rule, showAcked])

  async function ack(v) {
    try {
      if (v.acked) await api.unackViolation(v.vhash)
      else await api.ackViolation(v.vhash)
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  const activeTotal = data
    ? data.rules.reduce((s, r) => s + r.active, 0)
    : 0

  return (
    <div>
      <div className="page-header">
        <h1>Контроль данных</h1>
      </div>

      {error && <div className="error">{error}</div>}

      {/* Что проведено в одной базе 1С и не проведено в другой. Управленка и
          налоговая — разные базы, документ попадает во вторую руками; пока
          он не попал, товар в налоговой числится на складе, а выручки нет.
          Раньше это было видно только косвенно — расхождением остатков. */}
      {unposted?.firms?.length > 0 && (
        <div className="fresh-block">
          <h2 className="section-title">Управленка ↔ налоговая: непроведённое</h2>
          <p className="muted">
            Слева документ есть, справа нет. «Хвост» — документы свежее
            последнего документа второго контура: обычное отставание,
            бухгалтерия ещё не дошла. Всё остальное — дыра внутри закрытого
            периода, её и надо разбирать.
          </p>
          <div className="table-wrap compact">
            <table>
              <thead>
                <tr>
                  <th>Фирма</th><th>Документы</th>
                  <th className="num">Только в управленке</th>
                  <th className="num">Только в налоговой</th>
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
                            упр. {t.upr_last ? fdate(t.upr_last) : '—'} · нал.{' '}
                            {t.tax_last ? fdate(t.tax_last) : '—'}
                          </td>
                        </tr>
                        {open && (
                          <tr>
                            <td colSpan={5} className="doc-lines">
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
                                                : <span className="sc-diff">дыра</span>}
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

      {fresh?.rows?.length > 0 && (
        <div className="fresh-block">
          <h2 className="section-title">Свежесть выгрузок</h2>
          {fresh.stale_count > 0 ? (
            <p className="sc-diff">
              Молчит контуров: <b>{fresh.stale_count}</b>. Файлы в папке
              остались старые, автосинк исправно шлёт их снова — цифры на
              экране просто перестают меняться, и вчерашний остаток легко
              принять за сегодняшний.
            </p>
          ) : (
            <p className="muted">
              Все контуры присылали данные меньше {fresh.stale_hours} часов
              назад.
            </p>
          )}
          <div className="table-wrap compact">
            <table>
              <thead>
                <tr>
                  <th>Контур</th>
                  <th>Последний раз</th>
                  <th className="num">Часов назад</th>
                  <th className="num">Видов</th>
                </tr>
              </thead>
              <tbody>
                {fresh.rows.map((r) => (
                  <Fragment key={`${r.firm}-${r.ledger}`}>
                    <tr className={r.stale ? 'row-neg' : ''}>
                      <td>{r.label}</td>
                      <td>
                        {new Date(r.last_at + 'Z').toLocaleString('ru-RU')}
                      </td>
                      <td className={`num ${r.stale ? 'sc-diff' : 'sc-ok'}`}>
                        {r.hours_ago}
                      </td>
                      <td className="num muted">
                        {r.in_last_run} из {r.kinds}
                      </td>
                    </tr>
                    {r.silent.length > 0 && (
                      <tr>
                        <td colSpan={4} className="muted skipped-cols">
                          не пришли в последний заход:{' '}
                          {r.silent.map((s) => `${s.kind} (${s.hours_ago} ч)`).join(' · ')}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}


      {data && (
        <>
          {activeTotal === 0 && !showAcked ? (
            <div className="import-result">
              ✅ Активных нарушений нет — данные в порядке.
            </div>
          ) : (
            <div className="rules-grid">
              {data.rules.map((r) => (
                <button
                  key={r.rule}
                  className={`rule-card ${rule === r.rule ? 'rule-card-active' : ''} rule-${r.severity}`}
                  onClick={() => setRule(rule === r.rule ? '' : r.rule)}
                  title={r.hint}
                >
                  <span className="rule-count">{r.active}</span>
                  <span className="rule-title">{r.title}</span>
                </button>
              ))}
            </div>
          )}

          <div className="filters">
            <label className="filter-inline">
              <input
                type="checkbox"
                checked={showAcked}
                onChange={(e) => setShowAcked(e.target.checked)}
              />
              показать принятые
            </label>
            {rule && (
              <button className="btn btn-ghost" onClick={() => setRule('')}>
                Все правила
              </button>
            )}
          </div>

          <div className="table-wrap cards">
            <table>
              <thead>
                <tr>
                  <th>Важность</th>
                  <th>Нарушение</th>
                  <th>Документ</th>
                  <th>Дата</th>
                  <th>Контрагент</th>
                  <th>Детали</th>
                  {can.editPayments && <th></th>}
                </tr>
              </thead>
              <tbody>
                {data.violations.length === 0 && (
                  <tr>
                    <td colSpan={7} className="muted center">
                      Нарушений не найдено
                    </td>
                  </tr>
                )}
                {data.violations.map((v) => {
                  const meta = data.rules.find((r) => r.rule === v.rule)
                  return (
                    <tr key={v.vhash} className={v.acked ? 'row-acked' : ''}>
                      <td data-label="Важность">
                        <span
                          className={`badge ${
                            v.severity === 'critical' ? 'badge-overdue' : 'badge-planned'
                          }`}
                        >
                          {v.severity === 'critical' ? 'Критично' : 'Внимание'}
                        </span>
                      </td>
                      <td data-label="Нарушение">{meta?.title || v.rule}</td>
                      <td data-label="Документ">{v.doc_number || '—'}</td>
                      <td data-label="Дата">{v.date ? v.date.split('-').reverse().join('.') : '—'}</td>
                      <td data-label="Контрагент">{v.client || '—'}</td>
                      <td className="detail-cell" data-label="Детали">{v.detail}</td>
                      {can.editPayments && (
                        <td className="actions card-action">
                          <button
                            className="btn btn-sm"
                            title={
                              v.acked
                                ? `Принято: ${v.acked_by || ''} — вернуть в активные`
                                : 'Пометить как принятое (разобрано)'
                            }
                            onClick={() => ack(v)}
                          >
                            {v.acked ? '↩ Вернуть' : '✓ Принять'}
                          </button>
                        </td>
                      )}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </>
      )}

      {coverage?.sources?.length > 0 && (
        <>
          <h2 className="section-title">На чём держится склейка номенклатуры</h2>
          <p className="muted">
            Справочники 1С:{' '}
            {coverage.products > 0
              ? `номенклатура — ${coverage.products} позиций`
              : 'номенклатура не загружена'};{' '}
            {coverage.counterparties > 0
              ? `контрагенты — ${coverage.counterparties}`
              : 'контрагенты не загружены'}.
            {' '}Строки «без ключа» — те, где совпадение остаётся догадкой по
            написанию: именно они дают отрицательные остатки, «продано то,
            чего не закупали» и ручную возню с алиасами плательщиков.
          </p>
          <div className="table-wrap cards">
            <table>
              <thead>
                <tr>
                  <th>Источник</th>
                  <th className="num">Строк</th>
                  <th className="num">GUID из выгрузки</th>
                  <th className="num">Через справочник</th>
                  <th className="num">Без ключа</th>
                  <th>Чаще всего не опознано</th>
                </tr>
              </thead>
              <tbody>
                {coverage.sources.map((r) => (
                  <tr key={r.source}>
                    <td data-label="Источник">{r.source}</td>
                    <td className="num" data-label="Строк">{r.rows}</td>
                    <td className="num sc-ok" data-label="GUID">{r.direct || '—'}</td>
                    <td className="num" data-label="Справочник">{r.bridged || '—'}</td>
                    <td className={`num ${r.orphan ? 'sc-diff' : 'muted'}`}
                      data-label="Без ключа">{r.orphan || '—'}</td>
                    <td data-label="Не опознано">
                      {r.top_unmatched?.length ? (
                        <span className="muted skipped-cols">
                          {r.top_unmatched.map(([n, k]) => `${n} (${k})`).join(', ')}
                        </span>
                      ) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {skipped?.rows?.length > 0 && (
        <>
          <h2 className="section-title">Что 1С присылает, а портал не ведёт</h2>
          <p className="muted">
            Эти файлы приходят в папку выгрузок и пропускаются: импортёров для
            них нет. Виды, помеченные «двигает склад», — прямая причина того,
            что расчётный остаток не сходится с 1С: документ товар подвинул, а
            в наших движениях его нет вообще.
          </p>
          <div className="table-wrap cards">
            <table>
              <thead>
                <tr>
                  <th>Вид выгрузки</th>
                  <th>Влияние на остаток</th>
                  <th className="num">Файлов</th>
                  <th>Последний</th>
                </tr>
              </thead>
              <tbody>
                {skipped.rows.map((r) => (
                  <tr key={r.kind}>
                    <td data-label="Вид">{r.kind}</td>
                    <td data-label="Влияние"
                      className={r.moves_stock ? 'sc-diff' : 'muted'}>
                      {r.moves_stock ? 'двигает склад' : 'на остаток не влияет'}
                      {r.note && (
                        <span className="muted skipped-cols">{r.note}</span>
                      )}
                    </td>
                    <td className="num" data-label="Файлов">{r.files}</td>
                    <td data-label="Последний">
                      {new Date(r.last_at + 'Z').toLocaleDateString('ru-RU')}
                      <span className="muted"> · {r.last_file}</span>
                      {r.columns && (
                        <span className="muted skipped-cols">
                          колонки: {r.columns}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {problems?.total_rows > 0 && (
        <>
          <h2 className="section-title">Проблемные документы 1С</h2>
          <p className="muted">
            Список составляет сама 1С: непроведённые документы и помеченные на
            удаление. В учёте они не работают, но существуют — незакрытый
            авансовый отчёт, реализация, которую решили удалить, платёжка,
            которую не провели. Всего <b>{problems.total_rows}</b>.
          </p>

          {problems.leaked_total > 0 ? (
            <>
              <p className="sc-diff">
                В расчёты портала всё-таки попали{' '}
                <b>{problems.leaked_total} шт.</b> — эти строки считаются, хотя
                не должны:
              </p>
              <div className="table-wrap cards">
                <table>
                  <thead>
                    <tr>
                      <th>Дата</th><th>Документ</th><th>Статус</th>
                      <th>Контрагент</th><th className="num">Сумма</th>
                      <th>Где у нас</th>
                    </tr>
                  </thead>
                  <tbody>
                    {problems.leaked.map((r, i) => (
                      <tr key={`leak-${i}`}>
                        <td data-label="Дата">{r.date.split('-').reverse().join('.')}</td>
                        <td data-label="Документ">
                          {r.kind}
                          <span className="muted"> {r.doc_number || ''}</span>
                        </td>
                        <td data-label="Статус" className="sc-diff">{r.status}</td>
                        <td data-label="Контрагент">{r.counterparty || '—'}</td>
                        <td className="num" data-label="Сумма">
                          {r.amount == null ? '—' : formatMoney(r.amount, '')}
                        </td>
                        <td data-label="Где у нас">{r.in_portal.join(', ')}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <p className="sc-ok">
              Ни один из них в расчёты портала не попал — импортёры отсекли их
              все.
            </p>
          )}

          <p className="muted">Из чего состоит список:</p>
          <div className="table-wrap cards">
            <table>
              <thead>
                <tr>
                  <th>Вид документа</th>
                  <th className="num">Документов</th>
                  <th className="num">На сумму</th>
                </tr>
              </thead>
              <tbody>
                {problems.by_kind.map((r) => (
                  <tr key={r.kind}>
                    <td data-label="Вид">{r.kind}</td>
                    <td className="num" data-label="Документов">{r.docs}</td>
                    <td className="num" data-label="Сумма">
                      {r.amount ? formatMoney(r.amount, '') : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <td>
                    {problems.by_status.map((s) => `${s.status}: ${s.docs}`).join(' · ')}
                  </td>
                  <td className="num">{problems.total_rows}</td>
                  <td className="num">
                    {formatMoney(problems.by_kind.reduce((a, r) => a + r.amount, 0), '')}
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
        </>
      )}

      {manual?.total_rows > 0 && (
        <>
          <h2 className="section-title">Ручные операции и сторно</h2>
          <p className="muted">
            Обычная проводка рождается из документа — продали, оплатили,
            списали. Эти вписаны в обход документов, и в выгрузках продаж или
            оплат их не видно вовсе. Всего <b>{manual.total_rows}</b> на{' '}
            <b>{formatMoney(manual.amount)}</b>.
          </p>

          {manual.counted_anyway > 0 && (
            <p className="sc-diff">
              Из них <b>{manual.counted_anyway} шт.</b> — сторно документов,
              которые портал всё-таки считает целиком: 1С операцию сняла, а у
              нас она в расчёте. Это <b>{formatMoney(manual.counted_amount)}</b>{' '}
              мимо.
            </p>
          )}

          <div className="table-wrap cards">
            <table>
              <thead>
                <tr>
                  <th>Дата</th><th>Содержание</th><th className="num">Сумма</th>
                  <th>Сторнирует</th><th>Автор</th>
                </tr>
              </thead>
              <tbody>
                {manual.rows.map((r, i) => (
                  <tr key={`me-${i}`}>
                    <td data-label="Дата">{r.date.split('-').reverse().join('.')}</td>
                    <td data-label="Содержание">
                      {r.content || r.typical || '—'}
                      {r.comment && <span className="muted skipped-cols">{r.comment}</span>}
                    </td>
                    <td className="num" data-label="Сумма">
                      {r.amount == null ? '—' : formatMoney(r.amount, '')}
                    </td>
                    <td data-label="Сторнирует"
                      className={r.in_portal.length ? 'sc-diff' : 'muted'}>
                      {r.reversed_doc || '—'}
                      {r.in_portal.length > 0 && (
                        <span className="skipped-cols">
                          считается у нас: {r.in_portal.join(', ')}
                        </span>
                      )}
                    </td>
                    <td data-label="Автор" className="muted">{r.author || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {manual.shown < manual.total_rows && (
            <p className="muted">
              Показаны {manual.shown} из {manual.total_rows}.
            </p>
          )}
        </>
      )}

      <div className="journal-head">
        <h2 className="section-title">Журнал загрузок</h2>
        {user.role === 'admin' && (
          <button
            className="btn btn-danger btn-sm"
            disabled={resetting}
            onClick={rebuildFromScratch}
          >
            {resetting ? 'Очистка…' : '↻ Пересобрать всё из 1С'}
          </button>
        )}
      </div>
      <div className="table-wrap cards">
        <table>
          <thead>
            <tr>
              <th>Когда</th>
              <th>Файл</th>
              <th>Кто</th>
              <th className="num">Добавлено</th>
              <th className="num">Дублей</th>
              <th className="num">Ошибок</th>
              <th>Режим</th>
            </tr>
          </thead>
          <tbody>
            {imports.length === 0 && (
              <tr>
                <td colSpan={7} className="muted center">
                  Загрузок ещё не было
                </td>
              </tr>
            )}
            {imports.map((l) => (
              <tr key={l.id}>
                <td data-label="Когда">{new Date(l.created_at + 'Z').toLocaleString('ru-RU')}</td>
                <td data-label="Файл">{l.filename}</td>
                <td data-label="Кто">{l.user || '—'}</td>
                <td className="num" data-label="Добавлено">{l.added}</td>
                <td className="num" data-label="Дублей">{l.skipped}</td>
                <td className="num" data-label="Ошибок">{l.errors_count}</td>
                <td data-label="Режим">{l.replace_period ? 'замена периода' : 'дозагрузка'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

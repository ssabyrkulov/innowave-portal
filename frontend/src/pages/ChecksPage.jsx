import { useEffect, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'

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

  async function load() {
    setError(null)
    try {
      const [d, logs, sk] = await Promise.all([
        api.checks({ rule, include_acked: showAcked }),
        api.importLog(),
        api.skippedKinds().catch(() => null),
      ])
      setData(d)
      setImports(logs)
      setSkipped(sk)
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

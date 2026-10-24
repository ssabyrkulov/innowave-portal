import { useEffect, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'

// Задачи бухгалтерии в Telegram. Портал после каждой выгрузки сверяет
// управленку с налоговой — документы, остатки товаров, оплаты — и всё, что не
// сходится, отправляет в группу бухгалтерии. Здесь: подключение бота,
// ответственные и тот же список задач, что закреплён в группе.
const FIRMS = { hygiene: 'Innowave Hygiene', innowave: 'Innowave', bluecarbon: 'Blue Carbon' }
const SHORT = { hygiene: 'Hygiene', innowave: 'Innowave', bluecarbon: 'BC' }
const CHECKS = [
  ['doc_orgs', 'Документы', 'реализации, возвраты, списания, поступления без пары во второй базе'],
  ['payment_orgs', 'Оплаты', 'оплата покупателя есть в одной базе и нет в другой'],
  ['stock_orgs', 'Остатки', 'штуки по товару в налоговой ≠ управленке'],
]
const STATE = {
  open: ['☐', ''],
  check: ['⏳', 'muted'],
  done: ['✅', 'tb-done'],
  acked: ['🚫', 'tb-done'],
}

export default function TaskBotPanel() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const canSync = isAdmin || user?.role === 'accountant'
  const [cfg, setCfg] = useState(null)
  const [form, setForm] = useState(null)
  const [tasks, setTasks] = useState(null)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState(null)

  function load() {
    api.taskbotSettings().then((c) => { setCfg(c); setForm(c) })
      .catch((e) => setError(e.message))
    api.taskbotTasks().then(setTasks).catch(() => setTasks(null))
  }
  useEffect(load, [])

  async function run(label, fn) {
    setBusy(label)
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  const connect = () => run('connect', async () => {
    const c = await api.taskbotConnect(window.location.origin)
    setCfg(c); setForm(c)
  })
  const sync = () => run('sync', async () => {
    await api.taskbotSync()
    load()
  })
  const save = () => run('save', async () => {
    const c = await api.taskbotSave({
      enabled: form.enabled,
      control_from: form.control_from || null,
      doc_orgs: form.doc_orgs, payment_orgs: form.payment_orgs,
      stock_orgs: form.stock_orgs,
      upr_user_id: form.upr_user_id || null,
      nal_user_id: form.nal_user_id || null,
      owner_user_id: form.owner_user_id || null,
      escalate_days: Number(form.escalate_days),
      grace_days: Number(form.grace_days),
      item_aliases: form.item_aliases,
    })
    setCfg(c); setForm(c)
  })

  function toggleOrg(key, org) {
    const cur = new Set(form[key])
    if (cur.has(org)) cur.delete(org)
    else cur.add(org)
    setForm({ ...form, [key]: [...cur] })
  }

  if (!cfg) return error ? <div className="error">{error}</div> : null

  const openTasks = tasks?.rows.filter((t) => t.state === 'open' || t.state === 'check') || []
  const userSelect = (key) => (
    <select value={form[key] || ''} disabled={!isAdmin}
      onChange={(e) => setForm({ ...form, [key]: Number(e.target.value) || null })}>
      <option value="">— не назначен —</option>
      {cfg.users.map((u) => (
        <option key={u.id} value={u.id}>
          {u.name}{u.telegram ? ' · Telegram ✓' : ''}
        </option>
      ))}
    </select>
  )

  return (
    <div className="fresh-block tb-panel">
      <div className="tb-head">
        <h2 className="section-title">Задачи в Telegram</h2>
        <span className="muted">
          {!cfg.token_set ? 'бот не настроен'
            : !cfg.chat_bound ? 'группа не подключена'
              : `группа «${cfg.chat_title || 'бухгалтерия'}» · открыто ${openTasks.length}`}
        </span>
        {canSync && (
          <button className="btn btn-sm" disabled={!!busy} onClick={sync}>
            {busy === 'sync' ? 'Сверяю…' : 'Сверить сейчас'}
          </button>
        )}
        {isAdmin && (
          <button className="btn btn-sm btn-ghost" onClick={() => setOpen(!open)}>
            {open ? 'Скрыть настройки' : 'Настройки'}
          </button>
        )}
      </div>
      {error && <div className="error">{error}</div>}

      {!cfg.token_set && (
        <p className="muted">
          Нужен токен бота: переменная <code>TELEGRAM_BOT_TOKEN</code> в
          окружении Render (тот же бот, что присылает согласования платежей).
          После сохранения переменной сервис перезапустится сам.
        </p>
      )}

      {cfg.token_set && isAdmin && !cfg.chat_bound && (
        <div className="tb-steps">
          <ol>
            <li>Создайте группу в Telegram, добавьте бухгалтеров и бота, сделайте
              бота администратором (чтобы он мог закреплять список).</li>
            <li>
              <button className="btn btn-sm" disabled={!!busy} onClick={connect}>
                {busy === 'connect' ? 'Подключаю…' : 'Подключить бота'}
              </button>{' '}
              {cfg.bot_username && <span className="muted">бот @{cfg.bot_username}</span>}
            </li>
            {cfg.bind_code && (
              <li>Отправьте в группу команду: <code>/bind {cfg.bind_code}</code></li>
            )}
          </ol>
        </div>
      )}

      {cfg.my_link && (
        <p className="muted">
          {cfg.me_linked ? 'Ваш Telegram привязан ✓. ' : ''}
          <a href={cfg.my_link} target="_blank" rel="noreferrer">
            {cfg.me_linked ? 'Привязать заново' : 'Привязать мой Telegram'}
          </a>{' '}
          — нужно один раз каждому, кто нажимает кнопки в группе.
        </p>
      )}

      {open && form && (
        <div className="tb-settings">
          <div className="tb-grid">
            <label>Ответственный за налоговую {userSelect('nal_user_id')}</label>
            <label>Ответственный за управленку {userSelect('upr_user_id')}</label>
            <label>Эскалация руководителю {userSelect('owner_user_id')}</label>
            <label>Контроль с даты
              <input type="date" value={form.control_from || ''}
                onChange={(e) => setForm({ ...form, control_from: e.target.value })} />
            </label>
            <label>Дней на внесение во вторую базу
              <input type="number" min="0" value={form.grace_days}
                onChange={(e) => setForm({ ...form, grace_days: e.target.value })} />
            </label>
            <label>Эскалация через, дней
              <input type="number" min="1" value={form.escalate_days}
                onChange={(e) => setForm({ ...form, escalate_days: e.target.value })} />
            </label>
          </div>
          <table className="tb-checks">
            <thead>
              <tr><th>Проверка</th>{['innowave', 'hygiene', 'bluecarbon'].map(
                (o) => <th key={o}>{FIRMS[o]}</th>)}</tr>
            </thead>
            <tbody>
              {CHECKS.map(([key, label, hint]) => (
                <tr key={key}>
                  <td>{label}<div className="muted skipped-cols">{hint}</div></td>
                  {['innowave', 'hygiene', 'bluecarbon'].map((o) => (
                    <td key={o}>
                      <input type="checkbox" checked={form[key].includes(o)}
                        onChange={() => toggleOrg(key, o)} />
                      {key !== 'doc_orgs' && !cfg.nal_ledger[o] && (
                        <span className="muted" title="Налоговый журнал проводок этой фирмы ещё не загружен">
                          {' '}нет журнала
                        </span>
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted">
            Остатки Hygiene в налоговой законно отстают от управленки до
            проведения ККМ — включайте их, когда налоговая выровнена.
          </p>
          <label className="tb-aliases">
            Один товар — разные названия, по строке «налоговое = управленческое»
            <textarea rows={3} value={form.item_aliases}
              onChange={(e) => setForm({ ...form, item_aliases: e.target.value })} />
          </label>
          <label className="tb-enabled">
            <input type="checkbox" checked={form.enabled}
              onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
            Бот присылает задачи в группу
          </label>
          <button className="btn btn-sm" disabled={!!busy} onClick={save}>
            {busy === 'save' ? 'Сохраняю…' : 'Сохранить'}
          </button>
          {cfg.token_set && cfg.chat_bound && (
            <> <button className="btn btn-sm btn-ghost" disabled={!!busy} onClick={connect}>
              Переподключить группу
            </button></>
          )}
          {cfg.bind_code && cfg.chat_bound && (
            <span className="muted"> · новая группа: <code>/bind {cfg.bind_code}</code></span>
          )}
        </div>
      )}

      {tasks && tasks.rows.length > 0 ? (
        <div className="table-wrap compact">
          <table>
            <tbody>
              {tasks.rows.map((t) => {
                const [icon, cls] = STATE[t.state]
                return (
                  <tr key={t.id} className={cls}>
                    <td>{icon}</td>
                    <td className="muted">{SHORT[t.organization] || t.organization}</td>
                    <td>
                      {t.state === 'done' || t.state === 'acked' ? <s>{t.text}</s> : t.text}
                      {t.state === 'check' && <span className="muted"> · {t.claimed_by}, проверяю</span>}
                      {t.state === 'acked' && <span className="muted"> — {t.note}</span>}
                    </td>
                    <td className="muted">{t.responsible || 'не назначен'}</td>
                    <td className={t.state === 'open' && t.age_days >= tasks.escalate_days ? 'sc-diff' : 'muted'}>
                      {t.state === 'open' || t.state === 'check' ? `${t.age_days} дн.` : ''}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : tasks && (
        <p className="muted">Задач нет: управленка и налоговая сходятся.</p>
      )}
    </div>
  )
}

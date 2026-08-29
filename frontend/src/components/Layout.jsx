import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { api, getOrg, setOrg } from '../api'
import { useAuth } from '../auth'

const ORG_OPTIONS = [
  { value: 'all', label: 'Обе фирмы' },
  { value: 'hygiene', label: 'Innowave Hygiene' },
  { value: 'innowave', label: 'Innowave' },
]

/* Отпечаток версии. Дважды в неделю всплывает один и тот же вопрос —
   «правку сделали, а изменений нет». Причин ровно две: сервер ещё не
   пересобрался или браузер держит старый index.html. Здесь видно обе:
   слева — что развёрнуто на сервере, и если загруженный в браузере бандл
   не тот, что отдаёт сервер, показываем это прямо. */
function VersionBadge() {
  const [srv, setSrv] = useState(null)
  useEffect(() => {
    api.health().then(setSrv).catch(() => {})
  }, [])
  if (!srv) return null
  // Имя файла модуля, который реально исполняется в браузере.
  const loaded = (import.meta.url || '').split('/').pop() || ''
  const served = srv.frontend?.bundle || ''
  const stale = Boolean(served && loaded && served !== loaded)
  const built = srv.frontend?.built_at
    ? new Date(srv.frontend.built_at).toLocaleString('ru-RU',
        { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
    : null
  return (
    <div className="version-badge nav-txt">
      <span title={`бандл сервера: ${served || '—'}\nбандл в браузере: ${loaded || '—'}`}>
        версия {srv.commit}{built ? ` · ${built}` : ''}
      </span>
      {stale && (
        <button className="version-stale" onClick={() => window.location.reload(true)}>
          Открыта старая версия — обновить
        </button>
      )}
    </div>
  )
}

function OrgSwitch() {
  const [org, setOrgState] = useState(getOrg())
  function change(v) {
    setOrgState(v)
    setOrg(v)
    // Полная перезагрузка — надёжно применяет выбор во всех разделах сразу.
    window.location.reload()
  }
  return (
    <select className="org-switch" value={org} onChange={(e) => change(e.target.value)}>
      {ORG_OPTIONS.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  )
}

const ROLE_LABELS = {
  admin: 'Администратор',
  accountant: 'Бухгалтер',
  viewer: 'Наблюдатель',
}

// Единая модель навигации. primary — нижняя панель на телефоне и верх списка
// на десктопе; more — прячется в лист «Ещё» на телефоне.
const PRIMARY = [
  { to: '/', end: true, icon: '🏠', label: 'Главная' },
  { to: '/calendar', icon: '📅', label: 'Календарь' },
  { to: '/debt', icon: '💰', label: 'Дебиторка' },
  { to: '/checks', icon: '🛡', label: 'Контроль', badge: true },
]
const SECONDARY = [
  { to: '/work', icon: '🧭', label: 'Мой день', workOnly: true },
  { to: '/payments', icon: '📋', label: 'Платежи' },
  { to: '/analytics', icon: '📊', label: 'Аналитика' },
  { to: '/operations', icon: '🗂', label: 'Операции' },
  { to: '/agents', icon: '🧑‍💼', label: 'Агенты' },
  { to: '/stock', icon: '📦', label: 'Остатки' },
  { to: '/budget', icon: '📈', label: 'БДДС', editOnly: true },
  { to: '/salesdoc', icon: '⚖️', label: 'Сверка SD', editOnly: true },
  { to: '/id-match', icon: '🔗', label: 'Сверка по ID', editOnly: true },
  { to: '/tax', icon: '🧾', label: 'Налоговая', editOnly: true },
  { to: '/contours', icon: '⚖', label: 'Контуры 1С', editOnly: true,
    contourBadge: true },
  { to: '/accounting', icon: '📚', label: 'Учёт 1С', adminOnly: true },
  { to: '/tools/unit-economics', icon: '🧮', label: 'Юнит-экономика', adminOnly: true },
  { to: '/users', icon: '👥', label: 'Пользователи', adminOnly: true },
]

export default function Layout() {
  const { user, logout, can } = useAuth()
  const [alerts, setAlerts] = useState(null)
  // Открытые расхождения контуров: документ, проведённый в одной базе 1С и
  // не проведённый в другой. Без счётчика о нём узнают, только если зайти
  // на страницу, — а узнать надо сразу.
  const [contour, setContour] = useState(null)
  const [moreOpen, setMoreOpen] = useState(false)
  // Свёрнутая боковая панель (только десктоп) — состояние запоминаем.
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem('pc_sidebar_collapsed') === '1'
  )
  const location = useLocation()

  function toggleSidebar() {
    setCollapsed((c) => {
      const next = !c
      localStorage.setItem('pc_sidebar_collapsed', next ? '1' : '0')
      return next
    })
  }

  // Обновляем счётчик нарушений при смене раздела (и при входе)
  useEffect(() => {
    api
      .checksCount()
      .then(setAlerts)
      .catch(() => {})
    api
      .contourEvents('open')
      .then((d) => setContour(d.open_gaps))
      .catch(() => {})
  }, [location.pathname])

  // Закрываем лист «Ещё» при любой навигации
  useEffect(() => setMoreOpen(false), [location.pathname])

  const alertTotal = alerts ? alerts.critical + alerts.warning : 0
  const badge =
    alertTotal > 0 ? (
      <span
        className={`nav-badge ${alerts.critical > 0 ? 'nav-badge-critical' : ''}`}
        title={`Критичных: ${alerts.critical}, предупреждений: ${alerts.warning}`}
      >
        {alertTotal > 99 ? '99+' : alertTotal}
      </span>
    ) : null

  // Считаем только просроченные: документы, которые «ждут очереди», есть
  // всегда — из-за них счётчик горел бы вечно и перестал бы что-то значить.
  const contourBadge =
    contour > 0 ? (
      <span className="nav-badge nav-badge-critical"
        title={`Документов, пропущенных во второй базе 1С: ${contour}`}>
        {contour > 99 ? '99+' : contour}
      </span>
    ) : null

  const initials = user.full_name
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0])
    .join('')
    .toUpperCase()

  const secondaryVisible = SECONDARY.filter(
    (i) =>
      (!i.adminOnly || can.manageUsers) &&
      (!i.editOnly || can.editPayments) &&
      (!i.workOnly || can.work)
  )
  // раздел активен по «Ещё», если открыта одна из вторичных страниц
  const inSecondary = secondaryVisible.some((i) => i.to === location.pathname)

  return (
    <div className="app">
      {/* ---------- Десктоп: боковое меню ---------- */}
      <aside className={`sidebar ${collapsed ? 'collapsed' : ''}`}>
        <button
          className="sidebar-toggle"
          onClick={toggleSidebar}
          title={collapsed ? 'Развернуть меню' : 'Свернуть меню'}
          aria-label={collapsed ? 'Развернуть меню' : 'Свернуть меню'}
        >
          {collapsed ? '»' : '«'}
        </button>

        <div className="brand">
          <div className="brand-mark">IW</div>
          <div className="brand-text">
            <div className="brand-title">InnoWave Group</div>
            <div className="brand-sub">Корпоративный портал</div>
          </div>
        </div>

        <OrgSwitch />

        <nav className="nav">
          {PRIMARY.map((i) => (
            <NavLink key={i.to} to={i.to} end={i.end} className="nav-link"
              title={collapsed ? i.label : undefined}>
              <span className="nav-ico">{i.icon}</span>
              <span className="nav-txt">{i.label}</span>
              {i.badge && badge}
            </NavLink>
          ))}
          {secondaryVisible.map((i) => (
            <NavLink key={i.to} to={i.to} className="nav-link"
              title={collapsed ? i.label : undefined}>
              <span className="nav-ico">{i.icon}</span>
              <span className="nav-txt">{i.label}</span>
              {i.contourBadge && contourBadge}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="user-card">
            <div className="avatar">{initials}</div>
            <div className="user-text">
              <div className="user-name">{user.full_name}</div>
              <div className="user-role">{ROLE_LABELS[user.role] || user.role}</div>
            </div>
          </div>
          <button className="btn btn-ghost btn-logout" onClick={logout}
            title={collapsed ? 'Выйти' : undefined}>
            <span className="nav-txt">Выйти</span>
            <span className="logout-ico">⎋</span>
          </button>
          <VersionBadge />
        </div>
      </aside>

      {/* ---------- Телефон: верхняя панель приложения ----------
          Только бренд: раздел и так подсвечен в нижних вкладках, а меню
          открывается вкладкой «Ещё» — дубли сверху убраны. */}
      <header className="appbar">
        <div className="appbar-brand">
          <div className="brand-mark">IW</div>
          <span className="appbar-title">InnoWave Group</span>
        </div>
        <OrgSwitch />
      </header>

      <main className="content">
        <Outlet />
      </main>

      {/* ---------- Телефон: нижняя панель вкладок ---------- */}
      <nav className="tabbar">
        {PRIMARY.map((i) => (
          <NavLink key={i.to} to={i.to} end={i.end} className="tab">
            <span className="tab-icon">
              {i.icon}
              {i.badge && badge}
            </span>
            <span className="tab-label">{i.label}</span>
          </NavLink>
        ))}
        <button
          className={`tab tab-more ${moreOpen || inSecondary ? 'tab-active' : ''}`}
          onClick={() => setMoreOpen(true)}
        >
          <span className="tab-icon">⋯</span>
          <span className="tab-label">Ещё</span>
        </button>
      </nav>

      {/* ---------- Телефон: нижний лист «Ещё» ---------- */}
      {moreOpen && (
        <div className="sheet-backdrop" onClick={() => setMoreOpen(false)}>
          <div
            className="sheet"
            role="dialog"
            aria-label="Ещё"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="sheet-grip" />
            <div className="sheet-user">
              <div className="avatar">{initials}</div>
              <div>
                <div className="user-name">{user.full_name}</div>
                <div className="user-role">{ROLE_LABELS[user.role] || user.role}</div>
              </div>
            </div>
            <div className="sheet-links">
              {secondaryVisible.map((i) => (
                <NavLink key={i.to} to={i.to} className="sheet-link">
                  <span className="sheet-link-icon">{i.icon}</span>
                  {i.label}
                  {i.contourBadge && contourBadge}
                </NavLink>
              ))}
            </div>
            <button className="btn btn-ghost sheet-logout" onClick={logout}>
              Выйти
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../auth'

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
  { to: '/payments', icon: '📋', label: 'Платежи' },
  { to: '/analytics', icon: '📊', label: 'Аналитика' },
  { to: '/agents', icon: '🧑‍💼', label: 'Агенты' },
  { to: '/users', icon: '👥', label: 'Пользователи', adminOnly: true },
]

const TITLES = {
  '/': 'Главная',
  '/calendar': 'Календарь',
  '/payments': 'Платежи',
  '/analytics': 'Аналитика',
  '/debt': 'Дебиторка',
  '/agents': 'Агенты',
  '/checks': 'Контроль',
  '/users': 'Пользователи',
}

export default function Layout() {
  const { user, logout, can } = useAuth()
  const [alerts, setAlerts] = useState(null)
  const [moreOpen, setMoreOpen] = useState(false)
  const location = useLocation()

  // Обновляем счётчик нарушений при смене раздела (и при входе)
  useEffect(() => {
    api
      .checksCount()
      .then(setAlerts)
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

  const initials = user.full_name
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0])
    .join('')
    .toUpperCase()

  const secondaryVisible = SECONDARY.filter((i) => !i.adminOnly || can.manageUsers)
  const title = TITLES[location.pathname] || 'InnoWave Group'
  // раздел активен по «Ещё», если открыта одна из вторичных страниц
  const inSecondary = secondaryVisible.some((i) => i.to === location.pathname)

  return (
    <div className="app">
      {/* ---------- Десктоп: боковое меню ---------- */}
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">IW</div>
          <div>
            <div className="brand-title">InnoWave Group</div>
            <div className="brand-sub">Корпоративный портал</div>
          </div>
        </div>

        <nav className="nav">
          {PRIMARY.map((i) => (
            <NavLink key={i.to} to={i.to} end={i.end} className="nav-link">
              {i.icon} {i.label}
              {i.badge && badge}
            </NavLink>
          ))}
          {secondaryVisible.map((i) => (
            <NavLink key={i.to} to={i.to} className="nav-link">
              {i.icon} {i.label}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="user-card">
            <div className="avatar">{initials}</div>
            <div>
              <div className="user-name">{user.full_name}</div>
              <div className="user-role">{ROLE_LABELS[user.role] || user.role}</div>
            </div>
          </div>
          <button className="btn btn-ghost btn-logout" onClick={logout}>
            Выйти
          </button>
        </div>
      </aside>

      {/* ---------- Телефон: верхняя панель приложения ---------- */}
      <header className="appbar">
        <div className="appbar-brand">
          <div className="brand-mark">IW</div>
          <span className="appbar-title">{title}</span>
        </div>
        <button
          className="appbar-avatar"
          onClick={() => setMoreOpen(true)}
          aria-label="Меню"
        >
          {initials}
        </button>
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

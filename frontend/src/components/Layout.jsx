import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../auth'

const ROLE_LABELS = {
  admin: 'Администратор',
  accountant: 'Бухгалтер',
  viewer: 'Наблюдатель',
}

export default function Layout() {
  const { user, logout, can } = useAuth()
  const [alerts, setAlerts] = useState(null)
  const location = useLocation()

  // Обновляем счётчик нарушений при смене раздела (и при входе)
  useEffect(() => {
    api
      .checksCount()
      .then(setAlerts)
      .catch(() => {})
  }, [location.pathname])

  const alertTotal = alerts ? alerts.critical + alerts.warning : 0

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">IW</div>
          <div>
            <div className="brand-title">InnoWave Group</div>
            <div className="brand-sub">Корпоративный портал</div>
          </div>
        </div>

        <nav className="nav">
          <NavLink to="/" end className="nav-link">
            📅 Календарь
          </NavLink>
          <NavLink to="/payments" className="nav-link">
            📋 Платежи
          </NavLink>
          <NavLink to="/analytics" className="nav-link">
            📊 Аналитика
          </NavLink>
          <NavLink to="/debt" className="nav-link">
            💰 Дебиторка
          </NavLink>
          <NavLink to="/agents" className="nav-link">
            🧑‍💼 Агенты
          </NavLink>
          <NavLink to="/checks" className="nav-link">
            🛡 Контроль
            {alertTotal > 0 && (
              <span
                className={`nav-badge ${
                  alerts.critical > 0 ? 'nav-badge-critical' : ''
                }`}
                title={`Критичных: ${alerts.critical}, предупреждений: ${alerts.warning}`}
              >
                {alertTotal > 99 ? '99+' : alertTotal}
              </span>
            )}
          </NavLink>
          {can.manageUsers && (
            <NavLink to="/users" className="nav-link">
              👥 Пользователи
            </NavLink>
          )}
        </nav>

        <div className="sidebar-footer">
          <div className="user-card">
            <div className="user-name">{user.full_name}</div>
            <div className="user-role">{ROLE_LABELS[user.role] || user.role}</div>
          </div>
          <button className="btn btn-ghost" onClick={logout}>
            Выйти
          </button>
        </div>
      </aside>

      <main className="content">
        <Outlet />
      </main>
    </div>
  )
}

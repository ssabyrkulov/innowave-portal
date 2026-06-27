import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import { useAuth } from './auth'
import Login from './pages/Login'
import CalendarPage from './pages/CalendarPage'
import PaymentsPage from './pages/PaymentsPage'
import UsersPage from './pages/UsersPage'

function Protected({ children, adminOnly = false }) {
  const { user, loading } = useAuth()
  if (loading) return <div className="center muted">Загрузка…</div>
  if (!user) return <Navigate to="/login" replace />
  if (adminOnly && user.role !== 'admin') return <Navigate to="/" replace />
  return children
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        path="/"
        element={
          <Protected>
            <Layout />
          </Protected>
        }
      >
        <Route index element={<CalendarPage />} />
        <Route path="payments" element={<PaymentsPage />} />
        <Route
          path="users"
          element={
            <Protected adminOnly>
              <UsersPage />
            </Protected>
          }
        />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}

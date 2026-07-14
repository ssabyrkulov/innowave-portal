import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import { useAuth } from './auth'
import Login from './pages/Login'
import AnalyticsPage from './pages/AnalyticsPage'
import DashboardPage from './pages/DashboardPage'
import AgentsPage from './pages/AgentsPage'
import DebtPage from './pages/DebtPage'
import CalendarPage from './pages/CalendarPage'
import ChecksPage from './pages/ChecksPage'
import PaymentsPage from './pages/PaymentsPage'
import UsersPage from './pages/UsersPage'
import UnitEconomicsPage from './pages/UnitEconomicsPage'
import BudgetPage from './pages/BudgetPage'
import StockPage from './pages/StockPage'

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
        <Route index element={<DashboardPage />} />
        <Route path="calendar" element={<CalendarPage />} />
        <Route path="payments" element={<PaymentsPage />} />
        <Route path="analytics" element={<AnalyticsPage />} />
        <Route path="checks" element={<ChecksPage />} />
        <Route path="debt" element={<DebtPage />} />
        <Route path="agents" element={<AgentsPage />} />
        <Route path="budget" element={<BudgetPage />} />
        <Route path="stock" element={<StockPage />} />
        <Route
          path="tools/unit-economics"
          element={
            <Protected adminOnly>
              <UnitEconomicsPage />
            </Protected>
          }
        />
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

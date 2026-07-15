import { createContext, useContext, useEffect, useState } from 'react'
import { api, getToken, setToken } from './api'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const token = getToken()
    if (!token) {
      setLoading(false)
      return
    }
    api
      .me()
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setLoading(false))
  }, [])

  async function login(email, password) {
    const { access_token } = await api.login(email, password)
    setToken(access_token)
    const me = await api.me()
    setUser(me)
    return me
  }

  function logout() {
    setToken(null)
    setUser(null)
  }

  const can = {
    manageUsers: user?.role === 'admin',
    editPayments: user?.role === 'admin' || user?.role === 'accountant',
    isAgent: Boolean(user?.agent_name),
    work: user?.role === 'admin' || Boolean(user?.agent_name),
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, can }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}

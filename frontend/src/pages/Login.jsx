import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth'

export default function Login() {
  const { login, user } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  if (user) {
    navigate('/', { replace: true })
  }

  async function submit(e) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      await login(email, password)
      navigate('/', { replace: true })
    } catch (err) {
      setError(err.message)
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <div className="brand login-brand">
          <div className="brand-mark">IW</div>
          <div>
            <div className="brand-title">InnoWave Group</div>
            <div className="brand-sub">Платёжный календарь</div>
          </div>
        </div>

        <label>
          Email
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="admin@innowave.group"
            autoFocus
          />
        </label>
        <label>
          Пароль
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>

        {error && <div className="error">{error}</div>}

        <button type="submit" className="btn btn-primary btn-block" disabled={loading}>
          {loading ? 'Вход…' : 'Войти'}
        </button>
      </form>
    </div>
  )
}

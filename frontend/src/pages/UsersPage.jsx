import { useEffect, useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'

const ROLE_LABELS = {
  admin: 'Администратор',
  accountant: 'Бухгалтер',
  viewer: 'Наблюдатель',
}

const EMPTY = { email: '', full_name: '', role: 'viewer', password: '' }

export default function UsersPage() {
  const { user: me } = useAuth()
  const [users, setUsers] = useState([])
  const [error, setError] = useState(null)
  const [modal, setModal] = useState(null)

  async function load() {
    setError(null)
    try {
      setUsers(await api.listUsers())
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function toggleActive(u) {
    try {
      await api.updateUser(u.id, { is_active: !u.is_active })
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function remove(u) {
    if (!confirm(`Удалить пользователя ${u.full_name}?`)) return
    try {
      await api.deleteUser(u.id)
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div>
      <div className="page-header">
        <h1>Пользователи</h1>
        <button className="btn btn-primary" onClick={() => setModal({ ...EMPTY })}>
          + Добавить пользователя
        </button>
      </div>

      {error && <div className="error">{error}</div>}

      <div className="table-wrap cards">
        <table>
          <thead>
            <tr>
              <th>Имя</th>
              <th>Email</th>
              <th>Роль</th>
              <th>Статус</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td data-label="Имя">
                  {u.full_name}
                  {u.id === me.id && <span className="tag-you">вы</span>}
                </td>
                <td data-label="Email">{u.email}</td>
                <td data-label="Роль">{ROLE_LABELS[u.role]}</td>
                <td data-label="Статус">
                  <span className={`badge ${u.is_active ? 'badge-paid' : 'badge-overdue'}`}>
                    {u.is_active ? 'Активен' : 'Отключён'}
                  </span>
                </td>
                <td className="actions card-action">
                  <button className="btn btn-sm" onClick={() => setModal(u)}>
                    ✎
                  </button>
                  {u.id !== me.id && (
                    <>
                      <button className="btn btn-sm" onClick={() => toggleActive(u)}>
                        {u.is_active ? 'Отключить' : 'Включить'}
                      </button>
                      <button className="btn btn-sm btn-danger" onClick={() => remove(u)}>
                        🗑
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {modal && (
        <UserModal
          initial={modal}
          isSelf={modal.id === me.id}
          onClose={() => setModal(null)}
          onSaved={load}
        />
      )}
    </div>
  )
}

function UserModal({ initial, isSelf, onClose, onSaved }) {
  const isEdit = Boolean(initial.id)
  const [form, setForm] = useState({
    email: initial.email || '',
    full_name: initial.full_name || '',
    role: initial.role || 'viewer',
    password: '',
  })
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)

  function update(field, value) {
    setForm((f) => ({ ...f, [field]: value }))
  }

  async function submit(e) {
    e.preventDefault()
    setError(null)
    setSaving(true)
    try {
      if (isEdit) {
        const body = { full_name: form.full_name, role: form.role }
        if (form.password) body.password = form.password
        await api.updateUser(initial.id, body)
      } else {
        await api.createUser(form)
      }
      await onSaved()
      onClose()
    } catch (err) {
      setError(err.message)
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>{isEdit ? 'Редактировать пользователя' : 'Новый пользователь'}</h2>
        <form onSubmit={submit}>
          <label>
            Имя
            <input value={form.full_name} onChange={(e) => update('full_name', e.target.value)} />
          </label>
          <label>
            Email
            <input
              type="email"
              value={form.email}
              disabled={isEdit}
              onChange={(e) => update('email', e.target.value)}
            />
          </label>
          <label>
            Роль
            <select
              value={form.role}
              disabled={isSelf}
              onChange={(e) => update('role', e.target.value)}
            >
              <option value="admin">Администратор</option>
              <option value="accountant">Бухгалтер</option>
              <option value="viewer">Наблюдатель</option>
            </select>
          </label>
          <label>
            {isEdit ? 'Новый пароль (необязательно)' : 'Пароль'}
            <input
              type="password"
              value={form.password}
              onChange={(e) => update('password', e.target.value)}
              placeholder={isEdit ? 'Оставьте пустым, чтобы не менять' : ''}
            />
          </label>

          {error && <div className="error">{error}</div>}

          <div className="modal-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>
              Отмена
            </button>
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? 'Сохранение…' : 'Сохранить'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

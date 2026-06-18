import { useState } from 'react'
import { useAuth } from '../../hooks/useAuth'
import './AuthModal.css'

export default function AuthModal({ onClose }) {
  const { signUp, signIn } = useAuth()
  const [mode, setMode] = useState('login') // 'login' | 'register' | 'verify'
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [username, setUsername] = useState('')
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)

    if (mode === 'register') {
      if (username.length < 3 || username.length > 20) {
        setError('Username must be 3–20 characters')
        setSubmitting(false)
        return
      }
      if (password.length < 8) {
        setError('Password must be at least 8 characters')
        setSubmitting(false)
        return
      }
      const { error } = await signUp(email, password, username)
      if (error) setError(error.message)
      else setMode('verify')
    } else {
      const { error } = await signIn(email, password)
      if (error) setError(error.message)
      else onClose()
    }
    setSubmitting(false)
  }

  if (mode === 'verify') {
    return (
      <div className="auth-overlay" onClick={onClose}>
        <div className="auth-modal" onClick={e => e.stopPropagation()}>
          <button className="auth-close" onClick={onClose}>&times;</button>
          <h2>Check your email</h2>
          <p className="auth-subtitle">
            We sent a verification link to <strong>{email}</strong>.
            Click the link to activate your account.
          </p>
          <button className="auth-btn secondary" onClick={onClose}>Got it</button>
        </div>
      </div>
    )
  }

  return (
    <div className="auth-overlay" onClick={onClose}>
      <div className="auth-modal" onClick={e => e.stopPropagation()}>
        <button className="auth-close" onClick={onClose}>&times;</button>
        <h2>{mode === 'login' ? 'Sign in' : 'Create account'}</h2>
        <p className="auth-subtitle">
          {mode === 'login'
            ? 'Sign in to post reports'
            : 'Join the community'}
        </p>

        <form onSubmit={handleSubmit}>
          {mode === 'register' && (
            <input
              type="text"
              placeholder="Username"
              value={username}
              onChange={e => setUsername(e.target.value.replace(/[^a-zA-Z0-9_]/g, ''))}
              autoComplete="username"
              required
            />
          )}
          <input
            type="email"
            placeholder="Email"
            value={email}
            onChange={e => setEmail(e.target.value)}
            autoComplete="email"
            required
          />
          <input
            type="password"
            placeholder="Password"
            value={password}
            onChange={e => setPassword(e.target.value)}
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            required
            minLength={8}
          />

          {error && <div className="auth-error">{error}</div>}

          <button className="auth-btn primary" type="submit" disabled={submitting}>
            {submitting ? '...' : mode === 'login' ? 'Sign in' : 'Create account'}
          </button>
        </form>

        <div className="auth-switch">
          {mode === 'login' ? (
            <>No account? <button onClick={() => setMode('register')}>Register</button></>
          ) : (
            <>Have an account? <button onClick={() => setMode('login')}>Sign in</button></>
          )}
        </div>
      </div>
    </div>
  )
}

import { useState, useCallback } from 'react'
import { useAuth } from './hooks/useAuth'
import SnowMap from './components/map/SnowMap'
import AuthModal from './components/auth/AuthModal'
import UserBar from './components/ui/UserBar'
import ReportFAB from './components/reports/ReportFAB'
import ReportSheet from './components/reports/ReportSheet'
import EmailBanner from './components/auth/EmailBanner'
import './App.css'

export default function App() {
  const auth = useAuth()
  const [showAuth, setShowAuth] = useState(false)
  const [reportOpen, setReportOpen] = useState(false)

  const handleFAB = useCallback(() => {
    if (!auth.user) {
      setShowAuth(true)
      return
    }
    if (!auth.emailConfirmed) return
    setReportOpen(true)
  }, [auth.user, auth.emailConfirmed])

  return (
    <div className="app">
      <SnowMap />

      <UserBar
        user={auth.user}
        profile={auth.profile}
        onLogin={() => setShowAuth(true)}
        onLogout={auth.signOut}
      />

      {auth.user && !auth.emailConfirmed && (
        <EmailBanner
          email={auth.user.email}
          onResend={() => auth.resendConfirmation(auth.user.email)}
        />
      )}

      <ReportFAB onClick={handleFAB} />

      {reportOpen && (
        <ReportSheet
          user={auth.user}
          onClose={() => setReportOpen(false)}
        />
      )}

      {showAuth && <AuthModal onClose={() => setShowAuth(false)} />}
    </div>
  )
}

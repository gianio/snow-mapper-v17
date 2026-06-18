import { useState } from 'react'
import './EmailBanner.css'

export default function EmailBanner({ email, onResend }) {
  const [sent, setSent] = useState(false)

  async function handleResend() {
    await onResend()
    setSent(true)
    setTimeout(() => setSent(false), 5000)
  }

  return (
    <div className="email-banner">
      <span>Please confirm your email to post reports.</span>
      <button onClick={handleResend} disabled={sent}>
        {sent ? 'Sent!' : 'Resend email'}
      </button>
    </div>
  )
}

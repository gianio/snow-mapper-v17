import './UserBar.css'

export default function UserBar({ user, profile, onLogin, onLogout }) {
  return (
    <div className="user-bar">
      {user ? (
        <button className="user-pill" onClick={onLogout}>
          <span className="user-avatar">
            {profile?.avatar_url
              ? <img src={profile.avatar_url} alt="" />
              : <span>{(profile?.username || user.email)[0].toUpperCase()}</span>}
          </span>
          <span className="user-name">@{profile?.username || 'user'}</span>
        </button>
      ) : (
        <button className="login-btn" onClick={onLogin}>Sign in</button>
      )}
    </div>
  )
}

import { useReducer, useEffect, useRef, useCallback } from 'react'
import { supabase } from '../../lib/supabase'
import './ReportSheet.css'

const CATEGORIES = [
  { key: 'snow', emoji: '❄️', label: 'Schnee' },
  { key: 'route', emoji: '⛷️', label: 'Route' },
  { key: 'danger', emoji: '⚠️', label: 'Gefahr' },
  { key: 'tour', emoji: '✅', label: 'Tour' },
  { key: 'info', emoji: 'ℹ️', label: 'Info' },
]

const SNOW_TYPES = [
  { key: 'fresh_powder', emoji: '❄️', label: 'Frischer Powder' },
  { key: 'compact', emoji: '🎿', label: 'Kompakt/Race' },
  { key: 'firn', emoji: '🌅', label: 'Firn' },
  { key: 'breakable_crust', emoji: '💥', label: 'Bruchharsch' },
  { key: 'wind_pressed', emoji: '💨', label: 'Windgepresst' },
  { key: 'wet_snow', emoji: '💧', label: 'Nassschnee' },
  { key: 'hard_ice', emoji: '🧊', label: 'Hart/Eis' },
  { key: 'snow_free', emoji: '🟫', label: 'Schneefrei' },
]

const DEPTH_BUCKETS = ['<20', '20-50', '50-100', '>100']

const TOUR_RATINGS = [
  { key: 'green', emoji: '🟢', label: 'Top' },
  { key: 'yellow', emoji: '🟡', label: 'Ja, aber' },
  { key: 'orange', emoji: '🟠', label: 'Eher nicht' },
  { key: 'red', emoji: '🔴', label: 'Nein' },
]

const DANGER_TYPES = [
  { key: 'avalanche', emoji: '🌊', label: 'Lawine' },
  { key: 'wumm', emoji: '🏔️', label: 'Wumm' },
  { key: 'cornice', emoji: '🌀', label: 'Wächte' },
  { key: 'crevasse', emoji: '🕳️', label: 'Spalten' },
  { key: 'ice', emoji: '🧊', label: 'Blankeis' },
  { key: 'rockfall', emoji: '🪨', label: 'Steinschlag' },
  { key: 'impassable', emoji: '🚫', label: 'Unpassierbar' },
]

const INFO_TYPES = [
  { key: 'hut', emoji: '🏠', label: 'Hütte' },
  { key: 'road', emoji: '🛣️', label: 'Strasse' },
  { key: 'parking', emoji: '🅿️', label: 'Parkplatz' },
  { key: 'closure', emoji: '🔒', label: 'Sperrung' },
  { key: 'network', emoji: '📡', label: 'Netz' },
  { key: 'emergency', emoji: '🆘', label: 'Notfall' },
  { key: 'other', emoji: '📰', label: 'Sonstiges' },
]

const initialState = {
  photo: null,
  photoUrl: null,
  location: null,
  locationName: null,
  elevation: null,
  categories: [],
  subtype: null,
  conditionData: {},
  caption: '',
  hashtags: [],
  submitting: false,
}

function reducer(state, action) {
  switch (action.type) {
    case 'SET_PHOTO': return { ...state, photo: action.file, photoUrl: action.url }
    case 'SET_LOCATION': return { ...state, location: action.loc, locationName: action.name, elevation: action.elev }
    case 'TOGGLE_CATEGORY': {
      const cats = state.categories.includes(action.cat)
        ? state.categories.filter(c => c !== action.cat)
        : [...state.categories, action.cat]
      return { ...state, categories: cats, subtype: null, conditionData: {} }
    }
    case 'SET_SUBTYPE': return { ...state, subtype: action.subtype }
    case 'SET_CONDITION': return { ...state, conditionData: { ...state.conditionData, ...action.data } }
    case 'SET_CAPTION': return { ...state, caption: action.text }
    case 'SET_SUBMITTING': return { ...state, submitting: action.val }
    case 'RESET': return initialState
    default: return state
  }
}

function completionScore(s) {
  let score = 0
  if (s.photo) score += 20
  if (s.location) score += 15
  if (s.categories.length > 0) score += 15
  if (s.subtype) score += 25
  if (Object.keys(s.conditionData).length > 0) score += 15
  if (s.caption) score += 5
  if (s.hashtags.length > 0) score += 5
  return score
}

function nextHint(s) {
  if (!s.photo) return 'Take a photo for +20%'
  if (s.categories.length === 0) return 'Pick a category for +15%'
  if (!s.subtype) return 'Add a detail for +25%'
  if (Object.keys(s.conditionData).length === 0) return 'One more detail for +15%'
  if (!s.caption) return 'Add a note for +5%'
  return null
}

export default function ReportSheet({ user, onClose }) {
  const [state, dispatch] = useReducer(reducer, initialState)
  const fileRef = useRef(null)
  const score = completionScore(state)
  const hint = nextHint(state)
  const canPost = score >= 30

  useEffect(() => {
    if (navigator.geolocation) {
      navigator.geolocation.getCurrentPosition(
        pos => dispatch({
          type: 'SET_LOCATION',
          loc: { lat: pos.coords.latitude, lng: pos.coords.longitude },
          name: null,
          elev: null,
        }),
        () => {},
        { enableHighAccuracy: true, timeout: 10000 }
      )
    }
  }, [])

  function handlePhoto(e) {
    const file = e.target.files?.[0]
    if (!file) return
    const url = URL.createObjectURL(file)
    dispatch({ type: 'SET_PHOTO', file, url })
  }

  const openCamera = useCallback(() => {
    fileRef.current?.click()
  }, [])

  async function handleSubmit() {
    if (!canPost || state.submitting) return
    dispatch({ type: 'SET_SUBMITTING', val: true })

    let imageUrl = null
    if (state.photo) {
      const ext = state.photo.name.split('.').pop()
      const path = `reports/${user.id}/${Date.now()}.${ext}`
      const { error } = await supabase.storage.from('report-images').upload(path, state.photo)
      if (!error) {
        const { data } = supabase.storage.from('report-images').getPublicUrl(path)
        imageUrl = data.publicUrl
      }
    }

    const report = {
      user_id: user.id,
      location: state.location
        ? `POINT(${state.location.lng} ${state.location.lat})`
        : 'POINT(8.2 46.8)',
      elevation_m: state.elevation,
      location_name: state.locationName,
      image_url: imageUrl,
      primary_categories: state.categories,
      subtype: state.subtype,
      condition_data: state.conditionData,
      caption: state.caption || null,
      hashtags: state.hashtags.length ? state.hashtags : null,
      completion_score: score,
      captured_at: new Date().toISOString(),
    }

    const { error } = await supabase.from('reports').insert(report)
    dispatch({ type: 'SET_SUBMITTING', val: false })

    if (!error) {
      dispatch({ type: 'RESET' })
      onClose()
    }
  }

  const primaryCat = state.categories[0]

  return (
    <div className="report-overlay">
      <div className="report-sheet">
        <div className="sheet-handle" />

        {/* Photo */}
        <div className="sheet-photo" onClick={openCamera}>
          {state.photoUrl
            ? <img src={state.photoUrl} alt="Report" />
            : <div className="photo-placeholder">
                <span>📷</span>
                <span>Tap for photo</span>
              </div>}
          {state.locationName && <div className="photo-loc">📍 {state.locationName}</div>}
        </div>
        <input ref={fileRef} type="file" accept="image/*" capture="environment" onChange={handlePhoto} hidden />

        {/* Categories */}
        <div className="sheet-section">
          <div className="sheet-label">Was siehst du?</div>
          <div className="cat-chips">
            {CATEGORIES.map(c => (
              <button
                key={c.key}
                className={`cat-chip ${state.categories.includes(c.key) ? 'active' : ''}`}
                onClick={() => dispatch({ type: 'TOGGLE_CATEGORY', cat: c.key })}
              >
                <span className="cat-emoji">{c.emoji}</span>
                <span>{c.label}</span>
              </button>
            ))}
          </div>
        </div>

        {/* Contextual quick select */}
        {primaryCat === 'snow' && (
          <div className="sheet-section">
            <div className="sheet-label">Schneetyp</div>
            <div className="sub-chips">
              {SNOW_TYPES.map(s => (
                <button
                  key={s.key}
                  className={`sub-chip ${state.subtype === s.key ? 'active' : ''}`}
                  onClick={() => dispatch({ type: 'SET_SUBTYPE', subtype: s.key })}
                >
                  <span>{s.emoji}</span> {s.label}
                </button>
              ))}
            </div>
            {state.subtype === 'fresh_powder' && (
              <div className="bucket-row">
                <span className="bucket-label">Höhe (cm)</span>
                {DEPTH_BUCKETS.map(b => (
                  <button
                    key={b}
                    className={`bucket ${state.conditionData.depth_bucket === b ? 'active' : ''}`}
                    onClick={() => dispatch({ type: 'SET_CONDITION', data: { depth_bucket: b } })}
                  >{b}</button>
                ))}
              </div>
            )}
          </div>
        )}

        {primaryCat === 'tour' && (
          <div className="sheet-section">
            <div className="sheet-label">Nochmal empfehlen?</div>
            <div className="sub-chips">
              {TOUR_RATINGS.map(r => (
                <button
                  key={r.key}
                  className={`sub-chip ${state.subtype === r.key ? 'active' : ''}`}
                  onClick={() => dispatch({ type: 'SET_SUBTYPE', subtype: r.key })}
                >
                  <span>{r.emoji}</span> {r.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {primaryCat === 'danger' && (
          <div className="sheet-section">
            <div className="sheet-label">Gefahrentyp</div>
            <div className="sub-chips">
              {DANGER_TYPES.map(d => (
                <button
                  key={d.key}
                  className={`sub-chip ${state.subtype === d.key ? 'active' : ''}`}
                  onClick={() => dispatch({ type: 'SET_SUBTYPE', subtype: d.key })}
                >
                  <span>{d.emoji}</span> {d.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {primaryCat === 'info' && (
          <div className="sheet-section">
            <div className="sheet-label">Info-Typ</div>
            <div className="sub-chips">
              {INFO_TYPES.map(i => (
                <button
                  key={i.key}
                  className={`sub-chip ${state.subtype === i.key ? 'active' : ''}`}
                  onClick={() => dispatch({ type: 'SET_SUBTYPE', subtype: i.key })}
                >
                  <span>{i.emoji}</span> {i.label}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Caption (collapsed) */}
        <details className="sheet-section caption-details">
          <summary>+ Notiz, Hashtags, Tags</summary>
          <textarea
            placeholder="Kurz beschreiben (optional)..."
            value={state.caption}
            onChange={e => dispatch({ type: 'SET_CAPTION', text: e.target.value })}
            rows={3}
          />
        </details>

        {/* Score + Submit */}
        <div className="sheet-footer">
          <div className="score-bar">
            <div className="score-fill" style={{ width: `${score}%` }} />
          </div>
          <div className="score-info">
            <span>{score}%</span>
            {hint && <span className="score-hint">{hint}</span>}
          </div>
          <div className="sheet-actions">
            <button className="sheet-cancel" onClick={onClose}>Cancel</button>
            <button
              className="sheet-post"
              onClick={handleSubmit}
              disabled={!canPost || state.submitting}
            >
              {state.submitting ? 'Posting...' : 'Post'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

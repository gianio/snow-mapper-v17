import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import './SnowMap.css'

const SWISS_BOUNDS = [[45.8, 5.9], [47.85, 10.55]]

export default function SnowMap() {
  const containerRef = useRef(null)
  const mapRef = useRef(null)

  useEffect(() => {
    if (mapRef.current) return

    const map = L.map(containerRef.current, {
      zoomControl: false,
      attributionControl: true,
    }).fitBounds(SWISS_BOUNDS, { padding: [10, 10] })

    L.tileLayer(
      'https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg',
      { attribution: '© swisstopo / MeteoSwiss / SLF / Copernicus' }
    ).addTo(map)

    map.setMaxBounds([
      [SWISS_BOUNDS[0][0] - 0.1, SWISS_BOUNDS[0][1] - 0.2],
      [SWISS_BOUNDS[1][0] + 0.1, SWISS_BOUNDS[1][1] + 0.2],
    ])

    mapRef.current = map

    return () => {
      map.remove()
      mapRef.current = null
    }
  }, [])

  return <div ref={containerRef} className="snow-map" />
}

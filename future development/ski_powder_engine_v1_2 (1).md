# ❄️ Ski Powder Decision Engine (v1.2)

## 🎯 Purpose
Binary decision system to evaluate if ski conditions are **POWDERED (TRUE)** or **NOT POWDERED (FALSE)** using only meteorological + terrain data.

---

# 1. ❄️ Output

- POWDERED: TRUE / FALSE  
- VALID_ASPECTS: ALL / NORTH / LEESIDE  
- REASON_FLAGS: list of triggers  

---

# 2. ❄️ POWDER PRESERVATION RULES (ANY = TRUE)

## R1 — Deep Freeze
If Tmax < -2°C → POWDERED = TRUE (ALL aspects)

## R2 — Calm Wind
If gust < 20 km/h AND mean wind < 10 km/h → POWDERED = TRUE (ALL aspects)

## R3 — Wind Redistribution
If 20–60 km/h gust AND mean wind < 30 km/h → POWDERED = TRUE (LEESIDE only)

## R4 — Freeze + Clear Night
If -2°C ≤ Tmax < 5°C AND clear night → POWDERED = TRUE (NORTH only)

---

# 3. ☀️ DESTRUCTION RULES (OVERRIDE TO FALSE)

## D1 — Heavy Rain
Rain in last 48h → POWDERED = FALSE

## D2 — Freeze-Thaw Collapse
Freeze-thaw cycles > 4 → POWDERED = FALSE

## D3 — Strong Solar Melt
Solar radiation ≥ 500 W/m² → POWDERED = FALSE

---

# 4. ☀️ SOLAR MODERATION (FILTER ONLY)

- 200–500 W/m² → remove SOUTH + WEST aspects  
- <200 W/m² → no effect  

---

# 5. 🔁 INTERMEDIATE DEGRADATION

- 2–4 freeze-thaw cycles → reduced quality (still possible powder)
- 0–1 cycles → stable snowpack

---

# 6. 🧭 ASPECT RULES

- NORTH = most stable
- SOUTH = fastest degradation
- LEESIDE = wind-protected zones

Wind LEESIDE = opposite 180° from wind direction

---

# 7. ⚙️ PSEUDO-CODE

```python
def is_powdered(data):

    if data.rain_48h:
        return False

    if data.freeze_thaw_cycles > 4:
        return False

    if data.solar_radiation >= 500:
        return False

    if data.tmax < -2:
        return True

    if data.gust < 20 and data.wind_mean < 10:
        return True

    if 20 <= data.gust <= 60 and data.wind_mean < 30:
        return True

    if -2 <= data.tmax < 5 and data.clear_night:
        return True

    return False
```

---

# ❄️ Summary
A deterministic rule engine for ski powder detection using:
- temperature
- wind
- solar radiation
- freeze-thaw cycles
- precipitation history
- aspect logic

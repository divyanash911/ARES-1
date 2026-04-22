# ARES-1: Mars Habitat Managed System
### A Self-Adaptive Exemplar for POLARIS

A lightweight, gamified managed system simulating a Mars habitat with crew survival stakes.
Designed for POLARIS integration — exposes all telemetry and adaptation actions via REST.

---

## Quick Start

```bash
# Install
pip install fastapi uvicorn

# Run the managed system
cd backend
python main.py
# → Simulation running at http://localhost:8000
# → Swagger docs at http://localhost:8000/docs

# Open the dashboard (any browser)
open frontend/index.html
```

---

## System Overview

ARES-1 simulates a Mars habitat with 4 critical vitals that POLARIS must keep within SLA bounds:

| Vital           | SLA Threshold     | Description                      |
|-----------------|-------------------|----------------------------------|
| `oxygen_level`  | > 20%             | Crew breathability               |
| `power_level`   | > 15%             | Minimum operable power           |
| `heat_level`    | 30% – 95%         | Survivable temperature range     |
| `hull_integrity`| > 40%             | Structural safety                |

The simulation ticks every **2 seconds** with scheduled disturbance events.

---

## Disturbance Events

| Event              | Effect                                           |
|--------------------|--------------------------------------------------|
| `DUST_STORM`       | Reduces solar efficiency → power drops → heat drops |
| `METEOR_STRIKE`    | Damages hull, disrupts O₂ generation            |
| `EQUIPMENT_FAULT`  | Degrades O₂ generator output                    |
| `SOLAR_FLARE`      | Temporarily boosts solar efficiency              |
| `CREW_ACTIVITY`    | Spikes O₂ and power consumption                  |

---

## Action Space

POLARIS adapts the system through 4 controllable parameters:

```json
POST /adapt/composite
{
  "power_mode": "NORMAL | CONSERVATION | EMERGENCY",
  "oxygen_generators": 1 | 2 | 3,
  "active_heaters": 0 | 1 | 2 | 3,
  "life_support_priority": "OXYGEN | HEAT | BALANCED",
  "reason": "optional string for logging"
}
```

**Tradeoffs:**
- More O₂ generators → higher power consumption
- More heaters → higher power consumption, but prevents heat SLA breach during dust storms
- `CONSERVATION` mode → reduces power drain but limits all systems
- `EMERGENCY` mode → minimal consumption (survival only)

---

## POLARIS Integration

### Metric Collector → Poll Telemetry

```python
import requests

telemetry = requests.get("http://localhost:8000/telemetry").json()

# Key fields for POLARIS Reasoner:
vitals      = telemetry["vitals"]       # O2, power, heat, hull
environment = telemetry["environment"]  # active event, intensity, ticks remaining
sla         = telemetry["sla"]          # thresholds for each vital
performance = telemetry["performance"]  # violations, utility score
```

### Reasoner → Apply Adaptation

```python
adaptation = {
    "power_mode": "CONSERVATION",
    "oxygen_generators": 3,
    "active_heaters": 1,
    "reason": "Dust storm detected — conserving power, boosting O2"
}
requests.post("http://localhost:8000/adapt/composite", json=adaptation)
```

### Knowledge Base → Fetch History

```python
history = requests.get("http://localhost:8000/telemetry/history?n=50").json()
# Returns list of {tick, category, message} log entries
```

### Control → Inject Disturbances (for testing)

```python
requests.post("http://localhost:8000/control/inject",
    params={"event_type": "DUST_STORM", "intensity": 0.8, "duration": 20})
```

---

## Exemplar Prompt Skeleton for POLARIS

```
System Role: Proactive habitat controller maintaining crew survival SLA.
  - oxygen_level > 0.20
  - power_level > 0.15
  - heat_level in [0.30, 0.95]
  - hull_integrity > 0.40

Goals: Maximize utility (all 4 vitals in SLA). Prefer balanced power use.

Action Space:
  - power_mode: NORMAL | CONSERVATION | EMERGENCY
  - oxygen_generators: 1 | 2 | 3
  - active_heaters: 0 | 1 | 2 | 3
  - life_support_priority: OXYGEN | HEAT | BALANCED

Logic:
  1. Query KB for recent history
  2. Simulate proposed action with World Model
  3. If dust storm active: prioritize CONSERVATION power, maintain heaters
  4. If O2 < 0.40: switch to 3 generators
  5. If power < 0.25: switch to CONSERVATION, reduce heaters
  6. Default: NORMAL mode, 2 generators, 1 heater, BALANCED priority
```

---

## Utility Scoring

Each tick produces a utility score (0.0–1.0):
- O₂ in SLA: +0.35
- Power in SLA: +0.25
- Heat in SLA: +0.25
- Hull in SLA: +0.15

Total utility = sum over all ticks. Target: maximize `performance.utility_score`.

---

## API Reference

| Method | Endpoint                  | Description                          |
|--------|---------------------------|--------------------------------------|
| GET    | `/telemetry`              | Full system telemetry snapshot       |
| GET    | `/telemetry/vitals`       | Lightweight vitals-only poll         |
| GET    | `/telemetry/history?n=50` | Recent event log                     |
| POST   | `/adapt/composite`        | **Primary** — apply multiple actions |
| POST   | `/adapt/power`            | Set power mode                       |
| POST   | `/adapt/oxygen`           | Set O₂ generator count               |
| POST   | `/adapt/heat`             | Set heater count                     |
| POST   | `/adapt/priority`         | Set life support priority            |
| POST   | `/control/inject`         | Inject a disturbance event           |
| POST   | `/control/reset`          | Reset simulation                     |
| GET    | `/info`                   | System metadata + action space       |
| GET    | `/docs`                   | Swagger UI                           |
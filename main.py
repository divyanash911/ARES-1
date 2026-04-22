"""
ARES-1: Mars Habitat Managed System
A lightweight self-adaptive exemplar for POLARIS integration.

REST API exposes telemetry and accepts adaptation actions.
The simulation ticks every second, injecting disturbances autonomously.
"""

import asyncio
import random
import time
import math
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field, asdict
from enum import Enum
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ─────────────────────────────────────────────
# Simulation State
# ─────────────────────────────────────────────

class EventType(str, Enum):
    DUST_STORM      = "DUST_STORM"
    METEOR_STRIKE   = "METEOR_STRIKE"
    CREW_ACTIVITY   = "CREW_ACTIVITY"
    SOLAR_FLARE     = "SOLAR_FLARE"
    EQUIPMENT_FAULT = "EQUIPMENT_FAULT"
    NOMINAL         = "NOMINAL"

@dataclass
class HabitatState:
    # ── Core vitals (0.0 – 1.0 normalized) ──────
    oxygen_level: float = 0.85        # SLA: must stay > 0.2
    power_level: float = 0.75         # SLA: must stay > 0.15
    heat_level: float = 0.70          # SLA: must stay in 0.3–0.95
    hull_integrity: float = 1.0       # SLA: must stay > 0.4

    # ── Derived / environmental ──────────────────
    solar_efficiency: float = 1.0     # 0–1, reduced by dust storms
    crew_count: int = 6
    crew_alive: int = 6
    simulation_time: int = 0          # seconds elapsed

    # ── Infrastructure ───────────────────────────
    oxygen_generators: int = 2        # max 3
    active_heaters: int = 1           # max 3
    power_mode: str = "NORMAL"        # NORMAL | CONSERVATION | EMERGENCY
    life_support_priority: str = "BALANCED"  # OXYGEN | HEAT | BALANCED

    # ── Active disturbances ──────────────────────
    active_event: str = EventType.NOMINAL
    event_intensity: float = 0.0
    event_ticks_remaining: int = 0

    # ── SLA tracking ─────────────────────────────
    sla_violations: int = 0
    total_ticks: int = 0
    utility_score: float = 0.0

    # ── Event log ────────────────────────────────
    event_log: List[Dict] = field(default_factory=list)


STATE = HabitatState()
RUNNING = True
TICK_RATE = 2.0   # seconds per simulation tick

# ─────────────────────────────────────────────
# Simulation Engine
# ─────────────────────────────────────────────

DISTURBANCE_SCHEDULE = [
    # (tick_start, event_type, intensity, duration)
    (30,  EventType.DUST_STORM,      0.6, 20),
    (80,  EventType.CREW_ACTIVITY,   0.4, 10),
    (110, EventType.EQUIPMENT_FAULT, 0.5, 15),
    (150, EventType.DUST_STORM,      0.9, 25),
    (200, EventType.METEOR_STRIKE,   0.7,  5),
    (240, EventType.SOLAR_FLARE,     0.8, 12),
    (280, EventType.DUST_STORM,      0.5, 30),
    (330, EventType.CREW_ACTIVITY,   0.6, 10),
    (360, EventType.EQUIPMENT_FAULT, 0.8, 20),
]

def log_event(msg: str, category: str = "INFO"):
    entry = {
        "tick": STATE.simulation_time,
        "time": time.time(),
        "category": category,
        "message": msg
    }
    STATE.event_log.append(entry)
    if len(STATE.event_log) > 200:
        STATE.event_log.pop(0)
    print(f"[T={STATE.simulation_time:04d}] [{category}] {msg}")

def compute_utility() -> float:
    """
    Utility = weighted sum of SLA adherence.
    All vitals in safe range = 1.0 per tick.
    Violations deduct score.
    """
    score = 0.0
    if STATE.oxygen_level > 0.2:     score += 0.35
    if STATE.power_level > 0.15:     score += 0.25
    if 0.3 < STATE.heat_level < 0.95: score += 0.25
    if STATE.hull_integrity > 0.4:   score += 0.15
    return round(score, 4)

def tick():
    global STATE
    STATE.simulation_time += 1
    STATE.total_ticks += 1

    # ── Scheduled disturbances ───────────────────
    for (start, etype, intensity, duration) in DISTURBANCE_SCHEDULE:
        if STATE.simulation_time == start:
            STATE.active_event = etype
            STATE.event_intensity = intensity
            STATE.event_ticks_remaining = duration
            log_event(f"Disturbance START: {etype} (intensity={intensity})", "ALERT")
            break

    if STATE.event_ticks_remaining > 0:
        STATE.event_ticks_remaining -= 1
        if STATE.event_ticks_remaining == 0:
            prev = STATE.active_event
            STATE.active_event = EventType.NOMINAL
            STATE.event_intensity = 0.0
            log_event(f"Disturbance END: {prev}", "INFO")
    else:
        # Small random jitter even in nominal
        if random.random() < 0.05:
            STATE.event_intensity = random.uniform(0.0, 0.1)
        else:
            STATE.event_intensity = 0.0

    # ── Apply environmental physics ──────────────
    ev = STATE.active_event
    ei = STATE.event_intensity

    # Solar efficiency
    if ev == EventType.DUST_STORM:
        STATE.solar_efficiency = max(0.05, STATE.solar_efficiency - ei * 0.08)
    elif ev == EventType.SOLAR_FLARE:
        STATE.solar_efficiency = min(1.5, STATE.solar_efficiency + ei * 0.1)
    else:
        STATE.solar_efficiency = min(1.0, STATE.solar_efficiency + 0.03)

    # Power generation vs consumption
    power_gen = 0.04 * STATE.solar_efficiency
    power_consumption = {
        "NORMAL": 0.03 + STATE.active_heaters * 0.008 + STATE.oxygen_generators * 0.006,
        "CONSERVATION": 0.018 + STATE.active_heaters * 0.005 + STATE.oxygen_generators * 0.004,
        "EMERGENCY": 0.01 + STATE.active_heaters * 0.003 + STATE.oxygen_generators * 0.003,
    }[STATE.power_mode]
    STATE.power_level = min(1.0, max(0.0, STATE.power_level + power_gen - power_consumption))

    # Oxygen dynamics
    o2_gen = STATE.oxygen_generators * 0.025
    o2_consumption = STATE.crew_alive * 0.004
    if ev == EventType.EQUIPMENT_FAULT:
        o2_gen *= (1 - ei * 0.6)
    if ev == EventType.METEOR_STRIKE:
        o2_gen *= (1 - ei * 0.4)
        STATE.hull_integrity = max(0.0, STATE.hull_integrity - ei * 0.05)
    STATE.oxygen_level = min(1.0, max(0.0, STATE.oxygen_level + o2_gen - o2_consumption))

    # Heat dynamics
    if ev == EventType.DUST_STORM:
        heat_loss = 0.012 + ei * 0.015
    else:
        heat_loss = 0.008
    heat_gen = STATE.active_heaters * 0.018
    if STATE.power_level < 0.2:
        heat_gen *= 0.3   # power starvation reduces heating
    STATE.heat_level = min(1.0, max(0.0, STATE.heat_level + heat_gen - heat_loss))

    # Crew activity spikes consumption
    if ev == EventType.CREW_ACTIVITY:
        STATE.oxygen_level = max(0.0, STATE.oxygen_level - ei * 0.01)
        STATE.power_level = max(0.0, STATE.power_level - ei * 0.015)

    # Hull decay over time / meteor
    if ev != EventType.METEOR_STRIKE:
        STATE.hull_integrity = min(1.0, STATE.hull_integrity + 0.001)

    # ── SLA violation tracking ────────────────────
    violations = 0
    if STATE.oxygen_level <= 0.2:    violations += 1
    if STATE.power_level <= 0.15:    violations += 1
    if not (0.3 < STATE.heat_level < 0.95): violations += 1
    if STATE.hull_integrity <= 0.4:  violations += 1

    if violations > 0:
        STATE.sla_violations += violations
        log_event(f"SLA VIOLATION x{violations}: O2={STATE.oxygen_level:.2f} PWR={STATE.power_level:.2f} HEAT={STATE.heat_level:.2f}", "VIOLATION")

    tick_utility = compute_utility()
    STATE.utility_score += tick_utility

    # Round state values
    STATE.oxygen_level   = round(STATE.oxygen_level, 4)
    STATE.power_level    = round(STATE.power_level, 4)
    STATE.heat_level     = round(STATE.heat_level, 4)
    STATE.hull_integrity = round(STATE.hull_integrity, 4)
    STATE.solar_efficiency = round(STATE.solar_efficiency, 4)
    STATE.utility_score  = round(STATE.utility_score, 4)


async def simulation_loop():
    while RUNNING:
        tick()
        await asyncio.sleep(TICK_RATE)


# ─────────────────────────────────────────────
# FastAPI App
# ─────────────────────────────────────────────

app = FastAPI(
    title="ARES-1 Mars Habitat",
    description="Managed system exemplar for POLARIS self-adaptation framework",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    asyncio.create_task(simulation_loop())
    log_event("ARES-1 Habitat simulation started", "SYSTEM")


# ─────────────────────────────────────────────
# Telemetry Endpoints (READ)
# ─────────────────────────────────────────────

@app.get("/telemetry", tags=["Monitoring"],
    summary="Full system telemetry snapshot",
    description="Returns all current habitat metrics. Poll at your desired interval (recommended: every 2-5s).")
def get_telemetry():
    """Primary monitoring endpoint for POLARIS Metric Collector."""
    return {
        "timestamp": time.time(),
        "simulation_time": STATE.simulation_time,

        # ── Vitals (SLA-critical) ──
        "vitals": {
            "oxygen_level":    STATE.oxygen_level,
            "power_level":     STATE.power_level,
            "heat_level":      STATE.heat_level,
            "hull_integrity":  STATE.hull_integrity,
        },

        # ── SLA thresholds (for Reasoner context) ──
        "sla": {
            "oxygen_min": 0.20,
            "power_min":  0.15,
            "heat_min":   0.30,
            "heat_max":   0.95,
            "hull_min":   0.40,
        },

        # ── Environment ──
        "environment": {
            "solar_efficiency": STATE.solar_efficiency,
            "active_event":     STATE.active_event,
            "event_intensity":  STATE.event_intensity,
            "event_ticks_remaining": STATE.event_ticks_remaining,
        },

        # ── Infrastructure ──
        "infrastructure": {
            "oxygen_generators": STATE.oxygen_generators,
            "active_heaters":    STATE.active_heaters,
            "power_mode":        STATE.power_mode,
            "life_support_priority": STATE.life_support_priority,
        },

        # ── Crew ──
        "crew": {
            "total":  STATE.crew_count,
            "alive":  STATE.crew_alive,
        },

        # ── Performance ──
        "performance": {
            "sla_violations": STATE.sla_violations,
            "total_ticks":    STATE.total_ticks,
            "utility_score":  STATE.utility_score,
            "avg_utility_per_tick": round(STATE.utility_score / max(1, STATE.total_ticks), 4),
        }
    }


@app.get("/telemetry/vitals", tags=["Monitoring"],
    summary="Vitals-only snapshot (lightweight polling)")
def get_vitals():
    """Lightweight endpoint for high-frequency polling."""
    return {
        "simulation_time": STATE.simulation_time,
        "oxygen_level":    STATE.oxygen_level,
        "power_level":     STATE.power_level,
        "heat_level":      STATE.heat_level,
        "hull_integrity":  STATE.hull_integrity,
        "active_event":    STATE.active_event,
        "sla_violations":  STATE.sla_violations,
    }


@app.get("/telemetry/history", tags=["Monitoring"],
    summary="Recent event log",
    description="Returns last N log entries. Useful for Knowledge Base ingestion.")
def get_history(n: int = 50):
    return {
        "count": min(n, len(STATE.event_log)),
        "entries": STATE.event_log[-n:]
    }


# ─────────────────────────────────────────────
# Action Endpoints (WRITE — Adaptation Actions)
# ─────────────────────────────────────────────

class PowerModeAction(BaseModel):
    mode: str  # NORMAL | CONSERVATION | EMERGENCY
    reason: Optional[str] = None

class OxygenAction(BaseModel):
    generators: int  # 1 | 2 | 3
    reason: Optional[str] = None

class HeaterAction(BaseModel):
    heaters: int   # 0 | 1 | 2 | 3
    reason: Optional[str] = None

class PriorityAction(BaseModel):
    priority: str  # OXYGEN | HEAT | BALANCED
    reason: Optional[str] = None

class CompositeAction(BaseModel):
    """Send multiple adaptation actions in one call."""
    power_mode: Optional[str] = None
    oxygen_generators: Optional[int] = None
    active_heaters: Optional[int] = None
    life_support_priority: Optional[str] = None
    reason: Optional[str] = None


@app.post("/adapt/power", tags=["Adaptation"],
    summary="Set power conservation mode")
def adapt_power(action: PowerModeAction):
    valid = ["NORMAL", "CONSERVATION", "EMERGENCY"]
    if action.mode not in valid:
        raise HTTPException(400, f"mode must be one of {valid}")
    old = STATE.power_mode
    STATE.power_mode = action.mode
    log_event(f"ADAPT power_mode: {old} → {action.mode}  [{action.reason or 'no reason'}]", "ACTION")
    return {"status": "ok", "previous": old, "current": STATE.power_mode}


@app.post("/adapt/oxygen", tags=["Adaptation"],
    summary="Set number of active oxygen generators (1–3)")
def adapt_oxygen(action: OxygenAction):
    if not 1 <= action.generators <= 3:
        raise HTTPException(400, "generators must be 1, 2, or 3")
    old = STATE.oxygen_generators
    STATE.oxygen_generators = action.generators
    log_event(f"ADAPT oxygen_generators: {old} → {action.generators}  [{action.reason or 'no reason'}]", "ACTION")
    return {"status": "ok", "previous": old, "current": STATE.oxygen_generators}


@app.post("/adapt/heat", tags=["Adaptation"],
    summary="Set number of active heaters (0–3)")
def adapt_heat(action: HeaterAction):
    if not 0 <= action.heaters <= 3:
        raise HTTPException(400, "heaters must be 0–3")
    old = STATE.active_heaters
    STATE.active_heaters = action.heaters
    log_event(f"ADAPT active_heaters: {old} → {action.heaters}  [{action.reason or 'no reason'}]", "ACTION")
    return {"status": "ok", "previous": old, "current": STATE.active_heaters}


@app.post("/adapt/priority", tags=["Adaptation"],
    summary="Set life support priority (OXYGEN | HEAT | BALANCED)")
def adapt_priority(action: PriorityAction):
    valid = ["OXYGEN", "HEAT", "BALANCED"]
    if action.priority not in valid:
        raise HTTPException(400, f"priority must be one of {valid}")
    old = STATE.life_support_priority
    STATE.life_support_priority = action.priority
    log_event(f"ADAPT life_support_priority: {old} → {action.priority}  [{action.reason or 'no reason'}]", "ACTION")
    return {"status": "ok", "previous": old, "current": STATE.life_support_priority}


@app.post("/adapt/composite", tags=["Adaptation"],
    summary="Apply multiple adaptation actions atomically",
    description="Preferred endpoint for POLARIS — send all decisions in one request.")
def adapt_composite(action: CompositeAction):
    changes = {}

    if action.power_mode:
        valid = ["NORMAL", "CONSERVATION", "EMERGENCY"]
        if action.power_mode not in valid:
            raise HTTPException(400, f"power_mode must be one of {valid}")
        changes["power_mode"] = (STATE.power_mode, action.power_mode)
        STATE.power_mode = action.power_mode

    if action.oxygen_generators is not None:
        if not 1 <= action.oxygen_generators <= 3:
            raise HTTPException(400, "oxygen_generators must be 1–3")
        changes["oxygen_generators"] = (STATE.oxygen_generators, action.oxygen_generators)
        STATE.oxygen_generators = action.oxygen_generators

    if action.active_heaters is not None:
        if not 0 <= action.active_heaters <= 3:
            raise HTTPException(400, "active_heaters must be 0–3")
        changes["active_heaters"] = (STATE.active_heaters, action.active_heaters)
        STATE.active_heaters = action.active_heaters

    if action.life_support_priority:
        valid = ["OXYGEN", "HEAT", "BALANCED"]
        if action.life_support_priority not in valid:
            raise HTTPException(400, f"life_support_priority must be one of {valid}")
        changes["life_support_priority"] = (STATE.life_support_priority, action.life_support_priority)
        STATE.life_support_priority = action.life_support_priority

    summary = ", ".join([f"{k}: {v[0]}→{v[1]}" for k, v in changes.items()])
    log_event(f"ADAPT composite [{action.reason or 'no reason'}]: {summary}", "ACTION")

    return {"status": "ok", "changes": {k: {"from": v[0], "to": v[1]} for k, v in changes.items()}}


# ─────────────────────────────────────────────
# Control Endpoints
# ─────────────────────────────────────────────

@app.post("/control/reset", tags=["Control"],
    summary="Reset simulation to initial state")
def reset_simulation():
    global STATE
    STATE = HabitatState()
    log_event("Simulation RESET", "SYSTEM")
    return {"status": "ok", "message": "Simulation reset to initial state"}


@app.post("/control/inject", tags=["Control"],
    summary="Manually inject a disturbance event")
def inject_event(event_type: str, intensity: float = 0.7, duration: int = 15):
    valid = [e.value for e in EventType]
    if event_type not in valid:
        raise HTTPException(400, f"event_type must be one of {valid}")
    if not 0.0 <= intensity <= 1.0:
        raise HTTPException(400, "intensity must be 0.0–1.0")
    STATE.active_event = event_type
    STATE.event_intensity = intensity
    STATE.event_ticks_remaining = duration
    log_event(f"MANUAL INJECT: {event_type} (intensity={intensity}, duration={duration})", "INJECT")
    return {"status": "ok", "event": event_type, "intensity": intensity, "duration": duration}


@app.get("/info", tags=["Meta"],
    summary="System info and action space description")
def get_info():
    return {
        "system": "ARES-1 Mars Habitat",
        "version": "1.0.0",
        "description": "Self-adaptive exemplar for POLARIS. A Mars habitat simulation with O2, power, heat, and hull integrity.",
        "tick_rate_seconds": TICK_RATE,
        "sla": {
            "oxygen_level":   {"min": 0.20, "description": "Crew breathability"},
            "power_level":    {"min": 0.15, "description": "Minimum operable power"},
            "heat_level":     {"min": 0.30, "max": 0.95, "description": "Survivable temperature range"},
            "hull_integrity": {"min": 0.40, "description": "Structural safety"},
        },
        "action_space": {
            "power_mode":            ["NORMAL", "CONSERVATION", "EMERGENCY"],
            "oxygen_generators":     [1, 2, 3],
            "active_heaters":        [0, 1, 2, 3],
            "life_support_priority": ["OXYGEN", "HEAT", "BALANCED"],
        },
        "events": [e.value for e in EventType],
        "endpoints": {
            "telemetry":        "GET  /telemetry",
            "vitals":           "GET  /telemetry/vitals",
            "history":          "GET  /telemetry/history",
            "adapt_composite":  "POST /adapt/composite",
            "adapt_power":      "POST /adapt/power",
            "adapt_oxygen":     "POST /adapt/oxygen",
            "adapt_heat":       "POST /adapt/heat",
            "adapt_priority":   "POST /adapt/priority",
            "inject_event":     "POST /control/inject",
            "reset":            "POST /control/reset",
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
"""
ARES-1: Mars Habitat Managed System  v2.0
Self-adaptive exemplar for POLARIS.

v2 adds: crew death, hull breach decompression, power starvation cascade,
         mission status (NOMINAL→WARNING→CRITICAL→FAILED), /result endpoint.
"""

import asyncio
import random
import time
from typing import Optional, List, Dict
from dataclasses import dataclass, field
from enum import Enum
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────

class EventType(str, Enum):
    DUST_STORM       = "DUST_STORM"
    METEOR_STRIKE    = "METEOR_STRIKE"
    CREW_ACTIVITY    = "CREW_ACTIVITY"
    SOLAR_FLARE      = "SOLAR_FLARE"
    EQUIPMENT_FAULT  = "EQUIPMENT_FAULT"
    HULL_BREACH      = "HULL_BREACH"
    NOMINAL          = "NOMINAL"

class MissionStatus(str, Enum):
    NOMINAL   = "NOMINAL"
    WARNING   = "WARNING"
    CRITICAL  = "CRITICAL"
    FAILED    = "FAILED"

# ─────────────────────────────────────────────
# Grace period config (ticks before consequence fires)
# ─────────────────────────────────────────────
GRACE = {
    "oxygen":  5,    # ticks at O2 <= 0.20 before crew member dies
    "heat":    8,    # ticks outside heat range before crew member dies
    "hull":    6,    # ticks at hull <= 0.40 before decompression breach
    "power":   10,   # ticks at power <= 0.15 before forced generator shutdown
}

# ─────────────────────────────────────────────
# Simulation state
# ─────────────────────────────────────────────

@dataclass
class HabitatState:
    # Vitals
    oxygen_level:     float = 0.85
    power_level:      float = 0.75
    heat_level:       float = 0.70
    hull_integrity:   float = 1.0
    solar_efficiency: float = 1.0

    # Crew
    crew_count: int = 6
    crew_alive: int = 6

    # Time
    simulation_time: int = 0

    # Infrastructure
    oxygen_generators:      int = 2
    active_heaters:         int = 1
    power_mode:             str = "NORMAL"
    life_support_priority:  str = "BALANCED"

    # Active disturbance
    active_event:          str   = EventType.NOMINAL
    event_intensity:       float = 0.0
    event_ticks_remaining: int   = 0

    # Mission
    mission_status:   str = MissionStatus.NOMINAL
    failure_cause:    str = ""
    mission_end_tick: int = -1

    # Grace period counters
    ticks_o2_critical:    int = 0
    ticks_heat_critical:  int = 0
    ticks_hull_critical:  int = 0
    ticks_power_critical: int = 0

    # Cascading failure state
    hull_breach_active:  bool = False
    forced_gen_offline:  int  = 0

    # Scoring
    sla_violations: int   = 0
    crew_deaths:    int   = 0
    total_ticks:    int   = 0
    utility_score:  float = 0.0

    # Log
    event_log: List[Dict] = field(default_factory=list)


STATE   = HabitatState()
RUNNING = True
TICK_RATE = 2.0   # seconds per tick

# ─────────────────────────────────────────────
# Disturbance schedule
# ─────────────────────────────────────────────
DISTURBANCE_SCHEDULE = [
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

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def log_event(msg: str, category: str = "INFO"):
    entry = {"tick": STATE.simulation_time, "time": time.time(),
             "category": category, "message": msg}
    STATE.event_log.append(entry)
    if len(STATE.event_log) > 300:
        STATE.event_log.pop(0)
    print(f"[T={STATE.simulation_time:04d}] [{category}] {msg}")


def compute_utility() -> float:
    if STATE.mission_status == MissionStatus.FAILED:
        return 0.0
    score = 0.0
    if STATE.oxygen_level > 0.2:             score += 0.35
    if STATE.power_level > 0.15:            score += 0.25
    if 0.3 < STATE.heat_level < 0.95:       score += 0.25
    if STATE.hull_integrity > 0.4:          score += 0.15
    # crew survival multiplier
    crew_factor = STATE.crew_alive / STATE.crew_count if STATE.crew_count else 0
    score *= crew_factor
    return round(score, 4)


def update_mission_status():
    if STATE.mission_status == MissionStatus.FAILED:
        return
    criticals = sum([
        STATE.oxygen_level    <= 0.2,
        STATE.power_level     <= 0.15,
        not (0.3 < STATE.heat_level < 0.95),
        STATE.hull_integrity  <= 0.4,
    ])
    warnings = sum([
        STATE.oxygen_level    <= 0.35,
        STATE.power_level     <= 0.25,
        STATE.heat_level      <= 0.4 or STATE.heat_level >= 0.85,
        STATE.hull_integrity  <= 0.6,
    ])
    if criticals >= 2 or STATE.crew_alive <= STATE.crew_count // 2:
        STATE.mission_status = MissionStatus.CRITICAL
    elif criticals >= 1 or warnings >= 2:
        STATE.mission_status = MissionStatus.WARNING
    else:
        STATE.mission_status = MissionStatus.NOMINAL


def kill_crew(cause: str):
    if STATE.crew_alive <= 0:
        return
    STATE.crew_alive -= 1
    STATE.crew_deaths += 1
    log_event(
        f"CREW MEMBER DECEASED — {cause}. Survivors: {STATE.crew_alive}/{STATE.crew_count}",
        "DEATH"
    )
    if STATE.crew_alive == 0:
        end_mission(f"All crew lost — {cause}")


def end_mission(cause: str):
    if STATE.mission_status == MissionStatus.FAILED:
        return
    STATE.mission_status   = MissionStatus.FAILED
    STATE.failure_cause    = cause
    STATE.mission_end_tick = STATE.simulation_time
    log_event(f"MISSION FAILED — {cause}", "FATAL")


# ─────────────────────────────────────────────
# Tick
# ─────────────────────────────────────────────

def tick():
    if STATE.mission_status == MissionStatus.FAILED:
        return   # physics frozen after failure

    STATE.simulation_time += 1
    STATE.total_ticks     += 1

    # ── Scheduled disturbances ───────────────────
    for (start, etype, intensity, duration) in DISTURBANCE_SCHEDULE:
        if STATE.simulation_time == start:
            STATE.active_event          = etype
            STATE.event_intensity       = intensity
            STATE.event_ticks_remaining = duration
            log_event(f"Disturbance START: {etype} (intensity={intensity})", "ALERT")
            break

    if STATE.event_ticks_remaining > 0:
        STATE.event_ticks_remaining -= 1
        if STATE.event_ticks_remaining == 0:
            prev = STATE.active_event
            if not STATE.hull_breach_active:
                STATE.active_event    = EventType.NOMINAL
                STATE.event_intensity = 0.0
            log_event(f"Disturbance END: {prev}", "INFO")
    else:
        STATE.event_intensity = random.uniform(0.0, 0.1) if random.random() < 0.05 else 0.0

    ev = STATE.active_event
    ei = STATE.event_intensity

    # ── Solar ────────────────────────────────────
    if ev == EventType.DUST_STORM:
        STATE.solar_efficiency = max(0.05, STATE.solar_efficiency - ei * 0.08)
    elif ev == EventType.SOLAR_FLARE:
        STATE.solar_efficiency = min(1.5,  STATE.solar_efficiency + ei * 0.1)
    else:
        STATE.solar_efficiency = min(1.0,  STATE.solar_efficiency + 0.03)

    # ── Power ────────────────────────────────────
    effective_gens = max(0, STATE.oxygen_generators - STATE.forced_gen_offline)
    power_gen = 0.04 * STATE.solar_efficiency
    power_consumption = {
        "NORMAL":       0.03 + STATE.active_heaters * 0.008 + effective_gens * 0.006,
        "CONSERVATION": 0.018 + STATE.active_heaters * 0.005 + effective_gens * 0.004,
        "EMERGENCY":    0.01  + STATE.active_heaters * 0.003 + effective_gens * 0.003,
    }[STATE.power_mode]
    STATE.power_level = min(1.0, max(0.0, STATE.power_level + power_gen - power_consumption))

    # ── Oxygen ───────────────────────────────────
    o2_gen = effective_gens * 0.025
    o2_consumption = STATE.crew_alive * 0.004
    if ev == EventType.EQUIPMENT_FAULT:
        o2_gen *= (1 - ei * 0.6)
    if ev == EventType.METEOR_STRIKE:
        o2_gen *= (1 - ei * 0.4)
        STATE.hull_integrity = max(0.0, STATE.hull_integrity - ei * 0.05)
    if STATE.hull_breach_active:
        o2_gen -= 0.03   # decompression bleeds O2
    STATE.oxygen_level = min(1.0, max(0.0, STATE.oxygen_level + o2_gen - o2_consumption))

    # ── Heat ─────────────────────────────────────
    heat_loss = (0.012 + ei * 0.015) if ev == EventType.DUST_STORM else 0.008
    if STATE.hull_breach_active:
        heat_loss += 0.025   # rapid heat loss during decompression
    heat_gen = STATE.active_heaters * 0.018
    if STATE.power_level < 0.2:
        heat_gen *= 0.3      # power starvation starves heaters
    STATE.heat_level = min(1.0, max(0.0, STATE.heat_level + heat_gen - heat_loss))

    # ── Crew activity spike ───────────────────────
    if ev == EventType.CREW_ACTIVITY:
        STATE.oxygen_level = max(0.0, STATE.oxygen_level - ei * 0.01)
        STATE.power_level  = max(0.0, STATE.power_level  - ei * 0.015)

    # ── Hull passive repair ───────────────────────
    if ev not in (EventType.METEOR_STRIKE,) and not STATE.hull_breach_active:
        STATE.hull_integrity = min(1.0, STATE.hull_integrity + 0.001)

    # ════════════════════════════════════════════
    # CONSEQUENCE ENGINE
    # ════════════════════════════════════════════

    # 1 — Oxygen → asphyxiation
    if STATE.oxygen_level <= 0.20:
        STATE.ticks_o2_critical += 1
        remaining = GRACE["oxygen"] - STATE.ticks_o2_critical
        if remaining > 0:
            log_event(
                f"O2 CRITICAL {STATE.oxygen_level:.2f} — crew dies in {remaining} ticks if unresolved",
                "CRITICAL"
            )
        if STATE.ticks_o2_critical >= GRACE["oxygen"]:
            kill_crew(f"asphyxiation (O2={STATE.oxygen_level:.2f} for {GRACE['oxygen']}+ ticks)")
            STATE.ticks_o2_critical = 0   # next victim starts a new grace period
    else:
        if STATE.ticks_o2_critical > 0:
            log_event(f"O2 recovered to {STATE.oxygen_level:.2f} — crisis averted", "INFO")
        STATE.ticks_o2_critical = 0

    # 2 — Heat → hypothermia / heat stroke
    heat_ok = 0.30 < STATE.heat_level < 0.95
    if not heat_ok:
        STATE.ticks_heat_critical += 1
        cause_str = "hypothermia" if STATE.heat_level <= 0.30 else "heat stroke"
        remaining = GRACE["heat"] - STATE.ticks_heat_critical
        if remaining > 0:
            log_event(
                f"HEAT CRITICAL {STATE.heat_level:.2f} — {cause_str} in {remaining} ticks",
                "CRITICAL"
            )
        if STATE.ticks_heat_critical >= GRACE["heat"]:
            kill_crew(f"{cause_str} (heat={STATE.heat_level:.2f} for {GRACE['heat']}+ ticks)")
            STATE.ticks_heat_critical = 0
    else:
        if STATE.ticks_heat_critical > 0:
            log_event(f"Heat recovered to {STATE.heat_level:.2f} — crisis averted", "INFO")
        STATE.ticks_heat_critical = 0

    # 3 — Hull → breach & decompression
    if STATE.hull_integrity <= 0.40:
        STATE.ticks_hull_critical += 1
        if not STATE.hull_breach_active:
            remaining = GRACE["hull"] - STATE.ticks_hull_critical
            if remaining > 0:
                log_event(
                    f"HULL {STATE.hull_integrity:.2f} — BREACH IMMINENT in {remaining} ticks",
                    "CRITICAL"
                )
            if STATE.ticks_hull_critical >= GRACE["hull"]:
                STATE.hull_breach_active    = True
                STATE.active_event          = EventType.HULL_BREACH
                STATE.event_intensity       = 0.8
                STATE.event_ticks_remaining = 999   # persists until hull repaired
                log_event(
                    "HULL BREACH — decompression active. O2 bleeding, heat plummeting.",
                    "FATAL"
                )
    else:
        STATE.ticks_hull_critical = 0
        if STATE.hull_breach_active and STATE.hull_integrity > 0.55:
            STATE.hull_breach_active    = False
            STATE.active_event          = EventType.NOMINAL
            STATE.event_intensity       = 0.0
            STATE.event_ticks_remaining = 0
            log_event("Hull breach SEALED — decompression stopped.", "INFO")

    # 4 — Power starvation → forced generator offline
    if STATE.power_level <= 0.15:
        STATE.ticks_power_critical += 1
        remaining = GRACE["power"] - STATE.ticks_power_critical
        if remaining > 0:
            log_event(
                f"POWER CRITICAL {STATE.power_level:.2f} — generator forced offline in {remaining} ticks",
                "CRITICAL"
            )
        if STATE.ticks_power_critical >= GRACE["power"]:
            if STATE.forced_gen_offline < STATE.oxygen_generators:
                STATE.forced_gen_offline += 1
                active = STATE.oxygen_generators - STATE.forced_gen_offline
                log_event(
                    f"POWER STARVATION — O2 generator forced offline. Active gens: {active}",
                    "FATAL"
                )
            STATE.ticks_power_critical = 0
    else:
        if STATE.ticks_power_critical > 0:
            log_event(f"Power recovered to {STATE.power_level:.2f}", "INFO")
        STATE.ticks_power_critical = 0
        # Gradually restore forced-offline generators when power is stable
        if STATE.forced_gen_offline > 0 and STATE.power_level > 0.35:
            STATE.forced_gen_offline -= 1
            active = STATE.oxygen_generators - STATE.forced_gen_offline
            log_event(f"Power stable — O2 generator back online. Active: {active}", "INFO")

    # 5 — Crew-zero end check
    if STATE.crew_alive <= 0 and STATE.mission_status != MissionStatus.FAILED:
        end_mission("All crew deceased")

    # ── SLA tracking ─────────────────────────────
    violations = sum([
        STATE.oxygen_level   <= 0.2,
        STATE.power_level    <= 0.15,
        not heat_ok,
        STATE.hull_integrity <= 0.4,
    ])
    if violations:
        STATE.sla_violations += violations

    # ── Mission status + utility ──────────────────
    update_mission_status()
    STATE.utility_score = round(STATE.utility_score + compute_utility(), 4)

    # ── Round vitals ─────────────────────────────
    STATE.oxygen_level     = round(STATE.oxygen_level,     4)
    STATE.power_level      = round(STATE.power_level,      4)
    STATE.heat_level       = round(STATE.heat_level,       4)
    STATE.hull_integrity   = round(STATE.hull_integrity,   4)
    STATE.solar_efficiency = round(STATE.solar_efficiency, 4)


# ─────────────────────────────────────────────
# Simulation loop
# ─────────────────────────────────────────────

async def simulation_loop():
    while RUNNING:
        tick()
        await asyncio.sleep(TICK_RATE)


# ─────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────

app = FastAPI(
    title="ARES-1 Mars Habitat",
    description="Self-adaptive managed system exemplar for POLARIS — crew death, cascading failures, mission status.",
    version="2.0.0"
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup():
    asyncio.create_task(simulation_loop())
    log_event("ARES-1 v2 simulation started", "SYSTEM")


# ─────────────────────────────────────────────
# Telemetry endpoints
# ─────────────────────────────────────────────

@app.get("/telemetry", tags=["Monitoring"],
    summary="Full system telemetry — primary POLARIS Metric Collector endpoint")
def get_telemetry():
    effective_gens = max(0, STATE.oxygen_generators - STATE.forced_gen_offline)
    return {
        "timestamp":       time.time(),
        "simulation_time": STATE.simulation_time,

        "mission": {
            "status":        STATE.mission_status,
            "failure_cause": STATE.failure_cause,
            "end_tick":      STATE.mission_end_tick,
        },

        "vitals": {
            "oxygen_level":   STATE.oxygen_level,
            "power_level":    STATE.power_level,
            "heat_level":     STATE.heat_level,
            "hull_integrity": STATE.hull_integrity,
        },

        "sla": {
            "oxygen_min": 0.20,
            "power_min":  0.15,
            "heat_min":   0.30,
            "heat_max":   0.95,
            "hull_min":   0.40,
        },

        # Grace counters give POLARIS situational urgency
        "warnings": {
            "ticks_o2_critical":    STATE.ticks_o2_critical,
            "ticks_heat_critical":  STATE.ticks_heat_critical,
            "ticks_hull_critical":  STATE.ticks_hull_critical,
            "ticks_power_critical": STATE.ticks_power_critical,
            "grace_periods":        GRACE,
            "hull_breach_active":   STATE.hull_breach_active,
            "forced_gens_offline":  STATE.forced_gen_offline,
        },

        "environment": {
            "solar_efficiency":      STATE.solar_efficiency,
            "active_event":          STATE.active_event,
            "event_intensity":       STATE.event_intensity,
            "event_ticks_remaining": STATE.event_ticks_remaining,
        },

        "infrastructure": {
            "oxygen_generators":     STATE.oxygen_generators,
            "effective_generators":  effective_gens,
            "active_heaters":        STATE.active_heaters,
            "power_mode":            STATE.power_mode,
            "life_support_priority": STATE.life_support_priority,
        },

        "crew": {
            "total": STATE.crew_count,
            "alive": STATE.crew_alive,
            "dead":  STATE.crew_deaths,
        },

        "performance": {
            "sla_violations":       STATE.sla_violations,
            "crew_deaths":          STATE.crew_deaths,
            "total_ticks":          STATE.total_ticks,
            "utility_score":        STATE.utility_score,
            "avg_utility_per_tick": round(STATE.utility_score / max(1, STATE.total_ticks), 4),
        }
    }


@app.get("/telemetry/vitals", tags=["Monitoring"],
    summary="Lightweight vitals-only poll")
def get_vitals():
    return {
        "simulation_time":     STATE.simulation_time,
        "mission_status":      STATE.mission_status,
        "oxygen_level":        STATE.oxygen_level,
        "power_level":         STATE.power_level,
        "heat_level":          STATE.heat_level,
        "hull_integrity":      STATE.hull_integrity,
        "crew_alive":          STATE.crew_alive,
        "active_event":        STATE.active_event,
        "hull_breach_active":  STATE.hull_breach_active,
        "ticks_o2_critical":   STATE.ticks_o2_critical,
        "ticks_heat_critical": STATE.ticks_heat_critical,
        "ticks_hull_critical": STATE.ticks_hull_critical,
        "sla_violations":      STATE.sla_violations,
    }


@app.get("/telemetry/history", tags=["Monitoring"],
    summary="Recent event log for Knowledge Base ingestion")
def get_history(n: int = 50):
    return {"count": min(n, len(STATE.event_log)), "entries": STATE.event_log[-n:]}


@app.get("/result", tags=["Monitoring"],
    summary="Final scorecard — available after MISSION_FAILED")
def get_result():
    return {
        "mission_status":  STATE.mission_status,
        "failure_cause":   STATE.failure_cause,
        "survived_ticks":  STATE.mission_end_tick if STATE.mission_end_tick >= 0 else STATE.simulation_time,
        "crew_survived":   STATE.crew_alive,
        "crew_lost":       STATE.crew_deaths,
        "sla_violations":  STATE.sla_violations,
        "utility_score":   STATE.utility_score,
        "avg_utility":     round(STATE.utility_score / max(1, STATE.total_ticks), 4),
    }


# ─────────────────────────────────────────────
# Adaptation endpoints
# ─────────────────────────────────────────────

class CompositeAction(BaseModel):
    power_mode:            Optional[str] = None
    oxygen_generators:     Optional[int] = None
    active_heaters:        Optional[int] = None
    life_support_priority: Optional[str] = None
    reason:                Optional[str] = None

class PowerModeAction(BaseModel):
    mode: str
    reason: Optional[str] = None

class OxygenAction(BaseModel):
    generators: int
    reason: Optional[str] = None

class HeaterAction(BaseModel):
    heaters: int
    reason: Optional[str] = None

class PriorityAction(BaseModel):
    priority: str
    reason: Optional[str] = None


def _check_alive():
    if STATE.mission_status == MissionStatus.FAILED:
        raise HTTPException(409, f"Mission failed: {STATE.failure_cause}")


@app.post("/adapt/composite", tags=["Adaptation"],
    summary="Apply multiple adaptation actions atomically — preferred POLARIS endpoint")
def adapt_composite(action: CompositeAction):
    _check_alive()
    changes = {}
    if action.power_mode:
        valid = ["NORMAL", "CONSERVATION", "EMERGENCY"]
        if action.power_mode not in valid: raise HTTPException(400, f"power_mode must be one of {valid}")
        changes["power_mode"] = (STATE.power_mode, action.power_mode)
        STATE.power_mode = action.power_mode
    if action.oxygen_generators is not None:
        if not 1 <= action.oxygen_generators <= 3: raise HTTPException(400, "oxygen_generators must be 1–3")
        changes["oxygen_generators"] = (STATE.oxygen_generators, action.oxygen_generators)
        STATE.oxygen_generators = action.oxygen_generators
    if action.active_heaters is not None:
        if not 0 <= action.active_heaters <= 3: raise HTTPException(400, "active_heaters must be 0–3")
        changes["active_heaters"] = (STATE.active_heaters, action.active_heaters)
        STATE.active_heaters = action.active_heaters
    if action.life_support_priority:
        valid = ["OXYGEN", "HEAT", "BALANCED"]
        if action.life_support_priority not in valid: raise HTTPException(400, f"priority must be one of {valid}")
        changes["life_support_priority"] = (STATE.life_support_priority, action.life_support_priority)
        STATE.life_support_priority = action.life_support_priority
    summary = ", ".join([f"{k}: {v[0]}→{v[1]}" for k, v in changes.items()])
    log_event(f"ADAPT [{action.reason or 'no reason'}]: {summary}", "ACTION")
    return {"status": "ok", "changes": {k: {"from": v[0], "to": v[1]} for k, v in changes.items()}}


@app.post("/adapt/power", tags=["Adaptation"])
def adapt_power(action: PowerModeAction):
    _check_alive()
    valid = ["NORMAL", "CONSERVATION", "EMERGENCY"]
    if action.mode not in valid: raise HTTPException(400, f"mode must be one of {valid}")
    old = STATE.power_mode; STATE.power_mode = action.mode
    log_event(f"ADAPT power_mode: {old}→{action.mode}", "ACTION")
    return {"status": "ok", "previous": old, "current": STATE.power_mode}

@app.post("/adapt/oxygen", tags=["Adaptation"])
def adapt_oxygen(action: OxygenAction):
    _check_alive()
    if not 1 <= action.generators <= 3: raise HTTPException(400, "generators must be 1–3")
    old = STATE.oxygen_generators; STATE.oxygen_generators = action.generators
    log_event(f"ADAPT oxygen_generators: {old}→{action.generators}", "ACTION")
    return {"status": "ok", "previous": old, "current": STATE.oxygen_generators}

@app.post("/adapt/heat", tags=["Adaptation"])
def adapt_heat(action: HeaterAction):
    _check_alive()
    if not 0 <= action.heaters <= 3: raise HTTPException(400, "heaters must be 0–3")
    old = STATE.active_heaters; STATE.active_heaters = action.heaters
    log_event(f"ADAPT active_heaters: {old}→{action.heaters}", "ACTION")
    return {"status": "ok", "previous": old, "current": STATE.active_heaters}

@app.post("/adapt/priority", tags=["Adaptation"])
def adapt_priority(action: PriorityAction):
    _check_alive()
    valid = ["OXYGEN", "HEAT", "BALANCED"]
    if action.priority not in valid: raise HTTPException(400, f"priority must be one of {valid}")
    old = STATE.life_support_priority; STATE.life_support_priority = action.priority
    log_event(f"ADAPT priority: {old}→{action.priority}", "ACTION")
    return {"status": "ok", "previous": old, "current": STATE.life_support_priority}


# ─────────────────────────────────────────────
# Control endpoints
# ─────────────────────────────────────────────

@app.post("/control/reset", tags=["Control"])
def reset_simulation():
    global STATE
    STATE = HabitatState()
    log_event("Simulation RESET", "SYSTEM")
    return {"status": "ok", "message": "Reset to initial state"}

@app.post("/control/inject", tags=["Control"],
    summary="Manually inject a disturbance for testing")
def inject_event(event_type: str, intensity: float = 0.7, duration: int = 15):
    valid = [e.value for e in EventType]
    if event_type not in valid: raise HTTPException(400, f"event_type must be one of {valid}")
    if not 0.0 <= intensity <= 1.0: raise HTTPException(400, "intensity must be 0.0–1.0")
    STATE.active_event          = event_type
    STATE.event_intensity       = intensity
    STATE.event_ticks_remaining = duration
    log_event(f"INJECT: {event_type} intensity={intensity} duration={duration}", "INJECT")
    return {"status": "ok", "event": event_type, "intensity": intensity, "duration": duration}


@app.get("/info", tags=["Meta"])
def get_info():
    return {
        "system": "ARES-1 Mars Habitat", "version": "2.0.0",
        "tick_rate_seconds": TICK_RATE,
        "sla": {
            "oxygen_level":   {"min": 0.20, "grace_ticks": GRACE["oxygen"],  "consequence": "crew dies every 5 ticks below threshold"},
            "power_level":    {"min": 0.15, "grace_ticks": GRACE["power"],   "consequence": "O2 generator forced offline after 10 ticks"},
            "heat_level":     {"min": 0.30, "max": 0.95, "grace_ticks": GRACE["heat"], "consequence": "crew dies every 8 ticks outside range"},
            "hull_integrity": {"min": 0.40, "grace_ticks": GRACE["hull"],    "consequence": "hull breach triggers decompression (O2 bleed + rapid heat loss)"},
        },
        "cascading_failures": {
            "hull_breach":      "O2 bleeds -0.03/tick, heat loss +0.025/tick — won't stop until hull > 55%",
            "power_starvation": "generators forced offline one-by-one every 10 ticks; restored when power > 35%",
            "crew_death":       "reduces O2 consumption (fewer breathers), but utility score crew_factor drops",
        },
        "mission_status_transitions": "NOMINAL → WARNING → CRITICAL → FAILED",
        "action_space": {
            "power_mode":            ["NORMAL", "CONSERVATION", "EMERGENCY"],
            "oxygen_generators":     [1, 2, 3],
            "active_heaters":        [0, 1, 2, 3],
            "life_support_priority": ["OXYGEN", "HEAT", "BALANCED"],
        },
        "events": [e.value for e in EventType],
        "endpoints": {
            "telemetry":       "GET  /telemetry",
            "vitals":          "GET  /telemetry/vitals",
            "history":         "GET  /telemetry/history",
            "result":          "GET  /result",
            "adapt_composite": "POST /adapt/composite",
            "inject":          "POST /control/inject",
            "reset":           "POST /control/reset",
            "docs":            "GET  /docs",
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
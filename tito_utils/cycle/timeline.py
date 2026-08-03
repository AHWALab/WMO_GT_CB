"""
Pure cycle-timeline calculator for TITO.

This module encodes the *contracts* for simulation windows, state save/load
times, and phase ordering.  It has no I/O — every decision is a function of
cycle time, mode (operational vs hindcast), QPE source, and config knobs.

Operational STREAM_SAT (cycle time ``T``)
----------------------------------------
    Warmup (if needed):  T − warmup_days  →  T − 40 h   (states @ T−40h)
    Phase A STREAM_SAT:  ss_start         →  ss_end     (states @ ss_end
                         under states/stream_sat/ensS*)
                         ss_end ≈ T − 4 h (IMERG Early latency; data-driven)
    Phase B gap fill:    ss_end           →  T
                         SCaMPR (or HSAF) QPE only
                         states saved under states/scampr|hsaf/ensS*
    Phase C forecast:    T                →  T + LR
                         StormLab ensemble QPF (nested × STREAM-Sat members)
                         states NOT saved
                         Legacy: single GFS/AROME when qpf_sources has no STORMLAB

Hindcast STREAM_SAT (cycle time ``T``)
--------------------------------------
    Warmup (if needed):  same as operational
    Phase A STREAM_SAT:  → T (no IMERG latency)         (states @ T / ss_end)
    Phase B:             skipped (no IMERG latency gap)
    Phase C:             T → T+LR StormLab (or legacy GFS) from STREAM-Sat states
                         states NOT saved
    Next hour (T+1):     loads states saved at ~T (stream_sat)

IMERG operational
-----------------
    Phase IMERG:         → T − 4 h                      (states @ T−4h)
    Phase LR:            T−4h → T (SCaMPR) → T+24h (QPF)  (no states)

IMERG latency is fixed at **4 hours** (IMERG Early), not 4.5.
STREAM-Sat operational ``ss_end`` is data-driven and typically lands near T−4h;
if IMERG is late, SCaMPR gap fill spans whatever remains until ``T``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Sequence


# ── Canonical constants (single source of truth) ───────────────────────────
IMERG_LATENCY = timedelta(hours=4)
WARMUP_STATE_OFFSET = timedelta(hours=40)   # warmup ends / states saved here
STATE_LOOKBACK = timedelta(hours=48)        # find_available_states window
DEFAULT_LR_HOURS = 24
DEFAULT_WARMUP_DAYS = 5


@dataclass(frozen=True)
class PhaseWindow:
    """One EF5 simulation phase within a cycle."""

    name: str
    start: datetime
    end: datetime
    qpe_source: str
    qpf_sources: tuple = ()
    save_states: bool = False
    state_save_time: Optional[datetime] = None
    state_load_time: Optional[datetime] = None
    notes: str = ""

    def __post_init__(self):
        if self.end < self.start:
            raise ValueError(
                f"Phase '{self.name}': end ({self.end}) < start ({self.start})"
            )


@dataclass(frozen=True)
class CyclePlan:
    """Full timing plan for one region at one cycle time."""

    cycle_time: datetime
    mode: str                          # "operational" | "hindcast"
    qpe_source: str
    qpf_sources: tuple
    imerg_latency: timedelta
    lr_duration: timedelta

    # Warmup window (always computed; runner decides whether to execute)
    warmup_start: datetime
    warmup_end: datetime               # == warmup state save time (T − 40h)
    warmup_state_time: datetime
    state_lookback_start: datetime     # cycle_time − 48h

    # Expected QPE end for latency-aware products
    qpe_end: datetime                  # T−4h operational IMERG/STREAM_SAT; T in hindcast
    scampr_end: datetime               # always cycle_time (gap fill target)
    lr_end: datetime                   # cycle_time + lr_duration

    phases: tuple = field(default_factory=tuple)

    @property
    def is_hindcast(self) -> bool:
        return self.mode == "hindcast"

    @property
    def state_save_phases(self) -> List[PhaseWindow]:
        return [p for p in self.phases if p.save_states]

    @property
    def primary_state_time(self) -> Optional[datetime]:
        """Time of the states that the *next* cycle should find."""
        for p in reversed(self.phases):
            if p.save_states and p.state_save_time is not None:
                return p.state_save_time
        return self.warmup_state_time


def round_cycle_time(dt: datetime, step_minutes: int = 60) -> datetime:
    """Floor *dt* to the last completed ``step_minutes`` boundary."""
    if step_minutes <= 0:
        raise ValueError("step_minutes must be > 0")
    if step_minutes == 60:
        return dt.replace(minute=0, second=0, microsecond=0)
    if step_minutes == 30:
        m = 0 if dt.minute < 30 else 30
        return dt.replace(minute=m, second=0, microsecond=0)
    m = (dt.minute // step_minutes) * step_minutes
    return dt.replace(minute=m, second=0, microsecond=0)


def build_cycle_plan(
    cycle_time: datetime,
    *,
    mode: str = "operational",
    qpe_source: str = "STREAM_SAT",
    qpf_sources: Optional[Sequence[str]] = None,
    run_lr: bool = True,
    warmup_days: int = DEFAULT_WARMUP_DAYS,
    imerg_latency: timedelta = IMERG_LATENCY,
    lr_hours: int = DEFAULT_LR_HOURS,
    stream_sat_end: Optional[datetime] = None,
    stream_sat_start: Optional[datetime] = None,
    stream_sat_window_hours: int = 48,
) -> CyclePlan:
    """
    Build the full timing plan for one cycle.

    Parameters
    ----------
    cycle_time
        Simulation / wall-clock cycle time ``T`` (already rounded).
    mode
        ``"operational"`` or ``"hindcast"``.
    qpe_source
        ``IMERG``, ``HSAF``, ``SCAMPR``, or ``STREAM_SAT``.
    qpf_sources
        Requested QPF products for LR (e.g. ``("GFS", "AROME")``).
        In hindcast STREAM_SAT, only GFS is kept (AROME has no archive).
    run_lr
        If False, omit long-range / gap-fill phases.
    stream_sat_end / stream_sat_start
        Optional overrides from actual STREAM-Sat GeoTIFF timestamps.
        If omitted, expected windows are used (T−4h operational, T hindcast).
    """
    mode = mode.strip().lower()
    if mode not in ("operational", "hindcast"):
        raise ValueError(f"Unknown mode: {mode}")

    qpe = qpe_source.strip().upper()
    qpf = tuple(s.strip().upper() for s in (qpf_sources or ("GFS",)))
    lr_duration = timedelta(hours=lr_hours) if run_lr else timedelta(0)

    warmup_end = cycle_time - WARMUP_STATE_OFFSET
    warmup_start = cycle_time - timedelta(days=int(warmup_days))
    lookback_start = cycle_time - STATE_LOOKBACK

    # QPE end: latency-aware in operational IMERG / STREAM_SAT; full T in hindcast
    if mode == "hindcast":
        qpe_end = cycle_time
    elif qpe in ("IMERG", "STREAM_SAT"):
        qpe_end = cycle_time - imerg_latency
    else:
        # HSAF / SCAMPR as primary QPE — product latency handled by retrieve utils
        qpe_end = cycle_time

    scampr_end = cycle_time
    lr_end = cycle_time + lr_duration

    phases: List[PhaseWindow] = []

    if qpe == "STREAM_SAT":
        phases.extend(
            _stream_sat_phases(
                cycle_time=cycle_time,
                mode=mode,
                qpe_end=qpe_end,
                scampr_end=scampr_end,
                lr_end=lr_end,
                qpf=qpf,
                run_lr=run_lr,
                stream_sat_end=stream_sat_end,
                stream_sat_start=stream_sat_start,
                stream_sat_window_hours=stream_sat_window_hours,
            )
        )
    elif qpe == "IMERG":
        phases.extend(
            _imerg_phases(
                cycle_time=cycle_time,
                mode=mode,
                qpe_end=qpe_end,
                scampr_end=scampr_end,
                lr_end=lr_end,
                qpf=qpf,
                run_lr=run_lr,
            )
        )
    elif qpe in ("HSAF", "SCAMPR"):
        phases.extend(
            _direct_qpe_phases(
                cycle_time=cycle_time,
                qpe=qpe,
                scampr_end=scampr_end,
                lr_end=lr_end,
                qpf=qpf,
                run_lr=run_lr,
            )
        )
    else:
        raise ValueError(f"Unsupported qpe_source: {qpe}")

    return CyclePlan(
        cycle_time=cycle_time,
        mode=mode,
        qpe_source=qpe,
        qpf_sources=qpf,
        imerg_latency=imerg_latency,
        lr_duration=lr_duration,
        warmup_start=warmup_start,
        warmup_end=warmup_end,
        warmup_state_time=warmup_end,
        state_lookback_start=lookback_start,
        qpe_end=qpe_end,
        scampr_end=scampr_end,
        lr_end=lr_end,
        phases=tuple(phases),
    )


def expected_state_for_next_cycle(
    plan: CyclePlan,
    next_cycle_time: datetime,
) -> datetime:
    """
    Return the state timestamp the next cycle is expected to load.

    For STREAM_SAT / IMERG operational: previous primary state (~T−4h).
    For STREAM_SAT hindcast: previous cycle_time (ss_end ≈ T).
    Raises if that state would fall outside the next cycle's 48h lookback.
    """
    state_t = plan.primary_state_time
    if state_t is None:
        raise ValueError("Cycle plan has no primary state time")

    lookback_start = next_cycle_time - STATE_LOOKBACK
    if state_t < lookback_start:
        raise ValueError(
            f"State at {state_t} is older than lookback start "
            f"{lookback_start} for next cycle {next_cycle_time}"
        )
    if state_t > next_cycle_time:
        raise ValueError(
            f"State at {state_t} is after next cycle {next_cycle_time}"
        )
    return state_t


# ── Phase builders ─────────────────────────────────────────────────────────

def _stream_sat_phases(
    *,
    cycle_time: datetime,
    mode: str,
    qpe_end: datetime,
    scampr_end: datetime,
    lr_end: datetime,
    qpf: tuple,
    run_lr: bool,
    stream_sat_end: Optional[datetime],
    stream_sat_start: Optional[datetime],
    stream_sat_window_hours: int,
) -> List[PhaseWindow]:
    ss_end = stream_sat_end if stream_sat_end is not None else qpe_end
    if stream_sat_start is not None:
        ss_start = stream_sat_start
    else:
        ss_start = ss_end - timedelta(hours=int(stream_sat_window_hours))

    phases = [
        PhaseWindow(
            name="stream_sat",
            start=ss_start,
            end=ss_end,
            qpe_source="STREAM_SAT",
            qpf_sources=(),
            save_states=True,
            state_save_time=ss_end,
            state_load_time=ss_end,  # look for prior member states near ss_end
            notes=(
                "Hindcast: ss_end ≈ T (no IMERG latency). "
                "Operational: ss_end ≈ T−4h (data-driven). "
                "States under states/stream_sat/ensS*."
            ),
        )
    ]

    # Normalize QPF: STORMLAB preferred; legacy GFS/AROME still allowed
    has_stormlab = any(s == "STORMLAB" for s in qpf)
    legacy_qpf = tuple(s for s in qpf if s in ("GFS", "AROME", "WRF"))
    if mode == "hindcast":
        # AROME has no archive in hindcast
        legacy_qpf = tuple(s for s in legacy_qpf if s == "GFS")
        forecast_qpf = ("STORMLAB",) if has_stormlab else (legacy_qpf or ("GFS",))
    else:
        forecast_qpf = ("STORMLAB",) if has_stormlab else (legacy_qpf or qpf)

    # Phase B — gap fill only (operational). Saves states for Phase C.
    if mode == "operational" and ss_end < scampr_end:
        phases.append(
            PhaseWindow(
                name="gap_fill",
                start=ss_end,
                end=scampr_end,
                qpe_source="SCAMPR",
                qpf_sources=(),
                save_states=True,
                state_save_time=scampr_end,
                state_load_time=ss_end,
                notes=(
                    "SCaMPR/HSAF QPE fills ss_end→T; states saved under "
                    "states/scampr|hsaf/ensS* for StormLab forecast warm-start."
                ),
            )
        )
        qpf_state_load = scampr_end
        qpf_start = scampr_end
    else:
        # Hindcast (or no gap): forecast warm-starts from STREAM-Sat states
        qpf_state_load = ss_end
        qpf_start = ss_end

    if not run_lr:
        return phases

    phases.append(
        PhaseWindow(
            name="stormlab_qpf" if has_stormlab else "forecast_qpf",
            start=qpf_start,
            end=lr_end,
            qpe_source="SCAMPR" if mode == "operational" and ss_end < scampr_end else "STREAM_SAT",
            qpf_sources=forecast_qpf,
            save_states=False,
            state_load_time=qpf_state_load,
            notes=(
                "Nested StormLab ensemble QPF from gap-fill (or STREAM-Sat) states."
                if has_stormlab
                else "Legacy single-member GFS/AROME QPF from gap-fill/STREAM-Sat states."
            ),
        )
    )

    return phases


def _imerg_phases(
    *,
    cycle_time: datetime,
    mode: str,
    qpe_end: datetime,
    scampr_end: datetime,
    lr_end: datetime,
    qpf: tuple,
    run_lr: bool,
) -> List[PhaseWindow]:
    # In hindcast, IMERG has no latency → qpe_end == T; still save states at end.
    phases = [
        PhaseWindow(
            name="imerg",
            start=qpe_end - timedelta(hours=6),  # nominal lookback for display
            end=qpe_end,
            qpe_source="IMERG",
            qpf_sources=(),
            save_states=True,
            state_save_time=qpe_end,
            state_load_time=qpe_end,
            notes="States saved at IMERG end (T−4h operational, T hindcast).",
        )
    ]
    if run_lr and mode == "operational" and qpe_end < scampr_end:
        phases.append(
            PhaseWindow(
                name="scampr_qpf",
                start=qpe_end,
                end=lr_end,
                qpe_source="SCAMPR",
                qpf_sources=qpf,
                save_states=False,
                state_load_time=qpe_end,
                notes="SCaMPR fills T−4h→T; QPF fills T→T+LR.",
            )
        )
    elif run_lr and mode == "hindcast":
        phases.append(
            PhaseWindow(
                name="hindcast_qpf",
                start=qpe_end,
                end=lr_end,
                qpe_source="IMERG",
                qpf_sources=tuple(s for s in qpf if s == "GFS") or ("GFS",),
                save_states=False,
                state_load_time=qpe_end,
                notes="Hindcast LR from IMERG states with GFS only.",
            )
        )
    return phases


def _direct_qpe_phases(
    *,
    cycle_time: datetime,
    qpe: str,
    scampr_end: datetime,
    lr_end: datetime,
    qpf: tuple,
    run_lr: bool,
) -> List[PhaseWindow]:
    phases = [
        PhaseWindow(
            name=qpe.lower(),
            start=cycle_time - timedelta(hours=6),
            end=cycle_time,
            qpe_source=qpe,
            qpf_sources=(),
            save_states=True,
            state_save_time=cycle_time,
            state_load_time=cycle_time,
            notes=f"{qpe} as primary QPE through cycle time T.",
        )
    ]
    if run_lr:
        phases.append(
            PhaseWindow(
                name="qpf",
                start=cycle_time,
                end=lr_end,
                qpe_source=qpe,
                qpf_sources=qpf,
                save_states=False,
                state_load_time=cycle_time,
                notes="Long-range QPF from states at T.",
            )
        )
    return phases

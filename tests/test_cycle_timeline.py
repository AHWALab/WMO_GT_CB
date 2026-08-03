"""
Unit tests for TITO cycle timeline contracts.

These tests do NOT download precip or run EF5.  They verify that start/end
times, state save/load points, and cross-cycle chaining match the operational
and hindcast designs.
"""

from datetime import datetime, timedelta

import pytest

from tito_utils.cycle.timeline import (
    IMERG_LATENCY,
    STATE_LOOKBACK,
    WARMUP_STATE_OFFSET,
    build_cycle_plan,
    expected_state_for_next_cycle,
    round_cycle_time,
)


T = datetime(2026, 7, 22, 12, 0, 0)


# ── round_cycle_time ───────────────────────────────────────────────────────

def test_round_cycle_time_hourly():
    assert round_cycle_time(datetime(2026, 7, 22, 12, 37, 15)) == datetime(2026, 7, 22, 12, 0)


def test_round_cycle_time_half_hour():
    assert round_cycle_time(datetime(2026, 7, 22, 12, 44), 30) == datetime(2026, 7, 22, 12, 30)
    assert round_cycle_time(datetime(2026, 7, 22, 12, 14), 30) == datetime(2026, 7, 22, 12, 0)


# ── IMERG latency constant ─────────────────────────────────────────────────

def test_imerg_latency_is_four_hours_not_four_point_five():
    assert IMERG_LATENCY == timedelta(hours=4)
    assert IMERG_LATENCY != timedelta(hours=4, minutes=30)


# ── Operational STREAM_SAT ─────────────────────────────────────────────────

def test_operational_stream_sat_windows():
    plan = build_cycle_plan(
        T,
        mode="operational",
        qpe_source="STREAM_SAT",
        qpf_sources=["STORMLAB"],
        warmup_days=5,
    )

    assert plan.cycle_time == T
    assert plan.qpe_end == T - IMERG_LATENCY          # T−4h
    assert plan.scampr_end == T
    assert plan.lr_end == T + timedelta(hours=24)

    # Warmup ends 40h before T so states fall inside 48h lookback
    assert plan.warmup_end == T - WARMUP_STATE_OFFSET
    assert plan.warmup_state_time == plan.warmup_end
    assert plan.warmup_start == T - timedelta(days=5)
    assert plan.state_lookback_start == T - STATE_LOOKBACK
    assert plan.warmup_end >= plan.state_lookback_start

    names = [p.name for p in plan.phases]
    assert names == ["stream_sat", "gap_fill", "stormlab_qpf"]

    ss = plan.phases[0]
    assert ss.save_states is True
    assert ss.state_save_time == plan.qpe_end
    assert ss.qpe_source == "STREAM_SAT"
    assert ss.qpf_sources == ()

    gap = plan.phases[1]
    assert gap.name == "gap_fill"
    assert gap.save_states is True
    assert gap.start == plan.qpe_end
    assert gap.end == T
    assert gap.qpe_source == "SCAMPR"
    assert gap.qpf_sources == ()
    assert gap.state_save_time == T

    fc = plan.phases[2]
    assert fc.name == "stormlab_qpf"
    assert fc.save_states is False
    assert fc.start == T
    assert fc.end == plan.lr_end
    assert fc.qpf_sources == ("STORMLAB",)
    assert fc.state_load_time == T


def test_operational_stream_sat_data_driven_ss_end():
    """If IMERG is late (e.g. T−5h), ss_end follows data; gap widens."""
    late_ss_end = T - timedelta(hours=5)
    plan = build_cycle_plan(
        T,
        mode="operational",
        qpe_source="STREAM_SAT",
        qpf_sources=["STORMLAB"],
        stream_sat_end=late_ss_end,
        stream_sat_start=late_ss_end - timedelta(hours=48),
    )
    ss = plan.phases[0]
    assert ss.end == late_ss_end
    assert ss.state_save_time == late_ss_end
    gap = plan.phases[1]
    assert gap.name == "gap_fill"
    assert gap.start == late_ss_end
    assert gap.end == T
    fc = plan.phases[2]
    assert fc.start == T
    assert fc.end == T + timedelta(hours=24)


# ── Hindcast STREAM_SAT ────────────────────────────────────────────────────

def test_hindcast_stream_sat_no_latency_stormlab():
    plan = build_cycle_plan(
        T,
        mode="hindcast",
        qpe_source="STREAM_SAT",
        qpf_sources=["STORMLAB", "AROME"],
    )

    assert plan.qpe_end == T                          # no latency
    assert plan.phases[0].name == "stream_sat"
    assert plan.phases[0].end == T
    assert plan.phases[0].state_save_time == T

    # No gap_fill in hindcast; forecast from STREAM-Sat states
    assert len(plan.phases) == 2
    assert plan.phases[1].name == "stormlab_qpf"
    assert plan.phases[1].qpf_sources == ("STORMLAB",)
    assert plan.phases[1].save_states is False
    assert plan.phases[1].state_load_time == T


def test_hindcast_stream_sat_legacy_gfs():
    plan = build_cycle_plan(
        T,
        mode="hindcast",
        qpe_source="STREAM_SAT",
        qpf_sources=["GFS", "AROME"],
    )
    assert plan.phases[1].name == "forecast_qpf"
    assert plan.phases[1].qpf_sources == ("GFS",)  # AROME dropped


# ── State chaining across hourly cycles ────────────────────────────────────

def test_hindcast_state_chaining_hour_to_hour():
    """Hour N saves at T; hour N+1 must find that state inside 48h lookback."""
    t0 = datetime(2026, 7, 22, 0, 0)
    t1 = t0 + timedelta(hours=1)

    plan0 = build_cycle_plan(t0, mode="hindcast", qpe_source="STREAM_SAT")
    assert plan0.primary_state_time == t0

    expected = expected_state_for_next_cycle(plan0, t1)
    assert expected == t0

    plan1 = build_cycle_plan(t1, mode="hindcast", qpe_source="STREAM_SAT")
    # Next cycle lookback must cover previous state
    assert plan1.state_lookback_start <= expected < plan1.cycle_time


def test_operational_state_chaining_hour_to_hour():
    """Operational: primary state is gap-fill save at T (for next-cycle lookback).

    STREAM-Sat still saves at T−4h under states/stream_sat; gap fill saves at T
    under states/scampr.  primary_state_time is the last save (T).
    """
    t0 = datetime(2026, 7, 22, 12, 0)
    t1 = t0 + timedelta(hours=1)

    plan0 = build_cycle_plan(
        t0, mode="operational", qpe_source="STREAM_SAT", qpf_sources=["STORMLAB"])
    saved = plan0.primary_state_time
    assert saved == t0  # gap_fill state at cycle time

    expected = expected_state_for_next_cycle(plan0, t1)
    assert expected == saved

    plan1 = build_cycle_plan(
        t1, mode="operational", qpe_source="STREAM_SAT", qpf_sources=["STORMLAB"])
    assert plan1.state_lookback_start <= expected
    assert plan1.qpe_end == t1 - IMERG_LATENCY


def test_multi_hour_hindcast_chain():
    """Simulate 5 consecutive hindcast hours — each state must be findable."""
    start = datetime(2026, 7, 22, 0, 0)
    prev_plan = None
    for i in range(5):
        ct = start + timedelta(hours=i)
        plan = build_cycle_plan(ct, mode="hindcast", qpe_source="STREAM_SAT")
        assert plan.primary_state_time == ct
        if prev_plan is not None:
            st = expected_state_for_next_cycle(prev_plan, ct)
            assert st == ct - timedelta(hours=1)
            assert plan.state_lookback_start <= st < ct
        prev_plan = plan


# ── Warmup placement ───────────────────────────────────────────────────────

def test_warmup_states_inside_48h_lookback():
    plan = build_cycle_plan(T, mode="operational", qpe_source="STREAM_SAT", warmup_days=5)
    # Warmup states at T−40h must be ≥ lookback start T−48h
    assert plan.warmup_state_time >= plan.state_lookback_start
    assert (T - plan.warmup_state_time) == WARMUP_STATE_OFFSET
    assert (T - plan.state_lookback_start) == STATE_LOOKBACK


def test_warmup_duration():
    plan = build_cycle_plan(T, warmup_days=10)
    assert plan.warmup_start == T - timedelta(days=10)
    assert plan.warmup_end == T - timedelta(hours=40)


# ── IMERG operational ──────────────────────────────────────────────────────

def test_imerg_operational_saves_at_t_minus_4():
    plan = build_cycle_plan(
        T,
        mode="operational",
        qpe_source="IMERG",
        qpf_sources=["GFS"],
    )
    assert plan.qpe_end == T - IMERG_LATENCY
    imerg = plan.phases[0]
    assert imerg.name == "imerg"
    assert imerg.state_save_time == T - IMERG_LATENCY
    assert imerg.save_states is True

    gap = plan.phases[1]
    assert gap.name == "scampr_qpf"
    assert gap.start == T - IMERG_LATENCY
    assert gap.save_states is False


def test_imerg_hindcast_no_scampr_gap():
    plan = build_cycle_plan(T, mode="hindcast", qpe_source="IMERG", qpf_sources=["GFS", "AROME"])
    assert plan.qpe_end == T
    assert plan.phases[0].state_save_time == T
    assert plan.phases[1].name == "hindcast_qpf"
    assert plan.phases[1].qpf_sources == ("GFS",)


# ── Guatemala / no AROME ───────────────────────────────────────────────────

def test_guatemala_stormlab_qpf():
    plan = build_cycle_plan(
        T,
        mode="operational",
        qpe_source="STREAM_SAT",
        qpf_sources=["STORMLAB"],
    )
    assert plan.phases[2].qpf_sources == ("STORMLAB",)


# ── LR disabled ────────────────────────────────────────────────────────────

def test_no_lr_stream_sat_plus_gap_only():
    """run_lr=False skips forecast; gap fill (QPE) still runs in ops."""
    plan = build_cycle_plan(T, mode="operational", qpe_source="STREAM_SAT", run_lr=False)
    names = [p.name for p in plan.phases]
    assert names == ["stream_sat", "gap_fill"]
    assert plan.lr_end == T
    assert plan.phases[1].save_states is True


# ── Phase ordering invariants ──────────────────────────────────────────────

def test_phases_are_contiguous_for_stream_sat_operational():
    plan = build_cycle_plan(
        T, mode="operational", qpe_source="STREAM_SAT", qpf_sources=["STORMLAB"])
    ss, gap, fc = plan.phases
    assert ss.end == gap.start
    assert gap.end == fc.start
    assert fc.end == plan.lr_end


def test_primary_state_is_last_save():
    plan = build_cycle_plan(
        T, mode="operational", qpe_source="STREAM_SAT", qpf_sources=["STORMLAB"])
    # Last save is gap_fill at T (not STREAM-Sat at T−4h)
    assert plan.primary_state_time == plan.phases[1].state_save_time
    assert plan.primary_state_time == T


def test_stale_state_raises():
    old = build_cycle_plan(
        datetime(2026, 1, 1, 0, 0),
        mode="hindcast",
        qpe_source="STREAM_SAT",
    )
    with pytest.raises(ValueError, match="older than lookback"):
        expected_state_for_next_cycle(old, datetime(2026, 7, 22, 0, 0))

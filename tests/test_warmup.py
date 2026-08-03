"""Warmup decision unit tests."""

from datetime import datetime, timedelta

from tito_utils.cycle.warmup import decide_warmup, warmup_window
from tito_utils.cycle.timeline import WARMUP_STATE_OFFSET, STATE_LOOKBACK


T = datetime(2026, 7, 22, 12, 0)


def test_warmup_window_placement():
    start, end = warmup_window(T, warmup_days=5)
    assert end == T - WARMUP_STATE_OFFSET
    assert start == T - timedelta(days=5)
    assert end >= T - STATE_LOOKBACK


def test_warmup_skipped_when_disabled():
    d = decide_warmup("Guatemala", T, qpe_source="STREAM_SAT", warmup_enabled=False)
    assert d.needed is False
    assert d.reason == "warmup_disabled"


def test_warmup_skipped_when_states_exist():
    st = T - timedelta(hours=5)
    d = decide_warmup(
        "Guatemala", T, qpe_source="STREAM_SAT",
        states_found=True, states_time=st,
    )
    assert d.needed is False
    assert "states_ok" in d.reason


def test_warmup_needed_without_states():
    d = decide_warmup(
        "Guatemala", T, qpe_source="STREAM_SAT",
        states_found=False, warmup_precip_source="IMERG",
    )
    assert d.needed is True
    assert d.reason == "no_states_within_48h"
    assert d.is_stream_sat is True
    assert d.precip_source == "IMERG"
    assert d.warmup_end == T - WARMUP_STATE_OFFSET


def test_warmup_imerg_region_not_stream_sat():
    d = decide_warmup("Antigua", T, qpe_source="IMERG", states_found=False)
    assert d.needed is True
    assert d.is_stream_sat is False

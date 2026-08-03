"""Tests for EF5 job helpers (no EF5 / network)."""

from datetime import datetime
from pathlib import Path

from tito_utils.ef5.jobs.helpers import parse_streamsat_tif_window, resolve_cold_start_window
from types import SimpleNamespace
from datetime import timedelta


def test_parse_streamsat_tif_window(tmp_path: Path):
    ens = tmp_path / "ensP1"
    ens.mkdir()
    # STREAM-Sat naming: streamsat.qpe.YYYYMMDDHHMM.mmhInst.tif
    for stamp in ("202607221000", "202607221030", "202607221200"):
        (ens / f"streamsat.qpe.{stamp}.mmhInst.tif").write_text("x")

    parsed = parse_streamsat_tif_window(str(ens), "streamsat")
    assert parsed is not None
    ss_start, ss_end, stamps = parsed
    assert ss_start == datetime(2026, 7, 22, 10, 0)
    assert ss_end == datetime(2026, 7, 22, 12, 0)
    assert len(stamps) == 3


def test_parse_streamsat_tif_window_empty(tmp_path: Path):
    ens = tmp_path / "ensP1"
    ens.mkdir()
    assert parse_streamsat_tif_window(str(ens)) is None


def test_resolve_cold_start_window_defaults():
    cfg = SimpleNamespace()
    sim_end = datetime(2026, 7, 22, 8, 0)
    begin, warm_end = resolve_cold_start_window(cfg, sim_end)
    assert warm_end == sim_end - timedelta(hours=2)
    assert begin == warm_end - timedelta(hours=6)

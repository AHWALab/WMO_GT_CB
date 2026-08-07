"""Tests for EF5 job helpers (no EF5 / network)."""

from datetime import datetime
from pathlib import Path

from tito_utils.ef5.jobs.helpers import (
    parse_streamsat_tif_window,
    region_path_key,
    resolve_cold_start_window,
    resolve_control_template,
    resolve_region_resolution,
)
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


def test_region_path_key_and_resolution_map():
    assert region_path_key("Guatemala", "900m") == "guatemala_900m"
    assert resolve_region_resolution(
        "Guatemala", "90m", {"Guatemala": "900m"}) == "900m"
    assert resolve_region_resolution("Haiti", "90m", {"Guatemala": "900m"}) == "90m"


def test_resolve_control_template_prefers_resolution(tmp_path: Path):
    (tmp_path / "ef5_Guatemala_900m_control_template.txt").write_text("900")
    (tmp_path / "ef5_Guatemala_90m_control_template.txt").write_text("90")
    assert resolve_control_template(
        str(tmp_path), "Guatemala", "900m") == "ef5_Guatemala_900m_control_template.txt"
    assert resolve_control_template(
        str(tmp_path), "Guatemala", "90m") == "ef5_Guatemala_90m_control_template.txt"


def test_resolve_control_template_override_and_fallback(tmp_path: Path):
    (tmp_path / "custom.txt").write_text("c")
    (tmp_path / "ef5_Haiti_control_template.txt").write_text("h")
    assert resolve_control_template(
        str(tmp_path),
        "Haiti",
        "90m",
        region_template_map={"Haiti": "custom.txt"},
    ) == "custom.txt"
    assert resolve_control_template(
        str(tmp_path), "Antigua", "90m",
        default_template="ef5_Antigua_control_template.txt",
    ) == "ef5_Antigua_control_template.txt"

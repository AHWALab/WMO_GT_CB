"""Tests for build_region_configs (legacy key compatibility)."""

from datetime import datetime
from types import SimpleNamespace

from tito_utils.cycle.region_plan import build_region_configs
from tito_utils.cycle.timeline import IMERG_LATENCY


T = datetime(2026, 7, 22, 12, 0)


def _cfg():
    return SimpleNamespace(
        region_template_map={},
        warmup_days=5,
        templates="ef5_Antigua_control_template.txt",
    )


def test_stream_sat_region_config_has_cycle_plan():
    configs = build_region_configs(
        ["Guatemala"],
        {"Guatemala": T},
        {"Guatemala": "STREAM_SAT"},
        {"Guatemala": ["GFS"]},
        config=_cfg(),
        hindcast_mode=False,
        lr_run=True,
        states_path="states/",
        data_path="outputs/",
        qpf_store_path="qpf_store/",
        template_path="templates/",
        default_template="ef5_Antigua_control_template.txt",
    )
    cfg = configs["Guatemala"]
    assert cfg["region_key"] == "guatemala"
    assert cfg["r_scampr_end"] == T
    assert cfg["r_end_lr"] == T.replace(hour=12) + (T - T) + __import__("datetime").timedelta(hours=24)
    assert cfg["cycle_plan"].qpe_end == T - IMERG_LATENCY
    assert cfg["cycle_plan"].phases[0].name == "stream_sat"
    assert [p.name for p in cfg["cycle_plan"].phases] == [
        "stream_sat", "gap_fill", "forecast_qpf",
    ]
    assert cfg["cycle_plan"].primary_state_time == T  # gap_fill save
    # Legacy keys for STREAM_SAT stay at T (ss_end is data-driven later)
    assert cfg["r_imerg_end"] == T


def test_imerg_region_config_latency_keys():
    configs = build_region_configs(
        ["Haiti"],
        {"Haiti": T},
        {"Haiti": "IMERG"},
        {"Haiti": ["GFS", "AROME"]},
        config=_cfg(),
        hindcast_mode=False,
        lr_run=True,
        states_path="states/",
        data_path="outputs/",
        qpf_store_path="qpf_store/",
        template_path="templates/",
        default_template="ef5_Antigua_control_template.txt",
    )
    cfg = configs["Haiti"]
    assert cfg["r_imerg_end"] == T - IMERG_LATENCY
    assert cfg["r_warm_end"] == T - IMERG_LATENCY
    # IMERG still saves primary state at T−4h (combined LR does not save)
    assert cfg["cycle_plan"].primary_state_time == T - IMERG_LATENCY


def test_hindcast_stream_sat_plan_attached():
    configs = build_region_configs(
        ["Guatemala"],
        {"Guatemala": T},
        {"Guatemala": "STREAM_SAT"},
        {"Guatemala": ["GFS", "AROME"]},
        config=_cfg(),
        hindcast_mode=True,
        lr_run=True,
        states_path="states/",
        data_path="outputs/",
        qpf_store_path="qpf_store/",
        template_path="templates/",
        default_template="ef5_Antigua_control_template.txt",
    )
    plan = configs["Guatemala"]["cycle_plan"]
    assert plan.is_hindcast
    assert plan.qpe_end == T
    assert plan.phases[1].name == "forecast_qpf"
    assert plan.phases[1].qpf_sources == ("GFS",)

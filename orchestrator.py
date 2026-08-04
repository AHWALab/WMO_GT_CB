"""
Real-time operational EF5 execution script
==========================================

IMERG-based operational system that integrates NWP outputs (GFS, AROME)
and SCaMPR gap-fill QPE to produce flash-flood forecasts in real time.

Precipitation is prepared via :mod:`tito_utils.precip`.
Cycle timing / warmup live in :mod:`tito_utils.cycle`.
EF5 control prep & execution live in :mod:`tito_utils.ef5.jobs`.

Contributors:
    Vanessa Robledo  - vrobledodelgado@uiowa.edu
    Humberto Vergara - humberto-vergaraarrieta@uiowa.edu
    Naman Mehta      - naman-mehta@uiowa.edu

Usage::

    $> python orchestrator.py <configuration_file.py>
    $> python orchestrator.py config.py --regions Antigua,Haiti
"""

import importlib
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from tito_utils.precip import prepare_cycle_precip, summarize_shared_precip
from tito_utils.logging_utils import (
    console,
    configure_console_verbosity,
    debug_print,
    is_debug,
    is_user,
    setup_run_log,
    suppress_third_party_noise,
    user_print,
)
from tito_utils.cycle.warmup_runner import run_warmup_if_needed
from tito_utils.cycle.region_plan import build_region_configs
from tito_utils.cycle.timeline import round_cycle_time
from tito_utils.ef5.jobs import run_ef5_job_pipeline

# Quiet GDAL / OpenMP noise as early as possible (before heavy imports run)
suppress_third_party_noise()
debug_print(">>> Modules imported")


def main(args):
    # ── CLI ──────────────────────────────────────────────────────────────
    import argparse as _argparse
    _ap = _argparse.ArgumentParser(add_help=False)
    _ap.add_argument("config", nargs="?", default="Caribbean_Comoros_config")
    _ap.add_argument("--regions", default=None,
                     help="Comma-separated region names (overrides config)")
    _ap.add_argument("--hindcast-date", default=None,
                     help="Override HindCastDate in config (format: YYYY-MM-DD HH:MM). "
                          "Forces HindCastMode=True for this run.")
    _ap.add_argument("--debug-console", action="store_true",
                     help="Full developer console logs (overrides console_verbosity)")
    _cli, _ = _ap.parse_known_args(args[1:])
    _cli_regions = ([r.strip() for r in _cli.regions.split(",") if r.strip()]
                    if _cli.regions else None)
    _cli_hindcast_date = _cli.hindcast_date

    config_module_name = os.path.splitext(os.path.basename(_cli.config))[0]
    config = importlib.import_module(config_module_name)
    # Verbosity: CLI --debug-console > env TITO_CONSOLE_VERBOSITY > config
    if getattr(_cli, "debug_console", False):
        os.environ["TITO_CONSOLE_VERBOSITY"] = "debug"
    configure_console_verbosity(getattr(config, "console_verbosity", "user"))
    suppress_third_party_noise()
    debug_print(">>> Config file loaded")
    if is_user():
        user_print(f"TITO ready  (console=user; set console_verbosity='debug' or "
                   f"--debug-console for full logs)")

    regions_to_run = getattr(config, "regions_to_run", [config.subdomain])
    if isinstance(regions_to_run, str):
        regions_to_run = [regions_to_run]
    regions_to_run = [str(r).strip() for r in regions_to_run if str(r).strip()]
    if not regions_to_run:
        raise ValueError("No regions configured.  Set regions_to_run.")
    if _cli_regions:
        regions_to_run = [r for r in _cli_regions if r]

    systemTimestep = config.systemTimestep
    LR_run = config.run_LR
    qpe_source_default = getattr(config, "qpe_source", "IMERG").strip().upper()
    qpf_source_default = getattr(config, "qpf_source", "GFS").strip().upper()
    region_forcing_map = getattr(config, "region_forcing_map", {})
    qpe_gap_fill_mode = getattr(config, "qpe_gap_fill_mode", "IMERG_ONLY").strip().upper()

    _VALID_QPE = {"IMERG", "HSAF", "SCAMPR", "STREAM_SAT"}
    _VALID_QPF = {"GFS", "WRF", "AROME", "STORMLAB"}

    def _normalize_qpe(val, fallback):
        c = str(val).strip().upper()
        return c if c in _VALID_QPE else fallback

    def _normalize_qpf(val, fallback):
        if isinstance(val, (list, tuple)):
            return [v for v in (str(x).strip().upper() for x in val)
                    if v in _VALID_QPF] or [fallback]
        c = str(val).strip().upper()
        return [c] if c in _VALID_QPF else [fallback]

    region_qpe_sources = {}
    region_qpf_requested = {}
    for region in regions_to_run:
        rc = region_forcing_map.get(region, {}) if isinstance(region_forcing_map, dict) else {}
        if not isinstance(rc, dict):
            rc = {}
        region_qpe_sources[region] = _normalize_qpe(
            rc.get("qpe_source", rc.get("qpe", qpe_source_default)), qpe_source_default)
        region_qpf_requested[region] = _normalize_qpf(
            rc.get("qpf_source", rc.get("qpf", qpf_source_default)), qpf_source_default)

    HindCastMode = getattr(config, "HindCastMode", False)
    if _cli_hindcast_date:
        HindCastMode = True
        # prepare_precip / STREAM-Sat read config.HindCastMode — must set on config
        config.HindCastMode = True
        config.HindCastDate = _cli_hindcast_date
        config.HindCastEndDate = ""
    if HindCastMode:
        region_cycle_times = {}
        hc_dates = []
        hc_start = datetime.strptime(
            str(getattr(config, "HindCastDate", "2024-07-04 09:00")), "%Y-%m-%d %H:%M")
        hc_end_str = str(getattr(config, "HindCastEndDate", "") or "").strip()
        if hc_end_str:
            hc_end = datetime.strptime(hc_end_str, "%Y-%m-%d %H:%M")
            t = hc_start
            while t <= hc_end:
                hc_dates.append(t)
                t += timedelta(hours=1)
            console.info("[bold]HINDCAST LOOP[/] %s → %s (%d cycles)",
                         hc_start.strftime("%Y-%m-%d %H:%M"),
                         hc_end.strftime("%Y-%m-%d %H:%M"), len(hc_dates))
        else:
            hc_dates = [hc_start]
            console.info("[bold]HINDCAST MODE[/] — single cycle: %s",
                         hc_start.strftime("%Y-%m-%d %H:%M"))

        for cycle_idx, cycle_time in enumerate(hc_dates):
            if len(hc_dates) > 1:
                console.rule(
                    f"[bold]Hindcast cycle {cycle_idx+1}/{len(hc_dates)}: "
                    f"{cycle_time.strftime('%Y-%m-%d %H:%M')} UTC[/]")
            for r in regions_to_run:
                region_cycle_times[r] = cycle_time
            _run_single_cycle(
                cycle_time, region_cycle_times, config, regions_to_run,
                LR_run, region_qpe_sources, region_qpf_requested,
                              qpe_gap_fill_mode, HindCastMode)
    else:
        cycle_time = round_cycle_time(
            datetime.now(timezone.utc).replace(tzinfo=None), systemTimestep)
        region_cycle_times = {r: cycle_time for r in regions_to_run}
        _run_single_cycle(
            cycle_time, region_cycle_times, config, regions_to_run,
            LR_run, region_qpe_sources, region_qpf_requested,
                          qpe_gap_fill_mode, HindCastMode)


def _run_single_cycle(
    cycle_time, region_cycle_times, config, regions_to_run,
    LR_run, region_qpe_sources, region_qpf_requested,
    qpe_gap_fill_mode, HindCastMode,
):
    """Single-cycle pipeline: warmup → precip → region plan → EF5 jobs."""
    systemModel = config.systemModel
    ef5Path = config.ef5Path
    statesPath = config.statesPath
    precipEF5Folder = config.precipEF5Folder
    modelStates = config.modelStates
    templatePath = config.templatePath
    default_template = config.templates
    basicPath = getattr(config, "basicPath", "EF5_conf/basic/")
    parametersPath = getattr(config, "parametersPath", "EF5_conf/parameters/")
    dataPath = config.dataPath
    qpf_store_path = config.qpf_store_path
    SEND_ALERTS = config.SEND_ALERTS
    alert_recipients = config.alert_recipients
    smtp_config = {
        "smtp_server": config.smtp_server,
        "smtp_port": config.smtp_port,
        "account_address": config.account_address,
        "account_password": config.account_password,
        "alert_sender": config.alert_sender,
    }
    model_resolution = getattr(config, "model_resolution", "90m")
    region_resolution_map = getattr(config, "region_resolution_map", {})
    systemName = config.systemName
    LR_TimeStep = config.LR_timestep

    ss_out_root = getattr(config, "stream_sat_output_folder", "outputs/stream_sat/")
    master_log = setup_run_log(
        ss_out_root, f"pipeline_{cycle_time.strftime('%Y%m%d_%H%M')}")
    master_log.info("TITO cycle start — %s UTC", cycle_time.strftime("%Y-%m-%d %H:%M"))
    master_log.info("Regions: %s", ", ".join(regions_to_run))
    for r in regions_to_run:
        master_log.info(
            "  %s: qpe=%s qpf=%s", r,
                        region_qpe_sources.get(r, "?"),
                        region_qpf_requested.get(r, []))

    console.rule(f"[bold]TITO Cycle {cycle_time.strftime('%Y-%m-%d %H:%M')} UTC[/]")
    print(f"  Regions: {', '.join(regions_to_run)}")
    t_start = time.time()

    # STEP 1 — Warmup
    run_warmup_if_needed(
        cycle_time, regions_to_run, region_qpe_sources, config,
        statesPath, modelStates, model_resolution, region_resolution_map,
        ef5Path, systemModel, systemName, templatePath, basicPath,
        parametersPath, LR_TimeStep, dataPath, qpf_store_path,
        precipEF5Folder, SEND_ALERTS, alert_recipients, smtp_config,
        master_log=master_log,
    )

    # Pre-clean precipEF5 (always run; console message only in debug)
    if is_debug():
        console.info("[bold]Pre-clean:[/] wiping precipEF5 …")
    for root, dirs, files in os.walk(precipEF5Folder, topdown=False):
        for f in files:
            try:
                os.remove(os.path.join(root, f))
            except OSError:
                pass
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass
    master_log.info("Pre-clean: wiped precipEF5 (%s)", precipEF5Folder)

    # STEP 2 — Precipitation
    console.info("[bold]STEP 2:[/] Download & prepare precipitation …")
    shared = prepare_cycle_precip(
        regions_to_run, region_cycle_times,
        region_qpe_sources, region_qpf_requested,
        config,
        master_log=master_log,
    )
    if is_debug():
        console.info("[bold]STEP 2:[/] Precipitation ready. %s",
                     summarize_shared_precip(shared))
    else:
        console.info("[bold]STEP 2:[/] Precipitation ready.")

    # STEP 3 — Region timing configs
    region_configs = build_region_configs(
        regions_to_run, region_cycle_times,
        region_qpe_sources, region_qpf_requested,
        config=config,
        hindcast_mode=HindCastMode,
        lr_run=LR_run,
        states_path=statesPath,
        data_path=dataPath,
        qpf_store_path=qpf_store_path,
        template_path=templatePath,
        default_template=default_template,
    )

    # STEPS 4–7 — EF5 jobs
    run_ef5_job_pipeline(
        regions_to_run=regions_to_run,
        region_configs=region_configs,
        region_qpe_sources=region_qpe_sources,
        shared=shared,
        config=config,
        hindcast_mode=HindCastMode,
        lr_run=LR_run,
        qpe_gap_fill_mode=qpe_gap_fill_mode,
        cycle_time=cycle_time,
        t_start=t_start,
        master_log=master_log,
        ef5Path=ef5Path,
        systemModel=systemModel,
        systemName=systemName,
        modelStates=modelStates,
        templatePath=templatePath,
        basicPath=basicPath,
        parametersPath=parametersPath,
        LR_TimeStep=LR_TimeStep,
        precipEF5Folder=precipEF5Folder,
        model_resolution=model_resolution,
        region_resolution_map=region_resolution_map,
        SEND_ALERTS=SEND_ALERTS,
        alert_recipients=alert_recipients,
        smtp_config=smtp_config,
        qpf_store_path=qpf_store_path,
    )


if __name__ == "__main__":
    main(sys.argv)

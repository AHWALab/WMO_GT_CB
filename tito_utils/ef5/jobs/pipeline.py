"""
EF5 job pipeline: stage QPF → build jobs → run EF5 → cleanup.

Called by the orchestrator after precip prep and region_configs are ready.
"""

from __future__ import annotations

import os
import shutil
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from tito_utils.file_utils.file_handling import mkdir_p, newline
from tito_utils.file_utils.cleanup import cleanup_staged_precip_folders
from tito_utils.ef5.ef5_routines import run_ef5_simulations_parallel
from tito_utils.logging_utils import console
from tito_utils.qpf_utils.arome_downloader import get_arome_domain_for_region
from tito_utils.ef5.jobs.helpers import copy_tifs_from_shared
from tito_utils.ef5.jobs.builders import (
    JobBatch,
    build_imerg_job,
    build_lr_jobs,
    build_jobs_parallel,
    build_streamsat_jobs_parallel,
    seed_gap_states_from_streamsat,
)


def _fmt_secs(s: float) -> str:
    s = max(0.0, float(s))
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m}m {sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {sec:02d}s"


def _run_phase_jobs(label: str, jobs: list, master_log=None) -> dict:
    """Run a job list; return wall-clock + per-job timings."""
    if not jobs:
        return {"phase": label, "n": 0, "wall_s": 0.0, "jobs": []}
    t0 = time.time()
    per = run_ef5_simulations_parallel(
        jobs,
        max_workers=min(len(jobs), max(1, (os.cpu_count() or 4))),
    ) or []
    wall = time.time() - t0
    sum_job = sum(float(t.get("seconds", 0) or 0) for t in per)
    msg = (
        f"    {label}: wall={_fmt_secs(wall)}  "
        f"jobs={len(jobs)}  sum_job_cpu≈{_fmt_secs(sum_job)}"
    )
    print(msg)
    if master_log:
        master_log.info(msg)
        for t in sorted(per, key=lambda x: x.get("label", "")):
            st = "OK" if t.get("ok", True) else "FAIL"
            master_log.info(
                "      %s  %s  %s",
                t.get("label", "?"), st, _fmt_secs(t.get("seconds", 0)),
            )
    return {"phase": label, "n": len(jobs), "wall_s": wall, "jobs": per}


def _stage_qpf(
    regions_to_run: Sequence[str],
    region_configs: Mapping[str, dict],
    shared: Any,
) -> None:
    print("***_________Staging QPF to per-region qpf_store_________***")
    for region in regions_to_run:
        cfg = region_configs[region]
        ck = cfg["cycle_time_key"]
        store = cfg["region_qpf_store"]
        mkdir_p(store)

        for qpf_src in cfg["qpf_sources"]:
            if qpf_src == "GFS" and ck in shared.gfs_cache:
                dest = os.path.join(store, "gfs_data")
                copy_tifs_from_shared(shared.gfs_cache[ck], dest)
            elif qpf_src == "AROME":
                try:
                    domain = get_arome_domain_for_region(region)
                    akey = (ck, domain)
                    if akey in shared.arome_cache:
                        dest = os.path.join(store, "arome_data")
                        copy_tifs_from_shared(shared.arome_cache[akey], dest)
                except ValueError:
                    print(f"    AROME: no domain for {region} — skip")


def _ensure_region_dirs(
    regions_to_run: Sequence[str],
    region_configs: Mapping[str, dict],
    region_qpe_sources: Mapping[str, str],
) -> None:
    print("***_________Creating per-region directories_________***")
    for region in regions_to_run:
        cfg = region_configs[region]
        is_streamsat = region_qpe_sources.get(region, "").upper() == "STREAM_SAT"
        if not is_streamsat:
            mkdir_p(cfg["region_states_path"])
        mkdir_p(cfg["region_data_path"])
        mkdir_p(cfg["region_qpf_store"])
        print(f"    {region}: states={cfg['region_states_path']} "
              f"({'STREAM_SAT=per-member' if is_streamsat else 'IMERG/default'}), "
              f"data={cfg['region_data_path']}")


def _cleanup(
    batch: JobBatch,
    regions_to_run: Sequence[str],
    region_configs: Mapping[str, dict],
    qpf_store_path: str,
) -> None:
    if batch.staged_precip_folders:
        print("***_________Cleaning staged precipEF5 folders_________***")
        removed = cleanup_staged_precip_folders(batch.staged_precip_folders)
        print(f"    Removed {removed} staged precip files")

    print("***_________Cleaning regional qpf_store_________***")
    for region in regions_to_run:
        store = region_configs[region]["region_qpf_store"]
        for sub in ["gfs_data", "arome_data", "wrf_data", "stormlab_data"]:
            sp = os.path.join(store, sub)
            if os.path.islink(sp) or os.path.isdir(sp):
                try:
                    if os.path.islink(sp):
                        os.unlink(sp)
                    else:
                        shutil.rmtree(sp)
                    print(f"    Removed {sp}")
                except Exception as exc:
                    print(f"    Warning: could not remove {sp}: {exc}")

    for cache_name in ["_shared", "_shared_arome"]:
        cache_root = os.path.join(qpf_store_path, cache_name)
        if os.path.isdir(cache_root):
            for d in os.listdir(cache_root):
                dp = os.path.join(cache_root, d)
                try:
                    shutil.rmtree(dp)
                    print(f"    Removed stale cache: {dp}")
                except Exception as exc:
                    print(f"    Warning: could not remove {dp}: {exc}")


def run_ef5_job_pipeline(
    *,
    regions_to_run: Sequence[str],
    region_configs: Dict[str, dict],
    region_qpe_sources: Mapping[str, str],
    shared: Any,
    config: Any,
    hindcast_mode: bool,
    lr_run: bool,
    qpe_gap_fill_mode: str,
    cycle_time,
    t_start: float,
    master_log: Optional[Any] = None,
    # EF5 / system context
    ef5Path: str,
    systemModel: str,
    systemName: str,
    modelStates: list,
    templatePath: str,
    basicPath: str,
    parametersPath: str,
    LR_TimeStep: str,
    precipEF5Folder: str,
    model_resolution: str,
    region_resolution_map,
    SEND_ALERTS: bool,
    alert_recipients,
    smtp_config: dict,
    qpf_store_path: str,
) -> JobBatch:
    """
    Build and run all EF5 jobs for one cycle.

    Returns the :class:`JobBatch` (useful for tests / summaries).
    """
    ctx = {
        "ef5Path": ef5Path,
        "systemModel": systemModel,
        "systemName": systemName,
        "modelStates": modelStates,
        "templatePath": templatePath,
        "basicPath": basicPath,
        "parametersPath": parametersPath,
        "LR_TimeStep": LR_TimeStep,
        "precipEF5Folder": precipEF5Folder,
        "model_resolution": model_resolution,
        "region_resolution_map": region_resolution_map,
        "SEND_ALERTS": SEND_ALERTS,
        "alert_recipients": alert_recipients,
        "smtp_config": smtp_config,
    }

    _ensure_region_dirs(regions_to_run, region_configs, region_qpe_sources)

    if lr_run:
        _stage_qpf(regions_to_run, region_configs, shared)

    batch = JobBatch()
    build_kwargs = dict(
        region_configs=region_configs,
        shared=shared,
        batch=batch,
        config=config,
        ctx=ctx,
    )

    try:
        # ── STREAM-Sat ensemble (3-phase) ──────────────────────────────
        #   A: STREAM-Sat QPE → states/stream_sat
        #   B: SCaMPR/HSAF gap → states/scampr|hsaf  (ops only)
        #   C: StormLab nested QPF (or legacy GFS)
        ss_regions = [
            r for r in regions_to_run
            if region_qpe_sources.get(r, "").upper() == "STREAM_SAT"
        ]
        if ss_regions:
            stream_sat_gap_mode = getattr(
                config, "stream_sat_gap_fill_mode", "SCAMPR"
            ).strip().upper()
            if hindcast_mode:
                stream_sat_gap_mode = "NONE"
                console.info(
                    "[bold]HINDCAST:[/] gap fill disabled — forecast from STREAM-Sat states"
                )

            ss_state_root = getattr(
                config, "stream_sat_state_folder", "EF5_conf/states/stream_sat/")
            ss_out_root = getattr(
                config, "stream_sat_output_folder", "outputs/stream_sat/")

            # Build all phases; Phase B/C control files are finalized after A
            # so gap-state seeding can make ss_end states visible to EF5.
            build_streamsat_jobs_parallel(
                ss_regions,
                hindcast_mode=hindcast_mode,
                stream_sat_gap_mode=stream_sat_gap_mode,
                stream_sat_state_root=ss_state_root,
                stream_sat_output_root=ss_out_root,
                master_log=master_log,
                phases=("A",),
                **build_kwargs,
            )

            phase_timings = []

            from tito_utils.logging_utils import debug_print, is_debug, user_print

            if batch.streamsat_jobs:
                if is_debug():
                    debug_print(
                        f"***_________Phase SS-A: STREAM-Sat EF5 "
                        f"({len(batch.streamsat_jobs)} jobs)_________***")
                else:
                    user_print(
                        f"    EF5 Phase A: STREAM-Sat "
                        f"({len(batch.streamsat_jobs)} runs) …")
                phase_timings.append(
                    _run_phase_jobs("SS-A STREAM-Sat", batch.streamsat_jobs, master_log)
                )
                user_print("    STREAM-Sat EF5 complete — states saved")

            seed_gap_states_from_streamsat(
                ss_regions, region_configs, shared, config,
                ss_state_root, stream_sat_gap_mode, hindcast_mode,
            )

            # Phase B controls after seed so ss_end states are visible
            # (skipped in hindcast — gap_mode=NONE, no jobs built)
            build_streamsat_jobs_parallel(
                ss_regions,
                hindcast_mode=hindcast_mode,
                stream_sat_gap_mode=stream_sat_gap_mode,
                stream_sat_state_root=ss_state_root,
                stream_sat_output_root=ss_out_root,
                master_log=master_log,
                phases=("B",),
                **build_kwargs,
            )

            if batch.streamsat_gap_jobs:
                if is_debug():
                    debug_print(
                        f"***_________Phase SS-B: gap-fill EF5 "
                        f"({len(batch.streamsat_gap_jobs)} jobs)_________***")
                else:
                    user_print(
                        f"    EF5 Phase B: gap-fill "
                        f"({len(batch.streamsat_gap_jobs)} runs) …")
                phase_timings.append(
                    _run_phase_jobs("SS-B gap-fill", batch.streamsat_gap_jobs, master_log)
                )
                user_print("    Gap-fill EF5 complete — states saved at cycle time")

            # Phase C after B so forecast warm-starts from states @ T
            build_streamsat_jobs_parallel(
                ss_regions,
                hindcast_mode=hindcast_mode,
                stream_sat_gap_mode=stream_sat_gap_mode,
                stream_sat_state_root=ss_state_root,
                stream_sat_output_root=ss_out_root,
                master_log=master_log,
                phases=("C",),
                **build_kwargs,
            )

            if batch.streamsat_lr_jobs:
                if is_debug():
                    debug_print(
                        f"***_________Phase SS-C: StormLab QPE EF5 "
                        f"({len(batch.streamsat_lr_jobs)} jobs)_________***")
                else:
                    user_print(
                        f"    EF5 Phase C: StormLab forecast "
                        f"({len(batch.streamsat_lr_jobs)} runs) …")
                phase_timings.append(
                    _run_phase_jobs("SS-C StormLab QPE[Forecast]", batch.streamsat_lr_jobs, master_log)
                )
                user_print("    StormLab EF5 complete")

            if (batch.streamsat_jobs or batch.streamsat_gap_jobs
                    or batch.streamsat_lr_jobs):
                newline(1)
                user_print("******** STREAM-Sat EF5 outputs ready ********")
                _log_streamsat_summary(
                    regions_to_run, shared, batch, cycle_time, t_start, master_log,
                    phase_timings=phase_timings,
                )

        # ── IMERG / SCaMPR flow (non–STREAM_SAT regions) ───────────────
        non_ss = [
            r for r in regions_to_run
            if region_qpe_sources.get(r, "").upper() != "STREAM_SAT"
        ]

        if qpe_gap_fill_mode == "IMERG_SCAMPR":
            print("***_________Phase 2a: IMERG EF5 control files_________***")
            build_jobs_parallel(build_imerg_job, non_ss, **build_kwargs)

            if batch.imerg_jobs:
                print("***_________Running IMERG EF5 simulations_________***")
                run_ef5_simulations_parallel(
                    batch.imerg_jobs, max_workers=len(batch.imerg_jobs))
                print("    IMERG EF5 runs complete — states saved at T−4h")
            else:
                print("    No IMERG EF5 jobs prepared.")

            if lr_run:
                print("***_________Phase 2b: LR EF5 control files_________***")
                build_jobs_parallel(build_lr_jobs, non_ss, **build_kwargs)
                if batch.lr_jobs:
                    print("***_________Running LR EF5 simulations_________***")
                    run_ef5_simulations_parallel(
                        batch.lr_jobs, max_workers=len(batch.lr_jobs))
                else:
                    print("    No LR EF5 jobs prepared.")

            if batch.imerg_jobs or batch.lr_jobs:
                newline(2)
                print("******** EF5 Outputs are ready!!! ********")
            elif not (batch.streamsat_jobs or batch.streamsat_lr_jobs):
                print("No EF5 jobs were prepared.")

        else:
            print("***_________Preparing EF5 control files_________***")
            build_jobs_parallel(build_imerg_job, non_ss, **build_kwargs)
            if batch.imerg_jobs:
                print("***_________Running EF5 simulations_________***")
                run_ef5_simulations_parallel(
                    batch.imerg_jobs, max_workers=len(batch.imerg_jobs))
                newline(2)
                print("******** EF5 Outputs are ready!!! ********")
            elif not (batch.streamsat_jobs or batch.streamsat_lr_jobs):
                print("No EF5 jobs were prepared.")

    finally:
        _cleanup(batch, regions_to_run, region_configs, qpf_store_path)

    return batch


def _log_streamsat_summary(
    regions_to_run, shared, batch, cycle_time, t_start, master_log,
    phase_timings=None,
) -> None:
    summary = [
        "=" * 60,
        "  SIMULATION SUMMARY",
        "=" * 60,
        f"  Cycle:  {cycle_time.strftime('%Y-%m-%d %H:%M')} UTC",
    ]
    for region in regions_to_run:
        ss_info = shared.streamsat_info.get(region, {})
        if ss_info and "error" not in ss_info:
            summary.append(f"  {region}:")
            summary.append(f"    STREAM-Sat members: {ss_info.get('ensemble_size', '?')}")
            summary.append(f"    GeoTIFFs root:     {ss_info.get('tif_root', '?')}")
        sl_info = getattr(shared, "stormlab_info", {}).get(region, {})
        if sl_info and "error" not in sl_info:
            if not (ss_info and "error" not in ss_info):
                summary.append(f"  {region}:")
            summary.append(f"    StormLab members:  {sl_info.get('ensemble_size', '?')}")
            summary.append(f"    StormLab TIFs:     {sl_info.get('tif_root', '?')}")
    summary.append(
        f"  Phase A jobs: {len(batch.streamsat_jobs)}  (STREAM-Sat QPE, save states)")
    summary.append(
        f"  Phase B jobs: {len(batch.streamsat_gap_jobs)}  (SCaMPR/HSAF QPE gap, save states)")
    summary.append(
        f"  Phase C jobs: {len(batch.streamsat_lr_jobs)}  (StormLab QPF Forecast)")
    summary.append(f"  IMERG EF5 jobs:  {len(batch.imerg_jobs)}")
    summary.append(f"  LR EF5 jobs:     {len(batch.lr_jobs)}")

    # Prep timings (STREAM-Sat / StormLab) — unique by domain
    prep_lines = []
    seen_ss, seen_sl = set(), set()
    for region in regions_to_run:
        ss = shared.streamsat_info.get(region, {}) or {}
        if "error" not in ss and ss.get("elapsed_s") is not None:
            dom = ss.get("domain") or "stream_sat"
            if dom not in seen_ss:
                seen_ss.add(dom)
                prep_lines.append(
                    f"    STREAM-Sat [{dom}]: {_fmt_secs(ss.get('elapsed_s', 0))}  "
                    f"(pipeline={_fmt_secs(ss.get('pipeline_s', 0))}, "
                    f"convert={_fmt_secs(ss.get('convert_s', 0))})"
                )
        sl = (getattr(shared, "stormlab_info", {}) or {}).get(region, {}) or {}
        if "error" not in sl and (
            sl.get("elapsed_s") is not None or sl.get("pipeline_s") is not None
        ):
            dom = sl.get("domain") or "stormlab"
            if dom not in seen_sl:
                seen_sl.add(dom)
                prep_lines.append(
                    f"    StormLab [{dom}]: {_fmt_secs(sl.get('elapsed_s', 0))}  "
                    f"(pipeline={_fmt_secs(sl.get('pipeline_s', 0))}, "
                    f"convert={_fmt_secs(sl.get('convert_s', 0))})"
                )

    summary.append("  Phase wall-clock:")
    if prep_lines:
        summary.append("    --- precip prep ---")
        summary.extend(prep_lines)
    if phase_timings:
        summary.append("    --- EF5 ---")
        for pt in phase_timings:
            summary.append(
                f"    {pt['phase']}: {_fmt_secs(pt['wall_s'])}  "
                f"({pt['n']} jobs)"
            )
            for t in sorted(pt.get("jobs") or [], key=lambda x: x.get("label", "")):
                st = "OK" if t.get("ok", True) else "FAIL"
                summary.append(
                    f"      - {t.get('label','?')}: {_fmt_secs(t.get('seconds', 0))} [{st}]"
                )
    elapsed = time.time() - t_start
    summary.append(f"  Total time:      {_fmt_secs(elapsed)} ({elapsed/60:.1f} min)")
    summary.append("=" * 60)
    for line in summary:
        print(line)
        if master_log:
            master_log.info(line)

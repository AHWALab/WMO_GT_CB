"""
EF5 control-file / job builders for IMERG, LR (SCaMPR+QPF), and STREAM-Sat.

STREAM_SAT three-phase design
-----------------------------
  Phase A  STREAM-Sat QPE  → states/stream_sat/ensS*/
  Phase B  SCaMPR/HSAF gap → states/scampr|hsaf/ensS*/  (save states)
  Phase C  StormLab QPF    → nested SS×SL ensemble jobs (no state save)
"""

from __future__ import annotations

import glob
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Any, Dict, List, Optional, Set

from tito_utils.file_utils.file_handling import mkdir_p
from tito_utils.ef5.ef5_routines import prepare_ef5
from tito_utils.logging_utils import setup_run_log, console
from tito_utils.ef5.jobs.helpers import (
    with_sep,
    resolve_cold_start_window,
    parse_streamsat_tif_window,
    resolve_region_resolution,
)


class JobBatch:
    """Mutable containers filled by the builders."""

    def __init__(self):
        self.lock = threading.Lock()
        self.staged_precip_folders: Set[str] = set()
        self.imerg_jobs: List[dict] = []
        self.lr_jobs: List[dict] = []
        self.streamsat_jobs: List[dict] = []       # Phase A
        self.streamsat_gap_jobs: List[dict] = []   # Phase B gap fill (save states)
        self.streamsat_lr_jobs: List[dict] = []    # Phase C forecast (StormLab / legacy)


def _copy_state_tifs(src_dir: str, dst_dir: str) -> int:
    """Copy EF5 state GeoTIFFs from src → dst (for load/save path split)."""
    mkdir_p(dst_dir)
    n = 0
    if not os.path.isdir(src_dir):
        return 0
    for path in glob.glob(os.path.join(src_dir, "*.tif")):
        try:
            shutil.copy2(path, os.path.join(dst_dir, os.path.basename(path)))
            n += 1
        except OSError:
            pass
    return n


def _normalize_gap_mode(raw: str) -> str:
    """Map legacy gap-mode names onto SCAMPR | HSAF | NONE."""
    m = (raw or "SCAMPR").strip().upper()
    if m in ("SCAMPR_QPE", "SCAMPR_ONLY", "SCAMPR"):
        return "SCAMPR"
    if m in ("HSAF_QPE", "HSAF_ONLY", "HSAF"):
        return "HSAF"
    if m in ("NONE", "OFF", "NO"):
        return "NONE"
    return m


def _job_dict(region, ef5_path, run_path, ctrl_file, output_ts, member=None) -> dict:
    d = {
        "region": region,
        "ef5Path": ef5_path,
        "tmpOutput": run_path + "/",
        "controlFile": ctrl_file,
        "output_timestamp_str": output_ts,
    }
    if member is not None:
        d["member"] = member
    return d


def build_imerg_job(
    region: str,
    *,
    region_configs: Dict[str, dict],
    shared: Any,
    batch: JobBatch,
    config: Any,
    ctx: Dict[str, Any],
) -> None:
    """IMERG-only EF5 job (→ T−4h, saves state)."""
    cfg = region_configs[region]
    rkey = cfg["region_key"]
    r_imerg_end = cfg["r_imerg_end"]
    ck = cfg["cycle_time_key"]

    imerg_qpe_folder = shared.imerg_folders.get(ck, "")
    if not imerg_qpe_folder:
        print(f"    !!! {region}: no IMERG shared folder — skipping")
        return

    cold_begin, cold_warm_end = resolve_cold_start_window(config, r_imerg_end)

    tmp_out = os.path.join(cfg["region_data_path"], f"tmp_output_{ctx['systemModel']}_imerg")
    staging = os.path.join(ctx["precipEF5Folder"], rkey, "imerg_none")
    mkdir_p(staging)
    mkdir_p(tmp_out)

    try:
        job_log = setup_run_log(cfg["region_data_path"], f"ef5_imerg_{rkey}")
        job_log.info("IMERG EF5 prep — region=%s", region)
        eff_start, ctrl_file, run_path = prepare_ef5(
            staging,
            imerg_qpe_folder,
            with_sep(cfg["region_states_path"]),
            ctx["modelStates"],
            r_imerg_end - timedelta(minutes=30),
            r_imerg_end - timedelta(days=7),
            cfg["region_current_time"],
            ctx["systemName"],
            ctx["SEND_ALERTS"], ctx["alert_recipients"], ctx["smtp_config"],
            with_sep(tmp_out),
            with_sep(cfg["region_data_path"]),
            region, ctx["systemModel"],
            ctx["templatePath"], cfg["region_template"],
            r_imerg_end,
            cold_warm_end,
            r_imerg_end,
            r_imerg_end,
            ctx["LR_TimeStep"],
            False,
            region, resolve_region_resolution(
                region, ctx["model_resolution"], ctx["region_resolution_map"]),
            ctx["basicPath"], ctx["parametersPath"],
            "IMERG", "none",
            stage_precip=True,
            output_timestamp_str=cfg["output_timestamp_str"],
            qpf_store_forcing_path=cfg["region_qpf_store"],
            save_states=True,
            cold_start_begin_time=cold_begin,
            cold_start_warm_end_time=cold_warm_end,
            verbose=True,
            run_log=job_log,
        )
        print(f"    {region} [IMERG]: {eff_start.strftime('%Y%m%d_%H%M')} → "
              f"{r_imerg_end.strftime('%Y%m%d_%H%M')}, ctrl={ctrl_file}")
        with batch.lock:
            batch.staged_precip_folders.add(staging)
            batch.imerg_jobs.append(_job_dict(
                region, ctx["ef5Path"], run_path, ctrl_file,
                cfg["output_timestamp_str"]))
    except Exception as exc:
        print(f"    !!! {region} IMERG EF5 prep failed: {exc}")


def build_lr_jobs(
    region: str,
    *,
    region_configs: Dict[str, dict],
    shared: Any,
    batch: JobBatch,
    config: Any,
    ctx: Dict[str, Any],
) -> None:
    """SCaMPR QPE → GFS/AROME QPF long-range jobs (no state save)."""
    cfg = region_configs[region]
    rkey = cfg["region_key"]
    r_imerg_end = cfg["r_imerg_end"]
    r_scampr_end = cfg["r_scampr_end"]
    r_lr_end = cfg["r_end_lr"]

    scampr_precip = shared.scampr_folder
    if not scampr_precip:
        print(f"    !!! {region}: no SCaMPR folder — skipping LR runs")
        return

    for qpf_src in cfg["qpf_sources"]:
        actual_qpf = qpf_src

        if qpf_src == "WRF":
            wrf_path = getattr(config, "WRF_archive_path", "")
            if wrf_path:
                try:
                    from tito_utils.qpf_utils.wrf_manager import WRF_searcher as _wrf
                    wrf_ok = _wrf(
                        wrf_path, cfg["region_qpf_store"],
                        r_scampr_end, r_lr_end,
                        ctx["LR_TimeStep"],
                        getattr(config, "WRF_var_name", "PREC_ACC_C"),
                        getattr(config, "WRF_filename_template",
                                "PREC_d01_YYYY-MM-DD_HH_mm_SS.nc"),
                    )
                    actual_qpf = "WRF" if wrf_ok else "GFS"
                    if not wrf_ok:
                        print(f"    {region}: WRF unavailable → GFS fallback")
                except Exception:
                    actual_qpf = "GFS"
            else:
                actual_qpf = "GFS"

        tmp_out = os.path.join(
            cfg["region_data_path"],
            f"tmp_output_{ctx['systemModel']}_scampr_{actual_qpf.lower()}")
        staging = os.path.join(
            ctx["precipEF5Folder"], rkey, f"scampr_{actual_qpf.lower()}")
        mkdir_p(staging)
        mkdir_p(tmp_out)

        try:
            lr_log = setup_run_log(
                cfg["region_data_path"], f"ef5_lr_{rkey}_{actual_qpf.lower()}")
            lr_log.info("LR EF5 prep — region=%s qpf=%s", region, actual_qpf)
            eff_start, ctrl_file, run_path = prepare_ef5(
                staging,
                scampr_precip,
                with_sep(cfg["region_states_path"]),
                ctx["modelStates"],
                r_imerg_end,
                r_imerg_end,
                cfg["region_current_time"],
                ctx["systemName"],
                ctx["SEND_ALERTS"], ctx["alert_recipients"], ctx["smtp_config"],
                with_sep(tmp_out),
                with_sep(cfg["region_data_path"]),
                region, ctx["systemModel"],
                ctx["templatePath"], cfg["region_template"],
                r_scampr_end,
                r_scampr_end,
                r_lr_end,
                r_lr_end,
                ctx["LR_TimeStep"],
                True,
                region, resolve_region_resolution(
                    region, ctx["model_resolution"], ctx["region_resolution_map"]),
                ctx["basicPath"], ctx["parametersPath"],
                "SCAMPR", actual_qpf,
                stage_precip=True,
                output_timestamp_str=cfg["output_timestamp_str"],
                qpf_store_forcing_path=cfg["region_qpf_store"],
                save_states=False,
                verbose=True,
                run_log=lr_log,
            )
            print(f"    {region} [SCaMPR+{actual_qpf}]: "
                  f"state@T−4h → QPE→T → QPF→"
                  f"{r_lr_end.strftime('%Y%m%d_%H%M')}, ctrl={ctrl_file}")
            with batch.lock:
                batch.staged_precip_folders.add(staging)
                batch.lr_jobs.append(_job_dict(
                    region, ctx["ef5Path"], run_path, ctrl_file,
                    cfg["output_timestamp_str"]))
        except Exception as exc:
            print(f"    !!! {region} LR ({actual_qpf}) EF5 prep failed: {exc}")


def build_streamsat_ensemble_jobs(
    region: str,
    *,
    region_configs: Dict[str, dict],
    shared: Any,
    batch: JobBatch,
    config: Any,
    ctx: Dict[str, Any],
    hindcast_mode: bool,
    stream_sat_gap_mode: str,
    stream_sat_state_root: str,
    stream_sat_output_root: str,
    master_log: Optional[Any] = None,
    phases: Optional[tuple] = None,
) -> None:
    """Build Phase A/B/C EF5 jobs for one STREAM-Sat region.

    Phase A — STREAM-Sat QPE, save states under ``states/stream_sat/``.
    Phase B — SCaMPR/HSAF gap fill to cycle time, save states under
              ``states/scampr|hsaf/`` (operational only).
    Phase C — StormLab nested QPF (SS member × SL member), or legacy GFS.

    *phases* defaults to ``("A", "B", "C")``.  Pipeline runs A first, seeds
    gap states, then builds B+C so EF5 finds ss_end states at control write.
    """
    want = set(p.upper() for p in (phases or ("A", "B", "C")))
    cfg = region_configs[region]
    rkey = cfg["region_key"]
    ct = cfg["region_current_time"]

    ss_info = shared.streamsat_info.get(region, {})
    if "error" in ss_info:
        print(f"    !!! {region}: STREAM-Sat prep failed: {ss_info['error']}")
        return
    if not ss_info:
        print(f"    !!! {region}: no STREAM-Sat info — skipping")
        return

    ens_size = ss_info.get("ensemble_size", 10)
    tif_root = ss_info.get("tif_root", "")
    if not tif_root:
        print(f"    !!! {region}: no tif_root in STREAM-Sat info")
        return

    ens_p1_dir = os.path.join(tif_root, "ensP1")
    if not os.path.isdir(ens_p1_dir):
        print(f"    !!! {region}: STREAM-Sat ensP1 dir not found: {ens_p1_dir}")
        return

    tif_pattern = getattr(config, "stream_sat_tif_naming", "streamsat")
    parsed = parse_streamsat_tif_window(ens_p1_dir, tif_pattern)
    if parsed is None:
        print(f"    !!! {region}: no STREAM-Sat GeoTIFFs / timestamps in {ens_p1_dir}")
        return
    ss_start, ss_end, all_ts = parsed

    if hindcast_mode:
        valid = [t for t in all_ts if t <= ct]
        if valid:
            ss_end = max(valid)
            win_h = int(getattr(config, "stream_sat_window_hours", 48))
            ss_start = min(t for t in valid if t >= ss_end - timedelta(hours=win_h))
            if "A" in want:
                print(f"    {region} [STREAM_SAT]: window {ss_start.strftime('%Y%m%d_%H%M')} → "
                      f"{ss_end.strftime('%Y%m%d_%H%M')} (hindcast clamp to T="
                      f"{ct.strftime('%Y%m%d_%H%M')}), {ens_size} members")
        else:
            print(f"    !!! {region}: no STREAM-Sat TIFs <= hindcast T "
                  f"{ct.strftime('%Y%m%d_%H%M')} in {ens_p1_dir}")
            return
    elif "A" in want:
        print(f"    {region} [STREAM_SAT]: window {ss_start.strftime('%Y%m%d_%H%M')} → "
              f"{ss_end.strftime('%Y%m%d_%H%M')} (driven by actual data), {ens_size} members")

    gap_mode = _normalize_gap_mode(stream_sat_gap_mode)
    if hindcast_mode:
        gap_mode = "NONE"

    scampr_folder = getattr(shared, "scampr_folder", None)
    hsaf_folder = None
    other = getattr(shared, "_other_qpe", None) or {}
    if region in other:
        hsaf_folder = other[region]

    gap_qpe = gap_mode  # SCAMPR | HSAF | NONE
    gap_precip = None
    if gap_qpe == "SCAMPR":
        gap_precip = scampr_folder
    elif gap_qpe == "HSAF":
        gap_precip = hsaf_folder
    do_gap_fill = gap_qpe in ("SCAMPR", "HSAF") and bool(gap_precip) and ss_end < ct

    qpf_sources = [str(s).strip().upper() for s in cfg.get("qpf_sources", [])]
    use_stormlab = "STORMLAB" in qpf_sources
    legacy_qpf = [s for s in qpf_sources if s in ("GFS", "AROME", "WRF")]
    if hindcast_mode:
        legacy_qpf = [s for s in legacy_qpf if s == "GFS"]

    gap_state_root = getattr(
        config,
        "scampr_state_folder" if gap_qpe != "HSAF" else "hsaf_state_folder",
        "EF5_conf/states/scampr/" if gap_qpe != "HSAF" else "EF5_conf/states/hsaf/",
    )
    gap_output_root = getattr(
        config,
        "scampr_output_folder" if gap_qpe != "HSAF" else "hsaf_output_folder",
        "outputs/scampr/" if gap_qpe != "HSAF" else "outputs/hsaf/",
    )
    stormlab_output_root = getattr(
        config, "stormlab_output_folder", "outputs/stormlab/")

    sl_info = getattr(shared, "stormlab_info", {}) or {}
    sl_region = sl_info.get(region, {})
    sl_ens = int(sl_region.get("ensemble_size", 0) or 0)
    sl_tif_root = sl_region.get("tif_root", "")

    res = resolve_region_resolution(
        region, ctx["model_resolution"], ctx["region_resolution_map"])

    for member_idx in range(1, ens_size + 1):
        member_precip = os.path.join(tif_root, f"ensP{member_idx}", "")
        ss_states = os.path.join(
            stream_sat_state_root, f"ensS{member_idx}", rkey, "")
        ss_output = os.path.join(
            stream_sat_output_root, f"ensOut{member_idx}", rkey, "")
        member_tmp = os.path.join(
            ss_output, f"tmp_output_{ctx['systemModel']}_streamsat")

        mkdir_p(member_precip)
        mkdir_p(ss_states)
        mkdir_p(ss_output)
        mkdir_p(member_tmp)

        is_first = member_idx == 1
        run_log = setup_run_log(
            ss_output, f"ef5_ens{member_idx:02d}",
        ) if not is_first else None

        # Dry-run tail after each phase: TIME_END += dry_h; TIME_STATE stays
        # at the physical phase end (ss_end / T / not saved for StormLab).
        dry_h = int(cfg.get("dry_run_hours", 0) or 0)
        if dry_h < 0:
            dry_h = 0

        # ── Phase A: STREAM-Sat ──────────────────────────────────────
        if "A" in want:
            staging_a = os.path.join(
                ctx["precipEF5Folder"], rkey, f"streamsat_ens{member_idx:02d}")
            mkdir_p(staging_a)
            ss_dry_end = ss_end + timedelta(hours=dry_h)
            try:
                # TIME_STATE=ss_end; TIME_END=ss_dry_end (dry tail, no precip)
                eff_start, ctrl_file, run_path = prepare_ef5(
                    staging_a,
                    member_precip,
                    with_sep(ss_states),
                    ctx["modelStates"],
                    ss_end - timedelta(minutes=30),
                    ss_end - timedelta(hours=48),
                    ct,
                    ctx["systemName"],
                    ctx["SEND_ALERTS"], ctx["alert_recipients"], ctx["smtp_config"],
                    with_sep(member_tmp),
                    with_sep(ss_output),
                    region, ctx["systemModel"],
                    ctx["templatePath"], cfg["region_template"],
                    ss_end, ss_end, ss_end, ss_dry_end,
                    ctx["LR_TimeStep"],
                    False,
                    region, res,
                    ctx["basicPath"], ctx["parametersPath"],
                    "STREAM_SAT", "none",
                    stage_precip=True,
                    output_timestamp_str=cfg["output_timestamp_str"],
                    qpf_store_forcing_path=cfg["region_qpf_store"],
                    save_states=True,
                    cold_start_begin_time=ss_start,
                    cold_start_warm_end_time=ss_end,
                    verbose=is_first,
                    run_log=run_log,
                )
                if master_log:
                    master_log.info(
                        "    %s [SS ens%02d]: %s → %s dry→%s, ctrl=%s",
                        region, member_idx,
                        eff_start.strftime("%Y%m%d_%H%M"),
                        ss_end.strftime("%Y%m%d_%H%M"),
                        ss_dry_end.strftime("%Y%m%d_%H%M"), ctrl_file)
                if is_first:
                    dry_note = (
                        f" +{dry_h}h dry→{ss_dry_end.strftime('%Y%m%d_%H%M')}"
                        if dry_h else ""
                    )
                    print(f"    {region} [SS ens{member_idx:02d}]: "
                          f"{eff_start.strftime('%Y%m%d_%H%M')} → "
                          f"{ss_end.strftime('%Y%m%d_%H%M')} "
                          f"(state@{ss_end.strftime('%Y%m%d_%H%M')}){dry_note}")
                with batch.lock:
                    batch.staged_precip_folders.add(staging_a)
                    batch.streamsat_jobs.append(_job_dict(
                        region, ctx["ef5Path"], run_path, ctrl_file,
                        cfg["output_timestamp_str"], member=member_idx))
            except Exception as exc:
                print(f"    !!! {region} [SS ens{member_idx:02d}] EF5 prep failed: {exc}")
                continue

        # Forecast warm-start: gap states (ops) or STREAM-Sat states (hindcast)
        forecast_states = ss_states
        forecast_qpe = "STREAM_SAT"
        forecast_qpe_folder = member_precip
        if do_gap_fill:
            forecast_states = os.path.join(
                gap_state_root, f"ensS{member_idx}", rkey, "")
            forecast_qpe = gap_qpe
            forecast_qpe_folder = gap_precip

        # ── Phase B: gap fill (ops only), save states separately ─────
        if "B" in want and do_gap_fill:
            gap_states = os.path.join(
                gap_state_root, f"ensS{member_idx}", rkey, "")
            gap_output = os.path.join(
                gap_output_root, f"ensOut{member_idx}", rkey, "")
            gap_tmp = os.path.join(
                gap_output, f"tmp_output_{ctx['systemModel']}_gap_{gap_qpe.lower()}")
            mkdir_p(gap_states)
            mkdir_p(gap_output)
            mkdir_p(gap_tmp)

            staging_b = os.path.join(
                ctx["precipEF5Folder"], rkey,
                f"streamsat_ens{member_idx:02d}_gap_{gap_qpe.lower()}")
            mkdir_p(staging_b)
            gap_dry_end = ct + timedelta(hours=dry_h)
            try:
                # Load ss_end states; TIME_STATE=T; TIME_END=T+dry (no precip)
                _, ctrl_b, run_b = prepare_ef5(
                    staging_b,
                    gap_precip,
                    with_sep(gap_states),
                    ctx["modelStates"],
                    ss_end, ss_end - timedelta(hours=48), ct,
                    ctx["systemName"],
                    ctx["SEND_ALERTS"], ctx["alert_recipients"], ctx["smtp_config"],
                    with_sep(gap_tmp),
                    with_sep(gap_output),
                    region, ctx["systemModel"],
                    ctx["templatePath"], cfg["region_template"],
                    ct, ct, ct, gap_dry_end,
                    ctx["LR_TimeStep"],
                    False,
                    region, res,
                    ctx["basicPath"], ctx["parametersPath"],
                    gap_qpe, "none",
                    stage_precip=True,
                    output_timestamp_str=cfg["output_timestamp_str"],
                    qpf_store_forcing_path=cfg["region_qpf_store"],
                    save_states=True,
                    verbose=is_first,
                    run_log=run_log,
                )
                dry_note = (
                    f" +{dry_h}h dry→{gap_dry_end.strftime('%Y%m%d_%H%M')}"
                    if dry_h else ""
                )
                print(f"    {region} [gap ens{member_idx:02d} {gap_qpe}]: "
                      f"state@SS_end → QPE→T={ct.strftime('%Y%m%d_%H%M')} "
                      f"(state@{ct.strftime('%Y%m%d_%H%M')}){dry_note}")
                if master_log:
                    master_log.info(
                        "    %s [gap ens%02d %s]: %s → %s dry→%s, ctrl=%s",
                        region, member_idx, gap_qpe,
                        ss_end.strftime("%Y%m%d_%H%M"),
                        ct.strftime("%Y%m%d_%H%M"),
                        gap_dry_end.strftime("%Y%m%d_%H%M"), ctrl_b)
                with batch.lock:
                    batch.staged_precip_folders.add(staging_b)
                    batch.streamsat_gap_jobs.append(_job_dict(
                        region, ctx["ef5Path"], run_b, ctrl_b,
                        cfg["output_timestamp_str"], member=member_idx))
            except Exception as exc:
                print(f"    !!! {region} [gap ens{member_idx:02d} {gap_qpe}] "
                      f"EF5 prep failed: {exc}")

        if "C" not in want:
            continue

        # ── Phase C: StormLab as normal QPE (no long-range) ───────────
        # Load states from SCaMPR (or STREAM-Sat); PRECIP=STORMLAB hourly;
        # Simulation_QPE only; no TIME_STATE save.
        if use_stormlab and sl_ens > 0 and sl_tif_root:
            fc_start = ct if do_gap_fill else ss_end
            fc_end = cfg["r_end_lr"]
            # Dry-run tail: extend TIME_END only; no precip, no TIME_STATE
            dry_end = cfg.get("r_end_time", fc_end) or fc_end
            dry_h = int(cfg.get("dry_run_hours", 0) or 0)
            for sl_idx in range(1, sl_ens + 1):
                sl_member_dir = os.path.join(sl_tif_root, f"ensQ{sl_idx}", "")
                if not os.path.isdir(sl_member_dir):
                    continue

                sl_out = os.path.join(
                    stormlab_output_root,
                    f"ensOut{member_idx}_sl{sl_idx}", rkey, "")
                sl_tmp = os.path.join(
                    sl_out, f"tmp_output_{ctx['systemModel']}_stormlab")
                mkdir_p(sl_out)
                mkdir_p(sl_tmp)
                staging_c = os.path.join(
                    ctx["precipEF5Folder"], rkey,
                    f"stormlab_ens{member_idx:02d}_sl{sl_idx:02d}")
                mkdir_p(staging_c)
                try:
                    # QPE-only: stage StormLab TIFs into precipEF5 (EF5 sibling
                    # Docker only sees project-relative paths under /data).
                    # TIME_END=dry_end; TIME_STATE suppressed (save_states=False).
                    _, ctrl_c, run_c = prepare_ef5(
                        staging_c,
                        sl_member_dir,
                        with_sep(forecast_states),
                        ctx["modelStates"],
                        fc_start, fc_start - timedelta(hours=48), ct,
                        ctx["systemName"],
                        ctx["SEND_ALERTS"], ctx["alert_recipients"], ctx["smtp_config"],
                        with_sep(sl_tmp),
                        with_sep(sl_out),
                        region, ctx["systemModel"],
                        ctx["templatePath"], cfg["region_template"],
                        dry_end, fc_start, dry_end, dry_end,
                        ctx["LR_TimeStep"],
                        False,  # no long-range — Simulation_QPE only
                        region, res,
                        ctx["basicPath"], ctx["parametersPath"],
                        "STORMLAB", "none",
                        stage_precip=True,
                        output_timestamp_str=cfg["output_timestamp_str"],
                        qpf_store_forcing_path=cfg["region_qpf_store"],
                        save_states=False,
                        verbose=is_first and sl_idx == 1,
                        run_log=run_log,
                    )
                    if is_first and sl_idx == 1:
                        dry_note = (
                            f" +{dry_h}h dry→{dry_end.strftime('%Y%m%d_%H%M')}"
                            if dry_h > 0 and dry_end != fc_end else ""
                        )
                        print(f"    {region} [SS ens{member_idx:02d} × SL "
                              f"{sl_ens}]: state@T → STORMLAB QPE→"
                              f"{fc_end.strftime('%Y%m%d_%H%M')}{dry_note} "
                              f"(no LR, no state save)")
                    if master_log:
                        master_log.info(
                            "    %s [SS ens%02d SL%02d]: STORMLAB QPE→%s "
                            "dry_end=%s (no state), ctrl=%s",
                            region, member_idx, sl_idx,
                            fc_end.strftime("%Y%m%d_%H%M"),
                            dry_end.strftime("%Y%m%d_%H%M"), ctrl_c)
                    job = _job_dict(
                        region, ctx["ef5Path"], run_c, ctrl_c,
                        cfg["output_timestamp_str"], member=member_idx)
                    job["stormlab_member"] = sl_idx
                    with batch.lock:
                        batch.staged_precip_folders.add(staging_c)
                        batch.streamsat_lr_jobs.append(job)
                except Exception as exc:
                    print(f"    !!! {region} [SS ens{member_idx:02d} SL{sl_idx:02d}] "
                          f"EF5 prep failed: {exc}")

        elif legacy_qpf:
            fc_start = ct if do_gap_fill else ss_end
            dry_end = cfg.get("r_end_time", cfg["r_end_lr"]) or cfg["r_end_lr"]
            for qpf_src in legacy_qpf:
                actual_qpf = "GFS" if qpf_src == "WRF" else qpf_src
                staging_h = os.path.join(
                    ctx["precipEF5Folder"], rkey,
                    f"streamsat_ens{member_idx:02d}_qpf_{actual_qpf.lower()}")
                mkdir_p(staging_h)
                if do_gap_fill and not hindcast_mode:
                    out_leg = os.path.join(
                        gap_output_root, f"ensOut{member_idx}", rkey, "")
                else:
                    out_leg = ss_output
                member_tmp_h = os.path.join(
                    out_leg,
                    f"tmp_output_{ctx['systemModel']}_qpf_{actual_qpf.lower()}")
                mkdir_p(out_leg)
                mkdir_p(member_tmp_h)
                try:
                    _, ctrl_h, run_h = prepare_ef5(
                        staging_h,
                        forecast_qpe_folder,
                        with_sep(forecast_states),
                        ctx["modelStates"],
                        fc_start, fc_start - timedelta(hours=48), ct,
                        ctx["systemName"],
                        ctx["SEND_ALERTS"], ctx["alert_recipients"], ctx["smtp_config"],
                        with_sep(member_tmp_h),
                        with_sep(out_leg),
                        region, ctx["systemModel"],
                        ctx["templatePath"], cfg["region_template"],
                        fc_start, fc_start, dry_end, dry_end,
                        ctx["LR_TimeStep"],
                        True,
                        region, res,
                        ctx["basicPath"], ctx["parametersPath"],
                        forecast_qpe, actual_qpf,
                        stage_precip=False,
                        output_timestamp_str=cfg["output_timestamp_str"],
                        qpf_store_forcing_path=cfg["region_qpf_store"],
                        save_states=False,
                        verbose=is_first,
                        run_log=run_log,
                    )
                    print(f"    {region} [SS ens{member_idx:02d} QPF+{actual_qpf}]: "
                          f"→ {cfg['r_end_lr'].strftime('%Y%m%d_%H%M')} "
                          f"dry→{dry_end.strftime('%Y%m%d_%H%M')}")
                    with batch.lock:
                        batch.staged_precip_folders.add(staging_h)
                        batch.streamsat_lr_jobs.append(_job_dict(
                            region, ctx["ef5Path"], run_h, ctrl_h,
                            cfg["output_timestamp_str"], member=member_idx))
                except Exception as exc:
                    print(f"    !!! {region} [SS ens{member_idx:02d}] QPF ({actual_qpf}) "
                          f"EF5 prep failed: {exc}")


def seed_gap_states_from_streamsat(
    regions: List[str],
    region_configs: Dict[str, dict],
    shared: Any,
    config: Any,
    stream_sat_state_root: str,
    stream_sat_gap_mode: str,
    hindcast_mode: bool,
) -> None:
    """After Phase A EF5 completes, re-seed gap state folders from STREAM-Sat.

    Phase A writes fresh states at ss_end into stream_sat/; Phase B must load
    those.  Builders copy states at job-prep time (before Phase A runs), so
    this post-A refresh is required.
    """
    if hindcast_mode:
        return
    gap_mode = _normalize_gap_mode(stream_sat_gap_mode)
    if gap_mode not in ("SCAMPR", "HSAF"):
        return
    gap_state_root = getattr(
        config,
        "scampr_state_folder" if gap_mode != "HSAF" else "hsaf_state_folder",
        "EF5_conf/states/scampr/" if gap_mode != "HSAF" else "EF5_conf/states/hsaf/",
    )
    print("***_________Seeding gap-fill states from STREAM-Sat_________***")
    for region in regions:
        cfg = region_configs[region]
        rkey = cfg["region_key"]
        ss_info = shared.streamsat_info.get(region, {})
        ens_size = int(ss_info.get("ensemble_size", 0) or 0)
        for m in range(1, ens_size + 1):
            src = os.path.join(stream_sat_state_root, f"ensS{m}", rkey)
            dst = os.path.join(gap_state_root, f"ensS{m}", rkey)
            n = _copy_state_tifs(src, dst)
            if m == 1:
                print(f"    {region}: ensS{m} → {dst} ({n} files)")


def build_streamsat_jobs_parallel(
    regions: List[str],
    **kwargs,
) -> None:
    if not regions:
        return
    phases = kwargs.get("phases") or ("A", "B", "C")
    try:
        from tito_utils.logging_utils import debug_print, is_debug
        if is_debug():
            debug_print(
                f"***_________Building STREAM-Sat EF5 jobs phases={phases}_________***")
    except Exception:
        pass
    with ThreadPoolExecutor(max_workers=len(regions)) as ex:
        futures = {
            ex.submit(build_streamsat_ensemble_jobs, r, **kwargs): r
            for r in regions
        }
        for future in as_completed(futures):
            r = futures[future]
            try:
                future.result()
            except Exception as exc:
                print(f"    !!! {r} STREAM-Sat EF5 prep raised: {exc}")


def build_jobs_parallel(builder_fn, regions: List[str], **kwargs) -> None:
    with ThreadPoolExecutor(max_workers=len(regions) or 1) as ex:
        futures = {ex.submit(builder_fn, r, **kwargs): r for r in regions}
        for future in as_completed(futures):
            r = futures[future]
            try:
                future.result()
            except Exception as exc:
                print(f"    !!! {r} EF5 prep raised: {exc}")

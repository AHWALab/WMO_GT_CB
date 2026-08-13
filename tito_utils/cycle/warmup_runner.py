"""
Warmup EF5 runner — spin-up when no states exist within 48 hours.

Decision policy lives in :mod:`tito_utils.cycle.warmup`.
This module owns precip download + EF5 execution for the warmup window.
"""

from __future__ import annotations

import os
import shutil
import threading
from datetime import timedelta

from tito_utils.file_utils.file_handling import mkdir_p
from tito_utils.file_utils.cleanup import cleanup_staged_precip_folders
from tito_utils.ef5.ef5_routines import (
    prepare_ef5,
    run_ef5_simulations_parallel,
    find_available_states,
)
from tito_utils.logging_utils import setup_run_log
from tito_utils.cycle.timeline import STATE_LOOKBACK
from tito_utils.cycle.warmup import decide_warmup, warmup_window
from tito_utils.ef5.jobs.helpers import (
    region_path_key,
    resolve_control_template,
    resolve_region_resolution,
    with_sep,
)


def run_warmup_if_needed(
    cycle_time, regions_to_run, region_qpe_sources, config,
    statesPath, modelStates, model_resolution, region_resolution_map,
    ef5Path, systemModel, systemName, templatePath, basicPath,
    parametersPath, LR_TimeStep, dataPath, qpf_store_path,
    precipEF5Folder, SEND_ALERTS, alert_recipients, smtp_config,
    master_log=None,
):
    """Check if any region needs a warmup spin-up and run it.

    A warmup is triggered when **no** EF5 states exist within 48 hours of
    *cycle_time*.  The warmup simulates from ``cycle_time - warmup_days``
    to ``cycle_time - 40 h`` using the per-region warmup precip source
    (IMERG or HSAF).  States are saved at ``cycle_time - 40 h`` so they
    are within the 48‑h lookback of the next operational cycle.

    For STREAM_SAT regions, warmup states are saved to the standard
    ``states/<region>_<resolution>/`` path and then **copied** to all
    per‑member     ``EF5_conf/states/stream_sat/ensS{N}/<region>_<resolution>/``
    folders so every ensemble member can warm‑start from the same spun‑up
    state.
    """
    warmup_enabled = getattr(config, "warmup_enabled", False)
    if not warmup_enabled:
        return

    warmup_days = int(getattr(config, "warmup_days", 10))
    warmup_precip_map = getattr(config, "warmup_precip_source_map", {})
    if not isinstance(warmup_precip_map, dict):
        warmup_precip_map = {}

    warmup_start, warmup_end = warmup_window(cycle_time, warmup_days)

    # ═══════════════════════════════════════════════════════════════════
    # Phase 1 — Check ALL regions, classify into groups
    # ═══════════════════════════════════════════════════════════════════
    print("\n***_________WARMUP CHECK_________***")
    if master_log:
        master_log.info("Warmup check — cycle %s UTC", cycle_time.strftime("%Y-%m-%d %H:%M"))

    warmup_imerg = []    # (region, rkey, qpe, r_res, states_path, region_data)
    warmup_hsaf = []
    warmup_streamsat = set()

    for region in regions_to_run:
        region_slug = region.lower()
        qpe = region_qpe_sources.get(region, "IMERG").upper()
        r_res = resolve_region_resolution(
            region, model_resolution, region_resolution_map)
        rkey = region_path_key(region, r_res)

        # ── Check if states exist within 48 h ──────────────────────────
        if qpe == "STREAM_SAT":
            ss_state_root = getattr(config, "stream_sat_state_folder",
                                    "EF5_conf/states/stream_sat/")
            ens_size = int(getattr(config, "stream_sat_ensemble_size", 10))
            any_found = False
            found_time = None
            for m in range(1, ens_size + 1):
                m_path = os.path.join(ss_state_root, f"ensS{m}", rkey, "")
                found, st = find_available_states(
                    m_path, modelStates, cycle_time,
                    cycle_time - STATE_LOOKBACK,
                )
                if found:
                    any_found = True
                    found_time = st
                    break
            decision = decide_warmup(
                region, cycle_time,
                qpe_source=qpe,
                warmup_enabled=True,
                warmup_days=warmup_days,
                warmup_precip_source=warmup_precip_map.get(region, "IMERG"),
                states_found=any_found,
                states_time=found_time,
            )
            if not decision.needed:
                print(f"    {region} [{r_res}] [STREAM_SAT]: states OK → skip")
                if master_log:
                    master_log.info("    %s [%s] [STREAM_SAT]: states OK", region, r_res)
                continue
            warmup_streamsat.add(region)
            states_path_for_region = os.path.join(statesPath, rkey, "")
        else:
            # Deterministic IMERG path stores states under states/imerg/<rkey>/
            if qpe == "IMERG":
                states_path_for_region = os.path.join(
                    getattr(config, "imerg_state_folder", "EF5_conf/states/imerg/"),
                    rkey, "",
                )
            else:
                states_path_for_region = os.path.join(statesPath, rkey, "")
            found, st = find_available_states(
                states_path_for_region, modelStates, cycle_time,
                cycle_time - STATE_LOOKBACK,
            )
            decision = decide_warmup(
                region, cycle_time,
                qpe_source=qpe,
                warmup_enabled=True,
                warmup_days=warmup_days,
                warmup_precip_source=warmup_precip_map.get(region, "IMERG"),
                states_found=found,
                states_time=st if found else None,
            )
            if not decision.needed:
                print(f"    {region} [{r_res}]: states at {st.strftime('%Y%m%d_%H%M')} → skip")
                if master_log:
                    master_log.info("    %s [%s]: states OK at %s", region, r_res,
                                    st.strftime('%Y%m%d_%H%M'))
                continue

        wsrc = decision.precip_source
        region_data = os.path.join(dataPath, rkey)
        entry = (region, rkey, region_slug, qpe, r_res, states_path_for_region, region_data)

        if wsrc == "HSAF":
            warmup_hsaf.append(entry)
        else:
            warmup_imerg.append(entry)

    # ── Consolidated "states missing" message ──────────────────────────
    all_warmup = warmup_imerg + warmup_hsaf
    if not all_warmup:
        print("    All regions have states within 48h — no warmup needed.")
        return

    imerg_regions = [e[0] for e in warmup_imerg]
    hsaf_regions = [e[0] for e in warmup_hsaf]
    parts = []
    if imerg_regions:
        parts.append(f"IMERG: {', '.join(imerg_regions)}")
    if hsaf_regions:
        parts.append(f"HSAF: {', '.join(hsaf_regions)}")
    print(f"    NO states within 48h for: {' | '.join(parts)}")
    print(f"    Warmup: {warmup_days}d ({warmup_start.strftime('%Y%m%d_%H%M')} → "
          f"{warmup_end.strftime('%Y%m%d_%H%M')})")
    if master_log:
        master_log.info("Warmup needed: IMERG=%s HSAF=%s  window=%dd %s→%s",
                        imerg_regions, hsaf_regions, warmup_days,
                        warmup_start.strftime('%Y%m%d_%H%M'),
                        warmup_end.strftime('%Y%m%d_%H%M'))

    # ═══════════════════════════════════════════════════════════════════
    # Phase 2 — Download shared IMERG once (for ALL IMERG regions)
    # ═══════════════════════════════════════════════════════════════════
    shared_imerg_folder = None
    if warmup_imerg:
        shared_imerg_folder = os.path.join(
            getattr(config, "imerg_precip_folder",
                    getattr(config, "precipFolder", "EF5_conf/precip/")),
            "_warmup", "_shared_imerg",
        )
        mkdir_p(shared_imerg_folder)
        print(f"\n***_________Warmup: shared IMERG download "
              f"({len(warmup_imerg)} region(s))_________***")
        try:
            from tito_utils.qpe_utils import get_gpm_files
            _mw = int(getattr(config, "imerg_max_workers", 0) or 0) or None
            get_gpm_files(
                shared_imerg_folder,
                warmup_start,
                warmup_end - timedelta(minutes=30),
                config.server, config.email_gpm,
                config.xmin, config.ymin,
                config.xmax, config.ymax,
                max_workers=_mw,
            )
            print(f"    Shared IMERG warmup download done → {shared_imerg_folder}")
        except Exception as exc:
            print(f"    !!! Shared IMERG warmup download failed: {exc}")
            shared_imerg_folder = None

    # ═══════════════════════════════════════════════════════════════════
    # Phase 3 — Download HSAF per region (with IMERG fallback)
    # ═══════════════════════════════════════════════════════════════════
    warmup_regions_final = []

    for entry in warmup_imerg:
        region, rkey, region_slug, qpe, r_res, spath, rdata = entry
        if shared_imerg_folder:
            warmup_regions_final.append(
                (region, rkey, region_slug, qpe, r_res, spath, rdata,
                 "IMERG", shared_imerg_folder))
        else:
            print(f"    !!! {region}: no shared IMERG available — skipping warmup")

    for entry in warmup_hsaf:
        region, rkey, region_slug, qpe, r_res, spath, rdata = entry
        hsaf_folder = os.path.join(
            getattr(config, "imerg_precip_folder",
                    getattr(config, "precipFolder", "EF5_conf/precip/")),
            "_warmup", region_slug,
        )
        mkdir_p(hsaf_folder)
        print(f"\n***_________Warmup: HSAF download for {region}_________***")
        ok = False
        try:
            from tito_utils.qpe_utils import get_new_hsaf_precip
            get_new_hsaf_precip(
                current_timestamp=warmup_end,
                precipFolder=hsaf_folder,
                ftp_user=config.hsaf_ftp_user,
                ftp_pass=config.hsaf_ftp_pass,
                xmin=config.xmin, ymin=config.ymin,
                xmax=config.xmax, ymax=config.ymax,
                latency_minutes=0,
                lookback_hours=int(warmup_days * 24),
            )
            print(f"    {region}: HSAF warmup download done")
            warmup_regions_final.append(
                (region, rkey, region_slug, qpe, r_res, spath, rdata,
                 "HSAF", hsaf_folder))
            ok = True
        except Exception as exc:
            print(f"    !!! {region}: HSAF warmup download failed: {exc}")

        if not ok and shared_imerg_folder:
            print(f"    {region}: fallback to shared IMERG")
            warmup_regions_final.append(
                (region, rkey, region_slug, qpe, r_res, spath, rdata,
                 "IMERG", shared_imerg_folder))
        elif not ok:
            print(f"    !!! {region}: no precip available — skipping warmup")

    if not warmup_regions_final:
        print("    No regions have precip for warmup — aborting warmup.")
        return

    # ═══════════════════════════════════════════════════════════════════
    # Phase 4 — Build & run EF5 warmup jobs
    # ═══════════════════════════════════════════════════════════════════
    warmup_jobs = []
    warmup_staging_folders = []
    _lock = threading.Lock()
    output_ts = cycle_time.strftime("%Y%m%d.%H%M%S")
    region_template_map = getattr(config, "region_template_map", {})
    default_template = getattr(config, "templates", "ef5_Antigua_control_template.txt")

    print(f"\n***_________Building {len(warmup_regions_final)} warmup EF5 job(s)_________***")
    for (region, rkey, region_slug, qpe, r_res, spath, rdata,
         wsrc, wfolder) in warmup_regions_final:

        mkdir_p(spath)
        # outputs/<cycle>/<rkey>/warmup/
        warmup_tmp = os.path.join(
            getattr(config, "dataPath", "outputs/") or "outputs/",
            output_ts, rkey, "warmup")
        warmup_staging = os.path.join(precipEF5Folder, rkey, "warmup")
        mkdir_p(warmup_tmp)
        mkdir_p(warmup_staging)
        mkdir_p(rdata)

        tmpl = resolve_control_template(
            templatePath,
            region,
            r_res,
            region_template_map=region_template_map,
            default_template=default_template,
        )

        try:
            job_log = setup_run_log(rdata, f"ef5_warmup_{rkey}")
            job_log.info("Warmup EF5 — region=%s res=%s source=%s days=%d",
                         region, r_res, wsrc, warmup_days)

            eff_start, ctrl_file, run_path = prepare_ef5(
                warmup_staging,
                wfolder,
                with_sep(spath),
                modelStates,
                warmup_end - timedelta(minutes=30),
                warmup_end - timedelta(days=max(7, warmup_days + 1)),
                cycle_time,
                systemName,
                SEND_ALERTS, alert_recipients, smtp_config,
                with_sep(warmup_tmp),
                with_sep(rdata),
                region, systemModel,
                templatePath, tmpl,
                warmup_end,
                warmup_end,
                warmup_end,
                warmup_end,
                LR_TimeStep,
                False,
                region, r_res,
                basicPath, parametersPath,
                wsrc, "none",
                stage_precip=True,
                output_timestamp_str=output_ts,
                qpf_store_forcing_path=os.path.join(qpf_store_path, region_slug, ""),
                save_states=True,
                cold_start_begin_time=warmup_start,
                cold_start_warm_end_time=warmup_end,
                verbose=True,
                run_log=job_log,
            )
            print(f"    {region} [{r_res}] [WARMUP {wsrc}]: "
                  f"{eff_start.strftime('%Y%m%d_%H%M')} → "
                  f"{warmup_end.strftime('%Y%m%d_%H%M')}, ctrl={ctrl_file}")
            if master_log:
                master_log.info("    %s [%s] [WARMUP %s]: %s → %s, ctrl=%s",
                                region, r_res, wsrc,
                                eff_start.strftime('%Y%m%d_%H%M'),
                                warmup_end.strftime('%Y%m%d_%H%M'),
                                ctrl_file)

            with _lock:
                warmup_staging_folders.append(warmup_staging)
                warmup_jobs.append({
                    "region":               region,
                    "ef5Path":              ef5Path,
                    "tmpOutput":            run_path + "/",
                    "controlFile":          ctrl_file,
                    "output_timestamp_str": output_ts,
                    "_warmup_qpe":          qpe,
                    "_warmup_end":          warmup_end,
                    "_warmup_rkey":         rkey,
                    "_warmup_res":          r_res,
                })
        except Exception as exc:
            print(f"    !!! {region}: warmup EF5 prep failed: {exc}")
            if master_log:
                master_log.error("    %s: warmup EF5 prep failed: %s", region, exc)

    if not warmup_jobs:
        print("    No warmup jobs prepared.")
        return

    print(f"\n***_________Running {len(warmup_jobs)} warmup EF5 job(s)_________***")
    _w = getattr(config, "ef5_max_workers", None)
    try:
        _mw = int(_w) if _w not in (None, "") else None
    except (TypeError, ValueError):
        _mw = None
    if _mw is not None and _mw <= 0:
        _mw = None
    run_ef5_simulations_parallel(
        warmup_jobs,
        max_workers=_mw if _mw is not None else min(
            len(warmup_jobs), max(1, (os.cpu_count() or 4)),
        ),
    )
    print("    Warmup EF5 runs complete — states saved.")

    # ═══════════════════════════════════════════════════════════════════
    # Phase 5 — Copy states to STREAM_SAT per‑member folders
    # ═══════════════════════════════════════════════════════════════════
    for job in warmup_jobs:
        region = job["region"]
        rkey = job.get("_warmup_rkey") or region_path_key(
            region,
            resolve_region_resolution(
                region, model_resolution, region_resolution_map),
        )
        qpe = job.get("_warmup_qpe", "")
        w_end = job.get("_warmup_end")

        if qpe != "STREAM_SAT":
            continue

        ss_state_root = getattr(config, "stream_sat_state_folder",
                                "EF5_conf/states/stream_sat/")
        ens_size = int(getattr(config, "stream_sat_ensemble_size", 10))
        src_states_path = os.path.join(statesPath, rkey, "")

        print(f"    {region}: copying warmup states to {ens_size} "
              f"STREAM-Sat member folders ({rkey}) …")
        if master_log:
            master_log.info("    %s: copying warmup states → %d member folders (%s)",
                            region, ens_size, rkey)

        for m in range(1, ens_size + 1):
            dst_path = os.path.join(ss_state_root, f"ensS{m}", rkey, "")
            mkdir_p(dst_path)
            if w_end is None:
                continue
            state_ts = w_end.strftime("%Y%m%d_%H%M")
            for state_name in modelStates:
                src = os.path.join(src_states_path, f"{state_name}_{state_ts}.tif")
                dst = os.path.join(dst_path, f"{state_name}_{state_ts}.tif")
                if os.path.isfile(src) and not os.path.isfile(dst):
                    try:
                        shutil.copy2(src, dst)
                    except Exception as exc:
                        print(f"    Warning: copy {state_name} → ensS{m}: {exc}")

        print(f"    {region}: warmup state copy complete.")

    for staging in warmup_staging_folders:
        try:
            cleanup_staged_precip_folders({staging})
        except Exception:
            pass

    print("***_________WARMUP COMPLETE_________***\n")


# Back-compat alias used by older call sites
_run_warmup_if_needed = run_warmup_if_needed

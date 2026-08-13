"""
prepare_precip.py
================
Centralised precipitation preparation for TITO.

IMERG QPE is shared across **all** regions that use it (one download per
cycle time, regardless of QPF differences).  Other QPE sources (HSAF,
SCaMPR) and all QPF sources (GFS, AROME) are grouped by their forcing
signature for sharing.

Staging (copying to ``precipEF5/<region>/``) is left to
``prepare_ef5(…, stage_precip=True)`` inside ``tito_utils.ef5.jobs.builders``.

Usage (from orchestrator)::

    from tito_utils.precip import prepare_cycle_precip
    shared = prepare_cycle_precip(...)
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from tito_utils.file_utils.cleanup import cleanup_precip, cleanup_streamsat_outputs
from tito_utils.file_utils.file_handling import mkdir_p, newline
from tito_utils.ef5.ef5_routines import find_available_states
from tito_utils.qpe_utils import (
    get_gpm_files,
    get_new_hsaf_precip,
    get_new_scampr_precip,
)
from tito_utils.qpf_utils.gfs_manager import GFS_searcher
from tito_utils.qpf_utils.arome_manager import AROME_searcher
from tito_utils.qpf_utils.arome_downloader import get_arome_domain_for_region


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _with_sep(path: str) -> str:
    return os.path.join(path, "")


def _parse_scampr_ts(filename: str) -> Optional[datetime]:
    m = re.match(r"scampr\.qpe\.(\d{12})\.mmhInst\.tif$", filename)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d%H%M")
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# result container
# ---------------------------------------------------------------------------

@dataclass
class SharedPrecip:
    """Returned by :func:`prepare_all_precip`."""
    # IMERG — one folder per cycle_time (shared across ALL IMERG regions)
    imerg_folders: Dict[str, str] = field(default_factory=dict)       # cycle_key → folder
    imerg_eff_starts: Dict[str, datetime] = field(default_factory=dict)  # region → effective start

    # QPF — shared caches
    gfs_cache: Dict[str, str] = field(default_factory=dict)           # cycle_key → gfs_data/
    arome_cache: Dict[Tuple[str, str], str] = field(default_factory=dict)  # (ck, domain) → arome_data/

    # SCaMPR — shared folder (for gap-fill and direct LR QPE)
    scampr_folder: Optional[str] = None

    # STREAM-Sat — per-region dict: region → {"tif_root": ..., "ensemble_size": ...}
    streamsat_info: Dict[str, dict] = field(default_factory=dict)

    # StormLab-GFS — per-region dict: region → {"tif_root", "ensemble_size", ...}
    stormlab_info: Dict[str, dict] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# IMERG state-aware download window
# ---------------------------------------------------------------------------

def _resolve_imerg_download_window(
    region: str,
    cycle_time: datetime,
    states_path: str,
    model_states: List[str],
    cold_start_warmup: timedelta,
    cold_start_post_warmup: timedelta,
    *,
    hindcast: bool = False,
) -> Tuple[datetime, datetime]:
    """Return (dl_start, effective_start) for IMERG download.

    Checks states in *states_path* (must be the same folder EF5 will load,
    e.g. ``EF5_conf/states/imerg/<region_res>/``).

    Hindcast: search from cycle time T (no 4h latency).
    Operational: search from T−4h (IMERG Early).
    """
    target = cycle_time if hindcast else (cycle_time - timedelta(hours=4))
    fail_time = target - timedelta(days=7)

    found, state_time = find_available_states(
        _with_sep(states_path), model_states, target, fail_time,
    )

    if found:
        dl_start = state_time - timedelta(minutes=30)   # 30-min buffer
        effective = state_time
        print(f"    {region}: states at {state_time.strftime('%Y%m%d_%H%M')} → "
              f"IMERG from {dl_start.strftime('%Y%m%d_%H%M')} "
              f"(path={states_path})")
    else:
        warm_end = target - cold_start_post_warmup
        dl_start = warm_end - cold_start_warmup - timedelta(minutes=30)
        effective = dl_start + timedelta(minutes=30)
        print(f"    {region}: no states in {states_path} → cold-start IMERG from "
              f"{dl_start.strftime('%Y%m%d_%H%M')}")

    return dl_start, effective


def _seed_imerg_from_warmup(imerg_folder: str, warmup_folder: str,
                            dl_start: datetime, imerg_end: datetime) -> int:
    """Copy existing warmup IMERG TIFs into the cycle shared folder. Returns count."""
    if not warmup_folder or not os.path.isdir(warmup_folder):
        return 0
    n = 0
    t = dl_start
    delta = timedelta(minutes=30)
    while t <= imerg_end:
        name = f"imerg.qpe.{t.strftime('%Y%m%d%H%M')}.30minAccum.tif"
        src = os.path.join(warmup_folder, name)
        dst = os.path.join(imerg_folder, name)
        if os.path.isfile(src) and os.path.getsize(src) > 0:
            if not (os.path.isfile(dst) and os.path.getsize(dst) > 0):
                try:
                    shutil.copy2(src, dst)
                    n += 1
                except OSError:
                    pass
        t += delta
    return n


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def prepare_all_precip(
    regions_to_run: List[str],
    region_cycle_times: Dict[str, datetime],
    region_qpe_sources: Dict[str, str],
    region_qpf_requested: Dict[str, List[str]],
    config: Any,
    master_log: Any = None,
) -> SharedPrecip:
    """Download all QPE/QPF to shared folders.  No staging to precipEF5/.

    Key design decisions
    --------------------
    * IMERG is downloaded ONCE per cycle time — **all** regions that use
      IMERG (regardless of QPF) share a single download folder.
    * GFS is downloaded ONCE per cycle time.
    * AROME is downloaded ONCE per (cycle time × domain).
    * SCaMPR is downloaded ONCE (global).
    * The IMERG 4-hour latency gap is filled ONCE per IMERG cycle folder.
    """
    # config shortcuts
    precip_root = getattr(config, "imerg_precip_folder",
                          getattr(config, "precipFolder", "EF5_conf/precip/"))
    qpf_store_root = getattr(config, "qpf_store_path", "EF5_conf/qpf_store/")
    states_root = getattr(config, "statesPath", "EF5_conf/states/")
    model_states = getattr(config, "modelStates",
                           ["crest_SM", "kwr_IR", "kwr_pCQ", "kwr_pOQ"])
    gap_mode = getattr(config, "qpe_gap_fill_mode", "IMERG_ONLY").strip().upper()

    cold_warmup = timedelta(hours=6)
    cold_post = timedelta(hours=2)
    if hasattr(config, "imerg_cold_start_warmup"):
        raw = config.imerg_cold_start_warmup
        if isinstance(raw, timedelta):
            cold_warmup = raw
    if hasattr(config, "imerg_post_warmup_duration"):
        raw = config.imerg_post_warmup_duration
        if isinstance(raw, timedelta):
            cold_post = raw

    result = SharedPrecip()
    _lock = threading.Lock()

    # ═══════════════════════════════════════════════════════════════════
    # 1. Partition regions for sharing
    # ═══════════════════════════════════════════════════════════════════

    # IMERG regions grouped by cycle_time_key only (IGNORE QPF differences)
    imerg_cycle_regions: Dict[str, List[str]] = {}
    # Per-cycle: the cycle_time for that key
    imerg_cycle_times: Dict[str, datetime] = {}
    # STREAM-Sat regions (separate pipeline, not IMERG)
    streamsat_regions: Dict[str, str] = {}  # region → cycle_key
    # Non-IMERG regions (HSAF, SCaMPR)
    other_qpe_regions: List[str] = []

    for region in regions_to_run:
        qpe = region_qpe_sources.get(region, "IMERG").upper()
        ct = region_cycle_times[region]
        ck = ct.strftime("%Y%m%d%H%M")
        if qpe == "IMERG":
            imerg_cycle_regions.setdefault(ck, []).append(region)
            imerg_cycle_times[ck] = ct
        elif qpe == "STREAM_SAT":
            streamsat_regions[region] = ck
        else:
            other_qpe_regions.append(region)

    # QPF regions grouped by cycle_time_key (for GFS / AROME sharing)
    gfs_regions_by_cycle: Dict[str, List[str]] = {}
    arome_regions_by_cycle_domain: Dict[Tuple[str, str], List[str]] = {}

    for region in regions_to_run:
        qpf_list = region_qpf_requested.get(region, [])
        ck = region_cycle_times[region].strftime("%Y%m%d%H%M")

        for src in qpf_list:
            src_u = src.upper()
            if src_u in ("GFS", "WRF"):
                gfs_regions_by_cycle.setdefault(ck, []).append(region)
            elif src_u == "AROME":
                try:
                    domain = get_arome_domain_for_region(region)
                    arome_regions_by_cycle_domain.setdefault((ck, domain), []).append(region)
                except ValueError:
                    print(f"    AROME: no domain for {region} — skip")

    # ═══════════════════════════════════════════════════════════════════
    # 2. Shared GFS download (one per cycle_time_key)
    # ═══════════════════════════════════════════════════════════════════

    for ck, rlist in gfs_regions_by_cycle.items():
        ct = imerg_cycle_times.get(ck, region_cycle_times[rlist[0]])
        shared_store = _with_sep(os.path.join(qpf_store_root, "_shared", ck))
        mkdir_p(shared_store)
        print(f"***_________Shared GFS cycle {ck} "
              f"({len(set(rlist))} region(s))_________***")
        try:
            GFS_searcher(
                getattr(config, "GFS_precip_path", "EF5_conf/precip/gfs/"),
                shared_store,
                ct,
                ct + timedelta(hours=24),
                config.xmin, config.xmax, config.ymin, config.ymax,
            )
            result.gfs_cache[ck] = os.path.join(shared_store, "gfs_data")
        except Exception as exc:
            print(f"    Shared GFS failed for {ck}: {exc}")

    # ═══════════════════════════════════════════════════════════════════
    # 3. Shared AROME download (one per cycle × domain)
    # ═══════════════════════════════════════════════════════════════════
    # Hindcast with STREAM_SAT: only GFS QPF is needed, skip AROME.

    hindcast = getattr(config, "HindCastMode", False)
    if not hindcast:
        for (ck, domain), rlist in arome_regions_by_cycle_domain.items():
            ct = imerg_cycle_times.get(ck, region_cycle_times[rlist[0]])
            shared_store = _with_sep(
                os.path.join(qpf_store_root, "_shared_arome", ck, domain))
            mkdir_p(shared_store)
            print(f"***_________Shared AROME ({domain}) cycle {ck} "
                  f"({len(set(rlist))} region(s))_________***")
            try:
                AROME_searcher(
                    getattr(config, "AROME_precip_path", "EF5_conf/precip/arome/"),
                    shared_store,
                    ct,
                    ct + timedelta(hours=24),
                    config.xmin, config.xmax, config.ymin, config.ymax,
                    domain,
                )
                result.arome_cache[(ck, domain)] = os.path.join(shared_store, "arome_data")
            except Exception as exc:
                print(f"    Shared AROME failed for ({ck}, {domain}): {exc}")

    # ═══════════════════════════════════════════════════════════════════
    # 4. Shared SCaMPR download
    # ═══════════════════════════════════════════════════════════════════

    hindcast = getattr(config, "HindCastMode", False)
    # In hindcast, IMERG has no latency → SCaMPR gap fill is never needed.
    # Only download SCaMPR if (a) a region uses it as its primary QPE, or
    # (b) we're in realtime mode with IMERG_SCAMPR gap fill or STREAM_SAT+SCaMPR.
    _ss_gap = getattr(config, "stream_sat_gap_fill_mode", "SCAMPR").strip().upper()
    _ss_wants_scampr = _ss_gap in (
        "SCAMPR", "SCAMPR_QPE", "SCAMPR_ONLY",
    )
    if (not hindcast and gap_mode == "IMERG_SCAMPR") or any(
        region_qpe_sources.get(r, "").upper() == "SCAMPR" for r in regions_to_run
    ) or (not hindcast and any(
        # STREAM_SAT gap fill only in realtime (hindcast uses QPF-only)
        region_qpe_sources.get(r, "").upper() == "STREAM_SAT" and _ss_wants_scampr
        for r in regions_to_run
    )):
        scampr_root = getattr(config, "scampr_precip_folder", "EF5_conf/precip/scampr/")
        result.scampr_folder = _with_sep(os.path.join(scampr_root, "_shared"))
        mkdir_p(result.scampr_folder)

        ref_ct = list(region_cycle_times.values())[0]
        older_than = ref_ct - timedelta(hours=6.5)
        for fname in os.listdir(result.scampr_folder):
            ts = _parse_scampr_ts(fname)
            if ts and ts < older_than:
                try:
                    os.remove(os.path.join(result.scampr_folder, fname))
                except Exception:
                    pass

        print("***_________Shared SCaMPR download_________***")
        try:
            get_new_scampr_precip(
                current_timestamp=ref_ct, precipFolder=result.scampr_folder,
                xmin=config.xmin, ymin=config.ymin,
                xmax=config.xmax, ymax=config.ymax,
                latency_minutes=int(getattr(config, "scampr_latency_minutes", 20)),
            )
        except Exception as exc:
            print(f"    Shared SCaMPR failed: {exc}")
            result.scampr_folder = None

    # ═══════════════════════════════════════════════════════════════════
    # 5. IMERG QPE download — ONE per cycle_time_key
    # ═══════════════════════════════════════════════════════════════════

    for ck, imerg_regions in imerg_cycle_regions.items():
        ct = imerg_cycle_times[ck]
        imerg_folder = _with_sep(os.path.join(precip_root, "_shared", ck))
        result.imerg_folders[ck] = imerg_folder
        mkdir_p(imerg_folder)

        # cleanup
        try:
            cleanup_precip(ct, imerg_folder, imerg_folder,
                           keep_gap_fill=False, older_qpe_hours=6.5)
        except Exception as exc:
            print(f"    Warning: IMERG cleanup [{ck}]: {exc}")

        # State path MUST match EF5 Phase A (states/imerg/<region_res>/).
        # Looking under states/<region_res>/ only causes a short cold-start
        # download while EF5 still warm-starts from older imerg/ states →
        # mass "Missing precip" zeros.
        ref_region = imerg_regions[0]
        from tito_utils.ef5.jobs.helpers import (
            region_path_key,
            resolve_region_resolution,
        )
        _res = resolve_region_resolution(
            ref_region,
            getattr(config, "model_resolution", "90m"),
            getattr(config, "region_resolution_map", {}),
        )
        _rkey = region_path_key(ref_region, _res)
        imerg_state_root = getattr(
            config, "imerg_state_folder", "EF5_conf/states/imerg/")
        ref_states_path = os.path.join(imerg_state_root, _rkey)

        dl_start, eff_start = _resolve_imerg_download_window(
            ref_region, ct, ref_states_path, model_states,
            cold_warmup, cold_post,
            hindcast=hindcast,
        )

        # Hindcast: full archive IMERG through cycle time T (no 4h latency).
        # Operational: IMERG Early ends at T−4h.
        if hindcast:
            imerg_end = ct
        else:
            imerg_end = ct - timedelta(hours=4)

        # Prefer files already downloaded for warmup (same product, long archive).
        warmup_folder = os.path.join(
            precip_root, "_warmup", "_shared_imerg")
        n_seed = _seed_imerg_from_warmup(
            imerg_folder, warmup_folder, dl_start, imerg_end)
        if n_seed:
            print(f"    IMERG [{ck}]: seeded {n_seed} file(s) from warmup cache")

        # Check what we already have (after seed)
        existing = sorted(glob.glob(os.path.join(
            imerg_folder, "imerg.qpe.*.30minAccum.tif")))
        if existing:
            # Need coverage from dl_start through imerg_end — not only "latest"
            earliest = datetime.strptime(
                os.path.basename(existing[0])[10:22], "%Y%m%d%H%M")
            latest_dt = datetime.strptime(
                os.path.basename(existing[-1])[10:22], "%Y%m%d%H%M")
            need_start = dl_start
            if earliest <= dl_start + timedelta(minutes=30) and latest_dt >= imerg_end - timedelta(minutes=30):
                # Spot-check: if first/last OK, still fill holes via get_gpm (skip exists)
                print(
                    f"    IMERG [{ck}]: have {len(existing)} file(s) "
                    f"{earliest.strftime('%Y%m%d_%H%M')}→"
                    f"{latest_dt.strftime('%Y%m%d_%H%M')}; "
                    f"fill any gaps {need_start.strftime('%Y%m%d_%H%M')}→"
                    f"{imerg_end.strftime('%Y%m%d_%H%M')}"
                )
            _do_imerg_download(imerg_folder, need_start, imerg_end, ck, config)
        else:
            _do_imerg_download(imerg_folder, dl_start, imerg_end, ck, config)

        # Record effective start for each region in this cycle
        for region in imerg_regions:
            result.imerg_eff_starts[region] = eff_start

    # ═══════════════════════════════════════════════════════════════════
    # 6. STREAM-Sat QPE — run pipeline + convert NC → GeoTIFFs
    # ═══════════════════════════════════════════════════════════════════
    #
    # STREAM-Sat uses one config per DOMAIN (not per region):
    #   Caribbean config → Antigua + Barbados + Guatemala + Haiti
    #   Comoros config  → Comoros only
    # So we group regions by domain and run ONCE per domain.

    if streamsat_regions:
        # ── Group regions by STREAM-Sat domain ──────────────────────
        from tito_utils.qpe_utils.stream_sat_utils import (
            run_and_convert_streamsat,
            get_domain_for_region,
        )
        domain_regions: Dict[str, List[str]] = {}
        for region in streamsat_regions:
            try:
                domain = get_domain_for_region(region)
            except ValueError:
                print(f"    STREAM-Sat: unknown domain for {region} — skip")
                continue
            domain_regions.setdefault(domain, []).append(region)

        ens_size = int(getattr(config, "stream_sat_ensemble_size", 10))
        win_hours = int(getattr(config, "stream_sat_window_hours", 48))
        warm_hours = int(getattr(config, "stream_sat_warmup_hours", 12))
        max_w = getattr(config, "stream_sat_max_workers", None)
        timeout_s = int(getattr(config, "stream_sat_pipeline_timeout", 7200))
        tif_root_base = getattr(config, "stream_sat_precip_folder", "EF5_conf/precip/stream_sat/")
        tif_naming = getattr(config, "stream_sat_tif_naming", "streamsat")

        # Resolve tif_root_base relative to TITO root
        tito_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        if not os.path.isabs(tif_root_base):
            tif_root_base = os.path.join(tito_root, tif_root_base)

        # ── Run STREAM-Sat ONCE per domain ──────────────────────────
        for domain, domains_regions in domain_regions.items():
            representative = domains_regions[0]
            ck = streamsat_regions[representative]

            # Hindcast mode: pass cycle time as --end (no IMERG latency subtraction).
            # STREAM-Sat's half-hourly grid rounds down, so add +30 min to get
            # output covering up to the actual cycle time.
            hindcast = bool(getattr(config, "HindCastMode", False))
            ss_end_dt = None
            if hindcast:
                ss_end_dt = region_cycle_times.get(representative) + timedelta(minutes=30)
                from tito_utils.logging_utils import debug_print, is_debug, user_print
                if is_debug():
                    debug_print(
                        f"    STREAM-Sat hindcast --end "
                        f"{ss_end_dt.strftime('%Y-%m-%dT%H:%M')} "
                        f"(cycle {region_cycle_times.get(representative)})")
            else:
                from tito_utils.logging_utils import debug_print
                debug_print(
                    "    STREAM-Sat operational --end omitted "
                    "(pipeline uses now − IMERG latency)")

            # Domain-specific precip folder:
            #   precip/stream_sat/caribbean/ensP1/...  (Caribbean regions)
            #   precip/stream_sat/comoros/ensP1/...    (Comoros)
            domain_tif_root = os.path.join(tif_root_base, domain)

            from tito_utils.logging_utils import debug_print, is_debug, user_print
            if is_debug():
                debug_print(
                    f"***_________STREAM-Sat [{domain}] for "
                    f"{domains_regions} [{ck}]_________***")
            else:
                user_print(
                    f"    STREAM-Sat [{domain}] for "
                    f"{', '.join(domains_regions)} …")
            if master_log:
                master_log.info("STREAM-Sat [%s] start — regions: %s end=%s",
                                domain, domains_regions, ss_end_dt)

            # Drop NC/TIF products older than cycle−keep_hours (default 48h)
            try:
                keep_h = float(getattr(config, "stream_sat_keep_hours", 48))
                ct_ss = region_cycle_times.get(representative)
                # NC live under STREAM-Sat-realtime/extension/realtime/output/<domain>/
                ss_pkg = os.path.join(
                    tito_root, "tito_utils", "qpe_utils",
                    "STREAM-Sat-realtime", "extension", "realtime", "output",
                    domain,
                )
                # Also clean any leftover scratch dirs' parent output
                nc_dirs = [ss_pkg]
                cleanup_streamsat_outputs(
                    ct_ss or datetime.utcnow(),
                    nc_output_dirs=nc_dirs,
                    tif_root=domain_tif_root,
                    keep_hours=keep_h,
                )
            except Exception as _ss_clean_exc:
                print(f"    Warning: STREAM-Sat cleanup: {_ss_clean_exc}")

            try:
                info = run_and_convert_streamsat(
                    region=representative,
                    ensemble_size=ens_size,
                    window_hours=win_hours,
                    warmup_hours=warm_hours,
                    end_dt=ss_end_dt,
                    tif_root=domain_tif_root,
                    max_workers=max_w,
                    keep_scratch=False,
                    tif_naming=tif_naming,
                    pipeline_log=master_log,
                )

                # Share result across ALL regions in this domain
                for region in domains_regions:
                    result.streamsat_info[region] = info
                user_print(
                    f"    STREAM-Sat [{domain}] ready — "
                    f"{info['ensemble_size']} members")
                debug_print(
                    f"    [{domain}]: STREAM-Sat ready — "
                    f"{info['ensemble_size']} members in {info['tif_root']}"
                    f" (shared: {', '.join(domains_regions)})")

            except Exception as exc:
                print(f"    STREAM-Sat [{domain}] failed: {exc}")
                for region in domains_regions:
                    result.streamsat_info[region] = {"error": str(exc)}

    # ═══════════════════════════════════════════════════════════════════
    # 6b. StormLab-GFS QPF — run pipeline + convert NC → GeoTIFFs
    # ═══════════════════════════════════════════════════════════════════
    stormlab_regions = [
        r for r in regions_to_run
        if any(str(s).strip().upper() == "STORMLAB"
               for s in region_qpf_requested.get(r, []))
    ]
    if stormlab_regions:
        from tito_utils.qpf_utils.stormlab_utils import run_and_convert_stormlab
        from tito_utils.logging_utils import debug_print, is_debug, user_print

        ref_ct = region_cycle_times[stormlab_regions[0]]
        if is_debug():
            debug_print(
                f"***_________StormLab-GFS QPF for {stormlab_regions}_________***")
        else:
            user_print(f"    StormLab QPF for {', '.join(stormlab_regions)} …")
        if master_log:
            master_log.info("StormLab start — regions=%s cycle_time=%s",
                            stormlab_regions, ref_ct)
        try:
            sl_map = run_and_convert_stormlab(
                stormlab_regions,
                cycle_time=ref_ct,
                ensemble_size=int(getattr(config, "stormlab_ensemble_size", 10)),
                forcing_members=int(getattr(config, "stormlab_forcing_members", 1)),
                run_pipeline=bool(getattr(config, "stormlab_run_pipeline", True)),
                tif_root_base=getattr(config, "stormlab_precip_folder", "EF5_conf/precip/stormlab/"),
                nc_root=getattr(config, "stormlab_nc_root", None),
                source=str(getattr(config, "stormlab_source", "auto")),
                timeout_seconds=int(getattr(config, "stormlab_pipeline_timeout", 14400)),
                tif_naming=str(getattr(config, "stormlab_tif_naming", "stormlab")),
                lr_hours=24,
                hindcast=bool(getattr(config, "HindCastMode", False)),
                min_age_h=float(getattr(config, "stormlab_min_age_h", 5.0)),
                pipeline_log=master_log,
            )
            result.stormlab_info.update(sl_map)
            ok_n = sum(1 for v in sl_map.values() if "error" not in v)
            print(f"    StormLab ready for {ok_n}/{len(sl_map)} region(s)")
        except Exception as exc:
            print(f"    StormLab prep failed: {exc}")
            if master_log:
                master_log.error("StormLab prep failed: %s", exc)
            for r in stormlab_regions:
                result.stormlab_info[r] = {"error": str(exc)}

    # ═══════════════════════════════════════════════════════════════════
    # 7. Non-IMERG QPE download (HSAF, SCaMPR) — serial, per-region
    # ═══════════════════════════════════════════════════════════════════

    for region in other_qpe_regions:
        qpe = region_qpe_sources[region].upper()
        ct = region_cycle_times[region]
        ck = ct.strftime("%Y%m%d%H%M")
        folder = _with_sep(os.path.join(precip_root, "_shared", f"{qpe.lower()}_{ck}"))
        mkdir_p(folder)

        print(f"***_________{qpe} QPE for {region}_________***")
        try:
            cleanup_precip(ct, folder, folder, keep_gap_fill=False,
                           older_qpe_hours=6.5)
        except Exception as exc:
            print(f"    Warning: {qpe} cleanup [{region}]: {exc}")

        if qpe == "HSAF":
            try:
                get_new_hsaf_precip(
                    current_timestamp=ct, precipFolder=folder,
                    ftp_user=config.hsaf_ftp_user,
                    ftp_pass=config.hsaf_ftp_pass,
                    xmin=config.xmin, ymin=config.ymin,
                    xmax=config.xmax, ymax=config.ymax,
                    latency_minutes=int(getattr(config, "hsaf_latency_minutes", 20)),
                )
            except Exception as exc:
                print(f"    HSAF failed for {region}: {exc}")
        elif qpe == "SCAMPR":
            try:
                get_new_scampr_precip(
                    current_timestamp=ct, precipFolder=folder,
                    xmin=config.xmin, ymin=config.ymin,
                    xmax=config.xmax, ymax=config.ymax,
                    latency_minutes=int(getattr(config, "scampr_latency_minutes", 20)),
                )
            except Exception as exc:
                print(f"    SCaMPR failed for {region}: {exc}")

        # Store in result so orchestrator can find it
        if not hasattr(result, '_other_qpe'):
            result._other_qpe = {}
        result._other_qpe[region] = folder

    # ═══════════════════════════════════════════════════════════════════
    # 8. Done — no gap fill needed.  The two-phase EF5 pipeline handles
    #    the IMERG latency natively:
    #      Phase 2a (IMERG run):  T-start → T−4h, saves state at T−4h
    #      Phase 2b (SCaMPR+X):   loads state at T−4h, SCaMPR QPE (10-min)
    #                             T−4h→T, then X QPF T→T+24h
    #    SCaMPR runs use raw SCaMPR files — no conversion to IMERG format.
    # ═══════════════════════════════════════════════════════════════════

    newline(2)
    print("******** Precipitation preparation complete ********")
    return result


# ---------------------------------------------------------------------------
# internal: single IMERG download
# ---------------------------------------------------------------------------

def _do_imerg_download(
    folder: str,
    dl_start: datetime,
    imerg_end: datetime,
    label: str,
    config: Any,
) -> None:
    """Download IMERG files from *dl_start* to *imerg_end* into *folder*."""
    if dl_start >= imerg_end:
        print(f"    IMERG [{label}] window empty")
        return

    print(f"    IMERG [{label}]: {dl_start.strftime('%Y%m%d_%H%M')} → "
          f"{imerg_end.strftime('%Y%m%d_%H%M')}")

    get_gpm_files(
        folder,
        dl_start,
        imerg_end - timedelta(minutes=30),
        config.server, config.email_gpm,
        config.xmin, config.ymin, config.xmax, config.ymax,
    )

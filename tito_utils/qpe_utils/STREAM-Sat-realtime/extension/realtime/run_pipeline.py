#!/usr/bin/env python3
"""
run_pipeline.py  —  Real-time STREAM-Sat QPE orchestrator
==========================================================
Author:       Yagmur Derin, University of Iowa
Created:      2026-04-17
License:      MIT (see extension/LICENSE)

End-to-end portable driver that produces a fresh STREAM-Sat ensemble
QPE NetCDF using the latest IMERG Early Run and GFS 850 hPa
wind-derived motion vectors.

Usage
-----
    python extension/realtime/run_pipeline.py \\
        --config extension/realtime/config.yaml \\
        --end 2025-11-17T23:30 --ensemble 50

    # Or, "right now minus IMERG latency":
    python extension/realtime/run_pipeline.py --config extension/realtime/config.yaml

Exit codes
----------
    0  success
    2  configuration error
    3  data ingest failure (IMERG or GFS)
    4  STREAM-Sat failure
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
import tempfile
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np


log = logging.getLogger("streamsat-rt")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent     # the fork root
EXT_DIR   = REPO_ROOT / "extension"
TOOLS_DIR = EXT_DIR
RT_DIR    = EXT_DIR / "realtime"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    required = ["output_dir", "domain", "gfs_domain",
                "ensemble_size", "window_hours"]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise ValueError(f"config missing keys: {missing}")
    cfg["output_dir"] = Path(cfg["output_dir"]).expanduser().resolve()
    # streamsat_dir defaults to the repository root (where master files live)
    cfg["streamsat_dir"] = Path(
        cfg.get("streamsat_dir") or REPO_ROOT
    ).expanduser().resolve()
    return cfg


# ---------------------------------------------------------------------------
# Compatibility patch for upstream master set_area (Py3 int-cast bug)
# ---------------------------------------------------------------------------
def _install_set_area_patch():
    import STREAM_PrecipSimulation as _sps
    def _patched_set_area(ysize, xsize, dset=21):
        _sps.area = np.zeros((ysize, xsize))
        d = int(dset)
        _sps.area.fill(d ** 2)
        rads = (d - 1) // 2
        a = np.reshape(np.arange(rads + 1, d), (1, rads))
        b = np.reshape(np.arange(d - 1, rads, -1), (1, rads))
        _sps.area[:rads, :rads] = np.transpose(a) * a
        _sps.area[(ysize - rads):, :rads] = np.transpose(b) * a
        _sps.area[:rads, (xsize - rads):] = np.transpose(a) * b
        _sps.area[(ysize - rads):, (xsize - rads):] = np.transpose(b) * b
        for i in range(rads + 1, d):
            _sps.area[i - (rads + 1), rads:(xsize - rads)] = i * d
            _sps.area[(ysize + rads) - i, rads:(xsize - rads)] = i * d
            _sps.area[rads:(ysize - rads), i - (rads + 1)] = i * d
            _sps.area[rads:(ysize - rads), (xsize + rads) - i] = i * d
        _sps.area_set = True
    _sps.set_area = _patched_set_area


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------
def _run(cmd, label, check=True):
    log.info("[%s] %s", label, " ".join(cmd))
    t0 = time.time()
    cp = subprocess.run(cmd, check=check)
    log.info("[%s] done in %.1fs", label, time.time() - t0)
    return cp


def stage_fetch_imerg(cfg, end, scratch, hours):
    """Download IMERG Early and assemble into STREAM-Sat NetCDF.

    Source selection (set ``imerg_source`` in config.yaml):

    * ``"pps"`` (default, recommended) — NASA PPS GeoTIFFs
      (fetch_imerg_early_pps.py).  Simpler auth, faster, more reliable.
    * ``"gesdisc"`` — NASA GES DISC HDF5 (fetch_imerg_early.py).
      Legacy; requires .netrc + URS cookies.
    """
    source = (cfg.get("imerg_source") or "pps").lower()
    out = scratch / "IMERG_latest.nc"
    domain_str = "{lat_min},{lat_max},{lon_min},{lon_max}".format(**cfg["domain"])

    if source == "pps":
        script = RT_DIR / "fetch_imerg_early_pps.py"
        email = (cfg.get("imerg_pps_email") or
                 os.environ.get("IMERG_PPS_EMAIL", ""))
        if not email:
            raise RuntimeError(
                "config.yaml: 'imerg_pps_email' is required when imerg_source='pps',\n"
                "  or set IMERG_PPS_EMAIL in the environment."
            )
        cmd = [sys.executable, str(script),
               "--end", end.strftime("%Y-%m-%dT%H:%M"),
               "--hours", str(int(hours)),
               "--out", str(out),
               "--tmp-dir", str(scratch / "imerg_raw"),
               f"--domain={domain_str}",
               "--email", email]
        _run(cmd, "IMERG(PPS)")
    else:  # gesdisc or any other value
        cmd = [sys.executable, str(RT_DIR / "fetch_imerg_early.py"),
               "--end", end.strftime("%Y-%m-%dT%H:%M"),
               "--hours", str(int(hours)),
               "--out", str(out),
               "--tmp-dir", str(scratch / "imerg_raw"),
               f"--domain={domain_str}"]
        _run(cmd, "IMERG(GESDISC)")
    return out


def stage_fetch_gfs(cfg, start, end, scratch):
    """Obtain a GFS 850 hPa wind NetCDF for *start*..*end*.

    Strategy:
    1. If ``gfs_wind_archive_path`` is set in config, use the archive-first
       approach via ``GFS_wind_searcher`` (zero network if archive current).
    2. Otherwise, fall back to ``download_gfs_winds`` standalone script.
    """
    domain = cfg["gfs_domain"]
    lat_min = float(domain["lat_min"])
    lat_max = float(domain["lat_max"])
    lon_min = float(domain["lon_min"])
    lon_max = float(domain["lon_max"])

    archive_path = (cfg.get("gfs_wind_archive_path") or "").strip()
    if archive_path:
        try:
            _tito_root = str(REPO_ROOT.parent)
            if _tito_root not in sys.path:
                sys.path.insert(0, _tito_root)
            from tito_utils.qpf_utils.gfs_manager import GFS_wind_searcher

            start_naive = start.replace(tzinfo=None) if start.tzinfo else start
            end_naive = end.replace(tzinfo=None) if end.tzinfo else end

            out_nc = GFS_wind_searcher(
                archive_dir=archive_path,
                output_dir=str(scratch),
                start_time=start_naive,
                end_time=end_naive,
                lat_min=lat_min, lat_max=lat_max,
                lon_min=lon_min, lon_max=lon_max,
                out_res=0.1,
            )
            log.info("[GFS winds] archive/Herbie: %s", out_nc)
            return Path(out_nc)
        except Exception as e:
            log.warning("[GFS winds] archive approach failed (%s) — falling back.", e)

    # Fallback: standalone download_gfs_winds
    sys.path.insert(0, str(TOOLS_DIR))
    import download_gfs_winds as dgw
    dgw.EVENTS = {"realtime": {"name": "Realtime window",
                                "start": start.date(),
                                "end": end.date()}}
    dgw.OUTPUT_DIR = scratch / "gfs_out"
    dgw.TEMP_DIR = scratch / "gfs_raw"
    dgw.DOMAIN = domain
    out = dgw.process_event("realtime", dgw.EVENTS["realtime"])
    if out is None:
        raise RuntimeError("GFS download returned None (see logs).")
    return Path(out)


def stage_gfs_to_mv(cfg, gfs_nc, imerg_nc, scratch):
    out = scratch / "MV_from_gfs.nc"
    cmd = [sys.executable, str(TOOLS_DIR / "gfs_to_mv_adapter.py"),
           "--gfs", str(gfs_nc), "--imerg", str(imerg_nc), "--out", str(out)]
    _run(cmd, "GFS→MV")
    return out


def stage_streamsat(cfg, imerg_nc, mv_nc, scratch, ensemble,
                     tres=0.5, warmup_hours=0):
    sys.path.insert(0, str(cfg["streamsat_dir"]))
    from STREAM_NoiseGeneration import generateNoise
    from STREAM_PrecipSimulation import simulatePrecip
    from netCDF4 import Dataset, num2date, date2num
    import random

    sys.path.insert(0, str(RT_DIR))
    from state_persistence import (
        load_state, save_state, apply_state_to_noise,
    )

    _install_set_area_patch()

    scratch_run = scratch / "streamsat"
    scratch_run.mkdir(exist_ok=True)
    for src in [imerg_nc, mv_nc]:
        link = scratch_run / src.name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(src.resolve())

    # CSGD parameter file is required and NOT shipped with the repo
    # (the upstream `data/csgd_NLmodel_WAR.nc` is a small example for a
    # different domain and is intentionally not used here). The user
    # must set `csgd_params:` in config.yaml.
    csgd_str = str(cfg.get("csgd_params", "") or "").strip()
    if not csgd_str:
        raise RuntimeError(
            "config.yaml: 'csgd_params' is required.\n"
            "  → Set it to a CSGD parameter NetCDF whose lat/lon grid "
            "matches your IMERG observations.\n"
            "  → Train one with https://github.com/KaidiWisc/CSGD_error_model, "
            "or obtain it from your project lead."
        )
    csgd = Path(csgd_str).expanduser().resolve()
    if not csgd.exists():
        raise FileNotFoundError(f"csgd_params file not found: {csgd}")
    (scratch_run / csgd.name).symlink_to(csgd)

    with Dataset(imerg_nc) as ds:
        t = ds.variables["time"][:]
        t_units = ds.variables["time"].units
        lat = ds.variables["latitude"][:]
        lon = ds.variables["longitude"][:]
    t_py = num2date(t, t_units, only_use_cftime_datetimes=False)
    starti = t_py[0].date()
    tsi = len(t_py)

    ny, nx = len(lat), len(lon)
    all_prcp = np.zeros((ensemble, tsi, ny, nx), dtype=np.float32)

    # ---------------- state persistence (optional) ----------------
    state_dir = str(cfg.get("state_dir", "") or "").strip()
    region    = str(cfg.get("region", "default") or "default").strip()
    save_h    = float(cfg.get("state_save_hours", 6) or 6)
    max_age_h = float(cfg.get("state_max_age_hours", 6) or 6)
    state = None
    if state_dir:
        state = load_state(
            state_dir, region,
            expected_lat=np.asarray(lat),
            expected_lon=np.asarray(lon),
            expected_ensemble=ensemble,
            expected_tres=tres,
            max_age_hours=max_age_h,
        )

    if state_dir:
        n_save_steps = int(round(save_h / tres))
        all_q_tail = np.zeros((ensemble, n_save_steps, ny, nx), dtype=np.float16)
    else:
        all_q_tail = None

    cwd = os.getcwd()
    os.chdir(scratch_run)
    try:
        for ensid in range(ensemble):
            t0 = time.time()
            random.seed(1000 + ensid)
            np.random.seed(1000 + ensid)
            noise_name = f"{ensid}.noise_rt.nc"

            generateNoise(1, tsi, starti,
                          imerg_nc.name, mv_nc.name, "",
                          noise_name, tres)

            n_over = apply_state_to_noise(noise_name, state, ensid)
            if state is not None and n_over and ensid == 0:
                log.info("[state] member 0: overrode %d timesteps from saved state",
                         n_over)

            if all_q_tail is not None:
                with Dataset(noise_name) as nds:
                    q_full = nds.variables["q"][:]   # (1, time, lat, lon)
                tail = q_full[0, -all_q_tail.shape[1]:, :, :]
                all_q_tail[ensid] = np.asarray(tail, dtype=np.float16)

            simPrcp = simulatePrecip(starti, 1, tsi,
                                     imerg_nc.name, noise_name,
                                     csgd.name, 10, tres, verbose=False)
            all_prcp[ensid] = simPrcp[0].astype(np.float32)
            os.unlink(noise_name)
            log.info("  member %02d/%d  %.1fs", ensid, ensemble - 1,
                     time.time() - t0)
    finally:
        os.chdir(cwd)

    if state_dir and all_q_tail is not None:
        from netCDF4 import Dataset, date2num as _d2n
        tmp_state_nc = scratch_run / "state_input.nc"
        n_save_steps = all_q_tail.shape[1]
        tail_times = t_py[-n_save_steps:]
        with Dataset(str(tmp_state_nc), "w", format="NETCDF4") as snc:
            snc.createDimension("ens_n", ensemble)
            snc.createDimension("time", n_save_steps)
            snc.createDimension("lat", ny)
            snc.createDimension("lon", nx)
            tv = snc.createVariable("time", "d", ("time",))
            tv.units = t_units; tv.calendar = "gregorian"
            tv[:] = _d2n(list(tail_times), t_units, calendar="gregorian")
            latv = snc.createVariable("latitude", "f4", ("lat",), zlib=True)
            latv[:] = lat
            lonv = snc.createVariable("longitude", "f4", ("lon",), zlib=True)
            lonv[:] = lon
            qv = snc.createVariable("q", "f4",
                                    ("ens_n", "time", "lat", "lon"),
                                    zlib=True)
            qv[:, :, :, :] = all_q_tail.astype(np.float32)
        save_state(tmp_state_nc, state_dir, region,
                   save_hours=save_h, ensemble_size=ensemble, tres=tres)

    warmup_steps = int(round(warmup_hours / tres)) if warmup_hours else 0
    if warmup_steps > 0:
        if warmup_steps >= tsi:
            raise RuntimeError(
                f"warmup_hours ({warmup_hours} h) covers the entire window "
                f"({tsi * tres} h). Reduce warmup_hours or extend window_hours."
            )
        log.info("[trim] dropping first %d half-hourly steps (%.1f h) as AR(1) warm-up",
                 warmup_steps, warmup_steps * tres)
        all_prcp = all_prcp[:, warmup_steps:, :, :]
        t_py = t_py[warmup_steps:]
        tsi = len(t_py)

    region_dir = cfg["output_dir"] / region
    region_dir.mkdir(parents=True, exist_ok=True)

    n_saved, n_skipped = 0, 0
    last_saved_path = None
    for i, ts in enumerate(t_py):
        ts_str = ts.strftime("%Y%m%dT%H%M")
        out_path = region_dir / f"STREAMSat_{region}_{ts_str}_Ens{ensemble}.nc"
        if out_path.exists():
            n_skipped += 1
            continue
        time_val = date2num([ts], t_units, calendar="gregorian")
        with Dataset(str(out_path), "w", format="NETCDF4", clobber=False) as nc:
            nc.createDimension("ens_n", ensemble)
            nc.createDimension("time", 1)
            nc.createDimension("lat", ny)
            nc.createDimension("lon", nx)
            tv = nc.createVariable("time", "d", ("time",))
            tv.units = t_units
            tv.calendar = "gregorian"
            tv[:] = time_val
            latv = nc.createVariable("lat", "f4", ("lat",), zlib=True)
            latv.units = "degrees_north"
            latv[:] = lat
            lonv = nc.createVariable("lon", "f4", ("lon",), zlib=True)
            lonv.units = "degrees_east"
            lonv[:] = lon
            pv = nc.createVariable(
                "prcp", "f4", ("ens_n", "time", "lat", "lon"),
                zlib=True, complevel=4,
                chunksizes=(min(ensemble, 50), 1, ny, nx),
            )
            pv.units = "mm/hr"
            pv.long_name = ("STREAM-Sat real-time ensemble QPE — single "
                            "half-hourly timestep (upstream master + GFS "
                            "motion vectors, tres=0.5)")
            pv[:, 0, :, :] = all_prcp[:, i, :, :]
            nc.title = "STREAM-Sat real-time QPE (single timestep)"
            nc.region = region
            nc.timestep_utc = ts.isoformat(timespec="seconds")
            nc.generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
            nc.warmup_hours_dropped_from_run = float(warmup_hours)
        n_saved += 1
        last_saved_path = out_path

    log.info("[output] %s: %d new timesteps written, %d skipped (already on disk) → %s",
             region, n_saved, n_skipped, region_dir)
    return last_saved_path or region_dir

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Path to config.yaml")
    ap.add_argument("--end", default=None,
                    help="End datetime UTC (ISO 8601). Default: now − IMERG latency.")
    ap.add_argument("--ensemble", type=int, default=None,
                    help="Override ensemble_size from config.")
    ap.add_argument("--keep-scratch", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args()

    logging.basicConfig(level=a.log_level,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        cfg = load_config(Path(a.config))
    except Exception as e:
        log.error("config error: %s", e)
        sys.exit(2)

    ensemble = a.ensemble or int(cfg["ensemble_size"])
    window_hours = int(cfg["window_hours"])
    warmup_hours = int(cfg.get("warmup_hours", 0) or 0)
    extended_hours = window_hours + warmup_hours

    end = (datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc)
           if a.end else
           datetime.now(timezone.utc) - timedelta(hours=cfg.get("imerg_latency_hours", 4)))
    start = end - timedelta(hours=extended_hours)
    output_start = end - timedelta(hours=window_hours)
    log.info("Output window:   %s → %s (UTC)  [%d h]", output_start, end, window_hours)
    log.info("Ingest window:   %s → %s (UTC)  [%d h, includes %d h warm-up]",
             start, end, extended_hours, warmup_hours)
    log.info("Ensemble size:   %d", ensemble)
    log.info("STREAM-Sat dir:  %s", cfg["streamsat_dir"])

    scratch = Path(tempfile.mkdtemp(prefix="streamsat_rt_"))
    log.info("Scratch: %s", scratch)

    try:
        imerg = stage_fetch_imerg(cfg, end, scratch, hours=extended_hours)
    except Exception as e:
        log.error("IMERG ingest failed: %s", e); sys.exit(3)
    try:
        gfs = stage_fetch_gfs(cfg, start, end, scratch)
    except Exception as e:
        log.error("GFS ingest failed: %s", e); sys.exit(3)
    try:
        mv = stage_gfs_to_mv(cfg, gfs, imerg, scratch)
    except Exception as e:
        log.error("GFS→MV failed: %s", e); sys.exit(3)
    try:
        out_nc = stage_streamsat(cfg, imerg, mv, scratch, ensemble,
                                 warmup_hours=warmup_hours)
    except Exception as e:
        log.error("STREAM-Sat run failed: %s", e); sys.exit(4)
    finally:
        if not a.keep_scratch:
            shutil.rmtree(scratch, ignore_errors=True)

    log.info("SUCCESS → %s", out_nc)


if __name__ == "__main__":
    main()

"""
stream_sat_utils.py
==================
STREAM-Sat QPE integration for TITO.

Handles:
  1. Running the STREAM-Sat real-time pipeline to produce ensemble NetCDF files.
  2. Converting STREAM-Sat ensemble NetCDF → EF5-compatible GeoTIFFs (mm/h).
  3. Organizing GeoTIFFs into per-ensemble-member folders.

Usage (from orchestrator / prepare_precip)::

    from tito_utils.qpe_utils.stream_sat_utils import (
        run_streamsat_pipeline,
        convert_streamsat_nc_to_geotiffs,
        STREAMSAT_SCRIPT,
        STREAMSAT_REPO,
    )

Dependencies
------------
    netCDF4, numpy, osgeo (gdal), subprocess, concurrent.futures
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from osgeo import gdal, osr

log = logging.getLogger("streamsat-utils")

# ── Paths ────────────────────────────────────────────────────────────────
# STREAM-Sat lives next to this module under tito_utils/qpe_utils/
STREAMSAT_REPO = Path(__file__).resolve().parent / "STREAM-Sat-realtime"
STREAMSAT_SCRIPT = STREAMSAT_REPO / "extension" / "realtime" / "run_pipeline.py"

# Config files per domain
CONFIG_CARIBBEAN = STREAMSAT_REPO / "extension" / "config_caribbean.yaml"
CONFIG_COMOROS  = STREAMSAT_REPO / "extension" / "config_comoros.yaml"

# Output dir for GeoTIFFs
DEFAULT_TIF_ROOT = Path(__file__).resolve().parent.parent.parent / "precip" / "stream_sat"

# EF5-compatible fill value
FILL_VALUE = -9999.0
IMERG_FILL = 9.969209968386869e36


# ---------------------------------------------------------------------------
# Region → domain mapping
# ---------------------------------------------------------------------------
REGION_TO_DOMAIN = {
    "antigua":   "caribbean",
    "barbados":  "caribbean",
    "guatemala": "caribbean",
    "haiti":     "caribbean",
    "comoros":   "comoros",
}


def get_domain_for_region(region: str) -> str:
    r = region.strip().lower()
    if r in REGION_TO_DOMAIN:
        return REGION_TO_DOMAIN[r]
    raise ValueError(f"Unknown STREAM-Sat domain for region '{region}'")


def get_config_for_region(region: str) -> Path:
    domain = get_domain_for_region(region)
    if domain == "caribbean":
        return CONFIG_CARIBBEAN
    else:
        return CONFIG_COMOROS


# ---------------------------------------------------------------------------
# 1. Run STREAM-Sat pipeline
# ---------------------------------------------------------------------------

def run_streamsat_pipeline(
    region: str,
    ensemble_size: int = 10,
    window_hours: int = 48,
    warmup_hours: int = 12,
    end_dt: Optional[datetime] = None,
    keep_scratch: bool = False,
    timeout_seconds: int = 7200,
    pipeline_log: Any = None,
) -> Path:
    """Run the STREAM-Sat real-time pipeline for a given region.

    Parameters
    ----------
    region : str
        Region name (Antigua, Barbados, Comoros, Guatemala, Haiti).
    ensemble_size : int
        Number of ensemble members (default 10 for testing).
    window_hours : int
        Operational window duration in hours.
    warmup_hours : int
        AR(1) warm-up hours before the operational window.
    end_dt : datetime or None
        End datetime (UTC).  If None, uses "now minus IMERG latency".
    keep_scratch : bool
        Keep temporary scratch directory for debugging.
    timeout_seconds : int
        Max allowed runtime for the subprocess.

    Returns
    -------
    Path
        Path to the output directory containing STREAM-Sat NetCDF files.
    """
    config_path = get_config_for_region(region)
    domain = get_domain_for_region(region)

    if not STREAMSAT_SCRIPT.exists():
        raise FileNotFoundError(f"STREAM-Sat script not found: {STREAMSAT_SCRIPT}")
    if not config_path.exists():
        raise FileNotFoundError(f"STREAM-Sat config not found: {config_path}")

    # ── Pre-clean: DISABLED for pipeline persistence.
    #    STREAM-Sat NetCDFs must survive across cycles so post-processing
    #    scripts (streamsat_ensemble_tiles, streamsat_imerg_diff) can
    #    consume all timesteps after the hindcast completes.
    #    The pipeline already skips existing timestamp-specific files, so
    #    wiping the entire output dir is unnecessary for correctness.
    #    To re-enable, uncomment the block below.
    #
    # output_dir = STREAMSAT_REPO / "extension" / "realtime" / "output" / domain
    # alt_dir = STREAMSAT_REPO / "extension" / "realtime" / "extension" / "realtime" / "output" / domain
    # for d in (output_dir, alt_dir):
    #     if d.exists():
    #         for f in d.glob("*"):
    #             try:
    #                 f.unlink()
    #             except OSError:
    #                 pass
    # if pipeline_log:
    #     pipeline_log.info("[STREAM-Sat %s] Cleaned output dirs (state preserved)", domain)

    # Resolve output directory paths (needed for NC file discovery below)
    output_dir = STREAMSAT_REPO / "extension" / "realtime" / "output" / domain
    alt_dir = STREAMSAT_REPO / "extension" / "realtime" / "extension" / "realtime" / "output" / domain

    cmd = [
        sys.executable,
        str(STREAMSAT_SCRIPT),
        "--config", str(config_path),
        "--ensemble", str(ensemble_size),
        "--log-level", "INFO",
    ]

    if end_dt is not None:
        cmd.extend(["--end", end_dt.strftime("%Y-%m-%dT%H:%M")])

    if keep_scratch:
        cmd.append("--keep-scratch")

    from tito_utils.logging_utils import (
        debug_print, filter_streamsat_line, is_debug, is_user,
        progress_done, progress_line, user_print,
    )

    log.info("[STREAM-Sat %s] Running: %s", domain, " ".join(cmd))
    if is_debug():
        print(f"    [STREAM-Sat {domain}] cmd: {' '.join(cmd)}")
    else:
        user_print(f"    STREAM-Sat [{domain}]: starting ensemble={ensemble_size} …")
    if pipeline_log:
        pipeline_log.info("[STREAM-Sat %s] cmd: %s", domain, " ".join(cmd))
    t0 = time.time()

    # Stream stdout+stderr live so operators see IMERG/GFS/Semi-Lagrangian progress.
    # Full combined output is also written to pipeline_log.
    combined_chunks: List[str] = []
    sl_seen = 0
    in_progress = False
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(STREAMSAT_REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        deadline = t0 + float(timeout_seconds)
        while True:
            if time.time() > deadline:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd, timeout_seconds)
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            if not line:
                time.sleep(0.05)
                continue
            combined_chunks.append(line)
            # Console filter
            if "Running Semi-Lagrangian Scheme" in line:
                if in_progress:
                    progress_done()
                    in_progress = False
                sl_seen += 1
                user_print(
                    f"    Running Semi-Lagrangian Scheme "
                    f"(member {min(sl_seen, ensemble_size)}/{ensemble_size}) …"
                )
                continue
            shown = filter_streamsat_line(line)
            if shown is not None:
                # Avoid double-printing Semi-Lagrangian (handled above)
                if "Semi-Lagrangian" in shown:
                    continue
                # In-place progress lines (GFS winds / IMERG counters)
                if shown.startswith("\r"):
                    progress_line(shown.lstrip("\r"))
                    in_progress = True
                    continue
                if in_progress:
                    progress_done()
                    in_progress = False
                print(shown, flush=True)
            elif is_debug():
                if in_progress:
                    progress_done()
                    in_progress = False
                debug_print(line.rstrip("\n"))
        if in_progress:
            progress_done()
            in_progress = False
        rc = proc.wait(timeout=max(1, int(deadline - time.time())))
    except subprocess.TimeoutExpired:
        raise
    except Exception:
        raise

    elapsed = time.time() - t0
    combined = "".join(combined_chunks)

    if pipeline_log:
        if combined:
            pipeline_log.info("[STREAM-Sat %s] OUTPUT:\n%s", domain, combined[-50000:])
        pipeline_log.info("[STREAM-Sat %s] elapsed: %.1fs", domain, elapsed)

    if rc != 0:
        log.error("[STREAM-Sat %s] Failed (rc=%d) in %.1fs", domain, rc, elapsed)
        log.error("[STREAM-Sat %s] OUTPUT tail: %s", domain, combined[-2000:])
        raise RuntimeError(
            f"STREAM-Sat pipeline failed for {domain} (exit {rc})"
        )

    user_print(f"    STREAM-Sat [{domain}]: completed in {elapsed:.0f}s")
    log.info("[STREAM-Sat %s] Completed in %.1fs", domain, elapsed)

    # Determine which output directory got the fresh NC files
    found = None
    for candidate in (output_dir, alt_dir):
        if candidate.exists() and list(candidate.glob("*.nc")):
            found = candidate
            break
    if found is None:
        raise FileNotFoundError(
            f"STREAM-Sat output directory not found. Expected: {output_dir} or {alt_dir}"
        )
    output_dir = found

    return output_dir


# ---------------------------------------------------------------------------
# 2. NetCDF → GeoTIFF conversion
# ---------------------------------------------------------------------------

def parse_nc_timestamp(nc_path: str) -> Optional[str]:
    """Extract YYYYMMDDHHUU from STREAMSat_<region>_YYYYMMDDTHHUU_EnsN.nc"""
    m = re.search(r"_(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})_", str(nc_path))
    if not m:
        return None
    return m.group(1) + m.group(2) + m.group(3) + m.group(4) + m.group(5)


def nc_member_to_geotiff(
    nc_path: str,
    member_idx: int,
    output_path: str,
    divide_by: float = 1.0,
) -> Tuple[bool, str]:
    """Extract one ensemble member from a STREAM-Sat netCDF → GeoTIFF.

    Parameters
    ----------
    nc_path : str
        Path to STREAM-Sat ensemble netCDF file.
    member_idx : int
        0-based ensemble member index.
    output_path : str
        Destination GeoTIFF path.
    divide_by : float
        Conversion factor.  1.0 = keep mm/h (EF5 supports this).
        Use 2.0 if converting mm/h → mm/30min.

    Returns
    -------
    (success: bool, message: str)
    """
    import netCDF4 as nc

    ds = nc.Dataset(nc_path, "r")
    try:
        prcp = ds["prcp"][member_idx, 0, :, :]  # (lat, lon)
    except IndexError:
        ds.close()
        return False, f"Member {member_idx} out of range for {nc_path}"

    # Get lat/lon
    lat_var = None
    lon_var = None
    for vname in ["lat", "latitude"]:
        if vname in ds.variables:
            lat_var = vname
            break
    for vname in ["lon", "longitude"]:
        if vname in ds.variables:
            lon_var = vname
            break

    if lat_var is None or lon_var is None:
        ds.close()
        return False, f"Cannot find lat/lon variables in {nc_path}"

    lats = ds[lat_var][:]
    lons = ds[lon_var][:]

    # Determine lat direction (north→south or south→north)
    lat_ascending = len(lats) > 1 and lats[1] > lats[0]

    # Convert to numpy
    prcp_out = prcp.astype(np.float64) / float(divide_by)

    # Replace fill values
    prcp_out = np.where(np.isclose(prcp_out, IMERG_FILL / divide_by), FILL_VALUE, prcp_out)
    prcp_out = np.where(np.isnan(prcp_out), FILL_VALUE, prcp_out)
    prcp_out = np.where(prcp_out < -9000, FILL_VALUE, prcp_out)

    ds.close()

    # If lat is ascending (S→N), flip to N→S for GDAL
    if lat_ascending:
        prcp_out = np.flipud(prcp_out)
        ul_lat = float(lats[-1])
        lat_res = float(lats[1] - lats[0]) if len(lats) > 1 else 0.1
    else:
        ul_lat = float(lats[0])
        lat_res = float(lats[1] - lats[0]) if len(lats) > 1 else -0.1

    ul_lon = float(lons[0])
    lon_res = float(lons[1] - lons[0]) if len(lons) > 1 else 0.1

    ny, nx = prcp_out.shape

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(
        output_path, nx, ny, 1, gdal.GDT_Float32,
        options=["COMPRESS=LZW", "TILED=NO", "BIGTIFF=IF_SAFER"],
    )
    out_ds.SetGeoTransform([ul_lon, lon_res, 0, ul_lat, 0, lat_res])
    srs = osr.SpatialReference()
    srs.SetWellKnownGeogCS("WGS84")
    out_ds.SetProjection(srs.ExportToWkt())
    band = out_ds.GetRasterBand(1)
    band.WriteArray(prcp_out.astype(np.float32))
    band.SetNoDataValue(FILL_VALUE)
    band.FlushCache()
    out_ds = None

    return True, output_path


def _convert_one_nc_file(
    nc_path: str,
    dest_root: str,
    n_ensemble: int,
    divide_by: float = 1.0,
    tif_naming: str = "streamsat",
) -> List[Tuple[int, bool, str]]:
    """Convert all ensemble members from one netCDF file.

    Returns list of (member_idx, success, path_or_error).
    """
    timestamp = parse_nc_timestamp(nc_path)
    if timestamp is None:
        return [(i, False, f"Cannot parse timestamp from {nc_path}") for i in range(n_ensemble)]

    results = []
    for member in range(n_ensemble):
        out_name = f"{tif_naming}.qpe.{timestamp}.mmhInst.tif"
        member_dir = os.path.join(dest_root, f"ensP{member + 1}")
        os.makedirs(member_dir, exist_ok=True)
        out_path = os.path.join(member_dir, out_name)

        # Skip if already exists and non-zero
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            results.append((member, True, out_path))
            continue

        success, msg = nc_member_to_geotiff(nc_path, member, out_path, divide_by)
        results.append((member, success, msg))

    return results


# ---------------------------------------------------------------------------
# 3. Main entry point: run + convert
# ---------------------------------------------------------------------------

def convert_streamsat_nc_to_geotiffs(
    nc_output_dir: str,
    tif_dest_root: str,
    n_ensemble: int = 10,
    divide_by: float = 1.0,
    max_workers: Optional[int] = None,
    dry_run: bool = False,
    tif_naming: str = "streamsat",
    max_end_dt: Optional[datetime] = None,
) -> int:
    """Convert all STREAM-Sat NetCDF files → per-member GeoTIFFs.

    Parameters
    ----------
    nc_output_dir : str
        Directory containing STREAM-Sat NetCDF files
        (e.g. ``STREAMSat_caribbean_20260510T1030_Ens10.nc``).
    tif_dest_root : str
        Root directory where ``ensP1/`` … ``ensPN/`` folders will be created.
    n_ensemble : int
        Number of ensemble members per file.
    divide_by : float
        Unit conversion factor (1.0 = keep mm/h).
    max_workers : int or None
        Max parallel workers for conversion.
    dry_run : bool
        If True, only preview what would be done.
    tif_naming : str
        Prefix for GeoTIFF filenames (e.g. "streamsat").
    max_end_dt : datetime or None
        If set, only convert NetCDFs with timestamp <= max_end_dt
        (needed for hindcast so leftover operational NCs are ignored).

    Returns
    -------
    int
        Number of GeoTIFF files created.
    """
    ncs = sorted(glob.glob(os.path.join(nc_output_dir, "*.nc")))
    if not ncs:
        log.warning("No STREAM-Sat NetCDF files found in %s", nc_output_dir)
        return 0

    if max_end_dt is not None:
        kept = []
        skipped = 0
        for nc_path in ncs:
            ts_str = parse_nc_timestamp(nc_path)
            if not ts_str:
                skipped += 1
                continue
            try:
                ts = datetime.strptime(ts_str, "%Y%m%d%H%M")
            except ValueError:
                skipped += 1
                continue
            if ts <= max_end_dt:
                kept.append(nc_path)
            else:
                skipped += 1
        log.info(
            "NC filter max_end=%s: keep %d / skip %d (leftover future files)",
            max_end_dt.strftime("%Y%m%d%H%M"), len(kept), skipped,
        )
        ncs = kept
        if not ncs:
            log.warning(
                "No STREAM-Sat NetCDFs <= %s in %s",
                max_end_dt.strftime("%Y%m%d%H%M"), nc_output_dir,
            )
            return 0

    total_tifs = len(ncs) * n_ensemble
    log.info(
        "Converting %d STREAM-Sat NetCDFs → %d GeoTIFFs (divide_by=%.1f)",
        len(ncs), total_tifs, divide_by,
    )

    if dry_run:
        log.info("[DRY RUN] Would create %d GeoTIFFs in %s/ensP1..ensP%d",
                 total_tifs, tif_dest_root, n_ensemble)
        return 0

    os.makedirs(tif_dest_root, exist_ok=True)

    n_workers = max_workers or min(os.cpu_count() or 4, 8)
    ok = fail = 0
    t_start = time.time()

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        fut_map = {}
        for nc_path in ncs:
            f = executor.submit(
                _convert_one_nc_file,
                nc_path, tif_dest_root, n_ensemble, divide_by, tif_naming,
            )
            fut_map[f] = nc_path

        done = 0
        for f in as_completed(fut_map):
            nc_path = fut_map[f]
            results = f.result()
            for _member, success, _msg in results:
                if success:
                    ok += 1
                else:
                    fail += 1
            done += 1
            if done % 20 == 0 or done == len(ncs):
                elapsed = time.time() - t_start
                rate = ok / elapsed if elapsed > 0 else 0
                log.info(
                    "  [%d/%d NCs] %d OK, %d fail, %.0f tifs/sec",
                    done, len(ncs), ok, fail, rate,
                )

    elapsed = time.time() - t_start
    log.info("STREAM-Sat conversion done: %d OK, %d failed in %.0fs", ok, fail, elapsed)

    if fail > 0:
        raise RuntimeError(f"{fail} STREAM-Sat GeoTIFF conversions failed")

    return ok


def run_and_convert_streamsat(
    region: str,
    ensemble_size: int = 10,
    window_hours: int = 48,
    warmup_hours: int = 12,
    end_dt: Optional[datetime] = None,
    tif_root: Optional[str] = None,
    divide_by: float = 1.0,
    max_workers: Optional[int] = None,
    keep_scratch: bool = False,
    tif_naming: str = "streamsat",
    pipeline_log: Any = None,
) -> Dict[str, str]:
    """Run STREAM-Sat pipeline AND convert results to GeoTIFFs.

    This is the main entry point called by the orchestrator.
    Runs the pipeline, then converts all output NetCDFs to per-member GeoTIFFs.

    Parameters
    ----------
    region : str
        Region name.
    ensemble_size : int
        Number of ensemble members.
    window_hours : int
        Operational window in hours.
    warmup_hours : int
        AR(1) warm-up hours.
    end_dt : datetime or None
        End time (UTC).
    tif_root : str or None
        Root for GeoTIFF output. Defaults to ``precip/stream_sat/``.
    divide_by : float
        Conversion factor (1.0 = mm/h).
    max_workers : int or None
        Parallel workers for conversion.
    keep_scratch : bool
        Keep STREAM-Sat scratch directory.
    tif_naming : str
        GeoTIFF filename prefix.

    Returns
    -------
    dict
        Keys:
        - "nc_output_dir": Path to STREAM-Sat NetCDF output
        - "tif_root": Path to GeoTIFF root (contains ensP1..ensPN)
        - "ensemble_size": Number of ensemble members
    """
    # 1. Run STREAM-Sat
    t_pipe0 = time.time()
    nc_dir = run_streamsat_pipeline(
        region=region,
        ensemble_size=ensemble_size,
        window_hours=window_hours,
        warmup_hours=warmup_hours,
        end_dt=end_dt,
        keep_scratch=keep_scratch,
        pipeline_log=pipeline_log,
    )
    pipeline_s = time.time() - t_pipe0

    # 2. Convert NC → GeoTIFF
    dest = tif_root or str(DEFAULT_TIF_ROOT)
    os.makedirs(dest, exist_ok=True)

    t_conv0 = time.time()
    n_converted = convert_streamsat_nc_to_geotiffs(
        nc_output_dir=str(nc_dir),
        tif_dest_root=dest,
        n_ensemble=ensemble_size,
        divide_by=divide_by,
        max_workers=max_workers,
        tif_naming=tif_naming,
        max_end_dt=end_dt,
    )
    convert_s = time.time() - t_conv0
    total_s = pipeline_s + convert_s

    log.info(
        "STREAM-Sat + conversion complete: %d GeoTIFFs in %s "
        "(pipeline=%.1fs convert=%.1fs total=%.1fs)",
        n_converted, dest, pipeline_s, convert_s, total_s,
    )
    if pipeline_log:
        pipeline_log.info(
            "STREAM-Sat + conversion complete: %d GeoTIFFs in %s "
            "(pipeline=%.1fs convert=%.1fs total=%.1fs)",
            n_converted, dest, pipeline_s, convert_s, total_s,
        )

    return {
        "nc_output_dir": str(nc_dir),
        "tif_root": dest,
        "ensemble_size": ensemble_size,
        "pipeline_s": pipeline_s,
        "convert_s": convert_s,
        "elapsed_s": total_s,
        "domain": get_domain_for_region(region),
    }


# ---------------------------------------------------------------------------
# 4. Get per-member precip folder paths
# ---------------------------------------------------------------------------

def get_ensemble_precip_folders(tif_root: str, ensemble_size: int) -> List[str]:
    """Return a list of per-member precip folder paths.

    Parameters
    ----------
    tif_root : str
        Root directory containing ensP1/ ... ensPN/.
    ensemble_size : int
        Number of ensemble members.

    Returns
    -------
    list of str
        Sorted paths to each member's precip folder.
    """
    folders = []
    for i in range(1, ensemble_size + 1):
        d = os.path.join(tif_root, f"ensP{i}")
        if os.path.isdir(d):
            folders.append(os.path.join(d, ""))  # trailing separator
    return folders


def get_streamsat_time_range(nc_output_dir: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Determine the time range of STREAM-Sat NetCDF output files.

    Returns (start_dt, end_dt) as datetimes (UTC, naive).
    """
    ncs = sorted(glob.glob(os.path.join(nc_output_dir, "*.nc")))
    if not ncs:
        return None, None

    timestamps = []
    for nc_path in ncs:
        ts_str = parse_nc_timestamp(nc_path)
        if ts_str:
            try:
                timestamps.append(datetime.strptime(ts_str, "%Y%m%d%H%M"))
            except ValueError:
                pass

    if not timestamps:
        return None, None

    return min(timestamps), max(timestamps)

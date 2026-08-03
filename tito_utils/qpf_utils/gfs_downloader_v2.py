#!/usr/bin/env python3
"""
Herbie-based GFS precipitation downloader and GeoTIFF converter — V2.

┌─ Deployment ─────────────────────────────────────────────────────────────┐
│ Crontab (@reboot):                                                        │
│   @reboot sleep 30 && cd .../qpf_utils && while true; do                  │
│     python gfs_downloader_v2.py --auto-out .../precip/gfs                 │
│       >> .../logs/gfs_downloader.log 2>&1; sleep 10; done                 │
│                                                                           │
│ Auto-restart:     while-true loop in cron restarts on any crash.          │
│ DNS fallback:     Uses Google DNS (8.8.8.8) if university DNS fails.     │
│ Retry strategy:   Exponential backoff (5s→10s→20s→40s) for DNS errors.   │
│ Fallback cycles:  If current GFS cycle has no data, tries previous cycles │
│                   up to 4 cycles back (24h).                              │
│ Parallel:         Downloads forecast hours concurrently (default: 4).     │
│ Conda env:        tito_env2                                               │
│ Logs:             ~/TITOCubaMain/TITOCuba/data/logs/gfs_downloader.log    │
└──────────────────────────────────────────────────────────────────────────┘

Improvements over v1:
- Parallel downloads via ThreadPoolExecutor
- Previous-cycle fallback works in continuous auto mode (not just one-shot)
- Cleaner separation: DNS fallback, retry, download, auto-mode are isolated
- Same CLI interface as v1
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Fix PROJ database path — must be set BEFORE importing rioxarray/rasterio.
# The base conda has an old proj.db; use the env's copy.
# ---------------------------------------------------------------------------
_PROJ_ENV_DIR = os.path.join(os.path.dirname(os.path.dirname(sys.executable)), 'share', 'proj')
if os.path.isdir(_PROJ_ENV_DIR):
    os.environ['PROJ_DATA'] = _PROJ_ENV_DIR
    os.environ['PROJ_LIB'] = _PROJ_ENV_DIR

import numpy as np
import xarray as xr
import rioxarray  # noqa: F401

try:
    from herbie import Herbie
except Exception:
    raise ImportError("Herbie is required. Install with `pip install herbie-data`")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AUTO_OUT_DIR = ""
AUTO_BBOX = (-180.0, 180.0, -90.0, 90.0)
AUTO_HOURS = 120
AUTO_POLL_SECONDS = 3600
AUTO_CYCLE_GRACE_MINUTES = 120
MAX_CYCLES_BACK = 4          # try up to 24h of previous cycles
PARALLEL_WORKERS = 4         # concurrent downloads
DNS_RETRY_MAX = 4
DNS_RETRY_BASE_DELAY = 5.0
DOWNLOAD_TIMEOUT = 300       # per-forecast-hour timeout (seconds)
CYCLE_TIMEOUT = 3600         # max time for a full cycle download (seconds)
SOCKET_TIMEOUT = 60          # low-level socket timeout (seconds)

# ---------------------------------------------------------------------------
# DNS fallback (same as v1)
# ---------------------------------------------------------------------------
_original_getaddrinfo = socket.getaddrinfo
_noaa_ip_cache: Optional[str] = None


def _resolve_noaa_public() -> Optional[str]:
    for dns in ('8.8.8.8', '1.1.1.1'):
        try:
            r = subprocess.run(['dig', f'@{dns}', '+short', 'nomads.ncep.noaa.gov'],
                               capture_output=True, text=True, timeout=10)
            for line in r.stdout.strip().split('\n'):
                for part in line.strip().rstrip('.').split():
                    try:
                        socket.inet_pton(socket.AF_INET, part)
                        sys.stderr.write(f"[dns] nomads.ncep.noaa.gov → {part} via {dns}\n")
                        return part
                    except OSError:
                        continue
        except Exception:
            continue
    return None


def _patched_getaddrinfo(host, *args, **kwargs):
    if host and 'ncep.noaa.gov' in str(host) and _noaa_ip_cache:
        return _original_getaddrinfo(_noaa_ip_cache, *args, **kwargs)
    return _original_getaddrinfo(host, *args, **kwargs)


def _dns_fallback_enable() -> bool:
    global _noaa_ip_cache
    if _noaa_ip_cache is None:
        _noaa_ip_cache = _resolve_noaa_public()
    if _noaa_ip_cache:
        socket.getaddrinfo = _patched_getaddrinfo
        return True
    return False


def _dns_fallback_disable():
    socket.getaddrinfo = _original_getaddrinfo


def _is_dns_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(kw in msg for kw in (
        'name resolution', 'servfail', 'temporary failure',
        'name or service not known', 'nxdomain',
    ))


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------
def _retry_call(fn, max_retries: int = DNS_RETRY_MAX, base_delay: float = DNS_RETRY_BASE_DELAY):
    """Call fn() with exponential backoff on DNS errors. Enables DNS fallback."""
    dns_fallback = False
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            result = fn()
            if dns_fallback:
                _dns_fallback_disable()
            return result
        except Exception as e:
            last_err = e
            if not _is_dns_error(e) or attempt >= max_retries:
                if dns_fallback:
                    _dns_fallback_disable()
                raise
            if not dns_fallback:
                dns_fallback = _dns_fallback_enable()
                if dns_fallback:
                    sys.stderr.write("[retry] DNS fallback enabled, retrying immediately...\n")
                    continue
            delay = base_delay * (2 ** attempt)
            sys.stderr.write(f"[retry] DNS error, attempt {attempt+1}/{max_retries+1}, sleeping {delay:.0f}s: {e}\n")
            time.sleep(delay)
    raise last_err  # type: ignore


# ---------------------------------------------------------------------------
# GFS helpers
# ---------------------------------------------------------------------------
def _gfs_cycle(dt: datetime) -> datetime:
    """Snap to most recent GFS cycle (00, 06, 12, 18 UTC)."""
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    base = dt.replace(minute=0, second=0, microsecond=0)
    while base.hour % 6 != 0:
        base -= timedelta(hours=1)
    return base


def _forecast_hours(max_hours: int) -> List[int]:
    """GFS 0.25°: hourly 0..120, then 3-hourly beyond."""
    if max_hours <= 0:
        return []
    limit = min(max_hours, 384)
    if limit <= 120:
        return list(range(0, limit + 1))
    return list(range(0, 121)) + list(range(123, limit + 1, 3))


def _standardize(da: xr.DataArray) -> xr.DataArray:
    """Standardize lat/lon dims and set CRS=EPSG:4326."""
    da = da.squeeze(drop=True)
    # Map dims
    rename = {}
    for d in da.dims:
        dl = d.lower()
        if dl in ('latitude', 'y') and 'lat' not in da.dims:
            rename[d] = 'lat'
        if dl in ('longitude', 'x') and 'lon' not in da.dims:
            rename[d] = 'lon'
    if rename:
        da = da.rename(rename)
    # Ensure coords
    if 'lat' not in da.coords and 'latitude' in da.coords:
        da = da.rename({'latitude': 'lat'})
    if 'lon' not in da.coords and 'longitude' in da.coords:
        da = da.rename({'longitude': 'lon'})
    # Wrap lon
    if 'lon' in da.coords:
        lon = da.coords['lon'].values
        if np.nanmax(lon) > 180:
            da = da.assign_coords(lon=("lon", ((lon + 180) % 360) - 180))
            da = da.sortby('lon')
    da = da.rio.write_crs("EPSG:4326", inplace=False)
    da = da.rio.set_spatial_dims(x_dim="lon", y_dim="lat", inplace=False)
    return da


def _write_tiff(da: xr.DataArray, path: str):
    """Write float32 GeoTIFF with nodata=-9999."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = np.where(np.isnan(da.data.astype(np.float32)), -9999.0, da.data.astype(np.float32))
    out = xr.DataArray(data, dims=da.dims, coords=da.coords,
                       name=da.name or "PRATE_mm_hr", attrs={"units": "mm/h"})
    out.rio.write_nodata(-9999.0, inplace=True)
    out.rio.to_raster(path, driver="GTiff", dtype="float32")


# ---------------------------------------------------------------------------
# Single forecast-hour download
# ---------------------------------------------------------------------------
def _download_one_fxx(init_time: datetime, fxx: int,
                       xmin: float, xmax: float, ymin: float, ymax: float,
                       out_dir: str) -> Optional[str]:
    """Download one forecast hour. Returns output path or None on failure."""
    valid_time = init_time + timedelta(hours=fxx)
    out_path = os.path.join(out_dir, f"gfs.{valid_time:%Y%m%d%H%M}.tif")

    # Set socket timeout to prevent hung connections
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    try:
        try:
            H = _retry_call(lambda: Herbie(init_time, model="gfs", product="pgrb2.0p25", fxx=fxx))
        except Exception as e:
            sys.stderr.write(f"[WARN] Herbie init f{fxx:03d}: {e}\n")
            return None

        ds = None
        for query in (":PRATE:surface", ":PRATE:", "PRATE:surface", "PRATE"):
            try:
                ds = _retry_call(lambda: H.xarray(query))
                break
            except Exception:
                continue

        if ds is None:
            sys.stderr.write(f"[WARN] No PRATE f{fxx:03d} ({valid_time:%Y-%m-%d %H:%M})\n")
            return None

        # Extract variable
        if isinstance(ds, list):
            ds = ds[0]
        var = next((v for v in ('prate', 'PRATE') if v in ds.data_vars), None)
        if var is None:
            var = list(ds.data_vars)[0]

        prate = _standardize(ds[var])
        rate = prate.data.astype(np.float32)
        if rate.ndim == 3:
            rate = np.squeeze(rate, axis=0)

        step = xr.DataArray(rate * 3600.0, dims=("lat", "lon"),
                            coords={"lat": prate.coords["lat"], "lon": prate.coords["lon"]},
                            name="PRATE_mm_per_hour", attrs={"units": "mm/hour"})
        step = step.rio.write_crs("EPSG:4326", inplace=False)
        step = step.rio.set_spatial_dims(x_dim="lon", y_dim="lat", inplace=False)

        try:
            step = step.rio.clip_box(minx=xmin, miny=ymin, maxx=xmax, maxy=ymax)
        except Exception:
            pass

        _write_tiff(step, out_path)
        sys.stderr.write(f"[OK] f{fxx:03d} → {os.path.basename(out_path)}\n")
        return out_path
    finally:
        socket.setdefaulttimeout(old_timeout)


# ---------------------------------------------------------------------------
# Download full cycle (parallel)
# ---------------------------------------------------------------------------
def download_cycle(cycle: datetime, hours: int,
                   xmin: float, xmax: float, ymin: float, ymax: float,
                   out_dir: str, workers: int = PARALLEL_WORKERS) -> List[str]:
    """Download all forecast hours for one GFS cycle in parallel. Returns list of output paths."""
    fxx_list = _forecast_hours(hours)
    sys.stderr.write(f"[cycle] {cycle:%Y-%m-%d %H}z — {len(fxx_list)} forecast hours, {workers} workers\n")

    results: List[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_download_one_fxx, cycle, fxx, xmin, xmax, ymin, ymax, out_dir): fxx
            for fxx in fxx_list
        }
        deadline = time.monotonic() + CYCLE_TIMEOUT
        for future in as_completed(futures):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                sys.stderr.write(f"[cycle] Cycle timeout reached, cancelling remaining futures\n")
                for f in futures:
                    f.cancel()
                break
            fxx = futures[future]
            try:
                path = future.result(timeout=remaining)
                if path:
                    results.append(path)
            except Exception as e:
                sys.stderr.write(f"[FAIL] f{fxx:03d}: {e}\n")

    sys.stderr.write(f"[cycle] {cycle:%Y-%m-%d %H}z — wrote {len(results)}/{len(fxx_list)} files\n")
    return results


# ---------------------------------------------------------------------------
# Auto mode
# ---------------------------------------------------------------------------
def auto_mode(out_dir: str, hours: int = AUTO_HOURS,
              poll_seconds: int = AUTO_POLL_SECONDS,
              workers: int = PARALLEL_WORKERS,
              max_back: int = MAX_CYCLES_BACK):
    """Continuous polling mode. Tries latest cycle, falls back to previous cycles if no data."""
    xmin, xmax, ymin, ymax = AUTO_BBOX
    os.makedirs(out_dir, exist_ok=True)
    last_successful_cycle: Optional[datetime] = None

    while True:
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            latest = _gfs_cycle(now)

            # Respect grace period
            if now <= latest + timedelta(minutes=AUTO_CYCLE_GRACE_MINUTES):
                start_cycle = latest - timedelta(hours=6)
                sys.stderr.write(f"[auto] Within grace period of {latest:%H}z, starting from {start_cycle:%Y-%m-%d %H}z\n")
            else:
                start_cycle = latest

            # Try cycles: start_cycle, start_cycle-6h, start_cycle-12h, ...
            success = False
            for back in range(max_back + 1):
                cycle = start_cycle - timedelta(hours=6 * back)

                # Only skip if this cycle is already fully downloaded
                expected_files = _forecast_hours(hours)
                n_expected = len(expected_files)
                existing = [f for f in os.listdir(out_dir) if f.startswith("gfs.") and f.endswith(".tif")]
                cycle_prefix = f"gfs.{cycle:%Y%m%d}"
                cycle_existing = [f for f in existing if f.startswith(cycle_prefix)]
                if last_successful_cycle and cycle <= last_successful_cycle and len(cycle_existing) >= n_expected:
                    sys.stderr.write(f"[auto] Skipping {cycle:%Y-%m-%d %H}z (already complete: {len(cycle_existing)}/{n_expected})\n")
                    continue

                sys.stderr.write(f"[auto] Trying cycle {cycle:%Y-%m-%d %H}z (back={back})...\n")

                # Download into .staging — do NOT touch existing files yet
                staging = os.path.join(out_dir, ".staging")
                if os.path.isdir(staging):
                    for f in os.listdir(staging):
                        try:
                            os.remove(os.path.join(staging, f))
                        except Exception:
                            pass
                os.makedirs(staging, exist_ok=True)

                results = download_cycle(cycle, hours, xmin, xmax, ymin, ymax, staging, workers)

                # Only promote if we got ALL expected files
                if len(results) >= n_expected:
                    # Clear old files from previous cycle, then promote new ones
                    for f in os.listdir(out_dir):
                        if f.startswith("gfs.") and f.endswith(".tif"):
                            try:
                                os.remove(os.path.join(out_dir, f))
                            except Exception:
                                pass
                    for f in os.listdir(staging):
                        shutil.move(os.path.join(staging, f), os.path.join(out_dir, f))
                    try:
                        os.rmdir(staging)
                    except Exception:
                        pass

                    last_successful_cycle = cycle
                    sys.stderr.write(f"[auto] ✅ Cycle {cycle:%Y-%m-%d %H}z promoted — {len(results)}/{n_expected} files\n")
                    success = True
                    break
                else:
                    sys.stderr.write(f"[auto] ⚠ Only {len(results)}/{n_expected} files for {cycle:%Y-%m-%d %H}z — waiting for NOAA\n")
                    # Clean up staging, keep existing files untouched
                    for f in os.listdir(staging):
                        try:
                            os.remove(os.path.join(staging, f))
                        except Exception:
                            pass

            if not success:
                sys.stderr.write(f"[auto] ⚠ No complete cycle available, will retry after poll\n")

            if not success:
                sys.stderr.write(f"[auto] ⚠ All {max_back+1} cycles failed, will retry after poll\n")

        except Exception as e:
            sys.stderr.write(f"[auto] Error: {e}\n")

        try:
            time.sleep(poll_seconds)
        except KeyboardInterrupt:
            sys.stderr.write("[auto] Stopped by user.\n")
            return


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="GFS PRATE downloader V2 — parallel + cycle fallback")
    # Manual mode
    p.add_argument("--start", help="Cycle start (e.g. '2025-12-01 12')")
    p.add_argument("--end", help="End valid time")
    p.add_argument("--xmin", type=float); p.add_argument("--xmax", type=float)
    p.add_argument("--ymin", type=float); p.add_argument("--ymax", type=float)
    p.add_argument("--out", help="Output dir")
    # Auto mode
    p.add_argument("--auto-out", help="Auto mode output dir")
    p.add_argument("--auto-hours", type=int, default=AUTO_HOURS)
    p.add_argument("--poll-seconds", type=int, default=AUTO_POLL_SECONDS)
    p.add_argument("--workers", type=int, default=PARALLEL_WORKERS, help="Parallel workers")
    p.add_argument("--max-back", type=int, default=MAX_CYCLES_BACK, help="Max previous cycles to try")
    p.add_argument("--auto-once", action="store_true", help="Single pass (no polling)")
    args = p.parse_args()

    # Manual one-shot mode
    if args.start and args.end and args.out:
        cycle = _gfs_cycle(datetime.strptime(args.start.strip(), "%Y-%m-%d %H"))
        end = datetime.strptime(args.end.strip(), "%Y-%m-%d %H")
        hours = int((end - cycle).total_seconds() / 3600)
        results = download_cycle(cycle, hours,
                                 args.xmin or -180, args.xmax or 180,
                                 args.ymin or -90, args.ymax or 90,
                                 args.out, args.workers)
        print(f"Wrote {len(results)} files to {args.out}")

    # Auto mode
    elif args.auto_out:
        if args.auto_once:
            # One-shot: try cycles until we get data
            xmin, xmax, ymin, ymax = AUTO_BBOX
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            latest = _gfs_cycle(now)
            if now <= latest + timedelta(minutes=AUTO_CYCLE_GRACE_MINUTES):
                start_cycle = latest - timedelta(hours=6)
            else:
                start_cycle = latest

            os.makedirs(args.auto_out, exist_ok=True)
            success = False
            for back in range(args.max_back + 1):
                cycle = start_cycle - timedelta(hours=6 * back)
                staging = os.path.join(args.auto_out, ".staging")
                os.makedirs(staging, exist_ok=True)
                results = download_cycle(cycle, args.auto_hours, xmin, xmax, ymin, ymax, staging, args.workers)
                if results:
                    for f in os.listdir(args.auto_out):
                        if f.startswith("gfs.") and f.endswith(".tif"):
                            os.remove(os.path.join(args.auto_out, f))
                    for f in os.listdir(staging):
                        shutil.move(os.path.join(staging, f), os.path.join(args.auto_out, f))
                    print(f"Wrote {len(results)} files for cycle {cycle:%Y-%m-%d %H}z")
                    success = True
                    break
                else:
                    sys.stderr.write(f"No data for {cycle:%Y-%m-%d %H}z\n")
            if not success:
                sys.stderr.write("All cycles failed.\n")
                sys.exit(2)
        else:
            auto_mode(args.auto_out, args.auto_hours, args.poll_seconds, args.workers, args.max_back)
    else:
        p.print_help()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
download_gfs_winds.py
=====================
Downloads GFS 850 hPa U/V wind components for STREAM-Sat motion vectors.

Author: Yagmur Derin
"""

import os
import sys
import argparse
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from pathlib import Path

import numpy as np
import requests
import xarray as xr
from scipy.interpolate import RegularGridInterpolator
from netCDF4 import Dataset, date2num

# ============================================================================
# CONFIGURATION
# ============================================================================

# Caribbean domain (matching your STREAM-Sat target region)
DOMAIN = {
    "lat_min": 5.0,
    "lat_max": 25.0,
    "lon_min": -85.0,
    "lon_max": -55.0,
}

# Output grid: 0.1° to match IMERG / STREAM-Sat
OUT_RES = 0.1

# Events to download
EVENTS = {
    "barbados": {
        "name": "Barbados Event",
        "start": date(2025, 11, 16),
        "end": date(2025, 11, 17),
    },
    "tammy": {
        "name": "Hurricane Tammy",
        "start": date(2023, 10, 18),
        "end": date(2023, 10, 28),
    },
}

# GFS initialization hours
GFS_CYCLES = [0, 6, 12, 18]

# Output directory
# If running in a Jupyter notebook, __file__ won't exist — use Path.cwd() instead
try:
    _base = Path(__file__).parent
except NameError:
    _base = Path.cwd()  # works in Jupyter notebooks

OUTPUT_DIR = _base / "gfs_wind_data"

# Temporary GRIB download directory
TEMP_DIR = _base / "gfs_wind_data" / "temp_grib"

# ============================================================================
# DOWNLOAD FUNCTIONS
# ============================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def nomads_grib_filter_url(dt, cycle_hr, fxx,
                           lat_min, lat_max, lon_min, lon_max):

    date_str = dt.strftime("%Y%m%d")
    return (
        f"https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?"
        f"dir=%2Fgfs.{date_str}%2F{cycle_hr:02d}%2Fatmos&"
        f"file=gfs.t{cycle_hr:02d}z.pgrb2.0p25.f{fxx:03d}&"
        f"lev_850_mb=on&var_UGRD=on&var_VGRD=on&"
        f"subregion=&toplat={lat_max}&leftlon={lon_min}&"
        f"rightlon={lon_max}&bottomlat={lat_min}"
    )


def aws_url(dt, cycle_hr, fxx):
    date_str = dt.strftime("%Y%m%d")
    return (
        f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
        f"gfs.{date_str}/{cycle_hr:02d}/atmos/"
        f"gfs.t{cycle_hr:02d}z.pgrb2.0p25.f{fxx:03d}"
    )


def gcs_url(dt, cycle_hr, fxx):
    date_str = dt.strftime("%Y%m%d")
    return (
        f"https://storage.googleapis.com/global-forecast-system/"
        f"gfs.{date_str}/{cycle_hr:02d}/atmos/"
        f"gfs.t{cycle_hr:02d}z.pgrb2.0p25.f{fxx:03d}"
    )


def rda_info():
    return (
        "NCAR Research Data Archive (ds084.1):\n"
        "  https://rda.ucar.edu/datasets/d084001/\n"
        "  Free registration required. Has full GFS 0.25° archive back to 2015.\n"
        "  Use their web interface or API to download."
    )


def download_file(url, outpath, max_retries=3, timeout=60):
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, timeout=timeout, stream=True)
            if r.status_code == 200:
                with open(outpath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                size_kb = os.path.getsize(outpath) / 1024
                return True
            elif r.status_code == 404:
                return False  # File doesn't exist, no point retrying
            else:
                last_err = f"HTTP {r.status_code}"
                # Per-retry noise stays at DEBUG (TITO user console filters these)
                log.debug("  HTTP %s, retry %s/%s", r.status_code, attempt + 1, max_retries)
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            log.debug("  download error (retry %s/%s): %s", attempt + 1, max_retries, e)
        time.sleep(2 ** attempt)  # exponential backoff
    if last_err:
        log.debug("  download failed after %s tries: %s", max_retries, last_err)
    return False


def download_gfs_grib(dt, cycle_hr, fxx, outpath):
    if outpath.exists() and outpath.stat().st_size > 100:
        log.debug("  [cached] %s", outpath.name)
        return True

    # 1. Try AWS S3 (full file, fast and reliable)
    url = aws_url(dt, cycle_hr, fxx)
    log.debug("  Trying AWS... %s %02dZ f%03d", dt.strftime("%Y%m%d"), cycle_hr, fxx)
    if download_file(url, outpath, timeout=120):
        size_kb = outpath.stat().st_size / 1024
        log.debug("  [AWS] %.0f KB", size_kb)
        return True

    # 2. Try NOMADS GRIB filter (tiny download, server-side subset)
    url = nomads_grib_filter_url(dt, cycle_hr, fxx, **DOMAIN)
    log.debug("  AWS unavailable, trying NOMADS...")
    if download_file(url, outpath):
        size_kb = outpath.stat().st_size / 1024
        log.debug("  [NOMADS] %.0f KB", size_kb)
        return True

    # 3. Try Google Cloud Storage
    url = gcs_url(dt, cycle_hr, fxx)
    log.debug("  NOMADS unavailable, trying Google Cloud...")
    if download_file(url, outpath, timeout=120):
        size_kb = outpath.stat().st_size / 1024
        log.debug("  [GCS] %.0f KB", size_kb)
        return True

    log.debug(
        "  FAILED: Could not download %s %02dZ f%03d",
        dt.strftime("%Y%m%d"), cycle_hr, fxx,
    )
    return False

def read_grib_uv850(grib_path, is_subsetted=True):
    try:
        # Try reading with cfgrib
        ds = xr.open_dataset(grib_path, engine="cfgrib",
                             backend_kwargs={
                                 "filter_by_keys": {
                                     "typeOfLevel": "isobaricInhPa",
                                     "level": 850,
                                     "shortName": ["u", "v"],
                                 }
                             })
        u = ds["u"].values
        v = ds["v"].values
        lats = ds["latitude"].values
        lons = ds["longitude"].values
        ds.close()
        return u, v, lats, lons
    except Exception as e:
        # Fallback: try without level filter (subsetted files might not need it)
        try:
            ds = xr.open_dataset(grib_path, engine="cfgrib",
                                 backend_kwargs={
                                     "filter_by_keys": {
                                         "typeOfLevel": "isobaricInhPa",
                                         "level": 850,
                                     }
                                 })
            u = ds["u"].values
            v = ds["v"].values
            lats = ds["latitude"].values
            lons = ds["longitude"].values
            ds.close()
            return u, v, lats, lons
        except Exception as e2:
            log.error(f"  Failed to read {grib_path}: {e2}")
            return None, None, None, None


def regrid_to_01deg(data_025, lats_025, lons_025,
                    lat_min, lat_max, lon_min, lon_max, res=0.1):
    # Define output grid
    lats_01 = np.arange(lat_min + res/2, lat_max, res)
    lons_01 = np.arange(lon_min + res/2, lon_max, res)

    # Ensure input lats are ascending for RegularGridInterpolator
    if lats_025[0] > lats_025[-1]:
        lats_025 = lats_025[::-1]
        data_025 = data_025[::-1, :]

    # Ensure input lons are ascending
    if lons_025[0] > lons_025[-1]:
        lons_025 = lons_025[::-1]
        data_025 = data_025[:, ::-1]

    # Handle longitude wrapping (GFS uses 0-360, we might need -180 to 180)
    if lons_025.max() > 180:
        # Convert 0-360 to -180-180
        lons_025 = np.where(lons_025 > 180, lons_025 - 360, lons_025)
        # Re-sort
        sort_idx = np.argsort(lons_025)
        lons_025 = lons_025[sort_idx]
        data_025 = data_025[:, sort_idx]

    # Subset to domain + buffer for interpolation
    buf = 0.5  # degrees buffer
    lat_mask = (lats_025 >= lat_min - buf) & (lats_025 <= lat_max + buf)
    lon_mask = (lons_025 >= lon_min - buf) & (lons_025 <= lon_max + buf)

    lats_sub = lats_025[lat_mask]
    lons_sub = lons_025[lon_mask]
    data_sub = data_025[np.ix_(lat_mask, lon_mask)]

    # Build interpolator and evaluate on output grid
    interp = RegularGridInterpolator(
        (lats_sub, lons_sub), data_sub, method="linear",
        bounds_error=False, fill_value=None
    )

    lon_grid, lat_grid = np.meshgrid(lons_01, lats_01)
    pts = np.column_stack([lat_grid.ravel(), lon_grid.ravel()])
    data_01 = interp(pts).reshape(len(lats_01), len(lons_01))

    return data_01, lats_01, lons_01


def interpolate_temporal(data_hourly, times_hourly, dt_target=30):
    n_times, n_lat, n_lon = data_hourly.shape

    # Create half-hourly time axis
    t_start = times_hourly[0]
    t_end = times_hourly[-1]
    times_hh = []
    t = t_start
    while t <= t_end:
        times_hh.append(t)
        t += timedelta(minutes=dt_target)

    # Convert to fractional hours for interpolation
    t0 = times_hourly[0]
    hours_in = np.array([(t - t0).total_seconds() / 3600 for t in times_hourly])
    hours_out = np.array([(t - t0).total_seconds() / 3600 for t in times_hh])

    # Interpolate each grid cell
    data_hh = np.zeros((len(times_hh), n_lat, n_lon), dtype=np.float32)
    for i in range(n_lat):
        for j in range(n_lon):
            data_hh[:, i, j] = np.interp(hours_out, hours_in, data_hourly[:, i, j])

    return data_hh, times_hh

def process_event(event_key, event_cfg, max_workers=6):

    name = event_cfg["name"]
    dt_start = event_cfg["start"]
    dt_end = event_cfg["end"]
    n_days = (dt_end - dt_start).days + 1

    log.info(f"\n{'='*60}")
    log.info(f"Processing: {name}")
    log.info(f"Period: {dt_start} to {dt_end} ({n_days} days)")
    log.info(f"Domain: {DOMAIN}")
    log.info(f"{'='*60}")

    # Create directories
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Step 1: Build list of files to download ----
    # For each 6-hour GFS cycle, download f000 through f005 for hourly data
    # This gives us the freshest analysis-based winds at every hour
    download_list = []
    current_date = dt_start
    while current_date <= dt_end:
        for cycle_hr in GFS_CYCLES:
            for fxx in range(6):  # f000 to f005 = hours 0-5 from each cycle
                valid_time = datetime(
                    current_date.year, current_date.month, current_date.day,
                    cycle_hr, 0
                ) + timedelta(hours=fxx)

                # Only include if valid time is within our event window
                evt_start_dt = datetime(dt_start.year, dt_start.month, dt_start.day, 0, 0)
                evt_end_dt = datetime(dt_end.year, dt_end.month, dt_end.day, 23, 59)
                if evt_start_dt <= valid_time <= evt_end_dt:
                    fname = f"gfs_{current_date.strftime('%Y%m%d')}_{cycle_hr:02d}z_f{fxx:03d}.grib2"
                    download_list.append({
                        "date": current_date,
                        "cycle": cycle_hr,
                        "fxx": fxx,
                        "valid_time": valid_time,
                        "filename": fname,
                    })
        current_date += timedelta(days=1)

    # De-duplicate by valid_time (prefer f000 = analysis over forecast hours)
    seen_times = {}
    for item in download_list:
        vt = item["valid_time"]
        if vt not in seen_times or item["fxx"] < seen_times[vt]["fxx"]:
            seen_times[vt] = item

    download_list = sorted(seen_times.values(), key=lambda x: x["valid_time"])
    n_total = len(download_list)
    log.info(f"Files to download: {n_total}")

    # ---- Step 2: Download GRIB files (parallel) ----
    log.info(f"Downloading GFS 850 hPa U/V wind data ({max_workers} workers)...")
    failed = []
    done = 0
    ok_n = 0
    # Single-line progress token for TITO console filter (GFS progress: N/M ...)
    _prog_lock = threading.Lock()

    def _download_item(item):
        """Download a single GRIB file — thread-safe worker."""
        outpath = TEMP_DIR / item["filename"]
        ok = download_gfs_grib(item["date"], item["cycle"], item["fxx"], outpath)
        return item, ok

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_download_item, item): item for item in download_list}
        for future in as_completed(futures):
            item, ok = future.result()
            with _prog_lock:
                done += 1
                if ok:
                    ok_n += 1
                else:
                    failed.append(item)
                # Emit compact progress every file (TITO rewrites one console line)
                log.info(
                    "GFS progress: %s/%s (ok=%s fail=%s)",
                    done, n_total, ok_n, len(failed),
                )

    if failed:
        log.warning("%s/%s GFS wind files failed to download.", len(failed), n_total)
        log.debug("Failed valid times (first 5): %s",
                  [str(x["valid_time"]) for x in failed[:5]])
        if len(failed) == len(download_list):
            log.error("ALL GFS wind downloads failed (network/DNS?). Check connectivity.")
            log.debug("Alternative data source:\n%s", rda_info())
            return None

    # ---- Step 3: Read GRIB files and extract U/V at 850 hPa ----
    log.info(f"\nReading and processing GRIB files...")

    hourly_data = {}  # valid_time → (u850, v850)
    ref_lats = ref_lons = None

    for item in download_list:
        grib_path = TEMP_DIR / item["filename"]
        if not grib_path.exists():
            continue

        u, v, lats, lons = read_grib_uv850(grib_path)
        if u is None:
            continue

        if ref_lats is None:
            ref_lats = lats
            ref_lons = lons

        hourly_data[item["valid_time"]] = (u, v)

    if not hourly_data:
        log.error("No data could be read from downloaded files.")
        return None

    log.info(f"Successfully read {len(hourly_data)} hourly wind fields")
    log.info(f"GFS grid: {len(ref_lats)} lat × {len(ref_lons)} lon")

    # ---- Step 4: Regrid from 0.25° to 0.1° ----
    log.info(f"\nRegridding from 0.25° to 0.1°...")

    # Sort by time
    sorted_times = sorted(hourly_data.keys())

    # Regrid first field to get output grid dimensions
    u0, v0 = hourly_data[sorted_times[0]]
    _, lats_01, lons_01 = regrid_to_01deg(u0, ref_lats, ref_lons, **DOMAIN)
    n_lat = len(lats_01)
    n_lon = len(lons_01)
    log.info(f"Output grid: {n_lat} lat × {n_lon} lon (0.1°)")

    # Regrid all time steps
    u_hourly = np.zeros((len(sorted_times), n_lat, n_lon), dtype=np.float32)
    v_hourly = np.zeros((len(sorted_times), n_lat, n_lon), dtype=np.float32)

    for i, t in enumerate(sorted_times):
        u, v = hourly_data[t]
        u_01, _, _ = regrid_to_01deg(u, ref_lats, ref_lons, **DOMAIN)
        v_01, _, _ = regrid_to_01deg(v, ref_lats, ref_lons, **DOMAIN)
        u_hourly[i] = u_01
        v_hourly[i] = v_01

        if (i + 1) % 24 == 0:
            log.info(f"  Regridded {i+1}/{len(sorted_times)} timesteps")

    log.info(f"  Regridded {len(sorted_times)}/{len(sorted_times)} timesteps")

    # ---- Step 5: Interpolate from hourly to half-hourly ----
    log.info(f"\nInterpolating from hourly to half-hourly (30 min)...")

    u_hh, times_hh = interpolate_temporal(u_hourly, sorted_times, dt_target=30)
    v_hh, _ = interpolate_temporal(v_hourly, sorted_times, dt_target=30)

    log.info(f"Half-hourly timesteps: {len(times_hh)}")
    log.info(f"Time range: {times_hh[0]} to {times_hh[-1]}")

    # ---- Step 6: Save as STREAM-Sat NetCDF ----
    out_fname = f"GFS_UV850_0.1deg_{dt_start.strftime('%Y%m%d')}_{dt_end.strftime('%Y%m%d')}.nc"
    out_path = OUTPUT_DIR / out_fname
    log.info(f"\nSaving to: {out_path}")

    save_streamsat_netcdf(out_path, u_hh, v_hh, lats_01, lons_01, times_hh)

    # Print summary
    size_mb = out_path.stat().st_size / (1024 * 1024)
    log.info(f"\n{'='*60}")
    log.info(f"SUCCESS: {name}")
    log.info(f"Output: {out_path}")
    log.info(f"Size: {size_mb:.1f} MB")
    log.info(f"Grid: {n_lat} lat × {n_lon} lon (0.1°)")
    log.info(f"Time: {len(times_hh)} half-hourly steps")
    log.info(f"Variables: U_MV (eastward), V_MV (northward) [m/s]")
    log.info(f"{'='*60}\n")

    return out_path


def save_streamsat_netcdf(out_path, u_data, v_data, lats, lons, times):
    nc = Dataset(str(out_path), "w", format="NETCDF4", clobber=True)

    # Dimensions
    nc.createDimension("lat", len(lats))
    nc.createDimension("lon", len(lons))
    nc.createDimension("time", len(times))

    # Time variable
    time_units = "hours since 1970-01-01 00:00:00 UTC"
    time_var = nc.createVariable("time", "d", ("time",))
    time_var.units = time_units
    time_var.calendar = "gregorian"
    time_var[:] = date2num(times, time_units, calendar="gregorian")

    # Coordinate variables
    lat_var = nc.createVariable("lat", "f4", ("lat",), zlib=True)
    lat_var.units = "degrees_north"
    lat_var.long_name = "latitude"
    lat_var[:] = lats

    lon_var = nc.createVariable("lon", "f4", ("lon",), zlib=True)
    lon_var.units = "degrees_east"
    lon_var.long_name = "longitude"
    lon_var[:] = lons

    # Also add latitude/longitude aliases (STREAM-Sat code may use either)
    lat_var2 = nc.createVariable("latitude", "f4", ("lat",), zlib=True)
    lat_var2.units = "degrees_north"
    lat_var2.long_name = "latitude"
    lat_var2[:] = lats

    lon_var2 = nc.createVariable("longitude", "f4", ("lon",), zlib=True)
    lon_var2.units = "degrees_east"
    lon_var2.long_name = "longitude"
    lon_var2[:] = lons

    # Wind component variables — shape: (lat, lon, time)
    # STREAM-Sat accesses as: dsw.variables['U_MV'][:,:,time_idx]
    u_var = nc.createVariable("U_MV", "f4", ("lat", "lon", "time"),
                              zlib=True, least_significant_digit=3)
    u_var.units = "m/s"
    u_var.long_name = "eastward wind at 850 hPa (GFS)"

    v_var = nc.createVariable("V_MV", "f4", ("lat", "lon", "time"),
                              zlib=True, least_significant_digit=3)
    v_var.units = "m/s"
    v_var.long_name = "northward wind at 850 hPa (GFS)"

    # Input data is (time, lat, lon) → transpose to (lat, lon, time)
    u_var[:, :, :] = np.transpose(u_data, (1, 2, 0))
    v_var[:, :, :] = np.transpose(v_data, (1, 2, 0))

    # Global attributes
    nc.title = "GFS 850 hPa Wind Components for STREAM-Sat Motion Vectors"
    nc.source = "NCEP GFS v16, 0.25 deg regridded to 0.1 deg"
    nc.resolution = "0.1 degree, 30-minute temporal"
    nc.domain = f"lat [{lats.min():.1f}, {lats.max():.1f}], lon [{lons.min():.1f}, {lons.max():.1f}]"
    nc.created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    nc.note = ("U_MV = eastward wind component, V_MV = northward wind component. "
               "To use with STREAM-Sat, set windInFname to this file path. "
               "The STREAM-Sat code will read U_MV and V_MV as motion vectors.")

    nc.close()
    log.info(f"  Saved {out_path}")

def main():
    parser = argparse.ArgumentParser(description="Download GFS 850 hPa winds for STREAM-Sat")
    parser.add_argument("--event", choices=["barbados", "tammy", "all"], default="all",
                        help="Which event to download (default: all)")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Override output directory")
    parser.add_argument("--skip-download", action="store_true",
                        help="Skip download, process existing GRIB files only")
    parser.add_argument("--workers", type=int, default=6,
                        help="Number of parallel download threads (default: 6)")
    args = parser.parse_args()

    if args.output_dir:
        global OUTPUT_DIR, TEMP_DIR
        OUTPUT_DIR = Path(args.output_dir)
        TEMP_DIR = OUTPUT_DIR / "temp_grib"

    log.info("GFS 850 hPa Wind Download for STREAM-Sat")
    log.info(f"Output directory: {OUTPUT_DIR}")
    log.info(f"Parallel workers: {args.workers}")

    events_to_process = (
        EVENTS.keys() if args.event == "all"
        else [args.event]
    )

    results = {}
    for key in events_to_process:
        try:
            result = process_event(key, EVENTS[key], max_workers=args.workers)
            results[key] = result
        except Exception as e:
            log.error(f"Error processing {EVENTS[key]['name']}: {e}")
            import traceback
            traceback.print_exc()
            results[key] = None

    # Summary
    log.info("\n" + "=" * 60)
    log.info("DOWNLOAD SUMMARY")
    log.info("=" * 60)
    for key, result in results.items():
        status = f"OK → {result}" if result else "FAILED"
        log.info(f"  {EVENTS[key]['name']}: {status}")

    log.info(f"\nTo use with STREAM-Sat, update STREAM_Main.py:")
    log.info(f'  windInFname = "path/to/GFS_UV850_0.1deg_YYYYMMDD_YYYYMMDD.nc"')
    log.info(f"\nNOTE: You may need to update the variable names in")
    log.info(f"STREAM_NoiseGeneration.py if using the original code:")
    log.info(f"  Change 'U_MV'/'V_MV' to match your file, or vice versa.")
    log.info(f"  The example MERRA2 file uses 'U850'/'V850'.")


if __name__ == "__main__":
    main()

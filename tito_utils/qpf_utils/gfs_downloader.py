#!/usr/bin/env python3
"""
================================================================================
GFS Precipitation Downloader Module (QPF - Quantitative Precipitation Forecast)
================================================================================

Description:
------------
Downloads NOAA GFS (Global Forecast System) precipitation forecasts using
Herbie, converts PRATE (precipitation rate) from kg m⁻² s⁻¹ to mm/hour, clips
to specified domain, and writes EF5-compatible GeoTIFF files. Supports both
batch downloads for specific forecast windows and continuous auto-polling mode.

Quick usage guide:
- Activate the conda environment:
  `conda activate tito_env`
- One-shot/parameterized run:  
  `python gfs_downloader.py --start "2025-12-01 00" --end "2025-12-03 00" --xmin -85 --xmax -74 --ymin 19 --ymax 26 --out /path/to/output`
- Continuous auto mode (polls for latest cycle, uses defaults defined below):  
  `python gfs_downloader.py   cd /home/nammehta/TITOV2Cuba/tito_utils/qpf_utils
  `python gfs_downloader.py --out /home/nammehta/TITOV2Cuba/precip/GFS/GFSData ` or `python gfs_downloader.py --auto-once` for a single pass.
  `nohup python gfs_downloader.py --auto-out /home/nammehta/TITO_Final_DA_Cuba/precip/GFS > /home/nammehta/TITO_Final_DA_Cuba/data/logs/gfs_downloader.py

Standalone Usage:
-----------------
1. Install Herbie and dependencies (see Required Packages below)

2. One-shot batch download (specific time window):

   python gfs_downloader.py \
       --start "2025-12-01 00" \
       --end "2025-12-03 00" \
       --xmin -85 --xmax -74 \
       --ymin 19 --ymax 26 \
       --out /path/to/output

3. Single auto-mode execution (one forecast cycle):

   python gfs_downloader.py --auto-once

4. Continuous auto-polling (runs indefinitely, downloads latest cycle):

   python gfs_downloader.py --auto-out /path/to/output

5. Programmatic usage in Python:

   from gfs_downloader import download_GFS
   
   written_files = download_GFS(
       systemStartLRTime="2025-12-01 00",
       systemEndTime="2025-12-03 00",
       xmin=-85.0, xmax=-74.0,
       ymin=19.0, ymax=26.0,
       qpf_store_path="./gfs_output"
   )
   print(f"Downloaded {len(written_files)} forecast files")

6. Background execution (Linux/macOS):

   nohup python gfs_downloader.py \
       --auto-out /path/to/gfs/output \
       > /path/to/logs/gfs_downloader.log 2>&1 &

TITO Integration:
-----------------
TITO (Threading Inputs to Outputs) uses this module to:
1. Retrieve global forecast precipitation for the forecast period (+120 hours)
2. Provide fallback QPF source when high-resolution forecasts (AROME) unavailable
3. Initialize and update the precipitation boundary conditions for EF5

Called by: TITO orchestrator during forecast phase
Function: download_GFS() - Main entry point for TITO
          _auto_mode()   - For continuous operational polling

Parameters expected from TITO:
  - systemStartLRTime: Forecast start time (str or datetime)
  - systemEndTime: Forecast end time (str or datetime)
  - xmin/xmax/ymin/ymax: Domain bounding box in degrees
  - qpf_store_path: Output directory for GeoTIFFs
  - max_cycles_back: Retry attempts with older GFS cycles (default 4)

TITO typically calls with:
  - 120-hour forecast horizon (hourly to +120h, then 3-hourly to +384h)
  - Automatic cycle fallback if latest cycle unavailable

Required Packages:
------------------
- herbie-data: Download GFS GRIB2 files from various sources (NCEP, AWS, etc.)
  pip install herbie-data
  Note: Also installs cfgrib for GRIB2 reading

- xarray: Multi-dimensional array handling for weather data
  pip install xarray
  OR with conda:
  conda install -c conda-forge xarray

- rioxarray: Rasterio integration for xarray (spatial operations, GeoTIFF export)
  pip install rioxarray
  OR with conda:
  conda install -c conda-forge rioxarray

- numpy: Numerical operations and array manipulation
  pip install numpy

- rasterio: Underlying geospatial raster I/O (included with rioxarray)
  May need: conda install -c conda-forge rasterio

- cfgrib: GRIB2 file reading engine (usually installed with herbie)
  pip install cfgrib
  May need eccodes backend:
  conda install -c conda-forge eccodes

- Internal TITO dependencies: None (self-contained module)

Data Source:
------------
NOAA GFS (Global Forecast System) 0.25° Forecast Product
- Spatial Resolution: 0.25° x 0.25° (~25 km)
- Temporal Resolution: Hourly to +120h, then 3-hourly to +384h
- Coverage: Global
- Format: GRIB2 (downloaded) → GeoTIFF (output)
- Cycles: 00Z, 06Z, 12Z, 18Z UTC
- Variable: PRATE (instantaneous precipitation rate at surface)
- Latency: ~3-4 hours for full run

Output Format:
--------------
GeoTIFF files named: gfs.YYYYMMDDHHMM.tif (valid time in UTC)
- Projection: EPSG:4326 (WGS84)
- Units: mm/hour (converted from kg m⁻² s⁻¹)
- Data type: float32
- NoData value: -9999.0

Auto Mode Configuration:
------------------------
Edit these defaults in the script:
- AUTO_OUT_DIR: Default output directory
- AUTO_BBOX: Default domain bounds (xmin, xmax, ymin, ymax)
- AUTO_HOURS: Forecast horizon (default 120 hours)
- AUTO_POLL_SECONDS: Polling interval (default 3600 = 1 hour)
- AUTO_CYCLE_GRACE_MINUTES: Grace period before targeting new cycle (default 120)

Retry Logic:
------------
If no files written for requested window:
1. Automatically tries previous GFS cycles (up to max_cycles_back)
2. Clears partial outputs between attempts
3. Falls back to older data rather than failing completely

Notes:
------
- Herbie automatically selects best data source (NCEP, AWS, Google Cloud, etc.)
- Handles longitude wrapping (0-360° to -180-180°)
- Supports both string and datetime inputs for time parameters
- Staging directory used in auto-mode to prevent partial data exposure
================================================================================
"""

from __future__ import annotations

import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Tuple, Union

import numpy as np
import xarray as xr

# rioxarray import registers the rio accessor on xarray objects
import rioxarray  # noqa: F401

# --------------------
# Optional dependencies
# --------------------
try:
    from herbie import Herbie
except Exception as exc:  # pragma: no cover - provide a clearer import error
    raise ImportError(
        "Herbie is required. Install with `pip install herbie-data`"
    ) from exc


# --------------------
# Auto mode defaults (edit these to customize)
# --------------------
# Default output folder for auto mode (override via --auto-out or _auto_mode(out_dir=...))
# Default absolute output directory for auto mode
AUTO_OUT_DIR = ""

# Default auto mode spatial bbox (lon/lat). Edit as needed.
# Using global by default; set to your region for smaller files.
AUTO_BBOX = (-180.0, 180.0, -90.0, 90.0)  # (xmin, xmax, ymin, ymax)

# Forecast horizon in hours for auto mode
AUTO_HOURS = 120  # hourly to +120

# Poll frequency in seconds for auto mode
AUTO_POLL_SECONDS = 3600  # 1 hour

# Grace period after a new cycle starts before targeting it (minutes)
AUTO_CYCLE_GRACE_MINUTES = 120


def _ensure_datetime(dt_like: Union[str, datetime]) -> datetime:
    """Convert a string or datetime-like to a Python datetime (naive, UTC-assumed).

    Acceptable string formats include:
    - "YYYY-MM-DD HH"
    - "YYYY-MM-DDTHH"
    - "YYYY-MM-DD HH:MM"
    - "YYYY-MM-DDTHH:MM"
    - "YYYY-MM-DD" (defaults to 00 UTC)
    """
    if isinstance(dt_like, datetime):
        return dt_like

    s = str(dt_like).strip()
    for fmt in (
        "%Y-%m-%d %H",
        "%Y-%m-%dT%H",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            return dt
        except ValueError:
            continue
    raise ValueError(f"Unrecognized datetime format: {dt_like}")


def _gfs_forecast_hours(max_hours: int, upper_limit: int = 384) -> List[int]:
    """Generate forecast hours for GFS 0.25°: hourly to 120h, then 3-hourly.

    Ensures 0..min(max_hours, upper_limit), with step 1 to 120 and step 3 beyond.
    Avoids 121 and 122 which are typically unavailable in pgrb2.0p25 output.
    """
    if max_hours < 0:
        return []
    limit = min(max_hours, upper_limit)
    if limit <= 120:
        return list(range(0, limit + 1, 1))
    # Hourly 0..120, then 123..limit step 3
    tail_start = 123 if limit >= 123 else None
    head = list(range(0, 121, 1))
    if tail_start is None:
        return head
    tail = list(range(tail_start, limit + 1, 3))
    return head + tail


def _find_precip_var_name(ds: xr.Dataset) -> str:
    """Heuristically pick the precipitation variable (PRATE/APCP) from a Dataset.

    We expect exactly one primary data variable for the query. Prefer variables
    whose attributes indicate APCP/precip.
    """
    candidates = list(ds.data_vars)
    if not candidates:
        raise KeyError("No data variables found in dataset")

    # Prefer variables with GRIB/long name hints
    def score(var_name: str) -> int:
        v = ds[var_name]
        attrs = {k.lower(): str(v.attrs.get(k, "")).lower() for k in v.attrs}
        text = " ".join([var_name.lower()] + list(attrs.values()))
        hits = 0
        # prioritize prate first, then apcp
        for token in ("prate", "precipitation rate", "apcp", "precip", "total precipitation"):
            if token in text:
                hits += 1
        return hits

    scored = sorted(candidates, key=score, reverse=True)
    return scored[0]


def _standardize_latlon(var_da: xr.DataArray) -> xr.DataArray:
    """Return DataArray renamed to dims lat/lon with CRS=EPSG:4326 and spatial dims set.

    Handles common cfgrib outputs where dims may be (time, latitude, longitude), (latitude, longitude),
    or (y, x) with coordinates named latitude/longitude.
    """
    da = var_da.squeeze(drop=True)

    # Identify latitude/longitude dims
    dims = list(da.dims)
    lat_dim = None
    lon_dim = None
    for d in dims:
        dl = d.lower()
        if lat_dim is None and (dl == "latitude" or dl == "lat" or dl.endswith("_lat")):
            lat_dim = d
        if lon_dim is None and (dl == "longitude" or dl == "lon" or dl.endswith("_lon")):
            lon_dim = d

    # If dims are generic y/x, try to map via coordinates
    if lat_dim is None or lon_dim is None:
        for d in dims:
            if d.lower() in ("y",):
                lat_dim = d
            if d.lower() in ("x",):
                lon_dim = d

    # Rename dims to lat/lon
    rename_map = {}
    if lat_dim and lat_dim != "lat":
        rename_map[lat_dim] = "lat"
    if lon_dim and lon_dim != "lon":
        rename_map[lon_dim] = "lon"
    if rename_map:
        da = da.rename(rename_map)

    # Ensure coords named lat/lon exist
    if "lat" not in da.coords:
        # Try to attach from dataset coords/variables
        if "latitude" in da.coords:
            da = da.rename({"latitude": "lat"})
        elif "latitude" in da.to_dataset().variables:
            da = da.assign_coords(lat=da.to_dataset()["latitude"])  # type: ignore
        else:
            # Create synthetic lat coordinate if missing (assume regular grid)
            da = da.assign_coords(lat=np.arange(da.sizes["lat"]))

    if "lon" not in da.coords:
        if "longitude" in da.coords:
            da = da.rename({"longitude": "lon"})
        elif "longitude" in da.to_dataset().variables:
            da = da.assign_coords(lon=da.to_dataset()["longitude"])  # type: ignore
        else:
            da = da.assign_coords(lon=np.arange(da.sizes["lon"]))

    # Register spatial metadata for rioxarray
    da = da.rio.write_crs("EPSG:4326", inplace=False)
    da = da.rio.set_spatial_dims(x_dim="lon", y_dim="lat", inplace=False)

    return da


def _wrap_longitudes_to_180(da: xr.DataArray) -> xr.DataArray:
    """Wrap longitude coordinate to [-180, 180] and sort by longitude ascending."""
    if "lon" not in da.coords:
        return da
    lon_vals = da.coords["lon"].values
    # Only wrap if values exceed 180 (i.e., 0..360 grid)
    if np.nanmax(lon_vals) <= 180 and np.nanmin(lon_vals) >= -180:
        return da
    lon_wrapped = ((lon_vals + 180.0) % 360.0) - 180.0
    da = da.assign_coords(lon=("lon", lon_wrapped))
    da = da.sortby("lon")
    return da




def _safe_to_raster(da: xr.DataArray, out_path: str) -> None:
    """Write DataArray to GeoTIFF with sensible defaults for EF5 compatibility."""
    # Ensure directory exists
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    # Fill NaNs
    data = da.data.astype(np.float32)
    data = np.where(np.isnan(data), -9999.0, data)
    # rioxarray respects dtype/nodata via kwargs
    da_to_write = xr.DataArray(
        data=data,
        dims=da.dims,
        coords=da.coords,
        name=da.name or "PRATE_mm_hr",
        attrs={"units": "mm/h"},
    )
    da_to_write.rio.write_nodata(-9999.0, inplace=True)
    da_to_write.rio.to_raster(out_path, driver="GTiff", dtype="float32")


def _align_to_gfs_cycle(dt: datetime) -> datetime:
    """Align a datetime to the most recent GFS cycle (00, 06, 12, 18 UTC).

    Returns a naive datetime (tzinfo removed) at the cycle hour at or before dt.
    """
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    base = dt.replace(minute=0, second=0, microsecond=0)
    while base.hour % 6 != 0:
        base -= timedelta(hours=1)
    return base


def _parse_valid_time_from_filename(name: str) -> Optional[datetime]:
    """Extract valid time from a filename like 'gfs.YYYYMMDDHHMM.tif'."""
    try:
        base = os.path.basename(name)
        if not (base.startswith("gfs.") and base.endswith(".tif")):
            return None
        ts = base[len("gfs.") : -len(".tif")]
        if len(ts) != 12 or not ts.isdigit():
            return None
        return datetime.strptime(ts, "%Y%m%d%H%M")
    except Exception:
        return None


def _download_one_fxx(
    fxx: int,
    init_time: datetime,
    xmin: float,
    xmax: float,
    ymin: float,
    ymax: float,
    qpf_store_path: str,
) -> Tuple[int, Optional[str], Optional[str]]:
    """Download and process a single GFS forecast hour (PRATE → GeoTIFF).

    Designed to be called from a thread pool — each *fxx* is fully independent.

    Returns
    -------
    (fxx, out_path, error_msg)
        *out_path* is the written GeoTIFF path on success, or None.
        *error_msg* is a description of the failure, or None.
    """
    valid_time = init_time + timedelta(hours=fxx)

    # retrieve PRATE via Herbie for this forecast hour
    H = Herbie(init_time, model="gfs", product="pgrb2.0p25", fxx=fxx)

    ds: Optional[Union[xr.Dataset, List[xr.Dataset]]] = None
    last_err: Optional[Exception] = None
    for query in (":PRATE:surface", ":PRATE:", "PRATE:surface", "PRATE"):
        try:
            ds = H.xarray(query)
            break
        except Exception as e:
            last_err = e
            ds = None
    if ds is None:
        err = f"Could not retrieve PRATE for f{fxx:03d} (valid {valid_time:%Y-%m-%d %H:%M} UTC) from cycle {init_time:%Y-%m-%d %H}"
        if last_err:
            err += f" — {last_err}"
        return (fxx, None, err)

    # Herbie/cfgrib may return a list of datasets (multiple hypercubes).
    try:
        if isinstance(ds, list):
            if len(ds) == 0:
                raise KeyError("Empty hypercube list returned for PRATE")
            ds0 = ds[0]
            ds = ds0
            if "prate" in ds.data_vars:
                var_name = "prate"
            elif "PRATE" in ds.data_vars:
                var_name = "PRATE"
            else:
                data_vars = list(ds.data_vars)
                if not data_vars:
                    raise KeyError("PRATE variable not present in first hypercube")
                var_name = data_vars[0]
        else:
            if "prate" in ds.data_vars:
                var_name = "prate"
            elif "PRATE" in ds.data_vars:
                var_name = "PRATE"
            else:
                raise KeyError("PRATE variable not present in dataset")

        prate_da = ds[var_name]
    except Exception as e:
        return (fxx, None, f"PRATE variable not found for f{fxx:03d}: {e}")

    # Standardize spatial dims and CRS
    prate_da = _standardize_latlon(prate_da)
    prate_da = _wrap_longitudes_to_180(prate_da)

    # Convert rate (kg m-2 s-1 == mm/s) to mm/hour
    rate = prate_da.data.astype(np.float32)
    if rate.ndim == 3:
        rate = np.squeeze(rate, axis=0)
    rate_mm_per_hour = rate * 3600.0

    # Build DataArray with mm/hour precipitation rate
    step_da = xr.DataArray(
        data=rate_mm_per_hour,
        dims=("lat", "lon"),
        coords={"lat": prate_da.coords["lat"], "lon": prate_da.coords["lon"]},
        name="PRATE_mm_per_hour",
        attrs={"units": "mm/hour"},
    )

    # Attach spatial metadata for rioxarray
    step_da = step_da.rio.write_crs("EPSG:4326", inplace=False)
    step_da = step_da.rio.set_spatial_dims(x_dim="lon", y_dim="lat", inplace=False)

    # Clip to bounding box
    try:
        clipped_da = step_da.rio.clip_box(
            minx=float(xmin), miny=float(ymin), maxx=float(xmax), maxy=float(ymax)
        )
    except Exception:
        clipped_da = step_da

    # Build output path and write
    out_name = f"gfs.{valid_time:%Y%m%d%H%M}.tif"
    out_path = os.path.join(qpf_store_path, out_name)
    os.makedirs(qpf_store_path, exist_ok=True)
    _safe_to_raster(clipped_da, out_path)
    return (fxx, out_path, None)


def download_GFS(
    systemStartLRTime: Union[str, datetime],
    systemEndTime: Union[str, datetime],
    xmin: float,
    xmax: float,
    ymin: float,
    ymax: float,
    qpf_store_path: str,
    *,
    max_cycles_back: int = 4,
    max_workers: int = 6,
    force_cycle_start: Optional[datetime] = None,
    allow_previous_cycle_fallback: bool = True,
    clear_between_attempts: bool = True,
) -> List[str]:
    """Download GFS PRATE with Herbie and write hourly rate GeoTIFFs clipped to bbox.

    Implements a built-in retry: if no files are written for the requested window, it will
    step back by 6 hours to the previous GFS cycle and try again, up to ``max_cycles_back`` times.

    Args:
        systemStartLRTime: Requested start time of valid outputs (e.g., "2023-09-04 12").
        systemEndTime: Ending valid time for which to produce outputs.
        xmin/xmax/ymin/ymax: Bounding box in lon/lat for clipping.
        qpf_store_path: Output directory to store GeoTIFFs.
        max_cycles_back: How many previous 6-hour GFS cycles to attempt if none written.
        max_workers: Number of parallel download threads (default 4).

    Returns:
        List of output GeoTIFF file paths written.
    """
    # Normalize inputs and ensure naive datetimes (UTC-assumed)
    init_time_raw = _ensure_datetime(systemStartLRTime)
    end_time_raw = _ensure_datetime(systemEndTime)
    if init_time_raw.tzinfo is not None:
        init_time_raw = init_time_raw.replace(tzinfo=None)
    if end_time_raw.tzinfo is not None:
        end_time_raw = end_time_raw.replace(tzinfo=None)

    if end_time_raw < init_time_raw:
        raise ValueError("systemEndTime must be >= systemStartLRTime")

    # Start from the specified cycle (if forced) or the most recent cycle at or before init_time_raw
    attempt = 0
    outputs_overall: List[str] = []
    cycle_start = _align_to_gfs_cycle(force_cycle_start or init_time_raw)

    while attempt <= max_cycles_back:
        # Clear any partial outputs before a new attempt (optional)
        if clear_between_attempts:
            try:
                if os.path.isdir(qpf_store_path):
                    for name in os.listdir(qpf_store_path):
                        if name.startswith("gfs.") and name.endswith(".tif"):
                            try:
                                os.remove(os.path.join(qpf_store_path, name))
                            except Exception:
                                pass
            except Exception:
                pass

        init_time = cycle_start
        end_time = end_time_raw

        total_hours = int(round((end_time - init_time).total_seconds() / 3600.0))
        if total_hours < 0:
            # If going too far back, break early
            break
        fxx_list = _gfs_forecast_hours(total_hours)

        outputs: List[str] = []
        failures = 0

        # --- Parallel download of all forecast hours ---
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    _download_one_fxx,
                    fxx, init_time, xmin, xmax, ymin, ymax, qpf_store_path,
                ): fxx
                for fxx in fxx_list
            }
            for future in as_completed(future_map):
                fxx = future_map[future]
                try:
                    _, out_path, err_msg = future.result()
                except Exception as e:
                    sys.stderr.write(
                        f"Error: unhandled exception for f{fxx:03d}: {e}\n"
                    )
                    failures += 1
                    continue

                if out_path is not None:
                    outputs.append(out_path)
                else:
                    sys.stderr.write(f"Warning: {err_msg}\n")
                    failures += 1

        # Success criteria: at least one file successfully written for this cycle
        if len(outputs) > 0:
            outputs_overall = outputs
            break

        # If all hours failed (Herbie couldn't find any), optionally try previous cycle
        if not allow_previous_cycle_fallback:
            break

        prev_cycle = cycle_start - timedelta(hours=6)
        sys.stderr.write(
            f"No GFS data written for cycle {cycle_start:%Y-%m-%d %H}. Trying previous cycle {prev_cycle:%Y-%m-%d %H}...\n"
        )
        cycle_start = prev_cycle
        attempt += 1

    return outputs_overall


def _parse_cli_args(argv: Optional[List[str]] = None):  # pragma: no cover - CLI helper
    import argparse

    p = argparse.ArgumentParser(description="Download GFS PRATE via Herbie and write hourly GeoTIFFs.")
    # When provided, run once for the given window
    p.add_argument("--start", help="Model run start (e.g., '2023-09-04 12')")
    p.add_argument("--end", help="End valid time (e.g., '2023-09-09 00')")
    p.add_argument("--xmin", type=float)
    p.add_argument("--xmax", type=float)
    p.add_argument("--ymin", type=float)
    p.add_argument("--ymax", type=float)
    p.add_argument("--out", help="Output directory for GeoTIFFs")

    # Auto mode options
    p.add_argument("--auto-out", help="Auto mode output directory (default: AUTO_OUT_DIR in script)")
    p.add_argument("--auto-hours", type=int, help="Forecast horizon hours for auto mode (default: 120)")
    p.add_argument("--poll-seconds", type=int, help="Polling interval seconds for auto mode (default: 300)")
    p.add_argument(
        "--auto-once",
        action="store_true",
        help="Run auto mode for a single pass (no polling loop)",
    )
    return p.parse_args(argv)


def _latest_cycle_now() -> datetime:
    """Return the latest GFS cycle time (UTC) as of now."""
    return _align_to_gfs_cycle(datetime.utcnow())


def _auto_mode(
    out_dir: Optional[str] = None,
    hours: Optional[int] = None,
    poll_seconds: Optional[int] = None,
    one_shot: bool = False,
) -> int:
    """Continuously download the latest GFS cycle as it becomes available.

    - Does not fall back to previous cycles; it waits/polls for the latest cycle.
    - Uses hardcoded defaults unless overridden via CLI.
    """
    out_dir = out_dir or AUTO_OUT_DIR
    hours = hours or AUTO_HOURS
    poll = poll_seconds or AUTO_POLL_SECONDS
    xmin, xmax, ymin, ymax = AUTO_BBOX

    os.makedirs(out_dir, exist_ok=True)
    last_cycle: Optional[datetime] = None

    total_written_overall = 0
    while True:
        try:
            now_utc = datetime.utcnow()
            latest = _latest_cycle_now()
            # choose target cycle using grace window
            if now_utc <= latest + timedelta(minutes=AUTO_CYCLE_GRACE_MINUTES):
                target_cycle = latest - timedelta(hours=6)
                sys.stderr.write(
                    f"Auto mode: within {AUTO_CYCLE_GRACE_MINUTES} min of latest cycle {latest:%Y-%m-%d %H}; targeting previous cycle {target_cycle:%Y-%m-%d %H}.\n"
                )
            else:
                target_cycle = latest

            start = target_cycle
            end = target_cycle + timedelta(hours=hours)

            if last_cycle is None or target_cycle != last_cycle:
                # New cycle detected. Stage the first successful files before clearing the main folder.
                staging_dir = os.path.join(out_dir, ".staging")
                try:
                    if os.path.isdir(staging_dir):
                        for name in os.listdir(staging_dir):
                            try:
                                os.remove(os.path.join(staging_dir, name))
                            except Exception:
                                pass
                    else:
                        os.makedirs(staging_dir, exist_ok=True)
                except Exception:
                    pass

                sys.stderr.write(
                    f"Auto mode: switching to target cycle {target_cycle:%Y-%m-%d %H} (latest {latest:%Y-%m-%d %H}). Staging into {staging_dir} before clearing {out_dir}.\n"
                )

                staged = download_GFS(
                    systemStartLRTime=start,
                    systemEndTime=end,
                    xmin=xmin,
                    xmax=xmax,
                    ymin=ymin,
                    ymax=ymax,
                    qpf_store_path=staging_dir,
                    max_cycles_back=0,
                    force_cycle_start=target_cycle,
                    allow_previous_cycle_fallback=False,
                    clear_between_attempts=False,
                )

                if len(staged) > 0:
                    # Clear previous cycle files in out_dir and move staged files in
                    try:
                        if os.path.isdir(out_dir):
                            for name in os.listdir(out_dir):
                                if name.startswith("gfs.") and name.endswith(".tif"):
                                    try:
                                        os.remove(os.path.join(out_dir, name))
                                    except Exception:
                                        pass
                        # Move all staged files to out_dir
                        for name in os.listdir(staging_dir):
                            src = os.path.join(staging_dir, name)
                            dst = os.path.join(out_dir, name)
                            try:
                                shutil.move(src, dst)
                            except Exception:
                                pass
                        # Attempt to remove staging dir if empty
                        try:
                            os.rmdir(staging_dir)
                        except Exception:
                            pass
                        last_cycle = target_cycle
                        total_written_overall = len(staged)
                        sys.stderr.write(
                            f"Auto mode: staged {len(staged)} files. Cleared {out_dir} and promoted staged files for cycle {target_cycle:%Y-%m-%d %H}.\n"
                        )
                    except Exception as e:
                        sys.stderr.write(f"Auto mode: error promoting staged files: {e}\n")
                else:
                    # No data yet for the new cycle; do not clear out_dir. Try again next poll.
                    sys.stderr.write(
                        f"Auto mode: no files available yet for cycle {target_cycle:%Y-%m-%d %H}. Will retry later without clearing {out_dir}.\n"
                    )
                    # In one-shot mode, attempt a single fallback to previous cycle immediately
                    if one_shot:
                        try:
                            prev_cycle = target_cycle - timedelta(hours=6)
                            prev_start = prev_cycle
                            prev_end = prev_cycle + timedelta(hours=hours)
                            sys.stderr.write(
                                f"Auto mode (one-shot): attempting fallback to previous cycle {prev_cycle:%Y-%m-%d %H}.\n"
                            )
                            # ensure staging is empty
                            try:
                                if os.path.isdir(staging_dir):
                                    for name in os.listdir(staging_dir):
                                        try:
                                            os.remove(os.path.join(staging_dir, name))
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                            staged_prev = download_GFS(
                                systemStartLRTime=prev_start,
                                systemEndTime=prev_end,
                                xmin=xmin,
                                xmax=xmax,
                                ymin=ymin,
                                ymax=ymax,
                                qpf_store_path=staging_dir,
                                max_cycles_back=0,
                                force_cycle_start=prev_cycle,
                                allow_previous_cycle_fallback=False,
                                clear_between_attempts=False,
                            )
                            if len(staged_prev) > 0:
                                try:
                                    if os.path.isdir(out_dir):
                                        for name in os.listdir(out_dir):
                                            if name.startswith("gfs.") and name.endswith(".tif"):
                                                try:
                                                    os.remove(os.path.join(out_dir, name))
                                                except Exception:
                                                    pass
                                    for name in os.listdir(staging_dir):
                                        src = os.path.join(staging_dir, name)
                                        dst = os.path.join(out_dir, name)
                                        try:
                                            shutil.move(src, dst)
                                        except Exception:
                                            pass
                                    try:
                                        os.rmdir(staging_dir)
                                    except Exception:
                                        pass
                                    last_cycle = prev_cycle
                                    total_written_overall = len(staged_prev)
                                    sys.stderr.write(
                                        f"Auto mode (one-shot): promoted {len(staged_prev)} files for fallback cycle {prev_cycle:%Y-%m-%d %H}.\n"
                                    )
                                except Exception as e:
                                    sys.stderr.write(f"Auto mode: error promoting staged fallback files: {e}\n")
                        except Exception as e:
                            sys.stderr.write(f"Auto mode: fallback error: {e}\n")
            else:
                # Same cycle: top-up directly in out_dir
                sys.stderr.write(
                    f"Auto mode: attempting cycle {target_cycle:%Y-%m-%d %H} UTC to +{hours}h into {out_dir}\n"
                )
                written = download_GFS(
                    systemStartLRTime=start,
                    systemEndTime=end,
                    xmin=xmin,
                    xmax=xmax,
                    ymin=ymin,
                    ymax=ymax,
                    qpf_store_path=out_dir,
                    max_cycles_back=0,  # not used when fallback disabled
                    force_cycle_start=target_cycle,
                    allow_previous_cycle_fallback=False,
                    clear_between_attempts=False,
                )
                sys.stderr.write(
                    f"Auto mode: wrote {len(written)} files for cycle {target_cycle:%Y-%m-%d %H}.\n"
                )
                total_written_overall = len(written)
        except Exception as e:
            sys.stderr.write(f"Auto mode error: {e}\n")

        # Exit immediately in one-shot mode; otherwise sleep and continue polling
        if one_shot:
            return total_written_overall
        try:
            import time
            time.sleep(poll)
        except KeyboardInterrupt:
            sys.stderr.write("Auto mode stopped by user.\n")
            return total_written_overall


if __name__ == "__main__":  # pragma: no cover - CLI entry
    args = _parse_cli_args()
    # If both start and end are provided, run once in parameterized mode; otherwise, run auto mode
    if args.start and args.end and args.xmin is not None and args.xmax is not None and args.ymin is not None and args.ymax is not None and args.out:
        written = download_GFS(
            systemStartLRTime=args.start,
            systemEndTime=args.end,
            xmin=args.xmin,
            xmax=args.xmax,
            ymin=args.ymin,
            ymax=args.ymax,
            qpf_store_path=args.out,
        )
        print(f"Wrote {len(written)} files to {args.out}")
    else:
        wrote = _auto_mode(
            out_dir=args.auto_out,
            hours=args.auto_hours,
            poll_seconds=args.poll_seconds,
            one_shot=getattr(args, "auto_once", False),
        )
        # In one-shot mode, set exit code to non-zero if nothing was written
        if getattr(args, "auto_once", False):
            try:
                import sys as _sys
                _sys.exit(0 if wrote > 0 else 2)
            except SystemExit:
                raise
"""
================================================================================
SCaMPR Precipitation Retrieval Module (QPE - Quantitative Precipitation Estimate)
================================================================================

Description:
------------
Downloads NOAA Enterprise Rain Rate (SCaMPR / RRQPE - Self-Calibrating 
Multivariate Precipitation Retrieval) files from AWS S3, converts NetCDF
to GeoTIFF format, and prepares data for EF5 hydrologic model ingestion.
Provides 10-minute instantaneous rain rate estimates over the entire globe.

Standalone Usage:
-----------------
1. No authentication required - uses public AWS S3 bucket

2. Basic standalone script example:

   from datetime import datetime
   from scampr_retrieve import get_new_scampr_precip
   
   # Define domain bounds (e.g., Caribbean region)
   xmin, ymin, xmax, ymax = -85.0, 10.0, -60.0, 25.0
   
   # Current timestamp for data retrieval
   current_time = datetime.utcnow()
   
   # Download SCaMPR data
   get_new_scampr_precip(
       current_timestamp=current_time,
       precipFolder="./scampr_output",
       xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax,
       latency_minutes=20,    # Expected data latency
       lookback_hours=6       # Hours of historical data to retrieve
   )

3. Direct S3 exploration (for testing):

   import boto3
   from botocore import UNSIGNED
   from botocore.config import Config
   
   s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))
   response = s3.list_objects_v2(
       Bucket="noaa-enterprise-rainrate-pds",
       Prefix="BLEND/RainRate-Blend-INST/2024/01/15/12/"
   )
   for obj in response.get("Contents", []):
       print(obj["Key"])

TITO Integration:
-----------------
TITO (Threading Inputs to Outputs) uses this module to:
1. Provide alternative QPE source when other observations are unavailable
2. Supply 10-minute instantaneous precipitation rates for high-temporal-resolution
   applications in the Caribbean and Indian Ocean regions
3. Fill observation gaps in the precipitation time series

Called by: TITO orchestrator during precipitation preparation phase
Function: get_new_scampr_precip() - Main entry point for TITO

Parameters expected from TITO:
  - current_timestamp: datetime for the current forecast cycle (UTC)
  - precipFolder: Output directory for processed GeoTIFFs
  - xmin/ymin/xmax/ymax: Domain bounding box in degrees
  - latency_minutes: Expected data latency (default 20 min)
  - lookback_hours: Historical data window to populate (default 6 hours)

Required Packages:
------------------
- boto3: AWS SDK for Python to access S3 bucket
  pip install boto3 botocore

- numpy: Array manipulation for raster data
  pip install numpy

- xarray: NetCDF data handling
  pip install xarray
  Note: Also requires netCDF4 backend:
  conda install -c conda-forge netCDF4

- rasterio: GeoTIFF writing with proper georeferencing
  pip install rasterio
  OR:
  conda install -c conda-forge rasterio

- Internal TITO dependencies: None (self-contained module)

Data Source:
------------
NOAA Enterprise Rain Rate (SCaMPR / RRQPE)
- Source: s3://noaa-enterprise-rainrate-pds (public bucket)
- Spatial Resolution: 0.02° x 0.02° (~2 km)
- Temporal Resolution: 10 minutes
- Coverage: Global (-180° to 180°, -60° to 70° latitude)
- Format: NetCDF4 (converted to GeoTIFF)
- Latency: ~15-20 minutes

Output Format:
--------------
GeoTIFF files named: scampr.qpe.YYYYMMDDHHMM.mmhInst.tif
- Projection: EPSG:4326 (WGS84)
- Units: mm/hour (instantaneous rain rate)
- NoData: NaN (float32)
- Compression: DEFLATE with predictor=2

Behavior:
---------
- Downloads 10-minute products for the lookback window
- Files inside latency window are filled by copying last available data
- Creates _scampr_raw/ subfolder for temporary downloads/conversions
- Skips slots that cannot be filled (no data gaps)

Notes:
------
- No AWS credentials required (public bucket with UNSIGNED access)
- Clips data to requested bounding box before writing to save space
- Matches HSAF H40B temporal cadence (10 minutes)
================================================================================
"""

import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

# HDF5 tries to acquire file locks via xattr syscalls, which fail on NFS with
# "Operation not supported".  Disable locking before the HDF5/NetCDF4 backend
# is loaded so xarray/h5py never attempt it.
os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")

try:
    import boto3
    import numpy as np
    import xarray as xr
    import rasterio
    from botocore import UNSIGNED
    from botocore.config import Config
    from rasterio.transform import from_bounds
    _BOTO3_AVAILABLE = True
except ImportError:  # pragma: no cover
    _BOTO3_AVAILABLE = False

# ---------------------------------------------------------------------------
# S3 constants (public bucket — no credentials needed)
# ---------------------------------------------------------------------------
_S3_BUCKET  = "noaa-enterprise-rainrate-pds"
_S3_PREFIX  = "BLEND/RainRate-Blend-INST"

# Global grid parameters published in the NetCDF global attributes
_LAT_MAX =  70.0
_LAT_MIN = -60.0
_LON_MIN = -180.0
_LON_MAX =  180.0
_RES     =   0.02   # degrees (~2 km)
_N_ROWS  =  6501
_N_COLS  = 18000

# EF5-facing filename pattern.
# TITO will use this in the EF5 control template:
#   NAME=scampr.qpe.YYYYMMDDHHUU.mmhInst.tif
_SCAMPR_EF5_NAME_TPL = "scampr.qpe.{ts}.mmhInst.tif"   # ts = YYYYMMDDHHMM


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _s3_client():
    if not _BOTO3_AVAILABLE:
        raise ImportError(
            "boto3 is required for SCaMPR retrieval. "
            "Install with: pip install boto3 botocore"
        )
    return boto3.client("s3", config=Config(signature_version=UNSIGNED))


def _hour_prefix(dt: datetime, hour: int) -> str:
    return f"{_S3_PREFIX}/{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/{hour:02d}/"


def _list_nc_keys_for_hour(s3, dt: datetime, hour: int) -> list:
    prefix = _hour_prefix(dt, hour)
    resp = s3.list_objects_v2(Bucket=_S3_BUCKET, Prefix=prefix)
    if resp.get("KeyCount", 0) == 0:
        return []
    return sorted(
        obj["Key"] for obj in resp.get("Contents", []) if obj["Key"].endswith(".nc")
    )


def _assign_coordinates(ds):
    """Replace pixel-index dimensions with lat/lon coordinate arrays."""
    lats = np.arange(_LAT_MAX, _LAT_MIN - _RES / 2, -_RES)[:_N_ROWS]
    lons = np.arange(_LON_MIN, _LON_MAX, _RES)[:_N_COLS]
    ds = ds.assign_coords({"latitude": ("Rows", lats), "longitude": ("Columns", lons)})
    ds = ds.swap_dims({"Rows": "latitude", "Columns": "longitude"})
    ds = ds.drop_vars(["Rows", "Columns"], errors="ignore")
    return ds


def _nc_to_geotiff(nc_path: Path, tif_path: Path, xmin: float, ymin: float, xmax: float, ymax: float) -> bool:
    """Convert a single SCaMPR NetCDF to a clipped GeoTIFF (Band 1 = rain rate mm/h).

    Returns True on success, False on failure.
    """
    try:
        ds = xr.open_dataset(nc_path, decode_coords="all")
        ds = _assign_coordinates(ds)

        # Clip to requested bounding box before writing to reduce file size.
        ds_clip = ds.sel(
            latitude=slice(ymax + _RES, ymin - _RES),   # lat is descending
            longitude=slice(xmin - _RES, xmax + _RES),
        )

        rr = ds_clip["RRQPE"].where(ds_clip["RRQPE"] >= 0).values.astype("float32")
        n_rows, n_cols = rr.shape

        west  = float(ds_clip.longitude.min())
        east  = float(ds_clip.longitude.max())
        south = float(ds_clip.latitude.min())
        north = float(ds_clip.latitude.max())

        transform = from_bounds(west, south, east, north, n_cols, n_rows)
        t_start = ds.attrs.get("time_coverage_start", "")
        ds.close()

        tif_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(
            tif_path, mode="w", driver="GTiff",
            height=n_rows, width=n_cols, count=1, dtype="float32",
            crs="EPSG:4326", transform=transform,
            nodata=float("nan"),
            compress="deflate", predictor=2, BIGTIFF="IF_SAFER",
        ) as dst:
            dst.write(rr, 1)
            dst.update_tags(1, long_name="SCaMPR Rain Rate", units="mm/h", valid_min="0")
            dst.update_tags(
                source="NOAA Enterprise Rain Rate (SCaMPR/RRQPE)",
                source_bucket=f"s3://{_S3_BUCKET}",
                time_coverage_start=t_start,
                crs="EPSG:4326",
            )
        return True
    except Exception as exc:
        print(f"    SCaMPR nc→tif conversion failed for {nc_path.name}: {exc}")
        return False


def _parse_scampr_nc_timestamp(nc_key: str) -> datetime | None:
    """Extract the scan-start time from a SCaMPR S3 key.

    The filename uses a 15-character start-time field of the form
    ``_sYYYYMMDDHHMMSSt_`` where the last character is a tenths-of-second
    digit (always 0 in practice).  Only the first 14 digits are needed
    to recover the minute-resolution timestamp used by EF5.

    Example:
        RRQPE-INST-GLB-2_v1r1_blend_s202604091200000_e…_c….nc
                                      ^^^^^^^^^^^^^^^ ← 15 digits
    """
    import re
    m = re.search(r"_s(\d{15})_", nc_key)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1)[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _round_to_10min(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 10) * 10, second=0, microsecond=0)


def _ef5_name(ts: datetime) -> str:
    return _SCAMPR_EF5_NAME_TPL.format(ts=ts.strftime("%Y%m%d%H%M"))


# ---------------------------------------------------------------------------
# Public API — single entry point called by the orchestrator
# ---------------------------------------------------------------------------

def get_new_scampr_precip(
    current_timestamp: datetime,
    precipFolder: str,
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    latency_minutes: int = 20,
    lookback_hours: int = 6,
) -> None:
    """Download/convert SCaMPR files and map them into the TITO precip folder.

    Behavior mirrors ``get_new_hsaf_precip``:
    - Attempts to download every 10-minute slot from
      (current_timestamp − lookback_hours) through the latest safe time
      (current_timestamp − latency_minutes).
    - For slots inside the latency window (newer than latest_safe_time),
      copies the last successfully obtained file to fill the gap.
    - Output files are written as ``scampr.qpe.YYYYMMDDHHMM.mmhInst.tif``
      in *precipFolder* for EF5 ingestion.
    - A ``_scampr_raw/`` working subfolder is used for downloads/conversions
      so the precip folder itself stays clean.

    Parameters
    ----------
    current_timestamp   : naive or tz-aware UTC datetime for the current cycle.
    precipFolder        : path to the per-region precip folder.
    xmin, ymin, xmax, ymax : clipping bounding box for the GeoTIFF output.
    latency_minutes     : minutes of expected product delay (default 20).
    lookback_hours      : how many hours of past data to populate (default 6).
    """
    # Normalise to naive UTC.
    if getattr(current_timestamp, "tzinfo", None) is not None:
        from datetime import timezone as _tz
        current_timestamp = current_timestamp.astimezone(_tz.utc).replace(tzinfo=None)

    precip_dir = Path(precipFolder)
    precip_dir.mkdir(parents=True, exist_ok=True)

    raw_dir = precip_dir / "_scampr_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    s3 = _s3_client()

    # Build the list of expected 10-minute timestamps.
    start_time = _round_to_10min(current_timestamp - timedelta(hours=lookback_hours))
    end_time   = _round_to_10min(current_timestamp)
    latest_safe = _round_to_10min(current_timestamp - timedelta(minutes=latency_minutes))

    expected_times = []
    t = start_time
    while t <= end_time:
        expected_times.append(t)
        t += timedelta(minutes=10)

    # Identify which hours we need to query from S3 (deduplicated).
    hours_needed = sorted({(t.year, t.month, t.day, t.hour) for t in expected_times if t <= latest_safe})

    if not hours_needed:
        print(f"    SCaMPR: all expected slots are within the latency window — nothing to query.")
        return

    print(f"    SCaMPR: querying {len(hours_needed)} hour(s) from S3 "
          f"({start_time.strftime('%Y-%m-%d %H:%M')} → {latest_safe.strftime('%H:%M')} UTC)")

    # Build a map of  scan_start_10min → raw GeoTIFF path  from S3 downloads.
    scampr_tif_by_ts: dict[datetime, Path] = {}
    total_s3_keys = 0

    for (year, month, day, hour) in hours_needed:
        slot_dt = datetime(year, month, day, hour)
        nc_keys = _list_nc_keys_for_hour(s3, slot_dt, hour)
        total_s3_keys += len(nc_keys)
        if not nc_keys:
            print(f"    SCaMPR: no S3 files found for {year:04d}-{month:02d}-{day:02d} {hour:02d}:xx UTC "
                  f"(bucket may have latency > {lookback_hours}h or data not yet published)")
            continue
        print(f"    SCaMPR: found {len(nc_keys)} file(s) for {year:04d}-{month:02d}-{day:02d} {hour:02d}:xx UTC")
        for nc_key in nc_keys:
            file_ts = _parse_scampr_nc_timestamp(nc_key)
            if file_ts is None:
                print(f"    SCaMPR: could not parse timestamp from key: {Path(nc_key).name!r} — skipping")
                continue
            file_ts_10 = _round_to_10min(file_ts)
            if file_ts_10 < start_time or file_ts_10 > latest_safe:
                continue
            if file_ts_10 in scampr_tif_by_ts:
                continue  # already have this slot

            local_nc   = raw_dir / Path(nc_key).name
            local_tif  = local_nc.with_suffix(".tif")

            if local_tif.exists():
                scampr_tif_by_ts[file_ts_10] = local_tif
                continue

            if not local_nc.exists():
                print(f"    SCaMPR: downloading {Path(nc_key).name} …", end="", flush=True)
                try:
                    s3.download_file(_S3_BUCKET, nc_key, str(local_nc))
                    print(" done")
                except Exception as exc:
                    print(f" FAILED ({exc})")
                    continue

            if _nc_to_geotiff(local_nc, local_tif, xmin, ymin, xmax, ymax):
                scampr_tif_by_ts[file_ts_10] = local_tif
                try:
                    local_nc.unlink()   # reclaim disk space after conversion
                except OSError:
                    pass

    if not scampr_tif_by_ts:
        msg = (
            f"    SCaMPR: no files were available/converted for this cycle "
            f"(S3 keys found: {total_s3_keys}; slots mapped: 0). "
            f"If S3 keys=0 the bucket may not publish data in near-real-time — "
            f"verify availability at s3://{_S3_BUCKET}/{_S3_PREFIX}/<date>/<hour>/"
        )
        print(msg)
        return

    # Map converted tifs into precipFolder with EF5-compatible names, filling latency gap.
    last_available_target: Path | None = None
    for ts in expected_times:
        target_name = _ef5_name(ts)
        target_path = precip_dir / target_name

        source_tif = scampr_tif_by_ts.get(ts)
        if source_tif is not None:
            shutil.copy2(source_tif, target_path)
            last_available_target = target_path
            continue

        # Fill the latency gap with the last available converted file.
        if ts > latest_safe and last_available_target is not None:
            shutil.copy2(last_available_target, target_path)

    print("    SCaMPR retrieval/update complete.")

#!/usr/bin/env python3
"""
================================================================================
AROME Precipitation Downloader Module (QPF - Quantitative Precipitation Forecast)
================================================================================

Description:
------------
Downloads Météo-France AROME Outre-Mer (Overseas) high-resolution precipitation
forecasts from data.gouv.fr, extracts cumulative rainfall 'tirf' variable,
derives hourly precipitation rates by differencing consecutive leads, clips to
specified domain, and writes EF5-compatible GeoTIFF files. Provides the primary
high-resolution QPF for TITO in Caribbean and Indian Ocean regions.

Standalone Usage:
-----------------
1. No authentication required - uses public open data

2. Basic standalone script example:

   from datetime import datetime, timedelta
   from arome_downloader import download_AROME, get_arome_domain_for_region
   
   # Define forecast window
   start_time = datetime(2024, 1, 15, 0, 0)
   end_time = datetime(2024, 1, 16, 18, 0)
   
   # Define domain bounds (e.g., Caribbean)
   xmin, ymin, xmax, ymax = -85.0, 10.0, -60.0, 25.0
   
   # Get domain for your region
   domain = get_arome_domain_for_region("haiti")  # Returns "ANTIL"
   # Or specify directly: domain = "ANTIL"  # or "INDIEN"
   
   # Download AROME forecast
   written_files = download_AROME(
       start_time=start_time,
       end_time=end_time,
       xmin=xmin, xmax=xmax,
       ymin=ymin, ymax=ymax,
       out_dir="./arome_output",
       domain=domain,
       max_cycles_back=4
   )
   print(f"Downloaded {len(written_files)} forecast files")

3. Direct GRIB2 URL construction for testing:

   # AROME cycle time (00, 06, 12, 18 UTC)
   run_time = "2024-01-15T00:00:00"
   domain = "ANTIL"
   lead = 6  # Hours (1 to 48)
   
   url = (
       "https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net/pnt/"
       f"{run_time}Z/arome-om/{domain}/0025/SP2/"
       f"arome-om-{domain}__0025__SP2__{lead:03d}H__{run_time}Z.grib2"
   )
   print(url)

TITO Integration:
-----------------
TITO (Threading Inputs to Outputs) uses this module to:
1. Provide primary high-resolution QPF for Caribbean (ANTIL) and Indian Ocean (INDIEN)
   regions as the main precipitation forcing for EF5
2. Supply 48-hour forecast at 2.8km resolution for accurate flood forecasting
3. Automatically determine appropriate AROME domain based on region name

Called by: TITO orchestrator during forecast phase
Functions:
  - download_AROME()              - Main entry point for batch downloads
  - get_arome_domain_for_region()   - Maps TITO region to AROME domain code

Parameters expected from TITO:
  - start_time, end_time: Forecast window (str or datetime, UTC)
  - xmin/xmax/ymin/ymax: Domain bounding box in degrees
  - out_dir: Output directory for GeoTIFFs
  - domain: "ANTIL" (Caribbean) or "INDIEN" (Indian Ocean)
  - max_cycles_back: Retry attempts with older AROME cycles (default 4)

TITO typically calls with:
  - 48-hour forecast horizon (maximum available from AROME)
  - Region-mapped domain via get_arome_domain_for_region()
  - Automatic cycle fallback if latest run unavailable

Required Packages:
------------------
- xarray: Multi-dimensional array handling for GRIB2 data
  pip install xarray
  OR with conda:
  conda install -c conda-forge xarray

- rioxarray: Rasterio integration for xarray (registers .rio accessor)
  pip install rioxarray
  OR with conda:
  conda install -c conda-forge rioxarray

- cfgrib: GRIB2 file reading engine for xarray
  pip install cfgrib
  May need eccodes backend:
  conda install -c conda-forge eccodes

- requests: HTTP library for downloading GRIB2 files
  pip install requests

- numpy: Numerical operations and array manipulation
  pip install numpy

- rasterio: Geospatial raster I/O for GeoTIFF export
  pip install rasterio
  OR with conda:
  conda install -c conda-forge rasterio

- Internal TITO dependencies: None (self-contained module)

Data Source:
------------
Météo-France AROME Outre-Mer (Overseas) SP2 Package
- URL: https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net/pnt/
- Licence: Météo-France Licence Ouverte 2.0 (open data)
- Spatial Resolution: 0.025° x 0.025° (~2.8 km)
- Temporal Resolution: Hourly (48 forecast steps per run + 1 analysis)
- Coverage: Regional domains:
  * ANTIL: Caribbean (Antilles) - covers Antigua, Barbados, Haiti, etc.
  * INDIEN: Indian Ocean - covers Réunion, Mayotte, Comoros
- Format: GRIB2 (downloaded) → GeoTIFF (output)
- Cycles: 00Z, 06Z, 12Z, 18Z UTC
- Variable: tirf (Time integral of rain flux) - cumulative mm since run start
- Latency: ~2 hours after cycle time (AROME_GRACE_HOURS)

Output Format:
--------------
GeoTIFF files named: arome.YYYYMMDDHH00.tif (valid time, UTC)
- Projection: EPSG:4326 (WGS84)
- Units: mm/hour (hourly rate derived from cumulative tirf)
- Data type: float32
- NoData value: -9999.0

Domain Mapping:
---------------
TITO region → AROME domain:
  - "antigua"  → "ANTIL"
  - "barbados" → "ANTIL"
  - "haiti"    → "ANTIL"
  - "comoros"  → "INDIEN"

Usage: domain = get_arome_domain_for_region("haiti")

Processing Steps:
-----------------
1. Downloads SP2 GRIB2 files for required lead times
2. Extracts 'tirf' (cumulative rainfall) variable using cfgrib
3. Differences consecutive leads: hourly_rate = tirf(H) - tirf(H-1)
4. Clips to requested bounding box
5. Writes GeoTIFF with proper georeferencing

Retry Logic:
------------
If insufficient data for requested window:
1. Automatically tries previous AROME cycles (up to max_cycles_back)
2. Steps back 6 hours per attempt (AROME cycle frequency)
3. Skips leads that cannot be produced
4. Returns successfully written files even if incomplete

Notes:
------
- Longitude wrapping handled (0-360° to -180-180° for Caribbean)
- GRIB files cached in _grib_cache/ subfolder to avoid re-download
- First lead (H=1) uses tirf directly (assumes tirf(0)=0)
- Validates cycle age with AROME_GRACE_HOURS before attempting download
================================================================================
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union

import numpy as np
import requests

try:
    import xarray as xr
    import rioxarray  # noqa: F401  – registers .rio accessor
except ImportError as exc:
    raise ImportError(
        "xarray and rioxarray are required. "
        "Install with: pip install xarray rioxarray"
    ) from exc

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = (
    "https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net/pnt/"
    "{run_time}Z/arome-om/{domain}/0025/SP2/"
    "arome-om-{domain}__0025__SP2__{lead:03d}H__{run_time}Z.grib2"
)

# Valid AROME run hours (UTC)
AROME_CYCLE_HOURS = (0, 6, 12, 18)

# Maximum lead time available from AROME (SP2 package: 0–48 h)
AROME_MAX_LEAD = 48

# Minimum age (hours) of a cycle before files are considered available on the server
AROME_GRACE_HOURS = 2

# Fixed mapping from TITO region name (lowercase) to AROME domain code
REGION_DOMAIN_MAP: Dict[str, str] = {
    "antigua": "ANTIL",
    "barbados": "ANTIL",
    "haiti": "ANTIL",
    "comoros": "INDIEN",
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ensure_datetime(dt_like: Union[str, datetime]) -> datetime:
    """Parse a string or datetime into a naive UTC datetime."""
    if isinstance(dt_like, datetime):
        return dt_like.replace(tzinfo=None) if dt_like.tzinfo else dt_like
    s = str(dt_like).strip()
    for fmt in (
        "%Y-%m-%d %H",
        "%Y-%m-%dT%H",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized datetime format: {dt_like!r}")


def _find_arome_run(ref_time: datetime, grace_hours: int = AROME_GRACE_HOURS) -> datetime:
    """Return the most recent AROME cycle that is at least grace_hours old.

    Steps backward hour-by-hour from ref_time until finding a 00/06/12/18 UTC
    cycle that satisfies the grace period.
    """
    ref = ref_time.replace(tzinfo=None, minute=0, second=0, microsecond=0)
    candidate = ref
    for _ in range(48):  # search at most 48 h back
        candidate = candidate.replace(minute=0, second=0, microsecond=0)
        if (
            candidate.hour in AROME_CYCLE_HOURS
            and (ref - candidate).total_seconds() >= grace_hours * 3600
        ):
            return candidate
        candidate -= timedelta(hours=1)
    raise RuntimeError(f"Could not find a valid AROME run before {ref_time}")


def _download_grib(url: str, dest: str) -> bool:
    """Download one GRIB2 file. Returns True on success, False on HTTP 404."""
    try:
        r = requests.get(url, stream=True, timeout=120)
    except Exception as exc:
        print(f"    AROME: network error downloading {os.path.basename(dest)}: {exc}")
        return False
    if r.status_code == 404:
        return False
    r.raise_for_status()
    with open(dest, "wb") as fh:
        for chunk in r.iter_content(chunk_size=65536):
            fh.write(chunk)
    return True


def _extract_tirf(grib_path: str) -> Optional[xr.DataArray]:
    """Open an AROME SP2 GRIB2 and return the 'tirf' DataArray (lat × lon, 2-D).

    tirf = Time integral of rain flux = cumulative liquid rainfall [kg m⁻² = mm]
    since model run start.
    """
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ds = xr.open_dataset(
                grib_path,
                engine="cfgrib",
                backend_kwargs={"indexpath": "", "errors": "ignore"},
                decode_timedelta=True,
            )
    except Exception as exc:
        print(f"    AROME: could not read {os.path.basename(grib_path)}: {exc}")
        return None

    candidates = [
        v for v in ds.data_vars
        if any(kw in v.lower() for kw in ["tirf", "tp", "precip", "rain"])
    ]
    if not candidates:
        print(
            f"    AROME: no precipitation variable found in {os.path.basename(grib_path)}. "
            f"Available: {list(ds.data_vars)}"
        )
        return None

    da = ds[candidates[0]].squeeze(drop=True)

    # Normalise spatial dim names to lat / lon
    rename_map: Dict[str, str] = {}
    for d in list(da.dims):
        dl = d.lower()
        if "lat" in dl and d != "lat":
            rename_map[d] = "lat"
        elif "lon" in dl and d != "lon":
            rename_map[d] = "lon"
    if rename_map:
        da = da.rename(rename_map)

    # Wrap 0–360 longitudes to –180/180.
    # ANTIL (western hemisphere) GRIB2 files store longitudes as 284°–308°E.
    # Without wrapping, _clip_to_bbox produces an empty array for any negative
    # bounding box, causing the downstream "index 0 is out of bounds" error.
    lon_dim_name = "lon" if "lon" in da.dims else None
    if lon_dim_name is not None:
        lon_vals = da[lon_dim_name].values
        if float(np.nanmax(lon_vals)) > 180.0:
            lon_wrapped = ((lon_vals + 180.0) % 360.0) - 180.0
            da = da.assign_coords({lon_dim_name: lon_wrapped})
            da = da.sortby(lon_dim_name)

    return da


def _clip_to_bbox(
    da: xr.DataArray, xmin: float, xmax: float, ymin: float, ymax: float
) -> xr.DataArray:
    """Clip a lat/lon DataArray to the given bounding box."""
    lat_dim = "lat" if "lat" in da.dims else "latitude"
    lon_dim = "lon" if "lon" in da.dims else "longitude"

    lat_vals = da[lat_dim].values
    lat_asc = float(lat_vals[0]) < float(lat_vals[-1])

    da = da.sel(
        {
            lat_dim: (
                slice(
                    max(float(da[lat_dim].min()), ymin),
                    min(float(da[lat_dim].max()), ymax),
                )
                if lat_asc
                else slice(
                    min(float(da[lat_dim].max()), ymax),
                    max(float(da[lat_dim].min()), ymin),
                )
            ),
            lon_dim: slice(
                max(float(da[lon_dim].min()), xmin),
                min(float(da[lon_dim].max()), xmax),
            ),
        }
    )
    return da


def _save_geotiff(da: xr.DataArray, out_path: str) -> None:
    """Write 2-D DataArray to an EF5-compatible float32 GeoTIFF (EPSG:4326, nodata=-9999).

    Uses rasterio directly (not rioxarray) to build the transform from the
    explicit lat/lon coordinate arrays.  This avoids rioxarray's NoDataInBounds
    error which can occur when the DataArray carries extra scalar GRIB
    coordinates (time, step, valid_time, surface) that confuse bounds detection.
    """
    try:
        import rasterio
        from rasterio.transform import from_bounds
    except ImportError as exc:
        raise ImportError(
            "rasterio is required for GeoTIFF export. "
            "Install with: pip install rasterio"
        ) from exc

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    # Drop all non-spatial dims/coords so we have a clean 2-D (lat × lon) array
    da = da.squeeze(drop=True)

    lat_dim = "lat" if "lat" in da.dims else "latitude"
    lon_dim = "lon" if "lon" in da.dims else "longitude"

    lat_vals = da[lat_dim].values.astype(np.float64)
    lon_vals = da[lon_dim].values.astype(np.float64)

    # Ensure latitude is north-first (descending) — rasterio expects top-left origin
    if lat_vals[0] < lat_vals[-1]:
        da = da.isel({lat_dim: slice(None, None, -1)})
        lat_vals = da[lat_dim].values.astype(np.float64)

    data = da.values.astype(np.float32)
    data = np.where(np.isnan(data), -9999.0, data)

    nrows, ncols = data.shape

    # Pixel size (assume regular grid; average spacing to be safe)
    res_lon = float(np.abs(lon_vals[1] - lon_vals[0])) if ncols > 1 else 0.025
    res_lat = float(np.abs(lat_vals[0] - lat_vals[1])) if nrows > 1 else 0.025

    west  = float(lon_vals[0])  - res_lon / 2.0
    east  = float(lon_vals[-1]) + res_lon / 2.0
    north = float(lat_vals[0])  + res_lat / 2.0
    south = float(lat_vals[-1]) - res_lat / 2.0

    transform = from_bounds(west, south, east, north, ncols, nrows)

    with rasterio.open(
        out_path,
        "w",
        driver="GTiff",
        height=nrows,
        width=ncols,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=-9999.0,
    ) as dst:
        dst.write(data, 1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_arome_domain_for_region(region_name: str) -> str:
    """Return the AROME domain code (ANTIL or INDIEN) for a TITO region name.

    Raises ValueError if the region has no AROME coverage.
    """
    key = region_name.strip().lower()
    domain = REGION_DOMAIN_MAP.get(key)
    if domain is None:
        raise ValueError(
            f"No AROME domain configured for region '{region_name}'. "
            f"Regions with AROME coverage: {list(REGION_DOMAIN_MAP.keys())}"
        )
    return domain


def download_AROME(
    start_time: Union[str, datetime],
    end_time: Union[str, datetime],
    xmin: float,
    xmax: float,
    ymin: float,
    ymax: float,
    out_dir: str,
    domain: str,
    *,
    max_cycles_back: int = 4,
) -> List[str]:
    """Download AROME QPF and write hourly GeoTIFFs suitable for EF5.

    For each valid hour H in [start_time, end_time] coverable by AROME (≤48
    lead hours from the run), this function:
      1. Downloads the SP2 GRIB2 file for the required lead time.
      2. Extracts the cumulative 'tirf' DataArray.
      3. Differences consecutive leads: hourly_mm = tirf(H) - tirf(H-1).
      4. Clips to the requested bounding box.
      5. Writes `arome.YYYYMMDDHH00.tif` (valid-time-stamped).

    Parameters
    ----------
    start_time, end_time : str or datetime
        Forecast window to cover (UTC, inclusive).
    xmin / xmax / ymin / ymax : float
        Bounding box for clipping.
    out_dir : str
        Directory to write GeoTIFF files.
    domain : str
        AROME domain code: "ANTIL" (Caribbean) or "INDIEN" (Indian Ocean).
    max_cycles_back : int
        Number of 6-hour AROME cycles to attempt before giving up.

    Returns
    -------
    List[str]
        Absolute paths of the written GeoTIFF files.
    """
    t_start = _ensure_datetime(start_time)
    t_end = _ensure_datetime(end_time)
    os.makedirs(out_dir, exist_ok=True)

    # GRIB cache sits inside out_dir to avoid re-downloading on retry
    cache_dir = os.path.join(out_dir, "_grib_cache")
    os.makedirs(cache_dir, exist_ok=True)

    for cycle_attempt in range(max_cycles_back):
        # Step back by cycle_attempt * 6 h to try progressively older runs
        reference = t_start - timedelta(hours=cycle_attempt * 6)
        try:
            run_time = _find_arome_run(reference)
        except RuntimeError as exc:
            print(f"    AROME: {exc}")
            break

        run_time_str = run_time.strftime("%Y-%m-%dT%H:%M:%S")
        print(f"    AROME: trying run {run_time_str}Z (domain={domain}) ...")

        def _lead_for(vt: datetime) -> int:
            return int(round((vt - run_time).total_seconds() / 3600))

        # Build the list of valid times we want output for
        valid_times_out: List[datetime] = []
        t = t_start.replace(minute=0, second=0, microsecond=0)
        while t <= t_end:
            valid_times_out.append(t)
            t += timedelta(hours=1)

        if not valid_times_out:
            print("    AROME: no valid times in request window.")
            return []

        # Determine which leads we need (include lead-1 for the first diff)
        leads_needed: set = set()
        coverable: List[datetime] = []
        for vt in valid_times_out:
            lead = _lead_for(vt)
            if 1 <= lead <= AROME_MAX_LEAD:
                coverable.append(vt)
                leads_needed.add(lead)
                if lead > 1:
                    leads_needed.add(lead - 1)  # needed for diff

        if not coverable:
            print(
                f"    AROME: window [{t_start}, {t_end}] is outside the 48-h "
                f"horizon of run {run_time_str}Z. Trying previous cycle."
            )
            continue

        # Download GRIB2 files for all required leads
        tirf_by_lead: Dict[int, xr.DataArray] = {}
        any_missing = False

        for lead in sorted(leads_needed):
            grib_name = (
                f"arome-om-{domain}__0025__SP2__{lead:03d}H__{run_time_str}Z.grib2"
            )
            grib_path = os.path.join(cache_dir, grib_name)
            url = BASE_URL.format(run_time=run_time_str, domain=domain, lead=lead)

            if not os.path.exists(grib_path) or os.path.getsize(grib_path) == 0:
                print(f"    AROME: downloading lead +{lead:02d}h ...")
                ok = _download_grib(url, grib_path)
                if not ok:
                    print(f"    AROME: lead +{lead:02d}h not available (HTTP 404).")
                    any_missing = True
                    continue

            da_tirf = _extract_tirf(grib_path)
            if da_tirf is not None:
                tirf_by_lead[lead] = da_tirf

        if not tirf_by_lead:
            print(
                f"    AROME: no tirf data retrieved for run {run_time_str}Z. "
                "Trying previous cycle."
            )
            continue

        # Compute hourly rates and write GeoTIFFs
        written: List[str] = []
        for vt in coverable:
            lead_now = _lead_for(vt)
            if lead_now not in tirf_by_lead:
                print(
                    f"    AROME: missing tirf for lead +{lead_now:02d}h "
                    f"({vt.strftime('%Y%m%d %H:%M')} UTC) — skipping."
                )
                continue

            tirf_now = tirf_by_lead[lead_now]
            lead_prev = lead_now - 1
            if lead_prev >= 1 and lead_prev in tirf_by_lead:
                # Normal diff: subtract previous cumulative value
                hourly = (tirf_now - tirf_by_lead[lead_prev]).clip(min=0)
            else:
                # First lead (or prev not available): tirf(1) ≈ hourly rate
                # since tirf(0) = 0 by model initialisation
                hourly = tirf_now.clip(min=0)

            hourly_clipped = _clip_to_bbox(hourly, xmin, xmax, ymin, ymax)

            out_name = f"arome.{vt.strftime('%Y%m%d%H')}00.tif"
            out_path = os.path.join(out_dir, out_name)
            _save_geotiff(hourly_clipped, out_path)
            written.append(out_path)

        if written:
            print(
                f"    AROME: {len(written)} GeoTIFF(s) written to {out_dir} "
                f"from run {run_time_str}Z."
            )
            if any_missing:
                print(
                    f"    AROME: WARNING — {len(coverable) - len(written)} "
                    "lead(s) could not be produced."
                )
            return written

        print(
            f"    AROME: run {run_time_str}Z yielded no output. "
            "Trying previous cycle."
        )

    print(
        f"    AROME: WARNING — no files produced after {max_cycles_back} "
        "cycle attempts."
    )
    return []

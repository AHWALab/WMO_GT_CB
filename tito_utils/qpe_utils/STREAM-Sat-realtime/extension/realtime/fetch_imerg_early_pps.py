#!/usr/bin/env python3
"""
fetch_imerg_early_pps.py
========================
Author:       Naman Mehta, University of Iowa
Created:      2026-06-03
License:      MIT (see LICENSE at repository root)

Part of the real-time STREAM-Sat QPE pipeline for the WMO Caribbean
flood forecasting project.

**Drop-in replacement for fetch_imerg_early.py** that downloads IMERG
Early Run from NASA's PPS server (jsimpsonhttps.pps.eosdis.nasa.gov)
instead of GES DISC.

PPS serves pre-processed GeoTIFFs which are:
  - Faster and more reliably available than GES DISC
  - Authenticated with simple email/password (no .netrc / cookie hassles)
  - Already on the 0.1° IMERG grid — just clip, convert, and stack

Output NetCDF is byte-identical in structure to fetch_imerg_early.py:
  variables:  prcp(latitude, longitude, time)  [mm/hr]
              latitude, longitude, time

Usage (same CLI as fetch_imerg_early.py)::

    python fetch_imerg_early_pps.py \\
        --end 2025-11-17T23:30 --hours 48 \\
        --out IMERG_latest.nc \\
        --tmp-dir ./imerg_raw \\
        --domain "11.05,22.95,-93.95,-57.05" \\
        --email vrobledodelgado@uiowa.edu

If --email is omitted, the script reads IMERG_PPS_EMAIL from the
environment (set in your crontab or .bashrc).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
from netCDF4 import Dataset, date2num

# ── GDAL (already required by the TITO pipeline) ───────────────────────────
try:
    from osgeo import gdal
    from osgeo.gdal import gdalconst
except ImportError:
    # Fall back to rasterio if GDAL Python bindings aren't available
    try:
        import rasterio
        _USE_RASTERIO = True
    except ImportError:
        raise SystemExit(
            "This script needs GDAL Python bindings (osgeo.gdal) or rasterio.\n"
            "  conda install -c conda-forge gdal\n"
            "  pip install rasterio  # alternative"
        )
else:
    _USE_RASTERIO = False

log = logging.getLogger("imerg-pps")

# ── PPS server ─────────────────────────────────────────────────────────────
PPS_BASE = "https://jsimpsonhttps.pps.eosdis.nasa.gov/imerg/gis/early"

# V07C became operational on 2026-03-04; files before that are V07B.
IMERG_V07C_CUTOVER = datetime(2026, 3, 4, 0, 0, tzinfo=timezone.utc)

# Default Caribbean domain matching the existing IMERG NetCDFs
DEFAULT_DOMAIN = {
    "lat_min": 11.05,
    "lat_max": 22.95,
    "lon_min": -93.95,
    "lon_max": -57.05,
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _version_suffix(dt: datetime) -> str:
    """Return the IMERG version tag for a given half-hour timestamp."""
    return ".V07C.30min.tif" if dt >= IMERG_V07C_CUTOVER else ".V07B.30min.tif"


def _imerg_pps_url(halfhour_start: datetime) -> str:
    """Build the PPS URL for one IMERG Early 30-min GeoTIFF.

    File pattern (PPS):
      3B-HHR-E.MS.MRG.3IMERG.20251116-S000000-E002959.0000.V07C.30min.tif
    stored under YYYY/MM/ on the PPS server.
    """
    date_str = halfhour_start.strftime("%Y%m%d")
    s_str = halfhour_start.strftime("%H%M%S")
    e_dt = halfhour_start + timedelta(minutes=29, seconds=59)
    e_str = e_dt.strftime("%H%M%S")
    total_min = halfhour_start.hour * 60 + halfhour_start.minute
    version_tag = _version_suffix(halfhour_start)
    fname = (f"3B-HHR-E.MS.MRG.3IMERG.{date_str}"
             f"-S{s_str}-E{e_str}.{total_min:04d}{version_tag}")
    folder = halfhour_start.strftime("%Y/%m/")
    return f"{PPS_BASE}/{folder}{fname}"


def _download_one_pps(url: str, dest: Path, email: str,
                      timeout: int = 120) -> bool:
    """Download a single GeoTIFF from the PPS server.

    Returns True on success, False on any failure (including 404).
    """
    if dest.exists() and dest.stat().st_size > 1024:
        return True
    try:
        r = requests.get(url, auth=(email, email),
                         timeout=timeout, stream=True)
    except requests.exceptions.RequestException as e:
        log.warning("  net error: %s", e)
        return False
    if r.status_code == 404:
        log.debug("  404 (not yet available): %s", Path(url).name)
        return False
    if r.status_code != 200:
        log.warning("  HTTP %d for %s", r.status_code, Path(url).name)
        return False
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
    return True


def _read_geotiff_gdal(path: Path,
                       lat_min: float, lat_max: float,
                       lon_min: float, lon_max: float):
    """Read + clip a PPS GeoTIFF using GDAL. Returns (prcp, lat, lon).

    PPS GeoTIFFs:
      - EPSG:4326, 0.1° grid, global coverage (−180..180, −90..90)
      - Pixel values are tenths of mm per 30 minutes
      - We convert to mm/hr:  val × 0.1 (tenths→mm) × 2 (per-30min→per-hr) = val × 0.2

    Grid alignment guarantee:
      Lat/lon are derived directly from the GDAL geotransform, which exactly
      matches the IMERG 0.1° cell-center grid (centers at k×0.1 + 0.05°).
      This ensures pixel-perfect alignment with the GES DISC HDF5 approach.
    """
    ds = gdal.Open(str(path), gdalconst.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"GDAL cannot open {path}")

    gt = ds.GetGeoTransform()
    # PPS GeoTIFF: gt[0]=left edge of leftmost pixel, gt[3]=top edge of topmost pixel
    # Pixel width  gt[1] ≈ 0.1°, pixel height |gt[5]| ≈ 0.1°
    x0, px_w = gt[0], gt[1]
    y0, px_h = gt[3], abs(gt[5])
    nx_full, ny_full = ds.RasterXSize, ds.RasterYSize

    # Compute pixel range that covers the requested bounding box.
    # Pixel (row, col) spans:
    #   lon: [x0 + col*px_w,  x0 + (col+1)*px_w)
    #   lat: [y0 - (row+1)*px_h, y0 - row*px_h)
    # This edge-based math is immune to floating-point drift.
    col_start = max(0, int(np.floor((lon_min - x0) / px_w)))
    col_end   = min(nx_full, int(np.ceil((lon_max - x0) / px_w)))
    row_start = max(0, int(np.floor((y0 - lat_max) / px_h)))
    row_end   = min(ny_full, int(np.ceil((y0 - lat_min) / px_h)))

    xoff, yoff = col_start, row_start
    xsize = col_end - col_start
    ysize = row_end - row_start

    arr = ds.ReadAsArray(xoff, yoff, xsize, ysize)  # (ysize, xsize), row 0 = top (90°N)
    ds = None

    # GDAL row 0 = top (90°N).  Keep descending lat so the output matches
    # the existing fetch_imerg_early.py convention (row 0 = north).
    arr = arr.astype(np.float32)

    # Convert: tenths of mm / 30min  →  mm/hr
    arr *= 0.2
    arr = np.where(arr < 0, 0.0, arr)
    arr = np.where(np.isnan(arr), 0.0, arr)

    # Build lat/lon from geotransform — guaranteed to match the IMERG 0.1°
    # cell-centre grid (centres at k×0.1 + 0.05°), identical to the
    # HDF5-based approach.  Lat is descending (row 0 = northernmost).
    lat = np.array([y0 - px_h * (row_start + r + 0.5)
                    for r in range(ysize)], dtype=np.float64)
    lon = np.array([x0 + px_w * (col_start + c + 0.5)
                    for c in range(xsize)], dtype=np.float64)

    return arr, lat.astype(np.float32), lon.astype(np.float32)


def _read_geotiff_rasterio(path: Path,
                           lat_min: float, lat_max: float,
                           lon_min: float, lon_max: float):
    """Read + clip a PPS GeoTIFF using rasterio (fallback)."""
    import rasterio
    from rasterio.windows import from_bounds
    from rasterio.transform import rowcol

    with rasterio.open(path) as src:
        # Window from bounds
        window = from_bounds(lon_min, lat_min, lon_max, lat_max,
                             src.transform)
        window = window.round_offsets().round_lengths()
        arr = src.read(1, window=window).astype(np.float32)

        # Build lat/lon arrays from the window transform
        w_transform = src.window_transform(window)
        ny, nx = arr.shape
        lon = np.array([w_transform.c + w_transform.a * (c + 0.5)
                        for c in range(nx)], dtype=np.float32)
        lat = np.array([w_transform.f + w_transform.e * (r + 0.5)
                        for r in range(ny)], dtype=np.float32)

    # Flip to descending lat
    arr = arr[::-1, :]
    lat = lat[::-1]

    # Convert tenths of mm/30min → mm/hr
    arr *= 0.2
    arr = np.where(arr < 0, 0.0, arr)
    arr = np.where(np.isnan(arr), 0.0, arr)

    return arr, lat, lon


def _read_geotiff(path: Path, **domain):
    """Dispatch to GDAL or rasterio based on what's available."""
    if _USE_RASTERIO:
        return _read_geotiff_rasterio(path, **domain)
    return _read_geotiff_gdal(path, **domain)


def _assemble_netcdf(out_path: Path,
                     prcp_stack: np.ndarray,
                     times: list[datetime],
                     lat: np.ndarray, lon: np.ndarray):
    """Write the STREAM-Sat-compatible NetCDF.

    Format matches fetch_imerg_early.py exactly:
      - latitude (1D, descending, degrees_north)
      - longitude (1D, ascending, degrees_east)
      - time (1D, 'hours since 1970-01-01 00:00:00 UTC')
      - prcp (latitude, longitude, time)  [mm/hr]
    """
    ny, nx = len(lat), len(lon)
    nt = len(times)
    prcp_out = np.transpose(prcp_stack, (1, 2, 0))  # (t,lat,lon) → (lat,lon,t)

    time_units = "hours since 1970-01-01 00:00:00 UTC"
    with Dataset(str(out_path), "w", format="NETCDF4", clobber=True) as nc:
        nc.createDimension("latitude", ny)
        nc.createDimension("longitude", nx)
        nc.createDimension("time", nt)

        lat_v = nc.createVariable("latitude", "f4", ("latitude",), zlib=True)
        lat_v.units = "degrees_north"
        lat_v[:] = lat

        lon_v = nc.createVariable("longitude", "f4", ("longitude",), zlib=True)
        lon_v.units = "degrees_east"
        lon_v[:] = lon

        tv = nc.createVariable("time", "d", ("time",))
        tv.units = time_units
        tv.calendar = "gregorian"
        tv[:] = date2num(times, time_units, calendar="gregorian")

        pv = nc.createVariable("prcp", "f4",
                               ("latitude", "longitude", "time"),
                               zlib=True, complevel=4,
                               chunksizes=(ny, nx, min(48, nt)))
        pv.units = "mm/hr"
        pv.long_name = "IMERG Early Run half-hourly precipitation rate"
        pv[:, :, :] = prcp_out

        nc.title = "IMERG Early Run (PPS), assembled for STREAM-Sat"
        nc.source = f"NASA PPS {PPS_BASE}"
        nc.created = datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Main fetch routine ─────────────────────────────────────────────────────

def fetch(end: datetime, hours: int, domain: dict, out_path: Path,
          tmp_dir: Path, email: str) -> Path:
    """Download IMERG Early GeoTIFFs from PPS and assemble into NetCDF.

    Parameters
    ----------
    end : datetime
        End of the desired time window (UTC).
    hours : int
        Number of hours of data to fetch.
    domain : dict
        ``lat_min, lat_max, lon_min, lon_max`` in degrees.
    out_path : Path
        Output NetCDF path.
    tmp_dir : Path
        Scratch directory for raw GeoTIFF downloads.
    email : str
        NASA PPS-registered email (used as both username and password).

    Returns
    -------
    Path
        The output NetCDF path.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n_slots = hours * 2  # half-hourly IMERG
    # Align *end* to the nearest half-hour boundary
    end = end.replace(second=0, microsecond=0)
    if end.minute not in (0, 30):
        end = end.replace(minute=(end.minute // 30) * 30)

    last_start = end - timedelta(minutes=30)
    starts = sorted([last_start - timedelta(minutes=30 * i)
                     for i in range(n_slots)])

    log.info("Target range: %s .. %s  (%d half-hours)",
             starts[0].strftime("%Y-%m-%d %H:%M"),
             (starts[-1] + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M"),
             n_slots)

    prcp_stack, kept_times = [], []
    missing = 0
    ref_lat = ref_lon = None

    for st in starts:
        url = _imerg_pps_url(st)
        dest = tmp_dir / Path(url).name
        ok = _download_one_pps(url, dest, email)
        if not ok:
            missing += 1
            log.warning("  missing: %s", st.strftime("%Y-%m-%d %H:%M"))
            continue
        try:
            p, lat, lon = _read_geotiff(dest, **domain)
        except Exception as e:
            log.warning("  read failed %s: %s", dest.name, e)
            missing += 1
            continue
        if ref_lat is None:
            ref_lat, ref_lon = lat, lon
        prcp_stack.append(p.astype(np.float32))
        kept_times.append(st)

    if not prcp_stack:
        raise RuntimeError("No IMERG PPS files could be downloaded/read.")

    log.info("Downloaded %d/%d files (missing %d)",
             len(prcp_stack), n_slots, missing)

    stack = np.stack(prcp_stack, axis=0)  # (t, lat, lon)
    _assemble_netcdf(out_path, stack, kept_times, ref_lat, ref_lon)
    size_mb = out_path.stat().st_size / 1e6
    log.info("Wrote %s (%.1f MB)", out_path, size_mb)
    return out_path


# ── CLI ────────────────────────────────────────────────────────────────────

def _parse_domain(s: str | None) -> dict:
    if s is None:
        return DEFAULT_DOMAIN
    parts = [float(x) for x in s.split(",")]
    return {"lat_min": parts[0], "lat_max": parts[1],
            "lon_min": parts[2], "lon_max": parts[3]}


def main():
    ap = argparse.ArgumentParser(
        description="Download IMERG Early from NASA PPS → STREAM-Sat NetCDF")
    ap.add_argument("--end", default=None,
                    help="End datetime UTC (ISO 8601). Default: 4 h before now.")
    ap.add_argument("--hours", type=int, default=48,
                    help="Hours of IMERG Early to fetch (default 48).")
    ap.add_argument("--out", required=True,
                    help="Output NetCDF path.")
    ap.add_argument("--tmp-dir", default="./imerg_tmp",
                    help="Directory for raw GeoTIFF downloads.")
    ap.add_argument("--domain", default=None,
                    help="'lat_min,lat_max,lon_min,lon_max'. "
                         f"Default: {DEFAULT_DOMAIN}")
    ap.add_argument("--email", default=None,
                    help="NASA PPS email (also used as password). "
                         "Default: $IMERG_PPS_EMAIL from environment.")
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args()

    logging.basicConfig(level=a.log_level,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    # ── Resolve email ──
    email = a.email or os.environ.get("IMERG_PPS_EMAIL")
    if not email:
        ap.error("--email is required, or set IMERG_PPS_EMAIL in environment.")

    # ── Resolve end time ──
    if a.end is None:
        end = datetime.now(timezone.utc) - timedelta(hours=4)
    else:
        end = datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc)

    fetch(end, a.hours, _parse_domain(a.domain),
          Path(a.out), Path(a.tmp_dir), email)


if __name__ == "__main__":
    main()

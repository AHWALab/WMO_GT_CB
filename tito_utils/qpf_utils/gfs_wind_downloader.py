#!/usr/bin/env python3
"""
GFS 850 hPa wind downloader with archive support (HTTP-based, no Herbie).

┌─ Deployment ─────────────────────────────────────────────────────────────┐
│ Crontab (@reboot):                                                        │
│   @reboot sleep 30 && cd .../qpf_utils && while true; do                  │
│     python gfs_wind_downloader.py --auto-out .../precip/gfs               │
│       >> .../logs/gfs_wind_downloader.log 2>&1; sleep 10; done            │
│                                                                           │
│ Archive format:  gfs_wind.YYYYMMDDHHMM.nc  (per valid hour)               │
│                  U_MV, V_MV in m/s at 850 hPa, 0.1 deg grid              │
│ Conda env:       tito_env2                                                │
└──────────────────────────────────────────────────────────────────────────┘

Uses fast HEAD probe to check if a GFS cycle is on AWS before downloading.
Downloads U/V 850 only: NOMADS filter → AWS/GCS .idx byte-range (not full GRIB).
"""

# python tito_utils/qpf_utils/gfs_wind_downloader.py     --auto-out /Dedicated/Humberto/Naman/TITO_Caribbean_Comoros_VM/TITOCaribbeanAndComoros/gfs_wind     --lat-min -90 --lat-max 90 --lon-min -180 --lon-max 180     --workers 6     >> logs/gfs_wind_caribbean.log 2>&1 &

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import List, Optional

# ── Suppress noisy third-party warnings ───────────────────────────────────
warnings.filterwarnings("ignore", message=".*datetime.datetime.utcnow.*")
warnings.filterwarnings("ignore", message=".*HDF5-DIAG.*")
warnings.filterwarnings("ignore", message=".*Ignoring index file.*")
warnings.filterwarnings("ignore", message=".*Can't read index file.*")
warnings.filterwarnings("ignore", message=".*skipping corrupted Message.*")
warnings.filterwarnings("ignore", category=UserWarning, module="cfgrib")
warnings.filterwarnings("ignore", category=DeprecationWarning)
os.environ["HDF5_DISABLE_VERSION_CHECK"] = "1"
os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"

# ── Filter HDF5 C-library noise from stderr ───────────────────────────────
# netCDF4/HDF5 writes "HDF5-DIAG:" blocks directly to stderr at the C level,
# completely bypassing Python's warning system.  These multi-line blocks are
# triggered by parallel file-access races during staging and are harmless.
class _CleanStderr:
    """stderr wrapper that drops HDF5-DIAG diagnostic blocks."""
    def __init__(self, real_stderr):
        self._real = real_stderr
        self._in_block = False

    def write(self, text: str) -> int:
        for line in text.splitlines(keepends=True):
            if "HDF5-DIAG" in line:
                self._in_block = True
                continue
            if self._in_block:
                stripped = line.lstrip()
                # HDF5 stack frames are indented and start with # / major / minor
                if stripped and not (
                    stripped.startswith("#")
                    or stripped.startswith("major:")
                    or stripped.startswith("minor:")
                ):
                    self._in_block = False
                else:
                    continue
            self._real.write(line)
        return len(text)

    def flush(self):
        self._in_block = False
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)

sys.stderr = _CleanStderr(sys.stderr)

import numpy as np
import requests
import xarray as xr
from netCDF4 import Dataset, date2num
from scipy.interpolate import RegularGridInterpolator

# ── Configuration ─────────────────────────────────────────────────────────
AUTO_HOURS = 120
AUTO_POLL_SECONDS = 3600
AUTO_CYCLE_GRACE_MINUTES = 120
MAX_CYCLES_BACK = 4
PARALLEL_WORKERS = 6
OUT_RES = 0.1
LEVEL_HPA = 850

# ── URL builders ──────────────────────────────────────────────────────────
def _aws_url(dt: datetime, cycle_hr: int, fxx: int) -> str:
    return (f"https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
            f"gfs.{dt:%Y%m%d}/{cycle_hr:02d}/atmos/"
            f"gfs.t{cycle_hr:02d}z.pgrb2.0p25.f{fxx:03d}")

def _nomads_url(dt: datetime, cycle_hr: int, fxx: int,
                lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> str:
    return (f"https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?"
            f"dir=%2Fgfs.{dt:%Y%m%d}%2F{cycle_hr:02d}%2Fatmos&"
            f"file=gfs.t{cycle_hr:02d}z.pgrb2.0p25.f{fxx:03d}&"
            f"lev_850_mb=on&var_UGRD=on&var_VGRD=on&"
            f"subregion=&toplat={lat_max}&leftlon={lon_min}&"
            f"rightlon={lon_max}&bottomlat={lat_min}")

def _gcs_url(dt: datetime, cycle_hr: int, fxx: int) -> str:
    return (f"https://storage.googleapis.com/global-forecast-system/"
            f"gfs.{dt:%Y%m%d}/{cycle_hr:02d}/atmos/"
            f"gfs.t{cycle_hr:02d}z.pgrb2.0p25.f{fxx:03d}")

# ── Helpers ────────────────────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _gfs_cycle(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    base = dt.replace(minute=0, second=0, microsecond=0)
    while base.hour % 6 != 0:
        base -= timedelta(hours=1)
    return base

def _forecast_hours(max_hours: int) -> List[int]:
    if max_hours <= 0:
        return []
    limit = min(max_hours, 384)
    if limit <= 120:
        return list(range(0, limit + 1))
    return list(range(0, 121)) + list(range(123, limit + 1, 3))

# ── Fast cycle probe ──────────────────────────────────────────────────────
def _cycle_available(dt: datetime, cycle_hr: int, fxx: int = 0, timeout: int = 10) -> bool:
    """HEAD request to AWS — returns True if the cycle's f000 file exists."""
    url = _aws_url(dt, cycle_hr, fxx)
    try:
        return requests.head(url, timeout=timeout).status_code == 200
    except Exception:
        return False

# ── File download (U/V 850 only — never full multi-100MB GFS by default) ──
def _is_grib_file(path: str, min_bytes: int = 200) -> bool:
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < min_bytes:
            return False
        with open(path, "rb") as f:
            return f.read(4) == b"GRIB"
    except OSError:
        return False


def _download_file(url: str, outpath: str, max_retries: int = 2, timeout: int = 120) -> bool:
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(url, timeout=timeout, stream=True)
            if r.status_code == 200:
                tmp = outpath + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
                if not _is_grib_file(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                else:
                    os.replace(tmp, outpath)
                    return True
            elif r.status_code == 404:
                return False
            if attempt < max_retries:
                time.sleep(2 ** attempt)
        except requests.exceptions.RequestException:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
    return False


def _parse_grib_idx_uv850(idx_text: str):
    """Byte ranges for 850 mb UGRD + VGRD from NOAA .idx inventory."""
    lines = [ln.strip() for ln in idx_text.splitlines() if ln.strip()]
    entries = []
    for ln in lines:
        parts = ln.split(":")
        if len(parts) < 5:
            continue
        try:
            start = int(parts[1])
        except ValueError:
            continue
        entries.append((start, parts[3].strip().upper(), parts[4].strip().lower()))
    ranges = []
    for i, (start, varname, level) in enumerate(entries):
        if varname not in ("UGRD", "VGRD") or "850" not in level:
            continue
        end = entries[i + 1][0] - 1 if i + 1 < len(entries) else None
        ranges.append((start, end))
    return ranges


def _download_grib_idx_subset(grib_url: str, outpath: str,
                              max_retries: int = 2, timeout: int = 90) -> bool:
    """HTTP Range pull of 850 hPa U/V messages only (~1–3 MB vs ~500 MB full)."""
    idx_url = grib_url + ".idx"
    for attempt in range(max_retries + 1):
        try:
            ir = requests.get(idx_url, timeout=timeout)
            if ir.status_code == 404:
                return False
            if ir.status_code != 200:
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                continue
            ranges = _parse_grib_idx_uv850(ir.text)
            if len(ranges) < 2:
                return False
            tmp = outpath + ".part"
            with open(tmp, "wb") as out_f:
                for start, end in ranges:
                    hdr = {"Range": f"bytes={start}-" if end is None else f"bytes={start}-{end}"}
                    rr = requests.get(grib_url, headers=hdr, timeout=timeout, stream=True)
                    if rr.status_code not in (200, 206):
                        raise requests.exceptions.RequestException(f"range HTTP {rr.status_code}")
                    for chunk in rr.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            out_f.write(chunk)
            if not _is_grib_file(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                continue
            os.replace(tmp, outpath)
            return True
        except requests.exceptions.RequestException:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
    return False


def _download_grib(dt: datetime, cycle_hr: int, fxx: int, outpath: str,
                   lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> bool:
    """Download 850 hPa U/V only: NOMADS → AWS idx → GCS idx (no full GRIB)."""
    if _is_grib_file(outpath):
        return True
    # 1) NOMADS domain+var subset (smallest)
    if _download_file(
        _nomads_url(dt, cycle_hr, fxx, lat_min, lat_max, lon_min, lon_max),
        outpath, timeout=90,
    ):
        return True
    # 2) AWS .idx byte-range (U/V 850 messages only)
    aws = _aws_url(dt, cycle_hr, fxx)
    if _download_grib_idx_subset(aws, outpath):
        return True
    # 3) GCS .idx byte-range
    if _download_grib_idx_subset(_gcs_url(dt, cycle_hr, fxx), outpath):
        return True
    # 4) Full GRIB only if explicitly enabled (slow)
    if os.environ.get("GFS_ALLOW_FULL_GRIB", "").strip() in ("1", "true", "TRUE", "yes"):
        if _download_file(aws, outpath, timeout=300):
            return True
        if _download_file(_gcs_url(dt, cycle_hr, fxx), outpath, timeout=300):
            return True
    return False

# ── GRIB extraction ───────────────────────────────────────────────────────
def _read_uv850(grib_path: str):
    """Read U/V at 850 hPa from GRIB2. Returns (u, v, lats, lons) or Nones.
    Reads U and V SEPARATELY to avoid cfgrib multi-hypercube shape issues."""
    lats = lons = None
    u_result = None
    v_result = None

    # ── Read U ──
    for query in ({"shortName": "u"}, {"shortName": "u", "typeOfLevel": "isobaricInhPa", "level": LEVEL_HPA}):
        try:
            ds = xr.open_dataset(grib_path, engine="cfgrib",
                                 backend_kwargs={"filter_by_keys": query})
            if isinstance(ds, list):
                ds = ds[0] if ds else None
            if ds is not None and "u" in ds.data_vars:
                u_result = ds["u"].values.astype(np.float32)
                if u_result.ndim == 3:
                    u_result = np.squeeze(u_result, axis=0)
                lats = ds["latitude"].values.astype(np.float64)
                lons = ds["longitude"].values.astype(np.float64)
                ds.close()
                break
        except Exception:
            continue

    if u_result is None:
        return None, None, None, None

    # ── Read V ──
    for query in ({"shortName": "v"}, {"shortName": "v", "typeOfLevel": "isobaricInhPa", "level": LEVEL_HPA}):
        try:
            ds = xr.open_dataset(grib_path, engine="cfgrib",
                                 backend_kwargs={"filter_by_keys": query})
            if isinstance(ds, list):
                ds = ds[0] if ds else None
            if ds is not None and "v" in ds.data_vars:
                v_result = ds["v"].values.astype(np.float32)
                if v_result.ndim == 3:
                    v_result = np.squeeze(v_result, axis=0)
                ds.close()
                break
        except Exception:
            continue

    if v_result is None:
        return None, None, None, None

    return u_result, v_result, lats, lons

# ── Regrid ────────────────────────────────────────────────────────────────
def _regrid(data_2d: np.ndarray, src_lats: np.ndarray, src_lons: np.ndarray,
            lat_min: float, lat_max: float, lon_min: float, lon_max: float,
            out_res: float = OUT_RES):
    lats_out = np.arange(lat_min + out_res / 2, lat_max, out_res)
    lons_out = np.arange(lon_min + out_res / 2, lon_max, out_res)
    if src_lats[0] > src_lats[-1]:
        src_lats = src_lats[::-1]; data_2d = data_2d[::-1, :]
    if src_lons[0] > src_lons[-1]:
        src_lons = src_lons[::-1]; data_2d = data_2d[:, ::-1]
    if src_lons.max() > 180:
        src_lons = np.where(src_lons > 180, src_lons - 360, src_lons)
        idx = np.argsort(src_lons); src_lons = src_lons[idx]; data_2d = data_2d[:, idx]
    buf = 0.5
    lm = (src_lats >= lat_min - buf) & (src_lats <= lat_max + buf)
    lnm = (src_lons >= lon_min - buf) & (src_lons <= lon_max + buf)
    interp = RegularGridInterpolator(
        (src_lats[lm], src_lons[lnm]), data_2d[np.ix_(lm, lnm)],
        method="linear", bounds_error=False, fill_value=None)
    lg, latg = np.meshgrid(lons_out, lats_out)
    return interp(np.column_stack([latg.ravel(), lg.ravel()])).reshape(
        len(lats_out), len(lons_out)).astype(np.float32), lats_out, lons_out

# ── Single forecast-hour worker ───────────────────────────────────────────
def _process_one_fxx(dt: datetime, cycle_hr: int, fxx: int,
                     lat_min: float, lat_max: float, lon_min: float, lon_max: float,
                     tmp_dir: str, out_dir: str) -> Optional[str]:
    valid_time = datetime(dt.year, dt.month, dt.day, cycle_hr, 0) + timedelta(hours=fxx)
    out_path = os.path.join(out_dir, f"gfs_wind.{valid_time:%Y%m%d%H%M}.nc")

    grib_path = os.path.join(tmp_dir, f"gfs_{dt:%Y%m%d}_{cycle_hr:02d}z_f{fxx:03d}.grib2")
    if not _download_grib(dt, cycle_hr, fxx, grib_path, lat_min, lat_max, lon_min, lon_max):
        return None

    u, v, src_lats, src_lons = _read_uv850(grib_path)
    if u is None:
        try:
            os.remove(grib_path)
        except Exception:
            pass
        return None

    u_reg, lats_out, lons_out = _regrid(u, src_lats, src_lons, lat_min, lat_max, lon_min, lon_max)
    v_reg, _, _ = _regrid(v, src_lats, src_lons, lat_min, lat_max, lon_min, lon_max)

    os.makedirs(out_dir, exist_ok=True)
    with Dataset(out_path, "w", format="NETCDF4", clobber=True) as nc:
        nc.createDimension("lat", len(lats_out))
        nc.createDimension("lon", len(lons_out))
        nc.createDimension("time", 1)
        tv = nc.createVariable("time", "d", ("time",))
        tv.units = "hours since 1970-01-01 00:00:00 UTC"
        tv.calendar = "gregorian"
        tv[:] = date2num([valid_time], tv.units, calendar="gregorian")
        for name, arr in [("lat", lats_out), ("lon", lons_out)]:
            v = nc.createVariable(name, "f4", (name,), zlib=True)
            v.units = "degrees_north" if name == "lat" else "degrees_east"; v[:] = arr
        uv = nc.createVariable("U_MV", "f4", ("lat", "lon", "time"),
                               zlib=True, least_significant_digit=3)
        uv.units = "m/s"; uv.long_name = f"eastward wind at {LEVEL_HPA} hPa"
        uv[:, :, 0] = u_reg
        vv = nc.createVariable("V_MV", "f4", ("lat", "lon", "time"),
                               zlib=True, least_significant_digit=3)
        vv.units = "m/s"; vv.long_name = f"northward wind at {LEVEL_HPA} hPa"
        vv[:, :, 0] = v_reg
        nc.title = f"GFS {LEVEL_HPA} hPa wind — {valid_time:%Y-%m-%d %H:%M} UTC"
        nc.source = f"NCEP GFS 0.25 deg regridded to {OUT_RES} deg"
        nc.created = _utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    try:
        os.remove(grib_path)
    except Exception:
        pass

    sys.stderr.write(f"[OK] {out_path}\n")
    return out_path

# ── Download full cycle ───────────────────────────────────────────────────
def download_wind_cycle(cycle: datetime, hours: int,
                        lat_min: float, lat_max: float,
                        lon_min: float, lon_max: float,
                        out_dir: str, workers: int = PARALLEL_WORKERS,
                        out_res: float = OUT_RES,
                        tmp_dir: Optional[str] = None) -> List[str]:
    fxx_list = _forecast_hours(hours)
    sys.stderr.write(f"[cycle] {cycle:%Y-%m-%d %H}z — {len(fxx_list)} wind hours, "
                     f"{workers} workers\n")
    cycle_hr = cycle.hour
    cycle_date = cycle.replace(hour=0, minute=0, second=0, microsecond=0)
    tmp = tmp_dir or os.path.join(out_dir, ".tmp_grib")
    os.makedirs(tmp, exist_ok=True)

    results: List[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_process_one_fxx, cycle_date, cycle_hr, fxx,
                        lat_min, lat_max, lon_min, lon_max, tmp, out_dir): fxx
            for fxx in fxx_list
        }
        for future in as_completed(futures):
            fxx = futures[future]
            try:
                path = future.result()
                if path:
                    results.append(path)
            except Exception as e:
                sys.stderr.write(f"[FAIL] wind f{fxx:03d}: {e}\n")
    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass
    sys.stderr.write(f"[cycle] {cycle:%Y-%m-%d %H}z — wrote {len(results)}/{len(fxx_list)} files\n")
    return results

# ── Archive assembly ──────────────────────────────────────────────────────
def assemble_winds_from_archive(archive_dir: str,
                                start_time: datetime, end_time: datetime,
                                out_path: str) -> Optional[str]:
    from netCDF4 import Dataset, date2num as _d2n
    hourly_u, hourly_v = {}, {}
    lats = lons = None
    t = start_time.replace(minute=0, second=0, microsecond=0)
    while t <= end_time:
        fpath = os.path.join(archive_dir, f"gfs_wind.{t:%Y%m%d%H%M}.nc")
        if not os.path.isfile(fpath) or os.path.getsize(fpath) < 100:
            return None
        try:
            with Dataset(fpath) as nc:
                hourly_u[t] = np.squeeze(nc.variables["U_MV"][:]).astype(np.float32)
                hourly_v[t] = np.squeeze(nc.variables["V_MV"][:]).astype(np.float32)
                if lats is None:
                    lats = nc.variables["lat"][:]; lons = nc.variables["lon"][:]
        except Exception:
            return None
        t += timedelta(hours=1)
    if not hourly_u:
        return None

    sorted_times = sorted(hourly_u.keys())
    ny, nx = hourly_u[sorted_times[0]].shape
    u_h = np.zeros((len(sorted_times), ny, nx), dtype=np.float32)
    v_h = np.zeros((len(sorted_times), ny, nx), dtype=np.float32)
    for i, t in enumerate(sorted_times):
        u_h[i], v_h[i] = hourly_u[t], hourly_v[t]

    times_hh = []; t = sorted_times[0]
    while t <= sorted_times[-1]:
        times_hh.append(t); t += timedelta(minutes=30)
    t0 = sorted_times[0]
    h_in = np.array([(t - t0).total_seconds() / 3600 for t in sorted_times])
    h_out = np.array([(t - t0).total_seconds() / 3600 for t in times_hh])
    u_hh = np.zeros((len(times_hh), ny, nx), dtype=np.float32)
    v_hh = np.zeros((len(times_hh), ny, nx), dtype=np.float32)
    for i in range(ny):
        for j in range(nx):
            u_hh[:, i, j] = np.interp(h_out, h_in, u_h[:, i, j])
            v_hh[:, i, j] = np.interp(h_out, h_in, v_h[:, i, j])

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tunits = "hours since 1970-01-01 00:00:00 UTC"
    with Dataset(out_path, "w", format="NETCDF4", clobber=True) as nc:
        nc.createDimension("lat", ny); nc.createDimension("lon", nx)
        nc.createDimension("time", len(times_hh))
        tv = nc.createVariable("time", "d", ("time",))
        tv.units = tunits; tv.calendar = "gregorian"
        tv[:] = _d2n(times_hh, tunits, calendar="gregorian")
        for name, arr in [("lat", lats), ("lon", lons),
                          ("latitude", lats), ("longitude", lons)]:
            dim = "lat" if "lat" in name else "lon"
            v = nc.createVariable(name, "f4", (dim,), zlib=True)
            v.units = "degrees_north" if "lat" in name else "degrees_east"; v[:] = arr
        uv = nc.createVariable("U_MV", "f4", ("lat", "lon", "time"),
                               zlib=True, least_significant_digit=3)
        uv.units = "m/s"; uv[:, :, :] = np.transpose(u_hh, (1, 2, 0))
        vv = nc.createVariable("V_MV", "f4", ("lat", "lon", "time"),
                               zlib=True, least_significant_digit=3)
        vv.units = "m/s"; vv[:, :, :] = np.transpose(v_hh, (1, 2, 0))
        nc.title = "GFS 850 hPa Wind Components (from archive)"
        nc.source = f"NCEP GFS, assembled at {OUT_RES} deg"
        nc.created = _utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    sys.stderr.write(f"[archive] assembled {len(times_hh)} steps → {out_path}\n")
    return out_path

# ── Auto mode ─────────────────────────────────────────────────────────────
BACKFILL_HOURS = 72  # cover STREAM-Sat's typical 60h ingest window + margin

def _backfill_archive(out_dir: str, lat_min: float, lat_max: float,
                      lon_min: float, lon_max: float, workers: int):
    """On startup, fill archive with recent cycles so STREAM-Sat can use it
    immediately.  Downloads f000-f011 (12 h) from each available cycle going back
    BACKFILL_HOURS."""
    now = _utcnow()
    latest = _gfs_cycle(now)
    start_cycle = latest - timedelta(hours=BACKFILL_HOURS)

    sys.stderr.write(f"[backfill] filling archive from {start_cycle:%Y-%m-%d %H}z "
                     f"to {latest:%Y-%m-%d %H}z ({BACKFILL_HOURS}h)\n")

    cycle = start_cycle
    total = 0
    while cycle <= latest:
        cycle_date = cycle.replace(hour=0, minute=0, second=0, microsecond=0)
        if _cycle_available(cycle_date, cycle.hour):
            staging = os.path.join(out_dir, ".staging")
            if os.path.isdir(staging):
                shutil.rmtree(staging, ignore_errors=True)
            os.makedirs(staging, exist_ok=True)

            results = download_wind_cycle(
                cycle, 11, lat_min, lat_max, lon_min, lon_max, staging, workers)
            if results:
                # Always overwrite — the most recent download for a given
                # valid hour comes from the latest cycle, which has the
                # shortest forecast lead time (analysis > short forecast).
                for f in os.listdir(staging):
                    src = os.path.join(staging, f)
                    dst = os.path.join(out_dir, f)
                    shutil.move(src, dst)
                total += len(results)
                sys.stderr.write(f"[backfill] {cycle:%Y-%m-%d %H}z → {len(results)} files added to archive\n")
            try:
                shutil.rmtree(staging, ignore_errors=True)
            except Exception:
                pass
        else:
            sys.stderr.write(f"[backfill] ⏳ {cycle:%Y-%m-%d %H}z not on AWS — skipping\n")
        cycle += timedelta(hours=6)

    sys.stderr.write(f"[backfill] done — {total} wind files total\n")
    # Prune old files
    cutoff = _utcnow() - timedelta(hours=BACKFILL_HOURS)
    pruned = 0
    for f in os.listdir(out_dir):
        if not (f.startswith("gfs_wind.") and f.endswith(".nc")):
            continue
        try:
            ts = f.replace("gfs_wind.", "").replace(".nc", "")
            ft = datetime.strptime(ts, "%Y%m%d%H%M")
            if ft < cutoff:
                os.remove(os.path.join(out_dir, f))
                pruned += 1
        except (ValueError, OSError):
            pass
    if pruned:
        sys.stderr.write(f"[backfill] pruned {pruned} files older than {cutoff:%Y-%m-%d %H}:00\n")


def auto_mode(out_dir: str, hours: int = AUTO_HOURS,
              poll_seconds: int = AUTO_POLL_SECONDS,
              workers: int = PARALLEL_WORKERS,
              max_back: int = MAX_CYCLES_BACK,
              lat_min: float = -90, lat_max: float = 90,
              lon_min: float = -180, lon_max: float = 180):
    os.makedirs(out_dir, exist_ok=True)

    # ── Initial backfill: populate archive with recent history ──
    _backfill_archive(out_dir, lat_min, lat_max, lon_min, lon_max, workers)

    last_cycle: Optional[datetime] = None

    while True:
        try:
            now = _utcnow()
            latest = _gfs_cycle(now)
            target = latest - timedelta(hours=6) if now <= latest + timedelta(
                minutes=AUTO_CYCLE_GRACE_MINUTES) else latest

            if last_cycle is not None and target <= last_cycle:
                sys.stderr.write(f"[auto] cycle {target:%Y-%m-%d %H}z already processed\n")
                time.sleep(poll_seconds)
                continue

            success = False
            for back in range(max_back + 1):
                trial = target - timedelta(hours=6 * back)
                trial_date = trial.replace(hour=0, minute=0, second=0, microsecond=0)

                # ── FAST probe ──
                if _cycle_available(trial_date, trial.hour):
                    sys.stderr.write(f"[auto] ✅ cycle {trial:%Y-%m-%d %H}z on AWS — downloading\n")
                else:
                    sys.stderr.write(f"[auto] ⏳ cycle {trial:%Y-%m-%d %H}z not yet on AWS — skipping\n")
                    continue

                # ── Stage to .staging, then merge (don't clear old files!) ──
                staging = os.path.join(out_dir, ".staging")
                if os.path.isdir(staging):
                    shutil.rmtree(staging, ignore_errors=True)
                os.makedirs(staging, exist_ok=True)

                # Download 12 forecast hours per cycle (f000-f011).
                # 12h ensures the trailing edge is covered: when a new cycle
                # hasn't appeared on AWS yet (~5.5h latency), the previous
                # cycle's f006-f011 bridge the gap so STREAM-Sat rarely
                # needs the fallback downloader.
                # _forecast_hours is inclusive, so 11 -> [0..11] = 12 files.
                cycle_hours = min(hours, 11)
                results = download_wind_cycle(
                    trial, cycle_hours, lat_min, lat_max, lon_min, lon_max, staging, workers)
                if results:
                    # Always overwrite — the most recent download for a given
                    # valid hour comes from the latest cycle (shortest forecast
                    # lead time), so it replaces any older copy.
                    for f in os.listdir(staging):
                        src = os.path.join(staging, f)
                        dst = os.path.join(out_dir, f)
                        shutil.move(src, dst)
                    try:
                        shutil.rmtree(staging, ignore_errors=True)
                    except Exception:
                        pass

                    last_cycle = trial
                    sys.stderr.write(f"[auto] ✅ {trial:%Y-%m-%d %H}z — {len(results)} files added to archive\n")

                    # ── Prune files older than BACKFILL_HOURS ──
                    cutoff = _utcnow() - timedelta(hours=BACKFILL_HOURS)
                    pruned = 0
                    for f in os.listdir(out_dir):
                        if not (f.startswith("gfs_wind.") and f.endswith(".nc")):
                            continue
                        try:
                            ts = f.replace("gfs_wind.", "").replace(".nc", "")
                            ft = datetime.strptime(ts, "%Y%m%d%H%M")
                            if ft < cutoff:
                                os.remove(os.path.join(out_dir, f))
                                pruned += 1
                        except (ValueError, OSError):
                            pass
                    if pruned:
                        sys.stderr.write(f"[auto] pruned {pruned} files older than {cutoff:%Y-%m-%d %H}:00\n")

                    success = True
                    break
                sys.stderr.write(f"[auto] ❌ download failed for {trial:%Y-%m-%d %H}z\n")

            if not success:
                sys.stderr.write(f"[auto] ⚠ no available cycle, retrying after poll\n")
        except Exception as e:
            sys.stderr.write(f"[auto] error: {e}\n")
        try:
            time.sleep(poll_seconds)
        except KeyboardInterrupt:
            sys.stderr.write("[auto] stopped.\n")
            return

# ── CLI ───────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="GFS 850 hPa wind downloader + archive daemon")
    p.add_argument("--auto-out", help="Archive directory for auto mode")
    p.add_argument("--auto-hours", type=int, default=AUTO_HOURS)
    p.add_argument("--poll-seconds", type=int, default=AUTO_POLL_SECONDS)
    p.add_argument("--workers", type=int, default=PARALLEL_WORKERS)
    p.add_argument("--max-back", type=int, default=MAX_CYCLES_BACK)
    p.add_argument("--lat-min", type=float, default=-90)
    p.add_argument("--lat-max", type=float, default=90)
    p.add_argument("--lon-min", type=float, default=-180)
    p.add_argument("--lon-max", type=float, default=180)
    p.add_argument("--auto-once", action="store_true", help="Single pass")
    args = p.parse_args()

    if args.auto_out:
        if args.auto_once:
            now = _utcnow()
            latest = _gfs_cycle(now)
            target = latest - timedelta(hours=6) if now <= latest + timedelta(
                minutes=AUTO_CYCLE_GRACE_MINUTES) else latest
            os.makedirs(args.auto_out, exist_ok=True)
            for back in range(args.max_back + 1):
                trial = target - timedelta(hours=6 * back)
                trial_date = trial.replace(hour=0, minute=0, second=0, microsecond=0)
                if not _cycle_available(trial_date, trial.hour):
                    sys.stderr.write(f"Cycle {trial:%Y-%m-%d %H}z not on AWS — skipping\n")
                    continue
                results = download_wind_cycle(
                    trial, args.auto_hours, args.lat_min, args.lat_max,
                    args.lon_min, args.lon_max, args.auto_out, args.workers)
                if results:
                    print(f"Wrote {len(results)} wind files for {trial:%Y-%m-%d %H}z")
                    return
            print("No available cycles found.")
            sys.exit(2)
        else:
            auto_mode(args.auto_out, args.auto_hours, args.poll_seconds,
                      args.workers, args.max_back,
                      args.lat_min, args.lat_max, args.lon_min, args.lon_max)
    else:
        p.print_help()

if __name__ == "__main__":
    main()

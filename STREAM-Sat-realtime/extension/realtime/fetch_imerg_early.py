#!/usr/bin/env python3
"""
fetch_imerg_early.py
====================
Author:       Yagmur Derin, University of Iowa
Created:      2026-04-17
License:      MIT (see LICENSE at repository root)

Part of the real-time STREAM-Sat QPE pipeline for the WMO Caribbean
flood forecasting project.

Download the latest N half-hours of IMERG Early Run precipitation from
NASA GES DISC and assemble a STREAM-Sat-ready NetCDF on the project's
0.1° Caribbean grid.

IMERG Early has ~4 h latency, so "latest available" at run-time T is
roughly T − 4 h.

"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
from netCDF4 import Dataset, date2num

try:
    import h5py
except ImportError as e:  # pragma: no cover
    raise SystemExit("This script needs h5py. pip install h5py") from e


log = logging.getLogger("imerg-early")

# Default Caribbean domain that matches IMERG2025_Nov14_19.nc
DEFAULT_DOMAIN = {
    "lat_min": 11.05,
    "lat_max": 22.95,
    "lon_min": -93.95,
    "lon_max": -57.05,
}

GESDISC_BASE = ("https://gpm1.gesdisc.eosdis.nasa.gov/data/GPM_L3/"
                "GPM_3IMERGHHE.07")

# V07C became the operational version on 2026-03-04.
# Files before that date are V07B; from that date onward they are V07C.
IMERG_EARLY_V07C_TRANSITION = datetime(2026, 3, 4, 0, 0, tzinfo=timezone.utc)


def _version_for_date(dt: datetime) -> str:
    """Return the IMERG version string for a given half-hour timestamp."""
    return "V07C" if dt >= IMERG_EARLY_V07C_TRANSITION else "V07B"


def _netrc_session() -> requests.Session:
    s = requests.Session()
    # .netrc is picked up automatically by requests when `auth` isn't set.
    # For URS redirects we also need a cookie file:
    cookie_path = Path.home() / ".urs_cookies"
    cookie_path.touch(exist_ok=True)
    from http.cookiejar import MozillaCookieJar
    cj = MozillaCookieJar(str(cookie_path))
    try:
        cj.load(ignore_discard=True, ignore_expires=True)
    except Exception:
        pass
    s.cookies = cj
    return s


def _imerg_url(halfhour_start: datetime) -> str:
    # File name pattern (V07x):
    # 3B-HHR-E.MS.MRG.3IMERG.20251116-S000000-E002959.0000.V07C.HDF5
    doy = halfhour_start.timetuple().tm_yday
    date_str = halfhour_start.strftime("%Y%m%d")
    s_str = halfhour_start.strftime("%H%M%S")
    e_dt = halfhour_start + timedelta(minutes=29, seconds=59)
    e_str = e_dt.strftime("%H%M%S")
    mmmm = halfhour_start.hour * 60 + halfhour_start.minute
    version = _version_for_date(halfhour_start)
    fname = f"3B-HHR-E.MS.MRG.3IMERG.{date_str}-S{s_str}-E{e_str}.{mmmm:04d}.{version}.HDF5"
    return f"{GESDISC_BASE}/{halfhour_start.year}/{doy:03d}/{fname}"


def _download_one(session: requests.Session, url: str, dest: Path,
                  timeout: int = 180) -> bool:
    if dest.exists() and dest.stat().st_size > 1024:
        return True
    try:
        r = session.get(url, allow_redirects=True, timeout=timeout,
                        stream=True)
    except requests.exceptions.RequestException as e:
        log.warning("  net error for %s: %s", url, e)
        return False
    if r.status_code != 200:
        log.warning("  HTTP %d for %s", r.status_code, url)
        return False
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    return True


def _read_imerg_hdf5(path: Path, lat_min, lat_max, lon_min, lon_max):
    with h5py.File(path, "r") as hf:
        # V07 layout: /Grid/precipitation, /Grid/lat, /Grid/lon
        g = hf["Grid"]
        lat = g["lat"][:]   # ascending, 0.1 deg
        lon = g["lon"][:]   # ascending, 0.1 deg
        # precipitation shape: (time=1, lon, lat) in V07 — check and transpose
        prcp = g["precipitation"][:]
    # Squeeze time dim, and make sure shape ends up (lat, lon)
    prcp = np.squeeze(prcp)
    if prcp.shape == (len(lon), len(lat)):
        prcp = prcp.T
    # Subset to domain
    lat_mask = (lat >= lat_min) & (lat <= lat_max)
    lon_mask = (lon >= lon_min) & (lon <= lon_max)
    prcp = prcp[np.ix_(lat_mask, lon_mask)]
    lat = lat[lat_mask]
    lon = lon[lon_mask]
    # Flip to descending lat to match the existing IMERG NetCDFs
    prcp = prcp[::-1, :]
    lat = lat[::-1]
    # NaN → 0 and mm/hr is the native unit in V07
    prcp = np.where(np.isnan(prcp), 0.0, prcp).astype(np.float32)
    return prcp, lat.astype(np.float32), lon.astype(np.float32)


def _assemble_netcdf(out_path: Path,
                     prcp_stack: np.ndarray,
                     times: list[datetime],
                     lat: np.ndarray, lon: np.ndarray,
                     version: str = "V07x"):
    ny, nx = len(lat), len(lon)
    nt = len(times)
    # incoming stack is (time, lat, lon); transpose to (lat, lon, time)
    prcp_out = np.transpose(prcp_stack, (1, 2, 0))

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

        pv = nc.createVariable("prcp", "f4", ("latitude", "longitude", "time"),
                               zlib=True, complevel=4,
                               chunksizes=(ny, nx, min(48, nt)))
        pv.units = "mm/hr"
        pv.long_name = "IMERG Early Run half-hourly precipitation rate"
        pv[:, :, :] = prcp_out

        nc.title = "IMERG Early Run, Caribbean domain, assembled for STREAM-Sat"
        nc.source = f"NASA GES DISC {GESDISC_BASE} ({version})"
        nc.created = datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(end: datetime, hours: int, domain: dict, out_path: Path,
          tmp_dir: Path) -> Path:
    session = _netrc_session()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Build half-hourly start times: end is the END of the last half-hour.
    # We want `hours` hours of data → 2*hours slots.
    n_slots = hours * 2
    # Align `end` onto a half-hour boundary
    end = end.replace(second=0, microsecond=0)
    if end.minute not in (0, 30):
        # Floor to the nearest past half-hour
        end = end.replace(minute=(end.minute // 30) * 30)
    # last half-hour start:
    last_start = end - timedelta(minutes=30)
    starts = [last_start - timedelta(minutes=30 * i) for i in range(n_slots)]
    starts = sorted(starts)

    log.info("Target range: %s .. %s  (%d half-hours)",
             starts[0].strftime("%Y-%m-%d %H:%M"),
             (starts[-1] + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M"),
             n_slots)

    prcp_stack, kept_times = [], []
    missing = 0
    ref_lat = ref_lon = None
    for st in starts:
        url = _imerg_url(st)
        dest = tmp_dir / Path(url).name
        ok = _download_one(session, url, dest)
        if not ok:
            missing += 1
            log.warning("  missing: %s", st.strftime("%Y-%m-%d %H:%M"))
            continue
        try:
            p, lat, lon = _read_imerg_hdf5(dest, **domain)
        except Exception as e:
            log.warning("  read failed %s: %s", dest.name, e)
            missing += 1
            continue
        if ref_lat is None:
            ref_lat, ref_lon = lat, lon
        prcp_stack.append(p.astype(np.float32))
        kept_times.append(st)

    if not prcp_stack:
        raise RuntimeError("No IMERG files could be downloaded/read.")

    log.info("Downloaded %d/%d files (missing %d)",
             len(prcp_stack), n_slots, missing)

    # Determine the version from the most recent valid half-hour (most authoritative).
    latest_version = _version_for_date(kept_times[-1])
    stack = np.stack(prcp_stack, axis=0)   # (t, lat, lon)
    _assemble_netcdf(out_path, stack, kept_times, ref_lat, ref_lon,
                     version=latest_version)
    size_mb = out_path.stat().st_size / 1e6
    log.info("Wrote %s (%.1f MB)", out_path, size_mb)
    return out_path

def _parse_domain(s: str | None) -> dict:
    if s is None:
        return DEFAULT_DOMAIN
    lat_min, lat_max, lon_min, lon_max = [float(x) for x in s.split(",")]
    return {"lat_min": lat_min, "lat_max": lat_max,
            "lon_min": lon_min, "lon_max": lon_max}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("Usage")[0])
    ap.add_argument("--end", default=None,
                    help="End datetime UTC (ISO 8601). Default: "
                         "4 h before now (max IMERG Early latency).")
    ap.add_argument("--hours", type=int, default=48,
                    help="How many hours of IMERG Early to fetch (default 48).")
    ap.add_argument("--out", required=True, help="Output NetCDF path.")
    ap.add_argument("--tmp-dir", default="./imerg_tmp",
                    help="Directory for raw HDF5 downloads.")
    ap.add_argument("--domain", default=None,
                    help="'lat_min,lat_max,lon_min,lon_max'. "
                         f"Default: {DEFAULT_DOMAIN}")
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args()

    logging.basicConfig(level=a.log_level,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    if a.end is None:
        end = datetime.now(timezone.utc) - timedelta(hours=4)
    else:
        end = datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc)

    fetch(end, a.hours, _parse_domain(a.domain),
          Path(a.out), Path(a.tmp_dir))


if __name__ == "__main__":
    main()

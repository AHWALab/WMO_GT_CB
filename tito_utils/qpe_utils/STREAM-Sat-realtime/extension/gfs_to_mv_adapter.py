#!/usr/bin/env python3
"""
gfs_to_mv_adapter.py
====================
Convert GFS 850 hPa U/V winds (m/s) produced by download_gfs_winds.py into
STREAM-Sat motion-vector NetCDF in pixels per half-hour, on the IMERG grid.

Author:       Yagmur Derin, University of Iowa
Created:      2026-04-17
License:      MIT (see LICENSE at repository root)

Context:      Real-time STREAM-Sat QPE pipeline for the WMO Caribbean flood
              forecasting project. Built atop STREAM-Sat:
                Hartke, S.H. et al. (2022), Water Resources Research, 58(8).
                Peng, K. et al. (2025), Water Resources Research.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from netCDF4 import Dataset, date2num, num2date


# ---------------------------------------------------------------------------
# Geometry constants
# ---------------------------------------------------------------------------

R_EARTH_M = 6371000.0                    # mean Earth radius [m]
DEG_TO_M_LAT = np.pi / 180.0 * R_EARTH_M  # ~111195 m per degree of latitude
DT_SEC = 1800.0                          # half-hourly timestep

def read_imerg_grid(imerg_path: str | Path):
    with Dataset(imerg_path) as ds:
        lat = ds.variables["latitude"][:].astype("float64")
        lon = ds.variables["longitude"][:].astype("float64")
        time_arr = ds.variables["time"][:]
        time_units = ds.variables["time"].units
    return np.asarray(lat), np.asarray(lon), np.asarray(time_arr), time_units


def read_gfs(gfs_path: str | Path):
    with Dataset(gfs_path) as ds:
        gfs_lat = ds.variables["lat"][:].astype("float64")
        gfs_lon = ds.variables["lon"][:].astype("float64")
        t_raw = ds.variables["time"][:]
        t_units = ds.variables["time"].units
        times = num2date(t_raw, t_units, only_use_cftime_datetimes=False)
        # file stores (lat, lon, time) — reorient to (time, lat, lon) for interpolation
        U = ds.variables["U_MV"][:]   # (lat, lon, time)
        V = ds.variables["V_MV"][:]   # (lat, lon, time)
        U = np.transpose(np.asarray(U), (2, 0, 1))  # (time, lat, lon)
        V = np.transpose(np.asarray(V), (2, 0, 1))
    return U, V, gfs_lat, gfs_lon, times

def nearest_index_map(target: np.ndarray, source_asc: np.ndarray):
    source_asc = np.asarray(source_asc, dtype="float64")
    target = np.asarray(target, dtype="float64")
    # tolerance = 0.6 * spacing (slightly > 0.5 to avoid floating-point slips)
    spacing = np.median(np.abs(np.diff(source_asc))) if len(source_asc) > 1 else 0.1
    tol = 0.6 * spacing

    out = np.full(target.shape, -1, dtype=np.int64)
    # For each target value, find nearest in source
    idx = np.searchsorted(source_asc, target)
    idx = np.clip(idx, 0, len(source_asc) - 1)

    # candidates: idx and idx-1; choose nearer
    left_idx = np.clip(idx - 1, 0, len(source_asc) - 1)
    d_right = np.abs(source_asc[idx] - target)
    d_left = np.abs(source_asc[left_idx] - target)
    chosen = np.where(d_left < d_right, left_idx, idx)
    chosen_dist = np.minimum(d_left, d_right)

    within = chosen_dist <= tol
    out[within] = chosen[within]
    return out


def regrid_gfs_to_imerg(
    U_time_lat_lon: np.ndarray,
    V_time_lat_lon: np.ndarray,
    gfs_lat: np.ndarray,
    gfs_lon: np.ndarray,
    imerg_lat: np.ndarray,
    imerg_lon: np.ndarray,
):
    # Ensure GFS lat is ascending for index search; if it came ascending we're fine
    if gfs_lat[0] > gfs_lat[-1]:
        gfs_lat = gfs_lat[::-1]
        U_time_lat_lon = U_time_lat_lon[:, ::-1, :]
        V_time_lat_lon = V_time_lat_lon[:, ::-1, :]
    if gfs_lon[0] > gfs_lon[-1]:
        gfs_lon = gfs_lon[::-1]
        U_time_lat_lon = U_time_lat_lon[:, :, ::-1]
        V_time_lat_lon = V_time_lat_lon[:, :, ::-1]

    lat_idx = nearest_index_map(imerg_lat, gfs_lat)   # shape (ny,)
    lon_idx = nearest_index_map(imerg_lon, gfs_lon)   # shape (nx,)

    ny = len(imerg_lat)
    nx = len(imerg_lon)
    nt = U_time_lat_lon.shape[0]

    U_out = np.zeros((nt, ny, nx), dtype=np.float32)
    V_out = np.zeros((nt, ny, nx), dtype=np.float32)

    valid_lat = lat_idx >= 0
    valid_lon = lon_idx >= 0

    if not valid_lat.any() or not valid_lon.any():
        raise RuntimeError("GFS grid does not overlap with IMERG grid at all.")

    # Build index arrays for fancy indexing
    yy = np.where(valid_lat)[0]              # IMERG y-indices with a GFS counterpart
    xx = np.where(valid_lon)[0]
    gy = lat_idx[yy]
    gx = lon_idx[xx]

    # (time, yy, xx) slice
    U_slice = U_time_lat_lon[:, gy[:, None], gx[None, :]]
    V_slice = V_time_lat_lon[:, gy[:, None], gx[None, :]]

    U_out[:, yy[:, None], xx[None, :]] = U_slice
    V_out[:, yy[:, None], xx[None, :]] = V_slice

    n_missing = (ny * nx) - (valid_lat.sum() * valid_lon.sum())
    coverage = 100.0 * (valid_lat.sum() * valid_lon.sum()) / (ny * nx)
    return U_out, V_out, coverage, n_missing

def mps_to_pixels_per_halfhour(
    U_mps: np.ndarray,
    V_mps: np.ndarray,
    lat_vec: np.ndarray,
    d_deg: float = 0.1,
    dt_sec: float = DT_SEC,
):

    dy_m = d_deg * DEG_TO_M_LAT
    cos_lat = np.cos(np.radians(lat_vec))
    # avoid divide-by-zero at the poles (not an issue in the Caribbean but be safe)
    cos_lat = np.clip(cos_lat, 1e-6, 1.0)
    dx_m = d_deg * DEG_TO_M_LAT * cos_lat   # shape (ny,)

    # Broadcast: U,V have shape (nt, ny, nx)
    dpix_x = U_mps * (dt_sec / dx_m[np.newaxis, :, np.newaxis])
    dpix_y = V_mps * (dt_sec / dy_m)

    return dpix_x.astype(np.float32), dpix_y.astype(np.float32)

def align_times_to_imerg(
    U_t_y_x: np.ndarray,
    V_t_y_x: np.ndarray,
    gfs_times,
    imerg_time_raw: np.ndarray,
    imerg_time_units: str,
):
    imerg_dts = num2date(imerg_time_raw, imerg_time_units,
                         only_use_cftime_datetimes=False)
    # GFS times come from netCDF4 num2date which returns cftime or datetime;
    # normalize to python datetime
    gfs_dts = [datetime(t.year, t.month, t.day, t.hour, t.minute, t.second)
               for t in gfs_times]
    imerg_dts_py = [datetime(t.year, t.month, t.day, t.hour, t.minute, t.second)
                    for t in imerg_dts]

    gfs_epoch = np.array([(t - datetime(1970, 1, 1)).total_seconds()
                          for t in gfs_dts])
    imerg_epoch = np.array([(t - datetime(1970, 1, 1)).total_seconds()
                            for t in imerg_dts_py])

    nt = len(imerg_dts_py)
    _, ny, nx = U_t_y_x.shape
    U_out = np.zeros((nt, ny, nx), dtype=np.float32)
    V_out = np.zeros((nt, ny, nx), dtype=np.float32)

    # half-step tolerance (15 min)
    tol = 0.5 * DT_SEC
    covered = 0
    for i, ie in enumerate(imerg_epoch):
        j = np.argmin(np.abs(gfs_epoch - ie))
        if abs(gfs_epoch[j] - ie) <= tol:
            U_out[i] = U_t_y_x[j]
            V_out[i] = V_t_y_x[j]
            covered += 1

    return U_out, V_out, covered

def write_mv_netcdf(
    out_path: str | Path,
    dpix_x: np.ndarray,
    dpix_y: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    time_raw: np.ndarray,
    time_units: str,
    source_note: str,
):
    out_path = Path(out_path)
    with Dataset(str(out_path), "w", format="NETCDF4", clobber=True) as nc:
        nc.createDimension("lat", len(lat))
        nc.createDimension("lon", len(lon))
        nc.createDimension("time", len(time_raw))

        tv = nc.createVariable("time", "d", ("time",))
        tv.units = time_units
        tv.calendar = "gregorian"
        tv[:] = time_raw

        lat_v = nc.createVariable("latitude", "f4", ("lat",), zlib=True)
        lat_v.units = "degrees_north"
        lat_v.long_name = "latitude"
        lat_v[:] = lat

        lon_v = nc.createVariable("longitude", "f4", ("lon",), zlib=True)
        lon_v.units = "degrees_east"
        lon_v.long_name = "longitude"
        lon_v[:] = lon

        # transpose (time, lat, lon) -> (lat, lon, time)
        Ut = np.transpose(dpix_x, (1, 2, 0))
        Vt = np.transpose(dpix_y, (1, 2, 0))

        u = nc.createVariable("U_MV", "f4", ("lat", "lon", "time"),
                              zlib=True, least_significant_digit=4)
        u.units = "pixels/30min"
        u.long_name = ("eastward pixel displacement per 30-min step "
                       "(derived from GFS 850 hPa U)")

        v = nc.createVariable("V_MV", "f4", ("lat", "lon", "time"),
                              zlib=True, least_significant_digit=4)
        v.units = "pixels/30min"
        v.long_name = ("northward pixel displacement per 30-min step "
                       "(derived from GFS 850 hPa V)")

        u[:, :, :] = Ut
        v[:, :, :] = Vt

        nc.title = "STREAM-Sat motion vectors derived from GFS 850 hPa winds"
        nc.source = source_note
        nc.note = ("U_MV and V_MV are stored in PIXELS per 30 minutes on the "
                   "IMERG 0.1 deg grid. STREAM_NoiseGeneration uses these values "
                   "as integer pixel displacements in the semi-Lagrangian step.")
        nc.created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def main():
    parser = argparse.ArgumentParser(
        description="Adapt GFS 850 hPa winds (m/s) to STREAM-Sat motion vectors "
                    "(pixels per 30 min) on the IMERG grid."
    )
    parser.add_argument("--gfs", required=True,
                        help="Input GFS NetCDF from download_gfs_winds.py")
    parser.add_argument("--imerg", required=True,
                        help="IMERG NetCDF whose grid and time axis define the target")
    parser.add_argument("--out", required=True,
                        help="Output motion-vector NetCDF path")
    args = parser.parse_args()

    imerg_lat, imerg_lon, imerg_t_raw, imerg_t_units = read_imerg_grid(args.imerg)
    U_gfs, V_gfs, gfs_lat, gfs_lon, gfs_times = read_gfs(args.gfs)

    U_r, V_r, coverage_pct, _ = regrid_gfs_to_imerg(
        U_gfs, V_gfs, gfs_lat, gfs_lon, imerg_lat, imerg_lon
    )

    dpix_x_t, dpix_y_t = mps_to_pixels_per_halfhour(
        U_r, V_r, imerg_lat, d_deg=0.1, dt_sec=DT_SEC
    )

    U_aligned, V_aligned, n_covered = align_times_to_imerg(
        dpix_x_t, dpix_y_t, gfs_times, imerg_t_raw, imerg_t_units
    )

    source_note = (
        f"Regridded from GFS 850 hPa winds (m/s) in {Path(args.gfs).name} "
        f"to IMERG 0.1 deg grid in {Path(args.imerg).name}; "
        f"converted to pixel displacements per 30-min step using "
        f"0.1 deg * 111195 m/deg spacing with cos(lat) correction."
    )
    write_mv_netcdf(
        args.out, U_aligned, V_aligned,
        imerg_lat, imerg_lon, imerg_t_raw, imerg_t_units, source_note
    )

    size_mb = Path(args.out).stat().st_size / (1024 * 1024)


if __name__ == "__main__":
    main()

"""Empirical quantile mapping of operational GFS covariates onto the GEFS
reforecast training climatology (per grid cell, per month, per covariate).

Absorbs the GEFS v12 (GFS v15 physics) vs. current-GFS climatology mismatch —
the operational analogue of the CESM2->ERA5 mapping in Liu et al. (2024).
If no mapping file exists, mapping is the identity (with a warning).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr

N_Q = 101
QUANTILES = np.linspace(0, 1, N_Q)


def build_qmap(train_da: xr.DataArray, ops_da: xr.DataArray) -> xr.Dataset:
    out = {}
    for name, da in (("train_q", train_da), ("ops_q", ops_da)):
        has_time = "time" in da.coords
        qs = []
        for m in range(1, 13):
            sub = da.sel(time=da["time"].dt.month == m) if has_time else da
            qs.append(sub.quantile(QUANTILES, dim=sub.dims[0]).values)
        out[name] = np.stack(qs)  # (12, N_Q, ny, nx)
    ds = xr.Dataset(
        {k: (("month", "quantile", "lat", "lon"), v) for k, v in out.items()},
        coords={
            "month": np.arange(1, 13),
            "quantile": QUANTILES,
            "lat": train_da.lat.values,
            "lon": train_da.lon.values,
        },
    )
    return ds


def apply_qmap(field: np.ndarray, qmap: xr.Dataset | None, month: int) -> np.ndarray:
    if qmap is None:
        return field
    ops_q = qmap["ops_q"].sel(month=month).values      # (N_Q, ny, nx)
    train_q = qmap["train_q"].sel(month=month).values
    T, ny, nx = field.shape
    out = np.empty_like(field)
    for j in range(ny):
        for i in range(nx):
            out[:, j, i] = np.interp(field[:, j, i], ops_q[:, j, i], train_q[:, j, i])
    return out

N_WQ = 61
WET_QUANTILES = np.concatenate([np.linspace(0.0, 0.96, 49), np.linspace(0.97, 1.0, 12)])


def build_intensity_qmap(
    src: np.ndarray, obs: np.ndarray, months: np.ndarray,
    lat: np.ndarray, lon: np.ndarray, wet_threshold: float,
) -> xr.Dataset:
    ny, nx = src.shape[1:]
    src_q = np.full((12, N_WQ + 2, ny, nx), np.nan, dtype=np.float32)
    dst_q = np.full_like(src_q, np.nan)
    for m in range(1, 13):
        sel = months == m
        if not sel.any():
            continue
        s, o = src[sel], obs[sel]
        for j in range(ny):
            for i in range(nx):
                sc, oc = s[:, j, i], o[:, j, i]
                wet_frac = float((oc > wet_threshold).mean())
                thr = float(np.quantile(sc, 1.0 - np.clip(wet_frac, 1e-4, 1.0)))
                sw = sc[sc > thr]
                ow = oc[oc > wet_threshold]
                if sw.size < 20 or ow.size < 20:
                    continue
                sq = np.quantile(sw, WET_QUANTILES)
                oq = np.quantile(ow, WET_QUANTILES)
                src_q[m - 1, :, j, i] = np.maximum.accumulate(
                    np.concatenate([[0.0, thr], sq]))
                dst_q[m - 1, :, j, i] = np.maximum.accumulate(
                    np.concatenate([[0.0, wet_threshold], oq]))
    return xr.Dataset(
        {"src_q": (("month", "node", "lat", "lon"), src_q),
         "dst_q": (("month", "node", "lat", "lon"), dst_q)},
        coords={"month": np.arange(1, 13), "node": np.arange(N_WQ + 2),
                "lat": lat, "lon": lon},
        attrs={"wet_threshold": wet_threshold},
    )


def apply_intensity_qmap(field: np.ndarray, qmap: xr.Dataset | None, month: int) -> np.ndarray:
    if qmap is None:
        return field
    src_q = qmap["src_q"].sel(month=month).values         # (node, ny, nx)
    dst_q = qmap["dst_q"].sel(month=month).values
    out = field.copy()
    ny, nx = field.shape[1:]
    for j in range(ny):
        for i in range(nx):
            sq, dq = src_q[:, j, i], dst_q[:, j, i]
            if not np.isfinite(sq).all():
                continue
            v = np.interp(field[:, j, i], sq, dq)
            # linear tail extrapolation above the highest mapped quantile
            hi = field[:, j, i] > sq[-1]
            if hi.any():
                slope = (dq[-1] - dq[-2]) / max(sq[-1] - sq[-2], 1e-6)
                v[hi] = dq[-1] + slope * (field[hi, j, i] - sq[-1])
            out[:, j, i] = v
    return out


def apply_intensity_qmap_monthly(
    arr: np.ndarray, months: np.ndarray, qmap: xr.Dataset | None
) -> np.ndarray:
    if qmap is None:
        return arr
    out = arr.copy()
    for m in np.unique(months):
        sel = months == m
        out[sel] = apply_intensity_qmap(arr[sel], qmap, int(m))
    return out


def load_intensity_qmaps(params_root: Path, covariates: list[str]) -> dict:
    maps = {}
    for cov in covariates:
        f = params_root / f"qmap_imerg_{cov}.nc"
        maps[cov] = xr.load_dataset(f) if f.exists() else None
    return maps


def load_qmaps(params_root: Path, covariates: list[str]) -> dict:
    maps = {}
    for cov in covariates:
        f = params_root / f"qmap_{cov}.nc"
        maps[cov] = xr.load_dataset(f) if f.exists() else None
        if maps[cov] is None:
            print(f"WARNING: no quantile map for {cov}; using identity")
    return maps

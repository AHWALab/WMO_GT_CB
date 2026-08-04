"""Download & subset GEFS v12 reforecast (AWS open data) for training.

Requires: cfgrib + eccodes. Run where NOAA S3 is reachable.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from ..config import Config

VAR_FILES = {"PR": "apcp_sfc", "PW": "pwat_eatm"}


def _deaccumulate(pr3: xr.DataArray, step_h: np.ndarray) -> np.ndarray:
    vals = pr3.values.copy()
    for k, s in enumerate(step_h):
        if s % 6 == 0 and k > 0 and step_h[k - 1] == s - 3:
            vals[k] = vals[k] - pr3.values[k - 1]
    return np.clip(vals, 0.0, None)


def _download(url: str, dest: Path):
    subprocess.run(
        ["curl", "-sSf", "--retry", "3", "-o", str(dest), url], check=True
    )


def _subset(ds: xr.Dataset, cfg: Config) -> xr.Dataset:
    lat0, lat1 = cfg.domain.sim_lat
    lon0, lon1 = cfg.domain.sim_lon
    lon = ds.longitude.values
    if lon.max() > 180:  # 0..360 -> -180..180
        ds = ds.assign_coords(longitude=(("longitude"), np.where(lon > 180, lon - 360, lon)))
        ds = ds.sortby("longitude")
    if ds.latitude.values[0] > ds.latitude.values[-1]:
        ds = ds.sortby("latitude")
    return ds.sel(latitude=slice(lat0 - 0.5, lat1 + 0.5), longitude=slice(lon0 - 0.5, lon1 + 0.5))


def fetch_init(cfg: Config, init: pd.Timestamp, member: str, keep_grib: bool = False) -> Path | None:
    out = cfg.path("data_root", "gefs", f"{init:%Y%m%d%H}_{member}.nc")
    if out.exists():
        return out

    tmpdir = Path(tempfile.mkdtemp())
    dsets = {}
    try:
        for cov, fvar in VAR_FILES.items():
            fname = f"{fvar}_{init:%Y%m%d%H}_{member}.grib2"
            url = (
                f"{cfg.sources['gefs_reforecast_bucket']}/{init:%Y}/{init:%Y%m%d%H}/"
                f"{member}/Days:1-10/{fname}"
            )
            gpath = tmpdir / fname
            _download(url, gpath)
            ds = xr.open_dataset(gpath, engine="cfgrib", backend_kwargs={"indexpath": ""})
            ds = _subset(ds, cfg)
            dsets[cov] = ds.load()

        leads = cfg.leads  # hourly
        # APCP: bucket accumulation (kg/m2, 6-h reset) -> per-3h -> rate mm/h,
        # assign to each hour within the step. PWAT: instantaneous, linear interp.
        pr3 = dsets["PR"][list(dsets["PR"].data_vars)[0]]  # (step, lat, lon)
        pw3 = dsets["PW"][list(dsets["PW"].data_vars)[0]]
        step_h = (pr3.step / np.timedelta64(1, "h")).values.astype(int)

        pr_rate = pr3.copy(data=_deaccumulate(pr3, step_h)) / 3.0  # mm/h over the 3-h window ending at step
        pr_hourly = np.empty((leads.size, *pr3.shape[1:]), dtype=np.float32)
        for i, lead in enumerate(leads):
            j = np.searchsorted(step_h, lead)  # window ending at step >= lead
            j = min(j, len(step_h) - 1)
            pr_hourly[i] = pr_rate.isel(step=j).values

        pw_hourly = (
            pw3.assign_coords(step=step_h)
            .interp(step=leads, method="linear", kwargs={"fill_value": "extrapolate"})
            .values.astype(np.float32)
        )

        out_ds = xr.Dataset(
            {
                "PR": (("lead", "lat", "lon"), pr_hourly),
                "PW": (("lead", "lat", "lon"), pw_hourly),
            },
            coords={
                "lead": leads,
                "lat": dsets["PR"].latitude.values,
                "lon": dsets["PR"].longitude.values,
            },
            attrs={"init": f"{init:%Y-%m-%dT%H}", "member": member},
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out_ds.to_netcdf(out)
        return out
    except subprocess.CalledProcessError:
        print(f"  missing/failed: {init:%Y%m%d} {member}")
        return None
    finally:
        if not keep_grib:
            shutil.rmtree(tmpdir, ignore_errors=True)


def fetch_range(cfg: Config, y0: int, y1: int, members: list[str]):
    inits = pd.date_range(f"{y0}-01-01", f"{y1}-12-31", freq="D") + pd.Timedelta(hours=0)
    for init in inits:
        for m in members:
            fetch_init(cfg, init, m)

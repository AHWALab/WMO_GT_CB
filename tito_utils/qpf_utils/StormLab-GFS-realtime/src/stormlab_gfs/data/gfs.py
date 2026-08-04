"""Operational GFS fetch (AWS noaa-gfs-bdp-pds, .idx byte-range subsetting).
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from ..config import Config

FIELDS = {
    "APCP": ":APCP:surface:",
    "PW": ":PWAT:entire atmosphere",
    "U850": ":UGRD:850 mb:",
    "V850": ":VGRD:850 mb:",
}


def latest_cycle(now: pd.Timestamp | None = None, min_age_h: float = 4.5) -> pd.Timestamp:
    now = now or pd.Timestamp.utcnow().tz_localize(None)
    cyc = now.floor("6h")
    while (now - cyc) < pd.Timedelta(hours=min_age_h):
        cyc -= pd.Timedelta(hours=6)
    return cyc


def _grib_url(cfg: Config, cycle: pd.Timestamp, lead: int) -> str:
    return (
        f"{cfg.sources['gfs_bucket']}/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/"
        f"gfs.t{cycle:%H}z.pgrb2.0p25.f{lead:03d}"
    )


def _fetch_fields(url: str, dest: Path) -> None:
    idx = subprocess.run(
        ["curl", "-sSf", "--retry", "3", url + ".idx"], capture_output=True, check=True
    ).stdout.decode()
    lines = idx.strip().split("\n")
    ranges = []
    for i, line in enumerate(lines):
        if any(pat in line for pat in FIELDS.values()):
            start = int(line.split(":")[1])
            end = ""
            if i + 1 < len(lines):
                end = str(int(lines[i + 1].split(":")[1]) - 1)
            ranges.append(f"{start}-{end}")
    with open(dest, "wb") as f:
        for r in ranges:
            chunk = subprocess.run(
                ["curl", "-sSf", "--retry", "3", "-r", r, url],
                capture_output=True, check=True,
            ).stdout
            f.write(chunk)


def _subset(da: xr.DataArray, cfg: Config) -> xr.DataArray:
    lat0, lat1 = cfg.domain.sim_lat
    lon0, lon1 = cfg.domain.sim_lon
    lon = da.longitude.values
    if lon.max() > 180:
        da = da.assign_coords(longitude=np.where(lon > 180, lon - 360, lon)).sortby("longitude")
    da = da.sortby("latitude")
    return da.sel(latitude=slice(lat0 - 0.5, lat1 + 0.5), longitude=slice(lon0 - 0.5, lon1 + 0.5))


def fetch_cycle(cfg: Config, cycle: pd.Timestamp) -> xr.Dataset:
    leads = np.arange(cfg.forecast["lead_start"] - 1, cfg.forecast["lead_end"] + 1)
    raw = {k: [] for k in FIELDS}
    lat = lon = None
    with tempfile.TemporaryDirectory() as tmp:
        for lead in leads:
            g = Path(tmp, f"f{lead:03d}.grib2")
            _fetch_fields(_grib_url(cfg, cycle, int(lead)), g)
            for k, kwargs in {
                "APCP": dict(filter_by_keys={"shortName": "tp"}),
                "PW": dict(filter_by_keys={"shortName": "pwat"}),
                "U850": dict(filter_by_keys={"shortName": "u", "level": 850}),
                "V850": dict(filter_by_keys={"shortName": "v", "level": 850}),
            }.items():
                ds = xr.open_dataset(
                    g, engine="cfgrib",
                    backend_kwargs={"indexpath": "", **kwargs},
                )
                da = _subset(ds[list(ds.data_vars)[0]], cfg)
                raw[k].append(da.values)
                lat, lon = da.latitude.values, da.longitude.values

    arr = {k: np.stack(v) for k, v in raw.items()}
    # de-bucket APCP -> hourly rate mm/h
    pr = np.zeros_like(arr["APCP"])
    for i, lead in enumerate(leads):
        if i == 0 or lead % 6 == 1:
            pr[i] = arr["APCP"][i]
        else:
            pr[i] = arr["APCP"][i] - arr["APCP"][i - 1]
    pr = np.clip(pr, 0, None)

    sel = leads >= cfg.forecast["lead_start"]
    ds = xr.Dataset(
        {
            "PR": (("lead", "lat", "lon"), pr[sel]),
            "PW": (("lead", "lat", "lon"), arr["PW"][sel]),
            "U850": (("lead", "lat", "lon"), arr["U850"][sel]),
            "V850": (("lead", "lat", "lon"), arr["V850"][sel]),
        },
        coords={"lead": leads[sel], "lat": lat, "lon": lon},
        attrs={"cycle": f"{cycle:%Y-%m-%dT%H}"},
    )
    return ds

"""Parameter grid I/O (netCDF) shared by training and operations."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr


def save_param_grids(path: Path, lat, lon, lead_bins, logit, tngd, meta: dict | None = None):
    ds = xr.Dataset(
        {
            "logit": (("lead_bin", "logit_param", "lat", "lon"), logit.astype(np.float64)),
            "tngd": (("lead_bin", "tngd_param", "lat", "lon"), tngd.astype(np.float64)),
        },
        coords={
            "lat": lat,
            "lon": lon,
            "lead_bin": np.arange(len(lead_bins)),
            "lead_bin_lo": ("lead_bin", [b[0] for b in lead_bins]),
            "lead_bin_hi": ("lead_bin", [b[1] for b in lead_bins]),
        },
        attrs={"description": "StormLab-GFS per-grid distribution parameters", **(meta or {})},
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(path)
    return ds


def load_param_grids(path: Path) -> xr.Dataset:
    return xr.load_dataset(path)

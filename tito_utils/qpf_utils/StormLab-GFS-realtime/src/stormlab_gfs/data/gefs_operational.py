"""Operational GEFS member fetch (AWS noaa-gefs-pds, .idx byte-range subsetting).

GFS (gfs.py) remains the deterministic fallback path.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from ..config import Config
from ..noise import interp_to_grid
from .gefs_reforecast import _deaccumulate
from .gfs import _subset

MEMBERS_ALL = ["gec00"] + [f"gep{n:02d}" for n in range(1, 31)]


def latest_cycle(now: pd.Timestamp | None = None, min_age_h: float = 5.0) -> pd.Timestamp:
    now = now or pd.Timestamp.utcnow().tz_localize(None)
    cyc = now.floor("6h")
    while (now - cyc) < pd.Timedelta(hours=min_age_h):
        cyc -= pd.Timedelta(hours=6)
    return cyc


def _urls(cfg: Config, cycle: pd.Timestamp, member: str, lead: int) -> dict:
    base = f"{cfg.sources['gefs_bucket']}/gefs.{cycle:%Y%m%d}/{cycle:%H}/atmos"
    return {
        "sp25": f"{base}/pgrb2sp25/{member}.t{cycle:%H}z.pgrb2s.0p25.f{lead:03d}",
        "ap5": f"{base}/pgrb2ap5/{member}.t{cycle:%H}z.pgrb2a.0p50.f{lead:03d}",
    }


def _fetch_ranges(url: str, patterns: list[str], dest: Path) -> None:
    idx = subprocess.run(
        ["curl", "-sSf", "--retry", "3", url + ".idx"],
        capture_output=True, check=True,
    ).stdout.decode()
    lines = idx.strip().split("\n")
    with open(dest, "ab") as f:
        for i, line in enumerate(lines):
            if not any(p in line for p in patterns):
                continue
            start = int(line.split(":")[1])
            end = str(int(lines[i + 1].split(":")[1]) - 1) if i + 1 < len(lines) else ""
            f.write(subprocess.run(
                ["curl", "-sSf", "--retry", "3", "-r", f"{start}-{end}", url],
                capture_output=True, check=True,
            ).stdout)


def _open_var(path: Path, **filter_keys) -> xr.DataArray:
    ds = xr.open_dataset(
        path, engine="cfgrib",
        backend_kwargs={"indexpath": "", "filter_by_keys": filter_keys},
    )
    return ds[list(ds.data_vars)[0]]


def fetch_cycle_member(cfg: Config, cycle: pd.Timestamp, member: str) -> xr.Dataset:
    leads3 = np.arange(3, cfg.forecast["lead_end"] + 3, 3)   # 3-hourly steps
    raw = {"APCP": [], "PW": [], "U850": [], "V850": []}
    lat = lon = None
    with tempfile.TemporaryDirectory() as tmp:
        for lead in leads3:
            u = _urls(cfg, cycle, member, int(lead))
            gs = Path(tmp, f"s{lead:03d}.grib2")
            ga = Path(tmp, f"a{lead:03d}.grib2")
            _fetch_ranges(u["sp25"], [":APCP:surface:", ":PWAT:entire atmosphere"], gs)
            _fetch_ranges(u["ap5"], [":UGRD:850 mb:", ":VGRD:850 mb:"], ga)
            apcp = _subset(_open_var(gs, shortName="tp"), cfg)
            pw = _subset(_open_var(gs, shortName="pwat"), cfg)
            uu = _subset(_open_var(ga, shortName="u", level=850), cfg)
            vv = _subset(_open_var(ga, shortName="v", level=850), cfg)
            lat, lon = apcp.latitude.values, apcp.longitude.values
            raw["APCP"].append(apcp.values)
            raw["PW"].append(pw.values)
            # winds are 0.5 deg -> regrid onto the 0.25-deg covariate grid
            raw["U850"].append(interp_to_grid(
                uu.values, uu.latitude.values, uu.longitude.values, lat, lon))
            raw["V850"].append(interp_to_grid(
                vv.values, vv.latitude.values, vv.longitude.values, lat, lon))

    arr = {k: np.stack(v) for k, v in raw.items()}
    # de-bucket APCP (6-h resets, 3-h steps) -> mm/h over each 3-h window
    pr3 = xr.DataArray(arr["APCP"], dims=("step", "y", "x"))
    rate3 = _deaccumulate(pr3, leads3) / 3.0

    leads = cfg.leads
    pr_hourly = np.empty((leads.size, lat.size, lon.size), dtype=np.float32)
    for i, lead in enumerate(leads):
        j = min(int(np.searchsorted(leads3, lead)), leads3.size - 1)
        pr_hourly[i] = rate3[j]

    def interp_hourly(a):
        out = np.empty((leads.size, lat.size, lon.size), dtype=np.float32)
        for j in range(lat.size):
            for i in range(lon.size):
                out[:, j, i] = np.interp(leads, leads3, a[:, j, i])
        return out

    ds = xr.Dataset(
        {
            "PR": (("lead", "lat", "lon"), pr_hourly),
            "PW": (("lead", "lat", "lon"), interp_hourly(arr["PW"])),
            "U850": (("lead", "lat", "lon"), interp_hourly(arr["U850"])),
            "V850": (("lead", "lat", "lon"), interp_hourly(arr["V850"])),
        },
        coords={"lead": leads, "lat": lat, "lon": lon},
        attrs={"cycle": f"{cycle:%Y-%m-%dT%H}", "member": member},
    )
    return ds


def run_ensemble_cycle(cfg: Config, cycle: pd.Timestamp, n_forcing: int,
                       n_total: int, out_path: Path | None = None) -> Path:
    from ..operational import run_cycle

    members = MEMBERS_ALL[:n_forcing]
    m_seeds = int(np.ceil(n_total / n_forcing))
    parts = []
    with tempfile.TemporaryDirectory() as tmp:
        for k, member in enumerate(members):
            print(f"[{member}] fetching ...", flush=True)
            gefs = fetch_cycle_member(cfg, cycle, member)
            part = Path(tmp, f"{member}.nc")
            run_cycle(cfg, gefs, n_members=m_seeds, out_path=part,
                      use_state_handoff=False)
            parts.append(xr.load_dataset(part))
    combined = xr.concat(parts, dim="member")
    combined = combined.assign_coords(member=np.arange(combined.sizes["member"]))
    combined = combined.isel(member=slice(0, n_total))
    combined.attrs.update(parts[0].attrs)
    combined.attrs["forcing_members"] = ",".join(members)
    combined.attrs["seeds_per_member"] = m_seeds
    out_path = out_path or cfg.path(
        "output_root", f"qpf_ens_{cfg.domain.name}_{cycle:%Y%m%d%H}.nc")
    from ..config import _ensure_dir
    _ensure_dir(out_path.parent)
    combined.to_netcdf(out_path, encoding={
        "qpf": {"zlib": True, "complevel": 4, "dtype": "float32"}})
    print(f"combined ensemble ({combined.sizes['member']} members = "
          f"{len(members)} forcing x {m_seeds} seeds) -> {out_path}")
    return out_path

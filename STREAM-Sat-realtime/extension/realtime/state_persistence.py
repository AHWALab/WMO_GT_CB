"""
Author:   Yagmur Derin, University of Iowa
Created:  2026-04-30
License:  MIT (see extension/LICENSE)
"""

from __future__ import annotations

import logging
import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from netCDF4 import Dataset, num2date

log = logging.getLogger("streamsat-rt.state")

SCHEMA_VERSION = 1

def state_file_path(state_dir, region):
    return Path(state_dir).expanduser() / f"state_{region}.pkl"

def save_state(noise_file, state_dir, region, save_hours, ensemble_size, tres):
    state_dir = Path(state_dir).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)

    n_save_steps = int(round(save_hours / tres))

    with Dataset(noise_file) as ds:
        q = ds.variables["q"][:]
        t = ds.variables["time"][:]
        t_units = ds.variables["time"].units
        lat = np.asarray(ds.variables["latitude"][:])
        lon = np.asarray(ds.variables["longitude"][:])

    if q.shape[1] < n_save_steps:
        log.warning("[state] noise has %d steps, requested %d — saving all",
                    q.shape[1], n_save_steps)
        n_save_steps = q.shape[1]

    saved_q = np.asarray(q[:, -n_save_steps:, :, :], dtype=np.float16)
    saved_times_raw = num2date(t[-n_save_steps:], t_units,
                               only_use_cftime_datetimes=False)
    saved_times = [datetime(d.year, d.month, d.day,
                            d.hour, d.minute, d.second)
                   for d in saved_times_raw]

    state = {
        "schema_version": SCHEMA_VERSION,
        "saved_at_utc": datetime.now(timezone.utc).replace(tzinfo=None)
                                                   .isoformat(timespec="seconds"),
        "region": region,
        "ensemble_size": ensemble_size,
        "tres_hours": float(tres),
        "lat": lat,
        "lon": lon,
        "save_window_start_utc": saved_times[0].isoformat(timespec="seconds"),
        "save_window_end_utc":   saved_times[-1].isoformat(timespec="seconds"),
        "saved_times": saved_times,
        "noise_q": saved_q,
    }

    out = state_file_path(state_dir, region)
    with open(out, "wb") as f:
        pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
    size_mb = out.stat().st_size / (1024 * 1024)
    log.info("[state] saved %d timesteps × %d members to %s (%.1f MB)",
             n_save_steps, ensemble_size, out, size_mb)
    return out

def load_state(state_dir, region,
               expected_lat, expected_lon,
               expected_ensemble, expected_tres,
               max_age_hours=6):
    if not state_dir:
        return None
    state_path = state_file_path(state_dir, region)
    if not state_path.exists():
        log.info("[state] no file at %s — cold start", state_path)
        return None

    try:
        with open(state_path, "rb") as f:
            state = pickle.load(f)
    except Exception as e:
        log.warning("[state] could not unpickle %s: %s — cold start", state_path, e)
        return None

    if state.get("schema_version") != SCHEMA_VERSION:
        log.warning("[state] schema mismatch (got %s, expected %d) — cold start",
                    state.get("schema_version"), SCHEMA_VERSION)
        return None

    saved_at = datetime.fromisoformat(state["saved_at_utc"])
    age_h = (datetime.utcnow() - saved_at).total_seconds() / 3600.0
    if age_h > max_age_hours:
        log.warning("[state] stale (%.1f h > %.1f h max) — cold start",
                    age_h, max_age_hours)
        return None

    saved_lat = np.asarray(state["lat"])
    saved_lon = np.asarray(state["lon"])
    if saved_lat.shape != expected_lat.shape or saved_lon.shape != expected_lon.shape:
        log.warning("[state] grid shape mismatch — cold start")
        return None
    if not np.allclose(saved_lat, expected_lat, atol=1e-3) \
       or not np.allclose(saved_lon, expected_lon, atol=1e-3):
        log.warning("[state] grid coords differ — cold start")
        return None

    if state["ensemble_size"] != expected_ensemble:
        log.warning("[state] ensemble size mismatch (%d vs %d) — cold start",
                    state["ensemble_size"], expected_ensemble)
        return None
    if abs(state["tres_hours"] - expected_tres) > 1e-6:
        log.warning("[state] tres mismatch (%.3f vs %.3f) — cold start",
                    state["tres_hours"], expected_tres)
        return None

    log.info("[state] loaded %d timesteps from %s "
             "(saved %.1f h ago, covers %s .. %s)",
             len(state["saved_times"]), state_path, age_h,
             state["save_window_start_utc"], state["save_window_end_utc"])
    return state

def apply_state_to_noise(noise_file, state, ensid):
    if state is None:
        return 0

    saved_q_member = state["noise_q"][ensid]   # (n_save_steps, ny, nx)
    saved_times = state["saved_times"]

    with Dataset(noise_file, "r+") as ds:
        t = ds.variables["time"][:]
        t_units = ds.variables["time"].units
        nc_times_raw = num2date(t, t_units, only_use_cftime_datetimes=False)
        nc_times = [datetime(d.year, d.month, d.day,
                             d.hour, d.minute, d.second)
                    for d in nc_times_raw]
        nc_idx = {tm: i for i, tm in enumerate(nc_times)}

        n_overridden = 0
        for j, saved_tm in enumerate(saved_times):
            if saved_tm in nc_idx:
                t_idx = nc_idx[saved_tm]
                # noise is stored as (ens_n=1, time, lat, lon) in single-member files
                ds.variables["q"][0, t_idx, :, :] = saved_q_member[j].astype(np.float32)
                n_overridden += 1
    return n_overridden

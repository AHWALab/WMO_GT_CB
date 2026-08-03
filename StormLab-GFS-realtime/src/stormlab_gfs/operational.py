"""Operational cycle: GFS -> ensemble QPF netCDF. This is the code that must
run fast; everything expensive was precomputed offline.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from .config import Config
from .noise import generate_noise, interp_to_grid, temporal_autocorrelation
from .params import load_param_grids
from .quantile_map import (
    apply_intensity_qmap,
    apply_qmap,
    load_intensity_qmaps,
    load_qmaps,
)
from .simulate import rainfall_from_noise
from .tngd import predict_fields


def _match_spectra_targets(cfg: Config, pr_coarse: np.ndarray, month: int) -> np.ndarray:
    lib_path = cfg.path("params_root", f"spectra_library_m{month:02d}.nc")
    sim_lat, sim_lon = cfg.domain.hires_grid(sim=True)
    if not lib_path.exists():
        print("WARNING: no spectra library; using interpolated GFS precip for SSFT")
        return None
    lib = xr.load_dataset(lib_path)
    coarse = lib["coarse"].values  # (member, clat, clon)
    T = pr_coarse.shape[0]
    targets = np.empty((T, sim_lat.size, sim_lon.size), dtype=np.float32)
    for t in range(T):
        # RMSE against every library member's coarse field (resized by interp)
        ref = pr_coarse[t]
        if ref.shape != coarse.shape[1:]:
            k = min(coarse.shape[1], ref.shape[0]), min(coarse.shape[2], ref.shape[1])
            ref = ref[: k[0], : k[1]]
            cmp_fields = coarse[:, : k[0], : k[1]]
        else:
            cmp_fields = coarse
        d = np.sqrt(((cmp_fields - ref[None]) ** 2).mean(axis=(1, 2)))
        targets[t] = lib["hires"].values[int(np.argmin(d))]
    return targets


def _match_spectra_targets_predicted(
    cfg: Config, pred: np.ndarray, p_wet: np.ndarray, month: int,
    war_band: bool = True,
) -> np.ndarray | None:
    lib_path = cfg.path("params_root", f"spectra_library_m{month:02d}.nc")
    if not lib_path.exists():
        return None
    lib = xr.load_dataset(lib_path)
    hires = lib["hires"].values                      # (member, sim_ny, sim_nx)
    sim_lat, sim_lon = cfg.domain.hires_grid(sim=True)
    out_lat, out_lon = cfg.domain.hires_grid(sim=False)
    j0 = int(np.argmin(np.abs(sim_lat - out_lat[0])))
    i0 = int(np.argmin(np.abs(sim_lon - out_lon[0])))
    crop = hires[:, j0:j0 + out_lat.size, i0:i0 + out_lon.size]

    def blocks(f):
        ny, nx = f.shape[-2:]
        return f[..., : ny // 5 * 5, : nx // 5 * 5].reshape(
            *f.shape[:-2], ny // 5, 5, nx // 5, 5).mean((-3, -1))

    thr = float(cfg.training["wet_threshold"])
    war = (crop > thr).mean(axis=(1, 2))
    edges = np.quantile(war, [0.2, 0.4, 0.6, 0.8])
    q_scene = np.digitize(war, edges)
    cand = blocks(crop)

    T = pred.shape[0]
    targets = np.empty((T, sim_lat.size, sim_lon.size), dtype=np.float32)
    for t in range(T):
        sel = np.arange(len(q_scene))
        if war_band:
            q_pred = int(np.digitize(float(p_wet[t].mean()), edges))
            band = np.where(np.abs(q_scene - q_pred) <= 1)[0]
            if band.size:
                sel = band
        d = ((cand[sel] - blocks(pred[t])[None]) ** 2).mean(axis=(1, 2))
        targets[t] = hires[sel[int(np.argmin(d))]]
    return targets


def _match_targets_amplitude(
    cfg: Config, pred: np.ndarray, p_wet: np.ndarray, month: int, k: int = 1,
):
    lib_path = cfg.path("params_root", f"spectra_library_m{month:02d}.nc")
    clim_path = cfg.path("params_root", "predicted_dm_climatology.json")
    if not lib_path.exists() or not clim_path.exists():
        return None
    lib = xr.load_dataset(lib_path)
    hires = lib["hires"].values
    sim_lat, sim_lon = cfg.domain.hires_grid(sim=True)
    out_lat, out_lon = cfg.domain.hires_grid(sim=False)
    j0 = int(np.argmin(np.abs(sim_lat - out_lat[0])))
    i0 = int(np.argmin(np.abs(sim_lon - out_lon[0])))
    crop = hires[:, j0:j0 + out_lat.size, i0:i0 + out_lon.size]

    def blocks(f):
        ny, nx = f.shape[-2:]
        return f[..., : ny // 5 * 5, : nx // 5 * 5].reshape(
            *f.shape[:-2], ny // 5, 5, nx // 5, 5).mean((-3, -1))

    def unitvar(b):
        m = b.mean(axis=(-2, -1), keepdims=True)
        sd = b.std(axis=(-2, -1), keepdims=True)
        return (b - m) / np.where(sd > 0, sd, 1.0)

    thr = float(cfg.training["wet_threshold"])
    war_s = (crop > thr).mean(axis=(1, 2))
    dm_s = crop.mean(axis=(1, 2))
    q_s = (np.argsort(np.argsort(dm_s)) + 0.5) / dm_s.size
    zcand = unitvar(blocks(crop))
    edges = json.loads(clim_path.read_text())[f"{month:02d}"]

    T = pred.shape[0]
    idx = np.empty((T, k), dtype=int)
    for t in range(T):
        war_q = float(p_wet[t].mean())
        q_q = float(np.interp(pred[t].mean(), edges,
                              np.linspace(0.0, 1.0, len(edges))))
        tol_w, tol_q = 0.10, 0.15
        while True:
            sel = np.where((np.abs(war_s - war_q) <= tol_w)
                           & (np.abs(q_s - q_q) <= tol_q))[0]
            if sel.size >= min(10, dm_s.size) or tol_w > 1.0:
                break
            tol_w *= 1.5
            tol_q *= 1.5
        if sel.size == 0:
            sel = np.arange(dm_s.size)
        d = ((zcand[sel] - unitvar(blocks(pred[t]))[None]) ** 2).mean((1, 2))
        top = sel[np.argsort(d)[:k]]
        idx[t] = np.pad(top, (0, k - top.size), mode="edge") if top.size < k else top
    return idx, hires


def _load_noise_state(cfg: Config, cycle: pd.Timestamp) -> dict | None:
    prev = cfg.path("output_root", f"noise_state_{cycle - pd.Timedelta(hours=6):%Y%m%d%H}.npz")
    if not prev.exists():
        return None
    dat = np.load(prev)
    return {int(k): dat[k] for k in dat.files}


def run_cycle(
    cfg: Config,
    gfs: xr.Dataset,
    n_members: int | None = None,
    out_path: Path | None = None,
    init_noise_state: dict | None = None,
    use_state_handoff: bool = True,
) -> Path:
    t0 = time.time()
    n_members = n_members or cfg.forecast["n_members"]
    cycle = pd.Timestamp(gfs.attrs["cycle"])
    month = cycle.month
    leads = gfs.lead.values.astype(int)
    T = leads.size

    sim_lat, sim_lon = cfg.domain.hires_grid(sim=True)
    out_lat, out_lon = cfg.domain.hires_grid(sim=False)
    c_lat, c_lon = gfs.lat.values, gfs.lon.values

    # --- 1. quantile-map covariates: GFS->GEFS on the coarse grid, interp to
    # hi-res, then GEFS->IMERG intensity map per cell (same as training pairs)
    qmaps = load_qmaps(cfg.path("params_root"), cfg.covariates)
    imaps = load_intensity_qmaps(cfg.path("params_root"), cfg.covariates)
    covar_hires = np.empty((T, len(cfg.covariates), out_lat.size, out_lon.size), np.float64)
    for k, cov in enumerate(cfg.covariates):
        mapped = apply_qmap(gfs[cov].values, qmaps.get(cov), month)
        for t in range(T):
            covar_hires[t, k] = interp_to_grid(mapped[t], c_lat, c_lon, out_lat, out_lon)
        covar_hires[:, k] = apply_intensity_qmap(covar_hires[:, k], imaps.get(cov), month)

    # --- 2. distribution parameter fields per timestep (from lead-bin fits)
    pfile = load_param_grids(cfg.path("params_root", "distribution_params.nc"))
    p_wet = np.empty((T, out_lat.size, out_lon.size))
    a = np.empty_like(p_wet)
    scale = np.empty_like(p_wet)
    gg_c = None
    for b in range(pfile.sizes["lead_bin"]):
        sel = np.array([cfg.lead_to_bin(int(l)) == b for l in leads])
        if not sel.any():
            continue
        pw_, a_, s_, c_ = predict_fields(
            covar_hires[sel], pfile["logit"].values[b], pfile["tngd"].values[b]
        )
        p_wet[sel], a[sel], scale[sel] = pw_, a_, s_
        gg_c = c_ if gg_c is None else gg_c

    # --- 3. noise ingredients (sim domain)
    pr_coarse = gfs["PR"].values
    acf = temporal_autocorrelation(pr_coarse)
    if cfg.noise.get("acf_source", "gefs") == "imerg_clim":
        # monthly IMERG hourly lag-1 climatology instead of the per-cycle
        # coarse-GEFS estimate (see scripts/16: GEFS 3-h fields over-smooth)
        clim = json.loads(
            cfg.path("params_root", "imerg_lag1_climatology.json").read_text())
        acf = np.full(T, float(clim[f"{month:02d}"]))
        acf[0] = 0.5  # first step unused beyond initialization, as original
    match_mode = cfg.noise.get("spectra_match", "gefs")
    targets_bank = None                     # (T, k) scene idx for k5 variant
    if match_mode in ("predicted", "amplitude_aware", "amplitude_aware_k5"):
        from scipy.special import gammaln
        with np.errstate(invalid="ignore", over="ignore"):
            inv_c = 1.0 / gg_c[None]
            mu_cond = scale ** inv_c * np.exp(gammaln(a + inv_c) - gammaln(a))
        pred = np.nan_to_num(p_wet * mu_cond, nan=0.0, posinf=0.0)
        if match_mode == "predicted":
            targets = _match_spectra_targets_predicted(
                cfg, pred, p_wet, month,
                war_band=bool(cfg.noise.get("match_war_quintile", True)))
        else:
            kk = 5 if match_mode.endswith("k5") else 1
            res = _match_targets_amplitude(cfg, pred, p_wet, month, k=kk)
            if res is None:
                targets = None
            elif kk == 1:
                targets = res[1][res[0][:, 0]]
            else:
                targets_bank, _lib_hires = res
                targets = _lib_hires[targets_bank[:, 0]]  # placeholder; per-member below
    else:
        targets = _match_spectra_targets(cfg, pr_coarse, month)
    if targets is None:
        targets = np.stack(
            [interp_to_grid(pr_coarse[t], c_lat, c_lon, sim_lat, sim_lon) for t in range(T)]
        )
    a_adv = float(cfg.noise.get("advection_scale", 1.0))
    u = np.stack([interp_to_grid(gfs["U850"].values[t], c_lat, c_lon, sim_lat, sim_lon) for t in range(T)]) * a_adv
    v = np.stack([interp_to_grid(gfs["V850"].values[t], c_lat, c_lon, sim_lat, sim_lon) for t in range(T)]) * a_adv

    # crop indices sim -> out
    j0 = int(np.argmin(np.abs(sim_lat - out_lat[0])))
    i0 = int(np.argmin(np.abs(sim_lon - out_lon[0])))
    jsl, isl = slice(j0, j0 + out_lat.size), slice(i0, i0 + out_lon.size)

    # --- 4. members: noise -> rainfall
    if init_noise_state is None and use_state_handoff:
        init_noise_state = _load_noise_state(cfg, cycle)
    members = np.empty((n_members, T, out_lat.size, out_lon.size), np.float32)
    noise_states = {}
    for k in range(n_members):
        seed = cfg.forecast["base_seed"] + k
        init_n = init_noise_state.get(k) if init_noise_state else None
        member_targets = targets
        if targets_bank is not None:
            # k5 variant: each member samples one of the top-k scenes per
            # timestep (seeded per member) — scene diversity across members
            pick = np.random.RandomState(seed + 90_000).randint(
                0, targets_bank.shape[1], size=T)
            member_targets = _lib_hires[targets_bank[np.arange(T), pick]]
        noise = generate_noise(
            member_targets, acf, u, v, sim_lat, sim_lon,
            dt_hours=float(cfg.forecast["lead_step"]),
            win_size=(cfg.noise["window_size"],) * 2,
            overlap=cfg.noise["overlap_ratio"],
            war_thr=cfg.noise["ssft_war_thr"],
            seed=seed, init_noise=init_n,
            large_scale_weight=float(cfg.noise.get("large_scale_weight", 0.0)),
            plain_spectrum=bool(cfg.noise.get("ssft_plain_spectrum", False)),
        )
        noise_states[k] = noise[-1]
        members[k] = rainfall_from_noise(
            noise[:, jsl, isl], p_wet, a, scale, gg_c,
            p_cap=float(cfg.forecast.get("p_cap", 0.995)),
        )

    # --- 5. write output
    valid = [cycle + pd.Timedelta(hours=int(l)) for l in leads]
    out = xr.Dataset(
        {"qpf": (("member", "time", "lat", "lon"), members)},
        coords={"member": np.arange(n_members), "time": valid, "lat": out_lat, "lon": out_lon},
        attrs={
            "title": "StormLab-GFS ensemble QPF",
            "cycle": gfs.attrs["cycle"],
            "units": "mm/h",
            "domain": cfg.domain.name,
            "covariates": json.dumps(cfg.covariates),
        },
    )
    out["qpf"].attrs["units"] = "mm/h"
    out_path = out_path or cfg.path("output_root", f"qpf_ens_{cfg.domain.name}_{cycle:%Y%m%d%H}.nc")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoding = {"qpf": {"zlib": True, "complevel": 4, "dtype": "float32"}}
    out.to_netcdf(out_path, encoding=encoding)
    if use_state_handoff:
        np.savez_compressed(
            cfg.path("output_root", f"noise_state_{cycle:%Y%m%d%H}.npz"),
            **{str(k): v.astype(np.float32) for k, v in noise_states.items()},
        )

    elapsed = time.time() - t0
    print(f"cycle {cycle:%Y-%m-%d %HZ}: {n_members} members x {T} h -> {out_path} "
          f"({elapsed:.1f} s)")
    return out_path

"""Spatiotemporally correlated Gaussian noise (SSFT + AR(1) advection).

Port of the original StormLab `noise_generation.py`, modernized:
- no scipy.interpolate.interp2d (removed in scipy>=1.14) -> RegularGridInterpolator
- no pyproj / EPSG:2163 (US-only) -> spherical-earth grid metrics, valid anywhere
- vectorized advection index math (unchanged semantics)
"""
from __future__ import annotations

import numpy as np
from scipy.interpolate import RegularGridInterpolator

EARTH_M_PER_DEG = 111_320.0


# --------------------------------------------------------------------------
# SSFT spatial noise (identical algorithm to original FFST_based_noise_generation)
# --------------------------------------------------------------------------
def _tukey(R, alpha, N):
    W = np.ones_like(R)
    mask = (R < N // 2) & (R > N // 2 * (1.0 - alpha))
    W[mask] = 0.5 * (1.0 + np.cos(np.pi * (R[mask] / (alpha * 0.5 * N) - 1.0 / alpha + 1.0)))
    W[R >= N // 2] = 0.0
    return W


def tukey_window(m, n, alpha=0.2):
    X, Y = np.meshgrid(np.arange(n), np.arange(m))
    R = np.sqrt((X - n // 2) ** 2 + (Y - m // 2) ** 2)
    return _tukey(R, alpha, min(m, n)) + 1e-6


def amplitude_spectrum(field, plain=False):
    F = np.fft.fft2(field)
    if plain:
        return np.abs(F)
    F.imag = (F.imag - F.imag.mean()) / (F.imag.std() or 1.0)
    F.real = (F.real - F.real.mean()) / (F.real.std() or 1.0)
    return np.abs(F)


def ssft_noise(field, white, win_size=(64, 64), overlap=0.3, war_thr=0.1,
               plain_spectrum=False):
    dim_y, dim_x = field.shape
    n_win_y = int(np.ceil(dim_y / win_size[0]))
    n_win_x = int(np.ceil(dim_x / win_size[1]))

    noise_F = np.fft.fft2(white)
    global_noise = np.fft.ifft2(noise_F * amplitude_spectrum(field, plain_spectrum)).real
    out = np.zeros_like(global_noise)
    weight = np.zeros_like(global_noise)

    for i in range(n_win_y):
        for j in range(n_win_x):
            i0 = int(max(i * win_size[0] - overlap * win_size[0], 0))
            i1 = int(min(i0 + win_size[0] + overlap * win_size[0], dim_y))
            j0 = int(max(j * win_size[1] - overlap * win_size[1], 0))
            j1 = int(min(j0 + win_size[1] + overlap * win_size[1], dim_x))

            win = tukey_window(i1 - i0, j1 - j0)
            full_mask = np.zeros((dim_y, dim_x))
            full_mask[i0:i1, j0:j1] = win
            war = np.sum(field[i0:i1, j0:j1] * win > 0.01) / ((i1 - i0) * (j1 - j0))

            if war > war_thr:
                local_F = amplitude_spectrum(field * full_mask, plain_spectrum)
                local_noise = np.fft.ifft2(noise_F * local_F).real
                out += local_noise * full_mask
            else:
                out += global_noise * full_mask
            weight += full_mask

    out[weight > 0] /= weight[weight > 0]
    std = out.std()
    return (out - out.mean()) / (std if std > 0 else 1.0)


# --------------------------------------------------------------------------
# temporal pieces
# --------------------------------------------------------------------------
def temporal_autocorrelation(prcp_array: np.ndarray) -> np.ndarray:
    flat = prcp_array.reshape(prcp_array.shape[0], -1)
    alphas = [0.5]
    for i in range(flat.shape[0] - 1):
        mask = (flat[i] != 0) | (flat[i + 1] != 0)
        if mask.sum() < 3:
            alphas.append(alphas[-1])
            continue
        c = np.corrcoef(flat[i][mask], flat[i + 1][mask])[0, 1]
        alphas.append(np.clip(np.nan_to_num(c, nan=alphas[-1]), 0.0, 0.999))
    return np.array(alphas)


def interp_to_grid(field, src_lat, src_lon, dst_lat, dst_lon):
    lat_asc = src_lat if src_lat[0] < src_lat[-1] else src_lat[::-1]
    fld = field if src_lat[0] < src_lat[-1] else field[::-1]
    itp = RegularGridInterpolator(
        (lat_asc, src_lon), fld, bounds_error=False, fill_value=None, method="linear"
    )
    LAT, LON = np.meshgrid(dst_lat, dst_lon, indexing="ij")
    return itp(np.stack([LAT.ravel(), LON.ravel()], axis=-1)).reshape(LAT.shape)


def grid_metrics(lat, lon):
    dlat = float(np.mean(np.diff(lat)))
    dlon = float(np.mean(np.diff(lon)))
    dy = np.full((lat.size, lon.size), abs(dlat) * EARTH_M_PER_DEG)
    dx = np.outer(np.cos(np.deg2rad(lat)), np.ones(lon.size)) * abs(dlon) * EARTH_M_PER_DEG
    return dx, dy

def generate_noise(
    target_fields: np.ndarray,
    acf: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    dt_hours: float = 1.0,
    win_size=(64, 64),
    overlap=0.3,
    war_thr=0.1,
    seed: int = 0,
    init_noise: np.ndarray | None = None,
    large_scale_weight: float = 0.0,
    plain_spectrum: bool = False,
):
    T, ny, nx = target_fields.shape
    dx, dy = grid_metrics(lat, lon)
    yind, xind = np.mgrid[0:ny, 0:nx]

    rng = np.random.RandomState(seed)
    white = rng.randn(T, ny, nx)
    out = np.zeros((T, ny, nx))

    prev = init_noise
    for t in range(T):
        fresh = ssft_noise(target_fields[t], white[t], win_size, overlap, war_thr,
                           plain_spectrum)
        if prev is None:
            out[t] = fresh
        else:
            disp_x = np.round(u[t] * 3600.0 * dt_hours / dx)
            disp_y = np.round(v[t] * 3600.0 * dt_hours / dy)
            xa = (xind - disp_x).astype(int)
            ya = (yind - disp_y).astype(int)
            oob = (xa < 0) | (xa > nx - 1) | (ya < 0) | (ya > ny - 1)
            advected = prev[ya % ny, xa % nx]
            advected = np.where(oob, fresh, advected)
            out[t] = acf[t] * advected + np.sqrt(1.0 - acf[t] ** 2) * fresh
        prev = out[t]

    w = float(large_scale_weight)
    if w > 0.0:
        # scalar draws come after the white-noise block so the SSFT stream is
        # unchanged for any w
        z = rng.randn(T)
        large = np.empty(T)
        large[0] = z[0]
        for t in range(1, T):
            large[t] = acf[t] * large[t - 1] + np.sqrt(1.0 - acf[t] ** 2) * z[t]
        out = np.sqrt(1.0 - w**2) * out + w * large[:, None, None]
    return out

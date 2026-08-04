"""Noise -> precipitation transformation (port of original simulation.py, vectorized)."""
from __future__ import annotations

import numpy as np
import scipy.stats as st


def rainfall_from_noise(noise, p_wet, a, scale, gg_c, p_cap=0.995):
    u = st.norm.cdf(noise)
    p_wet = np.nan_to_num(p_wet, nan=0.0)
    dry = 1.0 - p_wet

    with np.errstate(invalid="ignore", divide="ignore"):
        p2 = (u - dry) / np.where(p_wet > 0, p_wet, np.nan)
    p2 = np.where((p2 < 0) | (dry >= 1.0), np.nan, p2)
    p2 = np.minimum(p2, p_cap)

    rain = np.zeros(noise.shape, dtype=np.float32)
    mask = np.isfinite(p2) & np.isfinite(a) & np.isfinite(scale)
    if mask.any():
        ggc3 = np.broadcast_to(gg_c, noise.shape)
        vals = st.gamma.ppf(p2[mask], a=a[mask], scale=scale[mask]) ** (1.0 / ggc3[mask])
        vals = np.nan_to_num(vals, nan=0.0, posinf=0.0)
        rain[mask] = vals.astype(np.float32)
    return rain

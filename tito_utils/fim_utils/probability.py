"""Probabilistic FIM product.

Per cycle: each ensemble member's rainfall total selects one storm from
the store; across members the per-pixel exceedance probability

    P(max depth >= threshold) = (members whose storm floods the pixel) / N

is computed on the FIM grid and written as GeoTIFF, together with an IBF
likelihood-class raster (Very Low / Low / Medium / High) that feeds the
likelihood axis of the flood risk matrix.
"""

import json
import os

import numpy as np

DEFAULT_BANDS = {"very_low": [0.0, 0.2], "low": [0.2, 0.4],
                 "medium": [0.4, 0.6], "high": [0.6, 1.01]}
BAND_CODES = {"very_low": 1, "low": 2, "medium": 3, "high": 4}


def exceedance_probability(store, storm_indices, depth_threshold_m: float = 0.30):
    """P(depth >= threshold) over the member-selected storms.

    storm_indices: one storm index per member (duplicates allowed and
    common). Duplicate indices are read once and weighted by their count,
    so at most len(set(indices)) chunks are read.
    """
    counts = {}
    for idx in storm_indices:
        counts[int(idx)] = counts.get(int(idx), 0) + 1
    n = sum(counts.values())
    if n == 0:
        raise ValueError("No members with a matched storm")

    acc = np.zeros(store.grid_shape, dtype="float64")
    for idx, weight in counts.items():
        depth = store.depth(idx)
        acc += (depth >= depth_threshold_m) * float(weight)
    return (acc / n).astype("float32"), n


def classify_likelihood(prob: np.ndarray, bands: dict = None) -> np.ndarray:
    """Left-closed bins [lo, hi): consistent with the quicklook color scale,
    so a probability exactly on a boundary (e.g. 0.20) takes the upper class."""
    bands = bands or DEFAULT_BANDS
    out = np.zeros(prob.shape, dtype="uint8")
    for name, (lo, hi) in bands.items():
        code = BAND_CODES.get(name)
        if code is None:
            continue
        sel = (prob >= lo) & (prob < hi)
        out[sel & (prob > 0)] = code
    return out


def write_geotiff(path: str, data: np.ndarray, transform, crs, nodata=None):
    import rasterio
    from rasterio.transform import Affine
    profile = {
        "driver": "GTiff", "height": data.shape[0], "width": data.shape[1],
        "count": 1, "dtype": str(data.dtype), "crs": crs,
        "transform": Affine(*transform[:6]), "compress": "lzw",
    }
    if nodata is not None:
        profile["nodata"] = nodata
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return path


def quicklook_png(path: str, prob: np.ndarray, title: str, threshold_m: float,
                  note: str = ""):
    """Simple probability quicklook (agency style, no basemap dependency)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap, BoundaryNorm

    colors = ["#f0f0f0", "#8DC63F", "#FFF200", "#F7941D", "#ED1C24"]
    cmap = ListedColormap(colors)
    norm = BoundaryNorm([0.0, 1e-6, 0.2, 0.4, 0.6, 1.0], cmap.N)

    fig, ax = plt.subplots(figsize=(7, 8))
    im = ax.imshow(prob, cmap=cmap, norm=norm, interpolation="nearest")
    cbar = fig.colorbar(im, ax=ax, shrink=0.7,
                        ticks=[0.1, 0.3, 0.5, 0.8])
    cbar.ax.set_yticklabels(["Very low\n(<20%)", "Low\n(20-40%)",
                             "Medium\n(40-60%)", "High\n(>60%)"], fontsize=8)
    ax.set_title(f"{title}\nP(max depth >= {threshold_m:.2f} m)", fontsize=11)
    ax.set_xticks([]); ax.set_yticks([])
    if note:
        ax.text(0.01, -0.02, note, transform=ax.transAxes, fontsize=7,
                color="0.35", va="top")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def summarize(prob: np.ndarray, bands: dict = None) -> dict:
    bands = bands or DEFAULT_BANDS
    wet = prob > 0
    out = {"pixels_any_probability": int(wet.sum()),
           "max_probability": round(float(prob.max()), 3) if prob.size else 0.0}
    for name, (lo, hi) in bands.items():
        sel = (prob >= lo) & (prob < hi) & (prob > 0)
        out[f"pixels_{name}"] = int(sel.sum())
    return out

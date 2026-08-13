"""Minimal raster IO helpers built on rasterio.

Grids are returned as float32 numpy arrays with NaN where nodata.
Windowed reads keep the trigger fast on the large Guatemala/Haiti domains.
"""

from dataclasses import dataclass

import numpy as np

try:
    import rasterio
    from rasterio.windows import from_bounds
except ImportError as exc:  # pragma: no cover
    raise ImportError("fim_utils requires rasterio (pip install rasterio)") from exc


@dataclass
class Grid:
    data: np.ndarray          # 2D float32, NaN = nodata
    transform: object         # affine transform of the (possibly windowed) read
    crs: object
    path: str = ""

    @property
    def shape(self):
        return self.data.shape


def read_grid(path: str, bounds=None) -> Grid:
    """Read band 1 of a GeoTIFF as float32 with NaN nodata.

    bounds: optional (minx, miny, maxx, maxy) in the raster CRS; only the
    intersecting window is read.
    """
    with rasterio.open(path) as src:
        if bounds is not None:
            window = from_bounds(*bounds, transform=src.transform)
            window = window.round_offsets().round_lengths()
            # Clamp to the raster
            window = window.intersection(
                rasterio.windows.Window(0, 0, src.width, src.height)
            )
            if window.width <= 0 or window.height <= 0:
                return Grid(np.full((0, 0), np.nan, dtype="float32"),
                            src.transform, src.crs, path)
            data = src.read(1, window=window, masked=True)
            transform = src.window_transform(window)
        else:
            data = src.read(1, masked=True)
            transform = src.transform
        arr = np.ma.filled(data.astype("float32"), np.nan)
        return Grid(arr, transform, src.crs, path)


def write_mask_like(path_out: str, mask: np.ndarray, like_path: str, window_bounds=None):
    """Write a uint8 0/1 mask with the georeferencing of like_path."""
    with rasterio.open(like_path) as src:
        profile = src.profile.copy()
    profile.update(dtype="uint8", count=1, nodata=0, compress="lzw")
    with rasterio.open(path_out, "w", **profile) as dst:
        dst.write(mask.astype("uint8"), 1)


def nan_stat(arr: np.ndarray, stat: str) -> float:
    """NaN-safe scalar statistic; returns nan on empty input."""
    if arr.size == 0 or np.all(np.isnan(arr)):
        return float("nan")
    if stat == "mean":
        return float(np.nanmean(arr))
    if stat == "max":
        return float(np.nanmax(arr))
    if stat == "sum":
        return float(np.nansum(arr))
    raise ValueError(f"Unknown stat '{stat}'")

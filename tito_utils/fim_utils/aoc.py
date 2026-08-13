"""Areas of Concern (AOC).

An AOC is where the lookup applies: a pilot basin (Haiti, Guatemala) or an
administrative unit (Antigua, Barbados, Comoros). The same code path serves
both; only the input file differs.

Supported sources:
- .geojson / .json : read with the standard library, no extra dependencies
- .shp / .gpkg     : read with fiona (already in tito_env)
- a folder of .tif : each raster is one AOC mask (values > 0 inside),
                     useful when someone hands us masks instead of polygons

Masks are rasterized per target grid and cached, so the trigger and the
zonal statistics reuse the same mask within a cycle.
"""

import json
import os
from dataclasses import dataclass, field

import numpy as np

from .io_utils import Grid, read_grid, nan_stat

try:
    from rasterio.features import geometry_mask
except ImportError as exc:  # pragma: no cover
    raise ImportError("fim_utils requires rasterio") from exc


@dataclass
class AreaOfConcern:
    aoc_id: str
    name: str = ""
    geoms: list = field(default_factory=list)   # GeoJSON-like geometry dicts
    mask_path: str = ""                          # set when source is a raster mask
    _bounds: tuple = None
    _mask_cache: dict = field(default_factory=dict, repr=False)

    # ---- geometry helpers -------------------------------------------------

    @property
    def bounds(self):
        """(minx, miny, maxx, maxy) of the AOC geometry, with a small pad."""
        if self._bounds is None:
            if self.geoms:
                xs, ys = [], []
                for geom in self.geoms:
                    _walk_coords(geom.get("coordinates", []), xs, ys)
                pad = 0.02  # degrees, keeps edge pixels
                self._bounds = (min(xs) - pad, min(ys) - pad,
                                max(xs) + pad, max(ys) + pad)
            elif self.mask_path:
                import rasterio
                with rasterio.open(self.mask_path) as src:
                    b = src.bounds
                self._bounds = (b.left, b.bottom, b.right, b.top)
        return self._bounds

    def mask_for(self, grid: Grid) -> np.ndarray:
        """Boolean mask, True inside the AOC, on the grid's window."""
        key = (grid.shape, tuple(np.round(np.asarray(grid.transform)[:6], 10)))
        if key in self._mask_cache:
            return self._mask_cache[key]

        if self.geoms:
            if grid.data.size == 0:
                mask = np.zeros(grid.shape, dtype=bool)
            else:
                mask = ~geometry_mask(self.geoms, out_shape=grid.shape,
                                      transform=grid.transform, invert=False)
        elif self.mask_path:
            mgrid = read_grid(self.mask_path, bounds=_grid_bounds(grid))
            mask = _resample_nearest(mgrid, grid) > 0
        else:
            raise ValueError(f"AOC {self.aoc_id} has neither geometry nor mask")

        self._mask_cache[key] = mask
        return mask

    # ---- statistics -------------------------------------------------------

    def zonal(self, grid: Grid, stat: str = "mean") -> float:
        mask = self.mask_for(grid)
        if not mask.any():
            return float("nan")
        return nan_stat(grid.data[mask], stat)


def _walk_coords(coords, xs, ys):
    if not coords:
        return
    if isinstance(coords[0], (int, float)):
        xs.append(coords[0])
        ys.append(coords[1])
        return
    for part in coords:
        _walk_coords(part, xs, ys)


def _grid_bounds(grid: Grid):
    from rasterio.transform import array_bounds
    return array_bounds(grid.shape[0], grid.shape[1], grid.transform)


def _resample_nearest(src: Grid, target: Grid) -> np.ndarray:
    """Nearest-neighbour sample of src onto target's pixel centres.

    Good enough for 0/1 masks; avoids a full warp dependency.
    """
    out = np.zeros(target.shape, dtype="float32")
    if src.data.size == 0:
        return out
    import rasterio.transform as rt
    rows, cols = np.indices(target.shape)
    xs, ys = rt.xy(target.transform, rows.ravel(), cols.ravel())
    src_rows, src_cols = rt.rowcol(src.transform, xs, ys)
    src_rows = np.asarray(src_rows)
    src_cols = np.asarray(src_cols)
    valid = ((src_rows >= 0) & (src_rows < src.shape[0]) &
             (src_cols >= 0) & (src_cols < src.shape[1]))
    flat = out.ravel()
    vals = src.data[src_rows[valid], src_cols[valid]]
    vals = np.nan_to_num(vals, nan=0.0)
    flat[np.flatnonzero(valid)] = vals
    return flat.reshape(target.shape)


# ---- loading ---------------------------------------------------------------

def load_aocs(source: str, id_field: str = "", name_field: str = "",
              layer: str = "") -> list:
    """Load Areas of Concern from a polygon file or a folder of mask rasters."""
    if os.path.isdir(source):
        aocs = []
        for fname in sorted(os.listdir(source)):
            if fname.lower().endswith(".tif"):
                stem = os.path.splitext(fname)[0]
                aocs.append(AreaOfConcern(aoc_id=stem, name=stem,
                                          mask_path=os.path.join(source, fname)))
        if not aocs:
            raise FileNotFoundError(f"No .tif masks found in {source}")
        return aocs

    ext = os.path.splitext(source)[1].lower()
    if ext in (".geojson", ".json"):
        with open(source, "r") as fh:
            gj = json.load(fh)
        features = gj.get("features", [])
        return _features_to_aocs(features, id_field, name_field)

    if ext in (".shp", ".gpkg"):
        try:
            import fiona
        except ImportError as exc:
            raise ImportError(
                f"Reading {ext} needs fiona (in tito_env). "
                "Alternatively provide a .geojson."
            ) from exc
        kwargs = {"layer": layer} if layer else {}
        with fiona.open(source, **kwargs) as coll:
            features = [
                {"properties": dict(f["properties"]), "geometry": dict(f["geometry"])}
                for f in coll
            ]
        return _features_to_aocs(features, id_field, name_field)

    raise ValueError(f"Unsupported AOC source: {source}")


def _features_to_aocs(features, id_field, name_field):
    aocs = []
    for idx, feat in enumerate(features):
        props = feat.get("properties", {}) or {}
        geom = feat.get("geometry")
        if geom is None:
            continue
        aoc_id = str(props.get(id_field, idx)) if id_field else str(idx)
        name = str(props.get(name_field, aoc_id)) if name_field else aoc_id
        aocs.append(AreaOfConcern(aoc_id=aoc_id, name=name, geoms=[geom]))
    if not aocs:
        raise ValueError("AOC source contained no usable features")
    return aocs

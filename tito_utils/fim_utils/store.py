"""Zarr-backed FIM lookup store.

One chunk per storm: given a rainfall accumulation, the matcher picks the
storm and only that storm's depth chunk is read from disk. No netCDF, no
whole-catalog loads, memory stays flat regardless of library size.

Layout of  <store>.zarr :
    depth        (storm, y, x)  float32, metres, chunks (1, ny, nx)
    extent       (storm, y, x)  uint8 0/1 (depth >= extent_threshold_m)
    magnitude_mm (storm,)       float64, 24-h rainfall of each storm
    storm_id     (storm,)       fixed-length strings
    attrs: crs, transform, extent_threshold_m, magnitude_source, ...

Storms are stored sorted by magnitude so band selection is a searchsorted.
Magnitudes can be refreshed in place (attach_magnitudes) when the real
RainyDay scenario totals arrive; depth chunks are never rewritten.
"""

import json
import os

import numpy as np

try:
    import zarr
except ImportError as exc:  # pragma: no cover
    raise ImportError("The FIM store needs zarr (pip install zarr)") from exc


def _create_array(group, name, data, chunks):
    """zarr 2/3 compatibility."""
    kwargs = dict(shape=data.shape, dtype=data.dtype, chunks=chunks)
    try:                      # zarr >= 3
        arr = group.create_array(name, **kwargs)
    except (AttributeError, TypeError):   # zarr 2.x
        arr = group.create_dataset(name, **kwargs)
    arr[...] = data
    return arr


class FimStore:
    """Read side of the store."""

    def __init__(self, path: str):
        self.path = path
        self.root = zarr.open_group(path, mode="r")
        self.magnitude = np.asarray(self.root["magnitude_mm"][:], dtype="float64")
        raw_ids = np.asarray(self.root["storm_id"][:])
        self.storm_id = [s.decode() if isinstance(s, bytes) else str(s) for s in raw_ids]
        self.attrs = dict(self.root.attrs)
        if not np.all(np.diff(self.magnitude[~np.isnan(self.magnitude)]) >= 0):
            raise ValueError("Store magnitudes are not sorted; rebuild or re-attach magnitudes")

    # -- shape / geo -------------------------------------------------------
    @property
    def n_storms(self):
        return len(self.storm_id)

    @property
    def grid_shape(self):
        return tuple(self.root["depth"].shape[1:])

    @property
    def transform(self):
        return tuple(self.attrs["transform"])

    @property
    def crs(self):
        return self.attrs.get("crs", "")

    # -- access ------------------------------------------------------------
    def index_of(self, storm_id: str) -> int:
        return self.storm_id.index(str(storm_id))

    def depth(self, idx) -> np.ndarray:
        """Depth map of one storm (reads exactly one chunk)."""
        if isinstance(idx, str):
            idx = self.index_of(idx)
        return np.asarray(self.root["depth"][idx])

    def extent(self, idx) -> np.ndarray:
        if isinstance(idx, str):
            idx = self.index_of(idx)
        return np.asarray(self.root["extent"][idx])

    # -- matching ----------------------------------------------------------
    def candidates_in_band(self, total_mm: float, band) -> np.ndarray:
        """Indices of storms with magnitude within [band0*T, band1*T]."""
        lo, hi = band[0] * total_mm, band[1] * total_mm
        i0 = int(np.searchsorted(self.magnitude, lo, side="left"))
        i1 = int(np.searchsorted(self.magnitude, hi, side="right"))
        return np.arange(i0, i1)

    def match(self, total_mm: float, band=(0.9, 1.2), band_wide=(0.8, 1.3)):
        """Widening-band, round-up match. Returns a decision dict."""
        d = {"total_mm": None if total_mm != total_mm else round(float(total_mm), 2),
             "storm_id": "", "storm_index": -1, "storm_magnitude_mm": None,
             "alt_storm_id": "", "rule_applied": "", "flags": []}
        if total_mm != total_mm:
            d["rule_applied"] = "no_total"
            d["flags"].append("missing_total")
            return d

        for rule, b in (("band", band), ("band_wide", band_wide)):
            cand = self.candidates_in_band(total_mm, b)
            if cand.size:
                mags = self.magnitude[cand]
                above = cand[mags >= total_mm]
                if above.size:
                    pick = int(above[0])                    # smallest above T
                else:
                    pick = int(cand[-1])                    # all below: largest
                    d["flags"].append("rounded_down")
                d.update(storm_index=pick, storm_id=self.storm_id[pick],
                         storm_magnitude_mm=round(float(self.magnitude[pick]), 2),
                         rule_applied=rule)
                return d

        if total_mm > float(np.nanmax(self.magnitude)):
            pick = self.n_storms - 1
            d.update(storm_index=pick, storm_id=self.storm_id[pick],
                     storm_magnitude_mm=round(float(self.magnitude[pick]), 2),
                     alt_storm_id=self.storm_id[max(0, pick - 1)],
                     rule_applied="beyond_catalog")
            d["flags"].append("beyond_catalog")
        else:
            pick = 0
            d.update(storm_index=pick, storm_id=self.storm_id[pick],
                     storm_magnitude_mm=round(float(self.magnitude[pick]), 2),
                     rule_applied="below_catalog")
            d["flags"].append("below_catalog")
        return d


def build_store(depth_files: dict, out_path: str, magnitudes: dict,
                extent_threshold_m: float = 0.05,
                crs: str = "", transform=None, nodata=None,
                magnitude_source: str = "provided",
                extra_attrs: dict = None) -> str:
    """Build the zarr store from {storm_id: depth_tif_path}.

    magnitudes: {storm_id: magnitude_mm} (NaN allowed but discouraged).
    Nodata cells in the depth rasters are stored as 0 (dry).
    Storms are written sorted by magnitude ascending.
    """
    import rasterio

    ids = sorted(depth_files)
    mags = np.array([float(magnitudes.get(s, np.nan)) for s in ids], dtype="float64")
    order = np.argsort(mags, kind="stable")
    ids = [ids[i] for i in order]
    mags = mags[order]

    ref = None
    stack = []
    for storm in ids:
        with rasterio.open(depth_files[storm]) as src:
            arr = src.read(1, masked=True).filled(0.0).astype("float32")
            arr[arr < 0] = 0.0
            sig = (src.width, src.height, tuple(np.round(np.asarray(src.transform)[:6], 9)), str(src.crs))
            if ref is None:
                ref = sig
                crs = crs or str(src.crs)
                transform = transform or tuple(np.asarray(src.transform)[:6])
                nodata = src.nodata
            elif sig != ref:
                raise ValueError(f"{storm}: grid differs from the first map; all maps must share one grid")
            stack.append(arr)

    depth = np.stack(stack)                       # (n, ny, nx)
    extent = (depth >= extent_threshold_m).astype("uint8")
    n, ny, nx = depth.shape

    root = zarr.open_group(out_path, mode="w")
    _create_array(root, "depth", depth, chunks=(1, ny, nx))
    _create_array(root, "extent", extent, chunks=(1, ny, nx))
    _create_array(root, "magnitude_mm", mags, chunks=(n,))
    max_len = max(len(s) for s in ids)
    _create_array(root, "storm_id",
                  np.array([s.encode() for s in ids], dtype=f"S{max_len}"), chunks=(n,))
    root.attrs.update({
        "crs": crs, "transform": list(transform),
        "extent_threshold_m": extent_threshold_m,
        "source_nodata": None if nodata is None else float(nodata),
        "magnitude_source": magnitude_source,
        "n_storms": n, "grid_shape": [ny, nx],
        **(extra_attrs or {}),
    })

    # Companion CSV index for humans
    import csv
    with open(os.path.join(out_path, "index.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["storm_index", "storm_id", "magnitude_mm"])
        for i, (s, m) in enumerate(zip(ids, mags)):
            w.writerow([i, s, "" if m != m else round(m, 2)])
    with open(os.path.join(out_path, "meta.json"), "w") as fh:
        json.dump(dict(root.attrs), fh, indent=2)
    return out_path


def attach_magnitudes(store_path: str, magnitudes: dict, source: str = "rainyday"):
    """Replace the magnitudes (e.g. when real RainyDay totals arrive).

    Because storms must stay sorted by magnitude, this rewrites the order
    arrays and re-sorts depth/extent chunk order lazily by rewriting the
    two 1-D arrays and a permutation attr is NOT used; instead chunks are
    re-linked by rewriting depth/extent in the new order. For 200 storms
    this is a one-off cost of reading and writing each chunk once.
    """
    root = zarr.open_group(store_path, mode="r+")
    ids = [s.decode() if isinstance(s, bytes) else str(s)
           for s in np.asarray(root["storm_id"][:])]
    mags = np.array([float(magnitudes.get(s, np.nan)) for s in ids], dtype="float64")
    order = np.argsort(mags, kind="stable")

    depth = np.asarray(root["depth"][:])[order]
    extent = np.asarray(root["extent"][:])[order]
    root["depth"][...] = depth
    root["extent"][...] = extent
    root["magnitude_mm"][...] = mags[order]
    root["storm_id"][...] = np.array([ids[i] for i in order], dtype=root["storm_id"].dtype)
    # Any additional per-storm index must follow the same permutation.
    # The fluvial boundary-discharge matrix (fluvial.py) is row-aligned with
    # storm order, so re-link it here too.
    if "fluvial_q" in root:
        root["fluvial_q"][...] = np.asarray(root["fluvial_q"][:])[order]
    root.attrs["magnitude_source"] = source
    return store_path

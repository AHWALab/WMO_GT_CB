"""Fluvial (F) hazard: analog matching on boundary discharges.

Python port of the MATLAB prototype prototypeAnalogMatchingFPFlood.m
(standalone_prototype). Each ensemble member is reduced to the maximum
discharge at the configured upstream boundary points; that vector is
standardized against the library's scenario discharges (MATLAB std,
ddof=1) and the nearest scenario in standardized Euclidean distance is
the member's fluvial flood map.

The fluvial index lives INSIDE the existing zarr flood map store as an
additional array, so pluvial and fluvial matching share one set of depth
chunks. Use attach_fluvial_index() once per store.

Port note: the MATLAB prototype computed the match index only in the
StreamSat loop, so its StormLab maps reused a stale index. This module
matches every member with its own discharges; the parity test reproduces
the prototype's StreamSat maps exactly and documents the StormLab
difference.
"""

import os

import numpy as np

FLUVIAL_INDEX_KEY = "fluvial_q"


def attach_fluvial_index(store_path: str, scenarios_q, names, source: str = ""):
    """Store the per-scenario boundary discharge matrix (n_storms x n_points).

    scenarios_q rows must be in the STORE's storm order (magnitude-sorted);
    use rows keyed by storm_id via reorder_index() when converting from an
    external library whose rows follow sample number order.
    """
    import zarr
    root = zarr.open_group(store_path, mode="a")
    q = np.asarray(scenarios_q, dtype="float64")
    if FLUVIAL_INDEX_KEY in root:
        del root[FLUVIAL_INDEX_KEY]
    arr = root.create_array(FLUVIAL_INDEX_KEY, shape=q.shape, dtype="float64")
    arr[:] = q
    root.attrs["fluvial_index_names"] = list(names)
    root.attrs["fluvial_index_source"] = source or "unspecified"
    # MATLAB std default is ddof=1; stored so matching is reproducible
    root.attrs["fluvial_mu"] = [float(v) for v in q.mean(axis=0)]
    root.attrs["fluvial_sigma"] = [float(v) for v in q.std(axis=0, ddof=1)]
    return q.shape


def reorder_index(scenarios_q_by_sample, sample_ids, store_storm_ids):
    """Reorder an external (per-sample) scenario matrix into store order."""
    pos = {s: i for i, s in enumerate(sample_ids)}
    return np.stack([scenarios_q_by_sample[pos[s]] for s in store_storm_ids])


class FluvialMatcher:
    """Standardized nearest-neighbor matching in boundary discharge space."""

    def __init__(self, store):
        import zarr
        self.store = store
        root = zarr.open_group(store.path, mode="r")
        if FLUVIAL_INDEX_KEY not in root:
            raise KeyError(
                f"store has no fluvial index; run attach_fluvial_index() first")
        self.q = np.asarray(root[FLUVIAL_INDEX_KEY][:], dtype="float64")
        self.names = list(root.attrs.get("fluvial_index_names", []))
        self.mu = np.asarray(root.attrs["fluvial_mu"], dtype="float64")
        self.sigma = np.asarray(root.attrs["fluvial_sigma"], dtype="float64")

    def match(self, q_point, method: str = "standardized", weights=None) -> dict:
        p = np.asarray(q_point, dtype="float64")
        flags = []
        if np.any(~np.isfinite(p)):
            return {"storm_index": -1, "storm_id": None, "distance": None,
                    "rule_applied": "none", "flags": ["missing_discharge"]}
        if method == "standardized":
            d = np.sqrt(((self.q - self.mu) / self.sigma
                         - (p - self.mu) / self.sigma) ** 2 @ np.ones(len(p)))
        elif method == "euclidean":
            d = np.sqrt(((self.q - p) ** 2).sum(axis=1))
        elif method == "weighted":
            w = np.asarray(weights, dtype="float64")
            d = np.sqrt((w * (self.q - p) ** 2).sum(axis=1))
        else:
            raise ValueError(f"unknown method {method}")
        idx = int(np.argmin(d))
        if np.any(p > self.q.max(axis=0)):
            flags.append("beyond_library_q")
        return {"storm_index": idx,
                "storm_id": self.store.storm_id[idx],
                "distance": round(float(d[idx]), 4),
                "rule_applied": f"fluvial_{method}",
                "flags": flags}


def member_boundary_q(run_dir: str, cycle: str, series_templates, stat: str = "max"):
    """Read the boundary discharge series of one run; returns (vector, flags)."""
    import csv
    vals, flags = [], []
    for tpl in series_templates:
        path = os.path.join(run_dir, tpl.format(cycle=cycle))
        if not os.path.isfile(path):
            vals.append(float("nan"))
            flags.append(f"missing_{os.path.basename(path)}")
            continue
        col = None
        best = float("nan")
        with open(path) as fh:
            rd = csv.reader(fh)
            header = next(rd)
            for i, h in enumerate(header):
                if h.startswith("Discharge"):
                    col = i
                    break
            if col is None:
                vals.append(float("nan"))
                flags.append("no_discharge_column")
                continue
            acc = []
            for row in rd:
                try:
                    acc.append(float(row[col]))
                except (ValueError, IndexError):
                    pass
            if acc:
                best = max(acc) if stat == "max" else sum(acc) / len(acc)
            else:
                flags.append("empty_series")
        vals.append(best)
    return np.array(vals, dtype="float64"), flags

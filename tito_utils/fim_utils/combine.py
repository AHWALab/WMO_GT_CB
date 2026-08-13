"""Combined pluvial and fluvial (PF) hazard: per-pixel maximum depth.

The union rule requested for the P&F strategy: each member carries one
pluvial scenario and one fluvial scenario; the member's combined flood
map takes the deeper water at every pixel. Degenerate members (only one
hazard matched) pass the available map through unchanged.

Vote counting is done per distinct scenario pair so each pair's combined
map is computed once, however many members share it.
"""

import numpy as np


def combined_depth(store, p_index: int, f_index: int):
    """Pixelwise max of the two matched scenario maps. -1 means no match."""
    if p_index < 0 and f_index < 0:
        return None
    if p_index < 0:
        return store.depth(f_index)
    if f_index < 0 or f_index == p_index:
        d = store.depth(p_index)
        return d if f_index < 0 else np.maximum(d, d)
    return np.maximum(store.depth(p_index), store.depth(f_index))


def exceedance_by_pairs(store, pairs, thresholds, mode: str = "PF"):
    """Member-vote exceedance for a list of (p_index, f_index) per member.

    mode: 'P' uses p_index only, 'F' f_index only, 'PF' the combined map.
    Returns {threshold: probability_array}, n_members_used.
    """
    from collections import Counter

    def key(pair):
        p, f = pair
        if mode == "P":
            return (p, -1)
        if mode == "F":
            return (-1, f)
        return (p, f)

    counts = Counter(key(pr) for pr in pairs
                     if (mode == "P" and pr[0] >= 0)
                     or (mode == "F" and pr[1] >= 0)
                     or (mode == "PF" and (pr[0] >= 0 or pr[1] >= 0)))
    n = sum(counts.values())
    out = {t: np.zeros(store.grid_shape, dtype="float64") for t in thresholds}
    if n == 0:
        return {t: a.astype("float32") for t, a in out.items()}, 0
    for (p, f), w in counts.items():
        depth = combined_depth(store, p, f)
        if depth is None:
            continue
        for t in thresholds:
            out[t] += (depth >= t) * float(w)
    return {t: (a / n).astype("float32") for t, a in out.items()}, n

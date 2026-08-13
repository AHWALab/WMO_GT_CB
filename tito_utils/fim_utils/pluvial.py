"""Pluvial (P) hazard: analog matching on rainfall magnitude.

Thin, named wrapper around the existing magnitude machinery so the three
hazard routines (pluvial, fluvial, combined) read the same way in code
and in configs. The matching itself is the banded round-up rule of
store.match(); the member magnitude is the zone mean of the accumulation
grid over the area of concern (ensemble.zone_stat).

TITO QPE-only chain (no long-range / qpfaccum):
  total = sum of zone-mean **qpeaccum** over rain_components
  e.g. STREAM-Sat + StormLab, or IMERG (+ SCaMPR gap) + GFS forecast.
"""

from .ensemble import zone_stat, grid_path, resolve_component_dir


def member_rain_total(run_dir: str, cycle: str, bounds, files: dict,
                      grid_role: str = "qpe_accum",
                      expand_steps_km=(0, 2, 5, 10, 15), min_valid_cells: int = 50):
    """Zone-mean rainfall total of one run over the AOC. Returns (mm, flags)."""
    gpath = grid_path(run_dir, grid_role, cycle, files)
    if not gpath:
        return float("nan"), [f"missing_{grid_role}"]
    zs = zone_stat(gpath, bounds, "mean", expand_steps_km, min_valid_cells)
    return zs.value, list(zs.flags)


def member_rain_total_components(
    outputs_root: str,
    member_keys: dict,
    cycle: str,
    bounds,
    files: dict,
    components: list,
    expand_steps_km=(0, 2, 5, 10, 15),
    min_valid_cells: int = 50,
):
    """
    Sum zone-mean QPE accums across multiple EF5 run folders for one member.

    Each component: {name, template, grid (default qpe_accum), required, scale}.
    Missing optional components are skipped; missing required → NaN total.
    """
    flags = []
    parts = {}
    total = 0.0
    ok = True
    for comp in components or []:
        name = comp.get("name") or "comp"
        grid_role = comp.get("grid", "qpe_accum")
        # Never use qpf_accum for TITO QPE-only FIM
        if str(grid_role).lower() in ("qpf_accum", "qpfaccum"):
            flags.append(f"skipped_qpf_{name}")
            continue
        try:
            run_dir = resolve_component_dir(
                outputs_root, comp["template"], member_keys, cycle)
        except (KeyError, TypeError):
            run_dir = ""
        gpath = grid_path(run_dir, grid_role, cycle, files) if run_dir else ""
        if not gpath:
            parts[name] = None
            flags.append(f"missing_{name}")
            if comp.get("required", True):
                ok = False
            continue
        zs = zone_stat(
            gpath, bounds, comp.get("stat", "mean"),
            expand_steps_km, min_valid_cells,
        )
        parts[name] = None if zs.value != zs.value else float(zs.value)
        flags.extend(zs.flags)
        if zs.value != zs.value:
            if comp.get("required", True):
                ok = False
        else:
            total += float(zs.value) * float(comp.get("scale", 1.0))
    if not ok:
        return float("nan"), flags, parts
    return total, flags, parts


def match_pluvial(store, total_mm: float, band=(0.9, 1.2), band_wide=(0.8, 1.3)) -> dict:
    """Banded round-up analog match on rainfall magnitude (existing rule)."""
    decision = store.match(total_mm, band=tuple(band), band_wide=tuple(band_wide))
    decision["rule_applied"] = "pluvial_" + decision["rule_applied"]
    return decision

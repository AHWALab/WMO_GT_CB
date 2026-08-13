"""Ensemble FIM cycle: trigger -> per-member totals -> store lookup ->
probabilistic flood map.

Driven entirely by a YAML config (see fim_config/Guatemala_SantaInes.yaml):
layouts describe the ensemble tree, the store holds the pre-simulated
maps, and the product is P(max depth >= threshold) on the FIM grid plus
IBF likelihood classes.

CLI:
    python -m tito_utils.fim_utils.pipeline_ensemble \
        --config fim_config/Guatemala_SantaInes.yaml [--cycle 20260730.150000]

Exit codes: 0 ok (triggered or quiet), 2 no members found.
"""

import csv
import json
import os
import sys

import numpy as np

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise ImportError("PyYAML required") from exc

from .ensemble import (
    discover_ensemble_members, resolve_component_dir, grid_path, zone_stat)
from .store import FimStore
from . import probability as prob_mod

DEFAULT_FILES = {"uq": "maxunitq.{cycle}.tif",
                 "qpe_accum": "qpeaccum.{cycle}.tif",
                 "qpf_accum": "qpfaccum.{cycle}.tif"}


def load_ensemble_config(path: str, root: str = None) -> dict:
    """Load a YAML config. Relative paths inside the YAML resolve against,
    in order of precedence:
        1. the ``root`` argument
        2. a ``root:`` key in the YAML itself
        3. the TITO_FIM_ROOT environment variable
        4. the current working directory (operational default: repo root)
    so the library and its data can live anywhere."""
    with open(path) as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg["_root"] = os.path.abspath(
        root or cfg.get("root") or os.environ.get("TITO_FIM_ROOT") or ".")
    for key in ("region", "outputs_root", "aoc_geojson", "store", "member", "components"):
        if key not in cfg:
            raise ValueError(f"Missing '{key}' in {path}")
    cfg.setdefault("files", {})
    for k, v in DEFAULT_FILES.items():
        cfg["files"].setdefault(k, v)
    cfg.setdefault("cycle_format", "%Y%m%d.%H%M%S")
    cfg.setdefault("trigger", {})
    cfg["trigger"].setdefault("threshold", 1.0)
    cfg["trigger"].setdefault("sources", [{"template": cfg["member"]["template"]}])
    cfg.setdefault("sampling", {})
    cfg["sampling"].setdefault("expand_steps_km", [0, 2, 5, 10, 15])
    cfg["sampling"].setdefault("min_valid_cells", 50)
    cfg.setdefault("matching", {})
    cfg["matching"].setdefault("band", [0.9, 1.2])
    cfg["matching"].setdefault("band_wide", [0.8, 1.3])
    cfg.setdefault("probability", {})
    cfg["probability"].setdefault("depth_threshold_m", 0.30)
    cfg["probability"].setdefault("bands", prob_mod.DEFAULT_BANDS)
    cfg.setdefault("products_root", "fim_out")
    return cfg


def _resolve(cfg, path):
    return path if os.path.isabs(path) else os.path.join(cfg["_root"], path)


def _aoc_bounds(cfg):
    with open(_resolve(cfg, cfg["aoc_geojson"])) as fh:
        gj = json.load(fh)
    xs, ys = [], []

    def walk(coords):
        if not coords:
            return
        if isinstance(coords[0], (int, float)):
            xs.append(coords[0]); ys.append(coords[1]); return
        for c in coords:
            walk(c)

    for feat in gj.get("features", []):
        walk(feat.get("geometry", {}).get("coordinates", []))
    if not xs:
        raise ValueError("AOC geojson has no coordinates")
    return (min(xs), min(ys), max(xs), max(ys))


def run_ensemble_cycle(cfg, cycle: str = None, verbose: bool = True) -> dict:
    if isinstance(cfg, str):
        cfg = load_ensemble_config(cfg)
    log = print if verbose else (lambda *a, **k: None)

    outputs_root = _resolve(cfg, cfg["outputs_root"])
    bounds = _aoc_bounds(cfg)
    files = cfg["files"]
    sampling = cfg["sampling"]

    members = discover_ensemble_members(outputs_root, cfg["member"]["template"],
                                        cycle=cycle, cycle_format=cfg["cycle_format"])
    if not members:
        log(f"  FIM {cfg['region']}: no ensemble members found (cycle={cycle or 'latest'})")
        return {"region": cfg["region"], "cycle": cycle, "status": "no_runs"}
    cycle = members[0].cycle
    log(f"  FIM {cfg['region']} cycle {cycle}: {len(members)} forecast member(s)")

    proot = _resolve(cfg, cfg["products_root"])
    if cfg.get("append_cycle", True):
        out_dir = os.path.join(proot, cycle)
    else:
        out_dir = proot
    os.makedirs(out_dir, exist_ok=True)

    # ---- 1. Trigger: max UQ over every trigger source of every member ------
    threshold = float(cfg["trigger"]["threshold"])
    trig_rows, seen_dirs = [], set()
    max_uq_overall = float("nan")
    for member in members:
        for source in cfg["trigger"]["sources"]:
            try:
                run_dir = resolve_component_dir(outputs_root, source["template"],
                                                member.keys, cycle)
            except KeyError:
                continue
            if run_dir in seen_dirs or not os.path.isdir(run_dir):
                continue
            seen_dirs.add(run_dir)
            uq_path = grid_path(run_dir, "uq", cycle, files)
            if not uq_path:
                continue
            zs = zone_stat(uq_path, bounds, "max",
                           sampling["expand_steps_km"], sampling["min_valid_cells"])
            rel = os.path.relpath(run_dir, outputs_root).replace(os.sep, "/")
            trig_rows.append({"source": rel, "max_uq": None if zs.value != zs.value else round(zs.value, 4),
                              "n_valid_cells": zs.n_valid, "expand_km": zs.expand_km,
                              "flags": ";".join(zs.flags)})
            if zs.value == zs.value:
                max_uq_overall = zs.value if max_uq_overall != max_uq_overall else max(max_uq_overall, zs.value)

    triggered = (max_uq_overall == max_uq_overall) and max_uq_overall >= threshold
    _write_csv(os.path.join(out_dir, "trigger_maxuq.csv"), trig_rows)
    log(f"    trigger: max UQ = {max_uq_overall:.3f} vs {threshold} -> "
        f"{'TRIGGERED' if triggered else 'quiet'}  ({len(trig_rows)} runs checked)")

    summary = {"region": cfg["region"], "cycle": cycle,
               "trigger": {"threshold": threshold,
                           "max_uq": None if max_uq_overall != max_uq_overall else round(max_uq_overall, 4),
                           "runs_checked": len(trig_rows), "triggered": bool(triggered)}}

    if not triggered:
        summary["status"] = "quiet"
        _dump(summary, cfg, out_dir)
        return summary

    # ---- 2. Rainfall totals per member (sum of component zone means) -------
    store = FimStore(_resolve(cfg, cfg["store"]))
    totals_rows, indices, member_flags = [], [], []
    for member in members:
        row = {"member_id": member.member_id}
        flags, total, ok = [], 0.0, True
        for comp in cfg["components"]:
            try:
                run_dir = resolve_component_dir(outputs_root, comp["template"],
                                                member.keys, cycle)
            except KeyError:
                run_dir = ""
            gpath = grid_path(run_dir, comp.get("grid", "qpe_accum"), cycle, files) if run_dir else ""
            if not gpath:
                row[comp["name"]] = None
                flags.append(f"missing_{comp['name']}")
                if comp.get("required", True):
                    ok = False
                continue
            zs = zone_stat(gpath, bounds, comp.get("stat", "mean"),
                           sampling["expand_steps_km"], sampling["min_valid_cells"])
            row[comp["name"]] = None if zs.value != zs.value else round(zs.value, 2)
            flags.extend(zs.flags)
            if zs.value != zs.value:
                ok = False
            else:
                total += zs.value * float(comp.get("scale", 1.0))
        row["total_mm"] = round(total, 2) if ok else None
        # ---- 3. store lookup ------------------------------------------------
        decision = store.match(total if ok else float("nan"),
                               band=tuple(cfg["matching"]["band"]),
                               band_wide=tuple(cfg["matching"]["band_wide"]))
        row.update({"storm_id": decision["storm_id"],
                    "storm_magnitude_mm": decision["storm_magnitude_mm"],
                    "rule_applied": decision["rule_applied"],
                    "flags": ";".join(sorted(set(flags + decision["flags"])))})
        totals_rows.append(row)
        if ok and decision["storm_index"] >= 0:
            indices.append(decision["storm_index"])
        member_flags.extend(flags)
    _write_csv(os.path.join(out_dir, "member_matches.csv"), totals_rows)
    used = len(indices)
    log(f"    members matched: {used}/{len(members)}")
    if used == 0:
        summary["status"] = "triggered_no_members"
        _dump(summary, cfg, out_dir)
        return summary

    # ---- 4. probability product on the FIM grid ----------------------------
    thr = float(cfg["probability"]["depth_threshold_m"])
    prob, n_used = prob_mod.exceedance_probability(store, indices, thr)
    classes = prob_mod.classify_likelihood(prob, cfg["probability"]["bands"])

    prob_tif = os.path.join(out_dir, f"prob_depth_ge_{int(round(thr*100))}cm.{cycle}.tif")
    class_tif = os.path.join(out_dir, f"likelihood_class.{cycle}.tif")
    prob_mod.write_geotiff(prob_tif, prob, store.transform, store.crs, nodata=None)
    prob_mod.write_geotiff(class_tif, classes, store.transform, store.crs, nodata=0)

    mag_src = store.attrs.get("magnitude_source", "unknown")
    note = f"members: {n_used} | catalog magnitudes: {mag_src}"
    if "placeholder" in str(mag_src).lower():
        note += "  ** DEMO: placeholder magnitudes, refresh with RainyDay totals **"
    png = os.path.join(out_dir, f"quicklook.{cycle}.png")
    prob_mod.quicklook_png(png, prob, f"{cfg['region']} {cycle}", thr, note)

    summary.update({
        "status": "triggered",
        "members_total": len(members), "members_used": n_used,
        "depth_threshold_m": thr,
        "probability_summary": prob_mod.summarize(prob, cfg["probability"]["bands"]),
        "catalog": {"store": cfg["store"], "n_storms": store.n_storms,
                    "magnitude_source": mag_src,
                    "magnitude_range_mm": [float(np.nanmin(store.magnitude)),
                                           float(np.nanmax(store.magnitude))]},
        "sampling_flags": sorted(set(member_flags)),
        "products": [os.path.basename(p) for p in (prob_tif, class_tif, png)],
    })
    _dump(summary, cfg, out_dir)
    log(f"    products -> {out_dir}")
    return summary


def _write_csv(path, rows):
    if not rows:
        with open(path, "w") as fh:
            fh.write("")
        return
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def _dump(summary, cfg, out_dir):
    summary["config_echo"] = {k: cfg[k] for k in
                              ("matching", "probability", "sampling", "trigger") if k in cfg}
    with open(os.path.join(out_dir, "fim_summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Run one ensemble FIM cycle")
    parser.add_argument("--config", required=True)
    parser.add_argument("--cycle", default=None)
    parser.add_argument("--root", default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_ensemble_config(args.config, root=args.root)
    summary = run_ensemble_cycle(cfg, cycle=args.cycle, verbose=not args.quiet)
    return 2 if summary.get("status") == "no_runs" else 0


if __name__ == "__main__":
    sys.exit(main())

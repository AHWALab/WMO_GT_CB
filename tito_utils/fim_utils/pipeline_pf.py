"""Pluvial + Fluvial (PF) ensemble FIM cycle runner.

One config drives three reproducible routines:
    hazards.pluvial.enabled  -> P products   (rainfall analog matching)
    hazards.fluvial.enabled  -> F products   (boundary discharge analog matching)
    both enabled             -> PF products  (per-pixel maximum of the two)

Per member the runner logs every decision (rain total, matched pluvial
scenario, boundary discharges, matched fluvial scenario, distance), then
counts member votes per depth threshold for each active routine.

CLI:
    python -m tito_utils.fim_utils.pipeline_pf --config cfg.yaml \
        [--cycle 20230620.000000] [--hazard P|F|PF]
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

from .ensemble import (discover_ensemble_members, resolve_component_dir,
                       grid_path, zone_stat)
from .store import FimStore
from .fluvial import FluvialMatcher, member_boundary_q
from .pluvial import member_rain_total, member_rain_total_components, match_pluvial
from .combine import exceedance_by_pairs
from . import probability as prob_mod

DEFAULT_FILES = {"uq": "maxunitq.{cycle}.tif",
                 "qpe_accum": "qpeaccum.{cycle}.tif",
                 "qpf_accum": "qpfaccum.{cycle}.tif"}


def load_pf_config(path: str, root: str = None) -> dict:
    with open(path) as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg["_root"] = os.path.abspath(
        root or cfg.get("root") or os.environ.get("TITO_FIM_ROOT") or ".")
    for key in ("region", "outputs_root", "aoc_geojson", "store", "member", "hazards"):
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
    hz = cfg["hazards"]
    hz.setdefault("pluvial", {})
    hz.setdefault("fluvial", {})
    hz["pluvial"].setdefault("enabled", True)
    hz["pluvial"].setdefault("band", [0.9, 1.2])
    hz["pluvial"].setdefault("band_wide", [0.8, 1.3])
    hz["pluvial"].setdefault("grid", "qpe_accum")  # single-folder fallback only
    # rain_components: list of {name, template, grid=qpe_accum, required}
    # When set, pluvial total = sum of component QPE accums (no qpf_accum).
    cfg.setdefault("rain_components", [])
    hz["fluvial"].setdefault("enabled", False)
    hz["fluvial"].setdefault("stat", "max")
    hz["fluvial"].setdefault("method", "standardized")
    cfg.setdefault("thresholds_m", [0.10, 0.30, 0.50, 1.00])
    cfg.setdefault("likelihood_bands", prob_mod.DEFAULT_BANDS)
    cfg.setdefault("overbank", {})
    cfg["overbank"].setdefault("enabled", True)
    cfg["overbank"].setdefault("reference_scenario", "sample_0002")
    cfg.setdefault("products_root", "fim_out_pf")
    return cfg


def _resolve(cfg, p):
    return p if os.path.isabs(p) else os.path.join(cfg["_root"], p)


def _aoc_bounds(cfg):
    with open(_resolve(cfg, cfg["aoc_geojson"])) as fh:
        gj = json.load(fh)
    xs, ys = [], []

    def walk(c):
        if not c:
            return
        if isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1]); return
        for e in c:
            walk(e)
    for f in gj.get("features", []):
        walk(f.get("geometry", {}).get("coordinates", []))
    return (min(xs), min(ys), max(xs), max(ys))


def _tag(t):
    return f"{round(t*100)}cm"


def run_pf_cycle(cfg, cycle: str = None, hazard_filter: str = None,
                 verbose: bool = True) -> dict:
    if isinstance(cfg, str):
        cfg = load_pf_config(cfg)
    log = print if verbose else (lambda *a, **k: None)
    outputs_root = _resolve(cfg, cfg["outputs_root"])
    bounds = _aoc_bounds(cfg)
    files, sampling = cfg["files"], cfg["sampling"]
    hz_p, hz_f = cfg["hazards"]["pluvial"], cfg["hazards"]["fluvial"]
    thresholds = [float(t) for t in cfg["thresholds_m"]]

    members = discover_ensemble_members(outputs_root, cfg["member"]["template"],
                                        cycle=cycle, cycle_format=cfg["cycle_format"])
    if not members:
        return {"region": cfg["region"], "cycle": cycle, "status": "no_runs"}
    cycle = members[0].cycle
    # products_root may already be cycle-first (TITO hook sets full path)
    proot = _resolve(cfg, cfg["products_root"])
    if cfg.get("append_cycle", True):
        out_dir = os.path.join(proot, cycle)
    else:
        out_dir = proot
    os.makedirs(out_dir, exist_ok=True)
    log(f"  PF FIM {cfg['region']} {cycle}: {len(members)} members "
        f"(P={hz_p['enabled']}, F={hz_f['enabled']})")

    # ---- trigger (unchanged semantics) --------------------------------------
    threshold = float(cfg["trigger"]["threshold"])
    max_uq, rows_trig, seen = float("nan"), [], set()
    for m in members:
        for src in cfg["trigger"]["sources"]:
            try:
                rd = resolve_component_dir(outputs_root, src["template"], m.keys, cycle)
            except KeyError:
                continue
            if rd in seen or not os.path.isdir(rd):
                continue
            seen.add(rd)
            up = grid_path(rd, "uq", cycle, files)
            if not up:
                continue
            zs = zone_stat(up, bounds, "max",
                           sampling["expand_steps_km"], sampling["min_valid_cells"])
            rows_trig.append({"source": os.path.relpath(rd, outputs_root).replace(os.sep, "/"),
                              "max_uq": None if zs.value != zs.value else round(zs.value, 4),
                              "n_valid_cells": zs.n_valid, "expand_km": zs.expand_km,
                              "flags": ";".join(zs.flags)})
            if zs.value == zs.value:
                max_uq = zs.value if max_uq != max_uq else max(max_uq, zs.value)
    _write_csv(os.path.join(out_dir, "trigger_maxuq.csv"), rows_trig)
    triggered = (max_uq == max_uq) and max_uq >= threshold
    summary = {"region": cfg["region"], "cycle": cycle,
               "trigger": {"threshold": threshold,
                           "max_uq": None if max_uq != max_uq else round(max_uq, 4),
                           "runs_checked": len(rows_trig), "triggered": bool(triggered)},
               "hazards_enabled": {"pluvial": hz_p["enabled"], "fluvial": hz_f["enabled"]},
               "thresholds_m": thresholds}
    log(f"    trigger: max UQ {max_uq:.2f} vs {threshold} -> "
        f"{'TRIGGERED' if triggered else 'quiet'}")
    if not triggered:
        summary["status"] = "quiet"
        json.dump(summary, open(os.path.join(out_dir, "pf_summary.json"), "w"), indent=2)
        return summary

    # ---- per-member decisions ----------------------------------------------
    store = FimStore(_resolve(cfg, cfg["store"]))
    fmatch = FluvialMatcher(store) if hz_f["enabled"] else None
    rows, pairs = [], []
    for m in members:
        run_dir = resolve_component_dir(outputs_root, cfg["member"]["template"],
                                        m.keys, cycle)
        row = {"member_id": m.member_id}
        p_idx = f_idx = -1
        if hz_p["enabled"]:
            rain_comps = cfg.get("rain_components") or []
            if rain_comps:
                # TITO QPE-only: sum qpeaccum across phases (SS+SL or IMERG±gap+GFS)
                total, pfl, parts = member_rain_total_components(
                    outputs_root, m.keys, cycle, bounds, files, rain_comps,
                    sampling["expand_steps_km"], sampling["min_valid_cells"])
                for pname, pval in parts.items():
                    row[f"rain_{pname}"] = (
                        None if pval is None else round(float(pval), 2))
            else:
                total, pfl = member_rain_total(
                    run_dir, cycle, bounds, files, hz_p.get("grid", "qpe_accum"),
                    sampling["expand_steps_km"], sampling["min_valid_cells"])
            dec = match_pluvial(store, total, hz_p["band"], hz_p["band_wide"])
            p_idx = dec["storm_index"]
            row.update({"total_mm": None if total != total else round(total, 2),
                        "p_storm": dec["storm_id"], "p_rule": dec["rule_applied"],
                        "p_flags": ";".join(sorted(set(pfl + dec["flags"])))})
        if hz_f["enabled"]:
            qvec, qfl = member_boundary_q(run_dir, cycle,
                                          hz_f["boundary_series"], hz_f["stat"])
            fdec = fmatch.match(qvec, hz_f["method"])
            f_idx = fdec["storm_index"]
            for name, val in zip(fmatch.names or
                                 [f"Q{i+1}" for i in range(len(qvec))], qvec):
                row[name] = None if val != val else round(float(val), 2)
            row.update({"f_storm": fdec["storm_id"], "f_distance": fdec["distance"],
                        "f_flags": ";".join(sorted(set(qfl + fdec["flags"])))})
        rows.append(row)
        pairs.append((p_idx, f_idx))
    _write_csv(os.path.join(out_dir, "member_decisions.csv"), rows)

    # ---- products per routine ----------------------------------------------
    modes = []
    if hz_p["enabled"]:
        modes.append("P")
    if hz_f["enabled"]:
        modes.append("F")
    if hz_p["enabled"] and hz_f["enabled"]:
        modes.append("PF")
    if hazard_filter:
        modes = [m for m in modes if m == hazard_filter.upper()]
    mode_dirs = {"P": "pluvial", "F": "fluvial", "PF": "combined"}
    summary["routines"] = {}
    ob_cfg = cfg["overbank"]
    ref_depth = None
    if ob_cfg["enabled"]:
        sid = list(store.storm_id)
        ref = ob_cfg["reference_scenario"]
        if ref in sid:
            ref_depth = store.depth(sid.index(ref))
        else:
            log(f"    overbank reference '{ref}' not in store, overbank skipped")
    for mode in modes:
        probs, n_used = exceedance_by_pairs(store, pairs, thresholds, mode)
        mdir = os.path.join(out_dir, mode_dirs[mode])
        os.makedirs(mdir, exist_ok=True)
        stats = {}
        for t in thresholds:
            p = probs[t]
            prob_mod.write_geotiff(
                os.path.join(mdir, f"prob_depth_ge_{_tag(t)}.{cycle}.tif"),
                p, store.transform, store.crs)
            prob_mod.write_geotiff(
                os.path.join(mdir, f"likelihood_class_ge_{_tag(t)}.{cycle}.tif"),
                prob_mod.classify_likelihood(p, cfg["likelihood_bands"]),
                store.transform, store.crs, nodata=0)
            stats[_tag(t)] = {"max_probability": round(float(p.max()), 3),
                              "pixels_gt0": int((p > 0).sum())}
            if ref_depth is not None:
                ob = np.where(ref_depth >= t, 0.0, p).astype("float32")
                obdir = os.path.join(out_dir, mode_dirs[mode] + "_overbank")
                os.makedirs(obdir, exist_ok=True)
                prob_mod.write_geotiff(
                    os.path.join(obdir, f"prob_depth_ge_{_tag(t)}_overbank.{cycle}.tif"),
                    ob, store.transform, store.crs)
                prob_mod.write_geotiff(
                    os.path.join(obdir, f"likelihood_class_ge_{_tag(t)}_overbank.{cycle}.tif"),
                    prob_mod.classify_likelihood(ob, cfg["likelihood_bands"]),
                    store.transform, store.crs, nodata=0)
                stats[_tag(t)]["overbank_max_probability"] = round(float(ob.max()), 3)
                stats[_tag(t)]["overbank_pixels_gt0"] = int((ob > 0).sum())
        summary["routines"][mode] = {"members_used": n_used, "stats": stats}
        log(f"    {mode:2s}: n={n_used} " + " ".join(
            f"{k}:maxP={v['max_probability']} px={v['pixels_gt0']}"
            for k, v in stats.items()))
    summary["status"] = "triggered"
    summary["catalog"] = {"store": cfg["store"], "n_storms": store.n_storms,
                          "pluvial_magnitude_source": store.attrs.get("magnitude_source"),
                          "fluvial_index_source": "boundary discharges (real, from flood_library.mat)"}
    json.dump(summary, open(os.path.join(out_dir, "pf_summary.json"), "w"), indent=2)
    return summary


def _write_csv(path, rows):
    if not rows:
        open(path, "w").write("")
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


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Run one P/F/PF FIM cycle")
    ap.add_argument("--config", required=True)
    ap.add_argument("--cycle", default=None)
    ap.add_argument("--root", default=None)
    ap.add_argument("--hazard", default=None, choices=["P", "F", "PF", None])
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_pf_config(a.config, root=a.root)
    s = run_pf_cycle(cfg, cycle=a.cycle, hazard_filter=a.hazard, verbose=not a.quiet)
    return 2 if s.get("status") == "no_runs" else 0


if __name__ == "__main__":
    sys.exit(main())

"""The FIM cycle: trigger -> totals -> match -> products.

Called by the TITO orchestrator after the EF5 runs finish, or standalone:

    python -m tito_utils.fim_utils.pipeline --config fim_config/Barbados.yaml
    python -m tito_utils.fim_utils.pipeline --config ... --cycle 20240704.090000

Exit codes: 0 success (triggered or quiet), 2 no EF5 runs found,
1 unexpected error. Everything the run decided is in fim_summary.json.
"""

import json
import os
import sys

from .config import load_config
from .aoc import load_aocs
from .ef5_runs import discover_runs
from .trigger import evaluate_trigger
from .rainfall import member_totals
from .catalog import Catalog
from .matching import MatchRules, match_members, select_scenarios
from .products import export_products


def run_fim_cycle(config, cycle: str = None, verbose: bool = True) -> dict:
    """Run one FIM lookup cycle for one region.

    config: FimConfig or path to a YAML config.
    cycle : cycle timestamp string; None = latest found on disk.
    Returns the summary dict (also written to fim_summary.json).
    """
    if isinstance(config, str):
        config = load_config(config)

    log = print if verbose else (lambda *a, **k: None)

    aocs = load_aocs(config.resolve(config.aoc.source),
                     id_field=config.aoc.id_field,
                     name_field=config.aoc.name_field,
                     layer=config.aoc.layer)
    log(f"  FIM {config.region}: {len(aocs)} areas of concern")

    ef5 = config.ef5
    runs = discover_runs(
        config.resolve(ef5.outputs_root), config.region, cycle=cycle,
        run_dir_glob=ef5.run_dir_glob, cycle_format=ef5.cycle_format,
        file_templates={
            "uq": ef5.files.uq, "qpe_accum": ef5.files.qpe_accum,
            "qpf_accum": ef5.files.qpf_accum, "max_q": ef5.files.max_q,
        },
        qpe_sources=ef5.qpe_sources, qpf_sources=ef5.qpf_sources,
        require_qpf=ef5.require_qpf)

    if not runs:
        log(f"  FIM {config.region}: no EF5 runs found "
            f"(cycle={cycle or 'latest'}); nothing to do")
        return {"region": config.region, "cycle": cycle, "status": "no_runs"}

    cycle = runs[0].cycle
    log(f"  FIM {config.region} cycle {cycle}: {len(runs)} member(s): "
        + ", ".join(r.member_id for r in runs))

    # 1. Trigger on EF5 max unit streamflow
    trig = evaluate_trigger(runs, aocs,
                            threshold=config.trigger.uq_threshold,
                            scope=config.trigger.scope)
    if not trig.triggered:
        log(f"  FIM {config.region}: quiet (max UQ below "
            f"{config.trigger.uq_threshold} in all areas of concern)")
        out_dir = os.path.join(config.outputs_dir, cycle)
        os.makedirs(out_dir, exist_ok=True)
        summary = {"region": config.region, "cycle": cycle, "status": "quiet",
                   "trigger": trig.to_dict()}
        with open(os.path.join(out_dir, "fim_summary.json"), "w") as fh:
            json.dump(summary, fh, indent=2)
        return summary

    log(f"  FIM {config.region}: TRIGGERED in {trig.triggered_aocs}")

    # 2. Rainfall totals per AOC per member (QPE accum + QPF accum)
    totals = member_totals(runs, aocs, stat=config.rainfall.stat,
                           qpe_scale=config.rainfall.qpe_scale,
                           qpf_scale=config.rainfall.qpf_scale,
                           missing_qpf=config.rainfall.missing_qpf)

    # 3. Match against the catalog
    catalog = Catalog.load(config.resolve(config.catalog_path))
    rules = MatchRules.from_config(config.matching)
    decisions = match_members(totals, catalog, rules)
    selections = select_scenarios(decisions, config.matching.selectors)

    # 4. Products
    out_dir = os.path.join(config.outputs_dir, cycle)
    clip_aocs = {a.aoc_id: a for a in aocs} if config.outputs.clip_to_aoc else None
    summary = export_products(
        out_dir, cycle, config.region, trig, totals, decisions, selections,
        catalog, clip_aocs=clip_aocs,
        config_echo={
            "uq_threshold": config.trigger.uq_threshold,
            "rainfall_stat": config.rainfall.stat,
            "band": list(rules.band), "band_wide": list(rules.band_wide),
            "use_direction": rules.use_direction,
            "catalog_path": config.catalog_path,
        })
    summary["status"] = "triggered"

    for aoc_id, named in selections.items():
        if aoc_id in trig.triggered_aocs:
            parts = [f"{sel}={d.storm_id} ({d.storm_magnitude_mm:.0f} mm, {d.rule_applied})"
                     for sel, d in named.items()]
            log(f"    {aoc_id}: " + " | ".join(parts))
    log(f"  FIM products -> {out_dir}")
    return summary


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Run one FIM lookup cycle")
    parser.add_argument("--config", required=True, help="Region FIM YAML config")
    parser.add_argument("--cycle", default=None,
                        help="Cycle timestamp (default: latest on disk)")
    parser.add_argument("--root", default=None,
                        help="Repo root for relative paths (default: cwd)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config, root=args.root)
        summary = run_fim_cycle(config, cycle=args.cycle, verbose=not args.quiet)
    except Exception as exc:  # pragma: no cover
        print(f"FIM pipeline error: {exc}")
        return 1
    if summary.get("status") == "no_runs":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

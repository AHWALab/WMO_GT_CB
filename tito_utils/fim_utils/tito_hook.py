"""
TITO orchestrator hook — FIM after the **forecast** EF5 phase only.

Rules (TITO Guatemala Training):
  - Only regions at **90m** (FIM library / AOC are 90m-scale).
  - Only when a forecast phase ran (``run_LR`` / Phase C GFS or StormLab).
  - Pluvial rain = sum of **qpeaccum** components (no qpfaccum / long-range).
  - Non-fatal: never blocks EF5.

Output layout (cycle-first, chain-tagged)::

  outputs/<cycle>/<rkey>/fim/<chain>/
    e.g. outputs/20230621.070000/guatemala_90m/fim/stream_sat_stormlab/
         outputs/20230621.070000/guatemala_90m/fim/imerg_gfs/

Discovers ``fim_config/<Region>*.yaml``. YAMLs with ``hazards:`` → pipeline_pf;
else → pipeline_ensemble.
"""

from __future__ import annotations

import glob
import os
import re
from typing import Any, Dict, List, Optional, Sequence


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def discover_fim_configs(
    regions: Sequence[str],
    fim_config_dir: str,
) -> List[str]:
    """Return sorted YAML paths for enabled FIM sites matching *regions*."""
    cfg_dir = os.path.abspath(fim_config_dir)
    if not os.path.isdir(cfg_dir):
        return []
    out: List[str] = []
    for region in regions:
        pattern = os.path.join(cfg_dir, f"{region}*.yaml")
        for path in sorted(glob.glob(pattern)):
            path = os.path.abspath(path)
            if os.path.dirname(path) != cfg_dir:
                continue
            try:
                import yaml
                with open(path) as fh:
                    raw = yaml.safe_load(fh) or {}
                if raw.get("enabled") is False:
                    continue
            except Exception:
                continue
            out.append(path)
    return out


def _regions_at_90m(
    regions: Sequence[str],
    config: Any,
) -> List[str]:
    """FIM only for 90m model resolution."""
    try:
        from tito_utils.ef5.jobs.helpers import resolve_region_resolution
    except Exception:
        resolve_region_resolution = None

    model_res = getattr(config, "model_resolution", "90m") if config else "90m"
    rmap = getattr(config, "region_resolution_map", {}) if config else {}
    keep = []
    for r in regions:
        if resolve_region_resolution is not None:
            res = resolve_region_resolution(r, model_res, rmap)
        else:
            res = (rmap or {}).get(r, model_res)
        res_s = str(res).lower().replace(" ", "")
        if res_s in ("90m", "90", "0.09km"):
            keep.append(r)
    return keep


def _slug(s: str) -> str:
    s = str(s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_") or "unknown"


def forcing_chain_tag(region: str, config: Any = None) -> str:
    """
    Build a short chain id from region_forcing_map, e.g.:
      STREAM_SAT + STORMLAB → stream_sat_stormlab
      IMERG + GFS           → imerg_gfs
    """
    fmap = getattr(config, "region_forcing_map", None) or {}
    entry = fmap.get(region) or {}
    qpe = entry.get("qpe_source") or entry.get("qpe") or ""
    qpf = entry.get("qpf_source") or entry.get("qpf_sources") or ""
    if isinstance(qpf, (list, tuple)):
        qpf_parts = [_slug(x) for x in qpf if x]
    else:
        qpf_parts = [_slug(qpf)] if qpf else []
    parts = []
    if qpe:
        parts.append(_slug(qpe))
    parts.extend(qpf_parts)
    return "_".join(parts) if parts else "unknown"


def _region_key(region: str, config: Any = None) -> str:
    try:
        from tito_utils.ef5.jobs.helpers import (
            region_path_key,
            resolve_region_resolution,
        )
        model_res = getattr(config, "model_resolution", "90m") if config else "90m"
        rmap = getattr(config, "region_resolution_map", {}) if config else {}
        res = resolve_region_resolution(region, model_res, rmap)
        return region_path_key(region, res)
    except Exception:
        res = "90m"
        if config is not None:
            res = (getattr(config, "region_resolution_map", {}) or {}).get(
                region, getattr(config, "model_resolution", "90m"))
        return f"{region.lower()}_{res}"


def _match_yaml_to_region(yml_basename: str, regions: Sequence[str]) -> Optional[str]:
    """Pick the longest region name that is a prefix of the yaml basename."""
    best = None
    for r in regions:
        if yml_basename.startswith(r) and (best is None or len(r) > len(best)):
            best = r
    return best


# Path templates for cycle-first EF5 layout, keyed by forcing_chain_tag().
# Applied at runtime so one site YAML works for both training chains.
_CHAIN_TEMPLATES = {
    "stream_sat_stormlab": {
        "member": "{cycle}/{rkey}/stormlab/ensOut{ens}_sl{sl}",
        "rain_components": [
            {"name": "stream_sat", "template": "{cycle}/{rkey}/stream_sat/ensOut{ens}",
             "grid": "qpe_accum", "required": True},
            {"name": "stormlab", "template": "{cycle}/{rkey}/stormlab/ensOut{ens}_sl{sl}",
             "grid": "qpe_accum", "required": True},
        ],
        "trigger_sources": [
            {"template": "{cycle}/{rkey}/stream_sat/ensOut{ens}"},
            {"template": "{cycle}/{rkey}/stormlab/ensOut{ens}_sl{sl}"},
        ],
    },
    "imerg_gfs": {
        "member": "{cycle}/{rkey}/gfs",
        "rain_components": [
            {"name": "imerg", "template": "{cycle}/{rkey}/imerg",
             "grid": "qpe_accum", "required": True},
            {"name": "scampr_gap", "template": "{cycle}/{rkey}/scampr_det",
             "grid": "qpe_accum", "required": False},
            {"name": "gfs_forecast", "template": "{cycle}/{rkey}/gfs",
             "grid": "qpe_accum", "required": True},
        ],
        "trigger_sources": [
            {"template": "{cycle}/{rkey}/gfs"},
            {"template": "{cycle}/{rkey}/imerg"},
        ],
    },
    "imerg_stormlab": {
        "member": "{cycle}/{rkey}/stormlab/ensOut{ens}_sl{sl}",
        "rain_components": [
            {"name": "imerg", "template": "{cycle}/{rkey}/imerg",
             "grid": "qpe_accum", "required": True},
            {"name": "scampr_gap", "template": "{cycle}/{rkey}/scampr_det",
             "grid": "qpe_accum", "required": False},
            {"name": "stormlab", "template": "{cycle}/{rkey}/stormlab/ensOut{ens}_sl{sl}",
             "grid": "qpe_accum", "required": True},
        ],
        "trigger_sources": [
            {"template": "{cycle}/{rkey}/stormlab/ensOut{ens}_sl{sl}"},
            {"template": "{cycle}/{rkey}/imerg"},
        ],
    },
    "stream_sat_gfs": {
        "member": "{cycle}/{rkey}/gfs/ensOut{ens}",
        "rain_components": [
            {"name": "stream_sat", "template": "{cycle}/{rkey}/stream_sat/ensOut{ens}",
             "grid": "qpe_accum", "required": True},
            {"name": "gfs_forecast", "template": "{cycle}/{rkey}/gfs/ensOut{ens}",
             "grid": "qpe_accum", "required": True},
        ],
        "trigger_sources": [
            {"template": "{cycle}/{rkey}/gfs/ensOut{ens}"},
            {"template": "{cycle}/{rkey}/stream_sat/ensOut{ens}"},
        ],
    },
}


def _apply_chain_templates(cfg: dict, chain: str) -> None:
    """Override member / rain / trigger paths for the active forcing chain."""
    spec = _CHAIN_TEMPLATES.get(chain)
    if not spec:
        return
    cfg.setdefault("member", {})
    cfg["member"]["template"] = spec["member"]
    cfg["rain_components"] = [dict(x) for x in spec["rain_components"]]
    cfg.setdefault("trigger", {})
    cfg["trigger"]["sources"] = [dict(x) for x in spec["trigger_sources"]]


def _explain_no_runs(outputs_root: str, template: str, cycle: str, chain: str) -> str:
    """Human-readable reason when discover finds zero members."""
    import glob as _glob
    tmpl = (template or "").strip("/").replace("\\", "/")
    if "{cycle}" in tmpl and cycle:
        search = tmpl.replace("{cycle}", cycle)
    else:
        search = tmpl
    # freeze known placeholders for a concrete glob hint
    hint = search
    for ph in ("{rkey}", "{ens}", "{sl}", "{qpf}"):
        hint = hint.replace(ph, "*")
    pattern = os.path.join(outputs_root, hint)
    hits = _glob.glob(pattern)
    # also list what exists under cycle/
    cycle_dir = os.path.join(outputs_root, cycle) if cycle else outputs_root
    kids = []
    if os.path.isdir(cycle_dir):
        for r in sorted(os.listdir(cycle_dir))[:8]:
            rp = os.path.join(cycle_dir, r)
            if os.path.isdir(rp):
                prods = [p for p in sorted(os.listdir(rp))[:12] if os.path.isdir(os.path.join(rp, p))]
                kids.append(f"{r}/[{', '.join(prods)}]")
    lines = [
        f"no EF5 member folders matched chain={chain}",
        f"  expected template: {template}",
        f"  glob tried:        {pattern}",
        f"  matches:           {len(hits)}",
    ]
    if kids:
        lines.append(f"  under outputs/{cycle}/: " + "; ".join(kids))
    else:
        lines.append(f"  under outputs/{cycle}/: (missing or empty)")
    lines.append(
        "  tip: FIM needs the forecast folder for this chain "
        "(imerg_gfs → …/gfs/; stream_sat_stormlab → …/stormlab/ensOut*_sl*/)"
    )
    return "\n".join(lines)


def run_fim_for_cycle(
    *,
    regions_to_run: Sequence[str],
    cycle: str,
    config: Any = None,
    master_log: Any = None,
    verbose: bool = True,
    forecast_ran: bool = True,
    region_qpe_sources: Optional[Dict[str, str]] = None,
    region_qpf_sources: Optional[Dict[str, Sequence[str]]] = None,
) -> List[dict]:
    """
    Run FIM after forecast EF5 finishes one cycle.

    Parameters
    ----------
    forecast_ran
        Must be True (Phase C / run_LR produced forecast QPE runs). Otherwise skip.
    region_qpe_sources / region_qpf_sources
        Optional explicit maps; otherwise read from config.region_forcing_map.
    """
    log = print if verbose else (lambda *a, **k: None)

    enabled = True
    if config is not None:
        enabled = bool(getattr(config, "fim_enabled", True))
    if not enabled:
        log("  FIM: disabled (fim_enabled=False)")
        if master_log:
            master_log.info("FIM disabled via fim_enabled=False")
        return []

    if not forecast_ran:
        log("  FIM: skipped (no forecast phase this cycle)")
        if master_log:
            master_log.info("FIM skipped — forecast_ran=False")
        return []

    # 90m only
    regions_90 = _regions_at_90m(regions_to_run, config)
    skipped = [r for r in regions_to_run if r not in regions_90]
    if skipped:
        log(f"  FIM: skip non-90m regions: {', '.join(skipped)}")
        if master_log:
            master_log.info("FIM skip non-90m: %s", skipped)
    if not regions_90:
        log("  FIM: no 90m regions — skip")
        if master_log:
            master_log.info("FIM skipped — no 90m regions")
        return []

    root = _project_root()
    if config is not None and getattr(config, "fim_root", None):
        root = os.path.abspath(getattr(config, "fim_root"))

    cfg_dir = os.path.join(root, "fim_config")
    if config is not None and getattr(config, "fim_config_dir", None):
        d = getattr(config, "fim_config_dir")
        cfg_dir = d if os.path.isabs(d) else os.path.join(root, d)

    yaml_paths = discover_fim_configs(regions_90, cfg_dir)
    if not yaml_paths:
        log(f"  FIM: no site configs under {cfg_dir} for {regions_90}")
        if master_log:
            master_log.info("FIM: no configs for %s in %s", regions_90, cfg_dir)
        return []

    os.environ.setdefault("TITO_FIM_ROOT", root)
    data_root = getattr(config, "dataPath", "outputs/") if config else "outputs/"
    summaries: List[dict] = []

    for yml in yaml_paths:
        site = os.path.basename(yml)
        site_stem = os.path.splitext(site)[0]
        region = _match_yaml_to_region(site_stem, regions_90) or regions_90[0]
        rkey = _region_key(region, config)

        # Prefer explicit maps from orchestrator; else config.region_forcing_map
        if region_qpe_sources is not None or region_qpf_sources is not None:
            qpe = (region_qpe_sources or {}).get(region, "")
            qpf = (region_qpf_sources or {}).get(region, [])
            if isinstance(qpf, str):
                qpf = [qpf]
            chain = "_".join(
                p for p in [_slug(qpe)] + [_slug(x) for x in (qpf or []) if x] if p
            ) or forcing_chain_tag(region, config)
        else:
            chain = forcing_chain_tag(region, config)

        # Cycle-first + chain-tagged products root (do not append cycle again)
        products_root = os.path.join(
            data_root.rstrip("/\\"), cycle, rkey, "fim", chain)

        try:
            import yaml
            with open(yml) as fh:
                raw = yaml.safe_load(fh) or {}
            has_hazards = isinstance(raw.get("hazards"), dict)
            log(
                f"  FIM: running {site} after forecast "
                f"(cycle={cycle}, 90m, chain={chain}, "
                f"{'P+F' if has_hazards else 'ensemble'}) …"
            )
            log(f"       products → {products_root}")
            if master_log:
                master_log.info(
                    "FIM start %s cycle=%s chain=%s out=%s",
                    site, cycle, chain, products_root)

            if has_hazards:
                from .pipeline_pf import load_pf_config, run_pf_cycle
                cfg = load_pf_config(yml, root=root)
                cfg["products_root"] = products_root
                cfg["append_cycle"] = False
                _apply_chain_templates(cfg, chain)
                log(f"       member template: {cfg.get('member', {}).get('template')}")
                summary = run_pf_cycle(cfg, cycle=cycle, verbose=verbose)
            else:
                from .pipeline_ensemble import load_ensemble_config, run_ensemble_cycle
                cfg = load_ensemble_config(yml, root=root)
                cfg["products_root"] = products_root
                cfg["append_cycle"] = False
                _apply_chain_templates(cfg, chain)
                log(f"       member template: {cfg.get('member', {}).get('template')}")
                summary = run_ensemble_cycle(cfg, cycle=cycle, verbose=verbose)

            status = summary.get("status", "?")
            summary["chain"] = chain
            summary["products_root"] = products_root
            if status == "no_runs":
                outputs_root = cfg.get("outputs_root", "outputs")
                if not os.path.isabs(outputs_root):
                    outputs_root = os.path.join(root, outputs_root)
                detail = _explain_no_runs(
                    outputs_root,
                    (cfg.get("member") or {}).get("template", ""),
                    cycle,
                    chain,
                )
                summary["detail"] = detail
                log(f"  FIM: {site} [{chain}] → no_runs")
                for line in detail.splitlines():
                    log(f"  FIM: {line}")
            else:
                log(f"  FIM: {site} [{chain}] → {status}")
            if master_log:
                master_log.info(
                    "FIM done %s chain=%s status=%s", site, chain, status)
            summaries.append(summary)
        except Exception as exc:
            msg = f"  FIM: {site} failed (non-fatal): {exc}"
            log(msg)
            if master_log:
                master_log.error("FIM failed %s: %s", site, exc)
            summaries.append({
                "config": yml, "cycle": cycle, "status": "error",
                "chain": chain, "error": str(exc),
            })

    return summaries

"""
fim_utils : scenario-library flood inundation mapping (FIM) for TITO.

Pre-simulated flood maps (RainyDay storms run through the hydraulic model)
are used as a lookup table. In real time, EF5 maximum unit streamflow
triggers the procedure, the observed + forecast rainfall total is matched
to the closest simulated storm, and that storm's max depth and max extent
are displayed for the area of concern.

This follows the "pre-simulated scenarios + real-time hydrological
simulation" approach of Speight et al. (2018, J Flood Risk Management,
doi:10.1111/jfr3.12281) and the offline forecast databases of
Bhola et al. (2018, Geosciences 8:346), with EF5 playing the role that
Grid-to-Grid played in the Glasgow pilot.

Design rules:
- agnostic to the number of QPE/QPF sources and ensemble members: every
  EF5 run folder found for a cycle is one member
- agnostic to cycle frequency: cycles are discovered from folder names
- file-based: inputs are the EF5 output grids already on disk, outputs are
  rasters, CSVs and a JSON decision log
"""

__version__ = "0.4.0"

from .config import FimConfig, load_config
from .aoc import AreaOfConcern, load_aocs
from .ef5_runs import EF5Run, discover_runs, latest_cycle
from .trigger import evaluate_trigger, TriggerReport
from .rainfall import member_totals
from .catalog import Catalog, build_catalog
from .matching import MatchRules, match_total, match_members, select_scenarios
from .pipeline import run_fim_cycle
from .tito_hook import run_fim_for_cycle, discover_fim_configs

__all__ = [
    "FimConfig", "load_config",
    "AreaOfConcern", "load_aocs",
    "EF5Run", "discover_runs", "latest_cycle",
    "evaluate_trigger", "TriggerReport",
    "member_totals",
    "Catalog", "build_catalog",
    "MatchRules", "match_total", "match_members", "select_scenarios",
    "run_fim_cycle",
    "run_fim_for_cycle", "discover_fim_configs",
    # v0.2 ensemble/zarr API (lazy: needs zarr only when used)
    "FimStore", "build_store", "run_ensemble_cycle", "load_ensemble_config",
    # v0.4 pluvial + fluvial hazard API
    "run_pf_cycle", "load_pf_config", "FluvialMatcher", "attach_fluvial_index",
    "match_pluvial", "combined_depth",
]

# Lazy imports resolved RELATIVE to this package, so fim_utils works no
# matter where it lives (tito_utils.fim_utils, some_other_pkg.fim_utils, ...).
_LAZY = {
    "FimStore": (".store", "FimStore"),
    "run_pf_cycle": (".pipeline_pf", "run_pf_cycle"),
    "load_pf_config": (".pipeline_pf", "load_pf_config"),
    "FluvialMatcher": (".fluvial", "FluvialMatcher"),
    "attach_fluvial_index": (".fluvial", "attach_fluvial_index"),
    "match_pluvial": (".pluvial", "match_pluvial"),
    "combined_depth": (".combine", "combined_depth"),
    "build_store": (".store", "build_store"),
    "attach_magnitudes": (".store", "attach_magnitudes"),
    "run_ensemble_cycle": (".pipeline_ensemble", "run_ensemble_cycle"),
    "load_ensemble_config": (".pipeline_ensemble", "load_ensemble_config"),
}


def __getattr__(name):
    if name in _LAZY:
        import importlib
        module, attr = _LAZY[name]
        return getattr(importlib.import_module(module, __package__), attr)
    raise AttributeError(name)

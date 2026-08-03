"""
Precipitation manager facade for TITO.

Owns download / generation of all QPE and QPF products used by a cycle:
  QPE: IMERG, HSAF, SCaMPR, STREAM-Sat (ensemble)
  QPF: GFS, AROME, WRF (where configured)

The orchestrator should call :func:`prepare_cycle_precip` and nothing else
for precipitation.  Source-specific retrieve modules stay under
``tito_utils.qpe_utils`` / ``tito_utils.qpf_utils``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Sequence

from tito_utils.file_utils.prepare_precip import prepare_all_precip


def prepare_cycle_precip(
    regions_to_run: Sequence[str],
    region_cycle_times: Mapping[str, datetime],
    region_qpe_sources: Mapping[str, str],
    region_qpf_requested: Mapping[str, Sequence[str]],
    config: Any,
    *,
    master_log: Optional[Any] = None,
):
    """
    Download / generate all precipitation for one cycle.

    Returns the shared precip handle from ``prepare_all_precip``
    (imerg folders, scampr folder, streamsat info, gfs/arome caches).
    """
    return prepare_all_precip(
        list(regions_to_run),
        dict(region_cycle_times),
        dict(region_qpe_sources),
        {k: list(v) for k, v in region_qpf_requested.items()},
        config,
        master_log=master_log,
    )


def summarize_shared_precip(shared) -> Dict[str, Any]:
    """Small dict summary for logging / tests (no heavy I/O)."""
    return {
        "imerg_cycles": sorted(getattr(shared, "imerg_folders", {}) or {}),
        "has_scampr": bool(getattr(shared, "scampr_folder", None)),
        "scampr_folder": getattr(shared, "scampr_folder", None),
        "streamsat_regions": sorted(getattr(shared, "streamsat_info", {}) or {}),
        "stormlab_regions": sorted(getattr(shared, "stormlab_info", {}) or {}),
        "gfs_cycles": sorted(getattr(shared, "gfs_cache", {}) or {}),
        "arome_keys": [
            f"{ck}/{dom}" for ck, dom in (getattr(shared, "arome_cache", {}) or {})
        ],
    }

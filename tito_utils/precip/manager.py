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

import os
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

    When ``TITO_OFFLINE=1`` (training flag from ``--offline``), never hits the
    network: stages local archive / EF5_conf precip and returns SharedPrecip.
    """
    if os.environ.get("TITO_OFFLINE", "").strip() in ("1", "true", "TRUE", "yes"):
        # Hard guard — works even if sitecustomize / monkey-patch did not load
        # (e.g. Apptainer --cleanenv before launcher fix).
        try:
            from offline.stage_offline_precips import install_offline_hook
            # install patches this function; call the patched path once
            install_offline_hook(config)
            import tito_utils.precip.manager as _self
            if _self.prepare_cycle_precip is not prepare_cycle_precip:
                return _self.prepare_cycle_precip(
                    regions_to_run,
                    region_cycle_times,
                    region_qpe_sources,
                    region_qpf_requested,
                    config,
                    master_log=master_log,
                )
            # install failed to replace us — build shared directly
            from offline.stage_offline_precips import (
                _stage_from_ef5_layout,
                build_shared_precip,
                stage,
            )
            from pathlib import Path
            root = Path(__file__).resolve().parents[2]
            src = Path(os.environ.get(
                "TITO_OFFLINE_PRECIP", str(root / "offline_precips")))
            # All region cycles must be in the training offline window
            from offline.stage_offline_precips import validate_offline_cycle
            for _r, _ct in region_cycle_times.items():
                validate_offline_cycle(_ct.strftime("%Y%m%d%H%M"), src)
            ct0 = next(iter(region_cycle_times.values()))
            ck = ct0.strftime("%Y%m%d%H%M")
            ss = int(getattr(config, "stream_sat_ensemble_size", 10))
            sl = int(getattr(config, "stormlab_ensemble_size", 5))
            print("==== TITO OFFLINE precip (hard guard, no downloads) ====")
            print(f"  cycle: {ck} (training archive only)")
            if src.is_dir() and any(src.iterdir()):
                stage(project_root=root, source=src, ss_ens=ss, sl_ens=sl,
                      cycle_key=ck)
            else:
                _stage_from_ef5_layout(
                    root, ss_ens=ss, sl_ens=sl, cycle_key=ck)
            return build_shared_precip(
                config, list(regions_to_run), dict(region_cycle_times),
                dict(region_qpe_sources),
                {k: list(v) for k, v in region_qpf_requested.items()},
            )
        except Exception as exc:
            print(f"    ERROR: TITO_OFFLINE=1 but offline staging failed: {exc}")
            print("    Refusing to download while offline. Fix offline_precips/ or unset TITO_OFFLINE.")
            raise

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

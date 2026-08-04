"""Build per-region timing dicts used by EF5 job builders."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any, Dict, Mapping, Sequence

from tito_utils.cycle.timeline import IMERG_LATENCY, build_cycle_plan
from tito_utils.ef5.jobs.helpers import (
    region_path_key,
    resolve_control_template,
    resolve_region_resolution,
)


def build_region_configs(
    regions_to_run: Sequence[str],
    region_cycle_times: Mapping[str, Any],
    region_qpe_sources: Mapping[str, str],
    region_qpf_requested: Mapping[str, Sequence[str]],
    *,
    config: Any,
    hindcast_mode: bool,
    lr_run: bool,
    states_path: str,
    data_path: str,
    qpf_store_path: str,
    template_path: str,
    default_template: str,
) -> Dict[str, dict]:
    """
    Build the ``region_configs`` dict previously inline in orchestrator.

    Also attaches a ``cycle_plan`` (:class:`CyclePlan`) per region so
    downstream code / tests can inspect timing contracts.

    States/outputs use ``{region_lower}_{resolution}`` (e.g. ``guatemala_900m``).
    QPF store stays region-only (forcing is resolution-independent).
    """
    region_template_map = getattr(config, "region_template_map", {})
    model_resolution = getattr(config, "model_resolution", "90m")
    region_resolution_map = getattr(config, "region_resolution_map", {})
    warmup_days = int(getattr(config, "warmup_days", 5))
    mode = "hindcast" if hindcast_mode else "operational"
    lr_duration = timedelta(hours=24) if lr_run else timedelta(0)

    region_configs: Dict[str, dict] = {}
    for region in regions_to_run:
        region_slug = region.lower()
        r_res = resolve_region_resolution(
            region, model_resolution, region_resolution_map)
        rkey = region_path_key(region, r_res)
        ct = region_cycle_times[region]
        qpe = region_qpe_sources[region]
        qpf_list = list(region_qpf_requested[region]) if lr_run else []

        plan = build_cycle_plan(
            ct,
            mode=mode,
            qpe_source=qpe,
            qpf_sources=qpf_list or ["GFS"],
            run_lr=lr_run,
            warmup_days=warmup_days,
            imerg_latency=IMERG_LATENCY,
        )

        # Legacy keys: match prior orchestrator exactly.
        # STREAM_SAT Phase A/B use data-driven ss_end from GeoTIFFs, not these.
        imerg_offset = IMERG_LATENCY if qpe == "IMERG" else timedelta(0)
        if qpe == "IMERG":
            r_imerg_end = ct - IMERG_LATENCY
        else:
            r_imerg_end = ct
        r_state_end = ct - imerg_offset
        r_warm_end = ct - imerg_offset

        r_start_lr = ct
        r_end_lr = r_start_lr + lr_duration
        # Dry-run tail after final forecast/StormLab window (no precip, no state save)
        dry_h = int(getattr(config, "dry_run_hours", 6) or 0)
        if dry_h < 0:
            dry_h = 0
        r_end_time = r_end_lr + timedelta(hours=dry_h) if (lr_run or dry_h) else ct
        r_sys_start = r_warm_end - timedelta(minutes=30)
        r_fail_time = r_warm_end - timedelta(days=7)
        output_ts = ct.strftime("%Y%m%d.%H%M%S")

        tmpl = resolve_control_template(
            template_path,
            region,
            r_res,
            region_template_map=region_template_map,
            default_template=default_template,
        )

        region_configs[region] = {
            "region_key":           rkey,
            "region_slug":          region_slug,
            "model_resolution":     r_res,
            "region_current_time":  ct,
            "output_timestamp_str": output_ts,
            "qpe_source":           qpe,
            "qpf_sources":          qpf_list,
            "region_template":      tmpl,
            "r_start_lr":           r_start_lr,
            "r_end_lr":             r_end_lr,
            "r_end_time":           r_end_time,  # r_end_lr + dry_run_hours (EF5 TIME_END)
            "dry_run_hours":        dry_h,
            "r_state_end":          r_state_end,
            "r_warm_end":           r_warm_end,
            "r_system_start":       r_sys_start,
            "r_fail_time":          r_fail_time,
            "r_imerg_end":          r_imerg_end,
            "r_scampr_end":         ct,
            "cycle_time_key":       ct.strftime("%Y%m%d%H%M"),
            "region_states_path":   os.path.join(states_path, rkey),
            "region_data_path":     os.path.join(data_path, rkey),
            "region_qpf_store":     os.path.join(qpf_store_path, region_slug, ""),
            "cycle_plan":           plan,
        }

    return region_configs

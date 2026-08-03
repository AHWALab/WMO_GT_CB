"""Shared helpers for EF5 job builders."""

from __future__ import annotations

import glob
import os
import re
import shutil
from datetime import datetime, timedelta
from typing import List, Optional, Tuple


def with_sep(path: str) -> str:
    return os.path.join(path, "")


def copy_tifs_from_shared(shared_folder: str, dest_folder: str) -> None:
    from tito_utils.file_utils.file_handling import mkdir_p

    mkdir_p(dest_folder)
    for src in glob.glob(os.path.join(shared_folder, "*.tif")):
        try:
            shutil.copy2(src, dest_folder)
        except Exception as exc:
            print(f"    Warning: copy {os.path.basename(src)}: {exc}")


def resolve_cold_start_window(config, sim_end: datetime) -> Tuple[datetime, datetime]:
    """Return ``(cold_start_begin, cold_start_warm_end)`` for IMERG-only EF5."""
    imerg_post = timedelta(hours=2)
    imerg_warmup = timedelta(hours=6)
    if hasattr(config, "imerg_post_warmup_duration"):
        raw = config.imerg_post_warmup_duration
        if isinstance(raw, timedelta):
            imerg_post = raw
    if hasattr(config, "imerg_cold_start_warmup"):
        raw = config.imerg_cold_start_warmup
        if isinstance(raw, timedelta):
            imerg_warmup = raw
    warm_end = sim_end - imerg_post
    begin = warm_end - imerg_warmup
    return begin, warm_end


def parse_streamsat_tif_window(
    ens_p1_dir: str,
    tif_pattern: str = "streamsat",
) -> Optional[Tuple[datetime, datetime, List[datetime]]]:
    """
    Parse STREAM-Sat GeoTIFF timestamps from ``ensP1``.

    Returns ``(ss_start, ss_end, all_timestamps)`` or ``None`` if none found.
    """
    tif_files = sorted(
        glob.glob(os.path.join(ens_p1_dir, f"{tif_pattern}.qpe.*.mmhInst.tif"))
    )
    if not tif_files:
        return None

    timestamps: List[datetime] = []
    for tf in tif_files:
        m = re.search(r"qpe\.(\d{12})\.", os.path.basename(tf))
        if m:
            try:
                timestamps.append(datetime.strptime(m.group(1), "%Y%m%d%H%M"))
            except ValueError:
                pass
    if not timestamps:
        return None
    return min(timestamps), max(timestamps), timestamps


def resolve_region_resolution(
    region_name: str,
    model_resolution: str,
    region_resolution_map,
) -> str:
    if isinstance(region_resolution_map, dict):
        return region_resolution_map.get(region_name, model_resolution)
    return model_resolution

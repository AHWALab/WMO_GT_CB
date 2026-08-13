"""Products: put the selected max depth and max extent on disk, with a log.

For each triggered AOC and each selector (best, upper, ...):
    {outputs_root}/{Region}/fim/{cycle}/
        {aoc}_{selector}_{storm}_depth.tif
        {aoc}_{selector}_{storm}_extent.tif
        member_totals.csv        every (AOC, member) rainfall total
        matches.csv              every per-member match decision
        fim_summary.json         trigger report, selections, rules, files
Only max depth and max extent are displayed, as agreed.
"""

import json
import os
import shutil
from datetime import datetime, timezone

import pandas as pd


def export_products(out_dir: str, cycle: str, region: str,
                    trigger_report, totals, decisions, selections,
                    catalog, clip_aocs=None, config_echo: dict = None) -> dict:
    """Write rasters, tables and the decision log. Returns the summary dict."""
    os.makedirs(out_dir, exist_ok=True)

    written = []
    for aoc_id, named in selections.items():
        if aoc_id not in trigger_report.triggered_aocs:
            continue
        for selector, decision in named.items():
            files = catalog.storm_files(decision.storm_id)
            for role in ("depth", "extent"):
                src = files.get(role, "")
                if not src or not os.path.isfile(src):
                    continue
                dst = os.path.join(
                    out_dir, f"{_safe(aoc_id)}_{selector}_{decision.storm_id}_{role}.tif")
                _deliver(src, dst, clip_aocs.get(aoc_id) if clip_aocs else None)
                written.append(os.path.basename(dst))

    pd.DataFrame([t.to_dict() for t in totals]).to_csv(
        os.path.join(out_dir, "member_totals.csv"), index=False)
    pd.DataFrame([d.to_dict() for d in decisions]).to_csv(
        os.path.join(out_dir, "matches.csv"), index=False)

    summary = {
        "region": region,
        "cycle": cycle,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "trigger": trigger_report.to_dict(),
        "selections": {
            aoc: {sel: d.to_dict() for sel, d in named.items()}
            for aoc, named in selections.items()
        },
        "files_written": written,
        "config": config_echo or {},
        "catalog_meta": catalog.meta or {},
    }
    with open(os.path.join(out_dir, "fim_summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    return summary


def _safe(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(text))


def _deliver(src: str, dst: str, aoc=None):
    """Copy the raster, clipped to the AOC when requested."""
    if aoc is None:
        if os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        return
    import rasterio
    from rasterio.mask import mask as rio_mask
    with rasterio.open(src) as srcds:
        geoms = aoc.geoms
        if not geoms:
            shutil.copy2(src, dst)
            return
        try:
            data, transform = rio_mask(srcds, geoms, crop=True)
        except ValueError:
            # AOC does not overlap this raster; fall back to a straight copy
            shutil.copy2(src, dst)
            return
        profile = srcds.profile.copy()
        profile.update(height=data.shape[1], width=data.shape[2],
                       transform=transform, compress="lzw")
        with rasterio.open(dst, "w", **profile) as dstds:
            dstds.write(data)

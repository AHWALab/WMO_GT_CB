"""
imerg_gap_fill.py
=================
Utilities that fill the IMERG 4-hour latency gap with higher-frequency
IR-based QPE (SCaMPR or HSAF) by converting 10-min instantaneous rainfall
rates to 30-min IMERG-compatible accumulation GeoTIFFs.

These functions are used in hindcast QPE experiments:
    IMERG_SCAMPR  — gap filled using SCaMPR (global NOAA RRQPE product)
    IMERG_HSAF    — gap filled using HSAF H40B (Indian Ocean / Africa region)

Outputs are written with IMERG naming convention so that the downstream
rename_ef5_precip() staging step picks them up alongside real IMERG files:
    imerg.qpe.YYYYMMDDHHMM.30minAccum.tif

Unit conversion:  mean(mm/h instantaneous) × 0.5 h  →  mm / 30-min accumulation
"""

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

try:
    import rasterio
    _RASTERIO_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RASTERIO_AVAILABLE = False

# Output filename template — matches the IMERG EF5 forcing NAME pattern.
_IMERG_NAME_TPL = "imerg.qpe.{ts}.30minAccum.tif"   # ts = YYYYMMDDHHMM


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_rasterio() -> None:
    if not _RASTERIO_AVAILABLE:
        raise ImportError(
            "rasterio is required for IMERG gap fill. "
            "Install with: pip install rasterio"
        )


def _parse_scampr_ts(filename: str) -> datetime | None:
    """Extract UTC timestamp from a SCaMPR EF5 filename.

    Expected pattern: scampr.qpe.YYYYMMDDHHMM.mmhInst.tif
    """
    m = re.match(r"scampr\.qpe\.(\d{12})\.mmhInst\.tif$", filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M")
    except ValueError:
        return None


def _parse_hsaf_ts(filename: str) -> datetime | None:
    """Extract UTC timestamp from a HSAF H40B TIF filename.

    Expected pattern: h40_YYYYMMDD_HHMM_fdk.tif
    """
    m = re.match(r"h40_(\d{8})_(\d{4})_fdk\.tif$", filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
    except ValueError:
        return None


def _average_rasters_to_accum(tif_paths: list, output_path: str, accum_hours: float = 0.5) -> bool:
    """Average a list of mm/h instantaneous GeoTIFFs and write as mm accumulation.

    Parameters
    ----------
    tif_paths    : list of file paths (mm/h instantaneous rate GeoTIFFs)
    output_path  : destination path for the output accumulation GeoTIFF
    accum_hours  : accumulation window duration in hours (default 0.5 = 30 min)

    Returns True on success, False if no valid data could be read.
    """
    _require_rasterio()
    if not tif_paths:
        return False

    arrays = []
    profile = None

    for path in tif_paths:
        try:
            with rasterio.open(path) as src:
                if profile is None:
                    profile = src.profile.copy()
                data = src.read(1).astype("float32")
                nodata = src.nodata
                if nodata is not None:
                    data[data == nodata] = np.nan
                data[data < 0] = np.nan
                arrays.append(data)
        except Exception as exc:
            print(f"    Warning: could not read raster {path}: {exc}")

    if not arrays:
        return False

    stacked = np.stack(arrays, axis=0)
    mean_rate = np.nanmean(stacked, axis=0)   # mm/h average
    accum = mean_rate * accum_hours            # mm over accum_hours

    fill_value = -9999.0
    accum = np.where(np.isnan(accum), fill_value, accum)

    profile.update(
        dtype="float32",
        nodata=fill_value,
        count=1,
        compress="deflate",
    )

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(accum.astype("float32"), 1)

    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fill_imerg_gap_with_scampr(
    imerg_precip_folder: str,
    scampr_precip_folder: str,
    gap_start: datetime,
    gap_end: datetime,
) -> int:
    """Aggregate SCaMPR 10-min inst TIFs into 30-min IMERG accumulation TIFs.

    Scans *scampr_precip_folder* for files named ``scampr.qpe.YYYYMMDDHHMM.mmhInst.tif``
    that fall within [gap_start, gap_end) and groups them into consecutive 30-min
    windows aligned to [gap_start, gap_start+30m), [gap_start+30m, gap_start+60m), …

    For each window the mean instantaneous rate is converted to a 30-min accumulation
    (mm) and written to *imerg_precip_folder* as ``imerg.qpe.{window_end}.30minAccum.tif``.
    Already-existing IMERG files for a window are never overwritten (real IMERG takes
    precedence over the gap-fill product).

    Parameters
    ----------
    imerg_precip_folder  : destination folder — same folder that holds real IMERG files.
    scampr_precip_folder : folder containing ``scampr.qpe.*.mmhInst.tif`` TIFs (the
                           per-region SCaMPR precip folder, not the _scampr_raw subfolder).
    gap_start            : start of the IMERG latency gap (inclusive), typically cycle_time − 4 h.
    gap_end              : end of the gap (exclusive), typically cycle_time.

    Returns
    -------
    int — number of 30-min windows successfully written.
    """
    _require_rasterio()

    # Normalize to naive UTC so comparisons with datetime.strptime() never raise
    # "can't compare offset-naive and offset-aware datetimes".
    if getattr(gap_start, "tzinfo", None) is not None:
        gap_start = gap_start.replace(tzinfo=None)
    if getattr(gap_end, "tzinfo", None) is not None:
        gap_end = gap_end.replace(tzinfo=None)

    scampr_dir = Path(scampr_precip_folder)
    scampr_by_ts: dict[datetime, str] = {}
    for tif_file in scampr_dir.glob("scampr.qpe.*.mmhInst.tif"):
        ts = _parse_scampr_ts(tif_file.name)
        if ts is not None and gap_start <= ts < gap_end:
            scampr_by_ts[ts] = str(tif_file)

    if not scampr_by_ts:
        print(f"    Gap fill: no SCaMPR files found in [{gap_start}, {gap_end})")
        return 0

    print(f"    Gap fill: found {len(scampr_by_ts)} SCaMPR file(s) for "
          f"[{gap_start.strftime('%Y-%m-%d %H:%M')}, {gap_end.strftime('%H:%M')}) UTC")

    written = 0
    window_start = gap_start
    while window_start < gap_end:
        window_end = window_start + timedelta(minutes=30)

        window_files = [
            scampr_by_ts[ts]
            for ts in scampr_by_ts
            if window_start <= ts < window_end
        ]

        if window_files:
            out_ts = window_end.strftime("%Y%m%d%H%M")
            out_name = _IMERG_NAME_TPL.format(ts=out_ts)
            out_path = os.path.join(imerg_precip_folder, out_name)

            # Do not overwrite existing real IMERG files.
            if not os.path.isfile(out_path):
                success = _average_rasters_to_accum(window_files, out_path, accum_hours=0.5)
                if success:
                    written += 1
                    print(f"    SCaMPR→IMERG gap fill: {out_name} ({len(window_files)} source file(s))")

        window_start = window_end

    return written


def fill_imerg_gap_with_hsaf(
    imerg_precip_folder: str,
    hsaf_precip_folder: str,
    gap_start: datetime,
    gap_end: datetime,
) -> int:
    """Aggregate HSAF H40B 10-min inst TIFs into 30-min IMERG accumulation TIFs.

    HSAF TIFs are expected in *hsaf_precip_folder/_hsaf_raw/* and named
    ``h40_YYYYMMDD_HHMM_fdk.tif``.  The function falls back to searching
    *hsaf_precip_folder* directly if the ``_hsaf_raw`` subfolder is absent.

    Output naming and semantics are identical to :func:`fill_imerg_gap_with_scampr`.

    Parameters
    ----------
    imerg_precip_folder : destination folder — same folder that holds real IMERG files.
    hsaf_precip_folder  : root of the HSAF precip folder for this region  (the
                          ``_hsaf_raw`` subfolder is searched automatically).
    gap_start           : start of the IMERG latency gap (inclusive).
    gap_end             : end of the gap (exclusive).

    Returns
    -------
    int — number of 30-min windows successfully written.
    """
    _require_rasterio()

    # Normalize to naive UTC so comparisons with datetime.strptime() never raise
    # "can't compare offset-naive and offset-aware datetimes".
    if getattr(gap_start, "tzinfo", None) is not None:
        gap_start = gap_start.replace(tzinfo=None)
    if getattr(gap_end, "tzinfo", None) is not None:
        gap_end = gap_end.replace(tzinfo=None)

    hsaf_search_dir = Path(hsaf_precip_folder) / "_hsaf_raw"
    if not hsaf_search_dir.is_dir():
        hsaf_search_dir = Path(hsaf_precip_folder)

    hsaf_by_ts: dict[datetime, str] = {}
    for tif_file in hsaf_search_dir.glob("h40_*_fdk.tif"):
        ts = _parse_hsaf_ts(tif_file.name)
        if ts is not None and gap_start <= ts < gap_end:
            hsaf_by_ts[ts] = str(tif_file)

    if not hsaf_by_ts:
        print(f"    Gap fill: no HSAF files found in [{gap_start}, {gap_end})")
        return 0

    print(f"    Gap fill: found {len(hsaf_by_ts)} HSAF file(s) for "
          f"[{gap_start.strftime('%Y-%m-%d %H:%M')}, {gap_end.strftime('%H:%M')}) UTC")

    written = 0
    window_start = gap_start
    while window_start < gap_end:
        window_end = window_start + timedelta(minutes=30)

        window_files = [
            hsaf_by_ts[ts]
            for ts in hsaf_by_ts
            if window_start <= ts < window_end
        ]

        if window_files:
            out_ts = window_end.strftime("%Y%m%d%H%M")
            out_name = _IMERG_NAME_TPL.format(ts=out_ts)
            out_path = os.path.join(imerg_precip_folder, out_name)

            if not os.path.isfile(out_path):
                success = _average_rasters_to_accum(window_files, out_path, accum_hours=0.5)
                if success:
                    written += 1
                    print(f"    HSAF→IMERG gap fill: {out_name} ({len(window_files)} source file(s))")

        window_start = window_end

    return written

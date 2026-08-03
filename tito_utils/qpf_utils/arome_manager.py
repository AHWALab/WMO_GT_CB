"""AROME QPF region-store manager for TITO.

Mirrors gfs_manager.py in architecture:
- A background/shared AROME cache stores downloaded GeoTIFFs.
- AROME_searcher populates the per-region qpf_store/<region>/arome_data/ folder
  by copying from the cache when available, or triggering a fresh download.

File naming: arome.YYYYMMDDHH00.tif  (valid time, UTC, 1 h cadence)
"""

import glob
import os
import shutil
from datetime import datetime, timedelta
from typing import Union

from .arome_downloader import download_AROME


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expected_arome_filenames(start_time: datetime, end_time: datetime) -> set:
    """Return the set of arome tif basenames expected for start_time..end_time (hourly)."""
    names: set = set()
    t = start_time.replace(minute=0, second=0, microsecond=0)
    while t <= end_time:
        names.add(f"arome.{t.strftime('%Y%m%d%H')}00.tif")
        t += timedelta(hours=1)
    return names


def _clear_arome_data_folder(folder: str) -> None:
    """Remove all .tif files from an arome_data working folder."""
    for f in glob.glob(os.path.join(folder, "*.tif")):
        try:
            os.remove(f)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def AROME_searcher(
    path_arome: str,
    qpf_store_path: str,
    start_time: Union[str, datetime],
    end_time: Union[str, datetime],
    xmin: float,
    xmax: float,
    ymin: float,
    ymax: float,
    domain: str,
) -> None:
    """Populate qpf_store_path/arome_data/ with AROME tifs for start_time..end_time.

    Architecture
    ------------
    ``path_arome`` is a shared/daemon-maintained cache folder (analogous to
    ``path_gfs`` for GFS).  AROME_searcher is a *consumer* of that folder and
    never writes back to it.

    Strategy
    --------
    1. Clear qpf_store_path/arome_data/ (stale files from the previous run).
    2. If all expected hourly tifs are already found in ``path_arome``:
       copy them into arome_data/ and return.
    3. Otherwise: download fresh data from Météo-France into arome_data/
       directly (the cache is NOT updated — the daemon owns that path).

    Parameters
    ----------
    path_arome : str
        Shared folder maintained by the background AROME cache/daemon.
    qpf_store_path : str
        Per-region EF5 working folder; tifs land in qpf_store_path/arome_data/.
    start_time, end_time : str or datetime
        Forecast window to cover (UTC).
    xmin / xmax / ymin / ymax : float
        Spatial clipping bbox.
    domain : str
        AROME domain: ``"ANTIL"`` (Caribbean) or ``"INDIEN"`` (Indian Ocean).
    """
    if isinstance(start_time, str):
        from .arome_downloader import _ensure_datetime
        start_time = _ensure_datetime(start_time)
    if isinstance(end_time, str):
        from .arome_downloader import _ensure_datetime
        end_time = _ensure_datetime(end_time)

    download_folder = os.path.join(qpf_store_path, "arome_data")
    os.makedirs(download_folder, exist_ok=True)
    os.makedirs(path_arome, exist_ok=True)

    # Step 1: clear stale tifs from the per-region EF5 folder
    _clear_arome_data_folder(download_folder)

    # Step 2: check the shared cache
    expected = _expected_arome_filenames(start_time, end_time)
    cached_files = {
        os.path.basename(f)
        for f in glob.glob(os.path.join(path_arome, "*.tif"))
    }
    missing = expected - cached_files

    if not missing:
        print(
            f"    AROME: all {len(expected)} file(s) found in shared cache "
            "— copying to region store."
        )
        for name in sorted(expected):
            src = os.path.join(path_arome, name)
            dst = os.path.join(download_folder, name)
            try:
                shutil.copy2(src, dst)
            except Exception as exc:
                print(f"    Warning: could not copy AROME file {name}: {exc}")
        return

    # Step 3: cache incomplete — download directly into region download_folder
    print(
        f"    AROME: {len(missing)} of {len(expected)} file(s) missing from "
        "shared cache — downloading directly."
    )
    result = download_AROME(
        start_time, end_time, xmin, xmax, ymin, ymax, download_folder, domain
    )
    num_written = len(result) if result else 0
    print(f"    AROME: download complete — {num_written} file(s) written.")

    if num_written == 0:
        raise RuntimeError(
            f"No AROME data produced for domain={domain}, "
            f"window=[{start_time}, {end_time}] after download attempts."
        )

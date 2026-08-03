import os
import shutil
from datetime import datetime as dt
from datetime import timedelta
from .gfs_downloader_v2 import download_cycle, _gfs_cycle, PARALLEL_WORKERS, MAX_CYCLES_BACK
from .gfs_wind_downloader import (
    assemble_winds_from_archive,
    download_wind_cycle,
)
import glob


def _expected_gfs_filenames(start_time, end_time):
    """Return the set of GFS tif basenames expected to cover start_time..end_time.

    GFS files are named gfs.YYYYMMDDHH00.tif (valid hour, one per hour).
    """
    names = set()
    t = start_time.replace(minute=0, second=0, microsecond=0)
    while t <= end_time:
        names.add(f"gfs.{t.strftime('%Y%m%d%H')}00.tif")
        t += timedelta(hours=1)
    return names


def _clear_gfs_data_folder(download_folder):
    """Remove all tif files from the EF5 working gfs_data folder."""
    for f in glob.glob(os.path.join(download_folder, "*.tif")):
        try:
            os.remove(f)
        except Exception:
            pass


def GFS_searcher(path_gfs, qpf_store_path, start_time, end_time, xmin, xmax, ymin, ymax):
    """Populate qpf_store_path/gfs_data/ with GFS tifs covering start_time..end_time.

    Architecture
    ------------
    The background gfs_downloader daemon continuously writes the latest GFS cycle
    files into the shared folder *path_gfs* (e.g. ``qpf_store/GFS/``).
    GFS_searcher is a *read-only consumer* of that folder — it never writes back to it.

    Strategy
    --------
    1. Always clear qpf_store_path/gfs_data/ (stale files from the previous run).
    2. Check path_gfs for all expected hourly tifs (gfs.YYYYMMDDHH00.tif):
       - **All present** → copy to gfs_data/ and return.  No network access.
       - **Any missing** → one-shot download via ``gfs_downloader_v2.download_cycle``
         into gfs_data/.  Not archived back to path_gfs; the daemon owns that folder.

    Parameters
    ----------
    path_gfs : str
        Shared folder maintained by the background gfs_downloader daemon.
    qpf_store_path : str
        Per-region EF5 working folder; tifs land in qpf_store_path/gfs_data/.
    start_time, end_time : datetime
        Forecast window to cover.
    xmin, xmax, ymin, ymax : float
        Spatial clipping bbox.
    """
    download_folder = os.path.join(qpf_store_path, "gfs_data/")
    os.makedirs(download_folder, exist_ok=True)
    os.makedirs(path_gfs, exist_ok=True)

    # Step 1: always clear the EF5 working folder before populating it.
    _clear_gfs_data_folder(download_folder)

    # Step 2: check the daemon-maintained shared folder.
    expected = _expected_gfs_filenames(start_time, end_time)
    daemon_files = {os.path.basename(f) for f in glob.glob(os.path.join(path_gfs, "*.tif"))}
    missing = expected - daemon_files

    if not missing:
        print(f"    GFS: all {len(expected)} file(s) found in shared folder — copying to region store.")
        for name in sorted(expected):
            src = os.path.join(path_gfs, name)
            dst = os.path.join(download_folder, name)
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                print(f"    Warning: could not copy GFS file {name}: {e}")
        return

    # Step 3: daemon folder is incomplete — fallback one-shot download via V2.
    print(f"    GFS: {len(missing)} of {len(expected)} file(s) missing from shared folder "
          f"(daemon may not have run yet) — downloading via V2 parallel downloader.")

    # Compute the GFS cycle and forecast window
    cycle = _gfs_cycle(start_time)
    hours = max(1, int((end_time - cycle).total_seconds() / 3600.0))

    num_written = 0
    for back in range(MAX_CYCLES_BACK + 1):
        trial_cycle = cycle - timedelta(hours=6 * back)
        results = download_cycle(
            trial_cycle, hours,
            xmin, xmax, ymin, ymax,
            download_folder,
            workers=PARALLEL_WORKERS,
        )
        num_written = len(results) if results else 0
        if num_written > 0:
            print(f"    GFS: V2 fallback — cycle {trial_cycle:%Y-%m-%d %H}z wrote {num_written} files.")
            break
        print(f"    GFS: V2 fallback — cycle {trial_cycle:%Y-%m-%d %H}z returned 0 files, "
              f"trying previous cycle...")

    if num_written == 0:
        raise RuntimeError(
            f"No GFS data available after {MAX_CYCLES_BACK + 1} cycle attempts "
            f"via V2 downloader."
        )


# ---------------------------------------------------------------------------
# GFS Wind (U/V 850 hPa) archive-first searcher
# ---------------------------------------------------------------------------

def _expected_wind_filenames(start_time, end_time):
    """Return the set of gfs_wind.*.nc basenames covering start_time..end_time."""
    names = set()
    t = start_time.replace(minute=0, second=0, microsecond=0)
    while t <= end_time:
        names.add(f"gfs_wind.{t:%Y%m%d%H%M}.nc")
        t += timedelta(hours=1)
    return names


def GFS_wind_searcher(archive_dir: str, output_dir: str,
                      start_time, end_time,
                      lat_min: float, lat_max: float,
                      lon_min: float, lon_max: float,
                      *,
                      out_res: float = 0.1,
                      workers: int = PARALLEL_WORKERS) -> str:
    """Obtain a GFS 850 hPa wind NetCDF covering *start_time*..*end_time*.

    Strategy (archive-first, Herbie-fallback):
    1. Check *archive_dir* for per-hour ``gfs_wind.YYYYMMDDHHMM.nc`` files.
       - **All present** → assemble into event NetCDF via
         ``assemble_winds_from_archive`` (zero network usage).
       - **Any missing** → call ``download_wind_cycle`` (Herbie) for the
         missing cycles, saving per-hour files back to the archive, then
         assemble.

    Parameters
    ----------
    archive_dir : str
        Shared archive populated by ``gfs_wind_downloader.py`` daemon.
    output_dir : str
        Working directory; the assembled event NetCDF lands here.
    start_time, end_time : datetime
        Valid-time window to cover.
    lat_min, lat_max, lon_min, lon_max : float
        Domain bounding box in degrees.
    out_res : float
        Output grid resolution (default 0.1°).
    workers : int
        Parallel download threads.

    Returns
    -------
    str
        Path to the assembled event NetCDF in *output_dir*.
    """
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(archive_dir, exist_ok=True)

    out_name = (f"GFS_UV850_{out_res}deg_"
                f"{start_time:%Y%m%d_%H}_{end_time:%Y%m%d_%H}.nc")
    out_path = os.path.join(output_dir, out_name)

    # Already cached?
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 1000:
        print(f"    GFS winds: already present — {out_name}")
        return out_path

    # Step 1: check archive
    expected = _expected_wind_filenames(start_time, end_time)
    archive_files = {os.path.basename(f)
                     for f in glob.glob(os.path.join(archive_dir, "gfs_wind.*.nc"))}
    missing = expected - archive_files

    if not missing:
        print(f"    GFS winds: all {len(expected)} files in archive — assembling.")
        result = assemble_winds_from_archive(archive_dir, start_time, end_time, out_path)
        if result is not None:
            return result
        print(f"    GFS winds: assembly failed, falling back to download.")

    # Step 2: archive incomplete — fail fast, let caller fall back to
    #   download_gfs_winds (which handles multi-cycle windows efficiently).
    raise RuntimeError(
        f"Archive incomplete: {len(missing)} of {len(expected)} wind files "
        f"missing ({start_time} → {end_time}). "
        f"Use download_gfs_winds fallback."
    )
        
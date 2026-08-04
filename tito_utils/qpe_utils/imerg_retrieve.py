"""
================================================================================
IMERG Precipitation Retrieval Module (QPE - Quantitative Precipitation Estimate)
================================================================================

Description:
------------
Downloads NASA GPM IMERG (Integrated Multi-satellitE Retrievals for GPM) 
precipitation data and converts it to EF5-compatible GeoTIFF format. IMERG
provides near-global precipitation estimates at 0.1° resolution every 30 minutes.

Standalone Usage:
-----------------
1. Set environment variables or pass directly:
   - email_gpm: Your NASA Earthdata/GPM registration email (used as both username
     and password for IMERG PPS server authentication)

2. Basic standalone script example:

   from datetime import datetime, timedelta
   from imerg_retrieve import get_new_precip, get_gpm_files
   
   # Define domain bounds
   xmin, ymin, xmax, ymax = -85.0, 10.0, -60.0, 25.0
   
   # Set your NASA GPM email
   email = "your-email@example.com"
   
   # Define time window
   start_time = datetime(2024, 1, 1, 0, 0)
   end_time = datetime(2024, 1, 1, 6, 0)
   
   # Download IMERG data
   get_gpm_files(
       precipFolder="./imerg_output",
       initial_timestamp=start_time,
       final_timestamp=end_time,
       ppt_server_path="https://jsimpsonhttps.pps.eosdis.nasa.gov/imerg/gis/early/",
       email_gpm=email,
       xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax,
       HindCastMode=True  # Set True for historical data retrieval
   )

TITO Integration:
-----------------
TITO (Threading Inputs to Outputs) uses this module to:
1. Retrieve observed precipitation for initialization of hydrologic models
2. Provide 30-minute accumulated precipitation inputs for EF5 hydrologic model

Called by: TITO orchestrator during the precipitation preparation phase
Function: get_new_precip() - Main entry point for TITO
          get_gpm_files()  - Batch download for hindcast mode

Parameters expected from TITO:
  - current_timestamp: datetime for the current forecast cycle
  - ppt_server_path: URL to NASA IMERG server
  - precipFolder: Output directory for processed GeoTIFFs
  - email: NASA GPM authentication email
  - xmin/ymin/xmax/ymax: Domain bounding box in degrees
  - HindCastMode: Boolean for historical data retrieval

Required Packages:
------------------
- requests: HTTP library for downloading from NASA servers
  pip install requests

- beautifulsoup4: HTML parsing for file listing
  pip install beautifulsoup4

- numpy: Array manipulation for raster data
  pip install numpy
  
- GDAL (osgeo.gdal): Geospatial processing and reprojection
  conda install -c conda-forge gdal
  OR on Windows with pre-built wheels:
  pip install GDAL-3.x.x-cp3x-cp3x-win_amd64.whl (from GIS Internals)

- Internal TITO dependencies:
  - tito_utils.file_utils.datetime_utils (extract_timestamp, to_naive_utc)

Data Source:
------------
NASA GPM IMERG Final Run (V07B/V07C)
- Spatial Resolution: 0.1° x 0.1° (approx 10km)
- Temporal Resolution: 30 minutes
- Format: GeoTIFF (converted from HDF5)
- Coverage: 60°N to 60°S
- Latency: ~2-4 hours for Final Run

Authentication:
-------------
Requires free NASA Earthdata account registered with GPM:
https://gpm.nasa.gov/data-access

Output Format:
--------------
GeoTIFF files named: imerg.qpe.YYYYMMDDHHMM.30minAccum.tif
- Projection: EPSG:4326 (WGS84)
- Units: mm per 30-minute accumulation
- NoData value: -9999.0
- Compression: DEFLATE

Notes:
------
- Handles version transitions (V07B to V07C cutoff around March 2026)
- Automatically clips to domain boundaries
- Uses thread-safe processing with unique temp files per region
================================================================================
"""

import requests
from bs4 import BeautifulSoup
import os
import glob
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup
import datetime
from datetime import datetime as dt
from datetime import timedelta
from os import makedirs, listdir, rename, remove
import numpy as np
import osgeo.gdal as gdal
from osgeo.gdal import gdalconst
from osgeo.gdalconst import GA_ReadOnly
from tito_utils.file_utils.datetime_utils import extract_timestamp, extract_datetime_from_filename, to_naive_utc


def retrieve_imerg_files(url, email_gpm, HindCastMode, date):
    """List bare filenames ending in '30min.tif' available for *date*'s YYYY/MM folder."""
    url = url.rstrip('/')
    folder = date.strftime('%Y/%m/')
    url_server = url + '/' + folder

    response = requests.get(url_server, auth=(email_gpm, email_gpm))

    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        links = soup.find_all('a')
        # Guard against hrefs that are None or don't end in the expected suffix.
        files = [
            link.get('href') for link in links
            if link.get('href') and link.get('href').endswith('30min.tif')
        ]
    else:
        print(f"    Failed to retrieve directory listing ({url_server}). Status: {response.status_code}")
        files = []

    return files


# IMERG version cutover: V07B → V07C happened around 2026-03-10.
_IMERG_V07C_CUTOVER = datetime.datetime(2026, 3, 10, tzinfo=datetime.timezone.utc)

def _imerg_file_suffix(date):
    """Return the correct IMERG 30-min TIF version suffix for a given date."""
    d = date if date.tzinfo is not None else date.replace(tzinfo=datetime.timezone.utc)
    return '.V07C.30min.tif' if d >= _IMERG_V07C_CUTOVER else '.V07B.30min.tif'


def _imerg_one_timestep(
    current_date,
    precipFolder,
    server,
    email_gpm,
    xmin, ymin, xmax, ymax,
    available_files,
    file_prefix,
    print_lock=None,
):
    """Download + convert one 30-min IMERG step. Returns status string."""
    initial_time_stmp = current_date.strftime('%Y%m%d-S%H%M%S')
    final_time = current_date + timedelta(minutes=29)
    final_time_stmp = final_time.strftime('E%H%M59')
    final_time_gridout = current_date + timedelta(minutes=30)
    folder = current_date.strftime('%Y/%m/')
    total_minutes = current_date.hour * 60 + current_date.minute
    date_stamp = initial_time_stmp + '-' + final_time_stmp + '.' + f"{total_minutes:04}"
    file_suffix = _imerg_file_suffix(current_date)
    filename = folder + file_prefix + date_stamp + file_suffix

    gridOutName = os.path.join(
        precipFolder,
        'imerg.qpe.' + final_time_gridout.strftime('%Y%m%d%H%M') + '.30minAccum.tif',
    )
    # Skip if already on disk (warmup / re-runs)
    if os.path.isfile(gridOutName) and os.path.getsize(gridOutName) > 0:
        return "exists"

    if filename not in available_files:
        return "missing"

    raw_dir = os.path.join(precipFolder, '_imerg_raw')
    os.makedirs(raw_dir, exist_ok=True)
    # Unique raw name per worker to avoid collisions under parallel download
    local_filename = f"{os.getpid()}_{threading.get_ident()}_{file_prefix}{date_stamp}{file_suffix}"
    local_file_path = os.path.join(raw_dir, local_filename)
    try:
        get_file(filename, server, email_gpm, local_path=local_file_path)
        NewGrid, nx, ny, gt, proj = processIMERG(local_file_path, xmin, ymin, xmax, ymax)
        WriteGrid(gridOutName, NewGrid, nx, ny, gt, proj)
        return "ok"
    except Exception as e:
        try:
            from tito_utils.logging_utils import debug_print, is_debug
            if is_debug():
                msg = f"    ERROR downloading {filename}: {e}"
                if print_lock:
                    with print_lock:
                        debug_print(msg)
                else:
                    debug_print(msg)
        except Exception:
            pass
        return "error"
    finally:
        try:
            if os.path.isfile(local_file_path):
                os.remove(local_file_path)
        except OSError:
            pass


def get_gpm_files(
    precipFolder,
    initial_timestamp,
    final_timestamp,
    ppt_server_path,
    email_gpm,
    xmin, ymin, xmax, ymax,
    HindCastMode=False,
    max_workers=None,
):
    """Batch download IMERG files for a given time range (parallel).

    Args:
        precipFolder: Output directory for processed GeoTIFFs
        initial_timestamp: Start datetime (UTC)
        final_timestamp: End datetime (UTC)
        ppt_server_path: URL to NASA IMERG PPS server
        email_gpm: NASA Earthdata/PPS email (used as both username and password)
        xmin, ymin, xmax, ymax: Domain bounding box in degrees
        HindCastMode: If True, uses historical retrieval mode (default: False)
        max_workers: Parallel download threads (default: IMERG_MAX_WORKERS env
            or min(16, cpu_count*2)).  Use 1 for serial.
    """
    server = ppt_server_path
    file_prefix = '3B-HHR-E.MS.MRG.3IMERG.'

    final_date = final_timestamp + timedelta(minutes=30)
    delta_time = datetime.timedelta(minutes=30)

    from tito_utils.logging_utils import (
        debug_print, is_debug, is_user, progress_done, progress_line, user_print,
    )

    user_print("    Checking server for available files...")
    available_files = _get_available_files_for_range(
        server, email_gpm, initial_timestamp, final_date, HindCastMode)
    user_print(f"    Found {len(available_files)} files available on server")

    # Build list of 30-min timesteps to fetch
    timesteps = []
    current_date = initial_timestamp
    while current_date < final_date:
        timesteps.append(current_date)
        current_date = current_date + delta_time

    if max_workers is None:
        env_w = os.environ.get("IMERG_MAX_WORKERS", "").strip()
        if env_w.isdigit() and int(env_w) > 0:
            max_workers = int(env_w)
        else:
            max_workers = min(16, max(4, (os.cpu_count() or 4) * 2))
    max_workers = max(1, int(max_workers))

    n_total = len(timesteps)
    user_print(f"    IMERG download: {n_total} timesteps, workers={max_workers}")

    downloaded_count = 0
    skipped_count = 0
    exists_count = 0
    error_count = 0
    print_lock = threading.Lock()
    done = 0
    import sys as _sys
    _tty = bool(getattr(_sys.stdout, "isatty", lambda: False)())
    # TTY user mode: rewrite one line continuously.
    # Log files / non-TTY: update ~ every 5% so files stay readable.
    _pct_step = max(1, n_total // 20) if n_total else 1
    _last_logged = [0]

    def _emit_progress(force=False):
        if n_total <= 0:
            return
        msg = (f"    IMERG progress: {done}/{n_total} "
               f"(new={downloaded_count} exist={exists_count} "
               f"skip={skipped_count} err={error_count})")
        if is_user() and _tty:
            progress_line(msg)
            return
        # non-TTY or debug: sparse lines
        if force or done == n_total or (done - _last_logged[0]) >= _pct_step:
            _last_logged[0] = done
            print(msg, flush=True)

    def _work(ts):
        return _imerg_one_timestep(
            ts, precipFolder, server, email_gpm,
            xmin, ymin, xmax, ymax,
            available_files, file_prefix, print_lock,
        )

    if max_workers == 1:
        for ts in timesteps:
            st = _work(ts)
            done += 1
            if st == "ok":
                downloaded_count += 1
            elif st == "exists":
                exists_count += 1
            elif st == "missing":
                skipped_count += 1
            else:
                error_count += 1
            _emit_progress()
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_work, ts): ts for ts in timesteps}
            for fut in as_completed(futs):
                st = fut.result()
                with print_lock:
                    done += 1
                    if st == "ok":
                        downloaded_count += 1
                    elif st == "exists":
                        exists_count += 1
                    elif st == "missing":
                        skipped_count += 1
                    else:
                        error_count += 1
                    _emit_progress()

    final = (f"    IMERG complete: {downloaded_count} new, "
             f"{exists_count} existing, {skipped_count} missing, "
             f"{error_count} errors")
    progress_done(final)


def _get_available_files_for_range(server, email_gpm, start_date, end_date, HindCastMode):
    """Return the set of server-available files for the date range.

    Files are stored as ``YYYY/MM/<basename>`` to match the format that
    ``get_gpm_files`` constructs when building ``filename = folder + prefix + stamp + suffix``.
    The NASA PPS server returns bare filenames in its directory listing, so this
    function explicitly prepends the ``YYYY/MM/`` folder so the set-lookup in
    ``get_gpm_files`` works correctly.
    """
    available_files = set()

    # Collect unique YYYY/MM pairs covered by the requested range.
    current = start_date
    year_months = set()
    while current < end_date:
        year_months.add((current.year, current.month))
        current += timedelta(days=1)

    for year, month in sorted(year_months):
        folder_prefix = f"{year:04d}/{month:02d}/"
        files = retrieve_imerg_files(server, email_gpm, HindCastMode, datetime.datetime(year, month, 1))
        for f in files:
            # The server returns bare basenames; strip any accidental path component
            # (e.g. if a future server version embeds the full path) then re-add the
            # canonical YYYY/MM/ prefix so lookups against `filename` always match.
            available_files.add(folder_prefix + f.split('/')[-1])

    return available_files


def get_file(filename, server, email_gpm, local_path=None):
   ''' Download an IMERG file from the PPS HTTPS server using requests.
   Uses the same auth pattern as retrieve_imerg_files() (email as both user
   and password).  requests follows redirects automatically, which curl
   without -L does not, so this avoids saving an HTML redirect page as the
   output file.
   When local_path is provided the file is saved there; otherwise it is saved
   to the current working directory using the bare filename.
   '''
   # Handle trailing slash to avoid double slashes in URL
   server = server.rstrip('/')
   url = server + '/' + filename
   if local_path is None:
       local_path = os.path.basename(filename)
   with requests.get(url, auth=(email_gpm, email_gpm), stream=True) as r:
       r.raise_for_status()
       with open(local_path, 'wb') as f:
           for chunk in r.iter_content(chunk_size=65536):
               f.write(chunk)


def ReadandWarp(gridFile, xmin, ymin, xmax, ymax):

    #Read grid and warp to domain grid
    #Assumes no reprojection is necessary, and EPSG:4326
    rawGridIn = gdal.Open(gridFile, GA_ReadOnly)

    # Use GDAL's in-memory virtual filesystem for the intermediate translate
    # output so parallel region threads never collide on a shared on-disk temp
    # file (previously the hardcoded 'OutTemp.tif' in CWD caused race conditions
    # when multiple regions processed the same IMERG timestamp simultaneously).
    import uuid as _uuid
    mem_path = f'/vsimem/OutTemp_{_uuid.uuid4().hex}.tif'
    pre_ds = gdal.Translate(mem_path, rawGridIn, options="-co COMPRESS=Deflate -a_nodata 29999 -a_ullr -180.0 90.0 180.0 -90.0")

    gt = pre_ds.GetGeoTransform()
    proj = pre_ds.GetProjection()
    nx = pre_ds.GetRasterBand(1).XSize
    ny = pre_ds.GetRasterBand(1).YSize
    NoData = 29999
    pixel_size = gt[1]

    #Warp to model resolution and domain extents
    ds = gdal.Warp('', pre_ds, srcNodata=NoData, srcSRS='EPSG:4326', dstSRS='EPSG:4326', dstNodata='29999', format='VRT', xRes=pixel_size, yRes=-pixel_size, outputBounds=(xmin,ymin,xmax,ymax))

    WarpedGrid = ds.ReadAsArray()
    new_gt = ds.GetGeoTransform()
    new_proj = ds.GetProjection()
    new_nx = ds.GetRasterBand(1).XSize
    new_ny = ds.GetRasterBand(1).YSize

    # Release the in-memory dataset and unlink the vsimem file.
    pre_ds = None
    gdal.Unlink(mem_path)

    return WarpedGrid, new_nx, new_ny, new_gt, new_proj


def WriteGrid(gridOutName, dataOut, nx, ny, gt, proj):
    #Writes out a GeoTIFF based on georeference information in RefInfo
    driver = gdal.GetDriverByName('GTiff')
    dst_ds = driver.Create(gridOutName, nx, ny, 1, gdal.GDT_Float32, ['COMPRESS=DEFLATE'])
    dst_ds.SetGeoTransform(gt)
    dst_ds.SetProjection(proj)
    dataOut.shape = (-1, nx)
    dst_ds.GetRasterBand(1).WriteArray(dataOut, 0, 0)
    dst_ds.GetRasterBand(1).SetNoDataValue(-9999.0)
    dst_ds = None

def processIMERG(local_filename,llx, lly ,urx, ury):
    # Process grid
    # Read and subset grid
    NewGrid, nx, ny, gt, proj = ReadandWarp(local_filename,llx, lly, urx, ury)
    # Scale value
    NewGrid = NewGrid*0.1
    return NewGrid, nx, ny, gt, proj



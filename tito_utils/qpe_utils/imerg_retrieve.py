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
2. Fill gaps in precipitation records using nowcast backup data
3. Provide 30-minute accumulated precipitation inputs for EF5 hydrologic model

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
  - qpf_store_path: Backup directory for gap-filling with nowcast data

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
- Falls back to nowcast data from qpf_store_path if IMERG is unavailable
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
        msg = f"    ERROR downloading {filename}: {e}"
        if print_lock:
            with print_lock:
                print(msg)
        else:
            print(msg)
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

    print(f"    Checking server for available files...")
    available_files = _get_available_files_for_range(
        server, email_gpm, initial_timestamp, final_date, HindCastMode)
    print(f"    Found {len(available_files)} files available on server")

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
    print(f"    IMERG parallel download: {n_total} timesteps, "
          f"workers={max_workers}")

    downloaded_count = 0
    skipped_count = 0
    exists_count = 0
    error_count = 0
    print_lock = threading.Lock()
    done = 0

    def _work(ts):
        return _imerg_one_timestep(
            ts, precipFolder, server, email_gpm,
            xmin, ymin, xmax, ymax,
            available_files, file_prefix, print_lock,
        )

    if max_workers == 1:
        results = [_work(ts) for ts in timesteps]
        for st in results:
            done += 1
            if st == "ok":
                downloaded_count += 1
            elif st == "exists":
                exists_count += 1
            elif st == "missing":
                skipped_count += 1
            else:
                error_count += 1
            if done % 50 == 0 or done == n_total:
                print(f"    IMERG progress: {done}/{n_total} "
                      f"(new={downloaded_count} exist={exists_count} "
                      f"skip={skipped_count} err={error_count})")
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_work, ts): ts for ts in timesteps}
            for fut in as_completed(futs):
                st = fut.result()
                done += 1
                if st == "ok":
                    downloaded_count += 1
                elif st == "exists":
                    exists_count += 1
                elif st == "missing":
                    skipped_count += 1
                else:
                    error_count += 1
                if done % 50 == 0 or done == n_total:
                    with print_lock:
                        print(f"    IMERG progress: {done}/{n_total} "
                              f"(new={downloaded_count} exist={exists_count} "
                              f"skip={skipped_count} err={error_count})")

    print(f"    Download complete: {downloaded_count} downloaded, "
          f"{exists_count} already present, "
          f"{skipped_count} skipped/missing, {error_count} errors")


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

def get_new_precip(current_timestamp, ppt_server_path, precipFolder, email, HindCastMode, qpf_store_path, xmin, ymin, xmax, ymax):
    """Function that brings latest IMERG precipitation file into the GeoTIFF precip folder

    Arguments:
        current_timestamp {datetime} -- current time step's timestamp
        netcdf_feed_path {str} -- path to the geoTIFF precip data feed --- el httml
        geotiff_precip_path {str} -- path to the GeoTIFF precip archive -- el folder precip 

    Returns:
        ahead {bool} -- Returns True if the latest GeoTIFF timestamp is agead of the current time step
        gap {bool} -- Returns True if there is a gap larger than 30min between the latest GeoTIFF timestamp and the current time step
        exists {bool} -- Returns True there is a GeoTIFF file in the archive for the current time step
    """
    #Look for the most recent file in precip folder
    #Obtainign the latest time step in the folder
    current_timestamp = to_naive_utc(current_timestamp)
    files_folder = os.listdir(precipFolder)
    tif_files = [f for f in files_folder if "qpe" in f]
    
    #the first hour of nowcast files will be current time - 3.5h
    nowcast_older = current_timestamp - timedelta(hours = 3.5) #This is the first nowcast file to be created 
    
    if tif_files:
        print("    There are IMERG files in the precip folder")
        # Extract the most recent date from files
        latest_date = max(tif_files, key=lambda x: datetime.datetime.strptime(x[10:22], '%Y%m%d%H%M')) #to improve 
        formatted_latest_pptfile = datetime.datetime.strptime(latest_date[10:22], '%Y%m%d%H%M') #last file on imerg precip
        #if the latest imerg file in folder corresponds to the older nowcast file (current time - 4h)
        if formatted_latest_pptfile < nowcast_older:
            # and if the time difference betwen the current timestep and the latest imerg in folder is less than 30 min.
            if nowcast_older - formatted_latest_pptfile <= timedelta(minutes=60):
                print(f"    There are less than 60 min between last imerg file available on folder: {formatted_latest_pptfile} and last imerg file on server: ", nowcast_older-timedelta(minutes=30))
                #List the missing dates between lastest ppt file and current timestep -4h
                missing_dates = []
                # Iterar desde la fecha del archivo más reciente hasta el timestamp actual en intervalos de 30 minutos
                next_timestamp = formatted_latest_pptfile + timedelta(minutes=30)
                while next_timestamp < nowcast_older:
                    missing_dates.append(next_timestamp)
                    next_timestamp += timedelta(minutes=30)
                for date in missing_dates:
                    #Verifying if missing dates are on the GPM server.
                    server_files = retrieve_imerg_files(ppt_server_path, email, HindCastMode, date)
                    timestamps = [to_naive_utc(extract_timestamp(file)) for file in server_files]
                    if date in timestamps:
                        print("    Downloading the last file of precip data")
                        #downloading the file 
                        date_server = date - timedelta(minutes=30)
                        nowcast_older_server = nowcast_older - timedelta(minutes=60) #this is because get imerg files sums up 30 min
                        get_gpm_files(precipFolder, date_server, nowcast_older_server, ppt_server_path, email, xmin, ymin, xmax, ymax)
                    else:
                        print("    The file required is not available on the IMERG server.")
                        print("    Copying the corresponding file from nowcast store folder")
                        formatted_date = date.strftime('%Y%m%d%H%M')
                        # Look for the filename in qpf store that cointains the 'formatted_timestamp' missing
                        for filename in os.listdir(qpf_store_path):
                            if formatted_date in filename:
                                source_file = os.path.join(qpf_store_path, filename)
                                destination_file = os.path.join(precipFolder, filename)
                                # Copiar el archivo al directorio de destino
                                shutil.copy2(source_file, destination_file)
                                print(f"    File '{filename}' was copied in '{precipFolder}'")
                            else:   
                                break                          
            else: 
                print(f"    There's more than a 60 min gap between latency Imerg: {nowcast_older-timedelta(minutes=30)} and the latest geoTIFF file {formatted_latest_pptfile}")
                print("    Latest Geotiff file available in folder:", formatted_latest_pptfile)
                print("    Last IMERG file to download:", nowcast_older - timedelta(minutes=30))
                #Downloading imerg files between dates
                nowcast_older_server = nowcast_older - timedelta(minutes=60)
                latest_pptfile = formatted_latest_pptfile
                get_gpm_files(precipFolder, latest_pptfile, nowcast_older_server, ppt_server_path, email, xmin, ymin, xmax, ymax)
                
                #List the missing dates between latest ppt file and current timestep
                missing_dates = []
                next_timestamp = formatted_latest_pptfile + timedelta(minutes=30)
                while next_timestamp < nowcast_older:
                    missing_dates.append(next_timestamp)
                    next_timestamp += timedelta(minutes=30)
               
                for date in missing_dates: 
                    #retrieven file names from GPM server
                    server_files = retrieve_imerg_files(ppt_server_path, email, HindCastMode, date)    
                    timestamps = [to_naive_utc(extract_timestamp(file)) for file in server_files]
                    
                    #Looking for timestaps missing in imerg
                    if date not in timestamps:
                        print(f"    File {date} is missing")
                        print("    Copying the corresponding file from nowcast store folder")
                        formatted_date = date.strftime('%Y%m%d%H%M')
                        # Copying missing file from qpf store folder 
                        for filename in os.listdir(qpf_store_path):
                            if formatted_date in filename:
                                source_file = os.path.join(qpf_store_path, filename)
                                destination_file = os.path.join(precipFolder, filename)
                                # Copying file to precip folder
                                shutil.copy2(source_file, destination_file)
                                print(f"    File '{filename}' was copied in '{precipFolder}'")
                            else:
                                break
                    #if date is in timestaps, file is available.    
    else:
        print("    No '.tif' files found in the precip folder.") 
        #If there is no files in folder, Download the entire chuck of dates 
        #from failtime (current time - 6h) to Nowcast time (current time -4h) 
        initial_time = current_timestamp - timedelta(hours = 9.5)
        #Downloading imerg Files
        nowcast_older_server = nowcast_older - timedelta(minutes=60)
        initial_time_server = initial_time - timedelta(minutes=30)
        print("    Last IMERG file to download:", nowcast_older- timedelta(minutes=30))
        print("    Initial time to download:", initial_time)
        get_gpm_files(precipFolder, initial_time_server, nowcast_older_server, ppt_server_path, email, xmin, ymin, xmax, ymax)
        #if some file is missing
        missing_dates = []
        next_timestamp = initial_time + timedelta(minutes=30)

        #retrieving gpm files for the last file that it is supposed to be downloaded.
        date_in_server = nowcast_older- timedelta(minutes=30)
        server_files = retrieve_imerg_files(ppt_server_path, email, HindCastMode, date_in_server)

        while next_timestamp < nowcast_older:
            missing_dates.append(next_timestamp)
            next_timestamp += timedelta(minutes=30)
            
            for date in missing_dates:     
                timestamps = [to_naive_utc(extract_timestamp(file)) for file in server_files]
                
                if date not in timestamps:
                    print(f"    File {date} is missing")
                    print("    Copying the corresponding file from nowcast store folder")
                    formatted_date = date.strftime('%Y%m%d%H%M')
                    for filename in os.listdir(qpf_store_path):
                        if formatted_date in filename:
                            source_file = os.path.join(qpf_store_path, filename)
                            destination_file = os.path.join(precipFolder, filename)
                            # Copying file to precip folder
                            shutil.copy2(source_file, destination_file)
                            print(f"    File '{filename}' was copied in '{precipFolder}'")
                        else:
                            break
                    """
                    print(f"   There is no file in qpf store with date: '{formatted_date}'") ### TO DO
                    tif_files = glob.glob(os.path.join(precipFolder, "imerg.qpe.*.30minAccum.tif"))
                    if tif_files:
                        # Find the most recent file
                        latest_file = max(tif_files, key=extract_datetime_from_filename)
                        print(f"    Latest file: {latest_file}")
                        new_filename = os.path.join(precipFolder, f"imerg.qpe.{formatted_date}.30minAccum.tif")
                        shutil.copy2(latest_file, new_filename)
                        print(f"    Created duplicate file: {new_filename}")
                    else:
                        print("    No .tif files found in precipFolder to copy")   
                    """
    # Get a list of all .tif files in the current directory and delete this files
    try:
        tif_files = glob.glob("./*.tif")
        for tif_file in tif_files:
            os.remove(tif_file)
    except:
        print(' ')


"""
================================================================================
HSAF Precipitation Retrieval Module (QPE - Quantitative Precipitation Estimate)
================================================================================

Description:
------------
Downloads HSAF (EUMETSAT Hydrological Satellite Application Facility) H40B 
product from MeteoAM FTP server, decompresses .nc.gz files, converts NetCDF
to GeoTIFF format, and prepares data for EF5 hydrologic model ingestion.
Provides 10-minute instantaneous rain rate estimates from multi-satellite
precipitation products.

Standalone Usage:
-----------------
1. Obtain HSAF credentials (free registration required):
   - Register at: https://www.meteoam.it/it/dati-az/ftphsaf
   - Username and password will be provided via email

2. Basic standalone script example:

   from datetime import datetime
   from hsaf_retrieve import get_new_hsaf_precip
   
   # Define domain bounds
   xmin, ymin, xmax, ymax = -85.0, 10.0, -60.0, 25.0
   
   # Your HSAF credentials
   ftp_user = "your_hsaf_username"
   ftp_pass = "your_hsaf_password"
   
   # Current timestamp for data retrieval
   current_time = datetime.utcnow()
   
   # Download HSAF data
   get_new_hsaf_precip(
       current_timestamp=current_time,
       precipFolder="./hsaf_output",
       ftp_user=ftp_user,
       ftp_pass=ftp_pass,
       xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax,
       latency_minutes=20,    # Expected data latency
       lookback_hours=6       # Hours of historical data to retrieve
   )

3. Test FTP connectivity:

   curl --head --user "username:password" \
        ftp://ftphsaf.meteoam.it/h40B/h40_cur_mon_data/h40_20240115_1200_fdk.nc.gz

TITO Integration:
-----------------
TITO (Threading Inputs to Outputs) uses this module to:
1. Provide multi-satellite precipitation estimates for flood forecasting
2. Supply 10-minute instantaneous precipitation rates for high-temporal-resolution
   applications
3. Alternative or complementary QPE source to IMERG and SCaMPR

Called by: TITO orchestrator during precipitation preparation phase
Function: get_new_hsaf_precip() - Main entry point for TITO

Parameters expected from TITO:
  - current_timestamp: datetime for the current forecast cycle (UTC)
  - precipFolder: Output directory for processed GeoTIFFs
  - ftp_user: HSAF FTP username
  - ftp_pass: HSAF FTP password
  - xmin/ymin/xmax/ymax: Domain bounding box in degrees
  - latency_minutes: Expected data latency (default 20 min)
  - lookback_hours: Historical data window to populate (default 6 hours)

Required Packages:
------------------
- Standard library only (no Python package dependencies):
  - os, re, shutil, subprocess, datetime, pathlib

- External tools required:
  - curl: For HTTP/FTP file download
    Windows: Download from https://curl.se/windows/
    Linux: sudo apt-get install curl
    macOS: brew install curl
  
  - gzip: For .nc.gz decompression
    Usually pre-installed on Unix/Linux/macOS
    Windows: Use 7-Zip or Git Bash
  
  - GDAL command-line tools: For NetCDF to GeoTIFF conversion
    conda install -c conda-forge gdal
    Required tools: gdalwarp, gdal_translate

- Internal TITO dependencies: None (self-contained module)

Data Source:
------------
HSAF (Hydrological Satellite Application Facility) H40B Product
- Server: ftp://ftphsaf.meteoam.it/h40B/h40_cur_mon_data/
- Spatial Resolution: 0.04° x 0.04° (~4 km at equator)
- Temporal Resolution: 10 minutes
- Coverage: 70°N to 70°S
- Format: NetCDF4 compressed (.nc.gz) → GeoTIFF
- Latency: ~15-20 minutes

Output Format:
--------------
GeoTIFF files named: h40_YYYYMMDD_HHMM_fdk.tif
- Projection: EPSG:4326 (WGS84)
- Units: mm/hour (instantaneous rain rate)
- Variable: rr (rain rate)
- NoData: -9999
- Compression: DEFLATE

Authentication:
---------------
Requires free registration at MeteoAM:
https://www.meteoam.it/it/dati-az/ftphsaf

Behavior:
---------
- Downloads 10-minute H40B products from FTP server
- Uses curl for robust HTTP/FTP transfers
- Decompresses .nc.gz files using gzip
- Converts using gdalwarp (NETCDF driver) → gdal_translate (-unscale)
- Files inside latency window filled by copying last available data
- Creates _hsaf_raw/ subfolder for temporary downloads

Notes:
------
- Normalizes input timestamps to naive UTC internally
- Skips already-downloaded files on subsequent runs
- Gracefully handles missing files by continuing to next timestamp
- Requires libgdal-netcdf for NetCDF driver support (install via conda)
================================================================================
"""

import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path


def _run_cmd(args):
    return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _format_stderr(stderr_text, max_lines=12):
    if not stderr_text:
        return "<no stderr output>"
    lines = [ln for ln in stderr_text.strip().splitlines() if ln.strip()]
    if not lines:
        return "<no stderr output>"
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


def _curl_head_exists(url, user, password):
    result = _run_cmd([
        "curl",
        "--fail",
        "--silent",
        "--head",
        "--user",
        f"{user}:{password}",
        url,
    ])
    return result.returncode == 0


def _curl_download(url, output_path, user, password):
    result = subprocess.run(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--user",
            f"{user}:{password}",
            "-o",
            str(output_path),
            url,
        ],
        check=False,
    )
    return result.returncode == 0


def _convert_netcdf_to_geotiff(nc_file, xmin, ymin, xmax, ymax):
    """Convert a .nc.gz or already-decompressed .nc HSAF H40B file to GeoTIFF.

    Mirrors convert_netcdf_to_geotiff() in the original hsaf_precip.py:
      1. Decompress .nc.gz if needed.
      2. gdalwarp with NETCDF:"path"://rr  (requires libgdal-netcdf conda package).
      3. gdal_translate -unscale.
      4. Delete intermediate .nc and .unscaled files.
    """
    # Resolve input to the .nc path regardless of whether caller passed .nc.gz or .nc
    if nc_file.suffix == ".gz":
        nc_gz  = nc_file
        nc_path = nc_file.with_suffix("")          # strip .gz  -> .nc
    else:
        nc_gz  = None
        nc_path = nc_file

    tif_path      = nc_path.with_suffix(".tif")
    unscaled_path = nc_path.with_suffix(".tif.unscaled")

    if tif_path.exists():
        return tif_path

    # ------------------------------------------------------------------
    # 1. Decompress if we have a .nc.gz and the .nc is not already there
    # ------------------------------------------------------------------
    if not nc_path.exists():
        if nc_gz and nc_gz.exists():
            result = subprocess.run(["gzip", "-d", str(nc_gz)], check=False)
            if result.returncode != 0 or not nc_path.exists():
                print(f"    Warning: gzip decompression failed for {nc_gz.name}")
                return None
        else:
            print(f"    Warning: source not found ({nc_path.name})")
            return None

    # ------------------------------------------------------------------
    # 2. gdalwarp  — same syntax as original hsaf_precip.py
    #    NETCDF:"abs_path"://rr  (NETCDF driver + CF georef, H40B variable "rr")
    #    Requires: conda install -c conda-forge libgdal-netcdf
    # ------------------------------------------------------------------
    src = f'NETCDF:"{nc_path.resolve()}"://rr'
    warp_cmd = [
        "gdalwarp", "-overwrite",
        "-of", "GTiff", "-ot", "Float32",
        "-t_srs", "EPSG:4326", "-r", "bilinear",
        "-wo", "NUM_THREADS=ALL_CPUS",
        "-co", "COMPRESS=DEFLATE",
        "-dstnodata", "-9999",
        "-te", str(xmin), str(ymin), str(xmax), str(ymax),
        src, str(unscaled_path),
    ]
    result = subprocess.run(warp_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"    gdalwarp failed for {nc_path.name}")
        print(f"    cmd: {' '.join(warp_cmd)}")
        print(f"    stderr:\n{_format_stderr(result.stderr)}")
        return None

    # ------------------------------------------------------------------
    # 3. gdal_translate -unscale
    # ------------------------------------------------------------------
    translate_cmd = [
        "gdal_translate",
        "-of", "GTiff", "-ot", "Float32",
        "-unscale", "-co", "COMPRESS=DEFLATE",
        str(unscaled_path), str(tif_path),
    ]
    result = subprocess.run(translate_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"    gdal_translate failed for {nc_path.name}")
        print(f"    stderr:\n{_format_stderr(result.stderr)}")
        return None

    # ------------------------------------------------------------------
    # 4. Clean up intermediate files
    # ------------------------------------------------------------------
    for p in (nc_path, unscaled_path):
        try:
            p.unlink()
        except OSError:
            pass

    return tif_path


def _extract_timestamp_from_h40_name(file_name):
    match = re.match(r"h40_(\d{8})_(\d{4})_fdk\.tif$", file_name)
    if not match:
        return None
    return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M")


def _build_hsaf_name(ts):
    return f"h40_{ts:%Y%m%d_%H%M}_fdk.nc.gz"


def get_new_hsaf_precip(
    current_timestamp,
    precipFolder,
    ftp_user,
    ftp_pass,
    xmin,
    ymin,
    xmax,
    ymax,
    latency_minutes=20,
    lookback_hours=6,
):
    """Download/convert HSAF H40B and map into TITO precip naming.

    Behavior:
    - Attempts 10-minute products from (current - lookback_hours) to current.
    - Files inside latency window are filled by copying the last available HSAF file.
    - Output names are kept in the existing qpe convention for downstream compatibility.
    """
    # Normalize to naive UTC so timestamp comparisons work regardless of whether
    # the caller passed a timezone-aware datetime (real-time mode) or a naive
    # datetime (hindcast mode).
    if getattr(current_timestamp, "tzinfo", None) is not None:
        from datetime import timezone as _tz
        current_timestamp = current_timestamp.astimezone(_tz.utc).replace(tzinfo=None)

    precip_dir = Path(precipFolder)
    precip_dir.mkdir(parents=True, exist_ok=True)

    hsaf_work_dir = precip_dir / "_hsaf_raw"
    hsaf_work_dir.mkdir(parents=True, exist_ok=True)

    ftp_base = "ftp://ftphsaf.meteoam.it/h40B/h40_cur_mon_data"
    start_time = current_timestamp - timedelta(hours=lookback_hours)
    start_time = start_time.replace(minute=(start_time.minute // 10) * 10, second=0, microsecond=0)
    end_time = current_timestamp.replace(minute=(current_timestamp.minute // 10) * 10, second=0, microsecond=0)

    latest_safe_time = current_timestamp - timedelta(minutes=latency_minutes)
    latest_safe_time = latest_safe_time.replace(minute=(latest_safe_time.minute // 10) * 10, second=0, microsecond=0)

    expected_times = []
    t = start_time
    while t <= end_time:
        expected_times.append(t)
        t += timedelta(minutes=10)

    for ts in expected_times:
        if ts > latest_safe_time:
            continue
        remote_name = _build_hsaf_name(ts)
        remote_url = f"{ftp_base}/{remote_name}"
        local_nc_gz = hsaf_work_dir / remote_name
        # Skip if we already have the .nc.gz, the decompressed .nc, OR the final .tif
        local_tif = hsaf_work_dir / remote_name.replace(".nc.gz", ".tif")
        if local_nc_gz.exists() or (hsaf_work_dir / remote_name.replace(".gz", "")).exists() or local_tif.exists():
            continue
        if not _curl_head_exists(remote_url, ftp_user, ftp_pass):
            continue
        _curl_download(remote_url, local_nc_gz, ftp_user, ftp_pass)

    # Convert both fresh downloads (.nc.gz) and already-decompressed files (.nc).
    for nc_gz in hsaf_work_dir.glob("*.nc.gz"):
        _convert_netcdf_to_geotiff(nc_gz, xmin, ymin, xmax, ymax)
    for nc in hsaf_work_dir.glob("*.nc"):
        _convert_netcdf_to_geotiff(nc, xmin, ymin, xmax, ymax)

    # Build in-memory index of converted HSAF files.
    hsaf_tif_by_ts = {}
    for tif_file in hsaf_work_dir.glob("h40_*_fdk.tif"):
        ts = _extract_timestamp_from_h40_name(tif_file.name)
        if ts is not None:
            hsaf_tif_by_ts[ts] = tif_file

    if not hsaf_tif_by_ts:
        print("    No HSAF files were available/converter-ready for this cycle.")
        return

    # Mirror HSAF tifs into precip folder preserving native HSAF naming.
    last_available_target = None
    for ts in expected_times:
        target_name = f"h40_{ts:%Y%m%d_%H%M}_fdk.tif"
        target_path = precip_dir / target_name
        source_tif = hsaf_tif_by_ts.get(ts)

        if source_tif is not None:
            shutil.copy2(source_tif, target_path)
            last_available_target = target_path
            continue

        # Fill the latency gap with the latest available HSAF file.
        if ts > latest_safe_time and last_available_target is not None:
            shutil.copy2(last_available_target, target_path)

    print("    HSAF retrieval/update complete.")

import os            
import re
import shutil        
import glob
from datetime import datetime, timedelta, timezone  
from tito_utils.file_utils.datetime_utils import get_geotiff_datetime, to_naive_utc


def _get_hsaf_datetime(filename):
    """Extract datetime from an HSAF filename like h40_YYYYMMDD_HHMM_fdk.tif."""
    m = re.match(r"h40_(\d{8})_(\d{4})_fdk\.tif$", filename)
    if m:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
    return None


def _get_scampr_datetime(filename):
    """Extract datetime from a SCaMPR filename like scampr.qpe.YYYYMMDDHHMM.mmhInst.tif."""
    m = re.match(r"scampr\.qpe\.(\d{12})\.mmhInst\.tif$", filename)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M")
    return None


def _to_naive_utc(dt):
    """Normalize datetime to naive UTC for robust comparisons."""
    return to_naive_utc(dt)


def cleanup_nowcast_qpe(current_datetime, precipFolder, latency_hours=4):
    """Delete synthetic IMERG nowcast/duplicated files generated to bridge latency.

    Files with timestamps newer than current_datetime - latency_hours are
    considered temporary nowcast/duplicated products and are removed.
    This includes both imerg.qpe.* and imerg.qpf.* products.
    """
    removed = 0
    current_naive_utc = _to_naive_utc(current_datetime)
    latency_threshold = current_naive_utc - timedelta(hours=latency_hours)

    try:
        for fname in os.listdir(precipFolder):
            if not fname.endswith(".tif"):
                continue
            if not (fname.startswith("imerg.qpe.") or fname.startswith("imerg.qpf.")):
                continue

            fpath = os.path.join(precipFolder, fname)
            try:
                geotiff_datetime = get_geotiff_datetime(fpath)
                if geotiff_datetime > latency_threshold:
                    os.remove(fpath)
                    removed += 1
            except Exception as e:
                print(f"Error processing nowcast/duplicate IMERG file {fname}: {e}")
    except Exception as e:
        print(f"General error in cleanup_nowcast_qpe function: {e}")

    return removed


def cleanup_staged_precip_folders(staging_folders):
    """Remove staged .tif files from EF5 precip folders.

    This keeps per-region/shared staging folders clean between runs.
    """
    removed = 0
    for folder in staging_folders:
        try:
            for tif_path in glob.glob(os.path.join(folder, "*.tif")):
                try:
                    os.remove(tif_path)
                    removed += 1
                except Exception as e:
                    print(f"Error deleting staged precip file {tif_path}: {e}")
        except Exception as e:
            print(f"Error cleaning staged precip folder {folder}: {e}")
    return removed

def cleanup_precip(current_datetime, precipFolder, qpf_store_path, keep_gap_fill=False, older_qpe_hours=6.5):
    """Function that cleans up the precip folder for the current EF5 run

    Arguments:
        current_datetime {datetime} -- datetime object for the current time step
        failTime {datetime} -- datetime object representing the maximum datetime in the past
        precipFolder {str} -- path to the geotiff precipitation folder
        qpf_store_path {str} -- path to the folder where QPF files are stored
        keep_gap_fill {bool} -- when True, skip deletion of QPE files newer than T-4h.
            Set to True for IMERG_SCAMPR/IMERG_HSAF hindcast experiments where those
            files are SCaMPR/HSAF gap-fill outputs from the previous step and must
            be preserved as inputs for the current step's EF5 run.
        older_qpe_hours {float} -- QPE files older than this many hours before
            current_datetime are removed.  Default 6.5 h is right for EF5's
            6 h simulation window.  Pass 10.0 for IMERG_NOWCAST mode so that
            the ConvLSTM model always has its 12 required 30-min input frames
            (12 × 0.5 h = 6 h ending at the 4 h IMERG latency boundary =
            currentTime − 10 h oldest file needed).
    """
    current_naive_utc = _to_naive_utc(current_datetime)

    qpes = []
    qpfs = []
    older_QPE = current_naive_utc - timedelta(hours=older_qpe_hours)
    imerg_Latency = current_naive_utc - timedelta(hours=4)

    try:
        # List all precip files
        precip_files = os.listdir(precipFolder)

        # Segregate between QPEs and QPFs
        for file in precip_files:
            if "qpe" in file:
                qpes.append(file)
            elif "qpf" in file:
                qpfs.append(file)

        for qpe in qpes:
            try:
                geotiff_datetime = get_geotiff_datetime(precipFolder + qpe)
                if geotiff_datetime < older_QPE:
                    os.remove(precipFolder + qpe)
            except Exception as e:
                print(f"Error processing QPE file {qpe}: {e}")

        print("    Copying all QPF files older than Current Time: ", current_naive_utc, " into qpf_store folder.")
        for qpf in qpfs:
            try:
                geotiff_datetime = get_geotiff_datetime(precipFolder + qpf)
                if geotiff_datetime < current_naive_utc:
                    shutil.copy2(precipFolder + qpf, qpf_store_path)
                os.remove(precipFolder + qpf)
            except Exception as e:
                print(f"Error processing QPF file {qpf}: {e}")

        # Remove duplicate/nowcast IMERG files that are newer than the latency boundary.
        # Skip this in IMERG_SCAMPR/IMERG_HSAF hindcast mode (keep_gap_fill=True): those
        # newer files are SCaMPR/HSAF gap-fill outputs written by the previous step and
        # are legitimate EF5 inputs for the current step — they must not be deleted here.
        if not keep_gap_fill:
            for qpedup in qpes:
                try:
                    geotiff_datetime = get_geotiff_datetime(precipFolder + qpedup)
                    if geotiff_datetime > current_naive_utc - timedelta(hours=4):
                        os.remove(precipFolder + qpedup)
                except Exception as e:
                    print(f"Error processing QPE duplicate file {qpedup}: {e}")

        max_qpf = current_naive_utc - timedelta(hours=4)
        print(f"    Deleting all QPF files in store folder and subfolders older than: {max_qpf}")
        
        qpf_stored_files = os.listdir(qpf_store_path)
        qpf_stored_files = [f for f in qpf_stored_files if f.endswith('.tif')]
        for qpf_stored in qpf_stored_files:
            try:
                qpf_datetime = get_geotiff_datetime(qpf_store_path + qpf_stored)
                if qpf_datetime < max_qpf:
                    os.remove(qpf_store_path + qpf_stored)
            except Exception as e:
                print(f"Error processing stored QPF file {qpf_stored}: {e}")


        # --- HSAF file cleanup (h40_*_fdk.tif in precipFolder and _hsaf_raw/) ---
        hsaf_raw_dir = os.path.join(precipFolder, "_hsaf_raw")
        for search_dir in [precipFolder, hsaf_raw_dir]:
            if not os.path.isdir(search_dir):
                continue
            for fname in os.listdir(search_dir):
                if not fname.startswith("h40_") or not fname.endswith(".tif"):
                    continue
                fdt = _get_hsaf_datetime(fname)
                if fdt is not None and fdt < older_QPE:
                    try:
                        os.remove(os.path.join(search_dir, fname))
                        print(f"    Deleted old HSAF file: {fname}")
                    except Exception as e:
                        print(f"Error deleting HSAF file {fname}: {e}")

        # --- SCaMPR file cleanup (scampr.qpe.* in precipFolder and _scampr_raw/) ---
        scampr_raw_dir = os.path.join(precipFolder, "_scampr_raw")
        for search_dir in [precipFolder, scampr_raw_dir]:
            if not os.path.isdir(search_dir):
                continue
            for fname in os.listdir(search_dir):
                if not fname.startswith("scampr.qpe.") or not fname.endswith(".tif"):
                    continue
                fdt = _get_scampr_datetime(fname)
                if fdt is not None and fdt < older_QPE:
                    try:
                        os.remove(os.path.join(search_dir, fname))
                        print(f"    Deleted old SCaMPR file: {fname}")
                    except Exception as e:
                        print(f"Error deleting SCaMPR file {fname}: {e}")

    except Exception as e:
        print(f"General error in cleanup_precip function: {e}")

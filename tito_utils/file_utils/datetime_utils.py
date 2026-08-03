import datetime
from datetime import datetime, timezone
import os
from datetime import timedelta


def to_naive_utc(dt):
    """Normalize a datetime to naive UTC for safe timezone-agnostic comparisons."""
    try:
        if getattr(dt, "tzinfo", None) is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return dt

def get_geotiff_datetime(geotiff_path):
    """Funtion that extracts a datetime object corresponding to a Geotiff's timestamp

    Arguments:
        geotiff_path {str} -- path to the geotiff to extract a datetime from

    Returns:
        datetime -- datetime object based on geotiff timestamp
    """
    geotiff_file = geotiff_path.split('/')[-1]
    geotiff_timestamp = geotiff_file.split('.')[2]
    geotiff_datetime = datetime.strptime(geotiff_timestamp, '%Y%m%d%H%M')
    return geotiff_datetime

def extract_timestamp(filename):
    """ This function is used in get_gpm_files"""
    date_str = filename.split('.')[4][:8]  
    time_str = filename.split('-')[3][1:]  
    date_time_str = date_str + time_str
    final_datetime = datetime.strptime(date_time_str, '%Y%m%d%H%M%S')+timedelta(minutes=30)
    return final_datetime

def extract_datetime_from_filename(filename):
    """ This function is used in get_gpm_files"""
    base_name = os.path.basename(filename)
    date_str = base_name.split('.')[2]  # Get YYYYMMDDHHMM part
    filename = datetime.strptime(date_str, '%Y%m%d%H%M')
    return filename
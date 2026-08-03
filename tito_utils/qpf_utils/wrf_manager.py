import glob
import os
import xarray as xr 
import numpy as np 
from datetime import datetime as dt
from datetime import timedelta
import re
import rioxarray

"""
For the use of this function, users must first have an archive of derived from WRF in ".nc" format. 
For this pre-processing, we recommend using tools such as the wrfout_to_cf.ncl script included in this repository, or any other tool 
the user considers appropriate for this conversion.

This code searches for those ".nc" files and converts them into an EF5-friendly format to be 
used within the TITO operational system.
"""

def parse_timestep(timestep: str) -> int:
    # Busca los dígitos en la cadena
    match = re.match(r"(\d+)", timestep)
    if match:
        return int(match.group(1))
    else:
        raise ValueError(f"Number not found in '{timestep}'")

def netcdf_to_geotiff(file_nc, qpf_store_folder, var_name):
    ds = xr.open_dataset(file_nc, engine='netcdf4')
    # Rename coords to be consistent 
    rename_dict = {}
    for c in list(ds.coords):
        cl = c.lower()
        if "lat" in cl:
            rename_dict[c] = "lat"
        elif "lon" in cl:
            rename_dict[c] = "lon"
        elif "time" in cl:
            ds = ds.drop_vars(c)
    ds = ds.rename(rename_dict)
    
    # Reduce dims if there is also a time dim
    lat = ds.lat.squeeze()[:, 0].values
    lon = ds.lon.squeeze()[0, :].values

    new_dataset = xr.Dataset({var_name: xr.DataArray(data=ds[var_name][0, :, :].values, 
                                                     dims=['lat', 'lon'],coords={'lat': lat, 'lon': lon},
                                                     attrs={'description': 'PRECIPITATION RATE','units': 'mm/h'})})
    da = new_dataset[var_name]
    
    # Prepare GeoTIFF
    da = da.astype('float32')
    array = np.where(np.isnan(da.values), -9999, da.values)
    da.values = array
    # Asign CRS y NoData
    da = da.rio.write_crs("EPSG:4326", inplace=True)
    da.rio.write_nodata(-9999, inplace=True)
    da = da.rio.set_spatial_dims(x_dim="lon", y_dim="lat")
    # Save as GeoTIFF
    out_name = f"{os.path.basename(file_nc)[:-len('.nc')]}.tif"
    da.rio.to_raster(f"{qpf_store_folder}/{out_name}")
    ds.close()

def WRF_searcher(path_wrf, qpf_store_path, start_time, end_time, LR_timestep, var_name, filename_template):
    """
    Check if WRF files exist between start_time and end_time.
    If all files are found, convert to tif and copy them to qpf_store_path+wrf_data.
    If not send a Message and continue execution 

    Parameters
    ----------
    path_wrf : str
        Path where the GFS tif files are stored.
    qpf_store_path : str
        Destination path to copy the files.
    start_time : datetime
        Start time of requested data.
    end_time : datetime
        End time of requested data.
    LR_timestep: str
        WRF time step in format "60u"
    var_name: str
        Indicate the variable name for the WRF precipitation files.
    filename_template: srt
        format of WRF filenames ex: PREC_d01_YYYY-MM-DD_HH_mm_SS.nc
    """
    
    # Ensure qpf_store_path exists
    download_folder = os.path.join(qpf_store_path, "wrf_data/")
    os.makedirs(download_folder, exist_ok=True)
    
    #remove old files 
    for f in glob.glob(os.path.join(download_folder, "*.tif")):
                os.remove(f)

    #recognize the timestep of QPF running
    time_step_qpf = parse_timestep(LR_timestep)
    
    # Build list of expected times (hourly steps assumed)
    expected_times = []
    current = start_time
    
    while current <= end_time:
        expected_times.append(current)
        current += timedelta(minutes=time_step_qpf)
        
    expected_files=[]
    for t in expected_times:
        filename = filename_template
        filename = filename.replace("YYYY", f"{t:%Y}") \
                           .replace("MM", f"{t:%m}") \
                           .replace("DD", f"{t:%d}") \
                           .replace("HH", f"{t:%H}") \
                           .replace("mm", f"{t:%M}") \
                           .replace("SS", f"{t:%S}")
        expected_files.append(os.path.join(path_wrf, filename))
        
    missing_files = [f for f in expected_files if not os.path.exists(f)]
    if not missing_files:
        print("All WRF files found. Converting to GeoTIFF...")
        conversion_ok = True
        for f, t in zip(expected_files, expected_times):
            try:
                netcdf_to_geotiff(f, download_folder, var_name)
                # Rename to EF5-compatible format: wrf.YYYYMMDDHH00.tif
                orig_tif = os.path.join(download_folder, os.path.basename(f)[:-len('.nc')] + '.tif')
                std_tif = os.path.join(download_folder, f"wrf.{t:%Y%m%d%H%M}.tif")
                if os.path.exists(orig_tif) and os.path.abspath(orig_tif) != os.path.abspath(std_tif):
                    os.rename(orig_tif, std_tif)
            except Exception as e:
                print(f"Failed to convert WRF file {f}: {e}")
                conversion_ok = False
        if conversion_ok:
            print("WRF conversion completed.")
        return conversion_ok
    else:
        print(f"⚠️ Missing {len(missing_files)} WRF files.")
        print(f"⚠️ WARNING ⚠️ The system will run without WRF inputs due to missing data.")
        print(f"⚠️ Check tito_utils/qpf_utils/wrf_manager.py for details.")
        return False
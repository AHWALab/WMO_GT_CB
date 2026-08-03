import os
import sys
import glob
import shutil
import datetime
from datetime import datetime
from datetime import timedelta
import subprocess

# Force the local nowcasting source tree to load before any installed (possibly
# stale) package.  The servir/servir_data_utils packages may have been installed
# with 'pip install .' (non-editable), so edits to the source files won't be
# picked up automatically.  Inserting the source path here guarantees we always
# use the version on disk in the TITO tree.
_local_ncast_dir = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 '../../Nowcast/nowcasting')
)
if _local_ncast_dir not in sys.path:
    sys.path.insert(0, _local_ncast_dir)
# Invalidate any already-cached stale module entries so the local version wins.
for _m in [k for k in sys.modules if k.startswith(('servir_data_utils', 'servir_nowcasting_examples', 'servir'))]:
    del sys.modules[_m]

from servir_nowcasting_examples.m_nowcasting import load_default_params_for_model, nowcast
from servir_data_utils.m_h5py2tif import h5py2tif
from servir_data_utils.m_tif2h5py import tif2h5py

def run_convlstm(currentTime, precipFolder, nowcast_model_name, xmin, ymin, xmax, ymax):
    #running nowcast codes
    try:
        tif2h5py(precipFolder, 'Nowcast/servir_nowcasting_examples/temp/input_imerg.h5', 'Nowcast/servir_nowcasting_examples/temp/imerg_geotiff_meta.json',
                 x1=xmin, y1=ymin, x2=xmax, y2=ymax)
        
        ### Command 2: python m_nowcasting.py
        # with library implementation
        param_dict = load_default_params_for_model(nowcast_model_name)
        param_dict['output_h5_fname'] = 'Nowcast/servir_nowcasting_examples/temp/output_imerg.h5'
    
        # optionally modify the parameter dictionary
        nowcast(param_dict)
    
        ### Command 3: python m_h5py2tif.py
        # with library implementation
        h5py2tif('Nowcast/servir_nowcasting_examples/temp/output_imerg.h5', 'Nowcast/servir_nowcasting_examples/temp/imerg_geotiff_meta.json', precipFolder)

        # Trim any ML-generated files past the cycle time.  The model always
        # produces 12 forecast steps (6 h), but the nowcast is only meant to
        # fill the 4-hour IMERG latency gap (T-4h → T).  Remove every
        # imerg.qpf.* file whose timestamp is strictly after currentTime.
        for _fname in glob.glob(os.path.join(precipFolder, "imerg.qpf.*.30minAccum.tif")):
            try:
                _dt_str = os.path.basename(_fname).split('.')[2]
                _dt = datetime.strptime(_dt_str, '%Y%m%d%H%M')
                if _dt > currentTime:
                    os.remove(_fname)
            except Exception:
                pass

    except Exception as e:
        print("    Something failed within ML-nowcast routines with exception {} . Execution has been paused.".format(e))
        print(e)
        
        # Fill the 4-hour IMERG latency gap: T-3.5h → T (one step after the
        # last real IMERG at T-4h, up to current cycle time only).
        # Previously this generated files up to T+2.5h which was wrong.
        init = currentTime - timedelta(hours = 3.5)
        final = currentTime  # only fill up to cycle time, not beyond
        print('    Duplicating last qpe file')
        date_list = []
        current_date = init
        while current_date <= final:
            date_list.append(current_date.strftime('%Y%m%d%H%M'))
            current_date += timedelta(minutes=30)
            
        # Find all .tif files in the directory
        tif_files = glob.glob(os.path.join(precipFolder, "imerg.qpe.*.30minAccum.tif"))
    
        # Extract dates from filenames and find the most recent file
        most_recent_file = None
        most_recent_date = None

        for file in tif_files:
            filename = os.path.basename(file)
            file_date_str = filename.split('.')[2]
            file_date = datetime.strptime(file_date_str, '%Y%m%d%H%M')
            if most_recent_date is None or file_date > most_recent_date:
                most_recent_date = file_date
                most_recent_file = file

        if most_recent_file is None:
            print("     No valid .tif files found in the directory.")
        else:
            print(f"     Most recent file selected: {most_recent_file}")

        # Duplicate the most recent file with new names based on the date list
        for date_str in date_list:
            new_filename = f"imerg.qpe.{date_str}.30minAccum.tif"
            new_filepath = os.path.join(precipFolder, new_filename)
            shutil.copy2(most_recent_file, new_filepath)
            print(f"Created file: {new_filepath}")    

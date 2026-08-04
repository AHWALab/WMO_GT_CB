"""
EF5 low-level routines (still actively used).

This is NOT dead code.  Job builders in ``tito_utils.ef5.jobs`` decide *which*
simulations to run; this module does the work for each job:

  prepare_ef5()
    → find_available_states()
    → rename_ef5_precip()          # stage precip into precipEF5/
    → write_control_file()         # write the EF5 control file
  run_ef5_simulations_parallel()
    → run_ef5_simulation()
    → run_EF5()                    # docker / apptainer container
"""

import os
import shutil
import re
import glob
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from shutil import rmtree
import datetime
from datetime import timedelta
import subprocess
from tito_utils.file_utils.file_handling import is_non_zero_file, mkdir_p
from tito_utils.ef5.alerts import send_mail


def _as_posix(path):
    return str(path).replace("\\", "/")


def _with_trailing_slash(path):
    if path.endswith("/") or path.endswith("\\"):
        return _as_posix(path)
    return _as_posix(path) + "/"


def _select_file(folder_path, patterns, fallback=None, exclude_tokens=None):
    exclude_tokens = [t.lower() for t in (exclude_tokens or [])]
    for pattern in patterns:
        matches = sorted(glob.glob(os.path.join(folder_path, pattern)))
        for match in matches:
            basename = os.path.basename(match)
            lowered = basename.lower()
            if any(token in lowered for token in exclude_tokens):
                continue
            return basename
    return fallback


def _resolve_region_paths(region_name, model_resolution, basicPath, parametersPath):
    region_slug = region_name.lower()
    crest_folder = f"CREST_{region_name}_{model_resolution}"
    kw_folder = f"KW_{region_name}_{model_resolution}"

    dem = f"DEM_{region_slug}_{model_resolution}.tif"
    ddm = f"FDIR_{region_slug}_{model_resolution}.tif"
    fam = f"FAC_{region_slug}_{model_resolution}.tif"

    basic_abs = os.path.abspath(basicPath)
    for required in [dem, ddm, fam]:
        required_path = os.path.join(basic_abs, required)
        if not os.path.isfile(required_path):
            raise FileNotFoundError(f"Missing required basic file: {required_path}")

    crest_abs = os.path.join(os.path.abspath(parametersPath), crest_folder)
    kw_abs = os.path.join(os.path.abspath(parametersPath), kw_folder)
    if not os.path.isdir(crest_abs):
        raise FileNotFoundError(f"Missing CREST parameter folder: {crest_abs}")
    if not os.path.isdir(kw_abs):
        raise FileNotFoundError(f"Missing KW parameter folder: {kw_abs}")

    wm_file = _select_file(crest_abs, ["crest_Wm*.tif", "crest_wm*.tif"], fallback="crest_Wm.tif")
    b_file = _select_file(crest_abs, ["crest_b*.tif"], fallback="crest_b.tif")
    fc_file = _select_file(crest_abs, ["crest_Fc*.tif", "crest_fc*.tif"], fallback="crest_Fc_Ksat.tif")
    im_file = _select_file(crest_abs, ["crest_im*.tif", "crest_Im*.tif", "crest_IM*.tif", "*_IM_final.tif", "*_IM*.tif"], fallback=None)

    alpha_file = _select_file(
        kw_abs,
        ["KW_alpha*.tif", "kw_alpha*.tif"],
        exclude_tokens=["alpha0"],
    )
    beta_file = _select_file(kw_abs, ["KW_beta*.tif", "kw_beta*.tif"])
    alpha0_file = _select_file(kw_abs, ["kw_alpha0*.tif", "KW_alpha0*.tif"], fallback="kw_alpha0.tif")

    wm_path = os.path.join(crest_abs, wm_file) if wm_file else ""
    b_path = os.path.join(crest_abs, b_file) if b_file else ""
    fc_path = os.path.join(crest_abs, fc_file) if fc_file else ""
    im_path = os.path.join(crest_abs, im_file) if im_file else ""
    alpha_path = os.path.join(kw_abs, alpha_file) if alpha_file else ""
    beta_path = os.path.join(kw_abs, beta_file) if beta_file else ""
    alpha0_path = os.path.join(kw_abs, alpha0_file) if alpha0_file else ""

    if not wm_file or not os.path.isfile(wm_path):
        raise FileNotFoundError(f"Missing CREST wm file in: {crest_abs}")
    if not b_file or not os.path.isfile(b_path):
        raise FileNotFoundError(f"Missing CREST b file in: {crest_abs}")
    if not fc_file or not os.path.isfile(fc_path):
        raise FileNotFoundError(f"Missing CREST fc file in: {crest_abs}")
    if not im_file or not os.path.isfile(im_path):
        print(f"    Warning: Missing CREST im file in: {crest_abs} (impervious layer not available)")
        im_file = ""
        im_path = ""
    if not alpha_file or not os.path.isfile(alpha_path):
        raise FileNotFoundError(f"Missing KW alpha file in: {kw_abs}")
    if not beta_file or not os.path.isfile(beta_path):
        raise FileNotFoundError(f"Missing KW beta file in: {kw_abs}")
    if not alpha0_file or not os.path.isfile(alpha0_path):
        raise FileNotFoundError(f"Missing KW alpha0 file in: {kw_abs}")

    return {
        "dem": dem,
        "ddm": ddm,
        "fam": fam,
        "wm_crest": f"{crest_folder}/{wm_file}",
        "b_crest": f"{crest_folder}/{b_file}",
        "im_crest": f"{crest_folder}/{im_file}" if im_file else "",
        "fc_crest": f"{crest_folder}/{fc_file}",
        "alpha_kw": f"{kw_folder}/{alpha_file}",
        "beta_kw": f"{kw_folder}/{beta_file}",
        "alpha0_kw": f"{kw_folder}/{alpha0_file}",
    }


def _load_basin_lines(templatePath, region_name, model_resolution):
    basin_file = os.path.join(
        templatePath,
        "basin_list",
        f"{region_name}_{model_resolution}_basin_new.txt",
    )
    if not os.path.isfile(basin_file):
        raise FileNotFoundError(f"Missing basin list file: {os.path.abspath(basin_file)}")

    with open(basin_file, "r") as basin_fh:
        basin_lines = basin_fh.readlines()

    return [line if line.endswith("\n") else line + "\n" for line in basin_lines]


def _inject_gauge_basin_block(lines, basin_lines):
    start_marker = "#---Start Gauge-Basin Block"
    end_marker = "#---End Gauge-Basin Block"
    rendered = []
    idx = 0
    inserted = False

    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        rendered.append(line)

        if stripped == start_marker:
            rendered.extend(basin_lines)
            inserted = True
            idx += 1
            while idx < len(lines) and lines[idx].strip() != end_marker:
                idx += 1
            continue

        idx += 1

    if not inserted:
        raise ValueError("Gauge-Basin markers were not found in template.")

    return rendered


def _apply_hsaf_control_overrides(lines, precip_forcing_loc):
    """Adjust generated EF5 control lines for HSAF forcing.

    - Comment the full IMERG forcing block.
    - Insert HSAF forcing block right after IMERG block.
    - In Task Simulation_QPE and Task Simulation_QPF, switch PRECIP to HSAF and TIMESTEP to 10u.
    """
    out = []
    i = 0
    inserted_hsaf_block = False
    in_qpe_task = False
    in_qpf_task = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Track whether we are inside Task Simulation_QPE block.
        if stripped == "[Task Simulation_QPE]":
            in_qpe_task = True
            in_qpf_task = False
        elif stripped == "[Task Simulation_QPF]":
            in_qpf_task = True
            in_qpe_task = False
        elif stripped.startswith("[") and stripped != "[Task Simulation_QPE]":
            in_qpe_task = False
            in_qpf_task = False

        # Comment IMERG forcing block and add HSAF block below it.
        if stripped == "[PrecipForcing IMERG]":
            while i < len(lines):
                block_line = lines[i]
                block_stripped = block_line.strip()
                if i > 0 and block_stripped.startswith("[") and block_stripped != "[PrecipForcing IMERG]":
                    break
                if block_line.lstrip().startswith("#"):
                    out.append(block_line)
                else:
                    out.append("#" + block_line)
                i += 1

            if not inserted_hsaf_block:
                out.extend([
                    "[PrecipForcing HSAF]\n",
                    "TYPE=TIF\n",
                    "UNIT=mm/h\n",
                    "FREQ=10u\n",
                    f"LOC={precip_forcing_loc}\n",
                    "NAME=h40_YYYYMMDD_HHUU_fdk.tif\n",
                    "\n",
                ])
                inserted_hsaf_block = True
            continue

        if (in_qpe_task or in_qpf_task) and stripped.startswith("PRECIP="):
            out.append("PRECIP=HSAF\n")
            i += 1
            continue

        if (in_qpe_task or in_qpf_task) and stripped.startswith("TIMESTEP="):
            out.append("TIMESTEP=10u\n")
            i += 1
            continue

        out.append(line)
        i += 1

    return out


def _apply_scampr_control_overrides(lines, precip_forcing_loc):
    """Adjust generated EF5 control lines for SCaMPR forcing.

    Mirrors the HSAF override logic:
    - Comment the full IMERG forcing block.
    - Insert a SCaMPR forcing block right after.
    - In Task Simulation_QPE / _QPF switch PRECIP=SCaMPR and TIMESTEP=10u.
    """
    out = []
    i = 0
    inserted_scampr_block = False
    in_qpe_task = False
    in_qpf_task = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped == "[Task Simulation_QPE]":
            in_qpe_task = True
            in_qpf_task = False
        elif stripped == "[Task Simulation_QPF]":
            in_qpf_task = True
            in_qpe_task = False
        elif stripped.startswith("[") and stripped not in ("[Task Simulation_QPE]", "[Task Simulation_QPF]"):
            in_qpe_task = False
            in_qpf_task = False

        if stripped == "[PrecipForcing IMERG]":
            # Comment the IMERG block out.
            while i < len(lines):
                block_line = lines[i]
                block_stripped = block_line.strip()
                if i > 0 and block_stripped.startswith("[") and block_stripped != "[PrecipForcing IMERG]":
                    break
                out.append(block_line if block_line.lstrip().startswith("#") else "#" + block_line)
                i += 1
            if not inserted_scampr_block:
                out.extend([
                    "[PrecipForcing SCaMPR]\n",
                    "TYPE=TIF\n",
                    "UNIT=mm/h\n",
                    "FREQ=10u\n",
                    f"LOC={precip_forcing_loc}\n",
                    "NAME=scampr.qpe.YYYYMMDDHHUU.mmhInst.tif\n",
                    "\n",
                ])
                inserted_scampr_block = True
            continue

        if (in_qpe_task or in_qpf_task) and stripped.startswith("PRECIP="):
            out.append("PRECIP=SCaMPR\n")
            i += 1
            continue

        if (in_qpe_task or in_qpf_task) and stripped.startswith("TIMESTEP="):
            out.append("TIMESTEP=10u\n")
            i += 1
            continue

        out.append(line)
        i += 1

    return out

def rename_ef5_precip(precipEF5Folder, precipFolder, qpe_source="IMERG"):
    """
    Copy precipitation TIFs into precipEF5Folder to be ingested by EF5.
    Clears precipEF5Folder first so files from a previous run (different QPE
    source or different cycle) never mix with the current run's files.
    For IMERG: scans precipFolder only.
    For HSAF:  also scans precipFolder/_hsaf_raw/ (converted TIFs live there).
    For SCaMPR: also scans precipFolder/_scampr_raw/ (converted TIFs live there).
    """
    # Clear the folder before populating it so no stale files remain.
    for stale in glob.glob(os.path.join(precipEF5Folder, "*.tif")):
        try:
            os.remove(stale)
        except Exception as e:
            print(f"Warning: could not remove stale precipEF5 file {stale}: {e}")

    search_dirs = [precipFolder]
    qpe_upper = str(qpe_source).upper()
    if qpe_upper == "HSAF":
        hsaf_raw = os.path.join(precipFolder, "_hsaf_raw")
        if os.path.isdir(hsaf_raw):
            search_dirs.append(hsaf_raw)
    # SCaMPR: get_new_scampr_precip already copies properly-named TIFs into
    # precipFolder.  The _scampr_raw/ subfolder holds intermediate conversion
    # artefacts (RRQPE-INST-GLB-*.tif) that must NOT be staged for EF5.

    for search_dir in search_dirs:
        for filename in os.listdir(search_dir):
            if filename.endswith('.tif'):
                source_file = os.path.join(search_dir, filename)
                dest_file = os.path.join(precipEF5Folder, filename)
                try:
                    shutil.copy(source_file, dest_file)
                except PermissionError as e:
                    print(f"PermissionError: {e}")
    for filename2 in os.listdir(precipEF5Folder):
        if 'qpf' in filename2 and filename2.endswith('.tif'):
            new_filename = filename2.replace('qpf', 'qpe')
            source_file = os.path.join(precipEF5Folder, filename2)
            dest_file = os.path.join(precipEF5Folder, new_filename)
            try:
                os.rename(source_file, dest_file)
            except PermissionError as e:
                print(f"PermissionError: {e}")


def _imerg_files_present(precipFolder, start, end):
    """Return True if IMERG 30-min TIF files exist locally for the full [start, end] range."""
    delta = timedelta(minutes=30)
    current = start
    while current <= end:
        fname = f"imerg.qpe.{current.strftime('%Y%m%d%H%M')}.30minAccum.tif"
        if not os.path.isfile(os.path.join(precipFolder, fname)):
            return False
        current += delta
    return True


def find_available_states(statesPath, modelStates, systemStartTime, failTime):
    """
    Look for the set of most recent states available.

    Searches backward from systemStartTime down to failTime (default: 7 days).
    Returns the newest timestamp for which all required state files exist.
    """
    foundAllStates = False
    realSystemStartTime = systemStartTime

    # Iterate over all necessary states and check if they're available for the current run
    # Go back up to failTime (7 days), in 30min decrements
    while not foundAllStates and realSystemStartTime > failTime:
        foundAllStates = True
        for state in modelStates:
            state_path = f"{statesPath}{state}_{realSystemStartTime.strftime('%Y%m%d_%H%M')}.tif"
            if not is_non_zero_file(state_path):
                foundAllStates = False
        if not foundAllStates:
            realSystemStartTime -= timedelta(minutes=30)

    return foundAllStates, realSystemStartTime


def send_state_alerts(foundAllStates,realSystemStartTime,systemStartTime,currentTime,systemName,SEND_ALERTS,alert_recipients, smtp_config):
    """
    Sends alert emails if necessary based on the availability of model states.

    Args:
        foundAllStates (bool): whether all required states were found
        realSystemStartTime (datetime): actual start time used for the simulation
        systemStartTime (datetime): originally planned system start time
        currentTime (datetime): current system time
        systemName (str): name of the system sending the alert
        SEND_ALERTS (bool): whether to send email alerts or not
        alert_recipients (list): list of email addresses to notify
        smtp_config (dict): configuration dictionary containing:
            - smtp_server (str)
            - smtp_port (int)
            - account_address (str)
            - account_password (str)
            - alert_sender (str)
    """
    # Exit early if email alerts are disabled
    if not SEND_ALERTS:
        return

    # If no valid states were found, notify about a cold start
    if not foundAllStates:
        subject = f"{systemName} failed for {currentTime.strftime('%Y%m%d_%H%M')}"
        message = (
            f"Missing states from {realSystemStartTime.strftime('%Y%m%d_%H%M')} "
            f"to {systemStartTime.strftime('%Y%m%d_%H%M')}. Starting model with cold states."
        )
    
    # If older states had to be used, notify about it
    elif realSystemStartTime != systemStartTime:
        subject = f"{systemName} warning for {currentTime.strftime('%Y%m%d_%H%M')}"
        message = (
            f"Using states from {realSystemStartTime.strftime('%Y%m%d_%H%M')} "
            f"instead of {systemStartTime.strftime('%Y%m%d_%H%M')}."
        )
    
    # If states were found and up to date, no alert needed
    else:
        return

    # Send the email to each recipient in the list
    for recipient in alert_recipients:
        send_mail(
            smtp_server=smtp_config['smtp_server'],
            smtp_port=smtp_config['smtp_port'],
            account_address=smtp_config['account_address'],
            account_password=smtp_config['account_password'],
            sender=smtp_config['alert_sender'],
            to=recipient,
            subject=subject,
            text=message
        )

def write_control_file(
    tmpOutput,
    subdomain,
    systemModel,
    templatePath,
    template,
    statesPath,
    realSystemStartTime,
    systemStartLRTime,
    systemWarmEndTime,
    systemStateEndTime,
    systemEndTime,
    LR_TimeStep,
    LR_run,
    statesFound,
    region_name,
    model_resolution,
    basicPath,
    parametersPath,
    precip_forcing_loc,
    qpf_store_forcing_path,
    qpe_source="IMERG",
    qpf_source="GFS",
    save_states=True,
):
    rmtree(tmpOutput, ignore_errors=1)
    mkdir_p(tmpOutput)

    controlFile = os.path.join(tmpOutput, f"WA_{subdomain}_{systemModel}.txt")
    template_file = os.path.join(templatePath, template)

    if not os.path.isfile(template_file):
        raise FileNotFoundError(f"Template file not found: {os.path.abspath(template_file)}")

    region_placeholders = _resolve_region_paths(region_name, model_resolution, basicPath, parametersPath)
    basin_lines = _load_basin_lines(templatePath, region_name, model_resolution)

    output_path = _with_trailing_slash(tmpOutput)
    states_path = _with_trailing_slash(statesPath)
    precip_loc = _with_trailing_slash(precip_forcing_loc)
    qpf_loc = _with_trailing_slash(qpf_store_forcing_path)

    substitutions = {
        "{OUTPUTPATH}": output_path,
        "{STATESPATH}": states_path,
        "{TIMEBEGIN}": realSystemStartTime.strftime("%Y%m%d%H%M"),
        "{TIMEWARMEND}": systemWarmEndTime.strftime("%Y%m%d%H%M"),
        "{TIMESTATE}": systemStateEndTime.strftime("%Y%m%d%H%M"),
        "{TIMEEND}": systemEndTime.strftime("%Y%m%d%H%M"),
        "{TIMEBEGINLR}": systemStartLRTime.strftime("%Y%m%d%H%M"),
        "{TIMESTEPLR}": LR_TimeStep,
        "{SYSTEMMODEL}": systemModel,
    }
    for key, value in region_placeholders.items():
        substitutions[f"{{{key}}}"] = _as_posix(value)

    with open(template_file, "r") as template_fh:
        raw_lines = template_fh.readlines()

    raw_lines = _inject_gauge_basin_block(raw_lines, basin_lines)

    rendered_lines = []
    for line in raw_lines:
        for token, value in substitutions.items():
            if token in line:
                line = line.replace(token, value)

        if "LOC=precipEF5/" in line:
            line = line.replace("LOC=precipEF5/", f"LOC={precip_loc}")
        if "LOC=qpf_store/gfs_data/" in line:
            line = line.replace("LOC=qpf_store/gfs_data/", f"LOC={qpf_loc}gfs_data/")
        if "LOC=qpf_store/wrf_data/" in line:
            line = line.replace("LOC=qpf_store/wrf_data/", f"LOC={qpf_loc}wrf_data/")
        if "LOC=qpf_store/arome_data/" in line:
            line = line.replace("LOC=qpf_store/arome_data/", f"LOC={qpf_loc}arome_data/")
        # StormLab: qpf_store_forcing_path is already the member folder
        # (…/stormlab_data/ensQn/ or a direct ensQ path).
        if "LOC=qpf_store/stormlab_data/" in line:
            if str(qpf_source).upper() == "STORMLAB":
                line = line.replace("LOC=qpf_store/stormlab_data/", f"LOC={qpf_loc}")
            else:
                line = line.replace(
                    "LOC=qpf_store/stormlab_data/", f"LOC={qpf_loc}stormlab_data/")

        if "task=Simulation_QPE" in line:
            if LR_run:
                line = "#task=Simulation_QPE\n"
        elif "task=Simulation_QPF" in line:
            if LR_run:
                line = "task=Simulation_QPF\n"
            else:
                line = "#task=Simulation_QPF\n"

        if LR_run and "PRECIPFORECAST=" in line and not line.lstrip().startswith('#'):
            line = re.sub(r'PRECIPFORECAST=\w+', f'PRECIPFORECAST={qpf_source.upper()}', line)

        # Comment TIME_WARMEND when states exist (normal QPE run) OR for any LR run
        # (no QPE warm-up in LR runs — the model loads the prior state directly).
        if (statesFound or LR_run) and "TIME_WARMEND=" in line:
            if not line.lstrip().startswith('#'):
                line = "#" + line

        # When save_states=False (e.g. AROME secondary run) suppress state writes
        # so the primary run's (GFS) states are not overwritten.
        if not save_states and "TIME_STATE=" in line:
            if not line.lstrip().startswith('#'):
                line = "#" + line

        rendered_lines.append(line)

    if str(qpe_source).upper() == "HSAF":
        rendered_lines = _apply_hsaf_control_overrides(rendered_lines, precip_loc)
    elif str(qpe_source).upper() == "SCAMPR":
        rendered_lines = _apply_scampr_control_overrides(rendered_lines, precip_loc)
    elif str(qpe_source).upper() == "STREAM_SAT":
        rendered_lines = _apply_streamsat_control_overrides(rendered_lines, precip_loc)
    elif str(qpe_source).upper() == "STORMLAB":
        rendered_lines = _apply_stormlab_control_overrides(rendered_lines, precip_loc)

    with open(controlFile, "w") as out_fh:
        out_fh.writelines(rendered_lines)

    return controlFile


def _apply_stormlab_control_overrides(lines, precip_forcing_loc):
    """StormLab as normal QPE (no long-range / PRECIPFORECAST).

    - Comment IMERG block.
    - Point template [PrecipForcing STORMLAB] LOC at staged precipEF5.
    - Simulation_QPE: PRECIP=STORMLAB, TIMESTEP=60u.
    - Execute QPE only (caller sets LR_run=False).
    """
    has_stormlab_block = any(
        ln.strip() == "[PrecipForcing STORMLAB]" for ln in lines
    )
    out = []
    i = 0
    inserted_block = False
    in_qpe_task = False
    in_qpf_task = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped == "[Task Simulation_QPE]":
            in_qpe_task = True
            in_qpf_task = False
        elif stripped == "[Task Simulation_QPF]":
            in_qpf_task = True
            in_qpe_task = False
        elif stripped.startswith("[") and stripped not in (
            "[Task Simulation_QPE]", "[Task Simulation_QPF]"
        ):
            in_qpe_task = False
            in_qpf_task = False

        if stripped == "[PrecipForcing IMERG]":
            while i < len(lines):
                block_line = lines[i]
                block_stripped = block_line.strip()
                if i > 0 and block_stripped.startswith("[") and block_stripped != "[PrecipForcing IMERG]":
                    break
                out.append(block_line if block_line.lstrip().startswith("#") else "#" + block_line)
                i += 1
            # Only inject a STORMLAB block if the template does not already have one
            if not has_stormlab_block and not inserted_block:
                out.extend([
                    "[PrecipForcing STORMLAB]\n",
                    "TYPE=TIF\n",
                    "UNIT=mm/h\n",
                    "FREQ=1h\n",
                    f"LOC={precip_forcing_loc}\n",
                    "NAME=stormlab.YYYYMMDDHH00.tif\n",
                    "\n",
                ])
                inserted_block = True
            continue

        if stripped == "[PrecipForcing STORMLAB]":
            out.append(line)
            i += 1
            while i < len(lines):
                block_line = lines[i]
                block_stripped = block_line.strip()
                if block_stripped.startswith("[") and block_stripped != "[PrecipForcing STORMLAB]":
                    break
                if block_stripped.startswith("LOC="):
                    out.append(f"LOC={precip_forcing_loc}\n")
                else:
                    out.append(block_line)
                i += 1
            inserted_block = True
            continue

        if (in_qpe_task or in_qpf_task) and stripped.startswith("PRECIP="):
            out.append("PRECIP=STORMLAB\n")
            i += 1
            continue

        if (in_qpe_task or in_qpf_task) and stripped.startswith("TIMESTEP="):
            out.append("TIMESTEP=60u\n")
            i += 1
            continue

        out.append(line)
        i += 1

    return out


def _apply_streamsat_control_overrides(lines, precip_forcing_loc):
    """Adjust generated EF5 control lines for STREAM-Sat forcing.

    - Comment the full IMERG forcing block.
    - Insert STREAM-Sat forcing block right after IMERG block.
    - In Task Simulation_QPE and Task Simulation_QPF, switch PRECIP to STREAM_SAT
      and TIMESTEP to 30u (half-hourly, mm/h).
    - STREAM-Sat files are named: streamsat.qpe.YYYYMMDDHHUU.mmhInst.tif
    """
    out = []
    i = 0
    inserted_block = False
    in_qpe_task = False
    in_qpf_task = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped == "[Task Simulation_QPE]":
            in_qpe_task = True
            in_qpf_task = False
        elif stripped == "[Task Simulation_QPF]":
            in_qpf_task = True
            in_qpe_task = False
        elif stripped.startswith("[") and stripped not in ("[Task Simulation_QPE]", "[Task Simulation_QPF]"):
            in_qpe_task = False
            in_qpf_task = False

        if stripped == "[PrecipForcing IMERG]":
            while i < len(lines):
                block_line = lines[i]
                block_stripped = block_line.strip()
                if i > 0 and block_stripped.startswith("[") and block_stripped != "[PrecipForcing IMERG]":
                    break
                out.append(block_line if block_line.lstrip().startswith("#") else "#" + block_line)
                i += 1
            if not inserted_block:
                out.extend([
                    "[PrecipForcing STREAM_SAT]\n",
                    "TYPE=TIF\n",
                    "UNIT=mm/h\n",
                    "FREQ=30u\n",
                    f"LOC={precip_forcing_loc}\n",
                    "NAME=streamsat.qpe.YYYYMMDDHHUU.mmhInst.tif\n",
                    "\n",
                ])
                inserted_block = True
            continue

        if (in_qpe_task or in_qpf_task) and stripped.startswith("PRECIP="):
            out.append("PRECIP=STREAM_SAT\n")
            i += 1
            continue

        if (in_qpe_task or in_qpf_task) and stripped.startswith("TIMESTEP="):
            out.append("TIMESTEP=30u\n")
            i += 1
            continue

        out.append(line)
        i += 1

    return out

def _detect_container_runtime():
    """Detect which container runtime is available.

    Returns ``"local"``, ``"apptainer"``, ``"singularity"``, or ``"docker"``.

    Preference when unset: Apptainer > Singularity > Docker.
    Override with ``EF5_RUNTIME``.

    ``local`` runs the glibc EF5 binary shipped under ``EF5/bin/ef5``
    (or ``EF5_LOCAL_BIN``) inside the *current* container — required for
    Apptainer partners because nesting Apptainer→Apptainer is unreliable
    on HPC (setuid / session dirs).
    """
    forced = os.environ.get("EF5_RUNTIME", "").strip().lower()
    if forced in ("local", "embedded", "apptainer", "singularity", "docker"):
        return "local" if forced == "embedded" else forced

    for cmd in ("apptainer", "singularity"):
        if shutil.which(cmd):
            return cmd
    if shutil.which("docker"):
        return "docker"
    # Last resort: local binary if present
    for candidate in (
        os.environ.get("EF5_LOCAL_BIN", "").strip(),
        "EF5/bin/ef5",
        "/ef5/bin/ef5",
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return "local"
    raise RuntimeError(
        "No EF5 runtime found. Set EF5_RUNTIME=local|docker|apptainer "
        "or install Docker/Apptainer."
    )


def _resolve_local_ef5_bin(ef5Path: str) -> str:
    """Resolve the in-container glibc EF5 binary path."""
    candidates = [
        os.environ.get("EF5_LOCAL_BIN", "").strip(),
        ef5Path if ef5Path and not ef5Path.endswith((".sif", ".tar")) else "",
        "EF5/bin/ef5",
        "/ef5/bin/ef5",
        "/app/EF5/bin/ef5",
    ]
    for c in candidates:
        if not c:
            continue
        path = c if os.path.isabs(c) else os.path.abspath(c)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise RuntimeError(
        "EF5_RUNTIME=local but no glibc EF5 binary found. Expected "
        "EF5/bin/ef5 (build with: ./EF5/docker/build_ef5_local.sh)."
    )


def run_EF5(ef5Path, hot_folder_path, control_file, log_file):
    """
    Run EF5 inside a container (Docker or Apptainer/Singularity) **or**
    as a local glibc binary embedded/bound into the TITO container.

    The container mounts the current working directory (TITO_Stream_Sat/)
    as ``/data`` and executes ``/ef5/bin/ef5`` with the supplied control
    file.  All paths in the control file must be relative to
    TITO_Stream_Sat/ so they resolve correctly under ``/data/``.

    Auto-detects Docker vs Apptainer/Singularity unless ``EF5_RUNTIME``
    is set in the environment.

    Arguments:
        ef5Path {str} -- Docker image name (e.g. "ef5-container:latest"),
                         SIF path ("EF5/ef5-container.sif"), local binary
                         ("EF5/bin/ef5"), or URI.
        hot_folder_path {str} -- Path to the current run's "hot" folder
        control_file {str} -- Path to the EF5 control file
        log_file {str} -- Log file name (created inside hot_folder_path)
    """
    cwd = os.path.abspath(os.getcwd())

    control_abs = os.path.abspath(control_file)
    log_abs = os.path.abspath(os.path.join(hot_folder_path, log_file))

    try:
        control_rel = os.path.relpath(control_abs, cwd)
    except ValueError:
        control_rel = control_abs.lstrip(os.sep)

    os.makedirs(os.path.dirname(log_abs), exist_ok=True)

    runtime = _detect_container_runtime()
    omp_threads = int(os.environ.get("EF5_OMP_NUM_THREADS", "1"))

    if runtime == "local":
        # Run EF5 binary inside the *current* TITO container/process tree.
        # No nested Docker/Apptainer — this is the Apptainer partner path.
        # stderr→log hides libgomp / TIFF codec noise from the console.
        ef5_bin = _resolve_local_ef5_bin(ef5Path)
        cmd = (
            f"OMP_NUM_THREADS={omp_threads} OMP_DYNAMIC=false "
            f"OMP_NESTED=false OMP_THREAD_LIMIT=1 OMP_PROC_BIND=false "
            f'"{ef5_bin}" "{control_rel}" '
            f'> "{log_abs}" 2>&1'
        )
        return subprocess.call(cmd, shell=True, cwd=cwd)

    if runtime == "docker":
        # When TITO itself runs in Docker and spawns EF5 via docker.sock,
        # volume paths are resolved on the *host*.  cwd inside the TITO
        # container is typically /app, which is not the host project path.
        # tito-run.sh / compose must set TITO_HOST_PROJECT to the host abs path.
        # OMP_PROC_BIND=false avoids "libgomp: Affinity not supported" spam.
        # stderr redirected to log (2>&1) so TIFF codec warnings stay off console.
        host_project = os.environ.get("TITO_HOST_PROJECT", "").strip() or cwd
        cmd = (
            f"docker run --rm "
            f"--network host "
            f"--ipc host "
            f"--shm-size=32g "
            f"--ulimit nofile=1048576:1048576 "
            f"--ulimit nproc=65535:65535 "
            f"--ulimit memlock=-1:-1 "
            f"--security-opt seccomp=unconfined "
            f'-v "{host_project}:/data:rw" '
            f'-u "$(id -u):$(id -g)" '
            f"-e OMP_NUM_THREADS={omp_threads} "
            f"-e OMP_PROC_BIND=false "
            f"-e OMP_DYNAMIC=false "
            f"-w /data "
            f'"{ef5Path}" '
            f'/ef5/bin/ef5 "/data/{control_rel}" '
            f'> "{log_abs}" 2>&1'
        )
        return subprocess.call(cmd, shell=True)

    # Apptainer / Singularity (host-level only — do NOT use from inside a SIF)
    runtime_bin = os.environ.get("EF5_APPTAINER_BIN", "").strip() or runtime
    if not shutil.which(runtime_bin) and not os.path.isfile(runtime_bin):
        raise RuntimeError(
            f"EF5 runtime '{runtime_bin}' not found. "
            "Inside a TITO Apptainer container use EF5_RUNTIME=local "
            "(./tito-run.sh sets this). On the host, install Apptainer."
        )
    image = ef5Path
    if "://" not in ef5Path and not os.path.isabs(ef5Path):
        image = os.path.abspath(ef5Path)

    cmd = (
        f'"{runtime_bin}" run --cleanenv '
        f'--env "OMP_NUM_THREADS=1,OMP_DYNAMIC=false,OMP_NESTED=false,OMP_THREAD_LIMIT=1" '
        f'--bind "{cwd}:/data" '
        f"--pwd /data "
        f'"{image}" '
        f'/ef5/bin/ef5 "/data/{control_rel}" '
        f'> "{log_abs}"'
    )
    return subprocess.call(cmd, shell=True)


def _rename_outputs_with_timestamp(hot_folder_path: str, timestamp_str: str) -> None:
    """Rename EF5 outputs in the hot folder to use a unified timestamp.

    - maxq.*, maxunitq.*, qpeaccum.*, qpfaccum.* -> base.{timestamp}.tif
    - ts.*.csv -> ts.*.{timestamp}.csv
    Log file naming is handled via the EF5 invocation (redirect target).
    """
    bases = ["maxq", "maxunitq", "qpeaccum", "qpfaccum", "maxsm", "maxdepth"]
    for base in bases:
        pattern = os.path.join(hot_folder_path, f"{base}.*.tif")
        matches = sorted(glob.glob(pattern))
        if not matches:
            continue
        # Prefer the newest file in case multiple exist
        latest = max(matches, key=lambda p: os.path.getmtime(p))
        new_name = os.path.join(hot_folder_path, f"{base}.{timestamp_str}.tif")
        try:
            if os.path.abspath(latest) != os.path.abspath(new_name):
                if os.path.exists(new_name):
                    os.remove(new_name)
                os.rename(latest, new_name)
        except Exception as e:
            print(f"Warning: could not rename {latest} -> {new_name}: {e}")

    # Timeseries CSVs
    for csv_path in glob.glob(os.path.join(hot_folder_path, "ts.*.csv")):
        root, ext = os.path.splitext(csv_path)
        new_name = f"{root}.{timestamp_str}{ext}"
        try:
            if os.path.abspath(csv_path) != os.path.abspath(new_name):
                if os.path.exists(new_name):
                    os.remove(new_name)
                os.rename(csv_path, new_name)
        except Exception as e:
            print(f"Warning: could not rename {csv_path} -> {new_name}: {e}")


def run_ef5_simulation(ef5Path, tmpOutput, controlFile, output_timestamp_str):
    # Use timestamped log name
    log_name = f"ef5.{output_timestamp_str}.log"
    t0 = time.time()
    exit_code = run_EF5(ef5Path, tmpOutput, controlFile, log_name)
    elapsed = time.time() - t0
    if exit_code != 0:
        raise RuntimeError(f"EF5 exited with code {exit_code} for control file {controlFile}")

    # Rename generated outputs to use the requested timestamp
    _rename_outputs_with_timestamp(tmpOutput, output_timestamp_str)
    return elapsed


def run_ef5_simulations_parallel(simulation_jobs, max_workers=None):
    """Run EF5 jobs in parallel. Returns list of per-job timing dicts."""
    if not simulation_jobs:
        return []

    workers = max_workers if max_workers else min(len(simulation_jobs), max(1, (os.cpu_count() or 1)))
    errors = []
    timings = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                run_ef5_simulation,
                job["ef5Path"],
                job["tmpOutput"],
                job["controlFile"],
                job["output_timestamp_str"],
            ): job
            for job in simulation_jobs
        }

        for future in as_completed(future_map):
            job = future_map[future]
            label = job.get("region", "unknown")
            if job.get("member") is not None:
                label = f"{label}/ens{int(job['member']):02d}"
            if job.get("stormlab_member") is not None:
                label = f"{label}/sl{int(job['stormlab_member']):02d}"
            try:
                elapsed = future.result()
                timings.append({"label": label, "seconds": float(elapsed or 0.0), "ok": True})
            except Exception as exc:
                errors.append((label, str(exc)))
                timings.append({"label": label, "seconds": 0.0, "ok": False, "error": str(exc)})

    if errors:
        details = "; ".join([f"{region}: {message}" for region, message in errors])
        raise RuntimeError(f"One or more EF5 runs failed: {details}")

    return timings

 
def prepare_ef5(precipEF5Folder, precipFolder, statesPath, modelStates,
    systemStartTime, failTime, currentTime, systemName, SEND_ALERTS,
    alert_recipients, smtp_config, tmpOutput, dataPath,
    subdomain, systemModel, templatePath, template, systemStartLRTime,
    systemWarmEndTime, systemStateEndTime, systemEndTime, LR_TimeStep, LR_run,
    region_name, model_resolution, basicPath, parametersPath, qpe_source="IMERG", qpf_source="GFS",
    stage_precip=True, output_timestamp_str=None, qpf_store_forcing_path="EF5_conf/qpf_store/",
    save_states=True, cold_start_begin_time=None, cold_start_warm_end_time=None,
    imerg_download_params=None, verbose=True, run_log=None):
    """Prepare EF5 control file and stage precipitation.

    Parameters
    ----------
    verbose : bool
        If False, suppress terminal output (state search, control file writing).
    run_log : logging.Logger or None
        If provided, detailed diagnostics are written here instead of print().
    """

    def _say(msg):
        """Write to run_log if available, else print (when verbose)."""
        if run_log:
            run_log.info(msg)
        elif verbose:
            print(msg)

    # Check to see if all the states for the current time step are available
    foundAllStates, realSystemStartTime = find_available_states(statesPath, modelStates, systemStartTime, failTime)

    if foundAllStates:
        _say(f"    States found at {realSystemStartTime.strftime('%Y%m%d_%H%M')}")
    else:
        _say("    No active states found within last 7 days — cold start")

    # send alerts if needed
    send_state_alerts(foundAllStates, realSystemStartTime, systemStartTime,
                      currentTime, systemName, SEND_ALERTS,
                      alert_recipients, smtp_config)

    control_start_time = realSystemStartTime
    control_warm_end_time = systemWarmEndTime
    if not foundAllStates:
        if cold_start_begin_time is not None:
            control_start_time = cold_start_begin_time
        if cold_start_warm_end_time is not None:
            control_warm_end_time = cold_start_warm_end_time

    # ── IMERG backfill when simulation start is older than pre-downloaded window ─────
    # Determine the effective simulation start time:
    #   - If states were found: realSystemStartTime (the state timestamp)
    #   - If cold start: cold_start_begin_time (user-specified start)
    # Then, if this start is older than the initial IMERG window, fetch the missing
    # IMERG files so EF5 always has forcing from its actual start time.
    if imerg_download_params is not None:
        _init_imerg_end = imerg_download_params["initial_imerg_end"]
        if foundAllStates:
            _sim_start_for_backfill = realSystemStartTime
        elif cold_start_begin_time is not None:
            _sim_start_for_backfill = cold_start_begin_time
        else:
            _sim_start_for_backfill = None

        if _sim_start_for_backfill is not None and _sim_start_for_backfill < _init_imerg_end - timedelta(minutes=30):
            _dl_start = _sim_start_for_backfill - timedelta(minutes=30)
            _dl_end   = _init_imerg_end - timedelta(minutes=30)
            # Skip download if all required files already exist locally (e.g., from Phase 1 shared pre-download)
            if _imerg_files_present(imerg_download_params["precipFolder"], _dl_start, _dl_end):
                print(f"    IMERG already present for {_dl_start.strftime('%Y%m%d_%H%M')} → "
                      f"{_dl_end.strftime('%Y%m%d_%H%M')} UTC, skipping backfill")
            else:
                _say(f"    Backfilling IMERG files from {_dl_start.strftime('%Y%m%d_%H%M')} "
                      f"to {_dl_end.strftime('%Y%m%d_%H%M')} UTC "
                      f"(simulation starts at {_sim_start_for_backfill.strftime('%Y%m%d_%H%M')})")
                from tito_utils.qpe_utils import get_gpm_files
                try:
                    get_gpm_files(
                        imerg_download_params["precipFolder"],
                        _dl_start, _dl_end,
                        imerg_download_params["server"],
                        imerg_download_params["email_gpm"],
                        imerg_download_params["xmin"],
                        imerg_download_params["ymin"],
                        imerg_download_params["xmax"],
                        imerg_download_params["ymax"],
                    )
                except Exception as _dl_exc:
                    _say(f"    Warning: IMERG backfill failed ({_dl_exc}). "
                          f"EF5 may fail or produce degraded results.")

    # Copy precipitation files into staging folder only once when requested.
    if stage_precip:
        rename_ef5_precip(precipEF5Folder, precipFolder, qpe_source)
    else:
        _say(f"    Reusing staged precip folder: {precipEF5Folder}")

    _say("    Writing control file.")

    # Keep each run in a timestamped subfolder based on the orchestrator trigger time.
    if not output_timestamp_str:
        output_timestamp_str = currentTime.strftime("%Y%m%d.%H%M%S")
    run_output_path = os.path.join(tmpOutput, output_timestamp_str)

    controlFile = write_control_file(
        run_output_path,
        subdomain,
        systemModel,
        templatePath,
        template,
        statesPath,
        control_start_time,
        systemStartLRTime,
        control_warm_end_time,
        systemStateEndTime,
        systemEndTime,
        LR_TimeStep,
        LR_run,
        foundAllStates,
        region_name,
        model_resolution,
        basicPath,
        parametersPath,
        precipEF5Folder,
        qpf_store_forcing_path,
        qpe_source,
        qpf_source,
        save_states=save_states,
    )

    """
    # If data assimilation if being used for CREST, clean up previous data assimilation logs
    #To do: Verify against EF5 control file - when this functionality is needed
    if DATA_ASSIMILATION and systemModel=="crest":
        # Data assimilation output files
        for log in assimilationLogs:
            if is_non_zero_file(assimilationPath + log) == True:
                remove(assimilationPath + log)
    """
    return control_start_time, controlFile, run_output_path

domain = "Caribbean_Comoros"
subdomain = "Regional"
# Console logging for operators vs developers:
#   "user"  = simplified progress (default)
#   "debug" = full developer diagnostics
# Override anytime:  export TITO_CONSOLE_VERBOSITY=debug
#               or:  python orchestrator.py … --debug-console
console_verbosity = "user"
model_resolution = "90m"
# Per-region resolution overrides.
# Barbados uses 30m (higher-res DEM, FAC, FDIR, CREST, KW parameter sets).
# Guatemala 900m example: region_resolution_map = {"Guatemala": "900m"}
# Switching resolution auto-selects:
#   EF5_conf/basic/DEM|FAC|FDIR_{region}_{res}.tif
#   EF5_conf/parameters/CREST|KW_{Region}_{res}/
#   EF5_conf/templates/basin_list/{Region}_{res}_basin_new.txt
#   EF5_conf/templates/ef5_{Region}_{res}_control_template.txt (fallback: no _{res}_)
#   EF5_conf/states|outputs …/{region}_{res}/  (e.g. guatemala_900m)
region_resolution_map = {"Guatemala": "90m", "Barbados": "30m"}
# Optional explicit control-template override (else resolution-aware auto-select):
# region_template_map = {"Guatemala": "ef5_Guatemala_900m_control_template.txt"}
regions_to_run = ["Antigua", "Barbados", "Comoros", "Guatemala", "Haiti"]
# regions_to_run = ["Guatemala"]
systemModel = "crest"
systemTimestep = 60 #in minutes

# Coordinates used for generating QPF files and for clipping SCaMPR GeoTIFFs.
# Use the tightest box that covers ALL regions you are running.
# For Caribbean-only runs (Antigua, Barbados, Guatemala, Haiti):
#   xmin=-95.0, xmax=-58.0, ymin=9.0, ymax=24.0
# For Caribbean + Comoros combined, extend to include Comoros (-12.5 to 45 E, -12.5 to 13 N):
xmin = -95.0
xmax = 45.0
ymin = -12.5
ymax = 24.0
systemName = systemModel.upper() + " " + domain.upper() + " " + subdomain.upper()
# ── EF5 container configuration ─────────────────────────────────────────────
# Docker partners:   EF5_RUNTIME=docker  → ef5-container image via docker.sock
# Apptainer partners: EF5_RUNTIME=local → glibc binary EF5/bin/ef5 (NO nesting)
# Host SIF fallback:  EF5/ef5-container.sif when Apptainer is available on host
import os as _os
_ef5_rt = _os.environ.get("EF5_RUNTIME", "").strip().lower()
if _ef5_rt == "docker":
    ef5Path = _os.environ.get("EF5_IMAGE", "ef5-container:latest")
elif _ef5_rt in ("local", "embedded"):
    ef5Path = _os.environ.get("EF5_LOCAL_BIN", "EF5/bin/ef5")
else:
    ef5Path = "EF5/ef5-container.sif"

# Legacy binary paths (no longer used):
# ef5Path = "/Dedicated/Humberto/EF5Binary/EF5V1.2.7/EF5/bin/ef5"
# ef5Path = "/home/nammehta/EF5Master/EF5/bin/ef5"
# EF5_conf/ holds all EF5 inputs/runtime folders (basic, params, states, precip, …).
statesPath = "EF5_conf/states/"
# Legacy combined precip folder (kept for backward compatibility).
precipFolder = "EF5_conf/precip/"

# Source-specific precip roots (recommended).
# The orchestrator creates per-region subfolders inside these roots.
imerg_precip_folder = "EF5_conf/precip/imerg/"
hsaf_precip_folder = "EF5_conf/precip/hsaf/"
scampr_precip_folder = "EF5_conf/precip/scampr/"  # SCaMPR — public AWS S3, no credentials needed
precipEF5Folder = "EF5_conf/precipEF5/"
modelStates = ["crest_SM", "kwr_IR", "kwr_pCQ", "kwr_pOQ"]
# Required state layers EF5 must find together at one timestamp.
# find_available_states() checks ALL of these exist (non-empty) before warm-start.
templatePath = "EF5_conf/templates/"
templates = "ef5_Antigua_control_template.txt"  # last-resort fallback only
# Auto-selected per region+resolution:
#   ef5_Guatemala_90m_control_template.txt / ef5_Guatemala_900m_control_template.txt
basicPath = "EF5_conf/basic/"
parametersPath = "EF5_conf/parameters/"
dataPath = "outputs/"
qpf_store_path = "EF5_conf/qpf_store/"
# Note: the orchestrator writes QPF working files to per-region folders:
# EF5_conf/qpf_store/<region>/gfs_data/ and …/wrf_data/
tmpOutput = dataPath + "tmp_output_" + systemModel + "/"

# QPE source configuration.
# Options: "IMERG" (default), "HSAF", "SCAMPR"
qpe_source = "IMERG"

# Default QPF source for forecast control generation.
# Options: "STORMLAB" (ensemble GFS via StormLab-GFS-realtime),
#          "GFS", "WRF", "AROME", or a list e.g. ["GFS", "AROME"]
qpf_source = "STORMLAB"

# Optional per-region forcing override.
# Keys are region names from regions_to_run.
# Values can use either qpe/qpf or qpe_source/qpf_source keys.
# Example:
# region_forcing_map = {
#     "Antigua":   {"qpe_source": "SCAMPR", "qpf_source": "GFS"},  # Caribbean — SCaMPR
#     "Barbados":  {"qpe_source": "SCAMPR", "qpf_source": "GFS"},
#     "Guatemala": {"qpe_source": "SCAMPR", "qpf_source": "GFS"},
#     "Haiti":     {"qpe_source": "SCAMPR", "qpf_source": "GFS"},
#     "Comoros":   {"qpe_source": "HSAF",   "qpf_source": "WRF"},  # Africa — HSAF
# }
region_forcing_map = {
    # STREAM-Sat QPE + StormLab ensemble QPF (3-phase EF5):
    #   A) STREAM-Sat → EF5_conf/states/stream_sat/ensS*/
    #   B) SCaMPR/HSAF gap → EF5_conf/states/scampr|hsaf/ensS*/  (ops only)
    #   C) StormLab nested SS×SL forecast (no state save)
    "Antigua":   {"qpe_source": "STREAM_SAT", "qpf_source": "STORMLAB"},  # StormLab domain: lesserantilles
    "Barbados":  {"qpe_source": "STREAM_SAT", "qpf_source": "STORMLAB"},
    "Guatemala": {"qpe_source": "STREAM_SAT", "qpf_source": "STORMLAB"},
    "Haiti":     {"qpe_source": "STREAM_SAT", "qpf_source": "STORMLAB"},
    "Comoros":   {"qpe_source": "STREAM_SAT", "qpf_source": "STORMLAB"},
}

# ── STREAM-Sat gap-fill mode ───────────────────────────────────────────
# When qpe_source == "STREAM_SAT", Phase B after STREAM-Sat:
#
#   "SCAMPR" / "SCAMPR_QPE" / "SCAMPR_ONLY"
#       SCaMPR QPE fills ss_end→T; states under EF5_conf/states/scampr/ensS*/
#
#   "HSAF" / "HSAF_QPE"
#       HSAF QPE fills ss_end→T; states under EF5_conf/states/hsaf/ensS*/
#
#   "NONE" — skip gap fill (hindcast default). Forecast warm-starts from
#            STREAM-Sat states.
#
# Phase C forecast uses qpf_source (STORMLAB preferred; GFS/AROME legacy).
stream_sat_gap_fill_mode = "SCAMPR"

# SCaMPR settings (required only when qpe_source == "SCAMPR")
# No credentials needed — data is fetched from the public AWS S3 bucket:
#   s3://noaa-enterprise-rainrate-pds/BLEND/RainRate-Blend-INST/
# pip install boto3 botocore xarray rasterio  (once per environment)
scampr_latency_minutes = 20  # expected product delay in minutes

# ── Warmup configuration ───────────────────────────────────────────────────
# When enabled, if no EF5 states exist within 48 hours of the current cycle
# time, a warmup EF5 run is triggered BEFORE the normal operational cycle.
# The warmup simulates from (cycle_time - warmup_days days) to
# (cycle_time - 40 hours), saving states at the end.
# From the next cycle onward, states should exist and warmup will be skipped.
# This applies to BOTH hindcast and operational modes.

warmup_enabled = True              # set to True to enable warmup

# Duration of the warmup simulation in days.
# The simulation starts at (cycle_time - warmup_days days) and ends at
# (cycle_time - 40 hours), saving states at (cycle_time - 40 hours).
# Default: 10 days if not specified.
warmup_days = 155

# Parallel IMERG download threads (warmup + get_gpm_files batch).
# 0 / unset → auto min(16, cpu*2).  Also: export IMERG_MAX_WORKERS=16
imerg_max_workers = 12

# Per-region precipitation source for warmup runs.
# Options: "IMERG" (default if region not listed), "HSAF"
# Warmup precip is downloaded only for missing files (skip-existing logic).
# Example:
# warmup_precip_source_map = {
#     "Antigua":   "HSAF",
#     "Barbados":  "IMERG",
#     "Comoros":   "HSAF",
#     "Guatemala": "IMERG",
#     "Haiti":     "IMERG",
# }
warmup_precip_source_map = {
    "Antigua":   "IMERG",
    "Barbados":  "IMERG",
    "Comoros":   "IMERG",
    "Guatemala": "IMERG",
    "Haiti":     "IMERG",
}

# ── STREAM-Sat ensemble configuration ──────────────────────────────────────
# Used when qpe_source == "STREAM_SAT" in region_forcing_map.
# STREAM-Sat repo: tito_utils/qpe_utils/STREAM-Sat-realtime/
stream_sat_ensemble_size = 10      # ← USER-TUNABLE (use 2 for test, 10 for ops)

# ── Informational only (STREAM-Sat pipeline internals — do not treat as knobs) ──
# These are passed through to STREAM-Sat run_pipeline; values below match the
# STREAM-Sat defaults. Prefer changing STREAM-Sat's own config if needed.
stream_sat_window_hours = 48        # operational window (h) — STREAM-Sat controlled
stream_sat_warmup_hours = 12        # AR(1) warm-up (h) — STREAM-Sat controlled

# Where STREAM-Sat GeoTIFFs (one folder per member) are written.
# The orchestrator appends the domain name automatically:
#   EF5_conf/precip/stream_sat/caribbean/ensP1/, ensP2/, ...  (Caribbean regions)
#   EF5_conf/precip/stream_sat/comoros/ensP1/, ensP2/, ...    (Comoros)
# This keeps Caribbean and Comoros precip isolated.
stream_sat_precip_folder = "EF5_conf/precip/stream_sat/"

# Where STREAM-Sat ensemble outputs are written.
# Each member gets: outputs/stream_sat/ensOut1/<region>_<resolution>/
#   e.g. outputs/stream_sat/ensOut1/guatemala_900m/
stream_sat_output_folder = "outputs/stream_sat/"

# Where STREAM-Sat ensemble states are saved per member.
# Each member gets: EF5_conf/states/stream_sat/ensS1/<region>_<resolution>/
stream_sat_state_folder = "EF5_conf/states/stream_sat/"

# Phase B gap-fill states / outputs (separate from STREAM-Sat).
# Each STREAM-Sat member gets: EF5_conf/states/scampr/ensS1/<region>_<resolution>/
scampr_state_folder = "EF5_conf/states/scampr/"
scampr_output_folder = "outputs/scampr/"
hsaf_state_folder = "EF5_conf/states/hsaf/"
hsaf_output_folder = "outputs/hsaf/"

# STREAM-Sat GeoTIFF naming convention (EF5 forcing name pattern).
# Files are named: streamsat.qpe.YYYYMMDDHHUU.mmhInst.tif
# Unit: mm/h (native, no conversion needed — EF5 supports mm/h)
stream_sat_tif_naming = "streamsat"

# Max parallel workers for STREAM-Sat NC→TIF conversion.
# None → auto-detect (CPU count).
stream_sat_max_workers = None

# Timeout (seconds) for the STREAM-Sat pipeline subprocess.
stream_sat_pipeline_timeout = 7200  # 2 hours

# ── StormLab-GFS ensemble QPF ──────────────────────────────────────────
# Repo: tito_utils/qpf_utils/StormLab-GFS-realtime/
# NC outputs: …/StormLab-GFS-realtime/output/<domain>/qpf_ens_<domain>_<cyc>.nc
# GeoTIFFs:   EF5_conf/precip/stormlab/<region|domain>/ensQ1/… stormlab.YYYYMMDDHH00.tif
# EF5 outs:   outputs/stormlab/ensOut{SS}_sl{SL}/<region>_<resolution>/
#
# TITO region → StormLab domain:
#   Antigua→lesserantilles, Barbados→barbados, Guatemala→guatemala,
#   Haiti→haiti, Comoros→comoros
stormlab_repo = "tito_utils/qpf_utils/StormLab-GFS-realtime"
stormlab_nc_root = "tito_utils/qpf_utils/StormLab-GFS-realtime/output"
stormlab_precip_folder = "EF5_conf/precip/stormlab/"
stormlab_output_folder = "outputs/stormlab/"
# StormLab defaults if CLI flags omitted (from config/<domain>.yaml):
#   forecast.n_members = 50
#   forecast.operational_forcing_members = 5
#     → 5 GEFS forcings × 10 seeds = 50 members
# Pass stormlab_ensemble_size / stormlab_forcing_members to override.
stormlab_ensemble_size = 5       # ← USER-TUNABLE (test=2, ops=10–50; default yaml=50)
stormlab_forcing_members = 5     # GEFS forcings (default yaml=5; 1=control only for speed)
stormlab_run_pipeline = True     # False → convert existing NC only (no StormLab run)
stormlab_source = "auto"         # auto (GEFS→GFS fallback) | gefs | gfs
stormlab_min_age_h = 5.0         # GEFS latency gate (same as StormLab latest_cycle)
stormlab_pipeline_timeout = 14400
stormlab_tif_naming = "stormlab"

#Alerts configuration
SEND_ALERTS = False
smtp_server = "smtp.gmail.com"
smtp_port = 587
account_address = "model_alerts@gmail.com"
account_password = "supersecurepassword9000"
alert_sender = "Real Time Model Alert" # can also be the same as account_address
alert_recipients = ["fixer1@company.com", "fixer2@company.com", "panic@company.com",...]
copyToWeb = False

#Simulation times 
"""
- **HindCastMode:** If you are running an event that happened in the PAST, set `HindCastMode = True` and write the date of interest in `HindCastDate`, use the format "YYYY-MM-DD HH:MM". If you want to run it in operational mode (meaning TITO will start running in the present time) set `HindCastMode = False`.

- **HindCastEndDate:** When `HindCastMode = True`, set `HindCastEndDate` to run multiple hourly cycles from `HindCastDate` → `HindCastEndDate`. Leave as empty string "" for a single-cycle hindcast.

If Hindcast and LR_mode is True LR_timestep,GFS_archive_path
If running in operational mode (Hindcast False) and LR_mode = True, user only have to define LR_timestep, GFS_archive_path
"""
HindCastMode = False
# Hindcast start time (used when HindCastMode=True)
HindCastDate = "2026-07-22 00:00"  # "%Y-%m-%d %H:%M" UTC
        
# Hindcast end time (optional; if set, runs hourly from HindCastDate → HindCastEndDate)
# Leave as empty string "" for single-cycle hindcast.
HindCastEndDate = "2026-07-23 20:00"  # "%Y-%m-%d %H:%M" UTC

run_LR = True
LR_timestep = "60u"
QPF_archive_path = "EF5_conf/qpf_store/archive/"  # legacy; kept for back-compat

# Dry-run tail (no precip): extend EF5 TIME_END by this many hours after
# each phase window.  Missing precip → EF5 zeros.
#   Phase A STREAM-Sat: TIME_END = ss_end + dry_run_hours; TIME_STATE = ss_end
#   Phase B SCaMPR gap:  TIME_END = T + dry_run_hours;      TIME_STATE = T
#   Phase C StormLab:    TIME_END = T+24h + dry_run_hours;  no TIME_STATE
# Set 0 to disable.
dry_run_hours = 6

# WRF configuration (used when run_LR=True).
# Set WRF_archive_path to the folder containing WRF netCDF files.
# Leave empty ("") to skip WRF and fall back directly to GFS.
WRF_archive_path = ""                               # e.g. "/data/wrf_output/"
WRF_var_name = "PREC_ACC_C"                         # precipitation variable name in WRF netCDFs
WRF_filename_template = "PREC_d01_YYYY-MM-DD_HH_mm_SS.nc"  # WRF filename pattern

# GFS configuration (used when run_LR=True and WRF not available).
# GFS tifs are stored here persistently and reused across cycles.
# The orchestrator writes to per-region subfolders under this root.
GFS_precip_path = "/Dedicated/Humberto/Naman/TITO_Caribbean_Comoros_VM/TITOCaribbeanAndComoros/precip/gfs"                     # persistent GFS tif archive root

# AROME configuration (used when qpf_source includes "AROME").
# AROME tifs are stored per-region under this root as a cache.
# Domain routing is automatic: ANTIL for Caribbean, INDIEN for Comoros.
AROME_precip_path = "EF5_conf/precip/arome/"         # persistent AROME tif cache root

# Email associated to GPM account
email_gpm = 'vrobledodelgado@uiowa.edu'
server = 'https://jsimpsonhttps.pps.eosdis.nasa.gov/imerg/gis/early/'

# HSAF credentials/settings (required only when qpe_source == "HSAF")
hsaf_ftp_user = "naman-mehta@uiowa.edu"
hsaf_ftp_pass = "change_me1234"
hsaf_latency_minutes = 10

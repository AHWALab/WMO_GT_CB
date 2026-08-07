# Threading Inputs to Outputs (TITO)

TITO is a framework designed to run the EF5 hydrologic model operationally, integrating satellite data, machine learning techniques and NWP products to support real-time forecasting and hydrologic analysis.

This repository is configured for the **Caribbean** (Antigua, Barbados, Guatemala, Haiti) and **Comoros**.

Partners need **either Docker or Apptainer/Singularity — not both**.

---

## Installation — recommended (containers)

### 0. Clone

```sh
git clone https://github.com/AHWALab/TITOCaribbeanAndComoros.git
cd TITOCaribbeanAndComoros
# or: cd path/to/TITO_Stream_Sat_test_env
git checkout TITO-StreamSat   # if using the Stream-Sat development branch
```

Populate static inputs before running (all under `EF5_conf/`):

- `EF5_conf/basic/` — DEM, FAC, FDIR  
- `EF5_conf/parameters/` — CREST / KW parameters  
- `EF5_conf/pet/` — monthly PET  
- `EF5_conf/templates/` — EF5 control templates (already in repo)  
- Edit `Caribbean_Comoros_config.py` (GPM email, regions, credentials)

Runtime folders under `EF5_conf/` (`precip/`, `precipEF5/`, `qpf_store/`, `states/`)
plus top-level `outputs/` are kept empty in git (`.gitkeep` only) and are filled when you run TITO.

---

### A) Docker partner / Docker host

**Build from scratch (Docker host):**

```sh
./container-build.sh
```

This builds:

- `tito:latest` — slim TITO conda env (no PyTorch/CUDA) + code  
- `ef5-container:latest` — EF5 Docker image (sibling container via `docker.sock`)  
- `EF5/bin/ef5` — small glibc EF5 binary (also used by Apptainer partners)

**Or use pre-built images (USB / pendrive — no rebuild)** — recommended for partners:

1. Install and start **Docker Desktop** (Windows / macOS) or Docker Engine (Linux).
2. Copy the project folder and place the image archives here:

```
dist/docker-archives/tito_latest.tar.gz
dist/docker-archives/ef5-container_latest.tar.gz
```

3. Load once, then run:

| Platform | Load images (once) | Run |
|---|---|---|
| **Linux / macOS** | `./load-docker-images.sh` or `./tito-run.sh load-images` | `./tito-run.sh operational --regions Guatemala` |
| **Windows (CMD)** | `load-docker-images.cmd` or `tito-run.cmd load-images` | `tito-run.cmd operational --regions Guatemala` |

The launcher **auto-loads** missing images from `dist/` the first time you run.
Manual load is still available via `load-images`.

```sh
# Linux / macOS / Git Bash / WSL
./tito-run.sh load-images
./tito-run.sh operational --regions Guatemala
./tito-run.sh hindcast "2026-07-22 00:00" "2026-07-22 06:00" --regions Guatemala
```

```bat
REM Windows cmd.exe / Docker Desktop — pure CMD (no PowerShell)
tito-run.cmd load-images
tito-run.cmd operational --regions Guatemala
tito-run.cmd hindcast "2026-07-22 00:00" "2026-07-22 06:00" --regions Guatemala
```

Windows uses **pure CMD** (`tito-run.cmd` / `tito-run.bat`) so Group Policy / execution-policy
blocks on unsigned `.ps1` scripts do not apply.

Large image tarballs are **not** on GitHub (`dist/` is gitignored). Ship them on USB or Zenodo.

EF5 runs as a **sibling** Docker container (`EF5_RUNTIME=docker`).
On **macOS/Windows** Docker Desktop, host networking is not used (bridge); outbound downloads still work.

---

### B) Apptainer / Singularity (HPC)

Apptainer cannot reliably nest Apptainer→Apptainer. The partner path is:

1. Build/convert **TITO** to `tito.sif`  
2. Use the **local glibc binary** `EF5/bin/ef5` inside that SIF (no EF5 SIF required)

**On a Docker host (once):**

```sh
./container-build.sh                 # builds tito:latest + EF5/bin/ef5
# save / copy to HPC:
docker save tito:latest | gzip > dist/docker-archives/tito_latest.tar.gz
# also copy EF5/bin/ef5 to the HPC project tree
```

Or download `tito_latest.tar.gz` + `EF5/bin/ef5` from the Zenodo deposit (placeholder DOI above).

**On HPC:**

```sh
# Convert TITO Docker archive → Apptainer SIF (TITO only; EF5 SIF skipped)
./docker-to-apptainer.sh
# → tito.sif

ls -lh EF5/bin/ef5                   # must exist (from Docker-host build)

# Rebuild the binary on a Docker host if missing:
#   ./EF5/docker/build_ef5_local.sh
```

**Run (Apptainer):**

```sh
# All regions from config
TITO_RUNTIME=apptainer ./tito-run.sh operational

# Single region
TITO_RUNTIME=apptainer ./tito-run.sh operational --regions Guatemala

# Hindcast
TITO_RUNTIME=apptainer ./tito-run.sh hindcast \
    "2026-07-22 00:00" "2026-07-22 06:00"

TITO_RUNTIME=apptainer ./tito-run.sh hindcast \
    "2026-07-22 00:00" "2026-07-22 06:00" --regions Guatemala
```

EF5 runs as **`EF5_RUNTIME=local`** → `EF5/bin/ef5` inside the TITO container.

---

### Regions flag

| Command | Behavior |
|---|---|
| *(no `--regions`)* | Runs **all** regions listed in `Caribbean_Comoros_config.py` → `regions_to_run` |
| `--regions Guatemala` | Runs only Guatemala |
| `--regions Antigua,Haiti` | Runs those regions only |

---

## Legacy conda install (dev)

```sh
bash setup_tito.sh
# then edit pipeline.sh conda path and run:
./pipeline.sh
```

Prefer `./tito-run.sh` with Docker or Apptainer for partner deployments.

---

## Repository structure

Region EF5 templates under `EF5_conf/templates/`:

- `ef5_Guatemala_90m_control_template.txt` (90m)
- `ef5_Guatemala_900m_control_template.txt` (900m)

Users must populate topographic and parameter grids for their region. See [EF5-builder-toolkit](https://github.com/AHWALab/EF5-builder-toolkit).

### Key files

- **`Caribbean_Comoros_config.py`** — operational / hindcast configuration  
- **`orchestrator.py`** — single-cycle driver  
- **`hindcast_manager.py`** — multi-hour hindcast loop  
- **`tito-run.sh`** — unified Docker / Apptainer / native launcher  
- **`container-build.sh`** — build TITO + EF5 images (+ local EF5 binary)  
- **`docker-to-apptainer.sh`** — convert `tito` Docker archive → `tito.sif`  
- **`tito_utils/`** — precip, EF5 jobs, cycle timeline helpers  
- **`tito_utils/qpe_utils/STREAM-Sat-realtime/`** — STREAM-Sat ensemble QPE  
- **`tito_utils/qpf_utils/StormLab-GFS-realtime/`** — StormLab ensemble QPF  
- **`EF5_conf/`** — all EF5 static inputs + runtime precip/states  

### Input / output directories

| Folder | Role |
|---|---|
| `EF5_conf/basic/` | DEM, FAC, FDIR |
| `EF5_conf/pet/` | Monthly PET |
| `EF5_conf/parameters/` | CREST / KW parameters |
| `EF5_conf/templates/` | EF5 control templates |
| `EF5_conf/states/` | Model states (runtime) |
| `EF5_conf/precip/` | QPE downloads / STREAM-Sat / StormLab GeoTIFFs |
| `EF5_conf/precipEF5/` | Staged precip for EF5 |
| `EF5_conf/qpf_store/` | GFS / AROME / WRF QPF working folders |
| `outputs/` | Simulation outputs + hindcast logs |
| `dist/` | Local Docker archives (**gitignored** → Zenodo) |

---

## STREAM-Sat + StormLab three-phase ensemble

Set `qpe_source = "STREAM_SAT"` and `qpf_source = "STORMLAB"` in `region_forcing_map`.

| Phase | Forcing | States |
|---|---|---|
| **A** STREAM-Sat QPE | `EF5_conf/precip/stream_sat/…/ensP*` | `EF5_conf/states/stream_sat/ensS*/` @ ss_end (~T−4h) |
| **B** SCaMPR/HSAF gap | SCaMPR (or HSAF) to cycle time T | `EF5_conf/states/scampr/ensS*/` (or `…/hsaf/`) @ T |
| **C** StormLab forecast | nested SS×SL members | not saved |

StormLab-GFS lives in `tito_utils/qpf_utils/StormLab-GFS-realtime/`; NC→GeoTIFF
conversion writes `EF5_conf/precip/stormlab/<region>/ensQ*/stormlab.YYYYMMDDHH00.tif`.
Antigua uses the StormLab **lesserantilles** domain.

| Config variable | Description | Default |
|---|---|---|
| `stream_sat_ensemble_size` | STREAM-Sat members | `2` (ops: 10) |
| `stream_sat_gap_fill_mode` | `"SCAMPR"`, `"HSAF"`, or `"NONE"` | `"SCAMPR"` |
| `stormlab_ensemble_size` | StormLab QPF members | `2` (ops: 10–50) |
| `stormlab_forcing_members` | GEFS forcings into StormLab | `1` |
| `stormlab_run_pipeline` | Run StormLab or convert existing NC | `True` |

**Hindcast:** STREAM-Sat `--end = cycle time` (no IMERG latency); Phase B skipped;
Phase C StormLab (or legacy GFS) from STREAM-Sat states.

References: Li et al. (2023), Hartke et al. (2022), Peng et al. (2025);
Liu, Wright & Lorenz (2024) for StormLab.

---

## Config notes (`Caribbean_Comoros_config.py`)

- **`HindCastMode` / `--hindcast-date`:** past-event cycles (prefer `./tito-run.sh hindcast …`)  
- **`email_gpm`:** NASA GPM PPS registration email ([register](https://registration.pps.eosdis.nasa.gov/registration/))  
- **`qpe_source`:** `"IMERG"`, `"HSAF"`, or `"STREAM_SAT"` (per-region map)  
- **`run_LR`:** enable QPF extension (GFS / AROME as configured)

---

## Contact

Naman Mehta — naman-mehta@uiowa.edu  
Vanessa Robledo — vanessa-robledodelgado@uiowa.edu  
AHWA Laboratory — [ahwa.lab.uiowa.edu](https://ahwa.lab.uiowa.edu/) — engr-ahwa-lab@uiowa.edu

## Cite

Robledo Delgado, V., & Vergara, H. (2025). Threading Inputs to Outputs (TITO) (v2.0.0). Zenodo. https://doi.org/10.5281/zenodo.17246491

Container image archives for this Stream-Sat release will also be deposited on Zenodo (DOI placeholder above).

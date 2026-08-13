# TITO — Guatemala Training

**Threading Inputs to Outputs (TITO)** is AHWA Lab’s framework for running the **EF5** hydrologic model with satellite QPE, ensemble nowcast/QPF products, and (optionally) flood inundation mapping (FIM).

This folder is the **Guatemala training package** — a trimmed, classroom-ready copy of TITO focused on **Guatemala** (90 m and 900 m). It is not the full Caribbean/Comoros operational tree.

**Training case (fixed):** hindcast **2023-06-21 07:00–08:00 UTC**.  
Warmup states are already provided. Use the [Setup Wizard](trainings/TITO_Setup_Wizard.html) to pick resolution, ensembles, and RAM, then run the printed command.

Partners need **either Docker or Apptainer/Singularity — not both**.

---

## What this package does

| Piece | Role |
|--------|------|
| **EF5** | Distributed hydrologic model (CREST + KW). Local glibc binary or sibling Docker image. |
| **STREAM-Sat** | Ensemble satellite QPE (IMERG + GFS 850 hPa U/V motion). See [Li et al. (2023)](https://doi.org/10.1029/2022WR033752), Hartke et al. (2022). Code: `tito_utils/qpe_utils/STREAM-Sat-realtime/`. |
| **StormLab** | Ensemble precipitation forecast from GEFS/GFS (Liu, Wright & Lorenz, 2024; Peng et al., 2025). Code: `tito_utils/qpf_utils/StormLab-GFS-realtime/`. In this package StormLab is run as **QPE** (no EF5 long-range / `PRECIPFORECAST`). |
| **IMERG + GFS** | Deterministic QPE + forecast-as-QPE chain (optional training path). |
| **FIM** | Scenario-library inundation **after the forecast phase**, **90 m only**. Details: [README_FIM.md](README_FIM.md). |

**QPE-only EF5 chain (no long-range block):**

| Mode | Phase A | Phase B | Phase C |
|------|---------|---------|---------|
| **Hindcast STREAM-Sat + StormLab** | STREAM-Sat QPE + 6 h dry → `states/stream_sat/ensS*/` | skipped | StormLab as QPE + dry (per SS×SL member) |
| **Ops STREAM-Sat + StormLab** | same | SCaMPR/HSAF gap QPE + dry | StormLab as QPE from gap states |
| **Hindcast IMERG + GFS** | IMERG QPE + dry → `states/imerg/` | skipped | GFS as QPE + dry |
| **Ops IMERG + GFS** | IMERG QPE + dry @ T−4 h | SCaMPR gap + dry @ T | GFS as QPE from gap states |

---

## Quick start (training)

1. Install **Docker Desktop** (Windows/macOS) or Docker Engine / **Apptainer** (Linux/HPC).
2. Load images (once) — see [Loading Docker images](#loading-docker-images).
3. Open **`trainings/TITO_Setup_Wizard.html`** in a browser (no server needed).
4. Work through: Run mode → Regions → Approach (RAM + ensembles) → Review.
5. Merge the printed snippet into `Caribbean_Comoros_config.py`.
6. Run the printed command (examples below).

**Intended training command:**

```sh
# Linux / macOS / Git Bash / WSL
./tito-run.sh hindcast "2023-06-21 07:00" "2023-06-21 08:00" --regions Guatemala
```

```bat
REM Windows CMD (no PowerShell)
tito-run.cmd hindcast "2023-06-21 07:00" "2023-06-21 08:00" --regions Guatemala
```

```sh
# HPC Apptainer
TITO_RUNTIME=apptainer ./tito-run.sh hindcast \
    "2023-06-21 07:00" "2023-06-21 08:00" --regions Guatemala
```

Add **`--offline`** to skip all precip downloads (uses `offline_precips/` — see [Offline mode](#offline-mode)).

---

## Loading Docker images

Place the USB / pendrive archives here (not on GitHub):

```text
dist/docker-archives/tito_latest.tar.gz
dist/docker-archives/ef5-container_latest.tar.gz
```

Start **Docker Desktop** (Windows/macOS) or the Docker daemon (Linux) first.

| Platform | Load once | Then run |
|----------|-----------|----------|
| **Linux / macOS** | `./load-docker-images.sh` or `./tito-run.sh load-images` | `./tito-run.sh hindcast "…" "…" --regions Guatemala` |
| **Windows (CMD)** | `load-docker-images.cmd` or `tito-run.cmd load-images` | `tito-run.cmd hindcast "…" "…" --regions Guatemala` |

Windows launchers are **pure CMD** (`tito-run.cmd`, `load-docker-images.cmd`) so Group Policy / unsigned `.ps1` blocks do not apply.

The first `tito-run` also **auto-loads** missing images from `dist/` if Docker is up.

**Check:**

```sh
docker images tito
docker images ef5-container
```

Expect `tito:latest` and `ef5-container:latest`. A 502 on `docker load` is usually Docker Desktop not fully started — restart Docker and retry.

---

## Setup Wizard

Open [`trainings/TITO_Setup_Wizard.html`](trainings/TITO_Setup_Wizard.html) locally.

| Step | Training lock-in |
|------|------------------|
| **Run mode** | Hindcast only · **2023-06-21 07:00 → 08:00 UTC** · Docker default (Apptainer optional) |
| **Regions** | **Guatemala only** · **900 m default** or 90 m |
| **Approach** | STREAM-Sat + StormLab · set N_ss, N_sl, RAM, `ef5_max_workers` · time estimate |
| **Review** | Config snippet + OS-specific run command |

Removed for this course: other countries, operational mode, Forcing/Warmup/Paths pages, case name.

**Laptop tip:** 900 m, `stream_sat_ensemble_size = 2`, `stormlab_ensemble_size = 2`, `ef5_max_workers = 1`.  
90 m + many parallel EF5 jobs can OOM on Docker Desktop (exit **137**).

---

## Run commands (all OS)

### Hindcast (training window)

| OS | Command |
|----|---------|
| Linux / macOS | `./tito-run.sh hindcast "2023-06-21 07:00" "2023-06-21 08:00" --regions Guatemala` |
| Windows CMD | `tito-run.cmd hindcast "2023-06-21 07:00" "2023-06-21 08:00" --regions Guatemala` |
| Apptainer HPC | `TITO_RUNTIME=apptainer ./tito-run.sh hindcast "2023-06-21 07:00" "2023-06-21 08:00" --regions Guatemala` |

### Offline (no downloads)

Same as above, add `--offline`. Only **07:00** and **08:00** on **2023-06-21** are allowed.

```sh
./tito-run.sh hindcast "2023-06-21 07:00" "2023-06-21 07:00" --regions Guatemala --offline
```

```bat
tito-run.cmd hindcast "2023-06-21 07:00" "2023-06-21 07:00" --regions Guatemala --offline
```

### Operational (not the classroom default)

```sh
./tito-run.sh operational --regions Guatemala
tito-run.cmd operational --regions Guatemala
```

### Helpful extras

```sh
./tito-run.sh load-images          # load USB archives
./tito-run.sh shell                # interactive container shell
```

`--regions Guatemala` is required for this package (other regions are locked in the wizard).

---

## Offline mode

Classroom / no-network mode. **Does not change the online pipeline** unless `--offline` is set.

1. Launcher sets `TITO_OFFLINE=1` and bind-mounts `offline/` + `offline_precips/`.
2. `prepare_cycle_precip` **refuses downloads** and stages from `offline_precips/`.
3. If you request fewer ensembles than the archive, the **wettest** members become `ensP1…N` / `ensQ1…N`.
4. EF5 + FIM then run as usual. Warmup stays off (states already in `EF5_conf/states/`).

**Allowed times only:** `2023-06-21 07:00` and `08:00` UTC. Any other timestamp **fails fast** (use online mode for other dates).

Archive layout (already populated in this package):

```text
offline_precips/
  stream_sat/caribbean/ensP*/
  stormlab/guatemala/ensQ*/
  imerg/*.tif
  qpf_store/_shared/202306210700/gfs_data/*.tif
```

Refresh after a good online run: `bash offline/materialize_offline_precips.sh`  
More: [offline/README.md](offline/README.md).

---

## Folder structure

```text
TITO_GuatemalaTraining/
  Caribbean_Comoros_config.py   # main config (regions, ensembles, FIM, EF5 workers)
  orchestrator.py               # one cycle
  hindcast_manager.py           # hourly hindcast loop
  tito-run.sh / tito-run.cmd    # Docker / Apptainer / native launcher
  load-docker-images.sh/.cmd    # load USB images
  trainings/TITO_Setup_Wizard.html
  tito_utils/
    qpe_utils/STREAM-Sat-realtime/   # STREAM-Sat
    qpf_utils/StormLab-GFS-realtime/ # StormLab
    ef5/                             # control files, jobs, runtimes
    fim_utils/                       # FIM
    precip/                          # precip facade (+ offline hard guard)
  EF5/bin/ef5                   # local EF5 binary (Apptainer)
  EF5_conf/
    basic/  parameters/  pet/   templates/
    states/                     # stream_sat/ensS*, imerg/, scampr*, …
    precip/                     # stream_sat, stormlab, imerg
    precipEF5/  qpf_store/
  outputs/<cycle>/<region_res>/<product>/   # cycle-first EF5 + FIM
  offline/  offline_precips/    # training offline mode
  fim_config/  fim_store/       # FIM site YAML + zarr store
  dist/docker-archives/         # USB images (gitignored)
```

### Cycle-first outputs

```text
outputs/20230621.070000/guatemala_90m/
  stream_sat/ensOut1/
  stormlab/ensOut1_sl1/
  imerg/
  gfs/
  fim/stream_sat_stormlab/    # or fim/imerg_gfs/
```

### States (keep separate per product)

| Chain | States |
|--------|--------|
| STREAM-Sat | `EF5_conf/states/stream_sat/ensS<n>/<region>_<res>/` |
| IMERG | `EF5_conf/states/imerg/<region>_<res>/` |
| SCaMPR gap (ops) | `EF5_conf/states/scampr/ensS*` or `states/scampr_det/` |

Training warmup snapshot used for IMERG: `…/imerg/guatemala_90m|900m/*_20230619_1500.tif`.

---

## Packages (container env)

Defined in `tito_env.yml` (Python 3.12, conda-forge). **No PyTorch/CUDA.**

| Area | Packages |
|------|----------|
| Arrays / science | numpy, scipy, pandas, xarray, netcdf4, h5py |
| GIS | gdal, rasterio, rioxarray, pyproj, shapely |
| STREAM-Sat noise | pysteps (FFT only) |
| GFS / GRIB | cfgrib, eccodes, herbie-data, boto3 |
| FIM | zarr, pyyaml |
| I/O | requests, pillow, tifffile, matplotlib-base |

Rebuild the TITO image after changing this file: `./container-build.sh --no-ef5`.

---

## Config highlights (`Caribbean_Comoros_config.py`)

```python
model_resolution = "90m"                      # or "900m"
region_resolution_map = {"Guatemala": "90m"}
regions_to_run = ["Guatemala"]

region_forcing_map = {
    "Guatemala": {"qpe_source": "STREAM_SAT", "qpf_source": "STORMLAB"},
    # or: {"qpe_source": "IMERG", "qpf_source": "GFS"},
}

stream_sat_ensemble_size = 2                  # 2 laptop / 10 ops
stormlab_ensemble_size = 2
ef5_max_workers = 1                           # 1 = sequential EF5 (safest)
dry_run_hours = 6
warmup_enabled = False                        # training states already shipped
fim_enabled = True                            # 90m + after forecast only
```

90 m control template: `EF5_conf/templates/ef5_Guatemala_90m_control_template.txt`  
900 m: `ef5_Guatemala_900m_control_template.txt`

FIM: unzip `fim_store/fim_store_SantaInesPetapa_v1.zarr.zip` once. See [README_FIM.md](README_FIM.md).

---

## Apptainer / HPC

```sh
TITO_RUNTIME=apptainer ./tito-run.sh hindcast \
    "2023-06-21 07:00" "2023-06-21 08:00" --regions Guatemala --offline
```

Uses `tito.sif` + **local** `EF5/bin/ef5` (no nested Apptainer). Needs `libtiff`/`libgeotiff`/`libgomp` inside the TITO image (already in the current Dockerfile). Exit **127** usually means a missing shared library — rebuild TITO image.

Convert a Docker archive on HPC: `./docker-to-apptainer.sh`.

---

## Reset after a crash

If a run dies mid-way (OOM, bad GeoTIFFs, half-written states, leftover precip), **reset TITO to the original training snapshot** before retrying **2023-06-21 07:00**:

| OS | Command |
|----|---------|
| Linux / macOS | `./reset_tito.sh` |
| Preview only | `./reset_tito.sh --dry-run` |
| Windows CMD | `reset_tito.cmd` |
| Windows preview | `reset_tito.cmd --dry-run` |

**What it does**

- Wipes contents of `outputs/`, `EF5_conf/precip/`, `EF5_conf/precipEF5/`, `EF5_conf/qpf_store/`
- Wipes STREAM-Sat / StormLab runtime output folders
- **Keeps** state GeoTIFFs for the training warmup **2023-06-19 15:00** (`20230619_1500` and common spellings); deletes other `*.tif` under `EF5_conf/states/`
- Never deletes state **folders**
- If no 15:00 warmup tifs are found, it **refuses** to delete any state tifs (unless `--force`)

Then re-run the training hindcast (add `--offline` if you do not want downloads):

```sh
./tito-run.sh hindcast "2023-06-21 07:00" "2023-06-21 07:00" --regions Guatemala --offline
```

```bat
tito-run.cmd hindcast "2023-06-21 07:00" "2023-06-21 07:00" --regions Guatemala --offline
```

`offline_precips/` is **not** wiped — the next `--offline` run restages precip from that archive.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `docker load` 502 | Docker Desktop not ready — restart daemon |
| EF5 exit **137** | OOM — lower ensembles, `ef5_max_workers=1`, use 900 m, raise Docker RAM |
| EF5 exit **127** | Missing `libtiff.so.5` (local EF5) — rebuild TITO image |
| `Cannot open TIFF` after STREAM-Sat | Corrupt GeoTIFFs from RAM pressure — delete `EF5_conf/precip/stream_sat` and re-run |
| FIM `no_runs` | Wrong chain vs folders, or not 90 m — check `outputs/<cycle>/<rkey>/gfs` vs `stormlab/` |
| FIM skip 900 m | Expected — FIM is **90 m only** |
| Offline refused cycle | Only 21 Jun 2023 07:00 and 08:00 UTC are allowed |
| Offline still downloads | Old Apptainer path; current launcher prints `Offline : YES` and `OFFLINE precip (hard guard…)` |

---

## References

- Li, Z., et al. (2023). STREAM-Sat / satellite QPE ensemble. *Water Resources Research*.  
- Hartke, S., et al. (2022). Related satellite precipitation ensemble work.  
- Liu, G., Wright, D. B., & Lorenz, D. (2024). StormLab.  
- Peng, B., et al. (2025). StormLab / GEFS applications.  
- EF5: [AHWALab/EF5-builder-toolkit](https://github.com/AHWALab/EF5-builder-toolkit)

---

## Contact

Naman Mehta — naman-mehta@uiowa.edu  
Vanessa Robledo — vanessa-robledodelgado@uiowa.edu  
AHWA Laboratory — [ahwa.lab.uiowa.edu](https://ahwa.lab.uiowa.edu/) — engr-ahwa-lab@uiowa.edu

## Cite

Robledo Delgado, V., & Vergara, H. (2025). Threading Inputs to Outputs (TITO) (v2.0.0). Zenodo. https://doi.org/10.5281/zenodo.17246491

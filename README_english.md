# EF5 — Guatemala Training (standalone Docker workspace)

Standalone EF5 setup for Guatemala (**90 m** and **900 m**) that runs the flood
model through a Docker container while **TITO_GuatemalaTraining stays
untouched**. The EF5 image is the same `ef5-container` used by TITO; only the
run layout differs.

There are separate **domain** and **cuenca (gauge)** control files. Cuenca
controls enable timeseries output at named gauges (e.g. Villalobos / Motagua).

## Folder layout

```text
EF5_GuatemalaTraining/
├── data/                        → mounted as /data  (model inputs, read/write)
│   ├── basic/                   DEM, flow direction (DDM), flow accumulation (FAM)
│   │   ├── DEM_guatemala_90m.tif / FDIR_guatemala_90m.tif / FAC_guatemala_90m.tif
│   │   └── DEM_guatemala_900m.tif / FDIR_guatemala_900m.tif / FAC_guatemala_900m.tif
│   ├── parameters/
│   │   ├── CREST_Guatemala_90m/   KW_Guatemala_90m/
│   │   └── CREST_Guatemala_900m/  KW_Guatemala_900m/
│   ├── pet/                     PET.01.tif … PET.12.tif climatology
│   ├── states/
│   │   ├── 90m/                 warm-start states for 90 m (crest_SM, kwr_*)
│   │   └── 900m/                warm-start states for 900 m (crest_SM, kwr_*)
│   └── precip/                  IMERG forcing: imerg.qpe.YYYYMMDDHHUU.30minAccum.tif
├── output/                      → mounted as /output (EF5 results)
│   ├── 900m/                    results — full-domain 900 m run
│   ├── 900m_cuenca/             results — 900 m run with named gauges
│   └── 90m_cuenca/              results — 90 m run with named gauges
├── conf/                        → mounted as /conf  (control files, read-only)
│   ├── control_900m.txt         full-domain control — Guatemala 900 m
│   ├── control_900m_cuenca.txt  gauge/cuenca control — Guatemala 900 m
│   ├── control_90m_cuenca.txt   gauge/cuenca control — Guatemala 90 m
│   └── basin_list/
│       ├── Guatemala_900m_basin_new.txt
│       └── Guatemala_90m_basin_new.txt
├── docker/
│   ├── Dockerfile               builds ef5-container:latest from source (AHWALab/EF5)
│   ├── build_ef5.sh             build/reuse (Linux & macOS)
│   ├── build_ef5.ps1            build/reuse (Windows PowerShell)
│   ├── build_ef5.cmd            Windows launcher (bypasses execution policy)
│   └── ef5-container.tar        prebuilt image archive (Git LFS / offline reuse)
├── docker-compose.yml           cross-platform launcher (works on all 3 OSes)
├── run_ef5.sh                   run EF5 (Linux / macOS / WSL)
├── run_ef5.ps1                  run EF5 (Windows PowerShell)
├── run_ef5.cmd                  Windows launcher (bypasses execution policy)
├── README_english.md
└── README.md
```

## How the container accesses the folders

`run_ef5.sh` / `run_ef5.ps1` / `docker-compose.yml` bind-mount the three
folders into the container and run EF5 from the container root, so every path
in the control file is relative to `/`:

| Host folder | Container path | Used for                                         |
| ----------- | -------------- | ------------------------------------------------ |
| `./data`    | `/data`        | basic, parameters, pet, states, precip           |
| `./output`  | `/output`      | EF5 outputs (maxq/maxunitq/ts.\*.tif, logs, csv) |
| `./conf`    | `/conf`        | EF5 control files                                |

## Build or reuse the image

**Linux / macOS** — `docker/build_ef5.sh` never forces a rebuild unless you ask:

```bash
./docker/build_ef5.sh                 # reuse existing image / load archive / build
./docker/build_ef5.sh --status        # what will be used
./docker/build_ef5.sh --rebuild       # compile from source (needs internet)
./docker/build_ef5.sh --load          # load prebuilt docker/ef5-container.tar
./docker/build_ef5.sh --save          # snapshot current image → docker/ef5-container.tar
```

**Windows (PowerShell)** — `docker\build_ef5.ps1` with the same flags:

```powershell
.\docker\build_ef5.ps1
.\docker\build_ef5.ps1 -Status
.\docker\build_ef5.ps1 -Rebuild
.\docker\build_ef5.ps1 -Load
.\docker\build_ef5.ps1 -Save
```

Reuse order: already-loaded local image → `docker/ef5-container.tar` archive
(needs `git lfs pull` after a GitHub clone) → build from `docker/Dockerfile`
(clones AHWALab/EF5 and compiles, a few minutes).

## Run EF5 — pick a control file

Pass the control path (must live under `conf/`):

```bash
# Linux / macOS / WSL
./run_ef5.sh conf/control_900m.txt          # full domain, 900 m  → output/900m/
./run_ef5.sh conf/control_900m_cuenca.txt   # gauges/cuenca, 900 m → output/900m_cuenca/
./run_ef5.sh conf/control_90m_cuenca.txt    # gauges/cuenca, 90 m  → output/90m_cuenca/
./run_ef5.sh --bash                         # interactive shell in the container
```

```powershell
# Windows PowerShell (prefer a local C: path — not a mapped network drive)
.\run_ef5.ps1 -Control control_900m.txt
.\run_ef5.ps1 -Control control_900m_cuenca.txt
.\run_ef5.ps1 -Control control_90m_cuenca.txt
.\run_ef5.ps1 -Bash
```

| Platform             | Example                                                          |
| -------------------- | ---------------------------------------------------------------- |
| Linux / WSL / macOS  | `./run_ef5.sh conf/control_90m_cuenca.txt`                       |
| Windows (PowerShell) | `.\run_ef5.ps1 -Control control_90m_cuenca.txt`                  |
| Any OS               | `docker compose run --rm ef5 /ef5/bin/ef5 /conf/control_900m.txt` |

If no control file is passed, the default is `conf/control_900m.txt`.

macOS (Docker Desktop) has no host networking, so `run_ef5.sh` automatically
delegates to `docker compose` there. Windows users can also run the `.sh`
scripts from Git Bash/WSL.

## Control files and outputs

| Control | Resolution | Purpose | Output folder | States |
| ------- | ---------- | ------- | ------------- | ------ |
| `conf/control_900m.txt` | 900 m | full domain | `./output/900m/` | `data/states/900m/` |
| `conf/control_900m_cuenca.txt` | 900 m | named gauges / cuenca | `./output/900m_cuenca/` | `data/states/900m/` |
| `conf/control_90m_cuenca.txt` | 90 m | named gauges / cuenca (e.g. Villalobos) | `./output/90m_cuenca/` | `data/states/90m/` |

Basin / gauge source lists (reference; already inlined into the control files
where applicable) live under `conf/basin_list/`.

Cuenca controls set `outputts=true` on selected gauges so EF5 writes timeseries
CSV under the matching `output/*_cuenca/` folder.

Includes `maxq` / `maxunitq` / precip-accum grids (and soil moisture where
enabled) plus `ts.*.csv` timeseries for gauges with `outputts=true`.

Populate `data/precip/` with IMERG GeoTIFFs named
`imerg.qpe.YYYYMMDDHHUU.30minAccum.tif` before a run with precipitation;
missing files are treated as zero precipitation.


## Windows notes (execution policy & network drives)

Cloned `.ps1` scripts are often **blocked** on Windows when:

- the repo lives on a **mapped/network drive** (e.g. `X:\`), or
- PowerShell policy is `RemoteSigned` / `AllSigned`.

**Prefer the `.cmd` launchers** (they force `-ExecutionPolicy Bypass`):

```bat
REM from repo root (Command Prompt or PowerShell)
docker\build_ef5.cmd -Status
docker\build_ef5.cmd -Load
docker\build_ef5.cmd -Rebuild

run_ef5.cmd -Control control_900m.txt
run_ef5.cmd -Control control_90m_cuenca.txt
```

PowerShell switches use a **single** dash: `-Rebuild`, `-Load`, `-Status`
(not `--Rebuild`).

If you must run the `.ps1` directly:

```powershell
# This session only (no Admin)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

# Unblock files marked "from the Internet"
Get-ChildItem -Recurse -Filter *.ps1 | Unblock-File

.\docker\build_ef5.ps1 -Rebuild
```

`Set-ExecutionPolicy RemoteSigned` is **not enough** for scripts on `X:` —
PowerShell treats network paths as remote and still requires a signature.
Use `Bypass` (Process or CurrentUser) or the `.cmd` wrappers.

**Docker bind mounts** from mapped network drives (`X:`) often fail or appear
empty inside the container. Copy the repo to a **local** folder first:

```powershell
Copy-Item -Recurse X:\WMO_GT_CB-Day1_EF5\WMO_GT_CB-Day1_EF5 C:\EF5_GuatemalaTraining
cd C:\EF5_GuatemalaTraining
git lfs pull
docker\build_ef5.cmd -Load
run_ef5.cmd -Control control_900m.txt
```

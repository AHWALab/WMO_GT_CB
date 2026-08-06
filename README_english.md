# EF5 — Guatemala Training (standalone Docker workspace)

Standalone EF5 setup for Guatemala (**90 m** and **900 m**) that runs the flood
model through a Docker container while **TITO_GuatemalaTraining stays
untouched**. The EF5 image is the same `ef5-container` used by TITO; only the
run layout differs.

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
│   ├── 90m/                     results from the 90 m run
│   └── 900m/                    results from the 900 m run
├── conf/                        → mounted as /conf  (control files, read-only)
│   ├── control_90m.txt          EF5 control file — Guatemala 90 m
│   ├── control_900m.txt         EF5 control file — Guatemala 900 m
│   ├── Guatemala_90m_basin_new.txt   gauge/basin defs (inlined into control_90m.txt)
│   └── Guatemala_900m_basin_new.txt  gauge/basin defs (inlined into control_900m.txt)
├── docker/
│   ├── Dockerfile               builds ef5-container:latest from source (AHWALab/EF5)
│   ├── build_ef5.sh             build/reuse (Linux & macOS)
│   ├── build_ef5.ps1            build/reuse (Windows PowerShell)
│   └── ef5-container.tar        prebuilt image archive (Git LFS / offline reuse)
├── docker-compose.yml           cross-platform launcher (works on all 3 OSes)
├── run_ef5.sh                   run EF5 (Linux / macOS / WSL)
├── run_ef5.ps1                  run EF5 (Windows PowerShell)
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
| `./conf`    | `/conf`        | EF5 control files (90 m and 900 m)               |

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
.\docker\build_ef5.ps1                # reuse existing image / load archive / build
.\docker\build_ef5.ps1 -Status
.\docker\build_ef5.ps1 -Rebuild
.\docker\build_ef5.ps1 -Load
.\docker\build_ef5.ps1 -Save
```

Reuse order: already-loaded local image → `docker/ef5-container.tar` archive
(needs `git lfs pull` after a GitHub clone) → build from `docker/Dockerfile`
(clones AHWALab/EF5 and compiles, a few minutes).

## Run EF5 — Linux, Windows, macOS

There are **two control files** under `conf/`. Choose the resolution by passing
the control path to the launcher:

```bash
# Linux / macOS / WSL
./run_ef5.sh conf/control_900m.txt    # Guatemala 900 m
./run_ef5.sh conf/control_90m.txt     # Guatemala 90 m
./run_ef5.sh --bash                   # interactive shell in the container
```

```powershell
# Windows PowerShell (prefer a local C: path — not a mapped network drive)
.\run_ef5.ps1 -Control control_900m.txt   # Guatemala 900 m
.\run_ef5.ps1 -Control control_90m.txt    # Guatemala 90 m
.\run_ef5.ps1 -Bash                       # interactive shell
```

| Platform             | Command                                                          |
| -------------------- | ---------------------------------------------------------------- |
| Linux / WSL          | `./run_ef5.sh conf/control_900m.txt` or `conf/control_90m.txt`   |
| macOS                | same (`run_ef5.sh` auto-uses docker compose)                     |
| Windows (PowerShell) | `.\run_ef5.ps1 -Control control_900m.txt` or `control_90m.txt`   |
| Any OS               | `docker compose run --rm ef5 /ef5/bin/ef5 /conf/control_900m.txt` |

If no control file is passed, the default is `conf/control_900m.txt`.

macOS (Docker Desktop) has no host networking, so `run_ef5.sh` automatically
delegates to `docker compose` there. Windows users can also run the `.sh`
scripts from Git Bash/WSL.

## Results

Outputs are written under `./output/` according to the control resolution:

| Control | Output folder | States |
| ------- | ------------- | ------ |
| `conf/control_90m.txt` | `./output/90m/` | `data/states/90m/` |
| `conf/control_900m.txt` | `./output/900m/` | `data/states/900m/` |

Includes `maxq` / `maxunitq` / precip-accum grids and `ts.*.csv` timeseries.

Populate `data/precip/` with IMERG GeoTIFFs named
`imerg.qpe.YYYYMMDDHHUU.30minAccum.tif` before a run with precipitation;
missing files are treated as zero precipitation.

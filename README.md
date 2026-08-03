# EF5 — Guatemala Training (standalone Docker workspace)

Standalone EF5 setup for Guatemala (900 m) that runs the flood model through a
Docker container while **TITO_GuatemalaTraining stays untouched**. The EF5 image
is the same `ef5-container` used by TITO; only the run layout differs.

## Folder layout

```
EF5_GuatemalaTraining/
├── data/                        → mounted as /data  (model inputs, read/write)
│   ├── basic/                   DEM, flow direction (DDM), flow accumulation (FAM)
│   ├── parameters/              CREST_Guatemala_900m/, KW_Guatemala_900m/ grids
│   ├── pet/                     PET.01.tif … PET.12.tif climatology
│   ├── states/                  model states for warm start (crest_SM, kwr_*)
│   └── precip/
│       └── imerg/               IMERG forcing: imerg.qpe.YYYYMMDDHHUU.30minAccum.tif
├── output/                      → mounted as /output (EF5 results)
├── conf/                        → mounted as /conf  (control files, read-only)
│   ├── control.txt              EF5 control file for Guatemala 900 m
│   └── Guatemala_900m_basin_new.txt  gauge/basin definitions (inlined into control.txt)
├── docker/
│   ├── Dockerfile               builds ef5-container:latest from source (AHWALab/EF5)
│   ├── build_ef5.sh             build/reuse (Linux & macOS)
│   ├── build_ef5.ps1            build/reuse (Windows PowerShell)
│   └── ef5-container.tar        prebuilt image archive (offline reuse)
├── docker-compose.yml           cross-platform launcher (works on all 3 OSes)
├── run_ef5.sh                   run EF5 (Linux / macOS / WSL)
├── run_ef5.ps1                  run EF5 (Windows PowerShell)
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
| `./conf`    | `/conf`        | EF5 control file(s)                              |

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

Reuse order: already-loaded local image → `docker/ef5-container.tar` archive →
build from `docker/Dockerfile` (clones AHWALab/EF5 and compiles, a few minutes).

## Run EF5 — Linux, Windows, macOS

| Platform             | Command                                   |
| -------------------- | ----------------------------------------- |
| Linux / WSL          | `./run_ef5.sh`                            |
| macOS                | `./run_ef5.sh` (auto-uses docker compose) |
| Windows (PowerShell) | `.\run_ef5.ps1` (Use C Drive)             |
| Any OS               | `docker compose run --rm ef5`             |

macOS (Docker Desktop) has no host networking, so `run_ef5.sh` automatically
delegates to `docker compose` there. Windows users can also run everything from
Git Bash/WSL with the `.sh` scripts. `run_ef5.ps1` takes the same arguments:

```powershell
.\run_ef5.ps1                          # runs conf/control.txt
.\run_ef5.ps1 -Control my_control.txt  # explicit control file (must live in conf/)
.\run_ef5.ps1 -Bash                    # interactive shell
```

Results appear in `./output/` (maxq/maxunitq/precipaccum grids and
`ts.*.csv` timeseries). Populate `data/precip/imerg/` with IMERG GeoTIFFs before
a run with precipitation; missing files are treated as zero precipitation.

## Relationship to TITO

- TITO (`TITO_GuatemalaTraining/`) keeps its own `EF5/` folder, image and
  `docker-compose.yml` — **unchanged**.
- This workspace shares the same `ef5-container:latest` image name by default.
  To use a distinct image, set `EF5_IMAGE=ef5-container-gt:latest` before
  running the scripts (and tag it in `build_ef5.sh` if you rebuild).
- Data was copied from `TITO_GuatemalaTraining` (Guatemala 900 m basic,
  parameters, PET, states) so the workspace runs out of the box.
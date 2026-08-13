# FIM integration (TITO Guatemala Training)

Scenario-library flood inundation mapping runs **after each EF5 cycle** when
`fim_enabled = True` in `Caribbean_Comoros_config.py`.

## Layout

| Path | Role |
|------|------|
| `tito_utils/fim_utils/` | FIM package (pipelines, store, matching) |
| `fim_config/` | One YAML per FIM **site** (`Guatemala_*.yaml`) |
| `fim_config/aoc/` | Area-of-concern polygons |
| `fim_store/` | Pre-simulated flood map stores (zarr) |
| `outputs/fim/<site>/<cycle>/` | Runtime products (gitignored) |

## Toggle & rules

```python
fim_enabled = True          # False = skip FIM
fim_config_dir = "fim_config"
```

| Rule | Behaviour |
|------|-----------|
| **When** | After **forecast** EF5 only (`run_LR` / Phase C). Not after IMERG/SS alone. |
| **Resolution** | **90m only** (non-90m regions skipped). |
| **Rain total** | Sum of **qpeaccum** components (never qpfaccum). |
| **IMERG+GFS** | IMERG + optional SCaMPR gap + GFS forecast QPE accums |
| **STREAM-Sat+StormLab** | STREAM-Sat + StormLab QPE accums per ensemble member |

Sites: `fim_config/<Region>*.yaml`. Park a site with `enabled: false`.

## One-time setup (Santa Ines Petapa)

```bash
# If the zip is a Git LFS pointer (~130 bytes text), fetch the real blob first:
#   git lfs pull --include "fim_store/*"
cd fim_store
unzip -o fim_store_SantaInesPetapa_v1.zarr.zip
# → fim_store/fim_store_SantaInesPetapa_v1.zarr/
```

Requires: `pyyaml`, `numpy`, `rasterio` (or gdal), `zarr` (`pip install zarr`).

## What runs

After EF5 finishes, orchestrator STEP 8 calls:

```python
from tito_utils.fim_utils import run_fim_for_cycle
run_fim_for_cycle(regions_to_run=..., cycle="YYYYMMDD.HHMMSS", config=config)
```

- YAML with `hazards:` → pluvial + fluvial runner (`pipeline_pf`)
- YAML without → classic ensemble runner
- Failures are **non-fatal** (logged; EF5 products kept)

## EF5 inputs (cycle-first)

```text
outputs/<cycle>/<rkey>/stream_sat/ensOut{N}/
outputs/<cycle>/<rkey>/stormlab/ensOut{N}_sl{M}/
outputs/<cycle>/<rkey>/imerg/
outputs/<cycle>/<rkey>/gfs/
```

## FIM products (cycle-first + chain tag)

The hook writes under a folder named by the forcing chain so IMERG+GFS and
STREAM-Sat+StormLab never mix:

```text
outputs/<cycle>/<rkey>/fim/stream_sat_stormlab/   # qpe=STREAM_SAT, qpf=STORMLAB
outputs/<cycle>/<rkey>/fim/imerg_gfs/             # qpe=IMERG, qpf=GFS
  pluvial/  fluvial/  combined/  …
```

IMERG+GFS FIM YAML template: `fim_config/examples/Guatemala_SantaInesPetapa_imerg_gfs.yaml`

## Manual test

```bash
python -m tito_utils.fim_utils.pipeline_pf \
  --config fim_config/Guatemala_SantaInesPetapa.yaml \
  --cycle 20230621.070000
```

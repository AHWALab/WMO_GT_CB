# fim_config: this folder decides where FIM runs

One YAML file in this folder = one FIM site. After each cycle’s EF5 runs,
`orchestrator.py` STEP 8 calls `tito_utils.fim_utils.tito_hook` when
`fim_enabled = True` in `Caribbean_Comoros_config.py`. It picks up files named
`<Region>*.yaml`, where `<Region>` is the exact name from `regions_to_run`.
No matching YAML means no FIM for that region. The `examples/` subfolder is
never auto-picked.

Toggle: `fim_enabled = False` skips FIM entirely (EF5 unchanged).

## Current sites

| file | site | hazards | status |
| --- | --- | --- | --- |
| `Guatemala_SantaInesPetapa.yaml` | Santa Ines Petapa, cuenca Villalobos | pluvial + fluvial + combined | READY (unzip the store once, see below) |
| `Guatemala_Morales.yaml` | Morales, Rio Motagua | pluvial + fluvial + combined | prepared, `enabled: false`, waiting for the flood map library |

Guatemala carries two basins, so it has two YAML files. Any country can hold
any number of sites the same way: `Haiti_SiteA.yaml`, `Haiti_SiteB.yaml`, and
so on.

## Declaring the hazards of a site

The `hazards:` block inside each YAML is the switch:

    hazards:
      pluvial:
        enabled: true          # rainfall analog matching (all sites)
      fluvial:
        enabled: true          # discharge analog matching (Guatemala sites)
        boundary_series:
          - "ts.cuenca_villalobos_1.crest.{cycle}.csv"
          - "ts.cuenca_villalobos_2.crest.{cycle}.csv"

Guatemala sites run both hazards plus the combined PF product (per pixel
maximum across the two matched maps, member by member). Sites in other
countries are pluvial only for now: set `fluvial: {enabled: false}` and the
site produces pluvial products alone. Start new pluvial only sites from
`examples/PluvialOnly_country_template.yaml`.

A YAML with a `hazards:` block uses the pluvial + fluvial runner
(`pipeline_pf`). A YAML without one is treated as a classic v0.2 config and
uses the original ensemble runner, so older configs keep working.

## Switching a site off and on

Add a top level line `enabled: false` to skip a site without deleting its
file (`Guatemala_Morales.yaml` ships that way until its store is built).
Remove the line to activate it.

## Depth thresholds are a user input

    thresholds_m: [0.10, 0.30, 0.50, 1.00]

Edit the list and rerun; every value produces its own probability raster and
likelihood class raster in every enabled routine. The values above are the
current working set agreed for Guatemala.

## Before a site can run

Each site needs, once: its scenario store under `fim_store/` (unzipped from
the shipped zip, or built new with `fim_dev/build_store_guatemala.py`), real
storm magnitudes attached to that store, and its area of concern polygon
under `fim_config/aoc/`. The full checklist with commands is in
`README_FIM.md`, section "Manual steps before a region can run". For the
Santa Ines Petapa site only the unzip step remains; everything else ships
done.

## Testing a site by hand

    python -m tito_utils.fim_utils.pipeline_pf \
        --config fim_config/Guatemala_SantaInesPetapa.yaml --cycle <cycle>

Use `--hazard P`, `--hazard F` or `--hazard PF` to run one routine alone.

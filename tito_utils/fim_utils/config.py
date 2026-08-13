"""Configuration for fim_utils.

One YAML file per region (see fim_dev/configs/ for examples). Everything
that could change later (cycle count, ensemble members, file naming,
matching bands) lives here, not in code.
"""

import os
from dataclasses import dataclass, field

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise ImportError("fim_utils requires PyYAML (pip install pyyaml)") from exc


@dataclass
class EF5Files:
    """Filename templates inside one EF5 run cycle folder.

    {cycle} is replaced with the cycle timestamp string, e.g. 20240704.090000.
    """
    uq: str = "maxunitq.{cycle}.tif"
    qpe_accum: str = "qpeaccum.{cycle}.tif"
    qpf_accum: str = "qpfaccum.{cycle}.tif"
    max_q: str = "maxq.{cycle}.tif"


@dataclass
class EF5Section:
    outputs_root: str = "outputs"
    run_dir_glob: str = "tmp_output_*"
    cycle_format: str = "%Y%m%d.%H%M%S"
    files: EF5Files = field(default_factory=EF5Files)
    # Only run folders whose parsed (model, qpe, qpf) pass these filters are
    # used. Empty list = accept all. Example: qpf_sources: ["gfs", "arome"].
    qpe_sources: list = field(default_factory=list)
    qpf_sources: list = field(default_factory=list)
    require_qpf: bool = True   # skip QPE-only state-building runs by default


@dataclass
class AOCSection:
    source: str = ""           # .geojson / .json / .shp / .gpkg or folder of mask .tif
    id_field: str = ""         # attribute holding the unit id (default: index)
    name_field: str = ""       # attribute holding a display name (optional)
    layer: str = ""            # layer name for .gpkg (optional)


@dataclass
class TriggerSection:
    uq_threshold: float = 1.0          # m3/s/km2
    mode: str = "any_pixel"            # any_pixel (max within AOC) is the only mode for now
    scope: str = "any_member"          # any_member | all_members


@dataclass
class RainfallSection:
    stat: str = "mean"                 # mean | max over the AOC
    qpe_scale: float = 1.0             # unit conversion factors if ever needed
    qpf_scale: float = 1.0
    missing_qpf: str = "skip_member"   # skip_member | qpe_only


@dataclass
class MatchingSection:
    band: list = field(default_factory=lambda: [0.9, 1.2])
    band_wide: list = field(default_factory=lambda: [0.8, 1.3])
    use_direction: bool = False
    direction_tolerance_deg: float = 45.0
    rounding: str = "up"
    selectors: dict = field(default_factory=lambda: {"best": 50, "upper": 90})


@dataclass
class OutputsSection:
    root: str = "outputs/{region}/fim"
    clip_to_aoc: bool = False
    write_quicklook: bool = False


@dataclass
class FimConfig:
    region: str = ""
    root: str = "."                    # repo root; relative paths resolve against it
    catalog_path: str = ""             # folder with index.csv, maps/, extents/
    aoc: AOCSection = field(default_factory=AOCSection)
    ef5: EF5Section = field(default_factory=EF5Section)
    trigger: TriggerSection = field(default_factory=TriggerSection)
    rainfall: RainfallSection = field(default_factory=RainfallSection)
    matching: MatchingSection = field(default_factory=MatchingSection)
    outputs: OutputsSection = field(default_factory=OutputsSection)

    def resolve(self, path: str) -> str:
        if not path:
            return path
        return path if os.path.isabs(path) else os.path.join(self.root, path)

    @property
    def outputs_dir(self) -> str:
        return self.resolve(self.outputs.root.format(region=self.region))


def _fill(dc, data: dict):
    """Fill a dataclass instance from a dict, ignoring unknown keys."""
    if not data:
        return dc
    for key, value in data.items():
        if not hasattr(dc, key):
            print(f"    fim_utils.config warning: unknown key '{key}' ignored")
            continue
        current = getattr(dc, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _fill(current, value)
        else:
            setattr(dc, key, value)
    return dc


def load_config(path: str, root: str = None) -> FimConfig:
    """Load a region FIM config from YAML.

    root: repo root against which relative paths resolve. Defaults to the
    current working directory (TITO runs from the repo root).
    """
    with open(path, "r") as fh:
        raw = yaml.safe_load(fh) or {}

    cfg = FimConfig()
    _fill(cfg, raw)
    cfg.root = os.path.abspath(root or raw.get("root", "."))

    if not cfg.region:
        raise ValueError(f"'region' is required in {path}")
    if not cfg.catalog_path:
        raise ValueError(f"'catalog_path' is required in {path}")
    if not cfg.aoc.source:
        raise ValueError(f"'aoc.source' is required in {path}")
    return cfg

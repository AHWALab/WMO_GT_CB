"""Discovery of EF5 run outputs.

TITO writes each EF5 run into
    {outputs_root}/{Region}/tmp_output_{model}[_{qpe}[_{qpf}]]/{cycle}/
with cycle folders named with the orchestrator trigger time
(%Y%m%d.%H%M%S) and grids renamed to
    maxunitq.{cycle}.tif   qpeaccum.{cycle}.tif   qpfaccum.{cycle}.tif

Every run folder that has outputs for a cycle is treated as one ensemble
member. Nothing here assumes how many members exist or how often cycles
run; add a new QPE or QPF source to TITO and it shows up as a new member
automatically.
"""

import glob
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

RUN_DIR_RE = re.compile(
    r"^tmp_output_(?P<model>[A-Za-z0-9]+)"
    r"(?:_(?P<qpe>[A-Za-z0-9]+))?"
    r"(?:_(?P<qpf>[A-Za-z0-9]+))?$"
)


@dataclass
class EF5Run:
    region: str
    model: str
    qpe_source: str            # e.g. imerg, scampr, hsaf ("" if unlabeled)
    qpf_source: str            # e.g. gfs, arome, wrf ("" for QPE-only runs)
    cycle: str                 # e.g. "20240704.090000"
    folder: str
    files: dict = field(default_factory=dict)  # role -> absolute path (existing only)

    @property
    def member_id(self) -> str:
        qpe = self.qpe_source or "qpe"
        qpf = self.qpf_source or "noqpf"
        return f"{self.model}_{qpe}_{qpf}"

    @property
    def has_qpf(self) -> bool:
        return "qpf_accum" in self.files

    def path(self, role: str) -> str:
        return self.files.get(role, "")


def _parse_run_dir(name: str):
    m = RUN_DIR_RE.match(name)
    if not m:
        return None
    return (m.group("model") or "", (m.group("qpe") or "").lower(),
            (m.group("qpf") or "").lower())


def _cycles_in(run_dir: str, cycle_format: str) -> list:
    out = []
    if not os.path.isdir(run_dir):
        return out
    for name in os.listdir(run_dir):
        full = os.path.join(run_dir, name)
        if not os.path.isdir(full):
            continue
        try:
            datetime.strptime(name, _fmt_to_strptime(cycle_format))
        except ValueError:
            continue
        out.append(name)
    return sorted(out)


def _fmt_to_strptime(cycle_format: str) -> str:
    # cycle_format is already a strptime pattern (default %Y%m%d.%H%M%S)
    return cycle_format


def latest_cycle(outputs_root: str, region: str, run_dir_glob: str = "tmp_output_*",
                 cycle_format: str = "%Y%m%d.%H%M%S") -> str:
    """Most recent cycle string found in any run folder for the region."""
    region_dir = os.path.join(outputs_root, region)
    latest = ""
    for run_dir in sorted(glob.glob(os.path.join(region_dir, run_dir_glob))):
        cycles = _cycles_in(run_dir, cycle_format)
        if cycles and cycles[-1] > latest:
            latest = cycles[-1]
    return latest


def discover_runs(outputs_root: str, region: str, cycle: str = None,
                  run_dir_glob: str = "tmp_output_*",
                  cycle_format: str = "%Y%m%d.%H%M%S",
                  file_templates: dict = None,
                  qpe_sources: list = None, qpf_sources: list = None,
                  require_qpf: bool = True) -> list:
    """Find all EF5 runs (ensemble members) for a region and cycle.

    cycle=None uses the latest cycle available. Filters:
    - qpe_sources / qpf_sources: keep only listed sources (empty = all)
    - require_qpf: drop runs without a qpfaccum grid (state-building runs)
    Returns a list of EF5Run.
    """
    file_templates = file_templates or {
        "uq": "maxunitq.{cycle}.tif",
        "qpe_accum": "qpeaccum.{cycle}.tif",
        "qpf_accum": "qpfaccum.{cycle}.tif",
        "max_q": "maxq.{cycle}.tif",
    }
    if cycle is None or cycle == "":
        cycle = latest_cycle(outputs_root, region, run_dir_glob, cycle_format)
        if not cycle:
            return []

    region_dir = os.path.join(outputs_root, region)
    runs = []
    for run_dir in sorted(glob.glob(os.path.join(region_dir, run_dir_glob))):
        parsed = _parse_run_dir(os.path.basename(run_dir))
        if parsed is None:
            continue
        model, qpe, qpf = parsed
        if qpe_sources and qpe and qpe not in [s.lower() for s in qpe_sources]:
            continue
        if qpf_sources and qpf and qpf not in [s.lower() for s in qpf_sources]:
            continue

        cycle_dir = os.path.join(run_dir, cycle)
        if not os.path.isdir(cycle_dir):
            continue

        files = {}
        for role, template in file_templates.items():
            candidate = os.path.join(cycle_dir, template.format(cycle=cycle))
            if os.path.isfile(candidate):
                files[role] = candidate
        if "uq" not in files:
            continue
        run = EF5Run(region=region, model=model, qpe_source=qpe,
                     qpf_source=qpf, cycle=cycle, folder=cycle_dir, files=files)
        if require_qpf and not run.has_qpf:
            continue
        runs.append(run)
    return runs

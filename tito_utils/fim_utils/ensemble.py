"""Global (layout-driven) ensemble handling.

TITO cycle-first output layout (current):

  outputs/<cycle>/<rkey>/<product>/ensOut…/

  member template example:
    "{cycle}/{rkey}/stormlab/ensOut{ens}_sl{sl}"

Legacy layout (still supported if template has no {cycle}):

  outputs/stormlab/ensOut{ens}_sl{sl}/<rkey>/tmp_output_…/<cycle>/
"""

import glob
import os
import re
from dataclasses import dataclass, field

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _template_to_regex(template: str):
    """'a/ensOut{ens}_sl{sl}/b' -> compiled regex with named groups."""
    out, pos = [], 0
    for m in _PLACEHOLDER.finditer(template):
        out.append(re.escape(template[pos:m.start()]))
        out.append(f"(?P<{m.group(1)}>[^/]+?)")
        pos = m.end()
    out.append(re.escape(template[pos:]))
    return re.compile("^" + "".join(out) + "$")


def _template_to_glob(template: str) -> str:
    return _PLACEHOLDER.sub("*", template)


@dataclass
class EnsembleMember:
    member_id: str                 # e.g. "ens03_sl2"
    keys: dict                     # {"ens": "3", "sl": "2"}
    run_dir: str                   # resolved member run dir (absolute)
    cycle: str


def discover_ensemble_members(outputs_root: str, member_template: str,
                              cycle: str = None,
                              cycle_format: str = "%Y%m%d.%H%M%S") -> list:
    """Find members by expanding the member template against the disk."""
    from datetime import datetime
    tmpl = member_template.strip("/").replace("\\", "/")
    cycle_in_tmpl = "{cycle}" in tmpl

    if cycle_in_tmpl:
        # Cycle-first layout: run dir IS the member folder (files inside).
        if cycle:
            search = tmpl.replace("{cycle}", cycle)
        else:
            search = tmpl.replace("{cycle}", "*")
        rx = _template_to_regex(tmpl)
        hits = []
        for path in sorted(glob.glob(os.path.join(outputs_root, _template_to_glob(search)))):
            rel = os.path.relpath(path, outputs_root).replace(os.sep, "/")
            m = rx.match(rel)
            if not m or not os.path.isdir(path):
                continue
            keys = m.groupdict()
            cyc = keys.pop("cycle", cycle or "")
            if cycle and cyc and cyc != cycle:
                continue
            if not cyc:
                continue
            try:
                datetime.strptime(cyc, cycle_format)
            except ValueError:
                continue
            hits.append((keys, path, cyc))
        if not hits:
            return []
        if not cycle:
            cycle = max(h[2] for h in hits)
        members = []
        for keys, path, cyc in hits:
            if cyc != cycle:
                continue
            # Deterministic runs (imerg_gfs) may only have {cycle}/{rkey}/gfs
            id_keys = {k: v for k, v in keys.items() if k not in ("rkey",)}
            if id_keys:
                member_id = "_".join(
                    f"{k}{_pad(v)}" for k, v in sorted(id_keys.items()))
            else:
                member_id = "det01"
            # keep rkey in keys so component templates can format
            members.append(EnsembleMember(
                member_id=member_id, keys=keys, run_dir=path, cycle=cycle))
        return members

    # Legacy: member folder contains cycle subdirs
    rx = _template_to_regex(tmpl)
    hits = []
    for path in sorted(glob.glob(os.path.join(outputs_root, _template_to_glob(tmpl)))):
        rel = os.path.relpath(path, outputs_root).replace(os.sep, "/")
        m = rx.match(rel)
        if not m or not os.path.isdir(path):
            continue
        hits.append((m.groupdict(), path))

    if cycle is None or cycle == "":
        latest = ""
        for _, path in hits:
            for name in os.listdir(path):
                if os.path.isdir(os.path.join(path, name)):
                    try:
                        datetime.strptime(name, cycle_format)
                    except ValueError:
                        continue
                    latest = max(latest, name)
        cycle = latest
    if not cycle:
        return []

    members = []
    for keys, path in hits:
        cdir = os.path.join(path, cycle)
        if not os.path.isdir(cdir):
            # maybe files already live in path (flat)
            if any(f.endswith(".tif") for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))):
                cdir = path
            else:
                continue
        member_id = "_".join(f"{k}{_pad(v)}" for k, v in sorted(keys.items()))
        members.append(EnsembleMember(member_id=member_id, keys=keys,
                                      run_dir=cdir, cycle=cycle))
    return members


def _pad(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if digits and digits == value.strip():
        return f"{int(digits):02d}"
    m = re.match(r"^(.*?)(\d+)$", value)
    if m:
        return f"{m.group(1)}{int(m.group(2)):02d}"
    return value


def resolve_component_dir(outputs_root: str, template: str, keys: dict, cycle: str) -> str:
    """Fill a component template with the member's keys (unused keys ok)."""
    tmpl = template.strip("/").replace("\\", "/")
    fmt_keys = dict(keys or {})
    if "{cycle}" in tmpl:
        fmt_keys = {**fmt_keys, "cycle": cycle}
        try:
            rel = tmpl.format(**fmt_keys)
        except KeyError as exc:
            raise KeyError(
                f"Component template '{template}' needs key {exc} "
                f"not present in member keys {sorted(fmt_keys)}") from exc
        return os.path.join(outputs_root, rel)
    try:
        rel = tmpl.format(**fmt_keys)
    except KeyError as exc:
        raise KeyError(f"Component template '{template}' needs key {exc} "
                       f"not present in member keys {sorted(keys)}") from exc
    return os.path.join(outputs_root, rel, cycle)


def grid_path(run_dir: str, role: str, cycle: str, file_templates: dict) -> str:
    template = file_templates.get(role)
    if not template:
        return ""
    path = os.path.join(run_dir, template.format(cycle=cycle))
    return path if os.path.isfile(path) else ""


# ---------------------------------------------------------------------------
# Sampling zone: AOC stats with expand-on-nodata fallback.
# ---------------------------------------------------------------------------

@dataclass
class ZoneStat:
    value: float
    n_valid: int
    expand_km: float
    flags: list = field(default_factory=list)


def zone_stat(raster_path: str, bounds, stat: str = "mean",
              expand_steps_km=(0, 2, 5, 10, 15), min_valid_cells: int = 50) -> ZoneStat:
    """Statistic over a lon/lat bounds box with widening fallback."""
    import numpy as np
    import rasterio
    from rasterio.windows import from_bounds, Window

    with rasterio.open(raster_path) as src:
        for exp_km in expand_steps_km:
            dd = exp_km / 111.0
            bb = (bounds[0] - dd, bounds[1] - dd, bounds[2] + dd, bounds[3] + dd)
            win = from_bounds(*bb, transform=src.transform).round_offsets().round_lengths()
            win = win.intersection(Window(0, 0, src.width, src.height))
            if win.width <= 0 or win.height <= 0:
                continue
            data = src.read(1, window=win)
            valid = data != src.nodata if src.nodata is not None else np.isfinite(data)
            n = int(valid.sum())
            if n >= min_valid_cells:
                vals = data[valid]
                value = float(vals.max()) if stat == "max" else float(vals.mean())
                flags = [] if exp_km == 0 else [f"sampling_expanded_{exp_km}km"]
                return ZoneStat(value=value, n_valid=n, expand_km=float(exp_km), flags=flags)
    return ZoneStat(value=float("nan"), n_valid=0, expand_km=float(expand_steps_km[-1]),
                    flags=["no_valid_cells"])

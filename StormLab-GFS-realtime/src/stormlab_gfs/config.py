"""Configuration loading for StormLab-GFS."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import yaml


@dataclasses.dataclass
class Domain:
    name: str
    out_lat: tuple[float, float]
    out_lon: tuple[float, float]
    sim_buffer: float | dict
    res_hires: float
    res_coarse: float

    def _buf(self, side: str) -> float:
        # scalar = symmetric buffer; mapping {s,n,w,e} = per-side (needed when
        # an archived-subset forcing bbox cannot fit the symmetric buffer)
        if isinstance(self.sim_buffer, dict):
            return float(self.sim_buffer[side])
        return float(self.sim_buffer)

    @property
    def sim_lat(self) -> tuple[float, float]:
        return (self.out_lat[0] - self._buf("s"), self.out_lat[1] + self._buf("n"))

    @property
    def sim_lon(self) -> tuple[float, float]:
        return (self.out_lon[0] - self._buf("w"), self.out_lon[1] + self._buf("e"))

    def _grid(self, lat_rng, lon_rng, res):
        # all cell centers within the CLOSED interval: a domain edge that
        # falls exactly on a center (comoros sim north, -9.75) must keep that
        # center, matching xarray's inclusive slice used at ingest
        lat = np.arange(lat_rng[0] + res / 2, lat_rng[1] + res / 2, res).round(6)
        lon = np.arange(lon_rng[0] + res / 2, lon_rng[1] + res / 2, res).round(6)
        return lat, lon

    def hires_grid(self, sim: bool = True):
        """(lat, lon) centers of the 0.1-deg grid (sim domain by default)."""
        rng = (self.sim_lat, self.sim_lon) if sim else (self.out_lat, self.out_lon)
        return self._grid(rng[0], rng[1], self.res_hires)

    def coarse_grid(self, sim: bool = True):
        rng = (self.sim_lat, self.sim_lon) if sim else (self.out_lat, self.out_lon)
        return self._grid(rng[0], rng[1], self.res_coarse)


@dataclasses.dataclass
class Config:
    domain: Domain
    forecast: dict
    covariates: list[str]
    training: dict
    noise: dict
    paths: dict
    sources: dict
    _config_dir: Path = Path(".")

    @property
    def leads(self) -> np.ndarray:
        f = self.forecast
        return np.arange(f["lead_start"], f["lead_end"] + 1, f["lead_step"])

    @property
    def lead_bins(self) -> list[tuple[int, int]]:
        return [tuple(b) for b in self.forecast["lead_bins"]]

    def lead_to_bin(self, lead: int) -> int:
        for i, (lo, hi) in enumerate(self.lead_bins):
            if lo <= lead <= hi:
                return i
        raise ValueError(f"lead {lead} outside configured bins")

    def path(self, key: str, *sub: str) -> Path:
        p = Path(self.paths[key])
        if not p.is_absolute():
            p = self._config_dir / p
        p = p.joinpath(*sub)
        # treat names with a suffix as files, otherwise as directories
        target = p.parent if p.suffix else p
        _ensure_dir(target)
        return p


def _ensure_dir(path: Path) -> None:
    """mkdir -p tolerant of parallel NFS/Ceph races (EEXIST + stale is_dir)."""
    import time

    for _ in range(8):
        try:
            path.mkdir(parents=True, exist_ok=True)
            return
        except FileExistsError:
            if path.is_dir():
                return
            time.sleep(0.05)
    path.mkdir(parents=True, exist_ok=True)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with open(path) as f:
        raw = yaml.safe_load(f)
    dom = raw.pop("domain")
    dom["out_lat"] = tuple(dom["out_lat"])
    dom["out_lon"] = tuple(dom["out_lon"])
    cfg = Config(domain=Domain(**dom), _config_dir=path.parent.parent, **raw)
    return cfg

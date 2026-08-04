"""QPF utilities (GFS, AROME, WRF, StormLab).

Heavy modules are imported lazily via ``__getattr__`` so importing
``GFS_searcher`` does not pull unnecessary dependencies.
"""

from __future__ import annotations

__all__ = [
    "download_GFS",
    "GFS_searcher",
    "GFS_wind_searcher",
    "WRF_searcher",
    "download_AROME",
    "get_arome_domain_for_region",
    "AROME_searcher",
    "run_and_convert_stormlab",
    "get_stormlab_domain",
]

_LAZY = {
    "download_GFS": (".gfs_downloader", "download_GFS"),  # legacy v1 CLI API
    "GFS_searcher": (".gfs_manager", "GFS_searcher"),
    "GFS_wind_searcher": (".gfs_manager", "GFS_wind_searcher"),
    "WRF_searcher": (".wrf_manager", "WRF_searcher"),
    "download_AROME": (".arome_downloader", "download_AROME"),
    "get_arome_domain_for_region": (".arome_downloader", "get_arome_domain_for_region"),
    "AROME_searcher": (".arome_manager", "AROME_searcher"),
    "run_and_convert_stormlab": (".stormlab_utils", "run_and_convert_stormlab"),
    "get_stormlab_domain": (".stormlab_utils", "get_stormlab_domain"),
}


def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = _LAZY[name]
    import importlib
    mod = importlib.import_module(module_name, __name__)
    value = getattr(mod, attr)
    globals()[name] = value
    return value

"""EF5 job construction and execution.

Import explicitly to avoid heavy transitive deps at package import time::

    from tito_utils.ef5.jobs.pipeline import run_ef5_job_pipeline
    from tito_utils.ef5.jobs.helpers import parse_streamsat_tif_window
"""


def run_ef5_job_pipeline(*args, **kwargs):
    from tito_utils.ef5.jobs.pipeline import run_ef5_job_pipeline as _impl
    return _impl(*args, **kwargs)


__all__ = ["run_ef5_job_pipeline"]

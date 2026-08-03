"""EF5 control-file prep, alerts, and job pipeline.

Import concrete symbols from submodules, e.g.::

    from tito_utils.ef5.ef5_routines import prepare_ef5
    from tito_utils.ef5.jobs import run_ef5_job_pipeline
"""

# Keep this package init free of eager imports to avoid circular deps with
# tito_utils.file_utils.prepare_precip ↔ ef5_routines.
__all__ = []

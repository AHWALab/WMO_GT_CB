"""
Present so PYTHONPATH=/app/offline is harmless.

Offline precip is enforced by the hard guard in
``tito_utils.precip.manager.prepare_cycle_precip`` when ``TITO_OFFLINE=1``.
No startup monkey-patch here (avoids importing numpy before conda env is ready).
"""
# intentionally empty

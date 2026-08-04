"""TNGD (transformed nonstationary gamma) — prediction (runtime-only release).

Evaluates the fitted conditional-distribution parameter fields each forecast
cycle: logistic occurrence P_wet and the Scheuerer & Hamill (2015) log-link
mean/spread of the transformed gamma, per grid cell and timestep, from the
regression coefficients stored in distribution_params.nc.


"""
from __future__ import annotations

import numpy as np

N_BASE = 3  # b0, b1, b2


def predict_fields(covar_fields: np.ndarray, logit_par: np.ndarray, tngd_par: np.ndarray):
    """Evaluate parameter fields for each forecast timestep.

    Parameters
    ----------
    covar_fields : (T, K, ny, nx) covariates on the hi-res grid
    logit_par : (K+1, ny, nx)
    tngd_par : (N_BASE+K+4, ny, nx)

    Returns
    -------
    p_wet : (T, ny, nx)
    a, scale : (T, ny, nx) gamma params of y = x^c
    gg_c : (ny, nx)
    """
    T, K = covar_fields.shape[:2]
    b0, b1, b2 = tngd_par[0], tngd_par[1], tngd_par[2]
    gcoef = tngd_par[N_BASE : N_BASE + K]          # (K, ny, nx)
    mu_clim, sigma_clim = tngd_par[N_BASE + K], tngd_par[N_BASE + K + 1]
    gg_c = tngd_par[N_BASE + K + 3]

    lin = logit_par[0] + np.einsum("kyx,tkyx->tyx", logit_par[1:], covar_fields)
    p_wet = 1.0 / (1.0 + np.exp(-lin))

    logarg = b1 + np.einsum("kyx,tkyx->tyx", gcoef, covar_fields)
    with np.errstate(invalid="ignore", divide="ignore"):
        mu = mu_clim / b0 * np.log1p(np.expm1(b0) * logarg)
        mu = np.clip(mu, 1e-6, None)
        sigma = b2 * sigma_clim * np.sqrt(mu / mu_clim)
        sigma = np.clip(sigma, 1e-6, None)
        a = (mu / sigma) ** 2
        scale = sigma**2 / mu
    return p_wet, a, scale, gg_c

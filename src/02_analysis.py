"""
Módulo de Análisis y Pronóstico Operativo Oceánico (WGMLEARN)
=============================================================

Este script representa el componente `02_analysis.py` de la arquitectura ODA.
Recoge los datos procesados/saneados por el Agente Inspector y aplica un
modelado predictivo (Time Series Forecasting) para proyectar 5 años hacia
el futuro, calculando intervalos de confianza (95%).

Arquitectura de Clases:
La lógica está encapsulada en `OceanForecaster`, una clase base preparada
para ser extendida en el futuro con Foundation Models de Deep Learning 
(ej. TimeGPT, PatchTST). Actualmente utiliza un modelo estadístico tipo ARIMA
como baseline robusto.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.tsa.statespace.sarimax import SARIMAX

logger = logging.getLogger(__name__)

FOURIER_K = 3
FORECAST_HORIZON_YEARS = 5
CONFIDENCE_LEVEL = 0.05
TARGET_VARIABLE = "temperatura"
AR1_FORECAST_MONTHS = 6

ATAC_FORECAST_MONTHS = 24
ATAC_AR_CANDIDATES: tuple[int, ...] = (0, 1, 2)
ATAC_SHORT_SERIES_MONTHS = 60
ATAC_FORECAST_SHORT = 12


def monthly_bin_and_anomaly(
    df: pd.DataFrame,
    *,
    col_fecha: str,
    col_valor: str,
) -> pd.DataFrame:
    """
    Construye una serie mensual estricta (MS) y calcula climatología + anomalía.

    Qué resuelve
    ------------
    - **Irregularidad intra-mensual**: si hay varios muestreos en un mes, promedia.
    - **Malla temporal estricta**: fuerza 12 bins/año (freq='MS') reindexando.
    - **Climatología mensual**: media histórica por mes (1..12).
    - **Anomalía mensual**: Valor_t - Climatologia_{mes}.

    Returns
    -------
    DataFrame con columnas:
      - fecha (Timestamp, inicio de mes)
      - <col_valor> (media mensual observada; NaN si mes sin datos)
      - mes (1..12)
      - climatologia_mes (media histórica del mes)
      - anomalia_mensual (valor - climatologia_mes)
    """
    work = df[[col_fecha, col_valor]].copy()
    work[col_fecha] = pd.to_datetime(work[col_fecha], errors="coerce")
    work[col_valor] = pd.to_numeric(work[col_valor], errors="coerce")
    work = work.dropna(subset=[col_fecha, col_valor])
    if work.empty:
        return pd.DataFrame(columns=["fecha", col_valor, "mes", "climatologia_mes", "anomalia_mensual"])

    # Bin mensual: inicio de mes (MS) y media si múltiples muestreos
    work["fecha"] = work[col_fecha].dt.to_period("M").dt.to_timestamp(how="start")
    monthly = work.groupby("fecha", as_index=False)[col_valor].mean()

    # Malla estricta mensual (12 bins/año)
    full_index = pd.date_range(
        start=monthly["fecha"].min(),
        end=monthly["fecha"].max(),
        freq="MS",
    )
    monthly = (
        monthly.set_index("fecha")
        .reindex(full_index)
        .rename_axis("fecha")
        .reset_index()
    )

    monthly["mes"] = monthly["fecha"].dt.month

    # Climatología mensual (12 valores). Se calcula solo con meses con dato.
    clim = monthly.dropna(subset=[col_valor]).groupby("mes")[col_valor].mean()
    monthly["climatologia_mes"] = monthly["mes"].map(clim)
    monthly["anomalia_mensual"] = monthly[col_valor] - monthly["climatologia_mes"]

    return monthly


def decompose_marcos(
    df: pd.DataFrame,
    *,
    col_fecha: str,
    col_y: str,
    n_future_months: int = 0,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """
    Descomposición tipo `references/marcos.R` (OLS): intercepto + efectos mensuales
    (enero como referencia) + tendencia lineal en años.

    `Trend` sigue la construcción de Marcos: el primer mes tiene índice (1)/12,
    el segundo (2)/12, etc., medido desde el primer mes de la malla temporal.
    """
    work = df[[col_fecha, col_y]].copy()
    work.columns = ["fecha", "y"]
    work["fecha"] = pd.to_datetime(work["fecha"], errors="coerce")
    work["y"] = pd.to_numeric(work["y"], errors="coerce")
    work = work.dropna(subset=["fecha"])
    if work.empty:
        return pd.DataFrame(), {}

    work["fecha_m"] = work["fecha"].dt.to_period("M").dt.to_timestamp(how="start")
    work = (
        work.groupby("fecha_m", as_index=False)["y"]
        .mean()
        .rename(columns={"fecha_m": "fecha"})
        .sort_values("fecha")
    )

    full_hist = pd.date_range(work["fecha"].min(), work["fecha"].max(), freq="MS")
    base = pd.DataFrame({"fecha": full_hist})
    base = base.merge(work, on="fecha", how="left")
    base.rename(columns={"y": "observation"}, inplace=True)

    if int(n_future_months) > 0:
        fut = pd.date_range(
            full_hist.max() + pd.DateOffset(months=1),
            periods=int(n_future_months),
            freq="MS",
        )
        base = pd.concat(
            [base, pd.DataFrame({"fecha": fut, "observation": np.nan})],
            ignore_index=True,
        )

    base["t_idx"] = np.arange(len(base), dtype=float)
    base["Trend"] = (base["t_idx"] + 1.0) / 12.0
    base["mes"] = base["fecha"].dt.month.astype(int)

    month_dums = pd.get_dummies(base["mes"], prefix="m", drop_first=True)
    X = pd.concat([month_dums, base[["Trend"]]], axis=1)
    X = sm.add_constant(X, has_constant="add")
    X = X.apply(pd.to_numeric, errors="coerce").astype(np.float64)
    mask = base["observation"].notna()
    if int(mask.sum()) < 14:
        return pd.DataFrame(), {}

    endog = pd.to_numeric(base.loc[mask, "observation"], errors="coerce").astype(np.float64)
    model = sm.OLS(endog, X.loc[mask]).fit()
    fitted = model.predict(X).to_numpy(dtype=float)
    beta_trend = float(model.params.get("Trend", np.nan))
    trend_component = beta_trend * base["Trend"].to_numpy(dtype=float)
    seasonal_component = fitted - trend_component

    out = pd.DataFrame(
        {
            "fecha": base["fecha"],
            "observation": base["observation"],
            "trend": trend_component,
            "seasonal": seasonal_component,
            "fitted": fitted,
        }
    )
    out["residual"] = out["observation"] - out["fitted"]

    meta: dict[str, float] = {
        "slope_per_year": beta_trend,
        "p_value_trend": float(model.pvalues.get("Trend", np.nan)),
        "adj_r2": float(model.rsquared_adj),
        "r2": float(model.rsquared),
    }
    return out, meta


def decompose_marcos_holdout_last_n(
    df: pd.DataFrame,
    *,
    col_fecha: str,
    col_y: str,
    holdout_months: int = 12,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Variante operativa para ATAC:
    - Ajusta Marcos SOLO con histórico (excluye los últimos `holdout_months`
      meses observados).
    - Predice (tendencia+estacionalidad) para TODO el rango mensual continuo,
      incluyendo esos últimos meses (para compararlos contra bandas).
    """
    holdout_months = int(max(1, holdout_months))

    work = df[[col_fecha, col_y]].copy()
    work.columns = ["fecha", "y"]
    work["fecha"] = pd.to_datetime(work["fecha"], errors="coerce")
    work["y"] = pd.to_numeric(work["y"], errors="coerce")
    work = work.dropna(subset=["fecha"])
    if work.empty:
        return pd.DataFrame(), {"error": "empty"}

    # Serie mensual (media si hay múltiple dentro de mes).
    # La clave debe ser columna del DataFrame: groupby(Series externa) puede no
    # devolver "fecha" en pandas recientes (KeyError en sort_values).
    work["fecha_m"] = work["fecha"].dt.to_period("M").dt.to_timestamp(how="start")
    work = (
        work.groupby("fecha_m", as_index=False)["y"]
        .mean()
        .rename(columns={"fecha_m": "fecha"})
        .sort_values("fecha")
    )

    full_hist = pd.date_range(work["fecha"].min(), work["fecha"].max(), freq="MS")
    base = pd.DataFrame({"fecha": full_hist})
    base = base.merge(work, on="fecha", how="left")
    base.rename(columns={"y": "observation"}, inplace=True)

    obs_months = base.loc[base["observation"].notna(), "fecha"].to_list()
    if len(obs_months) < (holdout_months + 14):
        return pd.DataFrame(), {"error": "insufficient_observed_months", "n_obs_months": float(len(obs_months))}

    cutoff = obs_months[-holdout_months]
    train_mask = (base["fecha"] < cutoff) & base["observation"].notna()
    if int(train_mask.sum()) < 14:
        return pd.DataFrame(), {"error": "insufficient_train_points", "n_train": float(train_mask.sum())}

    base["t_idx"] = np.arange(len(base), dtype=float)
    base["Trend"] = (base["t_idx"] + 1.0) / 12.0
    base["mes"] = base["fecha"].dt.month.astype(int)

    month_dums = pd.get_dummies(base["mes"], prefix="m", drop_first=True)
    X = pd.concat([month_dums, base[["Trend"]]], axis=1)
    X = sm.add_constant(X, has_constant="add")
    X = X.apply(pd.to_numeric, errors="coerce").astype(np.float64)

    endog = pd.to_numeric(base.loc[train_mask, "observation"], errors="coerce").astype(np.float64)
    model = sm.OLS(endog, X.loc[train_mask]).fit()

    fitted_all = model.predict(X).to_numpy(dtype=float)
    out = pd.DataFrame(
        {
            "fecha": base["fecha"],
            "observation": base["observation"],
            "fitted": fitted_all,
        }
    )
    out["residual"] = out["observation"] - out["fitted"]
    out["cutoff_holdout_start"] = pd.to_datetime(cutoff)
    out["is_holdout"] = out["fecha"] >= pd.to_datetime(cutoff)

    meta: dict[str, Any] = {
        "cutoff_holdout_start": pd.to_datetime(cutoff),
        "holdout_months": float(holdout_months),
        "slope_per_year": float(model.params.get("Trend", np.nan)),
        "p_value_trend": float(model.pvalues.get("Trend", np.nan)),
        "adj_r2": float(model.rsquared_adj),
        "r2": float(model.rsquared),
        "n_train_points": float(train_mask.sum()),
    }
    return out, meta


def atac_holdout_bands_on_residuals(
    residuals: pd.Series,
    *,
    cutoff_holdout_start: pd.Timestamp,
    holdout_months: int = 12,
    ar_candidates: tuple[int, ...] = ATAC_AR_CANDIDATES,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    ATAC para el caso que nos interesa:
    - Ajusta un AR(p) sobre residuos SOLO en el tramo anterior al cutoff.
    - Predice bandas (50/75/95) para:
        - el tramo de entrenamiento (banda azul/“esperable”)
        - el tramo holdout (últimos 12 meses) como “forecast” para detectar anomalía.

    Nota práctica sobre huecos:
    - Si faltan meses, interpolamos residuos a malla mensual para ajustar AR
      (para evitar varianzas absurdas por gaps).
    """
    holdout_months = int(max(1, holdout_months))
    s = residuals.copy()
    if not isinstance(s.index, pd.DatetimeIndex):
        s.index = pd.to_datetime(s.index, errors="coerce")
        s = s[~s.index.isna()]
    s = s.sort_index().astype(float)

    # Solo histórico de entrenamiento
    s_train = s[s.index < cutoff_holdout_start].dropna()
    if len(s_train) < 20:
        return pd.DataFrame(), {"error": "insufficient_residual_train", "n_train": float(len(s_train))}

    full_idx = pd.date_range(s_train.index.min(), cutoff_holdout_start - pd.DateOffset(months=1), freq="MS")
    y = s_train.reindex(full_idx)
    # Interpolar huecos (solo para ajuste AR)
    y = y.interpolate(method="linear", limit_direction="both")

    best_res = None
    best_aic = np.inf
    best_p = 0
    for p in ar_candidates:
        try:
            if p == 0:
                mod = SARIMAX(y, order=(0, 0, 0), trend="c", enforce_stationarity=False, enforce_invertibility=False)
            else:
                mod = SARIMAX(y, order=(p, 0, 0), trend="n", enforce_stationarity=False, enforce_invertibility=False)
            res = mod.fit(disp=False, maxiter=500)
            if np.isfinite(res.aic) and float(res.aic) < best_aic:
                best_aic = float(res.aic)
                best_res = res
                best_p = p
        except Exception as ex:  # noqa: BLE001
            logger.debug("ATAC holdout SARIMAX fallo (p=%s): %s", p, ex)
            continue

    if best_res is None:
        return pd.DataFrame(), {"error": "fit_failed"}

    pred = best_res.get_prediction(start=0, end=len(y) - 1)
    hist = pd.DataFrame({"fecha": full_idx, "resid_mean": np.asarray(pred.predicted_mean.values, dtype=float)})
    for alpha, lab in [(0.05, "95"), (0.25, "75"), (0.50, "50")]:
        ci = pred.conf_int(alpha=alpha)
        hist[f"resid_lo_{lab}"] = np.asarray(ci.iloc[:, 0].values, dtype=float)
        hist[f"resid_hi_{lab}"] = np.asarray(ci.iloc[:, 1].values, dtype=float)

    fc = best_res.get_forecast(steps=holdout_months)
    fc_idx = pd.date_range(cutoff_holdout_start, periods=holdout_months, freq="MS")
    fut = pd.DataFrame({"fecha": fc_idx, "resid_fc_mean": np.asarray(fc.predicted_mean.values, dtype=float)})
    for alpha, lab in [(0.05, "95"), (0.25, "75"), (0.50, "50")]:
        ci = fc.conf_int(alpha=alpha)
        fut[f"resid_fc_lo_{lab}"] = np.asarray(ci.iloc[:, 0].values, dtype=float)
        fut[f"resid_fc_hi_{lab}"] = np.asarray(ci.iloc[:, 1].values, dtype=float)

    out = pd.concat([hist, fut], ignore_index=True)
    meta = {"ar_order": int(best_p), "aic": float(best_aic)}
    return out, meta

def atac_forecast(
    residual_hist: pd.Series,
    *,
    n_forecast: int,
    ar_candidates: tuple[int, ...] = ATAC_AR_CANDIDATES,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Metodología ATAC (adaptada): modelo autorregresivo de orden 0, 1 o 2 sobre
    los residuos de Marcos; selección por AIC; bandas de predicción/pronóstico
    al 50 %, 75 % y 95 %.
    """
    s = residual_hist.copy().sort_index()
    if not isinstance(s.index, pd.DatetimeIndex):
        s.index = pd.to_datetime(s.index, errors="coerce")
        s = s[~s.index.isna()]
    s = s.astype(float)

    if s.empty:
        return pd.DataFrame(), {"ar_order": None, "error": "empty_residuals"}

    full_idx = pd.date_range(s.index.min(), s.index.max(), freq="MS")
    y = s.reindex(full_idx).astype(float)
    n_obs = int(y.notna().sum())
    if n_obs < 4:
        return pd.DataFrame(), {"ar_order": None, "error": "insufficient_data", "n_obs": float(n_obs)}

    eff_horizon = (
        int(n_forecast)
        if n_obs >= ATAC_SHORT_SERIES_MONTHS
        else min(int(n_forecast), ATAC_FORECAST_SHORT)
    )
    candidates = (0,) if n_obs < ATAC_SHORT_SERIES_MONTHS else tuple(ar_candidates)

    best_res = None
    best_aic = np.inf
    best_p = 0
    for p in candidates:
        try:
            if p == 0:
                mod = SARIMAX(
                    y,
                    order=(0, 0, 0),
                    trend="c",
                    enforce_stationarity=False,
                    enforce_invertibility=False,
                )
            else:
                mod = SARIMAX(
                    y,
                    order=(p, 0, 0),
                    trend="n",
                    enforce_stationarity=False,
                    enforce_invertibility=False,
                )
            fitted_m = mod.fit(disp=False, maxiter=500)
            if np.isfinite(fitted_m.aic) and fitted_m.aic < best_aic:
                best_aic = float(fitted_m.aic)
                best_res = fitted_m
                best_p = p
        except Exception as ex:  # noqa: BLE001
            logger.debug("SARIMAX fallo (p=%s): %s", p, ex)
            continue

    if best_res is None:
        return pd.DataFrame(), {"ar_order": None, "error": "fit_failed"}

    pred = best_res.get_prediction(start=0, end=len(y) - 1)
    pm = pred.predicted_mean
    if hasattr(pm, "values"):
        pmv = np.asarray(pm.values, dtype=float).ravel()
    else:
        pmv = np.asarray(pm, dtype=float).ravel()
    if pmv.size != len(full_idx):
        pm_s = pred.predicted_mean.reindex(full_idx)
        pmv = np.asarray(pm_s.values, dtype=float).ravel()

    hist_df = pd.DataFrame({"fecha": full_idx, "resid_pred_mean": pmv})

    for alpha, lab in [(0.05, "95"), (0.25, "75"), (0.50, "50")]:
        try:
            ci = pred.conf_int(alpha=alpha)
            lo = np.asarray(ci.iloc[:, 0].values, dtype=float).ravel()
            hi = np.asarray(ci.iloc[:, 1].values, dtype=float).ravel()
            if lo.size != len(full_idx):
                lo_s = ci.iloc[:, 0].reindex(full_idx)
                hi_s = ci.iloc[:, 1].reindex(full_idx)
                lo = np.asarray(lo_s.values, dtype=float).ravel()
                hi = np.asarray(hi_s.values, dtype=float).ravel()
            hist_df[f"resid_pred_lo_{lab}"] = lo
            hist_df[f"resid_pred_hi_{lab}"] = hi
        except Exception as ex:  # noqa: BLE001
            logger.debug("conf_int hist alpha=%s: %s", alpha, ex)
            hist_df[f"resid_pred_lo_{lab}"] = np.nan
            hist_df[f"resid_pred_hi_{lab}"] = np.nan

    fc = best_res.get_forecast(steps=eff_horizon)
    fc_idx = pd.date_range(full_idx[-1] + pd.DateOffset(months=1), periods=eff_horizon, freq="MS")
    fc_mean = np.asarray(fc.predicted_mean.values, dtype=float).ravel()
    fc_df = pd.DataFrame({"fecha": fc_idx, "resid_fc_mean": fc_mean})

    for alpha, lab in [(0.05, "95"), (0.25, "75"), (0.50, "50")]:
        try:
            ci = fc.conf_int(alpha=alpha)
            fc_df[f"resid_fc_lo_{lab}"] = np.asarray(ci.iloc[:, 0].values, dtype=float).ravel()
            fc_df[f"resid_fc_hi_{lab}"] = np.asarray(ci.iloc[:, 1].values, dtype=float).ravel()
        except Exception as ex:  # noqa: BLE001
            logger.debug("conf_int fc alpha=%s: %s", alpha, ex)
            fc_df[f"resid_fc_lo_{lab}"] = np.nan
            fc_df[f"resid_fc_hi_{lab}"] = np.nan

    out = pd.concat([hist_df, fc_df], ignore_index=True)
    meta: dict[str, Any] = {
        "ar_order": int(best_p),
        "aic": float(best_aic),
        "forecast_horizon": int(eff_horizon),
        "n_obs_residual": float(n_obs),
    }
    return out, meta


def marcos_atac_pipeline(
    df_monthly: pd.DataFrame,
    *,
    col_fecha: str = "fecha",
    col_y: str = "temp_5m",
    n_forecast: int = ATAC_FORECAST_MONTHS,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Encadena descomposición Marcos + ATAC sobre residuos y deriva bandas de
    temperatura futura sumando el ajuste Marcos (`fitted`) y las bandas del residuo.
    """
    dec, meta_m = decompose_marcos(
        df_monthly,
        col_fecha=col_fecha,
        col_y=col_y,
        n_future_months=int(n_forecast),
    )
    if dec.empty:
        return pd.DataFrame(), meta_m

    obs_ok = dec["observation"].notna()
    if not obs_ok.any():
        return pd.DataFrame(), meta_m

    last_obs_date = dec.loc[obs_ok, "fecha"].max()
    rs = dec.loc[dec["fecha"] <= last_obs_date].set_index("fecha")["residual"]

    atac_df, meta_a = atac_forecast(rs, n_forecast=int(n_forecast))
    if atac_df.empty:
        pipe = dec.copy()
        pipe["ar_order"] = np.nan
        meta_out: dict[str, Any] = {**meta_m, **meta_a}
        return pipe, meta_out

    pipe = dec.merge(atac_df, on="fecha", how="left")

    fut_mask = pipe["observation"].isna()
    if "resid_fc_mean" in pipe.columns:
        pipe.loc[fut_mask, "temp_fc_mean"] = pipe.loc[fut_mask, "fitted"] + pipe.loc[fut_mask, "resid_fc_mean"].fillna(
            0.0
        )
        for lab in ["95", "75", "50"]:
            lo_col = f"resid_fc_lo_{lab}"
            hi_col = f"resid_fc_hi_{lab}"
            if lo_col in pipe.columns:
                pipe.loc[fut_mask, f"temp_fc_lo_{lab}"] = pipe.loc[fut_mask, "fitted"] + pipe.loc[fut_mask, lo_col].fillna(
                    0.0
                )
            if hi_col in pipe.columns:
                pipe.loc[fut_mask, f"temp_fc_hi_{lab}"] = pipe.loc[fut_mask, "fitted"] + pipe.loc[fut_mask, hi_col].fillna(
                    0.0
                )

    for lab in ["95", "75", "50"]:
        lo_c = f"resid_pred_lo_{lab}"
        hi_c = f"resid_pred_hi_{lab}"
        if lo_c in pipe.columns:
            pipe[f"temp_pred_lo_{lab}"] = pipe["fitted"] + pipe[lo_c].fillna(0.0)
        if hi_c in pipe.columns:
            pipe[f"temp_pred_hi_{lab}"] = pipe["fitted"] + pipe[hi_c].fillna(0.0)

    pipe["ar_order"] = meta_a.get("ar_order", np.nan)
    meta_out: dict[str, Any] = {**meta_m, **meta_a}
    return pipe, meta_out


def _month_index(dates: pd.Series) -> np.ndarray:
    """
    Índice temporal entero (meses desde el primer mes).

    Esto permite usar el modelo de Fourier sobre una escala uniforme:
      t = 0, 1, 2, ... (unidades: meses)
    """
    d = pd.to_datetime(dates, errors="coerce")
    if d.isna().all():
        return np.array([], dtype=int)
    y0 = int(d.min().year)
    m0 = int(d.min().month)
    return ((d.dt.year - y0) * 12 + (d.dt.month - m0)).to_numpy(dtype=int)


def fit_fourier_seasonality(
    df_monthly: pd.DataFrame,
    *,
    col_fecha: str = "fecha",
    col_y: str = "yhat",
    K: int = FOURIER_K,
) -> Dict[str, object]:
    """
    Ajusta un componente estacional continuo mediante armónicos de Fourier.

    Modelo
    ------
      y_t = α + Σ_{k=1..K} [ a_k sin(2π k t / 12) + b_k cos(2π k t / 12) ]

    donde t está en meses (0,1,2,...) y 12 es el periodo anual en meses.

    Devuelve coeficientes y la serie estacional estimada para todas las fechas
    presentes en `df_monthly` (incluye meses sin dato en y).
    """
    work = df_monthly[[col_fecha, col_y]].copy()
    work[col_fecha] = pd.to_datetime(work[col_fecha], errors="coerce")
    work[col_y] = pd.to_numeric(work[col_y], errors="coerce")
    work = work.dropna(subset=[col_fecha])
    if work.empty:
        return {"coef": None, "seasonal": pd.Series(dtype=float)}

    t_all = _month_index(work[col_fecha])
    if t_all.size == 0:
        return {"coef": None, "seasonal": pd.Series(dtype=float)}

    # Construir X(t) para todos los meses
    cols = [np.ones_like(t_all, dtype=float)]
    for k in range(1, int(max(1, K)) + 1):
        w = 2.0 * np.pi * k * t_all / 12.0
        cols.append(np.sin(w))
        cols.append(np.cos(w))
    X_all = np.column_stack(cols)

    # Ajustar solo con y no-nulo (meses observados)
    mask = work[col_y].notna().to_numpy()
    if mask.sum() < (2 * int(max(1, K)) + 1):
        # No hay suficientes puntos para estimar K armónicos + intercepto
        # Fallback: estacionalidad = 0
        seasonal = pd.Series(np.zeros(len(work), dtype=float), index=work[col_fecha])
        return {"coef": None, "seasonal": seasonal}

    y_obs = work.loc[mask, col_y].to_numpy(dtype=float)
    X_obs = X_all[mask, :]
    coef, *_ = np.linalg.lstsq(X_obs, y_obs, rcond=None)

    seasonal_hat = X_all @ coef
    seasonal = pd.Series(seasonal_hat.astype(float), index=work[col_fecha])
    return {"coef": coef, "seasonal": seasonal}


def ar1_fit(x: pd.Series) -> Dict[str, float]:
    """
    Ajuste AR(1) por MCO:
      X_t = c + φ X_{t-1} + ε_t

    Devuelve:
      c, phi, sigma (desv. típ. innovación), n (muestras efectivas)
    """
    s = pd.to_numeric(x, errors="coerce").dropna().astype(float)
    if len(s) < 3:
        return {"c": float("nan"), "phi": float("nan"), "sigma": float("nan"), "n": float(len(s))}

    y = s.iloc[1:].to_numpy()
    xlag = s.iloc[:-1].to_numpy()
    X = np.column_stack([np.ones_like(xlag), xlag])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    c = float(beta[0])
    phi = float(beta[1])

    resid = y - (c + phi * xlag)
    dof = max(1, len(resid) - 2)
    sigma = float(np.sqrt(np.sum(resid**2) / dof))
    return {"c": c, "phi": phi, "sigma": sigma, "n": float(len(s))}


def ar1_forecast(
    *,
    last_x: float,
    c: float,
    phi: float,
    sigma: float,
    steps: int,
    start_date: pd.Timestamp,
    z: float = 1.96,
) -> pd.DataFrame:
    """
    Forecast AR(1) h pasos (mensual) con IC aproximado.

    Varianza acumulada del error de predicción:
      Var_h = σ^2 * Σ_{i=0..h-1} φ^{2i}
    """
    steps = int(max(0, steps))
    if steps == 0:
        return pd.DataFrame(columns=["fecha", "ar1_mean", "ar1_lower", "ar1_upper"])

    fechas = pd.date_range(start=start_date, periods=steps, freq="MS")
    means = np.empty(steps, dtype=float)
    ses = np.empty(steps, dtype=float)

    x_prev = float(last_x)
    for h in range(1, steps + 1):
        x_hat = float(c + phi * x_prev)
        means[h - 1] = x_hat

        # Σ φ^{2i} desde i=0..h-1
        if abs(phi) < 1e-12:
            var_h = float((sigma**2) * 1.0)
        else:
            var_h = float((sigma**2) * (1.0 - (phi ** (2 * h))) / (1.0 - phi**2))
        ses[h - 1] = float(np.sqrt(max(0.0, var_h)))

        x_prev = x_hat

    out = pd.DataFrame(
        {
            "fecha": fechas,
            "ar1_mean": means,
            "ar1_lower": means - z * ses,
            "ar1_upper": means + z * ses,
        }
    )
    return out


class OceanForecaster:
    """
    Clase modular orientada a objetos para el modelado de series temporales
    oceanográficas. Diseñada con el patrón Strategy para permitir la inyección
    de Foundation Models en el futuro.
    """
    
    def __init__(self, horizon_months: int = FORECAST_HORIZON_YEARS * 12, alpha: float = CONFIDENCE_LEVEL):
        self.horizon: int = int(horizon_months)
        self.alpha: float = alpha
        self.model: Any = None
        self.results: Any = None
        self._fallback_mean: float = 0.0
        self._fallback_std: float = 0.0
        self._last_date: Optional[pd.Timestamp] = None
        
    def _prepare_data(self, df: pd.DataFrame, target: str) -> pd.Series:
        """
        Prepara y asegura la continuidad temporal de la serie.
        """
        ts = df.set_index('fecha')[target].sort_index()
        # Rellenar huecos mensuales (malla estricta ya viene del binning)
        ts = ts.interpolate(method='linear')
        return ts
        
    def fit(self, df: pd.DataFrame, target: str) -> None:
        """
        Entrena el modelo base (SARIMAX baseline).
        
        [!] PUNTO DE ENCHUFE PARA FOUNDATION MODELS:
        Aquí es donde en el futuro se instanciaría el cliente de TimeGPT,
        o se cargarían los pesos pre-entrenados de PatchTST (HuggingFace)
        en lugar del SARIMAX clásico.
        """
        ts = self._prepare_data(df, target)
        
        # Necesitamos un mínimo de datos para que ARIMA/SARIMAX converja
        if len(ts) < 3:
            logger.warning("Histórico demasiado corto para un modelado complejo. Usando fallback basal (Media Variante).")
            self.model = "fallback"
            self._fallback_mean = ts.mean()
            self._fallback_std = ts.std() if len(ts) > 1 else 0.5
            self._last_date = ts.index[-1]
            return

        try:
            # Modelo Baseline: SARIMAX(1, 1, 1) sencillo. 
            # En un entorno de producción, los parámetros (p,d,q) se optimizarían 
            # dinámicamente usando auto_arima.
            self.model = sm.tsa.statespace.SARIMAX(
                ts, 
                order=(1, 1, 1), 
                enforce_stationarity=False, 
                enforce_invertibility=False
            )
            self.results = self.model.fit(disp=False)
        except Exception as e:
            logger.error(f"Error entrenando SARIMAX: {str(e)}. Activando Fallback.")
            self.model = "fallback"
            self._fallback_mean = ts.mean()
            self._fallback_std = ts.std() if len(ts) > 1 else 0.5
            self._last_date = ts.index[-1]

    def predict(self) -> pd.DataFrame:
        """
        Genera el pronóstico futuro con banda de confianza.
        
        Returns:
            pd.DataFrame con columnas [fecha, target, target_lower, target_upper, tipo]
        """
        if self.model == "fallback":
            # Generar fechas futuras (meses)
            future_dates = pd.date_range(
                start=self._last_date + pd.DateOffset(months=1),
                periods=self.horizon, 
                freq='MS'
            )
            
            # Predicción simple: Media móvil amortiguada o flat + ruido
            yhat = [self._fallback_mean] * self.horizon
            margin_error = self._fallback_std * 1.96 # Aprox 95% CI
            
            forecast_df = pd.DataFrame({
                'fecha': future_dates,
                'forecast_mean': yhat,
                'forecast_lower': np.array(yhat) - margin_error,
                'forecast_upper': np.array(yhat) + margin_error
            })
            return forecast_df
            
        # Predicción real usando Statsmodels
        pred = self.results.get_forecast(steps=self.horizon)
        pred_ci = pred.conf_int(alpha=self.alpha)
        
        forecast_df = pd.DataFrame({
            'fecha': pred.predicted_mean.index,
            'forecast_mean': pred.predicted_mean.values,
            'forecast_lower': pred_ci.iloc[:, 0].values,
            'forecast_upper': pred_ci.iloc[:, 1].values
        })
        
        return forecast_df

def process_station_forecast(df_station: pd.DataFrame, station_name: str, target: str) -> pd.DataFrame:
    """
    Ejecuta el ciclo de modelado completo de histórico + pronóstico para una estación.
    """
    # 1. Asegurar formato temporal anual (preferencia: fecha_muestreo -> fecha -> ano/year)
    if "fecha_muestreo" in df_station.columns:
        df_station["fecha"] = pd.to_datetime(df_station["fecha_muestreo"], errors="coerce")
    elif "fecha" in df_station.columns:
        df_station["fecha"] = pd.to_datetime(df_station["fecha"], errors="coerce")
    else:
        year_col = next((c for c in ("ano", "año", "year", "anio", "yy") if c in df_station.columns), None)
        if year_col is not None:
            years = pd.to_numeric(df_station[year_col], errors="coerce")
            df_station["fecha"] = pd.to_datetime(years.astype("Int64").astype(str) + "-06-15", errors="coerce")
        else:
            return pd.DataFrame()

    df_station = df_station.dropna(subset=["fecha"])
    
    if df_station.empty:
        return pd.DataFrame()
        
    # ----------------------------------------------------------
    # (A) BINNING MENSUAL + CLIMATOLOGÍA + ANOMALÍA
    # ----------------------------------------------------------
    monthly = monthly_bin_and_anomaly(df_station, col_fecha="fecha", col_valor=target)
    if monthly.empty:
        return pd.DataFrame()
    monthly["estacion"] = station_name

    # Climatología mensual (12 bins) por estación para aplicar también al pronóstico
    clim_map = (
        monthly.dropna(subset=[target])
        .groupby("mes")[target]
        .mean()
        .to_dict()
    )
    
    # 2. Marcar datos históricos (mensual)
    # Nota: mantenemos `yhat` como nombre estándar de "valor observado/modelado"
    # para compatibilidad con consumidores existentes.
    df_historical = monthly[["fecha", "estacion", target, "mes", "climatologia_mes", "anomalia_mensual"]].copy()
    df_historical["yhat_lower"] = df_historical[target]
    df_historical["yhat_upper"] = df_historical[target]
    df_historical.rename(columns={target: "yhat"}, inplace=True)
    df_historical["tipo_dato"] = "histórico"

    # ----------------------------------------------------------
    # (B) DESESTACIONALIZACIÓN CONTINUA (FOURIER)
    # ----------------------------------------------------------
    fourier_fit = fit_fourier_seasonality(monthly, col_fecha="fecha", col_y=target, K=FOURIER_K)
    seasonal_hist = fourier_fit["seasonal"]
    # Alinear por fecha (inicio de mes)
    df_historical["seasonal_fourier"] = df_historical["fecha"].map(seasonal_hist.to_dict())
    df_historical["temp_deseasonal_fourier"] = df_historical["yhat"] - df_historical["seasonal_fourier"]

    # Anomalía desestacionalizada (centrada a media histórica)
    mu_ds = float(df_historical["temp_deseasonal_fourier"].mean(skipna=True))
    df_historical["anomalia_fourier"] = df_historical["temp_deseasonal_fourier"] - mu_ds
    
    # 3. Modelado Predictivo
    forecaster = OceanForecaster()
    
    # [FEATURE FLAG] En el futuro:
    # forecaster = DeepOceanForecaster(foundation_model="TimeGPT")
    
    # 3. Modelado Predictivo (mensual): usa serie `yhat` (observada mensual) como target
    df_model = monthly[["fecha", target]].copy()
    forecaster.fit(df_model, target)
    df_future = forecaster.predict()
    
    # 4. Formatear y alinear DataFrame del pronóstico
    df_future['estacion'] = station_name
    df_future.rename(columns={
        'forecast_mean': 'yhat',
        'forecast_lower': 'yhat_lower',
        'forecast_upper': 'yhat_upper'
    }, inplace=True)
    df_future['tipo_dato'] = 'pronóstico'

    df_future["mes"] = df_future["fecha"].dt.month
    df_future["climatologia_mes"] = df_future["mes"].map(clim_map)
    df_future["anomalia_mensual"] = df_future["yhat"] - df_future["climatologia_mes"]

    # Fourier también para el futuro: extrapola el componente estacional
    # usando el mismo ajuste (t en meses desde el inicio de la serie histórica).
    if isinstance(fourier_fit.get("coef", None), np.ndarray):
        coef = fourier_fit["coef"]  # type: ignore[assignment]
        # construir t para futuro con el mismo origen (primer mes del monthly)
        origin = pd.to_datetime(monthly["fecha"].min())
        t_future = ((df_future["fecha"].dt.year - origin.year) * 12 + (df_future["fecha"].dt.month - origin.month)).to_numpy(dtype=int)
        cols_f = [np.ones_like(t_future, dtype=float)]
        for k in range(1, int(max(1, FOURIER_K)) + 1):
            w = 2.0 * np.pi * k * t_future / 12.0
            cols_f.append(np.sin(w))
            cols_f.append(np.cos(w))
        Xf = np.column_stack(cols_f)
        seasonal_future = (Xf @ coef).astype(float)
        df_future["seasonal_fourier"] = seasonal_future
        df_future["temp_deseasonal_fourier"] = df_future["yhat"] - df_future["seasonal_fourier"]
        df_future["anomalia_fourier"] = df_future["temp_deseasonal_fourier"] - mu_ds
    else:
        df_future["seasonal_fourier"] = np.nan
        df_future["temp_deseasonal_fourier"] = np.nan
        df_future["anomalia_fourier"] = np.nan

    # ----------------------------------------------------------
    # (C) AR(1) SOBRE ANOMALÍA MENSUAL + FORECAST CORTO (3–6 meses)
    # ----------------------------------------------------------
    ar1 = ar1_fit(df_historical["anomalia_mensual"])
    c = float(ar1["c"])
    phi = float(ar1["phi"])
    sigma = float(ar1["sigma"])

    # Guardar parámetros AR(1) como metadatos repetidos (fácil consumo en viz)
    for df_ in (df_historical, df_future):
        df_["ar1_c"] = c
        df_["ar1_phi"] = phi
        df_["ar1_sigma"] = sigma

    # Fitted 1-step (histórico): c + phi * X_{t-1}
    x_hist = pd.to_numeric(df_historical["anomalia_mensual"], errors="coerce")
    df_historical["ar1_fitted_1step"] = c + phi * x_hist.shift(1)
    df_historical["ar1_resid_1step"] = x_hist - df_historical["ar1_fitted_1step"]

    # Forecast AR(1) (solo primeros N meses futuros, por estándar operativo)
    last_obs = x_hist.dropna()
    if last_obs.empty or not np.isfinite(c) or not np.isfinite(phi) or not np.isfinite(sigma):
        df_future["ar1_anom_mean"] = np.nan
        df_future["ar1_anom_lower"] = np.nan
        df_future["ar1_anom_upper"] = np.nan
    else:
        start_date = pd.to_datetime(df_historical["fecha"].max()) + pd.DateOffset(months=1)
        steps = int(min(AR1_FORECAST_MONTHS, len(df_future)))
        df_ar1f = ar1_forecast(
            last_x=float(last_obs.iloc[-1]),
            c=c,
            phi=phi,
            sigma=sigma,
            steps=steps,
            start_date=pd.to_datetime(start_date),
        )
        df_future = df_future.merge(df_ar1f, on="fecha", how="left")
        df_future.rename(
            columns={"ar1_mean": "ar1_anom_mean", "ar1_lower": "ar1_anom_lower", "ar1_upper": "ar1_anom_upper"},
            inplace=True,
        )
        # Para meses más allá del horizonte corto, quedan NaN (intencional).
    
    # 5. Concatenar Histórico + Futuro
    df_final = pd.concat([df_historical, df_future], ignore_index=True)
    return df_final


def main():
    current_script_path = Path(__file__).resolve()
    project_root = current_script_path.parent.parent
    
    logger.info("Iniciando Previsión Operativa Oceánica (Time Series Forecasting)")
    
    input_file = project_root / "data" / "processed" / "sireno_gijon_ctd_processed.csv"
    output_file = project_root / "data" / "processed" / "sireno_gijon_ctd_forecast.csv"
    
    if not input_file.exists():
        logger.error(f"Archivo de entrada no encontrado: {input_file}")
        logger.error("Asegúrese de ejecutar primero '01_agent_inspector.py'")
        return
        
    df_processed = pd.read_csv(input_file)
    
    # Validar que existe la variable o usar fallback paramétrico
    target_var = TARGET_VARIABLE 
    if target_var not in df_processed.columns:
        # Fallback a la primera numérica disponible
        num_cols = df_processed.select_dtypes(include=np.number).columns.tolist()
        if num_cols:
            target_var = num_cols[0]
            logger.warning(f"Variable '{TARGET_VARIABLE}' no encontrada. Modelando: {target_var}")
        else:
            logger.error("No se encontraron variables numéricas para pronosticar.")
            return

    if "estacion" in df_processed.columns:
        estaciones = df_processed["estacion"].dropna().unique()
    else:
        df_processed["estacion"] = "global"
        estaciones = ["global"]
    logger.info(f"Target detectado: '{target_var}'. Estaciones a procesar: {len(estaciones)}")
    
    all_forecasts = []
    
    # Modelar cada estación iterativamente
    for st in estaciones:
        logger.info(f"Entrenando modelo Baseline (Statsmodels) para estación: {st}...")
        df_st = df_processed[df_processed['estacion'] == st].copy()
        df_forecast_st = process_station_forecast(df_st, st, target_var)
        
        if not df_forecast_st.empty:
            all_forecasts.append(df_forecast_st)
            
    if not all_forecasts:
        logger.error("No se pudo generar ningún pronóstico válido debido a falta explícita de datos históricos.")
        return
        
    df_final_results = pd.concat(all_forecasts, ignore_index=True)
    
    # Guardar resultados
    df_final_results.to_csv(output_file, index=False)
    
    # Resumen Ejecutivo
    print("\n" + "="*80)
    print(" RESUMEN OPERATIVO: PRONÓSTICO DE SERIES TEMPORALES (MODULO 02) ".center(80, "="))
    print("="*80)
    print(f"Estaciones modeladas      : {len(estaciones)}")
    print(f"Horizonte de proyección   : {FORECAST_HORIZON_YEARS} años")
    print(f"Resolución temporal       : Mensual (malla MS; 12 bins/año)")
    print(f"Intervalo de confianza    : {(1-CONFIDENCE_LEVEL)*100:.0f}%")
    print(f"Variable analizada        : {target_var}")
    print("-" * 80)
    print(f"Arquitectura              : Modular (OceanForecaster base class)")
    print(f"Backend Actual            : ARIMA / Statsmodels StateSpace")
    print(f"Preparación (Scale-out)   : Lista para inyección de Foundation Models (TimeGPT/PatchTST)")
    print("-" * 80)
    print("Qué datos estás viendo en el CSV exportado")
    print(" - `tipo_dato = histórico`: valores observados agregados a media mensual (si hay varios muestreos en un mes, se promedian).")
    print(" - `climatologia_mes`      : media histórica para ese mes (enero..diciembre) usando toda la serie disponible.")
    print(" - `anomalia_mensual`      : `yhat` (mensual) - `climatologia_mes` del mismo mes (también en pronóstico).")
    print(" - `seasonal_fourier`      : componente estacional continuo (K armónicos) ajustado sobre el histórico mensual.")
    print(" - `temp_deseasonal_fourier`: `yhat` - `seasonal_fourier` (señal sin ciclo intra‑anual continuo).")
    print(" - `ar1_phi`/`ar1_c`       : parámetros AR(1) ajustados sobre `anomalia_mensual` histórica.")
    print(" - `ar1_anom_mean` (+IC)   : forecast AR(1) corto (primeros meses futuros; resto NaN por diseño).")
    print("-" * 80)
    print(f"[OK] Archivo exportado -> {output_file.relative_to(project_root)}")
    print("="*80 + "\n")

if __name__ == "__main__":
    main()

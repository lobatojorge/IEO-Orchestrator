"""
experimentacion_anomalias.py
============================
Comparación de métodos de desestacionalización para series de temperatura (TFM).

Métodos
-------
- **anomalia_marcos**: modelo lineal temperatura ~ mes (efectos fijos) + tendencia lineal
  en el índice temporal; anomalía = residuo (statsmodels OLS).
- **anomalia_fourier**: ajuste de un armónico anual (seno/coseno) con scipy.optimize.curve_fit;
  anomalía = observado − ajuste.
- **anomalia_wginor**: alineado con la lógica usada en el estándar de series WGINOR / pipeline
  del proyecto (véase ``monthly_bin_and_anomaly`` en ``02_analysis.py``): agregación mensual,
  climatología = media histórica por mes calendario (1–12) usando solo meses con dato,
  anomalía = desvío respecto a esa climatología. No aplica la transformación ^0.25 del Excel
  WGINOR (eso es meta-variable ATAC, no la definición de anomalía climatológica).

Documentación extendida (diferencias entre métodos y trazabilidad):
``docs/experimentacion_anomalias/README.md``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _wginor_monthly_climatology_anomaly(
    df: pd.DataFrame,
    col_fecha: str,
    col_valor: str,
) -> pd.Series:
    """
    Traducción fiel a Pandas de la lógica de anomalía mensual tipo pipeline WGINOR/IEO:

    1. Agrupar por mes natural (inicio de mes MS), promediando si hay varios muestreos.
    2. Opcionalmente rellenar malla MS entre min y max (como en 02_analysis).
    3. Climatología del mes m = media de todos los valores mensuales cuyo mes calendario es m
       (solo con meses donde hay dato).
    4. Anomalía del mes = valor mensual − climatología(m).

    Luego se asigna a cada fila original la anomalía del bin mensual al que pertenece.
    """
    work = df[[col_fecha, col_valor]].copy()
    work[col_fecha] = pd.to_datetime(work[col_fecha], errors="coerce")
    work[col_valor] = pd.to_numeric(work[col_valor], errors="coerce")
    idx_orig = work.index
    work = work.dropna(subset=[col_fecha, col_valor])
    if work.empty:
        return pd.Series(np.nan, index=idx_orig, dtype=float)

    work["fecha_ms"] = work[col_fecha].dt.to_period("M").dt.to_timestamp(how="start")
    monthly = work.groupby("fecha_ms", as_index=False)[col_valor].mean()

    full_index = pd.date_range(
        monthly["fecha_ms"].min(),
        monthly["fecha_ms"].max(),
        freq="MS",
    )
    monthly = (
        monthly.set_index("fecha_ms")
        .reindex(full_index)
        .rename_axis("fecha_ms")
        .reset_index()
    )
    monthly["mes"] = monthly["fecha_ms"].dt.month

    clim = monthly.dropna(subset=[col_valor]).groupby("mes")[col_valor].mean()
    monthly["climatologia_mes"] = monthly["mes"].map(clim)
    monthly["anom_m"] = monthly[col_valor] - monthly["climatologia_mes"]

    # Mapa (fecha_ms) -> anomalía del bin mensual
    anom_by_bin = monthly.set_index("fecha_ms")["anom_m"]

    base = pd.to_datetime(df[col_fecha], errors="coerce")
    fecha_ms_orig = base.dt.to_period("M").dt.to_timestamp(how="start")
    mapped = fecha_ms_orig.map(anom_by_bin)
    return pd.Series(mapped.values, index=idx_orig, dtype=float)


def _marcos_residuals_numpy(y: np.ndarray, mes: np.ndarray, t: np.ndarray) -> np.ndarray:
    """
    OLS equivalente a ``y ~ C(mes) + t`` con mes 1..12 y categoría de referencia mes=1:
    y = β0 + Σ β_m·1(mes=m) + β_t·t + ε  (m = 2..12).
    """
    mes = np.asarray(mes, dtype=int)
    y = np.asarray(y, dtype=float)
    t = np.asarray(t, dtype=float)
    n = len(y)
    D = np.zeros((n, 11))
    for k, mref in enumerate(range(2, 13)):
        D[:, k] = (mes == mref).astype(float)
    X = np.column_stack([np.ones(n), D, t])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    fitted = X @ beta
    return y - fitted


def _fourier_season_basic(month: np.ndarray, a: float, b: float, c: float) -> np.ndarray:
    """Un ciclo anual: a + b*sin(φ) + c*cos(φ) con φ = 2π(m-1)/12, m ∈ 1..12."""
    m = np.asarray(month, dtype=float)
    phase = 2.0 * np.pi * (m - 1.0) / 12.0
    return a + b * np.sin(phase) + c * np.cos(phase)


def comparar_metodos(
    df: pd.DataFrame,
    col_fecha: str = "fecha",
    col_temperatura: str = "temperatura",
) -> pd.DataFrame:
    """
    Añade tres columnas de anomalía por fila, para comparar métodos de desestacionalización.

    Parameters
    ----------
    df
        Debe contener al menos columnas de fecha y temperatura (nombres configurables).
    col_fecha
        Columna parseable a datetime.
    col_temperatura
        Columna numérica de temperatura.

    Returns
    -------
    Copia de ``df`` con columnas:

    - **anomalia_marcos**: residuos de OLS ``y ~ C(mes) + t`` con ``t = np.arange(n)``.
    - **anomalia_fourier**: ``y - f_hat(mes)``, con ``f_hat`` ajustada por ``curve_fit``.
    - **anomalia_wginor**: anomalía respecto a climatología mensual (pipeline tipo WGINOR/IEO).

    Filas con temperatura no válida reciben NaN en las tres columnas (donde aplique).
    """
    from scipy.optimize import curve_fit

    if col_fecha not in df.columns or col_temperatura not in df.columns:
        raise ValueError(
            f"El DataFrame debe incluir '{col_fecha}' y '{col_temperatura}'."
        )

    out = df.copy()
    fechas = pd.to_datetime(out[col_fecha], errors="coerce")
    y = pd.to_numeric(out[col_temperatura], errors="coerce")
    n = len(out)
    t_idx = np.arange(n, dtype=float)
    mes = fechas.dt.month

    # --- Marcos: OLS y ~ C(mes) + t ---
    d = pd.DataFrame({"y": y, "mes": mes, "t": t_idx}, index=out.index)
    valid = d["y"].notna() & d["mes"].notna()
    anom_marcos = pd.Series(np.nan, index=out.index, dtype=float)
    if valid.sum() >= 3:
        dv = d.loc[valid].copy()
        dv["mes_i"] = dv["mes"].astype(int)
        try:
            import statsmodels.formula.api as smf

            # Referencia explícita enero (1), alineada con _marcos_residuals_numpy
            fit = smf.ols(
                "y ~ C(mes_i, Treatment(reference=1)) + t",
                data=dv,
            ).fit()
            anom_marcos.loc[dv.index] = fit.resid.values
        except Exception:
            r = _marcos_residuals_numpy(
                dv["y"].to_numpy(),
                dv["mes_i"].to_numpy(),
                dv["t"].to_numpy(),
            )
            anom_marcos.loc[dv.index] = r
    out["anomalia_marcos"] = anom_marcos

    # --- Fourier: curve_fit un armónico anual en función del mes calendario ---
    anom_fourier = pd.Series(np.nan, index=out.index, dtype=float)
    mask = y.notna() & mes.notna()
    if mask.sum() >= 3:
        m_obs = mes.loc[mask].astype(float).values
        y_obs = y.loc[mask].astype(float).values
        p0 = [float(np.nanmean(y_obs)), 0.0, 0.0]
        try:
            popt, _ = curve_fit(
                _fourier_season_basic,
                m_obs,
                y_obs,
                p0=p0,
                maxfev=10000,
            )
            pred_all = _fourier_season_basic(mes.astype(float).values, *popt)
            anom_fourier = y - pred_all
        except Exception:
            anom_fourier = pd.Series(np.nan, index=out.index, dtype=float)
    out["anomalia_fourier"] = anom_fourier

    # --- WGINOR / IEO: climatología mensual sobre serie mensual agregada ---
    out["anomalia_wginor"] = _wginor_monthly_climatology_anomaly(
        df, col_fecha=col_fecha, col_valor=col_temperatura
    )

    return out


def main() -> None:
    """Ejemplo mínimo con serie sintética."""
    rng = pd.date_range("2010-01-01", periods=48, freq="MS")
    temp = 14.0 + 3.0 * np.sin(2 * np.pi * (rng.month - 1) / 12.0) + np.linspace(0, 0.5, 48)
    temp = temp + np.random.default_rng(42).normal(0, 0.15, 48)
    demo = pd.DataFrame({"fecha": rng, "temperatura": temp})
    result = comparar_metodos(demo)
    print(result[["fecha", "temperatura", "anomalia_marcos", "anomalia_fourier", "anomalia_wginor"]].head(12))
    print("\nCorrelaciones entre métodos (pairwise, sin NaN):")
    cols = ["anomalia_marcos", "anomalia_fourier", "anomalia_wginor"]
    print(result[cols].corr())


if __name__ == "__main__":
    main()

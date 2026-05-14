from __future__ import annotations

"""
src/atac_monthly_report.py
==========================

Figura única (mensual) inspirada en ATAC, usando:
- Ajuste base tipo Marcos: tendencia lineal + estacionalidad mensual fija.
- ATAC sobre residuos: se entrena sin los últimos 12 meses observados (holdout) y
  se compara el holdout contra bandas 50/75/95%.

Este módulo devuelve:
- Una figura Plotly con leyenda ordenada y agrupada.
- Un texto corto (Markdown) “al pie” explicando qué se ve y cómo interpretarlo.
"""

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go


@dataclass(frozen=True, slots=True)
class AtacMonthlyResult:
    fig: go.Figure
    footer_md: str
    pipe: pd.DataFrame
    meta: dict[str, Any]


def _load_analysis_module(project_root: Path) -> Any:
    key = "ieo_02_analysis"
    if key in sys.modules:
        return sys.modules[key]
    path = project_root / "src" / "02_analysis.py"
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"No se pudo cargar el módulo de análisis: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[key] = mod
    return mod


def build_atac_monthly_figure(
    *,
    project_root: Path,
    monthly_5m: pd.DataFrame,
    station_label: str,
    holdout_months: int = 12,
    var_label: str = "Temperatura",
    var_units: str = "°C",
) -> AtacMonthlyResult:
    """
    monthly_5m: DataFrame con columnas:
      - fecha (datetime mensual)
      - temp_5m (valor mensual de la variable, nombre fijo internamente)
      - opcional: obs_lower, obs_upper (IC95 de muestreo si n>=2)

    var_label : nombre legible de la variable (p. ej. "Fluorescencia", "PAR", "Oxígeno").
    var_units : unidades para ejes y tooltips (p. ej. "μg/L", "μE/m²/s", "mL/L").
    """
    analysis = _load_analysis_module(project_root)
    holdout_months = int(max(1, holdout_months))

    base = monthly_5m.copy()
    if isinstance(base.columns, pd.MultiIndex):
        base.columns = [
            str(tup[-1]) if isinstance(tup, tuple) and len(tup) > 0 and str(tup[-1]) else str(tup[0])
            for tup in base.columns
        ]
    if "fecha" not in base.columns:
        for c in list(base.columns):
            if pd.api.types.is_datetime64_any_dtype(base[c]) or str(c).lower().strip() == "fecha":
                base = base.rename(columns={c: "fecha"})
                break
    if "fecha" not in base.columns:
        raise KeyError(
            "fecha: el DataFrame mensual debe incluir una columna de fecha (p. ej. 'fecha'). "
            f"Columnas recibidas: {list(base.columns)}"
        )
    if "temp_5m" not in base.columns:
        for c in list(base.columns):
            if c == "fecha":
                continue
            if pd.api.types.is_numeric_dtype(base[c]):
                base = base.rename(columns={c: "temp_5m"})
                break
    if "temp_5m" not in base.columns:
        raise KeyError(
            "temp_5m: el DataFrame mensual debe incluir la temperatura a 5 m. "
            f"Columnas recibidas: {list(base.columns)}"
        )

    base["fecha"] = pd.to_datetime(base["fecha"], errors="coerce")
    base["temp_5m"] = pd.to_numeric(base["temp_5m"], errors="coerce")
    base = base.dropna(subset=["fecha"]).sort_values("fecha")
    if base.empty:
        raise ValueError("Serie mensual vacía.")

    dec, meta_m = analysis.decompose_marcos_holdout_last_n(
        base,
        col_fecha="fecha",
        col_y="temp_5m",
        holdout_months=holdout_months,
    )
    if dec.empty:
        raise ValueError(f"No se pudo ajustar el modelo base (Marcos): {meta_m}")

    holdout_eff = int(float(meta_m.get("holdout_months", holdout_months)))
    cutoff = pd.to_datetime(meta_m["cutoff_holdout_start"])

    rs = dec.loc[dec["fecha"] < cutoff].set_index("fecha")["residual"]
    atac_df, meta_a = analysis.atac_holdout_bands_on_residuals(
        rs,
        cutoff_holdout_start=cutoff,
        holdout_months=holdout_eff,
    )
    if atac_df.empty:
        raise ValueError(f"No se pudo ajustar ATAC (residuos): {meta_a}")

    pipe = dec.merge(atac_df, on="fecha", how="left")
    pipe["temp_train_lo_95"] = pipe["fitted"] + pipe["resid_lo_95"].fillna(0.0)
    pipe["temp_train_hi_95"] = pipe["fitted"] + pipe["resid_hi_95"].fillna(0.0)

    pipe["temp_fc_mean"] = pipe["fitted"] + pipe["resid_fc_mean"].fillna(0.0)
    for lab in ("95", "75", "50"):
        lo = f"resid_fc_lo_{lab}"
        hi = f"resid_fc_hi_{lab}"
        if lo in pipe.columns:
            pipe[f"temp_fc_lo_{lab}"] = pipe["fitted"] + pipe[lo].fillna(0.0)
        if hi in pipe.columns:
            pipe[f"temp_fc_hi_{lab}"] = pipe["fitted"] + pipe[hi].fillna(0.0)

    # ---------------- Figura ----------------
    work = pipe.copy().sort_values("fecha")
    ok = work["observation"].notna()
    is_holdout = work["is_holdout"] == True  # noqa: E712
    is_train = work["is_holdout"] == False  # noqa: E712

    y0 = int(work.loc[ok, "fecha"].min().year)
    y1 = int(work.loc[ok, "fecha"].max().year)

    fig = go.Figure()

    # (1) Rango esperable del modelo (95%) — primero en la leyenda
    if "temp_train_lo_95" in work.columns and "temp_train_hi_95" in work.columns:
        width = (work["temp_train_hi_95"] - work["temp_train_lo_95"]).abs()
        good = is_train & width.notna()
        if good.any():
            thr = float(np.nanquantile(width[good].to_numpy(dtype=float), 0.95)) * 3.0
            good = good & (width <= thr)
        fig.add_trace(
            go.Scatter(
                x=work.loc[good, "fecha"],
                y=work.loc[good, "temp_train_hi_95"],
                mode="lines",
                line=dict(width=0),
                showlegend=False,
                hoverinfo="skip",
                legendgroup="modelo",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=work.loc[good, "fecha"],
                y=work.loc[good, "temp_train_lo_95"],
                mode="lines",
                line=dict(width=0),
                fill="tonexty",
                fillcolor="rgba(16,78,139,0.18)",
                name="Modelo: rango esperable 95%",
                hoverinfo="skip",
                legendgroup="modelo",
                legendgrouptitle_text="Modelo (histórico)",
            )
        )

    # (2) Ajuste base (Marcos) — línea azul discontinua
    fig.add_trace(
        go.Scatter(
            x=work.loc[ok, "fecha"],
            y=work.loc[ok, "fitted"],
            mode="lines",
            name="Modelo base (tendencia + ciclo mensual)",
            line=dict(color="#104E8B", width=1.8, dash="dash"),
            legendgroup="modelo",
            hovertemplate=f"%{{x|%b %Y}}<br>Modelo base: %{{y:.3g}} {var_units}<extra></extra>",
        )
    )

    # (3) Observación — 1 punto por mes
    fig.add_trace(
        go.Scatter(
            x=work.loc[ok, "fecha"],
            y=work.loc[ok, "observation"],
            mode="markers",
            name="Observación",
            marker=dict(color="#104E8B", size=7, symbol="circle-open"),
            legendgroup="obs",
            legendgrouptitle_text="Observación",
            hovertemplate=f"%{{x|%b %Y}}<br>{var_label}: %{{y:.3g}} {var_units}<extra></extra>",
        )
    )

    # (4) Pronóstico (holdout) — tonos rojizos, apilando porcentajes en leyenda
    red = "#8B1A1A"
    if is_holdout.any():
        wf = work.loc[is_holdout].copy()
        # Orden de leyenda: 95, 75, 50, media
        for lab, alpha_fill in [("95", 0.30), ("75", 0.22), ("50", 0.14)]:
            lo = f"temp_fc_lo_{lab}"
            hi = f"temp_fc_hi_{lab}"
            if lo not in wf.columns or hi not in wf.columns:
                continue
            fig.add_trace(
                go.Scatter(
                    x=wf["fecha"],
                    y=wf[hi],
                    mode="lines",
                    line=dict(width=0),
                    showlegend=False,
                    hoverinfo="skip",
                    legendgroup="forecast",
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=wf["fecha"],
                    y=wf[lo],
                    mode="lines",
                    line=dict(width=0),
                    fill="tonexty",
                    fillcolor=f"rgba(139,26,26,{alpha_fill})",
                    name=f"banda {lab}%",
                    hoverinfo="skip",
                    legendgroup="forecast",
                    legendgrouptitle_text="Pronóstico",
                )
            )
        fig.add_trace(
            go.Scatter(
                x=wf["fecha"],
                y=wf["temp_fc_mean"],
                mode="lines",
                name="media",
                line=dict(color=red, width=2.0),
                legendgroup="forecast",
                hovertemplate=f"%{{x|%b %Y}}<br>Pronóstico: %{{y:.3g}} {var_units}<extra></extra>",
            )
        )

    ar_order = meta_a.get("ar_order", None)
    ar_txt = "n/d" if ar_order is None or (isinstance(ar_order, float) and np.isnan(ar_order)) else str(int(ar_order))
    cutoff_txt = cutoff.strftime("%Y-%m")

    # Título / subtítulo
    fig.update_layout(
        template="simple_white",
        height=560,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=0.99),
        margin=dict(t=95),
        title=dict(
            text=(
                f"<span style='font-size:22px'><b>{var_label} mensual ({y0}–{y1})</b></span>"
                f"<br><span style='font-size:18px'><b>Estación {station_label}</b></span>"
                f"<br><span style='font-size:12px'>Evaluación de los últimos {holdout_eff} meses (desde {cutoff_txt}) · AR(residuos)={ar_txt}</span>"
            ),
            x=0.02,
        ),
        xaxis=dict(title="Fecha"),
        yaxis=dict(title=f"{var_label} ({var_units})"),
    )

    footer_md = "\n".join(
        [
            "**Qué estás viendo**",
            "- Los puntos muestran la **temperatura mensual a 5 m** en la estación.",
            "- En cada mes hay **dato o no hay dato**. Si falta, no se dibuja punto.",
            "- Cuando hay dato, **T(5 m)** se obtiene dentro del perfil vertical (CTD) estimando el valor a 5 m a partir del propio perfil. Si el perfil no cubre 5 m, ese mes queda vacío.",
            "",
            "**Modelo base (línea azul discontinua)**",
            "A partir del histórico se ajusta un modelo simple que captura dos señales:",
            "- Un **ciclo estacional** (invierno más frío, verano más cálido).",
            "- Una **tendencia lenta** a largo plazo (p. ej., calentamiento/enfriamiento).",
            "",
            "Ecuación (mensual):",
            "$$T_t = \\alpha + \\beta\\,t + \\gamma_{mes(t)} + \\varepsilon_t$$",
            "- $T_t$: temperatura observada del mes $t$.",
            "- $t$: índice temporal (meses).",
            "- $\\alpha$: nivel medio.",
            "- $\\beta$: pendiente (aprox. °C/año).",
            "- $\\gamma_{mes(t)}$: “efecto del mes” (patrón típico de enero, febrero, etc.).",
            "- $\\varepsilon_t$: residuo = lo que queda sin explicar (el “extra” de ese mes).",
            "",
            "**Banda azul (95%)**",
            "- Es un **rango de variación normal** alrededor del modelo según el histórico.",
            "- Se interpreta como: “aunque el patrón se mantenga, los meses reales suelen oscilar un poco por encima o por debajo”.",
            "",
            f"**Evaluación del último tramo (rojo) – holdout de {holdout_eff} meses (desde {cutoff_txt})**",
            "- Esos meses se dejan aparte para evaluar el final de la serie: **no se usan para ajustar** el modelo base.",
            "- ATAC trabaja con los **residuos** $\\varepsilon_t$ (el “extra” respecto a la línea azul) y aprende si hay **inercia/racha** de un mes a otro.",
            f"- La etiqueta **AR(residuos)={ar_txt}** resume cuánta memoria detecta el modelo en los residuos (p=0,1,2; se elige por AIC: equilibrio entre ajustar bien y no complicarse).",
            "",
            "**Bandas rojas 50/75/95%**",
            "- Son niveles de tolerancia del pronóstico: 50% (estrecho), 75% (intermedio), 95% (conservador).",
            "",
            "**Interpretación (tramo rojo vs bandas)**",
            "- Si varios puntos del tramo rojo caen fuera de la banda roja 95%, ese periodo es **poco esperable** comparado con el comportamiento histórico (posible cambio reciente).",
        ]
    )

    meta = {**meta_m, **meta_a}
    return AtacMonthlyResult(fig=fig, footer_md=footer_md, pipe=pipe, meta=meta)


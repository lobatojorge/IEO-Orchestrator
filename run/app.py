"""
run/app.py — Visor IEO · Radiales oceánicos
==========================================

Aviso práctico
--------------
Este dashboard todavía usa Pandas/Plotly para la UI.
El pipeline de producción (ingesta + anomalías + reportes) ya no depende de Pandas.

Si quieres que la UI sea 100% libre de Pandas, hay que migrar también `src/03_visualization.py`
y sus helpers (fuera del alcance del plan de refactor del pipeline).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = RUN_DIR.parent
_SRC_DIR = (PROJECT_ROOT / "src").resolve()
_IEO_LOCAL = _SRC_DIR / "ieo"


def _bootstrap_ieo_repo_path() -> None:
    """`run/` para `cudillero_csv`; `src/` primero; quita `ieo` pip homónimo de `sys.modules`."""
    run_s = str(RUN_DIR)
    if run_s not in sys.path:
        sys.path.insert(0, run_s)
    s = str(_SRC_DIR)
    while s in sys.path:
        sys.path.remove(s)
    sys.path.insert(0, s)
    local_init = (_IEO_LOCAL / "__init__.py").resolve()

    def _loaded_ieo_is_local(mod: object) -> bool:
        f = getattr(mod, "__file__", None)
        if f:
            return Path(f).resolve() == local_init
        p = getattr(mod, "__path__", None)
        if p is None:
            return False
        roots = list(p) if not isinstance(p, (str, bytes)) else [p]
        want = _IEO_LOCAL.resolve()
        return any(Path(x).resolve() == want for x in roots)

    if sys.modules.get("ieo") is not None and not _loaded_ieo_is_local(sys.modules["ieo"]):
        for k in list(sys.modules):
            if k == "ieo" or k.startswith("ieo."):
                del sys.modules[k]


_bootstrap_ieo_repo_path()

from pipeline_runs import (
    LATEST_SENTINEL,
    PipelineViewerLoadResult,
    clean_parquet_cache_token,
    list_valid_run_roots,
    load_pipeline_viewer_data,
    read_provenance_dict,
    resolve_run_root_for_ui,
)

import streamlit as st

st.set_page_config(
    page_title="Radiales Cudillero · IEO",
    layout="wide",
    page_icon=str(RUN_DIR / "assets" / "logo.webp"),
)

import pandas as pd
import plotly.graph_objects as go
import markdown
import numpy as np

METODOLOGIA_RADIALES_CUDILLERO_MD = PROJECT_ROOT / "docs" / "metodologia_radiales_cudillero.md"

_radial_contract_mod: object | None = None


def _load_radial_contract_mod() -> object:
    """Import perezoso: el contrato radial solo se carga al abrir la vista (arranque más rápido)."""
    global _radial_contract_mod
    if _radial_contract_mod is not None:
        return _radial_contract_mod
    _rc_path = _SRC_DIR / "ieo" / "validation" / "radial_contract.py"
    _rc_spec = importlib.util.spec_from_file_location("_ieo_streamlit_radial_contract", _rc_path)
    if _rc_spec is None or _rc_spec.loader is None:  # pragma: no cover
        raise ImportError(f"No se pudo cargar el contrato radial desde {_rc_path}")
    _rc_mod = importlib.util.module_from_spec(_rc_spec)
    sys.modules[str(_rc_spec.name)] = _rc_mod
    _rc_spec.loader.exec_module(_rc_mod)
    _radial_contract_mod = _rc_mod
    return _rc_mod


# Metadatos de las variables del CSV: etiqueta legible y unidades para el gráfico.
# Las variables que no estén aquí usarán el nombre de columna como etiqueta y "" como unidad.
_VAR_META: dict[str, tuple[str, str]] = {
    "temperatura":        ("Temperatura",    "°C"),
    "temperatura_c":      ("Temperatura",    "°C"),
    "salinidad":          ("Salinidad",      "PSU"),
    "salinidad_psu":      ("Salinidad",      "PSU"),
    "fluorescencia":      ("Fluorescencia",  "μg/L"),
    "fluorescencia_cdom": ("Fluorescencia CDOM", "ppb"),
    "fluorescencia_afl":  ("Fluorescencia AFL",  "μg/L"),
    "par":                ("PAR",            "μE/m²/s"),
    "oxigeno":            ("Oxígeno",        "mL/L"),
    "o_100":              ("Oxígeno (100 m)","mL/L"),
    "turbidez_ntu":       ("Turbidez",       "NTU"),
    "densidad00":         ("Densidad",       "kg/m³"),
}


def _var_meta(col_name: str) -> tuple[str, str]:
    """Devuelve (var_label, var_units) para una columna, con fallback al nombre."""
    key = str(col_name).lower().strip()
    return _VAR_META.get(key, (str(col_name).replace("_", " ").title(), ""))


def _atac_allowed_for_column(col_name: str) -> bool:
    """Ahora el modelo Marcos+ATAC se aplica a cualquier variable numérica."""
    return True


# build_atac_monthly_figure y radial_contract: carga perezosa (ver render_radiales_cudillero / _render_series_and_atac).

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def load_methodology(product_id: str) -> str:
    """Lee el Markdown de metodología desde docs/ o devuelve placeholder."""
    path = PROJECT_ROOT / "docs" / f"metodologia_{product_id}.md"
    if path.exists():
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            pass
    return (
        "<p>Texto de metodología pendiente de redacción (muestreo, estaciones, instrumental). "
        f"Puede editarse en <code>docs/metodologia_{product_id}.md</code>.</p>"
    )


@st.cache_data(show_spinner=False)
def _cached_cudillero_methodology_text() -> str:
    if METODOLOGIA_RADIALES_CUDILLERO_MD.exists():
        try:
            return METODOLOGIA_RADIALES_CUDILLERO_MD.read_text(encoding="utf-8")
        except OSError:
            pass
    return load_methodology("radiales_cudillero")


def parse_radial_station_coords_from_methodology(md_text: str) -> list[dict[str, float | int | str]]:
    """
    Extrae lat/lon WGS84 del markdown de metodología (comentario HTML E1|E2|E3 o DMS en viñetas).
    """
    if "<p>Texto de metodología pendiente" in md_text:
        return []

    block4 = re.search(
        r"<!--\s*E1\s+([\d.]+)\s+([-\d.]+)\s*\|\s*E2\s+([\d.]+)\s+([-\d.]+)\s*\|\s*E3\s+([\d.]+)\s+([-\d.]+)\s*\|\s*E4\s+([\d.]+)\s+([-\d.]+)\s*-->",
        md_text,
    )
    if block4:
        return [
            {"estacion": 1, "lat": float(block4.group(1)), "lon": float(block4.group(2)), "nombre": "E1 · Costa"},
            {"estacion": 2, "lat": float(block4.group(3)), "lon": float(block4.group(4)), "nombre": "E2 · Plataforma"},
            {"estacion": 3, "lat": float(block4.group(5)), "lon": float(block4.group(6)), "nombre": "E3 · Talud"},
            {"estacion": 4, "lat": float(block4.group(7)), "lon": float(block4.group(8)), "nombre": "E4 · Talud profundo"},
        ]

    block3 = re.search(
        r"<!--\s*E1\s+([\d.]+)\s+([-\d.]+)\s*\|\s*E2\s+([\d.]+)\s+([-\d.]+)\s*\|\s*E3\s+([\d.]+)\s+([-\d.]+)\s*-->",
        md_text,
    )
    if block3:
        return [
            {"estacion": 1, "lat": float(block3.group(1)), "lon": float(block3.group(2)), "nombre": "E1 · E1CU"},
            {"estacion": 2, "lat": float(block3.group(3)), "lon": float(block3.group(4)), "nombre": "E2 · E2CU"},
            {"estacion": 3, "lat": float(block3.group(5)), "lon": float(block3.group(6)), "nombre": "E3 · E3CU"},
        ]

    dms_pat = re.compile(
        r"\*\*Estación\s+(\d+)\s*·\s*([^*]+)\*\*\s*\(\s*(\d+)°\s*([\d.]+)\s*['′´]\s*N\s*,\s*(\d+)°\s*([\d.]+)\s*['′´]\s*W",
        re.IGNORECASE,
    )
    found: list[dict[str, float | int | str]] = []
    for m in dms_pat.finditer(md_text):
        n = int(m.group(1))
        label = f"E{n} · {m.group(2).strip()}"
        lat_deg, lat_min = int(m.group(3)), float(m.group(4))
        lon_deg, lon_min = int(m.group(5)), float(m.group(6))
        lat = lat_deg + lat_min / 60.0
        lon = -(lon_deg + lon_min / 60.0)
        found.append({"estacion": n, "lat": lat, "lon": lon, "nombre": label})
    found.sort(key=lambda x: int(x["estacion"]))  # type: ignore[arg-type, return-value]
    return found


def build_cudillero_radial_map_figure(stations: list[dict[str, float | int | str]]) -> go.Figure:
    """
    Mapa interactivo con marcadores diferenciados por batimetría.
    Colores graduados del azul claro (costa) al azul oscuro (talud profundo).
    """
    center_lat, center_lon = 43.689, -6.150
    zoom = 8.6
    if stations:
        lats = [float(s["lat"]) for s in stations]
        lons = [float(s["lon"]) for s in stations]
        center_lat = sum(lats) / len(lats)
        center_lon = sum(lons) / len(lons)
        lat_span = max(lats) - min(lats)
        lon_span = max(lons) - min(lons)
        span = max(lat_span * 1.35, lon_span * 1.6, 0.06)
        zoom = float(np.clip(11.2 - span * 18.0, 7.8, 10.5))

    fig = go.Figure()

    # Línea del transecto
    if len(stations) >= 2:
        ordered = sorted(stations, key=lambda s: float(s["lat"]))
        line_lat = [float(s["lat"]) for s in ordered]
        line_lon = [float(s["lon"]) for s in ordered]
        fig.add_trace(
            go.Scattermap(
                lat=line_lat,
                lon=line_lon,
                mode="lines",
                line=dict(color="rgba(30,58,95,0.4)", width=2),
                hoverinfo="skip",
                showlegend=False,
            )
        )

    if stations:
        # Profundidades nominales por estación (para tooltip y color)
        _DEPTH_LABELS = {1: "~65 m · E1CU", 2: "~130 m · E2CU", 3: "~850 m · E3CU", 4: "Estación 4"}
        # Gradiente azul claro → azul marino por profundidad
        _DEPTH_COLORS = ["#5ea8d4", "#2b6cb0", "#1a3f6f", "#0c1f3f"]

        lats = [float(s["lat"]) for s in stations]
        lons = [float(s["lon"]) for s in stations]
        ids = [int(s["estacion"]) for s in stations]
        colors = [_DEPTH_COLORS[min(i - 1, 3)] for i in ids]
        sizes = [22, 24, 26, 28]  # crece con la profundidad
        sizes_mapped = [sizes[min(i - 1, 3)] for i in ids]

        hover_texts = [
            f"<b>E{i} — {_DEPTH_LABELS.get(i, str(s['nombre']))}</b>"
            for i, s in zip(ids, stations)
        ]

        fig.add_trace(
            go.Scattermap(
                lat=lats,
                lon=lons,
                mode="markers+text",
                text=[str(i) for i in ids],
                textposition="middle center",
                textfont=dict(size=11, color="#ffffff", family="Arial, sans-serif"),
                marker=dict(
                    size=sizes_mapped,
                    color=colors,
                    symbol="circle",
                    opacity=0.95,
                ),
                customdata=np.array(ids).reshape(-1, 1),
                hovertext=hover_texts,
                hovertemplate="%{hovertext}<extra></extra>",
                showlegend=False,
            )
        )

    fig.update_layout(
        map=dict(
            style="carto-positron",
            center=dict(lat=center_lat, lon=center_lon),
            zoom=zoom,
            bearing=0,
            pitch=0,
        ),
        margin=dict(l=0, r=0, t=0, b=0),
        height=460,
        paper_bgcolor="#f8fafc",
        plot_bgcolor="#f8fafc",
        showlegend=False,
        uirevision="cudillero_map",
    )
    return fig


def _render_data_governance_card() -> None:
    """
    Sello breve de gobernanza: cadena de procesado en lenguaje llano.
    Usa componentes nativos Streamlit para evitar problemas de escaping HTML.
    """
    st.markdown(
        "<span style='padding:3px 11px;border-radius:999px;background:#1e3a5f;color:#fff;"
        "font-size:0.75rem;font-weight:700;letter-spacing:0.05em;'>IEO · RADIALES</span>"
        "&nbsp;&nbsp;<span style='font-size:0.82rem;color:#475569;'>"
        "Datos procesados bajo protocolo automatizado de control de calidad</span>",
        unsafe_allow_html=True,
    )
    pasos = [
        ("📄", "Parquet pipeline", "ctd_clean + anomalías", False),
        ("🔄", "Normalización", "formato canónico", False),
        ("🔍", "Control QC", "cuarentena si anomalía", False),
        ("📈", "Análisis", "Marcos + ATAC", False),
        ("🖥️", "Visor", "resultado final", True),
    ]
    cols = st.columns(len(pasos) * 2 - 1)
    for i, (icono, titulo, subtitulo, destacado) in enumerate(pasos):
        col_idx = i * 2
        bg = "#e8f0fb" if destacado else "#ffffff"
        border = "#1e3a5f" if destacado else "#dde3ea"
        color_titulo = "color:#1e3a5f;" if destacado else ""
        with cols[col_idx]:
            st.markdown(
                f"<div style='text-align:center;padding:10px 8px;border-radius:8px;"
                f"background:{bg};border:1px solid {border};'>"
                f"<div style='font-size:1.1rem;'>{icono}</div>"
                f"<div style='font-size:0.72rem;font-weight:600;margin-top:2px;{color_titulo}'>{titulo}</div>"
                f"<div style='font-size:0.68rem;color:#64748b;'>{subtitulo}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        if i < len(pasos) - 1:
            with cols[col_idx + 1]:
                st.markdown(
                    "<div style='text-align:center;padding-top:18px;color:#94a3b8;font-size:1rem;'>→</div>",
                    unsafe_allow_html=True,
                )
    st.caption(
        "Este visor muestra resultados ya validados. "
        "No sustituye el informe científico firmado ni la auditoría del instrumental de campaña."
    )


# ---------------------------------------------------------------------------
# Carga y validación de datos — cacheada por sesión
# ---------------------------------------------------------------------------


@st.cache_data(show_spinner="Cargando Parquet de la corrida seleccionada…")
def load_cudillero_pipeline_viewer(run_root_str: str, cache_token: str) -> PipelineViewerLoadResult | None:
    """``cache_token`` solo participa en la clave de caché de Streamlit (mtime/tamaño del Parquet limpio)."""
    _ = cache_token
    return load_pipeline_viewer_data(Path(run_root_str))


def _render_isolation_forest_tab(pipe: PipelineViewerLoadResult) -> None:
    st.subheader("Revisión Isolation Forest")
    st.caption(
        f"run_id `{pipe.run_root.name}` · Parquet anomalías: `{pipe.run_root / 'data' / 'perfiles_all.ctd_anomalies.parquet'}`"
    )
    c1, c2, c3 = st.columns(3)
    c1.metric("Filas limpias", f"{len(pipe.df_clean):,}")
    c2.metric("Filas anómalas", f"{len(pipe.df_anomalies):,}")
    c3.metric("Total (viz)", f"{len(pipe.df_concat_viz):,}")

    show_cols = [
        c
        for c in (
            "fecha",
            "estacion",
            "cast",
            pipe.col_prof,
            pipe.col_temp,
            pipe.col_sal,
            "anomaly_score",
            "run_id",
            "source_file",
            "row_id",
        )
        if c in pipe.df_anomalies.columns
    ]
    if pipe.df_anomalies.empty:
        st.info("Isolation Forest no marcó ninguna fila como anómala en esta corrida.")
    else:
        st.dataframe(
            pipe.df_anomalies[show_cols] if show_cols else pipe.df_anomalies,
            use_container_width=True,
            height=min(400, 120 + 28 * len(pipe.df_anomalies)),
        )

    viz = pipe.df_concat_viz
    if viz.empty or "estacion" not in viz.columns:
        st.warning("Sin datos de visualización.")
        return
    ids = sorted({int(float(x)) for x in viz["estacion"].dropna().unique().tolist()})
    if not ids:
        st.warning("Sin datos de visualización.")
        return
    est_pick = st.selectbox("Estación (perfil T–z)", options=ids, key="if_tab_estacion_tz")
    sub = viz[viz["estacion"].astype(float) == float(est_pick)].copy()
    if sub.empty:
        st.warning("Sin filas para esa estación.")
        return
    fig = go.Figure()
    for flag, lab, col in [
        (False, "Limpio (IF)", "#2E86AB"),
        (True, "Anomalía IF", "#C1121F"),
    ]:
        s = sub[sub["_viewer_anomaly"] == flag]
        if s.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=s[pipe.col_temp],
                y=s[pipe.col_prof],
                mode="markers",
                name=lab,
                marker=dict(size=6, color=col, opacity=0.75),
                text=s["fecha"].astype(str),
                hovertemplate="T=%{x:.3f} °C<br>z=%{y:.1f} m<br>%{text}<extra></extra>",
            )
        )
    fig.update_layout(
        title=f"Temperatura vs profundidad · estación {est_pick}",
        xaxis_title="T (°C)",
        yaxis_title="Profundidad (m)",
        yaxis_autorange="reversed",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        height=520,
        margin=dict(l=48, r=24, t=56, b=48),
    )
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Vistas
# ---------------------------------------------------------------------------


def render_radiales_cudillero() -> None:
    """Vista Radiales Cudillero: mapa, metodología y Marcos+ATAC desde la corrida del pipeline elegida en la barra lateral."""

    if "estacion_seleccionada_csv" not in st.session_state:
        st.session_state["estacion_seleccionada_csv"] = 1

    st.header("Radiales Cudillero")
    _rc = _load_radial_contract_mod()
    _render_data_governance_card()

    valid_runs = list_valid_run_roots(PROJECT_ROOT)
    if not valid_runs:
        st.warning("No hay datos procesados en el sistema. Ejecuta el pipeline principal primero.")
        st.stop()

    choice = st.session_state.get("pipeline_run_select", LATEST_SENTINEL)
    run_root = resolve_run_root_for_ui(PROJECT_ROOT, choice)
    if run_root is None:
        st.warning("No hay datos procesados en el sistema. Ejecuta el pipeline principal primero.")
        st.stop()

    cache_tok = clean_parquet_cache_token(run_root)
    pipe = load_cudillero_pipeline_viewer(str(run_root.resolve()), cache_tok)
    if pipe is None:
        st.warning("No hay datos procesados en el sistema. Ejecuta el pipeline principal primero.")
        st.stop()

    with st.expander("Fuente de datos (corrida seleccionada)", expanded=False):
        st.markdown(
            f"- **run_id:** `{pipe.run_root.name}`\n"
            f"- **Parquet limpio:** `{pipe.clean_parquet}`\n"
            f"- **Filas limpias:** {len(pipe.df_clean):,} · **Filas anómalas (IF):** {len(pipe.df_anomalies):,}"
        )
        fc = pipe.df_clean["fecha"].dropna()
        if len(fc):
            st.caption(f"Rango fechas (limpias): **{fc.min().date()}** → **{fc.max().date()}**")
        else:
            st.caption("Sin fechas válidas en el conjunto limpio.")

    md_method = _cached_cudillero_methodology_text()
    stations_geo = parse_radial_station_coords_from_methodology(md_method)

    col_map, col_txt = st.columns([1, 1], gap="large")

    with col_map:
        if not stations_geo:
            st.info(
                "Aún no hay coordenadas en metadatos del markdown: el mapa muestra la zona de referencia. "
                "Añada al final de `docs/metodologia_radiales_cudillero.md` la línea `<!-- E1 lat lon | … | E4 … -->` "
                "(solo metadatos; el visor la lee y no se muestra al usuario en el texto)."
            )
        fig_map = build_cudillero_radial_map_figure(stations_geo)
        map_sel = st.plotly_chart(
            fig_map,
            use_container_width=True,
            on_select="rerun",
            selection_mode="points",
            key="cud_mapbox",
            config={"displayModeBar": False},
        )

    with col_txt:
        st.markdown("#### Metodología")
        md_display = re.sub(r"<!--.*?-->", "", md_method, flags=re.DOTALL).strip()
        html_method = markdown.markdown(md_display, extensions=["extra", "sane_lists"])
        st.markdown(
            f"""
            <div style="max-height: 520px; overflow-y: auto; padding-right: 8px; font-size: 0.92em; line-height: 1.5;">
            {html_method}
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("---")

    @st.cache_data(show_spinner="Agregando serie mensual por profundidad…")
    def _monthly_value_at_depth(
        df: pd.DataFrame,
        col_fecha: str,
        col_prof: str,
        col_value: str,
        col_estacion: str,
        target_depth_m: float,
    ) -> tuple[pd.DataFrame, dict[str, float | int]]:
        """Interpola ``col_value`` a ``target_depth_m`` por lance y agrega media mensual por estación."""
        use_cols = [col_fecha, col_prof, col_value, col_estacion] + (["acronimo"] if "acronimo" in df.columns else [])
        work = df[use_cols].copy()
        work[col_fecha] = pd.to_datetime(work[col_fecha], errors="coerce")
        work[col_prof] = pd.to_numeric(work[col_prof], errors="coerce")
        work[col_value] = pd.to_numeric(work[col_value], errors="coerce")
        work[col_estacion] = pd.to_numeric(work[col_estacion], errors="coerce")
        work = work.dropna(subset=[col_fecha, col_prof, col_value, col_estacion])
        if work.empty:
            return pd.DataFrame(), {
                "n_casts": 0,
                "n_casts_con_valor": 0,
                "n_casts_sin_cobertura_en_profundidad": 0,
                "profundidad_objetivo_m": int(target_depth_m) if target_depth_m == int(target_depth_m) else target_depth_m,
            }

        if "acronimo" in work.columns:
            group_keys = ["acronimo", col_estacion]
        else:
            work["_fecha_d"] = work[col_fecha].dt.date
            group_keys = ["_fecha_d", col_estacion]

        def _interp_value(profile: pd.DataFrame) -> float:
            dft = (
                pd.DataFrame({"z": profile[col_prof].to_numpy(dtype=float), "v": profile[col_value].to_numpy(dtype=float)})
                .dropna()
                .groupby("z", as_index=False)["v"]
                .mean()
                .sort_values("z")
            )
            if dft.empty:
                return float("nan")
            z = dft["z"].to_numpy(dtype=float)
            v = dft["v"].to_numpy(dtype=float)
            if target_depth_m < float(z[0]) or target_depth_m > float(z[-1]):
                return float("nan")
            return float(np.interp(target_depth_m, z, v))

        per_cast = (
            work.groupby(group_keys, as_index=False)
            .apply(
                lambda g: pd.Series(
                    {
                        "fecha": pd.to_datetime(g[col_fecha].iloc[0]).to_period("M").to_timestamp(how="start"),
                        "valor_prof": _interp_value(g),
                    }
                ),
                include_groups=False,
            )
            .reset_index(drop=True)
        )
        n_casts = int(len(per_cast))
        n_valid = int(per_cast["valor_prof"].notna().sum())
        diag: dict[str, float | int] = {
            "n_casts": n_casts,
            "n_casts_con_valor": n_valid,
            "n_casts_sin_cobertura_en_profundidad": n_casts - n_valid,
            "profundidad_objetivo_m": int(target_depth_m) if target_depth_m == int(target_depth_m) else target_depth_m,
        }

        per_cast = per_cast.dropna(subset=["valor_prof", "fecha"])
        if per_cast.empty:
            return pd.DataFrame(), diag

        agg = per_cast.groupby([col_estacion, "fecha"], as_index=False)["valor_prof"].mean()
        agg = agg.loc[:, [col_estacion, "fecha", "valor_prof"]]
        return agg.sort_values([col_estacion, "fecha"]).reset_index(drop=True), diag

    def _axis_label_for_value_column(col: str) -> str:
        c = str(col).lower()
        if "temp" in c or c in ("temperatura", "temperatura_c"):
            return "T (°C)"
        if "salin" in c or c in ("sal", "salinidad_psu"):
            return "S (PSU)"
        if "fluor" in c or "cdom" in c or "afl" in c:
            return str(col)
        if "clor" in c:
            return str(col)
        if "par" in c:
            return str(col)
        if "ox" in c or "o2" in c or "o_100" in c:
            return str(col)
        if "turb" in c:
            return str(col)
        if "dens" in c or "density" in c:
            return str(col)
        if "cstar" in c or "at0" in c or "tr0" in c:
            return str(col)
        return str(col)

    def _render_series_and_atac(
        *,
        df_gijon: pd.DataFrame | None,
        source_label: str,
        col_prof: str | None = None,
        col_temp: str | None = None,
        col_value: str | None = None,
        target_depth_m: float = 5.0,
        col_estacion: str = "estacion",
        col_fecha: str = "fecha",
        fixed_station: int | None = None,
        widget_key_suffix: str = "",
        df_if_overlay: pd.DataFrame | None = None,
    ) -> None:
        station_key = f"estacion_seleccionada_{source_label.lower().strip()}{widget_key_suffix}"
        if df_gijon is None or df_gijon.empty:
            st.warning(
                "No hay datos listos para graficar: revisa la **corrida seleccionada** en la barra lateral "
                "y el expander **«Fuente de datos (corrida seleccionada)»** arriba."
            )
            return

        # Resolver columnas en CSV legacy si no se pasan explícitas
        if col_prof is None:
            col_prof = next((str(c) for c in df_gijon.columns if "prof" in str(c).lower()), None)
        if col_temp is None:
            col_temp = next((str(c) for c in df_gijon.columns if "temp" in str(c).lower()), None)

        col_value_resolved = col_value if col_value is not None else col_temp

        # Comprobaciones mínimas
        if col_estacion not in df_gijon.columns or col_fecha not in df_gijon.columns:
            st.warning(f"[{source_label}] Faltan columnas requeridas: `{col_estacion}` y/o `{col_fecha}`.")
            return
        if col_prof is None or col_value_resolved is None or str(col_value_resolved) not in df_gijon.columns:
            st.warning(
                f"[{source_label}] No se encuentran columnas de profundidad/valor en: {list(df_gijon.columns)}"
            )
            return

        df_monthly, diag_interp = _monthly_value_at_depth(
            df_gijon,
            col_fecha=col_fecha,
            col_prof=col_prof,
            col_value=str(col_value_resolved),
            col_estacion=col_estacion,
            target_depth_m=float(target_depth_m),
        )
        if df_monthly.empty:
            st.warning(
                f"[{source_label}] No hay datos suficientes para la serie mensual a **{target_depth_m:g} m** "
                f"(`{col_value_resolved}`)."
            )
            return

        # --- Contrato de datos radial (QC físico + serie mensual; antes de cualquier modelo) ---
        vk = _rc.infer_variable_kind(str(col_value_resolved))
        if "acronimo" in df_gijon.columns:
            cast_keys_t: tuple[str, ...] = ("acronimo", col_estacion)
            profile_df = df_gijon
        elif "cast" in df_gijon.columns:
            cast_keys_t = ("cast", col_estacion)
            profile_df = df_gijon
        else:
            profile_df = df_gijon.copy()
            profile_df["_perf_day"] = pd.to_datetime(profile_df[col_fecha], errors="coerce").dt.normalize()
            cast_keys_t = ("_perf_day", col_estacion)

        violations: list = []
        if vk in ("temperature", "salinity"):
            if vk == "temperature":
                chk = profile_df.copy()
                if str(col_value_resolved) != "temperatura_c":
                    chk = chk.rename(columns={str(col_value_resolved): "temperatura_c"})
                if str(col_prof) != "profundidad_m":
                    chk = chk.rename(columns={str(col_prof): "profundidad_m"})
                need = ["fecha", "estacion", "profundidad_m", "temperatura_c"]
                if all(c in chk.columns for c in need):
                    try:
                        import polars as pl  # noqa: PLC0415

                        ex = [c for c in ("cast", "acronimo") if c in chk.columns]
                        violations.extend(
                            _rc.validate_canonical_ctd_polars(pl.from_pandas(chk[need + ex].copy()))
                        )
                    except Exception:
                        violations.extend(
                            _rc.validate_profile_dataframe(
                                profile_df,
                                col_prof=str(col_prof),
                                col_value=str(col_value_resolved),
                                col_estacion=col_estacion,
                                cast_keys=cast_keys_t,
                                variable_kind="temperature",
                            )
                        )
                else:
                    violations.extend(
                        _rc.validate_profile_dataframe(
                            profile_df,
                            col_prof=str(col_prof),
                            col_value=str(col_value_resolved),
                            col_estacion=col_estacion,
                            cast_keys=cast_keys_t,
                            variable_kind="temperature",
                        )
                    )
            else:
                violations.extend(
                    _rc.validate_profile_dataframe(
                        profile_df,
                        col_prof=str(col_prof),
                        col_value=str(col_value_resolved),
                        col_estacion=col_estacion,
                        cast_keys=cast_keys_t,
                        variable_kind="salinity",
                    )
                )
            violations.extend(
                _rc.validate_monthly_radial_series(
                    df_monthly,
                    col_fecha="fecha",
                    col_val="valor_prof",
                    col_estacion=col_estacion,
                    variable_kind=vk,
                )
            )

        errs = [v for v in violations if v.severity == _rc.ViolationSeverity.ERROR]
        warns = [v for v in violations if v.severity == _rc.ViolationSeverity.WARNING]

        if errs:
            st.error(
                "**Contrato de datos radial:** se han detectado incumplimientos **ERROR**. "
                "El gráfico no se muestra por defecto hasta revisión (datos no auditados)."
            )
            st.markdown(_rc.format_violations_markdown(errs))
            unlock = st.checkbox(
                "Solo diagnóstico interno: mostrar gráfico pese a errores del contrato",
                value=False,
                key=f"contract_unlock_err_{source_label}_{col_value_resolved}_{target_depth_m}{widget_key_suffix}",
            )
            if not unlock:
                return
        if warns:
            st.warning("**Contrato de datos radial:** avisos\n\n" + _rc.format_violations_markdown(warns))

        with st.expander("Cobertura de datos (transparencia)", expanded=False):
            st.markdown(
                f"- Perfiles (lances) considerados: **{diag_interp['n_casts']}**\n"
                f"- Con valor interpolado a {diag_interp['profundidad_objetivo_m']} m: **{diag_interp['n_casts_con_valor']}**\n"
                f"- Sin cobertura a esa profundidad (perfil no alcanza el rango): **{diag_interp['n_casts_sin_cobertura_en_profundidad']}**\n"
                "\n*Sin extrapolación fuera del rango vertical observado en cada lance.*"
            )

        # Selector de estación (mapa + radio) o estación fija (p. ej. salinidad ×3)
        if fixed_station is None:
            if stations_geo:
                try:
                    pts = (map_sel.selection or {}).get("points", []) if map_sel else []  # type: ignore[union-attr]
                except Exception:
                    pts = []
                if pts:
                    cd = pts[0].get("customdata", None)
                    est_val = None
                    try:
                        import numpy as _np  # noqa: PLC0415
                    except Exception:
                        _np = None  # type: ignore[assignment]
                    if isinstance(cd, (list, tuple)):
                        est_val = cd[0] if len(cd) > 0 else None
                    elif _np is not None and isinstance(cd, _np.ndarray):  # type: ignore[arg-type]
                        est_val = cd.flatten()[0] if cd.size > 0 else None
                    elif isinstance(cd, dict):
                        est_val = cd.get(0, None)
                        if est_val is None and len(cd) > 0:
                            est_val = next(iter(cd.values()))
                    if est_val is not None and str(est_val).strip() != "":
                        st.session_state[station_key] = int(float(est_val))

            ids_map = [int(s["estacion"]) for s in stations_geo]
            ids_data = sorted(df_monthly[col_estacion].dropna().unique().astype(int).tolist())
            opts = sorted(set(ids_map) | set(ids_data))
            if not opts:
                opts = [1, 2, 3]
            if int(st.session_state.get(station_key, opts[0])) not in opts:
                st.session_state[station_key] = int(opts[0])

            name_by_id = {int(s["estacion"]): str(s["nombre"]) for s in stations_geo}
            st.caption("Elija la estación para el gráfico (clic en una marca del mapa o selector siguiente).")
            st.radio(
                "Estación",
                options=opts,
                horizontal=True,
                key=station_key,
                format_func=lambda n: name_by_id.get(int(n), f"Estación {int(n)}"),
            )

            selected = int(st.session_state.get(station_key, opts[0]))
        else:
            selected = int(fixed_station)
        df_station = df_monthly[df_monthly[col_estacion] == int(selected)].copy()
        df_station["fecha"] = pd.to_datetime(df_station["fecha"], errors="coerce")
        df_station["valor_prof"] = pd.to_numeric(df_station["valor_prof"], errors="coerce")
        df_station = df_station.dropna(subset=["fecha", "valor_prof"]).sort_values("fecha")
        if df_station.empty:
            st.warning(f"[{source_label}] No hay serie mensual válida para esta estación.")
            return

        n_obs_months = float(df_station["fecha"].dt.to_period("M").nunique())

        if n_obs_months < 2:
            st.info(
                f"No se calcula **Marcos+ATAC** porque hay pocos meses observados (n={int(n_obs_months)}). "
                "Añade campañas en meses distintos para activar el ajuste."
            )
            return

        var_lbl, var_unt = _var_meta(str(col_value_resolved))
        depth_lbl = int(target_depth_m) if float(target_depth_m).is_integer() else target_depth_m
        st.subheader(f"{var_lbl} a {depth_lbl} m — Descomposición Marcos + pronóstico ATAC · Estación {selected}")
        try:
            from atac_monthly_report import build_atac_monthly_figure  # noqa: PLC0415

            monthly_for_atac = df_station[["fecha", "valor_prof"]].rename(columns={"valor_prof": "temp_5m"})
            res = build_atac_monthly_figure(
                project_root=PROJECT_ROOT,
                monthly_5m=monthly_for_atac,
                station_label=str(int(selected)),
                holdout_months=12,
                var_label=var_lbl,
                var_units=var_unt,
            )
        except Exception as exc:
            st.warning(
                f"No se pudo generar la figura Marcos+ATAC para **{var_lbl}**. "
                f"Motivo: `{type(exc).__name__}: {exc}`\n\n"
                f"Meses observados: **{int(n_obs_months)}**; rango: **{df_station['fecha'].min().date()} → {df_station['fecha'].max().date()}**."
            )
            return

        st.plotly_chart(res.fig, use_container_width=True)
        st.markdown(res.footer_md)

        if (
            df_if_overlay is not None
            and not df_if_overlay.empty
            and "_viewer_anomaly" in df_if_overlay.columns
        ):
            band = df_if_overlay[df_if_overlay[col_estacion] == int(selected)].copy()
            band[col_fecha] = pd.to_datetime(band[col_fecha], errors="coerce")
            band[col_prof] = pd.to_numeric(band[col_prof], errors="coerce")
            vcol = str(col_value_resolved)
            band[vcol] = pd.to_numeric(band[vcol], errors="coerce")
            band = band.dropna(subset=[col_fecha, col_prof, vcol])
            near = band[(band[col_prof] - float(target_depth_m)).abs() <= 3.0]
            if not near.empty:
                st.caption(
                    f"Puntos cerca de **{target_depth_m:g} m** (±3 m): limpio vs anomalía IF (misma variable que el ATAC)."
                )
                fig_if = go.Figure()
                for flag, lab, colm in [
                    (False, "Limpio (pipeline)", "#2E86AB"),
                    (True, "Anomalía IF", "#C1121F"),
                ]:
                    sl = near[near["_viewer_anomaly"] == flag]
                    if sl.empty:
                        continue
                    fig_if.add_trace(
                        go.Scatter(
                            x=sl[col_fecha],
                            y=sl[vcol],
                            mode="markers",
                            name=lab,
                            marker=dict(size=7, color=colm, opacity=0.78),
                            hovertemplate=f"%{{x|%Y-%m-%d}}<br>{var_lbl}=%{{y:.4f}}<extra></extra>",
                        )
                    )
                fig_if.update_layout(
                    title=f"{var_lbl} (±3 m de {target_depth_m:g} m) · estación {selected}",
                    xaxis_title="Fecha",
                    yaxis_title=f"{var_lbl} ({var_unt})".strip(),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02),
                    height=380,
                    margin=dict(l=48, r=24, t=48, b=48),
                )
                st.plotly_chart(fig_if, use_container_width=True)

    df_c = pipe.df_clean
    col_temp, col_sal, col_prof = pipe.col_temp, pipe.col_sal, pipe.col_prof
    depth_m = 5.0
    tab_atac, tab_if = st.tabs(["Marcos + ATAC (datos limpios)", "Anomalías Isolation Forest"])
    with tab_atac:
        st.subheader("Temperatura a 5 m · Marcos + ATAC")
        _render_series_and_atac(
            df_gijon=df_c,
            source_label="Pipeline",
            col_prof=col_prof,
            col_temp=col_temp,
            col_value=col_temp,
            target_depth_m=depth_m,
            fixed_station=None,
            df_if_overlay=pipe.df_concat_viz,
        )
        st.markdown("---")
        st.subheader("Salinidad a 5 m · Marcos + ATAC (estaciones 1–3)")
        for sid in (1, 2, 3):
            st.markdown(f"##### Estación {sid}")
            _render_series_and_atac(
                df_gijon=df_c,
                source_label="Pipeline",
                col_prof=col_prof,
                col_temp=col_temp,
                col_value=col_sal,
                target_depth_m=depth_m,
                fixed_station=int(sid),
                widget_key_suffix=f"_sal_{sid}",
                df_if_overlay=pipe.df_concat_viz,
            )
    with tab_if:
        _render_isolation_forest_tab(pipe)


def main() -> None:
    # Una vez por sesión de navegador: diario legible de arranque (no en cada interacción).
    if "_public_run_journal_written" not in st.session_state:
        st.session_state["_public_run_journal_written"] = True
        try:
            _bootstrap_ieo_repo_path()
            from ieo.observability.session_audit import append_public_run_journal_entry  # noqa: PLC0415

            append_public_run_journal_entry(PROJECT_ROOT, kind="streamlit_dashboard_boot")
        except Exception:
            pass

    st.markdown("## Radiales Cudillero · visor exploratorio")
    valid_sidebar = list_valid_run_roots(PROJECT_ROOT)
    with st.sidebar:
        st.markdown("### Corrida del pipeline")
        if valid_sidebar:
            labels: dict[str, str] = {LATEST_SENTINEL: "Última corrida (automático)"}
            for p in valid_sidebar:
                labels[p.name] = f"{p.name} · {datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m-%d %H:%M')}"
            st.selectbox(
                "Datos a visualizar",
                options=[LATEST_SENTINEL] + [p.name for p in valid_sidebar],
                format_func=lambda x: labels.get(str(x), str(x)),
                key="pipeline_run_select",
            )
            _choice = st.session_state.get("pipeline_run_select", LATEST_SENTINEL)
            _rr = resolve_run_root_for_ui(PROJECT_ROOT, _choice)
            if _rr is not None:
                with st.expander("Provenance (corrida seleccionada)", expanded=False):
                    _prov = read_provenance_dict(_rr)
                    if _prov is not None:
                        st.json(_prov)
                    else:
                        st.caption("No hay `provenance.json` en esta corrida.")
                    st.caption(
                        f"Token frescura Parquet limpio: `{clean_parquet_cache_token(_rr)}` · "
                        f"`{(_rr / 'data' / 'perfiles_all.ctd_clean.parquet').resolve()}`"
                    )
        else:
            st.caption("Sin corridas con `ctd_clean.parquet`. Ejecuta `python run/main.py` primero.")

        st.markdown("### Pipeline (local)")
        st.caption(
            "Equivale a `python run/main.py` desde la raíz del repo. Requiere `data/processed/perfiles_all.csv`."
        )
        if st.button("Ejecutar pipeline ahora", key="btn_run_pipeline_main", help="Bloquea hasta terminar (típ. segundos–minutos)."):
            import subprocess  # noqa: PLC0415

            main_py = PROJECT_ROOT / "run" / "main.py"
            with st.spinner("Ejecutando pipeline…"):
                proc = subprocess.run(
                    [sys.executable, str(main_py)],
                    cwd=str(PROJECT_ROOT),
                    capture_output=True,
                    text=True,
                    timeout=7200,
                )
            tail = (proc.stdout or "")[-6000:] + "\n--- stderr ---\n" + (proc.stderr or "")[-4000:]
            st.code(tail or "(sin salida)", language="text")
            if proc.returncode == 0:
                st.success("Pipeline terminado. Recarga la página o revisa el selector de corrida.")
            else:
                st.error(f"Código de salida: {proc.returncode}")

        st.markdown("### Trazabilidad")
        st.caption(
            "Cada arranque del visor deja constancia en un diario Markdown (apéndices). "
            "Elige la corrida arriba; el detalle de fuente está en **«Fuente de datos (corrida seleccionada)»** "
            "en la página principal."
        )
        st.code("outputs/audit/registro_ejecuciones.md", language=None)
        st.caption("Ruta relativa al clon del repositorio en el servidor o puesto de trabajo.")
        st.markdown("### Costuras a vigilar")
        st.caption(
            "Serie mensual e interpolación a 5 m · pocos meses para ATAC · "
            "coherencia de los Parquet con la metodología antes de informar."
        )

    render_radiales_cudillero()


if __name__ == "__main__":
    main()


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

import re
import sys
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import markdown
import numpy as np

# Rutas base
RUN_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = RUN_DIR.parent
METODOLOGIA_RADIALES_GIJON_MD = PROJECT_ROOT / "docs" / "metodologia_radiales_gijon.md"

# Profundidades estándar (m) para interpolación en perfil — acordar con metodología.
TARGET_DEPTHS_M: tuple[float, ...] = (5.0, 30.0, 50.0, 100.0)

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


# Garantiza importación: raíz (src.* legacy) y src/ (paquete ieo)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
_SRC_DIR = PROJECT_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from src.atac_monthly_report import build_atac_monthly_figure  # noqa: E402

# Misma convención que el resto del visor: paquete `src.*` con PROJECT_ROOT en sys.path
# (no `import ieo.*`, que solo funciona si …/src está solo como raíz).
from src.ieo.validation.radial_contract import (  # noqa: E402
    ViolationSeverity,
    format_violations_markdown,
    infer_variable_kind,
    validate_monthly_radial_series,
    validate_profile_dataframe,
)

# ---------------------------------------------------------------------------
# Configuración de página
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Radiales Gijón · IEO",
    layout="wide",
    page_icon=str(RUN_DIR / "assets" / "logo.webp"),
)

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


def parse_gijon_station_coords_from_methodology(md_text: str) -> list[dict[str, float | int | str]]:
    """
    Extrae lat/lon WGS84 de `metodologia_radiales_gijon.md` únicamente si están documentadas.

    Orden: comentario HTML `<!-- E1 … | E2 … | E3 … | E4 … -->` (también admite solo E1–E3),
    o coordenadas DMS en viñetas `**Estación N · …** (…°…′N, …°…′W)`.
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
            {"estacion": 1, "lat": float(block3.group(1)), "lon": float(block3.group(2)), "nombre": "E1 · Costa"},
            {"estacion": 2, "lat": float(block3.group(3)), "lon": float(block3.group(4)), "nombre": "E2 · Plataforma"},
            {"estacion": 3, "lat": float(block3.group(5)), "lon": float(block3.group(6)), "nombre": "E3 · Talud"},
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


def build_gijon_radial_map_figure(stations: list[dict[str, float | int | str]]) -> go.Figure:
    """
    Mapa interactivo con marcadores diferenciados por batimetría.
    Colores graduados del azul claro (costa) al azul oscuro (talud profundo).
    """
    center_lat, center_lon = 43.545, -5.661
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
        _DEPTH_LABELS = {1: "~50 m · Costa", 2: "~150 m · Plataforma", 3: "~500 m · Talud", 4: "Talud profundo"}
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
        uirevision="gijon_map",
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
        ("📂", "Fichero CNV", "salida del CTD", False),
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


def _infer_estacion_from_cnv_path(path: Path) -> int:
    """
    Heurística: `E1`…`E4` en el nombre, `station_2`, o último grupo de 1–2 dígitos razonable.
    Por defecto 1 (un solo CNV sin pista se trata como estación 1).
    """
    stem = path.stem
    m = re.search(r"[Ee]([1-9])", stem)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:station|st|est)[_\-]?(\d+)", stem, re.IGNORECASE)
    if m:
        return max(1, min(99, int(m.group(1))))
    m = re.search(r"_(\d{1,2})(?:_|$)", stem)
    if m:
        d = int(m.group(1))
        if 1 <= d <= 12:
            return d
    return 1


def _cnv_header_start_time(path: Path) -> pd.Timestamp | None:
    """Lee `# start_time = ...` típico de cabecera Sea-Bird."""
    try:
        with path.open("rt", encoding="utf-8", errors="replace") as f:
            for _ in range(500):
                line = f.readline()
                if not line:
                    break
                m = re.match(r"^\s*#\s*start_time\s*=\s*(.+?)\s*$", line, re.IGNORECASE)
                if m:
                    raw = m.group(1).strip()
                    # Sea-Bird suele añadir sufijos informativos, p. ej.:
                    # "Apr 02 2026 09:49:57 [System UTC, header]"
                    # Pandas no lo parsea si dejamos el sufijo entre corchetes.
                    raw = raw.split("[", 1)[0].strip()
                    ts = pd.to_datetime(raw, errors="coerce")
                    if pd.notna(ts):
                        return pd.Timestamp(ts)
    except OSError:
        pass
    return None


def _pick_col(cols_lower: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for cand in candidates:
        key = cand.lower()
        if key in cols_lower:
            return cols_lower[key]
    return None


# Columnas CNV opcionales (nombre estable en merge → candidatos en cabecera lower).
_CNV_EXTRA_VALUE_SPECS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fluorescencia_cdom", ("wetcdom",)),
    ("fluorescencia_afl", ("fleco-afl", "flec0-afl")),
    ("turbidez_ntu", ("turbwetntu0",)),
    ("cstar_at0", ("cstarat0",)),
    ("cstar_tr0", ("cstartr0",)),
    ("densidad00", ("density00",)),
)


def _load_merged_cnv_profiles(cnv_dir: Path) -> pd.DataFrame | None:
    """
    Lee todos los ``.cnv`` de ``cnv_dir``, normaliza a columnas ``fecha``, ``estacion``,
    ``profundidad_m``, ``temperatura_c``, ``salinidad_psu`` (opcional) y ``acronimo`` (lance).
    """
    from ieo.io.cnv_reader import CNVReader  # noqa: PLC0415
    from ieo.transform.canonical_schema import CTDCanonicalSchema, normalize_ctd_columns  # noqa: PLC0415

    paths = sorted(cnv_dir.glob("*.cnv"))
    if not paths:
        return None

    reader = CNVReader()
    chunks: list[pd.DataFrame] = []
    for fp in paths:
        est_default = _infer_estacion_from_cnv_path(fp)
        try:
            with tempfile.TemporaryDirectory(prefix="ieo_cnv_") as td:
                staging = Path(td)
                res = reader.read(fp, staging_dir=staging)
                lf = normalize_ctd_columns(res.lazyframe, schema=CTDCanonicalSchema())
                df = lf.collect().to_pandas()
        except Exception:
            continue
        if df is None or df.empty:
            continue

        df.columns = pd.Index([str(c).lower().strip() for c in df.columns])
        cmap = {str(c).lower().strip(): str(c) for c in df.columns}

        fecha_ser = pd.to_datetime(df.get("fecha"), errors="coerce") if "fecha" in df.columns else pd.Series(pd.NaT, index=df.index)
        if fecha_ser.isna().all():
            tcol = _pick_col(
                cmap,
                (
                    "timejv2",
                    "time_j",
                    "timej",
                    "time_s",
                    "times",
                    "elapsed_time",
                    "timestamp",
                    "datetime",
                    "date",
                ),
            )
            st0 = _cnv_header_start_time(fp)
            if tcol is not None:
                tnum = pd.to_numeric(df[tcol], errors="coerce")
                if st0 is not None and tnum.notna().any():
                    if float(tnum.abs().max() or 0) < 1e7:
                        fecha_ser = pd.Timestamp(st0) + pd.to_timedelta(tnum, unit="s")
                    else:
                        fecha_ser = pd.to_datetime(tnum, errors="coerce")
                elif st0 is not None:
                    fecha_ser = pd.Series(st0, index=df.index)
                else:
                    fecha_ser = pd.to_datetime(df[tcol], errors="coerce")
            elif st0 is not None:
                fecha_ser = pd.Series(st0, index=df.index)

        z_col = _pick_col(cmap, ("profundidad_m", "prdm", "depm", "depsm", "dep_sm", "depth", "prd"))
        t_col = _pick_col(cmap, ("temperatura_c", "t090c", "tv290c", "t068c", "temp09", "temp"))
        s_col = _pick_col(cmap, ("salinidad_psu", "sal00", "sal10", "sal", "prsal"))

        if z_col is None or t_col is None or fecha_ser.isna().all():
            continue

        est_col = "estacion"
        if est_col in df.columns and pd.to_numeric(df[est_col], errors="coerce").notna().any():
            est_ser = pd.to_numeric(df[est_col], errors="coerce").fillna(est_default).astype(int)
        else:
            est_ser = pd.Series(est_default, index=df.index, dtype=int)

        block_data: dict[str, object] = {
            "fecha": pd.to_datetime(fecha_ser, errors="coerce"),
            "estacion": est_ser,
            "profundidad_m": pd.to_numeric(df[z_col], errors="coerce"),
            "temperatura_c": pd.to_numeric(df[t_col], errors="coerce"),
            "salinidad_psu": pd.to_numeric(df[s_col], errors="coerce") if s_col else np.nan,
            "acronimo": fp.stem,
        }
        for out_name, cands in _CNV_EXTRA_VALUE_SPECS:
            ec = _pick_col(cmap, cands)
            if ec is not None:
                block_data[out_name] = pd.to_numeric(df[ec], errors="coerce")

        block = pd.DataFrame(block_data)
        block = block.dropna(subset=["fecha", "profundidad_m", "temperatura_c", "estacion"])
        if not block.empty:
            chunks.append(block)

    if not chunks:
        return None
    return pd.concat(chunks, ignore_index=True)


@st.cache_data(show_spinner="Cargando datos de Gijón…")
def load_and_validate_gijon() -> tuple[pd.DataFrame | None, str | None, str | None, str | None, str | None]:
    """
    Prioridad:

    1. Ficheros ``*.cnv`` en ``data/cnv/`` (flujo actual del proyecto).
    2. Si no hay CNV, CSV legado ``data/processed/sireno_gijon_ctd_processed.csv``.

    Devuelve un ``DataFrame`` con columnas canónicas mínimas para el gráfico a 5 m
    (``fecha``, ``estacion``, ``profundidad_m``, ``temperatura_c``, ``salinidad_psu``, ``acronimo``).

    Returns
    -------
    (df, col_anio, col_temp, col_sal, col_prof)
        ``col_*`` son los nombres de columna usados por el bloque ATAC (nombres fijos en modo CNV).
    """
    cnv_dir = PROJECT_ROOT / "data" / "cnv"
    if cnv_dir.is_dir() and any(cnv_dir.glob("*.cnv")):
        merged = _load_merged_cnv_profiles(cnv_dir)
        if merged is None or merged.empty:
            return None, None, None, None, None
        merged["año"] = pd.to_datetime(merged["fecha"], errors="coerce").dt.year
        return merged, "año", "temperatura_c", "salinidad_psu", "profundidad_m"

    ruta = PROJECT_ROOT / "data" / "processed" / "sireno_gijon_ctd_processed.csv"
    if not ruta.exists():
        return None, None, None, None, None

    df = pd.read_csv(ruta, sep=None, engine="python")
    df.columns = pd.Index([str(c).lower().strip() for c in df.columns])
    cols: list[str] = [str(c) for c in df.columns]

    # --- inferencia del año ---
    # Prioridad 1: columna "fecha" parseable como datetime
    col_anio: str | None = None
    if "fecha" in cols:
        parsed_fecha = pd.to_datetime(df["fecha"], errors="coerce")
        if parsed_fecha.notna().any():
            df["año"] = parsed_fecha.dt.year
            col_anio = "año"
    # Prioridad 2: columna numérica directa de año (incluye "anio" sin tilde)
    if col_anio is None:
        col_anio = next((c for c in cols if c in ("anio", "año", "ano", "year", "yy")), None)
    # Prioridad 3: extraer el año de columnas de texto (acronimo, estacion, …)
    if col_anio is None:
        for col_name in [*[c for c in cols if c in ("estacion", "acronimo")], *df.select_dtypes(include=["object"]).columns]:
            extracted = df[col_name].astype(str).str.extract(r"((?:19|20)\d{2})")[0]
            if extracted.notna().any():
                df["año"] = pd.to_numeric(extracted, errors="coerce")
                col_anio = "año"
                break
    # Fallback — NO generar años sintéticos; devolvemos None para que la UI avise
    if col_anio is None:
        return None, None, None, None, None

    col_temp: str | None = next((c for c in cols if "temp" in c), None)
    col_sal: str | None = next((c for c in cols if "salin" in c or c == "sal"), None)
    col_prof: str | None = next((c for c in cols if "prof" in c), None)

    if not all([col_anio, col_temp, col_sal, col_prof]):
        return None, None, None, None, None

    df[col_prof] = pd.to_numeric(df[col_prof], errors="coerce")  # type: ignore[arg-type]
    return df, col_anio, col_temp, col_sal, col_prof


# ---------------------------------------------------------------------------
# Vistas
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Cargando datos CNV de Gijón…")
def load_gijon_from_cnv() -> pd.DataFrame | None:
    """Carga CNV desde `data/cnv/*.cnv` y devuelve el DataFrame canónico mínimo (o None)."""
    cnv_dir = PROJECT_ROOT / "data" / "cnv"
    if not (cnv_dir.is_dir() and any(cnv_dir.glob("*.cnv"))):
        return None
    merged = _load_merged_cnv_profiles(cnv_dir)
    if merged is None or merged.empty:
        return None
    merged["año"] = pd.to_datetime(merged["fecha"], errors="coerce").dt.year
    return merged


@st.cache_data(show_spinner="Cargando datos CSV de Gijón…")
def load_gijon_from_csv_legacy() -> tuple[pd.DataFrame | None, str | None, str | None, str | None, str | None]:
    """
    Carga el CSV legado `data/processed/sireno_gijon_ctd_processed.csv`.
    Devuelve (df, col_anio, col_temp, col_sal, col_prof) o (None, …) si no es válido.
    """
    ruta = PROJECT_ROOT / "data" / "processed" / "sireno_gijon_ctd_processed.csv"
    if not ruta.exists():
        return None, None, None, None, None

    df = pd.read_csv(ruta, sep=None, engine="python")
    df.columns = pd.Index([str(c).lower().strip() for c in df.columns])
    cols: list[str] = [str(c) for c in df.columns]

    # --- inferencia del año ---
    # Prioridad 1: columna "fecha" parseable como datetime
    col_anio: str | None = None
    if "fecha" in cols:
        parsed_fecha = pd.to_datetime(df["fecha"], errors="coerce")
        if parsed_fecha.notna().any():
            df["año"] = parsed_fecha.dt.year
            col_anio = "año"
    # Prioridad 2: columna numérica directa de año (incluye "anio" sin tilde)
    if col_anio is None:
        col_anio = next((c for c in cols if c in ("anio", "año", "ano", "year", "yy")), None)
    # Prioridad 3: extraer el año de columnas de texto (acronimo, estacion, …)
    if col_anio is None:
        for col_name in [*[c for c in cols if c in ("estacion", "acronimo")], *df.select_dtypes(include=["object"]).columns]:
            extracted = df[col_name].astype(str).str.extract(r"((?:19|20)\d{2})")[0]
            if extracted.notna().any():
                df["año"] = pd.to_numeric(extracted, errors="coerce")
                col_anio = "año"
                break
    # Fallback — NO generar años sintéticos; devolvemos None para que la UI avise
    if col_anio is None:
        return None, None, None, None, None

    col_temp: str | None = next((c for c in cols if "temp" in c), None)
    col_sal: str | None = next((c for c in cols if "salin" in c or c == "sal"), None)
    col_prof: str | None = next((c for c in cols if "prof" in c), None)

    if not all([col_anio, col_temp, col_sal, col_prof]):
        return None, None, None, None, None

    df[col_prof] = pd.to_numeric(df[col_prof], errors="coerce")  # type: ignore[arg-type]
    return df, col_anio, col_temp, col_sal, col_prof


def render_radiales_gijon() -> None:
    """Vista Radiales Gijón: mapa + metodología; series/ATAC desde ``data/cnv/*.cnv`` (o CSV legado)."""

    # Las pestañas (CSV/CNV) usan claves distintas para evitar colisiones de widgets.
    if "estacion_seleccionada_csv" not in st.session_state:
        st.session_state["estacion_seleccionada_csv"] = 1
    if "estacion_seleccionada_cnv" not in st.session_state:
        st.session_state["estacion_seleccionada_cnv"] = 1

    st.header("Radiales Gijón")
    _render_data_governance_card()

    if METODOLOGIA_RADIALES_GIJON_MD.exists():
        try:
            md_method = METODOLOGIA_RADIALES_GIJON_MD.read_text(encoding="utf-8")
        except Exception:
            md_method = load_methodology("radiales_gijon")
    else:
        md_method = load_methodology("radiales_gijon")
    stations_geo = parse_gijon_station_coords_from_methodology(md_method)

    col_map, col_txt = st.columns([1, 1], gap="large")

    with col_map:
        if not stations_geo:
            st.info(
                "Aún no hay coordenadas en metadatos del markdown: el mapa muestra la zona de referencia. "
                "Añada al final de `docs/metodologia_radiales_gijon.md` la línea `<!-- E1 lat lon | … | E4 … -->` "
                "(solo metadatos; el visor la lee y no se muestra al usuario en el texto)."
            )
        fig_map = build_gijon_radial_map_figure(stations_geo)
        map_sel = st.plotly_chart(
            fig_map,
            use_container_width=True,
            on_select="rerun",
            selection_mode="points",
            key="gijon_mapbox",
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
    ) -> None:
        station_key = f"estacion_seleccionada_{source_label.lower().strip()}"
        if df_gijon is None or df_gijon.empty:
            if source_label.lower().startswith("cnv"):
                st.warning(
                    "No hay datos CNV para gráficos: coloca uno o más **`.cnv`** en **`data/cnv/`** "
                    "(presión/profundidad, temperatura y tiempo o cabecera `# start_time`). "
                    "Si el CNV no trae `estacion`, se infiere del **nombre del fichero** (p. ej. `…E2…`)."
                )
            else:
                st.warning(
                    "No hay datos CSV para gráficos: falta **`data/processed/sireno_gijon_ctd_processed.csv`** "
                    "o no contiene columnas reconocibles (año/fecha, profundidad, temperatura, salinidad)."
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
        vk = infer_variable_kind(str(col_value_resolved))
        if "acronimo" in df_gijon.columns:
            cast_keys_t: tuple[str, ...] = ("acronimo", col_estacion)
        elif "cast" in df_gijon.columns:
            cast_keys_t = ("cast", col_estacion)
        else:
            cast_keys_t = (col_estacion,)

        violations: list = []
        if vk in ("temperature", "salinity"):
            violations.extend(
                validate_profile_dataframe(
                    df_gijon,
                    col_prof=str(col_prof),
                    col_value=str(col_value_resolved),
                    col_estacion=col_estacion,
                    cast_keys=cast_keys_t,
                    variable_kind=vk,
                )
            )
            violations.extend(
                validate_monthly_radial_series(
                    df_monthly,
                    col_fecha="fecha",
                    col_val="valor_prof",
                    col_estacion=col_estacion,
                    variable_kind=vk,
                )
            )

        errs = [v for v in violations if v.severity == ViolationSeverity.ERROR]
        warns = [v for v in violations if v.severity == ViolationSeverity.WARNING]

        if errs:
            st.error(
                "**Contrato de datos radial:** se han detectado incumplimientos **ERROR**. "
                "El gráfico no se muestra por defecto hasta revisión (datos no auditados)."
            )
            st.markdown(format_violations_markdown(errs))
            unlock = st.checkbox(
                "Solo diagnóstico interno: mostrar gráfico pese a errores del contrato",
                value=False,
                key=f"contract_unlock_err_{source_label}_{col_value_resolved}_{target_depth_m}",
            )
            if not unlock:
                return
        if warns:
            st.warning("**Contrato de datos radial:** avisos\n\n" + format_violations_markdown(warns))

        with st.expander("Cobertura de datos (transparencia)", expanded=False):
            st.markdown(
                f"- Perfiles (lances) considerados: **{diag_interp['n_casts']}**\n"
                f"- Con valor interpolado a {diag_interp['profundidad_objetivo_m']} m: **{diag_interp['n_casts_con_valor']}**\n"
                f"- Sin cobertura a esa profundidad (perfil no alcanza el rango): **{diag_interp['n_casts_sin_cobertura_en_profundidad']}**\n"
                "\n*Sin extrapolación fuera del rango vertical observado en cada lance.*"
            )

        # Selector estación (reutiliza mapa si aplica)
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

    tab_csv, tab_cnv = st.tabs(["Serie (CSV) — robustez Marcos", "Serie (CNV) — nuevo flujo"])
    with tab_csv:
        df_csv, _, col_temp, _col_sal, col_prof = load_gijon_from_csv_legacy()
        if df_csv is None:
            _render_series_and_atac(
                df_gijon=None,
                source_label="CSV",
                col_prof=col_prof,
                col_temp=col_temp,
            )
        else:
            depth_labels = [f"{int(d)} m" if d == int(d) else f"{d} m" for d in TARGET_DEPTHS_M]
            depth_map = dict(zip(depth_labels, [float(d) for d in TARGET_DEPTHS_M], strict=True))
            dlab = st.selectbox("Profundidad objetivo", depth_labels, index=0, key="csv_depth_sel")
            depth_m = depth_map[dlab]
            cand_vars = ["temperatura", "salinidad", "fluorescencia", "clorofila", "par", "oxigeno", "o_100"]
            available = [
                c
                for c in cand_vars
                if c in df_csv.columns and pd.to_numeric(df_csv[c], errors="coerce").notna().any()
            ]
            if not available:
                st.warning(
                    "[CSV] Ninguna columna de variable prevista tiene datos numéricos "
                    f"entre {cand_vars}."
                )
            else:
                vcol = st.selectbox("Variable", available, key="csv_variable_sel")
                _render_series_and_atac(
                    df_gijon=df_csv,
                    source_label="CSV",
                    col_prof=col_prof,
                    col_temp=col_temp,
                    col_value=vcol,
                    target_depth_m=depth_m,
                )

    with tab_cnv:
        df_cnv = load_gijon_from_cnv()
        if df_cnv is None:
            _render_series_and_atac(
                df_gijon=None,
                source_label="CNV",
                col_prof="profundidad_m",
                col_temp="temperatura_c",
            )
        else:
            depth_labels = [f"{int(d)} m" if d == int(d) else f"{d} m" for d in TARGET_DEPTHS_M]
            depth_map = dict(zip(depth_labels, [float(d) for d in TARGET_DEPTHS_M], strict=True))
            dlab = st.selectbox("Profundidad objetivo", depth_labels, index=0, key="cnv_depth_sel")
            depth_m = depth_map[dlab]
            skip = {"fecha", "estacion", "profundidad_m", "acronimo", "año"}
            num_cols = [
                str(c)
                for c in df_cnv.columns
                if str(c) not in skip and pd.api.types.is_numeric_dtype(df_cnv[c])
            ]
            pref_order = ["temperatura_c", "salinidad_psu"] + [spec[0] for spec in _CNV_EXTRA_VALUE_SPECS]
            ordered = [c for c in pref_order if c in num_cols]
            ordered.extend([c for c in num_cols if c not in ordered])
            if not ordered:
                st.warning("[CNV] No hay columnas numéricas de variables para graficar.")
            else:
                vcol = st.selectbox("Variable", ordered, key="cnv_variable_sel")
                _render_series_and_atac(
                    df_gijon=df_cnv,
                    source_label="CNV",
                    col_prof="profundidad_m",
                    col_temp="temperatura_c",
                    col_value=vcol,
                    target_depth_m=depth_m,
                )


def main() -> None:
    # Una vez por sesión de navegador: diario legible de arranque (no en cada interacción).
    if "_public_run_journal_written" not in st.session_state:
        st.session_state["_public_run_journal_written"] = True
        try:
            from ieo.observability.session_audit import append_public_run_journal_entry  # noqa: PLC0415

            append_public_run_journal_entry(PROJECT_ROOT, kind="streamlit_dashboard_boot")
        except Exception:
            pass

    st.markdown("## Radiales Gijón · visor exploratorio")
    with st.sidebar:
        st.markdown("### Trazabilidad")
        st.caption(
            "Cada arranque del visor deja constancia en un diario Markdown (apéndices), "
            "con advertencias sobre limitaciones del análisis."
        )
        st.code("outputs/audit/registro_ejecuciones.md", language=None)
        st.caption("Ruta relativa al clon del repositorio en el servidor o puesto de trabajo.")
        st.markdown("### Costuras a vigilar")
        st.caption(
            "Inferencias en CNV · cabeceras SBE · pocos meses para ATAC · "
            "CSV vs CNV deben alinearse antes de informar."
        )

    render_radiales_gijon()


if __name__ == "__main__":
    main()


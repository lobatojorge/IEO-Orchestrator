"""
================================================================================
00_data_scout.py — Agente de Reconocimiento de Datasets Crudos
================================================================================
Rol    : Data Engineer / Arquitecto de Datos Oceánicos
Misión : Analizar la estructura de un dataset crudo SIN modificarlo y exportar
         un informe de auditoría a outputs/reports/.

Uso    : python src/00_data_scout.py
================================================================================
"""

from __future__ import annotations

import sys
import io
from datetime import datetime
from pathlib import Path
import csv

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN DE RUTAS  (todo relativo al raíz del proyecto)
# ──────────────────────────────────────────────────────────────────────────────

# El script vive en src/, así que el raíz del proyecto es su carpeta padre.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CNV_DIR     = PROJECT_ROOT / "data" / "cnv"
REPORTS_DIR = PROJECT_ROOT / "outputs" / "reports"

# Flujo actual: reconocimiento de datasets procesables en data/cnv
REPORT_NAME = "recon_data_cnv.txt"
DATA_DICTIONARY_PATH = PROJECT_ROOT / "docs" / "data_dictionary.yaml"
PROCESSED_SIRENO_PATH = PROJECT_ROOT / "data" / "processed" / "sireno_gijon_ctd_processed.csv"


# ──────────────────────────────────────────────────────────────────────────────
# FUNCIONES AUXILIARES
# ──────────────────────────────────────────────────────────────────────────────


def load_data_dictionary(path: Path) -> dict[str, dict[str, str]] | None:
    """
    Carga el glosario de variables desde docs/data_dictionary.yaml.
    Estructura: { nombre_archivo: { nombre_columna: descripcion } }.
    Si el archivo no existe, está mal formado o falta PyYAML, devuelve None
    (el scout seguirá funcionando sin descripciones).
    """
    if not path.exists():
        return None
    try:
        import yaml
    except ImportError:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _glossary_description_for_column(
    file_glossary: dict[str, str], column_name: str
) -> str | None:
    """Busca descripción para una columna (coincidencia case-insensitive)."""
    col_norm = str(column_name).strip().lower()
    for k, v in (file_glossary or {}).items():
        if str(k).strip().lower() == col_norm and v:
            return v
    return None


def _columns_without_description(
    file_glossary: dict[str, str], column_names: list
) -> list[str]:
    """Columnas del DataFrame que no tienen entrada en el glosario del archivo."""
    if not file_glossary:
        return list(column_names)
    result = []
    for c in column_names:
        if _glossary_description_for_column(file_glossary, c) is None:
            result.append(str(c))
    return result

def detect_file(directory: Path, stem: str) -> Path:
    """
    Busca el archivo con el *stem* dado en *directory* probando
    las extensiones .xlsx, .xls y .csv (en ese orden de preferencia).

    Lanza FileNotFoundError si no encuentra ninguna variante.
    """
    for ext in (".xlsx", ".xls", ".csv"):
        candidate = directory / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"No se encontró ningún archivo con nombre '{stem}' "
        f"(.xlsx / .xls / .csv) en:\n  {directory}"
    )


def _peek_text_table(filepath: Path, *, max_lines: int = 200) -> tuple[list[str], int | None, str | None]:
    """
    Lee de forma ligera un CSV/TSV/TXT y devuelve:
    - header (lista de columnas) si se pudo
    - n_cols inferido
    - error si falló
    """
    try:
        with filepath.open("rt", encoding="utf-8", errors="replace", newline="") as f:
            sample_lines = []
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                sample_lines.append(line)
        if not sample_lines:
            return [], None, "Archivo vacío"
        # heurística: detectar separador por primera línea
        first = sample_lines[0]
        sep = "\t" if "\t" in first else (";" if ";" in first else ",")
        reader = csv.reader(sample_lines, delimiter=sep)
        rows = list(reader)
        if not rows:
            return [], None, "No se pudo parsear"
        header = [str(x).strip() for x in rows[0]]
        n_cols = len(header)
        return header, n_cols, None
    except Exception as exc:
        return [], None, f"{type(exc).__name__}: {exc}"


def _peek_excel(filepath: Path) -> tuple[list[str], int | None, list[str] | None, str | None]:
    """
    Lee Excel sin pandas (python-calamine) y devuelve:
    - header
    - n_cols
    - sheet_names
    - error si falló
    """
    try:
        from python_calamine import CalamineWorkbook  # type: ignore[import-untyped]
    except Exception as exc:
        return [], None, None, f"ImportError: falta python-calamine ({exc})"
    try:
        wb = CalamineWorkbook.from_path(str(filepath))
        sheets = wb.sheet_names
        if not sheets:
            return [], None, [], "Excel sin hojas"
        sheet = sheets[0]
        rows = wb.get_sheet_by_name(sheet).to_python()
        if not rows:
            return [], None, sheets, "Hoja vacía"
        header = [str(x).strip() for x in rows[0]]
        return header, len(header), sheets, None
    except Exception as exc:
        return [], None, None, f"{type(exc).__name__}: {exc}"

def _try_build_sampling_coverage_block_excel(filepath: Path) -> str | None:
    """
    Bloque de observabilidad: cobertura de muestreo por año/mes.

    Intención (responde a la pregunta):
      - "¿Este año se ha muestreado bien o faltan meses?"

    Nota:
    - Se ejecuta SOLO para Excels (porque ya usamos python-calamine en el scout).
    - Es una lectura más "profunda" que el resto del scout (recorre filas), pero
      sigue evitando pandas.
    """
    # Solo para Excel
    if filepath.suffix.lower() not in (".xls", ".xlsx"):
        return None

    try:
        from python_calamine import CalamineWorkbook  # type: ignore[import-untyped]
    except Exception:
        return None

    try:
        wb = CalamineWorkbook.from_path(str(filepath))
        sheets = wb.sheet_names
        if not sheets:
            return None
        sheet = sheets[0]
        rows = wb.get_sheet_by_name(sheet).to_python()
        if not rows or len(rows) < 2:
            return None
    except Exception:
        return None

    header = [str(x).strip() for x in rows[0]]
    idx: dict[str, int] = {str(c).strip().lower(): i for i, c in enumerate(header)}

    # Columnas mínimas esperables (tolerantes)
    # - año: año/ano/year/anio
    # - mes: mes/month
    # - estación: estacion/station
    year_key = next((k for k in ("año", "ano", "year", "anio") if k in idx), None)
    month_key = next((k for k in ("mes", "month") if k in idx), None)
    station_key = next((k for k in ("estacion", "station") if k in idx), None)
    if year_key is None or month_key is None:
        return None

    iy = idx[year_key]
    im = idx[month_key]
    ie = idx[station_key] if station_key is not None else None

    # Recolectar meses únicos por año (y por estación si existe)
    months_by_year: dict[int, set[int]] = {}
    months_by_year_station: dict[tuple[int, str], set[int]] = {}

    def _to_int(v: object) -> int | None:
        if v is None:
            return None
        try:
            # Calamine puede dar floats para enteros
            f = float(v)
            if not (f == f):  # NaN
                return None
            return int(f)
        except Exception:
            s = str(v).strip()
            if not s:
                return None
            try:
                return int(float(s))
            except Exception:
                return None

    for r in rows[1:]:
        if not isinstance(r, list):
            continue
        if iy >= len(r) or im >= len(r):
            continue
        yy = _to_int(r[iy])
        mm = _to_int(r[im])
        if yy is None or mm is None:
            continue
        if mm < 1 or mm > 12:
            continue
        months_by_year.setdefault(yy, set()).add(mm)
        if ie is not None and ie < len(r):
            est = str(r[ie]).strip()
            if est:
                months_by_year_station.setdefault((yy, est), set()).add(mm)

    if not months_by_year:
        return None

    # Norma operativa = mediana histórica de nº de meses por año
    counts = sorted(len(v) for v in months_by_year.values())
    mid = len(counts) // 2
    median = counts[mid] if len(counts) % 2 == 1 else int(round((counts[mid - 1] + counts[mid]) / 2))

    # Render del bloque (texto legible)
    thin = "─" * 72
    lines: list[str] = []
    lines += [
        thin,
        "  OBSERVABILIDAD · COBERTURA MENSUAL DE MUESTREO (por año)",
        thin,
        "  Interpretación práctica:",
        f"    - Norma operativa (mediana histórica): {median} mes(es) muestreados por año.",
        "    - Un año con muchos menos meses que la norma sugiere sesgo estacional o falta de campañas.",
        "",
        "  Resumen por año (todas las estaciones juntas):",
        "    Año   Meses_muestreados   Estado",
        "    ───   ────────────────   ─────────────────────────────────────────────",
    ]

    for yy in sorted(months_by_year.keys()):
        n = len(months_by_year[yy])
        # Umbral simple (misma idea que la auditoría anterior): desviación ≥ 3 meses
        delta = n - median
        estado = "OK" if abs(delta) < 3 else "ALERTA: cobertura anómala vs norma"
        lines.append(f"    {yy:<5} {n:<16}   {estado}")

    # Si hay estación, añadimos un mini-resumen por estación (top 3 líneas por año)
    if months_by_year_station:
        lines += [
            "",
            "  Desglose por estación (meses únicos por año):",
            "    Año   Estación   Meses_muestreados",
            "    ───   ────────   ────────────────",
        ]
        for (yy, est) in sorted(months_by_year_station.keys(), key=lambda t: (t[0], t[1])):
            lines.append(f"    {yy:<5} {est:<8}   {len(months_by_year_station[(yy, est)]):<16}")

    lines.append("")
    return "\n".join(lines)

def _try_build_sampling_coverage_block_processed_csv(filepath: Path) -> str | None:
    """
    Bloque de observabilidad (robusto, sin dependencias): cobertura mensual por año
    usando el CSV procesado si existe.

    Responde: "¿este año se ha muestreado bien o faltan meses?" en lo que
    realmente afecta al análisis a 5 m:
      - Cuenta un mes como "muestreado" si existe al menos un perfil (cast)
        en ese mes cuya profundidad cubre 5 m (min_z <= 5 <= max_z).
    """
    if not filepath.exists():
        return None
    try:
        with filepath.open("rt", encoding="utf-8", errors="replace", newline="") as f:
            # detectar separador con la cabecera
            first = f.readline()
            if not first:
                return None
            sep = "\t" if "\t" in first else (";" if ";" in first else ",")
            header = [h.strip() for h in first.strip().split(sep)]
            header_l = [h.lower().strip() for h in header]

            def _col(*cands: str) -> int | None:
                for c in cands:
                    if c in header_l:
                        return header_l.index(c)
                return None

            i_fecha = _col("fecha")
            i_est = _col("estacion", "est")
            i_prof = _col("profundidad", "profundidad_m", "prof", "depth")
            i_cast = _col("acronimo", "cast")
            if i_fecha is None or i_est is None or i_prof is None:
                return None
            if i_cast is None:
                # fallback: cast = fecha (día) + estación (menos robusto)
                i_cast = -1

            reader = csv.reader(f, delimiter=sep)

            # min/max z por (cast, estación, año, mes)
            # key -> (min_z, max_z)
            mm: dict[tuple[str, str, int, int], tuple[float, float]] = {}

            def _to_float(v: str) -> float | None:
                s = (v or "").strip().replace(",", ".")
                if not s:
                    return None
                try:
                    x = float(s)
                    if not (x == x):
                        return None
                    return x
                except Exception:
                    return None

            def _parse_year_month(s: str) -> tuple[int, int] | None:
                # esperado: YYYY-MM-DD (o similar). Tomamos YYYY y MM.
                t = (s or "").strip()
                if len(t) < 7:
                    return None
                # normalizar separadores
                t = t.replace("/", "-")
                try:
                    yy = int(t[0:4])
                    mm_ = int(t[5:7])
                    if mm_ < 1 or mm_ > 12:
                        return None
                    return yy, mm_
                except Exception:
                    return None

            for row in reader:
                try:
                    if not row or max(i_fecha, i_est, i_prof) >= len(row):
                        continue
                    ym = _parse_year_month(row[i_fecha])
                    if ym is None:
                        continue
                    yy, mo = ym
                    est = str(row[i_est]).strip()
                    if not est:
                        continue
                    z = _to_float(row[i_prof])
                    if z is None:
                        continue
                    if i_cast == -1:
                        cast = f"{row[i_fecha].strip()}|{est}"
                    else:
                        if i_cast >= len(row):
                            continue
                        cast = str(row[i_cast]).strip()
                        if not cast:
                            cast = f"{row[i_fecha].strip()}|{est}"
                    k = (cast, est, yy, mo)
                    if k not in mm:
                        mm[k] = (z, z)
                    else:
                        lo, hi = mm[k]
                        mm[k] = (min(lo, z), max(hi, z))
                except Exception:
                    continue

    except Exception:
        return None

    if not mm:
        return None

    # Meses con cobertura 5 m por (año, estación)
    months_by_year_station: dict[tuple[int, str], set[int]] = {}
    for (_cast, est, yy, mo), (lo, hi) in mm.items():
        if lo <= 5.0 <= hi:
            months_by_year_station.setdefault((yy, est), set()).add(mo)

    if not months_by_year_station:
        return None

    # Agregado por año (todas estaciones)
    months_by_year: dict[int, set[int]] = {}
    for (yy, _est), ms in months_by_year_station.items():
        months_by_year.setdefault(yy, set()).update(ms)

    counts = sorted(len(v) for v in months_by_year.values())
    mid = len(counts) // 2
    median = counts[mid] if len(counts) % 2 == 1 else int(round((counts[mid - 1] + counts[mid]) / 2))

    thin = "─" * 72
    lines: list[str] = []
    lines += [
        thin,
        "  OBSERVABILIDAD · ¿SE HA MUESTREADO BIEN ESTE AÑO? (a 5 m)",
        thin,
        "  Fuente:",
        f"    - {filepath.relative_to(PROJECT_ROOT)}",
        "  Criterio:",
        "    - Un mes cuenta como 'muestreado' si existe al menos 1 perfil en ese mes",
        "      cuya profundidad cubre 5 m (min_z ≤ 5 ≤ max_z).",
        "",
        "  Interpretación práctica:",
        f"    - Norma operativa (mediana histórica): {median} mes(es) con cobertura a 5 m por año.",
        "    - Si un año difiere mucho de la norma (±3 meses o más), se marca ALERTA.",
        "",
        "  Resumen por año (todas las estaciones):",
        "    Año   Meses_con_5m   Estado",
        "    ───   ────────────   ─────────────────────────────────────────────",
    ]
    for yy in sorted(months_by_year.keys()):
        n = len(months_by_year[yy])
        delta = n - median
        estado = "OK" if abs(delta) < 3 else "ALERTA: faltan/ sobran meses vs norma"
        lines.append(f"    {yy:<5} {n:<12}   {estado}")

    lines += [
        "",
        "  Desglose por estación:",
        "    Año   Estación   Meses_con_5m",
        "    ───   ────────   ────────────",
    ]
    for (yy, est) in sorted(months_by_year_station.keys(), key=lambda t: (t[0], t[1])):
        lines.append(f"    {yy:<5} {est:<8}   {len(months_by_year_station[(yy, est)]):<12}")
    lines.append("")
    return "\n".join(lines)


def build_report(
    filepath: Path,
    *,
    header: list[str],
    n_cols: int | None,
    sheet_names: list[str] | None,
    glossary: dict[str, dict[str, str]] | None = None,
    read_error: str | None = None,
) -> str:
    """
    Construye el texto completo del informe de reconocimiento.
    Si glossary está presente, enriquece con descripciones y añade sección
    "Columnas sin descripción".
    """
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sep    = "═" * 72
    thin   = "─" * 72
    lines: list[str] = []

    # ── Encabezado ────────────────────────────────────────────────────────────
    lines += [
        sep,
        "  INFORME DE RECONOCIMIENTO — DATA SCOUT  v1.0",
        f"  Proyecto  : Antigravity / IEO",
        f"  Fecha     : {now}",
        sep,
        "",
        f"  Archivo   : {filepath.name}",
        f"  Ruta      : {filepath.relative_to(PROJECT_ROOT)}",
        f"  Tamaño    : {filepath.stat().st_size / 1024:.2f} KB",
        "",
    ]

    # ── Pestañas (sólo Excel) ─────────────────────────────────────────────────
    if sheet_names is not None:
        lines.append(thin)
        lines.append("  PESTAÑAS DETECTADAS (Excel)")
        lines.append(thin)
        for i, name in enumerate(sheet_names, start=1):
            marker = " ◄ analizada" if i == 1 else ""
            lines.append(f"    [{i}] {name}{marker}")
        lines.append("")

    # ── Dimensiones (ligeras) ────────────────────────────────────────────────
    lines += [
        thin,
        "  DIMENSIONES",
        thin,
        "    Filas     : (no calculado en scout ligero)",
        f"    Columnas  : {n_cols if n_cols is not None else '(no inferido)'}",
        "",
    ]

    # ── Detalle por columna (solo nombres) ───────────────────────────────────
    file_glossary = (glossary or {}).get(filepath.name) or {}
    lines += [
        thin,
        "  DETALLE DE COLUMNAS",
        thin,
        f"  {'#':<5} {'NOMBRE':<40} {'DESCRIPCIÓN (si existe)':<30}",
        f"  {'─'*5} {'─'*40} {'─'*30}",
    ]

    for idx, col in enumerate(header, start=1):
        desc = _glossary_description_for_column(file_glossary, col) or ""
        lines.append(f"  {idx:<5} {str(col):<40} {str(desc):<30}")

    lines.append("")

    # ── Columnas sin descripción (si hay glosario cargado) ─────────────────────
    if glossary is not None:
        sin_desc = _columns_without_description(file_glossary, list(header))
        lines += [
            thin,
            "  COLUMNAS SIN DESCRIPCIÓN (preguntar al creador de los datos)",
            thin,
        ]
        if sin_desc:
            for c in sin_desc:
                lines.append(f"    - {c}")
        else:
            lines.append("    (ninguna)")
        lines.append("")

    # ── Resumen (ligero) ─────────────────────────────────────────────────────
    lines += [
        thin,
        "  RESUMEN DE CALIDAD",
        thin,
        "    Este scout es ligero: no calcula nulos ni tipos (evita cargar todo).",
        f"    Error de lectura      : {read_error or '-'}",
        "",
        sep,
        "  FIN DEL INFORME",
        sep,
    ]

    return "\n".join(lines)


def scan_raw_files(directory: Path) -> list[Path]:
    """
    Devuelve la lista de archivos a analizar en `data/cnv/`.

    Extensiones soportadas (case-insensitive):
    - .csv, .tsv, .txt
    - .xlsx, .xls
    """
    exts = {".csv", ".tsv", ".txt", ".xlsx", ".xls"}
    files: list[Path] = []

    if not directory.exists():
        return files

    for p in directory.iterdir():
        if p.is_file() and p.suffix.lower() in exts:
            files.append(p)

    return sorted(files)


def load_dataset_safe(filepath: Path) -> tuple[list[str], int | None, list[str] | None, dict[str, str | None]]:
    """
    Scout ligero (sin pandas): intenta extraer SOLO cabeceras y metadatos.
    Nunca lanza: devuelve header vacío si falla.
    """
    ext = filepath.suffix.lower()
    meta: dict[str, str | None] = {"engine": None, "error": None}

    if ext in (".csv", ".tsv", ".txt"):
        header, n_cols, err = _peek_text_table(filepath)
        meta["engine"] = "csv (ligero)"
        meta["error"] = err
        return header, n_cols, None, meta

    if ext in (".xlsx", ".xls"):
        header, n_cols, sheets, err = _peek_excel(filepath)
        meta["engine"] = "python-calamine"
        meta["error"] = err
        return header, n_cols, sheets, meta

    meta["engine"] = None
    meta["error"] = f"Extensión no soportada: '{ext}'"
    return [], None, None, meta


def build_report_with_meta(
    filepath: Path,
    header: list[str],
    n_cols: int | None,
    sheet_names: list[str] | None,
    meta: dict[str, str | None],
    glossary: dict[str, dict[str, str]] | None = None,
) -> str:
    """
    Envuelve `build_report` añadiendo un bloque explícito de parámetros
    de lectura (encoding, separador, engine, errores).
    """
    core = build_report(
        filepath,
        header=header,
        n_cols=n_cols,
        sheet_names=sheet_names,
        glossary=glossary,
        read_error=meta.get("error"),
    )

    meta_block = [
        "",
        "  PARÁMETROS DE LECTURA",
        "  ─────────────────────────────────────────────────────────────",
        f"    Encoding usado : {meta.get('encoding') or '-'}",
        f"    Separador usado: {meta.get('sep') or '-'}",
        f"    Engine         : {meta.get('engine') or '-'}",
        f"    Último error   : {meta.get('error') or '-'}",
        "",
    ]

    # Bloque extra de observabilidad (solo si se puede calcular)
    extra = None
    try:
        extra = _try_build_sampling_coverage_block_excel(filepath)
    except Exception:
        extra = None

    if extra:
        return core + "\n" + "\n".join(meta_block) + "\n" + extra
    return core + "\n" + "\n".join(meta_block)

def save_report(report_text: str, output_path: Path) -> None:
    """Guarda el informe en disco, creando el directorio si no existe."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report_text, encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# PUNTO DE ENTRADA
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # Forzar UTF-8 en stdout para mostrar correctamente los caracteres del
    # informe en terminales Windows que usan CP1252 como encoding por defecto.
    # open() sobre el buffer subyacente es más type-safe que .reconfigure().
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer,  # type: ignore[union-attr]
            encoding="utf-8",
            errors="replace",
            line_buffering=True,
        )
    except AttributeError:
        pass  # Entornos sin .buffer (p.ej. IDLE, notebooks) — se ignora.

    print("\n[*] Iniciando agente de reconocimiento...\n")

    # 0 · Cargar glosario de variables (opcional) ---------------------------
    glossary = load_data_dictionary(DATA_DICTIONARY_PATH)
    if glossary:
        print("[OK] Glosario de variables cargado (docs/data_dictionary.yaml)")
    else:
        print("[--] Sin glosario; informe sin descripciones semánticas.")

    # 1 · Escanear archivos en data/cnv --------------------------------------
    files = scan_raw_files(CNV_DIR)
    if not files:
        print(f"[!!] No se encontraron archivos soportados en {CNV_DIR}")
        sys.exit(1)

    print(f"[OK] Archivos detectados ({len(files)}) en data/cnv/:")
    for p in files:
        print(f"     - {p.name}")

    all_reports: list[str] = []

    # 2 · Procesar cada archivo de forma independiente (sin fugas de estado) -
    for filepath in files:
        print(f"\n[>] Analizando archivo: {filepath.name}")

        # Inicializar variables por archivo para evitar fugas de estado
        sheet_names: list[str] | None = None
        meta: dict[str, str | None] = {}

        # 2.1 · Carga robusta
        header, n_cols, sheet_names, meta = load_dataset_safe(filepath)

        if meta.get("error"):
            print(f"    [!!] Scout: {meta['error']}")
        else:
            print(f"    [OK] Cabeceras detectadas: {len(header)} columna(s)")
            if sheet_names:
                print(f"         Pestañas Excel: {sheet_names}")

        # 2.2 · Construir informe para este archivo
        try:
            report_text = build_report_with_meta(
                filepath, header, n_cols, sheet_names, meta, glossary
            )
            all_reports.append(report_text)
        except Exception as exc:
            print(
                f"[!!] ERROR al generar el informe para '{filepath.name}': "
                f"{type(exc).__name__}: {exc}"
            )
            continue

    # 3 · Imprimir por consola -----------------------------------------------
    final_report = "\n\n".join(all_reports)

    # 3.1 · Bloque de observabilidad (a 5 m) basado en CSV procesado ----------
    # Esto responde a la pregunta operativa: "¿este año está bien muestreado o faltan meses?"
    try:
        cov = _try_build_sampling_coverage_block_processed_csv(PROCESSED_SIRENO_PATH)
    except Exception:
        cov = None
    if cov:
        final_report = final_report + "\n\n" + cov
    print()
    print(final_report)

    # 4 · Exportar a disco ---------------------------------------------------
    try:
        output_path = REPORTS_DIR / REPORT_NAME
        save_report(final_report, output_path)
        rel_path = output_path.relative_to(PROJECT_ROOT)
        print(f"\n[OK] Informe exportado : {rel_path}\n")
    except Exception as exc:
        print(f"[!!] ERROR al guardar el informe: {type(exc).__name__}: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()

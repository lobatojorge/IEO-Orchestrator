from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from ieo.io.base import ReadResult


_END_HEADER_MARKERS = ("*END*", "*END OF HEADER*")


@dataclass(frozen=True, slots=True)
class CnvHeader:
    """
    Cabecera mínima de un fichero CNV.

    Explicación práctica
    --------------------
    Un CNV mezcla texto (cabecera) y una tabla numérica (datos).
    Para poder usar Polars (lazy), primero localizamos dónde empieza la tabla
    y cómo se llaman las columnas.
    """

    column_names: list[str]
    data_start_line: int  # 0-based, primera línea de datos
    raw_time: str | None


def _parse_cnv_header(lines: list[str]) -> CnvHeader:
    # Sea-Bird típico:
    # - Columnas: líneas tipo "# name 0 = prDM: Pressure [db]"
    # - Data start: después de "*END*"
    name_re = re.compile(r"^\s*#\s*name\s+(?P<idx>\d+)\s*=\s*(?P<name>[^:]+)\s*:", re.IGNORECASE)
    time_re = re.compile(r"^\s*#\s*start_time\s*=\s*(?P<val>.+?)\s*$", re.IGNORECASE)

    names_by_idx: dict[int, str] = {}
    raw_time: str | None = None
    data_start = -1

    for i, line in enumerate(lines):
        s = line.strip()

        m_t = time_re.match(line)
        if m_t and raw_time is None:
            raw_time = m_t.group("val").strip()

        m = name_re.match(line)
        if m:
            idx = int(m.group("idx"))
            nm = m.group("name").strip()
            # Normalización mínima: sin espacios, lower
            nm = re.sub(r"\s+", "_", nm).strip().lower()
            names_by_idx[idx] = nm

        if any(marker in s for marker in _END_HEADER_MARKERS):
            data_start = i + 1
            break

    if data_start < 0:
        # Heurística: primera línea que parece "números números números"
        for i, line in enumerate(lines):
            if re.match(r"^\s*-?\d", line):
                data_start = i
                break
    if data_start < 0:
        raise ValueError("No se pudo localizar el inicio de datos en el CNV.")

    if not names_by_idx:
        # Sin nombres: asumimos columnas genéricas
        column_names = []
    else:
        column_names = [names_by_idx[i] for i in sorted(names_by_idx.keys())]

    return CnvHeader(column_names=column_names, data_start_line=data_start, raw_time=raw_time)


def _canonicalize_data_section_to_tsv(
    *,
    source: Path,
    staging_dir: Path,
    header: CnvHeader,
) -> Path:
    """
    Convierte la tabla del CNV a un TSV simple, estable y consumible por Polars.

    Explicación práctica
    --------------------
    Polars es excelente leyendo tablas. Un `.cnv` no es una tabla “pura”.
    Este paso crea un archivo intermedio *dentro de la corrida*.
    """

    staging_dir.mkdir(parents=True, exist_ok=True)
    out_path = staging_dir / f"{source.stem}.data.tsv"

    with source.open("rt", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    data_lines = lines[header.data_start_line :]
    # Filtrar líneas vacías/comentarios
    data_lines = [ln for ln in data_lines if ln.strip() and not ln.lstrip().startswith(("#", "*"))]

    if not data_lines:
        raise ValueError("CNV sin sección de datos parseable.")

    # La mayoría de CNV separa por espacios; normalizamos a tab.
    # Importante: no usamos pandas. Es un stream de texto simple.
    rows: list[str] = []
    for ln in data_lines:
        parts = re.split(r"\s+", ln.strip())
        rows.append("\t".join(parts))

    # Si no conocemos nombres, inferimos #cols de la primera fila.
    if header.column_names:
        names = header.column_names
    else:
        ncols = len(re.split(r"\s+", data_lines[0].strip()))
        names = [f"col_{i}" for i in range(ncols)]

    out_path.write_text("\t".join(names) + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return out_path


@dataclass(frozen=True, slots=True)
class CNVReader:
    """
    Lector de `.cnv` (Sea-Bird/SeaSave) que devuelve un LazyFrame.
    """

    def read(self, source: Path, *, staging_dir: Path) -> ReadResult:
        if not source.exists():
            raise FileNotFoundError(str(source))

        with source.open("rt", encoding="utf-8", errors="replace") as f:
            head = f.read().splitlines()

        header = _parse_cnv_header(head)
        tsv_path = _canonicalize_data_section_to_tsv(source=source, staging_dir=staging_dir, header=header)

        # Lazy ingestion: scan_csv (TSV) y casting deferido.
        lf = pl.scan_csv(
            str(tsv_path),
            separator="\t",
            has_header=True,
            infer_schema_length=200,
            ignore_errors=True,
            try_parse_dates=True,
        )

        notes: list[str] = [
            "CNV leído y normalizado a TSV para ingestión lazy.",
            f"data_start_line={header.data_start_line}",
            f"raw_time={header.raw_time or '-'}",
            f"staging_file={tsv_path.name}",
        ]
        return ReadResult(lazyframe=lf, source=source, notes=notes)


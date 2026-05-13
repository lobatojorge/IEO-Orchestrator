from __future__ import annotations

import argparse
import importlib.metadata
import sys
from pathlib import Path
from typing import Any

from ieo.io.cnv_reader import CNVReader
from ieo.io.excel_reader import ExcelReader
from ieo.io.netcdf_reader import NetCDFReader
from ieo.observability.anomaly import IsolationForestConfig, detect_anomalies_isolation_forest
from ieo.observability.quality_summary import build_health_summary
from ieo.reports.html_report import StepReport, write_step_report
from ieo.reports.logbook import write_logbook
from ieo.runtime.paths import RunPaths
from ieo.runtime.provenance import build_provenance, write_provenance_json
from ieo.runtime.run_id import new_run_id
from ieo.transform.pipeline import PipelineConfig, ReaderFactory, build_canonical_lazyframe, dataset_metrics, write_parquet
from ieo.validation.radial_contract import ViolationSeverity, validate_canonical_ctd_polars
from ieo.convert.seabird_hex_to_cnv import (
    ConvertResult,
    build_datcnvw_command_template,
    convert_cast_sets,
    discover_cast_sets,
    resolve_datcnv_psa,
    resolve_datcnvw_exe,
    xmlcon_sensor_hints,
)


def _detect_sources(project_root: Path) -> list[Path]:
    """
    Detecta fuentes de entrada para el pipeline.

    Nota práctica:
    - `data/cnv/` es el “punto de entrada” operativo del pipeline (fuentes procesables).
    - `data/raw/` se reserva como backup de ficheros crudos del CTD (no se procesa automáticamente).
    """

    allowed = (".cnv", ".xls", ".xlsx", ".nc", ".netcdf")
    dirs = [
        project_root / "data" / "cnv",
    ]

    sources: list[Path] = []
    for d in dirs:
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if not p.is_file():
                continue
            if p.suffix.lower() in allowed:
                sources.append(p)
    return sources


def run_pipeline(*, project_root: Path) -> int:
    run_id = new_run_id(prefix="pipeline")
    run_paths = RunPaths(project_root=project_root, run_id=run_id)
    run_paths.ensure()

    raw_dir = project_root / "data" / "raw"
    cnv_dir = project_root / "data" / "cnv"

    sources = _detect_sources(project_root)
    ingestion_errors: list[str] = []

    if not sources:
        report = StepReport(
            step_id="01_ingestion",
            title="01 · Ingesta (sin datos)",
            summary_lines=[
                "No se encontraron fuentes compatibles en `data/cnv/`.",
                "Copia ficheros `.cnv` (o `.xls/.xlsx` / `.nc`) en `data/cnv/` y reintenta.",
                "Nota: `data/raw/` se usa como backup (no se procesa automáticamente).",
            ],
            metrics={"raw_dir": str(raw_dir), "cnv_dir": str(cnv_dir), "n_sources": 0},
            errors=[],
        )
        write_step_report(out_dir=run_paths.checkpoints_dir, report=report)
        return 2

    # Paquetes/versiones para provenance
    packages: dict[str, str] = {}
    for pkg in ("polars", "scikit-learn", "python-calamine"):
        try:
            packages[pkg] = importlib.metadata.version(pkg)
        except Exception:
            packages[pkg] = "not-installed"

    parameters: dict[str, Any] = {
        "sources": [p.name for p in sources],
    }

    prov = build_provenance(run_id=run_id, input_files=sources, parameters=parameters, packages=packages)
    write_provenance_json(run_paths.run_root / "provenance.json", prov)

    # Lectores
    factory = ReaderFactory(cnv=CNVReader(), excel=ExcelReader(), netcdf=NetCDFReader())
    config = PipelineConfig()

    # Ingesta y artefacto canónico por cada source
    canonical_paths: list[Path] = []
    for src in sources:
        try:
            lf, notes = build_canonical_lazyframe(
                source=src,
                run_paths=run_paths,
                factory=factory,
                config=config,
                run_id=run_id.value,
            )
            out_parquet = run_paths.data_dir / f"{src.stem}.ctd_canonical.parquet"
            write_parquet(lf, out_parquet)
            canonical_paths.append(out_parquet)

            report = StepReport(
                step_id="01_ingestion",
                title="01 · Ingesta (artefactos canónicos)",
                summary_lines=[
                    f"Fuente detectada: {src.name}",
                    "Se normalizó a una tabla estable (staging) y se escribió Parquet canónico.",
                    *notes,
                ],
                metrics={"source": src.name, "canonical_parquet": str(out_parquet), **dataset_metrics(lf)},
                errors=[],
            )
            write_step_report(out_dir=run_paths.checkpoints_dir, report=report)
        except Exception as exc:
            msg = f"{src.name}: {type(exc).__name__}: {exc}"
            ingestion_errors.append(msg)
            report = StepReport(
                step_id="01_ingestion_skipped",
                title="01 · Ingesta (fuente omitida)",
                summary_lines=[
                    f"Fuente omitida: {src.name}",
                    "Motivo: no cumple el esquema mínimo CTD o no es legible.",
                    "El pipeline continúa con el resto de fuentes.",
                ],
                metrics={"source": src.name},
                errors=[msg],
            )
            write_step_report(out_dir=run_paths.checkpoints_dir, report=report)

    if not canonical_paths:
        report = StepReport(
            step_id="01_ingestion_errors",
            title="01 · Ingesta (sin fuentes válidas)",
            summary_lines=[
                "No se pudo generar ningún artefacto canónico CTD.",
                "Asegúrate de que en `data/raw/` hay `.cnv` o un Excel CTD válido.",
            ],
            metrics={"n_sources": len(sources), "n_processed": 0, "n_errors": len(ingestion_errors)},
            errors=ingestion_errors,
        )
        write_step_report(out_dir=run_paths.checkpoints_dir, report=report)
        return 1

    # ------------------------------------------------------------
    # Paso 02: Anomalías + registro inmutable (por cada canónico)
    # ------------------------------------------------------------
    try:
        import polars as pl
    except Exception as exc:  # pragma: no cover
        raise ImportError("Falta polars para ejecutar el pipeline.") from exc

    step_errors: list[str] = []
    anomaly_cfg = IsolationForestConfig(contamination=0.05, random_state=42, n_estimators=200)
    for can_path in canonical_paths:
        try:
            df_can = pl.read_parquet(can_path)
            # Detectar columna row_id (creada en canonical)
            row_id_col = config.schema.row_id
            if row_id_col not in df_can.columns:
                raise ValueError(f"Artefacto canónico sin `{row_id_col}`: {can_path.name}")

            cv = validate_canonical_ctd_polars(df_can)
            n_contract_err = sum(1 for v in cv if v.severity == ViolationSeverity.ERROR)
            contract_lines = [f"{v.severity.value}:{v.code}: {v.message}" for v in cv[:25]]
            contract_report = StepReport(
                step_id="01b_radial_contract",
                title="01b · Contrato radial (rangos físicos en perfil CTD)",
                summary_lines=[
                    "Validación sobre columnas canónicas (temperatura/profundidad por fila).",
                    f"Total violaciones: {len(cv)} (errores: {n_contract_err}).",
                    *contract_lines,
                ],
                metrics={
                    "input": can_path.name,
                    "n_violations": float(len(cv)),
                    "n_errors": float(n_contract_err),
                    "codes": [v.code for v in cv],
                },
                errors=[v.message for v in cv if v.severity == ViolationSeverity.ERROR],
            )
            write_step_report(out_dir=run_paths.checkpoints_dir, report=contract_report)
            if n_contract_err:
                print(
                    f"[ieo] Contrato radial: {n_contract_err} ERROR(s) en {can_path.name} "
                    "(ver checkpoints/01b_radial_contract*.html)",
                    file=sys.stderr,
                )

            outs = detect_anomalies_isolation_forest(
                df=df_can,
                row_id_col=row_id_col,
                config=anomaly_cfg,
                run_id=run_id.value,
                source_file=can_path.name,
            )

            clean_path = run_paths.data_dir / can_path.name.replace("ctd_canonical", "ctd_clean")
            anom_path = run_paths.data_dir / can_path.name.replace("ctd_canonical", "ctd_anomalies")
            audit_path = run_paths.data_dir / can_path.name.replace("ctd_canonical", "ctd_anomaly_audit")
            outs.clean.write_parquet(clean_path)
            outs.anomalies.write_parquet(anom_path)
            outs.audit_log.write_parquet(audit_path)

            # Paso 03: salud del dataset (resumen para no técnicos)
            health = build_health_summary(
                canonical=df_can,
                clean=outs.clean,
                anomalies=outs.anomalies,
                audit_log=outs.audit_log,
            )
            q_report = StepReport(
                step_id="03_quality",
                title="03 · Reporte de calidad (salud del dataset)",
                summary_lines=[
                    "Este resumen está pensado para entender rápidamente si el dataset está 'sano'.",
                    "Incluye cuántas filas entraron, cuántas se limpiaron y cuántas se marcaron como sospechosas.",
                ],
                metrics={"input": can_path.name, **health.metrics},
                errors=[],
            )
            write_step_report(out_dir=run_paths.checkpoints_dir, report=q_report)

            report = StepReport(
                step_id="02_anomalies",
                title="02 · Anomalías (Isolation Forest) + registro inmutable",
                summary_lines=[
                    f"Entrada canónica: {can_path.name}",
                    "Se ejecutó Isolation Forest con semilla fija (reproducible).",
                    "Se generaron 3 artefactos: clean, anomalies y audit_log (inmutable).",
                ],
                metrics={
                    "input": can_path.name,
                    "clean_parquet": str(clean_path),
                    "anomalies_parquet": str(anom_path),
                    "audit_parquet": str(audit_path),
                    **outs.metrics,
                },
                errors=[],
            )
            write_step_report(out_dir=run_paths.checkpoints_dir, report=report)
        except Exception as exc:
            step_errors.append(f"{can_path.name}: {type(exc).__name__}: {exc}")

    if step_errors:
        report = StepReport(
            step_id="02_anomalies_errors",
            title="02 · Anomalías (errores)",
            summary_lines=["Falló la detección de anomalías para uno o más artefactos canónicos."],
            metrics={"n_errors": len(step_errors)},
            errors=step_errors,
        )
        write_step_report(out_dir=run_paths.checkpoints_dir, report=report)
        logbook_path = write_logbook(run_root=run_paths.run_root, run_id=run_id.value)
        print(f"[ieo] Cuaderno de bitácora: {logbook_path}", file=sys.stderr)
        return 1

    logbook_path = write_logbook(run_root=run_paths.run_root, run_id=run_id.value)
    print(f"[ieo] Cuaderno de bitácora: {logbook_path}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ieo", description="Pipeline IEO (producción)")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Ejecuta pipeline sobre data/cnv/")
    run.add_argument("--project-root", type=str, default=str(Path(__file__).resolve().parents[2]))

    conv = sub.add_parser("convert-cnv", help="Convierte Sea-Bird .hex/.xmlcon a .cnv (salida en data/cnv/)")
    conv.add_argument("--project-root", type=str, default=str(Path(__file__).resolve().parents[2]))
    conv.add_argument(
        "--command-template",
        type=str,
        required=False,
        default=None,
        help=(
            "Plantilla del comando externo. Placeholders: {hex} {xmlcon} {hdr} {bl} {out} {out_dir} {out_name} {stem} "
            "{hex_name} {xmlcon_name}. Si se omite, se arma DatCnvW con /p apuntando a DatCnv.psa (batch sin GUI). "
            "Ejemplo manual: '\"C:\\\\...\\\\DatCnvW.exe\" /p \"C:\\\\...\\\\DatCnv.psa\" /i \"{hex}\" ... /s'"
        ),
    )
    conv.add_argument(
        "--psa-path",
        type=str,
        default=None,
        help="Ruta absoluta a DatCnv.psa (anula IEO_SEABIRD_DATCNV_PSA y tools/seabird/DatCnv.psa).",
    )
    conv.add_argument("--overwrite", action="store_true", help="Sobrescribe .cnv si ya existe.")
    conv.add_argument("--timeout-s", type=int, default=600, help="Timeout por cast (segundos).")
    conv.add_argument(
        "--dry-run",
        action="store_true",
        help="No ejecuta DatCnv: no crea .cnv (solo informe y salida por consola). Por defecto la conversión es real.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "run":
        return run_pipeline(project_root=Path(args.project_root).resolve())
    if args.cmd == "convert-cnv":
        project_root = Path(args.project_root).resolve()
        raw_dir = project_root / "data" / "raw"
        out_dir = project_root / "data" / "cnv"

        run_id = new_run_id(prefix="convert-cnv")
        run_paths = RunPaths(project_root=project_root, run_id=run_id)
        run_paths.ensure()

        casts = discover_cast_sets(raw_dir)

        if not casts:
            print(f"[ieo] Carpeta cruda: {raw_dir}", file=sys.stderr)
            print(
                "[ieo] No se encontró ningún cast: hace falta un par "
                "`<mismo_nombre>.hex` + `<mismo_nombre>.xmlcon` en la misma carpeta (también bajo subcarpetas de data/raw).",
                file=sys.stderr,
            )
            report = StepReport(
                step_id="00_convert_cnv",
                title="00 · Conversión a CNV (sin casts)",
                summary_lines=[
                    "No se encontraron conjuntos .hex + .xmlcon en `data/raw/`.",
                    "Copia los ficheros del CTD y reintenta.",
                ],
                metrics={"raw_dir": str(raw_dir), "out_dir": str(out_dir), "n_casts": 0},
                errors=[],
            )
            write_step_report(out_dir=run_paths.checkpoints_dir, report=report)
            return 2

        psa_override: Path | None = None
        if args.psa_path:
            psa_override = Path(str(args.psa_path)).expanduser().resolve()

        datcnv_meta: dict[str, Any] = {}
        if args.command_template:
            cmd_template = str(args.command_template)
        else:
            try:
                cmd_template = build_datcnvw_command_template(project_root, psa_override=psa_override)
            except FileNotFoundError as exc:
                print(f"[ieo] {exc}", file=sys.stderr)
                report = StepReport(
                    step_id="00_convert_cnv",
                    title="00 · Conversión a CNV (sin DatCnvW)",
                    summary_lines=[str(exc), "Define IEO_SEABIRD_DATCNVW o instala Sea-Bird SBE Data Processing."],
                    metrics={"raw_dir": str(raw_dir), "out_dir": str(out_dir)},
                    errors=[str(exc)],
                )
                write_step_report(out_dir=run_paths.checkpoints_dir, report=report)
                return 3
            except ValueError as exc:
                if args.dry_run:
                    print(f"[ieo] AVISO (dry-run): {exc}", file=sys.stderr)
                    print("[ieo] No se ejecutará DatCnv; solo informe de casts.", file=sys.stderr)
                    cmd_template = ""
                    datcnv_meta = {}
                else:
                    print(str(exc), file=sys.stderr)
                    report = StepReport(
                        step_id="00_convert_cnv",
                        title="00 · Conversión a CNV (sin DatCnv.psa)",
                        summary_lines=[str(exc)],
                        metrics={"raw_dir": str(raw_dir), "out_dir": str(out_dir)},
                        errors=[str(exc)],
                    )
                    write_step_report(out_dir=run_paths.checkpoints_dir, report=report)
                    return 4
            try:
                ex = resolve_datcnvw_exe()
                ps = resolve_datcnv_psa(project_root=project_root, exe=ex, psa_override=psa_override)
                datcnv_meta = {"datcnv_exe": str(ex), "datcnv_psa": str(ps) if ps else ""}
            except Exception:
                datcnv_meta = {}

        print(f"[ieo] Carpeta cruda: {raw_dir}", file=sys.stderr)
        print(f"[ieo] Salida .cnv: {out_dir}", file=sys.stderr)
        print(
            f"[ieo] Casts detectados: {len(casts)} (regla: mismo nombre base .hex + .xmlcon en la misma carpeta).",
            file=sys.stderr,
        )
        for c in casts:
            print(f"  · {c.stem}: {c.hex_path.name} + {c.xmlcon_path.name}", file=sys.stderr)
        hints0 = xmlcon_sensor_hints(casts[0].xmlcon_path)
        if hints0:
            tail = ", ".join(hints0[:14])
            if len(hints0) > 14:
                tail += ", …"
            print(
                f"[ieo] Sensores declarados en {casts[0].xmlcon_path.name} (referencia; las columnas CNV las fija el .psa): {tail}",
                file=sys.stderr,
            )
        if args.dry_run:
            print(
                "[ieo] ATENCIÓN: --dry-run activo: no se ejecutará DatCnvW.exe y no se escribirán .cnv.",
                file=sys.stderr,
            )
        else:
            print("[ieo] Ejecutando conversión real (DatCnvW).", file=sys.stderr)

        if args.dry_run and not cmd_template:
            results = [
                ConvertResult(
                    cast=c,
                    out_cnv=out_dir / f"{c.stem}.cnv",
                    ok=True,
                    message="Dry-run (sin DatCnv.psa: no se construyó comando).",
                    command=None,
                )
                for c in casts
            ]
        else:
            results = convert_cast_sets(
                casts,
                out_dir=out_dir,
                command_template=str(cmd_template),
                overwrite=bool(args.overwrite),
                timeout_s=int(args.timeout_s),
                dry_run=bool(args.dry_run),
            )

        ok = [r for r in results if r.ok]
        bad = [r for r in results if not r.ok]
        print(f"[ieo] Resumen: OK={len(ok)} · fallos={len(bad)}", file=sys.stderr)
        for r in bad:
            print(f"  [!!] {r.cast.stem}: {r.message}", file=sys.stderr)
        if not args.dry_run and not bad and ok:
            print("[ieo] Ficheros .cnv esperados en data/cnv/ (mismo nombre base que el .hex).", file=sys.stderr)

        summary = [
            f"Detectados {len(results)} casts (con .hex + .xmlcon).",
            f"Salidas en: {out_dir}",
            f"OK: {len(ok)} · Fallos: {len(bad)}",
            "Nota: este paso requiere un conversor Sea-Bird instalado (la plantilla de comando lo define).",
        ]
        errors = [f"{r.cast.stem}: {r.message}" for r in bad]
        metrics: dict[str, Any] = {
            "raw_dir": str(raw_dir),
            "out_dir": str(out_dir),
            "n_casts": len(results),
            "n_ok": len(ok),
            "n_fail": len(bad),
            "dry_run": bool(args.dry_run),
            "command_template": str(cmd_template)[:800],
            **(datcnv_meta if not args.command_template else {}),
        }
        # Pequeña tabla legible (sin volcar stdout completo)
        details = []
        for r in results[:50]:
            ts = r.cast.guess_timestamp_text() or "-"
            details.append(f"- {r.cast.stem}: ts={ts} · out={r.out_cnv.name} · {r.message}")
        report = StepReport(
            step_id="00_convert_cnv",
            title="00 · Conversión a CNV (Sea-Bird)",
            summary_lines=summary + ["", *details],
            metrics=metrics,
            errors=errors,
        )
        write_step_report(out_dir=run_paths.checkpoints_dir, report=report)
        logbook_path = write_logbook(run_root=run_paths.run_root, run_id=run_id.value)
        print(f"[ieo] Cuaderno de bitácora: {logbook_path}", file=sys.stderr)
        return 0 if not bad else 1
    raise RuntimeError("Comando no soportado")


if __name__ == "__main__":
    raise SystemExit(main())

